# -*- coding: utf-8 -*-
"""O-1 B（#253 #254 · D-O2，2026-09-08）：出站确定性「去 AI 标点 / 句式」。

证据 RYE8Y8（一天 5 条 em dash 跨 3 号）/ 78W8DN（总结句式 + 出站文本不落日志）。
覆盖：每类替换 / 中日文标点不误伤 / 200 样例门禁 0 em dash·0 分号·0 总结尾句·0 反问尾 /
verbatim 与人工手发绕过（item.origin=manual · deferred 原文行反查）/ translate_outbound_text
出口挂点 / [outbound] 日志 / HOLD 透传。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.inbox import outbound_humanize as oh
from src.inbox.outbound_humanize import apply_outbound_humanize, humanize, resolve_cfg

_ROOT = Path(__file__).resolve().parents[1]


def _h(text, lang="en", **cfg):
    c = resolve_cfg({"inbox": {"l2_autosend": {"humanize": cfg}}}) if cfg else None
    return humanize(text, lang, cfg=c)


# ═══════════════════════════════════════════════════════════════════════
# ① 每类替换
# ═══════════════════════════════════════════════════════════════════════

def test_em_dash_to_comma_and_stats():
    out, st = _h("Rainy days are the best — they make everything slow down.")
    assert out == "Rainy days are the best, they make everything slow down."
    assert st["dash"] == 1 and st["punct_fix"] == 1 and st["changed"]


def test_en_dash_and_dash_variants():
    out, _ = _h("I get it – really. Also―this.")
    assert "–" not in out and "―" not in out and "—" not in out
    assert out.startswith("I get it, really.")


def test_dash_between_digits_becomes_hyphen():
    out, _ = _h("Working 9–5 today, 2019—2020 was rough.")
    assert "9-5" in out and "2019-2020" in out


def test_trailing_and_leading_dash_dropped():
    out, _ = _h("Sure thing —")
    assert out == "Sure thing"
    out2, _ = _h("— hi there")
    assert out2 == "hi there"


def test_curly_quotes_straightened_for_latin_only():
    out, st = _h("She said “fine” and I’m okay.")
    assert out == 'She said "fine" and I\'m okay.' and st["quote"] == 3
    zh, st2 = _h("她说“好的”，我就走了。", "zh")
    assert zh == "她说“好的”，我就走了。" and st2["quote"] == 0    # 中文正体引号不动


def test_semicolon_splits_sentence_and_capitalizes():
    out, st = _h("you just breathe; nothing else matters")
    assert out == "you just breathe. Nothing else matters" and st["semicolon"] == 1
    zh, _ = _h("先喘口气；别的都不急。", "zh")
    assert zh == "先喘口气。别的都不急。"


def test_emoticon_semicolon_untouched():
    out, st = _h("Wink ;) you know what I mean")
    assert out == "Wink ;) you know what I mean" and st["semicolon"] == 0


def test_ellipsis_normalized():
    out, _ = _h("honestly… I'd just nap")
    assert out == "honestly... I'd just nap"
    zh, _ = _h("今天也太累了吧……早点休息。", "zh")
    assert zh == "今天也太累了吧...早点休息。"


def test_units_spoken_per_language():
    assert _h("It's 25°C and 80% humidity.")[0] == "It's 25 degrees and 80 percent humidity."
    assert _h("今天大概 30°C，湿度 80%。", "zh")[0] == "今天大概 30度，湿度 80%。"   # 中文 % 保留
    assert _h("Hace 30°C hoy.", "es")[0] == "Hace 30 grados hoy."


def test_multi_exclamation_and_emoji_run_truncated():
    out, st = _h("That sounds exhausting!!! 😫😫😫😫 But hey.")
    assert out == "That sounds exhausting! 😫😫 But hey." and st["exclaim"] == 1 and st["emoji"] == 1
    zh, _ = _h("哈哈哈！！！太好了", "zh")
    assert zh == "哈哈哈！太好了"


def test_bullets_and_numbering_flattened_to_prose():
    out, st = _h("Here’s what I’d do:\n- grab a coffee\n- take a walk\n- call a friend\nThat usually helps.")
    assert out == "Here's what I'd do: grab a coffee, take a walk, call a friend. That usually helps."
    assert st["list"] == 1
    out2, _ = _h("1. Wake up early\n2. Stretch\n3. Drink water")
    assert out2 == "Wake up early, Stretch, Drink water."
    out3, _ = _h("**Plan**\n# Today\nJust rest.")
    assert "**" not in out3 and "#" not in out3


def test_single_bullet_line_left_alone():
    out, st = _h("- just one note here")
    assert out == "- just one note here" and st["list"] == 0


def test_tag_question_tail_removed():
    out, st = _h("Mornings are slow, aren't they?")
    assert out == "Mornings are slow." and st["tag_q"] == 1
    out2, _ = _h("Life is a dance, isn't it?")
    assert out2 == "Life is a dance."
    zh, _ = _h("有时候慢一点也挺好的，对吧？", "zh")
    assert zh == "有时候慢一点也挺好的。"


def test_real_question_kept():
    out, st = _h("Did you sleep ok?")
    assert out == "Did you sleep ok?" and st["tag_q"] == 0


def test_summary_tail_removed_but_never_the_only_sentence():
    out, st = _h("You just breathe. At the end of the day, it's the little things.")
    assert out == "You just breathe." and st["summary"] == 1
    only, st2 = _h("At the end of the day, it's the little things.")
    assert only == "At the end of the day, it's the little things." and st2["summary"] == 0
    zh, _ = _h("你说得对，慢一点也挺好。生活就是这样。", "zh")
    assert zh == "你说得对，慢一点也挺好。"


def test_summary_then_tag_question_pair_removed():
    out, st = _h("Rainy days are the best — they make everything slow down; you just breathe. "
                 "At the end of the day, it's the little things, isn't it?")
    assert out == "Rainy days are the best, they make everything slow down. You just breathe."
    assert st["summary"] == 1 and st["tag_q"] == 1 and st["dash"] == 1 and st["semicolon"] == 1


def test_custom_summary_pattern_via_cfg():
    out, st = _h("Coffee first, then we talk. Onward and upward.", summary_patterns=[r"onward and upward"])
    assert out == "Coffee first, then we talk." and st["summary"] == 1
    # 短句 + 感悟：总结句按原始切句删，剩下的短句照留（不因片段合并漏删）
    out2, st2 = _h("Coffee first. At the end of the day, it's the little things.")
    assert out2 == "Coffee first." and st2["summary"] == 1


def test_max_two_sentences_keeps_trailing_question():
    out, st = _h("I love that you noticed. It's funny how mornings feel different when someone "
                 "says hi first. Anyway, coffee time. What are you up to today?")
    assert out == "I love that you noticed. What are you up to today?" and st["trimmed"] == 2
    out2, st2 = _h("The first sentence is here. The second one is here too. "
                   "The third one is right here. The fourth one closes it.")
    assert out2 == "The first sentence is here. The second one is here too." and st2["trimmed"] == 2


def test_short_fragments_do_not_eat_sentence_budget():
    zh, st = _h("哈哈哈！！！今天也太累了吧……早点休息。晚安呀，明天见！", "zh")
    assert zh == "哈哈哈！今天也太累了吧...早点休息。晚安呀，明天见！" and st["trimmed"] == 0
    en, st2 = _h("Haha yes! Did you sleep ok?")
    assert en == "Haha yes! Did you sleep ok?" and st2["trimmed"] == 0


def test_max_sentences_configurable_and_zero_means_unlimited():
    text = ("Sentence number one is here. Sentence number two is here. "
            "Sentence number three is here. Sentence number four is here.")
    assert _h(text, max_sentences=3)[0] == (
        "Sentence number one is here. Sentence number two is here. Sentence number three is here.")
    assert _h(text, max_sentences=0)[0] == text


def test_punct_only_mode_skips_style():
    out, st = _h("You just breathe — really. At the end of the day, it's the little things, isn't it?")
    assert "—" not in out
    out2, st2 = humanize("You just breathe — really. At the end of the day, it's the little things, isn't it?",
                         "en", mode="punct_only")
    assert out2.endswith("isn't it?") and st2["summary"] == 0 and st2["tag_q"] == 0 and st2["dash"] == 1


# ═══════════════════════════════════════════════════════════════════════
# ② 中日文标点不误伤
# ═══════════════════════════════════════════════════════════════════════

def test_cjk_dash_and_japanese_long_vowel_untouched():
    zh, st = _h("你说得对——有时候慢一点也挺好的。", "zh")
    assert zh == "你说得对，有时候慢一点也挺好的。" and st["dash"] == 1
    ja, st2 = _h("コーヒーとケーキ、いいね。", "ja")            # ー 长音符不是破折号
    assert ja == "コーヒーとケーキ、いいね。" and st2["punct_fix"] == 0
    ja2, _ = _h("そうだね―雨の日はゆっくりできる。", "ja")
    assert ja2 == "そうだね、雨の日はゆっくりできる。"
    ja3, _ = _h("「大丈夫」って言ってたよ。", "ja")
    assert ja3 == "「大丈夫」って言ってたよ。"


def test_language_inferred_from_script_when_lang_missing():
    zh, _ = humanize("你说得对——慢一点也好。", "")
    assert zh == "你说得对，慢一点也好。"
    en, _ = humanize("Sure — why not.", "unknown")
    assert en == "Sure, why not."


def test_thai_dash_becomes_space():
    th, _ = _h("วันนี้ร้อนมาก — ประมาณ 35°C เลย", "th")
    assert th == "วันนี้ร้อนมาก ประมาณ 35 องศา เลย"


def test_clean_text_unchanged_and_idempotent():
    for lang, t in [("en", "Okay, I won't message you again."), ("zh", "好，我不会再打扰你了。"),
                    ("en", "Haha yes! Did you sleep ok?"), ("ja", "わかった、もう連絡しないね。")]:
        out, st = _h(t, lang)
        assert out == t and not st["changed"], (lang, t, out)
        again, st2 = _h(out, lang)
        assert again == out and not st2["changed"]


def test_empty_and_garbage_inputs_are_safe():
    assert humanize("", "en") == ("", humanize("", "en")[1])
    assert humanize(None, "en")[0] == ""      # type: ignore[arg-type]
    out, _ = humanize("👍", "en")
    assert out == "👍"


# ═══════════════════════════════════════════════════════════════════════
# ③ 200 样例门禁（与 scripts/outbound_style_gate.py 同函数）
# ═══════════════════════════════════════════════════════════════════════

def test_gate_200_samples_zero_violations():
    from scripts.outbound_style_gate import build_samples, run_gate, violations
    samples = build_samples(200)
    assert len(samples) >= 200 and {l for l, _ in samples} == {"en", "zh", "ja", "th"}
    # 样例确实脏：每条至少一类 AI 痕迹
    assert all(violations(t) for _, t in samples[:40])
    res = run_gate(samples)
    assert res["total"] >= 200 and res["bad"] == [], res["bad"][:3]


def test_gate_script_main_exit_codes(tmp_path, capsys):
    from scripts.outbound_style_gate import main
    assert main(["--n", "50"]) == 0
    bad = tmp_path / "in.jsonl"
    bad.write_text('{"text": "still here — see", "lang": "en"}\n', encoding="utf-8")
    assert main(["--n", "10", "--input", str(bad)]) == 0      # 经 humanize 后干净
    assert main(["--input", str(tmp_path / "missing.jsonl")]) == 2


# ═══════════════════════════════════════════════════════════════════════
# ④ 绕过 + 日志 + 出口挂点
# ═══════════════════════════════════════════════════════════════════════

def test_apply_bypass_for_manual_and_verbatim_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="src.inbox.outbound_humanize")
    raw = "I wrote this myself — with my own dash; deal with it."
    for org in ("manual", "verbatim", "human"):
        assert apply_outbound_humanize(raw, conversation_id="tg:a:1", origin=org, stage="t") == raw
    auto = apply_outbound_humanize(raw, conversation_id="tg:a:1", origin="auto", stage="t")
    assert "—" not in auto and ";" not in auto
    assert apply_outbound_humanize(None, conversation_id="tg:a:1") is None
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[outbound]")]
    assert len(lines) == 4
    assert any("origin=manual" in ln and "humanize=skip" in ln for ln in lines)
    assert any("origin=auto" in ln and "punct_fix=2" in ln and "dash=1" in ln and "semi=1" in ln
               for ln in lines)
    assert all("conv=tg:a:1" in ln and "len=" in ln and "fp=" in ln for ln in lines)


def test_apply_respects_config_off():
    raw = "Sure — fine."
    off = {"inbox": {"l2_autosend": {"humanize": False}}}
    assert apply_outbound_humanize(raw, conversation_id="c", origin="auto", cfg_root=off) == raw
    on = {"inbox": {"l2_autosend": {"humanize": {"enabled": True}}}}
    assert apply_outbound_humanize(raw, conversation_id="c", origin="auto", cfg_root=on) == "Sure, fine."


def test_resolve_origin_explicit_and_default():
    assert oh.resolve_origin({"origin": "manual"}) == "manual"
    assert oh.resolve_origin({"verbatim": True}) == "verbatim"
    assert oh.resolve_origin({"text": "x"}) == "auto"
    assert oh.resolve_origin(None) == "auto"


def test_deferred_verbatim_probe_with_real_stores(tmp_path):
    """deferred 队列里 pending 的 care:verbatim 原文行 → 反查命中；AI 行 / 已发行 → 不命中。"""
    from src.inbox.store import InboxStore
    from src.integrations.shared.deferred_outbox import DeferredOutboxStore
    oh._reset_probe_for_tests()
    inbox = InboxStore(tmp_path / "inbox.db")
    dq = DeferredOutboxStore(tmp_path / "deferred_outbox.db")
    verb = "到点了 — 记得吃药；别忘了"
    ai = "早安 — 今天也要加油；对吧？"
    rid = dq.enqueue(platform="telegram", account_id="a1", chat_key="u1", reply_text=verb,
                     defer_until=0, reason="care:verbatim", extra={"care": True, "verbatim": True})
    dq.enqueue(platform="telegram", account_id="a1", chat_key="u1", reply_text=ai,
               defer_until=0, reason="care:morning", extra={"care": True, "verbatim": False})
    assert oh.deferred_verbatim_pending(inbox, "telegram:a1:u1", verb) is True
    assert oh.deferred_verbatim_pending(inbox, "telegram:a1:u1", ai) is False
    assert oh.deferred_verbatim_pending(inbox, "telegram:a1:u2", verb) is False
    assert oh.resolve_origin({"conversation_id": "telegram:a1:u1", "text": verb}, inbox) == "verbatim"
    assert oh.resolve_origin({"conversation_id": "telegram:a1:u1", "text": ai}, inbox) == "auto"
    dq.mark_sent(rid)
    assert oh.deferred_verbatim_pending(inbox, "telegram:a1:u1", verb) is False   # 已发 → 不再算
    assert oh.deferred_verbatim_pending(None, "telegram:a1:u1", verb) is False
    oh._reset_probe_for_tests()
    dq.close()
    inbox.close()


class _Res:
    def __init__(self, translated, ok=True, provider="ai", error=""):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error


class _TS:
    def __init__(self, res=None, detect="en"):
        self._res = res
        self._detect = detect
        self.calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        self.calls.append((text, target_lang, source_lang))
        return self._res


class _Store:
    def __init__(self, *, language="en", recent=None):
        self._language = language
        self._recent = list(recent or [])
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self._language, "contact_id": ""}

    def get_outbound_lang_if_set(self, cid):
        return ""

    def list_recent_messages(self, cid, limit=50, **kw):
        return list(self._recent)

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig, kw))
        return True


@pytest.mark.asyncio
async def test_translate_outbound_text_exit_applies_humanize_on_each_path(caplog):
    from src.inbox.outbound_translate import translate_outbound_text
    caplog.set_level(logging.INFO)
    st = _Store(language="en", recent=[{"direction": "in", "text": "hello there my friend how are you", "ts": 1.0}])
    ts = _TS(detect="en")
    # pass_gate_only：无冲突原样放行 → 出口仍去 em dash
    item = {"conversation_id": "tg:a:1", "text": "Rainy days are the best — slow down; breathe."}
    out = await translate_outbound_text(item, translation_service=ts, store=st, gate_only=True)
    assert out == "Rainy days are the best, slow down. Breathe."
    assert item.get("_xlate_target") == "en" and item.get("_xlate_action") == "pass_gate_only"
    # pass_same_lang（完整翻译模式但已是客户语言）
    item2 = {"conversation_id": "tg:a:1", "text": "Sure — fine."}
    assert await translate_outbound_text(item2, translation_service=ts, store=st) == "Sure, fine."
    # translated：译文带 em dash 也被去掉
    ts3 = _TS(res=_Res("Good morning — sleep well?"), detect="zh")
    item3 = {"conversation_id": "tg:a:1", "text": "早安——睡得好吗？"}
    assert await translate_outbound_text(item3, translation_service=ts3, store=st) == "Good morning, sleep well?"
    assert st.recorded and st.recorded[-1][1] == "Good morning — sleep well?"   # 译文映射记原译文
    # 人工手发绕过
    item4 = {"conversation_id": "tg:a:1", "text": "I typed this — myself.", "origin": "manual"}
    assert await translate_outbound_text(item4, translation_service=ts, store=st, gate_only=True) == "I typed this — myself."
    # HOLD 透传
    ts5 = _TS(res=_Res("", ok=False, error="engine_refused"), detect="zh")
    item5 = {"conversation_id": "tg:a:1", "text": "早安——睡得好吗？"}
    assert await translate_outbound_text(item5, translation_service=ts5, store=st) is None
    # 无翻译服务 → 也过后处理（原函数早退路径）
    item6 = {"conversation_id": "tg:a:1", "text": "Hey — you there?"}
    assert await translate_outbound_text(item6, translation_service=None, store=st) == "Hey, you there?"
    outs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[outbound]")]
    assert len(outs) == 5        # HOLD 不记（没有出站）
    assert any("stage=pass_gate_only" in ln for ln in outs) and any("stage=translated" in ln for ln in outs)


def test_worker_human_approved_item_marks_origin_manual():
    src = (_ROOT / "src/inbox/autosend_worker.py").read_text(encoding="utf-8")
    seg = src[src.index("async def deliver_human_approved"):]
    seg = seg[:seg.index("send_cb = self._human_send_callback")]
    assert '"origin": "manual"' in seg
