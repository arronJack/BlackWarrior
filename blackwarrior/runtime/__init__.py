"""运行时层 —— 让黑武士"一直醒着"。

- :mod:`.queue`      优先级消息队列
- :mod:`.scheduler`  心跳间隔决策 + L2 自主节奏
- :mod:`.continuum`  主循环（抢占 / 看门狗 / 异常兜底）
- :mod:`.mcp_client` MCP 外部工具生态客户端（v0.7.0）
- :mod:`.channels`   外部渠道桥（飞书 / 企微 / 钉钉 / Webhook）
"""

from .continuum import Continuum, Execution
from .mcp_client import MCPClient, MCPManager
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
    "MCPClient",
    "MCPManager",
    "PRIORITY_USER",
    "PRIORITY_REMINDER",
    "PRIORITY_BACKGROUND",
    "PRIORITY_SYSTEM",
    "LANE_USER",
    "LANE_BACKGROUND",
]
