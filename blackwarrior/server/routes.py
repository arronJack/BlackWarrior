"""REST 路由。

提供对话 / 记忆 / 工具 / 认知四类接口，并独有认知与心智两组接口
（``/api/cognition/*`` —— 有了内核才谈得上把内核状态暴露出来）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..events import BUS, emit
from ..llm.providers import list_providers
from ..runtime import PRIORITY_USER
from ..version import version_info


def _json(fn):  # noqa: ANN001 - 装饰签名通用的 handler
    return fn



# ============================================================ 环境自检与修复

def _doctor(core):
    """取（或懒创建）体检器。挂在 core 上，避免每次请求重建。"""
    d = getattr(core, "doctor", None)
    if d is None:
        from ..doctor import Doctor
        d = core.doctor = Doctor(core)
    return d


def get_doctor(core, body, params, handler):
    """逐项体检。**只读**，绝不因为"检查"就改动任何东西。"""
    return _doctor(core).check()


def get_doctor_jobs(core, body, params, handler):
    """修复任务进度（装大包要几分钟，没有进度用户会以为卡死）。"""
    return {"jobs": _doctor(core).jobs()}


def post_doctor_fix(core, body, params, handler):
    """**用户点了某一项的「安装」才会走到这里**。绝不自动安装。"""
    item_id = str((body or {}).get("id") or "")
    if not item_id:
        return {"error": "缺少 id"}
    return _doctor(core).start_fix(item_id)


# ============================================================ 贾维斯面板

def _jarvis(core):  # noqa: ANN001 - 运行时就挂在 core 上
    """取（或懒创建）贾维斯运行时，**并确保它启动过**。

    之前这里只创建不启动：面板能拿到状态，但 ASR/TTS 永远是"未启用"，
    表现为「面板打开着、麦克风也授权了、但永远没反应」。这种半死不活的
    状态比直接报错更难查，所以启动放在这里做一次，并保证只做一次。
    """
    rt = getattr(core, "jarvis", None)
    if rt is None:
        from ..runtime.jarvis import JarvisRuntime
        rt = core.jarvis = JarvisRuntime(core)
    if not getattr(rt, "started", False):
        try:
            rt.start()
            rt.started = True
        except Exception as ex:
            # 启动失败只记事件：面板照常能用，只是没有语音
            rt.started = True
            emit("jarvis", {"stage": "start_failed", "error": str(ex)[:200]})
    return rt


def get_jarvis_state(core, body, params, handler):
    """面板轮询：当前状态 + 语音组件就绪情况。"""
    return _jarvis(core).status()


def get_jarvis_audio(core, body, params, handler):
    """把 TTS 产出的音频文件读出来给面板播放。

    只允许读自己 data 目录下的音频——这是把本地文件暴露成 HTTP 的入口，
    参数一旦能被穿越，就等于开了任意文件读取。
    """
    import os
    from urllib.parse import unquote

    # ★查询参数必须走 _query(handler)：路由传进来的 `params` 是**路径**
    # 变量（例如 /memories/{id}），不是 query string。按 params 取会永远
    # 拿到空 → 面板永远播不出声音（而 TTS 那边其实已经合成好了）。
    q = _query(handler) if handler is not None else {}
    p = unquote(str((q.get("p") or [""])[0]))
    if not p:
        return {"error": "缺少 p 参数"}
    from .. import paths as _paths
    try:
        root = os.path.abspath(str(_paths.data_root()))
        full = os.path.abspath(p)
    except Exception as ex:
        return {"error": f"路径无效: {ex}"}
    # 前缀比对要用 os.path.commonpath，startswith 会被
    # "C:\data_evil" 这种同前缀目录绕过。
    try:
        if os.path.commonpath([root, full]) != root:
            return {"error": "只能播放 data 目录内的音频"}
    except ValueError:
        return {"error": "路径不在 data 目录内"}
    if not os.path.isfile(full):
        return {"error": "音频不存在"}
    ext = os.path.splitext(full)[1].lower()
    ctype = {".wav": "audio/wav", ".mp3": "audio/mpeg",
             ".m4a": "audio/mp4", ".ogg": "audio/ogg"}.get(ext)
    if not ctype:
        return {"error": "不支持的音频格式"}
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except Exception as ex:
        return {"error": f"读取失败: {ex}"}
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except Exception:
        pass
    return None


def post_jarvis_transcribe(core, body, params, handler):
    """面板录到一段语音 → 识别成文字。

    这里**只做识别**，不做任何判断：唤醒判定、授权分流都在
    :meth:`JarvisRuntime.handle_text` 里，识别层不碰策略。
    """
    import base64
    import os
    import tempfile

    raw = (body or {}).get("audio_b64") or ""
    if not raw:
        return {"error": "缺少 audio_b64"}
    # 一次只接受一小段（唤醒片段通常 1-5 秒），别让人往里塞大文件
    if len(raw) > 8 * 1024 * 1024:
        return {"error": "音频过大（超过 8MB）"}
    ext = ".wav" if str((body or {}).get("mime", "")).find("wav") >= 0 else ".webm"
    path = os.path.join(tempfile.gettempdir(),
                        f"bw_jarvis_{os.getpid()}_{len(raw)}.wav")
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(raw))
    except Exception as ex:
        return {"error": f"解码失败: {ex}"}
    return _jarvis(core).transcribe(path)


def post_jarvis_utterance(core, body, params, handler):
    """面板送来一句文本 → 完整流程（唤醒→授权→执行→开口）。"""
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return {"error": "缺少 text"}
    return _jarvis(core).handle_text(text)


def post_jarvis_confirm(core, body, params, handler):
    """人工确认高危动作。ok=False 即取消。"""
    pid = str((body or {}).get("pending_id") or "")
    if not pid:
        return {"error": "缺少 pending_id"}
    return _jarvis(core).confirm(pid, bool((body or {}).get("ok")))


# ============================================================ 基础

def get_health(core, body, params, handler):
    return {"ok": True, "ts": time.time()}


def get_version(core, body, params, handler):
    return version_info()


def get_status(core, body, params, handler):
    return core.status()


def get_summary(core, body, params, handler):
    return core.summary()


# ============================================================ 对话

def post_message(core, body, params, handler):
    """异步发送一条消息到主循环。"""
    text = ""
    if isinstance(body, dict):
        text = str(body.get("text") or body.get("message") or "")
    if not text.strip():
        return 400, {"error": "消息内容为空"}
    msg = core.send(
        text,
        from_id=str((body or {}).get("from_id") or "ui"),
        channel=str((body or {}).get("channel") or "ui"),
    )
    if msg is None:
        return 200, {"ok": True, "queued": False, "note": "被去重忽略"}
    return {"ok": True, "queued": True, "message": msg.as_dict()}


def post_ask(core, body, params, handler):
    """同步问答（等待结果）。"""
    text = ""
    if isinstance(body, dict):
        text = str(body.get("text") or body.get("message") or "")
    if not text.strip():
        return 400, {"error": "消息内容为空"}
    timeout = 120.0
    if isinstance(body, dict):
        try:
            timeout = float(body.get("timeout") or 120)
        except Exception:
            timeout = 120.0
    result = core.ask(text, timeout=timeout)
    return {"ok": True, "reply": result.text, "rounds": result.rounds,
            "tool_calls": result.tool_calls, "error": result.error}


def get_conversations(core, body, params, handler):
    try:
        limit = int(handler.headers.get("X-Limit", 0) or 0)
    except Exception:
        limit = 0
    limit = limit or 50
    return {"items": core.store.recent_messages(limit=limit), "count": limit}


# ============================================================ 记忆

def get_memories(core, body, params, handler):
    limit = 100
    category = None
    q = _query(handler)
    if q:
        if "limit" in q:
            try:
                limit = int(q["limit"][0])
            except Exception:
                pass
        if "category" in q:
            category = q["category"][0]
    items = core.list_memories(limit=limit, category=category)
    return {"items": items, "count": len(items)}


def post_memories(core, body, params, handler):
    if not isinstance(body, dict) or not body.get("title"):
        return 400, {"error": "缺少 title"}
    rec = core.add_memory(
        str(body["title"]), str(body.get("brief") or ""),
        tags=body.get("tags") or [],
        salience=int(body.get("salience") or 3),
        category=str(body.get("category") or "日常"),
    )
    return {"ok": True, "memory": rec}


def patch_memory(core, body, params, handler):
    mem_id = int(params.get("id") or 0)
    if not mem_id:
        return 400, {"error": "缺少 id"}
    fields = {k: v for k, v in (body or {}).items()
              if k in ("title", "brief", "tags", "category", "salience", "archived")}
    ok = core.memory.update(mem_id, **fields)
    return {"ok": ok}


def delete_memory(core, body, params, handler):
    mem_id = int(params.get("id") or 0)
    if not mem_id:
        return 400, {"error": "缺少 id"}
    return {"ok": core.delete_memory(mem_id)}


def post_memory_search(core, body, params, handler):
    query = str((body or {}).get("query") or "")
    if not query:
        return 400, {"error": "缺少 query"}
    items = core.memory.recall(query, k=int((body or {}).get("k") or 5))
    return {"items": items, "count": len(items)}


def get_actions(core, body, params, handler):
    items = core.store.recent_actions(limit=50)
    return {"items": items, "count": len(items)}


# ============================================================ 提醒

def get_reminders(core, body, params, handler):
    items = core.list_reminders(params.get("status"))
    return {"items": items, "count": len(items)}


def post_reminders(core, body, params, handler):
    if not isinstance(body, dict) or not body.get("title"):
        return 400, {"error": "缺少 title"}
    due = body.get("due_at")
    if due is None:
        minutes = float(body.get("minutes") or 5)
        due = time.time() + minutes * 60.0
    rid = core.add_reminder(str(body["title"]), float(due),
                            body=str(body.get("body") or ""))
    return {"ok": True, "id": rid, "due_at": float(due)}


def delete_reminder(core, body, params, handler):
    rid = int(params.get("id") or 0)
    if not rid:
        return 400, {"error": "缺少 id"}
    core.cancel_reminder(rid)
    return {"ok": True}


# ============================================================ 工具

def get_tools(core, body, params, handler):
    return {"items": core.tools.describe(), "count": len(core.tools)}


# ============================================================ 认知（★独有）

def get_cognition(core, body, params, handler):
    """认知内核快照：档位、记忆强度分布、焦点、情绪、预测误差。"""
    try:
        snap = core.kernel.snapshot()
        # 心跳缩放来自情绪世界模型（core 层组件），内核本身不知道它
        try:
            snap["tick_scale"] = round(float(core.affect.suggest_tick_scale()), 3)
        except Exception:
            pass
    except Exception as ex:
        snap = {"error": str(ex)}
    try:
        aff = core.affect.snapshot()
    except Exception as ex:
        aff = {"error": str(ex)}
    return {"cognition": snap, "affect": aff}


def post_consolidate(core, body, params, handler):
    """手动触发一次记忆巩固。"""
    rep = core.memory.consolidate(apply=True)
    return {"ok": True, "report": rep}


def get_mood_curve(core, body, params, handler):
    try:
        n = int(params.get("n") or _query(handler).get("n", [60])[0])
    except Exception:
        n = 60
    return {"curve": core.affect.mood_curve(n=n)}


# ============================================================ 语音（★可选能力）

def get_voice_status(core, body, params, handler):
    """语音能力状态。前端据此决定按钮是可用还是置灰 + 提示原因。"""
    st = core.voice.status()
    st["local_asr_note"] = (
        "浏览器 SpeechRecognition 在 Electron 内不可用（Chromium 不带云端 key），"
        "黑武士走「录音上传 + 云端转写」，桌面壳内可用。")
    return st


def post_voice_transcribe(core, body, params, handler):
    """上传一段音频，返回转写文本。请求体是原始音频字节。"""
    data = handler.read_raw() if hasattr(handler, "read_raw") else b""
    mime = handler.raw_header("X-Audio-Mime", "audio/webm") \
        if hasattr(handler, "raw_header") else "audio/webm"
    try:
        out = core.voice.transcribe(data, mime=mime)
    except Exception as ex:
        code = getattr(ex, "code", "voice_error")
        # 503 表示"能力未就绪"，前端据此降级而不是反复重试
        return 503 if code in ("not_configured",) else 502, {
            "error": str(ex), "code": code}
    if not out.get("text"):
        return {"ok": True, "text": "", "empty": True, "note": "未识别到语音内容"}
    return {"ok": True, **out}


def post_voice_speak(core, body, params, handler):
    """云端合成语音。不可用时返回 ok=False，前端回退到浏览器本地合成。"""
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return 400, {"error": "缺少 text"}
    audio = core.voice.synthesize(text, str((body or {}).get("voice") or ""))
    if not audio:
        return {"ok": False, "reason": "云端合成不可用，请用浏览器本地朗读"}
    return {"__raw__": audio, "status": 200, "ctype": "audio/mpeg"}


# ============================================================ 设置

def get_settings(core, body, params, handler):
    return core.public_config()


def post_settings(core, body, params, handler):
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    # 不接受通过 API 改安全相关项（避免被网页脚本偷偷放大权限）
    blocked = {"allow_lan", "api_token"}
    # v0.7.0：访问权同样不走通用设置口，必须走 /api/access/grant（显式端点、
    # 有独立审计），否则任何能打这个接口的脚本都能给自己开整盘读写。
    blocked |= {"full_fs_access", "allowed_paths"}
    patch = {k: v for k, v in body.items() if k not in blocked}
    out = core.update_config(patch)
    if blocked & set(body.keys()):
        out["_ignored_security_keys"] = sorted(blocked & set(body.keys()))
    return out


def get_providers(core, body, params, handler):
    return {"items": list_providers(), "current": core.config.get("provider")}


def post_activate(core, body, params, handler):
    """激活：写入 provider / key / model。

    ★切provider 时必须处理 base_url（2026-10-07 修的真实故障）：
    用户从 ollama 切到 deepseek，只改了 provider 和 model，配置里却还留着
    ``http://127.0.0.1:11434/v1``。而 resolve 一直是"base_url 非空就以它为准"，
    于是 deepseek 的请求被打到本地 ollama → **404 model not found**。
    用户看到的现象是"换了模型还是报 model 错"，根因跟模型名毫无关系。

    规则：请求里**带了** base_url 就按用户填的记（并标记它属于哪个 provider）；
    **没带**就说明用户只想换 provider —— 旧的 base_url 属于别的 provider，
    必须清掉，否则它会一直劫持。
    """
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    patch = {}
    for key in ("provider", "api_key", "model", "base_url"):
        if key in body:
            patch[key] = body[key]
    if not patch:
        return 400, {"error": "没有可写入的配置项"}

    cur_provider = str(core.config.get("provider", "") or "")
    new_provider = str(patch.get("provider") or cur_provider or "")

    if "base_url" in patch:
        # 用户显式给了地址：记下它属于谁，之后这个 provider 下一直有效
        patch["base_url_provider"] = new_provider
    elif new_provider and new_provider != cur_provider:
        # 换 provider 但没给地址 → 旧地址不属于新 provider，清掉
        patch["base_url"] = ""
        patch["base_url_provider"] = ""

    core.update_config(patch)
    ping = core.gateway.ping() if core.config.is_activated() else {"ok": False}
    BUS.clear_sticky("activation_required")
    return {"ok": True, "activated": core.config.is_activated(), "ping": ping,
            "provider": new_provider,
            "model": core.config.get("model", ""),
            "endpoint": core.gateway.endpoint()}


def post_llm_ping(core, body, params, handler):
    return core.gateway.ping()


# ============================================================ 事件

def get_recent_events(core, body, params, handler):
    types = None
    q = _query(handler)
    if "types" in q:
        types = [t for t in q["types"][0].split(",") if t]
    try:
        n = int(q.get("n", [50])[0])
    except Exception:
        n = 50
    return {"items": BUS.recent(n=n, types=types)}


def get_bus_stats(core, body, params, handler):
    return BUS.stats()


# ============================================================ 管理

def post_admin_start(core, body, params, handler):
    core.start()
    running = core.is_running()
    # ★ 必须如实返回 ok：原先无论是否真起来都写死 {"ok": true}，
    #   而 running 字段可能是 false —— 调用方（前端按钮）只看 ok 就提示
    #   "主循环已启动"，用户看到状态没变却收到成功提示，完全摸不着头脑。
    return {"ok": bool(running), "running": running,
            "error": "" if running else "主循环未能启动，请查看日志"}


def post_admin_stop(core, body, params, handler):
    core.stop()
    return {"ok": True, "running": core.is_running()}


def post_admin_reset_memories(core, body, params, handler):
    n = core.clear_memories()
    return {"ok": True, "cleared": n}


def post_admin_reset_conversations(core, body, params, handler):
    core.store.execute("DELETE FROM conversations")
    return {"ok": True}


# ============================================================ 工具函数

def _query(handler) -> Dict[str, Any]:
    """解析 query string。"""
    import urllib.parse

    parsed = urllib.parse.urlparse(handler.path)
    return urllib.parse.parse_qs(parsed.query)


# ============================================================ 任务续跑 / 后台消息（v0.6）

def get_tasks(core, body, params, handler):
    """任务列表（含进度与当前步骤）。query: state / limit。"""
    q = _query(handler)
    items = core.tasks.list_tasks(state=(q.get("state", [""])[0] or ""),
                                 limit=int(q.get("limit", ["30"])[0] or 30))
    return {"count": len(items), "items": items,
            "active": core.tasks.active_task()}


def post_task_create(core, body, params, handler):
    """创建多步骤任务。body: {title, steps(分号分隔), goal}。"""
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    steps = str(body.get("steps") or "")
    raw = [s.strip() for s in steps.replace("\n", ";").split(";") if s.strip()]
    if not raw:
        return 400, {"error": "缺少 steps（用分号分隔各步骤）"}
    return core.tasks.create(str(body.get("title") or ""), raw,
                             goal=str(body.get("goal") or ""))


def post_task_action(core, body, params, handler):
    """对某个任务执行动作。body: {task_id, action, ...}。

    action 取值：current / step_done / step_failed / complete / resume /
    skip / abandon。这是"一个端点覆盖所有动作"的设计：任务动作种类有限，
    拆成七个端点反而让前端和文档都变复杂。
    """
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    try:
        tid = int(body.get("task_id") or 0)
    except (TypeError, ValueError):
        return 400, {"error": "task_id 必须是整数"}
    if tid <= 0:
        return 400, {"error": "缺少 task_id"}
    act = str(body.get("action") or "").strip()
    result = str(body.get("result") or "")
    if act == "current":
        return core.tasks.current_step(tid)
    if act == "step_done":
        return core.tasks.step_done(tid, result=result)
    if act == "step_failed":
        return core.tasks.step_failed(tid, error=result or
                                     str(body.get("error") or ""))
    if act == "complete":
        return core.tasks.complete(tid, evidence=str(body.get("evidence") or ""))
    if act == "resume":
        return core.tasks.resume(tid)
    if act == "skip":
        return core.tasks.skip_step(tid, reason=result)
    if act == "pause":
        return core.tasks.pause(tid, reason=result)
    if act == "abandon":
        return core.tasks.abandon(tid, reason=result)
    return 400, {"error": f"未知 action：{act}",
                 "allowed": ["current", "step_done", "step_failed",
                             "complete", "resume", "skip", "pause",
                             "abandon"]}


def post_background(core, body, params, handler):
    """投一条后台消息进主循环（渠道/定时任务的统一入口）。"""
    if not isinstance(body, dict) or not body.get("text"):
        return 400, {"error": "缺少 text"}
    return core.push_background(
        str(body.get("text")), source=str(body.get("source") or "api"),
        dedupe_key=str(body.get("dedupe_key") or ""),
        channel=str(body.get("channel") or "background"))


# ============================================================ 渠道桥（v0.6.2）

def post_channel_inbound(core, body, params, handler):
    """外部渠道消息入口（微信/Discord/Webhook 回调的通用接收端）。

    body: {name, token, text, from_id?}。校验失败如实拒绝（不猜）。
    """
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    r = core.channels.inbound(
        name=str(body.get("name") or ""),
        token=str(body.get("token") or ""),
        text=str(body.get("text") or body.get("message") or ""),
        from_id=str(body.get("from_id") or ""))
    if not r.get("ok"):
        return 401, r
    return r


def get_channel_list(core, body, params, handler):
    """渠道清单（令牌打码）+ 投递计数。"""
    return core.channels.list_channels()


def post_channels(core, body, params, handler):
    """保存渠道列表（设置页「渠道接入」面板）。

    仅接受 ``{"channels": [...]}``；密钥保护 / 回调名对齐等规则在
    :meth:`ChannelBridge.save_channels` 里统一处理，这里只做收口。
    """
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    try:
        result = core.channels.save_channels(body.get("channels", []))
    except ValueError as e:
        return 400, {"error": str(e)}
    return result


def _make_channel_callback(kind: str):
    """生成飞书/企微事件回调端点。

    两个端点的共性：**必须读原始字节**验签，还要从 query 取校验参数，
    所以不能走通用的 ``_read_body`` 解析路径。
    """
    import urllib.parse

    from ..runtime import channel_inbound

    def _fn(core, body, params, handler):
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(handler.path).query)
        raw = b""
        try:
            raw = handler.read_raw()
        except Exception:
            raw = b""
        fn = (channel_inbound.handle_feishu if kind == "feishu"
              else channel_inbound.handle_wecom)
        res = fn(core.channels, method=str(handler.command), query=q,
                 body=raw, headers=getattr(handler, "headers", {}))
        return res.as_route_result()

    return _fn


# ============================================================ MCP（v0.7.0）

def get_mcp(core, body, params, handler):
    """MCP 服务器状态：连了几个、挂了几个工具、每个服务器什么错。"""
    mgr = getattr(core, "mcp", None)
    return mgr.status() if mgr else {"enabled": False, "servers": []}


def post_mcp_reload(core, body, params, handler):
    """重连所有 MCP 服务器（改完配置不用重启进程）。"""
    mgr = getattr(core, "mcp", None)
    if mgr is None:
        return 503, {"error": "MCP 管理器未初始化"}
    if not mgr.enabled():
        return {"ok": False, "reason": "mcp_enabled=False（配置关闭）"}
    specs = mgr.specs()
    if not specs:
        return {"ok": False, "reason": "mcp_servers 为空，先在配置里加服务器"}
    mgr.unregister(core.tools)
    mgr.stop_all()
    # 同步重连：接口调用方要立刻知道结果，别让"reload 已提交"变成薛定谔状态
    mgr.start(reg=core.tools, background=False)
    st = mgr.status()
    return {"ok": st.get("connected", 0) > 0, **st}


def get_mcp_tools(core, body, params, handler):
    """已挂载的 MCP 工具清单。"""
    items = [s.as_dict() for s in core.tools.specs() if s.category == "mcp"]
    return {"count": len(items), "items": items}


# ============================================================ 访问权（v0.7.0）

def get_access(core, body, params, handler):
    """我现在能读写的目录（沙箱 + 授权目录 + full_fs 开关）。"""
    return {
        "sandbox": str(core.paths.sandbox_dir()),
        "allowed_paths": [str(p) for p in core.paths.allowed_roots()],
        "full_fs_access": bool(core.paths.full_fs_access()),
    }


def post_access_grant(core, body, params, handler):
    """授权一个真实目录（等价于对话里让黑武士调 grant_access）。"""
    if not isinstance(body, dict) or not body.get("path"):
        return 400, {"error": "缺少 path"}
    path = str(body["path"])
    roots = list(core.config.get("allowed_paths", []) or [])
    if path in roots:
        return {"ok": True, "path": path, "already": True}
    roots.append(path)
    core.update_config({"allowed_paths": roots})
    return {"ok": True, "path": path, **get_access(core, None, None, None)}


def post_access_revoke(core, body, params, handler):
    """撤销一个目录的授权。"""
    if not isinstance(body, dict) or not body.get("path"):
        return 400, {"error": "缺少 path"}
    path = str(body["path"])
    roots = [r for r in (core.config.get("allowed_paths", []) or [])
             if str(r) != path]
    core.update_config({"allowed_paths": roots})
    return {"ok": True, "revoked": path,
            **get_access(core, None, None, None)}


# ============================================================ 本地媒体库（v0.5）

def get_media(core, body, params, handler):
    """媒体库总览：条目数/分类分布/总时长/已登记目录。"""
    stats = core.media.stats()
    if not stats.get("available"):
        return 503, stats
    return stats


def get_media_items(core, body, params, handler):
    """列媒体条目。query 参数：category / q / limit。"""
    # ★ 路由的 ``params`` 只装**路径占位符**（``{kind}``），
    #   query string 必须走 _query(handler) —— 直接 params.get("category")
    #   永远拿不到值（v0.5 接媒体时踩过，表现为 DELETE 恒400）。
    q = _query(handler)
    items = core.media.list(
        category=(q.get("category", [""])[0] or ""),
        query=(q.get("q", [""])[0] or ""),
        limit=int(q.get("limit", ["50"])[0] or 50))
    return {"count": len(items), "items": items}


def get_media_roots(core, body, params, handler):
    """已登记的媒体目录（不删磁盘文件，只是不再索引）。"""
    items = core.store.list_media_roots()
    return {"count": len(items), "items": items}


def post_media_root(core, body, params, handler):
    """登记一个媒体目录（不立即扫描）。"""
    if not isinstance(body, dict) or not body.get("path"):
        return 400, {"error": "缺少 path"}
    return core.media.add_root(str(body.get("path")),
                              bool(body.get("recursive", True)))


def delete_media_root(core, body, params, handler):
    """取消登记一个媒体目录（不删磁盘文件）。"""
    # query string 走 _query(handler)；params 只装路径占位符
    path = (_query(handler).get("path", [""])[0]
            or (body or {}).get("path") or "")
    if not path:
        return 400, {"error": "缺少 path"}
    return core.media.remove_root(str(path))


def post_media_scan(core, body, params, handler):
    """扫描已登记目录并更新索引（默认增量）。"""
    full = bool((body or {}).get("full"))
    return core.media.scan(full=full)


# ============================================================ 用户画像 / 面板 / 预取（v0.2）
def get_profile(core, body, params, handler):
    """已了解的用户画像（带置信度与依据）。"""
    data = core.profile.get() or {}
    return {"items": data, "count": len(data)}


def post_profile(core, body, params, handler):
    """设置/纠正某维度画像。"""
    if not isinstance(body, dict) or not body.get("aspect"):
        return 400, {"error": "缺少 aspect"}
    confidence = 0.9
    try:
        confidence = float(body.get("confidence", 0.9))
    except Exception:
        confidence = 0.9
    ok = core.profile.set(
        str(body["aspect"]), str(body.get("value") or ""),
        evidence=str(body.get("evidence") or ""), confidence=confidence)
    if not ok:
        return 400, {"error": "未知画像维度：" + str(body.get("aspect"))}
    return {"ok": True, "aspect": body["aspect"]}


def get_panels(core, body, params, handler):
    """全部信息面板摘要（天气/热点/人物卡）。"""
    return core.panels.as_dict()


def get_panel(core, body, params, handler):
    """单个信息面板（/api/panels/{kind}）。"""
    kind = params.get("kind") or "weather"
    payload = core.panels.get(kind)
    payload.pop("ts", None)
    return payload


def get_prefetch(core, body, params, handler):
    items = core.prefetch.list()
    return {"items": items, "count": len(items)}


def post_prefetch(core, body, params, handler):
    """登记一条预取 URL（立即抓取一次）。"""
    if not isinstance(body, dict) or not body.get("url"):
        return 400, {"error": "缺少 url"}
    ttl = 3600.0
    try:
        ttl = float(body.get("ttl") or 3600.0)
    except Exception:
        ttl = 3600.0
    return {"ok": True, **core.prefetch.add(str(body["url"]), ttl=ttl)}


def delete_prefetch(core, body, params, handler):
    n = core.prefetch.clear()
    return {"ok": True, "cleared": n}


# ============================================================ PASM V2 十九层心智（v0.3）

def get_mind(core, body, params, handler):
    """心智全景：十九层活跃度 + 安全层 + 成长 + 校验（★黑武士独有）。"""
    b = getattr(core, "pasm2", None)
    if b is None:
        return {"available": False, "reason": "未构建 PASM V2 桥接层"}
    st = b.status()
    st["safety_state"] = b.safety_state()
    st["n_layers"] = len(st.get("layers") or [])
    return st


def get_mind_layers(core, body, params, handler):
    """只取十九层（UI 轮询用，负载更小）。"""
    b = getattr(core, "pasm2", None)
    if b is None:
        return {"available": False, "layers": []}
    st = b.status()
    return {"available": st.get("available"), "reason": st.get("reason"),
            "layers": st.get("layers") or [], "step": st.get("step"),
            "entities": st.get("entities"), "symbols": st.get("symbols")}


def post_mind_observe(core, body, params, handler):
    """手动喂一步认知（演示/调试用：看 V2 如何把一句话变成象量与情绪）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return 400, {"error": "缺少 text"}
    return {"ok": True, "digest": b.observe(text)}


def post_mind_sleep(core, body, params, handler):
    """睡眠巩固：符号升降级 + 失衡告警（★"发现自己不对劲"）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    return {"ok": True, "report": b.night()}


def post_mind_growth(core, body, params, handler):
    """成长复盘：读失衡告警 → 提议参数调整（纯函数，不改运行中引擎）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    return {"ok": True, "review": b.growth_review()}


def post_mind_whatif(core, body, params, handler):
    """反事实推演：「如果那样做会怎样」（层 3 世界模型）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    seed = (body or {}).get("context_seed") or []
    try:
        seed = [int(x) for x in seed]
    except Exception:
        return 400, {"error": "context_seed 必须是整数数组"}
    steps = int((body or {}).get("steps") or 6)
    return {"ok": True, "result": b.what_if(seed, steps=steps)}


def post_mind_verify(core, body, params, handler):
    """三角校验：把一条声明/一次工具调用交给 V2 四层校验。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    if (body or {}).get("tool"):
        return {"ok": True, "verdict": b.verify_tool(
            str(body["tool"]), dict(body.get("args") or {}))}
    conclusion = str((body or {}).get("conclusion") or "").strip()
    if not conclusion:
        return 400, {"error": "需要 conclusion 或 tool"}
    return {"ok": True, "verdict": b.verify_text(conclusion,
                                                 list(body.get("facts") or []))}


def post_mind_gate_reset(core, body, params, handler):
    """复位安全层锁死（连续拒绝 20 次会锁死全部工具，防探测扫描）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    return {"ok": True, **b.reset_safety_lockout()}


def post_mind_tell(core, body, params, handler):
    """符号化注入：告诉认知体「当前感知叫什么」（SLH 桥）。"""
    b = getattr(core, "pasm2", None)
    if b is None or not getattr(b, "available", False):
        return 503, {"error": "PASM V2 未接入", "reason": getattr(b, "reason", "")}
    ref = str((body or {}).get("reference") or "").strip()
    if not ref:
        return 400, {"error": "缺少 reference"}
    return {"ok": True, **b.tell(ref)}


def build_routes() -> Dict[Tuple[str, str], Callable[..., Any]]:
    """构造路由表。"""
    return {
        ("GET", "/api/healthz"): get_health,
        ("GET", "/healthz"): get_health,
        ("GET", "/api/version"): get_version,
        ("GET", "/api/status"): get_status,
        ("GET", "/status"): get_status,
        ("GET", "/api/summary"): get_summary,

        ("POST", "/api/message"): post_message,
        ("POST", "/api/ask"): post_ask,
        ("GET", "/api/conversations"): get_conversations,

        ("GET", "/api/memories"): get_memories,
        ("POST", "/api/memories"): post_memories,
        ("PATCH", "/api/memories/{id}"): patch_memory,
        ("DELETE", "/api/memories/{id}"): delete_memory,
        ("POST", "/api/memories/search"): post_memory_search,
        ("GET", "/api/actions"): get_actions,

        ("GET", "/api/reminders"): get_reminders,
        ("POST", "/api/reminders"): post_reminders,
        ("DELETE", "/api/reminders/{id}"): delete_reminder,

        ("GET", "/api/tools"): get_tools,

        ("GET", "/api/voice/status"): get_voice_status,
        ("POST", "/api/voice/transcribe"): post_voice_transcribe,
        ("POST", "/api/voice/speak"): post_voice_speak,

        ("GET", "/api/cognition"): get_cognition,
        ("POST", "/api/cognition/consolidate"): post_consolidate,
        ("GET", "/api/cognition/mood"): get_mood_curve,

        ("POST", "/api/channel/inbound"): post_channel_inbound,
        ("GET", "/api/channel/list"): get_channel_list,
        # v0.8.x：设置页「渠道接入」面板（读取清单 / 保存整份列表）
        ("GET", "/api/channels"): get_channel_list,
        ("POST", "/api/channels"): post_channels,
        # v0.7.0：飞书 / 企业微信事件回调（GET=URL 验证，POST=消息）
        ("GET", "/api/channel/feishu"): _make_channel_callback("feishu"),
        ("POST", "/api/channel/feishu"): _make_channel_callback("feishu"),
        ("GET", "/api/channel/wecom"): _make_channel_callback("wecom"),
        ("POST", "/api/channel/wecom"): _make_channel_callback("wecom"),

        # ---- v0.7.0：MCP 外部工具生态 ----
        ("GET", "/api/mcp"): get_mcp,
        ("POST", "/api/mcp/reload"): post_mcp_reload,
        ("GET", "/api/mcp/tools"): get_mcp_tools,

        # ---- v0.7.0：真实世界访问权 ----
        ("GET", "/api/access"): get_access,
        ("POST", "/api/access/grant"): post_access_grant,
        ("POST", "/api/access/revoke"): post_access_revoke,

        # ---- v0.8.0：贾维斯面板（唤醒 / 免提 / 授权确认）----
        # ---- v0.8.8：环境自检与一键修复 ----
        ("GET", "/api/doctor"): get_doctor,
        ("GET", "/api/doctor/jobs"): get_doctor_jobs,
        ("POST", "/api/doctor/fix"): post_doctor_fix,

        ("GET", "/api/jarvis/state"): get_jarvis_state,
        ("GET", "/api/jarvis/audio"): get_jarvis_audio,
        ("POST", "/api/jarvis/transcribe"): post_jarvis_transcribe,
        ("POST", "/api/jarvis/utterance"): post_jarvis_utterance,
        ("POST", "/api/jarvis/confirm"): post_jarvis_confirm,

        ("GET", "/api/settings"): get_settings,
        ("POST", "/api/settings"): post_settings,
        ("GET", "/api/providers"): get_providers,
        ("POST", "/api/activate"): post_activate,
        ("POST", "/api/llm/ping"): post_llm_ping,

        ("GET", "/api/events/recent"): get_recent_events,
        ("GET", "/api/bus"): get_bus_stats,

        ("POST", "/api/admin/start"): post_admin_start,
        ("POST", "/api/admin/stop"): post_admin_stop,
        ("POST", "/api/admin/reset-memories"): post_admin_reset_memories,
        ("POST", "/api/admin/reset-conversations"): post_admin_reset_conversations,

        # ---- v0.2：用户画像 / 信息面板 / 预取缓存 ----
        ("GET", "/api/profile"): get_profile,
        ("POST", "/api/profile"): post_profile,
        ("GET", "/api/panels"): get_panels,
        ("GET", "/api/panels/{kind}"): get_panel,
        ("GET", "/api/prefetch"): get_prefetch,
        ("POST", "/api/prefetch"): post_prefetch,
        ("DELETE", "/api/prefetch"): delete_prefetch,

        # ---- v0.5：本地媒体库 ----
        ("GET", "/api/media"): get_media,
        ("GET", "/api/media/items"): get_media_items,
        ("GET", "/api/media/roots"): get_media_roots,
        ("POST", "/api/media/roots"): post_media_root,
        ("DELETE", "/api/media/roots"): delete_media_root,
        ("POST", "/api/media/scan"): post_media_scan,

        # ---- v0.6：任务续跑 / 后台消息 ----
        ("GET", "/api/tasks"): get_tasks,
        ("POST", "/api/tasks"): post_task_create,
        ("POST", "/api/tasks/action"): post_task_action,
        ("POST", "/api/background"): post_background,

        # ---- v0.3：PASM V2 十九层心智 ----
        ("GET", "/api/mind"): get_mind,
        ("GET", "/api/mind/layers"): get_mind_layers,
        ("POST", "/api/mind/observe"): post_mind_observe,
        ("POST", "/api/mind/sleep"): post_mind_sleep,
        ("POST", "/api/mind/growth"): post_mind_growth,
        ("POST", "/api/mind/whatif"): post_mind_whatif,
        ("POST", "/api/mind/verify"): post_mind_verify,
        ("POST", "/api/mind/tell"): post_mind_tell,
        ("POST", "/api/mind/gate-reset"): post_mind_gate_reset,
    }
