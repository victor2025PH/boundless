# -*- coding: utf-8 -*-
"""实施55「聊过即退役」门禁：生活素材会话级账本 + 提及判定 + 消费链。

不变量：
- 账本：记账幂等/落盘轮回（换路径重挂载后从盘上恢复）/空键零操作；
  路径一律经 ``set_ledger_path`` 注入 tmp，**绝不写仓库 config/**；
- ``beat_mentioned``：改写型转述命中（真提及）；通用 bigram（今晚/晚上）
  不误标；不相关回复恒 False；
- ``pick_life_beat(skip_fn=)``：已用素材不再入选；全用完 → None（生活线
  让位给新闻等实时素材）；skip_fn 缺省＝旧行为；
- ``build_life_beat_opener(skip_fn=)``：同口径；
- ``build_deep_persona_block``：deep_ctx.beat_skip_fn 被消费 + 选中素材回写
  ``_life_beat_chosen``（调用方栈进 user_context 供出站提及判定）。
"""
from datetime import datetime

import pytest

from src.companion import life_beat_ledger as lbl
from src.companion.deep_persona import (
    build_deep_persona_block,
    build_life_beat_opener,
    pick_life_beat,
)

NOW = datetime(2026, 8, 22, 15, 0)

PERSONA = {
    "id": "lin_xiaoyu",
    "name": "林小雨",
    "life_arc": {
        "theme": "把自家烧烤店张罗红火",
        "stride_days": 2,
        "beats": [
            "早市抢着一批贼新鲜的羊肉，今晚串儿管够",
            "隔壁桌俩人喝多了差点干起来，我一嗓子给镇住了",
            "拍了条烤串视频播放量破十万，评论区都管我要地址",
        ],
    },
}


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path):
    """每测一个独立落盘路径（换路径自动重置内存态）；测后回 None＝回落
    AITR_DATA_DIR 解析（conftest 已把该 env 指向进程级 tmp，绝不落仓库）。"""
    lbl.set_ledger_path(str(tmp_path / "life_beat_used.json"))
    yield
    lbl.set_ledger_path(None)


# ── 账本 ───────────────────────────────────────────────────────────────────

def test_ledger_record_and_query_idempotent(tmp_path):
    ck = "telegram:acc:123"
    beat = PERSONA["life_arc"]["beats"][0]
    assert not lbl.is_beat_used(ck, beat)
    lbl.record_beat_used(ck, beat)
    lbl.record_beat_used(ck, beat)          # 幂等
    assert lbl.is_beat_used(ck, beat)
    assert lbl.used_count(ck) == 1
    assert not lbl.is_beat_used("telegram:acc:999", beat)   # 会话隔离
    # 空键/空素材零操作
    lbl.record_beat_used("", beat)
    lbl.record_beat_used(ck, "")
    assert not lbl.is_beat_used("", beat)


def test_ledger_persists_across_remount(tmp_path):
    p = tmp_path / "ledger_a.json"
    lbl.set_ledger_path(str(p))
    lbl.record_beat_used("c1", "早市抢着一批贼新鲜的羊肉")
    lbl.set_ledger_path(str(tmp_path / "ledger_b.json"))    # 换路径=清内存
    assert not lbl.is_beat_used("c1", "早市抢着一批贼新鲜的羊肉")
    lbl.set_ledger_path(str(p))                              # 挂回=盘上恢复
    assert lbl.is_beat_used("c1", "早市抢着一批贼新鲜的羊肉")


def test_beat_key_normalizes_noise():
    assert lbl.beat_key(" 早市，抢着！") == lbl.beat_key("早市抢着")
    assert lbl.beat_key("") == ""


# ── 提及判定 ────────────────────────────────────────────────────────────────

def test_beat_mentioned_paraphrase_hits():
    beat = "早市抢着一批贼新鲜的羊肉，今晚串儿管够"
    reply = "哎妈呀今儿早市抢了批羊肉，贼新鲜，晚上来吃串啊"
    assert lbl.beat_mentioned(reply, beat)


def test_beat_mentioned_generic_overlap_not_flagged():
    beat = "早市抢着一批贼新鲜的羊肉，今晚串儿管够"
    assert not lbl.beat_mentioned("今晚不聊了，睡了", beat)   # 只共享「今晚」
    assert not lbl.beat_mentioned("你吃饭了没？", beat)       # 完全无关
    assert not lbl.beat_mentioned("", beat)
    assert not lbl.beat_mentioned("随便说点啥", "")


# ── pick_life_beat / opener 消费 ────────────────────────────────────────────

def test_pick_life_beat_skips_used_and_exhausts_to_none():
    beats = PERSONA["life_arc"]["beats"]
    first = pick_life_beat(PERSONA, NOW)
    assert first in beats
    used = set()

    def _skip(b):
        return b in used

    # 逐条用完：每次选出的都不在已用集里，直到全用完返回 None
    for _ in range(len(beats)):
        b = pick_life_beat(PERSONA, NOW, skip_fn=_skip)
        assert b is not None and b not in used
        used.add(b)
    assert pick_life_beat(PERSONA, NOW, skip_fn=_skip) is None
    # skip_fn 缺省＝旧行为（不受账本影响）
    assert pick_life_beat(PERSONA, NOW) == first
    # skip_fn 自身异常 → 回落原池不抛
    assert pick_life_beat(
        PERSONA, NOW, skip_fn=lambda b: (_ for _ in ()).throw(ValueError())
    ) in beats


def test_life_beat_opener_respects_skip_fn():
    op = build_life_beat_opener(PERSONA, NOW)
    assert op.get("mode") == "life_share" and op.get("fact")
    all_used = build_life_beat_opener(PERSONA, NOW, skip_fn=lambda b: True)
    assert all_used == {}


def test_deep_persona_block_consumes_skip_and_stashes_choice():
    cfg = {"enabled": True, "life_line": True}
    ctx: dict = {}
    block = build_deep_persona_block(PERSONA, now=NOW, cfg=cfg, deep_ctx=ctx)
    assert "你最近的生活" in block
    chosen = str(ctx.get("_life_beat_chosen") or "")
    assert chosen and chosen in block
    # 全部已用 → 生活线整块消失、不回写选中素材
    ctx2: dict = {"beat_skip_fn": lambda b: True}
    block2 = build_deep_persona_block(PERSONA, now=NOW, cfg=cfg, deep_ctx=ctx2)
    assert "你最近的生活" not in block2
    assert "_life_beat_chosen" not in ctx2


def test_convo_key_from_context_formats():
    assert lbl.convo_key_from_context(
        {"conversation_id": "telegram:a:1"}) == "telegram:a:1"
    assert lbl.convo_key_from_context(
        {"platform": "telegram", "account_id": "a", "chat_id": "77"}
    ) == "telegram:a:77"
    assert lbl.convo_key_from_context(
        {"platform": "telegram", "chat_id": "77"}) == "telegram:default:77"
    assert lbl.convo_key_from_context({}) == ""
    assert lbl.convo_key_from_context(None) == ""
