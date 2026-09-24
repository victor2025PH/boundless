"""fleet P1：Agent 服务化（service.py）/ 自升级（updater.py）/ 操作端 CLI（admin.py）/ 下载清单（resolve_download）。

    python -m pytest tests/test_fleet_p1_service_updater.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.fleet import admin as admin_mod
from src.fleet import service as svc
from src.fleet import updater as upd
from src.fleet.agent import AgentConfig, NodeAgent, main as agent_main
from src.fleet.protocol import STATUS_DONE, STATUS_REJECTED, TASK_UPGRADE
from src.fleet.store import resolve_download


# ── service: 命令生成 ──────────────────────────────────────────────────────
def test_schtasks_commands_quote_paths_and_use_system_onstart():
    cmd = ["C:\\Program Files\\ChatX Agent\\chatx-agent.exe", "--state-dir", "C:\\ProgramData\\ChatX\\fleet", "run", "--service"]
    c = svc.build_schtasks_create(cmd)
    assert c[:4] == ["schtasks", "/Create", "/TN", svc.TASK_NAME]
    tr = c[c.index("/TR") + 1]
    assert tr.startswith('"C:\\Program Files\\ChatX Agent\\chatx-agent.exe" --state-dir')
    assert tr.endswith("run --service")
    for flag, val in (("/SC", "ONSTART"), ("/RU", "SYSTEM"), ("/RL", "HIGHEST")):
        assert c[c.index(flag) + 1] == val
    assert "/F" in c
    assert svc.build_schtasks_run() == ["schtasks", "/Run", "/TN", svc.TASK_NAME]
    d = svc.build_schtasks_delete()
    assert d[0][1] == "/End" and d[1][1] == "/Delete" and "/F" in d[1]


def test_systemd_unit_contains_exec_restart_and_state_env(tmp_path):
    unit = svc.build_systemd_unit(["/opt/agent/chatx-agent", "run", "--service"], state_dir=tmp_path)
    assert "ExecStart=/opt/agent/chatx-agent run --service" in unit
    assert "Restart=always" in unit
    assert f"Environment=CHATX_FLEET_STATE_DIR={tmp_path}" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "WorkingDirectory=" not in unit  # 未传 working_dir 时不写

    wd = tmp_path / "engine_root"
    unit_wd = svc.build_systemd_unit(
        ["/opt/agent/chatx-agent", "run", "--service"],
        state_dir=tmp_path,
        working_dir=wd,
    )
    assert f"WorkingDirectory={wd}" in unit_wd


def test_agent_command_source_vs_frozen(monkeypatch, tmp_path):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert svc.agent_command(tmp_path)[1:3] == ["-m", "src.fleet.agent"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert svc.agent_command(tmp_path) == [sys.executable, "--state-dir", str(tmp_path)]


def test_install_service_windows_runs_create_then_run(monkeypatch, tmp_path):
    monkeypatch.setattr(svc.os, "name", "nt")
    calls: List[List[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS", stderr="")

    res = svc.install_service(tmp_path, run=run)
    assert res["ok"] and res["kind"] == "schtasks"
    assert calls[0][1] == "/Create" and calls[1][1] == "/Run"
    assert res["command"][-2:] == ["run", "--service"]


def test_install_service_windows_failure_stops_early(monkeypatch, tmp_path):
    monkeypatch.setattr(svc.os, "name", "nt")
    calls: List[List[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: Access is denied.")

    res = svc.install_service(tmp_path, run=run)
    assert not res["ok"] and len(calls) == 1
    assert "denied" in res["steps"][0]["out"]


def test_service_workdir_source_vs_frozen(monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    wd = svc.service_workdir()
    assert wd is not None
    assert wd == Path(svc.__file__).resolve().parents[2]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert svc.service_workdir() is None


def test_install_service_linux_writes_working_directory(monkeypatch, tmp_path):
    """源码态 Linux install：unit 含 WorkingDirectory=引擎根。"""
    engine_root = tmp_path / "engine_root"
    engine_root.mkdir()
    unit_path = tmp_path / "chatx-agent.service"
    monkeypatch.setattr(svc.os, "name", "posix")
    monkeypatch.setattr(svc, "service_workdir", lambda: engine_root)
    monkeypatch.setattr(svc, "systemd_unit_path", lambda name=svc.SYSTEMD_UNIT: unit_path)

    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = svc.install_service(tmp_path / "state", run=run)
    assert res["ok"] and res["kind"] == "systemd"
    body = unit_path.read_text(encoding="utf-8")
    assert f"WorkingDirectory={engine_root}" in body
    assert f"Environment=CHATX_FLEET_STATE_DIR={tmp_path / 'state'}" in body


def test_install_service_linux_frozen_omits_working_directory(monkeypatch, tmp_path):
    """冻结 exe：不写 WorkingDirectory。"""
    import sys

    monkeypatch.setattr(svc.os, "name", "posix")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    unit_path = tmp_path / "chatx-agent.service"
    monkeypatch.setattr(svc, "systemd_unit_path", lambda name=svc.SYSTEMD_UNIT: unit_path)

    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = svc.install_service(tmp_path / "state", run=run)
    assert res["ok"] and res["kind"] == "systemd"
    body = unit_path.read_text(encoding="utf-8")
    assert "WorkingDirectory=" not in body


def test_service_status_parses_schtasks_list(monkeypatch):
    monkeypatch.setattr(svc.os, "name", "nt")

    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="HostName: X\nTaskName: \\ChatX Fleet Agent\nStatus: Running\n", stderr="")

    st = svc.service_status(run=run)
    assert st == {"installed": True, "kind": "schtasks", "task_name": svc.TASK_NAME, "state": "Running"}

    def run_missing(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: not found")

    assert svc.service_status(run=run_missing)["installed"] is False


# ── service: 监督循环 ──────────────────────────────────────────────────────
class _FakeAgent:
    def __init__(self, node_key: str, *, revoked=False, exit_requested=False, raise_on_run: Optional[Exception] = None):
        self.cfg = type("C", (), {"node_key": node_key})()
        self.revoked = revoked
        self.exit_requested = exit_requested
        self.raise_on_run = raise_on_run
        self.runs = 0

    def run_forever(self, stop):
        self.runs += 1
        if self.raise_on_run:
            raise self.raise_on_run


def test_supervise_waits_when_unenrolled_then_runs():
    sleeps: List[float] = []
    agents = [_FakeAgent(""), _FakeAgent("k")]
    it = iter(agents)
    stop = threading.Event()

    def make():
        a = next(it)
        if a is agents[-1]:
            stop.set()  # 最后一轮跑完即停
        return a

    rc = svc.supervise(make, stop, sleep=sleeps.append)
    assert rc == 0
    assert sleeps == [svc.RETRY_UNENROLLED_SEC]
    assert agents[1].runs == 1


def test_supervise_revoked_sleeps_and_retries_with_fresh_agent():
    sleeps: List[float] = []
    made = 0

    def make():
        nonlocal made
        made += 1
        return _FakeAgent("k", revoked=True)

    rc = svc.supervise(make, sleep=sleeps.append, max_rounds=3)
    assert rc == 0 and made == 3
    assert sleeps == [svc.RETRY_REVOKED_SEC] * 3


def test_supervise_exit_requested_returns_3():
    rc = svc.supervise(lambda: _FakeAgent("k", exit_requested=True), sleep=lambda s: None)
    assert rc == 3


def test_supervise_crash_backoff_doubles_and_caps():
    sleeps: List[float] = []
    svc.supervise(lambda: _FakeAgent("k", raise_on_run=RuntimeError("boom")), sleep=sleeps.append, max_rounds=8)
    assert sleeps[:4] == [5.0, 10.0, 20.0, 40.0]
    assert max(sleeps) <= svc.CRASH_BACKOFF_MAX


# ── updater ────────────────────────────────────────────────────────────────
def _blob(tmp_path: Path, content: bytes = b"NEWEXE") -> Tuple[bytes, str]:
    return content, hashlib.sha256(content).hexdigest()


def test_download_verified_ok_and_mismatch(tmp_path):
    data, sha = _blob(tmp_path)

    def fetch(url, dest):
        dest.write_bytes(data)

    out = upd.download_verified("http://x/a.exe", sha.upper(), tmp_path / "u" / "a.exe", fetch=fetch)
    assert out.read_bytes() == data and not out.with_suffix(".exe.part").exists()
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        upd.download_verified("http://x/a.exe", "0" * 64, tmp_path / "u" / "b.exe", fetch=fetch)
    assert not (tmp_path / "u" / "b.exe").exists() and not (tmp_path / "u" / "b.exe.part").exists()


def test_swap_script_windows_and_linux(tmp_path):
    cur, new = Path("C:/Program Files/ChatX Agent/chatx-agent.exe"), Path("C:/ProgramData/ChatX/fleet/updates/chatx-agent-0.2.0.exe")
    body, suf = upd.build_swap_script(cur, new, 4242, windows=True)
    assert suf == ".ps1"
    assert "Wait-Process -Id 4242" in body
    assert f"Copy-Item -LiteralPath '{cur}' -Destination '{cur}.bak'" in body
    assert f"Move-Item -LiteralPath '{new}' -Destination '{cur}'" in body
    assert f'schtasks /Run /TN "{svc.TASK_NAME}"' in body
    assert upd.swap_command(Path("x.ps1"))[:2] == ["powershell", "-NoProfile"]

    body, suf = upd.build_swap_script(Path("/opt/a/chatx-agent"), Path("/var/a/new"), 7, windows=False)
    assert suf == ".sh" and "kill -0 7" in body and f"systemctl restart {svc.SYSTEMD_UNIT}" in body
    assert upd.swap_command(Path("x.sh")) == ["/bin/sh", "x.sh"]


def test_windows_swap_runs_as_independent_scheduled_task():
    # 实机验证：agent 退出时 Task Scheduler 会连带杀掉 Popen 出来的 swap 子进程，必须借独立一次性任务
    cmds = upd.build_swap_task_commands(["powershell", "-File", r"C:\x\swap.ps1"])
    assert [c[1] for c in cmds] == ["/Create", "/Run"]
    create, run = cmds
    assert create[create.index("/TN") + 1] == upd.SWAP_TASK_NAME == f"{svc.TASK_NAME} Upgrade"
    assert "/RU" in create and create[create.index("/RU") + 1] == "SYSTEM" and "/F" in create
    assert r"C:\x\swap.ps1" in create[create.index("/TR") + 1]
    assert run[run.index("/TN") + 1] == upd.SWAP_TASK_NAME
    body, _ = upd.build_swap_script(Path(r"C:\a\chatx-agent.exe"), Path(r"C:\b\new.exe"), 1, windows=True)
    assert f'schtasks /Delete /TN "{upd.SWAP_TASK_NAME}" /F' in body


def test_apply_upgrade_rejects_without_sha_or_when_not_frozen(tmp_path):
    assert upd.apply_upgrade({"url": "http://x/a.exe"}, tmp_path, frozen=True)[0] == STATUS_REJECTED
    st, res, detail = upd.apply_upgrade({"url": "http://x/a.exe", "sha256": "ab"}, tmp_path, frozen=False)
    assert (st, detail) == (STATUS_REJECTED, "not_frozen")


def test_apply_upgrade_happy_path_spawns_swap_and_reports_exit(tmp_path):
    data, sha = _blob(tmp_path)
    spawned: List[List[str]] = []

    def fetch(url, dest):
        assert url == "https://bd2026.cc/downloads/fleet/chatx-agent.exe"
        dest.write_bytes(data)

    cur = tmp_path / "bin" / "chatx-agent.exe"
    st, res, detail = upd.apply_upgrade(
        {"url": "https://bd2026.cc/downloads/fleet/chatx-agent.exe", "sha256": sha, "version": "0.2.0"},
        tmp_path, current_exe=cur, fetch=fetch, spawn=spawned.append, frozen=True, pid=99)
    assert (st, detail) == (STATUS_DONE, "swap_scheduled")
    staged = Path(res["staged"])
    assert staged == tmp_path / "updates" / "chatx-agent-0.2.0.exe" and staged.read_bytes() == data
    assert res["exit"] is True and res["version"] == "0.2.0"
    assert len(spawned) == 1
    script = Path(spawned[0][-1])
    assert script.exists() and str(staged) in script.read_text(encoding="utf-8")


def test_apply_upgrade_download_failure_is_failed_not_exception(tmp_path):
    def fetch(url, dest):
        raise OSError("net down")

    st, res, detail = upd.apply_upgrade({"url": "http://x", "sha256": "ab"}, tmp_path, fetch=fetch, frozen=True)
    assert (st, detail) == ("failed", "download_failed") and "net down" in res["error"]


# ── agent 集成：upgrade 任务 → ack 后退出 ─────────────────────────────────
def _http_factory(acks: List[Dict[str, Any]], tasks: List[Dict[str, Any]]):
    def http(method, url, body, headers, timeout):
        if url.endswith("/api/fleet/heartbeat"):
            return 200, {"ok": True, "has_tasks": True, "heartbeat_sec": 30}
        if "/api/fleet/tasks/pull" in url:
            out, tasks[:] = list(tasks), []
            return 200, {"ok": True, "tasks": out}
        if url.endswith("/api/fleet/tasks/ack"):
            acks.append(body)
            return 200, {"ok": True}
        return 404, {"detail": url}
    return http


def test_agent_upgrade_task_acks_done_then_exits_loop(tmp_path, monkeypatch):
    cfg = AgentConfig(tmp_path)
    cfg.data.update({"controller_url": "https://c", "node_key": "nk", "node_id": "n1"})
    acks: List[Dict[str, Any]] = []
    data, sha = _blob(tmp_path)
    tasks = [{"task_id": "t1", "kind": TASK_UPGRADE, "payload": {"url": "https://c/dl/a.exe", "sha256": sha, "version": "9"}},
             {"task_id": "t2", "kind": "ping", "payload": {}}]
    ag = NodeAgent(cfg, http=_http_factory(acks, tasks))
    spawned: List[Any] = []
    monkeypatch.setattr(upd, "_fetch", lambda url, dest: dest.write_bytes(data))
    monkeypatch.setattr(upd, "_spawn_detached", spawned.append)
    monkeypatch.setattr(upd, "is_frozen", lambda: True)
    monkeypatch.setattr(upd.sys, "executable", str(tmp_path / "cur.exe"))
    # agent.py 通过 apply_upgrade 默认参数引用模块内函数 → 走 monkeypatch 后的实现
    monkeypatch.setattr("src.fleet.agent.apply_upgrade",
                        lambda payload, sd: upd.apply_upgrade(payload, sd, fetch=upd._fetch, spawn=upd._spawn_detached,
                                                              frozen=True, current_exe=tmp_path / "cur.exe"))
    ag.run_forever(sleep=lambda s: None)  # exit_requested → 直接返回，不会死循环
    assert ag.exit_requested is True
    assert [a["task_id"] for a in acks] == ["t1"] and acks[0]["status"] == STATUS_DONE
    assert acks[0]["result"]["exit"] is True
    assert len(spawned) == 1
    assert tasks == []  # t2 已被 pull 走但未执行：由重启后的新进程重新领（主控按 TTL 重排）


def test_agent_upgrade_not_frozen_is_rejected(tmp_path):
    cfg = AgentConfig(tmp_path)
    cfg.data.update({"controller_url": "https://c", "node_key": "nk"})
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    st, res, detail = ag.execute({"task_id": "t", "kind": TASK_UPGRADE, "payload": {"url": "u", "sha256": "s"}})
    assert (st, detail) == (STATUS_REJECTED, "not_frozen") and ag.exit_requested is False


# ── agent CLI：service 子命令 ───────────────────────────────────────────────
def test_cli_service_subcommands(tmp_path, monkeypatch, capsys):
    import src.fleet.agent as agent_mod

    monkeypatch.setattr(agent_mod, "install_service", lambda sd: {"ok": True, "kind": "schtasks", "state_dir": str(sd)})
    monkeypatch.setattr(agent_mod, "uninstall_service", lambda: {"ok": True})
    monkeypatch.setattr(agent_mod, "service_status", lambda: {"installed": False, "kind": "schtasks", "state": ""})
    assert agent_main(["--state-dir", str(tmp_path), "install-service"]) == 0
    assert json.loads(capsys.readouterr().out)["state_dir"] == str(tmp_path)
    assert agent_main(["--state-dir", str(tmp_path), "uninstall-service"]) == 0
    capsys.readouterr()
    assert agent_main(["--state-dir", str(tmp_path), "service-status"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is False


def test_cli_run_service_uses_supervise_and_file_log(tmp_path, monkeypatch):
    import src.fleet.agent as agent_mod

    seen = {}

    def fake_supervise(make, stop):
        seen["agent"] = make()
        return 3

    monkeypatch.setattr(agent_mod, "supervise", fake_supervise)
    assert agent_main(["--state-dir", str(tmp_path), "run", "--service"]) == 3
    assert isinstance(seen["agent"], NodeAgent) and seen["agent"].cfg.state_dir == tmp_path
    assert (tmp_path / "logs" / "agent.log").parent.exists()


def test_yaml_scalar_fallback_reads_web_admin_token(tmp_path):
    import src.fleet.agent as agent_mod

    p = tmp_path / "config.yaml"
    p.write_text("domain: player_care\nweb_admin:\n  enabled: true\n  auth_token: \"tok-123\"  # c\n  port: 1\nother:\n  auth_token: no\n",
                 encoding="utf-8")
    assert agent_mod._grep_yaml_scalar(str(p), "web_admin", "auth_token") == "tok-123"
    assert agent_mod._grep_yaml_scalar(str(p), "web_admin", "missing") == ""


# ── admin CLI ──────────────────────────────────────────────────────────────
def test_admin_urls_and_bodies():
    calls: List[Tuple[str, str, Any]] = []

    def http(method, url, body):
        calls.append((method, url, body))
        if url.endswith("/api/fleet/nodes?group=&include_revoked=false"):
            return {"ok": True, "nodes": [{"node_id": "n1", "status": "online"}, {"node_id": "n2", "status": "offline"}]}
        return {"ok": True, "code": "12345678"}

    adm = admin_mod.Admin("https://bd2026.cc/fleet/", "T", http=http)
    assert adm.base == "https://bd2026.cc/fleet"
    adm.new_code(label="A-01", group="A", ttl_min=30)
    assert calls[-1] == ("POST", "https://bd2026.cc/fleet/api/fleet/enroll-codes", {"label": "A-01", "group_name": "A", "ttl_min": 30})
    adm.task("n1", "ping", payload={"echo": 1}, ttl_sec=60)
    assert calls[-1][1] == "https://bd2026.cc/fleet/api/fleet/nodes/n1/tasks"
    assert calls[-1][2] == {"kind": "ping", "payload": {"echo": 1}, "target": {}, "ttl_sec": 60}
    nodes = adm.nodes()
    assert [n["node_id"] for n in nodes] == ["n1", "n2"]
    res = adm.upgrade({"url": "https://x/a.exe", "sha256": "ab", "version": "1"}, node_ids=["n1"])
    assert len(res) == 1 and calls[-1][2]["kind"] == "upgrade" and calls[-1][2]["payload"]["sha256"] == "ab"
    with pytest.raises(SystemExit):
        adm.upgrade({"url": "https://x/a.exe"}, node_ids=["n1"])


def test_admin_main_requires_controller_and_token(monkeypatch, capsys):
    monkeypatch.delenv(admin_mod.ENV_CONTROLLER, raising=False)
    monkeypatch.delenv(admin_mod.ENV_TOKEN, raising=False)
    assert admin_mod.main(["nodes"]) == 2


# ── 下载清单 resolve_download ──────────────────────────────────────────────
def test_resolve_download_merges_manifest_with_config_override(monkeypatch):
    from src.fleet import store as store_mod

    monkeypatch.setattr(store_mod, "_manifest_cache", {"url": "", "at": 0.0, "data": None})
    fetched: List[str] = []

    def fetch(url):
        fetched.append(url)
        return {"version": "0.2.0", "url": "https://d/chatx-agent.exe", "sha256": "ff", "installer": "https://d/Install.ps1"}

    cfg = {"download": {"manifest_url": "https://d/manifest.json", "version": "", "installer_url": "", "sha256": "",
                        "changelog_url": "https://d/CHANGELOG", "install_script_url": "", "agent_zip_url": ""}}
    dl = resolve_download(cfg, now=1000.0, fetch=fetch)
    assert (dl["version"], dl["installer_url"], dl["sha256"], dl["install_script_url"]) == (
        "0.2.0", "https://d/chatx-agent.exe", "ff", "https://d/Install.ps1")
    assert dl["changelog_url"] == "https://d/CHANGELOG"
    resolve_download(cfg, now=1030.0, fetch=fetch)
    assert fetched == ["https://d/manifest.json"]  # 60s 缓存
    resolve_download(cfg, now=1100.0, fetch=fetch)
    assert len(fetched) == 2
    cfg["download"]["version"] = "pinned"
    assert resolve_download(cfg, now=1100.0, fetch=fetch)["version"] == "pinned"


def test_resolve_download_without_manifest_or_on_fetch_failure(monkeypatch):
    from src.fleet import store as store_mod

    monkeypatch.setattr(store_mod, "_manifest_cache", {"url": "", "at": 0.0, "data": None})
    cfg = {"download": {"manifest_url": "", "version": "1", "installer_url": "u", "sha256": "s"}}
    assert resolve_download(cfg, fetch=lambda u: pytest.fail("must not fetch")) == cfg["download"]
    cfg2 = {"download": {"manifest_url": "https://d/m.json", "version": "", "installer_url": "static", "sha256": ""}}
    dl = resolve_download(cfg2, now=5.0, fetch=lambda u: None)
    assert dl["installer_url"] == "static" and dl["version"] == ""
