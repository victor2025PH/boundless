# -*- coding: utf-8 -*-
"""微信客服 48h/5 条窗口配额守卫门禁（实施97 线 A）。

钉住四件事：① 判序与预留语义；② 记账幂等/乱序保护；③ 三个接线点（send_guard /
reply_split / holding_reply）真的按平台早退；④ 敏感词只拦自动链。
"""
from __future__ import annotations

import pytest

from src.inbox import kf_window_guard as G


@pytest.fixture()
def store():
    st = G.KfStateStore(":memory:")
    yield st
    st.close()


T0 = 1_700_000_000.0


def test_no_inbound_blocks_and_first_inbound_opens_turn(store):
    v = G.check("wkKF", "wmU1", now=T0, store=store)
    assert not v.allowed and v.reason == G.REASON_NO_INBOUND
    store.record_inbound("wkKF", "wmU1", T0)
    v = G.check("wkKF", "wmU1", now=T0 + 10, store=store)
    assert v.allowed and v.remaining == 5
    assert abs(v.window_remaining_sec - (48 * 3600 - 10)) < 1


def test_quota_countdown_reserve_and_exhaustion(store):
    store.record_inbound("wkKF", "wmU1", T0)
    for i in range(4):
        assert store.record_sent("wkKF", "wmU1") == i + 1
    # 剩 1 条：自动链让路给坐席，人工仍可发
    auto = G.check("wkKF", "wmU1", origin="auto", now=T0 + 1, store=store)
    assert not auto.allowed and auto.reason == G.REASON_RESERVED_FOR_MANUAL and auto.remaining == 1
    manual = G.check("wkKF", "wmU1", origin="manual", now=T0 + 1, store=store)
    assert manual.allowed and manual.remaining == 1
    store.record_sent("wkKF", "wmU1")
    for origin in ("auto", "manual"):
        v = G.check("wkKF", "wmU1", origin=origin, now=T0 + 1, store=store)
        assert not v.allowed and v.reason == G.REASON_QUOTA_EXHAUSTED
    # 客户再发一条 → 本轮重置
    store.record_inbound("wkKF", "wmU1", T0 + 100)
    assert G.check("wkKF", "wmU1", now=T0 + 101, store=store).remaining == 5


def test_window_expiry_and_close_by_fail_event(store):
    store.record_inbound("wkKF", "wmU1", T0)
    v = G.check("wkKF", "wmU1", now=T0 + 48 * 3600 + 1, store=store)
    assert not v.allowed and v.reason == G.REASON_WINDOW_EXPIRED
    # 平台回 fail_type 6（超 5 条）→ 关窗，直到客户下一条
    assert store.record_fail("wkKF", "wmU1", 6) is True
    v = G.check("wkKF", "wmU1", now=T0 + 1, store=store)
    assert not v.allowed and v.reason == G.REASON_WINDOW_CLOSED and v.matched == "fail_type_6"
    store.record_inbound("wkKF", "wmU1", T0 + 5)
    assert G.check("wkKF", "wmU1", now=T0 + 6, store=store).allowed
    # 非关窗类失败（13 安全限制）不关窗；非法值不抛
    assert store.record_fail("wkKF", "wmU1", 13) is False
    assert store.record_fail("wkKF", "wmU1", "x") is False


def test_inbound_replay_does_not_rewind_window(store):
    store.record_inbound("wkKF", "wmU1", T0 + 100)
    store.record_sent("wkKF", "wmU1", 2)
    store.record_inbound("wkKF", "wmU1", T0 + 50)   # 游标重放的旧消息
    turn = store.get_turn("wkKF", "wmU1")
    assert turn["last_inbound_ts"] == T0 + 100 and turn["sent_since_inbound"] == 2
    # 同一秒内客户的第二条（微信 send_time 秒级）是真实新消息 → 必须重开本轮（e2e 实锤）
    store.record_inbound("wkKF", "wmU1", T0 + 100)
    turn = store.get_turn("wkKF", "wmU1")
    assert turn["sent_since_inbound"] == 0


def test_cursor_roundtrip(store):
    assert store.get_cursor("wkKF") == ""
    store.set_cursor("wkKF", "C1")
    store.set_cursor("wkKF", "C2")
    assert store.get_cursor("wkKF") == "C2"


def test_cfg_resolution_and_disabled_passthrough(store):
    cfg = G.resolve_guard_cfg({"wechat_kf": {"window_guard": {
        "quota_per_turn": 9, "reserve_for_manual": 7, "window_sec": 10}}})
    assert cfg["quota_per_turn"] == 5 and cfg["reserve_for_manual"] == 4  # 封顶到官方硬规则
    assert cfg["window_sec"] == 60.0
    v = G.check("wkKF", "nobody", store=store,
                config={"wechat_kf": {"window_guard": {"enabled": False}}})
    assert v.allowed


def test_sensitive_terms_default_and_override():
    assert G.sensitive_hit("可以走 USDT 付款") == "usdt"
    assert G.sensitive_hit("加我微信聊") == "加我微信"
    assert G.sensitive_hit("可以加微信吗") == "加微信"
    assert G.sensitive_hit("今天天气不错") == ""
    assert G.sensitive_hit("赌气", ()) == ""
    terms = G.resolve_sensitive_terms({"wechat_kf": {"sensitive_terms": ["特价"]}})
    assert terms == ("特价",) and G.sensitive_hit("usdt", terms) == ""
    # 自动链拦、人工链放；非配额平台放
    assert G.auto_text_block_reason("wechat_kf", "转账给我") == "kf_sensitive_term:转账"
    assert G.auto_text_block_reason("wechat_kf", "转账给我", origin="manual") == ""
    assert G.auto_text_block_reason("telegram", "转账给我") == ""


def test_send_guard_wiring_blocks_only_quota_platform(monkeypatch, store):
    """send_blocked 是唯一出站收口——配额平台按本地记账拦，其它平台零影响。"""
    monkeypatch.setattr(G, "get_kf_state_store", lambda path=None: store)
    from src.integrations.shared.send_guard import send_blocked

    blocked, reason = send_blocked("wechat_kf", "wkKF", chat_key="wxkf:user:wmU1", notify=False)
    assert blocked and reason == G.REASON_NO_INBOUND
    store.record_inbound("wkKF", "wmU1")
    blocked, reason = send_blocked("wechat_kf", "wkKF", chat_key="wxkf:user:wmU1", notify=False)
    assert not blocked
    for _ in range(5):
        store.record_sent("wkKF", "wmU1")
    blocked, reason = send_blocked("wechat_kf", "wkKF", chat_key="wxkf:user:wmU1",
                                   notify=False, origin="manual")
    assert blocked and reason == G.REASON_QUOTA_EXHAUSTED
    # 非配额平台：即使 chat_key 同形也不判
    blocked, _ = send_blocked("telegram", "acct", chat_key="12345", notify=False)
    assert not blocked


def test_reply_split_and_holding_skip_quota_platform():
    from src.inbox.reply_split import should_split_for_delivery
    cfg = {"enabled": True, "orch_only": False, "skip_groups": True}
    assert should_split_for_delivery(cfg=cfg, platform="telegram", chat_key="123", orch_owns=True)
    assert not should_split_for_delivery(cfg=cfg, platform="wechat_kf",
                                         chat_key="wxkf:user:x", orch_owns=True)
    assert G.is_quota_platform("WeChat_KF") and not G.is_quota_platform("wechat")


async def test_holding_reply_returns_false_for_quota_platform():
    from src.inbox import holding_reply as H

    class _Cfg:
        config = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}

    class _Assistant:
        config = _Cfg()

    ok = await H.maybe_send_holding_reply(_Assistant(), "wechat_kf", "wkKF",
                                          "wxkf:user:wmU1", "wechat_kf:wkKF:wxkf:user:wmU1")
    assert ok is False
    assert H.metrics_snapshot().get("skipped_quota_platform", 0) >= 1


def test_snapshot_shape(monkeypatch, store):
    monkeypatch.setattr(G, "get_kf_state_store", lambda path=None: store)
    assert G.snapshot("telegram", "a", "b") == {}
    store.record_inbound("wkKF", "wmU1", T0)
    store.record_sent("wkKF", "wmU1", 4)
    s = G.snapshot("wechat_kf", "wkKF", "wxkf:user:wmU1", now=T0 + 60)
    assert s["quota"] == 5 and s["sent"] == 4 and s["remaining"] == 1
    assert s["manual_allowed"] is True and s["auto_allowed"] is False
    assert s["reason"] == G.REASON_RESERVED_FOR_MANUAL


def test_ui_snapshot_matches_window_guard_contract(monkeypatch, store):
    """send-caps.reply_window 契约（工作台倒计时信息带消费的字段）：与 window_guard.snapshot 同键同义。"""
    monkeypatch.setattr(G, "get_kf_state_store", lambda path=None: store)
    assert G.ui_snapshot("telegram", "a", "b") == {}
    ck = "wxkf:user:wmU1"
    # 从未入站：no_inbound → 前端灰显「等客户先开口」
    s0 = G.ui_snapshot("wechat_kf", "wkKF", ck, now=T0)
    assert s0["cap"] == 5 and s0["no_inbound"] is True and s0["remaining_sec"] == 0 and s0["manual_allowed"] is False
    # 开窗 + 发了 4 条：剩 1 条、倒计时接近 48h、预留 1 条给坐席
    store.record_inbound("wkKF", "wmU1", T0)
    store.record_sent("wkKF", "wmU1", 4)
    s1 = G.ui_snapshot("wechat_kf", "wkKF", ck, now=T0 + 60)
    assert s1["sent"] == 4 and s1["remaining"] == 1 and s1["reserve_for_manual"] == 1
    assert abs(s1["remaining_sec"] - (48 * 3600 - 60)) < 2 and s1["window_sec"] == 48 * 3600
    assert s1["deadline_ts"] == T0 + 48 * 3600 and s1["no_inbound"] is False and s1["expired"] is False
    assert s1["manual_allowed"] is True and s1["auto_allowed"] is False
    # 平台关窗（fail_type 6）：按「已超窗」呈现（等客户再发言），并带 closed_reason 供排障
    store.record_fail("wkKF", "wmU1", 6)
    s2 = G.ui_snapshot("wechat_kf", "wkKF", ck, now=T0 + 120)
    assert s2["expired"] is True and s2["remaining_sec"] == 0 and s2["closed_reason"] == "fail_type_6"
    assert s2["manual_allowed"] is False
    # 与 window_guard 的键集合一致（前端只认这套键）
    from src.inbox.window_guard import WindowState
    wg_keys = set(WindowState("douyin", 1, 1, 0, 0, 0, 0).as_dict()) | {"manual_allowed", "auto_allowed", "reason"}
    assert wg_keys <= set(s1)
