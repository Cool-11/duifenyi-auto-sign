"""时间解析的离线测试：服务端换序列化方式时不能把签到卡死。"""

from __future__ import annotations

import unittest
from datetime import datetime

from duifene.models import CheckInActivity, parse_time


def make_activity(**overrides) -> CheckInActivity:
    values = {
        "id": "1",
        "type": "1",
        "code": "1234",
        "qr_token": "",
        "room_id": "0",
        "class_id": "0",
        "create_date": "2026-11-04T13:00:00",
        "valid_date": "2026-11-04T13:05:00",
        "check_in_status": "0",
        "status_id": "0",
        "status_name": "缺勤",
    }
    values.update(overrides)
    return CheckInActivity(**values)


class ParseTimeTest(unittest.TestCase):
    def test_iso_format(self) -> None:
        self.assertEqual(
            parse_time("2026-11-04T13:00:00"), datetime(2026, 11, 4, 13, 0, 0)
        )

    def test_iso_with_milliseconds(self) -> None:
        value = parse_time("2026-11-04T13:00:00.123")
        self.assertEqual(value.second, 0)
        self.assertEqual(value.microsecond, 123000)

    def test_slash_format(self) -> None:
        self.assertEqual(
            parse_time("2026/11/04 13:00:00"), datetime(2026, 11, 4, 13, 0, 0)
        )

    def test_aspnet_date_format(self) -> None:
        expected = datetime(2026, 11, 4, 13, 0, 0)
        milli = int(expected.timestamp() * 1000)
        self.assertEqual(parse_time(f"/Date({milli})/"), expected)

    def test_garbage_returns_none(self) -> None:
        for value in ("", "  ", "not-a-time", "2026-13-45", None):
            self.assertIsNone(parse_time(value))

    def test_age_is_none_for_unparsable_date(self) -> None:
        self.assertIsNone(make_activity(create_date="???").age_seconds())

    def test_ended_is_false_when_date_unparsable(self) -> None:
        # 解析不出来时不能判定「已结束」，否则活动会被直接跳过
        self.assertFalse(make_activity(valid_date="???").ended())


if __name__ == "__main__":
    unittest.main()
