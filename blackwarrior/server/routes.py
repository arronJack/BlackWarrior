"""REST 路由。

对齐同类项目暴露的接口，并补齐它们没有的认知接口
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
    }
