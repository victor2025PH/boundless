"""P2-1：本机页轮询不再每 8 秒起一次 PowerShell。"""
from __future__ import annotations

import threading
from pathlib import Path

from src.fleet import local_status as ls

ENGINE = Path(__file__).resolve().parents[1]


def _counter():
    calls = []

    def q():
        calls.append(1)
        return {"installed": True, "state": "Running", "task_name": "t", "query": "ok"}
    return calls, q


def test_cache_reuses_within_ttl_and_requeries_after():
    ls.reset_task_cache()
    calls, q = _counter()
    t = [100.0]
    clock = lambda: t[0]
    a = ls.query_task_status_cached(query=q, clock=clock)
    t[0] += 8
    b = ls.query_task_status_cached(query=q, clock=clock)
    t[0] += 8
    c = ls.query_task_status_cached(query=q, clock=clock)
    assert len(calls) == 1 and a["cached"] is False and b["cached"] is True and c["state"] == "Running"
    t[0] += 30
    ls.query_task_status_cached(query=q, clock=clock)
    assert len(calls) == 2
    ls.query_task_status_cached(query=q, clock=clock, force=True)     # 「刷新状态」按钮
    assert len(calls) == 3
    ls.reset_task_cache()


def test_concurrent_requests_spawn_one_query():
    ls.reset_task_cache()
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(2)
        return {"installed": True, "state": "Ready", "query": "ok"}
    ths = [threading.Thread(target=lambda: ls.query_task_status_cached(query=slow)) for _ in range(5)]
    for th in ths:
        th.start()
    gate.set()
    for th in ths:
        th.join(5)
    assert len(calls) == 1
    ls.reset_task_cache()


def test_build_local_status_uses_cache_by_default(monkeypatch, tmp_path):
    ls.reset_task_cache()
    calls, q = _counter()
    monkeypatch.setattr(ls, "query_task_status", q)

    class Cfg:
        data = {}
        state_dir = tmp_path
        controller_url = ""
        heartbeat_sec = 30
        instances = []
    for _ in range(4):
        ls.build_local_status(Cfg(), machine_id="m", probe=lambda u: False, host="h")
    assert len(calls) == 1
    ls.build_local_status(Cfg(), machine_id="m", probe=lambda u: False, host="h", fresh=True)
    assert len(calls) == 2
    ls.reset_task_cache()


def test_panel_page_skips_hidden_and_forces_on_button():
    src = (ENGINE / "src/fleet/panel.py").read_text(encoding="utf-8")
    assert "if (!document.hidden) refresh(false);" in src
    assert '$("refresh").onclick = function () { note(""); refresh(true); };' in src
    assert '"?fresh=1"' in src and 'fresh=(qs.get("fresh") or [""])[0] == "1"' in src
