"""inbox.reply_split — 文本短句分条纯函数门禁。"""

from __future__ import annotations

import random

import pytest

from src.inbox.reply_split import (
    DEFAULT_MAX_GAP_SEC,
    DEFAULT_TOTAL_BUDGET_SEC,
    collapse_paragraphs,
    inter_part_delay_sec,
    looks_like_group_chat,
    parse_bubbles_cfg,
    should_split_for_delivery,
    split_reply_parts,
    strip_blank_lines,
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


# ── strip_blank_lines（段落间空行剔除，2026-08-15）──────────────────────────────

def test_strip_blank_lines_single_line_and_empty_unchanged():
    assert strip_blank_lines("就一句话") == "就一句话"
    assert strip_blank_lines("  就一句话  ") == "就一句话"
    assert strip_blank_lines("") == ""
    assert strip_blank_lines(None) == ""


def test_strip_blank_lines_removes_blanks_keeps_line_structure():
    """截图实锤形态：英文三段（空行隔开）→ 三行相邻，换行本身保留（拆条合同）。"""
    text = (
        "Ah, fair point—the name does give it away.\n\n"
        "But trust me, my Mandarin's rusty.\n\n\n"
        "What about you—where are you based?"
    )
    out = strip_blank_lines(text)
    assert "\n\n" not in out
    assert out == (
        "Ah, fair point—the name does give it away.\n"
        "But trust me, my Mandarin's rusty.\n"
        "What about you—where are you based?"
    )


def test_strip_blank_lines_whitespace_only_lines_dropped():
    assert strip_blank_lines("第一行\n   \t \n第二行") == "第一行\n第二行"


def test_strip_blank_lines_no_blanks_untouched():
    text = "哈哈真的假的\n我还以为你忘了呢"
    assert strip_blank_lines(text) == text


def test_parse_bubbles_cfg_defaults_and_clamp():
    cfg = parse_bubbles_cfg({})
    assert cfg["enabled"] is False
    assert cfg["max_parts"] == 3
    assert cfg["orch_only"] is True
    assert cfg["skip_groups"] is True
    assert cfg["holdout_pct"] == 0.0
    # #210 / D-L3（1.0.75）出厂三件：逐句关、仅显式换行才拆、短回复门 80（加权）
    assert cfg["per_sentence"] is False
    assert cfg["explicit_newline_only"] is True
    assert cfg["min_total_chars"] == 80
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
    """无换行的长段 → 句级打包兜底（1.0.75 起需显式关掉 explicit_newline_only）。"""
    text = "今天天气真好呀！我们下午去喝茶吧。你觉得怎么样呢？"
    parts = split_reply_parts(
        text, max_parts=3, max_chars=20, min_total_chars=0, min_tail_chars=0,
        explicit_newline_only=False,
    )
    assert len(parts) >= 2
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    # 出厂默认（仅显式换行才拆）：同一段没有换行 → 整条
    assert split_reply_parts(
        text, max_parts=3, max_chars=20, min_total_chars=0, min_tail_chars=0,
    ) == [text]


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
    # 默认 12.0=5-10s 时代缺省（留呼吸头）；可配放宽；夹界 [1,60]
    assert parse_bubbles_cfg({})["max_gap_sec"] == DEFAULT_MAX_GAP_SEC
    wide = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 20}}}})
    assert wide["max_gap_sec"] == 20.0
    lo = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 0}}}})
    assert lo["max_gap_sec"] == 1.0
    hi = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "max_gap_sec": 999}}}})
    assert hi["max_gap_sec"] == 60.0


def test_inter_part_delay_default_capped_at_max_gap():
    # 默认封顶 DEFAULT_MAX_GAP_SEC（12s）：长文本 × 高打字耗时也不越顶
    d = inter_part_delay_sec(
        "很长的一条中文消息" * 10, gap_sec_lo=2.0, gap_sec_hi=2.0,
        per_char_sec=0.5, rng=None)
    assert d <= DEFAULT_MAX_GAP_SEC


def test_inter_part_delay_max_gap_lets_slow_persona_breathe():
    # 放宽 max_gap_sec 后，慢手速的打字时间不再被默认顶一刀切
    class _R:
        def uniform(self, a, b):
            return a

    text = "六十个字的中文长句" * 6      # 加权长度 54
    fast = inter_part_delay_sec(
        text, gap_sec_lo=2.0, gap_sec_hi=2.0, per_char_sec=0.25,
        rng=_R())
    slow = inter_part_delay_sec(
        text, gap_sec_lo=2.0, gap_sec_hi=2.0, per_char_sec=0.25,
        max_gap_sec=20.0, rng=_R())
    assert fast == DEFAULT_MAX_GAP_SEC   # 被默认顶截断（54*0.25=13.5 > 12）
    assert slow == pytest.approx(2.0 * 0.55 + 54 * 0.25)  # 14.6s，真实打字时长


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
                              min_total_chars=0, min_tail_chars=4,
                              explicit_newline_only=False)
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
                              min_total_chars=0, min_tail_chars=4,
                              explicit_newline_only=False)
    assert len(parts) >= 2
    assert "".join(parts) == text


def test_short_english_reply_stays_single():
    """min_total 门槛按加权长度：英文短回复不再装样子拆条。

    2026-09-06 起单条出口折成自然单段（换行不得漏进一条消息）。"""
    text = "Sounds good!\nSee you then 😄"
    parts = split_reply_parts(text, max_parts=3, max_chars=60,
                              min_total_chars=24, min_tail_chars=4)
    assert parts == ["Sounds good! See you then 😄"]


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
    # 缺省（2026-08-14 起）：拉丁手速有出厂真值——旧 None（沿用加权刻度）把
    # 英文打字耗时低估 ~4 倍＝173「第 2/3 条机关枪」根因；预算键缺省 40s
    # ＝手动链防前端 60s 超时护栏（典型 2 间隔永不触发）
    from src.inbox.reply_split import DEFAULT_LATIN_PER_CHAR_SEC
    d = parse_bubbles_cfg({})
    assert d["latin_per_char_sec"] == pytest.approx(DEFAULT_LATIN_PER_CHAR_SEC)
    assert d["total_budget_sec"] == pytest.approx(DEFAULT_TOTAL_BUDGET_SEC)
    # 非法回落出厂值（不再回 None）；负预算夹到 0（显式 0=关预算仍可表达）
    bad = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "latin_per_char_sec": "x", "total_budget_sec": -5,
    }}}})
    assert bad["latin_per_char_sec"] == pytest.approx(DEFAULT_LATIN_PER_CHAR_SEC)
    assert bad["total_budget_sec"] == 0.0
    # 显式 0（运营明确要旧加权刻度语义的极限值）必须被尊重，不被出厂值顶掉
    zero = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "latin_per_char_sec": 0,
    }}}})
    assert zero["latin_per_char_sec"] == 0.0


def test_default_latin_rate_slows_english_machine_gun():
    """出厂缺省下英文条间隔显著高于旧 None 行为（173 事故的回归钉）。"""
    from src.inbox.reply_split import DEFAULT_LATIN_PER_CHAR_SEC
    d = parse_bubbles_cfg({})
    kw = dict(gap_sec_lo=d["gap_sec_lo"], gap_sec_hi=d["gap_sec_hi"],
              per_char_sec=d["per_char_sec"], max_gap_sec=d["max_gap_sec"])
    # 40 字符英文句（≈8 词）：旧 None → 打字分量 0.3s；新缺省 → 3.2s
    new_gap = inter_part_delay_sec(
        "a" * 40, latin_per_char_sec=d["latin_per_char_sec"],
        rng=_FixedRng(), **kw)
    old_gap = inter_part_delay_sec(
        "a" * 40, latin_per_char_sec=None, rng=_FixedRng(), **kw)
    assert new_gap == pytest.approx(min(
        d["max_gap_sec"],
        d["gap_sec_lo"] * 0.55 + 40 * DEFAULT_LATIN_PER_CHAR_SEC))
    assert new_gap - old_gap > 2.0
    # 纯中文文本几乎不受影响（仅标点/空格按拉丁速率计）
    zh_new = inter_part_delay_sec(
        "今天真的好累呀想早点休息", latin_per_char_sec=d["latin_per_char_sec"],
        rng=_FixedRng(), **kw)
    zh_old = inter_part_delay_sec(
        "今天真的好累呀想早点休息", latin_per_char_sec=None,
        rng=_FixedRng(), **kw)
    assert zh_new == pytest.approx(zh_old)


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


# ── 2026-09-06（#210 / D-L3，82BF95 实锤）：拆条止血——出厂关 / 仅显式换行才拆 /
# 短回复永不拆。客户原话「huge difference between your typing here on TG and
# WhatsApp」：两句英文被逐句拆成 6–15s 固定两拍，是 AI 被识破的直接形态。


_SCEYA_REPLY = ("I guess you just bring out a different side of me here. "
                "But it's still me, just a little more relaxed with you.")


def test_defaults_synced_between_constants_and_parse_cfg():
    from src.inbox.reply_split import (
        DEFAULT_EXPLICIT_NEWLINE_ONLY, DEFAULT_MIN_TOTAL_CHARS,
    )
    d = parse_bubbles_cfg({})
    assert DEFAULT_EXPLICIT_NEWLINE_ONLY is True
    assert DEFAULT_MIN_TOTAL_CHARS == 80
    assert d["explicit_newline_only"] is DEFAULT_EXPLICIT_NEWLINE_ONLY
    assert d["min_total_chars"] == DEFAULT_MIN_TOTAL_CHARS
    # 显式 false（运营明确要回算法档）必须被尊重
    off = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {
        "explicit_newline_only": False}}}})
    assert off["explicit_newline_only"] is False


def test_two_sentence_english_reply_never_split_by_default():
    """事故原句（~105 字符 / 加权 ≈26）：出厂缺省下整条单发。"""
    d = parse_bubbles_cfg({"inbox": {"reply_style": {"bubbles": {"enabled": True}}}})
    parts = split_reply_parts(
        _SCEYA_REPLY,
        max_parts=d["max_parts"], max_chars=d["max_chars"],
        min_tail_chars=d["min_tail_chars"], min_total_chars=d["min_total_chars"],
        per_sentence=d["per_sentence"],
        explicit_newline_only=d["explicit_newline_only"],
    )
    assert parts == [_SCEYA_REPLY]
    # 两句英文 60 字符（验收口径）同样不拆
    short2 = "Sounds good to me. See you tomorrow at the usual place then!"
    assert len(short2) <= 62
    assert split_reply_parts(short2) == [short2]


def test_short_reply_gate_applies_to_per_sentence_too():
    """skuio 机实况：per_sentence=true / max_parts=2——旧版逐句绕过短回复门，
    现在短回复门先于一切模式生效，即便运营显式回到算法档也不再拆两句英文。"""
    parts = split_reply_parts(
        _SCEYA_REPLY, max_parts=2, per_sentence=True,
        explicit_newline_only=False,       # 算法档
    )
    assert parts == [_SCEYA_REPLY]
    # 同一句在旧口径（min_total_chars=24 且逐句）下会被拆成两条——钉住这就是被修掉的行为
    legacy = split_reply_parts(
        _SCEYA_REPLY, max_parts=2, per_sentence=True,
        min_total_chars=24, explicit_newline_only=False,
    )
    assert len(legacy) == 2


def test_explicit_blank_line_splits_when_long_enough():
    """草稿显式空行 → 拆（总长过门槛）；每行原样成条，零字符丢失。"""
    p1 = ("今天下午的会开得比想象中久，一屋子人围着一份预算表来回改了三遍，"
          "散场的时候外面天都黑了，走出大楼那一刻真的松了口气。")
    p2 = ("你那边呢，忙完了没？要是还没吃饭就别再拖了，先去吃点热的，"
          "等你回来再慢慢跟我说今天发生的事。")
    text = f"{p1}\n\n{p2}"
    parts = split_reply_parts(text)
    assert parts == [p1, p2]
    # 单换行（LLM 换行合同）同样算显式换行
    assert split_reply_parts(f"{p1}\n{p2}") == [p1, p2]


def test_long_paragraph_without_newline_stays_single_by_default():
    """没有显式换行的长段（多句、远超门槛）出厂缺省整条；显式关掉才走算法切句。"""
    text = ("今天下午的会开得比想象中久。一屋子人围着一份预算表来回改了三遍。"
            "散场的时候外面天都黑了，走出大楼那一刻真的松了口气。"
            "你那边呢，忙完了没？要是还没吃饭就别再拖了，先去吃点热的。"
            "等你回来再慢慢跟我说今天发生的事吧。")
    assert split_reply_parts(text, max_parts=3) == [text]
    algo = split_reply_parts(text, max_parts=3, explicit_newline_only=False)
    assert len(algo) >= 2 and "".join(algo).replace(" ", "") == text.replace(" ", "")


def test_explicit_mode_keeps_overlong_line_whole():
    """explicit_newline_only 下行就是条：超长行不再被句界重切，条数 == 行数。"""
    long_line = ("这一行故意写得很长很长，里面有好几句话。第一句说完了。第二句也说完了。"
                 "第三句还在继续说，直到远远超过默认的六十字预算才停下来。")
    tail = "然后这是第二行。"
    parts = split_reply_parts(f"{long_line}\n{tail}", max_chars=20,
                              min_total_chars=0, min_tail_chars=0)
    assert parts == [long_line, tail]
    # 算法档下同一输入会把超长行再按句界打包成更多条
    algo = split_reply_parts(f"{long_line}\n{tail}", max_chars=20,
                             min_total_chars=0, min_tail_chars=0,
                             explicit_newline_only=False)
    assert len(algo) > 2


def test_parts_never_carry_embedded_newlines():
    """bubbles 开时拟稿是「每行一句」合同：短回复不拆、或 max_parts 并入末条时，
    多行绝不能原样漏进一条消息（2026-08-08「always 2 parts」形态）——每条都折成
    自然单段，调用方拿到什么就发什么。"""
    # 短回复门挡下的多行草稿 → 单条且无换行
    two_lines = "I guess you just bring out a different side of me here.\nBut it's still me."
    parts = split_reply_parts(two_lines)
    assert parts == ["I guess you just bring out a different side of me here. But it's still me."]
    # CJK 裸边界补「，」（collapse_paragraphs 口径）
    assert split_reply_parts("今天好累\n想你了") == ["今天好累，想你了"]
    # 5 行 × max_parts=3：并入末条的多行（含 >40 字的长行 → 旧版用 \n 连接）也不带换行
    lines = ["今天路过那家咖啡店买了杯燕麦拿铁味道特别好。",
             "顺便帮你也看了下你说的那款蛋糕还有货。",
             "下午的会议临时取消了所以提前回家了，路上看到夕阳特别美还拍了好几张照片想发给你看。",
             "晚点发给你看看你肯定喜欢。",
             "对了周末有空吗，想约你去那家新开的店坐坐。"]
    out = split_reply_parts("\n".join(lines), max_parts=3, min_total_chars=0,
                            min_tail_chars=0)
    assert len(out) == 3
    for p in out:
        assert "\n" not in p, p
    # 句末标点边界直接续接（collapse_paragraphs 口径）→ 内容零丢失
    assert "".join(out) == "".join(lines)


def test_delivery_chains_consume_single_part_result():
    """三链在「拆不出第二条」时必须改发纯函数给的单条（已折叠），否则折叠白做。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src"
    manual = (root / "web/routes/unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "elif _cand and _cand[0] != text:" in manual and "text = _cand[0]" in manual
    bline = (root / "inbox/autosend_helpers.py").read_text(encoding="utf-8")
    assert "_parts = [_split[0]]" in bline
    aline = (root / "client/telegram_client.py").read_text(encoding="utf-8")
    assert "reply_final = _cand_bub[0]" in aline


def test_delivery_chains_pass_explicit_newline_only():
    """三条投递链（手动 send 路由 / B 线 autosend / A 线 telegram_client）调
    split_reply_parts 时必须透传 explicit_newline_only（否则纯函数缺省虽为 True，
    运营显式关掉也不会生效）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src"
    for rel in ("web/routes/unified_inbox_send_routes.py",
                "inbox/autosend_helpers.py",
                "client/telegram_client.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert 'explicit_newline_only=bool(' in src, rel
