"""Machine-shared dual-instance restart cooldown status (ops card)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.utils.instance_restart_status import (
    collect_restart_status,
    current_instance_id,
    dump_prom,
    load_instance_record,
    seat_restart_banner,
    summarize_record,
)


def test_summarize_active_and_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    now = time.time()
    rec = {
        "instance": "zhiliao",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 120)),
        "unix": int(now - 120),
        "reason": "restart_instance",
        "cooldown_min": 10,
        "window_sec": 28,
        "port": 18799,
    }
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps(rec), encoding="utf-8"
    )
    loaded = load_instance_record("zhiliao")
    assert loaded is not None
    assert loaded["reason"] == "restart_instance"
    s = summarize_record(loaded, now=now)
    assert s["has_record"] is True
    assert s["cooldown_active"] is True
    assert 7 <= s["cooldown_left_min"] <= 10
    assert s["window_sec"] == 28

    # expired
    old = dict(rec)
    old["unix"] = int(now - 3600)
    s2 = summarize_record(old, now=now)
    assert s2["cooldown_active"] is False
    assert s2["cooldown_left_sec"] == 0


def test_collect_both_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    now = time.time()
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 30),
            "reason": "watchdog_httpdead", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    (d / "last_restart_tongyi.json").write_text(
        json.dumps({
            "instance": "tongyi", "unix": int(now - 7200),
            "reason": "restart_instance", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    snap = collect_restart_status(now=now)
    assert snap["ok"] is True
    assert snap["cooldown_active_any"] is True
    by_id = {x["id"]: x for x in snap["instances"]}
    assert by_id["zhiliao"]["cooldown_active"] is True
    assert by_id["tongyi"]["cooldown_active"] is False
    assert by_id["zhiliao"]["reason"] == "watchdog_httpdead"


def test_missing_files_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(tmp_path / "empty"))
    snap = collect_restart_status()
    assert snap["ok"] is True
    assert snap["cooldown_active_any"] is False
    assert all(not x["has_record"] for x in snap["instances"])


def test_current_instance_id_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHENGJIE_PRODUCT_ID", raising=False)
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    assert current_instance_id() is None
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "zhiliao")
    assert current_instance_id() == "zhiliao"
    monkeypatch.delenv("CHENGJIE_PRODUCT_ID", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", r"D:\chengjie-instances\tongyi\data")
    assert current_instance_id() == "tongyi"


def test_seat_banner_only_self(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "tongyi")
    now = time.time()
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 30),
            "reason": "restart_instance", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    (d / "last_restart_tongyi.json").write_text(
        json.dumps({
            "instance": "tongyi", "unix": int(now - 30),
            "reason": "watchdog_start", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    ban = seat_restart_banner(now=now)
    assert ban["instance_id"] == "tongyi"
    assert ban["cooldown_active"] is True
    assert ban["reason"] == "watchdog_start"
    # other instance cooling must not show on this seat
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "zhiliao")
    # expire zhiliao only for contrast
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 7200),
            "reason": "restart_instance", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    ban2 = seat_restart_banner(now=now)
    assert ban2["instance_id"] == "zhiliao"
    assert ban2["cooldown_active"] is False


def test_dump_prom_gauges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    now = time.time()
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 60),
            "reason": "restart_instance", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    text = dump_prom(now=now)
    assert "chengjie_instance_restart_cooldown_active_any 1" in text
    assert 'chengjie_instance_restart_cooldown_active{instance="zhiliao"} 1' in text
    assert 'chengjie_instance_restart_cooldown_left_seconds{instance="zhiliao"}' in text
    assert 'chengjie_instance_restart_age_seconds{instance="tongyi"} -1' in text


def test_phase4_inbox_adaptive_wiring():
    """Static contract: soft-cool helpers + longer abort in cool window."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    base = (repo / "src" / "web" / "templates" / "workspace_base.html").read_text(
        encoding="utf-8"
    )
    assert "_threadSoftCoolActive" in inbox
    assert "_threadAbortMs" in inbox
    assert "22000" in inbox and "_markThreadSoftCoolLocal" in inbox
    assert "window.__wsRestartCool" in base
    assert "__wsRestartCoolLocal" in base
    assert "预热豁免" in (
        pathlib.Path(__file__).resolve().parents[3]
        / "deploy" / "instances" / "watchdog_instances.ps1"
    ).read_text(encoding="utf-8")


def test_phase5_thread_coalesce_and_status_phase():
    """Static contract: in-flight coalesce + graded http_phase in status/watchdog."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    root = pathlib.Path(__file__).resolve().parents[3]
    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    status = (root / "deploy" / "instances" / "status_instances.ps1").read_text(
        encoding="utf-8"
    )
    dog = (root / "deploy" / "instances" / "watchdog_instances.ps1").read_text(
        encoding="utf-8"
    )
    assert "_threadInflight" in inbox
    assert "_loadThreadBody" in inbox
    assert "_threadStillSelected" in inbox
    assert "_abortOtherThreadLoads" in inbox
    assert "function Probe-Login" in status
    assert "http_phase" in status
    assert "warming" in status
    assert "http_phase" in dog
    assert "warming" in dog


def test_phase6_chats_coalesce_and_flap_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import pathlib
    from src.utils.instance_restart_status import collect_restart_status, flap_for_instance

    repo = pathlib.Path(__file__).resolve().parent.parent
    root = pathlib.Path(__file__).resolve().parents[3]
    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    assert "_coalesceAsync" in inbox
    assert "_loadChatsBody" in inbox
    assert "_loadDraftsBody" in inbox
    assert "chats:main" in inbox

    ops = tmp_path / "ops"
    cd = ops / "restart_cooldown"
    cd.mkdir(parents=True)
    monkeypatch.setenv("CHENGJIE_OPS_DIR", str(ops))
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(cd))
    now = time.time()
    (ops / "last_status.json").write_text(
        json.dumps({
            "instances": [
                {
                    "id": "zhiliao", "http_phase": "warming",
                    "login_ready": False, "login": 0, "http": 0,
                    "proc_age_sec": 40, "note": "warming test",
                },
                {
                    "id": "tongyi", "http_phase": "ready",
                    "login_ready": True, "login": 200, "http": 401,
                    "proc_age_sec": 900,
                },
            ]
        }),
        encoding="utf-8",
    )
    # two recent events → flap
    lines = [
        json.dumps({"unix": int(now - 60), "instance": "zhiliao", "reason": "a"}),
        json.dumps({"unix": int(now - 30), "instance": "zhiliao", "reason": "b"}),
    ]
    (cd / "restart_events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    flap = flap_for_instance("zhiliao", now=now)
    assert flap["flapping"] is True and flap["count"] == 2
    snap = collect_restart_status(now=now)
    assert snap["warming_any"] is True
    assert snap["flapping_any"] is True
    by_id = {x["id"]: x for x in snap["instances"]}
    assert by_id["zhiliao"]["http_phase"] == "warming"
    assert by_id["zhiliao"]["flap"]["flapping"] is True
    assert by_id["tongyi"]["http_phase"] == "ready"
    assert (root / "deploy" / "instances" / "prometheus_alerts_chengjie_restart.yml").is_file()
    assert "RESTART FLAP" in (
        root / "deploy" / "instances" / "restart_instance.ps1"
    ).read_text(encoding="utf-8")


def test_phase7_snapshot_path_and_quiet_poll_wiring():
    """Static contract: status -SnapshotPath + seat quiet_poll + slow adaptive poll."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    repo = pathlib.Path(__file__).resolve().parent.parent
    status = (root / "deploy" / "instances" / "status_instances.ps1").read_text(
        encoding="utf-8"
    )
    dog = (root / "deploy" / "instances" / "watchdog_instances.ps1").read_text(
        encoding="utf-8"
    )
    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    base = (repo / "src" / "web" / "templates" / "workspace_base.html").read_text(
        encoding="utf-8"
    )
    assert "SnapshotPath" in status
    assert "Write-StatusSnapshot" in status
    assert "UTF8Encoding" in status
    assert "-SnapshotPath" in dog
    assert "Get-Content -LiteralPath $snapPath -Raw -Encoding UTF8" in dog
    assert "_pollIntervalMs" in inbox
    assert "ws-restart-cool-change" in inbox
    assert "quiet_poll" in (
        repo / "src" / "utils" / "instance_restart_status.py"
    ).read_text(encoding="utf-8")
    assert "quiet:" in base and "ws-restart-cool-change" in base


def test_seat_banner_quiet_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.utils.instance_restart_status import seat_restart_banner

    d = tmp_path / "cooldown"
    d.mkdir()
    ops = tmp_path / "ops"
    ops.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.setenv("CHENGJIE_OPS_DIR", str(ops))
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "zhiliao")
    now = time.time()
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 30),
            "reason": "restart_instance", "cooldown_min": 10,
        }),
        encoding="utf-8",
    )
    ban = seat_restart_banner(now=now)
    assert ban["cooldown_active"] is True
    assert ban["quiet_poll"] is True


def test_phase8_note_en_preferred_and_wiring():
    """ASCII note_en is ops SSOT; drill/probe/SSE quiet reconnect exist."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    repo = pathlib.Path(__file__).resolve().parent.parent
    status = (root / "deploy" / "instances" / "status_instances.ps1").read_text(
        encoding="utf-8"
    )
    smoke = (root / "deploy" / "instances" / "smoke_restart_resilience.ps1").read_text(
        encoding="utf-8"
    )
    prom = (
        root / "deploy" / "instances" / "prometheus_alerts_chengjie_restart.yml"
    ).read_text(encoding="utf-8")
    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    assert "note_en" in status
    assert "encoding-proof" in status or "Phase8" in status
    assert "note_en" in smoke
    assert (root / "deploy" / "instances" / "probe_ops_surface.ps1").is_file()
    assert (root / "deploy" / "instances" / "drill_restart_resilience.ps1").is_file()
    assert "18799/api/workspace/metrics" in prom.replace(" ", "")
    assert "delay=15000" in inbox


def test_phase9_grafana_wire_and_watchdog_note_en():
    """Grafana scrape/dashboard + watchdog alert prefers note_en."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    inst = root / "deploy" / "instances"
    dog = (inst / "watchdog_instances.ps1").read_text(encoding="utf-8")
    drill = (inst / "drill_restart_resilience.ps1").read_text(encoding="utf-8")
    assert "Get-InstAlertNote" in dog
    assert "note_en" in dog
    assert (inst / "prometheus_scrape_chengjie.yml").is_file()
    assert (inst / "grafana_dashboard_restart.json").is_file()
    assert (inst / "probe_prom_restart.ps1").is_file()
    dash = (inst / "grafana_dashboard_restart.json").read_text(encoding="utf-8")
    assert "chengjie_instance_restart_flapping_any" in dash
    assert "chengjie-dual-instance-restart" in dash
    assert "quiet_poll" in drill and "prometheus" in drill.lower()


def test_phase10_window_sla_breach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Restart window over the SLA ceiling surfaces as a breach; under stays clean."""
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.delenv("CHENGJIE_RESTART_WINDOW_SLA_SEC", raising=False)  # default 180
    now = time.time()
    # abnormally slow restart (240s > 180s SLA)
    slow = {
        "instance": "tongyi", "unix": int(now - 30),
        "reason": "drill", "cooldown_min": 10, "window_sec": 240,
    }
    s = summarize_record(slow, now=now)
    assert s["window_sec"] == 240
    assert s["window_sla_sec"] == 180
    assert s["window_sla_breach"] is True
    # normal cold start (150s <= 180s)
    fast = dict(slow, window_sec=150)
    s2 = summarize_record(fast, now=now)
    assert s2["window_sla_breach"] is False
    # missing window → never a breach
    nowin = dict(slow)
    nowin.pop("window_sec")
    s3 = summarize_record(nowin, now=now)
    assert s3["window_sla_breach"] is False

    # env override tightens ceiling
    monkeypatch.setenv("CHENGJIE_RESTART_WINDOW_SLA_SEC", "60")
    s4 = summarize_record(fast, now=now)
    assert s4["window_sla_sec"] == 60
    assert s4["window_sla_breach"] is True


def test_phase10_window_sla_prom_and_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.delenv("CHENGJIE_RESTART_WINDOW_SLA_SEC", raising=False)
    now = time.time()
    (d / "last_restart_zhiliao.json").write_text(
        json.dumps({
            "instance": "zhiliao", "unix": int(now - 30),
            "reason": "restart_instance", "cooldown_min": 10, "window_sec": 240,
        }),
        encoding="utf-8",
    )
    (d / "last_restart_tongyi.json").write_text(
        json.dumps({
            "instance": "tongyi", "unix": int(now - 30),
            "reason": "restart_instance", "cooldown_min": 10, "window_sec": 150,
        }),
        encoding="utf-8",
    )
    snap = collect_restart_status(now=now)
    assert snap["window_sla_breach_any"] is True
    assert snap["window_sla_sec"] == 180
    by_id = {x["id"]: x for x in snap["instances"]}
    assert by_id["zhiliao"]["window_sla_breach"] is True
    assert by_id["tongyi"]["window_sla_breach"] is False
    text = dump_prom(now=now)
    assert "chengjie_instance_restart_window_sla_breach_any 1" in text
    assert 'chengjie_instance_restart_window_seconds{instance="zhiliao"} 240' in text
    assert 'chengjie_instance_restart_window_sla_breach{instance="zhiliao"} 1' in text
    assert 'chengjie_instance_restart_window_sla_breach{instance="tongyi"} 0' in text


def test_phase10_soft_stop_smart_skip_and_sla_wiring():
    """stop_instance skips grace when soft-kill rejected; restart_instance alerts SLA."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    inst = root / "deploy" / "instances"
    stop = (inst / "stop_instance.ps1").read_text(encoding="utf-8")
    restart = (inst / "restart_instance.ps1").read_text(encoding="utf-8")
    prom = (inst / "prometheus_alerts_chengjie_restart.yml").read_text(encoding="utf-8")
    # soft-stop only waits grace when a soft-kill was accepted
    assert "softAccepted" in stop
    assert "skipping grace" in stop
    # restart_instance has an SLA window param + breach alert
    assert "WindowSlaSec" in restart
    assert "SLA BREACH" in restart
    # prom alert rule exists
    assert "ChengjieRestartWindowSlaBreach" in prom


def test_phase12b_boot_timing_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Boot phase timing rides the restart-status payload + prom (optional field)."""
    from src.monitoring.metrics_store import get_metrics_store
    from src.utils.instance_restart_status import boot_timing_snapshot

    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "zhiliao")
    store = get_metrics_store()
    try:
        # absent → None payload, no boot gauges (running pre-Phase11 code path)
        store.set_boot_timing(None)
        assert boot_timing_snapshot() is None
        snap0 = collect_restart_status(now=time.time())
        assert snap0["boot_timing"] is None
        assert snap0["boot_seat_ready_sec"] is None
        assert "chengjie_instance_boot_total_seconds" not in dump_prom()

        # present → payload carries phases + seat-ready; prom emits gauges
        store.set_boot_timing({
            "total_sec": 36.2,
            "slowest_phase": "telegram_clients",
            "slowest_sec": 20.1,
            "phases": [
                {"name": "config", "delta_sec": 0.4, "cumulative_sec": 0.4},
                {"name": "ai_client", "delta_sec": 2.1, "cumulative_sec": 2.5},
                {"name": "telegram_clients", "delta_sec": 20.1, "cumulative_sec": 22.6},
                {"name": "web_app", "delta_sec": 11.0, "cumulative_sec": 33.6},
                {"name": "monitoring", "delta_sec": 2.6, "cumulative_sec": 36.2},
            ],
        })
        snap = collect_restart_status(now=time.time())
        bt = snap["boot_timing"]
        assert bt and bt["total_sec"] == 36.2
        assert bt["instance"] == "zhiliao"  # tagged with this process's identity
        assert snap["boot_seat_ready_sec"] == 33.6  # cumulative at web_app mark
        text = dump_prom()
        assert 'chengjie_instance_boot_total_seconds{instance="zhiliao"} 36.2' in text
        assert 'chengjie_instance_boot_seat_ready_seconds{instance="zhiliao"} 33.6' in text
        assert (
            'chengjie_instance_boot_phase_seconds{instance="zhiliao",phase="telegram_clients"} 20.1'
            in text
        )
    finally:
        store.set_boot_timing(None)  # process-level singleton — never leak across tests


def test_deploy_ps1_nonascii_requires_bom():
    """PS5.1 decodes BOM-less .ps1 as ANSI/GBK → CJK scripts fail to PARSE.

    Real incident 2026-07-23 07:53: eight deploy scripts got re-saved without a
    BOM; the 08:03 scheduled watchdog run died on ParserError (ops toolchain
    silently dead). Any non-ASCII deploy script must carry a UTF-8 BOM so
    powershell.exe (the scheduled-task host) parses it correctly.
    """
    import pathlib

    inst = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "instances"
    offenders = []
    for p in sorted(inst.glob("*.ps1")):
        raw = p.read_bytes()
        has_bom = raw[:3] == b"\xef\xbb\xbf"
        if not has_bom and any(b > 127 for b in raw):
            offenders.append(p.name)
    assert not offenders, (
        f"BOM-less non-ASCII ps1 (PS5.1 GBK misparse breaks watchdog/restart): {offenders}"
    )


def test_phase12b_probe_and_card_wiring():
    """Static contract: probe prints boot phases; ops card renders boot line."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    repo = pathlib.Path(__file__).resolve().parent.parent
    probe = (root / "deploy" / "instances" / "probe_ops_surface.ps1").read_text(
        encoding="utf-8-sig"
    )
    ops = (repo / "src" / "web" / "templates" / "ops_overview.html").read_text(
        encoding="utf-8"
    )
    assert "boot_timing" in probe
    assert "boot phases" in probe
    assert "boot_timing" in ops
    assert "ov2_js_irst_boot" in ops


def test_live_note_prefers_note_en(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ops = tmp_path / "ops"
    ops.mkdir()
    cd = ops / "restart_cooldown"
    cd.mkdir()
    monkeypatch.setenv("CHENGJIE_OPS_DIR", str(ops))
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(cd))
    (ops / "last_status.json").write_text(
        json.dumps({
            "instances": [{
                "id": "zhiliao",
                "http_phase": "ready",
                "login_ready": True,
                "note": "HTTP 401 login 200（health 需鉴权，非 0 即视为活）",
                "note_en": "HTTP 401 login 200 (health may need auth; non-zero = alive)",
            }],
        }),
        encoding="utf-8",
    )
    snap = collect_restart_status(now=time.time())
    row = next(x for x in snap["instances"] if x["id"] == "zhiliao")
    assert "需鉴权" not in (row["live_note"] or "")
    assert "non-zero = alive" in (row["live_note"] or "")
    assert "需鉴权" in (row["live_note_zh"] or "")

