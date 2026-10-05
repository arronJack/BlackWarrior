"""事件总线 —— UI 实时性的唯一来源。

黑武士的 UI 不做轮询。内核每个动作（收到消息、思考片段、工具调用、记忆写入、
认知状态变化、心跳）都往总线上发事件，SSE 把事件流推给 Electron 渲染进程。

设计要点：

1. **保留最近 N 条**：浏览器重连 / 页面刷新后能补齐，不会"错过一段思考就没了"。
2. **sticky 事件**：需要用户处理后才消失的状态（如"未激活"）会一直留在总线里，
   新订阅者一上来就能拿到，不依赖时序。
3. **线程安全**：内核主循环、HTTP 工作线程、SSE 长连接会并发读写。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Iterable, List, Optional


class EventBus:
    """轻量事件总线。"""

    def __init__(self, history: int = 300) -> None:
        self._subs: "list[queue.Queue]" = []
        self._history: List[Dict[str, Any]] = []
        self._sticky: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._history_size = int(history)
        self._seq = 0

    # ------- 发布 -------------------------------------------------

    def publish(self, type_: str, **payload: Any) -> Dict[str, Any]:
        """发布一个事件，返回事件体。"""
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "type": type_,
                "ts": time.time(),
            }
            event.update(payload)
            self._history.append(event)
            if len(self._history) > self._history_size:
                del self._history[: len(self._history) - self._history_size]

            dead: "list[queue.Queue]" = []
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # 订阅者消费太慢（比如浏览器标签页被挂起）。
                    # 丢弃并标记为待清理，绝不让主线程被拖住。
                    dead.append(q)
            for q in dead:
                self._subs.remove(q)
            return event

    # ------- 订阅 -------------------------------------------------

    def subscribe(self, maxsize: int = 500) -> "queue.Queue":
        """订阅事件流，返回一个队列。"""
        q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    # ------- 查询 -------------------------------------------------

    def recent(self, n: int = 50, types: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """最近事件（可按类型过滤）。供 UI 首屏补齐。"""
        with self._lock:
            items = self._history
            if types is not None:
                want = set(types)
                items = [e for e in items if e.get("type") in want]
            return list(items[-int(n):])

    def since(self, last_id: int, n: int = 200) -> List[Dict[str, Any]]:
        """返回 id 大于 last_id 的事件（SSE 断线重连用）。"""
        with self._lock:
            out = [e for e in self._history if int(e.get("id", 0)) > int(last_id)]
            return out[-int(n):]

    # ------- sticky -----------------------------------------------

    def set_sticky(self, key: str, **payload: Any) -> Dict[str, Any]:
        """设置一个持续状态事件（如未激活、错误横幅）。"""
        ev = {"type": key, "ts": time.time(), "sticky": True}
        ev.update(payload)
        with self._lock:
            self._sticky[key] = ev
        return self.publish(key, **payload)

    def clear_sticky(self, key: str) -> None:
        with self._lock:
            self._sticky.pop(key, None)
        self.publish(key + "_cleared", key=key)

    def sticky(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._sticky)

    # ------- 运维 -------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "history": len(self._history),
                "seq": self._seq,
                "sticky": list(self._sticky.keys()),
            }


# 语义化的发布助手：让调用处读起来像在描述意图，而不是在拼 dict。

BUS = EventBus()


def emit(type_: str, payload: Optional[Dict[str, Any]] = None,
         **kw: Any) -> Dict[str, Any]:
    """向全局总线发布事件。

    两种写法都支持（历史原因：早期调用点传 dict，后来改用关键字）::

        emit("reply", {"text": "hi"})
        emit("reply", text="hi")
    """
    data: Dict[str, Any] = dict(payload or {})
    data.update(kw)
    return BUS.publish(type_, **data)


def thinking(text: str, *, turn_id: str = "") -> Dict[str, Any]:
    """思考流片段。"""
    return emit("thinking", text=text, turn_id=turn_id)


def token(text: str, *, turn_id: str = "", done: bool = False) -> Dict[str, Any]:
    """回复流式片段。"""
    return emit("reply_delta" if not done else "reply", text=text, turn_id=turn_id)


def tool_event(stage: str, name: str = "", **kw: Any) -> Dict[str, Any]:
    """工具生命周期：call / result / error。"""
    return emit("tool_" + stage, name=name, **kw)


def cognition(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """认知状态更新（情绪、记忆强度、焦点等）。"""
    return emit("cognition", snapshot=snapshot)


def error(label: str, message: str, **kw: Any) -> Dict[str, Any]:
    return emit("error", label=label, error=message, **kw)
