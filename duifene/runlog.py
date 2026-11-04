"""运行日志落盘：把界面里的日志同时写到 exe 同目录的 ``logs/run.log``。

windowed（``console=False``）构建没有 stderr，界面日志又只存在内存里，
一旦关窗或崩溃就什么都不剩——用户只能笼统地说「用不了」，无法定位。
这里把每条日志与启动期未捕获异常都追加到文件，事后可直接取日志排查。
写盘失败时静默禁用自身，绝不影响签到主流程。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from types import TracebackType

MAX_BYTES = 2 * 1024 * 1024


def default_log_path() -> str:
    """日志文件位置：冻结后锚到 exe 同目录，源码运行锚到项目根目录。"""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Logs/duifenyi-auto-sign")
        return os.path.join(base, "run.log")
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "logs", "run.log")


class FileLog:
    """线程安全的追加式日志文件；写盘失败后自动静默（不重试、不抛错）。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._broken = False

    @property
    def path(self) -> str:
        """日志文件绝对路径。"""
        return self._path

    def write(self, level: str, text: str) -> None:
        """追加一行日志；``text`` 含换行时按行拆分，保持时间戳逐行可读。"""
        if self._broken:
            return
        lines = [line for line in str(text).splitlines() if line.strip()] or [""]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            try:
                directory = os.path.dirname(self._path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                self._rotate_if_needed()
                with open(self._path, "a", encoding="utf-8") as handle:
                    for line in lines:
                        handle.write(f"{stamp} [{level}] {line}\n")
            except OSError:
                self._broken = True

    def _rotate_if_needed(self) -> None:
        """超过 ``MAX_BYTES`` 时改名成 ``run.log.1``（只保留一份旧文件）。"""
        try:
            if os.path.getsize(self._path) < MAX_BYTES:
                return
        except OSError:
            return
        try:
            os.replace(self._path, f"{self._path}.1")
        except OSError:
            pass


def write_crash(
    file_log: FileLog,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    """把未捕获异常（含 traceback）写进日志，便于定位启动期/线程内崩溃。"""
    detail = "".join(traceback.format_exception(exc_type, exc, tb)).strip()
    file_log.write("CRASH", detail)


def install_crash_hooks(file_log: FileLog) -> None:
    """安装主线程与后台线程的未捕获异常钩子，统一落到日志文件。"""
    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        write_crash(file_log, exc_type, exc, tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            write_crash(file_log, args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
