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


# ── 2026-07-31（198 实锤）：英文不得再被中文刻度拦腰切开 ────────────────────────

def test_weighted_len_latin_discounted():
    from src.inbox.reply_split import weighted_len
    assert weighted_len("你好呀") == 3.0
    assert weighted_len("abcd") == 1.0          # 4 个拉丁字符 ≈ 1 个汉字
    assert weighted_len("你好ab") == 2.5


def test_english_sentence_never_cut_mid_sentence():
    """事故原句：默认 max_chars=60 曾在 58 字符空格处硬切
    （'…kind of an old man' + 'now haha 😄'）。新语义：单句无句界绝不切。"""
    text = "Nice, 35 is a great age. I'm 41 myself, kind of an old man now haha 😄"
    parts = split_reply_parts(text, max_parts=3, max_chars=60,
                              min_total_chars=24, min_tail_chars=4)
    assert parts == [text]              # 加权长度 ~18 单位 < 60 → 整条单发

    text2 = "Flattering me really does get you everywhere with me, you know that?😄"
    parts2 = split_reply_parts(text2, max_parts=3, max_chars=60,
                               min_total_chars=24, min_tail_chars=4)
    assert parts2 == [text2]


def test_english_long_multi_sentence_splits_only_at_sentence_end():
    """超预算英文多句 → 只在句末标点处断，每段都是完整句。"""
    text = ("I only learned to cook after I moved out on my own and honestly it "
            "was not a big deal at all. I love making fusion dishes like black "
            "truffle rice cakes with an Asian twist. What about you, do you "
            "usually cook or order takeout?")
    parts = split_reply_parts(text, max_parts=3, max_chars=30,
                              min_total_chars=0, min_tail_chars=4)
    assert len(parts) >= 2
    for p in parts:
        # 每段必须以句末标点（可带 emoji）收尾——绝无句中断裂
        assert p.rstrip()[-1] in ".!?😄", p
    assert " ".join(parts).split() == text.split()   # 内容零丢失


def test_english_newline_contract_lines_stay_whole():
    """LLM 换行合同：英文行 60-240 字符属正常长度，不得再被句级重切。"""
    l1 = "I'm doing well, thanks for asking and today was quite a busy day! 😊"
    l2 = "Just wrapped up a busy day in New York, finally relaxing right now."
    parts = split_reply_parts(f"{l1}\n{l2}", max_parts=3, max_chars=60,
                              min_total_chars=0, min_tail_chars=4)
    assert parts == [l1, l2]


def test_cjk_overlong_single_sentence_soft_comma_fallback():
    """CJK 主导的超长单句（>1.6×预算）才放开逗号级软切；英文永不逗号切。"""
    text = "今天我们去了很多地方玩得特别开心，先是去了海边看日出，然后又去山上野餐，最后还在老街吃了好多小吃真的太满足了"
    parts = split_reply_parts(text, max_parts=3, max_chars=20,
                              min_total_chars=0, min_tail_chars=4)
    assert len(parts) >= 2
    assert "".join(parts) == text


def test_short_english_reply_stays_single():
    """min_total 门槛按加权长度：英文短回复不再装样子拆条。"""
    text = "Sounds good!\nSee you then 😄"
    parts = split_reply_parts(text, max_parts=3, max_chars=60,
                              min_total_chars=24, min_tail_chars=4)
    assert parts == [text.strip()] or parts == ["Sounds good!\nSee you then 😄"]
