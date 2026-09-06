"""L-2 B（#205 后半，D-L4）：兜底不换声 / 兜底不跨性别（2026-09-06）。

事故（V85TY9 / ERS5QQ，skuio 1.0.73）：男人设 steven 试听三次——无反应 → **系统
默认女声** → 正确克隆声。根因：`tts_pipeline` 主后端失败一律回落 edge，且
`safe_edge_voice(voice, fallback=zh-CN-XiaoxiaoNeural)` 不看人设性别/语种。

本文件钉住新契约：
- 克隆态人设（主后端 ∈ CLONE_BACKENDS）**引擎失败** → 不出任何预置声：ok=False、
  extra.degrade_to_text、extra.fallback_blocked=clone_engine_offline，edge 零调用；
- 预置态才允许回落，且兜底音色必须**同语种同性别**；目录里没有同类 → 改发文字；
- 语种闸（#161 保护性改标准声）仍走 edge，但音色按人设性别对齐（ja × male → Keita）；
- 性别未知 → 行为与改前一致（语种缺省声），既有部署零变更；
- `classify_voice_error` 把两种新失败归到坐席可行动桶。
"""
from pathlib import Path

import pytest

from src.ai import edge_voice_catalog as cat
from src.ai.tts_pipeline import TTSPipeline, classify_voice_error, safe_edge_voice

_JA = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"


# ── 目录 / 纯函数 ────────────────────────────────────────────────────────────

def test_catalog_first_voice_per_lang_matches_lang_route_defaults():
    """每语种第一条＝语种闸改道时的缺省声（与 EDGE_VOICE_BY_LANG 逐条对齐）。"""
    from src.ai.lang_voice_route import EDGE_VOICE_BY_LANG
    for lang, vid in EDGE_VOICE_BY_LANG.items():
        assert cat.pick_edge_voice(lang) == vid, (lang, vid)


def test_catalog_every_lang_has_both_genders():
    for code, _label in cat.languages():
        assert cat.pick_edge_voice(code, "female"), code
        assert cat.pick_edge_voice(code, "male"), code


def test_normalize_gender_free_text():
    assert cat.normalize_gender("男") == "male"
    assert cat.normalize_gender("Female") == "female"
    assert cat.normalize_gender("非二元") == ""
    assert cat.normalize_gender(None) == ""


def test_safe_edge_voice_legacy_contract_unchanged():
    """不传 gender ＝ 旧契约：合法透传 / 非法落 fallback / fallback 非法落内置缺省。"""
    assert safe_edge_voice("ja-JP-NanamiNeural") == "ja-JP-NanamiNeural"
    assert safe_edge_voice("steven", "zh-CN-YunxiNeural") == "zh-CN-YunxiNeural"
    assert safe_edge_voice("steven", "also-bad") == "zh-CN-XiaoxiaoNeural"


def test_safe_edge_voice_gender_aware_mapping():
    """克隆名漏到 edge：男人设 → 同语种男声，绝不落女声缺省；同类缺 → 空串。"""
    assert safe_edge_voice("steven", "zh-CN-XiaoxiaoNeural", gender="male") == "zh-CN-YunxiNeural"
    assert safe_edge_voice("steven", "zh-CN-XiaoxiaoNeural", gender="male", lang="ja") == "ja-JP-KeitaNeural"
    assert safe_edge_voice("steven", "zh-CN-XiaoxiaoNeural", gender="female") == "zh-CN-XiaoxiaoNeural"
    # 合法音色永远透传（用户自选的预置声不被性别规则改写）
    assert safe_edge_voice("zh-CN-XiaoxiaoNeural", gender="male") == "zh-CN-XiaoxiaoNeural"


def test_classify_new_buckets():
    assert classify_voice_error("clone_engine_offline:avatar_clone_unreachable") == "clone_engine_offline"
    assert classify_voice_error("x | fallback(edge_tts):no_voice_for(gender=male,lang=xx)") == "no_matching_voice"
    assert classify_voice_error("edge_voice_unresolved(steven|gender=male)") == "no_matching_voice"
    assert classify_voice_error("avatar_clone_unreachable") == ""


# ── 管线 ────────────────────────────────────────────────────────────────────

def _clone_cfg(tmp_path, **over):
    cfg = {
        "enabled": True,
        "backend": "avatar_clone",
        "fallback_on_error": True,
        "fallback_backend": "edge_tts",
        "fallback_voice": "zh-CN-XiaoxiaoNeural",
        "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "persona_id": "steven",
        "persona_gender": "male",
        "avatar_voice": {"enabled": True},
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "speaker_id": "steven",
        },
    }
    cfg.update(over)
    return cfg


def _deny_clone(monkeypatch):
    async def fake_avatar(self, rv, out, t0, **kw):
        return None          # = avatar_clone_unreachable

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)


def _spy_run_backend(monkeypatch, seen):
    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen.append({"voice": voice, "backend": backend})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ID3" + b"\x00" * 600)
        rv.ok = True
        rv.audio_path = str(out)
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)


async def test_clone_engine_offline_degrades_to_text_no_edge(tmp_path, monkeypatch):
    """steven（男）克隆引擎不可达 → 不出 edge 音频，ok=False + degrade_to_text。"""
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    tts = TTSPipeline(_clone_cfg(tmp_path))
    rv = await tts.synthesize("你好呀，我是 steven。")
    assert rv.ok is False
    assert rv.extra.get("degrade_to_text") is True
    assert rv.extra.get("fallback_blocked") == "clone_engine_offline"
    assert rv.extra.get("primary_error") == "avatar_clone_unreachable"
    assert rv.error.startswith("clone_engine_offline:")
    assert seen == [], f"edge 不得被调用: {seen}"
    assert classify_voice_error(rv.error) == "clone_engine_offline"


async def test_clone_engine_offline_blocks_even_without_gender(tmp_path, monkeypatch):
    """性别未知的克隆人设同样不换声——D-L4 是「一律」，不是只护有性别字段的。"""
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    tts = TTSPipeline(_clone_cfg(tmp_path, persona_gender=""))
    rv = await tts.synthesize("你好")
    assert rv.ok is False
    assert rv.extra.get("fallback_blocked") == "clone_engine_offline"
    assert seen == []


async def test_lang_gate_edge_voice_follows_persona_gender(tmp_path, monkeypatch):
    """语种闸改道 edge（保护性、非故障）仍出声，但 ja × 男人设 → Keita 而非 Nanami。"""
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    cfg = _clone_cfg(
        tmp_path, persona_id="p1",
        avatar_voice={"enabled": True,
                      "hub_fish": {"enabled": True, "tts_engine": "index_tts",
                                   "persona_allowlist": ["p1"]}})
    tts = TTSPipeline(cfg)
    rv = await tts.synthesize(_JA)
    assert rv.ok is True
    assert rv.provider == "edge_tts"
    assert seen and seen[-1]["voice"] == "ja-JP-KeitaNeural"
    assert rv.voice == "ja-JP-KeitaNeural"
    assert rv.extra.get("fallback_from") == "avatar_clone"
    assert rv.extra.get("clone_lang_blocked") == "ja"


async def test_lang_gate_gender_unknown_keeps_lang_default(tmp_path, monkeypatch):
    """性别未知 → 与改前一致：ja → Nanami（EDGE_VOICE_BY_LANG）。"""
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    cfg = _clone_cfg(
        tmp_path, persona_id="p1", persona_gender="",
        avatar_voice={"enabled": True,
                      "hub_fish": {"enabled": True, "tts_engine": "index_tts",
                                   "persona_allowlist": ["p1"]}})
    rv = await TTSPipeline(cfg).synthesize(_JA)
    assert rv.ok is True and seen[-1]["voice"] == "ja-JP-NanamiNeural"


def _preset_cfg(tmp_path, **over):
    """预置态人设：主后端 openai（远端），兜底 edge。"""
    cfg = {
        "enabled": True,
        "backend": "openai",
        "voice": "onyx",
        "api_key": "sk-test",
        "fallback_on_error": True,
        "fallback_backend": "edge_tts",
        "fallback_voice": "zh-CN-XiaoxiaoNeural",
        "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "persona_id": "laowang",
        "persona_gender": "male",
        "persona_lang": "zh",
    }
    cfg.update(over)
    return cfg


async def test_preset_fallback_keeps_gender(tmp_path, monkeypatch):
    """预置态主后端失败 → 允许回落 edge，但兜底音色必须同性别（男→云希，不是晓晓）。"""
    seen: list = []

    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen.append({"voice": voice, "backend": backend})
        if backend == "openai":
            return "tts_timeout(20s)"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ID3" + b"\x00" * 600)
        rv.ok = True
        rv.audio_path = str(out)
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)
    rv = await TTSPipeline(_preset_cfg(tmp_path)).synthesize("你好")
    assert rv.ok is True
    assert rv.provider == "edge_tts"
    assert [s["backend"] for s in seen] == ["openai", "edge_tts"]
    assert seen[-1]["voice"] == "zh-CN-YunxiNeural"
    assert rv.voice == "zh-CN-YunxiNeural"
    assert rv.extra.get("fallback_from") == "openai"


async def test_preset_fallback_without_matching_voice_degrades_to_text(tmp_path, monkeypatch):
    """同语种找不到同性别预置声 → 不出声改发文字（不拿别的语种/性别顶）。"""
    seen: list = []

    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen.append({"voice": voice, "backend": backend})
        return "tts_timeout(20s)"

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)
    # 目录里没有 "xx" 语种 → 男声找不到
    rv = await TTSPipeline(_preset_cfg(tmp_path, persona_lang="xx")).synthesize("hello")
    assert rv.ok is False
    assert rv.extra.get("degrade_to_text") is True
    assert rv.extra.get("fallback_blocked") == "no_matching_voice"
    assert [s["backend"] for s in seen] == ["openai"], "edge 不得被调用"
    assert classify_voice_error(rv.error) == "no_matching_voice"


async def test_preset_fallback_gender_unknown_uses_config_default(tmp_path, monkeypatch):
    """性别未知的预置态人设：兜底沿用配置 fallback_voice（旧行为，零变更）。"""
    seen: list = []

    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen.append({"voice": voice, "backend": backend})
        if backend == "openai":
            return "tts_timeout(20s)"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ID3" + b"\x00" * 600)
        rv.ok = True
        rv.audio_path = str(out)
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)
    rv = await TTSPipeline(_preset_cfg(tmp_path, persona_gender="")).synthesize("你好")
    assert rv.ok is True and seen[-1]["voice"] == "zh-CN-XiaoxiaoNeural"


def test_persona_voice_injects_gender_from_runtime_profile(monkeypatch):
    """resolve_voice_cfg 把人设 gender 随 cfg 注入（TTSPipeline.persona_gender 的来源）。"""
    from src.ai import persona_voice as pv
    from src.utils.persona_manager import PersonaManager

    class _PM:
        def get_persona_by_id(self, pid):
            return {"id": pid, "gender": "男",
                    "voice_profile": {"enabled": True, "backend": "avatar_clone",
                                      "owner_consent": True, "speaker_id": "steven"}}

    monkeypatch.setattr(PersonaManager, "get_instance", staticmethod(lambda: _PM()))
    cfg = pv.resolve_voice_cfg("steven", {"telegram": {"voice_reply": {"backend": "edge_tts"}}})
    assert cfg.get("persona_gender") == "男"
    assert TTSPipeline(cfg).persona_gender == "male"
