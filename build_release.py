"""打包发布：生成「干净」的 Windows 压缩包。

发布包绝不能带任何本地状态——``duifenyi.ini`` 里有登录令牌与（DPAPI 加密的）
账号密码，``logs/`` 里有运行记录。一旦打进压缩包，别人下载后会直接以**你的
身份**登录，看到的是你的课程，既泄漏凭证，又让对方以为「这软件签不上」。

用法：
  python build_release.py               # 打包现有 dist 产物
  python build_release.py --build       # 先跑 PyInstaller 再打包
  python build_release.py --check <zip>  # 只校验某个包是否干净

产物：duifenyi-auto-sign_v<版本>_win64.zip
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR_NAME = "对分易自动签到"
DIST_DIR = ROOT / "dist" / APP_DIR_NAME

# 绝不允许进入发布包的文件 / 目录（按名字匹配，大小写不敏感）
FORBIDDEN_NAMES = {"duifenyi.ini", "diagnose_cookies.json", "run.log", "run.log.1"}
FORBIDDEN_DIRS = {"logs"}
FORBIDDEN_SUFFIXES = (".log", ".ini.bak")


def app_version() -> str:
    """从源码读取版本号，避免包名与程序版本不一致。"""
    text = (ROOT / "duifene" / "__init__.py").read_text("utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("无法从 duifene/__init__.py 读取版本号")


def is_forbidden(path: Path) -> bool:
    """判断某个文件是否属于本地状态（不应发布）。"""
    if path.name.lower() in FORBIDDEN_NAMES:
        return True
    if path.name.lower().endswith(FORBIDDEN_SUFFIXES):
        return True
    return any(part.lower() in FORBIDDEN_DIRS for part in path.parts)


def build_exe() -> None:
    """调用 PyInstaller 重新构建 dist。"""
    print("→ 构建 exe …")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise SystemExit("PyInstaller 构建失败")


def make_zip(version: str) -> Path:
    """把 dist 下的程序目录复制到临时目录、剔除本地状态后压缩。"""
    if not DIST_DIR.is_dir():
        raise SystemExit(f"未找到构建产物：{DIST_DIR}（先执行 --build）")
    target = ROOT / f"duifenyi-auto-sign_v{version}_win64.zip"
    if target.exists():
        target.unlink()

    skipped: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / APP_DIR_NAME
        for source in DIST_DIR.rglob("*"):
            relative = source.relative_to(DIST_DIR)
            if is_forbidden(relative):
                skipped.append(str(relative))
                continue
            destination = staging / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(Path(tmp)))

    print(f"→ 已生成 {target.name}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
    if skipped:
        print("→ 已剔除本地状态文件：")
        for item in skipped:
            print(f"   - {item}")
    else:
        print("→ 未发现需要剔除的本地状态文件")
    verify_zip(target)
    return target


def verify_zip(path: Path) -> None:
    """校验压缩包里没有任何凭证/日志；有问题直接失败。"""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    bad = [
        name
        for name in names
        if is_forbidden(Path(name.replace("\\", "/")))
        and (name.endswith("duifenyi.ini") or "/logs/" in name or name.endswith(".log"))
    ]
    has_exe = any(name.endswith("对分易自动签到.exe") for name in names)
    print(f"→ 校验：{len(names)} 个条目，exe {'存在' if has_exe else '缺失'}")
    if bad:
        raise SystemExit(f"发布包仍含本地状态文件，已中止：{bad}")
    if not has_exe:
        raise SystemExit("发布包里没有主程序 exe")
    print("→ 校验通过：不含 duifenyi.ini / logs，可安全发布")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成干净的 Windows 发布包")
    parser.add_argument("--build", action="store_true", help="先重新构建 exe")
    parser.add_argument("--check", metavar="ZIP", help="只校验已有的压缩包")
    args = parser.parse_args()

    if args.check:
        verify_zip(Path(args.check))
        return 0
    version = app_version()
    if args.build:
        build_exe()
    make_zip(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
