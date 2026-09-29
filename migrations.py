"""事件迁移与旧库升级层：只负责 schema 版本与保管事件链的迁移。

旧版数据库（user_version=0，custody_events.event_type 不含期限复核事件）
升级时不会切断原保管编号和事件链：通过重建表放宽 event_type 的 CHECK 约束，
逐条复制事件（sequence / previous_hash / event_hash 原样保留），旧证据继续可查。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# V1：在初版 schema 基础上扩展 custody_events 的事件类型，新增 retention_reviews。
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS cases(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_members(
    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
    PRIMARY KEY(case_id,user_id)
);
CREATE TABLE IF NOT EXISTS evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
        CHECK(status IN ('custody','opened','released','derivative')),
    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
    retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(case_id,label)
);
CREATE TABLE IF NOT EXISTS custody_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED',
        'RETENTION_EXTEND','RETENTION_SHORTEN')),
    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
);
CREATE TABLE IF NOT EXISTS derivatives(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    review_type TEXT NOT NULL CHECK(review_type IN ('extend','shorten')),
    reason TEXT NOT NULL, current_retention_until TEXT NOT NULL,
    new_retention_until TEXT NOT NULL,
    legal_hold_snapshot INTEGER NOT NULL CHECK(legal_hold_snapshot IN (0,1)),
    status_snapshot TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(id),
    submitter_role TEXT NOT NULL, submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    decided_by TEXT REFERENCES users(id), decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    previous_retention_until TEXT,
    UNIQUE(evidence_id,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_retention_review_one_pending
    ON retention_reviews(evidence_id) WHERE status='pending';
"""

# 初版（V0）schema，仅用于测试中构造旧库。
SCHEMA_V0 = """
CREATE TABLE users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE cases(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE case_members(
    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
    PRIMARY KEY(case_id,user_id)
);
CREATE TABLE evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
        CHECK(status IN ('custody','opened','released','derivative')),
    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
    retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(case_id,label)
);
CREATE TABLE custody_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED')),
    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
);
CREATE TABLE derivatives(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
);
CREATE TABLE audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

EVENT_COLUMNS = (
    "id, evidence_id, sequence, event_type, actor_id, from_person, to_person, "
    "location, note, previous_hash, event_hash, created_at"
)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_v0_to_v1(conn):
    """旧库升级：迁移保管事件链，保留原编号、原哈希与原顺序。"""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    # 新增期限复核相关对象。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS retention_reviews(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               evidence_id INTEGER NOT NULL REFERENCES evidence(id),
               review_type TEXT NOT NULL CHECK(review_type IN ('extend','shorten')),
               reason TEXT NOT NULL, current_retention_until TEXT NOT NULL,
               new_retention_until TEXT NOT NULL,
               legal_hold_snapshot INTEGER NOT NULL CHECK(legal_hold_snapshot IN (0,1)),
               status_snapshot TEXT NOT NULL,
               submitted_by TEXT NOT NULL REFERENCES users(id),
               submitter_role TEXT NOT NULL, submitted_at TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
               decided_by TEXT REFERENCES users(id), decided_at TEXT,
               decision_note TEXT NOT NULL DEFAULT '',
               previous_retention_until TEXT,
               UNIQUE(evidence_id,id)
           )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_retention_review_one_pending "
        "ON retention_reviews(evidence_id) WHERE status='pending'"
    )

    # SQLite 无法直接 ALTER CHECK：按官方指南重建 custody_events，逐条搬运旧事件。
    conn.execute("ALTER TABLE custody_events RENAME TO custody_events_old")
    conn.execute(
        """CREATE TABLE custody_events(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
               event_type TEXT NOT NULL CHECK(event_type IN (
                   'INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED',
                   'RETENTION_EXTEND','RETENTION_SHORTEN')),
               actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
               to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
               previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
               created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
           )"""
    )
    old_count = conn.execute("SELECT COUNT(*) FROM custody_events_old").fetchone()[0]
    conn.execute(
        f"INSERT INTO custody_events({EVENT_COLUMNS}) "
        f"SELECT {EVENT_COLUMNS} FROM custody_events_old ORDER BY evidence_id, sequence"
    )
    migrated_count = conn.execute("SELECT COUNT(*) FROM custody_events").fetchone()[0]
    if migrated_count != old_count:
        conn.rollback()
        raise RuntimeError(f"保管事件迁移数量不一致: {old_count} -> {migrated_count}")
    conn.execute("DROP TABLE custody_events_old")
    conn.execute("PRAGMA user_version=1")
    conn.commit()

    # 事务外恢复外键设置（连接关闭后 PRAGMA 即失效；业务连接会再次开启）。
    conn.execute("PRAGMA foreign_keys=ON")
    return old_count


def init_or_migrate(db_path):
    """初始化新库（直接建 V1）或升级旧库（V0 -> V1）。返回迁移事件数（新库为 0）。"""
    path = Path(db_path)
    if not path.exists():
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(SCHEMA_V1)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        return 0

    conn = sqlite3.connect(str(path), timeout=15)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return 0
        if not _table_exists(conn, "users"):
            # 空/未知数据库文件：按全新 V1 建库。
            conn.executescript(SCHEMA_V1)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            conn.commit()
            return 0
        migrated = _migrate_v0_to_v1(conn)
        return migrated
    finally:
        conn.close()
