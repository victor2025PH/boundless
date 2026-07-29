# -*- coding: utf-8 -*-
"""运行根契约 — 夜间任务/CLI 与「实例数据根」的统一解析（2026-07-29）。

背景：本机 2026-07 迁双实例部署后，常驻服务的 CWD/配置/资产全在实例数据根
（如 ``D:\\chengjie-instances\\zhiliao\\data``），但夜间 CLI（预渲染/声纹抽检/
参考音审计）仍按**引擎仓库根**解析 config/profiles/产物路径 → 整条链空转 10 天
（「没有 avatar_clone 人设可渲染」），且产物即使渲出来 app 也读不到（app 按
CWD 相对路径读 ``assets/voices`` 与 ``logs/*.jsonl``）。

契约（所有离线工具统一走这里，禁止各自猜根）：

1. CLI 显式 ``--data-root`` 最高优先；
2. 环境变量 ``AITR_DATA_ROOT`` 次之；
3. 实例自动发现：``CHENGJIE_INSTANCES_BASE``（默认 ``D:\\chengjie-instances``）下
   每个 ``<id>/data`` 且含 ``config/config.yaml`` 且无退役旗标
   （``.ops/retired/<id>.flag``，与 status_instances.ps1 同一语义）；
4. 都没有 → 回落引擎仓库根（开发机/CI 的旧行为，零破坏）。

多实例机返回多个根：调用方逐根跑（产物各落各家，互不串味）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INSTANCES_BASE = r"D:\chengjie-instances"


def instances_base() -> Path:
    """实例部署基目录（env ``CHENGJIE_INSTANCES_BASE`` 可覆写，便于测试/异机）。"""
    return Path(os.environ.get("CHENGJIE_INSTANCES_BASE") or DEFAULT_INSTANCES_BASE)


def _is_retired(base: Path, instance_id: str) -> bool:
    """退役判定与 deploy/instances 同一旗标语义（cutover 回滚会摘旗）。"""
    try:
        return (base / ".ops" / "retired" / f"{instance_id}.flag").is_file()
    except Exception:
        return False


def discover_instance_roots(base: Optional[Path] = None) -> List[Path]:
    """枚举本机**活跃**实例的数据根；无实例部署 → []（绝不抛）。"""
    b = Path(base) if base else instances_base()
    out: List[Path] = []
    try:
        if not b.is_dir():
            return []
        for child in sorted(b.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            data = child / "data"
            if not (data / "config" / "config.yaml").is_file():
                continue
            if _is_retired(b, child.name):
                continue
            out.append(data)
    except Exception:
        return []
    return out


def resolve_data_roots(cli_value: str = "", *,
                       base: Optional[Path] = None) -> List[Path]:
    """按契约优先级解析数据根列表（至少一个元素，末级回落引擎根）。"""
    v = str(cli_value or "").strip() or os.environ.get("AITR_DATA_ROOT", "").strip()
    if v:
        return [Path(v)]
    found = discover_instance_roots(base)
    return found or [ENGINE_ROOT]


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_merged_config(root: Path) -> dict:
    """读 ``root/config/config.yaml`` + ``config.local.yaml`` overlay（深合并）。

    缺文件返回已有部分（空文件/缺 base 不抛）——离线工具对坏根应软失败，
    由调用方按「无目标可做」处理。
    """
    import yaml

    cfg_dir = Path(root) / "config"
    data: dict = {}
    base_f = cfg_dir / "config.yaml"
    if base_f.is_file():
        data = yaml.safe_load(base_f.read_text(encoding="utf-8")) or {}
    local_f = cfg_dir / "config.local.yaml"
    if local_f.is_file():
        overlay = yaml.safe_load(local_f.read_text(encoding="utf-8")) or {}
        if isinstance(overlay, dict):
            _deep_merge(data, overlay)
    return data


def profiles_runtime_path(root: Path) -> Path:
    """运行时人设文件（权威人设源）在该根下的位置。"""
    return Path(root) / "config" / "profiles_runtime.yaml"
