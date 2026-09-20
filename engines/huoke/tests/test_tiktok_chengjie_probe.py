"""TK-3 P2：真机联调探针。假 HTTP，零网络；每种失败给出可执行下一步与退出码。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from src.app_automation import tiktok_chengjie_probe as pr
from src.app_automation.tiktok_chengjie_bridge import DEVICES_PATH, DM_PATH, HANDBACK_PENDING_PATH

CFG = {
    "reply_engine": "chengjie",
    "chengjie": {"endpoint": "http://cj.test", "token": "secret-token", "account_id": "acc-main", "username": "shop_ph",
                 "timezone": "Asia/Manila", "device_account_map": {"DEV-A": {"account_id": "acc-1", "username": "u_a", "timezone": "Asia/Manila"}}},
}


def _fail_steps(rep: Dict[str, Any]) -> List[str]:
    return [s["step"] for s in rep["steps"] if not s["ok"]]


def test_config_failures_exit_1_with_fix_hints():
    rep = pr.run_probe({"reply_engine": "local"})
    assert rep["exit_code"] == pr.EXIT_CONFIG and "reply_engine" in _fail_steps(rep)
    assert any("chengjie" in s.get("fix", "") for s in rep["steps"] if s["step"] == "reply_engine")
    bad_tz = {**CFG, "chengjie": {**CFG["chengjie"], "timezone": "Mars/Olympus"}}
    rep = pr.run_probe(bad_tz)
    assert rep["exit_code"] == pr.EXIT_CONFIG and _fail_steps(rep) == ["timezone"]
    assert "Mars/Olympus" in [s for s in rep["steps"] if s["step"] == "timezone"][0]["detail"]
    no_tok = {**CFG, "chengjie": {**CFG["chengjie"], "token": ""}}
    assert _fail_steps(pr.run_probe(no_tok)) == ["token"]
    txt = pr.format_report(pr.run_probe(no_tok))
    assert "[FAIL] token" in txt and "→" in txt and "exit 1" in txt and "secret" not in txt


def test_connectivity_distinguishes_refused_auth_and_unmounted():
    def refused(m, u, h, b):
        return 0, {"error": "ConnectionRefusedError"}
    rep = pr.run_probe(CFG, http=refused)
    assert rep["exit_code"] == pr.EXIT_CONNECT and _fail_steps(rep) == ["connect"]
    assert "启动" in [s for s in rep["steps"] if s["step"] == "connect"][0]["fix"]

    rep = pr.run_probe(CFG, http=lambda m, u, h, b: (401, {"error": "unauthorized"}))
    assert rep["exit_code"] == pr.EXIT_CONNECT and _fail_steps(rep) == ["auth"]

    rep = pr.run_probe(CFG, http=lambda m, u, h, b: (404, {"detail": "Not Found"}))
    assert _fail_steps(rep) == ["bridge_mounted"]
    assert "huoke_bridge.enabled" in [s for s in rep["steps"] if s["step"] == "bridge_mounted"][0]["fix"]


def test_green_path_bind_and_roundtrip_exit_0():
    seen: List[Tuple[str, str, Any]] = []

    def http(method, url, headers, body):
        seen.append((method, url, body))
        assert headers.get("Authorization") == "Bearer secret-token"
        if url.endswith(pr.STATUS_PATH):
            return 200, {"ok": True, "enabled": True, "queued": 0, "sent": 3, "failed": 0, "health": {"acc-1": "authorized"},
                         "policy": {"dm_daily_cap": 20, "dm_max_len": 1000, "peer_silent_hours": 72}}
        if DEVICES_PATH in url:
            return 200, {"ok": True, "health": "authorized", "dm_daily_cap": 20}
        if DM_PATH in url:
            assert body["messages"][0]["direction"] == "in" and body["messages"][0]["msg_id"].startswith("probe:")
            assert body["messages"][0]["peer_username"] == pr.PROBE_PEER
            return 200, {"ok": True, "accepted": 1, "echo": 0, "drafted": 1}
        if HANDBACK_PENDING_PATH in url:
            return 200, {"ok": True, "queued": 0, "claimed": 0, "oldest_wait_sec": 0}
        raise AssertionError(url)

    rep = pr.run_probe(CFG, http=http, bind=True, roundtrip=True, now=1_800_000_000.0)
    assert rep["exit_code"] == pr.EXIT_OK and _fail_steps(rep) == []
    steps = {s["step"]: s for s in rep["steps"]}
    assert steps["bridge_mounted"]["policy"]["dm_daily_cap"] == 20 and steps["bind:DEV-A"]["ok"]
    assert "acc-1" in steps["bind:DEV-A"]["detail"] and "tiktok:user:chengjie_probe" in steps["roundtrip:dm_in"]["detail"]
    assert "人工在工作台点发送" in steps["roundtrip:pending"]["detail"]
    assert [u.split("http://cj.test")[1].split("?")[0] for _, u, _ in seen] == [
        pr.STATUS_PATH, DEVICES_PATH, DM_PATH, HANDBACK_PENDING_PATH]
    assert "全绿" in pr.format_report(rep)
    # 只 check：不碰 devices / dm
    seen.clear()
    assert pr.run_probe(CFG, http=http)["exit_code"] == 0 and [u for _, u, _ in seen] == ["http://cj.test" + pr.STATUS_PATH]


def test_bind_failure_exit_3_and_no_device_hint():
    def http(method, url, headers, body):
        if url.endswith(pr.STATUS_PATH):
            return 200, {"ok": True, "policy": {}}
        return 400, {"error": "bad_timezone:Mars/Olympus"}

    rep = pr.run_probe(CFG, http=http, bind=True)
    assert rep["exit_code"] == pr.EXIT_ACTION and _fail_steps(rep) == ["bind:DEV-A"]
    no_map = {**CFG, "chengjie": {**CFG["chengjie"], "device_account_map": {}}}
    rep = pr.run_probe(no_map, http=lambda m, u, h, b: (200, {"ok": True, "policy": {}}), bind=True)
    assert rep["exit_code"] == pr.EXIT_ACTION and _fail_steps(rep) == ["bind"] and "--device" in rep["steps"][-1]["fix"]
