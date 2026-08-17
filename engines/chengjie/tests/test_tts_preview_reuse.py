# -*- coding: utf-8 -*-
"""「所听即所发」试听产物复用契约（P1 2026-08-05）。

试听（/api/voice/tts-test）与发送（/api/unified-inbox/send-voice）此前是两次
独立合成：坐席听到 A 声、客户可能收到 B 声（克隆链中途恢复/掉线时 provider
漂移），同一句话烧两份 TTS/字符额度。契约＝试听落盘时写 sidecar（文本指纹+
音色键），发送带回 filename、校验通过 → 试听音频直接进出站管线。

安全不变量（本文件重点守「不该复用的绝不复用」）：
  - 文件名白名单（无路径分隔符 → 防穿越读任意文件当语音发出去）
  - 文本/音色键任一不符 → 回落合成（绝不把别的话/别的声发给客户）
  - 过期/空壳/缺 sidecar → 回落合成
  - 复用命中 = 零二次合成 + **不消费原件**（复制后进管线，发送失败重试仍可用）
"""

from __future__ import annotations

import json
import time
import types

import pytest

import src.integrations.shared.tts_preview as tp

_FN = "ttspreview-0a1b2c3d4e.mp3"
_TEXT = "我还在忙饭，你吃饭了吗？"


@pytest.fixture()
def preview_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "_TTS_DIR", tmp_path)
    return tmp_path


def _seed(preview_dir, *, fn=_FN, text=_TEXT, persona_key="", size=4096, meta=None):
    (preview_dir / fn).write_bytes(b"\x00" * size)
    assert tp.record_preview_meta(fn, text=text, persona_key=persona_key,
                                  meta=meta or {"provider": "edge_tts",
                                                "voice": "zh-CN-XiaoxiaoNeural",
                                                "resolved_persona_id": "linda",
                                                "duration_sec": 1.5})
    return preview_dir / fn


# ── 纯函数层 ────────────────────────────────────────────────────────────────

def test_roundtrip_hit(preview_dir):
    _seed(preview_dir)
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert why == "" and info is not None
    assert info["path"] == preview_dir / _FN
    assert info["meta"]["provider"] == "edge_tts"


def test_text_mismatch_rejected(preview_dir):
    _seed(preview_dir)
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT + "改了", persona_key="")
    assert info is None and why == "text_mismatch"


def test_persona_mismatch_rejected(preview_dir):
    _seed(preview_dir, persona_key="linda")
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert info is None and why == "persona_mismatch"


def test_text_key_strip_stable(preview_dir):
    """两侧路由都 strip；指纹对「首尾空白差异」必须稳定（同文不同壳仍命中）。"""
    _seed(preview_dir)
    info, why = tp.resolve_reusable_preview(_FN, text=f"  {_TEXT}\n", persona_key="")
    assert why == "" and info is not None


@pytest.mark.parametrize("bad", [
    "",                                   # 空
    "../secrets.mp3",                     # 穿越
    "ttspreview-0a1b2c3d4e.exe",          # 扩展名白名单外
    "sub/ttspreview-0a1b2c3d4e.mp3",      # 带路径分隔符
    "ttspreview-XYZ.mp3",                 # 名形不符
    "ttspreview-0a1b2c3d4e.mp3.json",     # 直接点 sidecar
])
def test_bad_names_rejected(preview_dir, bad):
    _seed(preview_dir)
    info, why = tp.resolve_reusable_preview(bad, text=_TEXT, persona_key="")
    assert info is None and why == "bad_name"


def test_missing_audio_or_sidecar(preview_dir):
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert info is None and why == "missing_audio"
    (preview_dir / _FN).write_bytes(b"\x00" * 4096)   # 有音频无 sidecar
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert info is None and why == "no_sidecar"


def test_expired_rejected(preview_dir):
    _seed(preview_dir)
    side = preview_dir / (_FN + ".json")
    payload = json.loads(side.read_text(encoding="utf-8"))
    payload["created_ts"] = time.time() - tp.REUSE_MAX_AGE_SEC - 5
    side.write_text(json.dumps(payload), encoding="utf-8")
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert info is None and why == "expired"


def test_tiny_file_rejected(preview_dir):
    _seed(preview_dir, size=100)
    info, why = tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")
    assert info is None and why == "too_small"


def test_record_rejects_nonconforming_name(preview_dir):
    """rename 失败回落的非常规文件名 → 不登记（自然失去复用资格，而非记坏契约）。"""
    assert tp.record_preview_meta("odd-name.mp3", text=_TEXT, persona_key="") is False


def test_cleanup_sweeps_sidecars(preview_dir):
    audio = _seed(preview_dir)
    side = preview_dir / (_FN + ".json")
    old = time.time() - 90000
    import os
    os.utime(audio, (old, old))
    os.utime(side, (old, old))
    removed = tp.cleanup_tts_previews(max_age_sec=3600)
    # 音频 + sidecar 一起清，不留孤儿契约（conftest 隔离层会往 tmp 放别的文件，
    # 只对本模块种的两个文件断言）
    assert removed >= 2
    assert not audio.exists() and not side.exists()


# ── 观测层（进程级计数：看板/调参的数据地基）────────────────────────────────

def test_reuse_stats_counting(preview_dir):
    base = tp.reuse_stats_snapshot()
    _seed(preview_dir)                                                    # recorded +1
    tp.resolve_reusable_preview(_FN, text=_TEXT, persona_key="")          # hit +1
    tp.resolve_reusable_preview(_FN, text=_TEXT + "x", persona_key="")    # miss: text_mismatch
    tp.resolve_reusable_preview("bad.mp3", text=_TEXT, persona_key="")    # miss: bad_name
    s = tp.reuse_stats_snapshot()
    assert s["recorded"] == base["recorded"] + 1
    assert s["hits"] == base["hits"] + 1
    assert s["misses"].get("text_mismatch", 0) == base["misses"].get("text_mismatch", 0) + 1
    assert s["misses"].get("bad_name", 0) == base["misses"].get("bad_name", 0) + 1
    # 快照自洽：attempts = hits + Σmisses；hit_rate ∈ [0,1]
    assert s["attempts"] == s["hits"] + sum(s["misses"].values())
    assert s["hit_rate"] is not None and 0 <= s["hit_rate"] <= 1


def test_reuse_stats_reason_cap(preview_dir, monkeypatch):
    monkeypatch.setattr(tp, "_MISS_REASON_CAP", 3)
    stats = {"recorded": 0, "hits": 0, "misses": {}}
    monkeypatch.setattr(tp, "_REUSE_STATS", stats)
    for i in range(10):
        tp._record_miss(f"reason_{i}")
    assert len(stats["misses"]) == 3   # 封顶防未来枚举失控撑爆快照
    tp._record_miss("reason_0")        # 已在桶内的原因照常累加
    assert stats["misses"]["reason_0"] == 2


def test_avatar_status_exposes_reuse_stats(auth_client):
    r = auth_client.get("/api/voice/avatar-status", follow_redirects=False)
    assert r.status_code == 200
    assert "preview_reuse" in r.json()


def test_workspace_metrics_exposes_reuse(auth_client):
    r = auth_client.get("/api/workspace/metrics", follow_redirects=False)
    assert r.status_code == 200
    assert "voice_preview_reuse" in r.json()


# ── 路由层（send-voice 复用分支端到端）─────────────────────────────────────

@pytest.fixture()
def _wired(monkeypatch, preview_dir, tmp_path):
    """protocol 账号在线 + 出站落盘/发送打桩；合成器装「不许被调用」哨兵。"""
    import src.integrations.account_orchestrator as _ao
    import src.integrations.protocol_bridge as _pb

    calls = {"synth": 0, "sent": 0}

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def send_media(self, *a, **k):
            calls["sent"] += 1
            return {"ok": True, "message_id": "m1"}

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    def _save(platform, account_id, basename, data):
        out = tmp_path / f"out-{basename}"
        out.write_bytes(data)
        return str(out), f"/media/{basename}", "voice"

    monkeypatch.setattr(_pb, "save_outbound_media", _save)

    from src.ai.tts_pipeline import TTSPipeline

    async def _synth(self, *a, **k):
        calls["synth"] += 1
        return types.SimpleNamespace(ok=False, error="synth-called", audio_path="")

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)
    return calls


def test_route_reuse_hit_skips_synthesis(auth_client, preview_dir, _wired):
    src_file = _seed(preview_dir)
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": _TEXT, "preview_filename": _FN},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("reused_preview") is True
    assert d.get("voice_meta", {}).get("persona_source") == "preview_reuse"
    assert d.get("voice_meta", {}).get("persona_id") == "linda"
    assert _wired["synth"] == 0, "复用命中不得二次合成"
    assert _wired["sent"] == 1
    assert src_file.is_file(), "复用必须复制消费，原件保留（发送失败重试仍可用）"


def test_route_reuse_miss_falls_back_to_synth(auth_client, preview_dir, _wired):
    _seed(preview_dir)
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": _TEXT + "（改过）",
              "preview_filename": _FN},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    # 文本指纹不符 → 回落现场合成（打桩合成器返回失败以证明「确实走到了合成」）
    assert d.get("ok") is False
    assert d.get("reason") == "synth-called"
    assert _wired["synth"] == 1
    assert _wired["sent"] == 0


def test_route_no_preview_field_unchanged(auth_client, _wired):
    """不带 preview_filename 的老客户端行为零变化（直接走合成路径）。"""
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": _TEXT},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json().get("reason") == "synth-called"
    assert _wired["synth"] == 1
