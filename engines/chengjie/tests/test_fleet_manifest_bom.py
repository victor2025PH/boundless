"""manifest.json with a UTF-8 BOM (Windows PowerShell 5.1 Set-Content -Encoding utf8) must still parse.

Covers the draft patch D:\fleet-prod-plan\draft\fleet_oneclick_and_bom.patch (FLEET_ISSUES e)
plus the console_url default in fleet_home.html.
"""
from __future__ import annotations

import codecs
import io
import json
import urllib.request
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.fleet import admin as admin_mod
from src.fleet import store as store_mod
from src.fleet.store import resolve_download, resolve_fleet_cfg

ENGINE = Path(__file__).resolve().parents[1]
MANIFEST = {
    "name": "chatx-agent", "version": "0.3.2", "url": "https://d/chatx-agent.exe", "sha256": "ab" * 32,
    "installer": "https://d/Install-ChatXAgent.ps1", "setup_url": "https://d/ChatXAgentSetup.exe",
    "setup_sha256": "cd" * 32,
}
BOM_BYTES = codecs.BOM_UTF8 + json.dumps(MANIFEST, indent=2).encode("utf-8")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def serve_bom(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Resp(BOM_BYTES))
    monkeypatch.setattr(store_mod, "_manifest_cache", {"url": "", "at": 0.0, "data": None})


def test_plain_utf8_decode_rejects_bom_this_is_the_live_bug():
    with pytest.raises(json.JSONDecodeError):
        json.loads(BOM_BYTES.decode("utf-8"))


def test_store_fetch_manifest_tolerates_bom(serve_bom):
    assert store_mod._fetch_manifest("https://d/manifest.json") == MANIFEST


def test_resolve_download_with_bom_manifest_publishes_setup(serve_bom):
    cfg = {"download": {"manifest_url": "https://d/manifest.json", "version": "", "installer_url": "", "sha256": ""}}
    dl = resolve_download(cfg, now=1000.0)
    assert dl["version"] == "0.3.2"
    assert dl.get("setup_url") == "https://d/ChatXAgentSetup.exe"
    assert dl.get("setup_sha256") == "cd" * 32


def test_admin_load_manifest_url_and_file_tolerate_bom(serve_bom, tmp_path):
    assert admin_mod.load_manifest("https://d/manifest.json") == MANIFEST
    p = tmp_path / "manifest.json"
    p.write_bytes(BOM_BYTES)
    assert admin_mod.load_manifest(str(p)) == MANIFEST
    p2 = tmp_path / "plain.json"
    p2.write_text(json.dumps(MANIFEST), encoding="utf-8")
    assert admin_mod.load_manifest(str(p2)) == MANIFEST


def test_build_setup_writes_manifest_without_bom():
    src = (ENGINE / "fleet_agent/build_setup.ps1").read_text(encoding="utf-8")
    assert "Set-Content -LiteralPath $mfPath -Encoding utf8" not in src
    assert "UTF8Encoding($false)" in src


def test_console_url_config_and_home_page_reinstall_section():
    assert resolve_fleet_cfg({})["console_url"] == ""
    c = resolve_fleet_cfg({"fleet_control": {"console_url": "https://fleet.bd2026.cc/fleet/console"}})
    assert c["console_url"] == "https://fleet.bd2026.cc/fleet/console"
    env = Environment(loader=FileSystemLoader(str(ENGINE / "domains/fleet_control/web/templates")),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("fleet_home.html").render(
        download={"setup_url": "https://d/ChatXAgentSetup.exe", "version": "0.3.2"},
        public_url="https://bd2026.cc/fleet", console_url="https://fleet.bd2026.cc/fleet/console", proto_version=1)
    assert 'id="reinstall"' in html and "/REMOVESTATE=1" in html
    assert html.count('href="https://fleet.bd2026.cc/fleet/console"') == 5
    assert 'href="/fleet/console"' not in html


def test_home_page_console_url_defaults_to_same_site_console():
    env = Environment(loader=FileSystemLoader(str(ENGINE / "domains/fleet_control/web/templates")),
                      autoescape=select_autoescape(["html"]))
    for extra in ({}, {"console_url": ""}, {"console_url": None}):
        html = env.get_template("fleet_home.html").render(
            download={}, public_url="https://bd2026.cc/fleet", proto_version=1, **extra)
        assert html.count('href="/fleet/console"') == 5
        assert 'href=""' not in html
    assert not html.lstrip().startswith("{")
    assert html.lstrip().lower().startswith("<!doctype html>")
