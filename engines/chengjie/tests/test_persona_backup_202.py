# -*- coding: utf-8 -*-
"""L-2 E（#202，P2）：「人设备份与迁移」——备份全部 / 从备份恢复（预览 + 确认）/ 导出单个（2026-09-06）。

事故（29BAG6 / E42974，skuio）：人设工作室底部「导出 / 导入 Profiles（主账号专属）」只有一个
「同步到 personas.yaml」按钮；后端启动 ``人设加载完成: config=0 canonical=0 runtime=14``，yaml 为 0，
该按钮是研发迁移残留、不按不影响运行；用户真正要的是存档备份与迁移。

钉住：
- GET /api/personas/profiles/export：信封 {format, version, exported_at, count, profiles, album_refs}
  （profiles/count 旧键不变）；?pid= 单个导出（404 不存在）；
- POST /api/personas/profiles/import?dry_run=1：只算不写，「新增 N · 覆盖 N」数字与 ids 正确；
- 备份 → 删一个 → 恢复 → 内容一致；覆盖走 upsert（历史栈仍可 /revert）；
- 模板：区块改名「人设备份与迁移」+ 说明；master 三入口；子账号灰字提示；yaml 同步收进开发者区并随
  ui_client_hide 隐藏；「未同步草稿」概览格 client 形态不渲染（JS 已判空）。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from src.utils.persona_manager import PersonaManager

_HDRS = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_JS = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "js" / "persona_studio_core.js"


@pytest.fixture
def app_on(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"}, "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": []},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump({"channels": {}}), encoding="utf-8")
    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm.upsert_profile("steven", {"id": "steven", "name": "steven", "role": "陪伴", "gender": "male",
                                 "background": "海外做生意", "tags": ["vip"]}, _track_history=False)
    pm.upsert_profile("mizuki", {"id": "mizuki", "name": "Mizuki", "role": "日语陪伴",
                                 "voice_profile": {"voice_mode": "preset", "backend": "edge_tts",
                                                   "voice": "ja-JP-NanamiNeural"}}, _track_history=False)
    from src.web.admin import create_app
    yield create_app(cm)
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_backup_delete_restore_roundtrip(app_on):
    pm = PersonaManager.get_instance()
    async with _client(app_on) as c:
        r = await c.get("/api/personas/profiles/export", headers=_HDRS)
        assert r.status_code == 200
        bk = r.json()
        assert bk["format"] == "chatx-personas-backup" and bk["version"] == 1 and bk["exported_at"]
        assert bk["count"] == 2 and {p["id"] for p in bk["profiles"]} == {"steven", "mizuki"}
        assert isinstance(bk["album_refs"], dict)
        steven_before = json.dumps(pm.get_persona_by_id("steven"), sort_keys=True, ensure_ascii=False)

        # 删一个 → 预览：新增 1（steven）· 覆盖 1（mizuki），且只算不写
        assert pm.delete_profile("steven") is True
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS, json=bk)
        assert r.status_code == 200
        pv = r.json()
        assert pv["dry_run"] is True and (pv["add"], pv["overwrite"]) == (1, 1)
        assert pv["add_ids"] == ["steven"] and pv["overwrite_ids"] == ["mizuki"]
        assert pv["names"]["steven"] == "steven" and pv["invalid"] == []
        assert pm.get_persona_by_id("steven") is None, "dry_run 不得写库"

        # 确认恢复 → 一致
        r = await c.post("/api/personas/profiles/import", headers=_HDRS, json={"profiles": bk["profiles"], "mode": "merge"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["imported"] == 2 and (d["add"], d["overwrite"]) == (1, 1)
        after = json.dumps(pm.get_persona_by_id("steven"), sort_keys=True, ensure_ascii=False)
        assert after == steven_before
        assert pm.get_persona_by_id("mizuki")["voice_profile"]["voice"] == "ja-JP-NanamiNeural"


@pytest.mark.asyncio
async def test_dry_run_reports_invalid_and_warnings(app_on):
    body = {"profiles": [
        {"name": "无 id"},
        "junk",
        {"id": "newbie", "name": "新", "totally_unknown": 1},
        {"id": "badvoice", "name": "坏声", "voice_profile": {"backend": "avatar_clone", "voice": "ja-JP-NanamiNeural"}},
    ], "dry_run": True}
    async with _client(app_on) as c:
        r = await c.post("/api/personas/profiles/import", headers=_HDRS, json=body)
        assert r.status_code == 200
        d = r.json()
        assert d["dry_run"] is True and d["add"] == 2 and d["overwrite"] == 0
        assert [x["reason"] for x in d["invalid"]] == ["missing_id", "not_object"]
        codes = {w["id"]: w for w in d["warnings"]}
        assert codes["newbie"]["unknown_keys"] == ["totally_unknown"]
        assert "clone_missing_reference" in codes["badvoice"]["voice_problems"]
        assert PersonaManager.get_instance().get_persona_by_id("newbie") is None


@pytest.mark.asyncio
async def test_export_single_persona(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/personas/profiles/export", params={"pid": "steven"}, headers=_HDRS)
        assert r.status_code == 200
        d = r.json()
        assert d["count"] == 1 and d["profiles"][0]["id"] == "steven" and d["profiles"][0]["gender"] == "male"
        r = await c.get("/api/personas/profiles/export", params={"pid": "ghost"}, headers=_HDRS)
        assert r.status_code == 404


def _render(**ctx) -> str:
    env = Environment(loader=FileSystemLoader(str(_TPL_DIR)), undefined=ChainableUndefined, autoescape=False)
    env.globals["url_for"] = lambda *a, **k: "#"
    env.filters["tojson"] = lambda v, **k: json.dumps(None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    env.policies["json.dumps_function"] = lambda v, **k: json.dumps(
        None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    base = {"i18n": {}, "request": None, "user_role": "master", "ui_lang": "zh"}
    base.update(ctx)
    return env.get_template("personas.html").render(**base)


def test_template_master_internal():
    html = _render(user_role="master", ui_client_hide=False)
    assert "人设备份与迁移" in html and "导出 / 导入 Profiles" not in html
    assert 'onclick="pbBackupAll()"' in html and 'id="pb-restore-file"' in html
    assert 'id="pb-dev-yaml"' in html and 'onclick="syncToConfig()"' in html
    assert 'id="hs-unsynced"' in html
    assert "window.__PSN_DEV_FACE = true;" in html


def test_template_master_client_hides_yaml_and_unsynced():
    html = _render(user_role="master", ui_client_hide=True)
    assert 'onclick="pbBackupAll()"' in html
    assert 'id="pb-dev-yaml"' not in html and 'id="sync-config-btn"' not in html
    assert 'id="hs-unsynced"' not in html
    assert "window.__PSN_DEV_FACE = false;" in html


def test_template_sub_account_grey_hint_only():
    html = _render(user_role="agent", ui_client_hide=False)
    assert "人设备份与迁移" in html
    assert 'onclick="pbBackupAll()"' not in html and 'id="pb-dev-yaml"' not in html
    assert "备份与恢复由主账号操作" in html


def test_card_menu_has_export_and_dashboard_null_safe():
    js = _JS.read_text(encoding="utf-8")
    assert "pbExportOne(" in js and "psn_export_one" in js
    seg = js[js.index("function _renderDashboard(d)"):js.index("_checkOnboarding(pf.count || 0)")]
    assert "getElementById('hs-unsynced').textContent" not in seg, "概览格可能不渲染，必须判空"
    assert "__PSN_DEV_FACE" in js
