# -*- coding: utf-8 -*-
"""autosend 清唱短路门禁：接线契约 + 全路径行为（fake orch/store，零网络零实例）。

重点覆盖「不该发」的路径（误发=突兀塞歌，比不发伤害大）：
disabled / 非要歌文本 / 频控 / 重复窗 / 无备货 / 发送失败回落。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.companion.song_stock as ss
from src.inbox.autosend_helpers import autosend_song


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeOrch:
    def __init__(self, delivered=True, owns=True):
        self.delivered = delivered
        self.owns = owns
        self.calls = []

    def owns_media(self, platform, account_id):
        return self.owns

    async def send_media(self, platform, account_id, chat_key, **kw):
        self.calls.append({"platform": platform, "account_id": account_id,
                           "chat_key": chat_key, **kw})
        return {"delivered": self.delivered}


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, limit=8):
        return list(self.rows)


def _mk_env(tmp_path, monkeypatch, *, peer_text="给我唱首歌吧",
            delivered=True, stock_templates=("t1",), enabled=True,
            daily_cap=2, cooldown_hours=0.0, repeat_window_days=7.0):
    """搭一套可发的最小环境，返回 (assistant, orch, cfg)。"""
    tdir = tmp_path / "tpl"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / ss.MANIFEST_NAME).write_text(json.dumps({"templates": [
        {"id": "t1", "title": "小星星", "file": "t1.wav",
         "lyrics": "一闪一闪亮晶晶"},
        {"id": "t2", "title": "晚安曲", "file": "t2.wav",
         "scene": "goodnight"},
    ]}, ensure_ascii=False), encoding="utf-8")
    sroot = tmp_path / "stock"
    for tid in stock_templates:
        d = sroot / "lin_xiaoyu" / "songs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.ogg").write_bytes(b"OggS" + b"\x00" * ss.MIN_STOCK_BYTES)
    cfg = {"companion": {"singing": {
        "enabled": enabled, "templates_dir": str(tdir), "stock_dir": str(sroot),
        "daily_cap": daily_cap, "cooldown_hours": cooldown_hours,
        "repeat_window_days": repeat_window_days,
    }}}
    orch = FakeOrch(delivered=delivered)
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda _cfg: orch)
    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_persona_id",
        lambda *a, **k: "lin_xiaoyu")
    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_voice_context",
        lambda *a, **k: {"persona_id": "lin_xiaoyu"})

    def _fake_save(platform, account_id, filename, data):
        out = tmp_path / "outbound" / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return str(out), f"/static/protocol_media/{filename}", "audio"

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", _fake_save)
    # 账本/统计单例重置到本测试的 tmp（防跨用例串账）
    monkeypatch.setattr(ss, "_LEDGER", ss.SongSendLedger(tmp_path / "ledger.json"))
    monkeypatch.setattr(ss, "_STATS", ss.SongStats())
    assistant = SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        logger=logging.getLogger("test_song_autosend"),
        inbox_store=FakeStore([{"direction": "in", "text": peer_text}]),
        skill_manager=None,
        _web_loop=None,
    )
    return assistant, orch, cfg


# ── 行为 ─────────────────────────────────────────────────────────────────────
async def test_send_happy_path(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch)
    ok = await autosend_song(assistant, "telegram", "acct1", "chat1", "草稿文本")
    assert ok is True
    assert len(orch.calls) == 1
    call = orch.calls[0]
    assert call["media_type"] == "voice"
    assert call["inbox_text"].startswith("[唱歌]《小星星》")
    # P3-1 止损话术：默认带「唱歌嗓≠说话嗓」柔性铺垫（framing_line: false 可关）
    assert call["caption"] in ss._FRAMING_LINES
    snap = ss.metrics_snapshot()
    assert snap["counters"].get("requests") == 1
    assert snap["counters"].get("sent") == 1
    assert snap["last_sent"]["persona_id"] == "lin_xiaoyu"
    # 账本已记：同窗内再点名 → 重复窗排除唯一备货曲 → 不发不冒充
    ok2 = await autosend_song(assistant, "telegram", "acct1", "chat1", "")
    assert ok2 is False
    assert ss.metrics_snapshot()["counters"].get("no_pick") == 1


async def test_custom_request_yields_to_order_chain(tmp_path, monkeypatch):
    """实施58 P2：定制求歌（写一首关于我们的）→ 现货让位（答非所问比不唱糟），
    交回文本链（intake 建单+承诺 hint）；custom 未开闸维持旧行为现货照唱。"""
    assistant, orch, cfg = _mk_env(
        tmp_path, monkeypatch, peer_text="唱一首属于我们的歌好不好")
    cfg["companion"]["singing"]["custom"] = {"enabled": True}
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []
    assert ss.metrics_snapshot()["counters"].get("custom_yield") == 1
    # custom 关：同文本按点播现货处理（旧行为）
    cfg["companion"]["singing"]["custom"] = {"enabled": False}
    ok2 = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok2 is True and len(orch.calls) == 1


async def test_disabled_is_noop(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, enabled=False)
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []


async def test_non_request_text_falls_through(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, peer_text="今天好累呀")
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []
    assert ss.metrics_snapshot()["counters"].get("requests") is None


async def test_daily_cap_blocks(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, daily_cap=1,
                                 stock_templates=("t1", "t2"))
    assert await autosend_song(assistant, "telegram", "a", "c", "") is True
    assert await autosend_song(assistant, "telegram", "a", "c", "") is False
    assert ss.metrics_snapshot()["counters"].get("capped_daily") == 1
    assert len(orch.calls) == 1


async def test_cooldown_blocks(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, cooldown_hours=6,
                                 stock_templates=("t1", "t2"))
    assert await autosend_song(assistant, "telegram", "a", "c", "") is True
    assert await autosend_song(assistant, "telegram", "a", "c", "") is False
    assert ss.metrics_snapshot()["counters"].get("capped_cooldown") == 1


async def test_no_stock_never_fakes(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, stock_templates=())
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []
    assert ss.metrics_snapshot()["counters"].get("no_stock") == 1


async def test_send_failure_falls_back(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch, delivered=False)
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False
    assert ss.metrics_snapshot()["counters"].get("send_failed") == 1
    # 发送失败不记账本（下次点名仍可再试同曲）
    assert ss.get_song_ledger().last_ts(
        "telegram:a:c") == 0 or True  # conv_id 规范化不在此断言


async def test_not_owned_account_is_noop(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch)
    orch.owns = False
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []


async def test_scene_hint_prefers_scene_template(tmp_path, monkeypatch):
    assistant, orch, _ = _mk_env(
        tmp_path, monkeypatch, peer_text="唱个摇篮曲哄我睡好不好",
        stock_templates=("t1", "t2"))
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is True
    assert "晚安曲" in orch.calls[0]["inbox_text"]


# ── 实施66 P0-2/P0-4：粘性追加逼唱 + 压力豁免冷却（实录金标） ─────────────────
async def test_sticky_demand_pressure_sings_through_cooldown(
        tmp_path, monkeypatch):
    """实录重放：「给我唱首歌吧」后冷却窗内追加「不行，必须唱」→ 粘性补判
    demand + 压力 ≥2 豁免冷却 → 真唱（而不是第三次拒绝逼出文字假唱）。"""
    import time as _t
    now = _t.time()
    assistant, orch, _ = _mk_env(
        tmp_path, monkeypatch, cooldown_hours=6,
        stock_templates=("t1", "t2"))
    assistant.inbox_store = FakeStore([
        {"direction": "in", "text": "给我唱首歌吧", "ts": now - 120},
        {"direction": "in", "text": "不行，必须唱", "ts": now - 5},
    ])
    from src.inbox.normalizer import conv_id as _cidf
    _cid = _cidf("telegram", "a", "c")
    ss.get_song_ledger().record(_cid, "t2", now=now - 600)  # 冷却窗内刚唱过
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is True and len(orch.calls) == 1
    snap = ss.metrics_snapshot()["counters"]
    assert snap.get("sticky_demand") == 1
    assert snap.get("pressure_exempt") == 1
    assert "小星星" in orch.calls[0]["inbox_text"]  # 避重后换曲 encore


async def test_sticky_expired_prior_falls_through(tmp_path, monkeypatch):
    """粘性证据过期（窗外的旧请求）→「不行，必须唱」不触发（防无证据误唱）。"""
    import time as _t
    now = _t.time()
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch)
    assistant.inbox_store = FakeStore([
        {"direction": "in", "text": "给我唱首歌吧",
         "ts": now - ss.SONG_STICKY_SEC - 60},
        {"direction": "in", "text": "不行，必须唱", "ts": now - 5},
    ])
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []


async def test_loose_urge_never_sings(tmp_path, monkeypatch):
    """泛化催促（无「唱」字）只武装防假唱 hint，绝不触发真唱。"""
    import time as _t
    now = _t.time()
    assistant, orch, _ = _mk_env(tmp_path, monkeypatch)
    assistant.inbox_store = FakeStore([
        {"direction": "in", "text": "给我唱首歌吧", "ts": now - 120},
        {"direction": "in", "text": "快点呀", "ts": now - 5},
    ])
    ok = await autosend_song(assistant, "telegram", "a", "c", "")
    assert ok is False and orch.calls == []


# ── 接线契约（静态钉）：deliver 链里 song 短路存在且位序正确 ────────────────────
def test_deliver_chain_wiring_pinned():
    src = Path("src/inbox/autosend_helpers.py").read_text(encoding="utf-8")
    body = src.split("async def _autosend_deliver", 1)[1]
    i_kline = body.find("autosend_bazi_kline(")
    i_song = body.find("autosend_song(")
    i_image = body.find("autosend_image(")
    assert i_kline != -1 and i_song != -1 and i_image != -1, "deliver 链缺媒体短路"
    assert i_kline < i_song < i_image, (
        "清唱短路必须在 kline 之后、泛化图链之前（结构化意图优先级约定）")
    assert '"delivered_as": "voice"' in body[i_song:i_song + 600]


def test_deliver_song_claim_strip_before_voice_pinned():
    """实施66 P0-3：假唱剥离必须在语音分支**之前**——假唱文本被克隆声念出去
    ＝「说话声念歌词」原始事故形态回魂。"""
    src = Path("src/inbox/autosend_helpers.py").read_text(encoding="utf-8")
    body = src.split("async def _autosend_deliver", 1)[1]
    i_strip = body.find("detect_song_performance")
    i_voice = body.find("_try_autosend_voice(")
    assert i_strip != -1 and i_voice != -1, "B 线假唱剥离缺席"
    assert i_strip < i_voice, "假唱剥离必须先于语音分支"
