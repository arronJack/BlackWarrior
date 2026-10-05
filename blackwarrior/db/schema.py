"""数据库表结构。

黑武士用 SQLite 做长期状态持久化（标准库自带，零依赖）。选择它的理由和白马一致：
单机桌面应用不需要外部数据库服务，且全文检索、索引、事务都够用。

表清单::

    conversations  对话记录（多渠道统一）
    memories       结构化记忆（供 UI 浏览/编辑，与认知内核侧车文件互补）
    actions        行动日志（工具调用，供审计与"我做过什么"回溯）
    reminders      提醒
    tasks          长任务
    ui_signals     UI 信号（前端写入、内核读取）
    meta           键值状态

注意：认知内核的向量索引/复习计数存在 ``cognition/`` 侧车文件里（由 pasm-skills 管理），
这里只存"给人看、给 UI 用"的结构化副本，二者通过 ``memories.key`` 关联。
"""

from __future__ import annotations

SCHEMA_VERSION = 1

TABLES: "list[str]" = [
    # ---- 对话 ----
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id     TEXT    NOT NULL DEFAULT '',
        role        TEXT    NOT NULL,              -- user / agent / system / tool
        content     TEXT    NOT NULL DEFAULT '',
        channel     TEXT    NOT NULL DEFAULT 'ui', -- ui / wechat / webhook / ...
        from_id     TEXT    NOT NULL DEFAULT '',
        ts          REAL    NOT NULL,
        meta        TEXT    NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conv_turn ON conversations(turn_id)
    """,

    # ---- 记忆 ----
    """
    CREATE TABLE IF NOT EXISTS memories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT    NOT NULL DEFAULT '',   -- 与认知侧车索引关联
        title       TEXT    NOT NULL,
        brief       TEXT    NOT NULL DEFAULT '',
        tags        TEXT    NOT NULL DEFAULT '[]',
        category    TEXT    NOT NULL DEFAULT '日常',
        salience    INTEGER NOT NULL DEFAULT 1,    -- 1..5
        retention   REAL    NOT NULL DEFAULT 1.0,  -- 遗忘曲线保留度
        hits        INTEGER NOT NULL DEFAULT 0,    -- 被检索命中次数
        source      TEXT    NOT NULL DEFAULT 'chat',
        archived    INTEGER NOT NULL DEFAULT 0,
        ts          REAL    NOT NULL,
        updated_at  REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mem_cat ON memories(category)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mem_title ON memories(title)
    """,

    # ---- 行动日志 ----
    """
    CREATE TABLE IF NOT EXISTS actions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id     TEXT    NOT NULL DEFAULT '',
        tool        TEXT    NOT NULL,
        args        TEXT    NOT NULL DEFAULT '{}',
        ok          INTEGER NOT NULL DEFAULT 1,
        summary     TEXT    NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        ts          REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_act_ts ON actions(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_act_tool ON actions(tool)
    """,

    # ---- 提醒 ----
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        body        TEXT    NOT NULL DEFAULT '',
        due_at      REAL    NOT NULL,
        status      TEXT    NOT NULL DEFAULT 'pending',  -- pending / fired / cancelled
        channel     TEXT    NOT NULL DEFAULT 'ui',
        created_at  REAL    NOT NULL,
        fired_at    REAL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(status, due_at)
    """,

    # ---- 任务 ----
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        state       TEXT    NOT NULL DEFAULT 'active',   -- active / done / failed / paused
        progress    REAL    NOT NULL DEFAULT 0.0,
        payload     TEXT    NOT NULL DEFAULT '{}',
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_state ON tasks(state)
    """,

    # ---- UI 信号 ----
    """
    CREATE TABLE IF NOT EXISTS ui_signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT    NOT NULL,
        payload     TEXT    NOT NULL DEFAULT '{}',
        consumed    INTEGER NOT NULL DEFAULT 0,
        ts          REAL    NOT NULL
    )
    """,

    # ---- 键值状态 ----
    """
    CREATE TABLE IF NOT EXISTS meta (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL DEFAULT '{}',
        updated_at  REAL NOT NULL
    )
    """,
]


def create_all(conn) -> None:
    """建表。可重复调用。"""
    cur = conn.cursor()
    for sql in TABLES:
        cur.execute(sql)
    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES(?,?,?)",
        ("schema_version", str(SCHEMA_VERSION), _now()),
    )
    conn.commit()


def _now() -> float:
    import time

    return time.time()
