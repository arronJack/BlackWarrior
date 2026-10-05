"""心跳调度 —— 决定"下一次醒来是什么时候"。

调度优先级（由高到低，与主流持续运行 Agent 一致，并做了两处增强）::

    1. 有用户消息        → 0s（立即）
    2. 有后台消息        → 0s（立即）
    3. 心跳已关闭        → 不排程（除非有待触发提醒）
    4. 被限流（429）     → 退避间隔
    5. L2 自定义节奏     → 智能体自己设定的间隔（带 TTL，会自然衰减回默认）
    6. 唤醒期            → 启动后的密集心跳（让智能体快速建立状态）
    7. 有活跃任务        → 任务节奏（比空闲快）
    8. 空闲              → 基础节奏 × 预测误差缩放 ★黑武士增强

★ 增强点一：**预测误差参与节奏决策**。
  环境可预测（误差低）时拉长间隔省 token；出现新情况（误差高）时缩短间隔加快复盘。
  这是主动推理在调度层的极简落地——同类项目的间隔是死的或只由模型自己设。

★ 增强点二：**提醒是独立计时源**。
  心跳关闭时，到期提醒仍然是唯一唤醒源，分段睡眠后重新计算，
  避免"关了心跳就再也收不到提醒"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: Node setTimeout 的溢出上限（约 24.8 天）；Python 侧保守取 7 天。
MAX_SLEEP_SECONDS = 7 * 24 * 3600


@dataclass
class TickDecision:
    """一次调度决策。``interval`` 为 None 表示不排程。"""

    interval: Optional[float]
    label: str
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"interval": self.interval, "label": self.label,
                "reason": self.reason}


class TickPolicy:
    """L2 自定义节奏（智能体自我调节的心跳间隔）。

    带 TTL：模型可以调用 ``set_tick_interval`` 临时把心跳改成 10 秒
    （比如"我正在等一个结果，快点检查"），TTL 用完后自动回到默认节奏。
    """

    def __init__(self) -> None:
        self._interval: Optional[float] = None
        self._ttl: int = 0
        self._reason: str = ""
        self._revision: int = 0

    def set(self, interval: float, ttl: int = 1, reason: str = "") -> None:
        """设置自定义节奏。``ttl`` 为剩余生效轮数。"""
        self._interval = max(0.0, float(interval))
        self._ttl = max(1, int(ttl))
        self._reason = reason or ""
        self._revision += 1

    def clear(self) -> None:
        self._interval = None
        self._ttl = 0
        self._reason = ""
        self._revision += 1

    def get(self) -> Optional[float]:
        """当前生效的自定义间隔（TTL 用完返回 None）。"""
        if self._interval is None or self._ttl <= 0:
            return None
        return self._interval

    def consume(self) -> None:
        """消耗一轮 TTL。"""
        if self._ttl > 0:
            self._ttl -= 1
        if self._ttl <= 0:
            self._interval = None
            self._reason = ""

    @property
    def revision(self) -> int:
        return self._revision

    def status(self) -> Dict[str, Any]:
        return {
            "interval": self._interval,
            "ttl": self._ttl,
            "reason": self._reason,
            "revision": self._revision,
            "active": self.get() is not None,
        }


class Scheduler:
    """心跳间隔决策器。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.policy = TickPolicy()
        self._rate_limited_until: float = 0.0
        self._last_decision: Optional[TickDecision] = None

    # ------- 限流 -------------------------------------------------

    def mark_rate_limited(self, cooldown: float = 600.0) -> None:
        """标记被限流（429）。在 cooldown 秒内使用退避间隔。"""
        self._rate_limited_until = time.time() + max(1.0, float(cooldown))

    def clear_rate_limit(self) -> None:
        self._rate_limited_until = 0.0

    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limited_until

    def rate_limit_remaining(self) -> float:
        return max(0.0, self._rate_limited_until - time.time())

    # ------- 配置读取 ---------------------------------------------

    def _get(self, key: str, default: Any) -> Any:
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    # ------- 决策 -------------------------------------------------

    def decide(self, *, queue_snapshot: Optional[Dict[str, int]] = None,
               heartbeat_enabled: bool = True,
               task_active: bool = False,
               awakening_ticks: int = 0,
               next_reminder_at: Optional[float] = None,
               tick_scale: float = 1.0) -> TickDecision:
        """计算下一次心跳的间隔。"""
        snap = queue_snapshot or {"user": 0, "background": 0}

        # 1) 用户消息：立即
        if snap.get("user", 0) > 0:
            return TickDecision(0.0, "immediate (用户消息待处理)", "user-pending")

        # 2) 后台消息：立即
        if snap.get("background", 0) > 0:
            return TickDecision(0.0, "immediate (后台消息待处理)", "bg-pending")

        # 3) 计算候选间隔（可能为 None = 不排程）
        interval: Optional[float] = None
        label = "心跳已关闭"
        reason = "heartbeat-disabled"

        if not heartbeat_enabled:
            interval = None
        elif self.is_rate_limited():
            interval = max(5.0, self.rate_limit_remaining())
            label = f"限流退避 {interval:.0f}s"
            reason = "rate-limited"
        else:
            custom = self.policy.get()
            if custom is not None:
                interval = float(custom)
                st = self.policy.status()
                label = (f"自主节奏 {interval:.0f}s"
                         f"（剩余 {st['ttl']} 轮"
                         f"{' · ' + st['reason'] if st['reason'] else ''}）")
                reason = "l2-custom"
            elif awakening_ticks > 0:
                interval = float(self._get("awakening_interval", 10.0))
                label = f"唤醒期 {interval:.0f}s（剩余 {awakening_ticks} 轮）"
                reason = "awakening"
            elif task_active:
                interval = float(self._get("task_tick_interval", 30.0))
                label = f"任务模式 {interval:.0f}s"
                reason = "task-active"
            else:
                base = float(self._get("tick_interval", 60.0))
                interval = max(2.0, base * float(tick_scale))
                label = f"空闲 {interval:.0f}s"
                if abs(tick_scale - 1.0) > 0.01:
                    label += f"（预测误差缩放 ×{tick_scale}）"
                reason = "idle"

        # 4) 提醒：独立计时源，取更早的那个
        if next_reminder_at is not None:
            due_in = max(0.0, float(next_reminder_at) - time.time())
            if interval is None or due_in < interval:
                interval = min(due_in, MAX_SLEEP_SECONDS)
                if due_in > MAX_SLEEP_SECONDS:
                    label = f"提醒扫描 {interval / 3600:.1f}h 后"
                else:
                    label = f"提醒触发 {due_in:.0f}s 后"
                reason = "reminder"

        decision = TickDecision(interval, label, reason)
        self._last_decision = decision
        return decision

    # ------- 运维 -------------------------------------------------

    @property
    def last_decision(self) -> Optional[TickDecision]:
        return self._last_decision

    def status(self) -> Dict[str, Any]:
        d = self._last_decision
        return {
            "policy": self.policy.status(),
            "rate_limited": self.is_rate_limited(),
            "rate_limit_remaining": round(self.rate_limit_remaining(), 1),
            "last": d.as_dict() if d else None,
        }
