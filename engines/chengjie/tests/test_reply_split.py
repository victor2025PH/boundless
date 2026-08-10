"""inbox.reply_split — 文本短句分条纯函数门禁。"""

from __future__ import annotations

import random

import pytest

from src.inbox.reply_split import (
    collapse_paragraphs,
    inter_part_delay_sec,
    looks_like_group_chat,
    parse_bubbles_cfg,
    should_split_for_delivery,
    split_reply_parts,
)


# ── collapse_paragraphs（单段落收口，2026-08-08）────────────────────────────────

def test_collapse_single_line_and_empty_unchanged():
    assert collapse_paragraphs("就一句话") == "就一句话"
    assert collapse_paragraphs("  就一句话  ") == "就一句话"
    assert collapse_paragraphs("") == ""
    assert collapse_paragraphs(None) == ""


def test_collapse_cjk_bare_boundary_gets_comma():
    """CJK 裸边界补「，」保句读，防「今天好累想你了」跑句。"""
    assert collapse_paragraphs("今天好累\n想你了") == "今天好累，想你了"


def test_collapse_cjk_punct_boundary_joins_direct():
    """前行已带句末标点 → 直接续，不再叠标点。"""
    assert collapse_paragraphs("真的假的？\n我还以为你忘了呢") == "真的假的？我还以为你忘了呢"
    assert collapse_paragraphs("好呀！\n\n那明天见。") == "好呀！那明天见。"


def test_collapse_latin_paragraphs_join_with_space():
    """截图实锤形态：英文两段（空行隔开）→ 一段话、单空格连接。"""
    text = (
        "Haha, I get that a lot—but nah, I'm just a real guy.\n\n"
        "What makes me sound like a robot to you? 😄"
    )
    out = collapse_paragraphs(text)
    assert "\n" not in out
    assert out == (
        "Haha, I get that a lot—but nah, I'm just a real guy. "
        "What makes me sound like a robot to you? 😄"
    )


def test_collapse_emoji_boundary_gets_space():
    out = collapse_paragraphs("哈哈真的假的😂\n你后来咋处理的")
    assert out == "哈哈真的假的😂 你后来咋处理的"
    assert "\n" not in out


def test_collapse_multiline_cjk_mixed():
    out = collapse_paragraphs("嗯嗯\n我在呢\n怎么啦？")
    assert out == "嗯嗯，我在呢，怎么啦？"


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


def test_parse_bubbles_cfg_max_gap_default_and_clamp():
    # 默认 6.0=旧行为锚；可配放宽；夹界 [1,60]
    assert parse_bubbles_cfg({})["max_gap_sec"] == 6.0
    wide = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 20}}}})
    assert wide["max_gap_sec"] == 20.0
    lo = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 0}}}})
    assert lo["max_gap_sec"] == 1.0
    hi = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 999}}}})
    assert hi["max_gap_sec"] == 60.0


def test_inter_part_delay_default_capped_at_six():
    # 默认封顶 6s（行为不变锚）：长文本 × 高打字耗时也不越 6
    d = inter_part_delay_sec(
        "很长的一条中文消息" * 10, gap_sec_lo=2.0, gap_sec_hi=2.0,
        per_char_sec=0.5, rng=None)
    assert d <= 6.0


def test_inter_part_delay_max_gap_lets_slow_persona_breathe():
    # 放宽 max_gap_sec 后，慢手速的打字时间不再被 6s 一刀切
    class _R:
        def uniform(self, a, b):
            return a

    text = "六十个字的中文长句" * 6      # 加权长度 54
    fast = inter_part_delay_sec(
        text, gap_sec_lo=2.0, gap_sec_hi=2.0, per_char_sec=0.15,
        rng=_R())
    slow = inter_part_delay_sec(
        text, gap_sec_lo=2.0, gap_sec_hi=2.0, per_char_sec=0.15,
        max_gap_sec=20.0, rng=_R())
    assert fast == 6.0                   # 旧顶
    assert slow == pytest.approx(2.0 * 0.55 + 54 * 0.15)  # 9.2s，真实打字时长


def test_inter_part_delay_typing_component_weighted_for_latin():
    # 打字分量按加权长度：同字符数英文 lead 应短于 CJK（同一手速刻度）
    class _R:
        def uniform(self, a, b):
            return a

    cjk = inter_part_delay_sec("字" * 40, gap_sec_lo=1.0, gap_sec_hi=1.0,
                               per_char_sec=0.1, max_gap_sec=30.0, rng=_R())
    latin = inter_part_delay_sec("a" * 40, gap_sec_lo=1.0, gap_sec_hi=1.0,
                                 per_char_sec=0.1, max_gap_sec=30.0, rng=_R())
    assert latin < cjk


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


# ── 2026-08-03（198 实锤二）：A 线接线必须认 ConfigManager ──────────────────


def test_parse_bubbles_cfg_rejects_config_manager_like_object():
    """把 ConfigManager（非 dict）整个塞进 parse_bubbles_cfg = 恒拿默认关。

    这就是 A 线分条「配置开了却从不生效」的机制：调用方必须先解引用
    ``manager.config``，本用例把这个坑函数级钉住（防止有人以为传管理器也行）。
    """
    class _FakeCM:
        config = {"inbox": {"reply_style": {"bubbles": {"enabled": True}}}}

    assert parse_bubbles_cfg(_FakeCM())["enabled"] is False
    assert parse_bubbles_cfg(_FakeCM.config)["enabled"] is True


def test_aline_bubble_gate_dereferences_config_manager():
    """A 线（telegram_client）分条调用点的配置取法回归钉。

    TelegramClient.config 是 ConfigManager 对象（main.py 与
    telegram_companion_worker 传入的都是管理器，不是 dict）。P1.5 首版写
    ``self.config if isinstance(self.config, dict) else {}`` → isinstance 恒
    False → 恒拿空配置 → A 线分条自上线起从未生效（2026-08-03 198 坐席机
    「两段话合一条发出」实锤）。修复后取法须对齐本类其它取点：
    ``self.config.config`` 属性优先，dict 仅作测试兜底。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "client" / "telegram_client.py").read_text(encoding="utf-8")
    i = src.find("_pbc_bub(")
    assert i > 0, "A 线分条接线（_pbc_bub 调用点）不见了——若重构请同步迁移本门禁"
    window = src[i:i + 400]
    assert "self.config.config" in window, (
        "A 线 parse_bubbles_cfg 必须先解引用 ConfigManager.config；"
        "self.config 是管理器对象不是 dict，isinstance dict 判断恒拿空配置"
    )


# ── 2026-08-09：拉丁真实手速（typing_time_sec / latin_per_char_sec）────────────
# 加权刻度（0.25 权）是给「分条预算」校准的，挪用到「打字耗时」会把英文手速
# 虚高 ~4 倍（60 字符英文只算 1.2s，真人 40-60wpm 要 6s+）——英文会话条间
# 机关枪的根因。latin_per_char_sec 显式给出时英文按真实手速计；缺省=旧行为锚。


class _FixedRng:
    """uniform 恒返下界：消除 think 分量随机，纯测打字分量。"""

    def uniform(self, a, b):
        return a


def test_typing_time_default_matches_weighted_len():
    from src.inbox.reply_split import typing_time_sec, weighted_len
    txt = "Hello 你好 world 123"
    assert typing_time_sec(txt, per_char_sec=0.1) == pytest.approx(
        weighted_len(txt) * 0.1)


def test_typing_time_latin_rate_only_scales_non_cjk():
    from src.inbox.reply_split import typing_time_sec
    cjk_only = "你好呀" * 5
    assert typing_time_sec(
        cjk_only, per_char_sec=0.1, latin_per_char_sec=0.5,
    ) == pytest.approx(typing_time_sec(cjk_only, per_char_sec=0.1))
    latin_only = "a" * 40
    assert typing_time_sec(
        latin_only, per_char_sec=0.1, latin_per_char_sec=0.06,
    ) == pytest.approx(40 * 0.06)


def test_typing_time_bad_latin_rate_falls_back_weighted():
    from src.inbox.reply_split import typing_time_sec
    assert typing_time_sec(
        "a" * 40, per_char_sec=0.1, latin_per_char_sec="junk",
    ) == pytest.approx(40 * 0.25 * 0.1)


def test_inter_part_delay_latin_rate_slows_english_gap():
    kw = dict(gap_sec_lo=1.0, gap_sec_hi=1.0, per_char_sec=0.1,
              max_gap_sec=30.0)
    legacy = inter_part_delay_sec("a" * 40, rng=_FixedRng(), **kw)
    real = inter_part_delay_sec("a" * 40, latin_per_char_sec=0.06,
                                rng=_FixedRng(), **kw)
    assert legacy == pytest.approx(max(1.0, 1.0 * 0.55 + 40 * 0.25 * 0.1))
    assert real == pytest.approx(1.0 * 0.55 + 40 * 0.06)
    assert real > legacy


def test_parse_bubbles_cfg_new_pacing_keys():
    cfg = {"inbox": {"reply_style": {"bubbles": {
        "enabled": True, "latin_per_char_sec": 0.06, "total_budget_sec": 75,
    }}}}
    b = parse_bubbles_cfg(cfg)
    assert b["latin_per_char_sec"] == pytest.approx(0.06)
    assert b["total_budget_sec"] == pytest.approx(75.0)
    # 缺省=None/0（零行为变更锚）
    d = parse_bubbles_cfg({})
    assert d["latin_per_char_sec"] is None
    assert d["total_budget_sec"] == 0.0
    # 非法/越界回落
    bad = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "latin_per_char_sec": "x", "total_budget_sec": -5,
    }}}})
    assert bad["latin_per_char_sec"] is None
    assert bad["total_budget_sec"] == 0.0


# ── 2026-08-09：plan_bubble_gaps（条间隔预排 + 序列总预算等比压缩）──────────────


def test_plan_gaps_no_budget_matches_individual_model():
    from src.inbox.reply_split import plan_bubble_gaps
    parts = ["第一句话在这里。", "第二句话稍微长一点点。", "第三句。"]
    kw = dict(gap_sec_lo=1.0, gap_sec_hi=1.0, per_char_sec=0.1,
              max_gap_sec=30.0)
    gaps = plan_bubble_gaps(parts, rng=random.Random(7), **kw)
    rng2 = random.Random(7)   # 与 plan 内部同为「单一 rng 顺序消费」
    expect = [inter_part_delay_sec(p, rng=rng2, **kw) for p in parts[1:]]
    assert gaps == pytest.approx(expect)
    assert len(gaps) == len(parts) - 1


def test_plan_gaps_budget_scales_proportionally_keeps_shape():
    from src.inbox.reply_split import plan_bubble_gaps
    parts = ["头。", "中文长句" * 15, "短句。", "又一条中文长句" * 12, "尾。"]
    kw = dict(gap_sec_lo=2.0, gap_sec_hi=6.0, per_char_sec=0.1,
              max_gap_sec=20.0)
    free = plan_bubble_gaps(parts, rng=_FixedRng(), **kw)
    assert sum(free) > 10.0
    capped = plan_bubble_gaps(parts, total_budget_sec=10.0,
                              rng=_FixedRng(), **kw)
    assert sum(capped) <= 10.0 + 1e-6
    # 等比例：节奏形状保持（长句间隔仍相对更长）
    order_free = sorted(range(len(free)), key=lambda i: free[i])
    order_capped = sorted(range(len(capped)), key=lambda i: capped[i])
    assert order_free == order_capped
    assert all(g >= 0.6 - 1e-9 for g in capped)


def test_plan_gaps_floor_wins_over_budget():
    # 预算逼到地板以下时地板胜出（反机关枪优先，预算是软约束）
    from src.inbox.reply_split import plan_bubble_gaps
    parts = ["a", "bb", "cc", "dd", "ee"]
    gaps = plan_bubble_gaps(parts, gap_sec_lo=3.0, gap_sec_hi=3.0,
                            per_char_sec=0.0, max_gap_sec=20.0,
                            total_budget_sec=0.1, rng=_FixedRng())
    assert all(g == pytest.approx(0.6) for g in gaps)


def test_plan_gaps_short_lists():
    from src.inbox.reply_split import plan_bubble_gaps
    assert plan_bubble_gaps([]) == []
    assert plan_bubble_gaps(["只有一条"]) == []
