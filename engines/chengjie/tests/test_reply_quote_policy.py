"""AI 自动链引用回复决策——#37 基线（I-4 D2）+ O-4 规则引擎（#254，2026-09-08）。

验收（指令 O-4 §A/B + I-4 §4 沿用）：
A1 连发 ≥2 条 → 引用最相关那条（金标句对）reason=burst
A2 所回为旧消息（>30 min）→ 必引 reason=stale
A3 10–15% 随机基线：会话级种子确定、可复现；全体会话命中率落在区间附近
A4 禁引：2 min 内单条短答不引 / 连引 ≤2 后至少 2 条不引 / 每会话每小时 ≤6 /
   引自己消息仅「补充上一条」≤5%
B1 能力位：Telegram / WhatsApp 可引；LINE / Messenger → rule=unsupported 落日志、普通发送
B2 带引用发送抛异常 → 去引用重发一次（不丢消息）
C  [quote] 日志行格式 conv= reply_to_mid= reason= rule=
D  开关默认开（parse_quote_cfg / autosend 未配置即引用）
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.inbox import reply_quote_policy as rqp
from src.inbox.reply_quote_policy import (
    decide_quote, parse_quote_cfg, platform_allows_quote, relevance_score,
    unanswered_burst, is_question, seeded_unit, session_rate, streak_blocked,
)

NOW = 1_800_000_000.0


def _m(direction, text, ts, pmid="", **kw):
    row = {"direction": direction, "text": text, "ts": ts,
           "platform_msg_id": pmid, "deleted_at": 0, "translated_text": "",
           "reply_to_id": "", "reply_to_text": ""}
    row.update(kw)
    return row


def _out_q(text, ts, pmid, quoted):
    """出站行：quoted=True 模拟编排器按 quote_applied 回执镜像进来的 reply_to_id。"""
    return _m("out", text, ts, pmid, reply_to_id=("q" if quoted else ""))


def _hit_key(prefix, mid, *, want_hit, cfg=None):
    """找一个会话 id，使 (会话, 目标 mid) 的掷骰 命中/不命中 随机基线——确定性搜索。"""
    c = parse_quote_cfg(cfg or {})
    for i in range(10_000):
        k = f"{prefix}{i}"
        hit = seeded_unit(k, "roll", mid) < session_rate(k, c)
        if hit == want_hit:
            return k
    raise AssertionError("no key found")


# ── 配置 / 默认开 ────────────────────────────────────────────────────────

def test_parse_cfg_defaults_on_and_clamps():
    c = parse_quote_cfg({})
    assert c["enabled"] is True                      # D：O-4 出厂默认开
    assert c["min_unanswered"] == 2
    assert c["stale_sec"] == 1800.0 and c["hourly_cap"] == 6
    assert (c["random_lo"], c["random_hi"]) == (0.10, 0.15)
    assert c["max_streak"] == 2 and c["cooldown_after_streak"] == 2
    assert c["self_quote_pct"] == 0.05
    c2 = parse_quote_cfg({"inbox": {"l2_autosend": {"quote_reply": {
        "enabled": False, "min_unanswered": 1, "min_relevance": 5,
        "platforms": "Telegram", "random_lo": 0.3, "random_hi": 0.1,
        "stale_sec": 5, "hourly_cap": -1, "max_streak": 0}}}})
    assert c2["enabled"] is False
    assert c2["min_unanswered"] == 2          # 连发铁律，配 1 也钳回 2
    assert c2["min_relevance"] == 1.0
    assert c2["platforms"] == ["telegram"]
    assert c2["random_hi"] >= c2["random_lo"] == 0.3
    assert c2["stale_sec"] == 60.0 and c2["hourly_cap"] == 0 and c2["max_streak"] == 1
    assert parse_quote_cfg({"inbox": {"l2_autosend": {"quote_reply": False}}})["enabled"] is False


def test_defaults_match_instruction_parameters():
    """指令 O-4 §A 的中档参数逐字钉住（少/中/多三档二期再做）。"""
    d = rqp.DEFAULTS
    assert d["enabled"] is True
    assert d["stale_sec"] == 30 * 60
    assert d["random_lo"] == 0.10 and d["random_hi"] == 0.15
    assert d["short_quick_sec"] == 120
    assert d["max_streak"] == 2 and d["cooldown_after_streak"] == 2
    assert d["hourly_cap"] == 6
    assert d["self_quote_pct"] == 0.05


# ── B1 能力位 ───────────────────────────────────────────────────────────

def test_capability_bits_telegram_whatsapp_only():
    cfg = parse_quote_cfg({})
    assert platform_allows_quote("telegram", cfg, orch_owns=True)
    assert platform_allows_quote("WhatsApp", cfg, orch_owns=True)
    assert not platform_allows_quote("line", cfg, orch_owns=True)       # 一期无能力位
    assert not platform_allows_quote("messenger", cfg, orch_owns=True)
    assert not platform_allows_quote("zalo", cfg, orch_owns=True)       # 未登记＝不支持
    assert not platform_allows_quote("telegram", cfg, orch_owns=False)  # RPA 回落不收 reply_to
    cfg2 = dict(cfg, platforms=["whatsapp"])
    assert not platform_allows_quote("telegram", cfg2, orch_owns=True)  # 白名单再收窄


def test_capability_bits_synced_with_surface_fusion_registry():
    """能力位静态表不得与双面板能力注册表漂移：策划过的平台 quote_reply.workspace==ok ⇔ True。"""
    from src.integrations.surface_fusion import capability_matrix, curated_platforms
    for plat in curated_platforms():
        row = next(r for r in capability_matrix(plat) if r["id"] == "quote_reply")
        assert rqp.platform_quote_capable(plat) is (row["workspace"] == "ok"), plat
    assert rqp.platform_quote_capable("line") is False


def test_unsupported_decision_and_log():
    d = rqp.unsupported_decision("line")
    assert d.quote is False and d.reason == "none" and d.rule == "unsupported"
    line = rqp.format_log(d, conv="line:a:1", platform="line")
    assert line.startswith("[quote] conv=line:a:1 reply_to_mid=- reason=none rule=unsupported")


# ── A1 burst（#37 金标沿用）───────────────────────────────────────────────

GOLD = [
    (["今天好累啊", "周末要不要一起去爬山？", "对了你上次说的那家火锅店叫什么"],
     "那家火锅店叫蜀大侠，在春熙路那边，超好吃", 2),
    (["今天好累啊", "周末要不要一起去爬山？", "对了你上次说的那家火锅店叫什么"],
     "爬山好呀！周末我有空，去哪座山？", 1),
    (["I'm so tired today", "Are you free this weekend for hiking?",
      "btw what was that hotpot place called"],
     "The hotpot place is Shu Daxia, near Chunxi Road", 2),
    (["我到家了", "你吃饭了没", "晚上要不要视频"],
     "还没吃呢，正准备点外卖，你吃了吗", 1),
]


@pytest.mark.parametrize("inbound,reply,expect_idx", GOLD)
def test_burst_quotes_most_relevant(inbound, reply, expect_idx):
    msgs = [_m("out", "嗯嗯", NOW - 600, "9")]
    for i, t in enumerate(inbound):
        msgs.append(_m("in", t, NOW - 100 + i * 10, str(20 + i)))
    d = decide_quote(msgs, reply, now=NOW, conv_key="c1")
    assert d.quote is True, (d.rule, d.candidates)
    assert d.reason == "burst" and d.rule == "burst_relevant"
    assert d.target_index == expect_idx, d.candidates
    rt = d.as_reply_to()
    assert rt["id"] == str(20 + expect_idx) and rt["text"] == inbound[expect_idx]
    assert rt["from_me"] is False
    assert set(rt) == {"id", "from_me", "participant", "text", "sender"}
    assert d.burst_len == 3


def test_burst_low_relevance_does_not_quote():
    msgs = [_m("in", "今天好累啊", NOW - 100, "1"),
            _m("in", "周末要不要去爬山", NOW - 90, "2"),
            _m("in", "火锅店叫什么", NOW - 80, "3")]
    d = decide_quote(msgs, "哈哈哈哈哈", now=NOW)
    assert d.quote is False and d.reason == "none" and d.rule == "low_relevance"


def test_burst_tie_prefers_earlier_message():
    msgs = [_m("in", "你几点下班", NOW - 100, "1"),
            _m("in", "你几点下班呀", NOW - 90, "2")]
    d = decide_quote(msgs, "我六点下班", now=NOW)
    assert d.quote and d.target_index == 0


def test_cross_language_uses_translated_text_and_reply_alt():
    msgs = [_m("in", "so tired today", NOW - 100, "1", translated_text="今天好累"),
            _m("in", "what is that hotpot place called", NOW - 90, "2",
               translated_text="那家火锅店叫什么")]
    d = decide_quote(msgs, "It's called Shu Daxia", now=NOW, reply_alt="那家火锅店叫蜀大侠")
    assert d.quote and d.target_index == 1


def test_media_only_and_deleted_and_stale_are_not_candidates():
    msgs = [
        _m("in", "", NOW - 100, "1", media_type="image"),
        _m("in", "你吃了吗", NOW - 90, "2", deleted_at=NOW - 50),
        _m("in", "你吃了吗", NOW - 3 * 86400, "3"),
    ]
    d = decide_quote(msgs, "我吃了", now=NOW)
    assert d.quote is False and d.rule == "no_candidate" and d.burst_len == 3


def test_no_inbound_and_burst_helper():
    d = decide_quote([_m("out", "嗨", NOW - 10, "1")], "回复", now=NOW)
    assert d.quote is False and d.rule in ("no_inbound", "self_roll_miss")
    assert decide_quote([], "回复", now=NOW).rule == "no_inbound"
    msgs = [_m("in", "旧问题", NOW - 1000, "1"), _m("out", "旧回答", NOW - 900, "2"),
            _m("in", "a", NOW - 100, "3"), _m("system", "x", NOW - 90),
            _m("in", "b", NOW - 80, "4")]
    assert [r["text"] for r in unanswered_burst(msgs)] == ["a", "b"]


# ── A2 stale：回旧消息必引 ───────────────────────────────────────────────

def test_single_inbound_stale_over_30min_must_quote():
    msgs = [_m("out", "在的", NOW - 4000, "10"),
            _m("in", "你明天有空吗？", NOW - 31 * 60, "11")]
    d = decide_quote(msgs, "有空", now=NOW, conv_key="c-stale")
    assert d.quote is True and d.reason == "stale" and d.rule == "stale_30m"
    assert d.as_reply_to()["id"] == "11"
    # 29 min：不算旧；短答 + 已过 2 min → 走随机基线（非必引）
    msgs2 = [_m("out", "在的", NOW - 4000, "10"), _m("in", "你明天有空吗？", NOW - 29 * 60, "11")]
    d2 = decide_quote(msgs2, "有空", now=NOW, conv_key="c-stale")
    assert d2.reason in ("random", "none") and d2.rule in ("random_baseline", "roll_miss")


def test_stale_threshold_configurable():
    msgs = [_m("in", "在吗", NOW - 10 * 60, "11")]
    d = decide_quote(msgs, "在的呀", now=NOW, cfg={"stale_sec": 5 * 60})
    assert d.quote and d.reason == "stale"


# ── A4 禁引：2 min 内单条短答 ─────────────────────────────────────────────

def test_short_quick_single_reply_never_quotes():
    msgs = [_m("in", "在吗", NOW - 30, "11")]
    for k in range(50):     # 任何会话种子都不许命中——禁引压过随机基线
        d = decide_quote(msgs, "在的", now=NOW, conv_key=f"c{k}")
        assert d.quote is False and d.rule == "short_quick", k
    # 同样 30 秒内但长答 → 不属「短答」，进入随机基线
    long_reply = ("在的呀，刚忙完手上的事，你那边怎么样，今天过得还顺利吗？"
                  "我这边刚好有空，想听你说说今天都发生了什么")
    assert len(long_reply) > rqp.DEFAULTS["short_answer_chars"]
    d2 = decide_quote(msgs, long_reply, now=NOW, conv_key="c0")
    assert d2.rule in ("random_baseline", "roll_miss")


# ── A3 随机基线：会话级种子 ──────────────────────────────────────────────

def test_random_baseline_is_deterministic_per_session_and_target():
    msgs = [_m("in", "你明天有空吗？", NOW - 5 * 60, "77")]
    k_hit = _hit_key("hit-", "77", want_hit=True)
    k_miss = _hit_key("miss-", "77", want_hit=False)
    for _ in range(3):      # 重试/重算不抖
        d = decide_quote(msgs, "明天下午有空", now=NOW, conv_key=k_hit)
        assert d.quote and d.reason == "random" and d.rule == "random_baseline"
        assert d.roll is not None and d.rate is not None and d.roll < d.rate
        d2 = decide_quote(msgs, "明天下午有空", now=NOW, conv_key=k_miss)
        assert d2.quote is False and d2.rule == "roll_miss" and d2.roll >= d2.rate
    assert 0.10 <= session_rate(k_hit, parse_quote_cfg({})) <= 0.15


def test_random_baseline_population_rate_in_band():
    """3000 个会话各回一条 5 min 前的单条消息：命中率应落在 10–15% 附近。"""
    msgs = [_m("in", "你明天有空吗？", NOW - 5 * 60, "77")]
    hits = sum(decide_quote(msgs, "明天下午有空", now=NOW, conv_key=f"pop-{i}").quote
               for i in range(3000))
    assert 0.09 <= hits / 3000 <= 0.16, hits


def test_seeded_unit_properties():
    assert seeded_unit("a", "b") == seeded_unit("a", "b")
    assert 0.0 <= seeded_unit("x") < 1.0
    assert seeded_unit("a", "b") != seeded_unit("a", "c")


# ── A4 禁引：连引 ≤2 后至少 2 条不引 / 每小时 ≤6 ──────────────────────────

def test_streak_blocked_semantics():
    Q, U = True, False
    assert streak_blocked([Q, Q], 2, 2)              # 刚到顶
    assert streak_blocked([U, Q, Q], 2, 2)           # 只歇了 1 条
    assert not streak_blocked([U, U, Q, Q], 2, 2)    # 歇够 2 条
    assert not streak_blocked([Q, U, Q, U], 2, 2)    # 单引间隔不算连引
    assert not streak_blocked([Q], 2, 2)
    assert streak_blocked([Q, Q], 2, 0)              # cooldown=0 仍封顶
    assert not streak_blocked([], 2, 2)


def _stale_conv_with_history(out_flags_recent_first):
    """构造：若干条出站（带/不带引用）+ 一条 31 min 前的入站（本应 stale 必引）。"""
    msgs = []
    t = NOW - 7200
    for i, q in enumerate(reversed(out_flags_recent_first)):
        msgs.append(_out_q(f"o{i}", t + i * 10, f"o{i}", q))
    msgs.append(_m("in", "你明天有空吗？", NOW - 31 * 60, "11"))
    return msgs


def test_streak_cooldown_suppresses_even_must_rules():
    d = decide_quote(_stale_conv_with_history([True, True]), "有空", now=NOW)
    assert d.quote is False and d.rule == "streak_cooldown" and d.suppressed == "stale_30m"
    d2 = decide_quote(_stale_conv_with_history([False, True, True]), "有空", now=NOW)
    assert d2.quote is False and d2.rule == "streak_cooldown"
    d3 = decide_quote(_stale_conv_with_history([False, False, True, True]), "有空", now=NOW)
    assert d3.quote is True and d3.reason == "stale"


def test_hourly_cap_six_per_conversation():
    msgs = []
    for i in range(6):      # 过去一小时内 6 条带引用出站，且各隔开不构成连引
        msgs.append(_out_q(f"q{i}", NOW - 3000 + i * 200, f"q{i}", True))
        msgs.append(_out_q(f"u{i}a", NOW - 3000 + i * 200 + 50, f"u{i}a", False))
        msgs.append(_out_q(f"u{i}b", NOW - 3000 + i * 200 + 60, f"u{i}b", False))
    msgs += [_m("in", "今天好累啊", NOW - 100, "20"),
             _m("in", "那家火锅店叫什么", NOW - 90, "21")]
    d = decide_quote(msgs, "那家火锅店叫蜀大侠", now=NOW)
    assert d.quote is False and d.rule == "hourly_cap" and d.suppressed == "burst_relevant"
    assert d.quotes_last_hour == 6
    # 其中一条掉出一小时窗 → 放行
    msgs[0]["ts"] = NOW - 3700
    d2 = decide_quote(msgs, "那家火锅店叫蜀大侠", now=NOW)
    assert d2.quote is True and d2.reason == "burst"
    # hourly_cap=0 ＝不限
    msgs[0]["ts"] = NOW - 3000
    assert decide_quote(msgs, "那家火锅店叫蜀大侠", now=NOW, cfg={"hourly_cap": 0}).quote


# ── A4 禁引：引自己消息仅「补充上一条」≤5% ────────────────────────────────

def test_self_quote_only_as_supplement_and_capped():
    cfg = parse_quote_cfg({})
    msgs = [_m("in", "在吗", NOW - 700, "1"), _m("out", "在的，刚到家", NOW - 120, "o1")]
    k_hit = next(f"s{i}" for i in range(10_000)
                 if seeded_unit(f"s{i}", "self", "o1") < cfg["self_quote_pct"])
    k_miss = next(f"m{i}" for i in range(10_000)
                  if seeded_unit(f"m{i}", "self", "o1") >= cfg["self_quote_pct"])
    d = decide_quote(msgs, "对了，明天我可能晚点", now=NOW, conv_key=k_hit)
    assert d.quote and d.reason == "self" and d.rule == "self_supplement"
    assert d.as_reply_to() == {"id": "o1", "from_me": True, "participant": "",
                               "text": "在的，刚到家", "sender": ""}
    d2 = decide_quote(msgs, "对了，明天我可能晚点", now=NOW, conv_key=k_miss)
    assert d2.quote is False and d2.rule == "self_roll_miss"
    # 自己上一条超过窗口（不是「补充」）→ 不引；无 mid → no_ref；pct=0 → 关
    old = [_m("in", "在吗", NOW - 7000, "1"), _m("out", "在的", NOW - 3600, "o1")]
    assert decide_quote(old, "补一句", now=NOW, conv_key=k_hit).rule == "no_inbound"
    nomid = [_m("in", "在吗", NOW - 700, "1"), _m("out", "在的", NOW - 120, "")]
    assert decide_quote(nomid, "补一句", now=NOW, conv_key=k_hit).rule == "no_ref"
    assert decide_quote(msgs, "补一句", now=NOW, conv_key=k_hit,
                        cfg={"self_quote_pct": 0}).rule == "no_inbound"


def test_self_quote_population_rate_under_five_percent():
    msgs = [_m("in", "在吗", NOW - 700, "1"), _m("out", "在的，刚到家", NOW - 120, "o1")]
    hits = sum(decide_quote(msgs, "补一句", now=NOW, conv_key=f"sp-{i}").quote for i in range(3000))
    assert hits / 3000 <= 0.07 and hits > 0


# ── C 日志行 / 观测 ───────────────────────────────────────────────────────

def test_format_log_contract():
    msgs = [_m("out", "嗯嗯", NOW - 600, "9"), _m("in", "今天好累啊", NOW - 100, "20"),
            _m("in", "那家火锅店叫什么", NOW - 90, "21")]
    d = decide_quote(msgs, "那家火锅店叫蜀大侠", now=NOW, conv_key="telegram:a:1")
    line = rqp.format_log(d, conv="telegram:a:1", platform="telegram")
    assert line.startswith("[quote] conv=telegram:a:1 reply_to_mid=21 reason=burst rule=burst_relevant")
    assert "platform=telegram" in line and "burst=2" in line and "score=" in line
    d2 = decide_quote([_m("in", "在吗", NOW - 30, "1")], "在的", now=NOW)
    line2 = rqp.format_log(d2, conv="c", platform="whatsapp")
    assert "reply_to_mid=- reason=none rule=short_quick" in line2


def test_question_and_relevance_helpers():
    assert is_question("你吃了吗") and is_question("what time?") and is_question("點解唔返工")
    assert not is_question("我到家了")
    assert relevance_score("", "x") == 0.0 and relevance_score("abc", "") == 0.0
    assert 0.0 <= relevance_score("火锅店叫蜀大侠", "火锅店叫什么") <= 1.0


def test_stats_counters_by_reason_and_rule():
    rqp.reset_stats()
    rqp.record_decision(decide_quote([_m("in", "a", NOW - 10, "1")], "b", now=NOW))
    rqp.record_decision(decide_quote(
        [_m("in", "今天好累啊", NOW - 100, "20"), _m("in", "那家火锅店叫什么", NOW - 90, "21")],
        "那家火锅店叫蜀大侠", now=NOW))
    rqp.record_decision(rqp.unsupported_decision("line"))
    rqp.record_applied({"quote_applied": True})
    rqp.record_applied({"delivered": True})
    rqp.record_fallback_plain()
    s = rqp.stats_snapshot()
    assert s["decided"] == 3 and s["quoted"] == 1
    assert s["quoted_by"] == {"burst": 1}
    assert s["skipped"] == {"short_quick": 1, "unsupported": 1}
    assert s["applied"] == 1 and s["fallback_plain"] == 1
    rqp.reset_stats()


# ── autosend 接线（B1/B2/C/D 的发送层半边）────────────────────────────────

class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, lvl):
        def _f(msg, *a, **k):
            try:
                self.lines.append((lvl, msg % a if a else msg))
            except Exception:
                self.lines.append((lvl, str(msg)))
        return _f

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error"):
            return self._rec(name)
        raise AttributeError(name)


def _assistant(cfg, rows):
    class _St:
        def list_recent_messages(self, cid, limit=50, **kw):
            return list(rows)

        def record_outreach(self, *a, **kw):
            return 1
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg), logger=_Log(),
        inbox_store=_St(), _web_loop=None,
    )


def _wire(monkeypatch, *, owns=True, fake_send):
    from src.inbox import autosend_helpers as ah
    import src.inbox.channel_adapters as _ca
    import src.integrations.account_orchestrator as _ao

    async def _false(*a, **k):
        return False
    for name in ("autosend_image", "autosend_voice", "autosend_bazi_kline", "autosend_video"):
        monkeypatch.setattr(ah, name, _false)
    monkeypatch.setattr(_ca, "send_via_adapters", fake_send)

    class _Orch:
        def owns(self, platform, account_id):
            return owns
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    return ah


def _quote_lines(a):
    return [m for lvl, m in a.logger.lines if m.startswith("[quote]") and lvl == "info"]


_QCFG = {"inbox": {"l2_autosend": {"quote_reply": {"enabled": True}}}}
_ROWS3 = [
    _m("out", "嗯嗯", NOW - 600, "9"),
    _m("in", "今天好累啊", NOW - 100, "20"),
    _m("in", "周末要不要一起去爬山？", NOW - 90, "21"),
    _m("in", "对了你上次说的那家火锅店叫什么", NOW - 80, "22"),
]


def _send(monkeypatch, cfg, rows, platform, text, *, owns=True, fake=None):
    seen = []

    async def _fake(shim, plat, account_id, chat_key, txt, adapters, **kw):
        seen.append(kw.get("reply_to"))
        if fake:
            return fake(kw)
        return {"delivered": True, "quote_applied": bool(kw.get("reply_to"))}
    ah = _wire(monkeypatch, owns=owns, fake_send=_fake)
    a = _assistant(cfg, rows)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb(platform, "acct", "123", text))
    return res, seen, a


def test_autosend_passes_reply_to_for_burst_and_logs(monkeypatch):
    rqp.reset_stats()
    res, seen, a = _send(monkeypatch, _QCFG, _ROWS3, "telegram", "那家火锅店叫蜀大侠，超好吃")
    assert res.get("delivered") is True
    assert len(seen) == 1 and seen[0]["id"] == "22"
    assert rqp.stats_snapshot()["applied"] == 1
    ql = _quote_lines(a)
    assert len(ql) == 1
    assert ql[0].startswith("[quote] conv=telegram:acct:123 reply_to_mid=22 reason=burst rule=burst_relevant")


def test_autosend_default_on_without_config(monkeypatch):
    """D：未配置 quote_reply → 默认开（1.0.78 起）；#37 时代是默认关。"""
    res, seen, a = _send(monkeypatch, {}, _ROWS3, "telegram", "那家火锅店叫蜀大侠")
    assert res.get("delivered") is True and seen[0]["id"] == "22"


def test_autosend_switch_off_sends_plain_and_only_debug_logs(monkeypatch):
    off = {"inbox": {"l2_autosend": {"quote_reply": {"enabled": False}}}}
    res, seen, a = _send(monkeypatch, off, _ROWS3, "telegram", "那家火锅店叫蜀大侠")
    assert seen == [None] and not _quote_lines(a)
    assert any("rule=disabled" in m for lvl, m in a.logger.lines if lvl == "debug")


def test_autosend_single_quick_short_sends_plain(monkeypatch):
    res, seen, a = _send(monkeypatch, _QCFG, [_m("in", "火锅店叫什么", NOW - 10, "1")],
                         "telegram", "蜀大侠")
    assert seen == [None]
    assert "reason=none rule=short_quick" in _quote_lines(a)[0]


def test_autosend_stale_single_quotes(monkeypatch):
    real_now = time.time()      # 接线处用墙钟算「旧消息」，行时间戳须相对真实 now
    rows = [_m("out", "嗯", real_now - 5000, "9"), _m("in", "火锅店叫什么", real_now - 40 * 60, "1")]
    res, seen, a = _send(monkeypatch, _QCFG, rows, "whatsapp", "蜀大侠")
    assert seen[0]["id"] == "1"
    assert "reason=stale rule=stale_30m" in _quote_lines(a)[0]


def test_autosend_unsupported_platform_logs_and_sends_plain(monkeypatch):
    """B1：LINE / Messenger 无能力位 → 普通发送 + rule=unsupported 日志，不报错。"""
    for plat in ("line", "messenger"):
        rqp.reset_stats()
        res, seen, a = _send(monkeypatch, _QCFG, _ROWS3, plat, "那家火锅店叫蜀大侠")
        assert res.get("delivered") is True and seen == [None]
        assert f"reply_to_mid=- reason=none rule=unsupported platform={plat}" in _quote_lines(a)[0]
        assert rqp.stats_snapshot()["skipped"] == {"unsupported": 1}


def test_autosend_rpa_path_never_quotes(monkeypatch):
    res, seen, a = _send(monkeypatch, _QCFG, _ROWS3, "telegram", "那家火锅店叫蜀大侠", owns=False)
    assert res.get("delivered") is True and seen == [None]
    assert "rule=unsupported" in _quote_lines(a)[0]


def test_autosend_quote_failure_falls_back_to_plain(monkeypatch):
    """B2：带引用发送抛异常 → 去引用重发一次，不抛、不丢消息。"""
    def _fake(kw):
        if kw.get("reply_to"):
            raise RuntimeError("MESSAGE_ID_INVALID")
        return {"delivered": True}
    rqp.reset_stats()
    res, seen, a = _send(monkeypatch, _QCFG, _ROWS3, "telegram", "那家火锅店叫蜀大侠", fake=_fake)
    assert res.get("delivered") is True
    assert len(seen) == 2 and seen[0]["id"] == "22" and seen[1] is None
    assert rqp.stats_snapshot()["fallback_plain"] == 1
    assert any("rule=fallback_plain" in m for lvl, m in a.logger.lines)


def test_autosend_store_error_is_swallowed(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True}
    ah = _wire(monkeypatch, fake_send=_fake)
    a = _assistant(_QCFG, _ROWS3)

    class _Bad:
        def list_recent_messages(self, *a, **k):
            raise RuntimeError("db locked")
    a.inbox_store = _Bad()
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠"))
    assert res.get("delivered") is True and seen == [None]


def test_autosend_bubbles_only_first_part_quotes(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append((text, kw.get("reply_to")))
        return {"delivered": True, "quote_applied": bool(kw.get("reply_to"))}
    ah = _wire(monkeypatch, fake_send=_fake)

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cfg = {"inbox": {
        "l2_autosend": {"quote_reply": {"enabled": True}},
        "reply_style": {"bubbles": {
            "enabled": True, "min_total_chars": 0, "min_tail_chars": 0,
            "gap_sec_lo": 0, "gap_sec_hi": 0, "per_char_sec": 0}},
    }}
    a = _assistant(cfg, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠\n在春熙路那边\n超好吃的"))
    assert res.get("parts_sent") == 3
    assert seen[0][1] is not None and seen[0][1]["id"] == "22"
    assert seen[1][1] is None and seen[2][1] is None


# ── 观测暴露面（I-4 续）：autosend-status.quote_reply / metrics.persona_region ──

def _obs_client(config: dict):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from src.web.routes.drafts_routes import register_drafts_routes, register_metrics_route

    app = FastAPI()

    @app.middleware("http")
    async def _inj(request: Request, call_next):
        request.scope["session"] = {"role": "admin", "user_id": "u1", "username": "u1"}
        return await call_next(request)

    def _api_auth():
        return True

    register_drafts_routes(app, api_auth=_api_auth)
    register_metrics_route(app, api_auth=_api_auth)
    app.state.config_manager = SimpleNamespace(config=config)
    app.state.autosend_worker = SimpleNamespace(
        status_snapshot=lambda: {"running": True, "enabled": True})
    return TestClient(app, raise_server_exceptions=True)


def test_autosend_status_exposes_quote_reply_stats_and_cfg():
    rqp.reset_stats()
    rqp.record_decision(decide_quote(_ROWS3, "那家火锅店叫蜀大侠", cfg={"enabled": True}, now=NOW))
    rqp.record_decision(decide_quote([_m("in", "在吗", NOW - 5, "1")], "在的",
                                     cfg={"enabled": True}, now=NOW))
    rqp.record_fallback_plain()
    c = _obs_client({"inbox": {"l2_autosend": {"quote_reply": {
        "enabled": True, "min_relevance": 0.2}}}})
    q = c.get("/api/drafts/autosend-status").json()["worker"]["quote_reply"]
    assert q["enabled"] is True
    assert q["min_unanswered"] == 2 and q["min_relevance"] == 0.2
    assert q["decided"] == 2 and q["quoted"] == 1
    assert q["quoted_by"] == {"burst": 1} and q["skipped"] == {"short_quick": 1}
    assert q["fallback_plain"] == 1
    # 未配置 → enabled=True 回显（1.0.78 默认开）；显式关 → False
    assert _obs_client({}).get("/api/drafts/autosend-status").json()["worker"]["quote_reply"]["enabled"] is True
    c3 = _obs_client({"inbox": {"l2_autosend": {"quote_reply": {"enabled": False}}}})
    assert c3.get("/api/drafts/autosend-status").json()["worker"]["quote_reply"]["enabled"] is False


def test_metrics_exposes_persona_region_stats():
    from src.ai import persona_region as pr
    pr.note_banned_hits("你什么时候来，这个说的对", "zh-HK")
    c = _obs_client({})
    m = c.get("/api/workspace/metrics").json()
    reg = m.get("persona_region")
    assert reg is not None
    for k in ("resolve_persona", "resolve_dialect", "resolve_location",
              "resolve_outbound", "resolve_default", "banned_hit", "script_hit", "observed"):
        assert k in reg, k
    assert reg["observed"] >= 1 and reg["banned_hit"] >= 1 and reg["script_hit"] >= 1
    assert "l1_region_inject" in (m.get("spoken_style") or {})
