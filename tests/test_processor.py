"""ActivityProcessor 的离线单元测试：回执判定、日志语义与重试收口。"""

from __future__ import annotations

import unittest
from datetime import datetime

from duifene.models import CheckInActivity, Course, Outcome
from duifene.processor import ActivityProcessor
from duifene.settings import Settings

COURSE = Course(course_id="123", class_id="456", name="测试课")


def make_activity(activity_id: str = "2557759", code: str = "") -> CheckInActivity:
    """构造一条「刚发起、可签到」的活动。"""
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    return CheckInActivity.from_raw(
        {
            "ID": activity_id,
            "CheckInType": "1",
            "CheckInCode": code,
            "CanApply": "1",
            "CheckInStatus": "0",
            "StatusID": "0",
            "StatusName": "缺勤",
            "CreaterDate": now,
            "ApplyLimitDate": now,
        }
    )


class StubLimiter:
    """限流状态机替身：始终不阻塞。"""

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
    """接口替身：记录提交内容，按预设返回文案。"""

    def __init__(self) -> None:
        self.limiter = StubLimiter()
        self.codes: list[str] = []
        self.qr_states: list[str] = []
        self.qr_error = ""
        self.entry_code: str | None = None
        self.hanzi: str | None = None
        self.qr_token: str | None = "TOKEN"
        self.code_reply: list[str] = ["签到成功!"]
        self.qr_reply: list[str] = ["参数错误,请重新尝试"]

    def sign_by_code(self, code: str):
        self.codes.append(code)
        return self.code_reply.pop(0) if self.code_reply else "签到成功!"

    def sign_by_qr(self, state: str):
        self.qr_states.append(state)
        return self.qr_reply.pop(0) if self.qr_reply else "参数错误,请重新尝试"

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


class StubPush:
    """WebSocket 推送替身：无订阅、无坐标。"""

    subscribed_id = None

    def subscribe(self, activity_id: str) -> bool:
        return False

    def take_follow_point(self):
        return None

    def stop(self) -> None:
        return None


class StubResolver:
    """坐标反推替身：解不出来。"""

    def resolve(self, course: Course):
        return None


class StubStore:
    """坐标缓存替身：永远无缓存。"""

    def load_center(self, class_id: str):
        return None

    def save_center(self, class_id: str, lon: float, lat: float) -> None:
        return None


def make_processor(manual=None, **overrides) -> tuple[ActivityProcessor, StubApi, list[str]]:
    """组装处理器与可观测的日志列表。"""
    settings = Settings()
    settings.qr_retry_interval_s = 0.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    api = StubApi()
    logs: list[str] = []
    processor = ActivityProcessor(
        settings, api, StubPush(), StubResolver(), StubStore(), logs.append, manual
    )
    return processor, api, logs


class SignedDetectionTest(unittest.TestCase):
    """服务端回执的判定矩阵。"""

    def test_matrix(self) -> None:
        cases = {
            "签到成功!": True,
            "签到成功": True,
            "已签到": True,
            "请勿重复签到": True,
            "签到未成功": False,
            "签到失败：距离太远": False,
            "参数错误,请重新尝试": False,
            "": False,
            None: False,
        }
        for text, expected in cases.items():
            self.assertIs(ActivityProcessor._is_signed(text), expected, text)


class LogSilenceTest(unittest.TestCase):
    """失败日志静音与 force 覆盖。"""

    def test_force_overrides_silence(self) -> None:
        processor, _api, logs = make_processor()
        activity = make_activity()
        processor._log_result(activity.id, COURSE, "签到失败：无响应")
        processor._log_result(activity.id, COURSE, "签到成功")
        self.assertEqual(len(logs), 1)
        processor._log_result(activity.id, COURSE, "签到成功", force=True)
        self.assertEqual(len(logs), 2)
        self.assertIn("签到成功", logs[-1])


class CodeSignTest(unittest.TestCase):
    """签到码：入口页兜底 + 轮次收口。"""

    def test_submits_code_from_entry_page(self) -> None:
        processor, api, _logs = make_processor()
        api.entry_code = "1234"
        outcome = processor._sign_code(make_activity(), COURSE)
        self.assertIs(outcome, Outcome.SIGNED)
        self.assertEqual(api.codes, ["1234"])

    def test_waits_then_gives_up(self) -> None:
        processor, _api, logs = make_processor(answer_wait_rounds=3)
        activity = make_activity()
        outcomes = [processor._sign_code(activity, COURSE) for _ in range(3)]
        self.assertEqual(
            outcomes, [Outcome.DEFERRED, Outcome.DEFERRED, Outcome.SKIPPED]
        )
        self.assertTrue(any("上限 3 轮" in line for line in logs))
        self.assertTrue(any("放弃" in line for line in logs))


class ManualCodeTest(unittest.TestCase):
    """手动提交的码：签到码与汉字都应使用，且只消费一次。"""

    def test_manual_code_signs_once(self) -> None:
        pending = ["8888"]
        processor, api, _logs = make_processor(
            manual=lambda: pending.pop() if pending else None
        )
        self.assertIs(processor._sign_code(make_activity(), COURSE), Outcome.SIGNED)
        self.assertEqual(api.codes, ["8888"])
        # 已消费：下一条活动回到等待，不会重复提交同一个码
        self.assertIs(
            processor._sign_code(make_activity(activity_id="2"), COURSE),
            Outcome.DEFERRED,
        )
        self.assertEqual(api.codes, ["8888"])

    def test_manual_answer_used_for_hanzi(self) -> None:
        pending = ["春眠不觉"]
        processor, api, _logs = make_processor(
            manual=lambda: pending.pop() if pending else None
        )
        self.assertIs(processor._sign_hanzi(make_activity(), COURSE), Outcome.SIGNED)
        self.assertEqual(api.codes, ["春眠不觉"])


class HanziSignTest(unittest.TestCase):
    """汉字：取不到答案同样按轮次收口。"""

    def test_gives_up_after_limit(self) -> None:
        processor, _api, logs = make_processor(answer_wait_rounds=2)
        activity = make_activity()
        first = processor._sign_hanzi(activity, COURSE)
        second = processor._sign_hanzi(activity, COURSE)
        self.assertIs(first, Outcome.DEFERRED)
        self.assertIs(second, Outcome.SKIPPED)
        self.assertTrue(any("汉字答案" in line for line in logs))

    def test_submits_when_answer_available(self) -> None:
        processor, api, _logs = make_processor()
        api.hanzi = "春眠不觉"
        self.assertIs(processor._sign_hanzi(make_activity(), COURSE), Outcome.SIGNED)
        self.assertEqual(api.codes, ["春眠不觉"])


class QrSignTest(unittest.TestCase):
    """二维码：直签失败 → 实时令牌重投 → 轮次用尽收口。"""

    def test_retries_then_gives_up(self) -> None:
        processor, api, logs = make_processor(qr_max_rounds=2, qr_retry_interval_s=0.0)
        activity = make_activity()
        first = processor._sign_qr(activity, COURSE)
        second = processor._sign_qr(activity, COURSE)
        self.assertIs(first, Outcome.DEFERRED)
        self.assertIs(second, Outcome.SKIPPED)
        self.assertTrue(any("二维码" in line for line in logs))
        # 每轮都应尝试过实时令牌，而不是只发活动 ID
        self.assertIn("TOKEN", api.qr_states)

    def test_success_with_live_token(self) -> None:
        processor, api, _logs = make_processor()
        api.qr_reply = ["参数错误,请重新尝试", "签到成功!"]
        self.assertIs(processor._sign_qr(make_activity(), COURSE), Outcome.SIGNED)

    def test_signed_clears_retry_state(self) -> None:
        processor, api, _logs = make_processor()
        api.qr_reply = ["参数错误,请重新尝试", "签到成功!"]
        processor._execute(make_activity(), COURSE)
        self.assertEqual(processor._qr_rounds, {})


if __name__ == "__main__":
    unittest.main()
