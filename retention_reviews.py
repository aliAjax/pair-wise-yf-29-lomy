"""保留期限复核服务。

流程：保管员提交延期 / 提前结束申请（原因 + 新日期）→
案件创建人或审计员处理 → 通过则更新到期日并追加 RETENTION_CHANGED 事件。

规则要点：
- 提交人不能自批；只有案件创建人或审计员能处理。
- 提交后到处理前，法律保留、释放状态或提交人角色一旦变化，批准前必须
  重新按当前状态核对：旧申请不能盖掉新状态（只能拒绝或重新提交）。
- 法律保留中不得缩短，延期照常（规则细节在 retention_rules）。
"""
from __future__ import annotations

from typing import Any

import retention_rules as rules
from errors import BusinessError, now


class RetentionReviewService:
    def __init__(self, store):
        self.store = store

    # ---- 读取 ----------------------------------------------------------------

    def _serialize(self, row) -> dict[str, Any]:
        data = dict(row)
        data["legal_hold_at_submit"] = bool(data["legal_hold_at_submit"])
        return data

    def rows_for_evidence(self, conn, evidence_id) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM retention_reviews WHERE evidence_id=? ORDER BY id", (evidence_id,)
        ).fetchall()
        return [self._serialize(r) for r in rows]

    def get(self, actor_id, review_id):
        with self.store.connect() as conn:
            review = self._review(conn, review_id)
            evidence = self.store._evidence(conn, review["evidence_id"])
            self.store._member(conn, evidence["case_id"], actor_id)
            return self._serialize(review)

    def list_for_case(self, actor_id, case_id, status=None):
        status = (status or "").strip() or None
        if status and status not in ("pending", "approved", "rejected"):
            raise BusinessError("状态过滤必须是 pending、approved 或 rejected", 422, "invalid_status")
        with self.store.connect() as conn:
            self.store._member(conn, case_id, actor_id)
            sql = (
                "SELECT r.* FROM retention_reviews r JOIN evidence e ON e.id=r.evidence_id "
                "WHERE e.case_id=? " + ("AND r.status=? " if status else "") + "ORDER BY r.id"
            )
            params = (case_id, status) if status else (case_id,)
            return [self._serialize(r) for r in conn.execute(sql, params).fetchall()]

    def pending_for_evidence(self, conn, evidence_id):
        return conn.execute(
            "SELECT * FROM retention_reviews WHERE evidence_id=? AND status='pending'", (evidence_id,)
        ).fetchone()

    def _review(self, conn, review_id):
        row = conn.execute("SELECT * FROM retention_reviews WHERE id=?", (review_id,)).fetchone()
        if not row:
            raise BusinessError("期限复核申请不存在", 404, "not_found")
        return row

    # ---- 提交 ----------------------------------------------------------------

    def submit(self, actor_id, evidence_id, request_type, new_retention_until, reason):
        request_type, new_date = rules.parse_request(request_type, new_retention_until)
        reason = rules.validate_reason(reason)
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                evidence = self.store._evidence(conn, evidence_id)
                self.store._member(conn, evidence["case_id"], actor_id, {"custodian"})
                if evidence["status"] == "released":
                    raise BusinessError("已释放证据不能申请保留期限复核", 409, "evidence_released")
                if self.pending_for_evidence(conn, evidence_id):
                    raise BusinessError("该证据已有待处理的期限复核申请", 409, "review_pending")
                change = rules.TermChange(
                    request_type=request_type,
                    current_until=evidence["retention_until"],
                    requested_until=new_date.isoformat(),
                    legal_hold=bool(evidence["legal_hold"]),
                )
                rules.validate_change(change)
                cur = conn.execute(
                    """INSERT INTO retention_reviews(
                           evidence_id,request_type,current_retention_until,requested_retention_until,
                           reason,submitted_by,submitted_at,status,
                           legal_hold_at_submit,evidence_status_at_submit,submitter_role_at_submit)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id, request_type, evidence["retention_until"], new_date.isoformat(),
                        reason, actor_id, now(), "pending",
                        int(evidence["legal_hold"]), evidence["status"],
                        self._role_of(conn, evidence["case_id"], actor_id),
                    ),
                )
                review_id = cur.lastrowid
                self.store._audit(
                    conn, evidence["case_id"], actor_id, "retention.submit",
                    {"review_id": review_id, "evidence_id": evidence_id, "request_type": request_type,
                     "requested_retention_until": new_date.isoformat(), "reason": reason},
                )
                return self._serialize(conn.execute(
                    "SELECT * FROM retention_reviews WHERE id=?", (review_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _role_of(conn, case_id, user_id):
        row = conn.execute(
            "SELECT role FROM case_members WHERE case_id=? AND user_id=? AND active=1", (case_id, user_id)
        ).fetchone()
        return row["role"] if row else "none"

    # ---- 处理 ----------------------------------------------------------------

    def _authorize_decider(self, conn, case_row, actor_id):
        """案件创建人或审计员处理。"""
        if actor_id == case_row["created_by"]:
            return
        self.store._member(conn, case_row["id"], actor_id, {"auditor"})

    def decide(self, actor_id, review_id, approve, decision_note=""):
        approve = bool(approve)
        decision_note = (decision_note or "").strip()
        if not approve and not decision_note:
            raise BusinessError("拒绝时必须填写原因", 422, "decision_note_required")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                review = self._review(conn, review_id)
                evidence = self.store._evidence(conn, review["evidence_id"])
                case = self.store._case(conn, evidence["case_id"])
                # 提交人不能自批（即使其同时是案件创建人或审计员），最先核对。
                if review["submitted_by"] == actor_id:
                    raise BusinessError("提交人不能自批", 403, "self_approval_forbidden")
                # 案件创建人或审计员才有权处理。
                self._authorize_decider(conn, case, actor_id)
                if review["status"] != "pending":
                    raise BusinessError("该复核申请已处理，不能重复处理", 409, "review_not_pending")

                if not approve:
                    self._finish(conn, review, evidence, actor_id, "rejected", decision_note)
                    return self._serialize(conn.execute(
                        "SELECT * FROM retention_reviews WHERE id=?", (review_id,)
                    ).fetchone())

                # 批准前：按当前最新状态重新核对，旧申请不能盖掉新状态。
                self._reverify_before_approve(conn, review, evidence)
                new_until = review["requested_retention_until"]
                change = rules.TermChange(
                    request_type=review["request_type"],
                    current_until=evidence["retention_until"],
                    requested_until=new_until,
                    legal_hold=bool(evidence["legal_hold"]),
                )
                rules.validate_change(change)

                conn.execute(
                    "UPDATE evidence SET retention_until=? WHERE id=?", (new_until, evidence["id"])
                )
                type_text = "延期" if review["request_type"] == rules.EXTEND else "提前结束"
                self.store._append_event(
                    conn, evidence["id"], "RETENTION_CHANGED", actor_id,
                    note=(
                        f"保留期限复核 #{review_id} 批准{type_text}："
                        f"{review['current_retention_until']} → {new_until}；"
                        f"申请原因：{review['reason']}；处理说明：{decision_note or '无'}"
                    ),
                )
                self._finish(conn, review, evidence, actor_id, "approved", decision_note)
                return self._serialize(conn.execute(
                    "SELECT * FROM retention_reviews WHERE id=?", (review_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise

    def _finish(self, conn, review, evidence, actor_id, status, decision_note):
        conn.execute(
            "UPDATE retention_reviews SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?",
            (status, actor_id, now(), decision_note, review["id"]),
        )
        self.store._audit(
            conn, evidence["case_id"], actor_id, "retention.decide",
            {"review_id": review["id"], "evidence_id": evidence["id"], "result": status,
             "note": decision_note},
        )

    def _reverify_before_approve(self, conn, review, evidence):
        """提交后状态变化检查：变化即拒绝批准，要求重新核对后重新提交。"""
        changed = []
        if evidence["status"] == "released":
            changed.append("证据已释放")
        elif evidence["status"] != review["evidence_status_at_submit"]:
            changed.append(f"证据状态由 {review['evidence_status_at_submit']} 变为 {evidence['status']}")
        if int(evidence["legal_hold"]) != int(review["legal_hold_at_submit"]):
            changed.append("法律保留状态已变化")
        current_role = self._role_of(conn, evidence["case_id"], review["submitted_by"])
        if current_role != review["submitter_role_at_submit"] or current_role != "custodian":
            changed.append("提交人的保管员角色已变化")
        # 到期日若已被其他途径改动，原申请里的基准日期也已失效。
        if evidence["retention_until"] != review["current_retention_until"]:
            changed.append("当前到期日已与申请时不同")
        if changed:
            raise BusinessError(
                "提交后状态发生变化（" + "、".join(changed) + "），批准前请重新核对，旧申请不能盖掉新状态",
                409,
                "state_changed_since_submit",
            )
