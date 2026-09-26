"""本机状态快照和「舰队节点」页。页面只监听 127.0.0.1。"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
from http.client import HTTPConnection
from pathlib import Path

import pytest

from src.fleet.agent import AGENT_VERSION, AgentConfig, NodeAgent, main as agent_main
from src.fleet.local_status import (
    build_local_status, classify_ui_state, mask_secrets, probe_controller, query_task_status,
    record_heartbeat, task_label_of, task_state_running,
)
from src.fleet.panel import PANEL_PORT, make_panel, request_allowed, run_panel, start_panel_background
from src.fleet.service import TASK_NAME

ENGINE = Path(__file__).resolve().parents[1]
SETUP = ENGINE / "fleet_agent" / "setup"


class _Cfg:
    def __init__(self, state_dir, data):
        self.state_dir = state_dir
        self.data = data
        self.controller_url = str(data.get("controller_url") or "").rstrip("/")
        self.node_key = str(data.get("node_key") or "")
        self.node_id = str(data.get("node_id") or "")
        self.heartbeat_sec = int(data.get("heartbeat_sec") or 30)
        self.instances = list(data.get("instances") or [])


def _snap(tmp_path, data, *, now=1_000.0, hb_at=None, probe=None, service=None, host="PC-1"):
    if hb_at is not None:
        record_heartbeat(tmp_path, hb_at)
    cfg = _Cfg(tmp_path, data)
    return build_local_status(
        cfg, machine_id="m-abc", now=now,
        probe=probe if probe is not None else (lambda url: False),
        service_status_fn=service or (lambda: {"installed": False, "state": "", "task_name": TASK_NAME, "query": "ok"}),
        host=host,
    )


@pytest.mark.parametrize("enrollment,reachable,hb,now,stale,expect", [
    ("pending", True, 990, 1000, 120, "pending"),
    ("rejected", True, 990, 1000, 120, "rejected"),
    ("none", True, 990, 1000, 120, "offline"),
    ("enrolled", True, 900, 1000, 120, "online"),
    ("enrolled", True, 100, 1000, 120, "offline"),
    ("enrolled", False, 900, 1000, 120, "offline"),
    ("enrolled", True, 5000, 1000, 120, "offline"),
])
def test_classify_ui_state(enrollment, reachable, hb, now, stale, expect):
    assert classify_ui_state(
        enrollment, controller_reachable=reachable, last_heartbeat_at=hb, now=now, stale_after=stale,
    ) == expect


@pytest.mark.parametrize("state,running", [
    ("Running", True),
    ("正在运行", True),
    ("active", True),
    ("Ready", False),
    ("就绪", False),
    ("", False),
])
def test_task_state_running(state, running):
    assert task_state_running(state) is running


def test_task_label_timeout_is_not_missing():
    assert task_label_of(installed=False, running=False, query="timeout") == "未能查询"
    assert task_label_of(installed=False, running=False, query="ok") == "未安装"
    assert task_label_of(installed=True, running=True, query="ok") == "运行中"
    assert task_label_of(installed=True, running=False, query="ok") == "未运行"


def test_build_local_status_masks_secrets_and_keeps_old_fields(tmp_path):
    data = {
        "controller_url": "https://ctl.test/fleet/",
        "node_id": "n1",
        "node_key": "nk_secret",
        "enroll_secret": "es_secret",
        "pairing_code": "AB12CD",
        "instances": [{
            "name": "player", "base_url": "http://127.0.0.1:18797",
            "auth_token": "tok", "domain": "player_care", "restart_cmd": "echo hi",
        }],
    }
    snap = _snap(
        tmp_path, data, hb_at=900, now=1000, probe=lambda url: True,
        service=lambda: {"installed": True, "state": "Running", "task_name": TASK_NAME, "query": "ok"},
    )
    dumped = json.dumps(snap, ensure_ascii=False)
    assert "nk_secret" not in dumped
    assert "es_secret" not in dumped
    assert '"tok"' not in dumped
    assert snap["enrolled"] is True and snap["enrollment"] == "enrolled"
    assert snap["pairing_code"] == "AB12CD"
    assert snap["instances"][0]["auth_token"] == "***"
    assert snap["instances"][0]["restart_cmd"] == "echo hi"
    assert snap["instances"][0]["domain"] == "player_care"
    assert snap["controller_url"] == "https://ctl.test/fleet"
    assert snap["controller_reachable"] is True
    assert snap["task_running"] is True and snap["task_label"] == "运行中"
    assert snap["ui_state"] == "online" and snap["ui_label"] == "在线"
    assert snap["last_heartbeat_at"].endswith("Z")
    assert snap["console_url"] == "https://ctl.test/fleet/console"
    assert snap["host_name"] == "PC-1"
    assert snap["agent_version"] == AGENT_VERSION
    assert snap["machine_id"] == "m-abc"
    assert snap["log_dir"].endswith("logs")


def test_pending_and_rejected_labels(tmp_path):
    pending = _snap(tmp_path, {"pending_request_id": "req-1", "pairing_code": "654321",
                               "controller_url": "https://ctl.test/fleet"})
    assert pending["ui_label"] == "待批准" and pending["pending"] is True and pending["enrolled"] is False
    rejected = _snap(tmp_path / "r", {"enroll_rejected": True})
    assert rejected["ui_label"] == "已拒绝" and rejected["enrollment"] == "rejected"


def test_ready_task_is_installed_but_not_running(tmp_path):
    snap = _snap(
        tmp_path, {"node_key": "nk_secret"},
        service=lambda: {"installed": True, "state": "Ready", "task_name": TASK_NAME, "query": "ok"},
    )
    assert snap["task_installed"] is True and snap["task_running"] is False
    assert snap["task_label"] == "未运行"


def test_mask_secrets_keeps_empty_and_pairing_code():
    masked = mask_secrets({
        "node_key": "nk_secret", "enroll_secret": "es", "token": "",
        "pairing_code": "AB12CD", "nested": {"password": "p", "room_key": "rk"},
        "restart_cmd": "echo hi", "api_token": "t",
    })
    assert masked["node_key"] == "***" and masked["token"] == ""
    assert masked["pairing_code"] == "AB12CD" and masked["restart_cmd"] == "echo hi"
    assert masked["nested"]["password"] == "***" and masked["api_token"] == "***"


def test_probe_controller_has_no_authorization_and_counts_http_errors():
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b""

    def urlopen(req, timeout=0):
        seen["auth"] = req.has_header("Authorization")
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    assert probe_controller("https://ctl.test/fleet", urlopen=urlopen) is True
    assert seen["auth"] is False and seen["ua"] == "chatx-agent-status"

    def missing(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "no", hdrs=None, fp=None)

    assert probe_controller("https://ctl.test/fleet", urlopen=missing) is True

    def down(req, timeout=0):
        raise urllib.error.URLError("down")

    assert probe_controller("https://ctl.test/fleet", urlopen=down) is False
    assert probe_controller("", urlopen=urlopen) is False
    assert probe_controller("ftp://ctl.test", urlopen=urlopen) is False


def test_query_timeout_is_not_reported_as_missing(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["schtasks"], timeout=5)

    monkeypatch.setattr("src.fleet.local_status.service_status", boom)
    got = query_task_status()
    assert got["query"] == "timeout" and got["installed"] is False
    snap = build_local_status(
        _Cfg(tmp_path, {"node_key": "nk"}), machine_id="m", now=10, probe=lambda url: False,
        service_status_fn=lambda: got, host="h",
    )
    assert snap["task_running"] is False and snap["task_label"] == "未能查询"


def test_heartbeat_stamp_is_timestamp_only(tmp_path):
    cfg = AgentConfig(tmp_path)
    cfg.data.update({"controller_url": "https://ctl.test/fleet", "node_key": "nk_secret", "node_id": "n1"})

    def http(method, url, body, headers, timeout):
        return 200, {"ok": True}

    agent = NodeAgent(cfg, http=http, clock=lambda: 1_700_000_000.5)
    assert agent.heartbeat()["ok"] is True
    text = (tmp_path / "last_heartbeat.json").read_text(encoding="utf-8")
    assert json.loads(text) == {"at": 1700000000.5}
    assert "nk_secret" not in text and "node_key" not in text


def test_cli_status_stays_compatible_and_adds_fields(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(
        "src.fleet.local_status.query_task_status",
        lambda: {"installed": False, "state": "", "task_name": TASK_NAME, "query": "ok"},
    )
    monkeypatch.setattr("src.fleet.local_status.probe_controller", lambda url, **kwargs: False)
    assert agent_main(["add-instance", "player=http://127.0.0.1:18797", "--auth-token", "tok", "--domain", "player_care"]) == 0
    capsys.readouterr()
    assert agent_main(["status"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["enrolled"] is False and '"tok"' not in out and "***" in out and "player_care" in out
    assert data["ui_label"] == "离线"
    assert data["controller_reachable"] is False and data["task_running"] is False
    assert "node_key" not in data


@pytest.fixture
def serve():
    servers = []

    def start(state_dir, machine_id="m-1", **kwargs):
        httpd = make_panel(state_dir, machine_id, port=0, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        servers.append(httpd)
        return httpd

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _req(port, method, path, *, host=None, body=None, content_type=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {}
    if host:
        headers["Host"] = host
    if content_type:
        headers["Content-Type"] = content_type
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    info = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, raw, info


def test_panel_is_loopback_and_renders_node_page(tmp_path, serve):
    httpd = serve(tmp_path, status_fn=lambda: {
        "ui_state": "pending", "ui_label": "待批准", "pairing_code": "654321",
        "node_key": "nk_secret", "auth_token": "tok", "enrollment": "pending",
    })
    host, port = httpd.server_address
    assert host == "127.0.0.1" and port != PANEL_PORT
    status, raw, headers = _req(port, "GET", "/api/local/health")
    assert status == 200
    body = json.loads(raw)
    assert body["service"] == "chatx-fleet-panel" and body["bind"] == "127.0.0.1"
    assert "access-control-allow-origin" not in headers
    assert "default-src 'none'" in headers["content-security-policy"]
    status, raw, _headers = _req(port, "GET", "/")
    page = raw.decode("utf-8")
    for text in ("舰队节点", "待批准", "在线", "离线", "已拒绝", "详情", "复制诊断"):
        assert text in page
    assert "innerHTML" not in page
    status, raw, _headers = _req(port, "GET", "/api/local/status")
    snap = json.loads(raw)
    assert snap["ui_label"] == "待批准" and snap["pairing_code"] == "654321"
    assert "nk_secret" not in raw.decode() and snap["node_key"] == "***"


def test_panel_rejects_foreign_host_and_ignores_client_log_path(tmp_path, serve):
    opened = []
    httpd = serve(tmp_path, opener=lambda path: opened.append(Path(path)))
    port = httpd.server_address[1]
    status, _raw, _headers = _req(port, "GET", "/api/local/health", host="evil.example")
    assert status == 403
    status, raw, _headers = _req(port, "POST", "/api/local/open-logs", body=b'{"path":"/etc/passwd"}')
    assert status == 415 and opened == []
    status, raw, _headers = _req(
        port, "POST", "/api/local/open-logs",
        body=b'{"path":"/etc/passwd"}', content_type="application/json",
    )
    assert status == 200
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert opened == [tmp_path.resolve() / "logs"]
    assert Path(payload["path"]) == tmp_path.resolve() / "logs"
    assert "/etc/passwd" not in payload["path"]


def test_open_logs_reports_path_when_opener_fails(tmp_path, serve):
    def boom(path):
        raise OSError("no window")

    httpd = serve(tmp_path, opener=boom)
    port = httpd.server_address[1]
    status, raw, _headers = _req(
        port, "POST", "/api/local/open-logs", body=b"{}", content_type="application/json",
    )
    assert status == 200
    payload = json.loads(raw)
    assert payload["ok"] is False and payload["path"].endswith("logs")


def test_make_panel_rejects_non_loopback(tmp_path):
    with pytest.raises(ValueError):
        make_panel(tmp_path, "m", host="0.0.0.0", port=0)


def test_request_allowed_only_loopback_hosts():
    assert request_allowed("127.0.0.1", "127.0.0.1:47321")
    assert request_allowed("::1", "[::1]:47321")
    assert request_allowed("127.0.0.1", "localhost")
    assert not request_allowed("192.168.1.9", "127.0.0.1")
    assert not request_allowed("127.0.0.1", "evil.example")
    assert not request_allowed("127.0.0.1", "")


def test_panel_status_reloads_pending_config(tmp_path, serve, monkeypatch):
    monkeypatch.setattr(
        "src.fleet.local_status.query_task_status",
        lambda: {"installed": True, "state": "正在运行", "task_name": TASK_NAME, "query": "ok"},
    )
    monkeypatch.setattr("src.fleet.local_status.probe_controller", lambda url, **kwargs: False)
    cfg = AgentConfig(tmp_path)
    cfg.data.update({
        "controller_url": "https://ctl.test/fleet",
        "pending_request_id": "req-9",
        "pairing_code": "112233",
        "enroll_secret": "es_secret",
    })
    cfg.save()
    httpd = serve(tmp_path, machine_id="m-reload")
    status, raw, _headers = _req(httpd.server_address[1], "GET", "/api/local/status")
    assert status == 200
    snap = json.loads(raw)
    assert snap["ui_label"] == "待批准" and snap["pairing_code"] == "112233"
    assert snap["task_label"] == "运行中" and snap["task_running"] is True
    assert "es_secret" not in raw.decode()


def test_cli_ui_delegates_to_run_panel(tmp_path, monkeypatch):
    import src.fleet.panel as panel_mod

    seen = {}

    def fake(cfg, *, machine_id, port, open_browser):
        seen["port"] = port
        seen["open"] = open_browser
        seen["dir"] = cfg.state_dir
        return 0

    monkeypatch.setattr(panel_mod, "run_panel", fake)
    assert agent_main(["--state-dir", str(tmp_path), "ui", "--no-browser", "--port", "9"]) == 0
    assert seen["port"] == 9 and seen["open"] is False and seen["dir"] == tmp_path
    assert agent_main(["--state-dir", str(tmp_path), "ui", "--no-browser"]) == 0
    assert seen["port"] == PANEL_PORT


def test_run_panel_opens_browser_when_already_up(monkeypatch):
    import src.fleet.panel as panel_mod

    monkeypatch.setattr(panel_mod, "panel_health_ok", lambda port, timeout=0.4: True)
    opened = []
    monkeypatch.setattr(panel_mod, "launch_browser", opened.append)
    monkeypatch.setattr(panel_mod, "make_panel", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bound")))
    rc = run_panel(type("C", (), {"state_dir": "."})(), machine_id="m", port=47321, open_browser=True, try_service=False)
    assert rc == 0 and opened == ["http://127.0.0.1:47321/"]


def test_run_panel_uses_existing_server_if_bind_fails(monkeypatch):
    import src.fleet.panel as panel_mod

    calls = {"n": 0}

    def health(port, timeout=0.4):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(panel_mod, "panel_health_ok", health)
    monkeypatch.setattr(panel_mod, "make_panel", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("busy")))
    opened = []
    monkeypatch.setattr(panel_mod, "launch_browser", opened.append)
    rc = run_panel(type("C", (), {"state_dir": "."})(), machine_id="m", port=9, open_browser=True, try_service=False)
    assert rc == 0 and opened == ["http://127.0.0.1:9/"]


def test_background_panel_does_not_bind_under_pytest(tmp_path, monkeypatch):
    import src.fleet.panel as panel_mod

    monkeypatch.setattr(panel_mod, "make_panel", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bound")))
    start_panel_background(type("C", (), {"state_dir": tmp_path})(), "m")
    monkeypatch.setattr("src.fleet.agent.supervise", lambda make, stop: 0)
    assert agent_main(["--state-dir", str(tmp_path), "run", "--service"]) == 0


def test_background_panel_starts_outside_pytest(tmp_path, monkeypatch):
    import src.fleet.panel as panel_mod

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    started = threading.Event()

    def fake_make(*args, **kwargs):
        started.set()
        raise OSError("busy")

    monkeypatch.setattr(panel_mod, "panel_health_ok", lambda port, timeout=0.4: False)
    monkeypatch.setattr(panel_mod, "make_panel", fake_make)
    start_panel_background(type("C", (), {"state_dir": tmp_path})(), "m")
    assert started.wait(2)


def test_installer_shortcuts_and_finish_page():
    iss = (SETUP / "ChatXAgent.iss").read_text(encoding="utf-8")
    assert iss.startswith("\ufeff")
    assert "{autodesktop}\\舰队节点" in iss
    assert "Open-Panel.vbs" in iss and "fleet-node.ico" in iss and "desktopicon" in iss
    assert "打开舰队节点" in iss and "打开舰队控制台" in iss
    assert "把配对码发给管理员，舰队控制台里能看到同一个码" in iss
    assert "开机后计划任务会自动连接主控" in iss
    assert "47321" in iss and str(PANEL_PORT) == "47321"
    icons = iss.split("[Icons]")[1].split("[Code]")[0]
    assert "Open-Status.cmd" not in icons
    assert "舰队节点" in icons and "舰队控制台" in icons
    cmd = (SETUP / "Open-Status.cmd").read_text(encoding="utf-8")
    assert "Open-Panel.vbs" in cmd and "pause" not in cmd.lower() and "status" not in cmd.lower()
    vbs = (SETUP / "Open-Panel.vbs").read_text(encoding="utf-8")
    assert "--state-dir" in vbs and " ui" in vbs and "Wscript.Shell" in vbs and ", 0, False" in vbs
    ico = (SETUP / "fleet-node.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00"
    deploy = (ENGINE / "docs/FLEET_DEPLOY.md").read_text(encoding="utf-8")
    home = (ENGINE / "domains/fleet_control/web/templates/fleet_home.html").read_text(encoding="utf-8")
    assert "Fleet node status" not in deploy and "Fleet console" not in deploy
    assert "舰队节点" in deploy and "Open-Panel.vbs" in deploy and "舰队控制台" in deploy
    assert "智控节点状态" not in home and "舰队节点" in home
