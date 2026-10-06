"""数据库表结构。

黑武士用 SQLite 做长期状态持久化（标准库自带，零依赖）。选它的理由：
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

SCHEMA_VERSION = 2

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

    # ---- 记忆线索（v0.3：联想边，补纯 FTS5 抓不到的语义关系）----
    # 记忆线索模型。两表结构：clues 存边，audit 存变更账本。
    """
    CREATE TABLE IF NOT EXISTS clues (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id     INTEGER NOT NULL,
        to_id       INTEGER NOT NULL,
        kind        TEXT    NOT NULL DEFAULT 'related',
        strength    REAL    NOT NULL DEFAULT 1.0,
        ts          REAL    NOT NULL,
        UNIQUE(from_id, to_id, kind)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_clue_from ON clues(from_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_clue_to ON clues(to_id)
    """,

    # ---- 记忆审计账本（v0.3：谁在何时写了/改了什么，可回溯追责）----
    """
    CREATE TABLE IF NOT EXISTS memory_audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mem_id      INTEGER NOT NULL DEFAULT 0,
        op          TEXT    NOT NULL,              -- create / update / delete
        field       TEXT    NOT NULL DEFAULT '',
        before      TEXT    NOT NULL DEFAULT '',
        after       TEXT    NOT NULL DEFAULT '',
        source      TEXT    NOT NULL DEFAULT '',
        ts          REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_maudit_ts ON memory_audit(ts DESC)
    """,

    # ---- 用户画像（v0.2 新增：让黑武士"更懂你"）----
    """
    CREATE TABLE IF NOT EXISTS user_profile (
        aspect      TEXT PRIMARY KEY,             -- name/role/domain/expertise/...
        value       TEXT NOT NULL DEFAULT '',
        evidence    TEXT NOT NULL DEFAULT '',     -- 这条画像来自哪句话
        confidence  REAL NOT NULL DEFAULT 0.5,    -- 0..1，越高越可信
        updated_at  REAL NOT NULL
    )
    """,

    # ---- 预取缓存（v0.2 新增：周期性信息预热，注入上下文）----
    """
    CREATE TABLE IF NOT EXISTS prefetch_cache (
        url         TEXT PRIMARY KEY,
        content     TEXT NOT NULL DEFAULT '',
        fetched_at  REAL NOT NULL,
        ttl         REAL NOT NULL DEFAULT 3600.0   -- 有效秒数
    )
    """,

    # ---- 记忆全文索引（v0.2 新增：FTS5 trigram，改善中文子串检索）----
    # FTS5 trigram 做中文全文索引：轻量档（light）下语义检索较弱，
    # trigram 兜底让"聊过的词"都能被搜到。
    #
    # ⚠ 外部内容表（content='memories'）的删除/更新**必须**用 FTS5 专用的
    # 'delete' 命令形式，不能写普通 DELETE —— 否则触发器会抛
    # "database disk image is malformed"，且索引与内容表静默失配。
    # 先 DROP 再建，保证旧库升级到新触发器定义。
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(title, brief, tags, content='memories', content_rowid='id',
               tokenize='trigram')
    """,
    "DROP TRIGGER IF EXISTS memories_ai",
    "DROP TRIGGER IF EXISTS memories_ad",
    "DROP TRIGGER IF EXISTS memories_au",
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, title, brief, tags)
        VALUES (new.id, new.title, new.brief, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, brief, tags)
        VALUES ('delete', old.id, old.title, old.brief, old.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, brief, tags)
        VALUES ('delete', old.id, old.title, old.brief, old.tags);
        INSERT INTO memories_fts(rowid, title, brief, tags)
        VALUES (new.id, new.title, new.brief, new.tags);
    END
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
