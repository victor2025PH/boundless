# -*- coding: utf-8 -*-
"""人设导入收 YAML：磁盘格式一直是 yaml，工作室导入原先只走浏览器 JSON.parse。"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from src.utils.persona_manager import (
    PersonaManager,
    parse_persona_import_text,
)

_HDRS = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_PACK = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "persona_packs"
    / "claire_brennan.yaml"
)


def test_parse_json_array_and_backup_envelope():
    profiles, fmt, extras = parse_persona_import_text(
        '[{"id":"a","name":"A"},{"id":"b","name":"B"}]'
    )
    assert fmt == "json" and [p["id"] for p in profiles] == ["a", "b"]
    profiles, fmt, extras = parse_persona_import_text(
        '{"format":"chatx-personas-backup","profiles":[{"id":"x","name":"X"}],'
        '"album_refs":{"x":[{"file":"1.jpg"}]}}'
    )
    assert fmt == "json" and profiles[0]["id"] == "x"
    assert extras["album_refs"]["x"][0]["file"] == "1.jpg"


def test_parse_yaml_runtime_mapping_injects_id():
    raw = """
profiles:
  claire_brennan:
    name: Claire
    role: designer
    location: los_angeles
"""
    profiles, fmt, extras = parse_persona_import_text(raw)
    assert fmt == "yaml" and extras == {}
    assert profiles[0]["id"] == "claire_brennan"
    assert profiles[0]["name"] == "Claire"


def test_parse_yaml_single_persona_and_rejects_junk():
    profiles, fmt, _ = parse_persona_import_text("name: Mei\nrole: companion\n")
    assert fmt == "yaml" and profiles[0]["name"] == "Mei"
    with pytest.raises(ValueError, match="empty"):
        parse_persona_import_text("  ")
    with pytest.raises(ValueError, match="no_profiles"):
        parse_persona_import_text("bindings: {}\n")
    with pytest.raises(ValueError, match="invalid_text"):
        parse_persona_import_text(":\n  - [")


def test_parse_claire_brennan_pack():
    assert _PACK.is_file(), "persona pack should exist for this test"
    profiles, fmt, _ = parse_persona_import_text(_PACK.read_text(encoding="utf-8"))
    assert fmt == "yaml"
    assert len(profiles) == 1
    p = profiles[0]
    assert p["id"] == "claire_brennan" and p["name"] == "Claire"
    assert p.get("location") == "los_angeles"
    assert p.get("tags") == [
        "female", "30", "american", "los_angeles", "designer",
        "apparel", "adult", "english",
    ]
    assert all(isinstance(t, str) for t in p["tags"])
    from src.ai.voice_tristate import split_problems, validate_voice_profile
    errs, _ = split_problems(validate_voice_profile(p.get("voice_profile")))
    assert errs == [], errs


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
    from src.web.admin import create_app
    yield create_app(cm)
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_parse_route_yaml_and_import_mapping(app_on):
    raw = _PACK.read_text(encoding="utf-8")
    async with _client(app_on) as c:
        r = await c.post("/api/personas/profiles/parse", headers=_HDRS, json={"text": raw})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["format"] == "yaml" and d["count"] == 1
        assert d["profiles"][0]["id"] == "claire_brennan"

        r = await c.post(
            "/api/personas/profiles/import?dry_run=1",
            headers=_HDRS,
            json={"text": raw, "mode": "merge"},
        )
        assert r.status_code == 200
        pv = r.json()
        assert pv["dry_run"] is True and pv["add"] == 1 and pv["add_ids"] == ["claire_brennan"]
        assert PersonaManager.get_instance().get_persona_by_id("claire_brennan") is None

        r = await c.post(
            "/api/personas/profiles/import",
            headers=_HDRS,
            json={"text": raw, "mode": "merge"},
        )
        assert r.status_code == 200
        assert r.json()["imported"] == 1
        stored = PersonaManager.get_instance().get_persona_by_id("claire_brennan")
        assert stored and stored.get("name") == "Claire"


@pytest.mark.asyncio
async def test_import_illegal_clone_yaml_becomes_preset(app_on):
    raw = (
        "profiles:\n  bad_pack:\n    name: Pack\n"
        "    voice_profile:\n      backend: avatar_clone\n"
        "      voice: en-US-AvaMultilingualNeural\n"
    )
    async with _client(app_on) as c:
        r = await c.post("/api/personas/profiles/import", headers=_HDRS,
                         json={"text": raw, "mode": "merge"})
        assert r.status_code == 200
        w = {x["id"]: x for x in r.json()["warnings"]}
        assert w["bad_pack"]["voice_rewrite"] == "preset"
        stored = PersonaManager.get_instance().get_persona_by_id("bad_pack")
        assert stored["voice_profile"]["voice_mode"] == "preset"
        assert stored["voice_profile"]["voice"] == "en-US-AvaMultilingualNeural"


def test_studio_file_pickers_accept_yaml():
    env = Environment(
        loader=FileSystemLoader(str(_TPL_DIR)),
        undefined=ChainableUndefined,
        autoescape=False,
    )
    env.globals["url_for"] = lambda *a, **k: "#"
    env.filters["tojson"] = lambda v, **k: json.dumps(
        None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    env.policies["json.dumps_function"] = lambda v, **k: json.dumps(
        None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    html = env.get_template("personas.html").render(
        i18n={}, request=None, user_role="master", ui_lang="zh",
        help_terms={},
    )
    assert 'id="import-file-input"' in html
    assert ".yaml" in html and ".docx" in html
    assert 'id="doc-import-toolbar-btn"' not in html
    assert 'id="doc-import-tpl-btn"' in html
    assert 'id="import-extract-btn"' in html
    assert "_ingestImportFile" in html and "_ingestDocxFile" in html
    assert 'id="pb-restore-file"' in html
    assert "/api/personas/profiles/parse" in html
    assert "_parsePersonaText" in html
    start = html.index("async function _executeImport")
    assert start > 0
    seg = html[start:start + 1800]
    assert "/api/personas/profiles/import" in seg
    assert "method: 'PUT'" not in seg
    assert "dry_run" in html[html.index("async function _parseImport"):html.index("async function _executeImport")]
    from src.web.i18n_packs.persona_studio import EN, ZH
    for key in ("psn_imp_extract_cta", "psn_imp_detected", "psn_imp_bad_type",
                "psn_bulk_import_t", "psn_bulk_import_title"):
        assert ZH.get(key) and EN.get(key), key
    assert set(re.findall(r"\{(\w+)\}", ZH["psn_imp_detected"])) == set(
        re.findall(r"\{(\w+)\}", EN["psn_imp_detected"])) == {"fmt", "n"}
