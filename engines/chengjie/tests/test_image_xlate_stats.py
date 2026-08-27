# -*- coding: utf-8 -*-
"""P1-OBS（2026-08-19）图片翻译链路观测：stats 语义 + 卡片三件套 + 接线钉。

与 value/atruth 卡同款不变量：section / 渲染函数 / 注册表三者少任何一件都是
静默缺陷；metrics 合装与路由三个记录点（unified / patch / 旧纯文本）用源码
扫描钉住——这张卡是 imgsvc176（ppocr 微服务）灰度切换的决策读数面，断了线
「切没切对」只能翻日志。
"""
from __future__ import annotations

from pathlib import Path

from src.ai.image_xlate_stats import (
    ImageXlateStats,
    get_image_xlate_stats,
    provider_bucket,
)

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── 纯语义 ─────────────────────────────────────────────────────────────


def test_provider_bucket_enum():
    assert provider_bucket("ppocr") == "ppocr"
    assert provider_bucket("ppocr_cache") == "ppocr_cache"
    assert provider_bucket("ppocr_empty") == "ppocr"        # 前缀归桶
    assert provider_bucket("cache") == "vlm_cache"
    assert provider_bucket("ollama_ok", cached=True) == "vlm_cache"
    assert provider_bucket("ollama_ok") == "vlm"
    assert provider_bucket("") == "vlm"


def test_record_and_dump_coverage():
    s = ImageXlateStats()
    assert s.dump()["active"] is False
    s.record_run(provider_tag="ppocr", lang="ja",
                 stats={"blocks": 5, "patched": 3, "identity": 2, "failed": 0},
                 ms=1200)
    s.record_patch(provider_tag="ppocr_cache", cached=True, lang="ja",
                   stats={"blocks": 5, "patched": 3, "identity": 2, "failed": 0},
                   ms=800)
    s.record_run(provider_tag="cache", cached=True, lang="ko", stats=None, ms=300)
    d = s.dump()
    assert d["active"] is True and d["runs"] == 2 and d["patches"] == 1
    assert d["providers"] == {"ppocr": 1, "ppocr_cache": 1, "vlm_cache": 1}
    assert d["langs"] == {"ja": 2, "ko": 1}
    # identity 同语保留算已译（与面板覆盖率行同口径）
    assert d["coverage"] == {"blocks": 10, "ok": 10, "failed": 0, "ratio": 1.0}
    assert d["avg_ms"] == int((1200 + 800 + 300) / 3)


def test_lang_cap_bounded():
    s = ImageXlateStats()
    for i in range(40):
        s.record_run(provider_tag="vlm", lang=f"l{i}", stats=None, ms=1)
    assert len(s.dump()["langs"]) <= 24


def test_singleton_and_reset():
    g = get_image_xlate_stats()
    g.reset()
    g.record_run(provider_tag="ppocr", lang="zh", stats=None, ms=1)
    assert get_image_xlate_stats().dump()["runs"] >= 1
    g.reset()
    assert get_image_xlate_stats().dump()["active"] is False


# ── 接线钉（源码扫描）─────────────────────────────────────────────────


def test_route_records_all_three_paths():
    src = _read("src/web/routes/unified_inbox_translate_routes.py")
    seg = src[src.index("translate-message-media"):]
    assert seg.count("get_image_xlate_stats") >= 3, \
        "unified / patch / 旧纯文本三条成功路径都必须记录（少一条=分布失真）"
    assert "record_patch(" in seg and "record_run(" in seg


def test_metrics_merge_wired():
    src = _read("src/web/routes/drafts_routes.py")
    assert 'metrics["image_xlate"]' in src, "metrics 合装点断线=卡片永远 no_data"


# ── 卡片三件套（section / 渲染函数 / 注册表）+ 隐藏惯例 + 分发链两分支 ──


def test_image_xlate_card_renders_and_registered():
    src = _read("src/web/templates/ops_overview.html")
    assert 'id="imgXlateSection"' in src
    assert "function renderImageXlate(" in src
    # 共享 metrics 分发链两分支（成功 + 403 早退）都必须喂这张卡
    assert src.count("renderImageXlate(d)") >= 1
    assert src.count("renderImageXlate(null)") >= 1
    # 注册表 + 隐藏原因回落
    assert "{key:'imgxl'" in src.replace(" ", "").replace('"', "'") or \
        "key:'imgxl'" in src
    assert "opsHideCardEl(sec, 'imgxl', 'no_data')" in src
    assert "imgxl:    {reason:'no_data'}" in src or "imgxl: {reason:'no_data'}" in src


def test_image_xlate_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_imgxl", "ov2_ix_hint", "ov2_ix_runs", "ov2_ix_patches",
            "ov2_ix_cover", "ov2_ix_cover_v", "ov2_ix_avgms",
            "ov2_ix_prov", "ov2_ix_langs")
    for k in keys:
        assert ZH.get(k), f"ZH 缺 {k}"
        assert EN.get(k), f"EN 缺 {k}"
