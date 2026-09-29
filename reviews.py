"""期限复核服务层：保管员提交延期/提前结束，案件创建人或审计员处理。

- 提交人不能自批；
- 批准前按最新状态重新核对法律保留、释放、成员角色和到期日，旧申请不能盖掉新状态；
- 批准后更新到期日并追加不可变保管事件链事件；
- 申请、处理结果（含拒绝原因）永久保留，供报告导出。
"""
from __future__ import annotations

from datetime import datetime, timezone

from errors import BusinessError
from retention import validate_decision, validate_submission


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serialize(row):
    if row is None:
        return None
    data = dict(row)
    data["legal_hold_snapshot"] = bool(data["legal_hold_snapshot"])
    return data


class ReviewService:
    def __init__(self, store):
        self.store = store  # CustodyStore：复用其连接、成员鉴权与事件/审计写入

    # ---- 查询 ----------------------------------------------------------
    def list_for_evidence(self, user_id, evidence_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not row:
                raise BusinessError("证据不存在", 404, "not_found")
            self.store._member(conn, row["case_id"], user_id)
            rows = conn.execute(
                "SELECT * FROM retention_reviews WHERE evidence_id=? ORDER BY id", (evidence_id,)
            ).fetchall()
            return [_serialize(r) for r in rows]

    def list_for_case(self, user_id, case_id):
        with self.store.connect() as conn:
            self.store._member(conn, case_id, user_id)
            rows = conn.execute(
                """SELECT r.*, e.label AS evidence_label
                   FROM retention_reviews r JOIN evidence e ON e.id = r.evidence_id
                   WHERE e.case_id=? ORDER BY r.id""",
                (case_id,),
            ).fetchall()
            return [_serialize(r) for r in rows]

    # ---- 提交 ----------------------------------------------------------
    def submit(self, user_id, evidence_id, review_type, new_retention_until, reason):
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if not row:
                    raise BusinessError("证据不存在", 404, "not_found")
                _, member = self.store._member(conn, row["case_id"], user_id, {"custodian"})

                _, new_date = validate_submission(
                    review_type=review_type,
                    new_retention_until=new_retention_until,
                    reason=reason,
                    evidence_released=row["status"] == "released",
                    legal_hold=bool(row["legal_hold"]),
                    current_retention_until=row["retention_until"],
                )

                pending = conn.execute(
                    "SELECT 1 FROM retention_reviews WHERE evidence_id=? AND status='pending'",
                    (evidence_id,),
                ).fetchone()
                if pending:
                    raise BusinessError("该证据已有在途期限复核，请先处理", 409, "review_pending")

                cur = conn.execute(
                    """INSERT INTO retention_reviews(
                           evidence_id, review_type, reason, current_retention_until,
                           new_retention_until, legal_hold_snapshot, status_snapshot,
                           submitted_by, submitter_role, submitted_at, status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'pending')""",
                    (evidence_id, review_type, reason.strip(), row["retention_until"], new_date,
                     int(bool(row["legal_hold"])), row["status"], user_id, member["role"], _now()),
                )
                review_id = cur.lastrowid
                self.store._audit(conn, row["case_id"], user_id, "retention.review.submit", {
                    "review_id": review_id, "evidence_id": evidence_id,
                    "review_type": review_type, "new_retention_until": new_date,
                    "reason": reason.strip(),
                })
                conn.commit()
                return self.get(user_id, review_id, conn=conn)
            except Exception:
                conn.rollback()
                raise

    # ---- 处理（批准/拒绝） --------------------------------------------
    def _reviewer(self, conn, case_row, user_id, review):
        """案件创建人或审计员可处理；提交人不能自批。"""
        if user_id != case_row["created_by"]:
            self.store._member(conn, case_row["id"], user_id, {"auditor"})
        if user_id == review["submitted_by"]:
            raise BusinessError("提交人不能自批期限复核申请", 403, "self_approval_forbidden")

    def decide(self, user_id, review_id, approve, decision_note=""):
        approve = bool(approve)
        note = (decision_note or "").strip()
        if not approve and not note:
            raise BusinessError("拒绝时必须填写拒绝原因", 422, "rejection_reason_required")

        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                review = conn.execute(
                    "SELECT * FROM retention_reviews WHERE id=?", (review_id,)
                ).fetchone()
                if not review:
                    raise BusinessError("期限复核申请不存在", 404, "not_found")
                if review["status"] != "pending":
                    raise BusinessError("该申请已处理，不能重复处理", 409, "review_decided")

                evidence = conn.execute(
                    "SELECT * FROM evidence WHERE id=?", (review["evidence_id"],)
                ).fetchone()
                if not evidence:
                    raise BusinessError("证据不存在", 404, "not_found")
                case_row = conn.execute(
                    "SELECT * FROM cases WHERE id=?", (evidence["case_id"],)
                ).fetchone()
                self._reviewer(conn, case_row, user_id, review)

                if approve:
                    # 批准前重新核对最新状态：法律保留、释放、成员角色、到期日。
                    submitter_member = conn.execute(
                        "SELECT 1 FROM case_members WHERE case_id=? AND user_id=? AND active=1 AND role='custodian'",
                        (case_row["id"], review["submitted_by"]),
                    ).fetchone()
                    validate_decision(
                        review=review,
                        evidence_released=evidence["status"] == "released",
                        legal_hold=bool(evidence["legal_hold"]),
                        submitter_still_custodian=submitter_member is not None,
                        current_retention_until=evidence["retention_until"],
                    )
                    old_date, new_date = evidence["retention_until"], review["new_retention_until"]
                    conn.execute(
                        "UPDATE evidence SET retention_until=? WHERE id=?",
                        (new_date, evidence["id"]),
                    )
                    event_type = "RETENTION_EXTEND" if review["review_type"] == "extend" else "RETENTION_SHORTEN"
                    type_label = "延期" if review["review_type"] == "extend" else "提前结束"
                    event_note = (
                        f"保留期限{type_label}：{old_date} -> {new_date}；"
                        f"申请 #{review_id}，提交人 {review['submitted_by']}，"
                        f"原因：{review['reason']}"
                    )
                    if note:
                        event_note += f"；批准备注：{note}"
                    self.store._append_event(
                        conn, evidence["id"], event_type, user_id, note=event_note
                    )
                    conn.execute(
                        """UPDATE retention_reviews SET status='approved', decided_by=?, decided_at=?,
                               decision_note=?, previous_retention_until=? WHERE id=?""",
                        (user_id, _now(), note, old_date, review_id),
                    )
                    self.store._audit(conn, case_row["id"], user_id, "retention.review.approve", {
                        "review_id": review_id, "evidence_id": evidence["id"],
                        "review_type": review["review_type"],
                        "previous_retention_until": old_date,
                        "new_retention_until": new_date,
                    })
                else:
                    conn.execute(
                        "UPDATE retention_reviews SET status='rejected', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                        (user_id, _now(), note, review_id),
                    )
                    self.store._audit(conn, case_row["id"], user_id, "retention.review.reject", {
                        "review_id": review_id, "evidence_id": evidence["id"],
                        "reason": note,
                    })
                conn.commit()
                return self.get(user_id, review_id, conn=conn)
            except Exception:
                conn.rollback()
                raise

    def get(self, user_id, review_id, conn=None):
        def _load(connection):
            review = connection.execute(
                "SELECT * FROM retention_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if not review:
                raise BusinessError("期限复核申请不存在", 404, "not_found")
            evidence = connection.execute(
                "SELECT case_id FROM evidence WHERE id=?", (review["evidence_id"],)
            ).fetchone()
            self.store._member(connection, evidence["case_id"], user_id)
            return _serialize(review)

        if conn is not None:
            return _load(conn)
        with self.store.connect() as connection:
            return _load(connection)
