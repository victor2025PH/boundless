# -*- coding: utf-8 -*-
"""克隆档「登记后挂载」断链回归网（#149，2026-09-02 钧机 KKXSTU/SY8TGE）。

事故链：人设工作室抽屉打开（语音 backend 下拉=「继承全局」）→ 同抽屉里完成克隆
登记（服务端写入 avatar_clone 档，试听正确）→ 点「保存」→ 表单 ``voice_profile:
{backend:"", voice:""}`` deep-merge 把 backend 顶空 → 克隆声静默卸载：启动统计
「13 个人设 12 个配了克隆 backend」、工具箱下拉无 🎤、合成回落非克隆声。

三层修复各有回归：
1. 纯函数 ``voice_profile_guard``：空值不覆写已登记克隆档；按登记来源自愈 backend；
2. 写/载边界 ``normalize_profile_shape``：存量坏档装载即修（钧机不必重新登记）；
3. 路由 ``PUT /api/personas/profiles/{id}``（merge）：抽屉保存后克隆档仍在、
   ``/api/voice/profiles`` 仍标 is_clone/🎤；显式换后端仍放行。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.ai.voice_profile_guard import (
    CLONE_BACKENDS,
    heal_clone_profile,
    infer_clone_backend,
    is_registered_clone_profile,
    protect_registered_clone_patch,
    protect_registered_clone_persona_patch,
)
from src.utils.persona_manager import PersonaManager


def _enrolled_vp() -> Dict[str, Any]:
    """voice_enroll.build_avatar_voice_profile 的真实产物形状。"""
    return {
        "enabled": True, "owner_consent": True, "backend": "avatar_clone",
        "source": "avatar_zeroshot", "speaker_id": "natalie",
        "voice": "", "reference_audio_path": "voice_samples/natalie.wav",
        "emotion_default": "gentle",
        "source_ref": {"kind": "upload", "imported_by": "admin"},
    }


# 抽屉「保存」的语音段形状（personas.html 表单只知道下拉里的 backend/voice）
_FORM_VP = {"backend": "", "voice": ""}


# ══ 1. 纯函数 ═══════════════════════════════════════════════════════════════

def test_infer_backend_from_enroll_traces():
    assert infer_clone_backend({"source": "avatar_zeroshot"}) == "avatar_clone"
    assert infer_clone_backend({"source": "lan_zeroshot"}) == "voice_clone_lan"
    assert infer_clone_backend({"command_args": ["python", "x.py"]}) == "voice_clone_command"
    assert infer_clone_backend({"voice_profile_json_path": "voice_samples/qwen_x.json"}) == "voice_clone_command"
    assert infer_clone_backend({"voice": "zh-CN-XiaoxiaoNeural"}) == ""
    assert infer_clone_backend("junk") == ""


def test_is_registered_clone_profile():
    assert is_registered_clone_profile(_enrolled_vp())
    broken = dict(_enrolled_vp(), backend="")         # 被抽屉保存打坏
    assert is_registered_clone_profile(broken)        # 登记痕迹仍认得出
    assert not is_registered_clone_profile(dict(_enrolled_vp(), enabled=False))
    assert not is_registered_clone_profile({"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"})
    assert not is_registered_clone_profile({"backend": "avatar_clone"})   # 无参考音/speaker
    assert not is_registered_clone_profile({})
    assert not is_registered_clone_profile(None)


def test_protect_patch_drops_empty_identity_keys_only():
    new_vp, preserved = protect_registered_clone_patch(_enrolled_vp(), dict(_FORM_VP))
    assert "backend" not in new_vp
    assert preserved == ["backend"], preserved      # voice 两边都空：不计
    # 显式换后端＝人的决定，放行
    new_vp2, preserved2 = protect_registered_clone_patch(
        _enrolled_vp(), {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"})
    assert new_vp2 == {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    assert preserved2 == []
    # 显式停用放行；None 视同空
    new_vp3, preserved3 = protect_registered_clone_patch(
        _enrolled_vp(), {"enabled": False, "owner_consent": None})
    assert new_vp3 == {"enabled": False}
    assert preserved3 == ["owner_consent"]
    # 非克隆档：补丁原样
    plain = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    assert protect_registered_clone_patch(plain, dict(_FORM_VP)) == (_FORM_VP, [])
    assert protect_registered_clone_patch(None, dict(_FORM_VP)) == (_FORM_VP, [])


def test_protect_persona_patch_wrapper():
    existing = {"id": "p", "name": "娜塔莉", "voice_profile": _enrolled_vp()}
    patch = {"name": "娜塔莉·海斯", "voice_profile": dict(_FORM_VP)}
    snap = copy.deepcopy(patch)
    out, preserved = protect_registered_clone_persona_patch(existing, patch)
    assert preserved == ["backend"]
    assert out["name"] == "娜塔莉·海斯"
    assert "backend" not in out["voice_profile"]
    assert patch == snap, "不就地修改入参"
    # 补丁没有 voice_profile → 原样
    assert protect_registered_clone_persona_patch(existing, {"name": "x"}) == ({"name": "x"}, [])


def test_heal_restores_backend_and_enabled_from_traces():
    broken = dict(_enrolled_vp(), backend="")
    healed, fixed = heal_clone_profile(broken)
    assert fixed == ["backend"] and healed["backend"] == "avatar_clone"
    assert broken["backend"] == "", "不就地修改入参"
    no_enabled = dict(_enrolled_vp())
    no_enabled.pop("enabled")
    no_enabled["backend"] = ""
    healed2, fixed2 = heal_clone_profile(no_enabled)
    assert fixed2 == ["backend", "enabled"]
    assert healed2["backend"] == "avatar_clone" and healed2["enabled"] is True
    # 显式停用不复活；健康档原样返回（同一对象）
    disabled = dict(_enrolled_vp(), backend="", enabled=False)
    assert heal_clone_profile(disabled) == (disabled, [])
    good = _enrolled_vp()
    assert heal_clone_profile(good)[0] is good
    # 没有登记痕迹的空 backend 不猜
    assert heal_clone_profile({"backend": "", "voice": "", "enabled": True}) == (
        {"backend": "", "voice": "", "enabled": True}, [])


# ══ 2. 写/载边界自愈 ═════════════════════════════════════════════════════════

def test_normalize_profile_shape_heals_damaged_clone(caplog):
    damaged = {"id": "RAEW235", "name": "娜塔莉·海斯",
               "voice_profile": dict(_enrolled_vp(), backend="")}
    with caplog.at_level(logging.WARNING, logger="src.utils.persona_manager"):
        out = PersonaManager.normalize_profile_shape(damaged)
    assert out["voice_profile"]["backend"] == "avatar_clone"
    assert any("#149" in r.getMessage() and "RAEW235" in r.getMessage()
               for r in caplog.records)
    # 健康档不打日志、不改
    caplog.clear()
    healthy = {"id": "ok", "voice_profile": _enrolled_vp()}
    out2 = PersonaManager.normalize_profile_shape(healthy)
    assert out2["voice_profile"] == _enrolled_vp()
    assert not caplog.records


def test_runtime_load_heals_and_counts_as_clone_backend(tmp_path):
    """启动装载 profiles_runtime.yaml：坏档修好 → persona_routes 那句「N 个配了克隆
    backend」的判据（voice_profile.backend ∈ 克隆集）对它成立。"""
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        rt = tmp_path / "profiles_runtime.yaml"
        rt.write_text(yaml.safe_dump({"profiles": {
            "RAEW235": {"id": "RAEW235", "name": "娜塔莉·海斯",
                        "voice_profile": dict(_enrolled_vp(), backend="")},
            "plain": {"id": "plain", "name": "无声"},
        }}, allow_unicode=True), encoding="utf-8")
        n = pm.load_profiles_runtime(tmp_path / "config.yaml",
                                     {"persona_persistence": {"profiles_path": str(rt)}})
        assert n == 2
        healed = pm.get_persona_by_id("RAEW235")
        assert healed["voice_profile"]["backend"] == "avatar_clone"
        clone_n = sum(
            1 for p in pm._profile_personas.values()
            if str(((p or {}).get("voice_profile") or {}).get("backend") or "")
            .strip().lower() in CLONE_BACKENDS)
        assert clone_n == 1
        summary = {s["id"]: s for s in pm.list_profiles_summary()}
        assert summary["RAEW235"]["has_voice"] is True
    finally:
        PersonaManager.reset()


# ══ 3. 路由级：抽屉保存不卸载克隆声 ═══════════════════════════════════════════

_HDRS = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}


@pytest.fixture
def app_on(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1",
                     "voice_reply": {"backend": "edge_tts",
                                     "voice": "zh-CN-XiaoxiaoNeural"}},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [{"id": "chen_mo", "name": "陈默", "role": "陪伴"}]},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
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


def _drawer_save(name: str = "娜塔莉·海斯") -> Dict[str, Any]:
    """personas.html 抽屉「保存」的请求体（merge=true，语音段只有下拉/输入框两键）。"""
    return {"persona": {
        "name": name, "role": "线上陪伴",
        "personality": {"style": "温柔"}, "tags": ["a", "b", "c"],
        "speaking": {"reply_length": "concise", "emoji_level": "moderate",
                     "forbidden_phrases": [], "openers": []},
        "identity": {"deny_ai": False, "claim_human": False},
        "voice_profile": dict(_FORM_VP),
    }, "merge": True}


@pytest.mark.asyncio
async def test_drawer_save_after_enroll_keeps_clone_mounted(app_on):
    """钧机事故复现：登记成功 → 抽屉保存 → 克隆档仍在、工具箱仍 🎤、is_clone 仍真。"""
    pm = PersonaManager.get_instance()
    pm.upsert_profile("RAEW235", {"id": "RAEW235", "name": "娜塔莉",
                                  "voice_profile": _enrolled_vp()}, _track_history=False)
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/RAEW235", headers=_HDRS, json=_drawer_save())
        assert r.status_code == 200
        body = r.json()
        assert body["merged"] is True
        assert body["voice_profile_preserved"] == ["backend"]

        r = await c.get("/api/personas/profiles/RAEW235", headers=_HDRS)
        p = r.json()["persona"]
        assert p["name"] == "娜塔莉·海斯"                       # 表单其余字段照常更新
        vp = p["voice_profile"]
        assert vp["backend"] == "avatar_clone"
        assert vp["enabled"] is True and vp["owner_consent"] is True
        assert vp["reference_audio_path"] == "voice_samples/natalie.wav"

        r = await c.get("/api/voice/profiles", headers=_HDRS)
        rows = {x["persona_id"]: x for x in r.json()["profiles"]}
        assert "RAEW235" in rows, "工具箱下拉必须能找到新克隆的人设"
        assert rows["RAEW235"]["is_clone"] is True and rows["RAEW235"]["ready"] is True


@pytest.mark.asyncio
async def test_drawer_save_explicit_backend_change_still_applies(app_on):
    """显式把 backend 换成 edge_tts（抽屉里有 confirm）＝人的决定，守卫不拦。"""
    pm = PersonaManager.get_instance()
    pm.upsert_profile("RAEW235", {"id": "RAEW235", "name": "娜塔莉",
                                  "voice_profile": _enrolled_vp()}, _track_history=False)
    body = _drawer_save()
    body["persona"]["voice_profile"] = {"backend": "edge_tts", "voice": "zh-CN-XiaoyiNeural"}
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/RAEW235", headers=_HDRS, json=body)
        assert r.status_code == 200
        assert r.json()["voice_profile_preserved"] == []
        r = await c.get("/api/personas/profiles/RAEW235", headers=_HDRS)
        vp = r.json()["persona"]["voice_profile"]
        assert vp["backend"] == "edge_tts" and vp["voice"] == "zh-CN-XiaoyiNeural"


@pytest.mark.asyncio
async def test_drawer_save_on_plain_persona_unchanged_contract(app_on):
    """无克隆登记的人设：表单空语音段照旧落库（既有 merge 契约零变化）。"""
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS, json=_drawer_save("陈默"))
        assert r.status_code == 200
        assert r.json()["voice_profile_preserved"] == []
        r = await c.get("/api/personas/profiles/chen_mo", headers=_HDRS)
        assert r.json()["persona"]["voice_profile"] == _FORM_VP


@pytest.mark.asyncio
async def test_damaged_profile_in_store_is_healed_on_save(app_on):
    """存量坏档（backend 已被顶空）：upsert 经 normalize 自愈，再保存也不再丢。"""
    pm = PersonaManager.get_instance()
    pm.upsert_profile("RAEW235", {"id": "RAEW235", "name": "娜塔莉",
                                  "voice_profile": dict(_enrolled_vp(), backend="")},
                      _track_history=False)
    assert pm.get_persona_by_id("RAEW235")["voice_profile"]["backend"] == "avatar_clone"
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/RAEW235", headers=_HDRS, json=_drawer_save())
        assert r.status_code == 200
        r = await c.get("/api/personas/profiles/RAEW235", headers=_HDRS)
        assert r.json()["persona"]["voice_profile"]["backend"] == "avatar_clone"
