"""GUI 无关的监控调度线程。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime

from .api import DuifeneApi
from .errors import AppApiError, AuthError, LogFn
from .models import Course
from .processor import ActivityProcessor
from .settings import Settings


class Watcher:
    """在后台 daemon 线程中轮询当前课程的签到活动。

    ``log`` 由外壳实现，需保证线程安全（watcher 线程绝不触碰 webview）。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        processor: ActivityProcessor,
        log: LogFn,
    ) -> None:
        self._settings = settings
        self._api = api
        self._processor = processor
        self._log = log
        self._course: Course | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_errors = 0
        self._last_tick_ts = 0.0
        # 时间窗内外状态：仅在跨越边界时记一条日志，避免窗外每轮刷屏
        self._in_window: bool | None = None
        self._last_api_error = ""
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """监控线程是否正在运行。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_tick_ts(self) -> float:
        """最近一轮循环的时间戳（秒）；从未运行过为 0。"""
        return self._last_tick_ts

    def set_course(self, course: Course | None) -> None:
        """设置当前监控的课程；``None`` 表示清空。"""
        with self._lock:
            self._course = course

    def start(self) -> None:
        """启动监控线程（若已在运行则先停止再重启）。"""
        if not self._api.is_authenticated:
            raise AuthError("登录状态已失效，请重新登录账号")
        self.stop()
        self._stop.clear()
        self._processor.reset()
        self._consecutive_errors = 0
        self._in_window = None
        with self._lock:
            course = self._course
        self._log(self._course_prefix(course, "开始监听"))
        self._thread = threading.Thread(
            target=self._run, name="duifene-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止监控：置停止标志并等待线程退出。

        仅在确实处于运行态时记录停止日志——``start`` 会先调 ``stop`` 以支持
        重启，若无条件记日志，每次启动都会多出一条「已停止监听」。
        """
        was_running = self.is_running
        with self._lock:
            course = self._course
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        if was_running:
            self._log(self._course_prefix(course, "已停止监听"))

    @staticmethod
    def _course_prefix(course: Course | None, message: str) -> str:
        """给日志加「课程: 名称 」前缀；``course`` 为 None 时只返回正文。"""
        return f"课程: {course.name} {message}" if course else message

    def _sleep(self, seconds: float) -> None:
        """可中断休眠；收到停止信号立即返回。"""
        self._stop.wait(max(0.0, seconds))

    def _in_monitor_window(self) -> bool:
        """当前是否处于可签到时间窗（左闭右开）。"""
        hour = datetime.now().hour
        return (
            self._settings.monitor_start_hour
            <= hour
            < self._settings.monitor_end_hour
        )

    def _run(self) -> None:
        """循环主体：认证失效即停止，其它异常按退避重试。"""
        while not self._stop.is_set():
            try:
                self._tick()
            except AuthError as exc:
                with self._lock:
                    course = self._course
                self._log(self._course_prefix(course, f"{exc!s}，监听已停止"))
                self._stop.set()
                break
            except Exception as exc:  # noqa: BLE001 - 单轮失败不终止监控
                self._consecutive_errors += 1
                # 退避从「一轮轮询」起算，且上限压到 error_backoff_max_s（默认 15 秒）：
                # 签到窗口很短，长退避会让程序在场内完全失声。
                backoff = min(
                    self._settings.poll_max_s * (2 ** (self._consecutive_errors - 1)),
                    self._settings.error_backoff_max_s,
                )
                with self._lock:
                    course = self._course
                self._log(
                    self._course_prefix(
                        course, f"监控异常：{exc!s}（{int(backoff)} 秒后重试）"
                    )
                )
                self._sleep(backoff)
                continue
            if self._stop.is_set():
                break
            self._sleep(
                random.uniform(self._settings.poll_min_s, self._settings.poll_max_s)
            )

    def _tick(self) -> None:
        """一轮监控：时间窗门禁 → 读取当前签到 → 交给处理器。

        无活动时不产生日志：持续监控的「存活」由前端状态行的最近轮询时间体现，
        日志只记录真实事件，避免空轮询刷屏。
        """
        self._last_tick_ts = time.time()
        if self._stop.is_set():
            return
        if not self._api.is_authenticated:
            raise AuthError("登录状态已失效，请重新登录账号")
        with self._lock:
            course = self._course
        in_window = self._in_monitor_window()
        if in_window != self._in_window:
            self._in_window = in_window
            window = (
                f"{self._settings.monitor_start_hour}–"
                f"{self._settings.monitor_end_hour} 时"
            )
            self._log(
                self._course_prefix(
                    course,
                    f"进入监听时间窗（{window}），开始轮询"
                    if in_window
                    else f"当前不在监听时间窗（{window}），暂不轮询",
                )
            )
        if not in_window or course is None:
            return
        try:
            activity = self._api.fetch_active_activity(course.course_id)
        except AppApiError as exc:
            # 接口偶尔抖动属正常：只记一条同类原因的日志，下一轮（3–5 秒后）继续，
            # 绝不进入退避——签到窗口常常只有一两分钟，睡 60 秒等于直接放弃。
            message = str(exc)
            if message != self._last_api_error:
                self._last_api_error = message
                self._log(self._course_prefix(course, f"读取签到活动失败：{message}"))
            return
        self._last_api_error = ""
        if activity is None:
            self._consecutive_errors = 0
            return
        self._processor.process(activity, course)
        self._consecutive_errors = 0


__all__ = ["Watcher"]
