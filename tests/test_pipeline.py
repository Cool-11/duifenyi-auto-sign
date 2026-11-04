"""全链路仿真测试：监控线程 → 处理器 → 四类签到提交 → 结果落定。

用假传输层替代真实网络，验证的是**调度链路**：轮询取活动 → 门禁 → 类型分发 →
提交参数 → 回执判定 → 去重。真机验收仍需有效账号与进行中的签到。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from duifene.errors import AuthError
from duifene.models import CheckInActivity, Course, Outcome, SERVER_TIMEZONE
from duifene.processor import ActivityProcessor
from duifene.settings import Settings
from duifene.watcher import Watcher

COURSE = Course(course_id="11", class_id="21", name="测试课")
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def make_activity(activity_type: str = "1", **overrides) -> CheckInActivity:
    now = datetime.now(SERVER_TIMEZONE)
    values = {
        "id": "2557759",
        "type": activity_type,
        "code": "1234" if activity_type == "1" else "",
        "qr_token": "TOKEN" if activity_type == "2" else "",
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
    def is_blocking(self) -> bool:
        return False

    def remaining(self) -> float:
        return 0.0


class StubApi:
    """假传输层：四类签到都记录到不同列表。"""

    def __init__(self) -> None:
        self.limiter = StubLimiter()
        self.authenticated = True
        self.activity: CheckInActivity | None = None
        self.codes: list[str] = []
        self.qr_tokens: list[str] = []
        self.locations: list[tuple[float, float]] = []

    @property
    def is_authenticated(self) -> bool:
        return self.authenticated

    def fetch_active_activity(self, course_id: str):
        return self.activity

    def fetch_headcount(self, activity_id: str):
        return (12, 30)

    def fetch_activity(self, activity_id: str):
        return self.activity

    def fetch_room_coordinates(self, activity):
        return (116.397, 39.909)

    def sign_by_code(self, code: str) -> str:
        self.codes.append(code)
        return "签到成功！"

    def sign_by_qr(self, token: str) -> str:
        self.qr_tokens.append(token)
        return "签到成功！"

    def sign_by_location(self, longitude: float, latitude: float) -> str:
        self.locations.append((longitude, latitude))
        return "签到成功！"


def build(**settings_overrides):
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
    watcher = Watcher(settings, api, processor, logs.append)
    watcher.set_course(COURSE)
    return watcher, processor, api, logs


class WatcherTest(unittest.TestCase):
    def test_tick_without_activity_does_nothing(self) -> None:
        watcher, _processor, api, logs = build()
        watcher._tick()
        self.assertEqual(api.codes, [])
        # 只允许出现「进入监听时间窗」一条，空轮询不得刷日志
        self.assertLessEqual(len([line for line in logs if "时间窗" not in line]), 0)

    def test_tick_signs_detected_activity(self) -> None:
        watcher, _processor, api, logs = build()
        api.activity = make_activity("1")
        watcher._tick()
        self.assertEqual(api.codes, ["1234"])
        self.assertTrue(any("签到成功" in line for line in logs))

    def test_tick_twice_signs_once(self) -> None:
        watcher, _processor, api, _logs = build()
        api.activity = make_activity("1")
        watcher._tick()
        watcher._tick()
        self.assertEqual(api.codes, ["1234"])

    def test_unauthenticated_tick_raises(self) -> None:
        watcher, _processor, api, _logs = build()
        api.authenticated = False
        with self.assertRaises(AuthError):
            watcher._tick()

    def test_start_requires_authentication(self) -> None:
        watcher, _processor, api, _logs = build()
        api.authenticated = False
        with self.assertRaises(AuthError):
            watcher.start()


class FourTypesTest(unittest.TestCase):
    """四类签到各走一遍完整链路。"""

    def run_type(self, activity_type: str) -> tuple[StubApi, list[str]]:
        watcher, _processor, api, logs = build()
        api.activity = make_activity(activity_type)
        watcher._tick()
        return api, logs

    def test_code(self) -> None:
        api, logs = self.run_type("1")
        self.assertEqual(api.codes, ["1234"])
        self.assertTrue(any("签到成功" in line for line in logs))

    def test_qr(self) -> None:
        api, logs = self.run_type("2")
        self.assertEqual(api.qr_tokens, ["TOKEN"])
        self.assertTrue(any("签到成功" in line for line in logs))

    def test_location(self) -> None:
        api, logs = self.run_type("3")
        self.assertEqual(len(api.locations), 1)
        self.assertTrue(any("定位签到成功" in line for line in logs))

    def test_hanzi(self) -> None:
        watcher, _processor, api, logs = build()
        api.activity = make_activity("4", code="春眠不觉")
        watcher._tick()
        self.assertEqual(api.codes, ["春眠不觉"])
        self.assertTrue(any("签到成功" in line for line in logs))


class ResetTest(unittest.TestCase):
    def test_reset_allows_reprocessing_same_activity(self) -> None:
        watcher, processor, api, _logs = build()
        api.activity = make_activity("1")
        watcher._tick()
        processor.reset()
        watcher._tick()
        self.assertEqual(api.codes, ["1234", "1234"])


if __name__ == "__main__":
    unittest.main()
