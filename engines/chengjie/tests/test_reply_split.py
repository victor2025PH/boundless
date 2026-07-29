"""inbox.reply_split — 文本短句分条纯函数门禁。"""

from __future__ import annotations

import random

from src.inbox.reply_split import (
    inter_part_delay_sec,
    looks_like_group_chat,
    parse_bubbles_cfg,
    should_split_for_delivery,
    split_reply_parts,
)


def test_parse_bubbles_cfg_defaults_and_clamp():
    cfg = parse_bubbles_cfg({})
    assert cfg["enabled"] is False
    assert cfg["max_parts"] == 3
    assert cfg["orch_only"] is True
    assert cfg["skip_groups"] is True
    assert cfg["holdout_pct"] == 0.0
    # holdout 夹界 0..0.5（保留组是测量工具，不允许配成"多数用户拿差体验"）
    hi = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {"holdout_pct": 0.9}}}})
    assert hi["holdout_pct"] == 0.5
    neg = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {"holdout_pct": -1}}}})
    assert neg["holdout_pct"] == 0.0
    # 脏值夹界
    dirty = parse_bubbles_cfg({
        "inbox": {"reply_style": {"bubbles": {
            "enabled": True, "max_parts": 99, "max_chars": 5, "gap_sec_lo": 3,
            "gap_sec_hi": 1,  # hi < lo → 抬到 lo
        }}}
    })
    assert dirty["enabled"] is True
    assert dirty["max_parts"] == 5
    assert dirty["max_chars"] == 20
    assert dirty["gap_sec_hi"] >= dirty["gap_sec_lo"]


def test_split_respects_llm_newlines():
    """陪伴域 prompt 已要求换行=独立消息 → 投递侧必须尊重，不能再句级重切乱序。"""
    text = "哈哈真的假的\n我还以为你忘了呢\n今天天气不错呀"
    parts = split_reply_parts(text, min_total_chars=0, min_tail_chars=0)
    assert parts == ["哈哈真的假的", "我还以为你忘了呢", "今天天气不错呀"]


def test_split_caps_max_parts_merges_tail():
    text = "一\n二\n三\n四\n五"
    parts = split_reply_parts(text, max_parts=3, min_total_chars=0, min_tail_chars=0)
    assert len(parts) == 3
    assert parts[0] == "一" and parts[1] == "二"
    assert "三" in parts[2] and "五" in parts[2]


def test_split_short_text_stays_single():
    assert split_reply_parts("嗯嗯", min_total_chars=24) == ["嗯嗯"]
    assert split_reply_parts("") == []


def test_split_fallback_sentence_pack():
    """无换行的长段 → 句级打包兜底（复用 pack_voice_parts）。"""
    text = "今天天气真好呀！我们下午去喝茶吧。你觉得怎么样呢？"
    parts = split_reply_parts(
        text, max_parts=3, max_chars=20, min_total_chars=0, min_tail_chars=0,
    )
    assert len(parts) >= 2
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_split_preserves_url_atomic():
    text = "看这个链接\nhttps://example.com/path/to/very/long/resource?x=1&y=2"
    parts = split_reply_parts(text, max_chars=30, min_total_chars=0)
    assert any("https://example.com" in p for p in parts)
    # URL 不得被切成两半
    url_parts = [p for p in parts if "http" in p]
    assert len(url_parts) == 1


def test_looks_like_group_chat_telegram():
    assert looks_like_group_chat("telegram", "-1001234567890") is True
    assert looks_like_group_chat("telegram", "8244899900") is False
    assert looks_like_group_chat("whatsapp", "-100123") is False


def test_looks_like_group_chat_whatsapp_jid():
    """WA(baileys) 是编排器路径，群 jid 必须在纯函数层识别，否则群里刷屏。"""
    assert looks_like_group_chat("whatsapp", "120363123456789012@g.us") is True
    assert looks_like_group_chat("whatsapp", "85291234567@s.whatsapp.net") is False


def test_bubble_counters_snapshot():
    from src.inbox.reply_split import bubbles_metrics_snapshot, record_bubble_send
    base = bubbles_metrics_snapshot()
    record_bubble_send("manual", 3)
    record_bubble_send("autosend", 2, partial=True)
    snap = bubbles_metrics_snapshot()
    assert snap["sends"] == base["sends"] + 2
    assert snap["parts_sent"] == base["parts_sent"] + 5
    assert snap["partial"] == base["partial"] + 1
    assert snap["sends_by_source"].get("manual", 0) >= 1


def test_should_split_gates():
    on = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {"enabled": True}}}})
    assert should_split_for_delivery(
        cfg=on, platform="telegram", chat_key="123", orch_owns=True) is True
    # RPA 路径（orch 不拥有）默认不拆——防与 human_pacing 双重切碎
    assert should_split_for_delivery(
        cfg=on, platform="line", chat_key="u1", orch_owns=False) is False
    # 群聊跳过（负数 peer / chat_type=group）
    assert should_split_for_delivery(
        cfg=on, platform="telegram", chat_key="-10099", orch_owns=True) is False
    assert should_split_for_delivery(
        cfg=on, platform="telegram", chat_key="1", orch_owns=True,
        chat_type="group") is False
    off = parse_bubbles_cfg({})
    assert should_split_for_delivery(
        cfg=off, platform="telegram", chat_key="1", orch_owns=True) is False


def test_inter_part_delay_deterministic_with_rng():
    rng = random.Random(0)
    d1 = inter_part_delay_sec("你好呀今天怎么样", gap_sec_lo=1.0, gap_sec_hi=1.0,
                              per_char_sec=0.05, rng=rng)
    # lo==hi → think=1.0*0.55 + len*0.05
    assert 0.5 <= d1 <= 6.0
    rng2 = random.Random(0)
    d2 = inter_part_delay_sec("你好呀今天怎么样", gap_sec_lo=1.0, gap_sec_hi=1.0,
                              per_char_sec=0.05, rng=rng2)
    assert d1 == d2


def test_merge_short_tail_emoji_line():
    """末条过短 emoji 并入前条——对齐语音 min_tail 语义。"""
    text = "今天真不错呀你说是不是\n😂"
    parts = split_reply_parts(
        text, max_parts=3, min_total_chars=0, min_tail_chars=4,
    )
    # emoji 单行长度 1 < 4 → 应并入
    assert len(parts) == 1
    assert "😂" in parts[0]
