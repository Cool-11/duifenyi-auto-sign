"""领域模型的跨时区时间计算测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from duifene.models import CheckInActivity, SERVER_TIMEZONE, parse_time


def activity(created: str, valid: str = "2026-11-05T00:00:00") -> CheckInActivity:
    return CheckInActivity("1", "1", "1234", "", "", "1", created, valid, "0", "0", "缺勤")


class ServerTimezoneTest(unittest.TestCase):
    def test_naive_server_time_is_beijing_time(self) -> None:
        parsed = parse_time("2026-11-04T23:00:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_los_angeles_clock_does_not_create_huge_delay(self) -> None:
        # 北京 23:00 与洛杉矶 07:00 是同一时刻（冬令时，UTC-8）。
        los_angeles = timezone(timedelta(hours=-8))
        now = datetime(2026, 11, 4, 7, 0, 10, tzinfo=los_angeles)
        self.assertAlmostEqual(activity("2026-11-04T23:00:00").age_seconds(now), 10.0)

    def test_naive_explicit_now_is_treated_as_server_time(self) -> None:
        now = datetime(2026, 11, 4, 23, 0, 10)
        self.assertAlmostEqual(activity("2026-11-04T23:00:00").age_seconds(now), 10.0)


if __name__ == "__main__":
    unittest.main()
