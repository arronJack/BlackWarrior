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

v0.6.4 新增三种"注册即用"渠道：

3. ``serverchan`` —— Server酱·Turbo（微信推送）
   (``https://sctapi.ftqq.com/<SENDKEY>.send``)：webhook_url 填 SENDKEY
   （或直接粘贴完整 .send URL）；form 表单 title/desp；业务码 code!=0 如实报错。
   免费额度小（5 条/天），适合重要通知，不适合聊天流。
4. ``pushplus`` —— PushPlus（微信推送，免费额度大）
   (``https://www.pushplus.plus/send``)：webhook_url 填 token
   （或粘贴 ?token=xxx 的链接也认）；JSON {token,title,content,template}；
   业务码 code==200 才算成功。
5. ``dingtalk_bot`` —— 钉钉自定义机器人
   (``https://oapi.dingtalk.com/robot/send?access_token=...``)：
   webhook_url 填完整 webhook；若机器人选了"加签"安全设置，把密钥放
   ``secret`` 字段，自动算 timestamp+HMAC-SHA256 签名；官方限频 20 条/分钟。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

#: 企业微信单条 markdown 上限 4096 字节，留出余量
WECOM_BYTE_LIMIT = 4000
#: 企业微信群机器人官方限频（条/分钟）
WECOM_RATE_PER_MIN = 20
#: 钉钉自定义机器人官方限频（条/分钟）
DINGTALK_RATE_PER_MIN = 20

SERVERCHAN_SEND_URL = "https://sctapi.ftqq.com/{key}.send"
PUSHPLUS_SEND_URL = "https://www.pushplus.plus/send"


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


def _post_json(url: str, payload: Dict[str, Any], timeout: float
               ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """POST JSON，返回 (2xx?, 错误说明, 业务响应解析)。网络异常不外抛。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    return _send(req, timeout)


def _post_form(url: str, fields: Dict[str, str], timeout: float
               ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """POST 表单编码，返回 (2xx?, 错误说明, 业务响应解析)。网络异常不外抛。"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    return _send(req, timeout)


def _send(req: urllib.request.Request, timeout: float
          ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status or 500)
            body = resp.read() or b""
            if not 200 <= status < 300:
                return False, f"HTTP {status}", None
            try:
                return True, "", json.loads(body or b"{}")
            except Exception:
                return True, "", None
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}", None


def _deliver_webhook(cfg: Dict[str, Any], text: str,
                     timeout: float) -> Tuple[bool, str]:
    """通用 webhook：{"channel","text","turn_id"}。"""
    payload = {"channel": cfg.get("name", ""), "text": text,
               "turn_id": cfg.get("_turn_id", "")}
    ok, err, _ = _post_json(str(cfg.get("webhook_url") or ""), payload, timeout)
    return ok, err


def _first_line(text: str, cap: int = 20) -> str:
    """取首行做推送标题，过长截断。"""
    line = text.strip().splitlines()[0] if text.strip() else "黑武士"
    return line[:cap] or "黑武士"


def _deliver_serverchan(cfg: Dict[str, Any], text: str,
                        timeout: float) -> Tuple[bool, str]:
    """Server酱·Turbo：推到微信。webhook_url 填 SENDKEY 或完整 .send URL。"""
    key = str(cfg.get("webhook_url") or "").strip()
    if not key:
        return False, "serverchan 渠道未配置 webhook_url（填 SENDKEY）"
    url = key if key.startswith("http") else SERVERCHAN_SEND_URL.format(key=key)
    ok, err, data = _post_form(
        url, {"title": _first_line(text), "desp": text}, timeout)
    if ok and isinstance(data, dict) and data.get("code") not in (0, None):
        return False, f"Server酱业务错误：{data.get('message') or data.get('code')}"
    return ok, err


def _deliver_pushplus(cfg: Dict[str, Any], text: str,
                      timeout: float) -> Tuple[bool, str]:
    """PushPlus：推到微信（免费额度大）。webhook_url 填 token 或 ?token= 链接。"""
    raw = str(cfg.get("webhook_url") or "").strip()
    token = ""
    url = PUSHPLUS_SEND_URL
    if raw.startswith("http"):
        q = urllib.parse.urlparse(raw)
        token = urllib.parse.parse_qs(q.query).get("token", [""])[0]
        if q.path.rstrip("/").endswith("send"):
            url = f"{q.scheme}://{q.netloc}{q.path}"
    else:
        token = raw
    if not token:
        return False, "pushplus 渠道未配置 webhook_url（填 token）"
    ok, err, data = _post_json(url, {
        "token": token, "title": _first_line(text),
        "content": text, "template": "txt",
    }, timeout)
    if ok and isinstance(data, dict) and data.get("code") != 200:
        return False, f"PushPlus业务错误：{data.get('msg') or data.get('code')}"
    return ok, err


def _dingtalk_signed_url(url: str, secret: str) -> str:
    """钉钉加签：timestamp\\nsecret 的 HMAC-SHA256 → base64 → urlenc。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    sign = base64.b64encode(hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256).digest()).decode()
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


def _deliver_dingtalk_bot(cfg: Dict[str, Any], text: str, timeout: float,
                          limiter: RateLimiter) -> Tuple[bool, str]:
    """钉钉自定义机器人。text 消息 + 可选加签 + 20 条/分钟限流（超限丢弃）。"""
    url = str(cfg.get("webhook_url") or "")
    if not url:
        return False, "dingtalk_bot 渠道未配置 webhook_url"
    secret = str(cfg.get("secret") or "")
    if secret and "sign=" not in url:
        url = _dingtalk_signed_url(url, secret)
    if not limiter.acquire():
        return False, "限流丢弃（20 条/分钟）"
    ok, err, data = _post_json(url, {
        "msgtype": "text", "text": {"content": text},
    }, timeout)
    if ok and isinstance(data, dict) and data.get("errcode") not in (0, None):
        return False, f"钉钉业务错误：{data.get('errmsg') or data.get('errcode')}"
    return ok, err


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
        ok, err, _ = _post_json(url, {
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
    "serverchan": _deliver_serverchan,
    "pushplus": _deliver_pushplus,
    "dingtalk_bot": _deliver_dingtalk_bot,
}

#: 需要滑动窗口限流的类型（各自官方 20 条/分钟）
_RATE_LIMITED = {"wecom_bot": WECOM_RATE_PER_MIN,
                 "dingtalk_bot": DINGTALK_RATE_PER_MIN}


def deliver(cfg: Dict[str, Any], text: str, *,
            limiters: Optional[Dict[str, RateLimiter]] = None,
            timeout: float = 5.0) -> Tuple[bool, str]:
    """按渠道类型投递。返回 (ok, 附注/错误)。"""
    ctype = str(cfg.get("type") or "webhook").strip().lower()
    fn = DELIVERERS.get(ctype)
    if fn is None:
        return False, f"未知渠道类型：{ctype}（可选：{sorted(DELIVERERS)}）"
    if ctype in _RATE_LIMITED:
        limiter = (limiters or {}).setdefault(
            cfg.get("name", ""), RateLimiter(_RATE_LIMITED[ctype]))
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

    # ---- 3) 收包验证各格式
    got: List[Dict[str, Any]] = []
    forms: List[Dict[str, str]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) or b"{}"
            if "/bizerr" in self.path:
                body = json.dumps(
                    {"code": 1, "message": "额度用尽", "errcode": 1},
                    ensure_ascii=False).encode("utf-8")
            elif "/send" in self.path:      # pushplus 成功码是 200
                body = b'{"code": 200}'
            else:
                body = b'{"code": 0, "errcode": 0}'
            if "form" in self.path:
                forms.append(dict(urllib.parse.parse_qsl(
                    raw.decode("utf-8"))))
                got.append({})
            else:
                try:
                    got.append(json.loads(raw))
                except Exception:
                    got.append({})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
        # serverchan：form 表单 title/desp（SENDKEY 拼 URL 逻辑在下方单独验证）
        ok, err = deliver({"name": "t5", "type": "serverchan",
                           "webhook_url": f"http://127.0.0.1:{port}/form.send"},
                          "第一行标题\n正文内容")
        check(ok, f"serverchan 投递失败：{err}")
        check(forms[-1].get("title") == "第一行标题"
              and forms[-1].get("desp") == "第一行标题\n正文内容",
              f"serverchan form 报文：{forms[-1]}")
        # SENDKEY → 完整 URL 的拼接规则
        check(SERVERCHAN_SEND_URL.format(key="SCTdemo")
              == "https://sctapi.ftqq.com/SCTdemo.send", "SENDKEY 拼 URL 规则")
        ok, err = deliver({"name": "t5b", "type": "serverchan",
                           "webhook_url": f"{base}/bizerr.send"}, "x")
        check(not ok and "额度用尽" in err, f"serverchan 业务错误应报出：{err}")
        # pushplus：token 从 ?token= 链接提取，JSON 报文
        ok, err = deliver({"name": "t6", "type": "pushplus",
                           "webhook_url": f"http://127.0.0.1:{port}/send?token=PP123"},
                          "推送内容")
        check(ok, f"pushplus 投递失败：{err}")
        check(got[-1].get("token") == "PP123"
              and got[-1].get("content") == "推送内容",
              f"pushplus 报文：{got[-1]}")
        # pushplus：业务码非 200 如实报错
        ok, err = deliver({"name": "t6b", "type": "pushplus",
                           "webhook_url": f"{base}/bizerr?token=k"}, "x")
        check(not ok and "PushPlus" in err, f"pushplus 业务错误应报出：{err}")
        # dingtalk：加签 URL 可验证（时间戳在分钟窗内 + 签名可重算）
        secret = "SECabc123"
        signed = _dingtalk_signed_url(base, secret)
        check("timestamp=" in signed and "sign=" in signed, "加签 URL 含参数")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(signed).query)
        ts, sign = q["timestamp"][0], q["sign"][0]
        expect = base64.b64encode(hmac.new(
            secret.encode(), f"{ts}\n{secret}".encode(),
            digestmod=hashlib.sha256).digest()).decode()
        check(sign == expect, "加签签名可用 secret 重算验证")
        ok, err = deliver({"name": "t7", "type": "dingtalk_bot",
                           "webhook_url": base, "secret": secret}, "钉钉消息")
        check(ok, f"dingtalk 投递失败：{err}")
        check(got[-1].get("msgtype") == "text"
              and got[-1]["text"]["content"] == "钉钉消息",
              f"dingtalk 报文：{got[-1]}")
        # dingtalk：业务码非 0 如实报错
        ok, err = deliver({"name": "t7b", "type": "dingtalk_bot",
                           "webhook_url": f"{base}/bizerr"}, "x")
        check(not ok and "钉钉" in err, f"dingtalk 业务错误应报出：{err}")
        # 未知类型如实拒绝
        ok, err = deliver({"name": "t4", "type": "nope",
                           "webhook_url": base}, "x")
        check(not ok and "未知渠道类型" in err, "未知类型应拒绝")
    finally:
        srv.shutdown()
    return True


if __name__ == "__main__":
    print("适配器自检：", "通过" if selftest() else "失败")
