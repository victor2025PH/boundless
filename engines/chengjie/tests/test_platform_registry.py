# -*- coding: utf-8 -*-
"""平台注册表门禁（实施96 P0-3）：注册表自洽 + 仍散落的各平台表与注册表不漂移。

哲学与 ``test_platform_matrix`` 相同：人写的平台表必然漂移，所以由门禁把它们钉在
同一份事实源上——改散表不改注册表（或反之）即红。已知的历史漂移（同一个 Telegram 蓝
三个值）登记在 ``ACCEPTED_COLOR_DRIFT``，**带原因**，收口后删掉对应行（删了门禁仍绿
才算真收口）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.integrations import platform_registry as reg

ROOT = Path(__file__).resolve().parents[1]

# (文件, 平台) → 该文件里目前写着的色值（小写比较）。原因：历史手写表，DY 线 UI 批统一收口。
ACCEPTED_COLOR_DRIFT = {
    ("src/web/templates/unified_inbox.html", "telegram"): "#2ba5e0",
    ("src/web/templates/unified_inbox.html", "line"): "#00c300",
    ("src/web/templates/workspace_base.html", "telegram"): "#2aabee",
    ("src/web/templates/workspace_base.html", "messenger"): "#a334fa",
}

# 「规划中 → 事实卡必须列为暂不支持」只对本线（实施96 / TK-1）引入的平台强制；
# qq / wechat 是实施97 线登记的规划项，其事实卡措辞（「QQ 个人号」「个人微信」）随该线落地。
FACTS_PLANNED_ENFORCED = {"douyin", "tiktok"}
# 事实卡「暂不支持」清单里还没登记、但注册表已标规划中的平台 → 原因。
# TikTok：事实卡措辞「…等国内平台」容不下 TikTok，随 TK-1 事实卡分层口径一并补。
FACTS_UNSUPPORTED_PENDING = {"TikTok": "TK-1 事实卡分层口径"}


def _js_obj_pairs(src: str, decl_regex: str) -> dict:
    """抓 ``var X = {a:"#fff", b:'y'};`` 这类一行/多行对象字面量的键值对。"""
    m = re.search(decl_regex + r"\s*=\s*\{(.*?)\}\s*;", src, re.S)
    assert m, f"declaration not found: {decl_regex}"
    body = m.group(1)
    out = {}
    for k, v in re.findall(r"""["']?([A-Za-z_][\w-]*)["']?\s*:\s*["']([^"']*)["']""", body):
        out[k] = v
    return out


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 1. 自洽 ────────────────────────────────────────────────────────────────────

def test_registry_validates_clean():
    assert reg.validate() == []


def test_ids_and_aliases_resolve():
    assert reg.normalize_platform_id(" TG ") == "telegram"
    assert reg.normalize_platform_id("fb") == "messenger"
    assert reg.normalize_platform_id("line_rpa") == "line"
    assert reg.normalize_platform_id("aweme") == "douyin"
    assert reg.normalize_platform_id("nope") == "nope"  # 未知原样、不猜
    assert reg.get("nope") is None
    assert reg.display_name("nope") == "Nope"  # 与 normalizer 旧回落同口径


def test_planned_platforms_are_registered_but_not_implemented():
    planned = set(reg.planned_ids())
    assert {"douyin", "tiktok"} <= planned
    assert not (planned & set(reg.implemented_ids()))
    assert reg.get("tiktok").region_aware is True


# ── 2. 散表不漂移 ───────────────────────────────────────────────────────────────

def test_platform_login_supported_platforms_are_implemented_in_registry():
    from src.integrations.platform_login import DEFAULT_PLATFORM_MODES, SUPPORTED_PLATFORMS
    impl = set(reg.implemented_ids())
    for p in SUPPORTED_PLATFORMS:
        assert p in impl, f"platform_login.SUPPORTED_PLATFORMS 有 {p!r}，注册表未登记/未标 implemented"
    for p, row in DEFAULT_PLATFORM_MODES.items():
        spec = reg.get(p)
        assert spec is not None, f"DEFAULT_PLATFORM_MODES 有 {p!r}，注册表未登记"
        assert set(row.get("modes") or []) <= set(spec.modes), (
            f"{p}: platform_login modes {row.get('modes')} 超出注册表 {spec.modes}")
        if row.get("modes"):
            assert row.get("default") == spec.default_mode, (
                f"{p}: default {row.get('default')!r} != 注册表 {spec.default_mode!r}")


def test_platform_readiness_implemented_modes_match_registry():
    from src.integrations.platform_readiness import _IMPLEMENTED_MODES
    impl = set(reg.implemented_ids())
    for p, m in _IMPLEMENTED_MODES:
        spec = reg.get(p)
        assert spec is not None and p in impl, f"_IMPLEMENTED_MODES 有 {p!r}，注册表未登记为 implemented"
        assert m in spec.modes or m == "web", f"{p}:{m} 不在注册表 modes {spec.modes}"


def test_normalizer_display_matches_registry():
    from src.inbox.normalizer import PLATFORM_DISPLAY
    for p, name in PLATFORM_DISPLAY.items():
        assert reg.display_name(p) == name, f"{p}: normalizer {name!r} != 注册表 {reg.display_name(p)!r}"


def _codebase_claims_supported() -> set:
    """代码库当前对外声称支持的平台（``platform_login.SUPPORTED_PLATFORMS`` + web）。

    「注册表标 implemented → 图标 / 事实卡必须到位」这一向的要求只对这些平台强制：
    共享工作树里他线可能先把平台登进注册表、图标与事实卡随后落地（或反之），
    以 SUPPORTED_PLATFORMS 为「已对外声称」的锚点，HEAD 与工作树两种状态下都成立。
    """
    from src.integrations.platform_login import SUPPORTED_PLATFORMS
    return set(SUPPORTED_PLATFORMS) | {"web"}


def test_platform_icons_js_colors_and_names_match_registry():
    src = _read("src/web/static/platform_icons.js")
    colors = _js_obj_pairs(src, r"var COLORS")
    names = _js_obj_pairs(src, r"var NAMES")
    claimed = _codebase_claims_supported()
    for spec in reg.all_platforms():
        key = spec.icon_key()
        if spec.implemented and spec.id in claimed:
            assert key in colors, f"platform_icons.js COLORS 缺 {key}（{spec.id}）"
        if key in colors and key == spec.id:
            assert colors[key].lower() == spec.color.lower(), (
                f"platform_icons.js COLORS.{key}={colors[key]} != 注册表 {spec.color}")
        if key in names and key == spec.id:
            assert names[key] == spec.name, f"platform_icons.js NAMES.{key}={names[key]!r} != {spec.name!r}"


def test_desktop_platform_icons_copy_tracks_web_version():
    """桌面壳 ``desktop/renderer/platform-icons.js`` 是 web 版的手工副本（历史上靠人记）。

    规则（严于「分叉」、宽于「滞后」）：两份文件里**共有**的键色/名必须同值——已同步的部分
    不许各改各的；桌面副本**暂缺**某键只记为滞后不判红（共享工作树里他线常先落 web 图标、
    收工时再同步桌面副本，HEAD 与工作树会短暂不一致）。滞后清单在报告里点名，由收口批补齐。
    """
    web = _read("src/web/static/platform_icons.js")
    desk = _read("desktop/renderer/platform-icons.js")
    web_c, web_n = _js_obj_pairs(web, r"var COLORS"), _js_obj_pairs(web, r"var NAMES")
    desk_c, desk_n = _js_obj_pairs(desk, r"var COLORS"), _js_obj_pairs(desk, r"var NAMES")
    for k in set(web_c) & set(desk_c):
        assert desk_c[k] == web_c[k], f"desktop/web platform-icons.js COLORS.{k} 分叉：{desk_c[k]} vs {web_c[k]}"
    for k in set(web_n) & set(desk_n):
        assert desk_n[k] == web_n[k], f"desktop/web platform-icons.js NAMES.{k} 分叉：{desk_n[k]!r} vs {web_n[k]!r}"
    lag = sorted((set(web_c) - set(desk_c)) | (set(web_n) - set(desk_n)))
    if lag:
        print(f"[platform_registry] 桌面 platform-icons.js 滞后于 web 版的键（待同步）：{lag}")


def test_unified_inbox_pn_pc_tables_match_registry():
    rel = "src/web/templates/unified_inbox.html"
    src = _read(rel)
    pc = _js_obj_pairs(src, r"const PC")
    pn = _js_obj_pairs(src, r"const PN")
    for p, name in pn.items():
        assert reg.get(p) is not None, f"{rel} PN 有 {p!r}，注册表未登记"
        assert name == reg.display_name(p), f"{rel} PN.{p}={name!r} != 注册表 {reg.display_name(p)!r}"
    for p, c in pc.items():
        assert reg.get(p) is not None, f"{rel} PC 有 {p!r}，注册表未登记"
        want = reg.color(p).lower()
        got = c.lower()
        if got != want:
            assert ACCEPTED_COLOR_DRIFT.get((rel, p)) == got, (
                f"{rel} PC.{p}={c} != 注册表 {reg.color(p)}（既未收口也未登记为已知漂移）")


def test_accepted_color_drift_is_not_stale():
    """登记的漂移必须还真实存在——收口了就把那一行删掉，别让允许清单变成僵尸。"""
    for (rel, p), val in ACCEPTED_COLOR_DRIFT.items():
        src = _read(rel)
        decl = r"const PC" if "unified_inbox" in rel else r"var PLAT_COLOR"
        table = _js_obj_pairs(src, decl)
        assert table.get(p, "").lower() == val, (
            f"ACCEPTED_COLOR_DRIFT[{rel}, {p}]={val} 已过期（现值 {table.get(p)!r}），请删除该行")


def test_workspace_base_plat_color_matches_registry():
    rel = "src/web/templates/workspace_base.html"
    table = _js_obj_pairs(_read(rel), r"var PLAT_COLOR")
    for p, c in table.items():
        spec = reg.get(p)
        if spec is None:
            continue  # x 等仅图标平台不在注册表范围
        if c.lower() != spec.color.lower():
            assert ACCEPTED_COLOR_DRIFT.get((rel, p)) == c.lower(), (
                f"{rel} PLAT_COLOR.{p}={c} != 注册表 {spec.color}")


def test_product_facts_lists_follow_registry():
    from src.assistant.product_facts import SUPPORTED_CHANNELS, UNSUPPORTED_CHANNELS
    claimed = _codebase_claims_supported()
    for spec in reg.all_platforms(implemented_only=True):
        if spec.compliance not in (reg.COMPLIANCE_MAIN, reg.COMPLIANCE_MIXED):
            continue
        if spec.id not in claimed:
            continue  # 他线在途：登了注册表、事实卡随后落地（见 _codebase_claims_supported）
        assert spec.facts_name() in SUPPORTED_CHANNELS, (
            f"注册表标已落地且合规可见的 {spec.facts_name()!r} 不在 product_facts.SUPPORTED_CHANNELS")
    for spec in reg.all_platforms():
        if spec.implemented or spec.id not in FACTS_PLANNED_ENFORCED:
            continue  # 他线的规划中平台（qq / wechat）事实卡措辞由其自行对齐
        label = spec.facts_name()
        if label in FACTS_UNSUPPORTED_PENDING:
            continue
        assert label in UNSUPPORTED_CHANNELS, (
            f"注册表标规划中的 {label!r} 不在 product_facts.UNSUPPORTED_CHANNELS——"
            "翻 implemented 前先同步事实卡")
    for label in FACTS_UNSUPPORTED_PENDING:
        assert label not in UNSUPPORTED_CHANNELS, f"{label} 已补进事实卡，请从 FACTS_UNSUPPORTED_PENDING 删除"


# ── 3. 产物同步 ─────────────────────────────────────────────────────────────────

def test_exported_json_is_in_sync():
    p = ROOT / "src" / "web" / "static" / "platform_registry.json"
    assert p.is_file(), "缺 static/platform_registry.json：python -m scripts.platform_registry_export --out"
    assert p.read_text(encoding="utf-8") == reg.export_json(), (
        "static/platform_registry.json 与注册表不一致：python -m scripts.platform_registry_export --out")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and {x["id"] for x in data["platforms"]} == set(
        s.id for s in reg.all_platforms())
