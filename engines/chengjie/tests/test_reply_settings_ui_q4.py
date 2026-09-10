# -*- coding: utf-8 -*-
"""Q-4（#267 #265）commit 2：reply_settings 页三块文案/交互的静态契约。

- A（D-Q1）：班表时区必填——模板不再写「留空 = 服务器本地时间」，有换算核对行、
  三选一快捷（账号 / 客户 / 本机），保存前置校验 + 后端 tz_required 错误码有 i18n。
- B（D-Q2 CSTCJT）：额度卡去掉「人工」列（闸门只管 origin=auto）、有进度条 + 80% /
  100% 文案、「安装包默认」→「推荐」+ 依据句、「永不限制人工」一句话。
- G（HEEEN9）：档位卡标题「未设置账号的兜底档位」+「N 个账号已设全自动 / M 个跟随本页」
  + 三层一行图；「新会话自动沿用此档位」改大白话并折进高级；全自动卡带「高风险转人工审核」。

纯静态断言（读模板/词包源文本），不起服务、不写 config。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TPL = (_ROOT / "src" / "web" / "templates" / "reply_settings.html").read_text("utf-8")


@pytest.fixture(scope="module")
def packs():
    from src.web.i18n_packs import reply_settings_page as p
    return p.ZH, p.EN


_NEW_KEYS = [
    "rps_err_tz_required", "rps_ws_tz_pick", "rps_ws_tz_pick_acct", "rps_ws_tz_pick_ny",
    "rps_ws_tz_pick_ldn", "rps_ws_tz_pick_local", "rps_ws_js_tz_missing", "rps_ws_js_tz_bad",
    "rps_ws_js_tz_now", "rps_ws_js_tz_conv", "rps_ws_js_tz_city", "rps_ws_js_city_sh",
    "rps_ws_js_city_ny", "rps_ws_js_city_ldn",
    "rps_sg_manual_note", "rps_sg_js_exhausted", "rps_sg_js_warn80",
    "rps_mode_tiers_diagram", "rps_mode_tiers", "rps_mode_adv",
]


def test_new_keys_present_in_zh_and_en(packs):
    zh, en = packs
    missing = [k for k in _NEW_KEYS if k not in zh or k not in en]
    assert not missing, missing
    cjk = [k for k in _NEW_KEYS if re.search(r"[\u4e00-\u9fff]", en[k])]
    assert not cjk, f"EN 词包漏简体：{cjk}"


def test_placeholders_preserved(packs):
    zh, en = packs
    for k in ("rps_err_tz_required", "rps_ws_js_tz_conv", "rps_ws_js_tz_now",
              "rps_mode_tiers", "rps_sg_js_warn80"):
        assert set(re.findall(r"\{\w+\}", zh[k])) == set(re.findall(r"\{\w+\}", en[k])), k


# ── A：时区必填 ───────────────────────────────────────────────────

def test_ws_tz_no_longer_defaults_to_server_local(packs):
    zh, en = packs
    assert "留空 = 服务器本地时间" not in _TPL
    assert "留空 = 服务器本地时间" not in zh["rps_ws_tz_hint"]
    assert "server local time" not in en["rps_ws_tz_hint"].lower() or "no longer" in en["rps_ws_tz_hint"].lower()
    assert "必填" in zh["rps_ws_tz"]
    assert "America/New_York" in zh["rps_ws_tz_hint"]


def test_ws_tz_conversion_and_quick_pick_wired():
    # 换算核对行 + 三选一快捷 + 开关/时区/上下班变更都触发换算
    assert 'id="rps-ws-tzconv"' in _TPL
    assert "function rpsWsTzConv()" in _TPL
    assert "function rpsWsConvHHMM(" in _TPL
    for tz in ("Asia/Shanghai", "America/New_York", "Europe/London", "__local__"):
        assert f"rpsWsTzSet('{tz}')" in _TPL, tz
    assert re.search(r'id="rps-ws-enabled" onchange="[^"]*rpsWsTzConv\(\)', _TPL)
    assert re.search(r'id="rps-ws-tz"[^>]*oninput="[^"]*rpsWsTzConv\(\)', _TPL)
    assert re.search(r'id="rps-ws-start" onchange="[^"]*rpsWsTzConv\(\)', _TPL)


def test_ws_save_precheck_blocks_enable_without_tz():
    body = _TPL[_TPL.index("async function rpsSave()"):]
    body = body[:body.index("rpsSaving = true;")]
    assert "wsEn.checked" in body and "wsTzEl" in body
    assert "RPS_I18N.wsTzRequired" in body
    assert "'inbox.work_schedule.timezone'" in body


def test_tz_required_error_code_mapped():
    src = (_ROOT / "src" / "web" / "routes" / "reply_settings_routes.py").read_text("utf-8")
    assert '"tz_required": "rps_err_tz_required"' in src


# ── B：额度卡 ─────────────────────────────────────────────────────

def _sg_card():
    i = _TPL.index('id="rps-sec-sendgate"')
    return _TPL[i:_TPL.index("</table>", i)]


def test_sendgate_manual_column_removed():
    card = _sg_card()
    assert "rps_sg_col_manual" not in card
    assert card.count("<th>") == 3
    # 行渲染只吐 3 个 td（账号 / 已发·上限 / 进度）
    row_fn = _TPL[_TPL.index("function rpsSgRow("):]
    row_fn = row_fn[:row_fn.index("\n}\n")]
    assert row_fn.count("<td") == 3
    assert "manual_verdict" not in row_fn


def test_sendgate_progress_bar_80_and_100(packs):
    zh, en = packs
    assert "function rpsSgPct(" in _TPL
    assert "auto_pct" in _TPL and "warn80" in _TPL
    assert "pct >= 80" in _TPL and "pct >= 100" in _TPL
    assert ".rps-sg-bar" in _TPL and ".rps-guard-st.warn" in _TPL
    assert zh["rps_sg_js_exhausted"] == "AI 已达今日额度，请手动接管"
    assert "{p}" in zh["rps_sg_js_warn80"]


def test_sendgate_recommended_and_manual_never_limited(packs):
    zh, en = packs
    assert zh["rps_sg_q_pkg"] == "推荐" and "安装包默认" not in zh["rps_sg_q_pkg"]
    assert "installer default" not in en["rps_sg_q_pkg"]
    assert "300" in zh["rps_sg_cap_hint"] and "100 → 300 / 3 天" in zh["rps_sg_cap_hint"]
    assert "人工发送永不受限" in zh["rps_sg_cap_hint"]
    assert "不受此额度限制" in zh["rps_sg_manual_note"]
    assert "AI 已发" in zh["rps_sg_today"]


# ── G：档位卡三层文案 ─────────────────────────────────────────────

def test_mode_card_three_tier_copy(packs):
    zh, en = packs
    assert zh["rps_mode_title"] == "未设置账号的兜底档位"
    assert zh["rps_mode_tiers"] == "{n} 个账号已设全自动 / {m} 个跟随本页"
    assert "▶" in zh["rps_mode_tiers_diagram"]
    assert "高风险" in zh["rps_mode_auto_d"] and "人工审核" in zh["rps_mode_auto_d"]
    assert "human review" in en["rps_mode_auto_d"]
    assert 'id="rps-mode-tiers"' in _TPL and 'id="rps-mode-tiers-line"' in _TPL
    assert "function rpsModeTiers(" in _TPL
    assert "a.mode === 'auto_ai'" in _TPL


def test_bootstrap_switch_in_advanced_with_plain_words(packs):
    zh, en = packs
    assert "新会话自动沿用此档位" not in zh["rps_bootstrap_label"]
    assert "持久化" not in zh["rps_bootstrap_hint"]
    assert zh["rps_bootstrap_hint"].startswith("出厂关")
    assert en["rps_bootstrap_hint"].startswith("Off by default")
    # 开关在 <details class="rps-guard-adv"> 折叠里
    i = _TPL.index('id="rps-bootstrap"')
    head = _TPL[_TPL.index('id="rps-sec-mode"'):i]
    assert head.rfind("<details") > head.rfind("</details>")
    assert "rps_mode_adv" in head


def test_bootstrap_factory_default_off():
    """G「默认关」：桌面种子（min/internal）bootstrap_automation_mode 都是 false。"""
    import yaml
    for name in ("config.desktop.min.yaml", "config.desktop.internal.yaml"):
        cfg = yaml.safe_load((_ROOT / "config" / name).read_text("utf-8")) or {}
        ad = ((cfg.get("inbox") or {}).get("auto_draft") or {})
        assert ad.get("bootstrap_automation_mode") is False, name
