# -*- mode: python ; coding: utf-8 -*-
# 对分易自动签到 — PyInstaller 打包配置（Windows / onedir / windowed）

import os

from PyInstaller.utils.hooks import collect_dynamic_libs

APP_NAME = "对分易自动签到"
ICON = os.path.join("assets", "icon.ico")

# pyzbar 通过包内自带的 libzbar / libiconv 动态库解码，必须随包收集，
# 否则打包后 import pyzbar 会失败，表现为「二维码签到不可用」。
ZBAR_BINARIES = collect_dynamic_libs("pyzbar")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=ZBAR_BINARIES,
    datas=[("web", "web")],
    hiddenimports=["pyzbar", "pyzbar.pyzbar", "PIL", "PIL.Image"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "PyQt5",
        "PySide6",
        "qtpy",
        "gi",
        "cefpython3",
        "cryptography",
        "trio",
        "websockets",
        "lxml",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：无控制台黑窗
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
