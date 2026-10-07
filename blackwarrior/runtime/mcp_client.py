"""MCP 客户端（v0.7.0）—— 让黑武士接入 Model Context Protocol 工具生态。

为什么需要它
------------
黑武士自带 60 来个工具，但生态里还有成千上万个工具以 **MCP 服务器**
的形式存在（文件系统、GitHub、浏览器、数据库、Figma、Playwright…）。
不接 MCP，就等于把这些能力全部拒之门外；接了，配置一行就能加一个服务器。

协议要点（不引第三方依赖，纯标准库）
------------------------------------
MCP = JSON-RPC 2.0 over stdio 或 HTTP。握手三步::

    → {"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"BlackWarrior","version":"..."}}}
    ← {"result":{"protocolVersion":...,"capabilities":{...},"serverInfo":{...}}}
    → {"jsonrpc":"2.0","method":"notifications/initialized"}   # 通知，无 id

之后 ``tools/list`` 拿工具清单，``tools/call`` 调工具。

stdio 传输的**分帧**有个坑：规范说"按行分隔"，但不少实现用了 LSP 风格的
``Content-Length`` 头。两种都收��，否则换个服务器实现就连不上（表现为
"启动了但一个工具都没有"）。

安全立场
--------
MCP 服务器是**用户显式配置的外部进程**，与内置工具不同：
- 默认 risk=caution（可能有副作用），服务器声明 ``readOnlyHint`` 才降为 safe；
- 绝不因为 MCP 工具而放宽文件系统沙箱——MCP 只是多了一批**工具**，
  路径边界由 policy 层统一裁决，MCP 工具的 ``path`` 参数同样受检。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

#: MCP 协议版本（对齐 2024-11-05，兼容性最好）
PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "BlackWarrior"

#: 工具名前缀，避免与内置工具重名
NAME_PREFIX = "mcp"

#: OpenAI function-calling 的工具名上限
MAX_TOOL_NAME = 64

_SAFE_NAME_RE = re.compile(r"[^0-9a-zA-Z_-]+")


# ====================================================================== 工具


class MCPError(RuntimeError):
    """MCP 协议/传输层错误。消息里必须带可读原因（会被回灌给模型）。"""


def _sanitize(name: str) -> str:
    """把任意工具名规整成 OpenAI 允许的 ``[A-Za-z0-9_-]{1,64}``。"""
    out = _SAFE_NAME_RE.sub("_", str(name or "tool")).strip("_")
    return out or "tool"


def _qualified(server: str, tool: str, taken: set) -> str:
    """生成全局唯一工具名：``mcp__<server>__<tool>``（超长则截断+哈希）。"""
    base = f"{NAME_PREFIX}__{_sanitize(server)}__{_sanitize(tool)}"
    if len(base) <= MAX_TOOL_NAME:
        cand, i = base, 1
    else:
        keep = MAX_TOOL_NAME - 9          # 预留 "_{:08x}" + 余量
        stem = base[:max(8, keep)]
        import hashlib

        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        cand = f"{stem}_{digest}"
    while cand in taken:
        i += 1
        suffix = f"_{i}"
        cand = cand[: MAX_TOOL_NAME - len(suffix)] + suffix
    taken.add(cand)
    return cand


def _normalize_schema(schema: Any) -> Dict[str, Any]:
    """把 MCP 的 inputSchema 规整成 OpenAI function parameters。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    out = dict(schema)
    out.setdefault("type", "object")
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    req = out.get("required")
    out["required"] = [str(x) for x in req] if isinstance(req, list) else []
    return out


def _flatten_result(result: Any) -> str:
    """MCP ``tools/call`` 返回 → 给模型看的文本。"""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)
    parts: List[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        t = str(item.get("type") or "")
        if t == "text":
            parts.append(str(item.get("text") or ""))
        elif t == "resource":
            res = item.get("resource") or {}
            parts.append(f"[resource] {res.get('uri', '')}\n"
                         f"{res.get('text') or res.get('blob') or ''}")
        else:
            parts.append(json.dumps(item, ensure_ascii=False, default=str))
    text = "\n".join(p for p in parts if p)
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            text = json.dumps(structured, ensure_ascii=False, indent=2,
                              default=str)
    return text or "(无内容)"


# ==================================================================== 传输层


class _BaseTransport:
    """传输层接口。"""

    def start(self) -> None: ...
    def close(self) -> None: ...
    def request(self, method: str, params: Any = None,
                timeout: float = 30.0) -> Any: ...
    def notify(self, method: str, params: Any = None) -> None: ...


class StdioTransport(_BaseTransport):
    """stdio 传输：拉起子进程，用 JSON-RPC 说话。

    分帧同时兼容两种：按行（``{"jsonrpc":...}\\n``）与
    ``Content-Length: N\\r\\n\\r\\n<N 字节>``。
    """

    def __init__(self, command: List[str], *, env: Optional[Dict[str, str]] = None,
                 cwd: str = "", timeout: float = 60.0) -> None:
        self.command = [str(c) for c in command if str(c) != ""]
        self.env = dict(env or {})
        self.cwd = str(cwd or "")
        self.timeout = float(timeout)
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rid = 0
        self._rid_lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.stderr_tail: List[str] = []

    # ---- 生命周期 ----

    def start(self) -> None:
        if not self.command:
            raise MCPError("stdio 服务器缺少 command")
        env = dict(os.environ)
        for k, v in self.env.items():
            env[str(k)] = str(v)
        kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "bufsize": 0,
        }
        if self.cwd and os.path.isdir(self.cwd):
            kwargs["cwd"] = self.cwd
        if os.name == "nt":
            # Windows 下必须隐藏控制台窗口，否则每次接 MCP 都弹黑框
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        try:
            self._proc = subprocess.Popen(self.command, **kwargs)
        except FileNotFoundError as ex:
            raise MCPError(
                f"找不到可执行文件：{self.command[0]}（{ex}）。"
                "stdio 型 MCP 需要先装好对应的运行时（常见是 npx / uvx / python）") from ex
        except Exception as ex:
            raise MCPError(f"启动 MCP 子进程失败：{type(ex).__name__}: {ex}") from ex

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="bw-mcp-reader", daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr,
                         name="bw-mcp-stderr", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 唤醒所有等待者，别让调用方干等到超时
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = "MCP 连接已关闭"
            slot["event"].set()

    # ---- 读写 ----

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                if self._stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-20]      # 只留最近 20 行
        except Exception:
            pass

    def _read_loop(self) -> None:
        """后台读 stdout，按帧切出 JSON-RPC 消息派发。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while not self._stop.is_set():
                line = stream.readline()
                if not line:
                    break     # 进程退出
                if line.strip().lower().startswith(b"content-length:"):
                    body = self._read_framed_body(stream, line)
                    if body:
                        self._dispatch_raw(body)
                    continue
                self._dispatch_raw(line)
        except Exception:
            pass
        finally:
            self._fail_all("MCP 服务器连接中断")

    def _read_framed_body(self, stream: Any, first: bytes) -> bytes:
        """读 ``Content-Length`` 帧：头部到空行，再精确读 N 字节。"""
        headers: Dict[str, str] = {}
        line = first
        while line and line.strip():
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip().decode("ascii", "ignore")
            nxt = stream.readline()
            if not nxt:
                return b""
            line = nxt
        try:
            length = int(headers.get("content-length", "0") or 0)
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        buf = b""
        while len(buf) < length:
            chunk = stream.read(length - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _dispatch_raw(self, raw: bytes) -> None:
        raw = (raw or b"").strip()
        if not raw:
            return
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return      # 服务器打了非 JSON 噪声，忽略（不猜）
        if isinstance(msg, list):
            for m in msg:
                self._dispatch(m)
        else:
            self._dispatch(msg)

    def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        mid = msg.get("id")
        # 服务器主动发来的**请求**（roots/list、sampling/…）：我们一律回
        # method not found。拖着不回会让某些服务器一直等。
        if mid is not None and msg.get("method"):
            self._write({"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601,
                                   "message": "BlackWarrior 不支持该服务器请求"}})
            return
        if mid is None:
            return      # 通知（logging、progress…），不需要回应
        with self._lock:
            slot = self._pending.pop(mid, None)
        if slot is None:
            return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            if isinstance(err, dict):
                slot["error"] = f"{err.get('code', '')} {err.get('message', '')}".strip()
            else:
                slot["error"] = str(err)
        else:
            slot["result"] = msg.get("result")
        slot["event"].set()

    def _fail_all(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = reason
            slot["event"].set()

    def _write(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("MCP 子进程未启动")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except Exception as ex:
            raise MCPError(f"写 MCP 子进程失败：{type(ex).__name__}: {ex}") from ex

    def next_id(self) -> int:
        with self._rid_lock:
            self._rid += 1
            return self._rid

    def request(self, method: str, params: Any = None,
                timeout: float = 30.0) -> Any:
        rid = self.next_id()
        slot: Dict[str, Any] = {"event": threading.Event(),
                                "result": None, "error": None}
        with self._lock:
            self._pending[rid] = slot
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
        except Exception:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            tail = "；".join(self.stderr_tail[-3:])
            raise MCPError(f"MCP 调用 {method} 超时（{timeout}s）"
                           + (f"。服务器日志：{tail}" if tail else ""))
        if slot["error"]:
            raise MCPError(f"MCP 调用 {method} 失败：{slot['error']}")
        return slot["result"]

    def notify(self, method: str, params: Any = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)


class HttpTransport(_BaseTransport):
    """Streamable HTTP 传输（POST JSON-RPC，响应可能是 JSON 或 SSE 流）。

    SSE 响应体是长连接，必须读到**属于本次 id 的那条消息**就停手；
    否则会一直挂到服务端关流（表现为"调用卡死"）。
    """

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 60.0) -> None:
        self.url = str(url or "")
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self._rid = 0
        self._rid_lock = threading.Lock()
        self._session_id = ""
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise MCPError(f"http 型 MCP 的 url 必须以 http(s):// 开头：{self.url}")

    def close(self) -> None:
        self._stop.set()

    def next_id(self) -> int:
        with self._rid_lock:
            self._rid += 1
            return self._rid

    def _post(self, payload: Dict[str, Any], rid: Optional[int],
              timeout: float) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=headers,
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise MCPError(f"MCP HTTP {ex.code}：{body or ex.reason}") from ex
        except Exception as ex:
            raise MCPError(f"MCP HTTP 请求失败：{type(ex).__name__}: {ex}") from ex

        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                return self._read_sse(resp, rid, timeout)
            raw = resp.read() or b"{}"
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                return None

    def _read_sse(self, resp: Any, rid: Optional[int], timeout: float) -> Any:
        deadline = time.time() + timeout
        buf: List[str] = []
        try:
            for raw in resp:
                if self._stop.is_set() or time.time() > deadline:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                elif not line and buf:
                    blob = "\n".join(buf)
                    buf = []
                    msg = _try_json(blob)
                    if msg is None:
                        continue
                    if rid is None:
                        return msg            # 通知/响应都行，先到先回
                    if isinstance(msg, dict) and msg.get("id") == rid:
                        return msg
        except Exception:
            pass
        return None

    def request(self, method: str, params: Any = None,
                timeout: float = 30.0) -> Any:
        rid = self.next_id()
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._post(payload, rid, timeout)
        if msg is None:
            raise MCPError(f"MCP 调用 {method} 无响应（{timeout}s 内未收到匹配响应）")
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"]
            text = f"{err.get('code', '')} {err.get('message', '')}".strip() \
                if isinstance(err, dict) else str(err)
            raise MCPError(f"MCP 调用 {method} 失败：{text}")
        return msg.get("result") if isinstance(msg, dict) else None

    def notify(self, method: str, params: Any = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._post(payload, None, min(10.0, self.timeout))
        except Exception:
            pass      # 通知失败不应影响主流程


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


# ==================================================================== 客户端


class MCPClient:
    """一个 MCP 服务器的客户端。"""

    def __init__(self, spec: Dict[str, Any]) -> None:
        self.name = _sanitize(spec.get("name") or "mcp")
        self.raw_name = str(spec.get("name") or self.name)
        self.transport_kind = str(
            spec.get("transport") or ("http" if spec.get("url") else "stdio")
        ).strip().lower()
        self.timeout = float(spec.get("timeout") or 60.0)
        self.risk_override = str(spec.get("risk") or "")
        self.spec = dict(spec)
        self.tools: List[Dict[str, Any]] = []
        self.error = ""
        self.connected = False
        self.server_info: Dict[str, Any] = {}
        self._transport: Optional[_BaseTransport] = None
        self._lock = threading.RLock()

    # ---- 连接 ----

    def _build_transport(self) -> _BaseTransport:
        if self.transport_kind == "http":
            return HttpTransport(self.spec.get("url", ""),
                                 headers=self.spec.get("headers"),
                                 timeout=self.timeout)
        return StdioTransport(self.spec.get("command") or [],
                              env=self.spec.get("env"),
                              cwd=self.spec.get("cwd", ""),
                              timeout=self.timeout)

    def connect(self) -> None:
        """握手 + 拉工具清单。失败抛 :class:`MCPError`，由调用方记录。"""
        with self._lock:
            if self.connected:
                return
            t = self._build_transport()
            t.start()
            self._transport = t
            try:
                result = t.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": CLIENT_NAME, "version": _self_version()},
                }, timeout=min(30.0, self.timeout))
                info = (result or {}).get("serverInfo") if isinstance(result, dict) else None
                self.server_info = dict(info or {})
                try:
                    t.notify("notifications/initialized")
                except Exception:
                    pass
                listed = t.request("tools/list", {}, timeout=min(30.0, self.timeout))
                raw = (listed or {}).get("tools") if isinstance(listed, dict) else None
                self.tools = [x for x in (raw or []) if isinstance(x, dict)]
                self.connected = True
            except Exception as ex:
                try:
                    t.close()
                except Exception:
                    pass
                self._transport = None
                self.connected = False
                raise MCPError(str(ex)) from ex

    def close(self) -> None:
        with self._lock:
            t, self._transport = self._transport, None
            self.connected = False
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    # ---- 调用 ----

    def call_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if not self.connected:
            raise MCPError(f"MCP 服务器 {self.raw_name} 未连接")
        t = self._transport
        if t is None:
            raise MCPError(f"MCP 服务器 {self.raw_name} 传输层缺失")
        result = t.request("tools/call",
                           {"name": tool_name, "arguments": dict(args or {})},
                           timeout=self.timeout)
        text = _flatten_result(result)
        if isinstance(result, dict) and result.get("isError"):
            raise MCPError(f"[{self.raw_name}/{tool_name}] {text}")
        return text

    # ---- 工具注册 ----

    def _risk_for(self, tool: Dict[str, Any]) -> str:
        from ..tools.registry import RISK_CAUTION, RISK_DANGER, RISK_SAFE

        if self.risk_override in (RISK_SAFE, RISK_CAUTION, RISK_DANGER):
            return self.risk_override
        ann = tool.get("annotations") or {}
        if isinstance(ann, dict):
            if ann.get("readOnlyHint") is True:
                return RISK_SAFE
            if ann.get("destructiveHint") is True:
                return RISK_DANGER
        return RISK_CAUTION

    def register_tools(self, reg: Any) -> List[str]:
        """把该服务器的工具注册进注册表，返回注册的工具名。"""
        from ..tools.registry import RISK_CAUTION   # noqa: F401  (分类用)

        taken: set = set(reg.names())
        registered: List[str] = []
        for tool in self.tools:
            tname = str(tool.get("name") or "").strip()
            if not tname:
                continue
            fname = _qualified(self.name, tname, taken)
            desc = str(tool.get("description") or
                       f"{self.raw_name} MCP 服务器提供的工具 {tname}")
            desc = (f"[MCP:{self.raw_name}] {desc}")[:1024]
            schema = _normalize_schema(tool.get("inputSchema"))

            def _make(tool_ref: str) -> Any:
                def _handler(**kwargs: Any) -> str:
                    return self.call_tool(tool_ref, kwargs)
                _handler.__name__ = _sanitize(fname)
                return _handler

            try:
                reg.register(
                    fname, _make(tname),
                    description=desc, parameters=schema,
                    risk=self._risk_for(tool), category="mcp",
                    aliases=[], timeout=self.timeout + 5.0)
                registered.append(fname)
            except Exception:
                continue
        return registered

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.raw_name,
            "transport": self.transport_kind,
            "connected": self.connected,
            "tools": len(self.tools),
            "server_info": self.server_info,
            "error": self.error,
        }


def _self_version() -> str:
    try:
        from ..version import __version__

        return str(__version__)
    except Exception:
        return "0"


# ==================================================================== 管理器


class MCPManager:
    """MCP 服务器总管：按配置连接、注册工具、汇总状态。

    设计要点：**任何一个服务器失败都不影响其它服务器，更不影响主程序启动**。
    连接在后台线程里做（有超时），所以 `mcp_servers` 里写错命令不会卡住开机。
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.clients: Dict[str, MCPClient] = {}
        self.registered: List[str] = []
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None

    # ---- 配置 ----

    def specs(self) -> List[Dict[str, Any]]:
        try:
            raw = self.config.get("mcp_servers", []) or []
        except Exception:
            raw = []
        out: List[Dict[str, Any]] = []
        for s in raw if isinstance(raw, list) else []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            if s.get("enabled") is False:
                continue
            out.append(dict(s))
        return out

    def enabled(self) -> bool:
        try:
            return bool(self.config.get("mcp_enabled", True))
        except Exception:
            return True

    # ---- 生命周期 ----

    def start(self, *, reg: Any = None, background: bool = True) -> None:
        """连接所有服务器并注册工具。

        ``reg`` 传入注册表则连接后立刻把工具挂上去。
        ``background=True`` 时另起线程，主流程不阻塞（推荐）——
        配置里写错命令不会卡住开机。
        """
        if not self.enabled() or not self.specs():
            return
        if background:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return
            self._thread = threading.Thread(
                target=self._start_blocking, args=(reg,),
                name="bw-mcp-boot", daemon=True)
            self._thread.start()
        else:
            self._start_blocking(reg)

    def _start_blocking(self, reg: Any = None) -> None:
        for spec in self.specs():
            client = MCPClient(spec)
            try:
                client.connect()
            except Exception as ex:
                client.error = str(ex)
                client.connected = False
            with self._lock:
                self.clients[client.name] = client
        if reg is not None:
            self.register_all(reg)

    def register_all(self, reg: Any) -> List[str]:
        """把所有已连接服务器的工具注册进 ``reg``，返回注册的工具名。"""
        with self._lock:
            clients = list(self.clients.values())
        registered: List[str] = []
        for client in clients:
            if not client.connected:
                continue
            try:
                registered.extend(client.register_tools(reg))
            except Exception as ex:
                client.error = f"工具注册失败：{type(ex).__name__}: {ex}"
        with self._lock:
            self.registered = registered
        return registered

    def stop_all(self) -> None:
        with self._lock:
            clients = list(self.clients.values())
            self.clients.clear()
            self.registered = []
        for c in clients:
            try:
                c.close()
            except Exception:
                pass

    def unregister(self, reg: Any) -> None:
        """从注册表里摘掉所有 MCP 工具（重建工具集时先摘再挂）。"""
        with self._lock:
            names = list(self.registered)
            self.registered = []
        for n in names:
            try:
                reg.unregister(n)
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        with self._lock:
            items = [c.status() for c in self.clients.values()]
        connected = [i for i in items if i["connected"]]
        return {
            "enabled": self.enabled(),
            "configured": len(self.specs()),
            "connected": len(connected),
            "tools": sum(i["tools"] for i in connected),
            "registered": len(self.registered),
            "servers": items,
        }


# ====================================================================== 自检


def _fake_stdio_server() -> str:
    """生成一个会说 MCP 的假服务器源码（自检用，零外部依赖）。"""
    return (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method'); i = msg.get('id')\n"
        "    if i is None:\n"
        "        continue\n"
        "    if m == 'initialize':\n"
        "        r = {'protocolVersion': '2024-11-05',\n"
        "             'capabilities': {'tools': {}},\n"
        "             'serverInfo': {'name': 'fake', 'version': '1'}}\n"
        "    elif m == 'tools/list':\n"
        "        r = {'tools': [{'name': 'echo',\n"
        "                        'description': 'echo back',\n"
        "                        'annotations': {'readOnlyHint': True},\n"
        "                        'inputSchema': {'type': 'object',\n"
        "                            'properties': {'text': {'type': 'string'}},\n"
        "                            'required': ['text']}}]}\n"
        "    elif m == 'tools/call':\n"
        "        a = (msg.get('params') or {}).get('arguments') or {}\n"
        "        r = {'content': [{'type': 'text',\n"
        "                          'text': 'echo: ' + str(a.get('text', ''))}]}\n"
        "    else:\n"
        "        r = {}\n"
        "    sys.stdout.write(json.dumps("
        "{'jsonrpc':'2.0','id':i,'result':r}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )


def selftest() -> bool:
    """MCP 客户端自检：分帧解析 / 工具注册 / 调用往返 / 名称规整。"""
    import sys
    import tempfile

    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError("MCP 自检失败：" + msg)

    # ---- 1) 工具名规整与唯一化
    taken: set = set()
    n1 = _qualified("fs", "read_file", taken)
    check(n1 == "mcp__fs__read_file", f"工具名拼接规则：{n1}")
    n2 = _qualified("fs", "read_file", taken)
    check(n2 != n1, "同名工具应自动去重")
    long_name = _qualified("a" * 40, "b" * 40, taken)
    check(len(long_name) <= MAX_TOOL_NAME, f"超长名应被截断：{len(long_name)}")

    # ---- 2) 结果扁平化
    check(_flatten_result({"content": [{"type": "text", "text": "hi"}]}) == "hi",
          "文本内容应被提取")
    check('"a"' in _flatten_result({"structuredContent": {"a": 1}}),
          "structuredContent 应回退成 JSON")

    # ---- 3) 真跑一个假 MCP 服务器（stdio 往返）
    tmp = tempfile.mkdtemp(prefix="bw-mcp-")
    script = os.path.join(tmp, "fake_mcp.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_fake_stdio_server())

    client = MCPClient({"name": "fake", "transport": "stdio",
                        "command": [sys.executable, script], "timeout": 20})
    try:
        client.connect()
        check(client.connected, f"假服务器应连接成功：{client.error}")
        check(len(client.tools) == 1, f"应拉到 1 个工具：{len(client.tools)}")
        text = client.call_tool("echo", {"text": "黑武士"})
        check("黑武士" in text, f"调用结果应回显：{text}")

        from ..tools.registry import RISK_SAFE, ToolRegistry

        reg = ToolRegistry()
        registered = client.register_tools(reg)
        check(registered and registered[0].startswith("mcp__fake__"),
              f"工具应注册进注册表：{registered}")
        spec = reg.get(registered[0])
        check(spec is not None and spec.risk == RISK_SAFE,
              f"readOnlyHint=true 应降级为 safe：{getattr(spec, 'risk', None)}")
        res = reg.call(registered[0], {"text": "hi"})
        check(res.ok and "echo: hi" in str(res.result),
              f"经注册表调用应成功：{res.error or res.result}")
    finally:
        client.close()

    # ---- 4) 失败路径：命令不存在 → 抛可读 MCPError，不静默
    bad = MCPClient({"name": "bad", "transport": "stdio",
                     "command": ["__definitely_not_existing__"]})
    try:
        bad.connect()
        check(False, "不存在的命令应抛错")
    except MCPError as ex:
        check("找不到可执行文件" in str(ex), f"错误信息应可读：{ex}")
    finally:
        bad.close()

    # ---- 5) 管理器：配置为空时安全返回
    class _Cfg:
        def get(self, k, d=None):
            return {"mcp_servers": [], "mcp_enabled": True}.get(k, d)

    mgr = MCPManager(_Cfg())
    mgr.start(background=False)
    st = mgr.status()
    check(st["configured"] == 0 and st["connected"] == 0,
          f"空配置应安全：{st}")
    return True


if __name__ == "__main__":
    print("MCP 客户端自检：", "通过" if selftest() else "失败")
