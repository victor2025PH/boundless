# -*- coding: utf-8 -*-
"""WP-6 人设备货就绪度门禁（2026-08-17）。

钉住：五行判定语义（音色=克隆需参考音落盘 / 相册=显式关图不适用 / 台词=专属
文件不被 _common 顶替 / 指纹=口称名逐字命中，缺包不适用不进分母）、完成度
分母只算适用项、路由端到端（真 PersonaManager + 真聚合，404 守卫）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.companion.persona_stock import (
    collect_stock_readiness,
    count_lines_file,
    load_speech_print_keys,
    speech_print_snippet,
)


def _mk_voice_cfg(tmp_path, *, backend="avatar_clone", with_ref=True,
                  with_transcript=True):
    ref = tmp_path / "ref.wav"
    if with_ref:
        ref.write_bytes(b"RIFFfake")
        if with_transcript:
            (tmp_path / "ref.txt").write_text("参考音逐字稿", encoding="utf-8")
    vp = {"backend": backend, "enabled": True}
    if with_ref:
        # 键名用生产正典 reference_audio_path（2026-08-18 真机探针教训：夹具与
        # 模块用同一个错键会互相印证假绿——夹具必须长得像生产数据）
        vp["reference_audio_path"] = str(ref)
    return {"personas": {"profiles": [{"id": "lin", "voice_profile": vp}]}}


def _mk_lines(tmp_path, *, own=2, common=3):
    d = tmp_path / "lines"
    d.mkdir(exist_ok=True)
    if own:
        (d / "lin.txt").write_text(
            "# 注释\n\n" + "\n".join(f"专属台词{i}" for i in range(own)),
            encoding="utf-8")
    if common:
        (d / "_common.txt").write_text(
            "\n".join(f"共享台词{i}" for i in range(common)), encoding="utf-8")
    return d


PERSONA = {"name": "Lin", "personality": "温柔", "role": "companion"}


def test_all_ready_full_completion(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path),
        prints_keys={"Lin"},
        supply={"lin": {"beach": 2, "home": 1}, "": {"cafe": 5}})
    items = out["items"]
    assert all(items[k]["ready"] for k in
               ("profile", "voice", "album", "lines", "speech_print")), items
    assert out["completion"] == 1.0
    assert out["applicable_count"] == 5
    assert items["voice"]["clone"] and items["voice"]["has_transcript"]
    assert items["album"]["count"] == 3 and items["album"]["shared_count"] == 5
    assert items["lines"]["count"] == 2 and items["lines"]["common_count"] == 3


def test_clone_voice_without_ref_not_ready(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path, with_ref=False),
        lines_dir=_mk_lines(tmp_path, own=0), prints_keys=set(), supply={})
    v = out["items"]["voice"]
    assert v["clone"] is True and v["ready"] is False and v["has_ref"] is False


def test_photos_off_album_inapplicable(tmp_path):
    p = dict(PERSONA)
    p["capabilities"] = {"photos": False}
    out = collect_stock_readiness(
        "lin", p, _mk_voice_cfg(tmp_path), lines_dir=_mk_lines(tmp_path),
        prints_keys={"Lin"}, supply={"lin": {"beach": 9}})
    a = out["items"]["album"]
    assert a["applicable"] is False and a["ready"] is False
    assert out["applicable_count"] == 4
    assert out["completion"] == 1.0, "关图人设其余四项全备＝满分，不被相册行拖住"


def test_prints_pack_missing_inapplicable(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path), prints_available=False,
        supply={"lin": {"home": 1}})
    sp = out["items"]["speech_print"]
    assert sp["applicable"] is False and sp["snippet"] == ""
    assert out["applicable_count"] == 4 and out["completion"] == 1.0


def test_missing_print_gets_named_with_snippet(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path), prints_keys={"别人"}, supply={})
    sp = out["items"]["speech_print"]
    assert sp["ready"] is False and sp["spoken_name"] == "Lin"
    assert '"Lin"' in sp["snippet"] and '"print"' in sp["snippet"], \
        "缺指纹必须点名 + 给可复制模板片段"


def test_common_lines_do_not_satisfy_persona_row(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path, own=0, common=9),
        prints_keys={"Lin"}, supply={})
    ln = out["items"]["lines"]
    assert ln["ready"] is False and ln["common_count"] == 9, \
        "_common 是共享基线，不得顶替人设专属台词行"


def test_count_lines_file_filters(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("# c\n\n a \nb\n#d\n", encoding="utf-8")
    assert count_lines_file(f) == 2
    assert count_lines_file(tmp_path / "nope.txt") == 0


def test_load_speech_print_keys_skips_meta(tmp_path):
    f = tmp_path / "prints.json"
    f.write_text('{"_说明": "x", "Lin": {"print": "p"}}', encoding="utf-8")
    assert load_speech_print_keys(f) == {"Lin"}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_speech_print_keys(bad) is None


def test_snippet_schema_matches_library():
    s = speech_print_snippet("Marcus Wei（韦明远）")
    assert "Marcus Wei（韦明远）" in s
    for k in ('"print"', '"catch"', '"example"'):
        assert k in s


# ── 路由端到端 ───────────────────────────────────────────────────────────────

@pytest.fixture()
def stock_client(tmp_path, monkeypatch):
    import src.web.routes.persona_media_routes as pmr
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin", "personality": "温柔",
                              "role": "companion"})

    class _CfgMgr:
        config = _mk_voice_cfg(tmp_path)
        config["companion"] = {"selfie": {"provider": {
            "album_dir": str(tmp_path / "persona_media")}}}
        config_path = str(tmp_path / "config" / "config.yaml")

    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, config_manager=_CfgMgr())
    yield TestClient(app)
    reset_persona_media_store()
    pm.delete_profile("lin")


def test_route_returns_aggregate(stock_client):
    r = stock_client.get("/api/personas/lin/stock-readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["persona_id"] == "lin"
    assert set(body["items"]) == {
        "profile", "voice", "album", "lines", "speech_print"}
    assert body["items"]["profile"]["ready"] is True
    assert 0.0 <= body["completion"] <= 1.0


def test_route_404_unknown_persona(stock_client):
    assert stock_client.get(
        "/api/personas/nope/stock-readiness").status_code == 404
