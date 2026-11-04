"""pywebview 的 js_api 门面：校验、转发、JSON 化。"""

from __future__ import annotations

import collections
import threading
import time
import sys
from typing import Any

import requests
import urllib3

from . import __version__
from .api import DuifeneApi
from .config import IniStore, resolve_ini_path
from .credentials import CredentialStore
from .errors import AppApiError, AuthError
from .models import Course
from .processor import ActivityProcessor
from .rate_limit import RateLimiter
from .runlog import FileLog, default_log_path
from .session_watch import SessionKeeper
from .settings import DERIVED_FIELDS, PERSISTABLE_FIELDS, SETTING_LIMITS, Settings
from .watcher import Watcher

# 日志级别推断关键字（顺序即优先级）。
_ERROR_MARKERS = ("失败", "错误", "异常", "未能", "失效")
_WARN_MARKERS = ("限流", "频繁", "静默", "重试")
_SUCCESS_MARKERS = ("成功",)


class Bridge:
    """前端可调用的门面。

    所有方法返回 JSON 可序列化的 ``dict``，且**绝不向 JS 抛异常**——失败统一以
    ``{"ok": False, "message": ...}`` 返回。

    日志经线程安全的 ``deque`` 中转，前端通过 :meth:`drain_logs` 轮询拉取，这是
    唯一允许的 UI 更新通道（watcher 线程绝不触碰 webview）。
    """

    def __init__(self, settings: Settings) -> None:
        """组装依赖；``settings`` 会被就地加载已持久化的配置。"""
        self._settings = settings
        self._store = IniStore(resolve_ini_path(settings.ini_filename))
        self._credentials = CredentialStore(self._store)
        # 界面日志同步落盘：windowed 构建没有 stderr，内存日志关窗即失，
        # 没有文件就无从排查「签不上」这类问题。
        self._file_log = FileLog(default_log_path())
        self._load_persisted_settings()
        self._limiter = RateLimiter(settings, self._log)
        self._session = self._build_session(settings)
        self._window: Any = None  # 由 main.py 注入，用于窗口控制（无边框模式）
        self._api = DuifeneApi(self._session, settings, self._limiter, self._store)
        # 手动签到的 4 位码/汉字：服务端没下发时的兜底，登记后 5 分钟内有效
        self._manual_lock = threading.Lock()
        self._manual_code: str | None = None
        self._manual_until = 0.0
        self._processor = ActivityProcessor(
            settings, self._api, self._log, self._take_manual_code
        )
        self._watcher = Watcher(settings, self._api, self._processor, self._log)
        self._keeper = SessionKeeper(self._api, self._log)
        self._courses: list[Course] = []
        self._course: Course | None = None
        # 登录态的权威标志：登录成功并拉到课程时置真，令牌失效时置否
        self._logged_in = False
        self._auth_lock = threading.RLock()
        self._logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=2000)
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._pending_notice: dict[str, str] | None = None
        self._last_login_message = ""

    # ─ 内部：会话与日志 ─────────────────────────────────────────────
    def _build_session(self, settings: Settings) -> requests.Session:
        """构造全局复用的 HTTP 会话。"""
        urllib3.disable_warnings()
        session = requests.Session()
        session.headers["User-Agent"] = settings.user_agent
        session.trust_env = False
        self._apply_session_config(session)
        return session

    def _apply_session_config(self, session: requests.Session) -> None:
        """把代理与 TLS 开关同步到会话（配置变更后需重新应用）。"""
        session.verify = self._settings.verify_tls
        session.proxies = self._settings.proxies or {}

    def _load_persisted_settings(self) -> None:
        """从 ini 的 `[SETTINGS]` 段加载已保存的运行参数。

        ini 中同时存有派生字段（如 ``verify_tls``），而 :meth:`Settings.apply`
        拒绝直接设置派生字段；故先滤除派生字段，只提交可编辑字段。
        """
        saved = self._store.load_settings()
        if not saved:
            return
        editable = {
            key: value
            for key, value in saved.items()
            if key in PERSISTABLE_FIELDS and key not in DERIVED_FIELDS
        }
        if editable:
            self._settings.apply(editable)

    @staticmethod
    def _level_of(text: str) -> str:
        """由文本关键字推断日志级别，返回 success / error / warning / info。"""
        if any(marker in text for marker in _ERROR_MARKERS):
            return "error"
        if any(marker in text for marker in _WARN_MARKERS):
            return "warning"
        if any(marker in text for marker in _SUCCESS_MARKERS):
            return "success"
        return "info"

    def _log(self, text: str) -> None:
        """线程安全的日志入队（实现 ``LogFn``）；文本含换行时按行拆分。

        同一份文本同步追加到 ``logs/run.log``——界面日志是唯一证据来源，
        关窗后就查不到，必须落盘。
        """
        now = time.time()
        for line in str(text).splitlines() or [""]:
            line = line.rstrip()
            if not line:
                continue
            level = self._level_of(line)
            with self._log_lock:
                self._log_seq += 1
                self._logs.append(
                    {"id": self._log_seq, "text": line, "ts": now, "level": level}
                )
            self._file_log.write(level, line)

    def _notify(self, level: str, text: str) -> None:
        """记录异步通知：写日志队列并置待读横幅。"""
        self._log(text)
        if "重新登录" in text:
            self._logged_in = False
        self._pending_notice = {"level": level, "text": text}

    def _take_manual_code(self) -> str | None:
        """取走手动登记的码（一次性）；过期或未登记返回 None。"""
        with self._manual_lock:
            code = self._manual_code
            self._manual_code = None
            if code and time.time() <= self._manual_until:
                return code
            return None

    def submit_manual_code(self, code: str) -> dict[str, Any]:
        """登记一次手动签到码/汉字，供接下来出现的签到码/汉字活动使用。"""
        try:
            text = "".join(str(code or "").split())
            if len(text) != 4:
                return {"ok": False, "message": "请输入 4 位签到码或 4 个汉字"}
            with self._manual_lock:
                self._manual_code = text
                self._manual_until = time.time() + self._settings.manual_code_ttl_s
            self._log(
                f"已登记手动签到内容 {text}"
                f"（{int(self._settings.manual_code_ttl_s / 60)} 分钟内有效，"
                f"遇到签到码/汉字活动即提交）"
            )
            return {"ok": True, "message": "已登记，等待签到活动"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"登记失败：{exc!s}"}

    # ─ 内部：登录态 ────────────────────────────────────────────────
    def _on_login_invalid(self) -> None:
        """登录态失效时的处置：停保活、停监控、清课程、告警。"""
        self._keeper.stop()
        self._watcher.stop()
        self._logged_in = False
        self._courses = []
        self._course = None
        self._watcher.set_course(None)
        self._pending_notice = {"level": "error", "text": "登录状态失效，请重新登录账号"}

    def _network_mode_text(self) -> str:
        """启动时说明当前网络走向。

        会话设置了 ``trust_env = False``，系统代理（Clash/系统设置里的 HTTP 代理）
        对本程序**无效**，因此「浏览器能开、程序连不上」时，第一条要确认的就是
        这里打印的模式；需要走代理必须在设置里显式填写地址。
        """
        if self._settings.proxy_url:
            return (
                f"网络模式：经代理 {self._settings.proxy_url}"
                f"（证书校验已关闭，仅抓包调试时这样用）"
            )
        return "网络模式：直连，已忽略系统代理设置"

    def _refresh_courses(self) -> str:
        """拉取课程列表并选中首门；返回错误文案，成功返回空串。

        Raises:
            AuthError: 接口返回登录失效。
        """
        with self._auth_lock:
            self._courses = self._api.fetch_courses()
            if not self._courses:
                return "未获取到课程（账号下没有正在进行的教学班）"
            self._logged_in = True
            self._select_course(self._course_key(self._courses[0]))
            return ""

    def _select_course(self, course_key: str) -> None:
        """按「课程ID:教学班ID」设为当前课程。"""
        for course in self._courses:
            if self._course_key(course) == str(course_key):
                self._course = course
                self._watcher.set_course(course)
                return
        for course in self._courses:
            if course.course_id == str(course_key):
                self._course = course
                self._watcher.set_course(course)
                return

    @staticmethod
    def _course_key(course: Course) -> str:
        """课程与教学班的唯一前端标识。"""
        return f"{course.course_id}:{course.class_id}"

    def _courses_payload(self) -> list[dict[str, str]]:
        """课程列表的前端表示。"""
        return [
            {
                "id": self._course_key(course),
                "class_id": course.class_id,
                "name": course.name,
            }
            for course in self._courses
        ]

    def _saved_account(self) -> str:
        """已保存的登录账号（仅用于输入框预填）。"""
        saved = self._credentials.load()
        return saved.account if saved is not None else ""

    # ── js_api ───────────────────────────────────────────────────────
    def bootstrap(self) -> dict[str, Any]:
        """初始化：创建配置、清理旧版残留、恢复登录会话、返回课程与日志。"""
        message = ""
        saved = self._credentials.load()
        try:
            created = self._store.ensure_exists()
            if created:
                self._log("已创建配置文件 duifenyi.ini")
            self._store.clear_legacy_login()
            self._log(f"程序启动 v{__version__}，日志文件：{self._file_log.path}")
            self._log(self._network_mode_text())
            if self._api.is_authenticated:
                message = self._refresh_courses()
            elif saved is not None:
                result = self.login_by_password(saved.account, saved.password)
                if not result.get("ok"):
                    message = str(result.get("message") or "")
        except AuthError as exc:
            self._on_login_invalid()
            message = f"{exc!s} 请重新登录。"
        except AppApiError as exc:
            message = f"自动恢复登录失败：{exc!s}"
        except Exception as exc:  # noqa: BLE001 - 门面绝不抛出
            message = f"初始化失败：{exc!s}"
        if message:
            # 初始化结论也要进日志，否则日志文件只有一行「程序启动」，等于没有证据
            self._log(f"初始化：{message}")
        if self._api.is_authenticated:
            self._keeper.start()
        return {
            "ok": True,
            "version": __version__,
            "platform": "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "other",
            "courses": self._courses_payload(),
            "selected": self._course_key(self._course) if self._course else "",
            "logged_in": self._logged_in,
            "user_account": self._api.identity.account if self._api.identity else "",
            "saved_account": saved.account if saved is not None else "",
            "logs": self.drain_logs(0).get("items", []),
            "message": message,
            "log_path": self._file_log.path,
        }

    def login_by_password(
        self, username: str, password: str, remember: bool = True
    ) -> dict[str, Any]:
        """账号密码登录（App 接口）。

        登录失败不清空已保存的账号密码——避免一次手误把「记住密码」抹掉。
        """
        try:
            with self._auth_lock:
                ok, message = self._api.login(
                    str(username or "").strip(), str(password or "")
                )
                self._last_login_message = message
                if not ok:
                    # 登录失败不代表原有会话失效：api 会自动回滚到已保存的会话，
                    # 因此状态位按回滚后的真实情况取值，避免「界面显示未登录、
                    # 实际还能监听」的不一致。
                    self._logged_in = self._api.is_authenticated
                    if not self._logged_in:
                        self._courses = []
                        self._course = None
                        self._watcher.set_course(None)
                    self._log(f"登录失败：{message}")
                    return {"ok": False, "message": message}
                error = self._refresh_courses()
                if error:
                    self._logged_in = False
                    self._log(f"登录失败：{error}")
                    return {"ok": False, "message": error}
                if remember:
                    self._credentials.save(str(username or "").strip(), str(password or ""))
            self._keeper.start()
            self._log(f"登录成功：{str(username or '').strip()}")
            return {"ok": True, "message": "登录成功", "user_account": str(username or "")}
        except AuthError as exc:
            self._on_login_invalid()
            return {"ok": False, "message": f"{exc!s} 请重新登录。"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"登录失败：{exc!s}"}

    def logout(self) -> dict[str, Any]:
        """退出登录：清会话与保存的密码，保留运行参数。"""
        try:
            self._watcher.stop()
            self._keeper.stop()
            self._api.clear()
            self._credentials.clear()
            self._logged_in = False
            self._courses = []
            self._course = None
            self._watcher.set_course(None)
            self._log("已退出登录")
            return {"ok": True, "message": "已退出登录"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"退出失败：{exc!s}"}

    def select_course(self, course_id: str) -> dict[str, Any]:
        """切换当前课程。"""
        try:
            self._select_course(str(course_id or ""))
            if self._course is None:
                return {"ok": False, "message": "课程不存在"}
            return {"ok": True, "message": f"已选择【{self._course.name}】"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"切换课程失败：{exc!s}"}

    def get_settings(self) -> dict[str, Any]:
        """返回用户可编辑的运行参数（派生字段不下发，故前端不渲染）。"""
        try:
            limits = {name: list(bounds) for name, bounds in SETTING_LIMITS.items()}
            return {
                "ok": True,
                "values": self._settings.editable_values(),
                "limits": limits,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取配置失败：{exc!s}"}

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验、应用并持久化一批配置更新（校验不通过时不改任何字段）。"""
        try:
            if not isinstance(patch, dict):
                return {"ok": False, "message": "参数格式错误"}
            error = self._settings.apply(patch)
            if error:
                return {"ok": False, "message": error}
            self._apply_session_config(self._session)
            self._store.save_settings(self._settings.persistable_values())
            return {
                "ok": True,
                "message": "设置已保存",
                "values": self._settings.editable_values(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"保存设置失败：{exc!s}"}

    def start_watching(self) -> dict[str, Any]:
        """启动监控。"""
        try:
            if not self._api.is_authenticated:
                self._on_login_invalid()
                return {"ok": False, "message": "登录状态失效，请重新登录账号"}
            if self._course is None or self._course.course_id == "0":
                return {"ok": False, "message": "课程未正确加载，请重新登录"}
            self._watcher.start()
            return {"ok": True, "message": f"正在监听【{self._course.name}】"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"启动监听失败：{exc!s}"}

    def stop_watching(self) -> dict[str, Any]:
        """停止监控。"""
        try:
            self._watcher.stop()
            return {"ok": True, "message": "已停止监听"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"停止失败：{exc!s}"}

    # ─ 窗口控制（无边框模式下由前端自绘按钮调用）──────────────────
    def set_window(self, window: Any) -> None:
        """注入 pywebview 窗口对象，供前端控制窗口。"""
        self._window = window

    def window_minimize(self) -> dict[str, Any]:
        """最小化窗口。"""
        try:
            if self._window is not None:
                self._window.minimize()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"最小化失败：{exc!s}"}

    def window_close(self) -> dict[str, Any]:
        """关闭窗口（先停监控与保活，确保线程被清理）。"""
        try:
            self._watcher.stop()
            self._keeper.stop()
            if self._window is not None:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"关闭失败：{exc!s}"}

    def get_state(self) -> dict[str, Any]:
        """返回当前运行状态，供前端刷新指标。"""
        try:
            if self._logged_in and not self._api.is_authenticated:
                self._on_login_invalid()
            notice = self._pending_notice
            self._pending_notice = None
            return {
                "running": self._watcher.is_running,
                "course_name": self._course.name if self._course else "",
                "limiter_remaining_s": round(self._limiter.remaining(), 1),
                "last_tick_ts": self._watcher.last_tick_ts,
                "logged_in": self._logged_in,
                "user_account": self._api.identity.account if self._api.identity else "",
                "notice": notice,
            }
        except Exception:  # noqa: BLE001
            return {
                "running": False,
                "course_name": "",
                "limiter_remaining_s": 0.0,
                "last_tick_ts": 0.0,
                "logged_in": False,
                "user_account": "",
                "notice": None,
            }

    def clear_logs(self) -> dict[str, Any]:
        """清空日志队列，供前端复位显示时同步。"""
        try:
            self._clear_logs()
            with self._log_lock:
                next_id = self._log_seq
            return {"ok": True, "next_id": next_id}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"清空日志失败：{exc!s}"}

    def _clear_logs(self) -> None:
        """清空日志队列。

        只清队列、不重置 ``_log_seq``：id 保持单调递增，前端的 ``logCursor``
        便永不失效——若把序号归零，前端已持有的旧游标会让新日志被永久漏掉。
        """
        with self._log_lock:
            self._logs.clear()

    def drain_logs(self, since_id: int = 0) -> dict[str, Any]:
        """取出 id 大于 ``since_id`` 的日志；每项含 ``id/text/ts/level``。"""
        try:
            since = int(since_id or 0)
        except (TypeError, ValueError):
            since = 0
        with self._log_lock:
            items = [item for item in self._logs if item["id"] > since]
            next_id = self._log_seq
        return {"next_id": next_id, "items": items}
