"""处理器单元测试：门禁放行、四类分发、答案取值与结果判定。"""

from __future__ import annotations

import math
import time
import unittest
from datetime import datetime, timedelta

from duifene.errors import AppApiError
from duifene.models import CheckInActivity, Course, Outcome
from duifene.processor import ActivityProcessor
from duifene.settings import Settings

COURSE = Course(course_id="11", class_id="21", name="测试课")
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def make_activity(**overrides) -> CheckInActivity:
    """构造一条「刚发起、可签到、未出勤」的活动。"""
    now = datetime.now()
    values = {
        "id": "2557759",
        "type": "1",
        "code": "1234",
        "qr_token": "",
        "room_id": "88",
        "class_id": "21",
        "create_date": (now - timedelta(seconds=10)).strftime(TIME_FMT),
        "valid_date": (now + timedelta(minutes=2)).strftime(TIME_FMT),
        "check_in_status": "0",
        "status_id": "0",
        "status_name": "缺勤",
    }
    values.update(overrides)
    return CheckInActivity(**values)


class StubLimiter:
    """限流状态机替身。"""

    def __init__(self, blocking: bool = False) -> None:
        self.blocking = blocking

    def is_blocking(self) -> bool:
        return self.blocking

    def remaining(self) -> float:
        return 9.0 if self.blocking else 0.0


class StubApi:
    """假传输层：记录每类签到实际提交了什么。"""

    def __init__(self) -> None:
        self.limiter = StubLimiter()
        self.codes: list[str] = []
        self.qr_tokens: list[str] = []
        self.locations: list[tuple[float, float]] = []
        self.headcount: tuple[int, int] | None = (10, 30)
        self.headcount_error: Exception | None = None
        self.code_reply = "签到成功！"
        self.qr_reply = "签到成功！"
        self.location_reply = "签到成功！"
        self.room: tuple[float, float] | None = (116.397, 39.909)
        self.detail: CheckInActivity | None = None
        self.detail_calls = 0
        self.code_error: Exception | None = None

    def fetch_headcount(self, activity_id: str):
        if self.headcount_error is not None:
            raise self.headcount_error
        return self.headcount

    def fetch_activity(self, activity_id: str):
        self.detail_calls += 1
        return self.detail

    def fetch_room_coordinates(self, activity):
        return self.room

    def sign_by_code(self, code: str) -> str:
        if self.code_error is not None:
            raise self.code_error
        self.codes.append(code)
        return self.code_reply

    def sign_by_qr(self, token: str) -> str:
        self.qr_tokens.append(token)
        return self.qr_reply

    def sign_by_location(self, longitude: float, latitude: float) -> str:
        self.locations.append((longitude, latitude))
        return self.location_reply


def make_processor(**settings_overrides):
    """组装处理器；默认关掉门禁等待，避免测试等待。"""
    settings = Settings()
    settings.sign_delay_s = 0.0
    settings.headcount_ratio = 0.0
    settings.qr_retry_interval_s = 0.0
    settings.location_retry_interval_s = 0.0
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    api = StubApi()
    logs: list[str] = []
    processor = ActivityProcessor(settings, api, logs.append)
    return processor, api, logs


class GateTest(unittest.TestCase):
    def test_attended_activity_is_skipped(self) -> None:
        processor, api, _ = make_processor()
        outcome = processor.process(make_activity(status_name="出勤"), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertEqual(api.codes, [])

    def test_ended_activity_is_skipped(self) -> None:
        processor, api, _ = make_processor()
        past = (datetime.now() - timedelta(minutes=1)).strftime(TIME_FMT)
        outcome = processor.process(make_activity(valid_date=past), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertEqual(api.codes, [])

    def test_stale_activity_is_skipped(self) -> None:
        processor, api, logs = make_processor(stale_sign_minutes=1)
        old = (datetime.now() - timedelta(minutes=5)).strftime(TIME_FMT)
        outcome = processor.process(make_activity(create_date=old), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertTrue(any("旧活动" in line for line in logs))

    def test_headcount_failure_does_not_block_signing(self) -> None:
        """人数接口异常必须放行：旧版正是卡在这里，四种签到全部没反应。"""
        processor, api, logs = make_processor(headcount_ratio=0.5)
        api.headcount_error = AppApiError("读取签到人数失败")
        outcome = processor.process(make_activity(), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["1234"])
        self.assertTrue(any("人数查询失败" in line for line in logs))

    def test_zero_total_does_not_block_signing(self) -> None:
        processor, api, _ = make_processor(headcount_ratio=0.5)
        api.headcount = (0, 0)
        self.assertIs(processor.process(make_activity(), COURSE), Outcome.SIGNED)

    def test_ratio_gate_waits_then_deadline_releases(self) -> None:
        processor, api, _ = make_processor(headcount_ratio=0.5)
        api.headcount = (1, 30)
        activity = make_activity()
        self.assertIs(processor.process(activity, COURSE), Outcome.DEFERRED)
        self.assertEqual(api.codes, [])
        # 把「首次看到」的时间往前拨到超过兜底时限
        processor._first_seen[activity.id] = (
            time.monotonic() - processor._settings.headcount_deadline_s - 5
        )
        self.assertIs(processor.process(activity, COURSE), Outcome.SIGNED)

    def test_deadline_works_even_when_create_date_is_garbage(self) -> None:
        """创建时间解析不出来时，兜底时限仍必须生效（旧实现会无限等下去）。"""
        processor, api, logs = make_processor(headcount_ratio=0.5)
        api.headcount = (1, 30)
        activity = make_activity(create_date="not-a-time")
        self.assertIs(processor.process(activity, COURSE), Outcome.DEFERRED)
        self.assertTrue(any("创建时间无法解析" in line for line in logs))
        processor._first_seen[activity.id] = (
            time.monotonic() - processor._settings.headcount_deadline_s - 5
        )
        self.assertIs(processor.process(activity, COURSE), Outcome.SIGNED)

    def test_delay_gate_waits(self) -> None:
        processor, api, logs = make_processor(sign_delay_s=30.0)
        self.assertIs(processor.process(make_activity(), COURSE), Outcome.DEFERRED)
        self.assertEqual(api.codes, [])
        self.assertTrue(any("延迟等待" in line for line in logs))

    def test_limiter_blocking_defers(self) -> None:
        processor, api, logs = make_processor()
        api.limiter.blocking = True
        self.assertIs(processor.process(make_activity(), COURSE), Outcome.DEFERRED)
        self.assertEqual(api.codes, [])
        self.assertTrue(any("限流中" in line for line in logs))


class AnswerTest(unittest.TestCase):
    def test_missing_code_warns_once_and_retries(self) -> None:
        processor, api, logs = make_processor()
        activity = make_activity(code="")
        self.assertIs(processor.process(activity, COURSE), Outcome.DEFERRED)
        self.assertIs(processor.process(activity, COURSE), Outcome.DEFERRED)
        self.assertEqual(api.codes, [])
        self.assertEqual(sum("未取到签到码" in line for line in logs), 1)

    def test_code_from_detail(self) -> None:
        processor, api, _ = make_processor()
        api.detail = make_activity(code="4321")
        outcome = processor.process(make_activity(code=""), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["4321"])

    def test_manual_code_is_used(self) -> None:
        processor, api, logs = make_processor()
        processor._take_manual = lambda: "8888"
        outcome = processor.process(make_activity(code=""), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["8888"])
        self.assertTrue(any("手动登记" in line for line in logs))

    def test_hanzi_requires_four_chars(self) -> None:
        processor, api, _ = make_processor()
        processor._take_manual = lambda: "春眠不觉"
        outcome = processor.process(make_activity(type="4", code=""), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["春眠不觉"])


class DispatchTest(unittest.TestCase):
    def test_code_signs(self) -> None:
        processor, api, _ = make_processor()
        self.assertIs(processor.process(make_activity(type="1"), COURSE), Outcome.SIGNED)
        self.assertEqual(api.codes, ["1234"])

    def test_qr_signs_with_token(self) -> None:
        processor, api, _ = make_processor()
        outcome = processor.process(
            make_activity(type="2", code="", qr_token="TOKEN"), COURSE
        )
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.qr_tokens, ["TOKEN"])

    def test_location_signs_near_room(self) -> None:
        processor, api, _ = make_processor()
        outcome = processor.process(make_activity(type="3", code=""), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(len(api.locations), 1)
        lon, lat = api.locations[0]
        dlat = (lat - 39.909) * 111132.92
        dlon = (lon - 116.397) * 111320.0 * math.cos(math.radians(39.909))
        distance = math.hypot(dlat, dlon)
        self.assertGreaterEqual(distance, 2.5)
        self.assertLessEqual(distance, 18.5)

    def test_location_without_room_retries(self) -> None:
        processor, api, logs = make_processor()
        api.room = None
        outcome = processor.process(make_activity(type="3", code=""), COURSE)
        self.assertIs(outcome, Outcome.DEFERRED)
        self.assertEqual(api.locations, [])
        self.assertTrue(any("未取到教室坐标" in line for line in logs))

    def test_unknown_type_is_skipped(self) -> None:
        processor, api, logs = make_processor()
        self.assertIs(processor.process(make_activity(type="9"), COURSE), Outcome.SKIPPED)
        self.assertEqual(api.codes, [])
        self.assertTrue(any("类型未知" in line for line in logs))

    def test_sign_api_error_degrades_to_retry(self) -> None:
        """单次接口异常不应抛出（旧实现会让整轮循环退避 60 秒，直接睡过签到）。"""
        processor, api, logs = make_processor()
        api.code_error = AppApiError("网络请求失败（ProxyError）")
        self.assertIs(processor.process(make_activity(), COURSE), Outcome.DEFERRED)
        self.assertTrue(any("请求失败" in line for line in logs))
        api.code_error = None
        self.assertIs(processor.process(make_activity(), COURSE), Outcome.SIGNED)

    def test_signed_activity_is_not_repeated(self) -> None:
        processor, api, _ = make_processor()
        activity = make_activity()
        processor.process(activity, COURSE)
        self.assertIs(processor.process(activity, COURSE), Outcome.SKIPPED)
        self.assertEqual(api.codes, ["1234"])


class ResultJudgeTest(unittest.TestCase):
    def test_success_variants(self) -> None:
        for message in ("签到成功！", "签到成功", "已签到", "请勿重复签到"):
            self.assertTrue(ActivityProcessor._is_signed(message), message)

    def test_failure_variants(self) -> None:
        for message in ("签到失败", "签到未成功", "参数错误", "", None):
            self.assertFalse(ActivityProcessor._is_signed(message), message)

    def test_failure_is_reported_with_server_text(self) -> None:
        processor, api, logs = make_processor()
        api.code_reply = "签到码错误"
        outcome = processor.process(make_activity(), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertTrue(any("签到失败：签到码错误" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
