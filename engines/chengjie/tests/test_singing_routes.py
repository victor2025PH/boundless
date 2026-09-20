# -*- coding: utf-8 -*-
"""歌房路由契约（实施58 P1）：overview 汇总 / 试听流 / 开关白名单 / 曲目启停。

不变量：
- 开关写入只接受白名单键且数值钳制（防任意键注入 overlay）；
- 试听 id 消毒（路径穿越直接 400）；
- 曲目启停只翻 enabled 位，manifest 其余字段（source 版权台账）原样保留。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.routes.singing_routes import register_singing_routes  # noqa: E402


class _FakeCfgMgr:
    def __init__(self, tdir: Path, sroot: Path):
        self.config = {"companion": {"singing": {
            "enabled": True, "daily_cap": 2, "cooldown_hours": 6,
            "repeat_window_days": 7, "allow_lang_fallback": False,
            "templates_dir": str(tdir), "stock_dir": str(sroot),
        }}}
        self.flags = []

    def set_overlay_flag(self, path, value):
        self.flags.append((path, value))
        return True, "ok"


def _seed(tmp_path: Path):
    tdir = tmp_path / "song_templates"
    (tdir / "anchors").mkdir(parents=True)
    sroot = tmp_path / "voices"
    (tdir / "manifest.json").write_text(json.dumps({"templates": [
        {"id": "origin_moon", "title": "月光", "file": "origin_moon_dry.wav",
         "lyrics": "月亮爬上了窗台", "lang": "zh", "source": "origin",
         "enabled": True},
        {"id": "origin_wind", "title": "晚风", "file": "origin_wind.wav",
         "lyrics": "晚风轻轻吹过窗前", "lang": "zh", "source": "origin",
         "enabled": False},
    ]}, ensure_ascii=False), encoding="utf-8")
    (tdir / "voices.json").write_text(json.dumps({"voices": [
        {"key": "warm_f", "label": "暖女声", "anchor": "anchors/warm_f.wav",
         "prompt": "warm female", "canvas_s": 28, "personas": ["p1", "p2"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    (tdir / "anchors" / "warm_f.wav").write_bytes(b"RIFF" + b"\0" * 200)
    sdir = sroot / "p1" / "songs"
    sdir.mkdir(parents=True)
    (sdir / "origin_moon.ogg").write_bytes(b"OggS" + b"\0" * 70000)
    (sdir / "origin_moon.json").write_text(json.dumps({
        "casting_voice": "warm_f", "duration_sec": 19.4,
        "sim_vs_anchor": 0.89, "supply": "casting_direct"}),
        encoding="utf-8")
    return tdir, sroot


def _client(tmp_path: Path):
    tdir, sroot = _seed(tmp_path)
    app = FastAPI()

    def _auth(request: Request):
        return True

    mgr = _FakeCfgMgr(tdir, sroot)
    register_singing_routes(app, api_auth=_auth, config_manager=mgr)
    return TestClient(app), mgr


def test_overview_matrix_and_config(tmp_path):
    client, _ = _client(tmp_path)
    d = client.get("/api/singing/overview").json()
    assert d["ok"] is True
    assert d["config"]["enabled"] is True
    assert d["config"]["daily_cap"] == 2
    assert [v["key"] for v in d["voices"]] == ["warm_f"]
    assert d["voices"][0]["anchor_ok"] is True
    assert {t["id"] for t in d["templates"]} == {"origin_moon", "origin_wind"}
    assert d["matrix"]["p1"]["origin_moon"]["voice"] == "warm_f"
    assert "origin_moon" not in d["matrix"]["p2"]        # p2 无货如实为空


def test_audio_serves_stock_and_sanitizes(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/singing/audio/p1/origin_moon")
    assert r.status_code == 200
    assert r.content[:4] == b"OggS"
    assert client.get("/api/singing/audio/p2/origin_moon").status_code == 404
    assert client.get("/api/singing/audio/..%2Fetc/x").status_code in (400, 404)
    assert client.get("/api/singing/audio/p1/bad..id").status_code == 400


def test_anchor_serves_and_unknown_404(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/singing/anchor/warm_f")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert client.get("/api/singing/anchor/nope").status_code == 404


def test_config_whitelist_and_clamp(tmp_path):
    client, mgr = _client(tmp_path)
    r = client.post("/api/singing/config",
                    json={"key": "enabled", "value": True})
    assert r.json()["ok"] is True
    r2 = client.post("/api/singing/config",
                     json={"key": "daily_cap", "value": 99})
    assert r2.json()["value"] == 10                       # 钳到上限
    assert ("companion.singing.enabled", True) in mgr.flags
    assert ("companion.singing.daily_cap", 10) in mgr.flags
    assert client.post("/api/singing/config",
                       json={"key": "evil.key", "value": 1}).status_code == 400
    assert client.post("/api/singing/config",
                       json={"key": "daily_cap",
                             "value": "abc"}).status_code == 400


def test_template_enable_toggles_and_preserves(tmp_path):
    client, mgr = _client(tmp_path)
    tdir = Path(mgr.config["companion"]["singing"]["templates_dir"])
    r = client.post("/api/singing/template-enable",
                    json={"id": "origin_wind", "enabled": True})
    assert r.json()["ok"] is True
    data = json.loads((tdir / "manifest.json").read_text(encoding="utf-8"))
    rows = {t["id"]: t for t in data["templates"]}
    assert rows["origin_wind"]["enabled"] is True
    assert rows["origin_wind"]["source"] == "origin"      # 台账字段原样保留
    assert rows["origin_moon"]["enabled"] is True
    assert client.post("/api/singing/template-enable",
                       json={"id": "nope", "enabled": True}).status_code == 404
