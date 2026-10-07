"""本地 HTTP 服务。

用标准库 ``http.server`` 实现（不引入 FastAPI/Flask），保证零强制依赖。
``ThreadingHTTPServer`` 每连接一线程，SSE 长连接不会阻塞普通请求。

安全模型（默认严格，显式放开）::

    1. 默认只监听 127.0.0.1，且拒绝非本机来源的请求
    2. 开启局域网模式后允许其它来源，但**必须**设置 API Token
    3. 敏感路径（设置 / 管理 / 记忆修改）在局域网模式下强制校验 Token
    4. 所有对外响应中的配置都经过脱敏
"""

from __future__ import annotations

import json
import mimetypes
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..events import BUS
from ..version import version_info
from .sse import SSEStream

STATIC_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"

#: 局域网模式下必须校验 Token 的路径前缀
SENSITIVE_PREFIXES = (
    "/api/settings",
    "/api/admin",
    "/api/memories",
    "/api/activate",
    "/api/config",
)


def _is_local(remote: str) -> bool:
    """判断来源是否本机。"""
    if remote in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        if remote.startswith("::ffff:") and remote[7:] == "127.0.0.1":
            return True
    except Exception:
        pass
    return False


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。类属性由 WarriorServer 注入。"""

    core: Any = None
    routes: Dict[Tuple[str, str], Callable[..., Any]] = {}
    token: str = ""
    allow_lan: bool = False
    static_dir: Path = STATIC_DIR
    server_version = "BlackWarrior/0.1"
    protocol_version = "HTTP/1.1"

    # ------- 日志 -------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """静音默认访问日志（太吵）；错误单独打。"""
        return

    def handle_error(self, e=None) -> None:
        """覆写基类：浏览器主动关闭 SSE 连接 / 客户端断网是常态，
        不是产品错误，不应打印那串吓人的 traceback
        （2026-10-06 用户于 CMS 窗口所见 ConnectionAbortedError 10053）。

        良性断连静默处理；真实异常仍交给基类保留可见性。
        """
        import socket
        benign = (ConnectionAbortedError, ConnectionResetError,
                  BrokenPipeError, socket.error)
        if isinstance(e, benign):
            return
        # 兼容不同 Python 版本的 handle_error 签名
        try:
            BaseHTTPRequestHandler.handle_error(self, e)
        except TypeError:
            BaseHTTPRequestHandler.handle_error(self)

    # ------- 工具 -------------------------------------------------

    def _send(self, status: int, payload: Any = None, *,
              headers: Optional[Dict[str, str]] = None,
              raw: Optional[bytes] = None, ctype: str = "") -> None:
        if raw is None:
            if payload is None:
                body = b""
            elif isinstance(payload, (bytes, bytearray)):
                body = bytes(payload)
            else:
                body = json.dumps(payload, ensure_ascii=False,
                                  default=str).encode("utf-8")
                ctype = ctype or "application/json; charset=utf-8"
        else:
            body = raw
            ctype = ctype or "application/octet-stream"

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _cors(self) -> None:
        """同源场景下 Electron 也需要 CORS 头（file:// 与 http 不同源）。"""
        if not self.allow_lan:
            return
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PATCH, DELETE, OPTIONS")

    def _read_body(self) -> Any:
        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        ctype = str(self.headers.get("Content-Type") or "")
        if "json" in ctype or raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                return None
        try:
            return urllib.parse.parse_qs(raw.decode("utf-8", "ignore"))
        except Exception:
            return None

    def read_raw(self) -> bytes:
        """读二进制请求体（语音上传用）。

        ``_read_body`` 会把非 JSON 的 body 当表单解析，音频流进去会被搅坏，
        所以二进制走独立入口。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def raw_header(self, name: str, default: str = "") -> str:
        return str(self.headers.get(name) or default)

    def _authorized(self) -> bool:
        """鉴权。本机一律放行；局域网模式下敏感路径要 Token。"""
        if _is_local(self.client_address[0]):
            return True
        if not self.allow_lan:
            return False
        path = self.path.split("?")[0]
        sensitive = any(path.startswith(p) for p in SENSITIVE_PREFIXES)
        if not sensitive:
            return True
        if not self.token:
            return False
        auth = str(self.headers.get("Authorization") or "")
        if auth.startswith("Bearer "):
            return auth[7:].strip() == self.token
        # 也接受 URL 上的 ?token=（方便浏览器直接打开）
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("token") or [""])[0] == self.token

    def _match(self, method: str, path: str
               ) -> Tuple[Optional[Callable[..., Any]], Dict[str, str]]:
        """路由匹配：精确 → 参数化（``{name}`` 段）。"""
        fn = self.routes.get((method, path))
        if fn is not None:
            return fn, {}
        segs = [s for s in path.split("/") if s]
        for (m, pattern), handler in self.routes.items():
            if m != method:
                continue
            psegs = [s for s in pattern.split("/") if s]
            if len(psegs) != len(segs):
                continue
            params: Dict[str, str] = {}
            ok = True
            for ps, actual in zip(psegs, segs):
                if ps.startswith("{") and ps.endswith("}"):
                    params[ps[1:-1]] = urllib.parse.unquote(actual)
                elif ps != actual:
                    ok = False
                    break
            if ok:
                return handler, params
        return None, {}

    # ------- 方法分派 ---------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorized():
            self._send(403, {"error": "未授权：局域网访问需要提供有效令牌"})
            return

        # SSE
        if path == "/events" or path == "/api/events":
            self._serve_sse(parsed)
            return

        fn, params = self._match(method, path)
        if fn is not None:
            try:
                # 这些端点要**原始字节**，绝不能先过 _read_body
                # （它会把二进制/加密报文当表单解析，签名与密文都会被搅坏）：
                #   - /api/voice/transcribe：音频流
                #   - /api/channel/{feishu,wecom}：飞书/企微事件回调（验签要用原文）
                is_raw = (method == "POST" and (
                    path == "/api/voice/transcribe"
                    or path.startswith("/api/channel/feishu")
                    or path.startswith("/api/channel/wecom")))
                body = None if is_raw else (
                    self._read_body() if method in ("POST", "PATCH", "PUT") else None)
                result = fn(self.core, body, params, self)
            except Exception as ex:
                self._send(500, {"error": f"{type(ex).__name__}: {ex}"})
                return
            self._handle_result(result)
            return

        # 静态资源
        if method == "GET":
            if self._serve_static(path):
                return

        self._send(404, {"error": f"未找到：{path}"})

    def _handle_result(self, result: Any) -> None:
        """把 handler 的返回值转成响应。"""
        if result is None:
            self._send(204)
            return
        if isinstance(result, tuple) and len(result) == 2:
            status, payload = result
            if isinstance(payload, tuple) and len(payload) == 2 and \
                    isinstance(payload[1], (bytes, bytearray)):
                raw, ctype = payload
                self._send(int(status), raw=bytes(raw), ctype=str(ctype))
            else:
                self._send(int(status), payload)
            return
        if isinstance(result, dict) and "__raw__" in result:
            self._send(result.get("status", 200),
                       raw=result["__raw__"], ctype=result.get("ctype", ""))
            return
        self._send(200, result)

    # ------- SSE --------------------------------------------------

    def _serve_sse(self, parsed: Any) -> None:
        q = urllib.parse.parse_qs(parsed.query)
        last_id = 0
        try:
            last_id = int((q.get("last_id") or ["0"])[0])
        except Exception:
            last_id = 0
        last_id = last_id or int(str(self.headers.get("Last-Event-ID") or "0") or 0)

        self.send_response(200)
        for k, v in SSEStream(BUS, self.wfile).headers():
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()

        # UI 首次接入 → 主动打招呼（每个 serve 会话只一次）。
        # 延迟触发：等 SSEStream.run() 完成订阅后再发事件，否则问候会丢。
        try:
            greet = getattr(self.core, "greet_once", None)
            if callable(greet):
                threading.Timer(0.8, greet).start()
        except Exception:
            pass

        try:
            SSEStream(BUS, self.wfile, last_event_id=last_id).run()
        except Exception:
            pass

    # ------- 静态文件 ---------------------------------------------

    def _serve_static(self, path: str) -> bool:
        rel = path.lstrip("/")
        if not rel or rel == "ui":
            rel = "index.html"
        target = (self.static_dir / rel).resolve()
        root = self.static_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False
        if not target.is_file():
            if (root / "index.html").is_file():
                target = root / "index.html"
            else:
                return False
        ctype, _ = mimetypes.guess_type(str(target))
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        try:
            raw = target.read_bytes()
        except Exception:
            return False
        self._send(200, raw=raw, ctype=ctype)
        return True


class WarriorServer:
    """黑武士本地服务。"""

    def __init__(self, core: Any, *, host: Optional[str] = None,
                 port: Optional[int] = None,
                 routes: Optional[Dict[Tuple[str, str], Callable[..., Any]]] = None,
                 static_dir: Optional[str] = None) -> None:
        self.core = core
        cfg = core.config
        self.host = host or cfg.host
        self.port = int(port or cfg.port)
        self.allow_lan = bool(cfg.get("allow_lan", False))
        self.token = str(cfg.get("api_token", "") or "")
        self.routes = routes or {}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._static_dir = Path(static_dir) if static_dir else STATIC_DIR
        self._skipped: List[int] = []

    # ------- 生命周期 ---------------------------------------------

    def _build_handler_class(self) -> type:
        core = self.core
        routes = self.routes
        token = self.token
        allow_lan = self.allow_lan
        static_dir = self._static_dir

        class Handler(_Handler):
            pass

        Handler.core = core               # type: ignore[attr-defined]
        Handler.routes = routes           # type: ignore[attr-defined]
        Handler.token = token             # type: ignore[attr-defined]
        Handler.allow_lan = allow_lan     # type: ignore[attr-defined]
        Handler.static_dir = static_dir   # type: ignore[attr-defined]
        return Handler

    def _probe_busy(self, port: int, timeout: float = 0.6) -> bool:
        """探测端口上是否已经有一个**活着的 HTTP 服务**。

        ★ 为什么必须显式探测，而不是只靠 bind 的 OSError：
          Windows 的 ``SO_REUSEADDR`` 允许**抢占**别人已绑定的端口——bind 会
          "成功"，但请求全部被先启动的那个进程吃掉。于是新实例看起来启动正常，
          实际所有 API 都打在旧进程上（用户视角就是"改了没生效"，
          日志里版本还是旧的，非常难查）。
          所以这里主动问一句"你在吗"，有人应答就换下一个端口。
        """
        try:
            with socket.create_connection(("127.0.0.1", int(port)),
                                          timeout=timeout):
                pass
        except OSError:
            return False        # 连不上 → 端口空闲
        # 能连上就发一个探测请求；能拿到 HTTP 响应头即视为占用
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{int(port)}/api/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                int(resp.status or 0)
            return True
        except urllib.error.HTTPError:
            return True          # 有 HTTP 服务只是路径/状态不对 → 仍算占用
        except Exception:
            # 连得上但不是 HTTP（比如别的 TCP 服务）：也算占用，别抢
            return True

    def _bind(self, start_port: int, tries: int = 20) -> int:
        """绑定端口；被占用时自动往后找（Electron 启动时常见）。"""
        port = int(start_port)
        self._skipped: List[int] = []
        for _ in range(int(tries)):
            if self._probe_busy(port):
                self._skipped.append(port)
                port += 1
                continue
            try:
                self._httpd = ThreadingHTTPServer(
                    (self.host, port), self._build_handler_class())
                self._httpd.daemon_threads = True
                return port
            except OSError:
                self._skipped.append(port)
                port += 1
        raise RuntimeError(
            f"无法绑定端口（{start_port} 起 {tries} 个均被占用："
            f"{self._skipped}）。请先关掉正在运行的黑武士实例。")

    def start(self, background: bool = True) -> int:
        """启动服务，返回实际端口。"""
        actual = self._bind(self.port)
        self.port = actual
        if background:
            t = threading.Thread(target=self._serve_forever,
                                 name="bw-http", daemon=True)
            t.start()
            self._thread = t
        return actual

    def _serve_forever(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.serve_forever(poll_interval=0.3)
        except Exception:
            pass

    def serve_forever(self) -> int:
        """前台运行（阻塞）。"""
        actual = self.start(background=False)
        self._serve_forever()
        return actual

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    # ------- 信息 -------------------------------------------------

    @property
    def url(self) -> str:
        shown_host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{shown_host}:{self.port}"

    def info(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "allow_lan": self.allow_lan,
            "token_required": bool(self.token),
            "static_dir": str(self._static_dir),
            "routes": len(self.routes),
            # 跳过的端口：有人已在运行（Windows 端口抢占高发区），便于排查
            "skipped_ports": list(self._skipped),
        }
