"""坐标反推的节流行为：静默期内不得再发探测请求。"""

from __future__ import annotations

import unittest

from duifene.location import LocationResolver
from duifene.models import Course
from duifene.settings import Settings

COURSE = Course(course_id="123", class_id="456", name="测试课")


class BlockingLimiter:
    """始终处于静默期的限流替身。"""

    def is_blocking(self) -> bool:
        return True

    def remaining(self) -> float:
        return 300.0


class RecordingApi:
    """记录探测次数的接口替身。"""

    def __init__(self) -> None:
        self.limiter = BlockingLimiter()
        self.calls = 0

    def probe_distance(self, course: Course, lon: float, lat: float):
        self.calls += 1
        return 100.0


class ResolverThrottleTest(unittest.TestCase):
    """限流期间反推必须直接放弃，而不是继续消耗服务端额度。"""

    def test_no_probe_during_cooldown(self) -> None:
        settings = Settings()
        settings.probe_interval_s = 0.0
        api = RecordingApi()
        resolver = LocationResolver(settings, api)
        self.assertIsNone(resolver.resolve(COURSE))
        self.assertEqual(api.calls, 0, "静默期内不应发出任何探测请求")


if __name__ == "__main__":
    unittest.main()
