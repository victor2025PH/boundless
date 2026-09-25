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

VPS 主控把 YAML 放在 ``/etc/chatx-fleet``（只读，``ProtectSystem=strict``），
运行时库必须落在 ``AITR_DATA_DIR``（``/var/lib/chatx-fleet``）。``config_dir()``
仍只表示 YAML 所在目录；``data_dir()`` / ``runtime_dir()`` 在「配置目录不在
数据根里面」时改指数据根本身。桌面布局（YAML 在 ``<data>/config/``）两边重合，
路径不变。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional


def _engine_root() -> Path:
    """引擎根（src/licensing/data_paths.py → 上三层）。"""
    return Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def config_dir() -> Path:
    """当前生效的 config 目录（YAML 所在目录）。绝不抛。"""
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


def _expand(raw: str) -> Optional[Path]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def _data_root() -> Optional[Path]:
    return _expand(os.environ.get("AITR_DATA_DIR") or "")


def _env_config_file() -> Optional[Path]:
    return _expand(os.environ.get("AITR_CONFIG_PATH") or "")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _split_layout() -> bool:
    """YAML 目录在 AITR_DATA_DIR 之外（VPS：/etc vs /var/lib）。"""
    root = _data_root()
    if root is None:
        return False
    return not _inside(config_dir(), root)


def data_dir() -> Path:
    """运行时可写根。

    配置目录落在 ``AITR_DATA_DIR`` 里面（只设数据根，或桌面 YAML 在
    ``<data>/config``）时返回 ``config_dir()``，库仍在 ``.../config/``。
    配置目录在数据根外面时返回数据根本身。
    """
    root = _data_root()
    if root is not None and _split_layout():
        return root
    return config_dir()


def data_file(name: str) -> str:
    """数据文件绝对路径（str，便于直接喂给旧签名）。

    与 ``data_dir()`` 相同的分裂规则：桌面/仅 ``AITR_DATA_DIR`` 时仍是
    ``<data>/config/<name>``；VPS 分裂布局时是 ``<data>/<name>``。
    """
    return str(data_dir() / name)


def runtime_dir(config_path: Any = None) -> Path:
    """某个配置文件对应的运行时目录。

    仅当 ``config_path`` 解析后就是 ``AITR_CONFIG_PATH``，且其父目录在
    ``AITR_DATA_DIR`` 之外时，返回数据根。其它路径（pytest 临时配置、
    ``--init`` 写到别的文件）仍是该文件的父目录，避免进程级
    ``AITR_DATA_DIR`` 把每份临时库收走。``runtime_dir(None)`` 等于 ``data_dir()``。
    """
    if config_path is None or not str(config_path).strip():
        return data_dir()
    try:
        resolved = Path(config_path).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return data_dir()
    env_cfg = _env_config_file()
    root = _data_root()
    if (
        env_cfg is not None
        and root is not None
        and resolved == env_cfg.resolve()
        and not _inside(resolved.parent, root)
    ):
        return root
    return resolved.parent


def runtime_file(name: str, config_path: Any = None) -> Path:
    """``runtime_dir(config_path) / name``。"""
    return runtime_dir(config_path) / name


def cwd_or_data_file(name: str) -> Path:
    """历史相对路径 ``config/<name>`` 的落点。

    没有 ``config_path`` 可传的单例（账号注册表、``registry.key``、代理池等）
    以前写 ``Path("config/<name>")``，按进程 CWD 解析。VPS 的
    ``WorkingDirectory`` 是安装树 ``/opt/chatx-fleet/app``，文件就进了
    ``app/config``（升级原子替换会清掉）。分裂布局改落 ``data_file``。

    同树布局（桌面 YAML 在数据根内、只设 ``AITR_DATA_DIR``、裸开发）保持
    相对路径：桌面壳 CWD 就是数据根，``config/<name>`` 与 ``data_file``
    重合；pytest 的进程级 ``AITR_DATA_DIR`` 也不会把这些默认库收走。
    """
    if data_dir() != config_dir():
        return Path(data_file(name))
    return Path("config") / name


def resolve_legacy_config_path(path: Any) -> str:
    """把历史相对路径 ``config/<单层文件名>`` 收成 :func:`cwd_or_data_file`。

    ``:memory:``、绝对路径、以及不是 ``config/<name>`` 的路径原样返回（保留
    调用方拼写）。同树布局同样原样返回，这样 pytest 只设 ``AITR_DATA_DIR``
    时默认库仍相对 CWD，不会被进程级数据根收走。必须在**打开时**调用，不能
    在 import 时把结果钉死。
    """
    if path is None:
        return ""
    text = str(path).strip()
    if not text or text == ":memory:":
        return text
    try:
        parsed = Path(text)
    except (TypeError, ValueError):
        return text
    if parsed.is_absolute():
        return text
    parts = parsed.parts
    if len(parts) != 2 or parts[0] != "config" or parts[1] in ("", ".", ".."):
        return text
    if not _split_layout():
        return text
    return str(cwd_or_data_file(parts[1]))


def plugin_dir(config_path: Any = None) -> Path:
    """插件目录。

    分裂布局：``<data root>/plugins``。
    配置与数据同树：沿用 ``<yaml 父目录的父目录>/plugins``
    （桌面是 ``<data>/plugins``，不是 ``<data>/config/plugins``）。
    """
    if config_path is None or not str(config_path).strip():
        rt = data_dir()
        if rt != config_dir():
            return rt / "plugins"
        return rt.parent / "plugins"
    rt = runtime_dir(config_path)
    try:
        parent = Path(config_path).expanduser().resolve().parent
    except Exception:  # noqa: BLE001
        return rt / "plugins"
    if rt != parent:
        return rt / "plugins"
    return parent.parent / "plugins"
