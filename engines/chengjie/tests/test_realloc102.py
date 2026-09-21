# -*- coding: utf-8 -*-
"""实施102 overlay 补丁工具 deploy/compute/realloc102.py 门禁。

覆盖：四阶段纯函数的目标值/幂等/不误伤、ja/ko 只删「已死本机 7852」条目、
ruamel round-trip 保注释、探活拒绝落盘。全部在 tmp 上跑，绝不碰仓库 config。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_MOD = _ENGINE / "deploy" / "compute" / "realloc102.py"


@pytest.fixture(scope="module")
def r102():
    spec = importlib.util.spec_from_file_location("_realloc102", _MOD)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _overlay_like_prod():
    """与 2026-09-22 zhiliao overlay 相关段同形（值取自实测），作各阶段输入。"""
    return {
        "vision": {
            "enabled": True,
            "base_url": "http://192.168.0.176:11434/v1",
            "base_urls": ["http://192.168.0.176:11434/v1", "https://api.siliconflow.cn/v1"],
            "endpoint_models": {"siliconflow": "Qwen/Qwen3-VL-8B-Instruct",
                                "192.168.0.176": "qwen3-vl:8b-instruct"},
            "endpoint_timeouts": {"192.168.0.176": 3, "siliconflow": 11},
        },
        "avatar_voice": {
            "enabled": True,
            "emotion_channel_threshold": 2,
            "base_url": "http://192.168.0.104:7865",
            "base_urls": ["http://192.168.0.104:7865"],
            "hub_fish": {"enabled": True, "base_url": "http://192.168.0.176:9000",
                         "tts_engine": "index_tts", "emotion_threshold": 0.35,
                         "persona_allowlist": ["lin_xiaoyu"]},
        },
        "minicpm_clone": {"enabled": True, "base_url": "http://192.168.0.104:7865",
                          "protocol": "fish_speech", "cloud_fallback": False},
        "voice_recognition": {"enabled": True, "base_url": "http://192.168.0.198:8765/v1",
                              "fallback": [{"provider": "avatar_whisper",
                                            "base_url": "http://192.168.0.140:7854"}]},
        "speech_emotion": {"remote": {"base_url": "http://192.168.0.198:8765", "timeout_sec": 10}},
        "audio_pipeline": {"enabled": True, "base_url": "http://192.168.0.198:8765/v1"},
        "companion": {"selfie": {"enabled": True, "generate": {
            "command": ["python", "tools/comfy_infer.py", "--server", "http://192.168.0.176:8188"],
            "note": "http://192.168.0.176:8188 是老落点"}}},
        "voice_lang_route": {
            "enabled": True,
            "cantonese": {"voice": "zh-HK-HiuMaanNeural", "backend": "minicpm_clone",
                          "voice_profile": {"backend": "minicpm_clone",
                                            "clone_base_url": "http://127.0.0.1:7852",
                                            "clone_text_prefix": "<|yue|>"}},
            "clone_langs": {
                "ja": {"backend": "minicpm_clone",
                       "voice_profile": {"backend": "minicpm_clone",
                                         "clone_base_url": "http://127.0.0.1:7852",
                                         "clone_text_prefix": "<|ja|>"}},
                "ko": {"backend": "minicpm_clone",
                       "voice_profile": {"backend": "minicpm_clone",
                                         "clone_base_url": "http://127.0.0.1:7852"}},
                "th": {"backend": "minicpm_clone",
                       "voice_profile": {"backend": "minicpm_clone",
                                         "clone_base_url": "http://192.168.0.199:7852"}},
            },
        },
    }


def test_phase0_vision_to_198(r102):
    d = _overlay_like_prod()
    notes = r102.apply_phase0(d)
    v = d["vision"]
    assert v["base_url"] == "http://192.168.0.198:11434/v1"
    assert v["base_urls"] == ["http://192.168.0.198:11434/v1", "https://api.siliconflow.cn/v1"]
    assert "192.168.0.176" not in v["endpoint_models"] and v["endpoint_models"]["192.168.0.198"] == "qwen3-vl:8b-instruct"
    assert v["endpoint_timeouts"] == {"siliconflow": 11, "192.168.0.198": 5}
    assert notes and r102.apply_phase0(d) == []   # 幂等


def test_phase1_voice_to_176_and_threshold(r102):
    d = _overlay_like_prod()
    notes = r102.apply_phase1(d, svc_token_env="AH_SVC_TOKEN")
    av = d["avatar_voice"]
    assert av["hub_fish"]["base_url"] == "http://192.168.0.176:9000"
    assert av["hub_fish"]["tts_engine"] == "index_tts"
    assert av["hub_fish"]["emotion_threshold"] == 0.35
    assert av["hub_fish"]["persona_allowlist"] == ["lin_xiaoyu"]       # 名单不动
    assert "lang_engines" not in av["hub_fish"]                        # ja/ko 不开闸
    assert av["base_url"] == "http://192.168.0.176:7865"
    assert av["base_urls"] == ["http://192.168.0.176:7865"]           # 104 腿摘掉
    assert av["emotion_channel_threshold"] == 0.5
    assert d["minicpm_clone"]["base_url"] == "http://192.168.0.176:7865"
    assert d["minicpm_clone"]["svc_token_env"] == "AH_SVC_TOKEN"
    assert d["minicpm_clone"]["cloud_fallback"] is False               # 不兜底不变
    assert any("emotion_channel_threshold: 2 -> 0.5" in n for n in notes)
    assert r102.apply_phase1(d, svc_token_env="AH_SVC_TOKEN") == []


def test_phase1_without_token_env_does_not_write_key(r102):
    d = _overlay_like_prod()
    r102.apply_phase1(d)
    assert "svc_token_env" not in d["minicpm_clone"]


def test_phase1_direct_path_disables_hub_keeps_engine_leg(r102):
    """direct：不用 176 角色库——关 hub_fish、本机参考音直连 176:7865；hub 端点保留便于切回。"""
    d = _overlay_like_prod()
    notes = r102.apply_phase1(d, voice_path="direct")
    av = d["avatar_voice"]
    assert av["hub_fish"]["enabled"] is False
    assert av["hub_fish"]["base_url"] == "http://192.168.0.176:9000"     # 保留
    assert av["base_urls"] == ["http://192.168.0.176:7865"]
    assert av["emotion_channel_threshold"] == 0.5
    assert d["minicpm_clone"]["base_url"] == "http://192.168.0.176:7865"
    assert any("hub_fish.enabled: True -> False" in n for n in notes)
    assert r102.apply_phase1(d, voice_path="direct") == []
    with pytest.raises(ValueError):
        r102.apply_phase1(_overlay_like_prod(), voice_path="bogus")


def test_probes_skip_hub_for_direct(r102, monkeypatch):
    asked = []
    monkeypatch.setattr(r102, "_probe", lambda url, must, timeout=5.0: asked.append(url) or None)
    r102.run_probes(["phase1"], voice_path="direct")
    assert asked == ["http://192.168.0.176:7865/health"]
    asked.clear()
    r102.run_probes(["phase1"], voice_path="hub")
    assert "http://192.168.0.176:9000/api/engines" in asked


def test_phase2_hearing_to_176(r102):
    d = _overlay_like_prod()
    r102.apply_phase2(d)
    assert d["voice_recognition"]["base_url"] == "http://192.168.0.176:8765/v1"
    assert d["voice_recognition"]["fallback"][0]["base_url"] == "http://192.168.0.140:7854"  # 备份位不动
    assert d["speech_emotion"]["remote"]["base_url"] == "http://192.168.0.176:8765"
    assert d["audio_pipeline"]["base_url"] == "http://192.168.0.176:8765/v1"
    assert r102.apply_phase2(d) == []


def test_phase3_comfy_yue_and_dead_ja_ko(r102):
    d = _overlay_like_prod()
    notes = r102.apply_phase3(d)
    cmd = d["companion"]["selfie"]["generate"]["command"]
    assert cmd[-1] == "http://192.168.0.104:8188"
    # 只换精确等值的字符串，夹在句子里的老地址不动（那是注释性文字，不是端点）
    assert d["companion"]["selfie"]["generate"]["note"].startswith("http://192.168.0.176:8188")
    assert d["voice_lang_route"]["cantonese"]["voice_profile"]["clone_base_url"] == "http://192.168.0.140:7852"
    assert d["voice_lang_route"]["cantonese"]["voice_profile"]["clone_text_prefix"] == "<|yue|>"
    cl = d["voice_lang_route"]["clone_langs"]
    assert "ja" not in cl and "ko" not in cl
    assert "th" in cl                                   # 指向别处的条目视为有意保留
    assert any("clone_langs.ja 删除" in n for n in notes)
    assert r102.apply_phase3(d) == []


def test_apply_all_prefixes_phase_in_notes(r102):
    d = _overlay_like_prod()
    notes = r102.apply_phases(d, r102.PHASES)
    assert notes and all(n.startswith("[phase") for n in notes)
    assert {n.split("]")[0] + "]" for n in notes} == {"[phase0]", "[phase1]", "[phase2]", "[phase3]"}


def test_roundtrip_keeps_comments_and_backup(r102, tmp_path, monkeypatch):
    """--apply 走 ruamel round-trip：注释保留、备份落盘、审计行追加、只改目标键。"""
    ov = tmp_path / "data" / "config" / "config.local.yaml"
    ov.parent.mkdir(parents=True)
    ov.write_text(
        "# 运维注释：这一行必须活下来\n"
        "vision:\n"
        "  enabled: true   # 行尾注释也要活\n"
        "  base_url: http://192.168.0.176:11434/v1\n"
        "  base_urls:\n"
        "  - http://192.168.0.176:11434/v1\n"
        "  - https://api.siliconflow.cn/v1\n"
        "  endpoint_timeouts:\n"
        "    192.168.0.176: 3\n"
        "    siliconflow: 11\n"
        "unrelated:\n"
        "  keep: me\n",
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["realloc102", "phase0", "--apply", "--no-probe",
                                      "--overlay", str(ov)])
    r102.main()
    text = ov.read_text(encoding="utf-8")
    assert "# 运维注释：这一行必须活下来" in text
    assert "# 行尾注释也要活" in text
    assert "http://192.168.0.198:11434/v1" in text and "192.168.0.176" not in text
    assert "keep: me" in text
    assert (ov.parent / "config.local.yaml.bak_realloc102_phase0").exists()
    audit = (tmp_path / "data" / "logs" / "ai_primary_audit.jsonl").read_text(encoding="utf-8")
    assert '"via": "realloc102_cli"' in audit and '"phase0"' in audit


def test_apply_refuses_when_probe_fails(r102, tmp_path, monkeypatch, capsys):
    ov = tmp_path / "config.local.yaml"
    ov.write_text("vision:\n  base_url: http://192.168.0.176:11434/v1\n", encoding="utf-8")
    monkeypatch.setattr(r102, "run_probes",
                        lambda phases, voice_path="hub": ["[phase0] http://x 不可达（URLError）"])
    monkeypatch.setattr(sys, "argv", ["realloc102", "phase0", "--apply", "--overlay", str(ov)])
    with pytest.raises(SystemExit) as ei:
        r102.main()
    assert "拒绝落盘" in str(ei.value)
    assert "192.168.0.176" in ov.read_text(encoding="utf-8")   # 没写


def test_dry_run_does_not_write(r102, tmp_path, monkeypatch, capsys):
    ov = tmp_path / "config.local.yaml"
    ov.write_text("vision:\n  base_url: http://192.168.0.176:11434/v1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["realloc102", "phase0", "--overlay", str(ov)])
    r102.main()
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "192.168.0.198" in out
    assert "192.168.0.176" in ov.read_text(encoding="utf-8")
