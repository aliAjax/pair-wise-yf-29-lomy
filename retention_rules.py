"""保留期限规则（纯函数，不触碰数据库）。

与复核服务分开维护：这里只负责判断一次期限变更在规则上是否成立，
例如延期 / 提前结束的新日期、法律保留中不得缩短等。
状态是否已经变化（旧申请盖新状态）由复核服务在处理时再次核对。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from errors import BusinessError

EXTEND = "extend"
EARLY_END = "early_end"
REQUEST_TYPES = (EXTEND, EARLY_END)
MIN_REASON_LEN = 5


@dataclass(frozen=True)
class TermChange:
    request_type: str
    current_until: str
    requested_until: str
    legal_hold: bool
    as_of: date | None = None

    @property
    def deadline(self) -> date:
        return date.fromisoformat(self.requested_until)

    @property
    def current(self) -> date:
        return date.fromisoformat(self.current_until)


def parse_request(raw_type, raw_date, today: date | None = None) -> tuple[str, date]:
    """解析并基础校验请求类型和新日期。"""
    request_type = (raw_type or "").strip()
    if request_type not in REQUEST_TYPES:
        raise BusinessError("复核类型必须是 extend（延期）或 early_end（提前结束）", 422, "invalid_request_type")
    try:
        new_date = date.fromisoformat((raw_date or "").strip())
    except (ValueError, AttributeError):
        raise BusinessError("新保留期限必须是 YYYY-MM-DD 日期", 422, "invalid_retention")
    if new_date < (today or date.today()):
        raise BusinessError("新保留期限不能早于今天", 422, "invalid_retention")
    return request_type, new_date


def validate_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LEN:
        raise BusinessError(f"变更原因至少 {MIN_REASON_LEN} 字", 422, "reason_required")
    return reason


def validate_change(change: TermChange, today: date | None = None) -> None:
    """按后端规则校验一次期限变更，任何一条不满足都抛 BusinessError。"""
    today = today or change.as_of or date.today()
    if change.deadline < today:
        raise BusinessError("新保留期限不能早于今天", 422, "invalid_retention")
    if change.request_type == EXTEND:
        if change.deadline <= change.current:
            raise BusinessError("延期的新到期日必须晚于当前到期日", 422, "not_extension")
        # 延期照常：即使处于法律保留中也允许。
    else:  # EARLY_END
        if change.deadline >= change.current:
            raise BusinessError("提前结束的新到期日必须早于当前到期日", 422, "not_early_end")
        if change.legal_hold:
            raise BusinessError("法律保留中不得缩短保留期限", 409, "legal_hold_active")


def classify(current_until: str, new_until: str) -> str:
    """按日期差推断延期 / 提前结束（供事件描述使用）。"""
    return EXTEND if date.fromisoformat(new_until) > date.fromisoformat(current_until) else EARLY_END
