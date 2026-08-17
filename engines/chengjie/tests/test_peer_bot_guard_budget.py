# -*- coding: utf-8 -*-
"""每日预算 P2 门禁（2026-08-04 198「全自动静默哑火」事故修复）。

事故链（backend.log + inbox.db 实锤）：会话当日 40 条出站里 27 条是**手动档
人工发的**，切全自动后 AI 只跑了 7 轮（13 条）就触顶 → A 线跳过 + AutoDraft
一并跳过拟稿 → 客户被已读不回、坐席界面零提示。三层修复各有门禁：

1. 台账口径：预算分子＝peer_reply_ledger「自动链回复轮次」，人工手发/双气泡
   拆条不再挤占；台账不可用回落旧口径（宁严勿松）。
2. 真人软停：触顶且无 bot 证据 → B 线仍拟稿但强制 review；bot/超 2× 硬顶全停。
3. 坐席救济：relief_day=当日 → 当日整体跳过预算，跨日自动失效。

重点覆盖**不该拦/不该计**的边界（与 test_peer_bot_guard.py 同哲学）。
"""
import time
from unittest.mock import MagicMock

import pytest

from src.inbox import peer_bot_guard as pbg
from src.inbox.store import InboxStore


@pytest.fixture(autouse=True)
def _reset():
    pbg._reset_for_tests()
    yield
    pbg._reset_for_tests()


@pytest.fixture()
def store(tmp_path):
    return InboxStore(tmp_path / "inbox_budget_test.db")


def _cfg(**over):
    return {"inbox": {"peer_bot_guard": dict({"enabled": True}, **over)}}


def _msg(direction, text, ts):
    return {"direction": direction, "text": text, "ts": ts}


# ── 台账（store 层） ────────────────────────────────────────────────────

def test_ledger_bump_and_day_rollover(store):
    cid = "telegram:a:1"
    assert store.bump_auto_reply(cid, "20260804") == 1
    assert store.bump_auto_reply(cid, "20260804") == 2
    led = store.get_auto_reply_ledger(cid)
    assert led["day"] == "20260804" and led["auto_replies"] == 2
    # 跨日：计数自动清零重记，不需要任何清理任务
    assert store.bump_auto_reply(cid, "20260805") == 1
    led = store.get_auto_reply_ledger(cid)
    assert led["day"] == "20260805" and led["auto_replies"] == 1


def test_ledger_relief_roundtrip(store):
    cid = "telegram:a:2"
    store.bump_auto_reply(cid, "20260804")
    assert store.set_budget_relief(cid, "20260804") is True
    led = store.get_auto_reply_ledger(cid)
    # 救济不清计数（观测口径保留），只标记 relief_day
    assert led["relief_day"] == "20260804" and led["auto_replies"] == 1
    # 救济后继续计数不冲掉 relief_day
    store.bump_auto_reply(cid, "20260804")
    assert store.get_auto_reply_ledger(cid)["relief_day"] == "20260804"
    # 无行会话读到空值行（不抛）
    assert store.get_auto_reply_ledger("telegram:a:none")["auto_replies"] == 0


# ── evaluate：口径 + 软停/硬停/救济 ────────────────────────────────────

def test_evaluate_prefers_ledger_over_messages():
    """台账口径优先：messages 里 50 条出站（旧口径必拦）也不作数。

    这正是 198 事故的修复点——人工手发出站不再挤占 AI 预算。
    """
    now = 1754000000.0
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    msgs = [_msg("out", f"人工{i}", now - 60 * i) for i in range(50)]
    v = pbg.evaluate(msgs, cfg, username="lc334456", chat_type="private",
                     now=now, auto_out_today=3)
    assert not v.blocked
    v2 = pbg.evaluate(msgs, cfg, username="lc334456", chat_type="private",
                      now=now, auto_out_today=5)
    assert v2.blocked and v2.reason == "daily_budget"


def test_evaluate_budget_soft_semantics():
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    # 真人触顶（<2×）→ 软停：仍拟稿转人审
    v = pbg.evaluate([], cfg, username="lc334456", chat_type="private",
                     auto_out_today=5)
    assert v.blocked and v.reason == "daily_budget" and v.budget_soft
    assert v.downgrade_to == ""   # 只停不降档，明天自动恢复
    # 已判定 bot（peer_override=1）→ 硬停（拟稿也停）
    v_bot = pbg.evaluate([], cfg, username="lc334456", chat_type="private",
                         auto_out_today=5, peer_override=1)
    assert v_bot.blocked and not v_bot.budget_soft
    # 超 2× 硬顶 → 真人也硬停（防对面 LLM 无限烧拟稿）
    v_hard = pbg.evaluate([], cfg, username="lc334456", chat_type="private",
                          auto_out_today=10)
    assert v_hard.blocked and not v_hard.budget_soft


def test_evaluate_relieved_skips_budget_entirely():
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    v = pbg.evaluate([], cfg, username="lc334456", chat_type="private",
                     auto_out_today=50, budget_relieved=True)
    assert not v.blocked


def test_evaluate_legacy_fallback_without_ledger():
    """auto_out_today=None（旧 store/测试假件）→ 回落 messages 全出站旧口径。"""
    now = 1754000000.0
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    msgs = [_msg("out", f"m{i}", now - 60 * i) for i in range(5)]
    v = pbg.evaluate(msgs, cfg, username="lc334456", chat_type="private", now=now)
    assert v.blocked and v.reason == "daily_budget"


# ── budget_state（横幅/救济 API 的单一事实源） ──────────────────────────

def test_budget_state_lifecycle(store):
    cid = "telegram:a:3"
    day = pbg.today_key()
    cfg = _cfg(daily_reply_budget=3)
    s0 = pbg.budget_state(store, cid, cfg)
    assert s0["enabled"] and s0["limit"] == 3
    assert s0["used"] == 0 and not s0["exhausted"]
    for _ in range(3):
        store.bump_auto_reply(cid, day)
    s1 = pbg.budget_state(store, cid, cfg)
    assert s1["used"] == 3 and s1["exhausted"] and not s1["hard_stopped"]
    for _ in range(3):
        store.bump_auto_reply(cid, day)
    s2 = pbg.budget_state(store, cid, cfg)
    assert s2["exhausted"] and s2["hard_stopped"]
    store.set_budget_relief(cid, day)
    s3 = pbg.budget_state(store, cid, cfg)
    assert s3["relieved"] and not s3["exhausted"]


def test_budget_state_disabled_or_no_ledger():
    # 守卫关 → enabled False（横幅永不显示）
    s = pbg.budget_state(None, "cid", {"inbox": {"peer_bot_guard": {"enabled": False}}})
    assert not s["enabled"] and not s["exhausted"]
    # store 无台账能力 → used=0、不 exhausted（横幅宁缺勿错）
    s2 = pbg.budget_state(object(), "cid", _cfg())
    assert s2["used"] == 0 and not s2["exhausted"]


def test_yesterday_ledger_not_counted_today(store):
    """昨日计满今天必须清零可用——「明天自动恢复」的字面保证。"""
    cid = "telegram:a:4"
    now = time.time()
    yesterday = pbg.today_key(now - 86400)
    for _ in range(10):
        store.bump_auto_reply(cid, yesterday)
    cnt, relieved = pbg._ledger_state(store, cid, now=now)
    assert cnt == 0 and not relieved
    # 昨日的救济今天同样失效
    store.set_budget_relief(cid, yesterday)
    _, relieved2 = pbg._ledger_state(store, cid, now=now)
    assert not relieved2


# ── A 线接入口：放行计数只数 auto_ai ────────────────────────────────────

def _a_line(monkeypatch, store, cid_chat, config):
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    return pbg.guard_a_line_should_skip(
        config=config, account_id="acct", chat_id=cid_chat,
        message=None, current_text="hello")


def test_a_line_counts_auto_ai_turns(monkeypatch, store):
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acct", "777")
    store.set_automation_mode(cid, "auto_ai")
    cfg = _cfg(daily_reply_budget=5)
    cfg["inbox"]["auto_draft"] = {"automation_mode": "auto_ai"}
    assert _a_line(monkeypatch, store, "777", cfg) == ""
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 1
    assert _a_line(monkeypatch, store, "777", cfg) == ""
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 2


def test_a_line_manual_review_turns_not_counted(monkeypatch, store):
    """手动/人审档 A 线随后会让位（档位闸在守卫之后）——不许挤占预算。

    198 事故的错口径回归钉：手动档人工聊 27 条曾把 AI 预算吃掉 2/3。
    """
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acct", "888")
    store.set_automation_mode(cid, "manual")
    assert _a_line(monkeypatch, store, "888", _cfg(daily_reply_budget=5)) == ""
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 0
    store.set_automation_mode(cid, "review")
    assert _a_line(monkeypatch, store, "888", _cfg(daily_reply_budget=5)) == ""
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 0


def test_a_line_blocks_at_budget_from_ledger(monkeypatch, store):
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acct", "999")
    store.set_automation_mode(cid, "auto_ai")
    day = pbg.today_key()
    for _ in range(5):
        store.bump_auto_reply(cid, day)
    r = _a_line(monkeypatch, store, "999", _cfg(daily_reply_budget=5))
    assert r == "daily_budget"
    snap = pbg.stats_snapshot()
    assert snap["budget_hits"] == 1 and snap["budget_softened"] == 1
    # 救济后立即恢复放行（且触顶轮不重复计）
    store.set_budget_relief(cid, day)
    assert _a_line(monkeypatch, store, "999", _cfg(daily_reply_budget=5)) == ""


# ── B 线接入口：action 语义 + 旧包装兼容 ────────────────────────────────

def _conv_row(store, cid):
    # 真 store 无会话行也能工作（get_conversation → None → row={}）
    return {"conversation_id": cid, "platform": "whatsapp",
            "account_id": "acct", "chat_key": "k"}


def test_auto_draft_action_soft_vs_hard(store):
    cid = "whatsapp:acct:k1"
    day = pbg.today_key()
    for _ in range(5):
        store.bump_auto_reply(cid, day)
    reason, soft = pbg.guard_auto_draft_action(
        conv=_conv_row(store, cid), store=store,
        config=_cfg(daily_reply_budget=5))
    assert reason == "daily_budget" and soft
    # 旧包装：软停＝拟稿要继续 → 空串（不跳过）
    assert pbg.guard_auto_draft_should_skip(
        conv=_conv_row(store, cid), store=store,
        config=_cfg(daily_reply_budget=5)) == ""
    # 超 2× 硬顶：action 硬停、旧包装恢复「跳过」语义
    for _ in range(5):
        store.bump_auto_reply(cid, day)
    reason2, soft2 = pbg.guard_auto_draft_action(
        conv=_conv_row(store, cid), store=store,
        config=_cfg(daily_reply_budget=5))
    assert reason2 == "daily_budget" and not soft2
    assert pbg.guard_auto_draft_should_skip(
        conv=_conv_row(store, cid), store=store,
        config=_cfg(daily_reply_budget=5)) == "daily_budget"


# ── AutoDraft 回调端到端：软停转 review + 轮次计数 ──────────────────────

def _run_cb(store, app_config, conv, text="hello there friend"):
    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    cfg = AutoDraftConfig(
        mode="auto_ai", min_len=0, skip=set(), platform_ceilings={},
        skip_groups=True, enrich=False)
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "draft-1"
    cb = make_auto_draft_cb(cfg, ds, store, MagicMock(), MagicMock(),
                            MagicMock(), app_config=app_config)
    cb(conv, text)
    return ds


def test_autodraft_budget_soft_forces_review(store):
    """软停轮：拟稿继续但强制 review——修「A 线停 + B 线也跳过 = 无人拟稿」。"""
    cid = "whatsapp:acct:k2"
    day = pbg.today_key()
    for _ in range(5):
        store.bump_auto_reply(cid, day)
    app_config = _cfg(daily_reply_budget=5)
    app_config["inbox"]["auto_draft"] = {"automation_mode": "auto_ai"}
    ds = _run_cb(store, app_config, _conv_row(store, cid))
    ds.auto_generate_draft.assert_called_once()
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"
    # 软停轮转人审＝人的决定，不再计入自动预算
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 5


def test_autodraft_budget_hard_still_skips(store):
    cid = "whatsapp:acct:k3"
    day = pbg.today_key()
    for _ in range(10):   # 2× 硬顶
        store.bump_auto_reply(cid, day)
    app_config = _cfg(daily_reply_budget=5)
    app_config["inbox"]["auto_draft"] = {"automation_mode": "auto_ai"}
    ds = _run_cb(store, app_config, _conv_row(store, cid))
    ds.auto_generate_draft.assert_not_called()


def test_autodraft_counts_auto_ai_turn(store):
    """预算内的 auto_ai 拟稿轮 +1；review 会话（人审）不计。"""
    cid = "whatsapp:acct:k4"
    app_config = _cfg(daily_reply_budget=5)
    app_config["inbox"]["auto_draft"] = {"automation_mode": "auto_ai"}
    ds = _run_cb(store, app_config, _conv_row(store, cid))
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"
    assert store.get_auto_reply_ledger(cid)["auto_replies"] == 1
    # 显式 review 会话：拟稿照常、预算不计
    cid2 = "whatsapp:acct:k5"
    store.set_automation_mode(cid2, "review")
    ds2 = _run_cb(store, app_config, _conv_row(store, cid2))
    assert ds2.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"
    assert store.get_auto_reply_ledger(cid2)["auto_replies"] == 0


# ── budget_flags（P0-guard：横幅与设置页列表的共用语义核） ──────────────

def test_budget_flags_zero_budget_means_unlimited():
    """daily_reply_budget=0 ＝ 预算检查整体关闭（不限额），**不是**「全拦」。

    设置页 UI clamp [5,500] 的依据正是这条语义（0 只许 YAML 配，防运营
    把 0 当全拦/把全拦当 0 误设）；与 evaluate 的 ``budget > 0`` 闸同源。
    """
    cfg0 = pbg.parse_cfg(_cfg(daily_reply_budget=0))
    f = pbg.budget_flags(9999, False, cfg0)
    assert not f["enabled"] and not f["exhausted"] and not f["hard_stopped"]
    v = pbg.evaluate([], cfg0, username="lc334456", chat_type="private",
                     auto_out_today=9999)
    assert not v.blocked


def test_budget_flags_near_limit_signal():
    """near（≥80% 预警，P1）：触顶前的提前量信号，三个消费面（收件箱横幅预警/
    设置页「接近」chip/watchdog 聚合计数）同源。边界与互斥关系钉死：
    near 与 exhausted 互斥、救济/守卫关/额度 0 一律不 near。"""
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=40))
    assert pbg.budget_flags(31, False, cfg)["near"] is False   # 31/40 = 77.5%
    assert pbg.budget_flags(32, False, cfg)["near"] is True    # 32/40 = 80% 整
    f39 = pbg.budget_flags(39, False, cfg)
    assert f39["near"] and not f39["exhausted"]
    f40 = pbg.budget_flags(40, False, cfg)
    assert f40["exhausted"] and not f40["near"]                # 互斥：触顶不再算接近
    assert pbg.budget_flags(39, True, cfg)["near"] is False    # 已救济不预警
    cfg_off = pbg.parse_cfg(_cfg(enabled=False, daily_reply_budget=40))
    assert pbg.budget_flags(39, False, cfg_off)["near"] is False
    cfg0 = pbg.parse_cfg(_cfg(daily_reply_budget=0))
    assert pbg.budget_flags(9999, False, cfg0)["near"] is False
    # 小额度整数算术不糊边界：5 轮额度 → 4 轮即 80%
    cfg5 = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    assert pbg.budget_flags(3, False, cfg5)["near"] is False
    assert pbg.budget_flags(4, False, cfg5)["near"] is True


def test_clear_budget_relief_roundtrip(store):
    """撤销豁免（P1 后悔药）：清 relief_day 立即恢复预算判定；计数保留；幂等。"""
    cid = "telegram:a:revoke"
    day = pbg.today_key()
    cfg = _cfg(daily_reply_budget=3)
    for _ in range(3):
        store.bump_auto_reply(cid, day)
    store.set_budget_relief(cid, day)
    assert pbg.budget_state(store, cid, cfg)["relieved"]
    assert store.clear_budget_relief(cid) is True
    s = pbg.budget_state(store, cid, cfg)
    assert not s["relieved"] and s["exhausted"] and s["used"] == 3  # 计数没被动
    # 幂等：本就未豁免/无行会话同样返回 True（终态一致即成功）
    assert store.clear_budget_relief(cid) is True
    assert store.clear_budget_relief("telegram:a:norow") is True
    assert store.clear_budget_relief("") is False


def test_budget_flags_matches_budget_state(store):
    """budget_state ＝ budget_flags(台账值)——横幅与设置页永不分叉的契约。"""
    cid = "telegram:a:flags"
    day = pbg.today_key()
    cfg = _cfg(daily_reply_budget=3)
    parsed = pbg.parse_cfg(cfg)
    for target in (0, 3, 6):   # 未触顶 / 触顶软停带 / 2× 硬顶
        while store.get_auto_reply_ledger(cid)["auto_replies"] < target:
            store.bump_auto_reply(cid, day)
        led = store.get_auto_reply_ledger(cid)
        assert pbg.budget_state(store, cid, cfg) == pbg.budget_flags(
            led["auto_replies"], led["relief_day"] == day, parsed)
    store.set_budget_relief(cid, day)
    led = store.get_auto_reply_ledger(cid)
    assert pbg.budget_state(store, cid, cfg) == pbg.budget_flags(
        led["auto_replies"], True, parsed)


# ── store.list_reply_budget_today（设置页「今日额度状态」表数据源） ─────

def _seed_conv(store, cid, platform, acct, chat, name):
    from src.inbox.models import InboxConversation
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform=platform, account_id=acct,
        chat_key=chat, display_name=name))


def test_list_reply_budget_today_join_and_order(store):
    day = pbg.today_key()
    _seed_conv(store, "telegram:a:hot", "telegram", "a", "hot", "话痨客户")
    _seed_conv(store, "telegram:a:cold", "telegram", "a", "cold", "冷淡客户")
    for _ in range(7):
        store.bump_auto_reply("telegram:a:hot", day)
    store.bump_auto_reply("telegram:a:cold", day)
    rows = store.list_reply_budget_today(day)
    # used 降序 + JOIN 出显示名与豁免所需三元组
    assert [r["conversation_id"] for r in rows] == [
        "telegram:a:hot", "telegram:a:cold"]
    top = rows[0]
    assert top["used"] == 7 and not top["relieved"]
    assert (top["platform"], top["account_id"], top["chat_key"]) == (
        "telegram", "a", "hot")
    assert top["display_name"] == "话痨客户"


def test_list_reply_budget_today_relief_rollover_and_orphan(store):
    """昨日行不出现；今日救济行要出现（used 按跨日口径清零）；
    台账孤儿（无会话行）如实返回、三元组空串（豁免按钮不亮）。"""
    day = pbg.today_key()
    yesterday = pbg.today_key(time.time() - 86400)
    store.bump_auto_reply("telegram:a:old", yesterday)     # 纯昨日 → 不出现
    store.bump_auto_reply("telegram:a:stale", yesterday)   # 昨日轮次+今日救济
    store.set_budget_relief("telegram:a:stale", day)
    rows = {r["conversation_id"]: r for r in store.list_reply_budget_today(day)}
    assert "telegram:a:old" not in rows
    st = rows["telegram:a:stale"]
    assert st["used"] == 0 and st["relieved"]              # 昨日轮次不算今天
    assert st["platform"] == "" and st["chat_key"] == ""   # 孤儿行如实空串
    assert store.list_reply_budget_today("") == []         # 空 day 防御
