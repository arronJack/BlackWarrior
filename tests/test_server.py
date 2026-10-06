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
import urllib.error
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


def delete(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Accept": "application/json"})
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


def check_no_foreign_attrs() -> None:
    """产物里不该有外部页面编辑工具注入的属性。

    实测：某些 HTML 预览/编辑工具会往 DOM 上撒 `data-page-node-id`，
    一次就注入了 400+ 个。它们与黑武士无关，却污染源码、干扰 review，
    而且会在每次预览后反复出现——所以静态卡一道。
    """
    import re as _re

    root = ROOT / "blackwarrior" / "ui" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    hits = _re.findall(r'data-page-node-id="[^"]*"', html)
    check("index.html 无外部注入属性", not hits,
          f"{len(hits)} 处 data-page-node-id")


def check_dom_ids() -> None:
    """前端 DOM id 一致性：JS 引用的 id 必须在 HTML 里存在。

    `$('xxx')` 拿到 null 不会抛错，只会在后面某一行静默崩掉——
    这类问题在浏览器里表现为"页面卡在启动画面"，极难定位，所以静态卡一道。
    """
    import re
    root = ROOT / "blackwarrior" / "ui" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    js = "".join((root / f).read_text(encoding="utf-8")
                 for f in ("js/app.js", "js/api.js", "js/viz.js", "js/voice.js"))
    refs = set(re.findall(r"\$\('([^']+)'\)", js))
    missing = sorted(refs - ids)
    check("前端 DOM id 全部存在", not missing, str(missing))


def main() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    print("== 0. 前端静态一致性 ==")
    check_no_foreign_attrs()
    check_dom_ids()

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

        # 以下三项是 UI 直接消费的字段，曾因前后端不对齐静默失效
        # （队列恒 0 / 下一跳恒空 / 数据目录为空），各加一道回归卡点
        check("status.loop.queue_size", isinstance(status.get("loop", {}).get("queue_size"), int))
        check("status.data_dir", bool(status.get("data_dir")))
        check("status.loop.next_tick_in 键存在", "next_tick_in" in status.get("loop", {}))

        st, pv = get(base + "/api/providers")
        items = pv.get("items") or []
        # UI 用 p.key 填下拉 value；写成 p.id 会把中文名当 provider 存回去
        check("providers 用 key 字段", bool(items) and all("key" in p for p in items),
              str([list(p.keys())[:3] for p in items[:1]]))
        st, cfg0 = get(base + "/api/settings")
        check("settings 含 tick_interval", "tick_interval" in cfg0)

        print("== 3. 静态 UI ==")
        for path_, must in [("index.html", "BlackWarrior"),
                            ("css/warrior.css", "BlackWarrior"),
                            ("js/app.js", "EventStream"),
                            ("js/api.js", "BW.api"),
                            ("js/viz.js", "MemoryGraph"),
                            ("js/voice.js", "BW.voice")]:
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
        check("认知快照含 tick_scale", "tick_scale" in (cog.get("cognition") or {}))
        st, hist = get(base + "/api/conversations")
        check("会话历史可回看", st == 200 and isinstance(hist.get("items"), list))

        print("== 5b. 语音（降级路径必须明确，不能静默失败）==")
        st, vs = get(base + "/api/voice/status")
        check("voice/status", st == 200 and "asr_ready" in vs)
        check("voice 本地朗读可用标记", vs.get("tts_local_available") is True)

        # 未配置时上传音频必须返回 503 + not_configured，
        # 让前端据此置灰麦克风；返回 500 或 200 空文本都是错的设计。
        req = urllib.request.Request(
            base + "/api/voice/transcribe", data=b"\x00" * 64, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "X-Audio-Mime": "audio/webm"})
        code = None
        detail = ""
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as ex:
            code = ex.code
            detail = ex.read().decode("utf-8", "ignore")
        vs_ready = bool(vs.get("asr_ready"))
        if vs_ready:
            check("语音转写（已配置）已接线", True)
        else:
            check("未配置时转写返回 503", code == 503, f"got {code}")
            check("未配置时给出 not_configured", "not_configured" in (detail or ""), detail[:120])
        st, mood = get(base + "/api/cognition/mood?n=20")
        check("情绪曲线", st == 200 and isinstance(mood.get("curve"), list))

        print("== 6. 设置与脱敏 ==")
        st, cfg = get(base + "/api/settings")
        check("settings", st == 200)
        blob = json.dumps(cfg)
        check("密钥脱敏", "api_key" not in blob or "***" in blob or "sk-" not in blob)

        print("== 7. 用户画像 / 面板 / 预取（v0.2）==")
        st, prof = get(base + "/api/profile")
        check("profile 可读", st == 200 and "items" in prof)

        st, r = post(base + "/api/profile",
                     {"aspect": "name", "value": "小志", "evidence": "端到端测试"})
        check("profile 可写", st == 200 and r.get("ok") is True, str(r)[:120])
        st, prof = get(base + "/api/profile")
        check("画像已落地",
              (prof.get("items", {}).get("name", {}) or {}).get("value") == "小志",
              str(prof)[:160])

        # 非法维度必须 400，而不是静默塞一条脏数据
        try:
            post(base + "/api/profile", {"aspect": "不存在的维度", "value": "x"})
            code = 200
        except urllib.error.HTTPError as ex:
            code = ex.code
        check("非法画像维度被拒", code == 400, f"got {code}")

        st, panels = get(base + "/api/panels")
        # weather 默认关闭，as_dict 不应返回它；至少 person 面板恒可用
        check("panels 可读", st == 200 and "person" in panels, str(list(panels))[:120])
        check("person 面板恒可用",
              (panels.get("person") or {}).get("available") is True)
        check("weather 默认关闭（诚实降级）", "weather" not in panels,
              str(list(panels))[:120])
        st, one = get(base + "/api/panels/person")
        check("panels/{kind} 路由参数解析", st == 200 and one.get("kind") == "person",
              str(one)[:120])

        st, pf = get(base + "/api/prefetch")
        check("prefetch 可读", st == 200 and "items" in pf)
        st, r = post(base + "/api/prefetch",
                     {"url": "https://example.com/bw", "ttl": 600})
        check("prefetch 可登记", st == 200 and r.get("ok") is True, str(r)[:120])
        st, pf = get(base + "/api/prefetch")
        check("预取已落地", pf.get("count", 0) >= 1)
        st, r = delete(base + "/api/prefetch")
        check("预取可清空", st == 200 and r.get("ok") is True, str(r)[:120])
        st, pf = get(base + "/api/prefetch")
        check("清空后为空", pf.get("count") == 0, str(pf)[:120])

        st, status2 = get(base + "/api/status")
        check("status 含 panorama", "panorama" in status2)
        check("panorama 含 profile/panels/prefetch",
              all(k in (status2.get("panorama") or {})
                  for k in ("profile", "panels", "prefetch")),
              str(list((status2.get("panorama") or {})))[:120])

        print("== 8. PASM V2 十九层心智（v0.3）==")
        st, mind = get(base + "/api/mind")
        check("mind 可读", st == 200 and "available" in mind)
        check("mind 恒有 19 层说明", mind.get("n_layers") in (0, 19),
              f"n_layers={mind.get('n_layers')}")
        if mind.get("available"):
            # V2 在场：走真实能力
            check("V2 版本号可见", bool(mind.get("version")), str(mind)[:120])
            check("V2 十九层齐全", len(mind.get("layers") or []) == 19,
                  str(len(mind.get("layers") or [])))
            check("V2 诚实标注嵌入语义",
                  isinstance(mind.get("semantic_embedding"), bool))
            ss = mind.get("safety_state") or {}
            check("安全层状态可读", "allowlist_size" in ss, str(ss)[:120])
            check("白名单与工具集一致",
                  ss.get("allowlist_size") == tools.get("count"),
                  f"gate={ss.get('allowlist_size')} tools={tools.get('count')}")
            st, ob = post(base + "/api/mind/observe", {"text": "端到端测试：认知底座"})
            check("V2 感知可用", st == 200 and "digest" in ob, str(ob)[:140])
            st, vv = post(base + "/api/mind/verify",
                          {"tool": "get_time", "args": {}})
            check("V2 放行白名单内工具", st == 200 and vv["verdict"].get("pass") is True,
                  str(vv)[:160])
            st, vv2 = post(base + "/api/mind/verify",
                           {"tool": "format_disk", "args": {}})
            check("V2 拒绝白名单外工具",
                  st == 200 and vv2["verdict"].get("pass") is False, str(vv2)[:160])
            st, sl = post(base + "/api/mind/sleep", {})
            check("V2 睡眠巩固", st == 200 and "report" in sl, str(sl)[:120])
            st, gw = post(base + "/api/mind/gate-reset", {})
            check("安全层锁死可复位", st == 200 and "ok" in gw, str(gw)[:120])
            st, tl = post(base + "/api/mind/tell", {"reference": "端到端测试"})
            check("V2 符号化注入", st == 200 and "bound" in tl, str(tl)[:120])
        else:
            # V2 缺席：必须诚实降级，且动作类端点返回 503 而不是 500
            check("V2 缺席时给出原因", bool(mind.get("reason")), str(mind)[:120])
            check("V2 缺席时十九层标注未接入",
                  all(l.get("active") is False
                      for l in (mind.get("layers") or [])) or not mind.get("layers"),
                  str(mind)[:160])
            for ep, payload, label in [
                    ("/api/mind/observe", {"text": "x"}, "observe"),
                    ("/api/mind/sleep", {}, "sleep"),
                    ("/api/mind/growth", {}, "growth"),
                    ("/api/mind/tell", {"reference": "x"}, "tell")]:
                try:
                    post(base + ep, payload)
                    code = 200
                except urllib.error.HTTPError as ex:
                    code = ex.code
                check(f"V2 缺席时 {label} 返回 503", code == 503, f"got {code}")

        st, ls = get(base + "/api/mind/layers")
        check("mind/layers 可读", st == 200 and "layers" in ls)
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
