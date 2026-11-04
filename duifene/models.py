"""领域模型：课程、签到活动、处理结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

# 服务端可能下发的时间格式：ISO（App 接口）、斜杠与短横线两种老格式
_TIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M")


def parse_time(value: str) -> datetime | None:
    """尽量解析服务端时间；无法解析返回 None（调用方按「未知」处理，不拦签到）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Course:
    """一门课程（含其教学班标识）。"""

    course_id: str
    class_id: str
    name: str


@dataclass(frozen=True)
class CheckInActivity:
    """当前进行中的签到。构造后不可变，便于跨轮次安全传递。"""

    id: str
    type: str
    code: str
    qr_token: str
    room_id: str
    class_id: str
    create_date: str
    valid_date: str
    check_in_status: str
    status_id: str
    status_name: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CheckInActivity | None":
        """从 ``GetModelByInProgress`` / ``GetCheckIn`` 的 ``data`` 构造。

        接口把签到主体包在 ``checkIn`` 里，二维码令牌在 ``qrCode`` 上；
        少数响应直接平铺，这里两种形态都兼容。没有进行中的签到时返回 None。
        """
        body = payload.get("checkIn")
        if not isinstance(body, Mapping):
            body = payload

        def get(source: Mapping[str, Any], key: str) -> str:
            return str(source.get(key) or "").strip()

        activity_id = get(body, "id")
        if not activity_id or activity_id == "0":
            return None
        return cls(
            id=activity_id,
            type=get(body, "checkInType"),
            code=get(body, "checkInCode"),
            qr_token=get(payload, "qrCode"),
            room_id=get(body, "roomID"),
            class_id=get(body, "tClassID"),
            create_date=get(body, "createrDate"),
            valid_date=get(body, "validDate"),
            check_in_status=get(body, "checkInStatus"),
            status_id=get(body, "statusID"),
            status_name=get(body, "statusName"),
        )

    @property
    def attended(self) -> bool:
        """平台是否已把本人记为出勤（含迟到/请假等已处理状态）。"""
        return (
            self.check_in_status == "1"
            or self.status_id == "1"
            or self.status_name == "出勤"
        )

    def ended(self, now: datetime | None = None) -> bool:
        """是否已过有效期；时间不可解析时按「未结束」处理。"""
        limit = parse_time(self.valid_date)
        if limit is None:
            return False
        current = now or datetime.now(limit.tzinfo)
        if limit.tzinfo is None:
            current = current.replace(tzinfo=None)
        return current > limit

    def remaining_seconds(self, now: datetime | None = None) -> int | None:
        """距签到结束还剩的秒数；时间不可解析返回 None。"""
        limit = parse_time(self.valid_date)
        if limit is None:
            return None
        current = now or datetime.now(limit.tzinfo)
        if limit.tzinfo is None:
            current = current.replace(tzinfo=None)
        return int((limit - current).total_seconds())

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """签到已创建多少秒；创建时间不可解析返回 None。"""
        created = parse_time(self.create_date)
        if created is None:
            return None
        current = now or datetime.now(created.tzinfo)
        if created.tzinfo is None:
            current = current.replace(tzinfo=None)
        return (current - created).total_seconds()


class Outcome(Enum):
    """单个签到的处理结果。"""

    SIGNED = "signed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"  # 条件未满足，保留待下一轮


__all__ = ["Course", "CheckInActivity", "Outcome", "parse_time"]
