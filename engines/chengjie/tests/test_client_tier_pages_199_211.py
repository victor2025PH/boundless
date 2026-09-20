# -*- coding: utf-8 -*-
"""歌房 / 声音评测用户版空态（L-4 G，#199 #211，D-L6，2026-09-06）门禁。

CMGJ9G / 4Q3M5G 实录：歌房曲库 0 / 声库 0 却开关开着（客户求歌建单进失败）；声音评测只有
消费端。钉住：
1. 两页 client 形态（未开开发者模式）：顶部「功能升级中，如需开通请联系客服」横幅 +
   原操作面整体 l4-locked，脚本不再拉数据；partner / internal 原页不变；
2. /api/singing/config enabled=true 在资产不齐（无启用曲目或无可用声库）时 409 并写明原因；
   资产齐了放行；关闭不拦；overview 回 assets 盘点；
3. 侧栏两页已进 CLIENT_HIDDEN_ITEM_IDS（A 项，这里只复核）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

REPO = Path(__file__).resolve().parents[1]
TPL_DIR = REPO / "src" / "web" / "templates"


def _tpl(name):
    return (TPL_DIR / name).read_text(encoding="utf-8")


# ── 1. 模板接线 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,root_id", [("singing.html", "sg-root"), ("voice_eval.html", "vev-root")])
def test_pages_include_upgrading_banner_and_lock_surface(name, root_id):
    tpl = _tpl(name)
    assert '{% include "_client_tier_upgrading.html" %}' in tpl
    assert "{% if ui_client_hide %} l4-locked{% endif %}" in tpl or \
        "{% if ui_client_hide %}l4-locked{% endif %}" in tpl
    assert f'id="{root_id}"' in tpl


def test_partial_renders_only_for_client_hide():
    env = Environment(loader=FileSystemLoader(str(TPL_DIR)))
    t = env.get_template("_client_tier_upgrading.html")
    out = t.render(ui_client_hide=True, i18n={})
    assert 'data-l4-upgrading="1"' in out and "功能升级中" in out and "联系客服" in out
    assert ".l4-locked{display:none!important}" in out
    assert t.render(ui_client_hide=False, i18n={}).strip() == ""
    assert t.render(i18n={}).strip() == "", "旧后端无 ui_client_hide → 不渲染横幅"


def test_singing_script_skips_api_when_locked():
    tpl = _tpl("singing.html")
    assert "classList.contains('l4-locked')" in tpl
    assert "if(LOCKED) return;" in tpl
    assert 'id="sg-assets-warn"' in tpl and "renderAssetsGuard" in tpl


def test_voice_eval_script_skips_api_when_locked():
    tpl = _tpl("voice_eval.html")
    assert "root.classList.contains('l4-locked')) return;" in tpl


def test_sidebar_hides_both_pages_for_client():
    from src.web.nav_schema import CLIENT_HIDDEN_ITEM_IDS
    assert "singing" in CLIENT_HIDDEN_ITEM_IDS and "voice_eval" in CLIENT_HIDDEN_ITEM_IDS


def test_i18n_bilingual():
    from src.web.i18n_packs.client_tier import EN, ZH
    for k in ("l4_upgrading_title", "l4_upgrading_body", "sg_assets_missing",
              "sg_assets_on_missing", "err.singing.assets_missing"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"


# ── 2. 唱歌能力位资产门禁 ───────────────────────────────────────────────────

class _CfgMgr:
    def __init__(self, cfg):
        self.config = cfg
        self.writes = []

    def set_overlay_flag(self, key, value):
        self.writes.append((key, value))
        return True, ""


def _app(tmp_path, enabled=False):
    from src.web.routes.singing_routes import register_singing_routes
    tdir = tmp_path / "tpl"
    sroot = tmp_path / "stock"
    tdir.mkdir()
    sroot.mkdir()
    cfg = {"companion": {"singing": {"enabled": enabled, "templates_dir": str(tdir),
                                     "stock_dir": str(sroot)}}}
    mgr = _CfgMgr(cfg)
    app = FastAPI()

    async def api_auth(request: Request):
        return True

    register_singing_routes(app, api_auth, config_manager=mgr)
    return TestClient(app), mgr, tdir


def _stock_assets(tdir):
    (tdir / "anchor.wav").write_bytes(b"RIFF" + b"\0" * 64)
    (tdir / "voices.json").write_text(json.dumps({"voices": [
        {"key": "warm_f", "anchor": "anchor.wav", "prompt": "warm", "personas": ["p1"]}]}),
        encoding="utf-8")
    (tdir / "manifest.json").write_text(json.dumps({"templates": [
        {"id": "origin_wind", "title": "风", "file": "wind.wav", "lyrics": "la", "enabled": True}]}),
        encoding="utf-8")


def test_enable_refused_when_assets_missing(tmp_path):
    c, mgr, _ = _app(tmp_path)
    ov = c.get("/api/singing/overview").json()
    assert ov["assets"] == {"songs_enabled": 0, "voices": 0, "voices_ok": 0,
                            "stock_files": 0, "ready": False}
    r = c.post("/api/singing/config", json={"key": "enabled", "value": True})
    assert r.status_code == 409
    assert "曲库 0" in r.json()["detail"] and "声库 0" in r.json()["detail"]
    assert mgr.writes == [], "资产不齐不得落 enabled=true"
    # 关闭与护栏参数不受门禁影响
    assert c.post("/api/singing/config", json={"key": "enabled", "value": False}).status_code == 200
    assert c.post("/api/singing/config", json={"key": "daily_cap", "value": 1}).status_code == 200


def test_enable_allowed_when_assets_ready(tmp_path):
    c, mgr, tdir = _app(tmp_path)
    _stock_assets(tdir)
    ov = c.get("/api/singing/overview").json()
    assert ov["assets"]["ready"] is True
    assert ov["assets"]["songs_enabled"] == 1 and ov["assets"]["voices_ok"] == 1
    r = c.post("/api/singing/config", json={"key": "enabled", "value": True})
    assert r.status_code == 200
    assert ("companion.singing.enabled", True) in mgr.writes


def test_enable_refused_when_voice_anchor_missing(tmp_path):
    c, mgr, tdir = _app(tmp_path)
    _stock_assets(tdir)
    (tdir / "anchor.wav").unlink()   # 声库条目在、锚定音不在＝声库 0 可用
    ov = c.get("/api/singing/overview").json()
    assert ov["assets"]["voices"] == 1 and ov["assets"]["voices_ok"] == 0
    assert c.post("/api/singing/config", json={"key": "enabled", "value": True}).status_code == 409
