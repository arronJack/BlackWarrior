"""SSE 事件流 —— UI 实时性的传输层。

浏览器用 ``EventSource`` 订阅 ``/events``，内核的每个动作实时推过去：
思考、增量文本、工具调用、认知状态、心跳……

三条实践经验（都是踩过的坑）::

1. **必须定期发保活注释**（``: ping``）。否则中间代理/浏览器会在几十秒后
   判定连接空闲并断开，表现为"聊着聊着 UI 就不动了"。
2. **客户端断开要能被感知**。写出时抛 BrokenPipe/ConnectionReset 就退出，
   否则线程会永远卡在这条死连接上，越积越多。
3. **重连要能补齐**。``Last-Event-ID`` 带回上次序号，从历史里补发遗漏事件，
   避免断线期间发生的事永久丢失。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Dict, Optional

#: 保活间隔（秒）
KEEPALIVE_SECONDS = 15.0


def _frame(event: Dict[str, Any]) -> str:
    """把一个事件编成 SSE 帧。

    ``id:`` 必须带上——浏览器断线重连时靠 ``Last-Event-ID`` 请求头回传序号，
    少了这一行，服务端永远收到 0，断线期间的事件就补不回来。
    """
    etype = str(event.get("type") or "message")
    try:
        payload = json.dumps(event, ensure_ascii=False, default=str)
    except Exception:
        payload = json.dumps({"type": etype}, ensure_ascii=False)
    lines = []
    eid = int(event.get("id") or 0)
    if eid:
        lines.append(f"id: {eid}")
    lines.append(f"event: {etype}")
    lines.append(f"data: {payload}")
    # 多行 data 需要逐行加前缀，这里 JSON 已压成单行，安全
    return "\n".join(lines) + "\n\n"


class SSEStream:
    """一条 SSE 连接。"""

    def __init__(self, bus: Any, wfile: Any, *, last_event_id: int = 0,
                 keepalive: float = KEEPALIVE_SECONDS) -> None:
        self.bus = bus
        self.wfile = wfile
        self.last_event_id = int(last_event_id or 0)
        self.keepalive = float(keepalive)
        self._q: Optional["queue.Queue"] = None
        self._closed = False

    # ------- 握手 -------------------------------------------------

    def headers(self) -> "list[tuple[str, str]]":
        return [
            ("Content-Type", "text/event-stream; charset=utf-8"),
            ("Cache-Control", "no-cache, no-transform"),
            ("Connection", "keep-alive"),
            ("X-Accel-Buffering", "no"),   # 关掉 nginx 缓冲
        ]

    # ------- 主循环 -----------------------------------------------

    def run(self) -> None:
        """把事件流写到 wfile，直到客户端断开。"""
        self._q = self.bus.subscribe(maxsize=800)
        try:
            self._send_replay()
            self._send_sticky()
            self._loop()
        except Exception:
            pass
        finally:
            self.close()

    def _send_replay(self) -> None:
        """断线重连补齐：补发 last_event_id 之后的事件。"""
        if self.last_event_id <= 0:
            return
        missed = self.bus.since(self.last_event_id, n=200)
        for ev in missed:
            self._write(_frame(ev))

    def _send_sticky(self) -> None:
        """补发 sticky 状态（如未激活横幅）。"""
        for ev in self.bus.sticky().values():
            self._write(_frame(ev))

    def _loop(self) -> None:
        assert self._q is not None
        last_write = time.time()
        while not self._closed:
            try:
                ev = self._q.get(timeout=1.0)
            except queue.Empty:
                # 空闲：按需发保活
                if time.time() - last_write >= self.keepalive:
                    self._write(": ping\n\n")
                    last_write = time.time()
                continue
            self._write(_frame(ev))
            last_write = time.time()

    # ------- 写出 -------------------------------------------------

    def _write(self, text: str) -> None:
        """写一帧。客户端断开时抛异常，由 run() 的 except 收口。"""
        if self._closed:
            return
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._q is not None:
            try:
                self.bus.unsubscribe(self._q)
            except Exception:
                pass
