"""期限规则层：保留期限复核的纯业务规则，不接触数据库与 HTTP。

规则要点：
- 保管员可提交延期（extend）或提前结束（shorten），必须写明原因和新日期；
- 批准时按当前最新状态重新核对：法律保留中不得缩短，延期照常；
- 提交后法律保留、释放或成员角色发生变化时，旧申请不能盖掉新状态；
- 提交人不能自批（自批属于权限问题，由复核服务层判定）。
"""
from __future__ import annotations

from datetime import date

from errors import BusinessError

REVIEW_TYPES = {"extend", "shorten"}
MIN_REASON_LEN = 5


def parse_iso_date(value):
    """校验并归一化 ISO 日期，非法或缺失时抛出 422。"""
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise BusinessError("新保留期限必须是 YYYY-MM-DD 日期", 422, "invalid_retention")


def validate_submission(*, review_type, new_retention_until, reason,
                        evidence_released, legal_hold, current_retention_until, today=None):
    """提交期限复核申请时的规则校验。

    返回 (复核类型, 新日期字符串)。批准阶段的再核对见 validate_decision，
    因此“法律保留变化、角色变化、释放”等情形在提交与批准两处分别校验。
    """
    today = today or date.today()
    if review_type not in REVIEW_TYPES:
        raise BusinessError("复核类型必须是 extend（延期）或 shorten（提前结束）",
                            422, "invalid_review_type")
    new_date = parse_iso_date(new_retention_until)

    if evidence_released:
        raise BusinessError("已释放证据不能申请期限复核", 409, "evidence_released")

    current_date = parse_iso_date(current_retention_until)
    if review_type == "extend" and new_date <= current_date:
        raise BusinessError("延期的新到期日必须晚于当前到期日", 422, "invalid_retention")
    if review_type == "shorten":
        if new_date < today:
            raise BusinessError("新保留期限不能早于今天", 422, "invalid_retention")
        if new_date >= current_date:
            raise BusinessError("提前结束的新到期日必须早于当前到期日", 422, "invalid_retention")
        # 法律保留中不得缩短（提交时即拒绝，批准时还会再核对一次）。
        if legal_hold:
            raise BusinessError("法律保留中不得缩短保留期限", 409, "legal_hold_active")

    if len(reason.strip()) < MIN_REASON_LEN:
        raise BusinessError("变更原因至少 5 字", 422, "reason_required")
    return review_type, new_date.isoformat()


def validate_decision(*, review, evidence_released, legal_hold, submitter_still_custodian,
                      current_retention_until):
    """批准前按最新状态重新核对，确保旧申请不能盖掉新状态。

    只在“批准”分支调用；拒绝不需要通过这些规则。任一项与申请提交时的快照
    不一致即拒绝批准，返回 409，申请本身保留为 pending 供重新提交/拒绝。
    """
    if evidence_released:
        raise BusinessError("证据已释放，旧申请不能盖掉新状态，请拒绝该申请",
                            409, "state_changed_released")
    if not submitter_still_custodian:
        raise BusinessError("提交人已不再是该案件保管员，旧申请不能盖掉新状态，请拒绝该申请",
                            409, "state_changed_role")
    if str(current_retention_until) != review["current_retention_until"]:
        raise BusinessError("当前到期日已被其他申请更新，旧申请已过期，请拒绝后重新提交",
                            409, "state_changed_retention")
    if review["review_type"] == "shorten" and legal_hold:
        # 提交后被设置/重新设置了法律保留：延期照常，缩短禁止。
        raise BusinessError("证据当前处于法律保留中，不得缩短保留期限",
                            409, "legal_hold_active")
    return True
