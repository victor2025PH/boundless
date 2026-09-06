# -*- coding: utf-8 -*-
"""#170（L-3 B）幽灵未读闭环：被埋告警上屏 / 孤儿未读归零 / 横幅常驻 / 首启引导。

U3YUH3（1.0.74）：看门狗 00:28 报被埋 8 个 86h，``alert_on_warn=False`` → 只写日志；
J-4 A 做的入口长在会折叠的筛选头里，skuio 一夜没找到。本批：
- 看门狗 ``surface_in_ui``（默认开）把结论写进工作台通知中心（notif_queue sys_status，
  按 id 合并，归零即撤），与 webhook 的 alert_on_warn 正交；
- 启动卫生扫描：已删（墓碑）会话的孤儿未读归零 + #207 已消化「需人工」摘标；
- 被埋横幅挪出 .list-header-body 常驻；角标浮层前 3 次带引导行。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.inbox.store import InboxStore
from src.inbox.unread_aggregate import purge_orphan_unread

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _row(cid: str, unread: int, *, age_h: float = 1.0) -> Dict[str, Any]:
    return {
        "conversation_id": cid, "platform": "whatsapp", "account_id": "a",
        "chat_key": cid, "display_name": "x", "unread": unread,
        "last_ts": time.time() - age_h * 3600.0, "last_text": "?",
        "archived_at": time.time() - age_h * 3600.0 - 60, "auto_archived_at": 0.0,
    }


def _wd(monkeypatch, rows, *, surface_cfg=None):
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    store = SimpleNamespace(list_buried_archived=lambda **k: list(rows))
    state = SimpleNamespace(inbox_store=store)
    conf: Dict[str, Any] = {"health_watchdog": {"buried_conv_remind": {"enabled": True}}}
    if surface_cfg is not None:
        conf["health_watchdog"]["surface_in_ui"] = surface_cfg
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = SimpleNamespace(config=conf)
    w._bc_alerted = False
    w._bc_last_remind = 0.0
    w.total_buried_conv_alerts = 0
    return w, bus, state


def _sys_status(state, sid: str):
    return [n for n in (getattr(state, "notif_queue", None) or [])
            if n.get("type") == "sys_status" and (n.get("data") or {}).get("id") == sid]


# ── 上屏 ─────────────────────────────────────────────────────────────────

def test_buried_alert_surfaces_to_notif_queue_and_merges(monkeypatch):
    rows = [_row("c1", 3, age_h=86), _row("c2", 5, age_h=2)]
    w, bus, state = _wd(monkeypatch, rows)
    t0 = time.time()
    w._check_buried_conversations(now=t0)
    got = _sys_status(state, "buried_conv")
    assert len(got) == 1
    txt = got[0]["data"]["text"]
    assert "8" in txt and "86" in txt, txt      # 共 8 条未读 / 最久 86 小时
    assert got[0]["data"]["source"] == "health_watchdog"
    # webhook 4h 节流期内再 tick：通知中心条目仍只有一条（按 id 合并），且随现状刷新
    rows.append(_row("c3", 1, age_h=1))
    w._check_buried_conversations(now=t0 + 600)
    got2 = _sys_status(state, "buried_conv")
    assert len(got2) == 1 and "9" in got2[0]["data"]["text"]
    assert len(bus.events) == 1, "上屏刷新不得绕过 webhook 节流重发事件"


def test_buried_alert_withdrawn_when_zero(monkeypatch):
    rows = [_row("c1", 2)]
    w, bus, state = _wd(monkeypatch, rows)
    w._check_buried_conversations(now=time.time())
    assert _sys_status(state, "buried_conv")
    rows.clear()
    w._check_buried_conversations(now=time.time() + 10)
    assert not _sys_status(state, "buried_conv"), "归零后通知中心条目必须撤掉"
    assert bus.events[-1][1].get("recovered") is True


def test_surface_in_ui_can_be_disabled_without_touching_webhook(monkeypatch):
    rows = [_row("c1", 2)]
    w, bus, state = _wd(monkeypatch, rows, surface_cfg=False)
    w._check_buried_conversations(now=time.time())
    assert not _sys_status(state, "buried_conv")
    assert len(bus.events) == 1, "关掉上屏不影响 webhook 事件"


def test_surface_does_not_evict_other_sys_status_entries(monkeypatch):
    rows = [_row("c1", 2)]
    w, bus, state = _wd(monkeypatch, rows)
    state.notif_queue = [{"type": "sys_status", "data": {"id": "aidegrade", "text": "x"},
                          "_notif_ts": 1}]
    w._check_buried_conversations(now=time.time())
    ids = [(n.get("data") or {}).get("id") for n in state.notif_queue]
    assert ids == ["aidegrade", "buried_conv"]


def test_alert_on_warn_and_surface_in_ui_are_separate_knobs():
    w = hw.HealthWatchdog(app=SimpleNamespace(state=SimpleNamespace()),
                          alert_on_warn=False, surface_in_ui=True)
    snap = w.status_snapshot()
    assert snap["alert_on_warn"] is False and snap["surface_in_ui"] is True


# ── 孤儿未读归零 ─────────────────────────────────────────────────────────

def test_purge_orphan_unread_only_touches_tombstoned(tmp_path):
    from src.inbox.models import InboxConversation, InboxMessage
    st = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    for ck in ("alive", "dead"):
        cid = f"whatsapp:a:{ck}"
        conv = InboxConversation(conversation_id=cid, platform="whatsapp", account_id="a",
                                 chat_key=ck, display_name=ck, last_text="hi",
                                 last_ts=now, unread=2)
        st.ingest_batch(conv, [InboxMessage(conversation_id=cid, direction="in",
                                            text="hi", ts=now, platform_msg_id=f"{ck}1")])
    # 直接立墓碑（与 delete_conversation 同表；不依赖其余清库副作用）
    with st._lock:
        st._conn.execute(
            "INSERT OR REPLACE INTO conversation_tombstones "
            "(conversation_id, platform, account_id, deleted_at) VALUES (?,?,?,?)",
            ("whatsapp:a:dead", "whatsapp", "a", now + 1))
        st._conn.commit()
    assert purge_orphan_unread(st) == 1
    assert int((st.get_conversation("whatsapp:a:dead") or {}).get("unread") or 0) == 0
    assert int((st.get_conversation("whatsapp:a:alive") or {}).get("unread") or 0) == 2
    assert purge_orphan_unread(st) == 0        # 幂等
    st.close()


def test_purge_orphan_unread_never_raises():
    assert purge_orphan_unread(None) == 0
    assert purge_orphan_unread(SimpleNamespace()) == 0


def test_startup_hygiene_runs_once_when_store_ready(monkeypatch):
    calls: List[str] = []
    monkeypatch.setattr("src.integrations.protocol_autoreply.sweep_stale_needs_human",
                        lambda store: calls.append("sweep") or {"cleared": 0, "kept": 0})
    monkeypatch.setattr("src.inbox.unread_aggregate.purge_orphan_unread",
                        lambda store: calls.append("orphan") or 0)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._hygiene_done = False
    w._app = SimpleNamespace(state=SimpleNamespace(inbox_store=None))
    w._startup_hygiene()
    assert calls == [] and w._hygiene_done is False, "store 未就绪 → 下 tick 再试"
    w._app = SimpleNamespace(state=SimpleNamespace(inbox_store=object()))
    w._startup_hygiene()
    assert calls == ["sweep", "orphan"] and w._hygiene_done is True


# ── 前端契约（模板热更新直上生产，静态钉住） ─────────────────────────────────

def test_buried_banner_lives_outside_collapsible_header():
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    i_body_end = tpl.index("<!-- /.list-header-body -->")
    i_hint = tpl.index('id="buried-hint"')
    assert i_hint > i_body_end, "被埋横幅必须在 .list-header-body 之外（那块列表一滚就折起）"
    assert "inbox.buried.banner" in tpl and "inbox.buried.view_each" in tpl
    assert "function _buriedOldestHours(" in tpl
    assert "ws_acup_hint_n" in tpl and "inbox.acct.unread_pop_hint" in tpl


def test_watchdog_wiring_pinned():
    src = (_ENGINE_ROOT / "src" / "inbox" / "health_watchdog.py").read_text(encoding="utf-8")
    assert "def _startup_hygiene(self)" in src
    assert "sweep_stale_needs_human" in src and "purge_orphan_unread" in src
    assert 'self._surface_ui_status(\n            "buried_conv"' in src.replace("\r\n", "\n")
    assert "surface_in_ui" in src


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for k in ("inbox.buried.banner", "inbox.buried.banner_nohours",
              "inbox.buried.view_each", "inbox.acct.unread_pop_hint"):
        assert k in ZH and k in EN, k
