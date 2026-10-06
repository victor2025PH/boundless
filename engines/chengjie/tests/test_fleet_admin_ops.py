"""操作端 CLI（src/fleet/admin.py）的运维子命令：caps / remote / phone-op / task-show / phone-tasks / phones /
update / revoke，以及 env 文件 + 操作员账号登录（会话 + CSRF）。凭据从不出现在输出里。"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.fleet import admin as admin_mod

PW = "pw-Very-Secret-938"
TOK = "tok-Very-Secret-412"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (admin_mod.ENV_CONTROLLER, admin_mod.ENV_TOKEN, admin_mod.ENV_FILE,
              admin_mod.ENV_OP_USER, admin_mod.ENV_OP_PASSWORD):
        monkeypatch.delenv(k, raising=False)


def _envfile(tmp_path, body: str):
    p = tmp_path / "launch.env"
    p.write_text("\ufeff" + body, encoding="utf-8")
    return str(p)


def test_read_env_file_parses_launch_env_style(tmp_path):
    p = _envfile(tmp_path, "# c\nexport FLEET_OPERATOR_USER = 'ops'\nFLEET_OPERATOR_PASSWORD=\"a=b\"\n\nNOEQ\nX=1\n")
    d = admin_mod.read_env_file(p)
    assert d == {"FLEET_OPERATOR_USER": "ops", "FLEET_OPERATOR_PASSWORD": "a=b", "X": "1"}
    assert admin_mod.read_env_file(str(tmp_path / "missing.env")) == {}


def test_scrub_drops_credentials_and_screenshots():
    out = admin_mod.scrub({"node_key": "k", "token": "t", "x": {"password": "p", "ok": 1},
                           "result": {"png_b64": PNG, "w": 1}, "rows": [{"secret_hash": "h", "a": 2}]})
    assert out == {"x": {"ok": 1}, "result": {"png_b64_len": len(PNG), "png_ok": True, "w": 1}, "rows": [{"a": 2}]}


def test_build_admin_prefers_token_then_operator_login(tmp_path, monkeypatch):
    assert admin_mod.build_admin("", "", "") is None
    f = _envfile(tmp_path, f"FLEET_CONTROLLER_URL=https://ctl.test/fleet/\nFLEET_OPERATOR_TOKEN={TOK}\n")
    adm = admin_mod.build_admin("", "", f)
    assert adm.base == "https://ctl.test/fleet" and adm.token == TOK
    f2 = _envfile(tmp_path, f"FLEET_OPERATOR_USER=ops\nFLEET_OPERATOR_PASSWORD={PW}\n")
    assert admin_mod.build_admin("", "", f2) is None              # 没有主控地址
    adm2 = admin_mod.build_admin("https://ctl.test/fleet", "", f2)
    assert isinstance(adm2.http, admin_mod.SessionHttp) and adm2.token == ""
    assert PW not in repr(adm2.http.__dict__.get("base")) and adm2.http.logged_in is False
    monkeypatch.setenv(admin_mod.ENV_OP_USER, "envops")
    monkeypatch.setenv(admin_mod.ENV_OP_PASSWORD, "envpw")
    adm3 = admin_mod.build_admin("https://ctl.test/fleet", "", "")
    assert isinstance(adm3.http, admin_mod.SessionHttp)


class _Fake:
    def __init__(self, nodes: Optional[List[Dict[str, Any]]] = None, tasks: Optional[List[Dict[str, Any]]] = None,
                 task: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Tuple[str, str, Any, Any]] = []
        self.nodes = nodes or []
        self.tasks = tasks or []
        self.task = task or {}

    def __call__(self, method, url, body, headers=None):
        self.calls.append((method, url, body, headers))
        path = url.split("https://ctl.test/fleet", 1)[1]
        if method == "GET" and path.startswith("/api/fleet/nodes?"):
            return {"ok": True, "nodes": self.nodes}
        if method == "GET" and path.startswith("/api/fleet/pending"):
            return {"ok": True, "pending": [{"request_id": "r1", "machine_id": "mach-123", "pairing_code": "ab12"}]}
        if method == "GET" and path.startswith("/api/fleet/tasks?"):
            return {"ok": True, "tasks": self.tasks}
        if method == "GET" and path.startswith("/api/fleet/tasks/"):
            return {"ok": True, "task": self.task}
        if method == "POST" and path.startswith("/api/fleet/nodes/") and path.count("/") == 4:
            n = dict(self.nodes[0]) if self.nodes else {}
            if body.get("remote_ops_enabled"):
                n.update({"remote_ops_enabled": True, "remote_ops_remaining_sec": 60 * body.get("remote_ops_minutes", 30),
                          "remote_ops_expires_at": 1_800_000_000, "remote_ops_enabled_by": "ops"})
            else:
                n.update({"remote_ops_enabled": False})
            n.update({k: v for k, v in body.items() if k in ("label", "group_name")})
            return {"ok": True, "node": n}
        if method == "POST" and "/phones/" in path:
            return {"ok": True, "task": {"task_id": "t_ph", "status": "queued"}}
        return {"ok": True}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def _run(monkeypatch, fake: _Fake, argv: List[str]) -> int:
    monkeypatch.setattr(admin_mod, "build_admin",
                        lambda c, t, f="": admin_mod.Admin("https://ctl.test/fleet", "T", http=fake))
    return admin_mod.main(argv)


N1 = {"node_id": "n1", "machine_id": "mach-1", "host_name": "PC-1", "state": "offline", "label": "一号",
      "caps": ["phone_ops_v1"], "remote_ops_enabled": False}


def test_admin_methods_urls_bodies_and_actor_header():
    fake = _Fake(nodes=[N1])
    adm = admin_mod.Admin("https://ctl.test/fleet/", "T", http=fake)
    adm.set_remote_ops("n1", True, minutes=45)
    adm.set_remote_ops("n1", False, minutes=45)
    adm.phone_op("n1", "AB/CD 01", "tap", payload={"x": 1, "y": 2}, actor="rollout")
    adm.phone_op("n1", "S1", "screenshot")
    adm.get_task("t1")
    adm.update_node("n1", label="二号")
    adm.revoke("n1")
    base = "https://ctl.test/fleet/api/fleet"
    assert fake.calls == [
        ("POST", f"{base}/nodes/n1", {"remote_ops_enabled": True, "remote_ops_minutes": 45}, None),
        ("POST", f"{base}/nodes/n1", {"remote_ops_enabled": False}, None),
        ("POST", f"{base}/nodes/n1/phones/AB%2FCD%2001/tap", {"x": 1, "y": 2}, {"X-Fleet-Actor": "rollout"}),
        ("POST", f"{base}/nodes/n1/phones/S1/screenshot", {}, None),
        ("GET", f"{base}/tasks/t1", None, None),
        ("POST", f"{base}/nodes/n1", {"label": "二号"}, None),
        ("POST", f"{base}/nodes/n1/revoke", {}, None),
    ]


def test_old_three_arg_http_hooks_still_work():
    calls = []
    adm = admin_mod.Admin("https://ctl.test/fleet", "T", http=lambda m, u, b: calls.append((m, u, b)) or {"ok": True})
    adm.set_remote_ops("n1", True)
    assert calls == [("POST", "https://ctl.test/fleet/api/fleet/nodes/n1", {"remote_ops_enabled": True})]


def test_remote_cli_checks_machine_and_minutes(monkeypatch, capsys):
    fake = _Fake(nodes=[N1])
    assert _run(monkeypatch, fake, ["remote", "n1", "on", "--minutes", "300"]) == 2
    assert fake.calls == []
    with pytest.raises(SystemExit):
        _run(monkeypatch, fake, ["remote", "n1", "on", "--machine-id", "other"])
    assert fake.posts() == []
    assert _run(monkeypatch, fake, ["remote", "n1", "on", "--minutes", "20", "--machine-id", "mach-1"]) == 0
    assert fake.posts()[-1][2] == {"remote_ops_enabled": True, "remote_ops_minutes": 20}
    out = capsys.readouterr().out
    assert "remote_ops=on(剩20分 by ops)" in out and "到期" in out and "UTC+8" in out
    assert _run(monkeypatch, fake, ["remote", "n1", "off"]) == 0
    assert fake.posts()[-1][2] == {"remote_ops_enabled": False}
    assert "remote_ops=off" in capsys.readouterr().out


def test_caps_and_phones_output(monkeypatch, capsys):
    nodes = [dict(N1, state="online", remote_ops_enabled=True, remote_ops_remaining_sec=61, remote_ops_enabled_by="ops",
                  phones=[{"serial": "S1", "state": "device"}]),
             {"node_id": "n2", "state": "offline", "last_heartbeat": {"phones_error": "adb_missing"}}]
    fake = _Fake(nodes=nodes)
    assert _run(monkeypatch, fake, ["caps"]) == 0
    out = capsys.readouterr().out
    assert "caps=phone_ops_v1" in out and "remote_ops=on(剩2分 by ops)" in out and "remote_ops=off" in out
    assert _run(monkeypatch, fake, ["phones"]) == 0
    out = capsys.readouterr().out
    assert "phones=1 S1:device" in out and "error=adb_missing" in out


def test_phone_op_task_show_and_phone_tasks(monkeypatch, capsys, tmp_path):
    task = {"task_id": "t_ph", "node_id": "n1", "kind": "phone_screenshot", "status": "done", "created_by": "ops via cli",
            "result": {"png_b64": PNG, "w": 720, "node_key": "nope"}, "acked_at": 1_800_000_000}
    fake = _Fake(nodes=[N1], task=task, tasks=[
        {"task_id": "t_ph", "kind": "phone_screenshot", "status": "done", "target": {"serial": "S1"}, "created_by": "x"},
        {"task_id": "t_pg", "kind": "ping", "status": "done"}])
    assert _run(monkeypatch, fake, ["phone-op", "n1", "S1", "tap", "--payload", '{"x":1,"y":2}']) == 0
    assert fake.posts()[-1][3] == {"X-Fleet-Actor": "cli"}
    assert "task_id=t_ph status=queued" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _run(monkeypatch, fake, ["phone-op", "n1", "S1", "tap", "--payload", "[1]"])
    shot = tmp_path / "s.png"
    assert _run(monkeypatch, fake, ["task-show", "t_ph", "--save-png", str(shot)]) == 0
    assert shot.read_bytes().startswith(b"\x89PNG")
    out = capsys.readouterr().out
    row = json.loads(out.strip().splitlines()[-1])
    assert row["result"] == {"png_b64_len": len(PNG), "png_ok": True, "w": 720}
    assert "UTC+8" in row["acked_at"] and PNG not in out and "nope" not in out
    assert _run(monkeypatch, fake, ["phone-tasks", "n1"]) == 0
    out = capsys.readouterr().out
    assert "phone_screenshot" in out and "serial=S1" in out and "ping" not in out


def test_update_revoke_and_approve_guards(monkeypatch, capsys):
    fake = _Fake(nodes=[N1])
    assert _run(monkeypatch, fake, ["update", "n1", "--machine-id", "mach-1"]) == 2
    assert _run(monkeypatch, fake, ["update", "n1", "--machine-id", "mach-1", "--group", "机房A"]) == 0
    assert fake.posts()[-1][2] == {"group_name": "机房A"}
    assert _run(monkeypatch, fake, ["revoke", "n1", "--machine-id", "mach-1", "--host", "PC-X"]) == 1
    fake.nodes = [dict(N1, state="online")]
    assert _run(monkeypatch, fake, ["revoke", "n1", "--machine-id", "mach-1", "--host", "PC-1"]) == 1
    fake.nodes = [N1]
    n_posts = len(fake.posts())
    assert _run(monkeypatch, fake, ["revoke", "n1", "--machine-id", "mach-1", "--host", "PC-1"]) == 0
    assert fake.posts()[-1][1].endswith("/api/fleet/nodes/n1/revoke") and len(fake.posts()) == n_posts + 1
    assert _run(monkeypatch, fake, ["approve", "r1", "--expect-machine", "zzz"]) == 1
    assert _run(monkeypatch, fake, ["approve", "r1", "--expect-pairing", "XX99"]) == 1
    assert _run(monkeypatch, fake, ["approve", "rX", "--expect-pairing", "AB12"]) == 1
    assert not any(c[1].endswith("/approve") for c in fake.posts())
    assert _run(monkeypatch, fake, ["approve", "r1", "--expect-machine", "mach-", "--expect-pairing", "AB12"]) == 0
    assert fake.posts()[-1][1].endswith("/api/fleet/pending/r1/approve")


def test_main_without_credentials_explains_env_file(capsys):
    assert admin_mod.main(["caps"]) == 2
    err = capsys.readouterr().err
    assert admin_mod.ENV_FILE in err and admin_mod.ENV_OP_USER in err


# ── 真 HTTP：操作员登录 → session cookie → 写请求带 X-CSRF-Token ─────────────────
class _Ctl(BaseHTTPRequestHandler):
    seen: List[Dict[str, Any]] = []

    def log_message(self, *a):  # 测试里不打访问日志
        pass

    def _reply(self, code: int, obj: Any, cookies: Tuple[str, ...] = (), location: str = "") -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.seen.append({"m": "POST", "p": self.path, "cookie": self.headers.get("Cookie") or "",
                          "csrf": self.headers.get("X-CSRF-Token") or "", "actor": self.headers.get("X-Fleet-Actor"),
                          "body": body})
        if self.path == "/fleet/login":
            if f"password={PW.replace('-', '-')}" in body:
                return self._reply(303, {}, ("session=S123; Path=/",), "/fleet/")
            return self._reply(200, {"login": "page"})
        if "session=S123" not in (self.headers.get("Cookie") or "") or self.headers.get("X-CSRF-Token") != "C456":
            return self._reply(403, {"detail": "csrf"})
        return self._reply(200, {"ok": True, "node": {"node_id": "n1", "remote_ops_enabled": False}})

    def do_GET(self):
        self.seen.append({"m": "GET", "p": self.path, "cookie": self.headers.get("Cookie") or ""})
        if "session=S123" not in (self.headers.get("Cookie") or ""):
            return self._reply(401, {"detail": "login"})
        return self._reply(200, {"ok": True, "pending": [], "nodes": [N1]}, ("csrf_token=C456; Path=/",))


@pytest.fixture()
def ctl():
    _Ctl.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ctl)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/fleet"
    finally:
        srv.shutdown()
        srv.server_close()


def test_session_login_and_csrf_against_http_server(ctl, tmp_path, capsys):
    f = _envfile(tmp_path, f"CHATX_FLEET_CONTROLLER={ctl}\nFLEET_OPERATOR_USER=ops\nFLEET_OPERATOR_PASSWORD={PW}\n")
    assert admin_mod.main(["--env-file", f, "remote", "n1", "off"]) == 0
    io = capsys.readouterr()
    assert "REMOTE n1 -> remote_ops=off" in io.out and PW not in io.out + io.err
    paths = [(s["m"], s["p"]) for s in _Ctl.seen]
    assert paths[0] == ("POST", "/fleet/login")
    write = [s for s in _Ctl.seen if s["m"] == "POST" and s["p"] == "/fleet/api/fleet/nodes/n1"]
    assert len(write) == 1 and write[0]["csrf"] == "C456" and "session=S123" in write[0]["cookie"]
    assert json.loads(write[0]["body"]) == {"remote_ops_enabled": False}


def test_session_login_failure_never_echoes_password(ctl, tmp_path, capsys):
    f = _envfile(tmp_path, f"CHATX_FLEET_CONTROLLER={ctl}\nFLEET_OPERATOR_USER=ops\nFLEET_OPERATOR_PASSWORD=wrong-{PW}\n")
    with pytest.raises(SystemExit) as ei:
        admin_mod.main(["--env-file", f, "caps"])
    msg = str(ei.value)
    assert "登录失败" in msg and PW not in msg and "wrong" not in msg
    io = capsys.readouterr()
    assert PW not in io.out + io.err
