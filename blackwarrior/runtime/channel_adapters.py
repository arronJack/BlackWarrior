"""渠道出站适配器（v0.6.3）——把一段回复变成对目标渠道的**正确请求**。

职责边界（与 :mod:`.channels` 的分工）：
- 适配器只管"文本 → 该渠道的 HTTP 报文"（格式、切段、限流）；
- 桥（ChannelBridge）统一管记账、事件、日志——适配器不 emit、不落库。

内置两种：

1. ``webhook`` —— 通用 JSON（v0.6.2 原行为，任何自建中转都能收）。
2. ``wecom_bot`` —— 企业微信群机器人
   (``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...``)：
   - ``msgtype=markdown``，保留基本排版；
   - 单条 content ≤4096 字节 → 超长按**字符边界**切段（CJK 按 3 字节算）；
   - 官方限频 20 条/分钟 → 令牌桶滑动窗口，超限**丢弃并如实报错**
     （宁可少发一条也不把队列攒着延迟轰炸群）。

新增渠道 = 新增一个 ``deliver_`` 函数 + 在 ``deliver()`` 的分发表里加一行。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

#: 企业微信单条 markdown 上限 4096 字节，留出余量
WECOM_BYTE_LIMIT = 4000
#: 企业微信群机器人官方限频（条/分钟）
WECOM_RATE_PER_MIN = 20


def _split_by_bytes(text: str, limit: int) -> List[str]:
    """按 UTF-8 字节数切段，绝不切在字符中间。"""
    out: List[str] = []
    buf: List[str] = []
    used = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if used + n > limit and buf:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(ch)
        used += n
    if buf:
        out.append("".join(buf))
    return out or [""]


class RateLimiter:
    """滑动窗口限流器（条/分钟）。线程安全。"""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute))
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """窗口内还有名额吗？有则占一个并返回 True。"""
        now = time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > 60.0:
                self._hits.popleft()
            if len(self._hits) >= self.per_minute:
                return False
            self._hits.append(now)
            return True

    def pending(self) -> int:
        with self._lock:
            return len(self._hits)


def _post_json(url: str, payload: Dict[str, Any],
               timeout: float) -> Tuple[bool, str]:
    """POST JSON，返回 (2xx?, 错误说明)。网络异常不外抛。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status or 500)
            if 200 <= status < 300:
                return True, ""
            return False, f"HTTP {status}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


def _deliver_webhook(cfg: Dict[str, Any], text: str,
                     timeout: float) -> Tuple[bool, str]:
    """通用 webhook：{"channel","text","turn_id"}。"""
    payload = {"channel": cfg.get("name", ""), "text": text,
               "turn_id": cfg.get("_turn_id", "")}
    return _post_json(str(cfg.get("webhook_url") or ""), payload, timeout)


def _deliver_wecom_bot(cfg: Dict[str, Any], text: str, timeout: float,
                       limiter: RateLimiter) -> Tuple[bool, str]:
    """企业微信群机器人。超长切段 + 20 条/分钟限流（超限丢弃并报错）。"""
    url = str(cfg.get("webhook_url") or "")
    chunks = _split_by_bytes(text, WECOM_BYTE_LIMIT)
    errs: List[str] = []
    sent = 0
    for i, chunk in enumerate(chunks):
        if not limiter.acquire():
            errs.append(f"限流丢弃 {len(chunks) - i} 片（20 条/分钟）")
            break
        ok, err = _post_json(url, {
            "msgtype": "markdown",
            "markdown": {"content": chunk},
        }, timeout)
        if ok:
            sent += 1
        else:
            errs.append(f"第 {i + 1} 片失败：{err}")
    if not errs:
        note = "" if sent <= 1 else f"（{sent} 片）"
        return True, note
    return False, "；".join(errs)


#: 分发表：type → 投递函数
DELIVERERS = {
    "webhook": _deliver_webhook,
    "wecom_bot": _deliver_wecom_bot,
}


def deliver(cfg: Dict[str, Any], text: str, *,
            limiters: Optional[Dict[str, RateLimiter]] = None,
            timeout: float = 5.0) -> Tuple[bool, str]:
    """按渠道类型投递。返回 (ok, 附注/错误)。"""
    ctype = str(cfg.get("type") or "webhook").strip().lower()
    fn = DELIVERERS.get(ctype)
    if fn is None:
        return False, f"未知渠道类型：{ctype}（可选：{sorted(DELIVERERS)}）"
    if ctype == "wecom_bot":
        limiter = (limiters or {}).setdefault(
            cfg.get("name", ""), RateLimiter(WECOM_RATE_PER_MIN))
        return fn(cfg, text, timeout, limiter)
    return fn(cfg, text, timeout)


def selftest() -> bool:
    """适配器自检：字节切段 / 限流 / 两种报文格式（本地 HTTP 收包）。"""
    import http.server

    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError("适配器自检失败：" + msg)

    # ---- 1) 字节切段：CJK 按 3 字节，不切字符
    long_text = "黑武士测试" * 3000          # 5 字 × 3B = 15B/组
    parts = _split_by_bytes(long_text, WECOM_BYTE_LIMIT)
    check(all(len(p.encode("utf-8")) <= WECOM_BYTE_LIMIT for p in parts),
          "每片不超过字节上限")
    check("".join(parts) == long_text, "切段不丢字")
    check(_split_by_bytes("", 100) == [""], "空文本返回一个空片")

    # ---- 2) 限流器
    rl = RateLimiter(3)
    check(rl.acquire() and rl.acquire() and rl.acquire(), "前 3 次放行")
    check(not rl.acquire(), "第 4 次拒绝")

    # ---- 3) 收包验证两种格式
    got: List[Dict[str, Any]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}/hook"
        # webhook：原格式不变
        ok, err = deliver({"name": "t1", "type": "webhook",
                           "webhook_url": base, "_turn_id": "tx"}, "你好")
        check(ok, f"webhook 投递失败：{err}")
        check(got[-1] == {"channel": "t1", "text": "你好", "turn_id": "tx"},
              f"webhook 报文格式：{got[-1]}")
        # wecom_bot：markdown 格式
        ok, err = deliver({"name": "t2", "type": "wecom_bot",
                           "webhook_url": base}, "群消息")
        check(ok, f"wecom 投递失败：{err}")
        check(got[-1].get("msgtype") == "markdown"
              and got[-1]["markdown"]["content"] == "群消息",
              f"wecom 报文格式：{got[-1]}")
        # wecom_bot：长文切段 + 多次投递
        ok, err = deliver({"name": "t3", "type": "wecom_bot",
                           "webhook_url": base}, long_text)
        check(ok, f"wecom 长文投递失败：{err}")
        joined = "".join(g["markdown"]["content"] for g in got[-len(parts):])
        check(joined == long_text, "长文分片拼回应还原全文")
        # 未知类型如实拒绝
        ok, err = deliver({"name": "t4", "type": "nope",
                           "webhook_url": base}, "x")
        check(not ok and "未知渠道类型" in err, "未知类型应拒绝")
    finally:
        srv.shutdown()
    return True


if __name__ == "__main__":
    print("适配器自检：", "通过" if selftest() else "失败")
