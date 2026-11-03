"""DuifeneApi 的离线单元测试：解析、表单构造与回执判定，全部不发真实请求。"""

from __future__ import annotations

import json
import unittest

from duifene.api import DuifeneApi
from duifene.models import Course
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings

COURSE = Course(course_id="123", class_id="456", name="测试课")


class FakeResponse:
    """最小可用的响应替身。"""

    def __init__(self, status_code: int = 200, text: str = "", headers=None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeCookies:
    """requests.cookies 的替身，只实现被用到的三个方法。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def clear(self) -> None:
        self._data.clear()

    def update(self, values) -> None:
        self._data.update(values)

    def get_dict(self) -> dict[str, str]:
        return dict(self._data)


class FakeSession:
    """按 URL 片段命中预置响应，并记录请求参数供断言。"""

    def __init__(self, responses: dict[str, FakeResponse] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []
        self.cookies = FakeCookies()
        self.proxies: dict[str, str] = {}
        self.verify = True

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._pick(url)

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._pick(url)

    def _pick(self, url: str) -> FakeResponse:
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        return FakeResponse(text="{}")

    def last_body(self) -> str:
        """最近一次请求的表单体（字符串形式）。"""
        for _method, _url, kwargs in reversed(self.calls):
            data = kwargs.get("data")
            if isinstance(data, str):
                return data
        return ""


def make_api(responses: dict[str, FakeResponse] | None = None) -> DuifeneApi:
    """构造被测对象；学生 ID 直接注入，避免额外的网络往返。"""
    session = FakeSession(responses)
    api = DuifeneApi(session, Settings(), RateLimiter(Settings()))
    api._user_id = "42"
    return api


class LocationPayloadTest(unittest.TestCase):
    """定位签到的表单必须与官方页面一致。"""

    def test_payload_contains_course_and_student(self) -> None:
        api = make_api()
        payload = api._location_payload(COURSE, 116.5, 39.5)
        for fragment in (
            "action=signin",
            "cid=123",
            "tcid=456",
            "sid=42",
            "longitude=116.5",
            "latitude=39.5",
        ):
            self.assertIn(fragment, payload)

    def test_sign_by_location_sends_full_form(self) -> None:
        api = make_api()
        api.sign_by_location(COURSE, 116.5, 39.5)
        body = api._session.last_body()
        self.assertIn("cid=123", body)
        self.assertIn("tcid=456", body)

    def test_probe_distance_shares_same_form(self) -> None:
        api = make_api(
            {"/_CheckIn/CheckInRoomHandler.ashx": FakeResponse(text="距离：123.5米")}
        )
        distance = api.probe_distance(COURSE, 116.5, 39.5)
        self.assertEqual(distance, 123.5)
        body = api._session.last_body()
        self.assertIn("cid=123", body)
        self.assertIn("tcid=456", body)


class QrParsingTest(unittest.TestCase):
    """二维码相关的纯解析逻辑。"""

    def test_extract_data_uri(self) -> None:
        text = "data:image/png;base64," + "A" * 300
        self.assertEqual(len(DuifeneApi._extract_base64_image(text) or ""), 300)

    def test_extract_json_body(self) -> None:
        text = json.dumps({"msg": 1, "msgbox": "B" * 260})
        self.assertEqual(len(DuifeneApi._extract_base64_image(text) or ""), 260)

    def test_extract_empty(self) -> None:
        self.assertIsNone(DuifeneApi._extract_base64_image(""))
        self.assertIsNone(DuifeneApi._extract_base64_image("参数错误"))

    def test_state_from_full_url(self) -> None:
        api = make_api()
        url = "https://www.duifene.com/_CheckIn/MB/QrCodeCheckOK.aspx?state=AbC-1_2#x"
        self.assertEqual(api._state_from_qr_content(url), "AbC-1_2")

    def test_state_from_short_link_follows_redirect(self) -> None:
        api = make_api(
            {
                "/G.aspx": FakeResponse(
                    status_code=302,
                    headers={"Location": "/_CheckIn/MB/QrCodeCheckOK.aspx?state=TOK9"},
                )
            }
        )
        content = "https://www.duifene.com/G.aspx?OnlyId=abc"
        self.assertEqual(api._state_from_qr_content(content), "TOK9")

    def test_state_absent(self) -> None:
        api = make_api()
        self.assertIsNone(api._state_from_qr_content("hello"))

    def test_sign_by_qr_keeps_server_text(self) -> None:
        api = make_api(
            {
                "/QrCodeCheckOK.aspx": FakeResponse(
                    text='<div id="DivOK">参数错误,请重新尝试</div>'
                )
            }
        )
        self.assertEqual(api.sign_by_qr("2557759"), "参数错误,请重新尝试")


class CodeAndHanziSourceTest(unittest.TestCase):
    """签到码 / 汉字答案的三级取码兜底。"""

    def test_code_from_hidden_input(self) -> None:
        api = make_api(
            {
                "CheckInStudent.aspx": FakeResponse(
                    text='<input id="HFCheckCodeKey" value="8765" />'
                )
            }
        )
        self.assertEqual(api.fetch_code_from_entry(), "8765")

    def test_code_from_inline_script(self) -> None:
        api = make_api(
            {
                "CheckInStudent.aspx": FakeResponse(
                    text="<script>var HFCheckCodeKey = '2143';</script>"
                )
            }
        )
        self.assertEqual(api.fetch_code_from_entry(), "2143")

    def test_code_absent(self) -> None:
        api = make_api({"CheckInStudent.aspx": FakeResponse(text="<html></html>")})
        self.assertIsNone(api.fetch_code_from_entry())

    def test_hanzi_falls_back_to_entry_page(self) -> None:
        api = make_api(
            {
                "TeachCheckIn.aspx": FakeResponse(text='<input id="HFChecktype" />'),
                "CheckInStudent.aspx": FakeResponse(
                    text='<input id="HFCheckCodeKey" value="春眠不觉" />'
                ),
            }
        )
        self.assertEqual(api.fetch_hanzi_answer(COURSE), "春眠不觉")

    def test_hanzi_uses_teacher_page_when_present(self) -> None:
        api = make_api(
            {
                "TeachCheckIn.aspx": FakeResponse(
                    text='<input id="HFChecktype" value="4" />'
                    '<input id="HFCheckCodeKey" value="处处闻啼" />'
                )
            }
        )
        self.assertEqual(api.fetch_hanzi_answer(COURSE), "处处闻啼")


class LoginProbeTest(unittest.TestCase):
    """登录态探测的容错行为。"""

    def test_check_login_true(self) -> None:
        api = make_api({"/AppCode/LoginInfo.ashx": FakeResponse(text='{"msg":"1"}')})
        self.assertIs(api.check_login(), True)

    def test_check_login_false_on_empty_body(self) -> None:
        api = make_api({"/AppCode/LoginInfo.ashx": FakeResponse(text="")})
        self.assertIsNone(api.check_login())


if __name__ == "__main__":
    unittest.main()
