"""配置持久化：``duifenyi.ini`` 的读写。

配置文件里只放三类内容：运行参数（``[SETTINGS]``）、App 登录令牌
（``[APP_SESSION]``）与加密后的账号密码（``[ACCOUNT]``）。旧版本写入的
网页 Cookie（``[INFO]``）与教室坐标缓存（``[LOC_*]``）会在启动时清理。
"""

from __future__ import annotations

import configparser
import os
import sys
from typing import Any, Mapping

SETTINGS_SECTION = "SETTINGS"
APP_SESSION_SECTION = "APP_SESSION"
ACCOUNT_SECTION = "ACCOUNT"

# 旧版本遗留段：网页 Cookie 与按班级缓存的教室坐标，新版本不再使用
_LEGACY_SECTIONS = ("INFO",)


def resolve_ini_path(filename: str) -> str:
    """把相对 ini 路径锚定到稳定位置，避免因启动时工作目录不同而读/写到不同的 ini。

    - PyInstaller 冻结运行：锚到 ``sys.executable``（exe）所在目录；
    - 源码运行：锚到项目根目录（``duifene`` 包的上一级）。
    """
    if os.path.isabs(filename):
        return filename
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


class IniStore:
    """管理 ``duifenyi.ini``：运行参数、App 登录会话与账号密码。"""

    def __init__(self, path: str) -> None:
        self._path = path

    @property
    def path(self) -> str:
        """配置文件绝对路径。"""
        return self._path

    # ─ 基础 ────────────────────────────────────────────────────────
    def _read(self) -> configparser.ConfigParser:
        """读取配置；文件损坏时返回空配置而不是抛错（宁可丢配置也不能打不开）。"""
        parser = configparser.ConfigParser()
        try:
            parser.read(self._path, encoding="utf-8")
        except (configparser.Error, OSError):
            return configparser.ConfigParser()
        return parser

    def _write(self, parser: configparser.ConfigParser) -> None:
        """写盘；失败静默忽略，绝不中断签到主流程。"""
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass

    def ensure_exists(self) -> bool:
        """保证配置文件可用；返回是否新建或补写了内容。

        旧版本残留的 ini 在清理后可能变成空文件（没有任何段），configparser
        仍能读，但用户看不到任何可改项，因此这里补一个空的 ``[SETTINGS]``。
        """
        if not os.path.exists(self._path):
            parser = configparser.ConfigParser()
            parser[SETTINGS_SECTION] = {}
            self._write(parser)
            return True
        parser = self._read()
        if not parser.sections():
            parser[SETTINGS_SECTION] = {}
            self._write(parser)
            return True
        return False

    def clear_legacy_login(self) -> None:
        """删除旧版本（网页接口时代）写入的 Cookie 与教室坐标缓存。"""
        parser = self._read()
        changed = False
        for section in _LEGACY_SECTIONS:
            if parser.has_section(section):
                parser.remove_section(section)
                changed = True
        for section in list(parser.sections()):
            if section.startswith("LOC_"):
                parser.remove_section(section)
                changed = True
        if changed:
            self._write(parser)

    # ─ 运行参数 ────────────────────────────────────────────────────
    def load_settings(self) -> dict[str, str]:
        """读取 `[SETTINGS]` 段原始键值（未做类型转换，由 Settings 校验）。"""
        parser = self._read()
        if not parser.has_section(SETTINGS_SECTION):
            return {}
        return dict(parser.items(SETTINGS_SECTION))

    def save_settings(self, values: Mapping[str, Any]) -> None:
        """写入 `[SETTINGS]` 段，保留其余段。"""
        parser = self._read()
        parser[SETTINGS_SECTION] = {str(k): str(v) for k, v in values.items()}
        self._write(parser)

    # ─ App 登录会话 ────────────────────────────────────────────────
    def load_app_session(self) -> dict[str, str]:
        """读取本地保存的 App 登录会话。"""
        parser = self._read()
        if not parser.has_section(APP_SESSION_SECTION):
            return {}
        return dict(parser.items(APP_SESSION_SECTION))

    def save_app_session(self, values: Mapping[str, Any]) -> None:
        """保存 App 登录会话。"""
        parser = self._read()
        parser[APP_SESSION_SECTION] = {str(k): str(v) for k, v in values.items()}
        self._write(parser)

    def clear_app_session(self) -> None:
        """删除 App 登录会话（保留账号密码，便于下次自动登录）。"""
        parser = self._read()
        if parser.has_section(APP_SESSION_SECTION):
            parser.remove_section(APP_SESSION_SECTION)
            self._write(parser)

    # ─ 账号密码 ────────────────────────────────────────────────────
    def load_account(self) -> dict[str, str]:
        """读取加密保存的账号密码。"""
        parser = self._read()
        if not parser.has_section(ACCOUNT_SECTION):
            return {}
        return dict(parser.items(ACCOUNT_SECTION))

    def save_account(self, values: Mapping[str, Any]) -> None:
        """保存账号密码。"""
        parser = self._read()
        parser[ACCOUNT_SECTION] = {str(k): str(v) for k, v in values.items()}
        self._write(parser)

    def clear_account(self) -> None:
        """删除保存的账号密码。"""
        parser = self._read()
        if parser.has_section(ACCOUNT_SECTION):
            parser.remove_section(ACCOUNT_SECTION)
            self._write(parser)


__all__ = [
    "IniStore",
    "resolve_ini_path",
    "SETTINGS_SECTION",
    "APP_SESSION_SECTION",
    "ACCOUNT_SECTION",
]
