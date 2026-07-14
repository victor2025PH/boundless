# -*- coding: utf-8 -*-
"""
Facebook 搜索相关 uiautomator2 selector 字典（单源）。

供 ``facebook.FacebookAutomation``、``scripts/w0_capture_direct.py`` 共用，
避免 Home 顶栏 / 搜索页 / People 筛选 的 selector 在多处拷贝漂移。

更新任一列表后，两边行为自动对齐；注释里保留与 katana 版本的对应关系说明。
支持 config/apps/facebook.yaml 的 ui_selectors 节热重载覆盖：
  当 FB 更新 UI 时，在 YAML 中添加新选择器即生效，无需重启服务。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# u2 的 kwargs 类型（便于类型检查与 IDE）
Selector = Dict[str, Any]

_log = logging.getLogger(__name__)

# ── YAML 热重载 — facebook.yaml 的 ui_selectors 覆盖节 ───────────────────────
_FB_YAML_PATH = Path(__file__).parent.parent.parent / "config" / "apps" / "facebook.yaml"
_yaml_overrides: Dict[str, Tuple[Selector, ...]] = {}
_yaml_mtime: float = 0.0
_yaml_lock = threading.Lock()


def _load_yaml_overrides() -> None:
    """从 config/apps/facebook.yaml 的 ui_selectors 节加载可配置选择器 (mtime 热重载).

    YAML 中定义的选择器会被 prepend 到代码默认列表前，优先级最高。
    """
    global _yaml_overrides, _yaml_mtime
    try:
        if not _FB_YAML_PATH.exists():
            return
        mtime = _FB_YAML_PATH.stat().st_mtime
        if mtime <= _yaml_mtime:
            return
        data = yaml.safe_load(_FB_YAML_PATH.read_text(encoding="utf-8")) or {}
        ui = data.get("ui_selectors") or {}
        loaded: Dict[str, Tuple[Selector, ...]] = {}
        for key, items in ui.items():
            if isinstance(items, list) and items:
                loaded[key] = tuple(items)
        with _yaml_lock:
            _yaml_overrides = loaded
            _yaml_mtime = mtime
        if loaded:
            _log.info("[fb_selectors] 热重载 YAML 覆盖: %s",
                      {k: len(v) for k, v in loaded.items()})
    except Exception as exc:
        _log.debug("[fb_selectors] YAML 加载失败 (ignored): %s", exc)


def _with_yaml_prefix(key: str, defaults: Tuple[Selector, ...]) -> Tuple[Selector, ...]:
    """将 YAML 覆盖选择器 prepend 到 Python 默认列表前返回合并结果。"""
    _load_yaml_overrides()  # mtime 检测，开销极低
    with _yaml_lock:
        prefix = _yaml_overrides.get(key, ())
    return prefix + defaults if prefix else defaults


# ── 公开 getter （支持热重载）— 调用方使用此函数替代直接引用常量 ─────────────
def get_home_search_button_selectors() -> Tuple[Selector, ...]:
    return _with_yaml_prefix("home_search_button", FB_HOME_SEARCH_BUTTON_SELECTORS)


def get_fallback_search_tap_selectors() -> Tuple[Selector, ...]:
    return _with_yaml_prefix("fallback_search_tap", FB_FALLBACK_SEARCH_TAP_SELECTORS)


def get_search_query_editor_selectors() -> Tuple[Selector, ...]:
    return _with_yaml_prefix("search_query_editor", FB_SEARCH_QUERY_EDITOR_SELECTORS)


def get_people_tab_selectors() -> Tuple[Selector, ...]:
    return _with_yaml_prefix("people_tab", FB_PEOPLE_TAB_SELECTORS)


# 模块导入时加载一次
_load_yaml_overrides()

# ── 从 Home 打开「搜索页」：顶栏 Search（2026-04-23 katana 实测为 Button，非 EditText）──
# 2026-04-24 v2: zh-CN 优先 (实测当前 FB katana 中文), 省 10s/task
FB_HOME_SEARCH_BUTTON_SELECTORS: Tuple[Selector, ...] = (
    {"className": "android.widget.Button", "description": "搜索"},
    {"description": "搜索", "clickable": True},
    {"className": "android.widget.Button", "description": "搜索 Facebook"},
    {"description": "搜索 Facebook", "clickable": True},
    {"className": "android.widget.Button", "description": "搜索Facebook"},
    {"description": "搜索Facebook", "clickable": True},
    {"className": "android.widget.Button", "description": "Search"},
    {"description": "Search", "clickable": True},
    {"className": "android.widget.Button", "description": "Search Facebook"},
    {"description": "Search Facebook", "clickable": True},
)

# ── ``_fallback_search_tap``：主循环全失败后的兜底 ─────────────────────
FB_FALLBACK_SEARCH_TAP_SELECTORS: Tuple[Selector, ...] = (
    {"description": "Search Facebook"},
    {"description": "搜索 Facebook"},
    {"description": "搜索Facebook"},
    {"description": "搜索"},
    {"description": "Search"},
    {"resourceId": "com.facebook.katana:id/search_bar_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar"},
)

# ── 已进入搜索/半展开界面时的补充尝试（W0 直连脚本合并遍历用）──────────
FB_SEARCH_SURFACE_EXTRA_SELECTORS: Tuple[Selector, ...] = (
    {"resourceId": "com.facebook.katana:id/search_query_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar"},
    {"resourceId": "com.facebook.katana:id/search_button"},
    {"className": "android.widget.EditText", "description": "Search Facebook"},
    {"className": "android.widget.EditText", "description": "搜索Facebook"},
    {"className": "android.widget.EditText", "description": "搜索 Facebook"},
)

# ── ``search_people``：在搜索页内向 EditText 写入 query（set_text）────
# 2026-04-24 简化: 前两个 selector 在新版 FB katana 永远 0 candidates —
#   resource-id 被混淆成 "(name removed)",
#   EditText 实际 content-desc 为空 (hint/text 都是 'Search', 不是 'Search Facebook').
# 反而 poll 期间偶现假阳性匹配到错 EditText → set_text 无效位置导致搜索失败.
# 实测只用 {className: EditText} 单字段稳定 work (搜索页只有 1 个顶部 EditText).
FB_SEARCH_QUERY_EDITOR_SELECTORS: Tuple[Selector, ...] = (
    {"className": "android.widget.EditText"},
)

# ── ``search_people``：People 筛选条 ───────────────────────────────────
# 2026-04-24 追加中文本地化 '用户' / '人' 变体
FB_PEOPLE_TAB_SELECTORS: Tuple[Selector, ...] = (
    {"descriptionContains": "People search results"},
    {"descriptionContains": "用户搜索结果"},
    {"text": "People"},
    {"text": "用户"},
    {"text": "人"},
    # 日文等界面可能只有 content-desc 含 People，无英文精确 text
    {"descriptionContains": "People"},
    {"descriptionContains": "用户"},
)
