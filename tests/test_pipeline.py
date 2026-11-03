"""四类签到的全链路仿真测试：从活动列表进入 ``process()`` 直到结果落定。

这里用假传输层替代真实网络，验证的是**调度与分支**是否正确：门禁 → 类型分发 →
提交参数 → 回执判定 → 去重。真机验收仍需要有效登录与进行中的活动。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from duifene.models import CheckInActivity, Course, Outcome
from duifene.processor import ActivityProcessor
from duifene.settings import Settings

COURSE = Course(course_id="123", class_id="456", name="测试课")
DATE_FMT = "%Y/%m/%d %H:%M:%S"


def make_activity(activity_type: str = "1", code: str = "", activity_id: str = "2557759"):
    """构造一条「刚发起、可签到、未出勤」的活动。"""
    now = datetime.now()
    return CheckInActivity.from_raw(
        {
            "ID": activity_id,
            "CheckInType": activity_type,
            "CheckInCode": code,
            "CanApply": "1",
            "CheckInStatus": "0",
            "StatusID": "0",
            "StatusName": "缺勤",
            "CreaterDate": (now - timedelta(seconds=10)).strftime(DATE_FMT),
            # 补签截止比签到结束晚 24 小时，这里给 +25h 表示活动仍在进行
            "ApplyLimitDate": (now + timedelta(hours=25)).strftime(DATE_FMT),
        }
    )


class StubLimiter:
    """限流状态机替身。"""

    def is_blocking(self) -> bool:
        return False

    def remaining(self) -> float:
        return 0.0

    def raise_if_blocking(self) -> None:
        return None

    def trip(self) -> None:
        return None

    def is_limit_text(self, text: str) -> bool:
        return False


class StubApi:
    """假传输层：记录每类签到实际提交了什么。"""

    def __init__(self) -> None:
        self.limiter = StubLimiter()
        self.codes: list[str] = []
        self.qr_states: list[str] = []
        self.locations: list[tuple[Course, float, float]] = []
        self.code_reply = "签到成功!"
        self.qr_replies = ["参数错误,请重新尝试", "签到成功!"]
        self.location_reply = "签到成功!"
        self.qr_token = "LIVE-TOKEN"
        self.entry_code: str | None = None
        self.hanzi: str | None = None

    def sign_by_code(self, code: str):
        self.codes.append(code)
        return self.code_reply

    def sign_by_qr(self, state: str):
        self.qr_states.append(state)
        return self.qr_replies.pop(0) if self.qr_replies else "参数错误,请重新尝试"

    def fetch_qr_state(self, course: Course):
        return self.qr_token

    def fetch_code_from_entry(self):
        return self.entry_code

    def fetch_hanzi_answer(self, course: Course):
        return self.hanzi

    def prime_checkin_entry(self, course: Course) -> None:
        return None

    def fetch_headcount(self, activity_id: str):
        return (10, 30)

    def sign_by_location(self, course: Course, lon: float, lat: float):
        self.locations.append((course, lon, lat))
        return self.location_reply


class StubPush:
    """无推送、订阅失败的推送替身：直接把流程推进到缓存/反推分支。"""

    subscribed_id = None

    def subscribe(self, activity_id: str) -> bool:
        return False

    def take_follow_point(self):
        return None

    def stop(self) -> None:
        return None


class StubResolver:
    """坐标反推替身。"""

    def __init__(self, result=None) -> None:
        self.result = result

    def resolve(self, course: Course):
        return self.result


class StubStore:
    """坐标缓存替身：可预置教室坐标并记录落盘。"""

    def __init__(self, center=None) -> None:
        self.center = center
        self.saved: list[tuple[str, float, float]] = []

    def load_center(self, class_id: str):
        return self.center

    def save_center(self, class_id: str, lon: float, lat: float) -> None:
        self.saved.append((class_id, lon, lat))


def make_processor(store: StubStore | None = None, resolver=None):
    """组装处理器；默认关掉延迟门禁，避免测试等待。"""
    settings = Settings()
    settings.sign_delay_s = 0.0
    settings.qr_retry_interval_s = 0.0
    settings.ws_wait_timeout_s = 0.0
    api = StubApi()
    logs: list[str] = []
    processor = ActivityProcessor(
        settings,
        api,
        StubPush(),
        resolver or StubResolver(),
        store or StubStore(),
        logs.append,
    )
    return processor, api, logs


class PipelineTest(unittest.TestCase):
    """四类签到各跑一遍完整调度。"""

    def test_code_type_signs(self) -> None:
        processor, api, _logs = make_processor()
        outcome = processor.process(make_activity("1", code="1234"), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["1234"])

    def test_qr_type_falls_back_to_live_token(self) -> None:
        processor, api, _logs = make_processor()
        outcome = processor.process(make_activity("2", activity_id="3001"), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertIn("LIVE-TOKEN", api.qr_states)

    def test_location_type_uses_cached_center(self) -> None:
        store = StubStore(center=(116.4, 39.9))
        processor, api, _logs = make_processor(store=store)
        outcome = processor.process(make_activity("3", activity_id="3002"), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(len(api.locations), 1)
        course, lon, lat = api.locations[0]
        self.assertEqual(course.class_id, "456")
        self.assertAlmostEqual(lon, 116.4, places=4)
        self.assertAlmostEqual(lat, 39.9, places=4)

    def test_location_without_center_is_skipped(self) -> None:
        processor, _api, logs = make_processor()
        outcome = processor.process(make_activity("3", activity_id="3003"), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertTrue(any("未取得教室坐标" in line for line in logs))

    def test_hanzi_type_signs(self) -> None:
        processor, api, _logs = make_processor()
        api.hanzi = "春眠不觉"
        outcome = processor.process(make_activity("4", activity_id="3004"), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["春眠不觉"])

    def test_signed_activity_is_not_repeated(self) -> None:
        processor, api, _logs = make_processor()
        activity = make_activity("1", code="1234")
        processor.process(activity, COURSE)
        again = processor.process(activity, COURSE)
        self.assertIs(again, Outcome.SKIPPED)
        self.assertEqual(api.codes, ["1234"])

    def test_already_attended_is_skipped(self) -> None:
        processor, api, _logs = make_processor()
        attended = replace(make_activity("1", code="1234"), status_name="出勤")
        outcome = processor.process(attended, COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertEqual(api.codes, [])

    def test_unknown_type_is_skipped(self) -> None:
        processor, api, logs = make_processor()
        outcome = processor.process(make_activity("9", activity_id="3005"), COURSE)
        self.assertIs(outcome, Outcome.SKIPPED)
        self.assertTrue(any("类型未知" in line for line in logs))
        self.assertEqual(api.codes, [])


if __name__ == "__main__":
    unittest.main()
