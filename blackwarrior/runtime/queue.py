"""消息队列 —— 主循环的"待办清单"。

优先级语义（**数值越大越优先**，与主循环抢占判定一致）::

    USER       100   用户消息（UI / 微信 / webhook 进来的真人消息）
    REMINDER    80   到点的提醒（用户设定的，等同于用户意图）
    BACKGROUND  50   后台消息（系统事件、工具回调、自主线索）
    SYSTEM      10   内部系统消息（自检、启动引导）

入队规则
--------
1. 同优先级 **FIFO**，保证"先说的先被处理"；
2. 不同优先级 **高者先出**；
3. 支持去重键（``dedupe_key``）：心跳里反复产生的同一条线索不该刷屏。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PRIORITY_USER = 100
PRIORITY_REMINDER = 80
PRIORITY_BACKGROUND = 50
PRIORITY_SYSTEM = 10

#: 队列名称
LANE_USER = "user"
LANE_BACKGROUND = "background"


@dataclass
class Message:
    """一条待处理的消息。"""

    text: str
    priority: int = PRIORITY_BACKGROUND
    lane: str = LANE_BACKGROUND          # user / background
    from_id: str = "local"
    channel: str = "ui"
    turn_id: str = ""
    ts: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""

    @property
    def is_user(self) -> bool:
        return self.lane == LANE_USER or self.priority >= PRIORITY_USER

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "priority": self.priority,
            "lane": self.lane,
            "from_id": self.from_id,
            "channel": self.channel,
            "turn_id": self.turn_id,
            "ts": self.ts,
            "meta": dict(self.meta),
        }


class MessageQueue:
    """线程安全的优先级队列。"""

    def __init__(self, dedupe_window: float = 30.0) -> None:
        self._items: List[Message] = []
        self._lock = threading.RLock()
        self._dedupe: Dict[str, float] = {}
        self._dedupe_window = float(dedupe_window)
        self._dropped = 0

    # ------- 入队 -------------------------------------------------

    def push(self, text: str, *, priority: int = PRIORITY_BACKGROUND,
             lane: Optional[str] = None, from_id: str = "local",
             channel: str = "ui", turn_id: str = "",
             meta: Optional[Dict[str, Any]] = None,
             dedupe_key: str = "") -> Optional[Message]:
        """入队。被去重拦截时返回 None。"""
        if lane is None:
            lane = LANE_USER if priority >= PRIORITY_USER else LANE_BACKGROUND
        if dedupe_key and self._is_duplicate(dedupe_key):
            self._dropped += 1
            return None
        msg = Message(
            text=text, priority=int(priority), lane=lane, from_id=from_id,
            channel=channel, turn_id=turn_id, meta=dict(meta or {}),
            dedupe_key=dedupe_key,
        )
        with self._lock:
            self._items.append(msg)
            # 高优先在前；同级按时间先后
            self._items.sort(key=lambda m: (-m.priority, m.ts))
        if dedupe_key:
            self._dedupe[dedupe_key] = time.time()
        return msg

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        last = self._dedupe.get(key)
        if last is None:
            return False
        if now - last > self._dedupe_window:
            return False
        return True

    # ------- 出队 -------------------------------------------------

    def pop(self) -> Optional[Message]:
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)

    def peek(self) -> Optional[Message]:
        with self._lock:
            return self._items[0] if self._items else None

    # ------- 查询 -------------------------------------------------

    def has_messages(self) -> bool:
        with self._lock:
            return bool(self._items)

    def has_user_messages(self) -> bool:
        with self._lock:
            return any(m.is_user for m in self._items)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            user = sum(1 for m in self._items if m.is_user)
            return {"user": user, "background": len(self._items) - user,
                    "total": len(self._items)}

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pending": len(self._items),
                "dropped_by_dedupe": self._dropped,
                "top": self._items[0].as_dict() if self._items else None,
            }

    def __len__(self) -> int:
        return self.pending_count()
