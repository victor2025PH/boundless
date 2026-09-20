# -*- coding: utf-8 -*-
"""识图抢跑修复回归（2026-08-23「把人看成猫」事故，P0）。

事故链（网关流水实锤）：客户 03:11 发人像照片 → 03:12 拟稿在 media_ref/文件
就绪前抢跑，prompt 只有「[图片]」占位（ref 空时连降级分支都进不去）→ LLM 自由
发挥「哈哈，这图是你家猫吗」→ 识图 03:13 才被下一轮补上。

三层修复，对应三组用例：
① 拟稿等图（media_wait_sec）：等 ref 出现/文件可解析/并行链路回写的描述，
   等到才生成；CDN 引用不等（旧语义）；同图去重锁防双调 VLM；
② 降级/扣留分支不再要求 media_ref 非空——看得见 media_type=图 就绝不裸拟；
③ 盲断言闸门：无描述图片轮的回复断言/猜测/夸赞画面 → 整稿换诚实追问
   （诚实标记句放行；有描述轮不干预）。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.inbox import media_enrich as me
from src.inbox import autodraft_helpers as adh
from src.inbox.autodraft_helpers import enrich_auto_draft
from src.inbox.media_enrich import (
    blind_image_assertion,
    honest_image_ask,
    media_wait_sec_from_cfg,
    try_mark_desc_inflight,
)


# ---------------------------------------------------------------- 纯函数层
def test_media_wait_sec_default_and_clamp():
    assert media_wait_sec_from_cfg({}) == 20.0
    assert media_wait_sec_from_cfg(None) == 20.0
    cfg = {"inbox": {"auto_draft": {"media_wait_sec": 5}}}
    assert media_wait_sec_from_cfg(cfg) == 5.0
    assert media_wait_sec_from_cfg(
        {"inbox": {"auto_draft": {"media_wait_sec": 999}}}) == 60.0
    assert media_wait_sec_from_cfg(
        {"inbox": {"auto_draft": {"media_wait_sec": 0}}}) == 0.0
    assert media_wait_sec_from_cfg(
        {"inbox": {"auto_draft": {"media_wait_sec": -3}}}) == 0.0
    assert media_wait_sec_from_cfg(
        {"inbox": {"auto_draft": {"media_wait_sec": "abc"}}}) == 20.0


def test_desc_inflight_mark_ttl_and_clear(monkeypatch):
    assert try_mark_desc_inflight("c1|r1") is True
    assert try_mark_desc_inflight("c1|r1") is False   # 占坑期内后来者让行
    assert try_mark_desc_inflight("c1|r2") is True    # 不同媒体互不影响
    me.clear_desc_inflight("c1|r1")
    assert try_mark_desc_inflight("c1|r1") is True    # 清坑后可重占
    # TTL 过期自动可重占（异常路径漏清的兜底）
    me._DESC_INFLIGHT["c1|r3"] = 1.0
    assert try_mark_desc_inflight("c1|r3") is True


@pytest.mark.parametrize("text", [
    "哈哈，这图是你家猫吗？深夜这么精神。",   # 事故原文
    "这张照片里是海边吧，真不错",
    "照片里的小姐姐好漂亮",
    "看到你发的照片啦，气质真好",
    "拍得真好看，构图很棒",
    "Nice pic! Where was that?",
    "Is this your cat?",
    "That photo looks amazing",
])
def test_blind_assertion_catches_content_claims(text):
    assert blind_image_assertion(text) is True


@pytest.mark.parametrize("text", [
    # 合规降级话术（诚实标记）必须放行
    "咦，这张图我这边加载不出来，你说说拍的是啥？",
    "图我看不清，再发一次呗？",
    "The image didn't load on my end — what is it?",
    # 普通文本回复不误伤
    "哈哈你也太逗了",
    "好呀，明天见！",
    "收到啦，稍等我看看哈",
    "",
])
def test_blind_assertion_spares_honest_and_plain(text):
    assert blind_image_assertion(text) is False


def test_honest_image_ask_lang_pick_and_deterministic():
    zh = honest_image_ask("zh", seed="conv-1")
    assert zh == honest_image_ask("zh", seed="conv-1")   # 同会话恒定
    assert any("\u4e00" <= ch <= "\u9fff" for ch in zh)
    en = honest_image_ask("en", seed="conv-1")
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in en)
    # 非 zh 一律走 en 池（出站翻译层再对齐客户语言）
    assert honest_image_ask("ja", seed="x") == honest_image_ask("en", seed="x")


# ------------------------------------------------- enrich_auto_draft 集成层
def _row(direction, text="", media_type="", media_ref="", mid="", ts=0):
    return {
        "direction": direction, "text": text, "media_type": media_type,
        "media_ref": media_ref, "message_id": mid, "ts": ts,
    }


class _MutStore:
    """快照序列 store：第 N 次 list_recent_messages 给第 N 份快照（末份复读），
    模拟「边车先落行、后补 media_ref / 并行链路回写描述」的时间线。"""

    def __init__(self, snapshots):
        self._snaps = [list(s) for s in snapshots]
        self.reads = 0
        self.updates = []
        self.draft_status = []

    def list_recent_messages(self, cid, limit=30):
        snap = self._snaps[min(self.reads, len(self._snaps) - 1)]
        self.reads += 1
        return [dict(r) for r in snap]

    def update_message_text(self, cid, message_id="", media_ref="",
                            text="", only_if_empty=False):
        self.updates.append({"message_id": message_id, "media_ref": media_ref,
                             "text": text})
        return True

    def update_draft_status(self, draft_id, status="", decided_by="",
                            expected_statuses=()):
        self.draft_status.append({"draft_id": draft_id, "status": status,
                                  "decided_by": decided_by})
        return True


class _Cfg:
    def __init__(self, data=None):
        self.config = data or {}

    def get(self, key, default=None):
        return self.config.get(key, default)


def _assistant(cfg=None):
    a = MagicMock()
    a.config = _Cfg(cfg)
    return a


def _drive(snapshots, *, cfg=None, image_desc=None, gen_reply="好可爱",
           path_map=None, text="hi", tick=0.01):
    """驱动 enrich_auto_draft：返回 (生成引擎捕获 kwargs, store, draft_svc)。"""
    store = _MutStore(snapshots)
    draft_svc = MagicMock()
    draft_svc.get_draft.return_value = {}
    draft_svc.enrich_draft.return_value = True
    app = MagicMock()
    tc = MagicMock()
    tc._get_image_content = AsyncMock(return_value=image_desc)
    tc.voice_transcriber = None
    app.state.telegram_client = tc
    captured = {}

    async def _fake_gen(**kw):
        captured.update(kw)
        return {"ok": True, "reply": gen_reply, "reply_lang": "zh"}

    def _fake_path(ref):
        if path_map is None:
            return "C:/tmp/fake.jpg"
        return path_map.get(ref)

    conv = {"conversation_id": "tg:acc:peer9", "platform": "telegram",
            "chat_key": "peer9", "account_id": "acc"}
    bf = AsyncMock(return_value=("", ""))
    with patch("src.inbox.persona_reply.generate_persona_reply", _fake_gen), \
         patch("src.integrations.protocol_bridge.static_media_ref_to_path",
               _fake_path), \
         patch("src.inbox.media_enrich.enrich_inbound_media_text", bf), \
         patch.object(adh, "_MEDIA_WAIT_TICK_SEC", tick):
        asyncio.run(enrich_auto_draft(
            _assistant(cfg), draft_svc, app, store, conv, text, "d1",
            "review"))
    return captured, store, draft_svc


def _wait_cfg(sec, degrade=True):
    return {"inbox": {"auto_draft": {
        "media_wait_sec": sec, "media_degrade_reply": degrade}}}


def test_late_media_ref_waits_then_describes():
    """事故形态①：图片行先落库（ref 空）→ 等待环重读拿到迟到的 ref → 识别成功。"""
    r0 = _row("in", "", media_type="image", media_ref="", mid="m1", ts=100)
    r1 = dict(r0, media_ref="/static/late.jpg")
    kw, store, _svc = _drive(
        [[r0], [r1]], cfg=_wait_cfg(2), image_desc="一位穿格子衬衫的女士")
    assert kw["last_inbound"] == "[图片内容] 一位穿格子衬衫的女士"
    assert kw["media_desc"] == "一位穿格子衬衫的女士"
    assert store.reads >= 2                      # 真的等 + 重读了
    assert any(u["text"].startswith("[图片内容]") for u in store.updates)


def test_parallel_writeback_desc_picked_up_without_describe():
    """等待环从消息行读到并行链路回写的「[图片内容] …」→ 不再调 VLM，直接采用。"""
    r0 = _row("in", "", media_type="image", media_ref="", mid="m1", ts=100)
    r1 = dict(r0, text="[图片内容] 一张度假村门口的合影")
    kw, store, _svc = _drive(
        [[r0], [r1]], cfg=_wait_cfg(2), image_desc="不该被调用")
    assert kw["media_desc"] == "一张度假村门口的合影"
    assert kw["last_inbound"] == "[图片内容] 一张度假村门口的合影"
    assert not store.updates                     # 别人回写的，不重复回写


def test_wait_timeout_degrade_on_blind_reply_rewritten():
    """等不到（ref 永远空）+ 降级开 + 模型盲断言 → 整稿换诚实追问。"""
    r0 = _row("in", "", media_type="image", media_ref="", mid="m1", ts=100)
    kw, _store, svc = _drive(
        [[r0]], cfg=_wait_cfg(0.05),
        gen_reply="哈哈，这图是你家猫吗？深夜这么精神。")
    assert kw["last_inbound"] == "[图片]"        # 降级放行（占位喂产线）
    sent = svc.enrich_draft.call_args.kwargs["reply_text"]
    assert "你家猫" not in sent
    assert sent in me._HONEST_ASK_POOL["zh"]


def test_wait_timeout_degrade_on_honest_reply_kept():
    """等不到 + 降级开 + 模型合规（诚实追问）→ 原稿放行不改写。"""
    r0 = _row("in", "", media_type="image", media_ref="", mid="m1", ts=100)
    _kw, _store, svc = _drive(
        [[r0]], cfg=_wait_cfg(0.05),
        gen_reply="图我这边看不清呢，你拍的是什么呀？")
    sent = svc.enrich_draft.call_args.kwargs["reply_text"]
    assert sent == "图我这边看不清呢，你拍的是什么呀？"


def test_wait_timeout_degrade_off_holds_draft():
    """等不到 + 降级关（服务器默认）→ 08-17 无兜底纪律：vision_hold 取消不生成。

    修复前该形态（ref 空）两个分支都进不去 → 裸生成盲猜——本例钉死不回归。"""
    r0 = _row("in", "", media_type="image", media_ref="", mid="m1", ts=100)
    kw, store, _svc = _drive(
        [[r0]], cfg={"inbox": {"auto_draft": {"media_wait_sec": 0.05}}})
    assert kw == {}                              # 生成引擎未被调用
    assert any(s["decided_by"] == "vision_hold" for s in store.draft_status)


def test_remote_cdn_ref_does_not_wait():
    """http(s) CDN 引用维持旧语义：本层不下载不等待，立刻走降级/扣留。"""
    r0 = _row("in", "", media_type="image",
              media_ref="https://cdn.example.com/x.jpg", mid="m1", ts=100)
    kw, store, _svc = _drive(
        [[r0]], cfg=_wait_cfg(5), path_map={},   # 解析不到本地路径
        gen_reply="收到！")
    assert kw["last_inbound"] == "[图片]"
    # 没进等待重读（5s 预算 × 0.01 tick 会是几百次）。Q-21 起草前 lang-plan 也会读 store，
    # 所以不再钉死 1；只要不是等待环即可。
    assert 1 <= store.reads < 20


def test_inflight_lock_second_round_polls_not_describes():
    """同图去重：锁被占（另一拟稿轮识别中）→ 本轮只轮询等回写，绝不双调 VLM。"""
    ref = "/static/dup.jpg"
    assert try_mark_desc_inflight(f"tg:acc:peer9|{ref}") is True   # 模拟他轮占坑
    r0 = _row("in", "", media_type="image", media_ref=ref, mid="m1", ts=100)
    r1 = dict(r0, text="[图片内容] 他轮识别的结果")
    kw, _store, _svc = _drive(
        [[r0], [r1]], cfg=_wait_cfg(2), image_desc="不该被调用")
    assert kw["media_desc"] == "他轮识别的结果"


# ------------------------------------------- 共享识别层 wait_file_sec
def test_enrich_inbound_media_text_waits_for_file(monkeypatch):
    calls = {"n": 0}

    def _resolve(ref):
        calls["n"] += 1
        return None if calls["n"] < 3 else "C:/tmp/ready.jpg"

    monkeypatch.setattr(me, "_resolve_local_path", _resolve)
    monkeypatch.setattr(me, "_WAIT_TICK_SEC", 0.01)
    monkeypatch.setattr(
        me, "_describe_image", AsyncMock(return_value="一只柴犬"))

    text, desc = asyncio.run(me.enrich_inbound_media_text(
        media_type="image", media_ref="/static/slow.jpg",
        config={"vision": {"enabled": True}}, wait_file_sec=2))
    assert desc == "一只柴犬"
    assert calls["n"] >= 3                       # 真的等了文件就绪


def test_enrich_inbound_media_text_no_wait_by_default(monkeypatch):
    calls = {"n": 0}

    def _resolve(ref):
        calls["n"] += 1
        return None

    monkeypatch.setattr(me, "_resolve_local_path", _resolve)
    text, desc = asyncio.run(me.enrich_inbound_media_text(
        media_type="image", media_ref="/static/x.jpg", config={}))
    assert desc == ""
    assert calls["n"] == 1                       # 旧行为：不等待
