# -*- coding: utf-8 -*-
"""用量页 v2 静态钉（2026-08-16）：硬限徽章 / 推导 AI 自动行 / 预计耗尽 +
ops 总览「字符额度」卡 + 工作台额度预警条（uqw-bar）。

背景：数据契约新字段（enforce / license_month / daily / agents[].has_overrides）由
另一条线实现、随 ~03:30 重启才存在；三个消费面（workspace_usage / ops_overview /
workspace_base）经模板热更新**先于后端**上生产。本文件把两类不变量钉死：

1. **中间态 fail-soft**：新字段 undefined / 新接口 404 一律「该项不渲染 / 整卡隐藏 /
   收条停轮询」，绝不破坏现网页面——谁删掉 typeof/Array.isArray 容错先在这里红；
2. **接线不丢**：DOM id、数据源路径、轮询/停止逻辑、i18n 键 zh/en 齐平——字段名与
   键名是跨线契约，改名即红。
"""
from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def _usage() -> str:
    return (_TPL / "workspace_usage.html").read_text(encoding="utf-8")


def _base() -> str:
    return (_TPL / "workspace_base.html").read_text(encoding="utf-8")


def _ops() -> str:
    return (_TPL / "ops_overview.html").read_text(encoding="utf-8")


# ── workspace_usage.html：主管视图三处增强 ─────────────────────────────────────

def test_usage_enforce_badge_failsoft():
    """硬限徽章：data.enforce（重启后才有）undefined → 徽章保持隐藏。"""
    html = _usage()
    assert 'id="uq-enforce"' in html, "字符额度标题旁缺硬限徽章骨架"
    assert "uq_enforce_on" in html and "uq_enforce_soft" in html, \
        "硬限/软提醒文案键接线丢失（uq_enforce_on / uq_enforce_soft）"
    assert "typeof d.enforce!=='boolean'" in html, \
        "enforce 新字段的 undefined 容错被删——重启前中间态会渲染出 undefined 文本"


def test_usage_derived_auto_row():
    """「AI 自动+未归因（推导）」行：license_month（重启后才有）缺席/差值≤0 → 整行不渲染，
    且必须追加在分坐席表**尾部**（推导行混进坐席行会被误读成某个坐席）。"""
    html = _usage()
    assert "uq_derived_auto" in html, "推导行行名键接线丢失"
    assert "uq_derived_auto_tip" in html, "推导行 title 说明（推导值非直接计量）丢失"
    assert "d.license_month||{}" in html, "license_month 消费必须带 ||{} undefined 容错"
    assert "lm.available===true" in html, "available 显式判真被改——undefined 时不得渲染"
    assert "derived>0" in html, "差值>0 才显示的条件被删"
    assert ".join('')+derivedRow" in html, "推导行必须追加在坐席行之后（表尾）"


def test_usage_exhaust_projection_failsoft():
    """预计耗尽：license.remaining 非数字 / daily（重启后才有）缺席 / 日均=0 → 整项不渲染。"""
    html = _usage()
    assert 'id="uq-exhaust"' in html, "预计耗尽骨架丢失"
    assert "uq_exhaust_text" in html, "预计耗尽文案键接线丢失"
    assert "Array.isArray(daily)" in html, "daily 新字段的数组判定容错被删"
    assert "typeof lic.remaining!=='number'" in html, \
        "remaining 非数字（不限/未知）时的容错被删"
    assert "avg>0" in html, "近 7 天日均>0 的前置条件被删（除零外推）"


def test_usage_buy_more_link_beside_exhaust():
    """「增购」CTA 落点契约（2026-08-18 P1 升格）：从预计耗尽行内的 12px 文字链
    升为 .ug-buy 按钮样式、常驻「字符额度（额度健康区）」标题行右侧——随 #uq-char
    区显隐的语义不变，只是动线从段尾抬到标题行（商业化关键动线不该藏）。
    2026-08-16 v2 的原断言钉在 uq-exhaust-row 行内，那是当时「挪出授权总池块」的
    过渡位；预计耗尽文本行保留，只挪 CTA。"""
    html = _usage()
    assert 'id="uq-char"' in html
    head = html.split('id="uq-char"', 1)[1].split("</h3>", 1)[0]
    assert 'href="/membership"' in head, "增购 CTA 不在字符额度区标题行内"
    assert 'class="ug-buy"' in head, "增购 CTA 未按钮化（.ug-buy）"
    assert 'id="uq-exhaust-row"' in html, "预计耗尽行被误删（只该挪 CTA）"


def test_usage_new_apis_still_failsoft():
    """v2 之后 char-usage 消费仍必须整链 .catch 收尾（404=后端未重启是常态而非异常）。"""
    html = _usage()
    assert "/api/users/char-usage" in html
    assert ".catch(" in html


# ── workspace_base.html：额度预警条（uqw-bar，warn-only 绝不常驻）──────────────

def test_warnbar_skeleton_and_polling():
    html = _base()
    # 实施75 batch2：预警条退役顶部，迁右下胶囊+卡片（AITRNotify）
    assert 'id="uqw-bar"' not in html, "坐席额度预警横幅不得回归顶部（实施75）"
    assert "AITRNotify.ongoing.set('uqw'" in html, "右下胶囊接线丢失"
    assert "/api/workspace/my-usage" in html, "预警条数据源接线丢失"
    # 首查延迟 10s、之后每 600s（600000ms）
    assert "setTimeout(function(){ _tick(); timer = setInterval(_tick, 600000); }, 10000);" in html, \
        "轮询节奏（首查 10s 延迟 + 600s 周期）被改"


def test_warnbar_shows_only_on_warn_or_over():
    html = _base()
    assert "lvl === 'warn' || lvl === 'over'" in html, "warn/over 才显示的条件被删"
    assert "d.enabled !== false && quota > 0" in html, \
        "enabled/quota>0 前置被删——计量关闭或不限额时会误出条"


def test_warnbar_stops_polling_after_failures():
    """404（后端未重启）/未登录/异常 → 隐藏；连续 3 次失败停止轮询不刷屏。"""
    html = _base()
    assert "fails += 1;" in html, "失败计数丢失"
    assert "if(fails >= 3 && timer){ clearInterval(timer); timer = null; }" in html, \
        "连续 3 次失败停止轮询的逻辑被删——重启前 404 会每 10 分钟白打一发直到永远"


def test_warnbar_reuses_existing_banner_infra():
    """实施75 batch2：渲染面统一走 AITRNotify（右下胶囊语义色 + 每档每日一次卡片），
    over=红/warn=琥珀由 tone 承载；「点击查看」直达用量页的出口保留为卡片按钮。"""
    html = _base()
    assert "tone: lvl === 'over' ? 'error' : 'warn'" in html, "over/warn 语义分档被删"
    assert "dedupKey: 'uqw:' + lvl" in html, "每档每日一次的去重键被删"
    assert "wsNav('/workspace/usage')" in html, "点击直达用量页的入口被删"


# ── ops_overview.html：「字符额度」卡（详细三件套断言在 test_ops_overview.py）────

def test_ops_quota_card_static_wiring():
    html = _ops()
    assert 'id="uqCharSection"' in html
    assert "async function loadUsageQuota()" in html
    assert "/api/users/char-usage" in html
    assert "anchor:'uqCharKpis'" in html


# ── i18n：两 pack 新键 zh/en 齐平 ─────────────────────────────────────────────

_USAGE_PACK_NEW_KEYS = (
    "uq_enforce_on", "uq_enforce_soft",
    "uq_derived_auto", "uq_derived_auto_tip",
    "uq_exhaust_text",
    "uq_warnbar_text", "uq_warnbar_cta",
)

_OPS_PACK_NEW_KEYS = (
    "ov2_uq_title", "ov2_uq_sub", "ov2_uq_meter", "ov2_uq_on", "ov2_uq_off",
    "ov2_uq_enforce_on", "ov2_uq_enforce_soft", "ov2_uq_pool", "ov2_uq_unlimited",
    "ov2_uq_month", "ov2_uq_alerts", "ov2_uq_top",
)


def test_usage_pack_new_keys_bilingual():
    from src.web.i18n_packs.usage_page import EN, ZH

    for k in _USAGE_PACK_NEW_KEYS:
        assert ZH.get(k), f"usage_page ZH 缺 {k}"
        assert EN.get(k), f"usage_page EN 缺 {k}"


def test_ops_pack_new_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    for k in _OPS_PACK_NEW_KEYS:
        assert ZH.get(k), f"ops_overview_page ZH 缺 {k}"
        assert EN.get(k), f"ops_overview_page EN 缺 {k}"


def test_warnbar_placeholders_interpolated():
    """uq_warnbar_text 的 {pct}/{used}/{quota} 三占位必须被 JS 逐个替换（漏一个＝
    半成品文案直显给坐席）；uq_exhaust_text 的 {days}/{date} 同理。"""
    base = _base()
    for ph in ("'{pct}'", "'{used}'", "'{quota}'"):
        assert ".replace(" + ph in base, f"预警条占位 {ph} 未插值"
    usage = _usage()
    for ph in ("'{days}'", "'{date}'"):
        assert ".replace(" + ph in usage, f"预计耗尽占位 {ph} 未插值"
