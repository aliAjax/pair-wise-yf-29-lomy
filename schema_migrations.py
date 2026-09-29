"""旧库升级与事件迁移。

升级目标：
1. custody_events 的 CHECK 增加 RETENTION_CHANGED，老库需要重建事件表，
   但完整保留旧事件的 sequence、previous_hash、event_hash——证据编号和
   事件链不会被割开，旧证据升级后继续可查、哈希可校验。
2. evidence 增加 original_retention_until，旧证据回填为当前到期日，
   报告里始终能看到最初入册时的原日期。
3. 新建 retention_reviews 复核申请表。

幂等：以 PRAGMA user_version 记录版本，重复执行安全；全新库直接建到最新。
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

USERS_SQL = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
)
"""
CASES_SQL = """
CREATE TABLE IF NOT EXISTS cases(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
)
"""
MEMBERS_SQL = """
CREATE TABLE IF NOT EXISTS case_members(
    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
    PRIMARY KEY(case_id,user_id)
)
"""
EVIDENCE_SQL = """
CREATE TABLE IF NOT EXISTS evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
        CHECK(status IN ('custody','opened','released','derivative')),
    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
    retention_until TEXT NOT NULL,
    original_retention_until TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(case_id,label)
)
"""
# 最新事件表 CHECK：保留期限复核通过后追加的事件类型。
EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS custody_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED','RETENTION_CHANGED')),
    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
)
"""
DERIVATIVES_SQL = """
CREATE TABLE IF NOT EXISTS derivatives(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
)
"""
AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
)
"""
REVIEWS_SQL = """
CREATE TABLE IF NOT EXISTS retention_reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    request_type TEXT NOT NULL CHECK(request_type IN ('extend','early_end')),
    current_retention_until TEXT NOT NULL, requested_retention_until TEXT NOT NULL,
    reason TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(id), submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    decided_by TEXT REFERENCES users(id), decided_at TEXT, decision_note TEXT NOT NULL DEFAULT '',
    legal_hold_at_submit INTEGER NOT NULL CHECK(legal_hold_at_submit IN (0,1)),
    evidence_status_at_submit TEXT NOT NULL, submitter_role_at_submit TEXT NOT NULL
)
"""
REVIEWS_PENDING_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_retention_reviews_pending "
    "ON retention_reviews(evidence_id) WHERE status='pending'"
)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_names(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _latest_schema_script():
    statements = [
        USERS_SQL, CASES_SQL, MEMBERS_SQL, EVIDENCE_SQL, EVENTS_SQL,
        DERIVATIVES_SQL, AUDIT_SQL, REVIEWS_SQL, REVIEWS_PENDING_INDEX,
    ]
    return "\n".join(s.strip().rstrip(";") + ";" for s in statements)


def _create_reviews_table(conn):
    conn.execute(REVIEWS_SQL)
    conn.execute(REVIEWS_PENDING_INDEX)


def _rebuild_events_table(conn):
    """把 RETENTION_CHANGED 加入事件类型 CHECK，逐条搬旧事件并保留全部哈希。"""
    conn.execute("ALTER TABLE custody_events RENAME TO custody_events_legacy")
    conn.execute(EVENTS_SQL)
    conn.execute(
        """INSERT INTO custody_events
           (id,evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)
           SELECT id,evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at
           FROM custody_events_legacy"""
    )
    conn.execute("DROP TABLE custody_events_legacy")


def _upgrade_from_v1(conn):
    # 证据表：追加原始保留期限列并回填旧证据，老数据不动。
    if "original_retention_until" not in _column_names(conn, "evidence"):
        conn.execute("ALTER TABLE evidence ADD COLUMN original_retention_until TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE evidence SET original_retention_until=retention_until WHERE original_retention_until=''")
    # 事件表：只有老 CHECK（不含 RETENTION_CHANGED）才需要重建。
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='custody_events'").fetchone()[0]
    if "RETENTION_CHANGED" not in ddl:
        _rebuild_events_table(conn)
    # 复核表：全新引入。
    if not _table_exists(conn, "retention_reviews"):
        _create_reviews_table(conn)


def migrate(db_path) -> int:
    """升级到最新结构，返回升级后的版本号。可重复调用。"""
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.isolation_level = None  # 显式事务，配合 ALTER TABLE 重建
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not _table_exists(conn, "evidence"):
            # 全新空库：直接建到最新。executescript 自身会先提交再执行脚本，
            # 这里不额外包 BEGIN/COMMIT。
            conn.executescript(_latest_schema_script())
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return SCHEMA_VERSION
        if version < SCHEMA_VERSION:
            if _table_exists(conn, "evidence"):
                # 已在使用的旧库（未记录版本号或版本落后）：逐步升级。
                conn.execute("BEGIN")
                _upgrade_from_v1(conn)
                conn.execute("COMMIT")
            else:
                # 空库兜底：直接建到最新。
                conn.executescript(_latest_schema_script())
        # 兜底：部分初始化的老库若缺少复核表也补上。
        if not _table_exists(conn, "retention_reviews"):
            conn.execute("BEGIN")
            _create_reviews_table(conn)
            conn.execute("COMMIT")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return SCHEMA_VERSION
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
