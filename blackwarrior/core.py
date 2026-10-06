"""黑武士主控 —— 把所有层串成一个活的系统。

``WarriorCore`` 既是系统门面（给 HTTP 服务与 Electron 壳用），
也是工具注册的上下文对象（工具通过 ``ctx.xxx`` 访问各层）。

启动顺序（有依赖关系，不能乱）::

    config → paths → store → kernel(PASM) → memory → affect
        → scheduler → queue → tools → gateway → turn_runner → continuum
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import paths as paths_mod
from .brain import AffectTracker, CognitiveKernel, MemoryService
from .voice import build_voice_service
from .config import Config, load_config
from .db import Store
from .events import emit
from .llm.gateway import LLMGateway
from .llm.turn import TurnRunner, TurnResult
from .runtime import (
    Continuum,
    Message,
    MessageQueue,
    PRIORITY_BACKGROUND,
    PRIORITY_USER,
    Scheduler,
)
from .tools import ToolPolicy, ToolRegistry
from .tools.builtin import register_builtin
from .version import version_info


class WarriorCore:
    """黑武士主控。"""

    def __init__(self, config: Optional[Config] = None, *,
                 data_root: Optional[str] = None,
                 auto_start: bool = False) -> None:
        if data_root:
            import os

            os.environ.setdefault("BLACKWARRIOR_DATA_ROOT", str(data_root))

        self.config = config or load_config()
        self.paths = paths_mod                    # 工具层通过 ctx.paths 访问
        self.started_at = time.time()

        # ---- 存储 ----
        self.store = Store()

        # ---- 认知内核 ----
        self.kernel = CognitiveKernel(
            self.config,
            enabled=bool(self.config.get("cognition_enabled", True)),
        )
        self.memory = MemoryService(self.kernel, self.store)
        self.affect = AffectTracker(self.kernel)

        # ---- 调度 ----
        self.scheduler = Scheduler(self.config)
        self.queue = MessageQueue()

        # ---- 工具 ----
        self.tools = ToolRegistry()
        self.policy = ToolPolicy(self.config)
        self._registered_tools = register_builtin(self.tools, self, self.config)

        # ---- v0.3：PASM V2 十九层认知底座 ----
        # 必须在工具注册之后构造：V2 安全层的工具白名单要拿真实工具名，
        # 否则空 allowlist = 全拒（pasm2 的保守起点），会把正常工具全拦掉。
        self.pasm2 = self._build_pasm2()

        # ---- 模型 ----
        self.gateway = LLMGateway(self.config)
        self.turn_runner = TurnRunner(self, self.gateway)

        # ---- 语音（可选能力，未配置时自动降级）----
        self.voice = build_voice_service(self.config)

        # ---- v0.2：用户画像 / 信息面板 / 预取缓存 ----
        from .brain.profile import UserProfile
        from .runtime.panels import PanelManager
        from .runtime.prefetch import PrefetchCache
        self.profile = UserProfile(self.store, self.config, self.gateway)
        self.panels = PanelManager(self.config, self.store)
        self.prefetch = PrefetchCache(self.config, self.store)

        # ---- 本地媒体库（v0.5）----
        from .media.library import MediaLibrary
        self.media = MediaLibrary(self.store, self.config)

        # ---- 任务续跑引擎（v0.6）----
        from .runtime.tasks import TaskEngine
        self.tasks = TaskEngine(self.store, self.config)

        # ---- 主循环 ----
        self.continuum = Continuum(
            run_turn=self._run_turn,
            config=self.config,
            queue=self.queue,
            scheduler=self.scheduler,
            hooks={
                "enqueue_due_reminders": self._enqueue_due_reminders,
                "next_reminder": self._next_reminder_at,
                "has_active_task": self._has_active_task,
                "tick_scale": self._tick_scale,
            },
        )

        self._consolidate_counter = 0
        self._auto_started = False
        if auto_start:
            self.start()

    # ------- v0.3：PASM V2 桥接 -----------------------------------

    def _build_pasm2(self) -> Any:
        """按配置构建 PASM V2 桥接层。

        V2 是**可选增强**（``pip install pasm2`` + numpy）。装了就用真底座，
        没装就保持 V1 引擎 / 内置降级，并在 ``/status`` 与「心智」页
        **明确标注原因**——不把降级伪装成正常。
        """
        from .brain.pasm2_bridge import build_bridge

        try:
            profile = str(self.config.get("pasm2_profile", "full") or "full")
        except Exception:
            profile = "full"
        try:
            enabled = bool(self.config.get("pasm2_enabled", True))
        except Exception:
            enabled = True
        if not enabled:
            from .brain.pasm2_bridge import Pasm2Bridge
            b = Pasm2Bridge(profile)
            b.reason = "pasm2_enabled=False（配置关闭）"
            return b
        # 把真实工具名交给 V2 安全层（构造期就要给，事后补会触发"全拒"）
        names = []
        try:
            names = [s.name for s in self.tools.specs()]
        except Exception:
            names = []
        try:
            return build_bridge(profile, tool_allowlist=names,
                                data_root=str(self.paths.data_root()))
        except Exception as ex:      # pragma: no cover - 兜底
            from .brain.pasm2_bridge import Pasm2Bridge
            b = Pasm2Bridge(profile)
            b.reason = f"PASM V2 桥接层构建异常，已降级：{type(ex).__name__}: {ex}"
            return b

    # ------- 生命周期 ---------------------------------------------

    def start(self) -> None:
        """启动主循环（可重复调用 —— 幂等）。"""
        #★ 这里原先是 `if self._auto_started: return` 的"一次性"语义，
        #   而 cli/electron 启动时已经调过一次 start()（把 _auto_started 置
        #   True）。于是 UI 上点「开启」永远被这个 return 拦掉——
        #   接口还返回 {"ok": true, "running": false}，看起来"调用成功"
        #   但主循环根本没起，status 一直显示已停止。
        #   2026-10-06 用户报「主循环停止后开启不了」即此因。
        #
        # 正确语义：start/stop 本就该可反复调用，由 continuum 自己判断
        # 是否已在运行；core 这层只做转发。
        if self.is_running():
            return
        self._auto_started = True
        self.continuum.start(run_immediate=True)
        emit("core_started", {"version": version_info()})
        # 恢复上次遗留的任务（转 paused，不自动继续）
        rec = self.recover_tasks()
        if rec.get("recovered"):
            emit("notice", {"message": rec.get("note", "")})

    def stop(self) -> None:
        """停止主循环。"""
        self.continuum.stop()
        self._auto_started = False

    def close(self) -> None:
        """优雅退出：停循环 → 保存认知状态 → 关库。"""
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.kernel.save()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        emit("core_stopped", {"ts": time.time()})

    def is_running(self) -> bool:
        return self.continuum.is_running()

    # ------- 消息入口 ---------------------------------------------

    def send(self, text: str, *, priority: int = PRIORITY_USER,
             from_id: str = "local", channel: str = "ui",
             meta: Optional[Dict[str, Any]] = None,
             dedupe_key: str = "") -> Optional[Message]:
        """把一条用户消息交给主循环（异步，不阻塞）。"""
        return self.continuum.push(
            text, priority=priority, from_id=from_id, channel=channel,
            meta=meta, dedupe_key=dedupe_key,
        )

    # ------- 后台消息（v0.6）---------------------------------------

    def push_background(self, text: str, *, source: str = "system",
                        priority: int = PRIORITY_BACKGROUND,
                        dedupe_key: str = "") -> Dict[str, Any]:
        """投一条**后台消息**进队列（不打断用户对话）。

        这是"外部世界 → 黑武士"的统一入口：渠道接入（微信/Discord/钉钉）、
        定时任务、任务续跑、Webhook 回调，全都走这一条路。好处是
        外部消息与用户消息**共用同一个主循环**（同一份记忆、同一套认知状态），
        不会各自为政。

        优先级低于用户消息，所以用户正在说话时后台消息会排队而不是插嘴。
        """
        if not str(text or "").strip():
            return {"ok": False, "reason": "消息内容为空"}
        msg = self.continuum.push(
            text, priority=priority, from_id=str(source or "system"),
            channel="background", dedupe_key=dedupe_key)
        if msg is None:
            # 被去重拦截也算成功：避免同一来源的重复推送刷屏
            return {"ok": True, "queued": False,
                    "reason": "被去重拦截（相同 dedupe_key 已在队列中）"}
        from .events import emit

        emit("background_pushed", {"from": msg.from_id, "chars": len(text)})
        return {"ok": True, "queued": True, "priority": msg.priority,
                "lane": msg.lane, "from": msg.from_id}

    def recover_tasks(self) -> Dict[str, Any]:
        """启动时恢复任务（把上次遗留的 active 转为paused）。"""
        try:
            return self.tasks.recover()
        except Exception as ex:
            return {"recovered": 0,
                    "error": f"{type(ex).__name__}: {ex}"}

    def ask(self, text: str, *, timeout: float = 120.0,
            channel: str = "ui") -> TurnResult:
        """同步问答（CLI / 测试 / 单轮 API 用）。

        注意：正常交互走 :meth:`send`（异步、可打断）。
        这里直接跑一个回合，不经过主循环队列。
        """
        from threading import Event

        return self.turn_runner.run(text, "sync ask", None, Event())

    # ------- 主循环回调 -------------------------------------------

    def _run_turn(self, text: str, label: str, msg: Optional[Message],
                  abort: Any) -> TurnResult:
        """主循环每一轮的实际执行体。"""
        if abort is not None and abort.is_set():
            return TurnResult("", aborted=True)

        result = self.turn_runner.run(text, label, msg, abort)

        # 心跳期做记忆巩固（"睡眠回放"）
        try:
            every = int(self.config.get("consolidate_every", 20) or 20)
        except Exception:
            every = 20
        if every > 0:
            self._consolidate_counter += 1
            if self._consolidate_counter >= every:
                self._consolidate_counter = 0
                try:
                    rep = self.memory.consolidate(apply=True)
                    emit("consolidated", rep)
                except Exception:
                    pass
                # v0.2：心跳顺便刷新到期预取（网络失败保留旧值，不阻断）
                try:
                    self._refresh_ambient()
                except Exception:
                    pass

        # 定期保存认知侧车
        if self._consolidate_counter % 5 == 0:
            try:
                self.kernel.save()
            except Exception:
                pass
        return result

    def _refresh_ambient(self) -> None:
        """心跳期的"环境预热"：刷新到期预取（网络失败保留旧值）。

        放在 core 层而不是让工具循环去撞网络——心跳节奏由调度器控制，
        预取失败也不该拖慢任何一轮对话。
        """
        try:
            self.prefetch.refresh_due()
        except Exception:
            pass

    def _enqueue_due_reminders(self) -> None:
        """把到期的提醒塞进队列。"""
        try:
            due = self.store.due_reminders()
        except Exception:
            return
        for r in due:
            text = f"[提醒] {r.get('title', '')} {r.get('body', '')}".strip()
            self.queue.push(text, priority=80, lane="user",
                            from_id="reminder", channel=r.get("channel", "ui"),
                            dedupe_key=f"reminder-{r.get('id')}")
            try:
                self.store.fire_reminder(int(r["id"]))
            except Exception:
                pass

    def _next_reminder_at(self) -> Optional[float]:
        try:
            r = self.store.next_reminder()
            return float(r["due_at"]) if r else None
        except Exception:
            return None

    def _has_active_task(self) -> bool:
        try:
            return len(self.store.active_tasks()) > 0
        except Exception:
            return False

    def _tick_scale(self) -> float:
        try:
            base = float(self.affect.suggest_tick_scale())
        except Exception:
            base = 1.0
        # 有活跃任务时整体再快一档（v0.6）：多步骤目标要在合理时间内
        # 推进完，不能等下一个空闲周期。任务引擎给的系数是"建议"，
        # 与情绪给的系数相乘 —— 两者都只是节奏，不改变任何行为语义。
        try:
            return float(self.tasks.tick_scale_hint(base))
        except Exception:
            return base

    # ------- 状态 -------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """完整运行状态（``/status`` 与 UI 状态面板的数据源）。"""
        try:
            db_stats = self.store.stats()
        except Exception:
            db_stats = {}
        try:
            cog = self.kernel.snapshot()
        except Exception:
            cog = {}
        try:
            aff = self.affect.snapshot()
        except Exception:
            aff = {}

        # v0.2：用户画像 / 信息面板 / 预取缓存 汇总（诚实标注 availability）
        try:
            panorama = {
                "profile": self.profile.as_dict(),
                "panels": self.panels.as_dict(),
                "prefetch": {
                    "count": len(self.prefetch.list()),
                    "enabled": bool(self.config.get("prefetch_enabled", False)),
                },
                "media": self.media.stats(),
            }
        except Exception:
            panorama = {}

        # 心跳缩放系数由情绪世界模型给出，UI 的"心跳节律"面板直接读它。
        # 放在 core 层注入，因为 affect 是 core 的组件、内核本身并不知道它。
        try:
            cog["tick_scale"] = round(float(self.affect.suggest_tick_scale()), 3)
        except Exception:
            cog["tick_scale"] = 1.0

        # 补齐 UI 直接消费的两个字段。
        # 放在这里而不是让前端去猜嵌套结构——前端改一次后端改一次，迟早对不上。
        loop = self.continuum.status()
        try:
            loop["queue_size"] = int((loop.get("queue") or {}).get("total") or 0)
        except Exception:
            loop["queue_size"] = 0
        try:
            last = ((loop.get("scheduler") or {}).get("last") or {})
            loop["next_tick_in"] = last.get("interval")
            loop["next_reason"] = last.get("reason") or ""
        except Exception:
            loop["next_tick_in"] = None

        return {
            "version": version_info(),
            "running": self.is_running(),
            "uptime": round(time.time() - self.started_at, 1),
            "activated": bool(self.config.is_activated()),
            "loop": loop,
            "data_dir": str(self.paths.data_root()),
            "db": db_stats,
            "tools": self.tools.stats(),
            "llm": self.gateway.stats(),
            "memory": self.memory.stats(),
            "cognition": cog,
            "affect": aff,
            "policy": self.policy.stats(),
            "panorama": panorama,
            "pasm2": self.pasm2.status() if self.pasm2 else {},
        }

    def summary(self) -> Dict[str, Any]:
        """精简状态（高频轮询用，省带宽）。"""
        return {
            "running": self.is_running(),
            "processing": self.continuum.is_processing(),
            "queue": self.queue.snapshot(),
            "mood": round(float(self.kernel.mood), 3),
            "memories": self.memory.stats().get("count", 0),
            "tier": self.kernel.tier,
        }

    # ------- 记忆操作（供 API）------------------------------------

    def list_memories(self, limit: int = 100, category: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
        return self.memory.list(limit=limit, category=category)

    def add_memory(self, title: str, brief: str = "", **kw: Any) -> Dict[str, Any]:
        return self.memory.remember(title, brief, **kw)

    def delete_memory(self, mem_id: int) -> bool:
        return self.memory.delete(mem_id)

    def clear_memories(self) -> int:
        return self.memory.clear()

    # ------- 提醒（供 API）----------------------------------------

    def add_reminder(self, title: str, due_at: float, body: str = "") -> int:
        return self.store.add_reminder(title, due_at, body=body)

    def list_reminders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.list_reminders(status=status)

    def cancel_reminder(self, rem_id: int) -> None:
        self.store.cancel_reminder(rem_id)

    # ------- 配置 -------------------------------------------------

    def save_config(self) -> None:
        self.config.save()

    def public_config(self) -> Dict[str, Any]:
        return self.config.public_dict()

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """更新配置。改模型相关项后需要重建 gateway 才能生效。"""
        self.config.update(patch)
        self.config.save()
        # provider/base_url/key 变动后，网关读的是 config，无需重建；
        # 但工具开关变化需要重新注册工具集。
        if any(k in patch for k in ("shell_enabled", "web_enabled", "tools_enabled")):
            self._rebuild_tools()
        return self.config.public_dict()

    def _rebuild_tools(self) -> None:
        """按当前配置重建工具集（关闭的能力直接不注册）。"""
        self.tools = ToolRegistry()
        self._registered_tools = register_builtin(self.tools, self, self.config)
        # 工具集变了，V2 安全层白名单必须跟着变——否则新开的工具会被
        # 安全层判成"不在白名单"而全拒，或者已关的工具还在白名单里漏过。
        try:
            if self.pasm2 is not None and getattr(self.pasm2, "available", False):
                names = [s.name for s in self.tools.specs()]
                self.pasm2.set_tool_allowlist(names)
        except Exception:
            pass
        emit("tools_reloaded", {"count": len(self._registered_tools)})

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<WarriorCore running={self.is_running()} "
                f"tier={self.kernel.tier} tools={len(self.tools)}>")
