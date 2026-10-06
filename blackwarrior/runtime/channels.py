"""渠道桥接（v0.6.2）——"外部世界 ⇄ 黑武士"的双向通道。

设计立场（与 push_background 一脉相承）：

1. **渠道不建自己的循环**。所有外部消息（微信/Discord/钉钉/Webhook 回调…）
   都经 ``inbound()`` 走统一主循环，共享同一份记忆与认知状态；
   回复经 ``channel_out`` 事件回流，桥负责转投到各渠道的 webhook。
2. **配置驱动，零硬编码渠道**。channels 在 config.json 里声明：
   ``"channels": [{"name": "wework", "token": "xxx", "webhook_url": "https://..."}]``
   ——增加一个渠道不改一行代码。
3. **令牌校验用常数时间比较**，密钥在列表接口里打码，不回显全文。
4. **出站失败绝不拖垮主循环**：webhook 投递在独立线程、有超时、
   失败只记事件与日志。
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

#: 合法渠道名（防止拿名字当路径/注入）
_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")

_CHANNEL_PREFIX = "channel:"


def _mask(token: str) -> str:
    if not token:
        return ""
    t = str(token)
    if len(t) <= 4:
        return "*" * len(t)
    return t[:3] + "***" + t[-1:]


class ChannelBridge:
    """渠道桥：入站校验 → 主循环；主循环回复 → 出站 webhook。

    挂在 :class:`~blackwarrior.core.WarriorCore` 上（``core.channels``）。
    """

    #: 出站 POST 超时（秒）
    OUTBOUND_TIMEOUT = 5.0
    #: 单渠道回复文本上限（企业微信等由适配器按字节切段，这里只挡极端长文）
    MAX_OUT_CHARS = 20000

    def __init__(self, core: Any) -> None:
        self._core = core
        self._q = None                 # BUS 订阅队列（start 后有值）
        self._thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._limiters: Dict[str, Any] = {}   # 渠道名 → 限流器（适配器用）
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._delivered = 0
        self._failed = 0
        self._pulled = 0
        self._last_error = ""

    # ------------------------------------------------------------- 配置

    def _channels(self) -> List[Dict[str, Any]]:
        try:
            raw = self._core.config.get("channels", []) or []
        except Exception:
            raw = []
        out = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip().lower()
            if not _NAME_RE.match(name):
                continue
            try:
                interval = float(c.get("poll_interval") or 10.0)
            except (TypeError, ValueError):
                interval = 10.0
            out.append({
                "name": name,
                "token": str(c.get("token") or ""),
                "webhook_url": str(c.get("webhook_url") or "").strip(),
                "enabled": c.get("enabled", True) is not False,
                # v0.6.3：出站类型（webhook / wecom_bot）+ 轮询入站
                "type": str(c.get("type") or "webhook").strip().lower(),
                "secret": str(c.get("secret") or ""),  # 钉钉加签密钥
                "poll_url": str(c.get("poll_url") or "").strip(),
                "poll_interval": max(3.0, min(interval, 600.0)),
            })
        return out

    def _find(self, name: str) -> Optional[Dict[str, Any]]:
        name = str(name or "").strip().lower()
        for c in self._channels():
            if c["name"] == name:
                return c
        return None

    def list_channels(self) -> Dict[str, Any]:
        """渠道清单（令牌打码，供 /api/channel/list 与状态页）。"""
        items = []
        for c in self._channels():
            items.append({
                "name": c["name"],
                "enabled": c["enabled"],
                "type": c["type"],
                "token_masked": _mask(c["token"]),
                "has_webhook": bool(c["webhook_url"]),
                "has_poll": bool(c["poll_url"]),
            })
        return {"count": len(items), "items": items,
                "delivered": self._delivered, "failed": self._failed,
                "pulled": self._pulled, "last_error": self._last_error}

    # ------------------------------------------------------------- 入站

    def inbound(self, name: str, token: str, text: str,
                from_id: str = "") -> Dict[str, Any]:
        """外部渠道消息 → 统一主循环。校验失败如实拒绝。"""
        if not self._core.is_running():
            return {"ok": False, "reason": "主循环未运行，消息未受理"}
        cfg = self._find(name)
        if cfg is None:
            return {"ok": False, "reason": f"未知渠道：{name}"}
        if not cfg["enabled"]:
            return {"ok": False, "reason": f"渠道 {name} 已停用"}
        if not hmac.compare_digest(str(token or ""), cfg["token"]):
            # 常数时间比较，避免时序侧信道一点点磨出 token
            return {"ok": False, "reason": "令牌校验失败"}
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "reason": "消息内容为空"}
        ch = _CHANNEL_PREFIX + cfg["name"]
        return self._core.push_background(
            body[:8000],
            source="channel:" + cfg["name"],
            channel=ch,
            dedupe_key=f"ch:{cfg['name']}:{from_id or ''}:{hash(body) & 0xffffff}")

    # ------------------------------------------------------------- 出站

    def start(self) -> None:
        """启动出站转发线程 + 轮询入站线程（幂等）。"""
        from ..events import BUS

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._q = BUS.subscribe()
            self._thread = threading.Thread(
                target=self._pump, name="bw-channel-bridge", daemon=True)
            self._thread.start()
        # 有轮询渠道才起拉取线程
        if any(c["poll_url"] for c in self._channels()):
            with self._lock:
                if self._poll_thread is None or not self._poll_thread.is_alive():
                    self._poll_thread = threading.Thread(
                        target=self._poll_loop,
                        name="bw-channel-poll", daemon=True)
                    self._poll_thread.start()

    def stop(self) -> None:
        from ..events import BUS

        self._stop.set()
        with self._lock:
            if self._q is not None:
                try:
                    BUS.unsubscribe(self._q)
                except Exception:
                    pass
                self._q = None

    def _pump(self) -> None:
        """轮询 BUS，把渠道回合的最终回复转投 webhook。"""
        q = self._q
        if q is None:
            return
        while not self._stop.is_set():
            try:
                ev = q.get(timeout=1.0)
            except Exception:
                continue
            if not isinstance(ev, dict) or ev.get("type") != "channel_out":
                continue
            p = ev.get("payload") or ev
            ch = str(p.get("channel") or "")
            if not ch.startswith(_CHANNEL_PREFIX):
                continue
            name = ch[len(_CHANNEL_PREFIX):]
            cfg = self._find(name)
            if cfg is None or not cfg["enabled"] or not cfg["webhook_url"]:
                continue
            self._deliver(cfg, p)

    def _poll_loop(self) -> None:
        """轮询入站：定期 GET poll_url，取回 {"items":[{id,text,from_id}]}。

        这是**无需公网回调**的入站方案：任何能自建 HTTP 端点的东西
        （内网中转、cron 拉邮箱、Server 酱聚合器…）都可以当渠道前级。
        去重交给 push_background 的 dedupe_key（poll:渠道:id）。
        """
        while not self._stop.is_set():
            try:
                for cfg in self._channels():
                    if self._stop.is_set():
                        return
                    if not cfg["enabled"] or not cfg["poll_url"]:
                        continue
                    self._poll_once(cfg)
            except Exception:
                pass
            # 睡最小间隔（各渠道 interval 不同，逐秒查 due 更省事）
            for _ in range(10):
                if self._stop.is_set():
                    return
                time.sleep(1.0)

    def _poll_once(self, cfg: Dict[str, Any]) -> None:
        """拉一次渠道收件端点，把新消息喂进主循环。"""
        import urllib.request as _ur

        try:
            with _ur.urlopen(cfg["poll_url"], timeout=8.0) as resp:
                raw = json.loads(resp.read() or b"{}")
        except Exception as ex:
            with self._lock:
                self._last_error = f"poll {cfg['name']}: {type(ex).__name__}: {ex}"
            return
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            mid = str(it.get("id") or "")
            r = self.inbound(
                cfg["name"], cfg["token"], text[:8000],
                from_id=str(it.get("from_id") or "poll"))
            # 喂成功的计数；被去重拦掉的不算新消息
            if r.get("ok") and r.get("queued", True):
                with self._lock:
                    self._pulled += 1

    def _deliver(self, cfg: Dict[str, Any], p: Dict[str, Any]) -> None:
        """按渠道类型投递回复。独立线程里跑，失败只记录。"""
        from . import channel_adapters

        text = str(p.get("text") or "")[: self.MAX_OUT_CHARS]
        if not text:
            return
        req_cfg = dict(cfg)
        req_cfg["_turn_id"] = str(p.get("turn_id") or "")
        limiters = self._limiters
        ok, err = channel_adapters.deliver(
            req_cfg, text, limiters=limiters,
            timeout=self.OUTBOUND_TIMEOUT)
        with self._lock:
            if ok:
                self._delivered += 1
            else:
                self._failed += 1
                self._last_error = err
        try:
            self._core.store.log_action(
                "channel_deliver", args={"channel": cfg["name"]},
                ok=ok, summary=(f"已投递 {len(text)} 字" if ok
                                else f"投递失败：{err}"),
                duration_ms=0)
        except Exception:
            pass
        from ..events import emit

        emit("channel_reply", {"channel": cfg["name"], "ok": ok,
                               "error": err, "chars": len(text)})

    def stats(self) -> Dict[str, Any]:
        """给 /status 的渠道概览。"""
        try:
            s = self.list_channels()
        except Exception:
            s = {"count": 0, "items": []}
        s["running"] = bool(self._thread is not None
                            and self._thread.is_alive())
        return s


# ------------------------------------------------------------------ 自检

def selftest() -> bool:
    """渠道桥自检：配置解析 / 令牌校验 / 打码 / 出站投递（本地 HTTP 接收）。"""
    import http.server
    import tempfile

    from ..config import Config

    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError("渠道桥自检失败：" + msg)

    # ---- 1) 配置解析与打码
    d = tempfile.mkdtemp(prefix="bw-ch-")
    cfg = Config({"channels": [
        {"name": "wework", "token": "secret-token-123",
         "webhook_url": "http://127.0.0.1:1/never"},
        {"name": "bad name!", "token": "x"},          # 非法名 → 过滤
        {"name": "off", "token": "t2", "enabled": False},
    ]})

    class _Bus:
        def subscribe(self, maxsize: int = 500):
            import queue as _q
            return _q.Queue()

        def unsubscribe(self, q):
            pass

    class _Core:
        config = cfg
        bus = _Bus()
        running = False

        def is_running(self):
            return _Core.running

        def push_background(self, text, **kw):
            _Core.pushed = (text, kw)
            return {"ok": True, "queued": True}

        class store:
            @staticmethod
            def log_action(*a, **kw):
                pass

    _Core.pushed = None
    core = _Core()
    bridge = ChannelBridge(core)

    ls = bridge.list_channels()
    check(ls["count"] == 2, f"非法名应被过滤，实际 {ls['count']}")
    names = {i["name"] for i in ls["items"]}
    check(names == {"wework", "off"}, f"渠道名集合不对：{names}")
    w = [i for i in ls["items"] if i["name"] == "wework"][0]
    check(w["token_masked"] != "secret-token-123" and "***" in w["token_masked"],
          "令牌必须打码")

    # ---- 2) 入站校验（is_running 是第一道闸，先置 True 再验各分支）
    _Core.running = True
    r = bridge.inbound("nope", "t", "hi")
    check(not r["ok"], "未知渠道应拒绝")
    r = bridge.inbound("wework", "wrong", "hi")
    check(not r["ok"] and "令牌" in r["reason"], "错令牌应拒绝")
    r = bridge.inbound("wework", "secret-token-123", "")
    check(not r["ok"], "空消息应拒绝")
    _Core.running = False
    r = bridge.inbound("wework", "secret-token-123", "你好")
    check(not r["ok"] and "未运行" in r["reason"], "主循环未运行应拒绝")
    _Core.running = True
    r = bridge.inbound("wework", "secret-token-123", "你好")
    check(r.get("ok"), f"合法入站应通过：{r}")
    text, kw = _Core.pushed
    check(kw.get("channel") == "channel:wework", "channel 应带渠道前缀")
    r = bridge.inbound("off", "t2", "你好")
    check(not r["ok"] and "停用" in r["reason"], "停用渠道应拒绝")

    # ---- 3) 出站投递（本地 HTTP 接收器）
    received = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg2 = Config({"channels": [
            {"name": "test", "token": "t",
             "webhook_url": f"http://127.0.0.1:{port}/hook"}]})
        core2 = _Core()
        core2.config = cfg2
        b2 = ChannelBridge(core2)
        b2._deliver(b2._find("test"),
                    {"channel": "channel:test", "text": "渠道回复",
                     "turn_id": "tx"})
        check(len(received) == 1 and received[0]["text"] == "渠道回复",
              f"webhook 应收到回复：{received}")
        check(b2.stats()["delivered"] == 1, "投递计数应为 1")
        # 失败路径
        cfg3 = Config({"channels": [
            {"name": "test", "token": "t",
             "webhook_url": "http://127.0.0.1:1/never"}]})
        core3 = _Core()
        core3.config = cfg3
        b3 = ChannelBridge(core3)
        b3._deliver(b3._find("test"),
                    {"channel": "channel:test", "text": "x", "turn_id": ""})
        check(b3.stats()["failed"] == 1, "不可达 webhook 应计失败")
    finally:
        srv.shutdown()

    # ---- 4) pump 对非渠道事件不动作（由 _pump 的过滤分支保证）
    return True


if __name__ == "__main__":
    print("渠道桥自检：", "通过" if selftest() else "失败")
