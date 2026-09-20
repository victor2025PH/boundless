# -*- coding: utf-8 -*-
"""实施86 0830 午批·批D 门禁：发送真相层（#72 假阴性对账 + #73 dead-peer 误标）。

金标=DC8F 包 UDEKBY 铁证：05:44 手动发送界面报「失败」而 05:47 轮询镜像确认
实际送达（慢确认 30-65s/条撞前端超时）；同一 peer 被 blocked 标 4 分钟内自动链
持续跳过、送达成功后标记仍不解除（AI 对该客户永久静默且坐席无感知）。
"""
from __future__ import annotations

import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── #72：文本发送对账（复用 voice_send_tracker，text: scope） ────────────────

def test_text_send_tracker_three_states():
    from src.inbox import voice_send_tracker as t
    scope = "text:telegram:acct:peer72"
    t.record_start(scope, "cmid-1")
    st = t.get_status(scope, "cmid-1")
    assert st["state"] == "in_flight"
    t.record_sent(scope, "cmid-1", payload={"message_id": "86556"})
    st2 = t.get_status(scope, "cmid-1")
    assert st2["state"] == "sent"
    assert st2["message_id"] == "86556"
    t.record_start(scope, "cmid-2")
    t.record_failed(scope, "cmid-2", "near_duplicate")
    assert t.get_status(scope, "cmid-2")["state"] == "failed"
    # 查无此键=unknown（老后端/过期/从未提交成功——前端按保守提示处理）
    assert t.get_status(scope, "cmid-ghost")["state"] == "unknown"


def test_send_route_wires_text_tracker():
    """路由静态契约：开始/终局各出口都落账 + 对账端点存在。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert 'f"text:{platform}:{account_id}:{chat_key}"' in src
    assert src.count("_track_failed(") >= 5   # 双守卫/两异常/undelivered
    assert "record_sent(_track_scope" in src
    assert '@app.get("/api/unified-inbox/send-status")' in src


def test_send_status_route_in_inventory():
    src = (_ENGINE_ROOT / "tests"
           / "test_admin_route_inventory.py").read_text(encoding="utf-8")
    assert "/api/unified-inbox/send-status\tGET" in src


# ── #73：dead-peer 误标三件套 ────────────────────────────────────────────────

def _reg(tmp_path, **kw):
    from src.ops.dead_peer_registry import DeadPeerRegistry
    return DeadPeerRegistry(path=str(tmp_path / "dp.json"), **kw)


def test_blocked_gets_default_ttl_not_permanent(tmp_path):
    """#73-②：blocked 未配置 TTL 时不再永久——内置 7 天到期放行探路。"""
    r = _reg(tmp_path)
    r.record("telegram", "8730484257", "blocked", evidence="USER_IS_BLOCKED x")
    assert r.is_blocked("telegram", "8730484257") is True
    # 篡改时间戳到 8 天前 → 过期释放
    key = "telegram:8730484257"
    r._data[key]["ts"] = time.time() - 8 * 86400
    assert r.is_blocked("telegram", "8730484257") is False


def test_deactivated_stays_permanent(tmp_path):
    """账号注销不可逆——内置 TTL 绝不适用（安全第一）。"""
    r = _reg(tmp_path)
    r.record("telegram", "111", "deactivated")
    r._data["telegram:111"]["ts"] = time.time() - 365 * 86400
    assert r.is_blocked("telegram", "111") is True


def test_evidence_provenance_recorded(tmp_path):
    """#73-①：标记带来源证据——「这个标哪来的」从无人能答变成条目自带答案。"""
    r = _reg(tmp_path)
    r.record("telegram", "222", "blocked",
             evidence="USER_IS_BLOCKED: [400 USER_IS_BLOCKED] ...")
    info = r.info_of("telegram", "222")
    assert info and info["evidence"].startswith("USER_IS_BLOCKED")
    assert r.info_of("telegram", "ghost") is None


def test_clear_on_delivery_unblocks(tmp_path, monkeypatch):
    """#73-②：真实送达即清标（送达成功=可达的最硬证据）。"""
    import src.ops.dead_peer_registry as dpr
    r = _reg(tmp_path)
    monkeypatch.setattr(dpr, "_SINGLETON", r)
    r.record("telegram", "8730484257", "blocked")
    assert dpr.clear_on_delivery("telegram", "8730484257") is True
    assert r.is_blocked("telegram", "8730484257") is False
    # 未标记/单例缺席都是 no-op False
    assert dpr.clear_on_delivery("telegram", "8730484257") is False
    monkeypatch.setattr(dpr, "_SINGLETON", None)
    assert dpr.clear_on_delivery("telegram", "1") is False


def test_send_route_clears_dead_peer_on_success():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "clear_on_delivery" in src


def test_sender_clears_and_records_evidence():
    src = (_ENGINE_ROOT / "src" / "client" / "sender.py").read_text(
        encoding="utf-8")
    assert "unblock(" in src            # 送达成功清标
    assert "evidence=str(e)" in src     # 记录来源证据


def test_send_caps_exposes_dead_peer_73():
    """#73-③：send-caps 带 chat_key 时回 dead_peer 可见块（坐席看得见停用+原因）。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert '"dead_peer"' in src
    assert "info_of" in src


# ── #88：解除钩子接全部送达路径 + 存量旧标记核销 ────────────────────────────

def test_88_clear_hooks_cover_all_delivery_paths():
    """#88 静态契约：四类真实送达路径都必须挂清标钩子。

    0830 skuio 实锤：#73 只挂了手动文本路由 + A 线主动外发——Yhang 会话
    AI 自动投递 18:38 双勾送达、黄条仍常驻。缺一路 = 那条链的送达永远
    解除不了黄条。
    """
    sender_src = (_ENGINE_ROOT / "src" / "client" / "sender.py").read_text(
        encoding="utf-8")
    # A 线：helper 定义 + 主动外发/自动回复/照片/语音文件 四个调用点
    assert sender_src.count("_dead_peer_clear_on_delivery(") >= 5
    worker_src = (_ENGINE_ROOT / "src" / "inbox"
                  / "autosend_worker.py").read_text(encoding="utf-8")
    # B 线：自动投递 + 人审通过投递 两个成功点
    assert worker_src.count("self._dead_peer_clear(") >= 2
    routes_src = (_ENGINE_ROOT / "src" / "web" / "routes"
                  / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    # 手动路由：文本 + 媒体 + 语音 三处
    assert routes_src.count("clear_on_delivery(platform, chat_key)") >= 3


def test_88_autosend_clear_behavior(tmp_path, monkeypatch):
    """#88 行为：worker 清标同时清共享表与本地封禁冷却（未标记 no-op 不抛）。"""
    import types

    import src.ops.dead_peer_registry as dpr
    from src.inbox.autosend_worker import AutosendWorker

    r = _reg(tmp_path)
    monkeypatch.setattr(dpr, "_SINGLETON", r)
    r.record("telegram", "888777", "blocked", evidence="USER_IS_BLOCKED probe")
    conv = "inbox:telegram:6834964252:888777"
    fake = types.SimpleNamespace(
        _blocked_conv_until={conv: 9e12},
        _dead_peer_shared=AutosendWorker._dead_peer_shared,
        _platform_of_conv=AutosendWorker._platform_of_conv,
    )
    AutosendWorker._dead_peer_clear(fake, conv)
    assert r.is_blocked("telegram", "888777") is False
    assert conv not in fake._blocked_conv_until
    # 未标记/空 conv：no-op 不抛
    AutosendWorker._dead_peer_clear(fake, conv)
    AutosendWorker._dead_peer_clear(fake, "")


def test_88_reconcile_stale_marks(tmp_path):
    """#88 存量核销：标记后有成功出站 → 清；无证据/仅入站/注销 → 保留。"""
    import sqlite3

    from src.ops.dead_peer_registry import reconcile_stale_marks

    r = _reg(tmp_path)
    now = time.time()
    r.record("telegram", "111222", "blocked")       # 标后有出站 → 清
    r.record("telegram", "333444", "blocked")       # 标后仅入站 → 留
    r.record("telegram", "555666", "deactivated")   # 注销恒不核销
    r.record("telegram", "777888", "blocked")       # 出站在标记**之前** → 留
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages ("
                "message_id TEXT PRIMARY KEY, conversation_id TEXT, "
                "direction TEXT, ts REAL)")
    rows = [
        ("m1", "telegram:6834964252:111222", "out", now + 60),
        ("m2", "telegram:6834964252:333444", "in", now + 60),
        ("m3", "telegram:6834964252:555666", "out", now + 60),
        ("m4", "telegram:6834964252:777888", "out", now - 3600),
    ]
    con.executemany("INSERT INTO messages VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    out = reconcile_stale_marks(r, db)
    assert out == {"checked": 4, "cleared": 1}
    assert r.is_blocked("telegram", "111222") is False
    assert r.is_blocked("telegram", "333444") is True
    assert r.is_blocked("telegram", "555666") is True
    assert r.is_blocked("telegram", "777888") is True
    # 幂等：再跑一遍零变化
    assert reconcile_stale_marks(r, db) == {"checked": 3, "cleared": 0}


def test_88_reconcile_soft_fails_without_db(tmp_path):
    """库文件缺失/坏路径 → 返回零计数绝不抛（启动任务 best-effort 承诺）。"""
    from src.ops.dead_peer_registry import reconcile_stale_marks

    r = _reg(tmp_path)
    r.record("telegram", "1", "blocked")
    out = reconcile_stale_marks(r, tmp_path / "no_such.db")
    assert out["cleared"] == 0
    assert r.is_blocked("telegram", "1") is True


def test_88_r2_reconcile_peer_mark_lazy(tmp_path):
    """#88 二轮：单会话 lazy 核销——打开会话复核「标记后有成功出站」即清。

    0831 skuio v1.0.64 复测实锤：启动一次性迁移只覆盖 boot 时刻已可核销的
    标记，之后才满足条件的要等下次重启（黄条在此期间赖着）。判据与批量核销
    完全同源：出站在标后 → 清；仅入站/出站在标前 → 留；deactivated 恒留；
    库缺失/别的账号的会话 → False 不抛。
    """
    import sqlite3

    from src.ops.dead_peer_registry import reconcile_peer_mark

    r = _reg(tmp_path)
    now = time.time()
    r.record("telegram", "111222", "blocked")
    r.record("telegram", "333444", "blocked")
    r.record("telegram", "555666", "deactivated")
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages ("
                "message_id TEXT PRIMARY KEY, conversation_id TEXT, "
                "direction TEXT, ts REAL)")
    con.executemany("INSERT INTO messages VALUES (?,?,?,?)", [
        ("m1", "telegram:6834964252:111222", "out", now + 60),
        ("m2", "telegram:6834964252:333444", "in", now + 60),
        ("m3", "telegram:6834964252:555666", "out", now + 60),
    ])
    con.commit()
    con.close()
    # 标后有出站 → 清标 + True
    assert reconcile_peer_mark(r, db, "telegram", "6834964252", "111222") is True
    assert r.is_blocked("telegram", "111222") is False
    # 幂等：已清 → False
    assert reconcile_peer_mark(r, db, "telegram", "6834964252", "111222") is False
    # 仅入站 → 留
    assert reconcile_peer_mark(r, db, "telegram", "6834964252", "333444") is False
    assert r.is_blocked("telegram", "333444") is True
    # 注销恒不核销（即便有出站镜像——只可能是时钟异常，保守）
    assert reconcile_peer_mark(r, db, "telegram", "6834964252", "555666") is False
    assert r.is_blocked("telegram", "555666") is True
    # 别的账号的会话不构成证据（精确 conversation_id 等值查询）
    assert reconcile_peer_mark(r, db, "telegram", "9999", "333444") is False
    # 库缺失 → False 不抛
    assert reconcile_peer_mark(
        r, tmp_path / "no_such.db", "telegram", "6834964252", "333444") is False


def test_88_r2_send_caps_wires_lazy_reconcile():
    """#88 二轮接线：send-caps 报 blocked 之前必须先跑 lazy 核销。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "reconcile_peer_mark" in src
    # lazy 复核必须发生在 dead_peer 可见块之前（清掉就不再报 blocked）
    i_rec = src.index("reconcile_peer_mark(")
    i_block = src.index('out["dead_peer"]')
    assert i_rec < i_block


def test_88_reconcile_wired_at_startup():
    """启动接线：lifecycle 挂 reconcile_dead_peer_marks 一次性任务。"""
    src = (_ENGINE_ROOT / "src" / "bootstrap" / "lifecycle.py").read_text(
        encoding="utf-8")
    assert "reconcile_dead_peer_marks" in src
    bt = (_ENGINE_ROOT / "src" / "bootstrap"
          / "background_tasks.py").read_text(encoding="utf-8")
    assert "reconcile_stale_marks" in bt
    assert "registry_from_config" in bt
