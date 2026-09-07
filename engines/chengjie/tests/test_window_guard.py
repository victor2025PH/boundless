# -*- coding: utf-8 -*-
"""平台回复窗 / 每轮配额执行器（实施96 DY P0 第二轮）：从收件箱事实现算，不另记账。

钉死：无入站不许发 / 超窗不许发 / 配额用完不许发 / 自动链给坐席留预留 / 客户再发言即重置 /
微信客服让路 kf_window_guard / 未登记平台恒放行 / TikTok 首条禁链 / 编排器与 send-caps 接线。
"""
from __future__ import annotations

import time

import pytest

from src.inbox import channel_policy as cp
from src.inbox import window_guard as wg
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb

ACCT, CHAT = "demo", "douyin:user:u1"
CID = f"douyin:{ACCT}:{CHAT}"


@pytest.fixture()
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: s)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(s, **m))
    yield s
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


def _in(store, text, ts, platform="douyin", chat=CHAT):
    pb.ingest_incoming(store, platform=platform, account_id=ACCT, chat_key=chat, name="客户",
                       text=text, ts=ts, msg_id=f"in-{ts}", direction="in")


def _out(store, text, ts, platform="douyin", chat=CHAT):
    pb.ingest_incoming(store, platform=platform, account_id=ACCT, chat_key=chat,
                       text=text, ts=ts, msg_id=f"out-{ts}", direction="out")


def test_unregistered_platform_never_blocks(store):
    assert wg.window_state("telegram", ACCT, "1", store=store) is None
    assert wg.send_block_reason("telegram", ACCT, "1", store=store) == ""
    assert wg.snapshot("telegram", ACCT, "1", store=store) == {}


def test_no_store_means_allow():
    pb.register_inbox_store_getter(lambda: None)
    try:
        assert wg.send_block_reason("douyin", ACCT, CHAT) == ""
        assert wg.is_first_message("tiktok", ACCT, CHAT) is False
    finally:
        pb.register_inbox_store_getter(None)


def test_douyin_window_lifecycle(store):
    t0 = time.time() - 600
    # 从未入站 → 不许发（平台只允许被动回复）
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store, now=t0) == wg.REASON_NO_INBOUND
    # 客户开口 → 窗口打开，6 条配额
    _in(store, "多少钱", t0)
    st = wg.window_state("douyin", ACCT, CHAT, store=store, now=t0 + 1)
    assert st is not None and st.cap == 6 and st.remaining == 6 and not st.expired
    assert abs(st.remaining_sec - (24 * 3600 - 1)) < 1
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store, now=t0 + 1) == ""
    # 我方发 5 条 → 剩 1：自动链让路（预留 1 给坐席），人工仍可发
    for i in range(5):
        _out(store, f"回复{i}", t0 + 2 + i)
    now = t0 + 10
    assert wg.send_block_reason("douyin", ACCT, CHAT, origin="auto", store=store, now=now) == wg.REASON_RESERVED
    assert wg.send_block_reason("douyin", ACCT, CHAT, origin="manual", store=store, now=now) == ""
    # 第 6 条发出 → 双方都用完
    _out(store, "回复5", t0 + 8)
    assert wg.send_block_reason("douyin", ACCT, CHAT, origin="manual", store=store, now=now) == wg.REASON_EXHAUSTED
    # 客户再发言 → 计数归零、窗口重开
    _in(store, "好的", t0 + 20)
    st2 = wg.window_state("douyin", ACCT, CHAT, store=store, now=t0 + 21)
    assert st2.remaining == 6 and st2.sent_since_inbound == 0
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store, now=t0 + 21) == ""
    # 24 小时后 → 超窗
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store,
                                now=t0 + 20 + 24 * 3600 + 1) == wg.REASON_EXPIRED
    snap = wg.snapshot("douyin", ACCT, CHAT, store=store, now=t0 + 21)
    assert snap["cap"] == 6 and snap["remaining"] == 6 and snap["manual_allowed"] and snap["auto_allowed"]
    assert snap["deadline_ts"] == pytest.approx(t0 + 20 + 24 * 3600)


def test_config_override_changes_cap(store):
    t0 = time.time() - 60
    _in(store, "hi", t0)
    _out(store, "a", t0 + 1)
    _out(store, "b", t0 + 2)
    cfg = {"channel_policy": {"douyin": {"per_window_cap": 2, "reserve_for_manual": 0}}}
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store, now=t0 + 3, config=cfg) == wg.REASON_EXHAUSTED
    assert wg.send_block_reason("douyin", ACCT, CHAT, store=store, now=t0 + 3) == ""  # 默认 6 条


def test_wechat_kf_is_left_to_kf_window_guard(store):
    kf = pytest.importorskip("src.inbox.kf_window_guard")
    assert kf.is_quota_platform("wechat_kf")
    # 即便从未入站，本模块也不判——由 kf_window_guard 记账执行
    assert wg.send_block_reason("wechat_kf", ACCT, "wechat_kf:user:x", store=store) == ""
    assert wg.snapshot("wechat_kf", ACCT, "wechat_kf:user:x", store=store) == {}


def test_tiktok_first_message_link_rule(store):
    chat = "tiktok:user:t1"
    t0 = time.time() - 60
    _in(store, "hello", t0, platform="tiktok", chat=chat)
    assert wg.is_first_message("tiktok", ACCT, chat, store=store) is True
    assert cp.text_block_reason("tiktok", "see https://x.com", first_message=True) == cp.REASON_LINK_DENIED
    assert cp.text_block_reason("tiktok", "see https://x.com", first_message=False) == ""
    _out(store, "hi there", t0 + 1, platform="tiktok", chat=chat)
    assert wg.is_first_message("tiktok", ACCT, chat, store=store) is False


def test_reason_family_and_messages():
    from src.inbox.send_gate_status import blocked_reason_key
    import src.web.i18n_packs.errors as _errs
    src_txt = open(_errs.__file__, encoding="utf-8").read()
    for fam in ("policy_window_no_inbound", "policy_window_expired", "policy_window_quota",
                "policy_link", "policy_len", "policy_media", "policy"):
        assert src_txt.count(f'"err.inbox.send_blocked_{fam}"') == 2, f"{fam} 需 zh/en 各一条"
    assert blocked_reason_key(wg.REASON_NO_INBOUND) == "policy_window_no_inbound"
    assert blocked_reason_key(wg.REASON_EXPIRED) == "policy_window_expired"
    assert blocked_reason_key(wg.REASON_EXHAUSTED) == "policy_window_quota"
    assert blocked_reason_key(wg.REASON_RESERVED) == "policy_window_quota"
    assert blocked_reason_key(cp.REASON_LINK_DENIED) == "policy_link"


class _W:
    def __init__(self):
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {}

    async def send(self, chat_key, text):
        self.sent.append(text)
        return {"delivered": True, "message_id": f"m{len(self.sent)}"}


async def test_orchestrator_enforces_window(store):
    from src.integrations import account_orchestrator as ao
    orch = ao.AccountOrchestrator(config={})
    w = _W()
    key = ao.account_key("douyin", ACCT)
    orch._managed[key] = ao._Managed(key=key, platform="douyin", account_id=ACCT, mode="web",
                                     worker=w, state="running")
    # 无入站 → 拦（人工也拦：平台规则）
    r = await orch.send("douyin", ACCT, CHAT, "你好呀", origin="manual")
    assert r == {"delivered": False, "blocked": wg.REASON_NO_INBOUND} and w.sent == []
    _in(store, "在吗", time.time() - 30)
    # 6 条配额：人工连发 6 条成功（出站镜像落库即计数），第 7 条拦
    for i in range(6):
        r = await orch.send("douyin", ACCT, CHAT, f"在的{i}", origin="manual")
        assert r["delivered"] is True, (i, r)
    r7 = await orch.send("douyin", ACCT, CHAT, "再来一条", origin="manual")
    assert r7 == {"delivered": False, "blocked": wg.REASON_EXHAUSTED}
    assert len(w.sent) == 6
