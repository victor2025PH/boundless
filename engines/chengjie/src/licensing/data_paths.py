"""授权/额度数据文件的落盘位置（单一事实源）。

为什么单独一个模块：``license.key`` / ``license_quota.db`` / ``trial_claim.json`` 原本
各自用 ``__file__`` 往上三层推「仓内 config/」。**打包后那是安装目录**，后果分两档：

* electron-builder 默认 per-user 安装（``%LOCALAPPDATA%\\Programs\\...``）可写，但
  **每次升级整目录被替换** → 授权要重新激活、字符用量归零（用量归零＝白送额度，
  这是收入侧的漏，不是洁癖）。
* 装到 ``Program Files`` / 企业统一分发时目录只读 → 激活直接写不进去。

而桌面壳 launcher 早就把可写数据根经 ``AITR_DATA_DIR`` + ``AITR_CONFIG_PATH`` 注进来了
（见 desktop/backend-launcher.js），ConfigManager 与 telemetry 也都按这个顺序找 config
目录。这里把同一套顺序收成一个函数，让「配置在哪，授权与用量就在哪」。

顺序（与 ConfigManager._resolve_config_path / telemetry._config_dir 一致）：
``AITR_CONFIG_PATH`` 的父目录 → ``AITR_DATA_DIR/config`` → 仓内 ``config/``。
"""
from __future__ import annotations

import os
from pathlib import Path


def _engine_root() -> Path:
    """引擎根（src/licensing/data_paths.py → 上三层）。"""
    return Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def config_dir() -> Path:
    """当前生效的 config 目录（可写数据区优先）。绝不抛。"""
    try:
        env_path = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
        if env_path:
            return Path(env_path).expanduser().parent
        env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
        if env_dir:
            return Path(env_dir).expanduser() / "config"
    except Exception:  # noqa: BLE001 - 环境变量畸形时退回仓内
        pass
    return _engine_root() / "config"


def data_file(name: str) -> str:
    """config 目录下某个数据文件的绝对路径（str，便于直接喂给旧签名）。"""
    return str(config_dir() / name)
