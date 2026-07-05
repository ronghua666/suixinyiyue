"""
随心一阅 — PyInstaller 双架构构建脚本
生成 x86 和 x64 两个版本的 .exe
"""
import subprocess
import sys
import os
import shutil
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ENTRY = PROJECT / "local_server.py"
BACKEND = PROJECT / "backend"
FRONTEND = PROJECT / "frontend-dist"
DIST = PROJECT / "dist"
ICON = PROJECT / "app_icon.ico"
CLIENT_BACKEND_FILES = [
    "grader.py",
    "question_analyzer.py",
    "screen_selector.py",
    "token_pricing.py",
]

BUILDS = {
    "x86": r"C:\Python312-32\python.exe",
    "x64": r"C:\Python312-64\python.exe",
}


def build(arch: str) -> Path:
    python = BUILDS.get(arch)
    if not python or not Path(python).exists():
        print(f"[build_client] {arch}: Python not found at {python}, skipping")
        return None

    DIST.mkdir(exist_ok=True)
    # Use ASCII executable names for installer compatibility on older Windows
    # and systems with non-Unicode installer code pages.
    name = f"SuixinYiyue_{arch}"

    cmd = [
        python, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name", name,
        "--icon", str(ICON),
        "--add-data", f"{FRONTEND};frontend-dist",
        *sum((["--add-data", f"{BACKEND / fname};backend"] for fname in CLIENT_BACKEND_FILES), []),
        "--hidden-import", "grader",
        "--hidden-import", "question_analyzer",
        "--hidden-import", "screen_selector",
        "--hidden-import", "token_pricing",
        "--hidden-import", "mss",
        "--hidden-import", "mss.windows",
        "--hidden-import", "pyautogui",
        "--hidden-import", "pyscreeze",
        "--hidden-import", "mouseinfo",
        "--hidden-import", "pyperclip",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--distpath", str(DIST),
        "--workpath", str(PROJECT / "build" / f"pyinstaller_{arch}"),
        "--specpath", str(PROJECT / "build"),
        str(ENTRY),
    ]
    print(f"[build_client] {arch}: {' '.join(cmd)}")
    subprocess.check_call(cmd)
    exe = DIST / f"{name}.exe"
    chinese_alias = DIST / f"随心一阅_{arch}.exe"
    shutil.copy2(exe, chinese_alias)
    print(f"[build_client] {arch}: Done -> {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return exe


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["x86", "x64", "all"], default="all")
    args = parser.parse_args()

    if args.arch == "all":
        for a in ["x86", "x64"]:
            build(a)
    else:
        build(args.arch)
