"""REST 路由。

提供对话 / 记忆 / 工具 / 认知四类接口，并独有认知与心智两组接口
（``/api/cognition/*`` —— 有了内核才谈得上把内核状态暴露出来）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..events import BUS
from ..llm.providers import list_providers
from ..runtime import PRIORITY_USER
from ..version import version_info


def _json(fn):  # noqa: ANN001 - 装饰签名通用的 handler
    return fn


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
    patch = {k: v for k, v in body.items() if k not in blocked}
    return core.update_config(patch)


def get_providers(core, body, params, handler):
    return {"items": list_providers(), "current": core.config.get("provider")}


def post_activate(core, body, params, handler):
    """激活：写入 provider / key / model。"""
    if not isinstance(body, dict):
        return 400, {"error": "需要 JSON 对象"}
    patch = {}
    for key in ("provider", "api_key", "model", "base_url"):
        if key in body:
            patch[key] = body[key]
    if not patch:
        return 400, {"error": "没有可写入的配置项"}
    core.update_config(patch)
    ping = core.gateway.ping() if core.config.is_activated() else {"ok": False}
    BUS.clear_sticky("activation_required")
    return {"ok": True, "activated": core.config.is_activated(), "ping": ping}


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
    return {"ok": True, "running": core.is_running()}


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
