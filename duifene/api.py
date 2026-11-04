"""对分易 App REST 接口封装（外部 I/O）。

对分易在 2026 年改版后，学生端网页的签到接口（``/_CheckIn/CheckIn.ashx`` 等）
对普通账号一律返回「禁止访问」，四类签到因此全部失效。官方手机 App 使用的是
``dfapp.duifene.com`` 上的 JSON 接口，它仍然是完整可用的，本模块即围绕它实现：

* ``/Auth/login``、``/Auth/RefreshAccessToken`` —— Bearer Token 认证与续期；
* ``/Course/GetStudentCourseList``、``/Class/GetClassListByCourseID`` —— 课程与教学班；
* ``/CK/GetModelByInProgress/{courseId}`` —— 当前进行中的签到（含签到码与二维码令牌）；
* ``/CK/GetCheckIn/{id}``、``/CK/GetCheckInTotalByCheckInIds`` —— 详情与人数；
* ``/CK/room/GetCheckInRoomById/{roomId}`` —— 教室经纬度（定位签到的坐标来源）；
* ``/CK/StudentCheckIn``、``/CK/QrCodeCheckIn``、``/CK/CheckInSignInLocation``
  —— 签到码 / 汉字、二维码、定位三类提交。

所有请求都带 Bearer Token；401 时自动用 Refresh Token 换新令牌并重试一次。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import requests

from .config import IniStore
from .errors import AppApiError, AuthError, RateLimited
from .models import CheckInActivity, Course
from .settings import Settings

# 服务端令牌有效期（App 端实测：access 30 分钟、refresh 7 天）
ACCESS_TOKEN_LIFETIME_S = 30 * 60
REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 60 * 60
# 提前 5 分钟续期，避免边界上刚好过期
TOKEN_REFRESH_AHEAD_S = 5 * 60


@dataclass(frozen=True)
class Identity:
    """App 登录态中的当前用户。"""

    account: str
    user_id: str
    user_name: str
    user_type_id: str


def _brief(text: str, limit: int = 120) -> str:
    """压成单行短文本，用于把服务端原话带进异常/日志。"""
    return " ".join(str(text).split())[:limit]


class DuifeneApi:
    """持有 App Bearer Token，负责全部运行期请求。"""

    def __init__(
        self,
        session: requests.Session,
        settings: Settings,
        limiter: Any,
        store: IniStore | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._store = store
        self._auth_lock = threading.RLock()
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_expires_at = 0.0
        self._identity: Identity | None = None
        self._restore_session()

    # ─ 状态 ────────────────────────────────────────────────────────
    @property
    def limiter(self) -> Any:
        """限流状态机。"""
        return self._limiter

    @property
    def identity(self) -> Identity | None:
        """当前 App 用户信息。"""
        return self._identity

    @property
    def is_authenticated(self) -> bool:
        """是否持有可用 Token 与用户信息。"""
        with self._auth_lock:
            return bool(
                self._token
                and self._refresh_token
                and self._identity
                and self._refresh_expires_at > time.time()
            )

    def clear(self) -> None:
        """清空登录态（内存 + 配置文件中的会话段）。"""
        with self._auth_lock:
            self._clear_memory()
            if self._store is not None:
                self._store.clear_app_session()

    def _clear_memory(self) -> None:
        """只清内存中的登录态。"""
        self._token = None
        self._refresh_token = None
        self._token_expires_at = 0.0
        self._refresh_expires_at = 0.0
        self._identity = None

    # ─ 认证 ────────────────────────────────────────────────────────
    def login(self, username: str, password: str) -> tuple[bool, str]:
        """账号密码登录；返回 ``(是否成功, 服务端文案)``。"""
        with self._auth_lock:
            self._clear_memory()
        try:
            response = self._session.post(
                self._url("/Auth/login"),
                json={"username": str(username or ""), "password": str(password or "")},
                headers={"Content-Type": "application/json"},
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException as exc:
            self._restore_session()
            return False, f"网络请求失败（{exc.__class__.__name__}），请检查网络或代理"
        try:
            payload = response.json()
        except ValueError:
            self._restore_session()
            return False, f"登录失败：服务端返回了无法解析的数据（HTTP {response.status_code}）"
        if not isinstance(payload, Mapping):
            self._restore_session()
            return False, "登录失败：服务端返回了无效数据"
        if not payload.get("success") or str(payload.get("code")) != "200":
            message = _brief(payload.get("message") or "登录失败")
            self._restore_session()
            return False, message
        if not self._store_login_payload(payload, initial_login=True):
            self._restore_session()
            return False, "登录失败：响应里没有有效的登录令牌"
        return True, "登录成功"

    def ensure_session_fresh(self, force: bool = False) -> bool:
        """Token 临近过期时续期；返回本轮是否真的续期了。

        Raises:
            AuthError: 没有可用登录态，或续期被服务端拒绝。
        """
        with self._auth_lock:
            if (
                not self._token
                or not self._refresh_token
                or self._identity is None
                or self._refresh_expires_at <= time.time()
            ):
                self.clear()
                raise AuthError("登录状态已失效，请重新登录账号")
            if not force and time.time() < self._token_expires_at - TOKEN_REFRESH_AHEAD_S:
                return False
            return self._refresh_access_token()

    def _refresh_access_token(self) -> bool:
        """用 Refresh Token 换新 Token；仅服务端明确拒绝时清空登录态。"""
        with self._auth_lock:
            old_token = self._token
            refresh_token = self._refresh_token
            if not old_token or not refresh_token:
                self.clear()
                raise AuthError("登录状态已失效，请重新登录账号")
        try:
            response = self._session.get(
                self._url("/Auth/RefreshAccessToken"),
                params={"OldToken": old_token, "RefreshToken": refresh_token},
                headers={"Authorization": f"Bearer {old_token}"},
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException as exc:
            # 网络问题不代表凭证失效：保留令牌，下一轮再试
            raise AppApiError(f"登录状态续期失败（{exc.__class__.__name__}）") from exc
        if response.status_code in (400, 401, 403):
            self.clear()
            raise AuthError("登录状态已失效，请重新登录账号")
        if response.status_code != 200:
            raise AppApiError(f"登录状态续期失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppApiError("登录状态续期返回了无法解析的数据") from exc
        if not isinstance(payload, Mapping) or not self._store_login_payload(
            payload, initial_login=False
        ):
            self.clear()
            raise AuthError("登录状态已失效，请重新登录账号")
        return True

    def _store_login_payload(
        self, payload: Mapping[str, Any], *, initial_login: bool
    ) -> bool:
        """保存登录/续期响应中的令牌与用户信息。"""
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return False
        token = str(data.get("token") or data.get("accessToken") or "")
        if not token:
            return False
        now = time.time()
        with self._auth_lock:
            self._token = token
            self._token_expires_at = now + ACCESS_TOKEN_LIFETIME_S
            refreshed = str(data.get("refreshToken") or "")
            if refreshed:
                self._refresh_token = refreshed
                self._refresh_expires_at = now + REFRESH_TOKEN_LIFETIME_S
            elif initial_login or not self._refresh_token:
                return False
            user_info = data.get("userInfo")
            if isinstance(user_info, Mapping):
                user_id = str(user_info.get("userId") or "")
                account = str(user_info.get("userLogin") or "")
                if user_id and account:
                    self._identity = Identity(
                        account=account,
                        user_id=user_id,
                        user_name=str(user_info.get("userName") or ""),
                        user_type_id=str(user_info.get("userTypeID") or ""),
                    )
            if self._identity is None or not self._refresh_token:
                return False
            self._save_session()
            return True

    def _restore_session(self) -> None:
        """从配置文件恢复尚未过期的会话（重启免登录）。"""
        if self._store is None:
            return
        values = self._store.load_app_session()
        if not values:
            return
        try:
            token_expires_at = float(values.get("access_expires_at") or 0)
            refresh_expires_at = float(values.get("refresh_expires_at") or 0)
        except ValueError:
            self._store.clear_app_session()
            return
        token = str(values.get("access_token") or "")
        refresh_token = str(values.get("refresh_token") or "")
        account = str(values.get("account") or "")
        user_id = str(values.get("user_id") or "")
        user_type_id = str(values.get("user_type_id") or "")
        if (
            not token
            or not refresh_token
            or not account
            or not user_id
            or not user_type_id
            or refresh_expires_at <= time.time()
        ):
            self._store.clear_app_session()
            return
        with self._auth_lock:
            self._token = token
            self._refresh_token = refresh_token
            self._token_expires_at = token_expires_at
            self._refresh_expires_at = refresh_expires_at
            self._identity = Identity(
                account=account,
                user_id=user_id,
                user_name=str(values.get("user_name") or ""),
                user_type_id=user_type_id,
            )

    def _save_session(self) -> None:
        """把当前会话写入配置文件。"""
        if self._store is None or self._identity is None:
            return
        if not self._token or not self._refresh_token:
            return
        self._store.save_app_session(
            {
                "account": self._identity.account,
                "user_id": self._identity.user_id,
                "user_name": self._identity.user_name,
                "user_type_id": self._identity.user_type_id,
                "access_token": self._token,
                "access_expires_at": f"{self._token_expires_at:.3f}",
                "refresh_token": self._refresh_token,
                "refresh_expires_at": f"{self._refresh_expires_at:.3f}",
            }
        )

    # ─ 基础请求 ────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        """拼接 App 接口完整地址。"""
        return self._settings.app_host.rstrip("/") + path

    def _require_identity(self) -> Identity:
        """取当前用户，未登录即抛错。"""
        if self._identity is None:
            raise AuthError("登录状态已失效，请重新登录账号")
        return self._identity

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Mapping[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """带 Bearer Token 发起请求；401 时续期并重试一次。"""
        self.ensure_session_fresh()
        with self._auth_lock:
            token = self._token
        if not token:
            raise AuthError("登录状态已失效，请重新登录账号")
        try:
            response = self._session.request(
                method,
                self._url(path),
                json=json_data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException as exc:
            raise AppApiError(
                f"网络请求失败（{exc.__class__.__name__}），请检查网络或代理"
            ) from exc
        if response.status_code == 401 and allow_refresh:
            self.ensure_session_fresh(force=True)
            return self._request(
                method, path, json_data=json_data, allow_refresh=False
            )
        if response.status_code == 401:
            self.clear()
            raise AuthError("登录状态已失效，请重新登录账号")
        if response.status_code != 200:
            raise AppApiError(f"请求失败（HTTP {response.status_code}）：{path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppApiError("服务端返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise AppApiError("服务端返回了无效数据")
        return payload

    @staticmethod
    def _require_success(payload: Mapping[str, Any], fallback: str) -> None:
        """响应 success 为假时抛出带服务端原因的异常。"""
        if payload.get("success"):
            return
        message = _brief(payload.get("message") or "")
        raise AppApiError(message or fallback)

    # ─ 课程 ────────────────────────────────────────────────────────
    def fetch_courses(self) -> list[Course]:
        """读取当前用户的课程与教学班。

        教学班接口失败不致命：签到所需的是课程 ID，教学班只用于界面区分，
        因此退化为「仅课程」而不是整体失败。
        """
        identity = self._require_identity()
        path = {
            "1": "/Course/GetTeacherCourseList",
            "2": "/Course/GetStudentCourseList",
            "3": "/Course/GetAssistantCourseList",
        }.get(identity.user_type_id, "/Course/GetStudentCourseList")
        payload = self._request("GET", path)
        self._require_success(payload, "未返回课程列表")
        data = payload.get("data")
        raw_courses = data.get("courses") if isinstance(data, Mapping) else None
        if not isinstance(raw_courses, list):
            raise AppApiError("课程数据格式异常")

        courses: list[Course] = []
        for raw in raw_courses:
            if not isinstance(raw, Mapping):
                continue
            course_id = str(raw.get("courseID") or "")
            course_name = str(raw.get("courseName") or "").strip()
            if not course_id or course_id == "0":
                continue
            classes = self._fetch_classes(course_id, identity.user_type_id)
            if not classes:
                courses.append(Course(course_id=course_id, class_id="0", name=course_name))
                continue
            for class_id, class_name in classes:
                display = course_name
                if len(classes) > 1 and class_name:
                    display = f"{course_name} · {class_name}"
                courses.append(
                    Course(course_id=course_id, class_id=class_id, name=display)
                )
        return courses

    def _fetch_classes(self, course_id: str, user_type_id: str) -> list[tuple[str, str]]:
        """读取课程下当前用户的教学班；失败返回空列表（不阻断签到）。"""
        try:
            payload = self._request(
                "GET",
                "/Class/GetClassListByCourseID/"
                f"{quote(str(course_id), safe='')}/{quote(str(user_type_id), safe='')}",
            )
        except AppApiError:
            return []
        if not payload.get("success"):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        classes: list[tuple[str, str]] = []
        for raw in data:
            if not isinstance(raw, Mapping):
                continue
            class_id = str(raw.get("classId") or "")
            if not class_id or class_id == "0":
                continue
            classes.append((class_id, str(raw.get("className") or "")))
        return classes

    # ─ 签到数据 ────────────────────────────────────────────────────
    @staticmethod
    def _is_empty_activity(payload: Mapping[str, Any]) -> bool:
        """响应是否表示「当前没有进行中的签到」（而非真正的接口错误）。

        服务端在没有活动时既可能返回 success=true + data=null，也可能返回
        success=false + 「没有进行中的签到」。把它当错误会每 3 秒刷一条异常，
        因此在轮询路径上必须识别出来。
        """
        if payload.get("success"):
            data = payload.get("data")
            return not isinstance(data, Mapping) or not data
        message = str(payload.get("message") or "")
        return any(
            marker in message
            for marker in ("没有", "暂无", "不存在", "未找到", "无进行中", "未开始")
        )

    def fetch_active_activity(self, course_id: str) -> CheckInActivity | None:
        """读取该课程当前进行中的签到；没有活动返回 None。"""
        payload = self._request(
            "GET", f"/CK/GetModelByInProgress/{quote(str(course_id), safe='')}"
        )
        data = payload.get("data")
        if isinstance(data, Mapping) and data:
            activity = CheckInActivity.from_payload(data)
            if activity is not None:
                return activity
        if self._is_empty_activity(payload):
            return None
        self._require_success(payload, "读取当前签到失败")
        return None

    def fetch_activity(self, activity_id: str) -> CheckInActivity | None:
        """按活动 ID 读取签到详情（用于补齐签到码/二维码令牌）。"""
        payload = self._request(
            "GET", f"/CK/GetCheckIn/{quote(str(activity_id), safe='')}"
        )
        data = payload.get("data")
        if isinstance(data, Mapping) and data:
            return CheckInActivity.from_payload(data)
        if self._is_empty_activity(payload):
            return None
        self._require_success(payload, "读取签到详情失败")
        return None

    def fetch_headcount(self, activity_id: str) -> tuple[int, int]:
        """读取签到人数 ``(已参与, 总人数)``。"""
        payload = self._request(
            "POST",
            "/CK/GetCheckInTotalByCheckInIds",
            json_data={"checkInIds": [int(activity_id)]},
        )
        self._require_success(payload, "读取签到人数失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AppApiError("签到人数数据格式异常")
        try:
            total = int(data.get("totalNumber") or 0)
            absence = int(data.get("absenceNumber") or 0)
        except (TypeError, ValueError) as exc:
            raise AppApiError("签到人数数据格式异常") from exc
        return max(total - absence, 0), total

    def fetch_room_coordinates(
        self, activity: CheckInActivity
    ) -> tuple[float, float] | None:
        """读取签到绑定教室的经纬度，作为定位签到的坐标来源。

        ``roomID`` 缺失时先拉一次签到详情再试；仍没有则返回 None。
        """
        room_id = activity.room_id
        if not room_id or room_id == "0":
            detail = self.fetch_activity(activity.id)
            room_id = detail.room_id if detail is not None else ""
        if not room_id or room_id == "0":
            return None
        payload = self._request(
            "GET", f"/CK/room/GetCheckInRoomById/{quote(str(room_id), safe='')}"
        )
        self._require_success(payload, "读取教室坐标失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AppApiError("教室坐标数据格式异常")
        try:
            return float(data["longitude"]), float(data["latitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppApiError("教室坐标数据格式异常") from exc

    # ─ 四类签到 ────────────────────────────────────────────────────
    def sign_by_code(self, code: str) -> str:
        """提交 4 位签到码或 4 个汉字（同一接口）。"""
        return self._sign("/CK/StudentCheckIn", {"checkInCode": str(code)})

    def sign_by_qr(self, token: str) -> str:
        """提交二维码令牌。"""
        return self._sign("/CK/QrCodeCheckIn", {"checkInCode": str(token)})

    def sign_by_location(self, longitude: float, latitude: float) -> str:
        """提交定位签到坐标。"""
        return self._sign(
            "/CK/CheckInSignInLocation",
            {"latitude": latitude, "longitude": longitude},
        )

    def _sign(self, path: str, data: Mapping[str, Any]) -> str:
        """提交签到并返回服务端文案；命中限流时进入静默并抛 :class:`RateLimited`。"""
        payload = self._request("POST", path, json_data=data)
        message = payload.get("message")
        if message is None:
            message = payload.get("msgbox")
        if message is None:
            if payload.get("success"):
                return "签到成功"
            raise AppApiError("签到请求失败（服务端未返回结果）")
        text = str(message)
        if self._limiter is not None and self._limiter.is_limit_text(text):
            self._limiter.trip()
            raise RateLimited(
                f"服务端提示签到过于频繁，已静默 "
                f"{int(self._settings.cooldown_seconds)} 秒"
            )
        return text


__all__ = ["DuifeneApi", "Identity"]
