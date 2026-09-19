"""P0-4（2026-09-19）：autosend 语音的桌面桥分支（wechat_pc 驱动经声卡录入）。

覆盖：
- 可行性预检 `_desktop_voice_bridge_ctx`：bridge 关 / 非桌面账号 / 驱动没报 voice_ready → None；齐全 → 配置块。
- `autosend_voice` 在 owns_media=False 且桥可行时不再早退，而是走 `_voice_gate` → 桥投递。
- `_deliver_voice_via_desktop_bridge`：真实时长门（ffprobe 结果而非字数）、单条超顶 → 强制分条重合成、
  分条后仍超 → 回落文字并清理文件、首条入队被拦 → 回落、逐条 kind=voice 入队字段完整、review_mode → held。
- `_force_split_cfg` 不改原 config。
"""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from src.inbox import autosend_helpers as ah


def _assistant(cfg: dict):
    logs = []

    class _Log:
        def _rec(self, *a, **k):
            logs.append(a[0] if a else "")

        info = debug = warning = _rec

    return SimpleNamespace(config=SimpleNamespace(config=cfg), logger=_Log(), _logs=logs)


def _bridge_cfg(**over):
    br = {"enabled": True}
    br.update(over)
    return {"inbox": {"l2_autosend": {"desktop_bridge": br,
                                      "voice": {"enabled": True}}}}


def _registry(mode="desktop", hb=None):
    row = {"mode": mode, "meta": {"bridge_heartbeat": hb} if hb is not None else {}}

    class _Reg:
        def get(self, platform, account_id):
            return row

    return _Reg()


def _alive_hb(voice_ready=True, mic_busy=False):
    stats = {"voice_ready": voice_ready}
    if mic_busy:
        stats["voice_mic_busy"] = True
        stats["voice_mic_busy_by"] = "Zoom.exe"
    return {"ts": time.time(), "tier": "auto_reply", "readonly": False,
            "stats": stats, "voice_ready": voice_ready}


# ── 预检 ──────────────────────────────────────────────────────────────────
def test_ctx_none_when_bridge_disabled(monkeypatch):
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=_alive_hb()))
    assert ah._desktop_voice_bridge_ctx({"inbox": {"l2_autosend": {"desktop_bridge": {"enabled": False}}}},
                                        "wechat_pc", "wx1") is None


def test_ctx_none_when_not_desktop_account(monkeypatch):
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(mode="protocol", hb=_alive_hb()))
    assert ah._desktop_voice_bridge_ctx(_bridge_cfg(), "wechat_pc", "wx1") is None


def test_ctx_none_when_driver_not_voice_ready(monkeypatch):
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=_alive_hb(voice_ready=False)))
    assert ah._desktop_voice_bridge_ctx(_bridge_cfg(), "wechat_pc", "wx1") is None


def test_ctx_none_when_heartbeat_stale(monkeypatch):
    import src.integrations.account_registry as _ar
    hb = _alive_hb()
    hb["ts"] = time.time() - 3600
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=hb))
    assert ah._desktop_voice_bridge_ctx(_bridge_cfg(), "wechat_pc", "wx1") is None


def test_ctx_none_when_voice_enabled_false(monkeypatch):
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=_alive_hb()))
    assert ah._desktop_voice_bridge_ctx(_bridge_cfg(voice_enabled=False), "wechat_pc", "wx1") is None


def test_ctx_none_when_seat_mic_busy(monkeypatch):
    """P2-3：心跳报了 voice_mic_busy → 预检直接 None，省一次 TTS + 入队 + 驱动拒发。"""
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=_alive_hb(mic_busy=True)))
    assert ah._desktop_voice_bridge_ctx(_bridge_cfg(), "wechat_pc", "wx1") is None


def test_ctx_ok_returns_bridge_block(monkeypatch):
    import src.integrations.account_registry as _ar
    monkeypatch.setattr(_ar, "get_account_registry", lambda: _registry(hb=_alive_hb()))
    br = ah._desktop_voice_bridge_ctx(_bridge_cfg(review_mode=True), "wechat_pc", "wx1")
    assert isinstance(br, dict) and br.get("review_mode") is True


# ── autosend_voice 入口分流 ─────────────────────────────────────────────────
def test_autosend_voice_routes_to_desktop_bridge(monkeypatch):
    cfg = {"voice_autosend": {"enabled": True}, **_bridge_cfg()}

    class _Orch:
        def owns_media(self, platform, account_id):
            return False

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(ah, "_desktop_voice_bridge_ctx", lambda cfg, p, a: {"enabled": True})

    async def _gate(*a, **k):
        return ("pid1", "cid1")

    calls = {}

    async def _deliver(assistant, platform, account_id, chat_key, text, **kw):
        calls.update(kw, platform=platform, text=text)
        return True

    monkeypatch.setattr(ah, "_voice_gate", _gate)
    monkeypatch.setattr(ah, "_deliver_voice_via_desktop_bridge", _deliver)

    async def _orch_deliver(*a, **k):
        raise AssertionError("桥账号不得走协议直发链")

    monkeypatch.setattr(ah, "_deliver_voice_via_orchestrator", _orch_deliver)
    r = asyncio.run(ah.autosend_voice(_assistant(cfg), "wechat_pc", "wx1", "wx:name:张三", "你好呀"))
    assert r is True
    assert calls["real_pid"] == "pid1" and calls["cid"] == "cid1" and calls["platform"] == "wechat_pc"
    assert calls["bridge_cfg"] == {"enabled": True}


def test_autosend_voice_still_false_when_bridge_not_eligible(monkeypatch):
    cfg = {"voice_autosend": {"enabled": True}, **_bridge_cfg()}

    class _Orch:
        def owns_media(self, platform, account_id):
            return False

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(ah, "_desktop_voice_bridge_ctx", lambda cfg, p, a: None)

    async def _gate(*a, **k):
        raise AssertionError("桥不可行时不该进决策闸（省一次历史查询）")

    monkeypatch.setattr(ah, "_voice_gate", _gate)
    assert asyncio.run(ah.autosend_voice(_assistant(cfg), "wechat_pc", "wx1", "c", "hi")) is False


# ── 桥投递 ───────────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, block_at=None):
        self.calls = []
        self.block_at = block_at

    def enqueue(self, platform, account_id, chat_key, text, **kw):
        self.calls.append({"platform": platform, "account_id": account_id, "chat_key": chat_key,
                           "text": text, **kw})
        if self.block_at is not None and len(self.calls) - 1 == self.block_at:
            return {"enqueued": False, "blocked": "kill_switch:test"}
        return {"enqueued": True, "id": len(self.calls), "status": "held" if kw.get("hold") else "pending"}


def _wire(monkeypatch, tmp_path, *, single_ms, parts_ms=None, forced_parts_ms=None, queue=None):
    """接线所有外部依赖：staging 落 tmp 文件、ffprobe 按文件名给时长、队列用 _Q。"""
    import src.inbox.voice_autosend as va
    import src.inbox.desktop_outbound as do
    import src.integrations.account_registry as _ar
    import src.client.voice_sender as vs
    import src.ai.persona_voice as pv

    dur_by_path = {}
    seq = {"n": 0}

    def _mk(ms, part_text=None, idx=0, total=1):
        seq["n"] += 1
        p = tmp_path / f"v{seq['n']}.ogg"
        p.write_bytes(b"OggS" + b"\0" * 16)
        dur_by_path[str(p)] = ms
        meta = {"provider": "index_tts"}
        if part_text is not None:
            meta.update({"part_text": part_text, "part_index": idx, "parts_total": total})
        return (str(p), f"/static/out/{p.name}", meta)

    state = {"parts_calls": []}

    async def _stage_parts(cfg, platform, account_id, pid, text, *, contact_key=None):
        forced = bool((((cfg.get("inbox") or {}).get("l2_autosend") or {}).get("voice") or {})
                      .get("split_send", {}).get("enabled"))
        state["parts_calls"].append(forced)
        src = forced_parts_ms if forced else parts_ms
        if not src:
            return None
        return [_mk(ms, part_text=f"第{i + 1}段", idx=i, total=len(src)) for i, ms in enumerate(src)]

    async def _stage_file(cfg, platform, account_id, pid, text, *, contact_key=None, out_dir=None):
        if single_ms is None:
            va._set_synth_failure("clone_unreachable")
            return None
        return _mk(single_ms)

    monkeypatch.setattr(va, "stage_voice_parts", _stage_parts)
    monkeypatch.setattr(va, "stage_voice_file", _stage_file)
    monkeypatch.setattr(vs, "probe_audio_duration_ms", lambda path: dur_by_path.get(str(path), 0))
    q = queue or _Q()
    monkeypatch.setattr(do, "get_desktop_outbound_queue", lambda: q)
    monkeypatch.setattr(_ar, "get_account_registry", lambda: SimpleNamespace())
    monkeypatch.setattr(pv, "persona_display_name", lambda pid: "小杰")
    fallbacks = []
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: fallbacks.append(r))
    state.update(q=q, fallbacks=fallbacks, dur_by_path=dur_by_path)
    return state


def _run(assistant, **kw):
    marks = []
    r = asyncio.run(ah._deliver_voice_via_desktop_bridge(
        assistant, "wechat_pc", "wx1", "wx:name:张三", "你好呀，今天过得怎么样",
        vb={"enabled": True}, real_pid="pid1", cid="cid1",
        bridge_cfg=kw.pop("bridge_cfg", {"enabled": True}),
        mark_delivered=marks.append))
    return r, marks


def test_single_clip_enqueued_with_media_fields(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=8200)
    r, marks = _run(_assistant({}))
    assert r is True and marks == ["cid1"]
    assert len(st["q"].calls) == 1
    c = st["q"].calls[0]
    assert c["kind"] == "voice" and c["duration_ms"] == 8200
    assert c["media_ref"].endswith(".ogg") and os.path.isfile(c["media_ref"])
    assert c["media_url"].startswith("/static/")
    assert c["sender_name"] == "小杰" and c["inbox_text"] == c["text"]
    assert c["hold"] is False
    assert c["reply_group"] == "", "单条不成组"
    assert st["fallbacks"] == []


def test_review_mode_holds_voice(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=5000)
    r, _ = _run(_assistant({}), bridge_cfg={"enabled": True, "review_mode": True})
    assert r is True and st["q"].calls[0]["hold"] is True


def test_parts_enqueued_in_order(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=30000, parts_ms=[9000, 11000, 7000])
    r, _ = _run(_assistant({}))
    assert r is True
    assert [c["text"] for c in st["q"].calls] == ["第1段", "第2段", "第3段"]
    assert [c["duration_ms"] for c in st["q"].calls] == [9000, 11000, 7000]
    assert all(c["kind"] == "voice" for c in st["q"].calls)
    groups = {c["reply_group"] for c in st["q"].calls}
    assert len(groups) == 1 and groups.pop().startswith("vg-"), "分条共用一个 reply_group（驱动据此连发）"


def test_too_long_single_forces_split_by_real_duration(monkeypatch, tmp_path):
    # 字数门放过、ffprobe 说 61s → 不能就这么排：强制分条重合成，两条各 30s 通过
    st = _wire(monkeypatch, tmp_path, single_ms=61000, parts_ms=None, forced_parts_ms=[30500, 30000])
    r, _ = _run(_assistant({}))
    assert r is True
    assert st["parts_calls"] == [False, True]        # 第一次常规（关着→None）、第二次强制
    assert len(st["q"].calls) == 2
    assert all(c["duration_ms"] <= 55000 for c in st["q"].calls)
    # 超顶的单条文件已清理
    long_paths = [p for p, ms in st["dur_by_path"].items() if ms == 61000]
    assert long_paths and not os.path.isfile(long_paths[0])


def test_too_long_after_forced_split_falls_back_and_cleans(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=70000, forced_parts_ms=[56000, 20000])
    r, marks = _run(_assistant({}))
    assert r is False and marks == []
    assert st["q"].calls == []
    assert st["fallbacks"] == ["too_long_audio"]
    assert all(not os.path.isfile(p) for p in st["dur_by_path"])


def test_forced_split_unavailable_falls_back(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=60000, forced_parts_ms=None)
    r, _ = _run(_assistant({}))
    assert r is False and st["fallbacks"] == ["too_long_audio"] and st["q"].calls == []


def test_custom_max_seconds_respected(monkeypatch, tmp_path):
    # 运营把硬顶收到 20s：25s 单条 → 强制分条；分条不可用 → 回落
    st = _wire(monkeypatch, tmp_path, single_ms=25000)
    r, _ = _run(_assistant({}), bridge_cfg={"enabled": True, "voice_max_seconds": 20})
    assert r is False and st["fallbacks"] == ["too_long_audio"]


def test_synth_failure_falls_back_with_reason(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=None)
    r, _ = _run(_assistant({}))
    assert r is False and st["fallbacks"] == ["clone_unreachable"] and st["q"].calls == []


def test_first_enqueue_blocked_falls_back_and_cleans(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=6000, queue=_Q(block_at=0))
    r, marks = _run(_assistant({}))
    assert r is False and marks == []
    assert st["fallbacks"] == ["bridge_blocked"]
    assert all(not os.path.isfile(p) for p in st["dur_by_path"])


def test_later_part_blocked_keeps_earlier(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, single_ms=1, parts_ms=[8000, 8000, 8000], queue=_Q(block_at=1))
    r, marks = _run(_assistant({}))
    assert r is True and marks == ["cid1"]
    assert len(st["q"].calls) == 2                 # 第 2 条被拦后停止，不再排第 3 条
    paths = sorted(st["dur_by_path"])
    assert os.path.isfile(paths[0])                # 已排队的保留给驱动读
    assert not os.path.isfile(paths[2])            # 剩余丢弃并清理


def test_burst_guard_noted_per_queued_part(monkeypatch, tmp_path):
    import src.client.voice_burst_guard as vbg
    notes = []
    monkeypatch.setattr(vbg, "note_voice_send", lambda key, cfg: notes.append(key))
    _wire(monkeypatch, tmp_path, single_ms=1, parts_ms=[5000, 5000])
    r, _ = _run(_assistant({}))
    assert r is True and notes == ["cid1", "cid1"]


# ── 配置覆盖 ───────────────────────────────────────────────────────────────────
def test_force_split_cfg_does_not_mutate_original():
    cfg = {"inbox": {"l2_autosend": {"voice": {"enabled": True,
                                                "split_send": {"enabled": False, "part_max_chars": 30}}}}}
    out = ah._force_split_cfg(cfg)
    sp = out["inbox"]["l2_autosend"]["voice"]["split_send"]
    assert sp["enabled"] is True and sp["min_total_chars"] == 1 and sp["part_max_chars"] == 30
    assert cfg["inbox"]["l2_autosend"]["voice"]["split_send"]["enabled"] is False
    assert "min_total_chars" not in cfg["inbox"]["l2_autosend"]["voice"]["split_send"]


def test_force_split_cfg_from_empty():
    out = ah._force_split_cfg({})
    assert out["inbox"]["l2_autosend"]["voice"]["split_send"]["enabled"] is True


def test_force_split_cfg_sizes_parts_from_measured_speech_rate():
    cfg = {"inbox": {"l2_autosend": {"voice": {"split_send": {"part_max_chars": 36, "max_parts": 3,
                                                               "min_tail_chars": 8}}}}}
    # 150 字念了 90s，硬顶 55s：每条最多 150×(55/90)×0.8 ≈ 73 字 → 受常规 36 封顶；条数至少 ceil(150/36)+1=6
    sp = ah._force_split_cfg(cfg, text_len=150, dur_ms=90_000, max_ms=55_000)["inbox"]["l2_autosend"]["voice"]["split_send"]
    assert sp["part_max_chars"] == 36 and sp["max_parts"] == 6 and sp["min_tail_chars"] == 8
    # 慢语速：37 字念了 7.1s，硬顶 3s → 每条 37×(3/7.1)×0.8 ≈ 12 字；尾巴门槛随之缩到 4
    sp2 = ah._force_split_cfg(cfg, text_len=37, dur_ms=7_100, max_ms=3_000)["inbox"]["l2_autosend"]["voice"]["split_send"]
    assert sp2["part_max_chars"] == 12 and sp2["max_parts"] >= 5 and sp2["min_tail_chars"] == 4
    # 没超顶 / 缺实测值 → 不动常规参数
    sp3 = ah._force_split_cfg(cfg, text_len=37, dur_ms=2_000, max_ms=3_000)["inbox"]["l2_autosend"]["voice"]["split_send"]
    assert sp3["part_max_chars"] == 36 and sp3["max_parts"] == 3
    assert cfg["inbox"]["l2_autosend"]["voice"]["split_send"]["part_max_chars"] == 36, "不改原 config"


@pytest.mark.parametrize("raw,expect", [({}, 55.0), ({"voice_max_seconds": 40}, 40.0),
                                        ({"voice_max_seconds": "abc"}, 55.0), ({"voice_max_seconds": 0}, 55.0)])
def test_max_sec_parse(raw, expect):
    assert ah._desktop_voice_max_sec(raw) == expect
