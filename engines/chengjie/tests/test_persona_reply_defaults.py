"""全局回复默认（ai.reply_defaults）门禁 — P0-style，2026-08-02。

守两条铁律：
1. **人设显式值永远优先**，全局默认只在人设沉默时兜底——含最阴的反转路径：
   人设只填 ``max_reply_sentences``（没填 reply_length）时，全局 length 不得
   把人设的句数上限顶掉（precedence 反转＝「运营改全局、个别人设行为突变」事故）。
2. **未装配 config 时行为与旧版逐字节一致**（测试/CLI 场景零波及）。

compact 与 full 两条 prompt 拼装链都要覆盖（生产两种 detail 档都在用）。
"""

from types import SimpleNamespace

from src.utils.persona_manager import (
    PersonaManager,
    explain_reply_style,
    resolve_reply_defaults,
)


def _pm(reply_defaults=None):
    pm = PersonaManager()
    if reply_defaults is not None:
        pm.attach_config_manager(
            SimpleNamespace(config={"ai": {"reply_defaults": reply_defaults}}))
    return pm


def _persona(speaking=None, personality=None):
    p = {"name": "小雅", "role": "私聊陪伴"}
    if speaking is not None:
        p["speaking"] = speaking
    if personality is not None:
        p["personality"] = personality
    return p


# ── resolve_reply_defaults 纯函数 ────────────────────────────────


class TestResolveReplyDefaults:
    def test_missing_or_bad_shapes(self):
        assert resolve_reply_defaults(None) == {}
        assert resolve_reply_defaults({}) == {}
        assert resolve_reply_defaults({"ai": "oops"}) == {}
        assert resolve_reply_defaults({"ai": {"reply_defaults": []}}) == {}

    def test_normalizes_valid_values(self):
        out = resolve_reply_defaults({"ai": {"reply_defaults": {
            "length": " Concise ", "max_sentences": "4",
            "emoji_level": "LOW", "tone_hint": "  多用短句\n别书面腔  "}}})
        assert out["length"] == "concise"
        assert out["max_sentences"] == 4
        # low 是历史别名 → 归一 minimal（与人设侧同表）
        assert out["emoji_level"] == "minimal"
        # 换行折叠成空格
        assert out["tone_hint"] == "多用短句 别书面腔"

    def test_invalid_values_dropped_not_raised(self):
        out = resolve_reply_defaults({"ai": {"reply_defaults": {
            "length": "yolo", "max_sentences": 99,
            "emoji_level": "rainbow", "tone_hint": ""}}})
        assert out == {}

    def test_tone_hint_capped_at_120(self):
        out = resolve_reply_defaults({"ai": {"reply_defaults": {
            "tone_hint": "长" * 500}}})
        assert len(out["tone_hint"]) == 120


# ── 兜底 precedence：full 模式 ───────────────────────────────────


class TestFullModeFallback:
    def test_no_config_attached_no_global_lines(self):
        pm = _pm(None)
        text = pm._format_persona_instructions(_persona())
        assert "全局说话风格要求" not in text
        assert "回复要短" not in text

    def test_silent_persona_gets_global_length(self):
        pm = _pm({"length": "concise"})
        text = pm._format_persona_instructions(_persona())
        assert "回复要短" in text

    def test_explicit_persona_length_beats_global(self):
        pm = _pm({"length": "concise"})
        text = pm._format_persona_instructions(
            _persona(speaking={"reply_length": "detailed"}))
        assert "可以稍详细" in text
        assert "回复要短" not in text

    def test_persona_max_sentences_not_overridden_by_global_length(self):
        # 铁律 1 的反转路径：人设只表态了句数 → 全局 length 必须整体退出
        pm = _pm({"length": "detailed"})
        text = pm._format_persona_instructions(
            _persona(speaking={"max_reply_sentences": 3}))
        assert "不超过 3 句" in text
        assert "可以稍详细" not in text

    def test_silent_persona_gets_global_max_sentences(self):
        pm = _pm({"max_sentences": 4})
        text = pm._format_persona_instructions(_persona())
        assert "不超过 4 句" in text

    def test_global_length_beats_global_max_sentences(self):
        # 与人设内两键并存的既有语义一致：length 占主导
        pm = _pm({"length": "moderate", "max_sentences": 4})
        text = pm._format_persona_instructions(_persona())
        assert "回复均衡" in text
        assert "不超过 4 句" not in text

    def test_emoji_fallback_and_precedence(self):
        pm = _pm({"emoji_level": "minimal"})
        assert "emoji 极少用" in pm._format_persona_instructions(_persona())
        text = pm._format_persona_instructions(
            _persona(personality={"emoji_level": "rich"}))
        assert "emoji 用得多一点" in text
        assert "emoji 极少用" not in text

    def test_tone_hint_applies_to_all_personas(self):
        # tone_hint 是「附加」语义（非兜底）：人设有自己的风格也追加
        pm = _pm({"tone_hint": "多用语气词"})
        text = pm._format_persona_instructions(
            _persona(speaking={"reply_length": "detailed"},
                     personality={"style": "御姐范"}))
        assert "全局说话风格要求：多用语气词" in text


# ── 兜底 precedence：compact 模式 ────────────────────────────────


class TestCompactModeFallback:
    def test_silent_persona_gets_global_length(self):
        pm = _pm({"length": "concise"})
        assert "回复 1-2 句即可" in pm._format_persona_compact(_persona())

    def test_explicit_persona_beats_global(self):
        pm = _pm({"length": "concise"})
        text = pm._format_persona_compact(
            _persona(speaking={"reply_length": "detailed"}))
        assert "回复可稍详细" in text
        assert "回复 1-2 句即可" not in text

    def test_persona_max_sentences_blocks_global_length(self):
        # compact 不消费句数，但人设表过态 → 全局 length 仍须退出（不得矛盾）
        pm = _pm({"length": "detailed"})
        text = pm._format_persona_compact(
            _persona(speaking={"max_reply_sentences": 3}))
        assert "回复可稍详细" not in text

    def test_emoji_and_tone_hint(self):
        pm = _pm({"emoji_level": "none", "tone_hint": "短句为主"})
        text = pm._format_persona_compact(_persona())
        assert "不用 emoji" in text
        assert "全局说话风格要求：短句为主" in text

    def test_no_config_attached_unchanged(self):
        pm = _pm(None)
        text = pm._format_persona_compact(_persona())
        assert "全局说话风格要求" not in text


# ── 溯源 ↔ 行为 一致性（P1 explain 的核心不变量） ────────────────
# 「溯源说全局、prompt 实际用人设」比没有溯源更糟（同 approve_blocked 徽标哲学）。
# 用 explain 的输出**预测** full 格式化器的 prompt 行，逐组交叉断言。

_LEN_LINES = {
    "concise": "回复要短", "short": "回复要短", "brief": "回复要短",
    "moderate": "回复均衡", "balanced": "回复均衡",
    "detailed": "可以稍详细", "long": "可以稍详细",
}
_EMOJI_LINES = {
    "none": "不使用任何 emoji",
    "minimal": "emoji 极少用",
    "moderate": "emoji 偶尔用",
    "rich": "emoji 用得多一点",
    "high": "emoji 用得很多",
}

# (人设 speaking, 人设 personality, 全局 defaults) 的判别矩阵
_CONSISTENCY_CASES = [
    ({}, {}, {}),                                          # 双沉默
    ({}, {}, {"length": "concise"}),                       # 仅全局长度
    ({}, {}, {"max_sentences": 4}),                        # 仅全局句数
    ({}, {}, {"length": "moderate", "max_sentences": 4}),  # 全局两键并存
    ({"reply_length": "detailed"}, {}, {"length": "concise"}),      # 人设长度 vs 全局
    ({"max_reply_sentences": 3}, {}, {"length": "detailed"}),       # 人设句数 vs 全局长度
    ({"reply_length": "concise", "max_reply_sentences": 5}, {}, {}),  # 人设两键并存
    ({}, {"emoji_level": "rich"}, {"emoji_level": "none"}),          # 人设 emoji vs 全局
    ({}, {}, {"emoji_level": "minimal", "tone_hint": "多用短句"}),   # 全局 emoji + 钉子
]


class TestExplainMatchesFormatter:
    def test_explain_predicts_prompt_lines(self):
        for speaking, personality, rd in _CONSISTENCY_CASES:
            pm = _pm(rd if rd else None)
            persona = _persona(
                speaking=speaking or None, personality=personality or None)
            text = pm._format_persona_instructions(persona)
            exp = explain_reply_style(
                pm.normalize_profile_shape(persona),
                resolve_reply_defaults({"ai": {"reply_defaults": rd}}))
            ctx = f"case speaking={speaking} personality={personality} rd={rd}"

            # 长度：explain 给出的生效值 ⇔ 对应行在场，其余长度行缺席
            eff_len = exp["length"]["value"]
            for val, line in _LEN_LINES.items():
                should = eff_len and _LEN_LINES.get(eff_len) == line
                assert (line in text) == bool(should), f"{ctx} 长度行 {val}"

            # 句数：explain 说生效 ⇔ 「单次回复建议不超过 N 句」在场
            ms = exp["max_sentences"]["value"]
            if ms:
                assert f"单次回复建议不超过 {ms} 句" in text, ctx
            else:
                assert "单次回复建议不超过" not in text, ctx

            # emoji：explain 生效值 ⇔ 对应行在场
            eff_emoji = exp["emoji_level"]["value"]
            for val, line in _EMOJI_LINES.items():
                should = eff_emoji and _EMOJI_LINES.get(eff_emoji) == line
                assert (line in text) == bool(should), f"{ctx} emoji 行 {val}"

            # 风格钉子：explain 有值 ⇔ 全局风格行在场
            assert (("全局说话风格要求" in text)
                    == bool(exp["tone_hint"]["value"])), ctx

    def test_explain_source_labels(self):
        # 来源标注抽查：人设句数压过全局长度 → max=persona 且 length 无来源
        exp = explain_reply_style(
            {"speaking": {"max_reply_sentences": 3}},
            {"length": "detailed"})
        assert exp["max_sentences"] == {"value": 3, "source": "persona"}
        assert exp["length"] == {"value": "", "source": ""}
        # 全局两键并存 → 长度主导、句数 suppressed（value 0 无来源）
        exp = explain_reply_style({}, {"length": "moderate", "max_sentences": 4})
        assert exp["length"]["source"] == "global"
        assert exp["max_sentences"]["value"] == 0

    def test_explain_never_raises_on_junk(self):
        assert explain_reply_style(None, None)["length"]["value"] == ""
        assert explain_reply_style("oops", {"length": "concise"})[
            "length"]["source"] == "global"


# ── 覆写计数（回复设置页提示用） ─────────────────────────────────


class TestCountSpeakingOverrides:
    def test_counts_by_formatter_precedence(self):
        pm = _pm(None)
        pm._profile_personas = {
            "a": {"speaking": {"reply_length": "concise"}},
            "b": {"speaking": {"max_reply_sentences": 3}},   # 只填句数也算长度表态
            "c": {"personality": {"emoji_level": "rich"}},
            "d": {"speaking": {}},
            "e": {},
        }
        out = pm.count_speaking_overrides()
        assert out == {"profiles": 5, "length": 2, "emoji": 1}

    def test_empty_store(self):
        assert _pm(None).count_speaking_overrides() == {
            "profiles": 0, "length": 0, "emoji": 0}
