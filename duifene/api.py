"""对分易 HTTP 接口封装（外部 I/O）。"""

from __future__ import annotations

import json
import random
import re
from typing import Any
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

from . import qrdecode
from .errors import AuthError, RateLimited
from .models import CheckInActivity, Course
from .rate_limit import RateLimiter
from .settings import Settings

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
AJAX_HEADERS = {**FORM_HEADERS, "X-Requested-With": "XMLHttpRequest"}
STUDENT_ENTRY_PATH = "/_CheckIn/MB/CheckInStudent.aspx?moduleid=16&pasd="
STUDENT_REFERER = f"https://www.duifene.com{STUDENT_ENTRY_PATH}"
DISTANCE_RE = re.compile(r"距离[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*米")


def _strip_data_uri(value: str) -> str:
    """去掉 ``data:image/png;base64,`` 前缀与所有空白，返回纯 base64。"""
    if value.startswith("data:"):
        value = value.partition(",")[2]
    return "".join(value.split())


def _brief(text: str, limit: int = 60) -> str:
    """压成单行短文本，用于把服务端原话带进日志。"""
    return " ".join(str(text).split())[:limit]
# 二维码内容里的令牌：state= 最常见，其余为兼容形态；G.aspx?OnlyId= 为短链形态
QR_PARAM_RE = re.compile(
    r"[?&](state|key|code|token|sign|signid|id)=([^\s&'\"#]+)", re.IGNORECASE
)
QR_ONLYID_RE = re.compile(r"[?&]OnlyId=([^\s&'\"#]+)", re.IGNORECASE)
# 图像响应中的 base64 主体（data URI 前缀另作处理）
BASE64_RE = re.compile(r"data:image/[a-zA-Z+]+;base64,([A-Za-z0-9+/=\s]+)")
RAW_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")
# 考勤入口页里可能承载签到码／汉字答案的隐藏域 id（按优先级）
CODE_ELEMENT_IDS = ("HFCheckCodeKey", "HFCheckCode", "hidCheckCode", "CheckInCode")
# JS 变量赋值形态的兜底正则
CODE_SCRIPT_RE = re.compile(
    r"(?:HFCheckCodeKey|checkincode)\s*=\s*['\"]([^'\"]{2,8})['\"]", re.IGNORECASE
)


class DuifeneApi:
    """持有注入的 `requests.Session`，负责所有网络往返与响应解析。

    签到类请求统一经 :meth:`_sign_post` 以施加限流约束。
    """

    def __init__(
        self,
        session: requests.Session,
        settings: Settings,
        limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._user_id: str | None = None
        # 已预访过考勤入口的教学班：预访一次即可，避免每轮重复请求
        self._primed_class: str | None = None
        # 最近一次取二维码令牌的失败原因，供上层原样打印，避免「只报参数错误」
        self._last_qr_error = ""

    @property
    def proxies(self) -> dict[str, str] | None:
        """底层会话使用的代理配置（供 WebSocket 复用）。"""
        return self._session.proxies or None

    @property
    def verify(self) -> bool:
        """底层会话的 TLS 校验开关（供 WebSocket 复用）。"""
        return bool(self._session.verify)

    @property
    def cached_user_id(self) -> str | None:
        """已缓存的学生 ID；未取过则为 None（不触发网络）。"""
        return self._user_id

    @property
    def limiter(self) -> RateLimiter:
        """限流状态机。"""
        return self._limiter

    @property
    def qr_error(self) -> str:
        """最近一次取二维码令牌失败的原因（成功时为空串）。"""
        return self._last_qr_error

    def _url(self, path: str) -> str:
        """拼接完整 URL。"""
        return self._settings.host + path

    def clear_session(self) -> None:
        """清空 Cookie 并作废缓存的学生 ID 与预访标记。"""
        self._session.cookies.clear()
        self._user_id = None
        self._primed_class = None

    def login_by_link(self, link_code: str) -> requests.Response:
        """用微信回调 code 登录；新 Cookie 由 Set-Cookie 落到 session 的 cookie jar。"""
        self.clear_session()
        return self._session.get(
            self._url(f"/P.aspx?authtype=1&code={link_code}&state=1"), timeout=10
        )

    def login_by_password(self, username: str, password: str) -> str:
        """用账号密码登录；返回服务端 msgbox 文案，请求失败返回空串。"""
        headers = {**FORM_HEADERS, "Referer": f"{self._settings.host}/AppGate.aspx"}
        payload = f"action=loginmb&loginname={username}&password={password}"
        self.clear_session()
        self._session.get(self._settings.host, timeout=10)
        response = self._session.post(
            self._url("/AppCode/LoginInfo.ashx"),
            data=payload,
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return ""
        try:
            return response.json().get("msgbox", "")
        except ValueError:
            return ""

    def fetch_courses(self) -> list[dict[str, Any]]:
        """拉取学生课程列表；请求失败返回空列表。

        Raises:
            AuthError: 接口以 msgbox 形式返回错误（通常为登录失效）。
        """
        headers = {
            **FORM_HEADERS,
            "Referer": f"{self._settings.host}/_UserCenter/PC/CenterStudent.aspx",
        }
        response = self._session.post(
            self._url("/_UserCenter/CourseInfo.ashx"),
            data="action=getstudentcourse&classtypeid=2",
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        if isinstance(data, list):
            return data
        message = (
            data.get("msgbox", "登录失效") if isinstance(data, dict) else "登录失效"
        )
        raise AuthError(message)

    def check_login(self) -> bool | None:
        """检查登录态：True 已登录 / False 已失效 / None 网络失败（无法判定）。

        用 POST 提交 ``Action``：该处理程序只从表单读参数，GET 带 body 属未定义
        行为（部分网关会丢弃 body），表现为把有效会话误判为失效。
        """
        headers = {
            **FORM_HEADERS,
            "Referer": f"{self._settings.host}/_UserCenter/PC/CenterStudent.aspx",
        }
        try:
            response = self._session.post(
                self._url("/AppCode/LoginInfo.ashx"),
                data="Action=checklogin",
                headers=headers,
                timeout=self._settings.request_timeout_s,
            )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (requests.exceptions.RequestException, ValueError):
            return None
        return isinstance(payload, dict) and str(payload.get("msg")) == "1"

    def fetch_user_id(self) -> str | None:
        """获取并缓存学生 ID（会话内不变，取到后复用）。"""
        if self._user_id:
            return self._user_id
        response = self._session.get(
            self._url("/_UserCenter/MB/index.aspx"),
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return None
        element = BeautifulSoup(response.text, "html.parser").find(id="hidUID")
        if element is not None:
            self._user_id = element.get("value")
        return self._user_id

    def _require_user_id(self) -> str:
        """取学生 ID，取不到即抛错，绝不允许 ``None`` 拼入签到参数。

        轮询每轮开头已确认过 ID，这里防的是轮次中途会话被清理（登录失效
        竞态）后的空参数提交：``sid=None`` 会被服务端当作非法请求，返回
        文案还会被 ``_report`` 误判为普通失败。

        Raises:
            AuthError: 无法获取学生 ID，需重新登录。
        """
        user_id = self.fetch_user_id()
        if not user_id:
            raise AuthError("无法获取学生 ID，请重新登录")
        return user_id

    def fetch_activities(
        self, class_id: str, user_id: str
    ) -> list[CheckInActivity] | None:
        """拉取当日签到活动列表；接口异常返回 None。"""
        headers = {
            **FORM_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._settings.host}/_CheckIn/PC/StudentNoCheckCount.aspx",
        }
        response = self._session.post(
            self._url("/_CheckIn/MBCount.ashx"),
            data={
                "action": "getstudentinlogbyday",
                "classid": class_id,
                "studentid": user_id,
            },
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        # msg 可能是字符串 "1" 也可能是数字 1，统一按字符串比；非 dict 直接判失败
        if not isinstance(data, dict) or str(data.get("msg")) != "1":
            return None
        rows = data.get("rows") or []
        if not isinstance(rows, list):
            return None
        return [CheckInActivity.from_raw(row) for row in rows]

    def fetch_headcount(self, check_in_id: str) -> tuple[int, int] | None:
        """查询签到人数 `(已签到, 总人数)`；失败返回 None。

        已签到 = 总数 − 缺勤（迟到、请假亦视为已参与）。
        """
        headers = {
            **FORM_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._settings.host}/_CheckIn/PC/StudentNoCheckCount.aspx",
        }
        try:
            response = self._session.post(
                self._url("/_CheckIn/MBCount.ashx"),
                data={
                    "action": "getcheckintotalbyciid",
                    "ciid": check_in_id,
                    "t": "cking",
                },
                headers=headers,
                timeout=self._settings.request_timeout_s,
            )
            if response.status_code != 200:
                return None
            data = response.json()
            total = int(data.get("TotalNumber", 0))
            absence = int(data.get("AbsenceNumber", total))
            return max(total - absence, 0), total
        except (
            requests.exceptions.RequestException,
            TypeError,
            ValueError,
            AttributeError,
        ):
            return None

    @staticmethod
    def _element_value(soup: BeautifulSoup, element_id: str) -> str:
        """读取隐藏域的 value；元素缺失或无 value 时返回空串。"""
        element = soup.find(id=element_id)
        if element is None:
            return ""
        return str(element.get("value") or "").strip()

    def fetch_hanzi_answer(self, course: Course) -> str | None:
        """读取当前汉字签到的答案（4 个汉字，顺序已正确）。

        答案只可能来自两处：教师端页面的 ``HFCheckCodeKey``（会话具备教师权限
        时才有值）、考勤入口页内嵌的隐藏域／内联脚本。两者都取不到时返回 None，
        由调用方给出明确提示并重试，绝不静默。

        Returns:
            按正确顺序排列的 4 个汉字；非汉字签到、无进行中活动或异常返回 None。
        """
        headers = {
            **FORM_HEADERS,
            "Referer": f"{self._settings.host}/_CheckIn/PC/TeachCheckInList.aspx",
        }
        try:
            response = self._session.post(
                self._url("/_CheckIn/PC/TeachCheckIn.aspx"),
                data={"courseid": course.course_id, "moduleid": "7", "classtype": "1"},
                headers=headers,
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException:
            response = None
        if response is not None and response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            kind = self._element_value(soup, "HFChecktype")
            answer = self._element_value(soup, "HFCheckCodeKey")
            # 教师端仅在自身为汉字签到时给答案；无权限时 value 为空
            if (not kind or kind == "4") and len(answer) == 4:
                return answer
        answer = self.fetch_code_from_entry()
        return answer if answer and len(answer) == 4 else None

    def fetch_code_from_entry(self) -> str | None:
        """从考勤入口页抓取 4 位签到码／汉字答案。

        平台改版后不再把 ``CheckInCode`` 放进当日签到列表，入口页的隐藏域与
        内联脚本成为学生侧唯一可能来源。取不到返回 None（由调用方提示）。
        """
        try:
            response = self._session.get(
                self._url(STUDENT_ENTRY_PATH), timeout=self._settings.request_timeout_s
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        for element_id in CODE_ELEMENT_IDS:
            value = self._element_value(soup, element_id)
            if len(value) == 4:
                return value
        match = CODE_SCRIPT_RE.search(response.text)
        if match:
            value = match.group(1).strip()
            if len(value) == 4:
                return value
        return None

    def prime_checkin_entry(self, course: Course) -> None:
        """按官方客户端路径预访考勤入口，建立模块会话上下文（每班级一次）。

        服务端在直签时校验 Referer 与会话上下文，冷会话直接提交常被判参数错误。
        """
        if self._primed_class == course.class_id:
            return
        self._primed_class = course.class_id
        for path in (f"/Module.aspx?data={course.course_id}", STUDENT_ENTRY_PATH):
            try:
                self._session.get(
                    self._url(path), timeout=self._settings.request_timeout_s
                )
            except requests.exceptions.RequestException:
                return

    def _sign_post(
        self, path: str, payload: str, timeout: float | None = None
    ) -> requests.Response:
        """发起签到类请求并施加限流约束。

        Raises:
            RateLimited: 处于静默期（不发送请求），或响应命中限流关键字
                （此时已进入静默）。
        """
        self._limiter.raise_if_blocking()
        response = self._session.post(
            self._url(path),
            data=payload,
            headers={**AJAX_HEADERS, "Referer": STUDENT_REFERER},
            timeout=timeout or self._settings.request_timeout_s,
        )
        self._guard_limit_text(response)
        return response

    def _guard_limit_text(self, response: requests.Response) -> None:
        """响应命中限流关键字时进入静默并抛错，保证调用方停止继续提交。

        Raises:
            RateLimited: 服务端提示过于频繁（此时已进入静默）。
        """
        if self._limiter.is_limit_text(response.text):
            self._limiter.trip()
            raise RateLimited(
                f"服务端提示签到过于频繁，已静默 "
                f"{int(self._settings.cooldown_seconds)} 秒"
            )

    def sign_by_code(self, code: str) -> str | None:
        """提交 4 位签到码；异常见 :meth:`_sign_post` 与 :meth:`_require_user_id`。"""
        user_id = self._require_user_id()
        response = self._sign_post(
            "/_CheckIn/CheckIn.ashx",
            f"action=studentcheckin&studentid={user_id}&checkincode={code}",
        )
        if response.status_code != 200:
            return None
        try:
            return response.json().get("msgbox", "")
        except ValueError:
            return None

    def sign_by_qr(self, state: str) -> str:
        """提交二维码签到；``state`` 必须是服务端当前认可的令牌。

        服务端改版后只认教师端滚动令牌，活动 ID 直签会被判「参数错误」。这里
        原样返回回执文案，由调用方决定重试或报错——不再把参数问题猜测成「非微信
        登录」，那会把真正的失败原因藏起来。
        """
        self._limiter.raise_if_blocking()
        # 令牌是 base64 形态，可能含 + / =，不编码会被服务端截断参数
        encoded = quote(str(state), safe="")
        response = self._session.get(
            self._url(f"/_CheckIn/MB/QrCodeCheckOK.aspx?state={encoded}"),
            timeout=self._settings.request_timeout_s,
        )
        self._guard_limit_text(response)
        if response.status_code != 200:
            return "二维码签到请求失败"
        element = BeautifulSoup(response.text, "html.parser").find(id="DivOK")
        return element.get_text().strip() if element is not None else ""

    def fetch_qr_state(self, course: Course) -> str | None:
        """取教师端当前实时二维码并解码出 state 令牌；失败返回 None。

        令牌每十几秒轮换，只能现取现用。失败原因写入 :attr:`qr_error`：活动已结束、
        响应无图、解码失败、内容无令牌这四种处置完全不同，笼统报「参数错误」会把
        真正的环节藏起来。
        """
        self._last_qr_error = ""
        try:
            response = self._session.post(
                self._url("/_CheckIn/CheckIn.ashx"),
                data=f"action=getcodeimage&cid={course.course_id}",
                headers={**AJAX_HEADERS, "Referer": STUDENT_REFERER},
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException as exc:
            self._last_qr_error = f"取图请求异常（{exc.__class__.__name__}）"
            return None
        if response.status_code != 200:
            self._last_qr_error = f"取图 HTTP {response.status_code}"
            return None
        image = self._extract_base64_image(response.text)
        if not image:
            self._last_qr_error = f"取图失败：{_brief(response.text)}"
            return None
        try:
            content = qrdecode.decode_image_base64(image)
        except qrdecode.QrDecodeUnavailable:
            self._last_qr_error = "本机缺少 Pillow/pyzbar，无法解码二维码"
            return None
        if not content:
            self._last_qr_error = "图片解码失败（未识别到二维码）"
            return None
        state = self._state_from_qr_content(content)
        if state is None:
            self._last_qr_error = f"二维码内容无令牌：{_brief(content, 80)}"
        return state

    def _state_from_qr_content(self, content: str) -> str | None:
        """从二维码文本解析令牌。

        兼容 ``?state=`` / ``?key=`` / ``?code=`` 等键名，以及不含 URL 的裸令牌；
        ``G.aspx?OnlyId=`` 短链需跟随一次 302 取其 ``Location``。
        """
        match = QR_PARAM_RE.search(content)
        if match:
            return unquote(match.group(2))
        match = QR_ONLYID_RE.search(content)
        if match is not None:
            try:
                response = self._session.get(
                    self._url(f"/G.aspx?OnlyId={match.group(1)}"),
                    allow_redirects=False,
                    timeout=self._settings.request_timeout_s,
                )
            except requests.exceptions.RequestException:
                return None
            inner = QR_PARAM_RE.search(response.headers.get("Location", ""))
            return unquote(inner.group(2)) if inner else None
        text = content.strip()
        if (
            text
            and "://" not in text
            and "?" not in text
            and "/" not in text
            and " " not in text
            and len(text) >= 8
        ):
            return text
        return None

    @staticmethod
    def _extract_base64_image(text: str) -> str | None:
        """从图像接口响应中取出 base64 主体。

        服务端实际返回 ``{"msg":1,"msgbox":"二维码生成成功！","data":"iVBOR..."}``，
        且 ASP.NET 会把 ``/`` 转义成 ``\\/``——直接跑正则会因转义把 base64 串切断，
        导致「有图却解不出令牌」。因此先按 JSON 取 ``data``，再回退到还原转义后的正则。
        """
        text = (text or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("data", "img", "image", "base64", "qrCode", "msgbox"):
                value = payload.get(key)
                if isinstance(value, str) and len(value) > 200:
                    return _strip_data_uri(value)
        if isinstance(payload, str) and len(payload) > 200:
            return _strip_data_uri(payload)
        unescaped = text.replace("\\/", "/")
        match = BASE64_RE.search(unescaped)
        if match:
            return "".join(match.group(1).split())
        match = RAW_BASE64_RE.search(unescaped)
        return match.group(0) if match else None

    def _location_payload(
        self, course: Course, longitude: float, latitude: float
    ) -> str:
        """定位签到的表单体：官方页面是 action/cid/tcid/sid/经纬度 六件套。

        缺 ``cid``/``tcid`` 时服务端无从判定是哪门课的教室，坐标再准也会被拒。
        真实签到与距离探测共用此构造，杜绝两处参数漂移。
        """
        return (
            f"action=signin&cid={course.course_id}&tcid={course.class_id}"
            f"&sid={self._require_user_id()}"
            f"&longitude={longitude}&latitude={latitude}"
        )

    def sign_by_location(
        self, course: Course, longitude: float, latitude: float
    ) -> str | None:
        """定位签到：坐标做微小抖动后按官方表单提交；异常见 :meth:`_sign_post`。"""
        jitter = self._settings.location_jitter_deg
        lon = round(longitude + random.uniform(-jitter, jitter), 8)
        lat = round(latitude + random.uniform(-jitter, jitter), 8)
        response = self._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx",
            self._location_payload(course, lon, lat),
            timeout=15,
        )
        if response.status_code != 200:
            return None
        try:
            return response.json().get("msgbox", "")
        except ValueError:
            return None

    def probe_distance(
        self, course: Course, longitude: float, latitude: float
    ) -> float | None:
        """距离预言机：测该坐标到教室的距离；None 表示该点落在签到范围内
        （本次探测已真实签到）。RateLimited 时调用方应中止探测。"""
        response = self._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx",
            self._location_payload(course, longitude, latitude),
            timeout=15,
        )
        if response.status_code != 200:
            return None
        match = DISTANCE_RE.search(response.text)
        return float(match.group(1)) if match else None
