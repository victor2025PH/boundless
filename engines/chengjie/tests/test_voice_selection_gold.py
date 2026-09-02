# -*- coding: utf-8 -*-
"""选声金标（#149，2026-09-02 钧机 KKXSTU 截图/日志复盘）：

    工具箱选了人设 X → 合成日志「来源」必须等于 X。

事故面：截图下拉「美月·伊莎贝拉」、预览「实际使用 人设 张景光」、20:29:49 合成
INFO ``来源=人设zhang_jingguang``——「选 X 出 Y」是 P0 级信任破坏。本文件把
「显式选择 → 解析 → 合成 INFO 行」整条链钉死：

1. 显式选 X 时，会话覆写/账号人设都绑着 Y 也不许改变结果（X 的参考音、X 的来源标签）；
2. TTSPipeline 的强制观测行 ``合成生效 … 来源=人设X``（下次诊断包的定锚）；
3. 显式 id 不存在 → ``check_voice_selection`` 判 persona_not_found（路由据此拒绝合成，
   旧行为是静默回落全局音色、UI 仍显示所选名字）；
4. 解析结果/来源标签被任何一层偷换 → persona_mismatch / source_mismatch；
5. 未显式选择 → 回落链照旧（会话覆写 Y），守卫不参与；「系统通用音色」哨兵同理。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

import pytest

from src.ai.persona_voice import (
    SYSTEM_VOICE_ID,
    check_voice_selection,
    resolve_effective_voice_context,
)

_A = "mizuki"            # 用户在下拉里选的
_B = "zhang_jingguang"   # 会话覆写 + 账号人设都指向的「别人」


def _clone_vp(pid: str) -> Dict[str, Any]:
    return {
        "enabled": True, "owner_consent": True, "backend": "avatar_clone",
        "source": "avatar_zeroshot", "speaker_id": f"{pid}_spk",
        "reference_audio_path": f"voice_samples/{pid}.wav",
    }


_PROFILES: Dict[str, Dict[str, Any]] = {
    _A: {"id": _A, "name": "美月·伊莎贝拉", "voice_profile": _clone_vp(_A)},
    _B: {"id": _B, "name": "张景光", "voice_profile": _clone_vp(_B),
         "quirks": "爱说「说实话」"},
}

_CFG: Dict[str, Any] = {
    "inbox": {"persona_conv_override": {"enabled": True}},
    "telegram": {"voice_reply": {
        "enabled": True, "backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "reference_audio_path": "config/voice_refs/lin_xiaoyu.wav",
        },
    }},
}

_CONV_KEY = f"line:UJ3W:UIq2ko"   # platform:account:chat（conv_binding_key 同构）


class _FakePM:
    """最小人设库：两个克隆人设 + 会话覆写绑到 B + 账号人设 B。"""

    def __init__(self) -> None:
        self.conv = {_CONV_KEY: _B}

    def get_persona_by_id(self, pid: str):
        return _PROFILES.get(str(pid or ""))

    def get_persona_with_tier(self, chat_id: str = "", account_persona_id: str = "",
                              conversation_key: str = "") -> Tuple[Any, str]:
        if conversation_key and conversation_key in self.conv:
            return _PROFILES[self.conv[conversation_key]], "conv_override"
        if account_persona_id in _PROFILES:
            return _PROFILES[account_persona_id], "account_profile"
        return {}, "default"

    def get_chat_binding_ref(self, key: str) -> str:
        return self.conv.get(key, "")

    def has_chat_binding(self, chat_id: str) -> bool:
        return False


@pytest.fixture()
def fake_pm(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    pm = _FakePM()
    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(lambda cls: pm))
    return pm


def _resolve(persona_id):
    return resolve_effective_voice_context(
        _CFG, persona_id=persona_id, chat_key="UIq2ko", account_persona_id=_B,
        contact_key="UIq2ko", platform="line", account_id="UJ3W",
        text="What are you up to right now? You haven't eaten yet, have you?")


# ══ 1. 显式选 X：会话/账号都绑着 Y 也不许改变结果 ═══════════════════════════════

def test_explicit_selection_wins_over_conv_and_account(fake_pm):
    ctx = _resolve(_A)
    vc = ctx["voice_cfg"]
    assert ctx["persona_id"] == _A
    assert ctx["persona_source"] == "explicit"
    assert vc["voice_source"] == f"人设{_A}", vc.get("voice_source")
    assert vc["voice_source_layer"] == f"persona:{_A}"
    assert vc["voice_profile"]["reference_audio_path"] == f"voice_samples/{_A}.wav"
    assert vc["persona_id"] == _A
    assert check_voice_selection(_A, ctx) == ""


def test_explicit_other_persona_also_exact(fake_pm):
    ctx = _resolve(_B)
    assert ctx["voice_cfg"]["voice_source"] == f"人设{_B}"
    assert ctx["voice_cfg"]["voice_profile"]["speaker_id"] == f"{_B}_spk"
    assert check_voice_selection(_B, ctx) == ""


# ══ 2. 合成强制观测行：来源 == 用户选择 ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_synth_log_source_equals_selection(fake_pm, caplog, tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, TTSResult

    ctx = _resolve(_A)
    cfg = dict(ctx["voice_cfg"])
    cfg.update({"enabled": True, "out_dir": str(tmp_path),
                "fallback_on_error": False, "tts_cache": {"enabled": False}})
    tts = TTSPipeline(cfg)

    async def _stub(self, text_s, **kw):   # 不真合成：只看 INFO 行
        return TTSResult(ok=False, text=text_s, provider="stub", error="stub")

    monkeypatch.setattr(TTSPipeline, "_synthesize_uncached", _stub)
    monkeypatch.setattr(TTSPipeline, "_try_prerendered", lambda self, t: None)
    with caplog.at_level(logging.INFO, logger="src.ai.tts_pipeline"):
        await tts.synthesize("What are you up to right now?", timeout_sec=1.0,
                             pre_colloquialized=True, interactive=True)
    hits = [r.getMessage() for r in caplog.records if "合成生效" in r.getMessage()]
    assert hits, "合成路径必须打「合成生效」INFO"
    assert f"来源=人设{_A}" in hits[0], hits[0]
    assert _B not in hits[0], f"选 {_A} 绝不许出现 {_B}：{hits[0]}"
    assert "backend=avatar_clone" in hits[0]


# ══ 3/4. 守卫：不存在 / 偷换 / 来源标签指认别人 ═══════════════════════════════

def test_missing_persona_is_refused_not_silently_global(fake_pm):
    ctx = _resolve("ghost_persona")
    # 旧行为：resolved_id 仍是显式 id、voice_cfg 落全局音色、UI 照显所选名字
    assert ctx["persona_id"] == "ghost_persona"
    assert ctx["persona"] == {}
    assert ctx["voice_cfg"]["voice_source"] == "全局"
    assert check_voice_selection("ghost_persona", ctx) == "persona_not_found"


def test_guard_catches_swapped_resolution():
    tampered = {"persona_id": _B, "persona": _PROFILES[_B],
                "voice_cfg": {"voice_source": f"人设{_B}",
                              "voice_source_layer": f"persona:{_B}"}}
    assert check_voice_selection(_A, tampered) == "persona_mismatch"


def test_guard_catches_source_label_naming_someone_else():
    tampered = {"persona_id": _A, "persona": _PROFILES[_A],
                "voice_cfg": {"voice_source": f"人设{_B}",
                              "voice_source_layer": f"persona:{_B}"}}
    assert check_voice_selection(_A, tampered) == "source_mismatch"
    tampered2 = {"persona_id": _A, "persona": _PROFILES[_A],
                 "voice_cfg": {"voice_source": f"会话覆盖(人设{_B})"}}
    assert check_voice_selection(_A, tampered2) == "source_mismatch"


def test_guard_accepts_global_voice_for_voiceless_persona(fake_pm):
    """显式选了没配音色的人设 → 用全局音色是既定语义（UI 状态条已如实预告），不算失配。"""
    _PROFILES["no_voice"] = {"id": "no_voice", "name": "无声人设"}
    try:
        ctx = _resolve("no_voice")
        assert ctx["voice_cfg"]["voice_source"] == "全局"
        assert check_voice_selection("no_voice", ctx) == ""
    finally:
        _PROFILES.pop("no_voice", None)


# ══ 5. 未显式选择 / 系统通用音色：守卫不参与，回落链照旧 ═══════════════════════

def test_fallback_chain_untouched_and_unguarded(fake_pm):
    ctx = _resolve(None)
    assert ctx["persona_id"] == _B
    assert ctx["persona_source"] == "conv_override"
    assert ctx["voice_cfg"]["voice_source"] == f"会话覆盖(人设{_B})"
    assert check_voice_selection("", ctx) == ""
    assert check_voice_selection(None, ctx) == ""


def test_system_voice_sentinel_pins_global(fake_pm):
    ctx = _resolve(SYSTEM_VOICE_ID)
    assert ctx["persona_source"] == "system"
    assert ctx["voice_cfg"]["voice_source"] == "全局(系统通用音色)"
    assert check_voice_selection(SYSTEM_VOICE_ID, ctx) == ""
