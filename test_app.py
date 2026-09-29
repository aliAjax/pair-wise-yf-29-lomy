import base64
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore
import schema_migrations


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交")
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")


def _create_case_with_evidence(store, label="E-R1", actor="custodian1", retention_days=400):
    case = store.create_case(actor, f"CASE-R-{label}", "期限复核测试")
    store.add_member(actor, case["id"], "custodian2", "custodian")
    store.add_member(actor, case["id"], "analyst1", "analyst")
    store.add_member(actor, case["id"], "auditor1", "auditor")
    retention = (date.today() + timedelta(days=retention_days)).isoformat()
    item = store.ingest_evidence(
        actor, case["id"], label, "raw.bin",
        base64.b64encode(b"retention").decode(), retention,
    )
    return case, item, retention


class RetentionReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case, self.item, self.retention = _create_case_with_evidence(self.store)
        self.later = (date.today() + timedelta(days=900)).isoformat()
        self.earlier = (date.today() + timedelta(days=60)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, actor="custodian2", rtype="extend", new_date=None, reason="上诉周期延长需要"):
        return self.store.reviews.submit(
            actor, self.item["id"], rtype, new_date or self.later, reason
        )

    def test_only_custodian_submits_and_creator_or_auditor_decides(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("analyst1", self.item["id"], "extend", self.later, "分析员无权发起")
        self.assertEqual(ctx.exception.status, 403)
        review = self._submit()
        self.assertEqual(review["status"], "pending")
        # 提交人不能自批。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("custodian2", review["id"], True, "同意")
        self.assertEqual(ctx.exception.code, "self_approval_forbidden")
        # 非创建人、非审计员不能处理（analyst1 既不是创建人也不是审计员）。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("analyst1", review["id"], True, "同意")
        self.assertEqual(ctx.exception.status, 403)
        # 审计员批准。
        done = self.store.reviews.decide("auditor1", review["id"], True, "材料齐全")
        self.assertEqual(done["status"], "approved")
        detail = self.store.get_evidence("custodian2", self.item["id"])
        self.assertEqual(detail["retention_until"], self.later)
        self.assertEqual(detail["original_retention_until"], self.retention)
        self.assertEqual(detail["events"][-1]["event_type"], "RETENTION_CHANGED")
        # 不能重复处理。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True, "再次同意")
        self.assertEqual(ctx.exception.code, "review_not_pending")

    def test_creator_cannot_approve_own_submission(self):
        # 案件创建人本人提交的申请，也不能由创建人自己批准。
        review = self.store.reviews.submit(
            "custodian1", self.item["id"], "extend",
            (date.today() + timedelta(days=1200)).isoformat(), "创建人自己发起的延期"
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("custodian1", review["id"], True, "自己批准")
        self.assertEqual(ctx.exception.code, "self_approval_forbidden")
        # 审计员仍可处理。
        done = self.store.reviews.decide("auditor1", review["id"], True, "代为批准")
        self.assertEqual(done["status"], "approved")

    def test_case_creator_can_approve(self):
        # 创建人 custodian1 处理 custodian2 的申请。
        review = self._submit(actor="custodian2")
        done = self.store.reviews.decide("custodian1", review["id"], True, "创建人批准")
        self.assertEqual(done["status"], "approved")

    def test_legal_hold_blocks_shortening_but_allows_extension(self):
        # 法律保留中提前结束：提交即被后端规则拒绝。
        self.store.set_hold("auditor1", self.item["id"], True, "长期诉讼法律保留")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian2", self.item["id"], "early_end", self.earlier, "提前结束保管")
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        # 延期照常。
        review = self._submit()
        done = self.store.reviews.decide("auditor1", review["id"], True, "保留期间顺延")
        self.assertEqual(done["status"], "approved")
        self.assertEqual(
            self.store.get_evidence("custodian2", self.item["id"])["retention_until"], self.later
        )

    def test_hold_added_after_submit_requires_recheck_before_approve(self):
        review = self._submit(rtype="early_end", new_date=self.earlier, reason="诉讼提前结束释放")
        # 提交后、批准前加上法律保留：旧申请不能盖掉新状态。
        self.store.set_hold("auditor1", self.item["id"], True, "新增长期诉讼保留")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True, "同意提前结束")
        self.assertEqual(ctx.exception.code, "state_changed_since_submit")
        # 到期日未被旧申请改动。
        self.assertEqual(
            self.store.get_evidence("custodian2", self.item["id"])["retention_until"], self.retention
        )
        # 仍可拒绝（拒绝不要求重新核对），拒绝后可以按新状态重新提交延期。
        rejected = self.store.reviews.decide("auditor1", review["id"], False, "保留已变更，需重新申请")
        self.assertEqual(rejected["status"], "rejected")
        extension = self.store.reviews.submit(
            "custodian2", self.item["id"], "extend", self.later, "保留中申请延长"
        )
        done = self.store.reviews.decide("auditor1", extension["id"], True, "同意顺延")
        self.assertEqual(done["status"], "approved")

    def test_release_after_submit_requires_recheck(self):
        review = self._submit()
        self.store.set_hold("auditor1", self.item["id"], False, "解除保留以释放")
        self.store.release("custodian2", self.item["id"], "检察机关", "依法释放")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True, "同意延期")
        self.assertEqual(ctx.exception.code, "state_changed_since_submit")

    def test_role_change_after_submit_requires_recheck(self):
        review = self._submit(actor="custodian2")
        # 创建人把提交人角色从 custodian 改为 analyst。
        self.store.add_member("custodian1", self.case["id"], "custodian2", "analyst")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True, "同意延期")
        self.assertEqual(ctx.exception.code, "state_changed_since_submit")

    def test_reject_requires_reason_visible_in_report(self):
        review = self._submit(new_date=self.later, reason="申请材料尚待补充")
        # 拒绝必须填原因。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], False, "")
        self.assertEqual(ctx.exception.code, "decision_note_required")
        self.store.reviews.decide("auditor1", review["id"], False, "申请材料不足，暂不同意延期")
        report = self.store.report("auditor1", self.case["id"])
        item = report["evidence"][0]
        rejected = item["retention_reviews"][0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["decision_note"], "申请材料不足，暂不同意延期")
        self.assertEqual(rejected["decided_by"], "auditor1")
        # 拒绝不改日期，报告里原日期仍在。
        self.assertEqual(item["retention_until"], self.retention)
        self.assertEqual(item["original_retention_until"], self.retention)
        self.assertTrue(report["overall_integrity_valid"])

    def test_only_one_pending_review_per_evidence(self):
        self._submit()
        with self.assertRaises(BusinessError) as ctx:
            self._submit()
        self.assertEqual(ctx.exception.code, "review_pending")

    def test_chain_still_validates_after_approval(self):
        review = self._submit()
        self.store.reviews.decide("auditor1", review["id"], True, "同意")
        report = self.store.report("custodian1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertTrue(report["evidence"][0]["chain_valid"])


# 与初版 app.py 中完全一致的旧结构（user_version=0 的已使用旧库）。
LEGACY_SCHEMA = """
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


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_database_keeps_old_evidence_and_chain_then_supports_reviews(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "legacy.db"

        # 1) 用旧结构 + 旧事件（不含 RETENTION_CHANGED）造一个在途旧库。
        legacy = sqlite3.connect(db_path)
        legacy.executescript(LEGACY_SCHEMA)
        legacy.execute("INSERT INTO users(id,name) VALUES('custodian1','保管员甲')")
        legacy.execute("INSERT INTO users(id,name) VALUES('auditor1','审计员')")
        legacy.execute(
            "INSERT INTO cases(case_number,title,created_by,created_at) VALUES('OLD-1','旧案','custodian1','2025-01-01T00:00:00+00:00')"
        )
        legacy.execute(
            "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(1,'custodian1','custodian','custodian1','2025-01-01T00:00:00+00:00')"
        )
        legacy.execute(
            "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(1,'auditor1','auditor','custodian1','2025-01-01T00:00:00+00:00')"
        )
        raw = b"legacy evidence bytes"
        digest = __import__("hashlib").sha256(raw).hexdigest()
        legacy.execute(
            "INSERT INTO evidence(case_id,label,filename,sha256,size,content,current_custodian,retention_until,created_by,created_at)"
            " VALUES(1,'OLD-E1','a.bin',?,20,?,'custodian1','2026-01-01','custodian1','2025-01-02T00:00:00+00:00')",
            (digest, raw),
        )
        # 用与后端一致的算法算出真实事件哈希，验证迁移后旧事件链仍可逐环校验。
        from app import CustodyStore as _CS
        event_payload = {
            "evidence_id": 1, "sequence": 1, "event_type": "INGEST",
            "actor_id": "custodian1", "from_person": None, "to_person": "custodian1",
            "location": "旧库", "note": "入册", "previous_hash": "GENESIS",
            "created_at": "2025-01-02T00:00:00+00:00",
        }
        chain_hash = _CS._event_hash(event_payload)
        legacy.execute(
            "INSERT INTO custody_events(evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)"
            " VALUES(1,1,'INGEST','custodian1',NULL,'custodian1','旧库','入册','GENESIS',?,'2025-01-02T00:00:00+00:00')",
            (chain_hash,),
        )
        legacy.commit()
        legacy.close()

        # 2) 升级：事件表重建但旧哈希保留，原保留日期回填。
        version = schema_migrations.migrate(db_path)
        self.assertEqual(version, schema_migrations.SCHEMA_VERSION)

        store = CustodyStore(db_path)
        detail = store.get_evidence("auditor1", 1)
        self.assertEqual(detail["sha256"], digest)
        self.assertTrue(detail["integrity_valid"])
        self.assertEqual(detail["events"][0]["event_hash"], chain_hash)
        self.assertEqual(detail["original_retention_until"], "2026-01-01")

        # 3) 升级后继续可查、报告可校验，且新事件类型可写入。
        later = (date.today() + timedelta(days=500)).isoformat()
        review = store.reviews.submit("custodian1", 1, "extend", later, "旧库升级后申请延期")
        done = store.reviews.decide("auditor1", review["id"], True, "同意")
        self.assertEqual(done["status"], "approved")
        report = store.report("auditor1", 1)
        self.assertTrue(report["overall_integrity_valid"])
        events = report["evidence"][0]["events"]
        self.assertEqual([e["event_type"] for e in events], ["INGEST", "RETENTION_CHANGED"])
        self.assertEqual(events[0]["event_hash"], chain_hash)

    def test_migrate_is_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "fresh.db"
        self.assertEqual(schema_migrations.migrate(db_path), schema_migrations.SCHEMA_VERSION)
        self.assertEqual(schema_migrations.migrate(db_path), schema_migrations.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
