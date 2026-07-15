"""阶段 B 单测：B1 克隆语音多端点路由 / B2 出站质量管道 / B3 人设资产 lint。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ── B1 多端点路由 ────────────────────────────────────────────────────────────
def _mk_client(bases, **extra):
    from src.ai.avatar_voice import AvatarVoiceClient, reset_caches
    reset_caches()
    cfg = {"enabled": True, "base_urls": bases,
           "retries": 0, "health_cache_sec": 999, **extra}
    return AvatarVoiceClient(cfg)


def test_b1_single_endpoint_backcompat():
    from src.ai.avatar_voice import AvatarVoiceClient, reset_caches
    reset_caches()
    c = AvatarVoiceClient({"enabled": True, "base_url": "http://127.0.0.1:7852"})
    assert c.base_urls == ["http://127.0.0.1:7852"]
    assert c.base_url == "http://127.0.0.1:7852"


def test_b1_base_url_merged_into_list():
    c = _mk_client(["http://192.168.0.176:7852"], base_url="http://127.0.0.1:7852")
    assert c.base_urls == ["http://192.168.0.176:7852", "http://127.0.0.1:7852"]
    assert c.base_url == "http://192.168.0.176:7852"   # 主端点=首个


def test_b1_failover_marks_bad_and_uses_next(monkeypatch):
    """主端点合成失败 → 冷却 + 自动切备端点；下一次直接跳过冷却中的主端点。"""
    c = _mk_client(["http://a:7852", "http://b:7852"])
    # 两端点都视为健康（跳过 3s 探测）
    monkeypatch.setattr(c, "_health_ok_base", lambda b, **k: True)
    calls = []

    def fake_post(url, payload, *, timeout, headers=None):
        calls.append(url)
        if url.startswith("http://a:"):
            raise RuntimeError("host a down")
        return b"WAVDATA"

    monkeypatch.setattr(c, "_post", fake_post)
    out = c._post_any("/v1/tts/clone", b"{}", timeout=5)
    assert out == b"WAVDATA"
    assert calls == ["http://a:7852/v1/tts/clone", "http://b:7852/v1/tts/clone"]
    assert c._endpoint_cooling("http://a:7852")      # a 进冷却
    calls.clear()
    out2 = c._post_any("/v1/tts/clone", b"{}", timeout=5)
    assert out2 == b"WAVDATA"
    assert calls == ["http://b:7852/v1/tts/clone"]   # 冷却期直接走 b


def test_b1_all_cooling_falls_back_to_primary(monkeypatch):
    c = _mk_client(["http://a:7852", "http://b:7852"])
    monkeypatch.setattr(c, "_health_ok_base", lambda b, **k: True)
    c._note_endpoint_bad("http://a:7852")
    c._note_endpoint_bad("http://b:7852")
    assert c._endpoint_candidates() == ["http://a:7852"]   # 全冷却 → 回退主端点


def test_b1_unhealthy_endpoint_skipped(monkeypatch):
    c = _mk_client(["http://a:7852", "http://b:7852"])
    monkeypatch.setattr(
        c, "_health_ok_base", lambda b, **k: not b.startswith("http://a"))
    assert c._endpoint_candidates() == ["http://b:7852"]


def test_b1_health_ok_any_endpoint(monkeypatch):
    c = _mk_client(["http://a:7852", "http://b:7852"])
    monkeypatch.setattr(
        c, "_health_ok_base", lambda b, **k: b.startswith("http://b"))
    assert c.health_ok() is True                     # 任一端点健康即可用


def test_b1_gpu_lock_per_host():
    from src.ai.avatar_voice import _gpu_lock_for
    assert _gpu_lock_for("http://127.0.0.1:7852") is _gpu_lock_for(
        "http://localhost:7858")                     # 本机 7852/7858 同卡同锁
    assert _gpu_lock_for("http://192.168.0.176:7852") is not _gpu_lock_for(
        "http://127.0.0.1:7852")                     # 远端主机独立锁


# ── B2 自称改写 ──────────────────────────────────────────────────────────────
def test_b2_third_person_rewritten():
    from src.ai.outbound_quality import sanitize_self_reference
    out, n = sanitize_self_reference(
        "林小雨现在不太方便拍照呢，不过我一直在这儿陪你～", "林小雨")
    assert n == 1
    assert out.startswith("我现在不太方便拍照")
    out2, n2 = sanitize_self_reference("喜欢林小雨吗？", "林小雨")
    assert n2 == 1 and out2 == "喜欢我吗？"


def test_b2_identity_statement_protected():
    from src.ai.outbound_quality import sanitize_self_reference
    for t in ("哈哈我是林小雨呀", "我叫林小雨，你呢", "叫我林小雨就好啦"):
        out, n = sanitize_self_reference(t, "林小雨")
        assert out == t and n == 0


def test_b2_name_followed_by_wo_dedupes():
    from src.ai.outbound_quality import sanitize_self_reference
    out, n = sanitize_self_reference("林小雨我跟你说哦", "林小雨")
    assert out == "我跟你说哦" and n >= 1


def test_b2_no_name_zero_cost():
    from src.ai.outbound_quality import sanitize_self_reference
    out, n = sanitize_self_reference("今天天气不错", "林小雨")
    assert out == "今天天气不错" and n == 0
    out2, n2 = sanitize_self_reference("随便说说", "")
    assert out2 == "随便说说" and n2 == 0


# ── B2 复读检测 ──────────────────────────────────────────────────────────────
def test_b2_repeat_guard_detects_exact_repeat():
    from src.ai.outbound_quality import OutboundRecentGuard
    g = OutboundRecentGuard(per_chat=5)
    assert g.note_and_check(1, "这会儿不太方便拍照呢～") is False
    # emoji/标点修饰不同也算复读（归一化比对）
    assert g.note_and_check(1, "这会儿不太方便拍照呢 ✨") is True
    assert g.note_and_check(2, "这会儿不太方便拍照呢～") is False   # 跨会话不误报


def test_b2_pipeline_pass_end_to_end():
    from src.ai.outbound_quality import outbound_quality_pass, reset_outbound_guard
    reset_outbound_guard()
    out = outbound_quality_pass(
        "林小雨今天有点忙哦", chat_id=9, persona_name="林小雨")
    assert out == "我今天有点忙哦"
    # 空名/异常输入不炸、原样返回
    assert outbound_quality_pass("hello", chat_id=9, persona_name="") == "hello"
    assert outbound_quality_pass(None, chat_id=9, persona_name="x") == ""


# ── B3 人设资产 lint ─────────────────────────────────────────────────────────
def _profiles(**overrides):
    base = {
        "girl": {
            "id": "girl", "name": "小雨", "age": 22, "gender": "female",
            "appearance": "a 22-year-old East Asian college girl",
            "selfie_scenes": ["cozy dorm room, warm lamp light"],
            "voice_profile": {"enabled": True,
                              "reference_audio_path": "refs/girl.wav"},
        },
    }
    base.update(overrides)
    return base


def test_b3_clean_profile_passes():
    from src.companion.persona_asset_lint import lint_personas
    issues = lint_personas(
        _profiles(),
        file_exists=lambda p: True)
    assert issues == []


def test_b3_missing_reference_audio_is_error():
    from src.companion.persona_asset_lint import lint_personas
    issues = lint_personas(_profiles(), file_exists=lambda p: p.endswith(".txt"))
    assert any(i["check"] == "reference_audio" and i["severity"] == "error"
               for i in issues)


def test_b3_cross_gender_shared_ref_is_error():
    from src.companion.persona_asset_lint import lint_personas
    profs = _profiles()
    profs["man"] = {
        "id": "man", "name": "Marcus", "age": 42, "gender": "male",
        "voice_profile": {"enabled": True,
                          "reference_audio_path": "refs/girl.wav"},
    }
    issues = lint_personas(profs, file_exists=lambda p: True)
    shared = [i for i in issues if i["check"] == "reference_audio_shared"]
    assert shared and all(i["severity"] == "error" for i in shared)
    assert {i["persona"] for i in shared} == {"girl", "man"}


def test_b3_same_gender_shared_ref_is_warn():
    from src.companion.persona_asset_lint import lint_personas
    profs = _profiles()
    profs["girl2"] = {
        "id": "girl2", "name": "美玲", "age": 35, "gender": "female",
        "voice_profile": {"enabled": True,
                          "reference_audio_path": "refs/girl.wav"},
    }
    issues = lint_personas(profs, file_exists=lambda p: True)
    shared = [i for i in issues if i["check"] == "reference_audio_shared"]
    assert shared and all(i["severity"] == "warn" for i in shared)


def test_b3_appearance_missing_and_age_drift():
    from src.companion.persona_asset_lint import lint_personas
    profs = _profiles()
    profs["girl"]["appearance"] = ""
    issues = lint_personas(profs, file_exists=lambda p: True)
    assert any(i["check"] == "appearance" and i["severity"] == "error"
               for i in issues)
    profs["girl"]["appearance"] = "a 19-year-old girl"
    issues2 = lint_personas(profs, file_exists=lambda p: True)
    assert any(i["check"] == "appearance_age" for i in issues2)


def test_b3_scene_bucket_all_conflicting_warns():
    from src.companion.persona_asset_lint import lint_personas
    profs = _profiles()
    profs["girl"]["selfie_scenes"] = [
        "campus at noon, midday light", "sunny day park at noon"]
    issues = lint_personas(profs, file_exists=lambda p: True)
    assert any(i["check"] == "scene_pool_bucket" for i in issues)   # 深夜桶全冲突


def test_b3_report_format():
    from src.companion.persona_asset_lint import format_report, lint_personas
    assert "全部通过" in format_report([])
    profs = _profiles()
    profs["girl"]["voice_profile"]["reference_audio_path"] = ""
    rep = format_report(lint_personas(profs, file_exists=lambda p: True))
    assert "error" in rep and "girl" in rep
