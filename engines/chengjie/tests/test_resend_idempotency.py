# -*- coding: utf-8 -*-
"""A1（#148 文本件，2026-09-03）：显式重发按**原件** client_msg_id 幂等。

事故（族4 第 6 层，钧机 64PY7D）：断连/慢发送窗里前端 60s 超时误标「发送失败」，
服务端其实已经把消息发出去了；坐席点「重发」，而旧前端**每次重发都现造新
client_msg_id**（旧口径「显式重试永不被去重拦截」）→ 服务端眼里是全新一件、幂等键
压根不认识它 → 客户收到两条一样的话。媒体件已在 E2（v1.0.71）用「幂等键随件固定」
修掉；文本件走乐观气泡 _retryBody，这里补服务端半边。

覆盖：``resend_verdict`` 四态纯函数 + ``SendDedup.holds`` 只读语义 + 路由/前端静态接线。
"""
from __future__ import annotations

import pathlib

from src.inbox.send_dedup import (
    RESEND_ALLOW,
    RESEND_SUPPRESS_IN_FLIGHT,
    RESEND_SUPPRESS_RESERVED,
    RESEND_SUPPRESS_SENT,
    SendDedup,
    resend_verdict,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── 裁决纯函数 ────────────────────────────────────────────────────────────────

def test_sent_is_suppressed():
    """本 bug 的正主：原件已送达 → 重发必须被压制（否则客户收双条）。"""
    assert resend_verdict("sent", True) == RESEND_SUPPRESS_SENT
    # 占位已过期但 tracker 仍记得 sent —— 仍以 tracker 的终局为准
    assert resend_verdict("sent", False) == RESEND_SUPPRESS_SENT


def test_in_flight_is_suppressed():
    """还在途：再发一遍就是双发；前端继续走 /send-status 对账收尾。"""
    assert resend_verdict("in_flight", True) == RESEND_SUPPRESS_IN_FLIGHT
    assert resend_verdict("in_flight", False) == RESEND_SUPPRESS_IN_FLIGHT


def test_failed_is_allowed():
    """服务端明确认败 → 重发正是该做的事，绝不能被幂等挡住。"""
    assert resend_verdict("failed", False) == RESEND_ALLOW
    assert resend_verdict("failed", True) == RESEND_ALLOW


def test_unknown_falls_back_to_reservation():
    """tracker 查无此键（条目过期/后端重启）时靠占位表兜底。

    占位还在＝已发或在途（``release`` 只在失败路径调用）→ 压制；
    占位也没了＝失败过、或重启把两张表都清了 → 放行（漏发比重发贵，
    且坐席是显式点的重发）。
    """
    assert resend_verdict("unknown", True) == RESEND_SUPPRESS_RESERVED
    assert resend_verdict("unknown", False) == RESEND_ALLOW
    assert resend_verdict("", False) == RESEND_ALLOW


def test_verdict_never_raises_on_junk():
    for bad in (None, 123, "SENT", " Sent ", object()):
        assert resend_verdict(bad, False) in (
            RESEND_ALLOW, RESEND_SUPPRESS_SENT, RESEND_SUPPRESS_IN_FLIGHT,
            RESEND_SUPPRESS_RESERVED)
    # 大小写/空格归一：'SENT' 与 ' Sent ' 都得判成已发
    assert resend_verdict("SENT", False) == RESEND_SUPPRESS_SENT
    assert resend_verdict(" Sent ", False) == RESEND_SUPPRESS_SENT


# ── holds() 只读语义 ─────────────────────────────────────────────────────────

def test_holds_is_readonly_and_tracks_reserve_release():
    d = SendDedup()
    assert d.holds("s", "c1") is False, "没占位过 → False"
    # holds 不得自己占位：查过之后 reserve 仍应算首见
    assert d.holds("s", "c1") is False
    assert d.reserve("s", "c1") is True
    assert d.holds("s", "c1") is True, "占位在窗口内 → True（已发或在途）"
    # 只读：查询不该改计数
    snap = d.snapshot()
    d.holds("s", "c1")
    assert d.snapshot() == snap, "holds 必须零副作用"
    d.release("s", "c1")
    assert d.holds("s", "c1") is False, "失败释放后 → False（同 id 重试可再发）"


def test_holds_respects_window_and_empty_id():
    d = SendDedup(window_sec=0.0)
    d.reserve("s", "c1")
    assert d.holds("s", "c1") is False, "窗口已过 → 不再算持有"
    assert d.holds("s", "") is False
    assert d.holds("", "") is False


def test_holds_is_scoped():
    d = SendDedup()
    d.reserve("scope-a", "c1")
    assert d.holds("scope-a", "c1") is True
    assert d.holds("scope-b", "c1") is False, "跨会话同 id 不得互相压制"


# ── 静态接线 ─────────────────────────────────────────────────────────────────

def test_wiring_send_route_checks_prior_id():
    src = (REPO / "src/web/routes/unified_inbox_send_routes.py").read_text(
        encoding="utf-8")
    assert "resend_of_cmid" in src, "路由要收前端带来的原件 id"
    assert "resend_verdict(" in src, "裁决必须走单一事实源纯函数"
    assert "guard=resend_dup" in src, "压制要留可 grep 的日志标签"
    # 裁决必须发生在真发送之前——落在 reserve 之前即可保证（reserve 之后才是发送链）
    i_verdict = src.index("resend_verdict(")
    i_reserve = src.index("_dedup.reserve(_dedup_scope, _client_msg_id)")
    assert i_verdict < i_reserve, "裁决要在占位/发送之前，别发完了才判"


def test_wiring_route_does_not_hijack_b63_resend_of():
    """``resend_of`` 在 B63③ 已有语义（失败留痕行 message_id → 改标 resent）。

    A1 刻意用独立键 ``resend_of_cmid``；两者混用会互相误伤（把 client_msg_id
    传给 mark_message_resent 是 no-op，把 message_id 当幂等键查则永远 unknown）。
    """
    src = (REPO / "src/web/routes/unified_inbox_send_routes.py").read_text(
        encoding="utf-8")
    assert 'body.get("resend_of")' in src, "B63③ 的 resend_of 语义必须还在"
    assert 'mark_message_resent' in src


def test_wiring_frontend_sends_prior_cmid_before_overwriting():
    tpl = (REPO / "src/web/templates/unified_inbox.html").read_text(
        encoding="utf-8")
    i_fn = tpl.index("async function resendFailed(")
    seg = tpl[i_fn:i_fn + 2000]
    assert "resend_of_cmid" in seg, "重发必须带原件 client_msg_id"
    # 顺序铁律：先记下旧键，再换新键——反了就永远只带自己
    i_prior = seg.index("body.resend_of_cmid")
    i_new = seg.index("body.client_msg_id=_newClientMsgId()")
    assert i_prior < i_new, "取原件 id 必须在覆盖 client_msg_id 之前"


def test_wiring_frontend_explains_suppression():
    tpl = (REPO / "src/web/templates/unified_inbox.html").read_text(
        encoding="utf-8")
    assert "d.resend_suppressed" in tpl, "压制要给坐席一句人话，不能静默"
    assert "inbox.send.resend_suppressed" in tpl


def test_i18n_key_is_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    key = "inbox.send.resend_suppressed"
    assert key in ZH and key in EN
    assert ZH[key].strip() and EN[key].strip()
