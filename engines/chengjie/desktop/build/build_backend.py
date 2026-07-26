#!/usr/bin/env python3
"""把仓库后端（main.py）打成自包含可执行，供桌面端作为 sidecar 随包分发（P0 本地自包含）。

用法（在 desktop/ 下）：
    npm run build:backend        # = python build/build_backend.py
或直接：
    python desktop/build/build_backend.py [--clean] [--onefile]

产出：desktop/build/backend-dist/backend(.exe)（onedir 默认，更稳）。
electron-builder 的 extraResources 会把 backend-dist/ → 安装包内 resources/backend/。

注意（重量级软依赖）：openai-whisper / torch / easyocr 体积巨大且为「可选」软依赖。
默认 EXCLUDE 之以控包体；若该机型需要本地 ASR/OCR，去掉对应 --exclude 重打或改走在线后端。
打包是「构建期」动作，需在装好 requirements.txt 的同款 Python 环境里跑，且与目标 OS 一致
（Windows 包要在 Windows 上打、mac 包在 mac 上打——PyInstaller 不跨平台交叉编译）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 ✓/✗/⚠ 等状态符 → 打包成功后最后一行 print 会抛
# UnicodeEncodeError 使脚本 exit 1（CI 打包烟测即便构建成功也误报失败）。强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent          # desktop/build
DESKTOP = HERE.parent                            # desktop
REPO = DESKTOP.parent                            # 仓库根
OUT = HERE / "backend-dist"                      # 产出目录（electron-builder extraResources.from）
NAME = "backend"

# 后端运行需要的数据（模板/静态/示例配置）。格式：(源, 包内目标相对路径)
# 注意：src/web/static 不直接入包——见 _stage_static()（剔除运行时落地的真实媒体后再打）。
DATAS = [
    (REPO / "src" / "web" / "templates", "src/web/templates"),
    # P0-1 A1：桌面随包种子 = 最小配置（无 YOUR_* 占位）。AITR_DESKTOP_MODE 下
    # ConfigManager._ensure_seeded 优先播种它 → 首启只差一个 AI Key（向导写 overlay）。
    # 完整 example 仍随包：供参考 + 非桌面模式回落种子。
    (REPO / "config" / "config.desktop.min.yaml", "config"),
    (REPO / "config" / "config.example.yaml", "config"),
]

# static/ 下的运行时落地目录：protocol_media＝客户聊天媒体（语音/照片/视频），
# persona_avatars＝运行时同步的账号/人设头像。均为 gitignore 的生产数据，随包分发
# ＝把真实客户隐私打进公网安装包（0.1.0 曾中招），必须剔除；两目录代码均按需重建。
RUNTIME_STATIC_EXCLUDES = {"protocol_media", "persona_avatars"}
STATIC_SRC = REPO / "src" / "web" / "static"
STATIC_STAGED = HERE / "static-staged"


def _stage_static() -> Path:
    """把 src/web/static 复制到构建暂存目录，顶层剔除运行时媒体目录后供 --add-data 使用。"""
    shutil.rmtree(STATIC_STAGED, ignore_errors=True)

    def _ignore(dirpath: str, names: list[str]):
        if Path(dirpath).resolve() == STATIC_SRC.resolve():
            return set(names) & RUNTIME_STATIC_EXCLUDES
        return set()

    shutil.copytree(STATIC_SRC, STATIC_STAGED, ignore=_ignore)
    leaked = [n for n in RUNTIME_STATIC_EXCLUDES if (STATIC_STAGED / n).exists()]
    if leaked:
        raise RuntimeError(f"static 暂存仍含运行时目录（打包中止防隐私泄漏）: {leaked}")
    return STATIC_STAGED

# 动态 import 的包，PyInstaller 静态分析抓不全 → 显式 collect。
COLLECT_SUBMODULES = ["src", "uvicorn", "pyrogram", "fastapi"]
COLLECT_ALL = ["uvicorn"]  # uvicorn 的 lifespan/loops/protocols 子模块按字符串加载

# 重量级可选软依赖：默认排除以控包体（缺失时后端对应能力软降级）。
#
# 桌面端定位＝云 AI + 翻译 + 统一收件箱的轻客户端：本地 ASR/向量嵌入/视觉 OCR/
# 浏览器自动化一律走云或 LAN GPU 服务，故其重依赖不随包分发。经 grep 核验：src 全库
# 仅 3 处 import 到这些库（sentence_transformers / faster_whisper，且**全部函数内惰性
# import**），其余（ctranslate2/av/transformers/scipy/sklearn/onnxruntime/numba/llvmlite/
# pyarrow/pandas/playwright…）都是这两个「根」的传递依赖——排除它们不碰后端启动链，
# 缺失时对应可选功能在惰性 import 处软降级（已有 try/except）。
# 保留：numpy（众多库基础依赖）/ jieba（中文分词，KB 可能用）/ PIL（收件箱图片）。
EXCLUDES = [
    # 本地 ASR / 音频声学（桌面走云或 LAN GPU faster-whisper 服务，不做本地转写/分析）
    "whisper", "faster_whisper", "ctranslate2", "av", "librosa", "soundfile",
    "torch", "torchaudio",
    # 本地向量嵌入 / ML / 评测（桌面 embedding 走云 API；eval/训练不随桌面分发）
    "sentence_transformers", "transformers", "tokenizers", "onnxruntime",
    "scipy", "sklearn", "numba", "llvmlite", "datasets", "pyarrow", "pandas",
    # 视觉 OCR / 绘图 / GUI（桌面图像走云 Vision；无需本地 OCR/CV/绘图/Tk）
    "easyocr", "cv2", "matplotlib", "tkinter", "imageio", "imageio_ffmpeg",
    # 浏览器自动化（桌面端不跑网页抓取 / RPA）
    "playwright",
]


def _sep() -> str:
    return ";" if os.name == "nt" else ":"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="打包前清空产出与缓存")
    ap.add_argument("--onefile", action="store_true", help="单文件模式（更慢、首启解压；默认 onedir 更稳）")
    ap.add_argument("--keep-heavy", action="store_true", help="不排除 whisper/torch 等重依赖")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✗ 未安装 PyInstaller。请先：pip install pyinstaller", file=sys.stderr)
        return 2

    if args.clean:
        for d in (OUT, HERE / "build", HERE / "__pycache__", STATIC_STAGED):
            shutil.rmtree(d, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)

    datas = [(_stage_static(), "src/web/static")] + DATAS

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(OUT),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE),
        "--paths", str(REPO),
        ("--onefile" if args.onefile else "--onedir"),
        "--console",
    ]
    for mod in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", mod]
    for mod in COLLECT_ALL:
        cmd += ["--collect-all", mod]
    if not args.keep_heavy:
        for mod in EXCLUDES:
            cmd += ["--exclude-module", mod]
    for src, dst in datas:
        if Path(src).exists():
            cmd += ["--add-data", f"{src}{_sep()}{dst}"]
        else:
            print(f"  · 跳过不存在的数据：{src}")

    cmd.append(str(REPO / "main.py"))

    print("→ PyInstaller:\n  " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(REPO))
    if proc.returncode != 0:
        print("✗ 打包失败", file=sys.stderr)
        return proc.returncode

    # onedir 模式产出 backend-dist/backend/backend(.exe)；electron-builder 取整个 backend-dist。
    # 这里把 onedir 的内层目录提平，使 resources/backend/backend(.exe) 路径与 launcher 解析一致。
    inner = OUT / NAME
    exe = (NAME + ".exe") if os.name == "nt" else NAME
    if not args.onefile and inner.is_dir():
        for item in inner.iterdir():
            target = OUT / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(inner, ignore_errors=True)

    final = OUT / exe
    print(f"✓ 完成：{final}" if final.exists() else f"⚠ 产出未在预期路径：{OUT}（请检查 PyInstaller 输出）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
