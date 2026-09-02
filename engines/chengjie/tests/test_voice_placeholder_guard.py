# -*- coding: utf-8 -*-
"""占位串音色（"___"）事故回归网（#93/#137/#140，2026-09-02 钧机诊断包）。

事故链：人设工作室把半成品语音设置以 ``voice: "___"`` 落库 → 人设层 merge
覆盖全局克隆档 → lang_voice_route 把占位串当「语种不匹配的音色」→ 每条语音
「文本语种 zh ≠ 音色 ___ → 切 edge_tts」，克隆登记明明全绿、对端却全是
edge 通用声。三层修复各有回归：

1. 路由层：占位/空音色改判「音色未配置」→ 回查人设/全局克隆档，有克隆走克隆；
2. 读取层：persona_voice 占位串视同未设置（人设半成品档不再覆盖全局克隆档）；
3. 写入层：normalize_profile_shape 保存时剥离占位串（拒绝落库）。

验收口径：中英文文本 + 克隆档在位时，路由日志不得再出现「切 edge_tts」。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import pytest

from src.ai.lang_voice_route import (
    is_reject_tag,
    is_voice_placeholder,
    route_voice_cfg_for_text,
)
from src.ai.persona_voice import resolve_voice_cfg

_ROUTE_ON = {"voice_lang_route": {"enabled": True}}

_GLOBAL_CLONE_VP: Dict[str, Any] = {
    "enabled": True,
    "owner_consent": True,
    "backend": "avatar_clone",
    "speaker_id": "global_voice",
    "reference_audio_path": "config/voice_refs/global.wav",
}


def _cfg_with_global_clone(**extra) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "voice_lang_route": {"enabled": True},
        "telegram": {"voice_reply": {
            "backend": "avatar_clone",
            "voice": "global_voice",
            "voice_profile": dict(_GLOBAL_CLONE_VP),
        }},
    }
    cfg.update(extra)
    return cfg


# ══ 1. 占位串判定（SSOT） ═══════════════════════════════════════════════════


def test_is_voice_placeholder():
    assert is_voice_placeholder("___")
    assert is_voice_placeholder("--")
    assert is_voice_placeholder("…")
    assert is_voice_placeholder("　___　")     # 全角空白包裹也认
    # 空串不算占位（＝本来就未设置，语义由调用方处理）
    assert not is_voice_placeholder("")
    assert not is_voice_placeholder("   ")
    # 真实音色/登记名/哨兵绝不误伤
    assert not is_voice_placeholder("zh-CN-XiaoxiaoNeural")
    assert not is_voice_placeholder("steven")
    assert not is_voice_placeholder("__system__")
    assert not is_voice_placeholder("my_voice_01")


# ══ 2. 路由层：音色未配置 → 回查克隆档 ═══════════════════════════════════════


def test_placeholder_voice_recovers_global_clone(caplog):
    """钧机形态：edge 链音色 "___" + 全局克隆档在位 → 恢复克隆主链。"""
    vc = {"backend": "edge_tts", "voice": "___"}
    with caplog.at_level(logging.INFO, logger="src.ai.lang_voice_route"):
        out, tag = route_voice_cfg_for_text(
            vc, "你好呀，今天过得怎么样？", _cfg_with_global_clone())
    assert tag == ""
    assert out["backend"] == "avatar_clone"
    vp = out["voice_profile"]
    assert vp["enabled"] is True
    assert vp["speaker_id"] == "global_voice"
    assert out.get("voice") != "___", "占位串不得残留为生效音色"
    # 克隆失败回落 edge 时兜底音色也对齐文本语种
    assert out["fallback_voice"] == "zh-CN-XiaoxiaoNeural"
    # 验收口径：不得再打「切 edge_tts」
    assert all("切 edge_tts" not in r.getMessage() for r in caplog.records)
    assert vc == {"backend": "edge_tts", "voice": "___"}, "原配置不可被就地修改"


def test_placeholder_voice_recovers_clone_for_english_text():
    """英文文本同样走克隆（克隆链原生跟随文本语种），兜底对齐 en。"""
    vc = {"backend": "edge_tts", "voice": "___"}
    out, tag = route_voice_cfg_for_text(
        vc, "Hello there, how are you doing today my friend?",
        _cfg_with_global_clone())
    assert tag == ""
    assert out["backend"] == "avatar_clone"
    assert out["fallback_voice"] == "en-US-JennyNeural"


def test_placeholder_voice_recovers_from_merged_profile():
    """人设层克隆档 enabled 键缺失（#93 形态）也能回查命中。"""
    vp = dict(_GLOBAL_CLONE_VP)
    vp.pop("enabled")
    vc = {"backend": "edge_tts", "voice": "___", "voice_profile": vp}
    out, tag = route_voice_cfg_for_text(vc, "你好呀，今天过得怎么样？", _ROUTE_ON)
    assert tag == ""
    assert out["backend"] == "avatar_clone"
    assert out["voice_profile"]["enabled"] is True


def test_placeholder_voice_no_clone_falls_to_lang_voice(caplog):
    """确实没有克隆档：按语种取 edge 音色，日志说「未配置」而非「不匹配」。"""
    vc = {"backend": "edge_tts", "voice": "___"}
    with caplog.at_level(logging.INFO, logger="src.ai.lang_voice_route"):
        out, tag = route_voice_cfg_for_text(vc, "你好呀，今天过得怎么样？", _ROUTE_ON)
    assert tag == "zh"
    assert out["backend"] == "edge_tts"
    assert out["voice"] == "zh-CN-XiaoxiaoNeural"
    assert all("切 edge_tts" not in r.getMessage() for r in caplog.records)
    assert any("音色未配置" in r.getMessage() for r in caplog.records)


def test_placeholder_voice_disabled_clone_not_resurrected():
    """enabled 显式 False＝运营手动停用，绝不复活——按未配置走 edge 语种音色。"""
    cfg = _cfg_with_global_clone()
    cfg["telegram"]["voice_reply"]["voice_profile"]["enabled"] = False
    vc = {"backend": "edge_tts", "voice": "___"}
    out, tag = route_voice_cfg_for_text(vc, "你好呀，今天过得怎么样？", cfg)
    assert tag == "zh"
    assert out["backend"] == "edge_tts"
    assert out["voice"] == "zh-CN-XiaoxiaoNeural"


def test_placeholder_speaker_only_clone_not_usable():
    """克隆档自身的 speaker_id 也是占位串且无参考音 → 不算可用克隆档。"""
    cfg = _cfg_with_global_clone()
    cfg["telegram"]["voice_reply"]["voice_profile"].update(
        {"speaker_id": "___", "reference_audio_path": ""})
    vc = {"backend": "edge_tts", "voice": "___"}
    out, tag = route_voice_cfg_for_text(vc, "你好呀，今天过得怎么样？", cfg)
    assert tag == "zh"
    assert out["backend"] == "edge_tts"


def test_valid_mismatch_voice_still_routes_to_edge():
    """真实但语种不匹配的音色：既有「切 edge_tts」行为分毫不动（回归守卫）。"""
    vc = {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，今天过得怎么样？", _cfg_with_global_clone())
    assert tag == "zh"
    assert out["voice"] == "zh-CN-XiaoxiaoNeural"
    assert not is_reject_tag(tag)


# ══ 3. 读取层：persona_voice 占位串视同未设置 ═══════════════════════════════


class _EmptyPM:
    def get_persona_by_id(self, pid):
        return None


@pytest.fixture()
def empty_pm(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    monkeypatch.setattr(
        PersonaManager, "get_instance", classmethod(lambda cls: _EmptyPM()))


def _full_cfg_with_persona(persona_vp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "telegram": {"voice_reply": {
            "backend": "avatar_clone",
            "voice": "global_voice",
            "voice_profile": dict(_GLOBAL_CLONE_VP),
        }},
        "personas": {"profiles": [
            {"id": "p1", "name": "小艾", "voice_profile": persona_vp},
        ]},
    }


def test_resolve_voice_cfg_persona_placeholder_does_not_shadow_global(empty_pm):
    """人设档只有占位串 → 视同未设置，全局克隆档原样生效（钧机根因②）。"""
    merged = resolve_voice_cfg("p1", _full_cfg_with_persona({"voice": "___"}))
    assert merged.get("voice") == "global_voice", "占位串不得覆盖全局音色"
    vp = merged.get("voice_profile") or {}
    assert vp.get("speaker_id") == "global_voice"
    assert vp.get("enabled") is True
    assert merged.get("voice_source_layer") == "global", \
        "被剥空的人设档不算贡献了音色配置"


def test_resolve_voice_cfg_persona_real_profile_still_wins(empty_pm):
    """真实人设档照旧压过全局（占位串豁免不改既有优先级）。"""
    merged = resolve_voice_cfg("p1", _full_cfg_with_persona({
        "enabled": True, "owner_consent": True, "backend": "avatar_clone",
        "speaker_id": "p1_voice",
        "reference_audio_path": "config/voice_refs/p1.wav",
    }))
    assert (merged.get("voice_profile") or {}).get("speaker_id") == "p1_voice"
    assert merged.get("voice_source_layer") == "persona:p1"


def test_resolve_voice_cfg_global_placeholder_voice_dropped(empty_pm):
    """全局层 voice 本身是占位串 → 顶层收尾清洗后不出现在结果里。"""
    cfg = {"telegram": {"voice_reply": {"backend": "edge_tts", "voice": "___"}}}
    merged = resolve_voice_cfg(None, cfg)
    assert "voice" not in merged


# ══ 4. 写入层：normalize_profile_shape 拒绝占位串落库 ════════════════════════


def test_normalize_profile_shape_strips_placeholder_keys():
    from src.utils.persona_manager import PersonaManager
    out = PersonaManager.normalize_profile_shape({
        "id": "p1",
        "voice_profile": {
            "voice": "___", "backend": "avatar_clone",
            "speaker_id": "s1", "reference_audio_path": "—",
        },
    })
    vp = out["voice_profile"]
    assert "voice" not in vp
    assert "reference_audio_path" not in vp
    assert vp["backend"] == "avatar_clone"
    assert vp["speaker_id"] == "s1"


def test_normalize_profile_shape_clean_profile_untouched():
    from src.utils.persona_manager import PersonaManager
    src = {"id": "p1", "voice_profile": dict(_GLOBAL_CLONE_VP)}
    out = PersonaManager.normalize_profile_shape(src)
    assert out["voice_profile"] == _GLOBAL_CLONE_VP


# ══ 5. TTSPipeline：init 占位音色视同未设置 + 强制 INFO ══════════════════════


def test_tts_pipeline_placeholder_voice_falls_to_default():
    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({"backend": "edge_tts", "voice": "___",
                       "fallback_voice": "---"})
    assert tts.voice == "zh-CN-XiaoxiaoNeural"
    assert tts.fallback_voice == "zh-CN-XiaoxiaoNeural"


def test_tts_pipeline_voice_source_stored():
    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({"backend": "edge_tts", "voice_source": "人设p1"})
    assert tts.voice_source == "人设p1"


@pytest.mark.asyncio
async def test_tts_pipeline_synth_logs_effective_source(caplog, tmp_path):
    """每次合成必打一行「合成生效 backend/voice/来源」INFO（强制观测）。"""
    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({
        "enabled": True, "backend": "pyttsx3", "voice": "v1",
        "voice_source": "人设p1", "out_dir": str(tmp_path),
        "fallback_on_error": False, "tts_cache": {"enabled": False},
    })
    with caplog.at_level(logging.INFO, logger="src.ai.tts_pipeline"):
        await tts.synthesize("你好，测试一句。", timeout_sec=1.0)
    hits = [r.getMessage() for r in caplog.records if "合成生效" in r.getMessage()]
    assert hits, "合成路径必须打「合成生效」INFO"
    assert "人设p1" in hits[0] and "pyttsx3" in hits[0]
