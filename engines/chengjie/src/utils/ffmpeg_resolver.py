"""ffmpeg / ffprobe 路径解析（实施49 P1-11，2026-08-20 内测工单 #3 定案）。

事故：客户桌面包不带 ffmpeg → 语音合成后无法转 OGG/Opus 原样上传 WAV →
pyrogram 上传中断 + 错误 repr 喷整段字节流。修复=安装包捆绑 ffmpeg/ffprobe
（electron-builder extraResources → ``resources/ffmpeg/``），本模块是**唯一**
路径解析口：

    优先级：env（CHATX_FFMPEG / CHATX_FFPROBE，显式覆写口）
          → 打包布局相对探测（冻结后端 exe 的邻居目录，见下）
          → PATH（``shutil.which``，内部部署/开发机的原行为）

打包布局（PyInstaller onedir + electron-builder）::

    resources/
      backend/<chatx-backend>.exe      ← sys.executable（冻结态）
      ffmpeg/ffmpeg.exe / ffprobe.exe  ← extraResources 落点

于是冻结态候选＝``Path(sys.executable).parent.parent / "ffmpeg" / <name>``；
另探 ``parent / "ffmpeg"``（备用：二进制与 exe 同层的布局变体）。
非冻结（源码跑）不做相对探测——直接回落 PATH，内部机器行为零变化。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_ENV_FFMPEG = "CHATX_FFMPEG"
_ENV_FFPROBE = "CHATX_FFPROBE"


def _resolve(name: str, env_key: str) -> Optional[str]:
    env = str(os.environ.get(env_key) or "").strip()
    if env and Path(env).is_file():
        return env
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for cand_dir in (exe_dir.parent / "ffmpeg", exe_dir / "ffmpeg"):
            cand = cand_dir / (name + (".exe" if os.name == "nt" else ""))
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                pass
    return shutil.which(name)


def ffmpeg_path() -> Optional[str]:
    """ffmpeg 可执行文件绝对路径；全部落空返回 None（调用方软降级）。"""
    return _resolve("ffmpeg", _ENV_FFMPEG)


def ffprobe_path() -> Optional[str]:
    """ffprobe 可执行文件绝对路径；全部落空返回 None。"""
    return _resolve("ffprobe", _ENV_FFPROBE)
