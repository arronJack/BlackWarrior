"""运行时层 —— 让黑武士"一直醒着"。

- :mod:`.queue`      优先级消息队列
- :mod:`.scheduler`  心跳间隔决策 + L2 自主节奏
- :mod:`.continuum`  主循环（抢占 / 看门狗 / 异常兜底）
"""

from .continuum import Continuum, Execution
from .queue import (
    LANE_BACKGROUND,
    LANE_USER,
    PRIORITY_BACKGROUND,
    PRIORITY_REMINDER,
    PRIORITY_SYSTEM,
    PRIORITY_USER,
    Message,
    MessageQueue,
)
from .scheduler import Scheduler, TickDecision, TickPolicy

__all__ = [
    "Continuum",
    "Execution",
    "MessageQueue",
    "Message",
    "Scheduler",
    "TickPolicy",
    "TickDecision",
    "PRIORITY_USER",
    "PRIORITY_REMINDER",
    "PRIORITY_BACKGROUND",
    "PRIORITY_SYSTEM",
    "LANE_USER",
    "LANE_BACKGROUND",
]
