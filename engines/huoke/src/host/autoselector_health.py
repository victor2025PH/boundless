# -*- coding: utf-8 -*-
"""AutoSelector cache 健康扫描 library (Phase 8, 2026-04-24).

MEMORY 里记录过 AutoSelector Pitfall: smart_tap 的自学习坐标 (fallback_coords)
会污染关键导航 selector, 历史上 "Search bar or search icon" 被学成 Messenger
图标坐标 (633, 96) 导致链路全错. Phase 6 已通过"不调 smart_tap + 硬编码 selector"
修复了搜索入口, 但 cache 里仍有遗留污染坐标 (data/selectors/*.yaml).

本 lib 静态扫描 cache 找可疑条目, 给出告警分级.

单独成 lib 是因为:
  * 单测友好 (用 tmp yaml 注入)
  * 可被 Dashboard/CI 复用
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


# 已知"导航类"key — 按 MEMORY 规则: 这些不该走 AutoSelector 学习,
# 应硬编码 selector + 坐标. cache 里有这些 key 时告警.
NAVIGATION_KEY_HINTS = (
    "Search bar",
    "Search icon",
    "Home tab",
    "Menu",
    "Back button",
    "Profile tab",
    "Notifications tab",
    "Messenger",
    "Messaging",
)


@dataclass
class Warning_:
    """单条告警."""
    severity: str          # "HIGH" / "MEDIUM" / "LOW"
    package: str           # com.facebook.katana
    key: str               # selector key
    issue: str             # 具体问题描述
    recommendation: str    # 建议处理动作


@dataclass
class ScanResult:
    scanned_yamls: int = 0
    scanned_keys: int = 0
    warnings: List[Warning_] = field(default_factory=list)

    @property
    def high_count(self) -> int:
        return sum(1 for w in self.warnings if w.severity == "HIGH")

    @property
    def medium_count(self) -> int:
        return sum(1 for w in self.warnings if w.severity == "MEDIUM")

    @property
    def low_count(self) -> int:
        return sum(1 for w in self.warnings if w.severity == "LOW")


def _is_navigation_key(key: str) -> bool:
    """key 描述 (如 'Search bar or search icon') 匹配导航 hints 任一."""
    k = (key or "").lower()
    return any(h.lower() in k for h in NAVIGATION_KEY_HINTS)


def _parse_learned_at(s: str) -> _dt.datetime:
    if not s:
        return _dt.datetime(1970, 1, 1)
    try:
        # 2026-04-23T17:06:06.368565+00:00 或 2026-04-20T22:45:00+00:00
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return _dt.datetime(1970, 1, 1)


def scan_selector_yaml(yaml_path: Path) -> List[Warning_]:
    """扫单个 YAML, 返回告警列表."""
    if not yaml_path.exists():
        return []
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return [Warning_(severity="HIGH",
                          package=yaml_path.stem,
                          key="(parse)",
                          issue=f"YAML parse 失败: {e}",
                          recommendation="手工修 YAML 语法")]

    pkg = data.get("package") or yaml_path.stem
    selectors = data.get("selectors") or {}
    warnings: List[Warning_] = []

    for key, entry in selectors.items():
        if not isinstance(entry, dict):
            continue
        fallback_coords = entry.get("fallback_coords")
        stats = entry.get("stats") or {}
        hits = int(stats.get("hits") or 0)
        misses = int(stats.get("misses") or 0)
        best = entry.get("best") or {}

        # Rule 1 (HIGH): 导航类 key 有 fallback_coords — 最高风险 (MEMORY 污染场景)
        if _is_navigation_key(key) and fallback_coords:
            warnings.append(Warning_(
                severity="HIGH", package=pkg, key=key,
                issue=f"导航类 key 有学习过的 fallback_coords={fallback_coords}, "
                       f"hits={hits}. 这是 AutoSelector 污染高发区 (历史上把"
                       f" 'Search bar' 学成了 Messenger 图标坐标).",
                recommendation="手工删除此 key 的 fallback_coords, 或整条删掉"
                                " 让它重新学习. 导航类硬编码 selector 更安全.",
            ))

        # Rule 2 (MEDIUM): 导航类 key 即使没 coords, 存在本身也应由生产硬编码
        elif _is_navigation_key(key):
            warnings.append(Warning_(
                severity="MEDIUM", package=pkg, key=key,
                issue=f"导航类 key 仍在 AutoSelector cache (hits={hits}). "
                       f"按 MEMORY 规则导航类应硬编码 selector, cache 只是冗余.",
                recommendation="生产若已切硬编码, 可安全删此条; 否则监控其 best "
                                "selector 与新版 app UI 的实际 desc/text 差异.",
            ))

        # Rule 3 (LOW): hits 很高但 misses == 0 且 learned_at 很旧 — 长期未更新
        if hits >= 50 and misses == 0:
            age_days = (_dt.datetime.now(_dt.timezone.utc)
                          - _parse_learned_at(entry.get("learned_at", ""))
                            .replace(tzinfo=_dt.timezone.utc)).days
            if age_days >= 30:
                warnings.append(Warning_(
                    severity="LOW", package=pkg, key=key,
                    issue=f"hits={hits}/misses=0, 学习于 {age_days} 天前未刷新. "
                           f"长期只走 cache 未再 vision 验证, app UI 若改版"
                           f" cache 可能已 stale.",
                    recommendation="考虑 30 天 TTL 自动失效策略, 或定期抽查 miss 1 次"
                                    " 触发 re-learn.",
                ))

        # Rule 4 (MEDIUM): best.description 带 'Facebook' 但设备可能 'Search'
        # 这个 rule 仅对 FB 相关做 heuristic check — 提示 potentially stale label
        best_desc = str(best.get("description") or "")
        if ("Facebook" in best_desc
                and ("search" in best_desc.lower() or "message" in best_desc.lower())):
            warnings.append(Warning_(
                severity="MEDIUM", package=pkg, key=key,
                issue=f"best.description='{best_desc}' 带 'Facebook' 字样, 新版"
                       f" FB katana 顶栏实际 desc 常为短名 (如 'Search' 而非 "
                       f"'Search Facebook').",
                recommendation="去真机 dump 看实际 desc, 必要时改 best 或 alts "
                                "覆盖多形态.",
            ))

    return warnings


def scan_all(selectors_dir: Path) -> ScanResult:
    """扫目录下所有 *.yaml."""
    result = ScanResult()
    if not selectors_dir.exists():
        return result
    for yf in sorted(selectors_dir.glob("*.yaml")):
        result.scanned_yamls += 1
        try:
            data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
            result.scanned_keys += len((data.get("selectors") or {}))
        except Exception:
            pass
        result.warnings.extend(scan_selector_yaml(yf))
    return result


def format_text_report(result: ScanResult) -> str:
    """控制台输出 — 按 severity 降序."""
    lines = [
        f"# AutoSelector Cache 健康扫描",
        f"- 扫描 {result.scanned_yamls} 个 YAML / {result.scanned_keys} 个 key",
        f"- HIGH: {result.high_count} / MEDIUM: {result.medium_count} /"
        f" LOW: {result.low_count}",
        "",
    ]
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    ranked = sorted(result.warnings, key=lambda w: (order.get(w.severity, 3),
                                                         w.package, w.key))
    if not ranked:
        lines.append("(无告警)")
    else:
        for w in ranked:
            lines += [
                f"## [{w.severity}] {w.package}: `{w.key}`",
                f"  issue: {w.issue}",
                f"  → {w.recommendation}",
                "",
            ]
    return "\n".join(lines)
