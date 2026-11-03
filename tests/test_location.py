"""坐标反推的节流行为：静默期内不得再发探测请求。"""

from __future__ import annotations

import unittest

from duifene.errors import RateLimited
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


class ToggleLimiter:
    """可手动切换的限流替身。"""

    def __init__(self) -> None:
        self.blocking = False

    def is_blocking(self) -> bool:
        return self.blocking

    def remaining(self) -> float:
        return 10.0 if self.blocking else 0.0


class FlakyApi:
    """第 N 次之后报限流的接口替身。"""

    def __init__(self, block_after: int) -> None:
        self.limiter = ToggleLimiter()
        self.block_after = block_after
        self.calls = 0

    def probe_distance(self, course: Course, lon: float, lat: float):
        self.calls += 1
        if self.calls > self.block_after:
            self.limiter.blocking = True
            raise RateLimited("频繁")
        return 5000.0 + self.calls


class ResolverResumeTest(unittest.TestCase):
    """被限流打断后应保留已采样本，冷却结束从断点续采而不是从头开始。"""

    def test_resumes_after_cooldown(self) -> None:
        settings = Settings()
        settings.probe_interval_s = 0.0
        settings.probe_azimuths = 4
        api = FlakyApi(block_after=2)
        resolver = LocationResolver(settings, api)

        self.assertIsNone(resolver.resolve(COURSE))
        self.assertEqual(api.calls, 3, "第三次探测应被限流打断")
        self.assertEqual(resolver._cursor, 2, "断点应停在已采到的两个点之后")

        # 冷却期间不发请求
        self.assertIsNone(resolver.resolve(COURSE))
        self.assertEqual(api.calls, 3)

        # 冷却结束后从断点继续，只需再采剩余两个方位
        api.limiter.blocking = False
        api.block_after = 99
        resolver.resolve(COURSE)
        self.assertEqual(api.calls, 5, "应只补采剩余两个方位")


if __name__ == "__main__":
    unittest.main()
