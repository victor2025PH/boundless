# -*- coding: utf-8 -*-
"""P3.2 跨平台身份影子扫描——周期化 + 观测层（把手动 CLI 升级为自动攒数据）。

分工（本模块只做「调度参数 + state 文件 + 看板快照」三件事，匹配/扫描全在
:mod:`src.utils.identity_shadow` 不动）：

- :class:`~src.inbox.health_watchdog.HealthWatchdog` 的 ``_check_identity_shadow``
  每个巡检 tick 调进来（tick 本身跑在 executor 线程，阻塞式 sqlite 只读无害）；
  节流基准＝内存 ts，冷启动采纳 state 文件 ``last_scan_ts``——**重启不重扫**。
- 每轮扫描结果压缩落 ``config/identity_shadow_state.json``（轻量 JSON，风格对齐
  ``line_rpa_state.json``）：跨重启的节流基准 + ops 看板的数据源。
- ``/api/workspace/metrics`` 的 ``identity_shadow`` 段 = :func:`metrics_snapshot`
  （读 state 文件，**绝不在 web 请求里现场扫库**）。

铁律沿袭影子模式本体：扫描只读（sqlite ``mode=ro``）、只报告、绝不写关联；
本模块唯一的写 = state JSON（观测数据，不是业务库）。
``contacts.identity_shadow.enabled=false`` 时：watchdog 每 tick 一次 dict 取值即返回、
metrics 段只出 ``{"enabled": false}``（ops 卡据此整卡隐藏）——零扫描零 IO。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.utils.identity_shadow import (
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    run_shadow_scan,
)

logger = logging.getLogger(__name__)

STATE_FILENAME = "identity_shadow_state.json"

DEFAULT_SCAN_INTERVAL_HOURS = 6.0
DEFAULT_MAX_ROWS = 2000
DEFAULT_SAMPLE_SIZE = 8


# ── 配置解析（纯函数）───────────────────────────────────────────────────────

def shadow_periodic_config(cfg: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """解析 ``contacts.identity_shadow`` 段（缺省/脏值全兜底，绝不抛）。

    - ``enabled``（默认 False，新子系统铁律）
    - ``scan_interval_hours``（默认 6，夹 [0.25, 168]——最快一刻钟、最慢一周）
    - ``max_rows``（默认 2000，夹 [50, 20000]，last_ts 降序取最近）
    - ``sample_size``（默认 8，夹 [0, 50]，state 文件里保留的样本对数）
    """
    node: Any = ((cfg or {}).get("contacts") or {}).get("identity_shadow") or {}
    if not isinstance(node, Mapping):
        node = {}

    def _num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(node.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    return {
        "enabled": bool(node.get("enabled", False)),
        "scan_interval_hours": _num(
            "scan_interval_hours", DEFAULT_SCAN_INTERVAL_HOURS, 0.25, 168.0),
        "max_rows": int(_num("max_rows", DEFAULT_MAX_ROWS, 50, 20000)),
        "sample_size": int(_num("sample_size", DEFAULT_SAMPLE_SIZE, 0, 50)),
    }


def resolve_inbox_db(cfg: Optional[Mapping[str, Any]], cfg_dir: Path) -> Path:
    """镜像 CLI / bootstrap 的解析：``inbox.db_path``（相对则挂 cfg_dir）→ 缺省 inbox.db。"""
    raw = str((((cfg or {}).get("inbox") or {}).get("db_path")) or "").strip()
    p = Path(raw) if raw else (Path(cfg_dir) / "inbox.db")
    return p if p.is_absolute() else (Path(cfg_dir) / p)


def resolve_identity_db(cfg: Optional[Mapping[str, Any]], cfg_dir: Path) -> Path:
    """镜像 skill_manager 的解析：``memory.db_path`` → 缺省 bot.db（user_identity_map 同库）。"""
    raw = str((((cfg or {}).get("memory") or {}).get("db_path")) or "").strip()
    p = Path(raw) if raw else (Path(cfg_dir) / "bot.db")
    return p if p.is_absolute() else (Path(cfg_dir) / p)


def state_path(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / STATE_FILENAME


# ── 报告 → 轻量 state（纯函数）──────────────────────────────────────────────

def _sample_entry(pair: Mapping[str, Any]) -> Dict[str, Any]:
    a = pair.get("a") or {}
    b = pair.get("b") or {}
    pa = str(a.get("platform") or "")
    ca = str(a.get("chat_key") or "")
    pb = str(b.get("platform") or "")
    cb = str(b.get("chat_key") or "")
    # 结构化字段供 ops 卡「证据/确认/否定」按钮直传 API；a/b 字符串保留兼容旧看板
    try:
        from src.utils.identity_shadow_actions import pair_key as _pair_key
        pk = _pair_key(pa, ca, pb, cb)
    except Exception:
        pk = ""
    return {
        "tier": str(pair.get("tier") or ""),
        "already_linked": bool(pair.get("already_linked")),
        "evidence": [str(e) for e in (pair.get("evidence") or [])][:3],
        "a": f"{pa}:{ca}",
        "a_name": str(a.get("display_name") or ""),
        "b": f"{pb}:{cb}",
        "b_name": str(b.get("display_name") or ""),
        "a_platform": pa,
        "a_chat": ca,
        "b_platform": pb,
        "b_chat": cb,
        "pair_key": pk,
    }


def build_state(
    report: Mapping[str, Any],
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """``run_shadow_scan`` 报告 → 轻量 state 文档（样本截前 N 对，不落全量 pairs）。

    失败报告（ok=False）也落 state：last_scan_ts 仍是跨重启节流基准，error 让
    看板能点名「扫描在失败」而不是静默陈旧。
    """
    ts = float(now if now is not None else time.time())
    state: Dict[str, Any] = {
        "ok": bool(report.get("ok")),
        "last_scan_ts": ts,
        "last_scan_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    }
    if not report.get("ok"):
        state["error"] = str(report.get("error") or "unknown")
        return state
    pairs = report.get("pairs") or []
    counts = report.get("counts") or {}
    state.update({
        "scanned_conversations": int(report.get("scanned_conversations") or 0),
        "candidates": int(report.get("candidates") or 0),
        "skipped": dict(report.get("skipped") or {}),
        # P7：各平台 phone/username 有效信号覆盖——「配对为 0」到底是覆盖不足
        # 还是匹配问题，看板一眼分清；也顺带可视化通讯录回填进度。
        "coverage": {k: dict(v) for k, v in
                     (report.get("coverage") or {}).items()},
        "pairs": len(pairs),
        "counts": {
            TIER_HIGH: int(counts.get(TIER_HIGH) or 0),
            TIER_MEDIUM: int(counts.get(TIER_MEDIUM) or 0),
            TIER_LOW: int(counts.get(TIER_LOW) or 0),
            "already_linked": int(counts.get("already_linked") or 0),
        },
        # pairs 已按 tier（high→low）稳定排序 → 截前 N 即最有价值的样本
        "sample": [_sample_entry(p) for p in pairs[:max(0, int(sample_size))]],
        "dismissed_total": int(report.get("dismissed_total") or 0),
    })
    return state


# ── state 文件 IO ───────────────────────────────────────────────────────────

def write_state(path: Path, state: Mapping[str, Any]) -> bool:
    """落盘（UTF-8 缩进 JSON，风格对齐 line_rpa_state.json）。失败只记 warning。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:
        logger.warning("身份影子 state 文件写入失败（已忽略）: %s", path, exc_info=True)
        return False


def read_state(path: Path) -> Dict[str, Any]:
    """读 state（缺失/损坏一律返回 ``{}``，绝不抛）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── 周期扫描入口（watchdog 调用）────────────────────────────────────────────

def run_periodic_scan(
    cfg: Optional[Mapping[str, Any]],
    cfg_dir: Path,
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """跑一轮只读扫描并把压缩 state 落盘。返回 state（含 ok）。

    run_shadow_scan 自身对库缺失/读失败返回 ``ok=False`` 不抛；这里再兜一层
    （state 写失败等）由调用方（watchdog）统一 try/except。
    """
    pcfg = shadow_periodic_config(cfg)
    report = run_shadow_scan(
        resolve_inbox_db(cfg, cfg_dir),
        resolve_identity_db(cfg, cfg_dir),
        limit=pcfg["max_rows"],
    )
    # 剔除人工否定对，再压缩 state（计数/样本与看板一致）
    if report.get("ok"):
        try:
            from src.utils.identity_shadow import TIER_HIGH, TIER_MEDIUM, TIER_LOW
            from src.utils.identity_shadow_actions import (
                dismiss_path, filter_dismissed_pairs, load_dismissed,
            )
            dismissed = load_dismissed(dismiss_path(cfg_dir))
            pairs = filter_dismissed_pairs(report.get("pairs") or [], dismissed)
            report = dict(report)
            report["pairs"] = pairs
            report["dismissed_total"] = len(dismissed.get("keys") or {})
            report["counts"] = {
                TIER_HIGH: sum(1 for p in pairs if p.get("tier") == TIER_HIGH),
                TIER_MEDIUM: sum(1 for p in pairs if p.get("tier") == TIER_MEDIUM),
                TIER_LOW: sum(1 for p in pairs if p.get("tier") == TIER_LOW),
                "already_linked": sum(
                    1 for p in pairs if p.get("already_linked")),
            }
        except Exception:
            logger.debug("dismiss filter skipped", exc_info=True)
    state = build_state(report, sample_size=pcfg["sample_size"], now=now)
    write_state(state_path(cfg_dir), state)
    return state


# ── 看板快照（metrics 路由调用；只读 state 文件，不扫库）────────────────────

def metrics_snapshot(
    cfg: Optional[Mapping[str, Any]],
    cfg_dir: Path,
) -> Dict[str, Any]:
    """``/api/workspace/metrics`` 的 ``identity_shadow`` 段。

    未启用 → ``{"enabled": false}``（ops 卡整卡隐藏）；启用但还没扫过 →
    ``state_available=false``（卡显示「等待首次扫描」）；有 state → 原样并入。
    """
    pcfg = shadow_periodic_config(cfg)
    if not pcfg["enabled"]:
        return {"enabled": False}
    st = read_state(state_path(cfg_dir))
    out: Dict[str, Any] = {
        "enabled": True,
        "state_available": bool(st),
        "scan_interval_hours": pcfg["scan_interval_hours"],
        "max_rows": pcfg["max_rows"],
    }
    out.update(st)
    # 合流观测累计（人工确认/手动链的记忆迁移读数；独立文件，扫描不冲）
    try:
        from src.utils.identity_shadow_actions import load_totals, totals_path
        tot = load_totals(totals_path(cfg_dir))
        if any(int(tot.get(k) or 0) for k in (
                "confirm_pairs", "manual_links", "merged_rows",
                "cluster_relinked")):
            out["totals"] = tot
    except Exception:
        logger.debug("totals snapshot skipped", exc_info=True)
    return out


__all__: List[str] = [
    "STATE_FILENAME",
    "DEFAULT_SCAN_INTERVAL_HOURS", "DEFAULT_MAX_ROWS", "DEFAULT_SAMPLE_SIZE",
    "shadow_periodic_config", "resolve_inbox_db", "resolve_identity_db",
    "state_path", "build_state", "write_state", "read_state",
    "run_periodic_scan", "metrics_snapshot",
]
