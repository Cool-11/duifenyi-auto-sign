"""账号密码持久化：Windows 使用 DPAPI，macOS 使用系统钥匙串。

两种安全存储都绑定当前系统用户；其它平台退化为明文，便于开发与测试。
"""

from __future__ import annotations

import base64
import ctypes
import sys
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

from .config import IniStore

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_KEYCHAIN_SERVICE = "com.duifenyi.auto-sign"


@dataclass(frozen=True)
class SavedCredentials:
    """本地保存的登录账号与密码。"""

    account: str
    password: str


class _DataBlob(ctypes.Structure):
    """Win32 ``DATA_BLOB``。"""

    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    )


class CredentialStore:
    """读写账号密码。"""

    def __init__(self, store: IniStore) -> None:
        self._store = store

    def load(self) -> SavedCredentials | None:
        """读取并解密账号密码；不存在或无法解密时返回 None。"""
        values = self._store.load_account()
        account = str(values.get("account") or "").strip()
        scheme = str(values.get("password_scheme") or "")
        data = str(values.get("password_data") or "")
        if not account or not scheme or not data:
            return None
        try:
            if scheme == "dpapi":
                password = _unprotect_windows(data)
            elif scheme == "keychain":
                password = _load_macos_keychain(account)
            elif scheme == "plain":
                password = data
            else:
                return None
        except (OSError, ValueError, ctypes.ArgumentError, subprocess.SubprocessError):
            return None
        if not password:
            return None
        return SavedCredentials(account=account, password=password)

    def save(self, account: str, password: str) -> None:
        """保存账号密码；Windows 上加密，其它平台明文。"""
        account = str(account or "").strip()
        password = str(password or "")
        if not account or not password:
            return
        if sys.platform == "darwin":
            try:
                _save_macos_keychain(account, password)
                self._store.save_account(
                    {
                        "account": account,
                        "password_scheme": "keychain",
                        "password_data": "stored-in-login-keychain",
                    }
                )
                return
            except (OSError, subprocess.SubprocessError):
                # 钥匙串不可用时不把密码明文写盘。
                return
        if sys.platform == "win32":
            try:
                self._store.save_account(
                    {
                        "account": account,
                        "password_scheme": "dpapi",
                        "password_data": _protect_windows(password),
                    }
                )
                return
            except (OSError, ctypes.ArgumentError):
                # 加密不可用时退回明文，至少保证「记住密码」仍可用
                pass
        self._store.save_account(
            {
                "account": account,
                "password_scheme": "plain",
                "password_data": password,
            }
        )

    def clear(self) -> None:
        """删除保存的账号密码。"""
        values = self._store.load_account()
        account = str(values.get("account") or "").strip()
        if sys.platform == "darwin" and account:
            _delete_macos_keychain(account)
        self._store.clear_account()


def _load_macos_keychain(account: str) -> str:
    """从当前用户的登录钥匙串读取密码。"""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def _save_macos_keychain(account: str, password: str) -> None:
    """写入或更新当前用户的登录钥匙串。"""
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE,
         "-a", account, "-w", password],
        check=True,
        capture_output=True,
        text=True,
    )


def _delete_macos_keychain(account: str) -> None:
    """删除钥匙串条目；条目不存在时忽略。"""
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account],
        check=False,
        capture_output=True,
        text=True,
    )


def _protect_windows(value: str) -> str:
    """使用 Windows DPAPI 加密文本，返回 Base64。"""
    raw = value.encode("utf-8")
    input_buffer = ctypes.create_string_buffer(raw, len(raw))
    input_blob = _DataBlob(
        len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char))
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        ctypes.c_wchar_p("duifene"),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    return base64.b64encode(encrypted).decode("ascii")


def _unprotect_windows(value: str) -> str:
    """解密 Windows DPAPI 生成的 Base64 文本。"""
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    input_buffer = ctypes.create_string_buffer(raw, len(raw))
    input_blob = _DataBlob(
        len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char))
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    return decrypted.decode("utf-8")


__all__ = ["CredentialStore", "SavedCredentials"]
