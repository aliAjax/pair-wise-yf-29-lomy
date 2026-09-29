import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore, now
from migrations import SCHEMA_V0


def b64(raw):
    return base64.b64encode(raw).decode()


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.today = date.today()
        self.retention = (self.today + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, label="E-001", custodian="custodian1"):
        return self.store.ingest_evidence(
            custodian, self.case["id"], label, "raw.bin", b64(b"evidence bytes"),
            self.retention, custodian,
        )

    # ---- 既有主流程 ----------------------------------------------------
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

    # ---- 期限复核：正常延期 --------------------------------------------
    def test_retention_extend_submit_approve_updates_date_and_chain(self):
        item = self.ingest()
        new_date = (self.today + timedelta(days=3650 + 365)).isoformat()
        review = self.store.reviews.submit(
            "custodian2", item["id"], "extend", new_date, "长期诉讼，申请继续保管一年",
        )
        self.assertEqual(review["status"], "pending")
        self.assertEqual(review["submitted_by"], "custodian2")
        self.assertEqual(review["current_retention_until"], self.retention)
        self.assertFalse(review["legal_hold_snapshot"])

        decided = self.store.reviews.decide("auditor1", review["id"], True, "同意延期")
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["previous_retention_until"], self.retention)
        self.assertEqual(decided["new_retention_until"], new_date)

        evidence = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(evidence["retention_until"], new_date)
        last_event = evidence["events"][-1]
        self.assertEqual(last_event["event_type"], "RETENTION_EXTEND")
        self.assertIn(self.retention, last_event["note"])
        self.assertIn(new_date, last_event["note"])
        # 哈希链在追加事件后仍然完整。
        report = self.store.report("auditor1", self.case["id"])
        entry = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertTrue(entry["chain_valid"])
        self.assertTrue(entry["hash_valid"])
        # 报告保留原日期、申请和处理结果。
        record = entry["retention_reviews"][0]
        self.assertEqual(record["current_retention_until"], self.retention)
        self.assertEqual(record["previous_retention_until"], self.retention)
        self.assertEqual(record["new_retention_until"], new_date)
        self.assertEqual(record["decided_by"], "auditor1")
        self.assertEqual(record["decision_note"], "同意延期")

    # ---- 期限复核：提前结束与拒绝原因 ----------------------------------
    def test_retention_shorten_requires_reason_and_records_rejection(self):
        item = self.ingest()
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian1", item["id"], "shorten",
                                      (self.today + timedelta(days=10)).isoformat(), "短")
        self.assertEqual(ctx.exception.code, "reason_required")

        review = self.store.reviews.submit(
            "custodian2", item["id"], "shorten",
            (self.today + timedelta(days=10)).isoformat(), "案件提前审结，申请缩短",
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], False)
        self.assertEqual(ctx.exception.code, "rejection_reason_required")

        decided = self.store.reviews.decide("auditor1", review["id"], False, "仍在复核窗口期，暂不提前结束")
        self.assertEqual(decided["status"], "rejected")
        self.assertEqual(decided["decision_note"], "仍在复核窗口期，暂不提前结束")
        # 拒绝不改变到期日、不追加保管事件。
        evidence = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(evidence["retention_until"], self.retention)
        self.assertNotIn("RETENTION_SHORTEN", [e["event_type"] for e in evidence["events"]])
        # 拒绝后可重新提交。
        again = self.store.reviews.submit(
            "custodian2", item["id"], "shorten",
            (self.today + timedelta(days=12)).isoformat(), "再次申请缩短期限",
        )
        self.assertEqual(again["status"], "pending")

    # ---- 提交人不能自批 ------------------------------------------------
    def test_submitter_cannot_self_approve(self):
        item = self.ingest()
        review = self.store.reviews.submit(
            "custodian1", item["id"], "extend",
            (self.today + timedelta(days=4000)).isoformat(), "长期诉讼延长保管",
        )
        # custodian1 既是案件创建人又是提交人：不能自批。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("custodian1", review["id"], True)
        self.assertEqual(ctx.exception.code, "self_approval_forbidden")
        # 另一名保管员也无权审批。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("custodian2", review["id"], True)
        self.assertEqual(ctx.exception.status, 403)
        # 审计员可以批准。
        decided = self.store.reviews.decide("auditor1", review["id"], True)
        self.assertEqual(decided["status"], "approved")
        # 已处理申请不能重复处理。
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True)
        self.assertEqual(ctx.exception.code, "review_decided")

    # ---- 非保管员不能提交；在途申请互斥 -------------------------------
    def test_only_custodian_submits_and_one_pending_at_a_time(self):
        item = self.ingest()
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("analyst1", item["id"], "extend",
                                      (self.today + timedelta(days=4000)).isoformat(), "分析员无权提交")
        self.assertEqual(ctx.exception.status, 403)
        self.store.reviews.submit("custodian2", item["id"], "extend",
                                  (self.today + timedelta(days=4000)).isoformat(), "存在在途复核申请")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian1", item["id"], "extend",
                                      (self.today + timedelta(days=4100)).isoformat(), "不得并行提交第二份")
        self.assertEqual(ctx.exception.code, "review_pending")

    # ---- 法律保留：提交时禁止缩短，批准前再核对，延期照常 --------------
    def test_legal_hold_blocks_shorten_but_allows_extend(self):
        item = self.ingest()
        # 已有法律保留时，缩短申请在提交阶段就被拒绝。
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian2", item["id"], "shorten",
                                      (self.today + timedelta(days=10)).isoformat(), "保留中想缩短")
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        # 法律保留中延期照常。
        ext = self.store.reviews.submit(
            "custodian2", item["id"], "extend",
            (self.today + timedelta(days=4000)).isoformat(), "保留中继续保管",
        )
        decided = self.store.reviews.decide("auditor1", ext["id"], True)
        self.assertEqual(decided["status"], "approved")
        self.store.set_hold("auditor1", item["id"], False, "诉讼保全解除")

        # 提交时无保留、批准前才被设置保留：旧申请不能盖掉新状态。
        review = self.store.reviews.submit(
            "custodian2", item["id"], "shorten",
            (self.today + timedelta(days=20)).isoformat(), "解除后申请缩短",
        )
        self.store.set_hold("auditor1", item["id"], True, "二审再次保全")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", review["id"], True)
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        # 申请仍为 pending，可在核对失败后拒绝。
        rejected = self.store.reviews.decide("auditor1", review["id"], False, "已重新设置法律保留")
        self.assertEqual(rejected["status"], "rejected")

    # ---- 批准前再核对：释放、角色、到期日变化 --------------------------
    def test_decision_rechecks_release_role_and_retention_changes(self):
        item = self.ingest()
        later = (self.today + timedelta(days=30)).isoformat()

        # 提交后证据被释放：批准被拒（旧申请不能盖掉新状态）。
        r1 = self.store.reviews.submit("custodian2", item["id"], "shorten", later, "提交后释放")
        self.store.release("custodian2", item["id"], "检察机关", "依法释放")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", r1["id"], True)
        self.assertEqual(ctx.exception.code, "state_changed_released")

        # 新证据：提交人角色在批准前被改变，批准被拒。
        item2 = self.ingest("E-002")
        r2 = self.store.reviews.submit("custodian2", item2["id"], "extend",
                                       (self.today + timedelta(days=4000)).isoformat(), "提交人角色将变化")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "analyst")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", r2["id"], True)
        self.assertEqual(ctx.exception.code, "state_changed_role")

        # 新证据：到期日已被另一申请更新，旧申请过期。
        item3 = self.ingest("E-003")
        first = self.store.reviews.submit("custodian1", item3["id"], "extend",
                                          (self.today + timedelta(days=3900)).isoformat(), "先批的申请")
        # 拒掉第一个，再让第二个申请在 first 仍 pending 时无法提交；改为直接制造日期漂移：
        self.store.reviews.decide("auditor1", first["id"], True)
        second = self.store.reviews.submit("custodian1", item3["id"], "extend",
                                           (self.today + timedelta(days=4200)).isoformat(), "后提交的申请")
        # 手工把证据到期日改回旧值模拟并发漂移（仅测试规则层的再核对）。
        with self.store.connect() as conn:
            conn.execute("UPDATE evidence SET retention_until=? WHERE id=?", (self.retention, item3["id"]))
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.decide("auditor1", second["id"], True)
        self.assertEqual(ctx.exception.code, "state_changed_retention")

    # ---- 日期方向与格式规则 --------------------------------------------
    def test_review_type_and_date_direction_rules(self):
        item = self.ingest()
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian1", item["id"], "extend", self.retention, "延期但日期没有变化")
        self.assertEqual(ctx.exception.code, "invalid_retention")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian1", item["id"], "shorten",
                                      (self.today - timedelta(days=1)).isoformat(), "新日期早于今天不行")
        self.assertEqual(ctx.exception.code, "invalid_retention")
        with self.assertRaises(BusinessError) as ctx:
            self.store.reviews.submit("custodian1", item["id"], "freeze",
                                      self.retention, "非法的复核类型")
        self.assertEqual(ctx.exception.code, "invalid_review_type")

    # ---- 旧库升级：旧证据继续可查，事件链不被割开 ----------------------
    def test_legacy_database_migration_preserves_evidence_and_chain(self):
        db_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_V0)
        conn.execute("INSERT INTO users(id,name) VALUES('custodian1','保管员甲')")
        conn.execute("INSERT INTO users(id,name) VALUES('auditor1','审计员')")
        ts = now()
        conn.execute(
            "INSERT INTO cases(id,case_number,title,created_by,created_at) VALUES(1,'OLD-1','旧库案件','custodian1',?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(1,'custodian1','custodian','custodian1',?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(1,'auditor1','auditor','custodian1',?)",
            (ts,),
        )
        raw = b"legacy evidence"
        digest = hashlib.sha256(raw).hexdigest()
        conn.execute(
            """INSERT INTO evidence(id,case_id,label,filename,sha256,size,content,status,
                   current_custodian,legal_hold,retention_until,created_by,created_at)
               VALUES(1,1,'OLD-E','old.bin',?,15,?,'custody','custodian1',0,?,'custodian1',?)""",
            (digest, raw, self.retention, ts),
        )
        payload = {
            "evidence_id": 1, "sequence": 1, "event_type": "INGEST", "actor_id": "custodian1",
            "from_person": None, "to_person": "custodian1", "location": "",
            "note": f"入册 SHA-256 {digest}", "previous_hash": "GENESIS", "created_at": ts,
        }
        event_hash = CustodyStore._event_hash(payload)
        conn.execute(
            """INSERT INTO custody_events(id,evidence_id,sequence,event_type,actor_id,from_person,
                   to_person,location,note,previous_hash,event_hash,created_at)
               VALUES(1,1,1,'INGEST','custodian1',NULL,'custodian1','',?, 'GENESIS',?,?)""",
            (payload["note"], event_hash, ts),
        )
        conn.commit()
        conn.close()

        store = CustodyStore(db_path)
        migrated = store.init_schema()
        self.assertEqual(migrated, 1)  # 迁移了 1 条旧事件
        # 重复初始化不重复迁移。
        self.assertEqual(store.init_schema(), 0)

        # 旧证据继续可查，原编号、原哈希链完整。
        evidence = store.get_evidence("auditor1", 1)
        self.assertEqual(evidence["label"], "OLD-E")
        self.assertTrue(evidence["integrity_valid"])
        self.assertEqual(evidence["events"][0]["event_hash"], event_hash)

        # 升级后期限复核可正常使用，追加新事件不割开旧链。
        new_date = (self.today + timedelta(days=4000)).isoformat()
        review = store.reviews.submit("custodian1", 1, "extend", new_date, "旧库升级后申请延期")
        store.reviews.decide("auditor1", review["id"], True, "升级迁移验证通过")
        report = store.report("auditor1", 1)
        self.assertTrue(report["overall_integrity_valid"])
        events = report["evidence"][0]["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events[0]["event_hash"], event_hash)
        self.assertEqual(events[1]["event_type"], "RETENTION_EXTEND")


if __name__ == "__main__":
    unittest.main()
