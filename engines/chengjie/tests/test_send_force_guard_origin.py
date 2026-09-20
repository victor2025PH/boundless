# -*- coding: utf-8 -*-
"""C3（#148 B件排查实锤，2026-09-02）：``guard=force`` 放行日志带操作来源。

旧日志只有 ``[send] guard=force kind=dup conv=…``——分不清是坐席在守卫弹窗亲点
「强发」，还是失败气泡「重发」把上一轮 force 标志顺手带了过来，定性多绕一轮
用户确认。现在一行日志写全 agent / sess / entry(ui|api) / src(confirm|
retry_carry|batch_confirm|duty_reply) / ip / cmid。

覆盖：纯字段函数（session/Bearer/无鉴权三形态、src 白名单、异常回退占位符）+
路由/前端/值守 CLI 静态接线 + chatx_readout 计数子串不变。
"""
from __future__ import annotations

import hashlib
import pathlib
import types

from src.web.routes.unified_inbox_send_routes import (
    _request_entry,
    _session_fingerprint,
    force_guard_log_fields,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


def _req(*, session=None, headers=None, cookies=None, host="10.0.0.9"):
    """duck-typed Request：只给 force_guard_log_fields 用到的面。"""
    scope = {}
    if session is not None:
        scope["session"] = session
    r = types.SimpleNamespace(
        scope=scope, headers=headers or {}, cookies=cookies or {},
        client=types.SimpleNamespace(host=host) if host else None,
    )
    if session is not None:
        r.session = session
    return r


def test_fields_for_ui_session_confirm():
    req = _req(session={"user_id": "u7", "username": "zhang", "display_name": "张"},
               cookies={"session": "abc.def"})
    f = force_guard_log_fields(req, {"force_dup": 1, "force_src": "confirm",
                                     "client_msg_id": "c-1234567890abcdefXYZ"})
    assert f["agent"] == "u7"
    assert f["entry"] == "ui"
    assert f["src"] == "confirm"
    assert f["ip"] == "10.0.0.9"
    assert f["cmid"] == "c-1234567890abcd"                # 前 16 位
    assert f["sess"] == hashlib.sha1(b"abc.def").hexdigest()[:8]
    assert "abc.def" not in "".join(f.values()), "指纹不得泄露 cookie 明文"


def test_fields_for_bearer_api_duty_reply():
    req = _req(headers={"Authorization": "Bearer tok"}, host="127.0.0.1")
    f = force_guard_log_fields(req, {"force_lang": 1, "force_src": "duty_reply"})
    assert f["entry"] == "api"
    assert f["src"] == "duty_reply"
    assert f["agent"] == "agent"           # 无 session → _session_agent 回落
    assert f["sess"] == "-"


def test_fields_retry_carry_and_unknown_src():
    req = _req(session={"username": "li"})
    assert force_guard_log_fields(req, {"force_src": "retry_carry"})["src"] == "retry_carry"
    assert force_guard_log_fields(req, {"force_src": "batch_confirm"})["src"] == "batch_confirm"
    # 未声明（老前端）→ '-'；不认识的值原样带前缀留痕，不吞
    assert force_guard_log_fields(req, {})["src"] == "-"
    assert force_guard_log_fields(req, {"force_src": "weird"})["src"] == "other:weird"


def test_entry_unknown_without_session_or_bearer_and_never_raises():
    req = _req(host=None)
    assert _request_entry(req) == "unknown"
    f = force_guard_log_fields(req, None)
    assert f == {"agent": "agent", "sess": "-", "entry": "unknown",
                 "src": "-", "ip": "-", "cmid": "-"}
    # session 对象坏掉也不抛
    bad = types.SimpleNamespace(scope={"session": 1}, headers={}, cookies={},
                                client=None)
    bad.session = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    assert _request_entry(bad) in ("unknown", "ui")
    assert _session_fingerprint(types.SimpleNamespace(cookies={})) == "-"


def test_wiring_send_route_logs_origin_fields():
    src = (REPO / "src/web/routes/unified_inbox_send_routes.py").read_text(encoding="utf-8")
    i = src.index("guard=force kind=%s")
    seg = src[i:i + 200]
    for key in ("agent=%s", "sess=%s", "entry=%s", "src=%s", "ip=%s", "cmid=%s"):
        assert key in seg, f"guard=force 日志缺 {key}"
    # chatx_readout 远程统计只认这个子串——不得改名
    ps1 = (REPO.parent.parent / "deploy" / "desktop" / "chatx_readout.ps1")
    if ps1.is_file():
        assert 'guard=force' in ps1.read_text(encoding="utf-8", errors="ignore")


def test_wiring_frontend_tags_force_source():
    tpl = (REPO / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    assert tpl.count("force_src='confirm'") >= 2, "两处守卫确认弹窗都要标 confirm"
    assert "force_src='batch_confirm'" in tpl, "整批沿用的标志是链路自带，不是本条人点"
    assert "force_src='retry_carry'" in tpl, "失败气泡「重发」携带旧 force 标志必须改标"
    # retry_carry 只在 resendFailed 里改标，且要在真正 POST 之前
    i_fn = tpl.index("async function resendFailed(")
    i_tag = tpl.index("force_src='retry_carry'", i_fn)
    i_post = tpl.index("/api/unified-inbox/send", i_fn)
    assert i_fn < i_tag < i_post


def test_wiring_duty_reply_declares_source():
    from tools.duty_reply import build_send_payload
    p = build_send_payload("-1004345824259", "hi", account_id="6834964252",
                           client_msg_id="duty-x")
    assert p["force_lang"] == 1 and p["force_src"] == "duty_reply"
