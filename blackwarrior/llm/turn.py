"""回合执行器 —— 一"轮"到底发生了什么。

一个回合 = 组装上下文 → 问模型 → （可能）调工具 → 拿结果再问 → 产出回复。

几个关键设计：

1. **工具循环有上限**（默认 4 轮）：防止模型陷入"调工具→不满意→再调"的死循环
   烧钱。达到上限时把已有结果收口成一个回复，而不是报错。
2. **每一步都发事件**：思考、增量文本、工具调用、工具结果都实时推给 UI，
   用户能看到"它在干什么"，而不是干等一个最终结果。
3. **abort 贯穿全程**：用户新消息到达时中止流式读取与工具循环。
4. **自动沉淀记忆**：回合结束后把这一轮写进长期记忆（可配置关闭）。
"""

from __future__ import annotations

import time
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

from ..context.assembler import ContextAssembler
from ..events import emit
from ..runtime.queue import Message
from ..tools.policy import ToolPolicy
from ..tools.registry import ToolRegistry, ToolResult
from .gateway import LLMError, LLMGateway


#: 自主心跳轮的标识。与 :meth:`Continuum._format_tick` 保持一致：
#: 文本以 ``[自主 TICK]`` 开头、label 为 ``自主 TICK``。
AUTO_TICK_LABEL = "自主 TICK"
AUTO_TICK_PREFIX = "[自主 TICK]"


class TurnResult:
    """一个回合的结果。"""

    def __init__(self, text: str = "", *, tool_calls: Optional[List[Dict[str, Any]]] = None,
                 rounds: int = 0, aborted: bool = False, error: str = "",
                 injected: Optional[Dict[str, Any]] = None) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.rounds = rounds
        self.aborted = aborted
        self.error = error
        self.injected = injected or {}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": self.tool_calls,
            "rounds": self.rounds,
            "aborted": self.aborted,
            "error": self.error,
        }


class TurnRunner:
    """回合执行器。"""

    def __init__(self, ctx: Any, gateway: Optional[LLMGateway] = None) -> None:
        self.ctx = ctx
        self.gateway = gateway or LLMGateway(ctx.config)
        self.policy = ToolPolicy(ctx.config)
        self.assembler = ContextAssembler(ctx)
        self.max_tool_rounds = 4

    # ------- 主流程 -----------------------------------------------

    def run(self, text: str, label: str = "", msg: Optional[Message] = None,
            abort: Optional[Event] = None) -> TurnResult:
        ctx = self.ctx
        turn_id = f"t{int(time.time() * 1000)}"
        is_auto = (label == AUTO_TICK_LABEL
                   or text.startswith(AUTO_TICK_PREFIX))

        # 离线 + 自主轮：直接静默。
        # 没有模型时"自主思考"没有意义，只会把 TICK 提示词本身写进记忆，
        # 污染后续所有召回（实测第一条记忆会变成 TICK 提示词原文）。
        if is_auto and not self.gateway.ready():
            emit("tick_skipped", {"turn_id": turn_id,
                                  "reason": "offline-no-model"})
            return TurnResult("", rounds=0)

        emit("turn_begin", {"turn_id": turn_id, "label": label,
                            "text": text[:200], "auto": is_auto})

        # 1) 世界模型：更新预测误差（影响心跳节奏与语气）
        try:
            affect_report = ctx.affect.observe_input(text)
            emit("prediction", {"turn_id": turn_id, **affect_report})
        except Exception:
            pass

        # 1b) PASM V2 十九层底座：感知这一步（黑武士 v0.3）
        # 放在世界模型之后、组装上下文之前：digest 既进上下文，也驱动 UI 心智页。
        try:
            bridge = getattr(ctx, "pasm2", None)
            if bridge is not None and getattr(bridge, "available", False):
                rpe = 0.0
                try:
                    rpe = float((affect_report or {}).get("error") or 0.0)
                except Exception:
                    rpe = 0.0
                digest = bridge.observe(text, rpe=rpe)
                if digest:
                    emit("pasm2_step", {"turn_id": turn_id, **digest})
        except Exception:
            pass

        # 2) 组装上下文
        try:
            built = self.assembler.build(text)
        except Exception as ex:
            return TurnResult("", error=f"上下文组装失败：{ex}")

        messages: List[Dict[str, Any]] = list(built["messages"])
        messages.append({"role": "user", "content": text})
        tools = built.get("tools") or []

        emit("context_built", {
            "turn_id": turn_id,
            "injected": built.get("injected", {}),
            "tools": [t["function"]["name"] for t in tools],
        })

        # 3) 判定模型是否就绪（不假装有 LLM）
        readiness = self.gateway.readiness()

        # 4) 生成（模型可用走流式+工具循环，不可用走内核降级回复）
        if readiness.get("ready"):
            result = self._generate_loop(messages, tools, turn_id, abort)
        else:
            result = self._offline_reply(text, turn_id, built, facts_hint=None)

        # 5) 落库 + 记忆沉淀（两条路径**都必须**走到这里）
        #    早期只有模型路径沉淀，导致离线对话"聊完什么都不记得"。
        self._persist(text, result, turn_id, msg, is_auto=is_auto)

        # 6) 情绪采样，让 UI 曲线动起来
        try:
            ctx.affect.sample_mood(label or "turn")
        except Exception:
            pass

        emit("turn_end", {"turn_id": turn_id, "rounds": result.rounds,
                          "chars": len(result.text), "aborted": result.aborted})
        return result

    # ------- 生成循环 ---------------------------------------------

    def _generate_loop(self, messages: List[Dict[str, Any]],
                       tools: List[Dict[str, Any]], turn_id: str,
                       abort: Optional[Event]) -> TurnResult:
        ctx = self.ctx
        collected: List[str] = []
        all_calls: List[Dict[str, Any]] = []
        rounds = 0
        error = ""

        while rounds < self.max_tool_rounds:
            rounds += 1
            if abort is not None and abort.is_set():
                return TurnResult("".join(collected), tool_calls=all_calls,
                                  rounds=rounds, aborted=True)

            pending_calls: List[Dict[str, Any]] = []
            finish_reason = ""
            try:
                for ev in self.gateway.stream(messages, tools=tools or None,
                                              abort=abort):
                    kind = ev.get("type")
                    if kind == "delta":
                        chunk = ev.get("text") or ""
                        collected.append(chunk)
                        emit("reply_delta", {"turn_id": turn_id, "text": chunk})
                    elif kind == "tool_calls":
                        pending_calls = ev.get("calls") or []
                    elif kind == "done":
                        finish_reason = str(ev.get("finish_reason") or "")
                    elif kind == "error":
                        if ev.get("aborted"):
                            return TurnResult("".join(collected),
                                              tool_calls=all_calls,
                                              rounds=rounds, aborted=True)
                        error = str(ev.get("error") or "")
                        if "429" in error or "rate" in error.lower():
                            try:
                                ctx.scheduler.mark_rate_limited(600.0)
                            except Exception:
                                pass
            except LLMError as ex:
                error = str(ex)
                if ex.rate_limited:
                    try:
                        ctx.scheduler.mark_rate_limited(ex.retry_after or 600.0)
                    except Exception:
                        pass
                break
            except Exception as ex:
                error = f"{type(ex).__name__}: {ex}"
                break

            if not pending_calls:
                break

            # 执行工具，把结果喂回模型
            all_calls.extend(pending_calls)
            tool_msgs = self._execute_tools(pending_calls, turn_id, abort)
            messages.extend(tool_msgs)

            if abort is not None and abort.is_set():
                return TurnResult("".join(collected), tool_calls=all_calls,
                                  rounds=rounds, aborted=True)
            if error:
                break

        text = "".join(collected).strip()
        if text:
            emit("reply", {"turn_id": turn_id, "text": text})
        return TurnResult(text, tool_calls=all_calls, rounds=rounds, error=error)

    def _execute_tools(self, calls: List[Dict[str, Any]], turn_id: str,
                       abort: Optional[Event]) -> List[Dict[str, Any]]:
        """执行一批工具调用，返回要追加的消息。"""
        ctx = self.ctx
        out: List[Dict[str, Any]] = []
        for call in calls:
            if abort is not None and abort.is_set():
                break
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            call_id = str(call.get("id") or "")

            emit("tool_call", {"turn_id": turn_id, "name": name, "args": args})

            spec = ctx.tools.get(name)
            if spec is None:
                result = ToolResult(False, name, error=f"未知工具：{name}")
            else:
                allowed, reason = self.policy.check(spec, args)
                # v0.3：PASM V2 安全层闸门（内核级，模型绕不过）
                # 放在应用层 policy 之后、真正执行之前——两道闸门职责不同：
                #   policy  管"这个工具允不允许用"（配置/风险/频率）
                #   V2 gate 管"这次调用越不越界"（白名单/限流/注入/伦理）
                if allowed:
                    allowed, reason, gate = self._pasm2_gate(name, args)
                    if gate:
                        emit("pasm2_gate", {"turn_id": turn_id, "name": name,
                                             "allowed": allowed,
                                             "reason": reason})
                if not allowed:
                    self.policy.deny(name)
                    result = ToolResult(False, name, blocked=True, reason=reason)
                else:
                    self.policy.record(name)
                    result = ctx.tools.call(name, args)

            emit("tool_result", {
                "turn_id": turn_id, "name": name,
                "ok": result.ok, "blocked": result.blocked,
                "result": _safe(result.result),
                "error": result.error or result.reason,
                "duration_ms": result.duration_ms,
            })

            try:
                ctx.store.log_action(
                    name, turn_id=turn_id, args=args, ok=result.ok,
                    summary=result.text(500), duration_ms=result.duration_ms)
            except Exception:
                pass

            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result.text(),
            })
        return out

    # ------- 降级回复 ---------------------------------------------

    def _pasm2_gate(self, name: str, args: Any
                    ) -> Tuple[bool, str, bool]:
        """PASM V2 安全层闸门。返回 ``(allowed, reason, gate_active)``。

        ``gate_active=False`` 表示 V2 没装或闸门关闭——此时**放行**，
        交给原有的应用层 policy 判。绝不能因为 V2 缺席就把工具全禁了
        （那会让"没装 V2"变成"什么都用不了"）。

        V2 在时它是**第二道闸**：白名单外的工具、探测式连试、提示注入、
        高风险动作都在这一层被拦下，且不依赖 LLM 自觉。
        """
        ctx = self.ctx
        try:
            if not bool(ctx.config.get("pasm2_tool_gate", True)):
                return True, "", False
        except Exception:
            return True, "", False
        bridge = getattr(ctx, "pasm2", None)
        if bridge is None or not getattr(bridge, "available", False):
            return True, "", False
        try:
            v = bridge.verify_tool(str(name), dict(args or {}))
        except Exception:
            return True, "", True      # V2 自己出错时不阻断主流程
        if not v:
            return True, "", True      # 无校验结果 = 无可校验，不当成拒绝
        if v.get("pass") is True:
            return True, "", True
        failed = ",".join(v.get("failed_layers") or []) or "safety"
        reason = (f"PASM V2 安全层拒绝（{failed}）："
                  f"{v.get('detail', {}).get('safety') or '未通过'}")
        return False, reason, True

    def _persist(self, text: str, result: TurnResult, turn_id: str,
                 msg: Optional[Message], *, is_auto: bool = False) -> None:
        """落库 + 记忆沉淀。所有回复路径共用。

        自主心跳轮用独立的低重要度类别（``自省``），不混进``对话``——
        否则用户问"我叫什么"时，召回落点可能是心跳提示词。
        """
        ctx = self.ctx
        channel = msg.channel if msg else "ui"
        from_id = msg.from_id if msg else "local"

        try:
            ctx.store.add_message("user", text, turn_id=turn_id,
                                  channel=channel, from_id=from_id)
        except Exception:
            pass
        if result.text:
            try:
                ctx.store.add_message("agent", result.text, turn_id=turn_id,
                                      channel=channel)
            except Exception:
                pass

        if ctx.config.get("auto_observe", True) and text and not result.aborted:
            try:
                if is_auto:
                    ctx.memory.remember(
                        f"自省：{text[:24]}", (result.text or "")[:300],
                        tags=["自省"], salience=1, category="自省",
                    )
                else:
                    ctx.memory.remember(
                        f"对话：{text[:24]}", text[:400],
                        tags=["对话"], salience=2, category="对话",
                    )
            except Exception:
                pass

    def _offline_reply(self, text: str, turn_id: str,
                       built: Dict[str, Any],
                       facts_hint: Optional[List[Dict[str, Any]]] = None
                       ) -> TurnResult:
        """模型不可用时的回复。

        这一步是黑武士相对纯 LLM Agent 的硬优势：**断网、没密钥也能对话**，
        而且回复里带着真实召回的记忆与当前情绪——因为记忆、情绪、性格
        都在本地内核里，不在模型里。
        """
        ctx = self.ctx
        facts: List[Dict[str, Any]] = list(facts_hint or [])
        if not facts:
            try:
                facts = ctx.memory.recall(text, k=3)
            except Exception:
                facts = []
        try:
            mood = float(ctx.kernel.mood)
        except Exception:
            mood = 0.0
        try:
            body = ctx.kernel.agent._render_reply(text, facts, mood)
        except Exception as ex:
            # 渲染失败也要说明原因，别静默换成一句没营养的兜底话
            body = f"（离线内核渲染失败：{type(ex).__name__}: {ex}）我收到了：{text[:80]}"

        note = ""
        try:
            r = self.gateway.readiness()
            if r.get("missing"):
                note = (f"\n\n— 当前未配置模型（缺少 {'、'.join(r['missing'])}），"
                        "以上回复来自本地认知内核。")
        except Exception:
            pass

        full = body + note
        emit("reply_delta", {"turn_id": turn_id, "text": full})
        emit("reply", {"turn_id": turn_id, "text": full, "offline": True})
        return TurnResult(full, rounds=0, injected=built.get("injected", {}))


def _safe(value: Any) -> Any:
    """把工具结果转成可 JSON 序列化的形式（给事件流用）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in list(value)[:20]]
    return str(value)[:500]
