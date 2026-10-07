"""LLM 网关 —— 一套代码接住所有 OpenAI 兼容服务商。

用标准库实现（``urllib.request`` + 逐行读 SSE），不依赖 requests/openai sdk，
这样黑武士在最小环境里也能跑起来。

流式
----
``stream()`` 是一个生成器，产出统一事件::

    {"type": "delta",      "text": "..."}            增量文本
    {"type": "tool_calls", "calls": [...]}           工具调用（已拼装完整）
    {"type": "done",       "finish_reason": "stop"}   结束
    {"type": "error",      "error": "...", "rate_limited": True}

``abort`` 是一个 ``threading.Event``：主循环抢占时置位，流式读取会立刻停下——
这是"用户改主意了要能打断"在传输层的实现。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from .providers import resolve


class LLMError(Exception):
    """LLM 调用失败。"""

    def __init__(self, message: str, *, status: int = 0,
                 rate_limited: bool = False, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited
        self.retry_after = retry_after


class LLMGateway:
    """统一模型调用门面。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.last_error: str = ""
        self.last_status: int = 0
        self.total_calls = 0
        self.total_tokens = 0

    # ------- 配置 -------------------------------------------------

    def _provider(self):
        try:
            return resolve(str(self.config.get("provider", "deepseek") or ""),
                           str(self.config.get("base_url", "") or ""))
        except Exception:
            return resolve("custom")

    def _api_key(self) -> str:
        try:
            return str(self.config.get("api_key", "") or "").strip()
        except Exception:
            return ""

    def _model(self) -> str:
        try:
            m = str(self.config.get("model", "") or "").strip()
        except Exception:
            m = ""
        return m or self._provider().default_model

    def _timeout(self) -> float:
        try:
            return float(self.config.get("timeout", 120) or 120)
        except Exception:
            return 120.0

    def endpoint(self) -> str:
        return self._provider().base_url + "/chat/completions"

    def ready(self) -> bool:
        """是否可以发起调用。"""
        p = self._provider()
        if not p.requires_key:
            return bool(p.base_url)
        return bool(self._api_key()) and bool(p.base_url)

    def readiness(self) -> Dict[str, Any]:
        p = self._provider()
        missing = []
        if not p.base_url:
            missing.append("base_url")
        if p.requires_key and not self._api_key():
            missing.append("api_key")
        return {
            "ready": not missing,
            "provider": p.key,
            "model": self._model(),
            "base_url": p.base_url,
            "missing": missing,
        }

    # ------- 请求体 -----------------------------------------------

    def _payload(self, messages: List[Dict[str, Any]], *,
                 tools: Optional[List[Dict[str, Any]]] = None,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None,
                 stream: bool = False) -> Dict[str, Any]:
        try:
            temp = float(temperature if temperature is not None
                         else self.config.get("temperature", 0.7))
        except Exception:
            temp = 0.7
        try:
            mt = int(max_tokens if max_tokens is not None
                     else self.config.get("max_tokens", 4096))
        except Exception:
            mt = 4096

        body: Dict[str, Any] = {
            "model": self._model(),
            "messages": messages,
            "temperature": temp,
            "max_tokens": mt,
            "stream": bool(stream),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    # ------- 非流式 -----------------------------------------------

    def complete(self, messages: List[Dict[str, Any]], *,
                 tools: Optional[List[Dict[str, Any]]] = None,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None,
                 abort: Optional[threading.Event] = None) -> Dict[str, Any]:
        """一次性完成。返回 ``{content, tool_calls, finish_reason, usage}``。"""
        payload = self._payload(messages, tools=tools, temperature=temperature,
                                max_tokens=max_tokens, stream=False)
        raw = self._post(payload, abort=abort)
        self.total_calls += 1

        try:
            choice = (raw.get("choices") or [{}])[0]
        except Exception:
            choice = {}
        msg = choice.get("message") or {}
        usage = raw.get("usage") or {}
        try:
            self.total_tokens += int(usage.get("total_tokens") or 0)
        except Exception:
            pass
        return {
            "content": str(msg.get("content") or ""),
            "tool_calls": msg.get("tool_calls") or [],
            "finish_reason": str(choice.get("finish_reason") or ""),
            "usage": usage,
            "raw": raw,
        }

    # ------- 流式 -------------------------------------------------

    def stream(self, messages: List[Dict[str, Any]], *,
               tools: Optional[List[Dict[str, Any]]] = None,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None,
               abort: Optional[threading.Event] = None) -> Iterator[Dict[str, Any]]:
        """流式生成。产出统一事件字典。"""
        payload = self._payload(messages, tools=tools, temperature=temperature,
                                max_tokens=max_tokens, stream=True)
        self.total_calls += 1

        req = urllib.request.Request(
            self.endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout())
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "ignore")[:500]
            except Exception:
                pass
            raise self._as_error(ex.code, body) from None
        except Exception as ex:
            raise LLMError(f"{type(ex).__name__}: {ex}") from None

        calls: Dict[int, Dict[str, Any]] = {}
        try:
            with resp:
                for raw_line in resp:
                    if abort is not None and abort.is_set():
                        yield {"type": "error", "error": "已中止", "aborted": True}
                        return
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    for ev in self._parse_chunk(chunk, calls):
                        yield ev
                if calls:
                    yield {"type": "tool_calls",
                           "calls": self._finalize_calls(calls)}
                yield {"type": "done", "finish_reason": "stop"}
        except urllib.error.HTTPError as ex:  # pragma: no cover
            yield {"type": "error", "error": f"HTTP {ex.code}"}
        except Exception as ex:
            yield {"type": "error", "error": f"{type(ex).__name__}: {ex}"}

    def _parse_chunk(self, chunk: Dict[str, Any],
                     calls: Dict[int, Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        """解析一个 SSE chunk，产出增量事件。"""
        choices = chunk.get("choices") or []
        if not choices:
            usage = chunk.get("usage")
            if usage:
                try:
                    self.total_tokens += int(usage.get("total_tokens") or 0)
                except Exception:
                    pass
            return
        delta = choices[0].get("delta") or {}
        finish = choices[0].get("finish_reason")

        content = delta.get("content")
        if content:
            yield {"type": "delta", "text": content}

        # 工具调用增量（不同服务商字段略有差异，这里做兼容拼装）
        inc = delta.get("tool_calls") or []
        if not inc and "function_call" in delta:
            fc = delta.get("function_call") or {}
            inc = [{"index": 0, "id": fc.get("id", ""),
                    "function": {"name": fc.get("name", ""),
                                 "arguments": fc.get("arguments", "")}}]
        for item in inc:
            idx = int(item.get("index") or 0)
            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                slot["id"] = item["id"]
            fn = item.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn.get("arguments") or ""

        if finish:
            yield {"type": "done", "finish_reason": str(finish)}

    @staticmethod
    def _finalize_calls(calls: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for idx in sorted(calls.keys()):
            c = calls[idx]
            name = c.get("name") or ""
            if not name:
                continue
            args: Any = {}
            raw = c.get("arguments") or ""
            if raw.strip():
                try:
                    args = json.loads(raw)
                except Exception:
                    args = {"_raw": raw}
            out.append({
                "id": c.get("id") or f"call_{idx}",
                "name": name,
                "args": args if isinstance(args, dict) else {"value": args},
                # 原始参数字符串：工具循环里要把 assistant(tool_calls) 消息
                # 重新拼回 messages 喂给下一轮，arguments 必须是 JSON 字符串，
                # 这里直接保留流式拼装出的原文，避免二次序列化失真。
                "raw": (c.get("arguments") or "").strip(),
            })
        return out

    # ------- 底层 -------------------------------------------------

    def _post(self, payload: Dict[str, Any],
              abort: Optional[threading.Event] = None) -> Dict[str, Any]:
        if abort is not None and abort.is_set():
            raise LLMError("已中止", status=499)
        req = urllib.request.Request(
            self.endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                self.last_status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "ignore")[:800]
            except Exception:
                pass
            raise self._as_error(ex.code, body) from None
        except Exception as ex:
            raise LLMError(f"{type(ex).__name__}: {ex}") from None

        try:
            return json.loads(raw)
        except Exception:
            raise LLMError(f"响应不是合法 JSON：{raw[:200]}")

    def _as_error(self, status: int, body: str) -> "LLMError":
        """把 HTTP 错误转成 LLMError，识别限流。"""
        rate_limited = status == 429
        retry_after = 0.0
        msg = f"HTTP {status}"
        try:
            parsed = json.loads(body)
            err = parsed.get("error") or {}
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or msg)
            elif isinstance(err, str):
                msg = err
        except Exception:
            if body:
                msg = body[:200]
        self.last_error = msg
        self.last_status = status
        if rate_limited:
            retry_after = 600.0
        return LLMError(msg, status=status, rate_limited=rate_limited,
                        retry_after=retry_after)

    # ------- 连通性 -----------------------------------------------

    def ping(self, timeout: float = 15.0) -> Dict[str, Any]:
        """用一条极短消息探测连通性（设置页"测试连接"用）。"""
        if not self.ready():
            return {"ok": False, "error": "未配置 API Key 或 base_url",
                    **self.readiness()}
        started = time.time()
        try:
            res = self.complete(
                [{"role": "user", "content": "回复两个字：就绪"}],
                max_tokens=16, temperature=0.0,
            )
            return {"ok": True, "latency_ms": int((time.time() - started) * 1000),
                    "reply": res.get("content", "")[:50], **self.readiness()}
        except LLMError as ex:
            return {"ok": False, "error": str(ex), "status": ex.status,
                    "rate_limited": ex.rate_limited, **self.readiness()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}",
                    **self.readiness()}

    def stats(self) -> Dict[str, Any]:
        return {
            "calls": self.total_calls,
            "tokens": self.total_tokens,
            "last_error": self.last_error,
            "last_status": self.last_status,
            **self.readiness(),
        }
