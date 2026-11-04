"""异常与日志回调类型。"""

from __future__ import annotations

from collections.abc import Callable


class AuthError(RuntimeError):
    """登录态失效，需重新登录。"""


class AppApiError(RuntimeError):
    """App 接口返回的业务错误（服务端文案随异常带出）。"""


class RateLimited(RuntimeError):
    """命中服务端限流，需要静默一段时间。"""


# 业务层只依赖这个回调签名，不依赖具体 GUI
LogFn = Callable[[str], None]

__all__ = ["AuthError", "AppApiError", "RateLimited", "LogFn"]
