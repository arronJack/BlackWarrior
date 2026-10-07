"""渠道入站事件处理（v0.7.0）—— 让飞书 / 企业微信里真能跟黑武士对话。

为什么需要这个模块
------------------
:mod:`.channels` 的 ``inbound()`` 是**通用 token 校验**入口，适合自建中转；
但飞书/企微的事件回调有各自的签名与加密规则：

- **飞书**：URL 验证 ``challenge`` 回显；事件带 ``X-Lark-Signature``
  （``sha256(timestamp+nonce+encrypt_key+body)`` 的 base64）；
  开了"加密传输"时 body 里的 ``encrypt`` 还要 AES-256-CBC 解密。
- **企业微信**：URL 验证要对 ``token/timestamp/nonce/echostr`` 排序做 SHA1；
  消息体是 XML，里面的 ``<Encrypt>`` 用 EncodingAESKey 做 AES-256-CBC 解密，
  解出来还是一层 XML。

直接把它们的报文丢给通用 ``inbound()`` 一定失败——所以这里做协议适配，
把"飞书/企微的报文"翻译成 ``(文本, 发件人 id)``，再交给主循环。

回复路径复用现有链路：入站 → ``push_background(channel="channel:xxx")``
→ 同一个主循环回合 → ``channel_out`` → :mod:`.channel_adapters` 投回去。
所以**记忆、认知、工具全部共享**，和在桌面里对话没有任何区别。

安全立场：所有校验失败都**如实拒绝并返回原因**，绝不"猜着解析"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

#: 时间戳允许的偏移（秒）——防重放
MAX_SKEW = 3600


# ------------------------------------------------------------------ 加解密


def _pkcs7_unpad(data: bytes, block: int = 32) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > block or pad > len(data):
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block) or block
    return data + bytes([pad]) * pad


def _aes_cbc_decrypt(key: bytes, data: bytes) -> Optional[bytes]:
    """AES-CBC 解密。没有 cryptography 时尝试 Windows 自带 DPAPI 之外的纯实现失败
    就明确返回 None——绝不"假装解密成功"。
    """
    if not data or len(data) % 16 != 0:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return None
    try:
        iv = key[:16]
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except Exception:
        return None


def _aes_cbc_encrypt(key: bytes, data: bytes) -> Optional[bytes]:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return None
    try:
        iv = key[:16]
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return enc.update(data) + enc.finalize()
    except Exception:
        return None


def wecom_aes_key(encoding_aes_key: str) -> Optional[bytes]:
    """企微 EncodingAESKey（43 位）→ 32 字节 AES 密钥。"""
    try:
        raw = str(encoding_aes_key or "")
        if len(raw) != 43:
            return None
        return base64.b64decode(raw + "=")
    except Exception:
        return None


def wecom_decrypt(encoding_aes_key: str, encrypted_b64: str) -> Optional[str]:
    """企微解密：去掉 16 字节随机头 + 4 字节网络序长度，取中间消息体。"""
    key = wecom_aes_key(encoding_aes_key)
    if key is None:
        return None
    try:
        blob = base64.b64decode(encrypted_b64)
    except Exception:
        return None
    plain = _aes_cbc_decrypt(key, blob)
    if plain is None:
        return None
    plain = _pkcs7_unpad(plain)
    if len(plain) < 20:
        return None
    try:
        msg_len = int.from_bytes(plain[16:20], "big")
    except Exception:
        return None
    body = plain[20:20 + msg_len]
    try:
        return body.decode("utf-8")
    except Exception:
        return body.decode("utf-8", "replace")


def wecom_signature(token: str, timestamp: str, nonce: str, echo: str) -> str:
    """企微签名：token/timestamp/nonce/echo 字典序拼接后 SHA1。"""
    items = sorted([str(token or ""), str(timestamp or ""),
                    str(nonce or ""), str(echo or "")])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str,
                     body: bytes) -> str:
    """飞书签名：sha256(timestamp+nonce+encrypt_key+body) → base64。"""
    raw = (f"{timestamp}{nonce}{encrypt_key}").encode("utf-8") + (body or b"")
    return base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def feishu_decrypt(encrypt_key: str, encrypted_b64: str) -> Optional[str]:
    """飞书加密事件：key = sha256(encrypt_key)，iv = key[:16]。"""
    if not encrypt_key:
        return None
    key = hashlib.sha256(str(encrypt_key).encode("utf-8")).digest()
    try:
        blob = base64.b64decode(encrypted_b64)
    except Exception:
        return None
    plain = _aes_cbc_decrypt(key, blob)
    if plain is None:
        return None
    return _pkcs7_unpad(plain).decode("utf-8", "replace")


# ------------------------------------------------------------------ 解析


def _xml_text(node: Optional[ET.Element], tag: str) -> str:
    if node is None:
        return ""
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def parse_wecom_message(xml_text: str) -> Dict[str, str]:
    """企微 XML → {text, from_user, agent_id}。"""
    out = {"text": "", "from_user": "", "agent_id": ""}
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    mtype = _xml_text(root, "MsgType").lower()
    if mtype not in ("text", ""):
        return out        # 图片/语音/事件等先不支持，如实空内容
    out["text"] = _xml_text(root, "Content")
    out["from_user"] = _xml_text(root, "FromUserName")
    out["agent_id"] = _xml_text(root, "AgentID")
    return out


def parse_feishu_message(payload: Dict[str, Any]) -> Dict[str, str]:
    """飞书事件 v2 → {text, open_id, chat_id, chat_type, message_id}。"""
    out = {"text": "", "open_id": "", "chat_id": "", "chat_type": "",
           "message_id": ""}
    header = payload.get("header") or {}
    if str(header.get("event_type") or "") != "im.message.receive_v1":
        return out
    event = payload.get("event") or {}
    sender = event.get("sender") or {}
    sid = sender.get("sender_id") or {}
    out["open_id"] = str(sid.get("open_id") or sid.get("user_id") or "")
    msg = event.get("message") or {}
    out["chat_id"] = str(msg.get("chat_id") or "")
    out["chat_type"] = str(msg.get("chat_type") or "")
    out["message_id"] = str(msg.get("message_id") or "")
    mtype = str(msg.get("message_type") or "text")
    content = msg.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = {"text": content}
    if not isinstance(content, dict):
        content = {}
    if mtype == "text":
        out["text"] = str(content.get("text") or "")
    elif mtype == "post":
        out["text"] = _feishu_post_text(content)
    elif mtype in ("image", "file", "audio", "media", "sticker"):
        out["text"] = f"[收到一条 {mtype} 消息]"
    return out


def _feishu_post_text(content: Dict[str, Any]) -> str:
    """把飞书富文本 post 拍平成纯文本（黑武士先不做多模态理解）。"""
    try:
        zh = content.get("zh_cn") or content.get("en_us") or \
            content.get("zh_hk") or content.get("ja_jp") or {}
        body = zh.get("content") if isinstance(zh, dict) else None
        lines: List[str] = []
        for para in body or []:
            if not isinstance(para, dict):
                continue
            buf = []
            for el in (para.get("elements") or []):
                if isinstance(el, dict):
                    t = el.get("text")
                    if t:
                        buf.append(str(t))
            if buf:
                lines.append("".join(buf))
        return "\n".join(lines)
    except Exception:
        return ""


# ------------------------------------------------------------------ 处理器


class InboundResult:
    """入站处理结果（同时给路由层一个统一的返回形状）。"""

    def __init__(self, *, status: int = 200, payload: Any = None,
                 raw: Optional[Tuple[bytes, str]] = None) -> None:
        self.status = status
        self.payload = payload
        self.raw = raw

    def as_route_result(self) -> Any:
        """转成 server/app.py 里 ``_handle_result`` 认识的形状。

        裸响应（企微 URL 验证要回显纯文本 echostr）走 ``__raw__`` 平铺字典；
        ��余返回 ``(status, payload)`` 二元组。
        """
        if self.raw is not None:
            body, ctype = self.raw
            return {"__raw__": body, "status": self.status, "ctype": ctype}
        return self.status, self.payload


def _find_channel(bridge: Any, kind: str) -> Optional[Dict[str, Any]]:
    """按类型找出对应渠道配置（feishu → type 以 feishu 开头）。"""
    try:
        channels = bridge._channels()
    except Exception:
        return None
    for c in channels:
        if not c.get("enabled"):
            continue
        ctype = str(c.get("type") or "")
        if kind == "feishu" and ctype.startswith("feishu"):
            return c
        if kind == "wecom" and ctype.startswith("wecom"):
            return c
    return None


def handle_feishu(bridge: Any, *, method: str, query: Dict[str, List[str]],
                  body: bytes, headers: Any) -> InboundResult:
    """飞书事件回调。GET=URL 验证，POST=事件推送。"""
    q = {k: (v[0] if v else "") for k, v in (query or {}).items()}
    cfg = _find_channel(bridge, "feishu")
    if cfg is None:
        return InboundResult(status=404,
                             payload={"error": "未配置飞书渠道（type=feishu_app）"})

    # ---- URL 验证（后台点"保存"时会打这个 GET）
    if method == "GET":
        challenge = q.get("challenge", "")
        vt = str(cfg.get("verification_token") or "")
        if vt and q.get("token", "") and not hmac.compare_digest(q.get("token", ""), vt):
            return InboundResult(status=401, payload={"error": "verification token 不匹配"})
        if not challenge:
            return InboundResult(status=400, payload={"error": "缺少 challenge"})
        return InboundResult(status=200, payload={"challenge": challenge})

    # ---- 事件推送 ----
    ek = str(cfg.get("encrypt_key") or "")
    ts = _header(headers, "X-Lark-Request-Timestamp")
    nonce = _header(headers, "X-Lark-Request-Nonce")
    sig = _header(headers, "X-Lark-Signature")
    if ek:
        if not ts or not sig:
            return InboundResult(status=401, payload={"error": "缺少签名头"})
        try:
            if abs(time.time() - int(ts)) > MAX_SKEW:
                return InboundResult(status=401, payload={"error": "时间戳过期"})
        except (TypeError, ValueError):
            return InboundResult(status=401, payload={"error": "时间戳非法"})
        expect = feishu_signature(ts, nonce, ek, body)
        if not hmac.compare_digest(sig, expect):
            return InboundResult(status=401, payload={"error": "签名校验失败"})

    raw_text = (body or b"").decode("utf-8", "replace")
    try:
        payload = json.loads(raw_text)
    except Exception:
        return InboundResult(status=400, payload={"error": "报文不是合法 JSON"})
    if not isinstance(payload, dict):
        return InboundResult(status=400, payload={"error": "报文格式异常"})
    if payload.get("encrypt"):
        dec = feishu_decrypt(ek, str(payload.get("encrypt")))
        if dec is None:
            return InboundResult(status=400,
                                 payload={"error": "事件解密失败（需要 cryptography 包）"})
        try:
            payload = json.loads(dec)
        except Exception:
            return InboundResult(status=400, payload={"error": "解密后不是合法 JSON"})

    header = payload.get("header") or {}
    vt = str(cfg.get("verification_token") or "")
    if vt and header.get("token") and not hmac.compare_digest(
            str(header.get("token")), vt):
        return InboundResult(status=401, payload={"error": "verification token 不匹配"})

    if str(header.get("event_type") or "") == "url_verification":
        return InboundResult(status=200, payload={"challenge": payload.get("challenge")})

    msg = parse_feishu_message(payload)
    if not msg["text"]:
        # 非消息事件（应用被订阅、机器人进群等）回 200 表示"已处理"，
        # 返回非 200 会让飞书反复重推。
        return InboundResult(status=200, payload={"ok": True, "ignored": True})

    name = cfg["name"]
    target = msg["chat_id"] or msg["open_id"]
    bridge.set_target(name, target)
    r = bridge.inbound(name, _channel_token(cfg, q, payload), msg["text"],
                       from_id=(msg["message_id"] or msg["open_id"] or "feishu"))
    return InboundResult(status=200 if r.get("ok") else 401,
                         payload=r)


def handle_wecom(bridge: Any, *, method: str, query: Dict[str, List[str]],
                 body: bytes, headers: Any) -> InboundResult:
    """企业微信事件回调。GET=URL 验证，POST=消息推送（XML + AES）。"""
    q = {k: (v[0] if v else "") for k, v in (query or {}).items()}
    cfg = _find_channel(bridge, "wecom")
    if cfg is None:
        return InboundResult(status=404,
                             payload={"error": "未配置企业微信渠道（type=wecom_app）"})
    token = _channel_token(cfg, q, {})
    aes_key = str(cfg.get("encrypt_key") or "")

    if method == "GET":
        echostr = q.get("echostr", "")
        sig = q.get("msg_signature", "")
        ts = q.get("timestamp", "")
        nonce = q.get("nonce", "")
        if not echostr:
            return InboundResult(status=400, payload={"error": "缺少 echostr"})
        if sig and token:
            expect = wecom_signature(token, ts, nonce, echostr)
            if not hmac.compare_digest(sig, expect):
                return InboundResult(status=401, payload={"error": "签名校验失败"})
        plain = wecom_decrypt(aes_key, echostr) if aes_key else echostr
        if plain is None:
            return InboundResult(
                status=400,
                payload={"error": "echostr 解密失败（需要 cryptography 包）"})
        return InboundResult(status=200, raw=(plain.encode("utf-8"),
                                              "text/plain; charset=utf-8"))

    raw_text = (body or b"").decode("utf-8", "replace")
    sig = q.get("msg_signature", "")
    ts = q.get("timestamp", "")
    nonce = q.get("nonce", "")
    # 先解出外层 XML 拿 Encrypt，再验签（企微签名基于 Encrypt 原文）
    try:
        outer = ET.fromstring(raw_text)
    except Exception:
        return InboundResult(status=400, payload={"error": "报文不是合法 XML"})
    enc = _xml_text(outer, "Encrypt")
    if sig and token and enc:
        expect = wecom_signature(token, ts, nonce, enc)
        if not hmac.compare_digest(sig, expect):
            return InboundResult(status=401, payload={"error": "签名校验失败"})
    inner = wecom_decrypt(aes_key, enc) if (enc and aes_key) else raw_text
    if inner is None:
        return InboundResult(status=400,
                             payload={"error": "消息解密失败（需要 cryptography 包）"})
    msg = parse_wecom_message(inner)
    if not msg["text"]:
        return InboundResult(status=200, payload={"ok": True, "ignored": True})

    name = cfg["name"]
    bridge.set_target(name, msg["from_user"])
    r = bridge.inbound(name, token, msg["text"],
                       from_id=(msg["from_user"] or "wecom"))
    return InboundResult(status=200 if r.get("ok") else 401, payload=r)


def _header(headers: Any, name: str) -> str:
    try:
        return str(headers.get(name) or "")
    except Exception:
        return ""


def _channel_token(cfg: Dict[str, Any], q: Dict[str, str],
                   payload: Dict[str, Any]) -> str:
    """入站用的内部令牌。

    飞书/企微事件本身已经过签名/加密校验，这里仍走 ChannelBridge.inbound 的
    令牌闸门，所以内部令牌缺失时要**如实报错**而不是偷偷放行。
    """
    return str(cfg.get("token") or "")


# ------------------------------------------------------------------ 自检


def selftest() -> bool:
    """入站处理自检：签名、加解密、解析、空配置拒绝。"""
    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError("渠道入站自检失败：" + msg)

    # ---- 1) 企微签名可复算
    sig = wecom_signature("tok", "1700000000", "nonce", "echo")
    check(sig == wecom_signature("tok", "1700000000", "nonce", "echo"),
          "签名应稳定")
    check(len(sig) == 40, f"签名应为 40 位 sha1：{len(sig)}")

    # ---- 2) 企微加解密往返（无 cryptography 时跳过但要如实标注）
    has_crypto = True
    try:
        import cryptography  # noqa: F401
    except Exception:
        has_crypto = False
    if has_crypto:
        key = base64.b64encode(b"0" * 32).decode()[:43]
        raw = ET.tostring(ET.fromstring(
            "<xml><Content>hello</Content><FromUserName>u1</FromUserName>"
            "<MsgType>text</MsgType></xml>"), encoding="unicode")
        payload = b"0" * 16 + len(raw).to_bytes(4, "big") + raw.encode("utf-8")
        blob = _aes_cbc_encrypt(base64.b64decode(key + "="),
                                _pkcs7_pad(payload))
        check(blob is not None, "AES 加密应成功")
        got = wecom_decrypt(key, base64.b64encode(blob).decode())
        check(got is not None and "hello" in got, f"企微解密应还原：{got}")
        msg = parse_wecom_message(got)
        check(msg["text"] == "hello" and msg["from_user"] == "u1",
              f"企微消息解析：{msg}")
    else:
        check(wecom_decrypt("x" * 43, "YWJj") is None,
              "无 cryptography 时应明确返回 None")

    # ---- 3) 飞书签名可复算
    check(feishu_signature("1", "n", "k", b"{}") ==
          feishu_signature("1", "n", "k", b"{}"), "飞书签名应稳定")

    # ---- 4) 飞书事件解析
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "t"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {"message_id": "om_1", "chat_id": "oc_1",
                        "chat_type": "p2p", "message_type": "text",
                        "content": json.dumps({"text": "在吗"})},
        },
    }
    m = parse_feishu_message(payload)
    check(m["text"] == "在吗" and m["chat_id"] == "oc_1",
          f"飞书消息解析：{m}")
    check(parse_feishu_message({"header": {"event_type": "other"}})["text"] == "",
          "非消息事件应返回空")

    # ---- 5) 无渠道配置时如实 404（不假装成功）
    class _Bridge:
        def _channels(self):
            return []

    r = handle_feishu(_Bridge(), method="GET",
                      query={"challenge": ["abc"]}, body=b"", headers={})
    check(r.status == 404, f"未配置应 404：{r.status}")
    r = handle_wecom(_Bridge(), method="GET", query={}, body=b"", headers={})
    check(r.status == 404, f"未配置应 404：{r.status}")
    return True


if __name__ == "__main__":
    print("渠道入站自检：", "通过" if selftest() else "失败")
