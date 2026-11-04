# -*- mode: python ; coding: utf-8 -*-
# 对分易自动签到 — PyInstaller 打包配置（Windows / onedir / windowed）
#
# v1.6 起后端改为官方 App 的 JSON 接口，二维码令牌由服务端直接下发，
# 不再需要 Pillow / pyzbar 解码图片，打包体积也随之下降。

import os

APP_NAME = "对分易自动签到"
ICON = os.path.join("assets", "icon.ico")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[("web", "web")],
    hiddenimports=[],
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
        # 已不再使用的旧依赖：显式排除，避免开发机上装过就被打进包
        "PIL",
        "pyzbar",
        "bs4",
        "websocket",
        "numpy",
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
