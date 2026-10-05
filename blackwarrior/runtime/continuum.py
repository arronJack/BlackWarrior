"""持续运行主循环（Continuum Loop）。

黑武士不是"问一句答一句"的程序，而是一个**一直醒着**的进程：
有消息优先处理消息，空闲时按节奏自主整理记忆、检查提醒、复盘任务。

这是从同类项目（BaiLongma 的 consciousness-loop）借鉴的**架构模式**，
但在 Python 侧重新实现，并补了三处它们没做的东西：

1. **看门狗 + 抢占的 abort 是真传给执行体的**
   （``abort_event`` 由主循环创建并交给回合执行器，LLM 流式读取时会真的中断，
   而不是"标记一下但请求还在跑"）。
2. **预测误差参与节奏**（见 :mod:`.scheduler`）。
3. **每一轮都留下轨迹**（turn trace），便于事后回答"它刚才为什么这么做"。

调度优先级
----------
见 :meth:`Scheduler.decide`。主循环只负责"按决策醒来 + 安全执行"。

健壮性三条铁律
--------------
1. 单轮异常**绝不能**让主循环停摆（``try/finally`` 保证重新排程）；
2. 回合卡死**必须**能被看门狗打断（超时 abort，防止永久占用）；
3. 高优先级消息到达**必须**能抢占正在进行的低优先级回合。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..events import emit
from .queue import Message, MessageQueue
from .scheduler import Scheduler, MAX_SLEEP_SECONDS


@dataclass
class Execution:
    """当前正在执行的一轮。"""

    label: str
    priority: int
    started_at: float
    abort: threading.Event = field(default_factory=threading.Event)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "priority": self.priority,
            "elapsed": round(self.elapsed(), 2),
            "aborted": self.abort.is_set(),
        }


class Continuum:
    """持续运行主循环。

    参数
    ----
    run_turn : callable
        ``run_turn(text, label, msg, abort_event)``，执行一轮。
        实现方**应当**定期检查 ``abort_event`` 并在被设置时尽快退出。
    config : Config
    queue : MessageQueue
    scheduler : Scheduler
    hooks : dict
        可选回调：``on_due_reminders`` → 把到期提醒塞进队列。
    """

    def __init__(
        self,
        run_turn: Callable[[str, str, Optional[Message], threading.Event], Any],
        config: Any,
        queue: MessageQueue,
        scheduler: Scheduler,
        *,
        hooks: Optional[Dict[str, Callable[..., Any]]] = None,
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._run_turn = run_turn
        self.config = config
        self.queue = queue
        self.scheduler = scheduler
        self.hooks = hooks or {}
        self._emit = event_sink or (lambda t, p: emit(t, **p))

        self._running = False
        self._processing = False
        self._timer: Optional[threading.Timer] = None
        self._current: Optional[Execution] = None
        self._lock = threading.RLock()

        self._awakening_ticks = 0
        self._last_tick_aborted = False
        self._tick_count = 0
        self._started_at: Optional[float] = None
        self._loop_started = False

    # ------- 生命周期 ---------------------------------------------

    def start(self, run_immediate: bool = True) -> None:
        """启动主循环。"""
        with self._lock:
            if self._loop_started:
                return
            self._loop_started = True
            self._running = True
            self._started_at = time.time()
            try:
                self._awakening_ticks = int(
                    self.config.get("awakening_ticks", 3) or 0)
            except Exception:
                self._awakening_ticks = 0

        self._emit("loop_started", {"ts": time.time()})

        if run_immediate and (self._heartbeat_enabled()
                              or self._awakening_ticks > 0
                              or self.queue.has_messages()):
            self._on_tick()
        self._schedule_next()

    def stop(self) -> None:
        """停止主循环。"""
        with self._lock:
            self._running = False
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None
        self._emit("loop_stopped", {"ts": time.time()})

    def is_running(self) -> bool:
        return self._running

    def is_processing(self) -> bool:
        return self._processing

    # ------- 消息入口 ---------------------------------------------

    def push(self, text: str, *, priority: int = 100, lane: Optional[str] = None,
             from_id: str = "local", channel: str = "ui",
             meta: Optional[Dict[str, Any]] = None,
             dedupe_key: str = "") -> Optional[Message]:
        """入队一条消息，并按需抢占当前回合。"""
        msg = self.queue.push(
            text, priority=priority, lane=lane, from_id=from_id,
            channel=channel, meta=meta, dedupe_key=dedupe_key,
        )
        if msg is None:
            return None  # 被去重
        self._emit("message_queued", {"message": msg.as_dict()})
        self._interrupt(msg)
        return msg

    def _interrupt(self, msg: Message) -> None:
        """高优先级消息到达：中断当前回合 + 立即触发下一轮。"""
        with self._lock:
            current = self._current
            if current is not None and self._should_preempt(msg, current):
                current.abort.set()
                self._emit("processing_preempted", {
                    "by": msg.from_id,
                    "lane": msg.lane,
                    "priority": msg.priority,
                    "current": current.as_dict(),
                })
        self.trigger_immediate_tick()

    def _should_preempt(self, incoming: Message, current: Execution) -> bool:
        """抢占判定。"""
        if not self._processing:
            return True
        if incoming.priority > current.priority:
            return True
        # 并发的用户消息之间允许互相打断（用户改主意了）
        if incoming.priority >= 100 and current.priority >= 100:
            return True
        return False

    def trigger_immediate_tick(self) -> None:
        """清掉待执行的定时器，立即跑一轮。"""
        with self._lock:
            if self._processing:
                # 正在执行：靠 abort + 执行完后的 schedule_next 接上
                return
            if not self._running:
                return
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None
        t = threading.Thread(target=self._tick_and_reschedule,
                             name="bw-immediate-tick", daemon=True)
        t.start()

    # ------- 主循环体 ---------------------------------------------

    def _tick_and_reschedule(self) -> None:
        try:
            self._on_tick()
        except Exception as ex:  # pragma: no cover - 兜底
            self._emit("error", {"label": "tick", "error": str(ex)})
        finally:
            self._schedule_next()

    def _on_tick(self) -> None:
        """执行一轮。"""
        with self._lock:
            if self._processing:
                return
            self._processing = True
            self._last_tick_aborted = False

        auto_tick = False
        revision_at_start: Optional[int] = None
        try:
            self._enqueue_due_reminders()

            msg = self.queue.pop()
            if msg is not None:
                lane = "L1" if msg.is_user else "BG"
                self._run_turn_with_watchdog(
                    msg.text, f"{lane} message from {msg.from_id}", msg, msg.priority)
            else:
                # 心跳关闭时不能漏跑自主轮（防御性边界）
                if not self._heartbeat_enabled() and self._awakening_ticks <= 0:
                    return
                auto_tick = True
                revision_at_start = self.scheduler.policy.revision
                self._run_turn_with_watchdog(
                    self._format_tick(), "自主 TICK", None, 0)
        except Exception as ex:
            self._last_tick_aborted = True
            self._emit("error", {"label": "on_tick",
                                 "error": f"{type(ex).__name__}: {ex}",
                                 "trace": traceback.format_exc(limit=3)})
        finally:
            with self._lock:
                self._processing = False
                self._tick_count += 1
                self._current = None

            if auto_tick and not self._last_tick_aborted:
                # 节奏 TTL：本轮设置的节奏从**下一轮**开始生效，不当场消耗
                reconfigured = (revision_at_start is not None
                                and self.scheduler.policy.revision != revision_at_start)
                if not reconfigured:
                    self.scheduler.policy.consume()
                if self._awakening_ticks > 0:
                    self._awakening_ticks -= 1

    def _run_turn_with_watchdog(self, text: str, label: str,
                                msg: Optional[Message], priority: int) -> None:
        """用看门狗包住一轮执行：超时强制 abort。"""
        try:
            watchdog = float(self.config.get("watchdog_seconds", 300) or 300)
        except Exception:
            watchdog = 300.0

        abort = threading.Event()
        exec_obj = Execution(label=label, priority=priority,
                             started_at=time.time(), abort=abort)
        with self._lock:
            self._current = exec_obj

        self._emit("turn_started", {"label": label, "priority": priority,
                                    "text": text[:200]})

        box: Dict[str, Any] = {}

        def worker() -> None:
            try:
                box["result"] = self._run_turn(text, label, msg, abort)
            except Exception as ex:
                box["error"] = ex
                box["trace"] = traceback.format_exc(limit=5)

        t = threading.Thread(target=worker, name="bw-turn", daemon=True)
        t.start()
        t.join(timeout=watchdog)

        if t.is_alive():
            # 卡死了：强制中止。线程留在后台，abort 生效后自行退出。
            abort.set()
            self._last_tick_aborted = True
            self._emit("error", {
                "label": "watchdog",
                "error": f"回合卡死超过 {watchdog:.0f}s，已强制中止（{label}）",
                "elapsed": round(exec_obj.elapsed(), 1),
            })
            return

        if "error" in box:
            self._last_tick_aborted = True
            self._emit("error", {
                "label": label,
                "error": f"{type(box['error']).__name__}: {box['error']}",
                "trace": box.get("trace", ""),
            })
        else:
            self._emit("turn_finished", {
                "label": label,
                "elapsed": round(exec_obj.elapsed(), 2),
            })

    # ------- 排程 -------------------------------------------------

    def _schedule_next(self) -> None:
        """安排下一次心跳。"""
        with self._lock:
            if not self._running:
                return
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

        self._enqueue_due_reminders()

        snap = self.queue.snapshot()
        reminder = self._next_reminder_at()
        scale = self._tick_scale()

        decision = self.scheduler.decide(
            queue_snapshot=snap,
            heartbeat_enabled=self._heartbeat_enabled(),
            task_active=self._task_active(),
            awakening_ticks=self._awakening_ticks,
            next_reminder_at=reminder,
            tick_scale=scale,
        )

        self._emit("quota", {
            "next_tick_ms": (decision.interval * 1000) if decision.interval is not None else None,
            "label": decision.label,
            "reason": decision.reason,
            "queue": snap,
            "tick_scale": scale,
            "scheduler": self.scheduler.status(),
        })

        if decision.interval is None:
            return

        interval = max(0.0, min(float(decision.interval), MAX_SLEEP_SECONDS))
        timer = threading.Timer(interval, self._tick_and_reschedule)
        timer.daemon = True
        with self._lock:
            if not self._running:
                return
            self._timer = timer
        timer.start()

    # ------- 辅助 -------------------------------------------------

    def _heartbeat_enabled(self) -> bool:
        try:
            return bool(self.config.get("heartbeat_enabled", True))
        except Exception:
            return True

    def _task_active(self) -> bool:
        hook = self.hooks.get("has_active_task")
        if callable(hook):
            try:
                return bool(hook())
            except Exception:
                return False
        return False

    def _next_reminder_at(self) -> Optional[float]:
        hook = self.hooks.get("next_reminder")
        if callable(hook):
            try:
                v = hook()
                return float(v) if v is not None else None
            except Exception:
                return None
        return None

    def _tick_scale(self) -> float:
        hook = self.hooks.get("tick_scale")
        if callable(hook):
            try:
                return float(hook() or 1.0)
            except Exception:
                return 1.0
        return 1.0

    def _enqueue_due_reminders(self) -> None:
        hook = self.hooks.get("enqueue_due_reminders")
        if callable(hook):
            try:
                hook()
            except Exception:
                pass

    def _format_tick(self) -> str:
        """自主 TICK 的输入文本。

        空闲时不给模型下死命令，而是让它自己判断这一轮该做什么。
        """
        return (
            "[自主 TICK] 现在没有待处理的用户消息。"
            "你可以：整理记忆、检查待办、复盘上一轮、或只是记录一条观察。"
            "如果确实无事可做，直接回复一条极短的静默标记即可。"
        )

    # ------- 状态 -------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            current = self._current.as_dict() if self._current else None
            return {
                "running": self._running,
                "processing": self._processing,
                "tick_count": self._tick_count,
                "awakening_ticks": self._awakening_ticks,
                "uptime": round(time.time() - (self._started_at or time.time()), 1),
                "current": current,
                "queue": self.queue.snapshot(),
                "scheduler": self.scheduler.status(),
                "last_aborted": self._last_tick_aborted,
            }
