# -*- coding: utf-8 -*-
"""case_center（案例中心纯函数核心 + /api/cases 路由改造，2026-08-03）门禁。

覆盖重点：
- 生命周期不变量：单活跃案例去重 / 严重度只升不降 / 结案后复发＝归档另立新案
- 保守检测器：宁漏不误（第三人称叙事、否定句、闲聊不误伤）
- 行构建：legacy 存量兜底 / 已结案淡出窗 / 排序 / 会话深链
- ContextStore SQLite 兜底扫描（重启不失明的地基）
- 路由端到端：持久化案例可见、note/close 写后即 flush、reason 按语言渲染
"""

from __future__ import annotations

import time

import pytest

from src.utils.case_center import (
    CLOSED_LINGER_SEC,
    DEFAULT_MEDIA_STALE_HOURS,
    DRILL_CLEANUP_RESOLUTION,
    DRILL_RESET_SEC,
    archive_closed_case,
    case_board_text_lines,
    case_row,
    chain_reason,
    claim_case,
    classify_resolution,
    close_case,
    close_open_drill_cases,
    collect_case_rows,
    conv_ref,
    count_open_cases,
    count_open_drill_cases,
    detect_ai_doubt,
    detect_human_request,
    is_drill_uid,
    open_case,
    release_case,
    resolve_inbound_mid,
    should_hide,
    sort_key,
    summarize_case_board,
)


# ── 生命周期 ─────────────────────────────────────────────────────────────────

def test_open_case_creates_fields():
    ctx = {}
    cid, action = open_case(ctx, "12345678", "human_request",
                            "case.reason.human_request", now=1000.0)
    assert action == "created"
    assert cid.startswith("CASE-345678-")
    assert ctx["_case_id"] == cid
    assert ctx["_case_source"] == "human_request"
    assert ctx["_case_reason_code"] == "case.reason.human_request"
    assert ctx["_case_created_at"] == 1000.0
    assert ctx["_case_signal_count"] == 1


def test_open_case_dedupes_active_and_counts_signals():
    ctx = {}
    cid1, _ = open_case(ctx, "u1", "media_complaint", "case.reason.media_fake")
    cid2, action = open_case(ctx, "u1", "media_complaint", "case.reason.media_repeat")
    assert cid1 == cid2
    assert action == "repeat"
    assert ctx["_case_signal_count"] == 2
    # 同严重度不改 reason（首因保留，锚点稳定）
    assert ctx["_case_reason_code"] == "case.reason.media_fake"


def test_open_case_upgrades_but_never_downgrades():
    ctx = {}
    cid1, _ = open_case(ctx, "u1", "intent_chain", "case.reason.pat.repeated_failure")
    cid2, action = open_case(ctx, "u1", "crisis", "case.reason.crisis")
    assert cid1 == cid2 and action == "upgraded"
    assert ctx["_case_source"] == "crisis"
    assert ctx["_case_reason_code"] == "case.reason.crisis"
    # 危机之后再来低级信号 → 不降级
    _, action2 = open_case(ctx, "u1", "intent_chain", "case.reason.pat.refund_flow")
    assert action2 == "repeat"
    assert ctx["_case_source"] == "crisis"


def test_close_then_new_signal_archives_and_opens_new_case():
    ctx = {}
    cid1, _ = open_case(ctx, "u1", "human_request", "case.reason.human_request", now=100.0)
    ctx["_case_note"] = "n1"
    assert close_case(ctx, "handled", now=200.0) is True
    assert ctx["_case_closed"] is True

    cid2, action = open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt", now=999999.0)
    assert action == "created"
    assert cid2 != cid1
    assert not ctx.get("_case_closed")  # 新案是干净的（归档已清键族）
    hist = ctx["_case_history"]
    assert len(hist) == 1
    assert hist[0]["case_id"] == cid1
    assert hist[0]["resolution"] == "handled"
    assert hist[0]["note"] == "n1"


def test_case_history_capped_at_five():
    ctx = {}
    for i in range(7):
        open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt", now=1000.0 + i)
        close_case(ctx, f"r{i}", now=2000.0 + i)
    archive_closed_case(ctx)
    assert len(ctx["_case_history"]) == 5
    assert ctx["_case_history"][-1]["resolution"] == "r6"


def test_close_case_without_case_returns_false():
    assert close_case({}, "x") is False


# ── 检测器（宁漏不误） ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "转人工！", "帮我转人工", "有没有人工客服", "我要真人客服",
    "找你们经理来", "投诉你们平台",
    "I want to talk to a human", "transfer me to a human agent please",
])
def test_detect_human_request_positives(text):
    assert detect_human_request(text) is True


@pytest.mark.parametrize("text", [
    "我昨天找客服退货吵了半天",      # 第三人称叙事（讲自己的生活）
    "人工智能真厉害",                # 「人工」歧义词
    "你比客服温柔多了",
    "hello there", "", None,
])
def test_detect_human_request_negatives(text):
    assert detect_human_request(text) is False


@pytest.mark.parametrize("text", [
    "你是机器人吧？", "你是不是机器人", "你是不是AI啊", "你就是个机器人",
    "感觉在跟机器人聊天", "are you a bot?", "Am I talking to a bot??",
])
def test_detect_ai_doubt_positives(text):
    assert detect_ai_doubt(text) is True


@pytest.mark.parametrize("text", [
    "我知道你不是机器人啦",          # 否定句＝信任表达
    "我买了个扫地机器人",            # 闲聊里的「机器人」
    "你真人比照片好看",
    "not a bot, right, me neither", "", None,
])
def test_detect_ai_doubt_negatives(text):
    assert detect_ai_doubt(text) is False


# ── reason / 行构建 ──────────────────────────────────────────────────────────

def test_chain_reason_known_and_generic():
    code, params = chain_reason("repeated_failure", "desc")
    assert code == "case.reason.pat.repeated_failure" and params == {}
    code2, params2 = chain_reason("custom_pack_pattern", "自定义描述")
    assert code2 == "case.reason.pat.generic"
    assert params2 == {"desc": "自定义描述"}
    assert chain_reason("", "")[0] == "case.reason.legacy"


def test_case_row_legacy_context_backfills_reason():
    """改造前的存量案例（只有 _case_id + _chain_pattern）→ 回推 reason/source。"""
    ctx = {
        "_case_id": "CASE-x-1",
        "_chain_pattern": {"pattern": "refund_flow", "desc": "投诉后要求退款"},
        "last_message": "m", "last_reply": "r",
    }
    row = case_row("u1", ctx, now=1000.0)
    assert row["source"] == "intent_chain"
    assert row["severity"] == 1
    assert row["reason_code"] == "case.reason.pat.refund_flow"


def test_case_row_none_without_case():
    assert case_row("u1", {"last_message": "hi"}) is None


def test_case_row_new_fields_and_conv_ref():
    ctx = {}
    open_case(ctx, "line:U99", "crisis", "case.reason.crisis", now=500.0)
    ctx["conversation_id"] = "line:acct1:U99"
    ctx["last_message"] = "x" * 300
    row = case_row("line:U99", ctx, now=4100.0)
    assert row["severity"] == 3
    assert row["conv_ref"] == "line:acct1:U99"
    assert row["age_hours"] == 1.0
    assert len(row["last_message"]) == 100  # 截断保留


def test_conv_ref_telegram_fallback_and_empty():
    assert conv_ref({"chat_id": "123", "account_id": ""}) == "telegram:default:123"
    assert conv_ref({"user_id": "88", "account_id": "acct9"}) == "telegram:acct9:88"
    # ctx 带平台软标记（A/B 线均落）→ RPA 会话不再误拼成 telegram
    assert conv_ref({"chat_id": "U9", "platform": "line", "account_id": "a1"}) == "line:a1:U9"
    assert conv_ref({}) == ""


def test_should_hide_closed_after_linger_window():
    now = time.time()
    fresh = {"closed": True, "closed_at": now - 3600}
    stale = {"closed": True, "closed_at": now - CLOSED_LINGER_SEC - 10}
    legacy = {"closed": True, "closed_at": 0}   # 旧数据无时间戳 → 宁可多显
    opened = {"closed": False, "closed_at": 0}
    assert should_hide(fresh, now) is False
    assert should_hide(stale, now) is True
    assert should_hide(legacy, now) is False
    assert should_hide(opened, now) is False


def test_sort_key_severity_then_recency():
    crisis = {"closed": False, "severity": 3, "at_risk": False, "created_at": 100}
    chain_risky = {"closed": False, "severity": 1, "at_risk": True, "created_at": 999}
    chain_new = {"closed": False, "severity": 1, "at_risk": False, "created_at": 5000}
    closed = {"closed": True, "severity": 3, "at_risk": True, "created_at": 9999}
    rows = sorted([closed, chain_new, chain_risky, crisis], key=sort_key)
    assert rows[0] is crisis          # 严重度优先
    assert rows[1] is chain_risky     # 同级 at_risk 在前
    assert rows[2] is chain_new
    assert rows[3] is closed          # 已结案永远殿后


# ── 证据锚点：信号事件时间线 + 身份字段（2026-08-09） ────────────────────────

def test_open_case_records_event_timeline_with_quotes():
    ctx = {}
    open_case(ctx, "u1", "media_complaint", "case.reason.media_lie_caught",
              now=100.0, quote="你不是说发了？我这还没收到呢")
    open_case(ctx, "u1", "media_complaint", "case.reason.media_repeat",
              now=200.0, quote="这张图你发过了")
    open_case(ctx, "u1", "crisis", "case.reason.crisis", now=300.0, quote="难受")
    evs = ctx["_case_events"]
    assert [e["ts"] for e in evs] == [100.0, 200.0, 300.0]
    # repeat / upgraded 也各记一条——时间线的意义就是「每次信号何时、说了什么」
    assert [e["code"] for e in evs] == [
        "case.reason.media_lie_caught", "case.reason.media_repeat",
        "case.reason.crisis"]
    assert "没收到" in evs[0]["quote"]


def test_case_events_capped_and_quote_truncated():
    ctx = {}
    for i in range(15):
        open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt",
                  now=float(i), quote="q" * 200)
    evs = ctx["_case_events"]
    assert len(evs) == 10
    assert evs[0]["ts"] == 5.0            # 只留最近 10 条
    assert len(evs[0]["quote"]) == 80     # 原话截 80 字防上下文膨胀


def test_case_row_identity_and_last_signal():
    ctx = {"platform": "line", "account_id": "a1", "chat_id": "U9",
           "chat_title": "王先生"}
    open_case(ctx, "line:a1:U9", "human_request", "case.reason.human_request",
              now=100.0, quote="转人工")
    open_case(ctx, "line:a1:U9", "human_request", "case.reason.human_request",
              now=900.0, quote="有人吗")
    row = case_row("line:a1:U9", ctx, now=1000.0)
    assert row["platform"] == "line" and row["account_id"] == "a1"
    assert row["peer_name"] == "王先生"
    assert row["last_signal_at"] == 900.0
    assert row["drill"] is False
    assert [e["quote"] for e in row["events"]] == ["转人工", "有人吗"]


def test_case_row_identity_falls_back_to_conv_ref():
    ctx = {"conversation_id": "telegram:8244899900:555000"}   # 无 platform/account 软标记
    open_case(ctx, "8244899900:555000", "ai_doubt", "case.reason.ai_doubt")
    row = case_row("8244899900:555000", ctx)
    assert row["platform"] == "telegram"
    assert row["account_id"] == "8244899900"


def test_case_row_without_events_falls_back_last_signal_to_created():
    ctx = {"_case_id": "CASE-legacy-1", "_case_created_at": 777.0}
    row = case_row("u_legacy", ctx)
    assert row["events"] == []
    assert row["last_signal_at"] == 777.0


def test_sort_key_recent_signal_floats_resurfaced_case():
    old_resurfaced = {"closed": False, "severity": 2, "at_risk": False,
                      "created_at": 100, "last_signal_at": 9000}
    newer_quiet = {"closed": False, "severity": 2, "at_risk": False,
                   "created_at": 5000, "last_signal_at": 5000}
    rows = sorted([newer_quiet, old_resurfaced], key=sort_key)
    assert rows[0] is old_resurfaced      # 老案刚复发 → 浮上来


# ── 演练号段隔离（duel_nightly 保留段 990001000+，2026-08-09 生产实锤） ──────

def test_drill_uid_detection():
    assert is_drill_uid("990001004") is True
    assert is_drill_uid("8244899900:990001004") is True        # store 键形态
    assert is_drill_uid("telegram:acct:990001500") is True     # 三段式 conv id
    assert is_drill_uid("990002000") is False                  # 段外
    assert is_drill_uid("line:a1:U990001004") is False         # 非纯数字尾段
    assert is_drill_uid("") is False


def test_drill_case_opens_flagged_but_fully_silent():
    eb, old = _fresh_bus()
    import src.utils.case_stats as cs_mod
    cs_mod._stats = None
    try:
        ctx = {"last_message": "你不是说发了？"}
        cid, action = open_case(
            ctx, "8244899900:990001004", "media_complaint",
            "case.reason.media_lie_caught", quote="你不是说发了？")
        assert action == "created"
        assert ctx["_case_drill"] is True
        assert _case_alert_events(eb) == []                    # 不发告警
        assert cs_mod.get_case_stats().dump()["opened"] == 0   # 不进观测计数
        row = case_row("8244899900:990001004", ctx)
        assert row["drill"] is True
    finally:
        import src.integrations.shared.event_bus as _ebm
        _ebm._bus = old
        cs_mod._stats = None


def test_legacy_drill_rows_flagged_dynamically():
    """改造前已存在的演练案例（无 _case_drill 键）→ 按 uid 号段动态判定。"""
    ctx = {"_case_id": "CASE-001004-1", "_case_created_at": 100.0}
    row = case_row("8244899900:990001004", ctx)
    assert row["drill"] is True


def test_collect_rows_excludes_drill_by_default():
    store = _FakeStore()
    store._cache["u_real"] = _case_ctx("u_real")
    store._cache["990001004"] = _case_ctx("990001004")
    rows = collect_case_rows(store)
    assert [r["user_id"] for r in rows] == ["u_real"]          # 看门狗/徽标口径
    rows_all = collect_case_rows(store, include_drill=True)
    assert {r["user_id"] for r in rows_all} == {"u_real", "990001004"}
    assert count_open_cases(store) == 1                        # 徽标不数演练


def test_archive_clears_events_and_drill_flag():
    ctx = {}
    open_case(ctx, "990001004", "ai_doubt", "case.reason.ai_doubt", quote="x")
    close_case(ctx, "done")
    archive_closed_case(ctx)
    assert "_case_events" not in ctx
    assert "_case_drill" not in ctx


def test_resolve_inbound_mid_prefers_explicit_then_ctx_keys():
    assert resolve_inbound_mid(None, "42") == "42"
    assert resolve_inbound_mid({"user_msg_id": 99}, "") == "99"
    assert resolve_inbound_mid({"user_msg_id": 0, "message_id": "wamid.x"}, "") == "wamid.x"
    assert resolve_inbound_mid({"user_msg_id": "0"}, "") == ""
    assert resolve_inbound_mid({}, "") == ""


def test_open_case_records_mid_on_events_and_anchor():
    ctx = {"user_msg_id": 4242}
    open_case(ctx, "u1", "media_complaint", "case.reason.media_lie_caught",
              now=100.0, quote="没收到", mid="")          # 自吸 ctx
    open_case(ctx, "u1", "media_complaint", "case.reason.media_repeat",
              now=200.0, quote="发过了", mid="5555")      # 显式覆盖
    evs = ctx["_case_events"]
    assert evs[0]["mid"] == "4242"
    assert evs[1]["mid"] == "5555"
    row = case_row("u1", ctx)
    assert row["anchor_mid"] == "5555"                   # 最近带 mid 的信号
    assert [e["mid"] for e in row["events"]] == ["4242", "5555"]


def test_open_case_omits_mid_key_when_absent():
    ctx = {}
    open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt", quote="机器人？")
    assert "mid" not in ctx["_case_events"][0]
    assert case_row("u1", ctx)["anchor_mid"] == ""


def test_drill_auto_reset_after_idle_window():
    """跨夜跑：距上次信号 ≥ DRILL_RESET_SEC → 旧演练案归档，另立新案。"""
    ctx = {}
    cid1, _ = open_case(ctx, "990001004", "media_complaint",
                        "case.reason.media_lie_caught", now=1000.0, quote="a")
    cid2, action = open_case(
        ctx, "990001004", "media_complaint", "case.reason.media_repeat",
        now=1000.0 + DRILL_RESET_SEC + 1, quote="b")
    assert action == "created"
    assert cid2 != cid1
    assert len(ctx.get("_case_history") or []) == 1
    assert ctx["_case_history"][0]["resolution"] == "drill auto-reset"


def test_drill_within_reset_window_still_dedupes():
    ctx = {}
    cid1, _ = open_case(ctx, "990001004", "ai_doubt", "case.reason.ai_doubt",
                        now=1000.0)
    cid2, action = open_case(
        ctx, "990001004", "ai_doubt", "case.reason.ai_doubt",
        now=1000.0 + DRILL_RESET_SEC - 10)
    assert action == "repeat" and cid2 == cid1


def test_close_open_drill_cases_only_touches_drill_open():
    store = _FakeStore()
    store._cache["u_real"] = _case_ctx("u_real")
    drill = {}
    open_case(drill, "990001004", "media_complaint",
              "case.reason.media_lie_caught", quote="x")
    store._cache["990001004"] = drill
    closed_already = {}
    open_case(closed_already, "990001005", "ai_doubt", "case.reason.ai_doubt")
    close_case(closed_already, "manual")
    store._cache["990001005"] = closed_already
    n = close_open_drill_cases(store, persist=False)
    assert n == 1
    assert store._cache["990001004"]["_case_closed"] is True
    assert store._cache["990001004"]["_case_resolution"] == DRILL_CLEANUP_RESOLUTION
    assert not store._cache["u_real"].get("_case_closed")      # 真实客户零误伤
    assert store._cache["990001005"]["_case_resolution"] == "manual"


# ── 认领 / 释放（P4 多坐席分工） ─────────────────────────────────────────────

def test_claim_release_lifecycle():
    ctx = {}
    open_case(ctx, "u1", "human_request", "case.reason.human_request")
    ok, holder = claim_case(ctx, "alice", now=100.0)
    assert ok and holder == "alice"
    assert ctx["_case_claimed_at"] == 100.0
    # 同人重复认领幂等（保留首次时刻）
    ok2, _ = claim_case(ctx, "alice", now=999.0)
    assert ok2 and ctx["_case_claimed_at"] == 100.0
    # 他人认领被拒并告知现认领人
    ok3, holder3 = claim_case(ctx, "bob")
    assert not ok3 and holder3 == "alice"
    # 释放后任何人可再认领
    assert release_case(ctx) is True
    ok4, holder4 = claim_case(ctx, "bob")
    assert ok4 and holder4 == "bob"


def test_claim_rejected_without_case_or_closed():
    assert claim_case({}, "alice") == (False, "")
    ctx = {}
    open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt")
    close_case(ctx, "done")
    assert claim_case(ctx, "alice")[0] is False


def test_archive_clears_claim_and_history_records_owner():
    ctx = {}
    open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt")
    claim_case(ctx, "alice")
    close_case(ctx, "done")
    open_case(ctx, "u1", "crisis", "case.reason.crisis")  # 复发→归档另立新案
    assert "_case_claimed_by" not in ctx
    assert ctx["_case_history"][-1]["claimed_by"] == "alice"
    row = case_row("u1", ctx)
    assert row["claimed_by"] == ""


# ── 趋势落库（P4：按日 opened/closed/by_source，跨重启） ────────────────────

def test_case_trend_store_daily_upsert(tmp_path):
    from src.utils.case_trend_store import CaseTrendStore
    store = CaseTrendStore(tmp_path / "trend.db")
    t0 = 1_700_000_000.0
    store.add_opened("crisis", now=t0)
    store.add_opened("crisis", now=t0)
    store.add_opened("human_request", now=t0)
    store.add_closed(now=t0)
    store.add_opened("ai_doubt", now=t0 + 86400)   # 次日
    rows = store.recent(14)
    assert len(rows) == 2
    assert rows[0]["opened"] == 3 and rows[0]["closed"] == 1
    assert rows[0]["by_source"] == {"crisis": 2, "human_request": 1}
    assert rows[1]["opened"] == 1 and rows[1]["by_source"] == {"ai_doubt": 1}
    store.close()


def test_open_close_bump_trend_store(tmp_path, monkeypatch):
    import src.utils.case_trend_store as cts
    store = cts.CaseTrendStore(tmp_path / "trend.db")
    monkeypatch.setattr(cts, "get_case_trend_store", lambda: store)
    ctx = {}
    open_case(ctx, "u1", "human_request", "case.reason.human_request")
    close_case(ctx, "done")
    rows = store.recent(3)
    assert rows and rows[-1]["opened"] == 1 and rows[-1]["closed"] == 1
    store.close()


# ── 危机直达中继（severity 3 → ops_alert 集团 TG，30min 防抖在 notify 内） ──

def test_crisis_case_relays_to_ops_alert(monkeypatch):
    calls = []
    import src.ops.ops_alert as oa
    monkeypatch.setattr(oa, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)))
    ctx = {"last_message": "活不下去了"}
    open_case(ctx, "u_crisis", "crisis", "case.reason.crisis")
    assert len(calls) == 1
    kind, text, kw = calls[0]
    assert kind == "case_crisis"
    assert "u_crisis" in text or kw.get("account_id") == "u_crisis"
    # severity 2 不走中继
    calls.clear()
    ctx2 = {}
    open_case(ctx2, "u2", "human_request", "case.reason.human_request")
    assert calls == []


# ── 告警发射与观测计数（2026-08-03 P3：severity≥2 开案/升级 → case_alert） ──

def _fresh_bus():
    import src.integrations.shared.event_bus as eb
    old = eb._bus
    eb._bus = eb.EventBus()
    return eb, old


def _case_alert_events(eb):
    return [e for e in eb._bus._history if e["type"] == "case_alert"]


def test_open_case_emits_alert_created_and_upgrade_only():
    eb, old = _fresh_bus()
    try:
        ctx = {"last_message": "转人工", "platform": "line",
               "account_id": "a1", "chat_id": "U9"}
        open_case(ctx, "u1", "human_request", "case.reason.human_request")
        evts = _case_alert_events(eb)
        assert len(evts) == 1
        p = evts[0]["data"]
        assert p["severity"] == 2 and p["source"] == "human_request"
        assert p["action"] == "created"
        assert p["conv_ref"] == "line:a1:U9"
        assert p["rate_key"] == "case:u1"
        # 同级重复信号不再发（防风暴）
        open_case(ctx, "u1", "human_request", "case.reason.human_request")
        assert len(_case_alert_events(eb)) == 1
        # 升级到危机 → 再发一次（严重度真的变了，值得再响）
        open_case(ctx, "u1", "crisis", "case.reason.crisis")
        evts2 = _case_alert_events(eb)
        assert len(evts2) == 2
        assert evts2[-1]["data"]["action"] == "upgraded"
        assert evts2[-1]["data"]["severity"] == 3
    finally:
        import src.integrations.shared.event_bus as _ebm
        _ebm._bus = old


def test_open_case_low_severity_intent_chain_no_alert():
    eb, old = _fresh_bus()
    try:
        ctx = {}
        open_case(ctx, "u1", "intent_chain", "case.reason.pat.repeated_failure")
        assert _case_alert_events(eb) == []
    finally:
        import src.integrations.shared.event_bus as _ebm
        _ebm._bus = old


def test_case_stats_counters_and_prom():
    import src.utils.case_stats as cs_mod
    cs_mod._stats = None   # 重置进程单例（隔离本用例）
    try:
        ctx = {}
        open_case(ctx, "u1", "media_complaint", "case.reason.media_fake", now=1000.0)
        open_case(ctx, "u1", "media_complaint", "case.reason.media_repeat")   # repeat
        open_case(ctx, "u1", "crisis", "case.reason.crisis")                  # upgraded
        close_case(ctx, "done", now=1000.0 + 7200)
        close_case(ctx, "done again", now=1000.0 + 9999)  # 二次结案不重复计数
        d = cs_mod.get_case_stats().dump()
        assert d["opened"] == 1
        assert d["opened_by_source"] == {"media_complaint": 1}
        assert d["repeat_signals"] == 1
        assert d["upgraded"] == 1
        assert d["closed"] == 1
        assert d["avg_close_hours"] == 2.0
        assert d["alerts_emitted"] == 2   # created(sev2) + upgraded(sev3)
        prom = cs_mod.get_case_stats().dump_prom()
        assert "ws_cases_opened_total 1" in prom
        assert 'ws_cases_opened_by_source_total{source="media_complaint"} 1' in prom
    finally:
        cs_mod._stats = None


# ── 合并（缓存 + SQLite 兜底） ───────────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self._cache = {}
        self._db = {}
        self.flushed = []

    def iter_persisted_case_rows(self, exclude=(), limit=300):
        ex = set(exclude or ())
        return [(u, dict(c)) for u, c in self._db.items()
                if u not in ex and c.get("_case_id")]

    def get(self, uid):
        ctx = self._cache.get(uid)
        if ctx is None:
            ctx = dict(self._db.get(uid) or {})
            self._cache[uid] = ctx
        return ctx

    def mark_dirty(self, uid):
        pass

    def flush(self, uid=""):
        self.flushed.append(uid)
        if uid in self._cache:
            self._db[uid] = dict(self._cache[uid])


def _case_ctx(uid, source="human_request", closed=False, now=1000.0):
    ctx = {}
    open_case(ctx, uid, source, f"case.reason.{source}", now=now)
    if closed:
        close_case(ctx, "done", now=now + 10)
    ctx["last_message"] = "hello"
    return ctx


def test_collect_case_rows_merges_cache_and_db_dedup():
    store = _FakeStore()
    store._cache["u_mem"] = _case_ctx("u_mem")
    store._db["u_db"] = _case_ctx("u_db", source="crisis")
    # 同 uid 缓存优先（缓存里已结案，DB 里还是旧的未结案态）
    both = _case_ctx("u_both")
    store._db["u_both"] = dict(both)
    both_closed = dict(both)
    close_case(both_closed, "x", now=2000.0)
    store._cache["u_both"] = both_closed

    rows = collect_case_rows(store, now=3000.0)
    by_uid = {r["user_id"]: r for r in rows}
    assert set(by_uid) == {"u_mem", "u_db", "u_both"}
    assert by_uid["u_db"]["source"] == "crisis"
    assert by_uid["u_both"]["closed"] is True  # 缓存赢
    assert rows[0]["user_id"] == "u_db"        # crisis 排最前
    assert count_open_cases(store) == 2


def test_collect_case_rows_survives_store_without_db_scan():
    class _Old:
        _cache = {"u1": _case_ctx("u1")}
    rows = collect_case_rows(_Old())
    assert len(rows) == 1


def test_iter_persisted_case_rows_ttl_cache_and_flush_invalidation(tmp_path):
    """扫描按 TTL 缓存（四方轮询共用不重复付费）；flush 带案例即失效。"""
    from src.utils.context_store import ContextStore
    store = ContextStore(tmp_path / "ctx.db")
    ctx = store.get("u1")
    open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt")
    store.mark_dirty("u1")
    store.flush()
    store._cache.clear()

    assert len(store.iter_persisted_case_rows()) == 1
    # 绕过 store 直接清库（模拟他处写入）→ TTL 窗内仍旧值＝缓存确实在工作
    store._conn.execute("DELETE FROM user_context")
    store._conn.commit()
    assert len(store.iter_persisted_case_rows()) == 1
    # ttl=0 强制重扫 → 看到真相
    assert store.iter_persisted_case_rows(ttl=0) == []
    # flush 带案例的 ctx → 缓存失效，无需等 TTL
    ctx2 = store.get("u2")
    open_case(ctx2, "u2", "crisis", "case.reason.crisis")
    store.mark_dirty("u2")
    store.flush()
    uids = [u for u, _ in store.iter_persisted_case_rows()]
    assert uids == ["u2"]
    store.close()


def test_context_store_iter_persisted_case_rows(tmp_path):
    from src.utils.context_store import ContextStore
    store = ContextStore(tmp_path / "ctx.db")
    ctx = store.get("u_case")
    open_case(ctx, "u_case", "ai_doubt", "case.reason.ai_doubt", now=100.0)
    store.get("u_plain")["last_message"] = "no case"
    store.mark_dirty("u_case")
    store.mark_dirty("u_plain")
    store.flush()
    store._cache.clear()   # 模拟重启后的空缓存

    rows = store.iter_persisted_case_rows()
    assert len(rows) == 1
    uid, loaded = rows[0]
    assert uid == "u_case" and loaded["_case_source"] == "ai_doubt"
    # exclude 生效
    assert store.iter_persisted_case_rows(exclude={"u_case"}) == []
    store.close()


# ── 路由端到端（auth_client + 假 skill_manager） ────────────────────────────

class _FakeSM:
    def __init__(self, store):
        self._context_store = store


@pytest.fixture()
def cases_env(auth_client):
    store = _FakeStore()
    auth_client.app.state.skill_manager = _FakeSM(store)
    try:
        yield auth_client, store
    finally:
        try:
            delattr(auth_client.app.state, "skill_manager")
        except Exception:
            pass


def test_api_cases_active_includes_persisted_and_localizes_reason(cases_env):
    client, store = cases_env
    store._db["u_db"] = _case_ctx("u_db", source="human_request")
    r = client.get("/api/cases/active")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    row = body["cases"][0]
    assert row["source"] == "human_request"
    from src.web.i18n_packs.cases_page import ZH
    assert row["reason"] == ZH["case.reason.human_request"]
    assert "reason_params" not in row
    assert row["conv_ref"] == ""


def test_api_case_note_persists_even_for_db_only_case(cases_env):
    client, store = cases_env
    store._db["u_db"] = _case_ctx("u_db")
    cid = store._db["u_db"]["_case_id"]
    r = client.post(f"/api/cases/{cid}/note", json={"note": "跟进中"})
    assert r.status_code == 200
    assert store.flushed, "note 写入后必须立刻 flush（否则重启丢备注）"
    assert store._db["u_db"]["_case_note"] == "跟进中"


def test_api_case_close_persists_and_row_marked_closed(cases_env):
    client, store = cases_env
    store._cache["u_mem"] = _case_ctx("u_mem")
    cid = store._cache["u_mem"]["_case_id"]
    r = client.post(f"/api/cases/{cid}/close", json={"resolution": "已安抚"})
    assert r.status_code == 200
    assert store._cache["u_mem"]["_case_closed"] is True
    assert store._cache["u_mem"]["_case_resolution"] == "已安抚"
    assert store.flushed
    body = client.get("/api/cases/active").json()
    assert body["cases"][0]["closed"] is True


def test_api_cases_active_includes_drill_and_localized_event_labels(cases_env):
    """路由层：演练案例带 drill 标记进列表（本页是它唯一可见处）；
    事件时间线逐条带按请求语言渲染的 label + 原话 quote。"""
    client, store = cases_env
    ctx = {}
    open_case(ctx, "990001004", "media_complaint",
              "case.reason.media_lie_caught", quote="你不是说发了？")
    ctx["last_message"] = "hello"
    store._cache["990001004"] = ctx
    body = client.get("/api/cases/active").json()
    assert body["count"] == 1
    row = body["cases"][0]
    assert row["drill"] is True
    from src.web.i18n_packs.cases_page import ZH
    ev = row["events"][0]
    assert ev["label"] == ZH["case.reason.media_lie_caught"]
    assert ev["quote"] == "你不是说发了？"
    assert row["last_signal_at"] == ev["ts"]


def test_api_cases_close_drill_batch(cases_env):
    client, store = cases_env
    store._cache["u_real"] = _case_ctx("u_real")
    dctx = {}
    open_case(dctx, "990001004", "media_complaint",
              "case.reason.media_lie_caught", quote="x", mid="77")
    store._cache["990001004"] = dctx
    r = client.post("/api/cases/close-drill", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["closed"] == 1
    assert body["remaining_open_drill"] == 0
    assert store._cache["990001004"]["_case_closed"] is True
    assert not store._cache["u_real"].get("_case_closed")
    # 幂等：再调一次 closed=0，残影仍为 0
    r2 = client.post("/api/cases/close-drill", json={"resolution": "again"})
    assert r2.json()["closed"] == 0
    assert r2.json()["remaining_open_drill"] == 0


def test_api_case_note_unknown_case_404(cases_env):
    client, _ = cases_env
    r = client.post("/api/cases/CASE-nope-1/note", json={"note": "x"})
    assert r.status_code == 404


def test_api_case_claim_release_and_conflict(cases_env):
    client, store = cases_env
    store._cache["u1"] = _case_ctx("u1")
    cid = store._cache["u1"]["_case_id"]
    r = client.post(f"/api/cases/{cid}/claim", json={})
    assert r.status_code == 200
    me = r.json()["claimed_by"]
    assert me  # 会话用户名（conftest 下为 admin）
    assert store.flushed
    row = client.get("/api/cases/active").json()["cases"][0]
    assert row["claimed_by"] == me
    # 他人已认领 → 409（换个 holder 模拟）
    store._cache["u1"]["_case_claimed_by"] = "someone_else"
    r2 = client.post(f"/api/cases/{cid}/claim", json={})
    assert r2.status_code == 409
    assert "someone_else" in r2.json()["detail"]
    # 释放
    r3 = client.post(f"/api/cases/{cid}/claim", json={"release": True})
    assert r3.status_code == 200
    assert store._cache["u1"].get("_case_claimed_by") is None


# ── P3：结案分桶 + 趋势窗合计 ────────────────────────────────────────────────

def test_classify_resolution_quick_tags_and_free_text():
    assert classify_resolution("已安抚") == "soothed"
    assert classify_resolution("已安抚，客户接受补偿") == "soothed"
    assert classify_resolution("已转人工") == "handoff"
    assert classify_resolution("误报") == "false_alarm"
    assert classify_resolution("false alarm") == "false_alarm"
    assert classify_resolution("") == "empty"
    assert classify_resolution("客户自己消气了") == "other"


def test_close_case_records_source_and_resolution_buckets(tmp_path, monkeypatch):
    import src.utils.case_stats as cs
    import src.utils.case_trend_store as cts
    stats = cs.CaseStats()
    monkeypatch.setattr(cs, "get_case_stats", lambda: stats)
    store = cts.CaseTrendStore(tmp_path / "trend.db")
    monkeypatch.setattr(cts, "get_case_trend_store", lambda: store)
    ctx = {}
    t0 = 1_700_000_000.0
    open_case(ctx, "u1", "media_complaint", "case.reason.media_lie_caught",
              now=t0)
    close_case(ctx, "已安抚", now=t0 + 3600)
    d = stats.dump()
    assert d["closed"] == 1
    assert d["closed_by_source"]["media_complaint"] == 1
    assert d["closed_by_resolution"]["soothed"] == 1
    assert d["avg_close_hours_by_source"]["media_complaint"] == 1.0
    rows = store.recent(3)
    assert rows and rows[-1]["closed"] == 1, rows
    assert rows[-1]["closed_by_source"]["media_complaint"] == 1
    assert rows[-1]["closed_by_resolution"]["soothed"] == 1
    # 事件在日末：lo 贴近事件时刻仍应计入该 UTC 日（日段重叠，非日起点落窗）
    win = store.window_totals(t0 - 100, t0 + 86400)
    assert win["opened"] == 1 and win["closed"] == 1, win
    store.close()


def test_bline_inbound_msg_id_param_wired():
    """契约：B 线入口暴露 inbound_msg_id，贯通 generate_inbox_draft。"""
    import inspect
    from pathlib import Path

    from src.inbox.persona_reply import generate_persona_reply
    from src.skills.skill_manager import SkillManager
    assert "inbound_msg_id" in inspect.signature(generate_persona_reply).parameters
    assert "inbound_msg_id" in inspect.signature(
        SkillManager.generate_inbox_draft).parameters
    ad_src = Path("src/inbox/autodraft_helpers.py").read_text(encoding="utf-8")
    assert "inbound_msg_id=" in ad_src and "_peer_msg_id" in ad_src


# ── P4：看板摘要 / 结案闭环 / 媒体 mid / 演练卫生 ─────────────────────────────

def test_summarize_case_board_media_stale_and_drill_isolation():
    now = 1_700_000_000.0
    rows = [
        {"closed": False, "severity": 3, "source": "crisis", "drill": False,
         "created_at": now - 3600, "age_hours": 1.0},
        {"closed": False, "severity": 2, "source": "media_complaint", "drill": False,
         "created_at": now - 5 * 3600, "age_hours": 5.0},
        {"closed": False, "severity": 2, "source": "media_complaint", "drill": False,
         "created_at": now - 1 * 3600, "age_hours": 1.0},
        {"closed": True, "severity": 2, "source": "media_complaint", "drill": False,
         "created_at": now - 10 * 3600, "age_hours": 10.0},
        {"closed": False, "severity": 2, "source": "media_complaint", "drill": True,
         "created_at": now - 20 * 3600, "age_hours": 20.0},
    ]
    s = summarize_case_board(rows, now=now, media_stale_hours=4.0)
    assert s["open"] == 3
    assert s["urgent"] == 1
    assert s["media_open"] == 2
    assert s["media_stale"] == 1
    assert s["open_drill"] == 1
    assert s["oldest_media_hours"] == 5.0
    assert s["media_stale_hours"] == 4.0


def test_case_board_text_lines_only_when_actionable():
    assert case_board_text_lines({"open": 2, "urgent": 0, "media_stale": 0,
                                  "open_drill": 0}) == []
    lines = case_board_text_lines({
        "urgent": 2,
        "media_stale": 1,
        "media_stale_hours": DEFAULT_MEDIA_STALE_HOURS,
        "oldest_media_hours": 6.5,
        "open_drill": 3,
    })
    assert any("紧急" in x for x in lines)
    assert any("媒体质疑" in x and "6.5" in x for x in lines)
    assert any("演练" in x and "3" in x for x in lines)


def test_api_cases_active_summary_and_close_bucket_note(cases_env):
    client, store = cases_env
    now = time.time()
    ctx = {"last_message": "这张照片是假的吧"}
    open_case(ctx, "u_media", "media_complaint", "case.reason.media_fake",
              quote="这张照片是假的吧", mid="mid-abc", now=now - 5 * 3600)
    store._cache["u_media"] = ctx
    body = client.get("/api/cases/active").json()
    assert "summary" in body
    assert body["summary"]["media_open"] == 1
    assert body["summary"]["media_stale"] == 1
    assert body["summary"]["open_drill"] == 0
    row = body["cases"][0]
    assert row["anchor_mid"] == "mid-abc"
    cid = row["case_id"]
    r = client.post(f"/api/cases/{cid}/close", json={"resolution": "已安抚"})
    assert r.status_code == 200
    assert r.json()["resolution_bucket"] == "soothed"
    assert store._cache["u_media"]["_case_resolution_bucket"] == "soothed"
    # 备注空 → 抄结案原因（列表备注列立刻可读）
    assert store._cache["u_media"]["_case_note"] == "已安抚"
    closed = client.get("/api/cases/active").json()["cases"][0]
    assert closed["resolution_bucket"] == "soothed"


def test_media_complaint_open_case_wiring_uses_resolve_inbound_mid():
    """静态契约：A/B 共用的媒体投诉开案必须显式 mid=resolve_inbound_mid。"""
    from pathlib import Path
    src = Path("src/skills/skill_manager.py").read_text(encoding="utf-8")
    assert "def _maybe_flag_media_complaint" in src
    assert "mid=resolve_inbound_mid(user_context)" in src
    assert "open_case(user_context" in src


# ── P5：误报静默（结案时显式勾选，仅拦同来源新立案） ─────────────────────────

def test_close_mute_suppresses_same_source_until_expiry():
    ctx = {}
    t0 = 1_700_000_000.0
    open_case(ctx, "u1", "media_complaint", "case.reason.media_fake", now=t0)
    close_case(ctx, "误报", now=t0 + 60, mute_same_source_hours=24)
    assert ctx["_case_mute_source"] == "media_complaint"
    assert ctx["_case_mute_until"] == t0 + 60 + 24 * 3600
    # 窗内同来源 → 拦下，不开新案、旧结案卡不被归档（淡出窗内仍可见）
    cid, action = open_case(ctx, "u1", "media_complaint",
                            "case.reason.media_fake", now=t0 + 3600)
    assert (cid, action) == ("", "muted")
    assert ctx.get("_case_closed") is True
    # 跨来源不受影响（要求人工照常开案）
    cid2, action2 = open_case(ctx, "u1", "human_request",
                              "case.reason.human_request", now=t0 + 3700)
    assert action2 == "created" and cid2
    close_case(ctx, "ok", now=t0 + 3800)
    # 窗后同来源 → 照常开案，且过期静默键被清
    cid3, action3 = open_case(ctx, "u1", "media_complaint",
                              "case.reason.media_fake",
                              now=t0 + 60 + 24 * 3600 + 10)
    assert action3 == "created" and cid3
    assert "_case_mute_source" not in ctx and "_case_mute_until" not in ctx


def test_close_mute_never_applies_to_crisis():
    ctx = {}
    t0 = 1_700_000_000.0
    open_case(ctx, "u1", "crisis", "case.reason.crisis", now=t0)
    close_case(ctx, "误报", now=t0 + 60, mute_same_source_hours=24)
    # crisis 硬排除：静默键根本不落
    assert "_case_mute_source" not in ctx
    cid, action = open_case(ctx, "u1", "crisis", "case.reason.crisis",
                            now=t0 + 3600)
    assert action == "created" and cid


def test_mute_suppression_counted_in_stats(monkeypatch):
    import src.utils.case_stats as cs
    stats = cs.CaseStats()
    monkeypatch.setattr(cs, "get_case_stats", lambda: stats)
    ctx = {}
    t0 = 1_700_000_000.0
    open_case(ctx, "u1", "media_complaint", "case.reason.media_fake", now=t0)
    close_case(ctx, "误报", now=t0 + 60, mute_same_source_hours=4)
    open_case(ctx, "u1", "media_complaint", "case.reason.media_fake",
              now=t0 + 3600)
    d = stats.dump()
    assert d["suppressed"] == 1
    assert d["suppressed_by_source"]["media_complaint"] == 1
    assert "ws_cases_suppressed_total 1" in stats.dump_prom()


def test_mute_hours_clamped_to_max():
    from src.utils.case_center import FALSE_ALARM_MUTE_MAX_HOURS
    ctx = {}
    t0 = 1_700_000_000.0
    open_case(ctx, "u1", "ai_doubt", "case.reason.ai_doubt", now=t0)
    close_case(ctx, "误报", now=t0, mute_same_source_hours=99999)
    assert ctx["_case_mute_until"] == t0 + FALSE_ALARM_MUTE_MAX_HOURS * 3600


def test_api_case_close_mute_hours_applied_and_crisis_excluded(cases_env):
    client, store = cases_env
    ctx = {}
    open_case(ctx, "u_fa", "media_complaint", "case.reason.media_fake",
              quote="假的吧", now=time.time() - 600)
    store._cache["u_fa"] = ctx
    cid = ctx["_case_id"]
    r = client.post(f"/api/cases/{cid}/close",
                    json={"resolution": "误报", "mute_hours": 24})
    assert r.status_code == 200
    body = r.json()
    assert body["resolution_bucket"] == "false_alarm"
    assert body["muted_hours"] == 24
    assert store._cache["u_fa"]["_case_mute_source"] == "media_complaint"
    # 列表行带 mute_until（前端标注「同类静默至」）
    row = client.get("/api/cases/active").json()["cases"][0]
    assert row["mute_until"] > time.time()
    # crisis：勾了也不静默（回包 muted_hours=0，键不落）
    ctx2 = {}
    open_case(ctx2, "u_cr", "crisis", "case.reason.crisis",
              now=time.time() - 600)
    store._cache["u_cr"] = ctx2
    r2 = client.post(f"/api/cases/{ctx2['_case_id']}/close",
                     json={"resolution": "误报", "mute_hours": 24})
    assert r2.status_code == 200
    assert r2.json()["muted_hours"] == 0
    assert "_case_mute_source" not in store._cache["u_cr"]


def test_count_open_drill_cases_ignores_real_customers():
    store = _FakeStore()
    real = {}
    open_case(real, "u_real", "human_request", "case.reason.human_request")
    drill = {}
    open_case(drill, "990001004", "media_complaint",
              "case.reason.media_lie_caught")
    store._cache["u_real"] = real
    store._cache["990001004"] = drill
    assert count_open_cases(store) == 1
    assert count_open_drill_cases(store) == 1


# ── P6：安抚有效性回环 + 演练定龄清扫 + 转人工接管 ───────────────────────────

def _hist_entry(created, closed, bucket, source="media_complaint"):
    return {
        "case_id": f"C-{int(created)}", "source": source,
        "created_at": created, "closed_at": closed,
        "resolution": "x", "resolution_bucket": bucket,
        "note": "", "claimed_by": "",
    }


def test_media_resolution_effectiveness_recurrence_and_immature():
    from src.utils.case_center import media_resolution_effectiveness
    now = 1_700_000_000.0
    store = _FakeStore()
    # A：10 天前安抚结案，1h 后同源复发（复发案现役未结）→ recurred
    a = {"_case_id": "CA", "_case_source": "media_complaint",
         "_case_created_at": now - 10 * 86400 + 3600,
         "_case_history": [
             _hist_entry(now - 10 * 86400 - 3600, now - 10 * 86400, "soothed")]}
    # B：1h 前误报结案、无复发 → immature（还没满 7 天窗，不算「没复发」定论）
    b = {"_case_id": "CB", "_case_source": "media_complaint",
         "_case_created_at": now - 7200, "_case_closed": True,
         "_case_closed_at": now - 3600,
         "_case_resolution_bucket": "false_alarm"}
    # C：演练号段整体跳过
    c = {"_case_id": "CC", "_case_source": "media_complaint",
         "_case_created_at": now - 3600,
         "_case_history": [
             _hist_entry(now - 86400, now - 43200, "soothed")]}
    # D：超出 lookback（40 天前）不进分母
    d = {"_case_history": [
        _hist_entry(now - 41 * 86400, now - 40 * 86400, "soothed")]}
    # E：跨来源不串——human_request 的结案不进媒体口径
    e = {"_case_id": "CE", "_case_source": "media_complaint",
         "_case_created_at": now - 1800,
         "_case_history": [
             _hist_entry(now - 7200, now - 3600, "handoff",
                         source="human_request")]}
    store._cache.update({"uA": a, "uB": b, "990001004": c, "uD": d, "uE": e})
    eff = media_resolution_effectiveness(store, now=now)
    assert eff["closed"] == 2 and eff["recurred"] == 1
    soothed = eff["by_bucket"]["soothed"]
    assert soothed["closed"] == 1 and soothed["recurred"] == 1
    assert soothed["immature"] == 0
    # P8：复发会话样本（建议条点击过滤的深链原料）
    assert soothed["sample_uids"] == ["uA"]
    assert eff["by_bucket"]["false_alarm"] == {
        "closed": 1, "recurred": 0, "immature": 1}
    assert "handoff" not in eff["by_bucket"]


def test_media_resolution_effectiveness_no_recurrence_outside_window():
    from src.utils.case_center import media_resolution_effectiveness
    now = 1_700_000_000.0
    store = _FakeStore()
    # 结案 20 天前、复发发生在 10 天后（> 7 天窗）→ 不算复发、已满窗
    store._cache["u1"] = {
        "_case_id": "CX", "_case_source": "media_complaint",
        "_case_created_at": now - 10 * 86400,
        "_case_history": [
            _hist_entry(now - 21 * 86400, now - 20 * 86400, "soothed")]}
    eff = media_resolution_effectiveness(store, now=now)
    assert eff["by_bucket"]["soothed"] == {
        "closed": 1, "recurred": 0, "immature": 0}


def test_close_open_drill_cases_min_age_keeps_running_drill():
    now = 1_700_000_000.0
    store = _FakeStore()
    old = {}
    open_case(old, "990001004", "media_complaint",
              "case.reason.media_lie_caught", now=now - 7 * 3600)
    fresh = {}
    open_case(fresh, "990001005", "media_complaint",
              "case.reason.media_lie_caught", now=now - 600)
    store._cache["990001004"] = old
    store._cache["990001005"] = fresh
    n = close_open_drill_cases(store, now=now, persist=False,
                               min_age_hours=6.0)
    assert n == 1
    assert store._cache["990001004"]["_case_closed"] is True
    assert not store._cache["990001005"].get("_case_closed")
    # min_age=0 保持旧语义：全清
    n2 = close_open_drill_cases(store, now=now, persist=False)
    assert n2 == 1
    assert store._cache["990001005"]["_case_closed"] is True


def test_api_case_close_takeover_sets_manual_mode(cases_env, monkeypatch):
    client, store = cases_env
    calls = []

    class _FakeInbox:
        def set_automation_mode(self, cid, mode, *, source=""):
            calls.append((cid, mode, source))

    import src.web.routes.unified_inbox_services as svc
    monkeypatch.setattr(svc, "_inbox_store", lambda request: _FakeInbox())
    ctx = {"conversation_id": "telegram:acct1:777"}
    open_case(ctx, "u_ho", "human_request", "case.reason.human_request",
              now=time.time() - 600)
    store._cache["u_ho"] = ctx
    r = client.post(f"/api/cases/{ctx['_case_id']}/close",
                    json={"resolution": "已转人工", "takeover": True})
    assert r.status_code == 200
    body = r.json()
    assert body["resolution_bucket"] == "handoff"
    assert body["takeover"] is True
    assert calls == [("telegram:acct1:777", "manual", "case:handoff")]
    # inbox store 未挂载 → soft-fail：结案照常、takeover=False
    monkeypatch.setattr(svc, "_inbox_store", lambda request: None)
    ctx2 = {"conversation_id": "telegram:acct1:778"}
    open_case(ctx2, "u_ho2", "human_request", "case.reason.human_request",
              now=time.time() - 600)
    store._cache["u_ho2"] = ctx2
    r2 = client.post(f"/api/cases/{ctx2['_case_id']}/close",
                     json={"resolution": "已转人工", "takeover": True})
    assert r2.status_code == 200
    assert r2.json()["takeover"] is False
    assert store._cache["u_ho2"]["_case_closed"] is True


def test_api_cases_active_effectiveness_block(cases_env):
    client, store = cases_env
    now = time.time()
    store._cache["u_eff"] = {
        "_case_id": "CEF", "_case_source": "media_complaint",
        "_case_created_at": now - 9 * 86400,
        "last_message": "x",
        "_case_history": [
            _hist_entry(now - 10 * 86400, now - 10 * 86400 + 3600, "soothed")]}
    body = client.get("/api/cases/active").json()
    eff = body.get("effectiveness")
    assert eff and eff["source"] == "media_complaint"
    assert eff["by_bucket"]["soothed"]["closed"] == 1
    assert eff["by_bucket"]["soothed"]["recurred"] == 1
    # P7：单样本（<min_matured=3）不点名——小样本 1/1 不该吓人
    assert eff.get("alerts") == []


# ── P7：复发率告警阈值 / 周报行 / 人工接管闭环 ───────────────────────────────

def test_effectiveness_alerts_need_sample_and_rate():
    from src.utils.case_center import effectiveness_alerts
    eff = {"by_bucket": {
        # 样本够 + 复发率 2/3=0.67 ≥ 0.5 → 点名
        "soothed": {"closed": 3, "recurred": 2, "immature": 0,
                    "sample_uids": ["u1", "u2"]},
        # 复发率 1/4=0.25 < 0.5 → 不点名
        "handoff": {"closed": 4, "recurred": 1, "immature": 0},
        # 已满窗只有 2（<3）→ 样本不够不点名（哪怕 2/2=100%）
        "false_alarm": {"closed": 3, "recurred": 2, "immature": 1},
    }}
    alerts = effectiveness_alerts(eff)
    assert [a["bucket"] for a in alerts] == ["soothed"]
    assert alerts[0]["recurred"] == 2 and alerts[0]["matured"] == 3
    assert alerts[0]["rate"] == 0.67
    # P8：复发会话样本透传（前端点击建议条→过滤）
    assert alerts[0]["sample_uids"] == ["u1", "u2"]


def test_effectiveness_text_lines_zh():
    from src.utils.case_center import effectiveness_text_lines
    eff = {"by_bucket": {
        "soothed": {"closed": 4, "recurred": 3, "immature": 0}}}
    lines = effectiveness_text_lines(eff)
    assert len(lines) == 1
    assert "安抚" in lines[0] and "3/4" in lines[0] and "75%" in lines[0]
    assert effectiveness_text_lines({"by_bucket": {}}) == []


def test_summarize_takeover_episodes_median_and_open():
    from src.utils.case_center import summarize_takeover_episodes
    now = 1_700_000_000.0
    eps = [
        {"conversation_id": "a", "start": now - 10 * 3600, "end": now - 8 * 3600},   # 2h
        {"conversation_id": "b", "start": now - 10 * 3600, "end": now - 4 * 3600},   # 6h
        {"conversation_id": "c", "start": now - 9 * 3600, "end": now - 5 * 3600},    # 4h
        {"conversation_id": "d", "start": now - 3 * 3600, "end": 0.0},               # 进行中
    ]
    s = summarize_takeover_episodes(eps, now=now)
    assert s["count"] == 4 and s["open"] == 1
    assert s["median_hours"] == 4.0
    assert s["oldest_open_hours"] == 3.0
    empty = summarize_takeover_episodes([], now=now)
    assert empty["count"] == 0 and empty["median_hours"] is None


def test_inbox_store_takeover_episodes_pairing(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    try:
        cid = "telegram:acct1:777"
        store.set_automation_mode(cid, "manual", source="case:handoff")
        store.set_automation_mode(cid, "auto_ai", source="rearm")   # 回流
        store.set_automation_mode("telegram:acct1:888", "manual",
                                  source="case:handoff")            # 未回流
        # 非 case: 前缀的人肉接管不进本口径
        store.set_automation_mode("telegram:acct1:999", "manual", source="human")
        eps = store.takeover_episodes()
        by_cid = {e["conversation_id"]: e for e in eps}
        assert set(by_cid) == {cid, "telegram:acct1:888"}
        assert by_cid[cid]["end"] > by_cid[cid]["start"] > 0
        assert by_cid["telegram:acct1:888"]["end"] == 0.0
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_api_cases_active_takeover_stats(cases_env, monkeypatch):
    client, store = cases_env
    store._cache["u1"] = _case_ctx("u1")
    now = time.time()

    class _FakeInbox:
        def takeover_episodes(self, **kw):
            return [{"conversation_id": "x", "start": now - 7200,
                     "end": now - 3600}]

    import src.web.routes.unified_inbox_services as svc
    monkeypatch.setattr(svc, "_inbox_store", lambda request: _FakeInbox())
    body = client.get("/api/cases/active").json()
    tk = body.get("takeover_stats")
    assert tk and tk["count"] == 1 and tk["open"] == 0
    assert tk["median_hours"] == 1.0


def test_weekly_report_wires_effectiveness_lines():
    """静态契约：周报 value_lines 装配必须带处置质量行（P7）。"""
    from pathlib import Path
    src = Path("src/inbox/health_watchdog.py").read_text(encoding="utf-8")
    assert "effectiveness_text_lines" in src
    assert "media_resolution_effectiveness(ctx_store, now=ts)" in src


# ── P8：开案告警带历史处置上下文 ─────────────────────────────────────────────

def test_prior_resolution_summary_buckets_and_ago():
    from src.utils.case_center import prior_resolution_summary
    now = 1_700_000_000.0
    ctx = {"_case_history": [
        _hist_entry(now - 10 * 3600, now - 5 * 3600, "soothed"),
        _hist_entry(now - 30 * 3600, now - 25 * 3600, "soothed"),
        _hist_entry(now - 50 * 3600, now - 48 * 3600, "handoff"),
        # 跨来源不串
        _hist_entry(now - 8 * 3600, now - 7 * 3600, "false_alarm",
                    source="human_request"),
    ]}
    p = prior_resolution_summary(ctx, "media_complaint", now=now)
    assert p["buckets"] == {"soothed": 2, "handoff": 1}
    assert p["last_closed_ago_hours"] == 5.0
    assert prior_resolution_summary({}, "media_complaint", now=now) is None
    assert prior_resolution_summary(ctx, "crisis", now=now) is None


def test_case_alert_carries_prior_resolutions(monkeypatch):
    events = []

    class _Bus:
        def publish(self, name, payload):
            events.append((name, payload))

    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: _Bus())
    now = 1_700_000_000.0
    ctx = {"_case_history": [
        _hist_entry(now - 10 * 3600, now - 5 * 3600, "soothed"),
    ], "last_message": "这照片还是假的吧"}
    open_case(ctx, "u_prior", "media_complaint", "case.reason.media_fake",
              now=now)
    alerts = [p for n, p in events if n == "case_alert"]
    assert len(alerts) == 1
    prior = alerts[0].get("prior_resolutions")
    assert prior and prior["buckets"] == {"soothed": 1}
    assert prior["last_closed_ago_hours"] == 5.0
    # 无历史 → 不带字段（文案不出空行）
    events.clear()
    ctx2 = {"last_message": "找真人"}
    open_case(ctx2, "u_fresh", "human_request", "case.reason.human_request",
              now=now)
    alerts2 = [p for n, p in events if n == "case_alert"]
    assert len(alerts2) == 1 and "prior_resolutions" not in alerts2[0]
