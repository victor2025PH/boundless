"""
AutoSelector — self-learning selector cache.

Flow:
  1st attempt: try cached selectors (from YAML)
  If miss:     use Vision → match to XML → learn new selector → save YAML
  Next time:   selector hits instantly (zero API cost)

Selector YAML structure (per package):
  data/selectors/<package>.yaml:
    package: com.facebook.katana
    selectors:
      "Send button":
        screen: .HomeActivity  (optional — scope to activity)
        best: {resourceId: "com.facebook.katana:id/send_button"}
        alts:
          - {description: "Send"}
          - {text: "Send"}
        stats: {hits: 42, misses: 1, last_hit: "2026-03-12T..."}
      "Search field":
        ...
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.host.device_registry import data_dir

from .backends import VisionBackend
from .screen_parser import ScreenParser, XMLParser, ParsedElement

log = logging.getLogger(__name__)

_DEFAULT_SELECTORS_DIR = data_dir() / "selectors"


_SELECTOR_KEY_ALIAS = {
    "content_desc": "description",
    "contentDescription": "description",
    "content-desc": "description",
    "content_desc_contains": "descriptionContains",
    "contentDescriptionContains": "descriptionContains",
    "content-desc-contains": "descriptionContains",
    "resource_id": "resourceId",
    "resource-id": "resourceId",
    "id": "resourceId",
    "text_contains": "textContains",
    "class_name": "className",
    "package_name": "packageName",
}


# Sprint 3 P2 真机验证发现:dump_hierarchy 同时返回当前 app + 系统状态栏 +
# launcher 等的元素。当目标 app 还没加载完(如 Facebook 启动到 LoginActivity)
# 而要查找 "Home tab" 时,_xml_text_search 会优先命中 com.android.systemui:id/home
# (导航条的 Home 键),把它当成 Facebook 的 Home tab 学进缓存,导致后续点击系
# 统按钮而非 app 内的 tab。这里把这些"显然不是业务 app"的包列入污染黑名单,
# 学习时如果 selector 的 packageName 在此名单内则**拒绝学习**。
_LEARN_BLACKLIST_PACKAGES = {
    "com.android.systemui",
    "com.android.systemui.navigationbar",
    "com.android.intentresolver",
    "com.android.launcher",
    "com.android.launcher2",
    "com.android.launcher3",
    "com.miui.home",
    "com.miui.systemui",
    "com.miui.systemAdSolution",
    "com.miui.securitycenter",
    "com.miui.notification",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "android",
    "com.android.shell",
    "com.android.providers.settings",
}


def _selector_is_polluted(sel: Dict[str, Any], expected_package: str) -> bool:
    """判断学到的 selector 是否被系统/launcher 元素污染。

    判断依据:
      1. selector 显式声明了 packageName 且不在 expected_package 下
      2. selector 的 resourceId 以已知系统/launcher 包前缀开头
      3. resourceId 末段是 release-build dump 的占位符 `(name removed)`
         (典型如 `com.facebook.katana:id/(name removed)`,所有混淆 id 都叫
         同一名字,无法用作精准 selector,必须淘汰)
      4. xml_match.package(若可获取)在黑名单内
    """
    if not isinstance(sel, dict):
        return False
    pkg = (sel.get("packageName") or "").strip()
    if pkg:
        if pkg in _LEARN_BLACKLIST_PACKAGES:
            return True
        if expected_package and pkg != expected_package:
            return True
    rid = (sel.get("resourceId") or "").strip()
    if rid:
        for bad in _LEARN_BLACKLIST_PACKAGES:
            if rid.startswith(bad + ":"):
                return True
        if expected_package and ":" in rid:
            owner = rid.split(":", 1)[0]
            if owner != expected_package and owner in _LEARN_BLACKLIST_PACKAGES:
                return True
        # 真机修复:release APK 把 id 名混淆成 `(name removed)`,这种 id
        # 在 dump_hierarchy 里到处都是,绝对不能当 best,会污染所有 selector。
        rid_lc = rid.lower()
        if rid_lc.endswith("(name removed)") or "(name removed)" in rid_lc:
            return True
    return False


def _normalize_selector_dict(sel: Any) -> Dict[str, Any]:
    """把 YAML 种子里的人类友好 key 映射成 uiautomator2 兼容 key。

    Sprint 3 P2 真机验证发现:之前种子文件用 `content_desc` / `resource_id`,
    但 uiautomator2 的 selector 接口需要 `description` / `resourceId`,导致
    所有 katana 种子失效。这里在加载时统一规范化,避免每个使用方各自处理。
    """
    if not isinstance(sel, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in sel.items():
        if v is None or v == "":
            continue
        canonical = _SELECTOR_KEY_ALIAS.get(k, k)
        out[canonical] = v
    return out


def _normalize_selector_list(sels: Any) -> List[Dict[str, Any]]:
    if not isinstance(sels, list):
        return []
    return [_normalize_selector_dict(s) for s in sels if s]


# ---------------------------------------------------------------------------
# Selector entry
# ---------------------------------------------------------------------------

@dataclass
class SelectorEntry:
    """A single learned selector for a UI target."""

    target: str
    best: Dict[str, str] = field(default_factory=dict)
    alts: List[Dict[str, str]] = field(default_factory=list)
    fallback_coords: Optional[Tuple[int, int]] = None
    screen: str = ""
    hits: int = 0
    misses: int = 0
    last_hit: str = ""
    learned_at: str = ""

    @property
    def confidence(self) -> float:
        total = self.hits + self.misses
        if total == 0:
            return 0.5
        return self.hits / total

    def all_selectors(self) -> List[Dict[str, str]]:
        result = []
        if self.best:
            result.append(self.best)
        result.extend(self.alts)
        return result


# ---------------------------------------------------------------------------
# Selector store (YAML persistence)
# ---------------------------------------------------------------------------

class SelectorStore:
    """Thread-safe YAML-backed selector store, one file per package."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._dir = base_dir or _DEFAULT_SELECTORS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, SelectorEntry]] = {}

    def _file(self, package: str) -> Path:
        safe_name = package.replace(".", "_")
        return self._dir / f"{safe_name}.yaml"

    def load(self, package: str) -> Dict[str, SelectorEntry]:
        with self._lock:
            if package in self._cache:
                return self._cache[package]

            path = self._file(package)
            if not path.exists():
                self._cache[package] = {}
                return self._cache[package]

            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                entries: Dict[str, SelectorEntry] = {}
                for target, data in raw.get("selectors", {}).items():
                    entries[target] = SelectorEntry(
                        target=target,
                        best=_normalize_selector_dict(data.get("best", {})),
                        alts=_normalize_selector_list(data.get("alts", [])),
                        fallback_coords=tuple(data["fallback_coords"])
                        if data.get("fallback_coords") else None,
                        screen=data.get("screen", ""),
                        hits=data.get("stats", {}).get("hits", 0),
                        misses=data.get("stats", {}).get("misses", 0),
                        last_hit=data.get("stats", {}).get("last_hit", ""),
                        learned_at=data.get("learned_at", ""),
                    )
                self._cache[package] = entries
                return entries
            except Exception as e:
                log.warning("Failed to load selectors for %s: %s", package, e)
                self._cache[package] = {}
                return self._cache[package]

    def save(self, package: str):
        with self._lock:
            entries = self._cache.get(package, {})
            data = {
                "package": package,
                "updated": datetime.now(timezone.utc).isoformat(),
                "selectors": {},
            }
            for target, entry in entries.items():
                data["selectors"][target] = {
                    "best": entry.best,
                    "alts": entry.alts,
                    "fallback_coords": list(entry.fallback_coords)
                    if entry.fallback_coords else None,
                    "screen": entry.screen,
                    "learned_at": entry.learned_at,
                    "stats": {
                        "hits": entry.hits,
                        "misses": entry.misses,
                        "last_hit": entry.last_hit,
                    },
                }

            path = self._file(package)
            path.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )

    def get(self, package: str, target: str) -> Optional[SelectorEntry]:
        entries = self.load(package)
        return entries.get(target)

    def put(self, package: str, entry: SelectorEntry):
        with self._lock:
            if package not in self._cache:
                self._cache[package] = {}
            self._cache[package][entry.target] = entry
        self.save(package)

    def record_hit(self, package: str, target: str):
        with self._lock:
            entries = self._cache.get(package, {})
            if target in entries:
                entries[target].hits += 1
                entries[target].last_hit = datetime.now(timezone.utc).isoformat()
        self.save(package)

    def record_miss(self, package: str, target: str):
        with self._lock:
            entries = self._cache.get(package, {})
            if target in entries:
                entries[target].misses += 1
        self.save(package)

    def invalidate_entry(self, package: str, target: str):
        """Remove a single selector entry (for auto-relearn after staleness)."""
        with self._lock:
            entries = self._cache.get(package, {})
            if target in entries:
                del entries[target]
        self.save(package)

    def list_packages(self) -> List[str]:
        return [
            f.stem.replace("_", ".")
            for f in self._dir.glob("*.yaml")
        ]

    def stats(self, package: str) -> Dict[str, Any]:
        entries = self.load(package)
        return {
            "package": package,
            "total_selectors": len(entries),
            "entries": {
                t: {"confidence": e.confidence, "hits": e.hits, "misses": e.misses}
                for t, e in entries.items()
            },
        }

    def health_report(self) -> Dict[str, Any]:
        """Return health statistics across all packages for monitoring."""
        packages = self.list_packages()
        total_selectors = 0
        stale_selectors = 0
        healthy_selectors = 0
        stale_list = []
        for pkg in packages:
            entries = self.load(pkg)
            for target, entry in entries.items():
                total_selectors += 1
                if entry.confidence < 0.4 and entry.misses >= 3:
                    stale_selectors += 1
                    stale_list.append({
                        "package": pkg,
                        "target": target,
                        "confidence": round(entry.confidence, 2),
                        "hits": entry.hits,
                        "misses": entry.misses,
                    })
                else:
                    healthy_selectors += 1
        return {
            "total_packages": len(packages),
            "total_selectors": total_selectors,
            "healthy": healthy_selectors,
            "stale": stale_selectors,
            "stale_details": stale_list,
        }


# ---------------------------------------------------------------------------
# AutoSelector — the main API
# ---------------------------------------------------------------------------

# 受保护的 target 列表 — smart_tap 仍可即时找元素并 click,
# 但 **不允许写学习 cache**, 防止污染重灾区. 适用于 "已经有硬编码
# helper 替代主路径, smart_tap 只是 fallback" 的场景.
#
# 2026-04-27 加 (社群客服拓展 5h 死循环事故 R3 治本):
# 这些 target 历史上反复被 AutoSelector 学错 (4-23 第一次, 4-26 又来
# 一次), 见 memory/autoselector_pitfall.md. 现已有硬编码 helper:
#
# 社群客服拓展 / Groups 相关流程:
#   "Search bar or search icon"        → _tap_search_bar_preferred
#   "Groups tab"                       → _tap_groups_bottom_tab
#   "Groups tab or filter"             → _tap_search_results_groups_filter
#   "Members tab in the group header"  → _tap_group_members_tab
#   "First matching group"             → _tap_first_search_result_group
#   "Your groups"                      → _tap_your_groups_entry
#   "Group name or icon at top to open info"  → enter_group + 自检兜底
#
# 加新 helper 后请同步加入这个集合; 撤销 helper 时也要从集合移除
# (否则没主路径 + 不学 cache, smart_tap 会永远依赖 Vision 路径, 浪费 API).
_PROTECTED_TARGETS: frozenset = frozenset({
    "Search bar or search icon",
    "Groups tab",
    "Groups tab or filter",
    "Members tab in the group header",
    "First matching group",
    "Your groups",
    "Group name or icon at top to open info",
})


class AutoSelector:
    """
    Intelligent element finder: cached selectors → Vision learning → save.

    Features:
        - Cached selector fast path (zero API cost)
        - Vision fallback when cache misses
        - Auto-relearn: when a selector's confidence drops below threshold,
          automatically invalidate and re-learn via Vision
        - Protected targets: smart_tap can still resolve via Vision, but
          will NOT write learned selectors back (防污染). See _PROTECTED_TARGETS.

    Usage:
        auto = AutoSelector(vision_backend)
        element = auto.find(device, "com.facebook.katana", "Send button")
        # First time: uses Vision, learns selector
        # Second time: instant selector hit
        # After app update: detects staleness, re-learns automatically
    """

    # Confidence below this triggers automatic re-learning via Vision
    RELEARN_CONFIDENCE_THRESHOLD = 0.4
    # Minimum misses before considering re-learn (avoid triggering on first miss)
    RELEARN_MIN_MISSES = 3

    def __init__(self, backend: Optional[VisionBackend] = None,
                 store: Optional[SelectorStore] = None,
                 selectors_dir: Optional[Path] = None):
        self._store = store or SelectorStore(selectors_dir)
        self._parser = ScreenParser(backend)
        self._backend = backend

    @property
    def store(self) -> SelectorStore:
        return self._store

    def find(self, device, package: str, target: str,
             context: str = "",
             learn: bool = True) -> Optional[ParsedElement]:
        """
        Find an element, using cached selectors first, then Vision.

        Args:
            device: u2 device connection
            package: Android package name
            target: human-readable description ("Send button", "Search field")
            context: optional screen context for Vision
            learn: if True, save newly discovered selectors

        Returns ParsedElement on success, None if not found.
        """
        # 0. protected target: 跳过 cache 读取 (防历史污染 cache 持续作恶) +
        # 跳过学习写入 (本次 Vision 找到的也不写). smart_tap 仍可用 Vision
        # 路径正常 click. 适用于"已有硬编码 helper 主路径, smart_tap 仅
        # fallback" 的关键入口, 见 _PROTECTED_TARGETS.
        is_protected = target in _PROTECTED_TARGETS

        # 1. try cached selectors (skip for protected)
        entry = None if is_protected else self._store.get(package, target)
        if entry:
            result = self._try_selectors(device, entry)
            if result:
                self._store.record_hit(package, target)
                log.info("AutoSelector cache HIT: %s → %s", target, entry.best)
                return result
            log.info("AutoSelector cache MISS: %s (selectors stale?, tried best=%s + %d alts)",
                     target, entry.best, len(entry.alts))
            self._store.record_miss(package, target)

            # Auto-relearn: if confidence dropped below threshold, invalidate
            # and force Vision re-learning
            if self._should_relearn(entry):
                log.warning(
                    "AutoSelector AUTO-RELEARN: %s/%s confidence=%.2f "
                    "(misses=%d) — invalidating stale selector",
                    package, target, entry.confidence, entry.misses,
                )
                self._store.invalidate_entry(package, target)
                entry = None  # force Vision path below

        # 2. use Vision + XML fusion to find the element
        parsed = self._parser.find(device, target, context)
        if not parsed:
            return None

        # 2.5 protected target: 跳过学习写入 (Step 0 已跳过 cache 读取).
        # smart_tap 仍可正常 click (用 parsed.center), 但 cache 保持空白 →
        # 下次 smart_tap 还会走 Vision (不会因学错 cache 持续点错位置).
        if learn and is_protected:
            log.info("AutoSelector PROTECTED: %s/%s 关键入口受保护, 跳过学习写入 "
                     "(由硬编码 helper 处理主路径, smart_tap 仅 Vision fallback)",
                     package, target)
            return parsed

        # 3. learn the selector for next time
        if learn and parsed.selectors:
            # Sprint 5 P2-1 新增 防线 ⓪ : bounds sanity check。
            # 真机 s4_5 回归暴露:XML 有时会把"底部 toolbar 小图标""状态栏角标"
            # 当成业务主按钮返回(尤其是 description 模糊匹配),bounds 宽高都 < 40px。
            # 业务主控件(搜索栏/登录按钮/发送按钮)尺寸通常 >= 60x40。
            # 如果 bounds.top < 80 (状态栏区) 且宽度 < 150,几乎都是系统级小元素,拒学。
            try:
                if parsed.xml is not None:
                    l, t, r, b = parsed.xml.bounds or (0, 0, 0, 0)
                    w = max(0, r - l)
                    h = max(0, b - t)
                    # 规则:
                    # - too_tiny: w<30 AND h<30 (任何业务控件都不可能这么小)
                    # - status_bar_only: 整个元素落在顶部 60px 以内(标准状态栏高度) AND w<200
                    # - zero_area: bounds 全 0(异常值)
                    too_tiny = (0 < w < 30 and 0 < h < 30)
                    status_bar_only = (b < 80 and w < 200 and h > 0)
                    zero_area = (w == 0 and h == 0 and (l + t + r + b) != 0)
                    if too_tiny or status_bar_only or zero_area:
                        log.warning(
                            "AutoSelector SKIP-LEARN (bounds): %s/%s "
                            "bounds=(%d,%d,%d,%d) w=%d h=%d "
                            "(too_tiny=%s status_bar=%s zero=%s),拒学",
                            package, target, l, t, r, b, w, h,
                            too_tiny, status_bar_only, zero_area,
                        )
                        return parsed
            except Exception:
                pass
            # Sprint 3 P2 真机修复:dump_hierarchy 会把系统状态栏 / launcher / 输入法
            # 都吐回来,如果目标 app 此刻没有匹配元素,XML 搜索会命中系统包的同名元素
            # (典型:"Home tab" 命中 systemui 的导航条 home 键),被错误学到 cache。
            # 防线 ① :若 parsed.xml.package 不在期望 package 下且属于黑名单,直接拒学
            xml_pkg = ""
            try:
                if parsed.xml is not None:
                    xml_pkg = (getattr(parsed.xml, "package", "") or "").strip()
            except Exception:
                pass
            if (xml_pkg
                and package
                and xml_pkg != package
                and xml_pkg in _LEARN_BLACKLIST_PACKAGES):
                log.warning(
                    "AutoSelector SKIP-LEARN: %s/%s 命中元素来自系统/launcher "
                    "包 %s(期望 %s),很可能 %s 还未加载完成,本次不写入 cache",
                    package, target, xml_pkg, package, package,
                )
                return parsed
            # 防线 ② :对单个 selector 字段做包名过滤,丢弃显然不属于业务 app 的候选
            normalized = [_normalize_selector_dict(s) for s in parsed.selectors]
            clean = [s for s in normalized if s and not _selector_is_polluted(s, package)]
            dropped = len(normalized) - len(clean)
            if not clean:
                log.warning(
                    "AutoSelector SKIP-LEARN: %s/%s 候选 %d 个全部被污染过滤"
                    "(可能 dump_hierarchy 命中了系统/launcher 元素而非 %s),"
                    "本次不写入 cache",
                    package, target, len(normalized), package,
                )
                return parsed
            if dropped:
                log.info(
                    "AutoSelector LEARN-FILTER: %s/%s 过滤掉 %d 个污染候选,"
                    "保留 %d 个真实属于 %s 的 selector",
                    package, target, dropped, len(clean), package,
                )
            new_entry = SelectorEntry(
                target=target,
                best=clean[0],
                alts=clean[1:],
                fallback_coords=parsed.center if parsed.center != (0, 0) else None,
                screen=self._get_current_activity(device, package),
                learned_at=datetime.now(timezone.utc).isoformat(),
                hits=1,
            )
            self._store.put(package, new_entry)
            log.info("AutoSelector LEARNED: %s → %s", target, new_entry.best)

        return parsed

    def _should_relearn(self, entry: SelectorEntry) -> bool:
        """Check if a selector is stale enough to trigger automatic re-learning."""
        if entry.misses < self.RELEARN_MIN_MISSES:
            return False
        return entry.confidence < self.RELEARN_CONFIDENCE_THRESHOLD

    def find_all(self, device, package: str,
                 context: str = "",
                 use_vision: bool = False) -> List[ParsedElement]:
        """Parse the full screen and return all interactive elements."""
        return self._parser.parse(device, use_vision=use_vision, context=context)

    def invalidate(self, package: str, target: Optional[str] = None):
        """Invalidate cached selectors (after app update, etc.)."""
        if target:
            entries = self._store.load(package)
            entries.pop(target, None)
            self._store.save(package)
        else:
            # invalidate all for this package
            entries = self._store.load(package)
            entries.clear()
            self._store.save(package)

    # -- internal -----------------------------------------------------------

    def _try_selectors(self, device, entry: SelectorEntry) -> Optional[ParsedElement]:
        """Try each selector in order, return first match."""
        for sel in entry.all_selectors():
            try:
                el = device(**sel)
                if el.exists(timeout=2):
                    info = el.info
                    bounds = info.get("bounds", {})
                    from .screen_parser import XMLElement
                    xml_el = XMLElement(
                        resource_id=sel.get("resourceId", ""),
                        text=sel.get("text", info.get("text", "")),
                        content_desc=sel.get("description", info.get("contentDescription", "")),
                        class_name=info.get("className", ""),
                        bounds=(
                            bounds.get("left", 0), bounds.get("top", 0),
                            bounds.get("right", 0), bounds.get("bottom", 0),
                        ),
                        clickable=info.get("clickable", False),
                        enabled=info.get("enabled", True),
                    )
                    return ParsedElement(
                        xml=xml_el,
                        semantic_label=entry.target,
                        match_confidence=entry.confidence,
                        selectors=entry.all_selectors(),
                    )
            except Exception:
                continue

        # try fallback coordinates
        if entry.fallback_coords:
            return ParsedElement(
                semantic_label=entry.target,
                match_confidence=0.3,
                selectors=entry.all_selectors(),
            )

        return None

    def sweep_stale_selectors(self, package: str,
                              stale_days: int = 7) -> dict:
        """
        主动健康检查：找出超过 stale_days 天未被验证的选择器并清除。
        在 TikTok 等 App 发版后调用，避免等到任务失败才发现选择器失效。

        Args:
            package: 包名，如 "com.zhiliaoapp.musically"
            stale_days: 超过此天数未命中的选择器视为过期

        Returns:
            {"swept": N, "total": M, "package": package}
        """
        cutoff = time.time() - stale_days * 86400
        entries = self._store.load(package)
        swept = 0
        stale_targets = []

        for target, entry in list(entries.items()):
            last_hit_str = (entry.stats or {}).get("last_hit", "")
            if not last_hit_str:
                # 从未命中 → 检查 learned_at
                learned_str = getattr(entry, "learned_at", "") or ""
                if not learned_str:
                    stale_targets.append(target)
                    continue
                try:
                    from datetime import datetime, timezone
                    learned_dt = datetime.fromisoformat(
                        learned_str.replace("Z", "+00:00")
                    )
                    if learned_dt.timestamp() < cutoff:
                        stale_targets.append(target)
                except Exception:
                    stale_targets.append(target)
                continue

            try:
                from datetime import datetime, timezone
                last_hit_dt = datetime.fromisoformat(
                    last_hit_str.replace("Z", "+00:00")
                )
                if last_hit_dt.timestamp() < cutoff:
                    stale_targets.append(target)
            except Exception:
                stale_targets.append(target)

        for target in stale_targets:
            self._store.invalidate_entry(package, target)
            swept += 1
            log.info("[AutoSelector] 主动清除过期选择器: %s/%s (>%d天未用)",
                     package, target, stale_days)

        if swept:
            self._store.save(package)

        return {"swept": swept, "total": len(entries), "package": package}

    @staticmethod
    def _get_current_activity(device, package: str) -> str:
        try:
            info = device.app_current()
            activity = info.get("activity", "")
            if activity:
                return activity
        except Exception:
            pass
        return ""


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[AutoSelector] = None
_lock = threading.Lock()


def get_auto_selector(backend: Optional[VisionBackend] = None) -> AutoSelector:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = AutoSelector(backend)
    return _instance
