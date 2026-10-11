"""垂直模板包运行时加载器（智语 2026-10-08）。

公开包可列出、可预览、可显式播种；内部包默认隐藏；未填占位符的条目不进 KB。
起步包未知域仍回落 general。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.utils.kb_starter import get_starter_pack
from src.utils.kb_store import KnowledgeBaseStore
from src.utils.vertical_pack import (
    InternalPackHidden,
    UnknownPack,
    apply_kb_seed,
    assert_persona_out_allowed,
    edition_shows_internal,
    handle_seed_request,
    holding_line,
    list_packs,
    load_pack,
    persona_profile,
    stop_confirm_line,
)

ENGINE = Path(__file__).resolve().parents[1]
PUBLIC = ("cross_border_private", "agency", "own_business")


def _clear_edition(monkeypatch):
    monkeypatch.delenv("CHATX_FLAVOR", raising=False)
    monkeypatch.delenv("CHATX_EDITION", raising=False)


def test_default_list_hides_internal_pack(monkeypatch):
    _clear_edition(monkeypatch)
    ids = [p["id"] for p in list_packs()]
    assert set(ids) == set(PUBLIC)
    assert all(p["internal"] is False and p["edition"] == "public" for p in list_packs())
    assert edition_shows_internal(None) is False


def test_explicit_include_and_env(monkeypatch):
    _clear_edition(monkeypatch)
    shown = [p["id"] for p in list_packs(include_internal=True)]
    assert "gambling_operator" in shown
    hidden = [p["id"] for p in list_packs(include_internal=False)]
    assert "gambling_operator" not in hidden

    monkeypatch.setenv("CHATX_EDITION", "internal")
    assert "gambling_operator" in [p["id"] for p in list_packs()]
    assert "gambling_operator" not in [p["id"] for p in list_packs(include_internal=False)]

    monkeypatch.setenv("CHATX_FLAVOR", "public")
    monkeypatch.setenv("CHATX_EDITION", "internal")
    assert "gambling_operator" not in [p["id"] for p in list_packs()]
    assert "gambling_operator" in [p["id"] for p in list_packs(include_internal=True)]

    monkeypatch.setenv("CHATX_FLAVOR", "client")
    assert edition_shows_internal(None) is False
    monkeypatch.setenv("CHATX_FLAVOR", "dev")
    assert edition_shows_internal(None) is False
    monkeypatch.setenv("CHATX_FLAVOR", "full")
    assert edition_shows_internal(None) is False
    monkeypatch.delenv("CHATX_FLAVOR", raising=False)
    monkeypatch.setenv("CHATX_EDITION", "internal")
    assert edition_shows_internal(None) is True


def test_load_unknown_and_hidden(monkeypatch):
    _clear_edition(monkeypatch)
    pack = load_pack("agency")
    assert pack["id"] == "agency" and pack["persona"]["id"] == "agency_brand_assistant"
    assert pack["kb"]["entries"]
    with pytest.raises(UnknownPack):
        load_pack("no-such-pack")
    with pytest.raises(InternalPackHidden):
        load_pack("gambling_operator")
    internal = load_pack("gambling_operator", include_internal=True)
    assert internal["edition"] == "internal_only" and internal["internal"] is True


def test_starter_pack_unknown_domain_still_general():
    assert get_starter_pack("agency")["name"] == get_starter_pack("general")["name"]
    assert get_starter_pack("cross_border_private")["name"] == get_starter_pack("general")["name"]


def test_dry_run_blocks_unfilled_and_does_not_write(tmp_path, monkeypatch):
    _clear_edition(monkeypatch)
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    report = apply_kb_seed(kb, "agency", {}, apply=False)
    assert report["apply"] is False and report["added"] == 0
    assert report["would_add_titles"]
    assert report["blocked"] >= 1
    assert any("brand_name" in e["placeholders"] for e in report["blocked_entries"])
    assert kb.stats()["total_entries"] == 0
    for title in report["would_add_titles"]:
        assert "{" not in title


def test_apply_writes_import_entries_and_skips_placeholders(tmp_path, monkeypatch):
    _clear_edition(monkeypatch)
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    report = apply_kb_seed(kb, "agency", {"brand_name": "Acme"}, apply=True)
    assert report["added"] >= 1
    assert report["blocked"] >= 1
    rows = kb.list_entries()
    assert len(rows) == report["added"]
    for row in rows:
        assert row["source"] == "import"
        assert row["reply_mode"] == "ai_guided"
        assert int(row["enabled"]) == 1
        blob = " ".join([
            str(row.get("title") or ""),
            str(row.get("example_reply_zh") or ""),
            str(row.get("scenario") or ""),
            str(row.get("triggers") or ""),
        ])
        assert "{brand_name}" not in blob
        assert "{" not in str(row.get("example_reply_zh") or "")
    again = apply_kb_seed(kb, "agency", {"brand_name": "Acme"}, apply=True)
    assert again["added"] == 0 and again["skipped"] == report["added"]


def test_apply_full_agency_placeholders(tmp_path, monkeypatch):
    _clear_edition(monkeypatch)
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    values = {
        "brand_name": "Acme",
        "main_products": "bags",
        "product": "tote",
        "price": "99",
        "hours": "9-6",
        "address": "Manila",
    }
    report = apply_kb_seed(kb, "agency", values, apply=True)
    assert report["blocked"] == 0
    assert report["added"] == len(load_pack("agency")["kb"]["entries"])
    assert all("{" not in t for t in report["added_titles"])


def test_persona_profile_and_lines(monkeypatch):
    _clear_edition(monkeypatch)
    pack = load_pack("agency")
    bare = persona_profile(pack, {})
    assert bare["meta"]["vertical_pack"] == "agency"
    assert bare["kind"] == "support"
    assert bare["name"] == "{assistant_name}"
    filled = persona_profile(pack, {
        "assistant_name": "Mia", "brand_name": "Acme", "brand_tone": "warm",
    })
    assert filled["name"] == "Mia"
    assert "Acme" in filled["role"] and "{" not in filled["background"]
    assert "warm" in filled["personality"]["style"]
    assert stop_confirm_line(pack, "tl").startswith("Sige")
    assert "Acme" in stop_confirm_line(pack, "zh", voice="brand", values={"brand_name": "Acme"})
    assert holding_line(pack, "en").startswith("Got it")
    assert holding_line(pack, "tl", kind="after_hours_reply")


def test_refuses_profiles_runtime_yaml(tmp_path):
    with pytest.raises(Exception, match="profiles_runtime"):
        assert_persona_out_allowed(tmp_path / "profiles_runtime.yaml")
    assert_persona_out_allowed(tmp_path / "agency_persona.yaml")


def test_http_seed_ignores_allow_internal_and_reports_codes(tmp_path, monkeypatch):
    _clear_edition(monkeypatch)
    monkeypatch.setenv("CHATX_FLAVOR", "public")
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    hidden = handle_seed_request(kb, {
        "vertical_pack": "gambling_operator",
        "allow_internal": True,
        "include_internal": True,
        "dry_run": True,
    })
    assert hidden == {"ok": False, "detail": "internal_pack_hidden"}
    unknown = handle_seed_request(kb, {"vertical_pack": "nope", "dry_run": True})
    assert unknown["detail"] == "unknown_pack" and unknown["ok"] is False

    preview = handle_seed_request(kb, {"vertical_pack": "agency", "dry_run": True})
    assert preview["ok"] is True and preview["apply"] is False and preview["added"] == 0
    assert kb.stats()["total_entries"] == 0

    written = handle_seed_request(kb, {
        "vertical_pack": "agency",
        "vars": {"brand_name": "Acme"},
    })
    assert written["ok"] is True and written["apply"] is True and written["added"] >= 1
    assert kb.stats()["total_entries"] == written["added"]


def test_http_all_blocked_is_unfilled(monkeypatch):
    def _fake(*_a, **_k):
        return {
            "pack_id": "agency", "edition": "public", "apply": True,
            "added": 0, "skipped": 0, "blocked": 2,
            "blocked_entries": [{"title": "x", "placeholders": ["price"]}],
            "added_titles": [], "would_add_titles": [],
        }

    monkeypatch.setattr("src.utils.vertical_pack.apply_kb_seed", _fake)
    out = handle_seed_request(object(), {"vertical_pack": "agency"})
    assert out["ok"] is False and out["detail"] == "unfilled_placeholders"
    assert out["blocked"] == 2


def test_cli_list_dry_run_apply_and_persona(tmp_path, monkeypatch, capsys):
    _clear_edition(monkeypatch)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "apply_vertical_pack", ENGINE / "scripts" / "apply_vertical_pack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert "agency" in listed and "gambling_operator" not in listed

    assert mod.main(["--pack", "gambling_operator"]) == 2
    err = capsys.readouterr().err
    assert "internal_pack_hidden" in err

    assert mod.main(["--pack", "agency", "--set", "brand_name=Acme"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False and preview["added"] == 0
    assert preview["holding_reply"]["zh"]

    persona = tmp_path / "mia.yaml"
    db = tmp_path / "kb.db"
    code = mod.main([
        "--pack", "agency", "--db", str(db), "--apply",
        "--set", "brand_name=Acme", "--set", "main_products=bags",
        "--set", "product=tote", "--set", "price=99",
        "--set", "hours=9-6", "--set", "address=Manila",
        "--set", "assistant_name=Mia", "--set", "brand_tone=warm",
        "--persona-out", str(persona),
    ])
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["blocked"] == 0 and body["added"] >= 6
    assert body["persona_out"] == str(persona)
    profile = yaml.safe_load(persona.read_text(encoding="utf-8"))
    assert profile["name"] == "Mia" and profile["meta"]["vertical_pack"] == "agency"
    assert not (tmp_path / "profiles_runtime.yaml").exists()

    refused = tmp_path / "profiles_runtime.yaml"
    assert mod.main(["--pack", "agency", "--persona-out", str(refused)]) == 2
    assert "profiles_runtime" in capsys.readouterr().err
    assert not refused.exists()


def test_routes_advertise_vertical_pack_without_new_paths():
    import inspect
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes.register_kb_routes)
    assert "/api/kb/cold-start" in src and "/api/kb/seed-pack" in src
    assert "vertical_packs" in src and "handle_seed_request" in src
    assert "unknown_pack" not in src
    assert "internal_pack_hidden" not in src
