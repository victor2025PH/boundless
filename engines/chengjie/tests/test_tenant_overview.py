"""托管租户观测面收集器门禁（ops「☁️ 托管租户」卡数据源，全注入零真实IO）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from src.ops.tenant_overview import collect_tenant_overview


def _seed(tmp_path: Path) -> dict:
    """最小生产形态：stack 两租户+双生产实例、一张交付卡、暂停旗、备份、守护日志。"""
    now = time.time()
    stack = tmp_path / "stack.json"
    stack.write_text(json.dumps({"services": [
        {"id": "chengjie_zhiliao", "ports": [18799]},        # 生产，必须被排除
        {"id": "chengjie_tongyi", "ports": [18899]},         # 生产，必须被排除
        {"id": "chengjie_zhiliao_acme", "ports": [19099]},
        {"id": "chengjie_zhiliao_pilot", "ports": [18999]},
        {"id": "other_service", "ports": [1234]},            # 非本引擎，排除
    ]}), encoding="utf-8")

    base = tmp_path / "instances"
    ops = base / ".ops"
    (ops / "suspended").mkdir(parents=True)
    (ops / "suspended" / "zhiliao_pilot.flag").write_text("{}", encoding="utf-8")
    # pilot：suspended + 已过期账本 → 到期计数必须排除（停机=到期口径已执行）
    (base / "zhiliao_pilot").mkdir(parents=True)
    (base / "zhiliao_pilot" / "tenant_card.json").write_text(json.dumps({
        "customer": "pilot", "status": "suspended",
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(now - 86400)),
    }), encoding="utf-8")

    (base / "zhiliao_acme").mkdir(parents=True)
    (base / "zhiliao_acme" / "tenant_card.json").write_text(json.dumps({
        "customer": "Acme", "status": "running", "exposed": True,
        "public_url": "https://bd2026.cc:19087",
        "provisioned_at": "2026-08-07 03:00:00",
        # 到期账本：now-2h 已过期 → expiry_state=expired（收入红灯判据）
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(now - 7200)),
    }), encoding="utf-8")

    bdir = ops / "backups" / "zhiliao_acme"
    bdir.mkdir(parents=True)
    (bdir / "zhiliao_acme_20260807_030000.zip").write_bytes(b"zip")

    (ops / "fulfilled_hosted.json").write_text(json.dumps({
        "done": {"AH-1": 1, "AH-2": 2},
        "held": {"AH-3": {"since": now - 7200, "instance_id": "zhiliao_acme",
                          "public_url": "https://bd2026.cc:19087",
                          "last_attempt": now - 60}},
        "last_tick": {"ts": int(now - 30), "handled": 0, "held": 1, "manual": 0},
    }), encoding="utf-8")

    logs = tmp_path / "guard_logs"
    logs.mkdir()
    (logs / "fulfill_20260807.log").write_text("tick", encoding="utf-8")
    return {"stack": stack, "base": base, "ops": ops, "logs": logs, "now": now}


def test_collect_full_snapshot(tmp_path):
    s = _seed(tmp_path)
    d = collect_tenant_overview(
        stack_path=s["stack"], instances_base=s["base"], ops_base=s["ops"],
        guard_log_dir=s["logs"],
        listeners_fn=lambda port: port == 19099,   # 只有 acme 在听
        now=s["now"])

    assert d["active"] is True
    assert d["counts"] == {"total": 2, "running": 1, "suspended": 1, "down": 0,
                           "expired": 1, "expiring": 0}
    iids = {t["instance_id"] for t in d["tenants"]}
    assert iids == {"zhiliao_acme", "zhiliao_pilot"}   # 生产双实例/外族服务绝不入列
    acme = next(t for t in d["tenants"] if t["instance_id"] == "zhiliao_acme")
    assert acme["state"] == "running" and acme["exposed"] is True
    assert acme["customer"] == "Acme"
    assert acme["last_backup_age_h"] is not None and acme["last_backup_age_h"] >= 0
    assert acme["expiry_state"] == "expired" and acme["expiry_days_left"] < 0
    assert d["counts"]["expired"] == 1
    pilot = next(t for t in d["tenants"] if t["instance_id"] == "zhiliao_pilot")
    assert pilot["state"] == "suspended"
    assert pilot["last_backup_age_h"] is None
    # suspended+过期账本：行内如实展示 expired，但 counts 排除（停机=已处置，不再红）
    assert pilot["expiry_state"] == "expired"
    assert d["counts"]["expired"] == 1   # 只有 running 的 acme 计入

    assert d["fulfill"]["done_total"] == 2
    held = d["fulfill"]["held"]
    assert len(held) == 1 and held[0]["order_id"] == "AH-3"
    assert held[0]["held_hours"] == 2.0
    assert d["fulfill"]["last_tick"]["held"] == 1
    assert d["guards"]["fulfill_log_age_min"] is not None
    assert d["guards"]["watch_log_age_min"] is None   # 没写过 watch 日志


def test_down_state_when_should_run_but_no_listener(tmp_path):
    s = _seed(tmp_path)
    d = collect_tenant_overview(
        stack_path=s["stack"], instances_base=s["base"], ops_base=s["ops"],
        guard_log_dir=s["logs"], listeners_fn=lambda port: False, now=s["now"])
    acme = next(t for t in d["tenants"] if t["instance_id"] == "zhiliao_acme")
    assert acme["state"] == "down"          # 未暂停且端口没人听 = 掉线（自愈/红灯依据）
    assert d["counts"]["down"] == 1


def test_inactive_when_no_tenants_and_no_held(tmp_path):
    (tmp_path / "stack.json").write_text(json.dumps({"services": [
        {"id": "chengjie_zhiliao", "ports": [18799]}]}), encoding="utf-8")
    d = collect_tenant_overview(
        stack_path=tmp_path / "stack.json", instances_base=tmp_path,
        ops_base=tmp_path / ".ops", guard_log_dir=tmp_path / "logs",
        listeners_fn=lambda port: False, now=time.time())
    assert d["active"] is False              # 前端据此整卡隐藏
    assert d["counts"]["total"] == 0
    assert d["fulfill"]["held"] == []


def test_expiry_status_pure():
    """到期判定纯函数：none/ok/expiring/expired + 坏格式按 none（宁漏催不误停）。"""
    from src.ops.tenant_lifecycle import expiry_status

    now = time.time()
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731
    assert expiry_status({}, now) == ("none", None)
    assert expiry_status({"expires_at": "garbage"}, now) == ("none", None)
    st, d = expiry_status({"expires_at": fmt(now + 30 * 86400)}, now)
    assert st == "ok" and d > 3
    st, d = expiry_status({"expires_at": fmt(now + 2 * 86400)}, now)
    assert st == "expiring" and 0 < d <= 3
    st, d = expiry_status({"expires_at": fmt(now - 86400)}, now)
    assert st == "expired" and d < 0


def test_missing_everything_soft_fails(tmp_path):
    """全部文件缺失（新装机）：返回空快照，绝不抛异常。"""
    d = collect_tenant_overview(
        stack_path=tmp_path / "nope.json", instances_base=tmp_path / "nope",
        ops_base=tmp_path / "nope_ops", guard_log_dir=tmp_path / "nope_logs",
        listeners_fn=lambda port: False, now=time.time())
    assert d["active"] is False and d["tenants"] == []
    assert d["guards"]["fulfill_log_age_min"] is None
