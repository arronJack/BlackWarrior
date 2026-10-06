"""SQLite 读写封装。

并发模型：SQLite 连接本身不是跨线程安全的，这里用**单连接 + 一把锁**。
桌面 Agent 的写入量远小于读取量，一把锁不会成为瓶颈，却能让代码保持简单可靠。

开启 WAL 后读写不互相阻塞，SSE 推流的同时主循环写库也不会卡。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import schema


def _now() -> float:
    return time.time()


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def _load(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


class Store:
    """数据库门面。"""

    def __init__(self, path: Optional[str] = None, *, verbose: bool = False) -> None:
        from .. import paths

        self.path = str(path or paths.db_path())
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._verbose = verbose
        self.connect()

    # ------- 连接 -------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self.path, check_same_thread=False,
                               timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        self._conn = conn
        schema.create_all(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------- 底层 -------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            conn = self.connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            cur = conn.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------- 对话 -------------------------------------------------

    def add_message(self, role: str, content: str, *, turn_id: str = "",
                    channel: str = "ui", from_id: str = "",
                    meta: Optional[Dict[str, Any]] = None) -> int:
        cur = self.execute(
            "INSERT INTO conversations(turn_id, role, content, channel, from_id, ts, meta)"
            " VALUES(?,?,?,?,?,?,?)",
            (turn_id, role, content, channel, from_id, _now(), _dump(meta or {})),
        )
        return int(cur.lastrowid or 0)

    def recent_messages(self, limit: int = 50, channel: Optional[str] = None
                        ) -> List[Dict[str, Any]]:
        if channel:
            rows = self.query(
                "SELECT * FROM conversations WHERE channel=? ORDER BY ts DESC LIMIT ?",
                (channel, int(limit)))
        else:
            rows = self.query(
                "SELECT * FROM conversations ORDER BY ts DESC LIMIT ?", (int(limit),))
        rows.reverse()
        for r in rows:
            r["meta"] = _load(r.get("meta"), {})
        return rows

    def messages_of_turn(self, turn_id: str) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM conversations WHERE turn_id=? ORDER BY ts ASC", (turn_id,))
        for r in rows:
            r["meta"] = _load(r.get("meta"), {})
        return rows

    # ------- 记忆 -------------------------------------------------

    def add_memory(self, title: str, brief: str = "", *, tags: Optional[Iterable[str]] = None,
                   category: str = "日常", salience: int = 1, source: str = "chat",
                   key: str = "", retention: float = 1.0) -> int:
        now = _now()
        cur = self.execute(
            "INSERT INTO memories(key, title, brief, tags, category, salience,"
            " retention, hits, source, archived, ts, updated_at)"
            " VALUES(?,?,?,?,?,?,?,0,?,0,?,?)",
            (key, title, brief, _dump(list(tags or [])), category,
             max(1, min(5, int(salience))), float(retention), source, now, now),
        )
        return int(cur.lastrowid or 0)

    def list_memories(self, limit: int = 100, category: Optional[str] = None,
                      include_archived: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memories"
        clauses, params = [], []
        if not include_archived:
            clauses.append("archived=0")
        if category:
            clauses.append("category=?")
            params.append(category)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self.query(sql, params)
        for r in rows:
            r["tags"] = _load(r.get("tags"), [])
        return rows

    def get_memory(self, mem_id: int) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM memories WHERE id=?", (int(mem_id),))
        if row:
            row["tags"] = _load(row.get("tags"), [])
        return row

    def update_memory(self, mem_id: int, **fields: Any) -> bool:
        allowed = {"title", "brief", "tags", "category", "salience",
                   "retention", "archived", "hits"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "tags":
                v = _dump(list(v or []))
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_now())
        params.append(int(mem_id))
        # 审计：先取旧值再改，才能记到 before/after
        try:
            old = self.get_memory(mem_id) or {}
        except Exception:
            old = {}
        self.execute("UPDATE memories SET " + ",".join(sets) + " WHERE id=?", params)
        for k in fields:
            if k in old:
                self.add_memory_audit(mem_id, "update", field=k,
                                      before=str(old.get(k))[:200],
                                      after=str(fields[k])[:200], source="update")
        return True

    def delete_memory(self, mem_id: int) -> bool:
        try:
            old = self.get_memory(mem_id) or {}
        except Exception:
            old = {}
        self.add_memory_audit(mem_id, "delete", field="*",
                              before=str(old.get("title") or "")[:200],
                              source="delete")
        self.execute("DELETE FROM memories WHERE id=?", (int(mem_id),))
        return True

    def bump_memory_hits(self, mem_id: int) -> None:
        self.execute(
            "UPDATE memories SET hits=hits+1, updated_at=? WHERE id=?",
            (_now(), int(mem_id)))

    def search_memories(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """检索记忆：优先 FTS5 trigram 中文全文，失败回退 LIKE 字面匹配。

        语义检索仍由认知内核负责；这里解决"轻量档下中文子串也能被搜到"的问题，
        对标白马 AI 的 FTS5 trigram 中文检索能力。

        注意 trigram 分词器的**固有限制**：只有 ≥3 字符的查询串才能建索引命中，
        2 字查询（如"散步"）FTS 一定返回空，此时由下面的 LIKE 分支兜住。
        所以"查不到"不等于"没这条记忆"——两层都空才是真的没有。
        """
        q = (query or "").strip()
        if not q:
            return []
        try:
            rows = self.query(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id=f.rowid"
                " WHERE memories_fts MATCH ? AND m.archived=0"
                " ORDER BY rank LIMIT ?",
                (q, int(limit)))
            if rows:
                for r in rows:
                    r["tags"] = _load(r.get("tags"), [])
                return rows
        except Exception:
            pass
        like = f"%{q}%"
        rows = self.query(
            "SELECT * FROM memories WHERE archived=0 AND"
            " (title LIKE ? OR brief LIKE ? OR tags LIKE ?)"
            " ORDER BY salience DESC, ts DESC LIMIT ?",
            (like, like, like, int(limit)))
        for r in rows:
            r["tags"] = _load(r.get("tags"), [])
        return rows

    def count_memories(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM memories WHERE archived=0")
        return int(row["n"]) if row else 0

    def clear_memories(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM memories")
        n = int(row["n"]) if row else 0
        self.execute("DELETE FROM memories")
        return n

    # ------- 行动日志 ---------------------------------------------

    def log_action(self, tool: str, *, turn_id: str = "", args: Any = None,
                   ok: bool = True, summary: str = "",
                   duration_ms: int = 0) -> int:
        cur = self.execute(
            "INSERT INTO actions(turn_id, tool, args, ok, summary, duration_ms, ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (turn_id, tool, _dump(args if args is not None else {}),
             1 if ok else 0, summary[:2000], int(duration_ms), _now()),
        )
        return int(cur.lastrowid or 0)

    def recent_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (int(limit),))
        for r in rows:
            r["args"] = _load(r.get("args"), {})
            r["ok"] = bool(r.get("ok"))
        return rows

    # ------- 提醒 -------------------------------------------------

    def add_reminder(self, title: str, due_at: float, body: str = "",
                     channel: str = "ui") -> int:
        cur = self.execute(
            "INSERT INTO reminders(title, body, due_at, status, channel, created_at)"
            " VALUES(?,?,?, 'pending', ?, ?)",
            (title, body, float(due_at), channel, _now()),
        )
        return int(cur.lastrowid or 0)

    def due_reminders(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM reminders WHERE status='pending' AND due_at<=?"
            " ORDER BY due_at ASC",
            (float(now if now is not None else _now()),))

    def next_reminder(self) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM reminders WHERE status='pending' ORDER BY due_at ASC LIMIT 1")

    def fire_reminder(self, rem_id: int) -> None:
        self.execute(
            "UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
            (_now(), int(rem_id)))

    def cancel_reminder(self, rem_id: int) -> None:
        self.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (int(rem_id),))

    def list_reminders(self, status: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        if status:
            return self.query(
                "SELECT * FROM reminders WHERE status=? ORDER BY due_at ASC LIMIT ?",
                (status, int(limit)))
        return self.query("SELECT * FROM reminders ORDER BY due_at DESC LIMIT ?",
                          (int(limit),))

    # ------- 任务 -------------------------------------------------

    def add_task(self, title: str, payload: Optional[Dict[str, Any]] = None) -> int:
        now = _now()
        cur = self.execute(
            "INSERT INTO tasks(title, state, progress, payload, created_at, updated_at)"
            " VALUES(?,'active',0,?,?,?)",
            (title, _dump(payload or {}), now, now))
        return int(cur.lastrowid or 0)

    def update_task(self, task_id: int, **fields: Any) -> bool:
        allowed = {"title", "state", "progress"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_now())
        params.append(int(task_id))
        self.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", params)
        return True

    def active_tasks(self) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM tasks WHERE state='active' ORDER BY updated_at DESC")
        for r in rows:
            r["payload"] = _load(r.get("payload"), {})
        return rows

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                          (int(limit),))
        for r in rows:
            r["payload"] = _load(r.get("payload"), {})
        return rows

    # ------- UI 信号 ----------------------------------------------

    def push_signal(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> int:
        cur = self.execute(
            "INSERT INTO ui_signals(kind, payload, consumed, ts) VALUES(?,?,0,?)",
            (kind, _dump(payload or {}), _now()))
        return int(cur.lastrowid or 0)

    def pending_signals(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM ui_signals WHERE consumed=0 ORDER BY ts ASC LIMIT ?",
            (int(limit),))
        for r in rows:
            r["payload"] = _load(r.get("payload"), {})
        return rows

    def consume_signals(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.execute(f"UPDATE ui_signals SET consumed=1 WHERE id IN ({marks})", ids)

    # ------- 键值 -------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        if not row:
            return default
        return _load(row.get("value"), default)

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES(?,?,?)",
            (key, _dump(value), _now()))

    # ------- 用户画像（v0.2）-------------------------------------

    def upsert_profile(self, aspect: str, value: str, *, evidence: str = "",
                       confidence: float = 0.5) -> None:
        self.execute(
            "INSERT INTO user_profile(aspect, value, evidence, confidence, updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(aspect) DO UPDATE SET value=excluded.value,"
            " evidence=excluded.evidence, confidence=excluded.confidence,"
            " updated_at=excluded.updated_at",
            (str(aspect), str(value), str(evidence), float(confidence), _now()))

    def list_profile(self) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM user_profile ORDER BY confidence DESC")
        return rows

    def clear_profile(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM user_profile")
        n = int(row["n"]) if row else 0
        self.execute("DELETE FROM user_profile")
        return n

    # ------- 预取缓存（v0.2）-------------------------------------

    def add_prefetch(self, url: str, content: str, ttl: float = 3600.0) -> None:
        self.execute(
            "INSERT INTO prefetch_cache(url, content, fetched_at, ttl) VALUES(?,?,?,?)"
            " ON CONFLICT(url) DO UPDATE SET content=excluded.content,"
            " fetched_at=excluded.fetched_at, ttl=excluded.ttl",
            (str(url), str(content)[:4000], _now(), float(ttl)))

    def list_prefetch(self) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM prefetch_cache ORDER BY fetched_at DESC")
        return rows

    def serve_prefetch(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回仍在有效期内的预取内容（供上下文注入）。"""
        now = float(now if now is not None else _now())
        rows = self.query(
            "SELECT url, content, fetched_at, ttl FROM prefetch_cache")
        out = []
        for r in rows:
            if now - float(r.get("fetched_at") or 0) <= float(r.get("ttl") or 0):
                out.append({"url": r["url"], "content": r["content"]})
        return out

    def purge_prefetch(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM prefetch_cache")
        n = int(row["n"]) if row else 0
        self.execute("DELETE FROM prefetch_cache")
        return n

    # ------- 线索与审计（v0.3）----------------------------------

    def add_clue(self, from_id: int, to_id: int, kind: str = "related",
                 strength: float = 1.0) -> bool:
        """建立一条记忆联想边（重复调用为加强，不新增行）。"""
        self.execute(
            "INSERT INTO clues(from_id, to_id, kind, strength, ts)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(from_id, to_id, kind) DO UPDATE SET"
            " strength=MIN(2.0, strength + excluded.strength),"
            " ts=excluded.ts",
            (int(from_id), int(to_id), str(kind or "related"),
             float(strength), _now()))
        self.add_memory_audit(int(to_id), "link", field=str(kind),
                              after=f"clue:{from_id}", source="link_clue")
        return True

    def list_clues(self, mem_id: int) -> List[Dict[str, Any]]:
        """列出与某条记忆相连的全部线索（双向）。"""
        rows = self.query(
            "SELECT * FROM clues WHERE from_id=? OR to_id=?"
            " ORDER BY strength DESC, ts DESC",
            (int(mem_id), int(mem_id)))
        for r in rows:
            r["peer"] = int(r["to_id"]) if int(r["from_id"]) == int(mem_id) \
                else int(r["from_id"])
        return rows

    def add_memory_audit(self, mem_id: int, op: str, *, field: str = "",
                         before: str = "", after: str = "",
                         source: str = "") -> None:
        try:
            self.execute(
                "INSERT INTO memory_audit(mem_id, op, field, before, after,"
                " source, ts) VALUES(?,?,?,?,?,?,?)",
                (int(mem_id or 0), str(op), str(field)[:80],
                 str(before)[:500], str(after)[:500], str(source)[:40], _now()))
        except Exception:
            # 审计写失败不能影响主流程（表可能还不存在）
            pass

    def memory_audit(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM memory_audit ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),))

    # ------- 运维 -------------------------------------------------

    def reset_all(self) -> Dict[str, int]:
        """清空运行数据（设置页的"重置"按钮）。"""
        counts = {}
        for table in ("conversations", "memories", "actions", "reminders",
                      "tasks", "ui_signals"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(row["n"]) if row else 0
            self.execute(f"DELETE FROM {table}")
        return counts

    def stats(self) -> Dict[str, int]:
        out = {}
        for table in ("conversations", "memories", "actions", "reminders",
                      "tasks", "ui_signals"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        return out
