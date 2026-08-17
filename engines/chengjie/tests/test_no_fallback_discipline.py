"""无兜底纪律（2026-08-17 老板拍板，docs/实施33 v2）核心门禁。

三原则：① 链路失败=不发消息（禁一切静默替代品）② 失败必弹窗+ERROR 上报主机
③ 内容不丢（转工作台待发）。本文件钉：
  - delivery_block 模块契约（计数/快照/notify_host 出口）
  - true_probe 规格推导 + 连败状态机
  - 各拦截点的源码级接线（谁把兜底翻回来先红）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ops import delivery_block as db
from src.ops.true_probe import build_probe_specs, next_strike_state

_SRC = Path(__file__).parent.parent / "src"


@pytest.fixture(autouse=True)
def _clean_counters():
    db.reset_for_tests()
    yield
    db.reset_for_tests()


# ── delivery_block 契约 ─────────────────────────────────────────────


def test_report_block_counts_and_snapshot(monkeypatch):
    calls = []

    def _fake_notify(title, message, *, key="", cooldown_sec=1800.0, **kw):
        calls.append((title, key, cooldown_sec))
        return True

    import src.utils.host_alert as ha
    monkeypatch.setattr(ha, "notify_host", _fake_notify)

    db.report_block("voice", reason="tts_failed", platform="telegram",
                    conversation_id="telegram:a:1", queued_draft=True)
    db.report_block("voice", reason="tts_failed")
    db.report_block("translate", reason="hold")

    snap = db.snapshot()
    assert snap["total"] == 3
    assert snap["by_domain"]["voice"] == 2
    assert snap["by_domain"]["translate"] == 1
    assert snap["by_reason"]["voice:tts_failed"] == 2
    assert snap["recent"][-1]["domain"] == "translate"
    # notify_host 每次都被调（去抖由 notify_host 自己的 cooldown 承担）
    assert len(calls) == 3
    # 2026-08-17 弹窗美化：标题从「AI 不可用」改「AI 回复未发出」——语义更准
    # （链路故障被拦下 ≠ 整个 AI 死了），纪律三原则（拦发/必响/内容不丢）不变。
    assert calls[0][0].startswith("AI 回复未发出")
    assert calls[0][1] == "deliv:voice"


def test_report_block_never_raises(monkeypatch):
    import src.utils.host_alert as ha

    def _boom(*a, **k):
        raise RuntimeError("popup broke")

    monkeypatch.setattr(ha, "notify_host", _boom)
    db.report_block("asr", reason="x")          # 不抛
    db.report_block("", reason="")               # 空域照记
    assert db.snapshot()["total"] == 2


def test_unknown_domain_tolerated():
    db.report_block("weird_domain", reason="r")
    assert db.snapshot()["by_domain"]["weird_domain"] == 1


# ── true_probe：规格推导 ────────────────────────────────────────────


_FULL_CFG = {
    "avatar_voice": {
        "enabled": True,
        "hub_fish": {
            "enabled": True,
            "base_url": "http://hub:9000",
            "tts_engine": "index_tts",
            "profile_map": {"lin_xiaoyu": "林小雨-智聊"},
        },
    },
    "translation": {"engines": {"ollama_mt": {
        "base_urls": ["http://mt:11434"], "model": "hy-mt2"}}},
    "vision": {"enabled": True, "base_url": "http://vl:11434/v1",
               "model": "qwen3-vl:8b"},
    "voice_recognition": {"enabled": True,
                          "base_url": "http://asr:8765/v1",
                          "model": "large-v3-turbo"},
}


def test_build_probe_specs_full_config():
    specs = build_probe_specs(_FULL_CFG)
    domains = [s["domain"] for s in specs]
    assert domains == ["tts", "translate", "vision", "asr"]
    tts = specs[0]
    assert tts["url"] == "http://hub:9000/api/tts_only"
    assert tts["json"]["tts_engine"] == "index_tts"   # 引擎显式钉，禁隐式路由
    assert tts["json"]["profile"] == "林小雨-智聊"
    assert tts["json"]["best_of"] == 1
    vis = specs[2]
    assert vis["url"] == "http://vl:11434/v1/chat/completions"
    assert vis["json"]["model"] == "qwen3-vl:8b"


def test_build_probe_specs_skips_disabled_domains():
    cfg = {
        "avatar_voice": {"enabled": False},
        "translation": {},
        "vision": {"enabled": False},
        "voice_recognition": {"enabled": False},
    }
    assert build_probe_specs(cfg) == []
    assert build_probe_specs({}) == []


def test_build_probe_specs_hub_without_profile_skipped():
    cfg = dict(_FULL_CFG)
    cfg["avatar_voice"] = {
        "enabled": True,
        "hub_fish": {"enabled": True, "base_url": "http://hub:9000",
                     "profile_map": {}},
    }
    assert "tts" not in [s["domain"] for s in build_probe_specs(cfg)]


# ── true_probe：连败状态机 ──────────────────────────────────────────


def test_strike_state_alert_once_then_recover():
    st = {}
    st, a1 = next_strike_state(st, "tts", False, fail_strikes=2, now=1.0)
    assert a1 == ""                       # 第 1 败：不响（吸收抖动）
    st, a2 = next_strike_state(st, "tts", False, fail_strikes=2, now=2.0)
    assert a2 == "alert"                  # 第 2 败：首报
    st, a3 = next_strike_state(st, "tts", False, fail_strikes=2, now=3.0)
    assert a3 == ""                       # 持续失败不重复出 action
    st, a4 = next_strike_state(st, "tts", True, fail_strikes=2, now=4.0)
    assert a4 == "recovered"              # 报过警后恢复：绿窗
    st, a5 = next_strike_state(st, "tts", True, fail_strikes=2, now=5.0)
    assert a5 == ""                       # 正常运行无动作
    assert st["tts"]["fails"] == 0


def test_strike_state_domains_independent():
    st = {}
    st, _ = next_strike_state(st, "tts", False, fail_strikes=2, now=1.0)
    st, a = next_strike_state(st, "asr", False, fail_strikes=1, now=1.0)
    assert a == "alert"                   # asr 阈值 1 独立触发
    assert st["tts"]["fails"] == 1 and not st["tts"]["alerted"]


def test_recover_without_prior_alert_is_silent():
    st = {}
    st, _ = next_strike_state(st, "vision", False, fail_strikes=3, now=1.0)
    st, a = next_strike_state(st, "vision", True, fail_strikes=3, now=2.0)
    assert a == ""                        # 没报过警的抖动恢复不发绿窗（防噪）


# ── 各拦截点源码级接线钉（兜底翻回来先红）────────────────────────────


def test_ai_client_chat_fallback_gate_wired():
    src = (_SRC / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "_fb_chat_fallback_enabled" in src
    assert "cloud_failed_no_fallback" in src
    # 闸必须只拦兜底身份，不拦本地主链（as_primary）
    i = src.index("cloud_failed_no_fallback")
    assert "not as_primary" in src[i - 600: i]


def test_autosend_worker_no_original_on_translate_failure():
    src = (_SRC / "inbox" / "autosend_worker.py").read_text(encoding="utf-8")
    assert "出站翻译异常，发原文" not in src
    assert "人工通过出站翻译异常，发原文" not in src
    assert src.count("translate_hold") >= 2   # 两条投递路径都走 HOLD


def test_outbound_translate_all_failures_hold():
    src = (_SRC / "inbox" / "outbound_translate.py").read_text(encoding="utf-8")
    assert "翻译调用失败，发原文" not in src
    assert "_report_block_safe" in src
    assert "_TRANSLATABLE_RE" in src           # 纯 emoji 放行护栏在场


def test_asr_failure_skips_auto_reply():
    src = (_SRC / "client" / "telegram_client.py").read_text(encoding="utf-8")
    assert "语音没听懂就不装懂" in src
    i = src.index("语音没听懂就不装懂")
    seg = src[i: i + 2600]
    assert 'report_block(' in seg and '"asr"' in seg
    assert "跳过自动回复" in seg


def test_promise_fail_excuse_removed():
    src = (_SRC / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert '"promise_fail"' not in src         # 「手机抽风改天补」话术出口拆除
    assert "promise_fulfill_failed" in src     # 改为 delivery_block 上报


def test_sender_voice_block_queues_draft():
    src = (_SRC / "client" / "sender.py").read_text(encoding="utf-8")
    assert "voice_blocked" in src              # 拦下的回复转工作台待发
    assert "upsert_draft" in src


def test_seat_banner_only_recent_failures():
    db.report_block("vision", reason="no_desc")
    now = 10_000.0
    # 把这次失败改成超过 30min 的旧账
    with db._lock:
        db._last_ts["vision"] = now - 2000
        db._recent[-1]["ts"] = now - 2000
    stale = db.seat_banner(now=now, max_age_sec=1800)
    assert stale["active"] is False
    assert stale["by_domain"] == {}

    db.report_block("asr", reason="empty")
    fresh = db.seat_banner(now=now + 1, max_age_sec=1800)
    assert fresh["active"] is True
    assert "asr" in fresh["by_domain"]
    assert "vision" not in fresh["by_domain"]


def test_vision_single_track_no_ocr_fallback():
    src = (_SRC / "client" / "telegram_client.py").read_text(encoding="utf-8")
    i = src.index("async def _get_image_content")
    seg = src[i: i + 1200]
    assert "不再" in seg and "OCR" in seg
    assert "ImageRecognizer" not in seg
    assert "没看懂图就不装懂" in src
    hold = src[src.index("没看懂图就不装懂"): src.index("没看懂图就不装懂") + 1800]
    assert 'report_block(' in hold and '"vision"' in hold
    assert "跳过自动回复" in hold


async def test_vision_zhipu_fallback_hard_gate(monkeypatch):
    """no_cloud_fallback=true 时智谱回落结构性关闭——即使有人贴回 key 也不触碰。"""
    from src import vision_client as vcm

    monkeypatch.setattr(vcm.VisionClient, "initialize", lambda self: False)

    def _boom(*a, **k):  # 硬闸生效 = 凭据函数根本不该被咨询
        raise AssertionError("zhipu credentials consulted despite hard gate")

    monkeypatch.setattr(vcm, "_zhipu_credentials", _boom)
    merged = {
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "x",
        "no_cloud_fallback": True,
    }
    gv = {"zhipu_api_key": "sk-someone-pasted-a-key-back"}
    txt, dbg = await vcm.VisionClient._describe_fallback_chain(
        merged, gv, "nonexistent.jpg")
    assert txt is None
    assert dbg.endswith("no_cloud_fallback")


def test_vision_hard_gate_overlay_enabled():
    """zhiliao 生产 overlay 必须开着硬闸（防止将来被顺手删掉）。"""
    overlay = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")
    if not overlay.exists():
        pytest.skip("非生产机，无 zhiliao overlay")
    text = overlay.read_text(encoding="utf-8")
    assert "no_cloud_fallback: true" in text


def test_protocol_and_autodraft_hold_unrecognized_media():
    proto = (_SRC / "integrations" / "protocol_autoreply.py").read_text(
        encoding="utf-8")
    assert "图片未看懂 → 跳过自动回复" in proto
    assert "语音未转写 → 跳过自动回复" in proto
    ad = (_SRC / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    assert 'decided_by="vision_hold"' in ad
    assert 'decided_by="asr_hold"' in ad


def test_workbench_delivery_block_wired():
    setup = (_SRC / "web" / "routes" / "unified_inbox_setup_routes.py").read_text(
        encoding="utf-8")
    assert "seat_banner" in setup and '"delivery_block"' in setup
    drafts = (_SRC / "web" / "routes" / "drafts_routes.py").read_text(
        encoding="utf-8")
    assert 'metrics["delivery_block"]' in drafts
    html = (_SRC / "web" / "templates" / "workspace_base.html").read_text(
        encoding="utf-8")
    assert 'id="ws-delivblock"' in html
    assert "_renderDelivBlock" in html
    api = (_SRC / "web" / "templates" / "_api_fetch.html").read_text(
        encoding="utf-8")
    assert "ws-delivblock" in api
