# -*- coding: utf-8 -*-
"""WP-6 人设备货就绪度 → #189「上线准备」门禁（2026-08-17 / 2026-09-05 改口径）。

钉住：清单四项判定语义（声音=克隆需参考音落盘、选了不发语音不适用 / 相册=只在
capabilities.photos 显式开时适用 / 绑定账号=注册表·config·会话绑定·默认人设任一
命中即就绪、路由没喂不进分母）、台词库与说话指纹 internal 永不进分母（#189：
台词库「专属 0/共享 11」曾把就绪度卡在 75%）、指纹 applicable＝包随部署、
路由端到端（真 PersonaManager + 真聚合，404 守卫）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.companion.persona_stock import (
    CHECKLIST_ITEMS,
    collect_binding_usage,
    collect_stock_readiness,
    count_lines_file,
    load_speech_print_keys,
    speech_print_snippet,
)

BOUND = {"account_count": 1, "chat_count": 0, "is_default": False,
         "accounts": [["telegram", "111"]]}
UNBOUND = {"account_count": 0, "chat_count": 0, "is_default": False, "accounts": []}


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


PERSONA = {"name": "Lin", "personality": "温柔", "role": "companion",
           "capabilities": {"photos": True}}


def test_all_ready_full_completion(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path),
        prints_keys={"Lin"},
        supply={"lin": {"beach": 2, "home": 1}, "": {"cafe": 5}},
        binding=BOUND)
    items = out["items"]
    assert out["checklist"] == list(CHECKLIST_ITEMS) == ["profile", "voice", "album", "binding"]
    assert all(items[k]["ready"] for k in CHECKLIST_ITEMS), items
    assert out["completion"] == 1.0
    assert out["applicable_count"] == 4
    assert items["voice"]["clone"] and items["voice"]["has_transcript"]
    assert items["album"]["count"] == 3 and items["album"]["shared_count"] == 5
    # 台词库 / 指纹仍聚合（运营面板读）但标 internal、不进分母
    assert items["lines"]["internal"] is True and items["lines"]["applicable"] is False
    assert items["lines"]["count"] == 2 and items["lines"]["common_count"] == 3
    assert items["speech_print"]["internal"] is True and items["speech_print"]["ready"] is True


def test_lines_never_block_completion_189(tmp_path):
    """#189 原景：台词库专属 0 / 共享 11 → 旧口径卡 75%；新口径不进分母＝满分。"""
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path, own=0, common=11),
        prints_available=False, supply={"lin": {"home": 1}}, binding=BOUND)
    assert out["items"]["lines"]["ready"] is False
    assert out["items"]["lines"]["common_count"] == 11
    assert out["applicable_count"] == 4 and out["completion"] == 1.0


def test_clone_voice_without_ref_not_ready(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path, with_ref=False),
        lines_dir=_mk_lines(tmp_path, own=0), prints_keys=set(), supply={})
    v = out["items"]["voice"]
    assert v["clone"] is True and v["ready"] is False and v["has_ref"] is False


def test_voice_opt_out_inapplicable(tmp_path):
    """「声音（或选不发语音）」：人设 voice_profile.enabled=false ＝ 选了不发语音。"""
    p = dict(PERSONA)
    p["voice_profile"] = {"enabled": False}
    out = collect_stock_readiness(
        "lin", p, _mk_voice_cfg(tmp_path, with_ref=False),
        lines_dir=_mk_lines(tmp_path, own=0), prints_keys=set(),
        supply={"lin": {"home": 1}}, binding=BOUND)
    v = out["items"]["voice"]
    assert v["voice_off"] is True and v["applicable"] is False and v["ready"] is False
    assert out["applicable_count"] == 3 and out["completion"] == 1.0


def test_photos_default_off_album_inapplicable(tmp_path):
    """发图能力缺省关：capabilities 缺省 / 显式 false 都不催备相册；显式 true 才进清单。"""
    for caps in (None, {"photos": False}):
        p = dict(PERSONA)
        if caps is None:
            p.pop("capabilities", None)
        else:
            p["capabilities"] = caps
        out = collect_stock_readiness(
            "lin", p, _mk_voice_cfg(tmp_path), lines_dir=_mk_lines(tmp_path),
            prints_keys={"Lin"}, supply={"lin": {"beach": 9}}, binding=BOUND)
        a = out["items"]["album"]
        assert a["applicable"] is False and a["ready"] is False and a["photos_on"] is False
        assert a["count"] == 9, "素材数仍如实聚合（相册 tab 的「已备货但开关关」提示要用）"
        assert out["applicable_count"] == 3
        assert out["completion"] == 1.0, "关图人设其余三项全备＝满分，不被相册行拖住"


def test_binding_row_semantics(tmp_path):
    base = dict(persona=dict(PERSONA), full_config=_mk_voice_cfg(tmp_path),
                lines_dir=_mk_lines(tmp_path), prints_keys={"Lin"},
                supply={"lin": {"home": 1}})
    # 路由没喂 → 不适用不进分母（旧调用方零行为变化）
    out = collect_stock_readiness("lin", **base)
    assert out["items"]["binding"]["applicable"] is False
    assert out["applicable_count"] == 3
    # 没人用 → 缺项
    out = collect_stock_readiness("lin", **base, binding=UNBOUND)
    assert out["items"]["binding"]["applicable"] is True
    assert out["items"]["binding"]["ready"] is False
    assert out["applicable_count"] == 4 and out["ready_count"] == 3
    # 会话绑定 / 全局默认 任一命中即就绪
    out = collect_stock_readiness("lin", **base, binding={**UNBOUND, "chat_count": 2})
    assert out["items"]["binding"]["ready"] is True
    out = collect_stock_readiness("lin", **base, binding={**UNBOUND, "is_default": True})
    assert out["items"]["binding"]["ready"] is True


def test_collect_binding_usage_sources(monkeypatch):
    """注册表 meta.persona_ids + config 各平台 persona_ids + 会话绑定 + 默认人设。"""
    import src.companion.persona_stock as ps

    class _Reg:
        def list(self, plat):
            if plat == "telegram":
                return [{"account_id": "111", "meta": {"persona_ids": ["lin"]}},
                        {"account_id": "222", "meta": {"persona_ids": ["other"]}},
                        {"account_id": "333", "status": "removed", "meta": {"persona_ids": ["lin"]}}]
            if plat == "line":
                return [{"account_id": "U9", "meta": {"persona_ids": "lin"}}]
            return []

    import types
    fake_mod = types.SimpleNamespace(
        get_account_registry=lambda: _Reg(),
        parse_persona_ids=lambda meta: (
            [meta["persona_ids"]] if isinstance((meta or {}).get("persona_ids"), str)
            else list((meta or {}).get("persona_ids") or [])),
    )
    monkeypatch.setitem(__import__("sys").modules, "src.integrations.account_registry", fake_mod)

    class _PM:
        _domain_persona = {"id": "lin"}

        def get_all_chat_bindings(self):
            return {"c1": {"id": "lin"}, "c2": {"id": "x", "_profile_ref": "lin"}, "c3": {"id": "y"}}

    cfg = {"whatsapp_rpa": {"accounts": [{"account_id": "wa1", "persona_ids": ["lin"]}]},
           "telegram": {"persona_ids": ["lin"]}}
    u = ps.collect_binding_usage("lin", _PM(), cfg)
    assert u["account_count"] == 4, u          # tg 111 + line U9 + wa1 + telegram/default
    assert u["chat_count"] == 2 and u["is_default"] is True
    assert ["telegram", "333"] not in u["accounts"], "removed 行不算"
    assert ps.collect_binding_usage("", _PM(), cfg)["account_count"] == 0


def test_prints_pack_missing_inapplicable(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path), prints_available=False,
        supply={"lin": {"home": 1}}, binding=BOUND)
    sp = out["items"]["speech_print"]
    assert sp["applicable"] is False and sp["snippet"] == "" and sp["internal"] is True
    assert out["applicable_count"] == 4 and out["completion"] == 1.0


def test_missing_print_gets_named_with_snippet(tmp_path):
    out = collect_stock_readiness(
        "lin", dict(PERSONA), _mk_voice_cfg(tmp_path),
        lines_dir=_mk_lines(tmp_path), prints_keys={"别人"}, supply={}, binding=BOUND)
    sp = out["items"]["speech_print"]
    assert sp["ready"] is False and sp["spoken_name"] == "Lin"
    assert '"Lin"' in sp["snippet"] and '"print"' in sp["snippet"], \
        "缺指纹必须点名 + 给可复制模板片段"
    # 指纹缺席不再拖完成度（internal）：分母只有四项清单；供给空 → 相册缺，其余三项满
    assert out["applicable_count"] == 4 and out["ready_count"] == 3
    assert out["items"]["album"]["ready"] is False


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
        "profile", "voice", "album", "binding", "lines", "speech_print"}
    assert body["checklist"] == ["profile", "voice", "album", "binding"]
    assert body["items"]["profile"]["ready"] is True
    # 路由真喂了绑定用量 → 行适用（真 PersonaManager 下 lin 未被任何账号用 → 缺项）
    assert body["items"]["binding"]["applicable"] is True
    assert 0.0 <= body["completion"] <= 1.0


def test_route_404_unknown_persona(stock_client):
    assert stock_client.get(
        "/api/personas/nope/stock-readiness").status_code == 404
