"""激活漏斗里程碑（P1-⑧）门禁：beacon 里程碑语义 + 两个采集点位 + 直传接线。

漏斗五段中 account_online / first_reply 依赖客户端上报——这两个点位断了，
官网 /console/funnel 的后两段永远是 0 且**没有任何报错**（回传是旁路），
故用测试钉住采集语义与接线。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from src.utils import telemetry_beacon as tb

_ROOT = Path(__file__).resolve().parents[1]


class _FakeBeacon:
    def __init__(self):
        self.events = []

    def note_event(self, logger_name, level, msg):
        self.events.append((logger_name, level, msg))


def _install_fake(monkeypatch):
    fake = _FakeBeacon()
    monkeypatch.setattr(tb, "_installed", fake)
    tb._MILESTONE_SENT.clear()
    return fake


def test_note_milestone_noop_without_beacon(monkeypatch):
    monkeypatch.setattr(tb, "_installed", None)
    tb._MILESTONE_SENT.clear()
    tb.note_milestone("account_online", "platform=telegram")  # 不抛即过（server 部署形态）


def test_note_milestone_sends_once_per_kind(monkeypatch):
    fake = _install_fake(monkeypatch)
    tb.note_milestone("account_online", "platform=telegram mode=protocol")
    tb.note_milestone("account_online", "platform=whatsapp mode=protocol")  # 同类去重
    tb.note_milestone("first_reply", "platform=telegram")
    kinds = [e[2].split(" ", 1)[0] for e in fake.events]
    assert kinds == ["account_online", "first_reply"]
    assert all(e[0] == "milestone" and e[1] == "INFO" for e in fake.events)


def test_note_milestone_sanitizes_detail(monkeypatch):
    fake = _install_fake(monkeypatch)
    tb.note_milestone("first_reply", r"path C:\Users\boss\secret sk-abcdef123456")
    msg = fake.events[0][2]
    assert "boss" not in msg and "sk-abcdef123456" not in msg


def test_store_first_reply_freshness_gate(monkeypatch):
    """目录同步灌入的历史出站（旧 ts）绝不算「首条真实出站」。"""
    from src.inbox import store as st

    sent = []
    monkeypatch.setattr(
        "src.utils.telemetry_beacon.note_milestone",
        lambda kind, detail="": sent.append((kind, detail)))

    # 旧消息（1 小时前）→ 不触发，且一次性闸不得被消耗
    monkeypatch.setattr(st, "_FIRST_REPLY_NOTED", False)
    st._note_first_reply_milestone(SimpleNamespace(
        ts=time.time() - 3600, conversation_id="telegram:a:c1"))
    assert sent == []
    assert st._FIRST_REPLY_NOTED is False, "旧消息不得烧掉一次性闸"

    # 新鲜消息 → 触发一次，之后闸死
    st._note_first_reply_milestone(SimpleNamespace(
        ts=time.time(), conversation_id="telegram:a:c1"))
    assert sent == [("first_reply", "platform=telegram")]
    assert st._FIRST_REPLY_NOTED is True
    st._note_first_reply_milestone(SimpleNamespace(
        ts=time.time(), conversation_id="whatsapp:a:c2"))
    assert len(sent) == 1, "进程内只报一次"


# ── 静态轨：采集点位与直传接线（声明了就必须有人消费）──────────────────────

def test_account_online_wired_at_login_persist():
    src = (_ROOT / "src" / "web" / "routes" / "unified_inbox_login_routes.py").read_text(
        encoding="utf-8")
    assert 'note_milestone("account_online"' in src, \
        "登录成功单点（_persist_login_account）未上报 account_online"


def test_first_reply_wired_at_ingest():
    src = (_ROOT / "src" / "inbox" / "store.py").read_text(encoding="utf-8")
    assert "_note_first_reply_milestone" in src
    assert 'msg.direction == "out"' in src


def test_diag_upload_route_and_button_wired():
    """P1-⑦ 一键直传：后端转投路由 + 设置页按钮（旧后端探活自隐藏）。"""
    routes = (_ROOT / "src" / "web" / "routes" / "ops_overview_routes.py").read_text(
        encoding="utf-8")
    assert "/api/admin/diagnostic-upload" in routes
    assert "x-diag-meta" in routes, "直传必须带 app/fp 元信息（官网按它推客服 TG 摘要）"
    tpl = (_ROOT / "src" / "web" / "templates" / "settings.html").read_text(
        encoding="utf-8")
    assert "diag-upload-btn" in tpl
    assert "diagnostic-upload?probe=1" in tpl, "按钮必须探活后才显示（旧后端无路由）"
