"""设备 RPA 屏幕级风控识别（P6）门禁：补 P0 留下的「WA/LINE 风控不进反馈环」空白。

守：
1. 纯函数分类——ban/verify/limit 各类多语正例命中；日常聊天/空/无关屏零误报；
   ban 优先级压过 verify/limit（封号页里的通用词不得抢先）。
2. kind → risk_events 映射：verify/limit → flood（喂 account_health），ban/none → None。
3. note_risk_screen 接线：verify/limit 调 record；ban 只告警不 record；全 best-effort。
4. WA/LINE runner 静态接线断言（两条链发送失败收口必须调 _note_risk_screen）。
"""
from __future__ import annotations

import inspect

import pytest

from src.ops.rpa_risk_screen import (
    BAN, LIMIT, NONE, VERIFY,
    classify_risk_screen, note_risk_screen, risk_event_kind_for_screen,
)


# ── 1. 纯函数分类 ────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    '<node text="Your phone number is banned from using WhatsApp"/>',
    '<node text="This account is not allowed to use WhatsApp"/>',
    '<node text="Your account has been suspended"/>',
    '<node text="此帐号无法使用"/>',
    '<node text="アカウントが利用できません"/>',
    '<node text="你的手机号已被封禁了"/>',
])
def test_ban_screens(text):
    assert classify_risk_screen(text) == BAN


@pytest.mark.parametrize("text", [
    '<node text="Verify your phone number"/>',
    '<node text="Enter the 6-digit code we sent"/>',
    '<node text="验证你的手机号"/>',
    '<node text="輸入驗證碼"/>',
    '<node text="認証番号を入力してください"/>',
])
def test_verify_screens(text):
    assert classify_risk_screen(text) == VERIFY


@pytest.mark.parametrize("text", [
    '<node text="You are sending messages too fast"/>',
    '<node text="You have been temporarily restricted"/>',
    '<node text="操作过于频繁，请稍后再试"/>',
    '<node text="送信が速すぎます"/>',
])
def test_limit_screens(text):
    assert classify_risk_screen(text) == LIMIT


@pytest.mark.parametrize("text", [
    "",
    None,
    '<node text="在吗？在的，请稍等一下～"/>',
    '<node text="今天天气不错，出去玩吗"/>',
    '<node text="Type a message"/><node text="Send"/>',
])
def test_benign_and_empty_no_false_positive(text):
    assert classify_risk_screen(text) == NONE


def test_ban_priority_over_limit():
    """封号页常同时含「稍后再试」类通用词——ban 必须优先，不被 limit 抢判。"""
    mixed = ('<node text="Your account has been suspended"/>'
             '<node text="Please try again later"/>')
    assert classify_risk_screen(mixed) == BAN


# ── 2. kind → risk_events 映射 ──────────────────────────────────


def test_kind_mapping():
    from src.ops.risk_events import KIND_FLOOD
    assert risk_event_kind_for_screen(VERIFY) == KIND_FLOOD
    assert risk_event_kind_for_screen(LIMIT) == KIND_FLOOD
    assert risk_event_kind_for_screen(BAN) is None      # 终态不进 24h 预警
    assert risk_event_kind_for_screen(NONE) is None


# ── 3. note_risk_screen 接线（注入假 recorder/alert）──────────────


def test_note_records_flood_for_verify():
    rec = []
    kind = note_risk_screen(
        "whatsapp", "wa_1",
        '<node text="Verify your phone number"/>',
        risk_recorder=lambda p, a, k, now=None: rec.append((p, a, k)))
    from src.ops.risk_events import KIND_FLOOD
    assert kind == VERIFY
    assert rec == [("whatsapp", "wa_1", KIND_FLOOD)]


def test_note_ban_alerts_but_not_records():
    rec, alerts = [], []
    kind = note_risk_screen(
        "line", "line_1",
        '<node text="Your account has been suspended"/>',
        risk_recorder=lambda *a, **k: rec.append(a),
        alert=lambda *a, **k: alerts.append(a))
    assert kind == BAN
    assert rec == [], "ban 是终态，绝不进 24h 滚动风控计数"
    assert len(alerts) == 1


def test_note_none_is_noop():
    rec, alerts = [], []
    kind = note_risk_screen(
        "whatsapp", "wa_1", '<node text="在吗"/>',
        risk_recorder=lambda *a, **k: rec.append(a),
        alert=lambda *a, **k: alerts.append(a))
    assert kind == NONE and rec == [] and alerts == []


def test_note_recorder_failure_is_swallowed():
    def _boom(*a, **k):
        raise RuntimeError("db down")
    # 记账失败绝不能拖垮 runner 主链
    assert note_risk_screen(
        "whatsapp", "wa_1", '<node text="Verify your account"/>',
        risk_recorder=_boom) == VERIFY


# ── 4. runner 静态接线（两条链发送失败收口必须调用）──────────────


def test_wa_runner_wires_note_risk_screen():
    from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner as _R
    assert hasattr(_R, "_note_risk_screen")
    # send_fail 收口调用（复用 chat_xml）
    src = inspect.getsource(_R)
    assert "self._note_risk_screen(chat_xml)" in src


def test_line_runner_wires_note_risk_screen():
    from src.integrations.line_rpa.runner import LineRpaRunner as _R
    assert hasattr(_R, "_note_risk_screen")
    src = inspect.getsource(_R)
    assert "self._note_risk_screen()" in src
