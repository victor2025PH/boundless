# -*- coding: utf-8 -*-
"""评测器的「运行态真实配置」解析（2026-07-29 修一处静默失真）。

**为什么需要它**：`src/eval/*` 里有四处各自 `open("config/config.yaml")` 读配置——
CWD 相对路径。评测的既有文档口径是「读**运行态真实配置**（ollama_mt 端点等运营开关
常只写在 overlay，不合并会漏评）」，但从引擎根跑 `python -m scripts.run_eval`（正是
文档给的用法、也是周批任务 `TranslationEvalWeekly` 的 `Set-Location` 落点）时，
CWD = **引擎根**，读到的是**迁移时刻遗留的旧副本**，不是实例数据根里那份在跑的配置。

实测差异（2026-07-29，zhiliao 实例）——注意**症状不是报错也不是 skip，是「评的不是在跑的」**：

  translation.engines.ollama_mt  引擎根 base_urls=[140,176]（双活、140 优先）
                                 / 实例 base_url=176（单端点）
  translation.engines.per_lang_order  引擎根 {'hi': [ai, ollama_mt]} / 实例 {}（已下线）
  ai.embedding_base_url(s)       引擎根 140 优先 / 实例 176 优先

A/B 实证（同命令、仅数据根不同）：读引擎根 38.0s vs 读实例 15.1s——耗时差就是「先打 140」
的直接证据；两次都 10/10 PASS，所以**光看报告永远发现不了**。真正的代价是归因失真：
周批趋势里 hi 语对的数字描述的是一条**已被运营下线**的路由覆写，端点延迟画的是另一套拓扑，
而「弱语对该不该进 per_lang_order」这类决策正是读这些数字做的。

解析顺序复用**既有唯一事实源** `scripts/_data_root`（CLI 值 → `AITR_DATA_ROOT` →
自动发现 `D:\\chengjie-instances` 活跃实例 → 引擎根）——注意不能用
`licensing.data_paths.config_dir()`：它只认 `AITR_CONFIG_PATH`/`AITR_DATA_DIR` 两个
env，而 CLI 场景两者都没设 → 仍回落仓内，得不到活跃实例。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _cwd_relative_fallback() -> Dict[str, Any]:
    """旧行为：读 CWD 下的 config/config.yaml + config.local.yaml（深合并）。

    保留作兜底——单测/离线环境可能既无实例目录也无 env，此时旧语义仍可用。
    """
    import os

    import yaml
    data: Dict[str, Any] = {}
    try:
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    try:
        if os.path.exists("config/config.local.yaml"):
            with open("config/config.local.yaml", "r", encoding="utf-8") as f:
                over = yaml.safe_load(f) or {}
            if isinstance(over, dict):
                _deep_merge(data, over)
    except Exception:
        logger.debug("overlay 合并失败（忽略）", exc_info=True)
    return data


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def runtime_config_source() -> str:
    """本次会从哪个根读配置（供评测报告/排障打印，避免「不知道评的是哪份」）。"""
    try:
        from scripts._data_root import resolve_data_roots
        roots = resolve_data_roots()
        return str(roots[0]) if roots else "<cwd>"
    except Exception:
        return "<cwd>"


def _cwd_is_engine_root() -> bool:
    """当前 CWD 是否就是引擎根（那里的 config 正是迁移遗留的陈旧副本）。"""
    from pathlib import Path
    try:
        from scripts._data_root import ENGINE_ROOT
        return Path.cwd().resolve() == Path(ENGINE_ROOT).resolve()
    except Exception:
        return False


def _cwd_has_config() -> bool:
    from pathlib import Path
    return (Path.cwd() / "config" / "config.yaml").is_file()


def load_runtime_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回运行态真实配置（已合并 overlay）。``config`` 非空时原样返回（调用方显式给）。

    优先级（顺序本身就是本模块的全部价值，别随手改）：

    1. 显式 ``config`` 实参——调用方说了算；
    2. **CWD 里真有 config 且 CWD 不是引擎根** → 用它。这条保住两件事：
       ``monkeypatch.chdir(tmp)`` 的**测试密闭性**（否则单测会静默去读生产实例配置，
       结果取决于跑在哪台机器上——与 global_rules 那次「测试写生产」同一类事故），
       以及「从某个实例数据根直接跑」的显式意图；
    3. 数据根契约 ``scripts/_data_root``（CLI 值 → ``AITR_DATA_ROOT`` → 自动发现活跃
       实例）——这是修「从引擎根跑 run_eval 读到迁移遗留旧副本」的那一条；
    4. 兜底 CWD 相对读取（离线/无实例环境的旧行为）。

    第 2 与第 3 条的**唯一分界就是「CWD 是不是引擎根」**：引擎根那份是陈旧副本，必须
    让位给活跃实例；别处的 config 是调用方刻意摆的，必须尊重。全程不抛——评测缺配置
    应优雅 skip，不该把回归搞崩。
    """
    if config is not None:
        return config
    if _cwd_has_config() and not _cwd_is_engine_root():
        data = _cwd_relative_fallback()
        if data:
            return data
    try:
        from scripts._data_root import load_merged_config, resolve_data_roots
        roots = resolve_data_roots()
        if roots:
            data = load_merged_config(roots[0])
            if data:
                return data
    except Exception:
        logger.debug("数据根解析失败，回落 CWD 相对读取", exc_info=True)
    return _cwd_relative_fallback()
