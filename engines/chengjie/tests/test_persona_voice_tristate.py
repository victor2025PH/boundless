# -*- coding: utf-8 -*-
"""L-2 A（#205 前半，D-L4）：人设语音三态 + 保存校验 + 新建默认「不发语音」（2026-09-06）。

事故（8CE7U3 / 5QMZ5E / ERS5QQ，skuio）：语音 tab「语音 BACKEND 五选一 + 手填 VOICE ID」
研发面直出；Mizuki 存成 ``avatar_clone + ja-JP-NanamiNeural``（克隆引擎 + 预置声名、无
录音）保存成功、页面还标「已绑定克隆音色: ja-JP-NanamiNeural」。

本文件钉住：
- ``derive_voice_mode`` 三态互斥推导（显式 > 登记齐备克隆 > 克隆引擎无录音 > 预置 > 沿用全局）；
- ``validate_voice_profile``：Mizuki 形态 error / 预置声不在合法表 error / 参考音文件不在 warning；
- ``PUT /api/personas/profiles/{id}``：非法组合 400 + voice_problems；新建人设默认 off；
  存量非法档保存时 400 提示修正（不静默改）；显式 clone 补丁可清掉残留预置声名；
- TTSPipeline / autosend：voice_mode=off → persona_voice_off + degrade_to_text，不打引擎；
- ``_merge_voice_profile``：{voice_mode: off} 盖过全局克隆档；登记构造器写 voice_mode=clone。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.ai import voice_tristate as vt
from src.ai.tts_pipeline import TTSPipeline
from src.utils.persona_manager import PersonaManager


def _enrolled_vp(**over) -> Dict[str, Any]:
    vp = {
        "enabled": True, "owner_consent": True, "backend": "avatar_clone",
        "voice_mode": "clone", "source": "avatar_zeroshot", "speaker_id": "steven",
        "voice": "", "reference_audio_path": "voice_samples/steven.wav",
    }
    vp.update(over)
    return vp


# Mizuki 实机形态（8CE7U3）：克隆引擎 + 预置声名，没有录音
_MIZUKI_VP = {"backend": "avatar_clone", "voice": "ja-JP-NanamiNeural"}


# ══ 1. 纯函数 ═══════════════════════════════════════════════════════════════

def test_derive_mode_precedence():
    assert vt.derive_voice_mode({"voice_mode": "off", "backend": "avatar_clone"}) == ("off", "explicit")
    assert vt.derive_voice_mode(_enrolled_vp(voice_mode="")) == ("clone", "registered_clone")
    assert vt.derive_voice_mode(_MIZUKI_VP) == ("clone", "clone_backend")
    assert vt.derive_voice_mode({"backend": "edge_tts", "voice": "zh-CN-YunxiNeural"}) == ("preset", "preset_backend")
    assert vt.derive_voice_mode({"voice": "zh-CN-YunxiNeural"}) == ("preset", "preset_voice")
    assert vt.derive_voice_mode({"backend": "", "voice": ""}) == ("preset", "inherit_global")
    assert vt.derive_voice_mode({}) == ("preset", "inherit_global")
    assert vt.derive_voice_mode(None) == ("preset", "inherit_global")
    assert vt.derive_voice_mode({"enabled": False}) == ("off", "disabled")


def _codes(problems):
    return sorted(p["code"] for p in problems)


def test_validate_mizuki_shape_rejected():
    errs, warns = vt.split_problems(vt.validate_voice_profile(_MIZUKI_VP))
    assert _codes(errs) == ["clone_missing_reference", "clone_with_preset_voice"]
    assert warns == []


def test_validate_registered_clone_ok_and_missing_file_is_warning(tmp_path):
    ref = tmp_path / "steven.wav"
    ref.write_bytes(b"RIFF")
    assert vt.validate_voice_profile(_enrolled_vp(reference_audio_path=str(ref))) == []
    errs, warns = vt.split_problems(vt.validate_voice_profile(_enrolled_vp()))
    assert errs == []
    assert _codes(warns) == ["clone_reference_file_missing"]
    # hub 只留 speaker_id、无本地路径：合法（远端登记）
    assert vt.validate_voice_profile(_enrolled_vp(reference_audio_path="")) == []
    # check_files 关：路径不在也不警告
    assert vt.validate_voice_profile(_enrolled_vp(), check_files=False) == []


def test_validate_clone_with_stray_preset_name_even_if_registered(tmp_path):
    ref = tmp_path / "s.wav"
    ref.write_bytes(b"RIFF")
    errs, _ = vt.split_problems(vt.validate_voice_profile(
        _enrolled_vp(reference_audio_path=str(ref), voice="ja-JP-NanamiNeural")))
    assert _codes(errs) == ["clone_with_preset_voice"]


def test_validate_preset_rules():
    ok = {"voice_mode": "preset", "backend": "edge_tts", "voice": "ja-JP-KeitaNeural"}
    assert vt.validate_voice_profile(ok) == []
    assert _codes(vt.validate_voice_profile({"voice_mode": "preset", "backend": "edge_tts", "voice": "steven"})) == ["preset_voice_unknown"]
    assert _codes(vt.validate_voice_profile({"voice_mode": "preset", "backend": "edge_tts", "voice": ""})) == ["preset_voice_missing"]
    assert _codes(vt.validate_voice_profile({"voice_mode": "preset", "backend": "avatar_clone", "voice": "zh-CN-XiaoxiaoNeural"})) == ["preset_backend_is_clone"]
    assert vt.validate_voice_profile({"voice_mode": "preset", "backend": "openai", "voice": "onyx"}) == []
    assert _codes(vt.validate_voice_profile({"voice_mode": "preset", "backend": "openai", "voice": "zh-CN-XiaoxiaoNeural"})) == ["preset_voice_unknown"]
    # 存量「什么都没填」（沿用全局）不报错——D-L4 其余→预置声并标注
    assert vt.validate_voice_profile({"backend": "", "voice": ""}) == []
    assert vt.validate_voice_profile({"voice_mode": "off"}) == []
    assert _codes(vt.validate_voice_profile({"voice_mode": "loud"})) == ["invalid_mode"]


def test_import_coerce_mizuki_pack_becomes_preset():
    """克隆无录音 + Neural 名（Claire / skuio Mizuki 包）→ 预置声，不丢音色名。"""
    vp, info = vt.coerce_voice_profile_for_import(_MIZUKI_VP)
    assert info["action"] == "preset"
    assert "clone_missing_reference" in info["voice_problems"]
    assert vp["voice_mode"] == "preset" and vp["backend"] == "edge_tts"
    assert vp["voice"] == "ja-JP-NanamiNeural"
    assert vt.validate_voice_profile(vp) == []
    # 不改入参
    assert _MIZUKI_VP["backend"] == "avatar_clone"


def test_import_coerce_clone_without_voice_goes_off():
    vp, info = vt.coerce_voice_profile_for_import({"backend": "avatar_clone"})
    assert info["action"] == "off"
    assert vp == {"voice_mode": "off"}
    assert vt.validate_voice_profile(vp) == []


def test_import_coerce_strips_stray_neural_on_registered_clone(tmp_path):
    """有录音的克隆只多了残留 Neural 名：剥声名、保留克隆，不能降成预置。"""
    ref = tmp_path / "s.wav"
    ref.write_bytes(b"RIFF")
    src = _enrolled_vp(reference_audio_path=str(ref), voice="ja-JP-NanamiNeural")
    vp, info = vt.coerce_voice_profile_for_import(src, check_files=False)
    assert info["action"] == "strip_preset_voice"
    assert vp["voice"] == "" and vp["voice_mode"] == "clone"
    assert vp["backend"] == "avatar_clone" and vp["speaker_id"] == "steven"
    assert src["voice"] == "ja-JP-NanamiNeural"
    assert vt.split_problems(vt.validate_voice_profile(vp, check_files=False))[0] == []


def test_import_coerce_legal_unchanged():
    legal = {"voice_mode": "preset", "backend": "edge_tts", "voice": "en-US-AvaMultilingualNeural"}
    vp, info = vt.coerce_voice_profile_for_import(legal)
    assert info == {} and vp is legal


def test_apply_import_voice_coerce_on_persona():
    raw = {"id": "claire", "name": "Claire", "voice_profile": dict(_MIZUKI_VP)}
    out, info = vt.apply_import_voice_coerce(raw)
    assert info["action"] == "preset" and out is not raw
    assert out["name"] == "Claire"
    assert out["voice_profile"]["voice_mode"] == "preset"
    keep = {"id": "ok", "voice_profile": {"voice_mode": "off"}}
    out2, info2 = vt.apply_import_voice_coerce(keep)
    assert info2 == {} and out2 is keep


def test_new_persona_default_off():
    out = vt.apply_new_persona_voice_default({"name": "新人设"})
    assert out["voice_profile"] == {"voice_mode": "off"}
    out2 = vt.apply_new_persona_voice_default({"name": "x", "voice_profile": {"backend": "", "voice": ""}})
    assert out2["voice_profile"] == {"voice_mode": "off"}
    # 带了真配置（导入 / 复制 / 登记流）原样尊重
    keep = {"name": "y", "voice_profile": {"backend": "edge_tts", "voice": "zh-CN-YunxiNeural"}}
    assert vt.apply_new_persona_voice_default(keep) is keep
    assert vt.voice_mode_is_off({"voice_mode": "off"}) and not vt.voice_mode_is_off({"enabled": False})


def test_binding_summary_labels_by_actual_type():
    s = vt.binding_summary(_enrolled_vp())
    assert s["mode"] == "clone" and s["ref_name"] == "steven.wav"
    s2 = vt.binding_summary({"voice_mode": "preset", "backend": "edge_tts", "voice": "ja-JP-NanamiNeural"})
    assert (s2["mode"], s2["lang"], s2["gender"]) == ("preset", "ja", "female")


def test_enroll_builders_write_voice_mode_clone():
    from src.ai.voice_enroll import (
        build_avatar_voice_profile, build_lan_voice_profile, build_qwen_voice_profile)
    assert build_avatar_voice_profile(reference_audio_path="a.wav", speaker_id="s")["voice_mode"] == "clone"
    assert build_lan_voice_profile(reference_audio_path="a.wav", speaker_id="s", base_url="http://x")["voice_mode"] == "clone"
    assert build_qwen_voice_profile(voice="v", reference_audio_path="a.wav",
                                    voice_profile_json_path="p.json", speaker_id="s")["voice_mode"] == "clone"


def test_guard_lets_explicit_clone_patch_clear_stray_voice():
    from src.ai.voice_profile_guard import protect_registered_clone_patch
    existing = _enrolled_vp(voice="ja-JP-NanamiNeural")
    out, preserved = protect_registered_clone_patch(existing, {"voice_mode": "clone", "voice": "", "backend": "avatar_clone"})
    assert out["voice"] == "" and "voice" not in preserved
    # 旧契约不变：无 voice_mode 的空补丁仍被守卫保留
    out2, preserved2 = protect_registered_clone_patch(existing, {"backend": "", "voice": ""})
    assert "voice" not in out2 and "backend" not in out2 and sorted(preserved2) == ["backend", "voice"]


def test_merge_voice_profile_off_overrides_global_clone():
    from src.ai.persona_voice import _merge_voice_profile
    merged = {"backend": "avatar_clone", "voice_profile": _enrolled_vp(voice_mode="")}
    assert _merge_voice_profile(merged, {"voice_mode": "off"}) is True
    assert merged["voice_profile"]["voice_mode"] == "off"
    # 空占位仍被忽略
    assert _merge_voice_profile({"backend": "edge_tts"}, {"backend": "", "voice": ""}) is False


# ══ 2. 管线 / autosend ══════════════════════════════════════════════════════

async def test_pipeline_voice_mode_off_degrades_to_text_without_engine(tmp_path, monkeypatch):
    called = {"n": 0}

    async def fake_run_backend(self, *a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)
    tts = TTSPipeline({"enabled": True, "backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
                       "out_dir": str(tmp_path), "tts_cache": {"enabled": False},
                       "voice_profile": {"voice_mode": "off"}})
    rv = await tts.synthesize("你好")
    assert rv.ok is False and rv.error == "persona_voice_off"
    assert rv.extra.get("degrade_to_text") is True
    assert called["n"] == 0


async def test_autosend_synth_skips_off_persona(monkeypatch):
    from src.inbox import voice_autosend as va

    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "avatar_clone",
                                       "voice_profile": {"voice_mode": "off"}}, "emotion": None})
    path, meta = await va._synth_ogg({}, "quiet", "你好", out_dir=".")
    assert path is None and meta.get("voice_mode") == "off"
    assert va.pop_synth_failure_reason() == "persona_voice_off"


# ══ 3. 路由 ══════════════════════════════════════════════════════════════════

_HDRS = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}


@pytest.fixture
def app_on(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1",
                     "voice_reply": {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [{"id": "chen_mo", "name": "陈默", "role": "陪伴"}]},
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
    PersonaManager.get_instance().load_profiles_from_config(cfg)
    from src.web.admin import create_app
    app = create_app(cm)
    yield app
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(vp: Dict[str, Any], name: str = "测试") -> Dict[str, Any]:
    return {"persona": {"name": name, "role": "陪伴", "voice_profile": vp}, "merge": True}


@pytest.mark.asyncio
async def test_put_rejects_mizuki_combo_with_reason(app_on):
    """avatar_clone + ja-JP-NanamiNeural（无录音）→ 400，人话原因 + voice_problems。"""
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/mizuki", headers=_HDRS, json=_body(dict(_MIZUKI_VP), "Mizuki"))
        assert r.status_code == 400
        body = r.json()
        assert body["ok"] is False and body["voice_tab"] is True
        assert sorted(p["code"] for p in body["voice_problems"]) == ["clone_missing_reference", "clone_with_preset_voice"]
        assert "需修正" in body["detail"] or "needs fixing" in body["detail"].lower()
        r2 = await c.get("/api/personas/profiles/mizuki", headers=_HDRS)
        assert r2.status_code == 404, "被拒的保存不得落库"


@pytest.mark.asyncio
async def test_put_new_persona_defaults_to_off(app_on):
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/newbie", headers=_HDRS,
                        json={"persona": {"name": "小新", "role": "陪伴"}, "merge": True})
        assert r.status_code == 200
        p = (await c.get("/api/personas/profiles/newbie", headers=_HDRS)).json()["persona"]
        assert p["voice_profile"] == {"voice_mode": "off"}
        rows = {s["id"]: s for s in PersonaManager.get_instance().list_profiles_summary()}
        assert rows["newbie"]["has_voice"] is False and rows["newbie"]["voice_mode"] == "off"


@pytest.mark.asyncio
async def test_put_existing_persona_without_voice_keeps_legacy_contract(app_on):
    """存量人设（陈默）表单空语音段照旧落库：不注入 off、不报错（沿用全局＝预置态标注）。"""
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS, json=_body({"backend": "", "voice": ""}, "陈默"))
        assert r.status_code == 200 and r.json()["voice_warnings"] == []
        p = (await c.get("/api/personas/profiles/chen_mo", headers=_HDRS)).json()["persona"]
        assert p["voice_profile"] == {"backend": "", "voice": ""}


@pytest.mark.asyncio
async def test_put_legacy_invalid_profile_prompts_fix_even_when_form_untouched(app_on):
    """Mizuki 存量档已在库里：只改名字、没碰语音 → 仍 400 提示修正（不静默改）。"""
    pm = PersonaManager.get_instance()
    pm.upsert_profile("mizuki", {"id": "mizuki", "name": "Mizuki", "voice_profile": dict(_MIZUKI_VP)},
                      _track_history=False)
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/mizuki", headers=_HDRS,
                        json={"persona": {"name": "Mizuki·改名"}, "merge": True})
        assert r.status_code == 400
        assert pm.get_persona_by_id("mizuki")["name"] == "Mizuki", "拒绝保存＝库里原样"
        # 修正路径 A：改选预置声（三态 preset + 目录内音色）→ 200
        r = await c.put("/api/personas/profiles/mizuki", headers=_HDRS,
                        json=_body({"voice_mode": "preset", "backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}, "Mizuki"))
        assert r.status_code == 200
        vp = pm.get_persona_by_id("mizuki")["voice_profile"]
        assert vp["voice_mode"] == "preset" and vp["backend"] == "edge_tts" and vp["voice"] == "ja-JP-NanamiNeural"


@pytest.mark.asyncio
async def test_put_preset_unknown_voice_rejected(app_on):
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS,
                        json=_body({"voice_mode": "preset", "backend": "edge_tts", "voice": "steven"}, "陈默"))
        assert r.status_code == 400
        assert [p["code"] for p in r.json()["voice_problems"]] == ["preset_voice_unknown"]


@pytest.mark.asyncio
async def test_put_explicit_clone_clears_stray_voice_and_warns_missing_file(app_on):
    """登记齐备但残留预置声名：重新点选克隆 → voice 清空、200，参考音本机不在只作 warning。"""
    pm = PersonaManager.get_instance()
    pm.upsert_profile("steven", {"id": "steven", "name": "steven",
                                 "voice_profile": _enrolled_vp(voice="ja-JP-NanamiNeural")},
                      _track_history=False)
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/steven", headers=_HDRS,
                        json=_body({"voice_mode": "clone", "backend": "avatar_clone", "voice": "", "enabled": True}, "steven"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert [w["code"] for w in body["voice_warnings"]] == ["clone_reference_file_missing"]
        assert body["voice_warnings"][0]["message"]
        vp = pm.get_persona_by_id("steven")["voice_profile"]
        assert vp["voice"] == "" and vp["voice_mode"] == "clone" and vp["backend"] == "avatar_clone"
        assert vp["reference_audio_path"] == "voice_samples/steven.wav", "登记键不受影响"


@pytest.mark.asyncio
async def test_preset_catalog_endpoint(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/voice/preset-catalog", headers=_HDRS)
        assert r.status_code == 200
        d = r.json()
        ids = {v["id"] for v in d["edge_voices"]}
        assert {"zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "ja-JP-KeitaNeural"} <= ids
        assert any(l["code"] == "ja" for l in d["languages"])
        assert all(v["gender"] in ("female", "male") for v in d["edge_voices"])
