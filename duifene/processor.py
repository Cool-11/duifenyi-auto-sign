"""签到业务核心：单个活动的判定与执行。

四类签到全部走 App 接口：

* 签到码 / 汉字 —— ``/CK/StudentCheckIn``，码由服务端下发（也可由界面上手动登记）；
* 二维码 —— ``/CK/QrCodeCheckIn``，令牌同样由服务端下发（``qrCode`` 字段）；
* 定位 —— 先取签到绑定教室的经纬度，再按 3–18 米随机偏移提交。

设计原则：**门禁只做风险控制，绝不阻断签到**。人数接口异常、人数为 0、迟迟
达不到占比门槛时，一律在兜底时限内放行——宁可早签，也不能因为「等人数」把
整场活动等过去（这正是旧版「四种签到全无反应」的成因之一）。
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from .api import DuifeneApi
from .errors import AppApiError, AuthError, RateLimited
from .models import CheckInActivity, Course, Outcome
from .settings import Settings

METERS_PER_DEGREE_LATITUDE = 111_132.92
METERS_PER_DEGREE_LONGITUDE = 111_320.0


class ActivityProcessor:
    """判定单条签到是否可提交，并完成签到。

    跨轮次状态（已处理集合、提示去重、重试节拍）内聚于本类，由 :meth:`reset`
    在开始监听时清空。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        log: Callable[[str], None],
        manual_code: Callable[[], str | None] | None = None,
    ) -> None:
        self._settings = settings
        self._api = api
        self._log = log
        # 用户在界面上手动登记的 4 位签到码 / 4 个汉字（服务端没下发时的兜底）
        self._take_manual = manual_code or (lambda: None)
        self._handled: set[str] = set()
        # 提示去重：同一活动同一状态只提示一次，避免逐轮刷屏
        self._notices: dict[str, str] = {}
        # 已输出过的结果文案：文案变化时才再记一条（失败→成功不会被吞掉）
        self._results: dict[str, str] = {}
        # 二维码 / 定位的重试节拍
        self._last_attempt: dict[str, float] = {}

    def reset(self) -> None:
        """清空跨轮次状态。"""
        self._handled.clear()
        self._notices.clear()
        self._results.clear()
        self._last_attempt.clear()

    # ─ 主流程 ──────────────────────────────────────────────────────
    def process(self, activity: CheckInActivity, course: Course) -> Outcome:
        """处理一条进行中的签到，返回处理结果。"""
        aid = activity.id
        if aid in self._handled:
            return Outcome.SKIPPED
        if activity.attended:
            self._mark(course, aid, "已签到，无需重复签到")
            return Outcome.SKIPPED
        if activity.ended():
            self._mark(course, aid, "已超过签到结束时间")
            return Outcome.SKIPPED
        age = activity.age_seconds()
        if age is not None and age > self._settings.stale_sign_minutes * 60:
            self._mark(
                course,
                aid,
                f"创建已超过 {self._settings.stale_sign_minutes} 分钟，视为旧活动",
            )
            return Outcome.SKIPPED
        if self._api.limiter is not None and self._api.limiter.is_blocking():
            left = int(self._api.limiter.remaining())
            self._notice(aid, f"limiter_{left}", course, f"限流中，{left} 秒后恢复")
            return Outcome.DEFERRED

        gate = self._delay_gate(activity, age, course)
        if gate is not None:
            return gate
        gate = self._headcount_gate(activity, age, course)
        if gate is not None:
            return gate
        return self._execute(activity, course)

    def _delay_gate(
        self, activity: CheckInActivity, age: float | None, course: Course
    ) -> Outcome | None:
        """延迟门禁：检测到签到后等待 ``sign_delay_s`` 秒再签；未启用返回 None。"""
        delay = self._settings.sign_delay_s
        if delay <= 0 or age is None or age >= delay:
            return None
        left = int(delay - age)
        self._notice(
            activity.id, f"delay_{left}", course, f"活动 {activity.id} 延迟等待中，还剩 {left} 秒"
        )
        return Outcome.DEFERRED

    def _headcount_gate(
        self, activity: CheckInActivity, age: float | None, course: Course
    ) -> Outcome | None:
        """人数门禁：需继续等待返回 :attr:`Outcome.DEFERRED`，可签返回 None。

        任何取不到人数、人数为 0、或等待超过 ``headcount_deadline_s`` 的情况都
        直接放行（fail-open）——人数只用于降低「第一个签」的风控风险，不是签到
        的前提条件。
        """
        aid = activity.id
        deadline = self._settings.headcount_deadline_s
        if age is not None and deadline > 0 and age >= deadline:
            self._notice(aid, "deadline", course, f"活动 {aid} 已达兜底时限，直接签到")
            return None
        try:
            signed, total = self._api.fetch_headcount(aid)
        except AuthError:
            raise
        except (AppApiError, ValueError) as exc:
            self._notice(
                aid,
                "headcount_failed",
                course,
                f"活动 {aid} 人数查询失败（{exc!s}），跳过人数门槛直接签到",
            )
            return None
        if total <= 0:
            self._notice(
                aid, "headcount_zero", course, f"活动 {aid} 人数为 0，跳过人数门槛直接签到"
            )
            return None
        if total < self._settings.small_class_max:
            wait_until = self._settings.small_class_delay_s
            if age is not None and age < wait_until:
                self._notice(
                    aid,
                    f"small_{int(wait_until - age)}",
                    course,
                    f"活动 {aid} 小班 {total} 人，{int(wait_until - age)} 秒后签到",
                )
                return Outcome.DEFERRED
            return None
        threshold = self._settings.headcount_ratio
        if threshold <= 0:
            return None
        ratio = signed / total
        if ratio < threshold:
            self._notice(
                aid,
                f"ratio_{signed}_{total}",
                course,
                f"活动 {aid} 已签 {signed}/{total}（{ratio:.0%}），"
                f"未达 {threshold:.0%} 门槛，继续等待（最迟 {int(deadline)} 秒后直接签到）",
            )
            return Outcome.DEFERRED
        return None

    def _execute(self, activity: CheckInActivity, course: Course) -> Outcome:
        """按签到类型执行；命中限流时降级为 :attr:`Outcome.DEFERRED`。"""
        try:
            if activity.type == "1":
                outcome = self._sign_code(activity, course)
            elif activity.type == "2":
                outcome = self._sign_qr(activity, course)
            elif activity.type == "3":
                outcome = self._sign_location(activity, course)
            elif activity.type == "4":
                outcome = self._sign_hanzi(activity, course)
            else:
                self._notice(
                    activity.id,
                    "unknown_type",
                    course,
                    f"活动 {activity.id} 类型未知（{activity.type or '空'}），跳过",
                )
                return Outcome.SKIPPED
        except RateLimited as exc:
            self._log(f"课程: {course.name} 活动 {activity.id} {exc!s}")
            return Outcome.DEFERRED
        if outcome is Outcome.SIGNED:
            self._handled.add(activity.id)
        return outcome

    # ─ 四类签到 ────────────────────────────────────────────────────
    def _sign_code(self, activity: CheckInActivity, course: Course) -> Outcome:
        """签到码：服务端下发 → 详情接口 → 界面手动登记。"""
        code = self._resolve_answer(activity, "签到码")
        if not code:
            self._notice(
                activity.id,
                "code_missing",
                course,
                f"活动 {activity.id} 未取到签到码，持续重试；"
                f"也可把群里的 4 位码填入顶部「手动签到」",
            )
            return Outcome.DEFERRED
        self._attempt(activity.id, course, f"活动 {activity.id} 提交签到码 {code}")
        return self._report(activity.id, self._api.sign_by_code(code), course)

    def _sign_hanzi(self, activity: CheckInActivity, course: Course) -> Outcome:
        """汉字签到：与签到码同一接口，答案同样由服务端下发。"""
        answer = self._resolve_answer(activity, "汉字答案")
        if not answer:
            self._notice(
                activity.id,
                "hanzi_missing",
                course,
                f"活动 {activity.id} 未取到汉字答案，持续重试；"
                f"也可把 4 个汉字按正确顺序填入顶部「手动签到」",
            )
            return Outcome.DEFERRED
        self._attempt(activity.id, course, f"活动 {activity.id} 提交汉字 {answer}")
        return self._report(activity.id, self._api.sign_by_code(answer), course)

    def _sign_qr(self, activity: CheckInActivity, course: Course) -> Outcome:
        """二维码：令牌滚动有效，按节拍现取现用。"""
        aid = activity.id
        if not self._throttle(aid, self._settings.qr_retry_interval_s):
            return Outcome.DEFERRED
        token = activity.qr_token
        if not token:
            detail = self._api.fetch_activity(aid)
            token = detail.qr_token if detail is not None else ""
        if not token:
            self._notice(
                aid,
                "qr_missing",
                course,
                f"活动 {aid} 未取到二维码令牌，持续重试",
            )
            return Outcome.DEFERRED
        self._attempt(aid, course, f"活动 {aid} 提交二维码签到")
        return self._report(aid, self._api.sign_by_qr(token), course)

    def _sign_location(self, activity: CheckInActivity, course: Course) -> Outcome:
        """定位：取签到绑定教室的经纬度，按 3–18 米随机偏移提交。"""
        aid = activity.id
        if not self._throttle(aid, self._settings.location_retry_interval_s):
            return Outcome.DEFERRED
        coordinates = self._api.fetch_room_coordinates(activity)
        if coordinates is None:
            self._notice(
                aid,
                "room_missing",
                course,
                f"活动 {aid} 未取到教室坐标（签到未绑定教室），持续重试",
            )
            return Outcome.DEFERRED
        longitude, latitude, offset_m = self._offset(*coordinates)
        self._attempt(
            aid,
            course,
            f"活动 {aid} 提交定位（距教室约 {offset_m:.0f} 米）",
        )
        message = self._api.sign_by_location(longitude, latitude)
        if self._is_signed(message):
            self._result(
                aid,
                course,
                f"定位签到成功：经度 {longitude:.6f}，纬度 {latitude:.6f}",
            )
            return Outcome.SIGNED
        self._result(aid, course, f"定位签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    # ─ 取值与工具 ──────────────────────────────────────────────────
    def _resolve_answer(self, activity: CheckInActivity, what: str) -> str:
        """取 4 位答案：活动字段 → 详情接口 → 手动登记；都取不到返回空串。"""
        code = activity.code
        if len(code) == 4:
            return code
        detail = None
        try:
            detail = self._api.fetch_activity(activity.id)
        except AuthError:
            raise
        except AppApiError:
            detail = None
        if detail is not None and len(detail.code) == 4:
            return detail.code
        manual = self._take_manual() or ""
        if len(manual) == 4:
            self._log(f"使用手动登记的{what} {manual}")
            return manual
        return ""

    def _throttle(self, activity_id: str, interval: float) -> bool:
        """同一活动的重试节拍；未到间隔返回 False。"""
        now = time.time()
        last = self._last_attempt.get(activity_id, 0.0)
        if interval > 0 and now - last < interval:
            return False
        self._last_attempt[activity_id] = now
        return True

    def _offset(self, longitude: float, latitude: float) -> tuple[float, float, float]:
        """在教室坐标周围按随机方向生成 3–18 米的签到点。"""
        distance = random.uniform(
            self._settings.location_offset_min_m, self._settings.location_offset_max_m
        )
        direction = random.uniform(0.0, 2 * math.pi)
        north = distance * math.sin(direction)
        east = distance * math.cos(direction)
        lon_scale = max(
            abs(METERS_PER_DEGREE_LONGITUDE * math.cos(math.radians(latitude))), 1.0
        )
        return (
            longitude + east / lon_scale,
            latitude + north / METERS_PER_DEGREE_LATITUDE,
            distance,
        )

    @staticmethod
    def _is_signed(message: str | None) -> bool:
        """服务端文案是否表示签到成功。

        除「签到成功」外，「已签到 / 请勿重复签到」同样是有效回执：本轮之前
        已在别处签过、或服务端把重复提交归一化时返回的是这类文案。
        """
        if not message or "未成功" in message or "失败" in message:
            return False
        return any(
            marker in message
            for marker in ("签到成功", "已签到", "已经签到", "请勿重复签到")
        )

    # ─ 日志 ────────────────────────────────────────────────────────
    def _notice(self, activity_id: str, marker: str, course: Course, message: str) -> None:
        """同一活动同一状态只提示一次。"""
        if self._notices.get(activity_id) == marker:
            return
        self._notices[activity_id] = marker
        self._log(f"课程: {course.name} {message}")

    def _attempt(self, activity_id: str, course: Course, message: str) -> None:
        """输出一次提交动作（该活动已有结果文案时不再刷）。"""
        if activity_id in self._results:
            return
        self._log(f"课程: {course.name} {message}")

    def _result(self, activity_id: str, course: Course, message: str) -> None:
        """输出结果文案；文案与上次相同时不重复记录。"""
        if self._results.get(activity_id) == message:
            return
        self._results[activity_id] = message
        self._log(f"课程: {course.name} {message}")

    def _report(self, activity_id: str, message: str | None, course: Course) -> Outcome:
        """归一化签到结果：「签到成功」或「签到失败：<服务端原文>」。"""
        if self._is_signed(message):
            self._result(activity_id, course, "签到成功")
            return Outcome.SIGNED
        self._result(activity_id, course, f"签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _mark(self, course: Course, activity_id: str, reason: str) -> None:
        """记录活动为已处理并打印原因，避免每轮重复提示。"""
        self._handled.add(activity_id)
        self._log(f"课程: {course.name} 活动 {activity_id} {reason}，跳过")


__all__ = ["ActivityProcessor"]
