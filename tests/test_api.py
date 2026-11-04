"""DuifeneApi 的离线单元测试：请求构造、响应解析与令牌续期，全部不发真实请求。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from duifene.api import DuifeneApi
from duifene.config import IniStore
from duifene.errors import AppApiError, RateLimited
from duifene.models import CheckInActivity
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings

LOGIN_OK = {
    "code": 200,
    "success": True,
    "message": "登录成功",
    "data": {
        "token": "ACCESS-1",
        "refreshToken": "REFRESH-1",
        "userInfo": {
            "userId": "9527",
            "userLogin": "20210001",
            "userName": "张三",
            "userTypeID": "2",
        },
    },
}


class FakeResponse:
    """最小可用的响应替身。"""

    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按「方法 + 路径后缀」路由的会话替身，并记录所有请求。"""

    def __init__(self, routes: list[tuple[str, str, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, url, json=None, headers=None, **kwargs):
        self.calls.append((method, url, json))
        return self._dispatch(method, url, json)

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append(("GET", url, params))
        return self._dispatch("GET", url, params)

    def post(self, url, json=None, headers=None, **kwargs):
        self.calls.append(("POST", url, json))
        return self._dispatch("POST", url, json)

    def _dispatch(self, method: str, url: str, body):
        for want_method, suffix, reply in self.routes:
            if want_method == method and url.endswith(suffix):
                return reply(method, url, body) if callable(reply) else reply
        raise AssertionError(f"未预期的请求：{method} {url}")

    def body_of(self, method: str, suffix: str):
        for call_method, url, body in self.calls:
            if call_method == method and url.endswith(suffix):
                return body
        raise AssertionError(f"没有发出 {method} {suffix}")


class ApiTestBase(unittest.TestCase):
    """提供「已登录」的 api 与临时配置文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = IniStore(str(Path(self._tmp.name) / "duifenyi.ini"))
        self.settings = Settings()
        self.limiter = RateLimiter(self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_api(self, routes, *, login: bool = True) -> DuifeneApi:
        session = FakeSession(routes)
        self.session = session
        api = DuifeneApi(session, self.settings, self.limiter, self.store)
        if login:
            ok, message = api.login("20210001", "secret")
            self.assertTrue(ok, message)
            session.calls.clear()
        return api


def payload_with_checkin(**check_in) -> dict:
    """构造一个「有进行中签到」的响应体。"""
    body = {
        "id": 2557759,
        "checkInType": 1,
        "checkInCode": "1234",
        "roomID": 88,
        "tClassID": 21,
        "createrDate": "2026-11-04T13:00:00",
        "validDate": "2026-11-04T13:05:00",
        "checkInStatus": 0,
        "statusID": 0,
        "statusName": "缺勤",
    }
    body.update(check_in)
    return {"success": True, "data": {"checkIn": body}}


class LoginTest(ApiTestBase):
    def test_login_success_saves_session(self) -> None:
        api = self.make_api([("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK))])
        self.assertTrue(api.is_authenticated)
        self.assertEqual(api.identity.account, "20210001")
        self.assertEqual(api.identity.user_type_id, "2")
        saved = self.store.load_app_session()
        self.assertEqual(saved["access_token"], "ACCESS-1")
        self.assertEqual(saved["refresh_token"], "REFRESH-1")

    def test_login_failure_returns_server_message(self) -> None:
        session = FakeSession(
            [
                (
                    "POST",
                    "/Auth/login",
                    FakeResponse(
                        payload={
                            "code": 400,
                            "success": False,
                            "message": "用户名或密码错误！",
                        }
                    ),
                )
            ]
        )
        api = DuifeneApi(session, self.settings, self.limiter, self.store)
        ok, message = api.login("20210001", "wrong")
        self.assertFalse(ok)
        self.assertEqual(message, "用户名或密码错误！")
        self.assertFalse(api.is_authenticated)

    def test_login_network_error_is_reported(self) -> None:
        import requests

        def boom(*_args, **_kwargs):
            raise requests.exceptions.ConnectTimeout("timeout")

        session = FakeSession([("POST", "/Auth/login", boom)])
        api = DuifeneApi(session, self.settings, self.limiter, self.store)
        ok, message = api.login("20210001", "secret")
        self.assertFalse(ok)
        self.assertIn("网络请求失败", message)

    def test_persisted_session_is_restored(self) -> None:
        self.store.save_app_session(
            {
                "account": "20210001",
                "user_id": "9527",
                "user_name": "张三",
                "user_type_id": "2",
                "access_token": "ACCESS-1",
                "access_expires_at": "9999999999",
                "refresh_token": "REFRESH-1",
                "refresh_expires_at": "9999999999",
            }
        )
        api = DuifeneApi(FakeSession([]), self.settings, self.limiter, self.store)
        self.assertTrue(api.is_authenticated)
        self.assertEqual(api.identity.user_id, "9527")


class SignTest(ApiTestBase):
    def test_sign_by_code_payload(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/StudentCheckIn",
                    FakeResponse(payload={"success": True, "message": "签到成功！"}),
                ),
            ]
        )
        self.assertEqual(api.sign_by_code("1234"), "签到成功！")
        self.assertEqual(
            self.session.body_of("POST", "/CK/StudentCheckIn"), {"checkInCode": "1234"}
        )

    def test_sign_by_qr_payload(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/QrCodeCheckIn",
                    FakeResponse(payload={"success": True, "message": "签到成功"}),
                ),
            ]
        )
        self.assertEqual(api.sign_by_qr("TOKEN+/="), "签到成功")
        self.assertEqual(
            self.session.body_of("POST", "/CK/QrCodeCheckIn"),
            {"checkInCode": "TOKEN+/="},
        )

    def test_sign_by_location_payload(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/CheckInSignInLocation",
                    FakeResponse(payload={"success": True, "message": "签到成功"}),
                ),
            ]
        )
        self.assertEqual(api.sign_by_location(116.397, 39.909), "签到成功")
        self.assertEqual(
            self.session.body_of("POST", "/CK/CheckInSignInLocation"),
            {"latitude": 39.909, "longitude": 116.397},
        )

    def test_rate_limit_message_trips_limiter(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/StudentCheckIn",
                    FakeResponse(payload={"success": False, "message": "操作过于频繁"}),
                ),
            ]
        )
        with self.assertRaises(RateLimited):
            api.sign_by_code("1234")
        self.assertTrue(api.limiter.is_blocking())

    def test_server_error_message_is_returned(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/StudentCheckIn",
                    FakeResponse(payload={"success": False, "message": "签到已结束"}),
                ),
            ]
        )
        self.assertEqual(api.sign_by_code("1234"), "签到已结束")


class QueryTest(ApiTestBase):
    def test_fetch_courses_parses_classes(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/Course/GetStudentCourseList",
                    FakeResponse(
                        payload={
                            "success": True,
                            "data": {
                                "courses": [{"courseID": 11, "courseName": "高等数学"}]
                            },
                        }
                    ),
                ),
                (
                    "GET",
                    "/Class/GetClassListByCourseID/11/2",
                    FakeResponse(
                        payload={
                            "success": True,
                            "data": [
                                {"classId": 21, "className": "1 班"},
                                {"classId": 22, "className": "2 班"},
                            ],
                        }
                    ),
                ),
            ]
        )
        courses = api.fetch_courses()
        self.assertEqual([c.class_id for c in courses], ["21", "22"])
        self.assertEqual(courses[0].name, "高等数学 · 1 班")

    def test_fetch_courses_survives_missing_classes(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/Course/GetStudentCourseList",
                    FakeResponse(
                        payload={
                            "success": True,
                            "data": {
                                "courses": [{"courseID": "11", "courseName": "线性代数"}]
                            },
                        }
                    ),
                ),
                (
                    "GET",
                    "/Class/GetClassListByCourseID/11/2",
                    FakeResponse(payload={"success": False, "message": "无教学班"}),
                ),
            ]
        )
        courses = api.fetch_courses()
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].class_id, "0")

    def test_fetch_active_activity_parses_code(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/CK/GetModelByInProgress/11",
                    FakeResponse(payload=payload_with_checkin()),
                ),
            ]
        )
        activity = api.fetch_active_activity("11")
        self.assertIsNotNone(activity)
        self.assertEqual(activity.id, "2557759")
        self.assertEqual(activity.type, "1")
        self.assertEqual(activity.code, "1234")
        self.assertEqual(activity.room_id, "88")
        self.assertFalse(activity.attended)

    def test_fetch_active_activity_qr_token(self) -> None:
        payload = payload_with_checkin(checkInType=2, checkInCode="")
        payload["data"]["qrCode"] = "QR-TOKEN"
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                ("GET", "/CK/GetModelByInProgress/11", FakeResponse(payload=payload)),
            ]
        )
        activity = api.fetch_active_activity("11")
        self.assertEqual(activity.qr_token, "QR-TOKEN")

    def test_fetch_active_activity_empty_payload_returns_none(self) -> None:
        for payload in (
            {"success": True, "data": None},
            {"success": True, "data": {}},
            {"success": True, "data": {"checkIn": {"id": 0}}},
            {"success": False, "message": "当前没有进行中的签到"},
        ):
            api = self.make_api(
                [
                    ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                    ("GET", "/CK/GetModelByInProgress/11", FakeResponse(payload=payload)),
                ]
            )
            self.assertIsNone(api.fetch_active_activity("11"), payload)

    def test_fetch_active_activity_real_error_raises(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/CK/GetModelByInProgress/11",
                    FakeResponse(payload={"success": False, "message": "参数错误"}),
                ),
            ]
        )
        with self.assertRaises(AppApiError):
            api.fetch_active_activity("11")

    def test_fetch_headcount_counts_absence(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "POST",
                    "/CK/GetCheckInTotalByCheckInIds",
                    FakeResponse(
                        payload={
                            "success": True,
                            "data": {"totalNumber": 40, "absenceNumber": 10},
                        }
                    ),
                ),
            ]
        )
        self.assertEqual(api.fetch_headcount("77"), (30, 40))
        self.assertEqual(
            self.session.body_of("POST", "/CK/GetCheckInTotalByCheckInIds"),
            {"checkInIds": [77]},
        )

    def test_fetch_room_coordinates(self) -> None:
        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/CK/room/GetCheckInRoomById/88",
                    FakeResponse(
                        payload={
                            "success": True,
                            "data": {"longitude": 116.397, "latitude": 39.909},
                        }
                    ),
                ),
            ]
        )
        activity = CheckInActivity(
            id="1",
            type="3",
            code="",
            qr_token="",
            room_id="88",
            class_id="21",
            create_date="",
            valid_date="",
            check_in_status="0",
            status_id="0",
            status_name="缺勤",
        )
        self.assertEqual(api.fetch_room_coordinates(activity), (116.397, 39.909))


class TokenRefreshTest(ApiTestBase):
    def test_401_triggers_refresh_then_retry(self) -> None:
        state = {"sign_calls": 0}

        def sign_reply(method, url, body):
            state["sign_calls"] += 1
            if state["sign_calls"] == 1:
                return FakeResponse(status_code=401, payload=None)
            return FakeResponse(payload={"success": True, "message": "签到成功"})

        api = self.make_api(
            [
                ("POST", "/Auth/login", FakeResponse(payload=LOGIN_OK)),
                (
                    "GET",
                    "/Auth/RefreshAccessToken",
                    FakeResponse(
                        payload={
                            "code": 200,
                            "success": True,
                            "data": {
                                "token": "ACCESS-2",
                                "refreshToken": "REFRESH-2",
                                "userInfo": {
                                    "userId": "9527",
                                    "userLogin": "20210001",
                                    "userTypeID": "2",
                                },
                            },
                        }
                    ),
                ),
                ("POST", "/CK/StudentCheckIn", sign_reply),
            ]
        )
        self.assertEqual(api.sign_by_code("1234"), "签到成功")
        self.assertEqual(state["sign_calls"], 2)
        self.assertEqual(self.store.load_app_session()["access_token"], "ACCESS-2")


if __name__ == "__main__":
    unittest.main()
