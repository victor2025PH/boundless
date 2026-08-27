# -*- coding: utf-8 -*-
"""A 线唱歌兑现短路 + 文字假唱守卫门禁（实施66 P0-1/P0-3；轻量绑定零实例）。

实录金标（2026-08-23 22:50 lin_xiaoyu）：「给我唱首歌吧」被拒（A 线当时无兑现
短路）→「不行，必须唱」逃逸窄词表 → hint 消失 → LLM 四条文本假唱
《月亮代表我的心》。本文件钉：
- Stage S 状态路由：demand 真唱短路 / loose 只禁假唱 / 未开闸·无货 REFUSAL hint
- _deliver_song 双路发送（编排器 → ``_send_voice_to_chat`` 直发缝）+ 账本/观测
- 5c2s 假唱守卫：语境内兑现→剥表演文字（把谎变真）；不能兑现→剥离+台阶句
- 接线静态钉：Stage S 位序 / 5c2s 调用 / 回调注入 / 直发缝风控四件套
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.companion.song_stock as ss

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]).SkillManager

PID = "lin_xiaoyu"
_INCIDENT_REPLY = (
    "哎妈呀，你这是要逼我现眼啊……"
    "行，那我唱了，就一句啊，跑调了不许笑我。"
    "\"月亮代表我的心～\"……"
    "完了，我自己先起鸡皮疙瘩了。")


class _NoOrch:
    def owns_media(self, platform, account_id):
        return False


def _mk_cfg(tmp_path, *, enabled=True, stock_templates=("t1", "t2"),
            cooldown_hours=0.0, daily_cap=5):
    tdir = tmp_path / "tpl"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / ss.MANIFEST_NAME).write_text(json.dumps({"templates": [
        {"id": "t1", "title": "月光", "file": "t1.wav",
         "lyrics": "月亮爬上了窗台"},
        {"id": "t2", "title": "晚风", "file": "t2.wav",
         "lyrics": "晚风轻轻吹过"},
    ]}, ensure_ascii=False), encoding="utf-8")
    sroot = tmp_path / "stock"
    for tid in stock_templates:
        d = sroot / PID / "songs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.ogg").write_bytes(b"OggS" + b"\x00" * ss.MIN_STOCK_BYTES)
    return {"companion": {"singing": {
        "enabled": enabled, "templates_dir": str(tdir),
        "stock_dir": str(sroot), "daily_cap": daily_cap,
        "cooldown_hours": cooldown_hours,
    }}}


def _mk_self(cfg):
    fake = SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        logger=logging.getLogger("test_song_a_line"),
        _record_media_sent=lambda *a, **k: None,
        _stage_lang=lambda uc, t: "zh",
    )
    fake._handle_song_request = (
        lambda *a, **k: _SMcls._handle_song_request(fake, *a, **k))
    fake._deliver_song = (
        lambda *a, **k: _SMcls._deliver_song(fake, *a, **k))
    fake._apply_song_claim_guard = (
        lambda *a, **k: _SMcls._apply_song_claim_guard(fake, *a, **k))
    return fake


@pytest.fixture()
def _iso(tmp_path, monkeypatch):
    """账本/统计隔离 + 编排器不认领（走 _send_voice_to_chat 直发缝）。"""
    monkeypatch.setattr(
        ss, "_LEDGER", ss.SongSendLedger(tmp_path / "ledger.json"))
    monkeypatch.setattr(ss, "_STATS", ss.SongStats())
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda _cfg: _NoOrch())
    return None


def _uc(vsend):
    return {"account_persona_id": PID, "platform": "telegram",
            "account_id": "ai_zkw", "_send_voice_to_chat": vsend}


def _vsend_recorder(sent, ok=True):
    async def _v(chat_id, path, caption, note):
        sent.append({"chat": str(chat_id), "path": str(path),
                     "caption": caption, "note": note})
        return ok
    return _v


# ── Stage S 行为 ─────────────────────────────────────────────────────────────

async def test_stage_s_strict_request_sings(tmp_path, _iso):
    """点名要歌 → A 线直接发真唱段（实施66 之前这里只能拒，事故的第一环）。"""
    fake = _mk_self(_mk_cfg(tmp_path))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    out = await fake._handle_song_request("给我唱首歌吧", "u1", uc, 555)
    assert out == ""                       # 短路：唱段已发出，不补文字
    assert len(sent) == 1
    assert sent[0]["note"].startswith("[唱歌]《")
    assert uc.get("_song_req_count") == 1  # 粘性/压力记账建立
    assert str(uc.get("_stage_media_note") or "").startswith("[语音]")
    c = ss.metrics_snapshot()["counters"]
    assert c.get("a_line_sent") == 1 and c.get("a_requests") == 1


async def test_stage_s_incident_full_replay(tmp_path, _iso):
    """实录全重放：strict 轮真唱（记账）→ 冷却窗内「不行，必须唱」→ 粘性
    demand + 压力豁免冷却 → encore 换曲再唱。整条链上事故不再可能发生。"""
    fake = _mk_self(_mk_cfg(tmp_path, cooldown_hours=6))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    out1 = await fake._handle_song_request("给我唱首歌吧", "u1", uc, 555)
    assert out1 == "" and len(sent) == 1
    out2 = await fake._handle_song_request("不行，必须唱", "u1", uc, 555)
    assert out2 == "" and len(sent) == 2   # 冷却被压力豁免，真唱而非再拒
    assert sent[0]["note"] != sent[1]["note"]  # 避重换曲
    c = ss.metrics_snapshot()["counters"]
    assert c.get("sticky_demand") == 1
    assert c.get("pressure_exempt") == 1
    assert uc.get("_song_req_count") == 2


async def test_stage_s_loose_urge_arms_hint_only(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    ss.touch_song_request(uc)              # 已在要歌粘性窗内
    out = await fake._handle_song_request("快点呀", "u1", uc, 555)
    assert out is None and sent == []      # 不误唱
    assert uc.get("_song_coherence_hint") == ss._HINT_REFUSAL


async def test_stage_s_disabled_arms_refusal_hint(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path, enabled=False))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    out = await fake._handle_song_request("给我唱首歌吧", "u1", uc, 555)
    assert out is None and sent == []
    assert uc.get("_song_coherence_hint") == ss._HINT_REFUSAL
    assert ss.metrics_snapshot()["counters"].get("refusal_hint_a") == 1


async def test_stage_s_no_stock_refuses_honestly(tmp_path, _iso):
    """无备货绝不冒充：不发、注 REFUSAL hint、回落文字链。"""
    fake = _mk_self(_mk_cfg(tmp_path, stock_templates=()))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    out = await fake._handle_song_request("给我唱首歌吧", "u1", uc, 555)
    assert out is None and sent == []
    assert uc.get("_song_coherence_hint") == ss._HINT_REFUSAL
    assert ss.metrics_snapshot()["counters"].get("no_stock") == 1


async def test_stage_s_non_topic_is_silent(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path))
    uc = _uc(_vsend_recorder([]))
    out = await fake._handle_song_request("今天好累呀", "u1", uc, 555)
    assert out is None
    assert "_song_coherence_hint" not in uc
    assert "_song_req_ts" not in uc


async def test_stage_s_send_failure_falls_back(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path))
    sent: list = []
    uc = _uc(_vsend_recorder(sent, ok=False))
    out = await fake._handle_song_request("给我唱首歌吧", "u1", uc, 555)
    assert out is None                     # 发送失败 → 回落文字（hint 已 REFUSAL）
    assert uc.get("_song_coherence_hint") == ss._HINT_REFUSAL
    c = ss.metrics_snapshot()["counters"]
    assert c.get("send_failed") == 1
    # 失败不记账本（下次点名可再试）
    assert not Path(ss.get_song_ledger().path).is_file()


# ── 5c2s 文字假唱守卫 ─────────────────────────────────────────────────────────

async def test_claim_guard_fulfills_and_strips(tmp_path, _iso):
    """语境内 LLM 假唱 → 先发真唱段再剥表演文字（LLM＝词表漏检的兜底检测器）。"""
    fake = _mk_self(_mk_cfg(tmp_path))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    ss.touch_song_request(uc)              # 要歌粘性窗内
    out = await fake._apply_song_claim_guard(
        _INCIDENT_REPLY, uc, chat_id=555, user_text="不行，必须唱")
    assert len(sent) == 1                  # 真唱段发出
    assert "月亮代表我的心" not in out     # 假唱段剥净
    assert "那我唱了" not in out
    assert "现眼" in out                   # 中性句保留
    c = ss.metrics_snapshot()["counters"]
    assert c.get("claim_detected") == 1 and c.get("claim_fulfilled") == 1


async def test_claim_guard_blocks_without_supply(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path, stock_templates=()))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    ss.touch_song_request(uc)
    out = await fake._apply_song_claim_guard(
        _INCIDENT_REPLY, uc, chat_id=555, user_text="不行，必须唱")
    assert sent == []
    assert "月亮代表我的心" not in out
    c = ss.metrics_snapshot()["counters"]
    assert c.get("claim_blocked") == 1


async def test_claim_guard_deflection_when_all_performance(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path, stock_templates=()))
    uc = _uc(_vsend_recorder([]))
    ss.touch_song_request(uc)
    reply = "那我唱了。\"月亮代表我的心～\"……"
    out = await fake._apply_song_claim_guard(
        reply, uc, chat_id=555, user_text="必须唱")
    assert out.strip()                     # 剥空必须换台阶句，绝不空投递
    assert "月亮代表我的心" not in out


async def test_claim_guard_no_context_strips_without_fulfill(tmp_path, _iso):
    """无语境（没人要歌）的强表演体：只剥不兑现（防误发随机唱段）。"""
    fake = _mk_self(_mk_cfg(tmp_path))
    sent: list = []
    uc = _uc(_vsend_recorder(sent))
    out = await fake._apply_song_claim_guard(
        "行，那我唱了。\"月亮代表我的心～\"……", uc,
        chat_id=555, user_text="今天好累呀")
    assert sent == []                      # 未尝试兑现
    assert "月亮代表我的心" not in out
    assert ss.metrics_snapshot()["counters"].get("claim_blocked") == 1


async def test_claim_guard_passthrough_normal_reply(tmp_path, _iso):
    fake = _mk_self(_mk_cfg(tmp_path))
    uc = _uc(_vsend_recorder([]))
    ss.touch_song_request(uc)
    normal = "今天也想你呀，晚饭吃了吗？"
    out = await fake._apply_song_claim_guard(
        normal, uc, chat_id=555, user_text="不行，必须唱")
    assert out == normal                   # 守卫零误伤


# ── 接线静态钉（heat-zone 并行重构哨兵） ─────────────────────────────────────

_SM_PATH = Path(__file__).resolve().parent.parent / "src" / "skills" / "skill_manager.py"
_TC_PATH = Path(__file__).resolve().parent.parent / "src" / "client" / "telegram_client.py"
_SD_PATH = Path(__file__).resolve().parent.parent / "src" / "client" / "sender.py"


def test_stage_s_and_claim_guard_wiring_pinned():
    src = _SM_PATH.read_text(encoding="utf-8", errors="replace")
    i_kline = src.find("await self._handle_bazi_kline_request(")
    i_song = src.find("await self._handle_song_request(")
    i_cd = src.find("self._cooldown_remaining(")
    assert i_kline != -1 and i_song != -1 and i_cd != -1
    assert i_kline < i_song < i_cd, (
        "Stage S 必须在 kline 之后、冷却闸之前（要歌不能被冷却吞掉）")
    assert "await self._apply_song_claim_guard(" in src, "5c2s 假唱守卫未接线"
    i_pg = src.find("self._apply_media_promise_guard(")
    i_scg = src.find("await self._apply_song_claim_guard(")
    assert i_pg != -1 and i_pg < i_scg, "假唱守卫应在媒体承诺守卫之后（5c2 → 5c2s）"


def test_voice_send_callback_injected_pinned():
    src = _TC_PATH.read_text(encoding="utf-8", errors="replace")
    assert "'_send_voice_to_chat': self.send_voice_file" in src, (
        "A 线语音文件直发缝未注入（唱段发不出去）")


def test_send_voice_file_has_guard_suite_pinned():
    """直发缝必须自带风控四件套（护栏/节流/记账/镜像）——绕过=旁路风控缺口。"""
    src = _SD_PATH.read_text(encoding="utf-8", errors="replace")
    body = src.split("async def send_voice_file", 1)
    assert len(body) == 2, "send_voice_file 缺席"
    seg = body[1].split("async def", 1)[0]
    for token in ("_presend_blocked", "_presend_pace",
                  "_postsend_record_count", "_postsend_mirror_and_record"):
        assert token in seg, f"send_voice_file 缺 {token}（风控四件套不齐）"
