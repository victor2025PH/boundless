# -*- coding: utf-8 -*-
"""P-1 D（#259 #254）：ops 页「AI 指纹」卡接线。

钉住：
  - /api/workspace/metrics 汇总里挂 ai_fingerprint（snapshot 24h）；
  - ops_overview.html：section / renderAiFingerprint / loadFrontendErrors 两处分发 / OPS_CARDS 注册 / 隐藏回落；
  - i18n 小包 zh / en / zh_hant 三语键齐、模板与 JS 用到的键全部在包里；
  - JS 段零中文（window.T 无中文兜底）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_metrics_endpoint_exposes_ai_fingerprint():
    src = (ROOT / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert 'metrics["ai_fingerprint"] = _aifp_snapshot(hours=24)' in src
    assert src.index('metrics["autosend_shadow"]') < src.index('metrics["ai_fingerprint"]')


def test_ops_overview_card_wired_and_keys_resolve():
    html = (ROOT / "src" / "web" / "templates" / "ops_overview.html").read_text(encoding="utf-8")
    assert '<section id="aiFpSection"' in html and 'id="aiFpKpis"' in html and 'id="aiFpDetail"' in html
    assert html.count("try{ renderAiFingerprint(d); }") == 1 and html.count("renderAiFingerprint(null)") == 1
    assert "function renderAiFingerprint(d)" in html
    assert re.search(r"\{key:'aifp',\s*group:'quality',\s*anchor:'aiFpKpis',\s*loaders:\[loadFrontendErrors\]\}", html)
    assert re.search(r"aifp:\s*\{reason:'no_data'\}", html)
    # 灯色三档
    fn = html[html.index("function renderAiFingerprint(d)"):html.index("function renderOutboundMirror(d)")]
    assert "opsSetCardLight('aifp', lv === 'red' ? 'red' : (lv === 'yellow' ? 'yellow' : ''))" in fn
    # JS 段零中文（注释除外）
    code_only = "\n".join(ln for ln in fn.splitlines() if not ln.strip().startswith("//"))
    assert not re.search(r"[\u4e00-\u9fff]", code_only), "renderAiFingerprint JS 段不得含中文（用 window.T）"

    from src.web.i18n_packs import ai_fingerprint_card as pk
    assert set(pk.ZH) == set(pk.EN) == set(pk.ZH_HANT)
    used = set(re.findall(r"window\.T\('(ov2_js_aifp_[a-z_]+)'\)", fn))
    assert used and used <= set(pk.ZH), used - set(pk.ZH)
    for k in ("ov2_s_aifp", "ov2_aifp_hint"):
        assert f"get('{k}'" in html and k in pk.ZH
    # 繁体确实是繁体（不是简体复制）
    assert pk.ZH_HANT["ov2_js_aifp_dash"] == "破折號率" and pk.ZH["ov2_js_aifp_dash"] == "破折号率"


def test_snapshot_shape_matches_card_contract():
    from src.inbox import ai_fingerprint_stats as fp
    fp._reset_for_tests(persist=False)
    fp.record_draft(1)
    snap = fp.snapshot(hours=24)
    for k in ("dash", "service_tone", "claim_unanchored", "promise_no_action", "gate_leak"):
        assert {"hits", "total", "rate_pct", "level"} <= set(snap[k])
    assert "ready" in snap["promise_no_action"] and "counts" in snap and "level" in snap and snap["window_hours"] == 24
    fp._reset_for_tests(persist=False)
