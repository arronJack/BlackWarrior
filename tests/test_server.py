"""HTTP 服务端到端测试：拉起真实内核 + 真实端口，走完整 REST 面。

用法::

    python tests/test_server.py          # 跑完退出（exit 0/1）
    python tests/test_server.py --keep   # 跑完保留服务（手动调试 UI）
"""

from __future__ import annotations

import json
import urllib.parse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READY_PREFIX = "[BW-READY]"

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post(url: str, payload: dict, timeout: float = 130.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def read_sse_frames(host: str, port: int, seconds: float = 3.0):
    """原始 socket 读 SSE：先发请求，再读 seconds 秒，解析出的事件列表。

    用 urllib 读流式响应不可靠（缓冲层会吞掉未满一帧的数据），
    直接对裸 socket 收字节最贴近浏览器 EventSource 的真实行为。
    """
    import socket
    sock = socket.create_connection((host, port), timeout=seconds)
    try:
        sock.sendall((f"GET /events HTTP/1.1\r\nHost: {host}:{port}\r\n"
                      "Accept: text/event-stream\r\n\r\n").encode("ascii"))
        buf = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            sock.settimeout(max(0.2, deadline - time.time()))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    text = buf.decode("utf-8", "ignore")
    frames = []
    for block in text.split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        ev = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                ev["id"] = int(line[4:])
            elif line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = line[6:]
        if ev:
            frames.append(ev)
    return frames


def main() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    print("== 1. 拉起内核 ==")
    env = dict(__import__("os").environ)
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen(
        [sys.executable, "-m", "blackwarrior.cli", "serve",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    base = f"http://127.0.0.1:{port}"
    ready = None
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith(READY_PREFIX):
            ready = json.loads(line[len(READY_PREFIX):])
            break
    check("BW-READY 标记", ready is not None)
    if ready is None:
        proc.kill()
        return 1
    base = ready["url"] if ready["url"].startswith("http") else base
    print(f"  url={ready['url']} tier={ready['tier']} engine={ready['engine']}")

    try:
        print("== 2. 基础接口 ==")
        st, health = get(base + "/api/healthz")
        check("healthz", st == 200 and health.get("ok") is True)
        st, ver = get(base + "/api/version")
        check("version", st == 200 and "BlackWarrior" in json.dumps(ver))
        st, status = get(base + "/api/status")
        check("status.cognition.tier", st == 200 and bool(status.get("cognition", {}).get("tier")))
        check("status.affect", "prediction_error" in json.dumps(status.get("affect", {})))
        st, tools = get(base + "/api/tools")
        check("tools 注册", st == 200 and tools.get("count", 0) >= 10)

        print("== 3. 静态 UI ==")
        for path_, must in [("index.html", "BlackWarrior"),
                            ("css/warrior.css", "BlackWarrior"),
                            ("js/app.js", "EventStream"),
                            ("js/api.js", "BW.api"),
                            ("js/viz.js", "MemoryGraph")]:
            with urllib.request.urlopen(base + "/" + path_, timeout=8) as r:
                body = r.read().decode("utf-8")
            check(f"静态 {path_}", r.status == 200 and must in body)

        print("== 4. SSE ==")
        # 关键：先连上事件流，再触发对话。
        # （离线档回复在毫秒级完成，若先发消息再连流，事件早已过去，
        #   而新订阅者不带 last_event_id 不回放历史——这正是真实浏览器的行为。）
        host_, port_ = urllib.parse.urlparse(base).hostname, urllib.parse.urlparse(base).port
        import threading
        result = {}

        def _reader():
            result["frames"] = read_sse_frames(host_, port_, 4.0)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        time.sleep(0.5)
        post(base + "/api/message", {"text": "SSE 测试：黑武士是带认知内核的桌面智能体"})
        t.join(timeout=6)
        frames = result.get("frames") or []
        named = [f for f in frames if f.get("event") and f.get("id")]
        check("SSE 具名帧 + id 序号", len(named) >= 1,
              f"total={len(frames)} named={len(named)} types={[f.get('event') for f in frames][:8]}")

        print("== 5. 对话与记忆 ==")
        st, r = post(base + "/api/message", {"text": "端到端测试：我叫小志，请记住"})
        check("message 入队", st == 200 and r.get("queued") is True)
        time.sleep(2.5)
        st, r = post(base + "/api/ask", {"text": "我叫什么名字", "timeout": 60})
        check("ask 回复非空", st == 200 and bool((r.get("reply") or "").strip()),
              repr((r or {}).get("reply", "")[:60]))
        st, mems = get(base + "/api/memories?limit=20")
        check("记忆已写入", st == 200 and mems.get("count", 0) >= 1)
        st, cog = get(base + "/api/cognition")
        check("认知快照", st == 200 and "cognition" in cog and "affect" in cog)
        st, mood = get(base + "/api/cognition/mood?n=20")
        check("情绪曲线", st == 200 and isinstance(mood.get("curve"), list))

        print("== 6. 设置与脱敏 ==")
        st, cfg = get(base + "/api/settings")
        check("settings", st == 200)
        blob = json.dumps(cfg)
        check("密钥脱敏", "api_key" not in blob or "***" in blob or "sk-" not in blob)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print(f"\n== 结果: {PASSED} 通过 / {FAILED} 失败 ==")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
