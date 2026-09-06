"""#210 B（L-1，2026-09-06）：工作台按**实际发出条数**显示——逐条镜像不变量。

82BF95 / WMY7A6：Sceya 会话一条回复被拆两条发出，报告称「工作台合并显示为一条气泡，
手机端为两条」。三条投递链的镜像口径本就是**每条独立落行**（各自带平台
message_id）：手动 ``_deliver_bubble_parts`` 逐条走 ``send_via_adapters`` → 编排器
``send`` 成功即镜像**该条**；B 线 ``_send_one`` 逐条同路；A 线 ``_send_reply``
逐条 ``_postsend_mirror_and_record``。本文件把这个不变量钉死（任何一链回退成
「原稿整段一行」都会在此失败），并钉住 1.0.75 新增的对账面：手动链响应体
``bubbles.message_ids`` / 汇总日志 / 前端「已拆 N 条发出」提示。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_MANUAL = ROOT / "src" / "web" / "routes" / "unified_inbox_send_routes.py"
_ORCH = ROOT / "src" / "integrations" / "account_orchestrator.py"
_ALINE = ROOT / "src" / "client" / "telegram_client.py"
_SENDER = ROOT / "src" / "client" / "sender.py"
_BLINE = ROOT / "src" / "inbox" / "autosend_helpers.py"
_INBOX_HTML = ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_I18N = ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


_BCFG_INSTANT = {
    "gap_sec_lo": 0.0, "gap_sec_hi": 0.0, "per_char_sec": 0.0,
    "latin_per_char_sec": 0.0, "max_gap_sec": 1.0, "total_budget_sec": 0.0,
}


def test_manual_chain_sends_and_reports_each_part(monkeypatch):
    """手动链：N 条 → send_via_adapters 恰好调 N 次、每次带**那一条**的文本；
    返回 (首条结果, N, N 个 message_id)。镜像发生在 send_via_adapters → 编排器
    send 内（每次调用一行），故「调用次数 == 条数」就是「工作台行数 == 手机条数」。"""
    from src.web.routes import unified_inbox_send_routes as R

    calls: list = []

    async def _fake_send(request, platform, account_id, chat_key, text, adapters,
                         *, reply_to=None, mentions=None, origin="manual"):
        calls.append({"text": text, "reply_to": reply_to, "origin": origin})
        return {"delivered": True, "message_id": f"mid{len(calls)}"}

    async def _no_wait(**kw):
        return None

    monkeypatch.setattr(R, "send_via_adapters", _fake_send)
    import src.inbox.humanize as hz
    monkeypatch.setattr(hz, "run_presend_humanization", _no_wait)

    parts = ["You noticed that, huh?",
             "I guess you just bring out a different side of me here.",
             "But it's still me, just a little more relaxed with you."]
    first, sent, ids = asyncio.run(R._deliver_bubble_parts(
        object(), "telegram", "7092595256", "8035703354", parts, {},
        reply_to={"id": "88700", "text": "…"}, bcfg=dict(_BCFG_INSTANT)))

    assert sent == 3 and len(calls) == 3
    assert [c["text"] for c in calls] == parts          # 每条独立发送＝每条独立镜像
    assert ids == ["mid1", "mid2", "mid3"]
    assert first == {"delivered": True, "message_id": "mid1"}
    # 仅首条带引用（其余条不该在工作台画引用条）
    assert calls[0]["reply_to"] and calls[1]["reply_to"] is None and calls[2]["reply_to"] is None


def test_manual_chain_partial_failure_reports_only_sent(monkeypatch):
    """中途失败：已发算数、剩余丢弃——message_ids 只含真发出去的（工作台也只有那几行）。"""
    from src.web.routes import unified_inbox_send_routes as R

    n = {"i": 0}

    async def _flaky(request, platform, account_id, chat_key, text, adapters, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("network")
        return {"delivered": True, "message_id": f"mid{n['i']}"}

    async def _no_wait(**kw):
        return None

    monkeypatch.setattr(R, "send_via_adapters", _flaky)
    import src.inbox.humanize as hz
    monkeypatch.setattr(hz, "run_presend_humanization", _no_wait)

    first, sent, ids = asyncio.run(R._deliver_bubble_parts(
        object(), "telegram", "a", "c", ["一", "二", "三"], {},
        reply_to=None, bcfg=dict(_BCFG_INSTANT)))
    assert sent == 1 and ids == ["mid1"]
    assert first["message_id"] == "mid1"


def test_manual_route_returns_message_ids_and_never_mirrors_whole_draft():
    src = _src(_MANUAL)
    seg = src[src.index("def _deliver_bubble_parts"):]
    ends = [seg.find(tok, 10) for tok in ("\nasync def ", "\ndef ")]
    body = seg[: min(e for e in ends if e > 0)]
    # 分条函数内不得再给原稿整段补镜像行（那才会造出「工作台一条、手机两条」）
    for tok in ("emit_incoming(", "make_message(", "record_failed_outbound_mirror"):
        assert tok not in body, f"_deliver_bubble_parts 不该自己写镜像行：{tok}"
    assert "return first_result, sent, message_ids" in body
    assert "气泡分条已发 %d/%d 条" in body, "缺「实发 N/M 条」汇总日志"
    # 响应体 bubbles 段带 message_ids（与镜像行 platform_msg_id 同值，供对账）
    assert '"message_ids": list(_bub_ids or [])' in src


def test_orchestrator_send_mirrors_the_text_it_sent():
    """编排器 send：镜像行文本 == 本次 send 的 text、msg_id == worker 回的 message_id
    ——每次 send 一行，是三链逐条镜像的共同底座。"""
    src = _src(_ORCH)
    i = src.index("async def send(")
    body = src[i: i + 12000]
    assert 'text=text, direction="out", msg_id=_mid' in body
    assert '_mid = str(res.get("message_id") or "")' in body


def test_aline_mirrors_each_chunk_and_logs_count():
    aline = _src(_ALINE)
    sender = _src(_SENDER)
    # 逐条 _send_reply → 每条一次 _postsend_mirror_and_record（带真实 _sent.id）
    loop = aline[aline.index("for i, chunk in enumerate(chunks):"):]
    assert "await self._send_reply(message, chunk, parse_mode=_parse_mode)" in loop[:3500]
    assert "self._postsend_mirror_and_record(" in sender
    assert 'msg_id=getattr(_sent, "id", "") or ""' in sender
    # 1.0.75：一行汇总「A线已拆 N/M 条发出」
    assert "A线已拆 %d/%d 条发出" in aline


def test_bline_logs_count_and_sends_each_part():
    src = _src(_BLINE)
    assert "_last_res = await _send_one(" in src
    assert "分条已发 %d platform=%s" in src


def test_frontend_shows_actual_bubble_count():
    html = _src(_INBOX_HTML)
    assert "inbox.send.bubbles_sent" in html
    assert "d.bubbles.parts_sent" in html
    pack = _src(_I18N)
    assert pack.count('"inbox.send.bubbles_sent"') == 2, "zh/en 两份词表都要有键"
