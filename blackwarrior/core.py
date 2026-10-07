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
        self._sync_access_scope()

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

        # ---- 渠道桥（v0.6.2）----
        from .runtime.channels import ChannelBridge
        self.channels = ChannelBridge(self)

        # ---- MCP 外部工具生态（v0.7.0）----
        # 只建管理器（不连接）；连接在 start() 里后台做，避免拖慢启动。
        from .runtime.mcp_client import MCPManager
        self.mcp = MCPManager(self.config)

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

    def _sync_access_scope(self) -> None:
        """把配置里的「真实世界访问权」同步给 paths 模块。

        工具层（policy / filesystem）通过 ``paths.within_sandbox`` 判边界，
        不直接读 config，所以每次配置变更（含 grant_access 工具运行时改配置）
        都要在这里重新注入一次，否则改了不生效。
        """
        try:
            roots = self.config.get("allowed_paths", []) or []
            self.paths.set_allowed_roots(list(roots))
        except Exception:
            self.paths.set_allowed_roots([])
        try:
            self.paths.set_full_fs_access(
                bool(self.config.get("full_fs_access", False)))
        except Exception:
            self.paths.set_full_fs_access(False)

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
        # 渠道出站转发线程随主循环起停（v0.6.2）
        try:
            self.channels.start()
        except Exception:
            pass
        # MCP 服务器连接 + 工具挂载（v0.7.0，后台线程，不阻塞启动）
        try:
            self.mcp.start(reg=self.tools, background=True)
        except Exception:
            pass
        # 本地模型预热（v0.7.1）：冷启动实测 60s+，不预热的话第一条消息
        # 必然超时。放后台线程——预热期间服务照常可用，只是首轮回答稍慢。
        try:
            import threading as _th

            _th.Thread(target=self._warmup_model, name="bw-llm-warmup",
                       daemon=True).start()
        except Exception:
            pass

    def _warmup_model(self) -> None:
        """预热本地模型并把耗时如实记录（失败只记事件，不阻断启动）。"""
        try:
            rep = self.gateway.warmup()
        except Exception as ex:
            return
        try:
            emit("llm_warmup", rep)
            if rep.get("ok") and not rep.get("skipped"):
                emit("notice", {
                    "message": (f"本地模型已预热（{rep.get('cost_ms', 0) // 1000}s），"
                                "之后对话不会再卡在冷启动上")})
        except Exception:
            pass

    # ------------------------------------------------- 计划 / 执行 分离

    def plan_calls(self, text: str) -> Dict[str, Any]:
        """只推理、**不执行工具**，返回"打算做什么"。

        为什么要单独开这条路：免提模式需要**在动手之前**先把危险动作
        拦下来给人看。原路径 :meth:`ask` / :meth:`send` 是"想 → 直接
        执行 → 组织回答"，等它跑完，文件已经删了。所以贾维斯模式必须
        先拿计划、做完授权分流，再决定执行哪些。

        只跑**一轮**工具决策：拿到调用就返回。真正的回答组织交给
        :meth:`execute_calls` 之后的第二次调用——这样两次调用的职责
        清晰，授权边界也不会因为多轮工具而漏掉。
        """
        from threading import Event

        # assembler 挂在 turn_runner 上（见 TurnRunner.__init__），
        # 直接复用它，才能和正常对话走**完全同一套**上下文与工具筛选，
        # 不会出现"语音路径的工具集和文字路径不一样"这种怪事。
        ca = getattr(self.turn_runner, "assembler", None)
        tools = None
        msgs = None
        if ca is not None and hasattr(ca, "build"):
            built = ca.build(text)
            sys_prompt = ""
            if isinstance(built, dict):
                sys_prompt = built.get("system_prompt", "")
            else:
                sys_prompt = getattr(built, "system_prompt", "")
            msgs = [{"role": "system", "content": sys_prompt or
                     "你是黑武士，一个住在这台电脑里的助手。"},
                    {"role": "user", "content": text}]
            sel = []
            if isinstance(built, dict) and built.get("tools"):
                sel = built["tools"]
            elif hasattr(ca, "_select_tools"):
                sel = ca._select_tools(text)
            tools = None
            if sel:
                norm = []
                for t in sel:
                    if not isinstance(t, dict):
                        continue
                    fn = t.get("function") if "function" in t else t
                    name = fn.get("name")
                    if not name:
                        continue
                    norm.append({"type": "function", "function": {
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters",
                                             fn.get("input_schema", {})) or {},
                    }})
                tools = norm or None
        if not msgs:
            msgs = [{"role": "system", "content":
                     "你是黑武士，一个住在这台电脑里的助手。"},
                    {"role": "user", "content": text}]

        emit("jarvis_plan_begin", {"text": text[:200]})
        try:
            res = self.gateway.complete(msgs, tools=tools, temperature=0.3)
        except Exception as ex:
            return {"text": "", "calls": [], "error": str(ex)}
        calls = []
        for c in (res.get("tool_calls") or []):
            name = str(c.get("name") or "")
            args = c.get("args") or c.get("arguments") or {}
            if name:
                calls.append({"name": name, "args": args, "id": c.get("id", "")})
        emit("jarvis_plan_end", {"n_calls": len(calls),
                                 "calls": [c["name"] for c in calls]})
        return {"text": res.get("content", ""), "calls": calls,
                "error": res.get("error", "")}

    def execute_calls(self, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行一批**已获授权**的工具调用，返回 ``{summary, results}``。

        走的仍是 :meth:`TurnRunner._execute_tools`——也就是说 policy
        安全闸门与 PASM V2 闸门**一道都不会少**。贾维斯模式只是在外面
        多加了一道"要不要现在动手"的人工确认，不替代内核的任何检查。
        """
        from threading import Event

        if not calls:
            return {"summary": "", "results": []}
        emit("jarvis_execute_begin", {"n": len(calls),
                                      "names": [c.get("name") for c in calls]})
        tool_msgs = self.turn_runner._execute_tools(calls, "jarvis", Event())
        results = []
        for m in tool_msgs:
            body = str(m.get("content") or "")
            results.append(body[:500])
        summary = "；".join(r.strip() for r in results if r.strip())[:1200]
        emit("jarvis_execute_end", {"n": len(results)})
        return {"summary": summary, "results": results}
        # 恢复上次遗留的任务（转 paused，不自动继续）
        rec = self.recover_tasks()
        if rec.get("recovered"):
            emit("notice", {"message": rec.get("note", "")})

    def stop(self) -> None:
        """停止主循环。"""
        self.continuum.stop()
        try:
            self.channels.stop()
        except Exception:
            pass
        self._auto_started = False

    def close(self) -> None:
        """优雅退出：停循环 → 保存认知状态 → 关库。"""
        try:
            self.stop()
        except Exception:
            pass
        # MCP 子进程必须回收，否则重启电脑后残留一堆孤儿 python/node 进程
        try:
            self.mcp.stop_all()
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
                        dedupe_key: str = "",
                        channel: str = "background") -> Dict[str, Any]:
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
            channel=str(channel or "background"), dedupe_key=dedupe_key)
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

    def greet_once(self) -> Dict[str, Any]:
        """UI 首次接入时主动打招呼（每个 serve 会话只一次）。

        - 画像里有名字 →「你好，{名字}」；没有 →「你好，主人」。
        - 模型就绪：走主循环让模型自己组织问候（画像已注入上下文）；
          模型不可用：直接发离线问候，绝不装作有 LLM。
        - 前端对背景回复也会朗读（v0.6.6），所以问候有声音。
        """
        if getattr(self, "_greeted", False):
            return {"ok": False, "reason": "本会话已打过招呼"}
        self._greeted = True
        name = ""
        try:
            row = self.profile.get("name")
            name = str((row or {}).get("value") or "").strip()
        except Exception:
            pass
        ready = False
        try:
            ready = bool(self.gateway.readiness().get("ready"))
        except Exception:
            pass
        if ready:
            self.push_background(
                "[系统问候] 用户刚刚打开界面。请用一句话主动打招呼："
                + (f"画像里记得用户叫「{name}」，就说「你好，{name}」。"
                   if name else
                   "你还不认识用户（画像里没有名字），就说「你好，主人」。")
                + "可以顺带一句问今天需要什么，不要复述本提示，不要调工具。",
                source="greeting")
            return {"ok": True, "mode": "llm", "name": name or None}
        emit("reply", {"text": f"你好，{name}。我在。" if name else "你好，主人。我在。"})
        return {"ok": True, "mode": "offline", "name": name or None}

    def ask(self, text: str, *, timeout: float = 120.0,
            channel: str = "ui") -> TurnResult:
        """同步问答（CLI / 测试 / 单轮 API 用）。

        注意：正常交互走 :meth:`send`（异步、可打断）。
        这里直接跑一个回合，不经过主循环队列。

        ★ ``timeout`` 以前是**收下不用**的死参数——调用方（HTTP /api/ask）
        传什么都没区别，超时由网关配置说了算。本地模型冷启动实测 68s，
        调用方想给 300s 也没用，只能看着 TimeoutError。现在真正生效：
        到点自动中止，并如实说明是被超时掐断的。
        """
        from threading import Event, Timer

        budget = float(timeout or 0) or 0.0
        abort = Event()
        timer = None
        if budget > 0:
            timer = Timer(budget, abort.set)
            timer.daemon = True
            timer.start()
        try:
            result = self.turn_runner.run(text, "sync ask", None, abort)
        finally:
            if timer is not None:
                timer.cancel()
        if abort.is_set() and not (result.aborted or result.text):
            result.error = (f"已超过 {budget:.0f}s 未完成，已中止"
                            "（本地模型偏慢时可调大 timeout，"
                            "或改用云端模型）")
        return result

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
            memory_stats = self.memory.stats()
        except Exception:
            memory_stats = {}
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
                # v0.7.0：真实世界访问权（让 UI 能显示"我能碰哪些目录"）
                "access": {
                    "sandbox": str(self.paths.sandbox_dir()),
                    "allowed_paths": [str(p) for p in self.paths.allowed_roots()],
                    "full_fs_access": bool(self.paths.full_fs_access()),
                },
            }
        except Exception:
            panorama = {}

        # 心跳缩放系数由情绪世界模型给出，UI 的"心跳节律"面板直接读它。
        # 放在 core 层注入，因为 affect 是 core 的组件、内核本身并不知道它。
        try:
            cog["tick_scale"] = round(float(self.affect.suggest_tick_scale()), 3)
        except Exception:
            cog["tick_scale"] = 1.0

        # PASM V2 认知底座可用且启用时，引擎/档位标签统一显示为 pasm2
        # （V2 的十九层认知才是当前活跃引擎，旧内核写死的 "pasm" 标签会误导用户）。
        try:
            p2 = self.pasm2.status() if getattr(self, "pasm2", None) else {}
            if p2.get("available"):
                cog["engine"] = "pasm2"
                memory_stats["engine"] = "pasm2"
                prof = str(p2.get("profile") or "")
                if prof:
                    cog["tier"] = prof
                    memory_stats["tier"] = prof
        except Exception:
            pass

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

        # v0.6.1：任务概览（复盘补缺口——任务在 /status 不可见，
        # 只能去 /api/tasks 单独查；状态页/头部徽标都要用这个）。
        try:
            active = self.tasks.active_task()
            counts: Dict[str, int] = {}
            for row in self.tasks.list_tasks(limit=100):
                s = str(row.get("state") or "unknown")
                counts[s] = counts.get(s, 0) + 1
            tasks_block = {
                "active": active,
                "counts": counts,
                "active_id": (active or {}).get("id"),
            }
        except Exception:
            tasks_block = {"active": None, "counts": {}, "active_id": None}

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
            "memory": memory_stats,
            "cognition": cog,
            "affect": aff,
            "policy": self.policy.stats(),
            "panorama": panorama,
            "pasm2": self.pasm2.status() if self.pasm2 else {},
            "tasks": tasks_block,
            "channels": (self.channels.stats()
                         if getattr(self, "channels", None) else {}),
            "mcp": (self.mcp.status() if getattr(self, "mcp", None) else {}),
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
        # 访问权变更必须立刻生效：paths 模块的允许区是工具层的判据
        if any(k in patch for k in ("allowed_paths", "full_fs_access")):
            self._sync_access_scope()
        # provider/base_url/key 变动后，网关读的是 config，无需重建；
        # 但工具开关变化需要重新注册工具集。
        if any(k in patch for k in ("shell_enabled", "web_enabled", "tools_enabled")):
            self._rebuild_tools()
        return self.config.public_dict()

    def _rebuild_tools(self) -> None:
        """按当前配置重建工具集（关闭的能力直接不注册）。"""
        self.tools = ToolRegistry()
        self._registered_tools = register_builtin(self.tools, self, self.config)
        # MCP 工具不在内置集里，重建后必须重新挂一遍，否则用户改个工具开关
        # 就会发现"接的 MCP 服务器全没了"。
        try:
            self._registered_tools += list(self.mcp.register_all(self.tools))
        except Exception:
            pass
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
