"""账号密码持久化：Windows 上用当前用户的 DPAPI 加密，其它平台明文。

DPAPI 的密文只能被同一台机器的同一 Windows 账户解开，配置文件被拷走后
也解不出密码；开发/测试环境（非 Windows）退化为明文，便于跑单元测试。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from .config import IniStore

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


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
            elif scheme == "plain":
                password = data
            else:
                return None
        except (OSError, ValueError, ctypes.ArgumentError):
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
        self._store.clear_account()


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
