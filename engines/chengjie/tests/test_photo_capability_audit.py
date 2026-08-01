# -*- coding: utf-8 -*-
"""人设发图能力盘点 + 试聊占位 + 消毒观测（P1，2026-07-31）。"""
from __future__ import annotations

from pathlib import Path

from src.companion.photo_capability_audit import (
    recommend_enable,
    stock_map_from_rows,
    summarize_audit,
)

_ENGINE = Path(__file__).resolve().parents[1]


def test_recommend_enable_default_off_with_stock():
    personas = [
        {"id": "a", "name": "有货未开", "capabilities": {}},
        {"id": "b", "name": "已开", "capabilities": {"photos": True}},
        {"id": "c", "name": "无货", "capabilities": {}},
    ]
    stock = {
        "a": {"total": 3, "enabled": 2, "hits": 5, "sends": 1},
        "b": {"total": 1, "enabled": 1, "hits": 0, "sends": 0},
        "c": {"total": 0, "enabled": 0, "hits": 0, "sends": 0},
    }
    rec = recommend_enable(personas, stock)
    assert [r["id"] for r in rec] == ["a"]
    assert rec[0]["action"] == "enable"
    assert "stock_enabled=2" in rec[0]["reason"]


def test_recommend_enable_hits_only_signal():
    # 无启用库存但有历史 hits → 仍建议开（曾真发过）
    personas = [{"id": "x", "name": "旧投放", "capabilities": {"photos": False}}]
    stock = {"x": {"total": 0, "enabled": 0, "hits": 12, "sends": 0}}
    rec = recommend_enable(personas, stock)
    assert len(rec) == 1 and rec[0]["id"] == "x"


def test_summarize_counts_orphan_on():
    personas = [
        {"id": "on_empty", "capabilities": {"photos": True}},
        {"id": "off", "capabilities": {}},
    ]
    stock = {"on_empty": {"total": 0, "enabled": 0, "hits": 0, "sends": 0}}
    s = summarize_audit(personas, stock)
    assert s["photos_on"] == 1
    assert s["photos_off"] == 1
    assert s["orphan_on"] == 1
    assert s["recommend_count"] == 0


def test_stock_map_from_rows():
    m = stock_map_from_rows([
        {"persona_id": "p1", "total": 2, "enabled": 1, "hits": 3},
        {"id": "p2", "total": 1, "enabled": 1, "total_hits": 9, "sends": 2},
    ])
    assert m["p1"]["enabled"] == 1 and m["p1"]["hits"] == 3
    assert m["p2"]["hits"] == 9 and m["p2"]["sends"] == 2


def test_chat_test_returns_photo_preview_wiring():
    src = (_ENGINE / "src" / "web" / "routes" / "chat_test_routes.py").read_text(
        encoding="utf-8")
    assert "photo_preview" in src
    assert "parse_photo_directive" in src
    assert 'resp["photo_preview"]' in src


def test_frontend_renders_photo_preview():
    src = (_ENGINE / "src" / "web" / "static" / "js" / "persona_chat_test.js"
           ).read_text(encoding="utf-8")
    assert "photo_preview" in src
    assert "psc-photo-prev" in src
    assert "psc_pp_title" in src


def test_sanitize_stats_and_source_buckets():
    from src.companion import photo_capability as pc
    # 重置观测（测试隔离）
    with pc._STATS_LOCK:
        for k in list(pc._STATS):
            pc._STATS[k] = 0
    out = pc.sanitize_no_photo_reply(
        "有啊，我翻翻手机相册哈。", media_context=True, source="chat_test")
    assert "翻翻" not in out
    d = pc.dump_sanitize_stats()
    assert d["sanitize_calls"] >= 1
    assert d["sanitize_stripped"] >= 1
    assert d["chat_test_stripped"] >= 1
    assert d["active"] is True


def test_metrics_route_wires_photo_capability_dump():
    src = (_ENGINE / "src" / "web" / "routes" / "drafts_routes.py").read_text(
        encoding="utf-8")
    assert "dump_sanitize_stats" in src
    assert 'metrics["photo_capability"]' in src


def test_audit_cli_module_importable():
    from scripts.photo_capability_audit import audit_root, main
    assert callable(audit_root) and callable(main)


def test_audit_cli_apply_never_uses_persona_manager_persist():
    """--apply 必须走实例 API，绝不可在 CLI 进程里 persist_profiles——
    新进程 PersonaManager 为空，persist 会把实例 profiles_runtime.yaml
    覆盖成只剩一条人设（P1 初版隐患，P2 修正后钉死不回潮）。"""
    src = (_ENGINE / "scripts" / "photo_capability_audit.py").read_text(
        encoding="utf-8")
    assert "persist_profiles" not in src
    # 只抓真实 import/调用（docstring 里解释「为什么不用」是允许的）
    assert "from src.utils.persona_manager import" not in src
    assert "PersonaManager.get_instance" not in src
    assert "_apply_enable_via_api" in src
    assert "verify_failed" in src   # 写后回读校验必须存在


def test_instance_base_url_derivation(tmp_path):
    from scripts.photo_capability_audit import instance_base_url
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "web_admin:\n  port: 18799\n", encoding="utf-8")
    assert instance_base_url(tmp_path) == "http://127.0.0.1:18799"
    # overlay 覆写 base
    (cfg_dir / "config.local.yaml").write_text(
        "web_admin:\n  port: 28888\n", encoding="utf-8")
    assert instance_base_url(tmp_path) == "http://127.0.0.1:28888"
    # 缺配置 → 空串（调用方如实报错，不猜端口）
    assert instance_base_url(tmp_path / "nope") == ""


def test_ops_card_wired():
    """ops-overview 卡：模板段 + 渲染器 + 共享 metrics 链挂载 + i18n 键。"""
    html = (_ENGINE / "src" / "web" / "templates" / "ops_overview.html"
            ).read_text(encoding="utf-8")
    assert 'id="photoCapSection"' in html
    assert "renderPhotoCapability" in html
    # 零流量隐藏（bazi 惯例）
    assert "if(!pc || !pc.active){ sec.style.display='none'; return; }" in html
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        tr_map = get_translations(lang)
        for key in ("ov2_s_photocap", "ov2_pc_calls", "ov2_pc_stripped",
                    "ov2_pc_rate", "ov2_pc_src_chat", "ov2_pc_src_draft",
                    "ov2_pc_hint"):
            assert key in tr_map, f"{lang} 缺 i18n 键 {key}"
