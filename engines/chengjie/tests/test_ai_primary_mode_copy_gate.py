# -*- coding: utf-8 -*-
"""主链档位表达层门禁（2026-09-17）：lock=local 时任何出口不得再用 08-22 cloud 叙事。

守：
1. ``build_summary`` 是单一口径：local 档顺序 local→cloud→pool，文案带本地模型；
2. 推送器 ``chain_roles`` 与正本 order/roles/mode_label 一致（引擎不可达时的回落副本）；
3. 告警卡 / 每日摘要 / 运维通报在 local 夹具下不含过时字眼；
4. 媒体落点从 overlay 现读（ASR 198 不得回落成写死的 176）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from src.ai.ai_primary_summary import build_summary, chain_roles
from src.inbox.webhook_notifier import _build_message

_STALE = (
    "主链厂商",
    "仍在 cloud 档",
    "三线 · 局域网",
    "三线 173",
    "主胎 · 硅基",
    "2026-08-22 起锁 cloud",
)

_AI_LOCAL = {
    "ai": {
        "primary": "local",
        "primary_lock": "local",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "cost_guard": {"provider": "deepseek"},
        "fallback": {
            "enabled": True,
            "base_url": "http://192.168.0.173:8001/v1",
            "model": "chatx",
        },
        "key_pool": {
            "keys": [{"api_key": "sk-pool", "base_url": "https://api.siliconflow.cn/v1"}],
        },
    }
}


def _pusher():
    p = Path(__file__).resolve().parents[1] / "tools" / "compute_status_report.py"
    spec = importlib.util.spec_from_file_location("compute_status_report_gate", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _ops():
    p = Path(__file__).resolve().parents[1] / "tools" / "send_ops_group_report.py"
    spec = importlib.util.spec_from_file_location("send_ops_group_report_gate", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _assert_fresh(text: str) -> None:
    for phrase in _STALE:
        assert phrase not in text, f"过时字眼 {phrase!r} 出现在：{text}"


def test_summary_local_lock_shapes_chain():
    s = build_summary(_AI_LOCAL, effective="local", lock="local")
    assert s["ok"] is True
    assert s["order"] == ["local", "cloud", "pool"]
    assert s["roles"]["local"].startswith("① 主链")
    assert "本地 vLLM chatx" in s["primary_text"]
    assert "锁 local" in s["primary_text"]
    assert "回落" in s["chain_text"] and "deepseek" in s["chain_text"]
    assert s["billing_provider"] == "deepseek"
    _assert_fresh(s["primary_text"] + " " + s["chain_text"] + " ".join(s["roles"].values()))


def test_summary_cloud_keeps_cloud_first():
    cfg = {"ai": dict(_AI_LOCAL["ai"], primary="cloud", primary_lock="cloud")}
    s = build_summary(cfg, effective="cloud", lock="cloud")
    assert s["order"] == ["cloud", "pool", "local"]
    assert "云端" in s["primary_text"] and "档位 cloud" in s["primary_text"]


def test_pusher_chain_roles_matches_canonical():
    pusher = _pusher()
    for mode in ("local", "local_only", "cloud"):
        canon = chain_roles(mode, mode)
        copy = pusher.chain_roles(mode, mode)
        assert copy["order"] == canon["order"]
        assert copy["roles"] == canon["roles"]
        assert copy["mode_label"] == canon["mode_label"]


def test_media_endpoints_follow_overlay_not_hardcoded_176():
    pusher = _pusher()
    ep = pusher.media_endpoints({
        "voice_recognition": {"base_url": "http://192.168.0.198:8765/v1", "model": "large-v3-turbo"},
        "speech_emotion": {"remote": {"base_url": "http://192.168.0.198:8765"}},
        "avatar_voice": {"base_url": "http://192.168.0.104:7865"},
    })
    assert ep["asr"] == "http://192.168.0.198:8765"
    assert ep["ser"] == "http://192.168.0.198:8765"
    assert ep["tts"] == "http://192.168.0.104:7865"
    assert "176" not in ep["asr"]
    # 缺键才回落旧常量
    fallback = pusher.media_endpoints({})
    assert fallback["asr"].endswith(":8765") and "176" in fallback["asr"]


def test_mode_switched_card_uses_payload_not_stale_cloud_story():
    title, text = _build_message("ai_primary_guard_alert", {
        "kind": "mode_switched",
        "from_mode": "cloud",
        "to_mode": "local",
        "lock_from": "cloud",
        "lock": "local",
        "effective": "local",
        "primary_text": "本地 vLLM chatx（档位 local，锁 local）",
        "chain_text": "本地 LAN .173 vLLM chatx → 回落 deepseek-official → canned",
        "mode_label": "本地主链（可回落云端）",
        "via": "ai_client_init",
    })
    blob = title + "\n" + text
    assert "cloud → local" in title
    assert "本地 vLLM chatx" in blob
    _assert_fresh(blob)


def test_probe_fail_locked_card_does_not_claim_switched_to_cloud():
    title, text = _build_message("ai_primary_guard_alert", {
        "kind": "probe_fail_locked",
        "lock": "local",
        "effective": "local",
        "base_url": "http://192.168.0.173:8001/v1",
        "fail_count": 3,
        "down_minutes": 12,
    })
    blob = title + "\n" + text
    assert "未动" in title and "local" in title
    assert "已热切 cloud" not in blob
    _assert_fresh(blob)


def test_ops_digest_primary_line_for_local():
    title, text = _build_message("ops_digest_report", {
        "day": "2026-09-18",
        "open": [],
        "primary": {
            "primary_text": "本地 vLLM chatx（档位 local，锁 local）",
            "effective": "local",
            "billing_provider": "deepseek",
        },
    })
    assert "主链" in text and "本地 vLLM chatx" in text
    assert "云端计费 deepseek" in text
    _assert_fresh(title + "\n" + text)


def test_ops_report_prefers_summary_primary_text():
    mod = _ops()
    summary_shape = {
        "ok": True,
        "effective": "local",
        "lock": "local",
        "primary_text": "本地 vLLM chatx（档位 local，锁 local）",
        "chain_text": "本地 LAN .173 vLLM chatx → 回落 deepseek-official deepseek-chat → canned",
        "local_ready": True,
        "local": {"ready": True, "model": "chatx"},
    }
    line = mod.primary_line(summary_shape, {"provider": "deepseek"})
    assert line.startswith("• 主链：本地 vLLM chatx")
    assert "降级链" in line
    _assert_fresh(line)
    msg = mod.build_ops_message(
        "tok", {"available": True, "provider": "deepseek", "pricing_configured": True,
                "today": {"calls": 1, "cost": 0.1}, "budget": {}, "last_recon": {}},
        notes=[], open_items=[], probe_txt="8/8", primary=summary_shape)
    assert "主链：本地 vLLM chatx" in msg
    _assert_fresh(msg)


def test_pusher_nudge_consume_and_path_matches_canonical(tmp_path):
    pusher = _pusher()
    from src.ai.ai_primary_summary import BOARD_NUDGE_PATH
    assert pusher.NUDGE_FILE == BOARD_NUDGE_PATH
    p = tmp_path / "nudge"
    assert pusher.consume_board_nudge(p) is False
    p.write_text("1", encoding="utf-8")
    assert pusher.consume_board_nudge(p) is True
    assert not p.exists()
    assert pusher.consume_board_nudge(p) is False