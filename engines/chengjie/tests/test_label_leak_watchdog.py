# -*- coding: utf-8 -*-
"""系统标签泄漏巡检（2026-09-12「[我方语音消息]」事故）：按落库出站行兜底看见。

守卫（outbound_text_guard.strip_system_labels）负责拦；本巡检负责**看见**——守卫漏了 /
别的生成链没过守卫 / 老进程没装载新代码，都要有人知道。判定单源 label_leak_scan，
store 只给「以方括号开头的出站行」，语义全在纯函数里。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.inbox.label_leak_scan import scan_rows, scan_store, summarize


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _row(text: str, *, mid: str, ts: float, media_type: str = "", sent_by: str = "ai",
         cid: str = "whatsapp:639270135480:639273815533") -> Dict[str, Any]:
    return {"message_id": mid, "conversation_id": cid, "ts": ts, "text": text,
            "media_type": media_type, "sent_by": sent_by}


# 生产实锤四行 + 合法镜像占位行 + 人工广播行
def _incident_rows(t0: float) -> List[Dict[str, Any]]:
    return [
        _row("[我方发出的语音] 对呀，就是英语。", mid="m1", ts=t0 - 600, media_type="voice"),
        _row("[Voice message from our side] Um...", mid="m2", ts=t0 - 400, media_type="voice"),
        _row("[我方语音消息] 天哪，我这边说英语。", mid="m3", ts=t0 - 300),
        _row("[语音消息来自我们这边] 嗯...怎么说呢", mid="m4", ts=t0 - 100),
        _row("[图片] 刚拍的", mid="ok1", ts=t0 - 90, media_type="image"),
        _row("[语音]×2 晚安", mid="ok2", ts=t0 - 80, media_type="voice", sent_by=""),
        _row("【智聊 ChatX 1.0.84 已发布】…", mid="ok3", ts=t0 - 70, sent_by="",
             cid="telegram:6834964252:-1004345824259"),
        _row("[自动秒发] 你好", mid="bp1", ts=t0 - 60),
    ]


def _wd(monkeypatch, rows, cfg_extra=None, *, store_present=True):
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    calls: List[Dict[str, Any]] = []

    def _list(*, since_ts: float, limit: int = 500):
        calls.append({"since_ts": since_ts, "limit": limit})
        return [r for r in rows if r["ts"] > since_ts][:limit]

    store = SimpleNamespace(list_outbound_bracket_rows=_list) if store_present else None
    state = SimpleNamespace(inbox_store=store)
    conf: Dict[str, Any] = {"health_watchdog": {"label_leak_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["label_leak_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=state)
    w._config_manager = SimpleNamespace(config=conf)
    w.total_label_leak_alerts = 0
    return w, bus, calls


def test_scan_rows_classifies_incident_rows_only():
    t0 = time.time()
    found = scan_rows(_incident_rows(t0))
    kinds = {f["message_id"]: f["kind"] for f in found}
    assert kinds == {"m1": "system_label", "m2": "system_label", "m3": "system_label",
                     "m4": "system_label", "bp1": "bracket_prefix"}
    assert [f["message_id"] for f in found] == ["m1", "m2", "m3", "m4", "bp1"]   # ts 升序
    s = summarize(found)
    assert s["count"] == 5 and s["system_label"] == 4 and s["bracket_prefix"] == 1
    assert s["conversations"] == 1
    assert s["sample_tags"][:2] == ["[我方发出的语音]", "[Voice message from our side]"]
    assert summarize([]) == {"count": 0, "system_label": 0, "bracket_prefix": 0,
                             "conversations": 0, "top_conversations": [], "sample_tags": []}


def test_scan_store_tolerates_missing_method_and_errors():
    assert scan_store(SimpleNamespace()) == []
    assert scan_store(None) == []

    def _boom(**k):
        raise RuntimeError("db locked")
    assert scan_store(SimpleNamespace(list_outbound_bracket_rows=_boom)) == []


def test_alert_fires_once_with_samples_and_then_holds(monkeypatch):
    t0 = time.time()
    w, bus, calls = _wd(monkeypatch, _incident_rows(t0))
    w._check_label_leak(now=t0)
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "label_leak_alert"
    assert p["system_label"] == 4 and p["count"] == 5 and p["bracket_prefix"] == 1
    assert p["conversations"] == 1
    assert "[我方语音消息]" in p["sample_tags"]
    assert p["latest_text"].startswith("[语音消息来自我们这边]")
    assert p["reminder"] is False and p["rate_key"] == "label_leak:remind"
    assert w.total_label_leak_alerts == 1
    # 回看窗口＝24h 默认
    assert abs((t0 - calls[0]["since_ts"]) - 24 * 3600) < 1.0
    # 同一批行：1h 后不重提；4h 后内容未变也不重提（一天一次）
    w._check_label_leak(now=t0 + 3600)
    w._check_label_leak(now=t0 + 4 * 3600 + 10)
    assert len(bus.events) == 1


def test_new_leak_row_triggers_reminder_after_interval(monkeypatch):
    t0 = time.time()
    rows = _incident_rows(t0)
    w, bus, _ = _wd(monkeypatch, rows)
    w._check_label_leak(now=t0)
    rows.append(_row("[我方发出的图片] 看这张", mid="m5", ts=t0 + 5 * 3600, media_type="image"))
    w._check_label_leak(now=t0 + 5 * 3600 + 1)
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True and bus.events[1][1]["unchanged"] is False


def test_recovery_notice_only_after_prior_alert(monkeypatch):
    t0 = time.time()
    rows = _incident_rows(t0)
    w, bus, _ = _wd(monkeypatch, rows)
    w._check_label_leak(now=t0)
    assert len(bus.events) == 1
    # 25h 后窗口里没有任何泄漏行 → 恢复通知一次
    w._check_label_leak(now=t0 + 25 * 3600)
    assert len(bus.events) == 2 and bus.events[1][1] == {
        "recovered": True, "lookback_hours": 24.0, "rate_key": "label_leak:recovered"}
    # 再巡检：没告警过就没有恢复
    w._check_label_leak(now=t0 + 26 * 3600)
    assert len(bus.events) == 2


def test_quiet_paths(monkeypatch):
    t0 = time.time()
    # 只有合法占位 / 人工广播 / bracket_prefix → 不响（低级别不单独告警）
    rows = [r for r in _incident_rows(t0) if r["message_id"] in ("ok1", "ok2", "ok3", "bp1")]
    w, bus, _ = _wd(monkeypatch, rows)
    w._check_label_leak(now=t0)
    assert bus.events == []
    # 关闭开关
    w2, bus2, _ = _wd(monkeypatch, _incident_rows(t0), {"enabled": False})
    w2._check_label_leak(now=t0)
    assert bus2.events == []
    # 无 store / 老 store 没有该方法
    w3, bus3, _ = _wd(monkeypatch, _incident_rows(t0), store_present=False)
    w3._check_label_leak(now=t0)
    assert bus3.events == []
    w4 = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w4._app = SimpleNamespace(state=SimpleNamespace(inbox_store=SimpleNamespace()))
    w4._config_manager = SimpleNamespace(config={})
    w4._check_label_leak(now=t0)          # 不抛即通过


def test_store_method_end_to_end(tmp_path):
    """真 InboxStore：只回出站、以方括号开头、未软删的行，含 sent_by / media_type。"""
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "whatsapp:acct:peer"
    t0 = time.time()
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="a", direction="out",
                                      text="[我方语音消息] 天哪", ts=t0 - 10))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="b", direction="in",
                                      text="[我方语音消息] 客户发的不算", ts=t0 - 9))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="c", direction="out",
                                      text="正常文本", ts=t0 - 8))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="d", direction="out",
                                      text="[图片] 配文", media_type="image", ts=t0 - 7))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="e", direction="out",
                                      text="【公告】x", ts=t0 - 3 * 86400))     # 窗外
    rows = store.list_outbound_bracket_rows(since_ts=t0 - 86400)
    assert [r["platform_msg_id"] if "platform_msg_id" in r else r["message_id"] for r in rows]
    texts = [r["text"] for r in rows]
    assert texts == ["[图片] 配文", "[我方语音消息] 天哪"]          # ts DESC
    assert set(rows[0]) >= {"message_id", "conversation_id", "ts", "text", "media_type", "sent_by"}
    found = scan_store(store, lookback_hours=24, now=t0)
    assert [f["kind"] for f in found] == ["system_label"] and found[0]["text"].startswith("[我方语音消息]")
    store.close()
