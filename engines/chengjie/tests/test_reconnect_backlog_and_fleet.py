# -*- coding: utf-8 -*-
"""实施72 P2 契约：补收时间戳诚实化 + 重连积压封顶 + 外机自有号对端封顶。

三件事一个共同目标——「断线补收/跨机自有号」场景下 AI 不再按错误的时间/身份
语境自动开口，且合成时间不污染 SLO：

1. ``approx_ts``：合成时间戳的消息打标（回填/拉更早两条写入路径），SLO 剔除；
2. ``reconnect_backlog`` 封顶层：长断线恢复后的短窗强制 review（盖章数据来自
   session-status offline→online 的 meta 写入）；
3. ``own_fleet_peer`` 封顶层：对端命中 ``companion.own_fleet.extra``（外机自有号
   显式登记）→ review。**刻意不对同库自有号对子封顶**（老板拿自己账号扮客户
   测试是常态工作流）。
"""
from __future__ import annotations

import time
from typing import Any, Dict

from src.companion.proactive_peer_hygiene import (
    build_own_fleet_index,
    external_fleet_peer_match,
    own_fleet_extra_from_config,
)
from src.inbox.effective_automation import (
    apply_mode_caps,
    compute_mode_caps,
    reconnect_backlog_cfg,
)

_FLEET_CFG = {
    "companion": {"own_fleet": {"extra": [
        {"platform": "messenger", "account_id": "6158", "name": "Calixa Lopez"},
        {"platform": "telegram", "account_id": "999888777"},
    ]}},
}


def _caps(**kw: Any):
    base = dict(platform="messenger", account_id="A", config={},
                business_line="", connected_at=1.0)
    base.update(kw)
    return compute_mode_caps(**base)


def _layers(caps) -> list:
    return [c.layer for c in caps]


# ── 1. reconnect_backlog 配置解析 ────────────────────────────────────────


def test_backlog_cfg_defaults_and_overrides():
    d = reconnect_backlog_cfg(None)
    assert d == {"enabled": True, "min_downtime_hours": 6.0, "window_min": 10.0}
    o = reconnect_backlog_cfg({"inbox": {"auto_draft": {"reconnect_backlog": {
        "enabled": False, "min_downtime_hours": 2, "window_min": 30}}}})
    assert o == {"enabled": False, "min_downtime_hours": 2.0, "window_min": 30.0}
    # 结构坏 → 安全默认
    assert reconnect_backlog_cfg(
        {"inbox": {"auto_draft": {"reconnect_backlog": "junk"}}}
    )["enabled"] is True


# ── 2. reconnect_backlog 封顶层 ──────────────────────────────────────────


def test_backlog_cap_within_window_after_long_downtime():
    now = time.time()
    meta = {"last_recovered_at": now - 120, "last_downtime_sec": 8 * 3600}
    caps = _caps(account_meta=meta, now=now)
    assert "reconnect_backlog" in _layers(caps)
    cap = next(c for c in caps if c.layer == "reconnect_backlog")
    assert cap.ceiling == "review"
    assert cap.until_ts == (now - 120) + 600.0     # 窗口尾＝恢复时刻+window_min
    eff, applied = apply_mode_caps("auto_ai", caps)
    assert eff == "review"
    assert any(c.layer == "reconnect_backlog" for c in applied)


def test_backlog_cap_expires_after_window():
    now = time.time()
    meta = {"last_recovered_at": now - 601, "last_downtime_sec": 8 * 3600}
    assert "reconnect_backlog" not in _layers(_caps(account_meta=meta, now=now))


def test_backlog_cap_ignores_short_downtime():
    """worker 45 分钟级重启循环（本机实测）绝不触发——只有真·长断线才管。"""
    now = time.time()
    meta = {"last_recovered_at": now - 60, "last_downtime_sec": 45 * 60}
    assert "reconnect_backlog" not in _layers(_caps(account_meta=meta, now=now))


def test_backlog_cap_disabled_by_config():
    now = time.time()
    meta = {"last_recovered_at": now - 60, "last_downtime_sec": 9 * 3600}
    caps = _caps(account_meta=meta, now=now, config={
        "inbox": {"auto_draft": {"reconnect_backlog": {"enabled": False}}}})
    assert "reconnect_backlog" not in _layers(caps)


def test_backlog_cap_fail_open_on_meta_error(monkeypatch):
    import src.integrations.account_identity as ai_mod

    def boom(plat, acct):
        raise RuntimeError("registry down")

    monkeypatch.setattr(ai_mod, "cached_account_meta", boom)
    caps = _caps(account_meta=None)
    assert "reconnect_backlog" not in _layers(caps)


# ── 3. own_fleet 外机自有号 ──────────────────────────────────────────────


def test_own_fleet_extra_parse_and_robustness():
    assert own_fleet_extra_from_config(_FLEET_CFG) == [
        {"platform": "messenger", "account_id": "6158", "name": "Calixa Lopez"},
        {"platform": "telegram", "account_id": "999888777", "name": ""},
    ]
    assert own_fleet_extra_from_config(None) == []
    assert own_fleet_extra_from_config(
        {"companion": {"own_fleet": {"extra": "junk"}}}) == []
    # 缺 platform / 全空条目剔除
    assert own_fleet_extra_from_config({"companion": {"own_fleet": {"extra": [
        {"account_id": "x"}, {"platform": "tg"}, "junk"]}}}) == []


def test_external_fleet_match_by_id_and_name():
    assert external_fleet_peer_match(
        "telegram", "999888777", "", _FLEET_CFG) == "extra:999888777"
    # messenger e2ee：chat_key=线程 id 匹配不上 → 名字归一匹配兜住
    assert external_fleet_peer_match(
        "messenger", "1054679137531045", "  calixa   LOPEZ ",
        _FLEET_CFG) == "extra:6158"
    assert external_fleet_peer_match(
        "messenger", "1054679137531045", "Micah Bindo", _FLEET_CFG) == ""
    assert external_fleet_peer_match(          # 平台不符不命中
        "whatsapp", "6158", "Calixa Lopez", _FLEET_CFG) == ""
    assert external_fleet_peer_match("messenger", "", "", _FLEET_CFG) == ""
    assert external_fleet_peer_match("messenger", "6158", "x", None) == ""


def test_own_fleet_cap_layer_and_conversation_scoping():
    caps = _caps(config=_FLEET_CFG, chat_key="1054679137531045",
                 peer_name="Calixa Lopez")
    assert "own_fleet_peer" in _layers(caps)
    eff, _ = apply_mode_caps("auto_ai", caps)
    assert eff == "review"
    # 不传会话级信息 → 该层不判（账号级调用零影响）
    assert "own_fleet_peer" not in _layers(_caps(config=_FLEET_CFG))
    # 普通客户不命中
    assert "own_fleet_peer" not in _layers(_caps(
        config=_FLEET_CFG, chat_key="777", peer_name="Real Customer"))


def test_build_own_fleet_index_merges_extra_even_without_registry():
    class _BadReg:
        def list(self):
            raise RuntimeError("down")

    idx = build_own_fleet_index(
        _BadReg(), extra=own_fleet_extra_from_config(_FLEET_CFG))
    assert idx == {"messenger": {"6158"}, "telegram": {"999888777"}}


# ── 4. approx_ts 落库与读回 ──────────────────────────────────────────────


def _mk_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(tmp_path / "inbox.db")


def test_approx_ts_roundtrip_via_ingest(tmp_path):
    from src.inbox.ingest import ingest_thread
    store = _mk_store(tmp_path)
    cid = "messenger:A:777"
    chat = {"conversation_id": cid, "platform": "messenger", "account_id": "A",
            "chat_key": "777", "name": "Bea", "last_ts": 0, "unread": 0}
    n = ingest_thread(store, chat, [
        {"text": "synthetic-old", "direction": "in", "ts": 1750000000,
         "approx_ts": 1},
        {"text": "real-time", "direction": "in", "ts": 1750000100},
    ])
    assert n == 2
    msgs = store.list_recent_messages(cid, limit=10)
    by_text = {m["text"]: int(m.get("approx_ts") or 0) for m in msgs}
    assert by_text == {"synthetic-old": 1, "real-time": 0}


def test_reply_latency_excludes_approx_rows(tmp_path):
    """合成时间的补收行（in/out 双向）绝不进 SLO——不伪造「在等」也不伪造「已回」。"""
    from src.inbox.ingest import ingest_thread
    from src.ops.reply_latency import build_reply_latency
    store = _mk_store(tmp_path)
    now = time.time()
    cid = "messenger:A:777"
    chat = {"conversation_id": cid, "platform": "messenger", "account_id": "A",
            "chat_key": "777", "name": "Bea", "last_ts": 0, "unread": 0,
            "chat_type": "private"}
    ingest_thread(store, chat, [
        # 补收历史（合成 ts 落在窗口内）：一进一出——若不剔除会造出一个假 episode
        {"text": "backfill-in", "direction": "in", "ts": now - 3000,
         "approx_ts": 1},
        {"text": "backfill-out", "direction": "out", "ts": now - 2900,
         "approx_ts": 1},
        # 真实一问一答
        {"text": "real-q", "direction": "in", "ts": now - 600},
        {"text": "real-a", "direction": "out", "ts": now - 500},
    ])
    snap = build_reply_latency(store, now=now)
    d1 = snap["d1"]
    assert d1["episodes"] == 1                      # 只有真实那一段
    assert abs(d1["p50_s"] - 100.0) < 1.0           # 等待 100s，不被补收段拉偏


# ── 4b. 下游消费：thread 序列化透传 + 拟稿上下文打标 ─────────────────────


def test_store_message_to_obj_carries_approx_flag():
    from src.inbox.normalizer import store_message_to_obj
    row = {"message_id": "m1", "direction": "in", "text": "hi",
           "ts": 123.0, "approx_ts": 1}
    assert store_message_to_obj(row)["approx_ts"] == 1
    row2 = {"message_id": "m2", "direction": "in", "text": "hi", "ts": 123.0}
    assert store_message_to_obj(row2)["approx_ts"] == 0


def test_trim_stale_history_labels_approx_rows():
    """合成 ts 的补收行必须打「补收」标——它们的 ts 紧贴实时消息，断层判据
    对其失效，LLM 会把 9 天前的补收内容当刚说的接话（事故原型）。"""
    from src.inbox.persona_reply import trim_stale_history
    now = time.time()
    msgs = [
        {"direction": "in", "text": "old backfilled", "ts": now - 30,
         "approx_ts": 1},
        {"direction": "out", "text": "[图片] x", "ts": now - 20,
         "approx_ts": 1},                       # 已带 [ 前缀 → 不叠加
        {"direction": "in", "text": "fresh real", "ts": now - 10},
    ]
    out = trim_stale_history(msgs)
    assert out[0]["text"].startswith("[补收的历史消息，时间不详] ")
    assert out[1]["text"] == "[图片] x"          # 不叠加已有标记
    assert out[2]["text"] == "fresh real"        # 实时行原样
    # 原列表不被就地污染（纯函数语义）
    assert msgs[0]["text"] == "old backfilled"


# ── 5. thread-history / 拉更早 写入路径打标（端到端）─────────────────────


def test_backfill_paths_write_approx_flag(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.routes.unified_inbox_account_routes as uar

    app = FastAPI()
    cfg = {"platform_login": {"messenger": {"web_enabled": True,
                                            "web_url": "http://127.0.0.1:8791"}}}
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=SimpleNamespace(config=cfg))
    store = _mk_store(tmp_path)
    app.state.inbox_store = store
    c = TestClient(app)
    cid = "messenger:6158:777"

    # 空会话回填：缺 ts → 合成 → approx=1；显式 ts → approx=0
    r = c.post("/api/internal/protocol/thread-history", json={
        "platform": "messenger", "account_id": "6158", "chat_key": "777",
        "ts": 1750000100, "messages": [
            {"direction": "in", "text": "synth"},
            {"direction": "in", "text": "explicit", "ts": 1749990000},
        ]})
    assert r.json()["inserted"] == 2
    by_text = {m["text"]: int(m.get("approx_ts") or 0)
               for m in store.list_recent_messages(cid, limit=10)}
    assert by_text == {"synth": 1, "explicit": 0}

    # 非空合并分支：新条目锚定前置 + approx=1
    r2 = c.post("/api/internal/protocol/thread-history", json={
        "platform": "messenger", "account_id": "6158", "chat_key": "777",
        "ts": 1750000200, "messages": [
            {"direction": "out", "text": "older history"},
            {"direction": "in", "text": "synth"},        # 重复 → 滤
        ]})
    assert r2.json()["inserted"] == 1
    rows = store.list_recent_messages(cid, limit=10)
    older = next(m for m in rows if m["text"] == "older history")
    assert int(older.get("approx_ts") or 0) == 1

    # 拉更早：全部合成锚定 → approx=1
    async def fake_post(url, payload, timeout=20.0):
        return {"ok": True, "messages": [
            {"direction": "in", "text": "pulled ancient"}]}

    monkeypatch.setattr(
        "src.integrations.messenger_web_login._post_json", fake_post)
    r3 = c.post("/api/platforms/messenger/6158/history",
                json={"chat_key": "777", "count": 10})
    assert r3.json()["inserted"] == 1
    rows = store.list_recent_messages(cid, limit=10)
    pulled = next(m for m in rows if m["text"] == "pulled ancient")
    assert int(pulled.get("approx_ts") or 0) == 1


# ── 6. session-status 恢复盖章（端到端 wiring）───────────────────────────


def test_session_status_stamps_recovery_meta(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.routes.unified_inbox_account_routes as uar

    class _Reg:
        def __init__(self):
            self.rows = {"messenger:A": {
                "platform": "messenger", "account_id": "A",
                "status": "offline", "updated_at": time.time() - 7 * 3600,
                "meta": {"offline_reason": "worker:expired"},
            }}
            self.patches = []

        def get(self, plat, acct):
            row = self.rows.get(f"{plat}:{acct}")
            return dict(row) if row else None

        def upsert(self, plat, acct, **kw):
            self.patches.append(kw)
            row = self.rows.setdefault(f"{plat}:{acct}", {"meta": {}})
            if kw.get("status"):
                row["status"] = kw["status"]
            if kw.get("meta"):
                row.setdefault("meta", {}).update(kw["meta"])
            return dict(row)

        def list(self, *a, **k):
            return list(self.rows.values())

    reg = _Reg()
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: reg)

    class _Health:
        def record(self, *a, **kw):
            return {"changed": True, "went_unhealthy": False,
                    "recovered": True, "prev": "expired",
                    "status": "authorized", "superseded": []}

    monkeypatch.setattr(
        "src.integrations.platform_session_health.get_platform_session_health",
        lambda: _Health())
    monkeypatch.setattr(
        "src.integrations.platform_session_health.mark_superseded_accounts",
        lambda *a, **k: None)

    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={}))
    c = TestClient(app)
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "A",
        "status": "authorized", "login_id": "msg_x",
    })
    assert r.status_code == 200, r.text
    meta = reg.rows["messenger:A"]["meta"]
    assert float(meta.get("last_recovered_at") or 0) > 0
    # 断线时长 ≈ 7h（updated_at 距今）
    assert 6.5 * 3600 < float(meta.get("last_downtime_sec") or 0) < 7.5 * 3600
    assert reg.rows["messenger:A"]["status"] == "online"


# ── 封顶层「对坐席可解释」不变量（实施72 P6，2026-08-27）─────────────────────

def test_effcap_layers_have_seat_labels():
    """`compute_mode_caps` 产出的**每一个**封顶层都必须有坐席可读文案，并进前端
    已知层白名单——否则掉进 generic 分支，坐席看到的是 `identity_pending` 这种
    原始层名，且完全不告诉他「我该做什么」。

    实锤：④identity_pending / ⑤reconnect_backlog / ⑥own_fleet_peer 三层 08-27
    上线时就漏了这一步（后端封顶生效、前端说人话的那半没跟上）。本门禁从源码
    反查层名，新增层漏配即红。
    """
    import re
    from pathlib import Path

    from src.web.i18n_packs.inbox_effective_mode import EN, ZH

    root = Path(__file__).resolve().parents[1]
    src = (root / "src" / "inbox" / "effective_automation.py").read_text(
        encoding="utf-8")
    # ModeCap("layer", ...) —— 允许换行（多行调用形态）
    layers = sorted(set(re.findall(r'ModeCap\(\s*"([a-z_]+)"', src)))
    assert len(layers) >= 6, f"层名解析异常，只认出 {layers}"
    tpl = (root / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    for layer in layers:
        assert ZH.get(f"inbox.effcap.{layer}"), f"缺中文标签: {layer}"
        assert EN.get(f"inbox.effcap.{layer}"), f"缺英文标签: {layer}"
        assert ZH.get(f"inbox.effcap.{layer}_t"), f"缺中文说明: {layer}"
        assert EN.get(f"inbox.effcap.{layer}_t"), f"缺英文说明: {layer}"
        assert f"'{layer}'" in tpl, f"未进前端 EFFCAP_LAYERS 白名单: {layer}"

