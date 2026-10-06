"""语音服务：转写（ASR）与合成（TTS），全部走 OpenAI 兼容协议。

零第三方依赖：multipart 请求体自己拼（就是一个 boundary + 两段），
省掉为了一个接口引入 requests。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional, Tuple

from ..llm.providers import PROVIDERS

#: 转写接口的默认模型（OpenAI 兼容命名，各家基本都认 whisper-1）
DEFAULT_ASR_MODEL = "whisper-1"
DEFAULT_TTS_MODEL = "tts-1"
DEFAULT_TTS_VOICE = "alloy"

#: 音频大小上限（10MB）。超过基本不是语音，多半是误传文件。
MAX_AUDIO_BYTES = 10 * 1024 * 1024


class VoiceError(Exception):
    """语音服务错误。带 code 便于前端区分降级方式。"""

    def __init__(self, message: str, code: str = "voice_error") -> None:
        super().__init__(message)
        self.code = code


def _multipart(fields: Dict[str, str], file_field: str, filename: str,
               data: bytes, mime: str) -> Tuple[bytes, str]:
    """手写一个 multipart/form-data 请求体。"""
    boundary = "----BW" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += str(v).encode("utf-8") + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n').encode()
    out += f"Content-Type: {mime}\r\n\r\n".encode()
    out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class VoiceService:
    """语音服务门面。未配置时所有方法给出明确原因，不静默失败。"""

    def __init__(self, config: Any = None) -> None:
        self.config = config

    # ------- 配置解析 ---------------------------------------------

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            v = self.config.get(key)
        except Exception:
            v = None
        return default if v is None else v

    def _key(self) -> str:
        """API Key：优先环境变量（各家 provider 都定义了 env 名），再取配置。"""
        prov = str(self._cfg("asr_provider") or self._cfg("provider") or "").lower()
        p = PROVIDERS.get(prov)
        if p is not None and p.env:
            env_v = os.environ.get(p.env, "")
            if env_v:
                return env_v
        api_key = str(self._cfg("api_key") or "")
        if api_key:
            return api_key
        return os.environ.get(p.env, "") if p and p.env else ""

    def _base(self) -> str:
        """ASR/TTS 的 base_url：专用配置 > provider 预设 > 配置里的 base_url。"""
        explicit = str(self._cfg("asr_base_url") or "")
        if explicit:
            return explicit.rstrip("/")
        prov = str(self._cfg("asr_provider") or self._cfg("provider") or "").lower()
        p = PROVIDERS.get(prov)
        if p is not None and p.base_url:
            return p.base_url.rstrip("/")
        return str(self._cfg("base_url") or "").rstrip("/")

    # ------- 状态 -------------------------------------------------

    def asr_ready(self) -> Tuple[bool, str]:
        base = self._base()
        if not base:
            return False, "未配置语音服务地址（填 provider 或 asr_base_url）"
        if not self._key():
            return False, "未配置 API Key"
        if not bool(self._cfg("asr_enabled", False)):
            return False, "语音识别未启用（设置页打开 asr_enabled）"
        return True, "就绪"

    def status(self) -> Dict[str, Any]:
        ok, why = self.asr_ready()
        return {
            "asr_enabled": bool(self._cfg("asr_enabled", False)),
            "asr_ready": ok,
            "asr_reason": why,
            "asr_model": str(self._cfg("asr_model") or DEFAULT_ASR_MODEL),
            "asr_base_url": self._base(),
            "tts_enabled": bool(self._cfg("tts_enabled", False)),
            "tts_voice": str(self._cfg("tts_voice") or ""),
            # 浏览器本地合成不需要任何后端配置，这是离线下唯一的朗读通道
            "tts_local_available": True,
            "max_audio_bytes": MAX_AUDIO_BYTES,
        }

    # ------- 转写 -------------------------------------------------

    def transcribe(self, data: bytes, mime: str = "audio/webm") -> Dict[str, Any]:
        """把一段音频转成文字。"""
        if not data:
            raise VoiceError("音频为空", "empty_audio")
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceError(f"音频过大（{len(data)} 字节，上限 {MAX_AUDIO_BYTES}）",
                             "too_large")

        ok, why = self.asr_ready()
        if not ok:
            raise VoiceError(why, "not_configured")

        ext = "webm"
        low = (mime or "").lower()
        for cand in ("webm", "mp3", "wav", "m4a", "mp4", "ogg", "flac", "mpga"):
            if cand in low:
                ext = cand
                break

        fields = {
            "model": str(self._cfg("asr_model") or DEFAULT_ASR_MODEL),
        }
        lang = str(self._cfg("asr_language") or "")
        if lang:
            fields["language"] = lang

        body, ctype = _multipart(fields, "file", f"speech.{ext}", data,
                                 mime or f"audio/{ext}")
        raw = self._post(f"{self._base()}/audio/transcriptions", body, ctype)

        text = ""
        try:
            obj = json.loads(raw.decode("utf-8", "ignore"))
            text = str((obj or {}).get("text") or "")
        except Exception:
            # 少数实现直接返回纯文本
            text = raw.decode("utf-8", "ignore").strip()
        return {"text": text.strip(), "model": fields["model"], "bytes": len(data)}

    # ------- 合成 -------------------------------------------------

    def synthesize(self, text: str, voice: str = "") -> Optional[bytes]:
        """云端合成，返回 mp3 字节；未配置返回 None（前端应回退到浏览器本地合成）。"""
        text = (text or "").strip()
        if not text:
            return None
        base = self._base()
        if not base or not self._key():
            return None
        payload = json.dumps({
            "model": str(self._cfg("tts_model") or DEFAULT_TTS_MODEL),
            "input": text[:2000],
            "voice": voice or str(self._cfg("tts_voice") or DEFAULT_TTS_VOICE),
            "response_format": "mp3",
        }).encode("utf-8")
        try:
            return self._post(f"{base}/audio/speech", payload,
                              "application/json")
        except Exception:
            # 合成失败不该拖垮对话：交给前端本地合成兜底
            return None

    # ------- 底层 -------------------------------------------------

    def _post(self, url: str, body: bytes, ctype: str, timeout: float = 60.0) -> bytes:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("Authorization", f"Bearer {self._key()}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            code = "auth_failed" if ex.code in (401, 403) else "upstream_error"
            raise VoiceError(f"语音服务返回 {ex.code}：{detail or ex.reason}", code)
        except Exception as ex:
            raise VoiceError(f"语音服务不可达：{ex}", "unreachable")


def build_voice_service(config: Any = None) -> VoiceService:
    return VoiceService(config)
