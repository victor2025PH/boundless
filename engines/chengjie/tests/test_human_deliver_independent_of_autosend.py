# -*- coding: utf-8 -*-
"""人工通过 inbox 草稿的投递能力**不得**受自动投递开关 (`l2_autosend.deliver`) 约束。

语义依据（2026-07-29 定）：`deliver` 管的是「**AI 可否自己发**」；坐席点「通过」是
**人的明示决定**，而手动发送端点 `/api/unified-inbox/send` 本就不受 `deliver` 约束。
用同一个开关闸住人工通过自相矛盾，后果是「AI 拟稿 + 人审后发」这个**最谨慎、
最常被推荐的档位**（AGENTS.md 给 Messenger 的推荐档就是 review）里发送按钮空转：
坐席以为发了、客户什么也没收到，两端都看不出区别。

本机实测（2026-07-29）：zhiliao deliver=true 正常，tongyi deliver=false → 缺陷面。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    """够用的 DraftService 替身（deliver_human_approved 只用到审计/事件旁路）。"""

    def __init__(self) -> None:
        self.audits: List[Dict[str, Any]] = []

    def audit(self, *a, **k) -> None:
        self.audits.append({"a": a, "k": k})


def _draft(text: str = "你好呀") -> Dict[str, Any]:
    return {
        "draft_id": "inbox:42", "conversation_id": "c1", "platform": "telegram",
        "account_id": "default", "chat_key": "u1", "final_text": text,
    }


def test_human_delivery_works_when_auto_deliver_is_off():
    """deliver=false（自动链 send_callback=None）时，人工通过仍须真发出去。"""
    sent: List[tuple] = []

    async def _human_send(platform, account_id, chat_key, text, original_text=None):
        sent.append((platform, account_id, chat_key, text, original_text))
        return {"ok": True, "delivered": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"enabled": True},
        send_callback=None,             # 自动链关（deliver=false 的真实形态）
        human_send_callback=_human_send,  # 人工链仍具备真发能力
    )
    res = asyncio.run(w.deliver_human_approved(_draft()))

    assert res.get("ok") is True, f"人工通过应真发送，实得 {res}"
    assert len(sent) == 1 and sent[0][3] == "你好呀"
    assert w.total_human_delivered == 1


def test_no_send_path_at_all_reports_failure_not_silent_success():
    """两个回调都没有时必须如实失败——静默成功=坐席以为发了（本次要修的病根）。"""
    w = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": True},
                       send_callback=None, human_send_callback=None)
    res = asyncio.run(w.deliver_human_approved(_draft()))
    assert res.get("ok") is False
    assert "no_send_path" in str(res.get("error") or "")


def test_original_text_probe_follows_the_callback_actually_used():
    """签名探测须按**实际调用的回调**判定，且两个回调的结论不互相串味。

    回归 2026-07-29 的真 bug：探测硬编 self._send_callback，deliver=false 时它是 None
    → inspect.signature(None) 抛 TypeError → 缓存 False → 人工链永久丢 original_text
    （语音分支据原文合成，丢了就用译文发声＝念错语言）。
    """
    got: Dict[str, Any] = {}

    async def _with_original(platform, account_id, chat_key, text, original_text=None):
        got["original_text"] = original_text
        return {"ok": True}

    w = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": True},
                       send_callback=None, human_send_callback=_with_original)
    asyncio.run(w.deliver_human_approved(_draft("原文")))
    assert got.get("original_text") == "原文", "接受 original_text 的回调必须收到它"

    # 4 参旧回调（不接受 original_text）不得被硬塞 kwarg → TypeError
    async def _legacy_four(platform, account_id, chat_key, text):
        got["legacy_called"] = True
        return {"ok": True}

    w2 = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": True},
                        send_callback=None, human_send_callback=_legacy_four)
    res = asyncio.run(w2.deliver_human_approved(_draft()))
    assert got.get("legacy_called") and res.get("ok") is True

    # 同一实例上两个不同签名的回调分别缓存，不互相污染
    w3 = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": True},
                        send_callback=_legacy_four,
                        human_send_callback=_with_original)
    assert w3._send_cb_kwargs("x", _legacy_four) == {}
    assert w3._send_cb_kwargs("x", _with_original) == {"original_text": "x"}


def test_deliver_only_instance_is_marked_and_not_running():
    """deliver_only 实例：只作人工投递载体，快照须自证「自动回复没在跑」。

    它被刻意放进同一个 ``app.state.autosend_worker`` 键（观测链零改动全通），
    所以必须能让读者区分「worker 存在」与「自动回复在跑」——否则看板/报表会误读。
    """
    async def _send(platform, account_id, chat_key, text, original_text=None):
        return {"ok": True}

    w = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": False},
                       send_callback=None, human_send_callback=_send,
                       deliver_only=True)
    snap = w.status_snapshot()
    assert snap["deliver_only"] is True
    assert snap["running"] is False and snap["enabled"] is False
    assert snap["deliver_enabled"] is False       # 自动链无能力
    assert snap["human_deliver_enabled"] is True  # 人工链有能力（两者正交）

    # 且它确实能投递
    assert asyncio.run(w.deliver_human_approved(_draft())).get("ok") is True


def test_normal_worker_is_not_marked_deliver_only():
    """反面：正常 worker 不得被标成 deliver_only（否则看板会误报自动回复未跑）。"""
    async def _send(platform, account_id, chat_key, text, original_text=None):
        return {"ok": True}

    w = AutosendWorker(draft_service=_FakeSvc(), config={"enabled": True},
                       send_callback=_send)
    snap = w.status_snapshot()
    assert snap["deliver_only"] is False
    assert snap["deliver_enabled"] is True


def test_bootstrap_has_deliver_only_fallback_when_worker_absent():
    """静态接线门禁：worker 未创建（l2_autosend.enabled=false / 授权档位不含）时
    必须仍有人消费人工通过，否则「人审后发」部署里发送按钮空转。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "bootstrap" / "web_app.py").read_text(encoding="utf-8")
    assert "deliver_only=True" in src, "缺 deliver_only 兜底实例"
    # 取整段兜底块（从「worker 未创建」守卫到注入完成）——按字符距离取窗口太脆，
    # 块内换行/注释一变就假红（本门禁自己首版就踩了这个）。
    guard = 'getattr(web_app.state, "autosend_worker", None) is None'
    gi = src.find(guard)
    assert gi > 0, "兜底必须以「worker 确实未创建」为前置守卫（否则会覆盖真 worker）"
    block = src[gi:gi + 2200]
    assert "deliver_only=True" in block, "守卫之后应构造 deliver_only 实例"
    assert "set_inbox_deliver_callback" in block, "兜底实例必须注入投递回调"
    assert "human_deliver" in block, "兜底同样受 human_deliver 配置约束"
    # 兜底实例的自动链必须无能力（只投递不自动发）
    assert "send_callback=None" in block


def test_worker_accepts_human_send_callback_kwarg():
    """构造契约：bootstrap 按关键字传 human_send_callback，签名漂移即红。"""
    params = inspect.signature(AutosendWorker.__init__).parameters
    assert "human_send_callback" in params
    assert params["human_send_callback"].kind == inspect.Parameter.KEYWORD_ONLY


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _watchdog(monkeypatch, *, wired, deliver_enabled, approvals=50,
              worker_present=True):
    """装一个只为本检查服务的 HealthWatchdog（state 形态与生产一致）。"""
    from types import SimpleNamespace

    from src.inbox import health_watchdog as hw

    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    worker = SimpleNamespace(status_snapshot=lambda: {
        "deliver_enabled": deliver_enabled,
        "total_human_delivered": 0,
        "total_human_deliver_errors": 0,
    }) if worker_present else None
    state = SimpleNamespace(
        autosend_worker=worker,
        draft_service=(None if wired is None
                       else SimpleNamespace(inbox_deliver_wired=wired)),
    )
    cm = SimpleNamespace(config={"health_watchdog": {
        "human_deliver_remind": {"enabled": True, "after_min": 30}}})
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = cm
    w._hd_bad_since = 0.0
    w._hd_alerted = False
    w._hd_last_remind = 0.0
    w._hd_since_ts = 0.0
    w.total_human_deliver_alerts = 0
    w.human_approved_since = lambda _ts: approvals
    return w, bus


def test_alerts_when_wired_even_if_auto_deliver_is_off(monkeypatch):
    """已接线时不再看 deliver 开关——人审档（deliver=false）正是新能力所在处。"""
    w, bus = _watchdog(monkeypatch, wired=True, deliver_enabled=False)
    w._check_human_deliver_chain(now=1000.0)      # 首见：只记时不报
    w._check_human_deliver_chain(now=1000.0 + 40 * 60)
    assert [e[0] for e in bus.events] == ["human_deliver_alert"]


def test_not_wired_alerts_with_deterministic_cause(monkeypatch):
    """未接线＝确定性根因，且 worker 压根不存在时也要报（l2 整体关闭的形态）。"""
    w, bus = _watchdog(monkeypatch, wired=False, deliver_enabled=False,
                       worker_present=False)
    w._check_human_deliver_chain(now=2000.0)
    w._check_human_deliver_chain(now=2000.0 + 40 * 60)
    assert len(bus.events) == 1
    assert bus.events[0][1].get("not_wired") is True


def test_silent_when_operator_chose_mark_only(monkeypatch):
    """运营显式 human_deliver=false（刻意只标记）→ 接线缺失不是故障，绝不告警。"""
    w, bus = _watchdog(monkeypatch, wired=False, deliver_enabled=False)
    w._config_manager.config["inbox"] = {"auto_draft": {"human_deliver": False}}
    w._check_human_deliver_chain(now=3000.0)
    w._check_human_deliver_chain(now=3000.0 + 40 * 60)
    assert bus.events == []


def test_unknown_wiring_falls_back_to_conservative_gate(monkeypatch):
    """接线状态未知时退回旧闸门（deliver 关就静默）——信息不足宁可漏报不误报。"""
    w, bus = _watchdog(monkeypatch, wired=None, deliver_enabled=False)
    w._check_human_deliver_chain(now=4000.0)
    w._check_human_deliver_chain(now=4000.0 + 40 * 60)
    assert bus.events == []


def test_bootstrap_wires_human_delivery_regardless_of_deliver_flag():
    """静态接线门禁：注入不得再被 `if _deliver:` 包住（那正是本次修掉的错闸门）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "bootstrap" / "web_app.py").read_text(encoding="utf-8")
    idx = src.find("set_inbox_deliver_callback")
    assert idx > 0, "bootstrap 必须注入人工投递回调"
    window = src[max(0, idx - 900):idx]
    assert "human_deliver" in window, (
        "注入应由 inbox.auto_draft.human_deliver 控制（默认开），而非 deliver 开关")
    # 注入点上方最近的条件不应是裸 `if _deliver:`
    assert "if _deliver:\n" not in window[-400:], (
        "人工投递注入不得受 l2_autosend.deliver 闸门约束——"
        "deliver 管『AI 可否自己发』，人工通过是人的明示决定")
