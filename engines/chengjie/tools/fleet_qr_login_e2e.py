"""Local end-to-end harness for console QR-code login (docs/FLEET_CONSOLE_QR_LOGIN.md).

Everything runs on 127.0.0.1 inside a temp directory; no production controller, no real
instance, no real state dir (never C:\\ProgramData\\ChatX), no live-stream port:

  fake instance  : /api/platforms/{p}/login/start + /login/{id}/status (+ /health, fleet-health)
  controller     : FastAPI + the real fleet_control routes + a temp FleetStore (uvicorn)
  node agent     : the real NodeAgent with a temp state dir, enrolled with a one-time code
  console        : the real fleet_console.html in headless Chromium (Playwright); only
                   base.html is a stub (CSS vars + apiFetch adding the operator token)

    cd engines/chengjie
    python tools/fleet_qr_login_e2e.py                    # authorized after 2 pending polls
    python tools/fleet_qr_login_e2e.py --scenario failed  # instance reports failed
    python tools/fleet_qr_login_e2e.py --keep --shots D:\\fleet-issue-logs\\qr-e2e

Exit 0 = every expectation held; 1 = an expectation failed; 2 = Playwright/Chromium missing.
Prints one JSON summary line at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

ENGINE = Path(__file__).resolve().parents[1]
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from src.fleet.detect import LIVE_STREAM_PORTS  # noqa: E402
from fastapi import Request  # noqa: E402  (module level: the auth deps are annotated with it)


# ── helpers ─────────────────────────────────────────────────────────────────
def free_port() -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in LIVE_STREAM_PORTS:
            return port
    raise RuntimeError("no free non-live port")


def png_data_url(seed: int, size: int = 21, scale: int = 6) -> str:
    """A small black/white PNG (QR-looking pattern) built with zlib only."""
    import base64
    w = h = size * scale
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            cx, cy = x // scale, y // scale
            finder = any(cx - ox in range(7) and cy - oy in range(7) and
                         (cx - ox in (0, 6) or cy - oy in (0, 6) or (2 <= cx - ox <= 4 and 2 <= cy - oy <= 4))
                         for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)))
            in_finder_box = any(0 <= cx - ox < 8 and 0 <= cy - oy < 8 for ox, oy in ((0, 0), (size - 8, 0), (0, size - 8)))
            on = finder if in_finder_box else ((cx * 7 + cy * 13 + seed) % 3 == 0)
            row.append(0 if on else 255)
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


# ── fake instance ───────────────────────────────────────────────────────────
class FakeInstance:
    def __init__(self, scenario: str, pending_polls: int):
        self.scenario = scenario
        self.pending_polls = pending_polls
        self.calls: List[str] = []
        self.status_calls: Dict[str, int] = {}
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj: Dict[str, Any]) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append("GET " + self.path)
                parts = self.path.split("?")[0].strip("/").split("/")
                if self.path.startswith("/health"):
                    return self._send(200, {"ok": True})
                if self.path.startswith("/api/accounts/fleet-health"):
                    return self._send(200, {"ok": True, "total": 1, "lifecycle": {"active": 1}, "accounts": []})
                if len(parts) == 6 and parts[:2] == ["api", "platforms"] and parts[3] == "login" and parts[5] == "status":
                    lid = parts[4]
                    n = outer.status_calls[lid] = outer.status_calls.get(lid, 0) + 1
                    if n <= outer.pending_polls:
                        res = {"ok": True, "status": "pending", "detail": f"waiting ({n})"}
                        if n == 1:
                            res["qr_image"] = png_data_url(seed=7)          # refreshed QR
                        return self._send(200, res)
                    if outer.scenario == "failed":
                        return self._send(200, {"ok": True, "status": "failed", "reason_code": "e2e_failed",
                                                "detail": "instance said no"})
                    return self._send(200, {"ok": True, "status": "authorized", "account_id": f"{parts[2]}-e2e-1"})
                return self._send(404, {"detail": "not found"})

            def do_POST(self):
                outer.calls.append("POST " + self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.strip("/").split("/")
                if len(parts) == 5 and parts[:2] == ["api", "platforms"] and parts[3:] == ["login", "start"]:
                    outer.start_body = body
                    return self._send(200, {"ok": True, "login_id": "L-" + secrets.token_hex(6), "status": "pending",
                                            "mode": "qr", "qr_image": png_data_url(seed=1)})
                return self._send(404, {"detail": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.start_body: Dict[str, Any] = {}

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()


# ── controller ──────────────────────────────────────────────────────────────
STUB_BASE = """<!doctype html><html><head><meta charset="utf-8"><title>{% block title %}{% endblock %}</title>
<style>:root{--card:#fff;--bd:#ddd;--r:8px;--t:#111;--t2:#555;--t3:#888;--input:#f3f3f3;--gs:#e6f7ea;--green:#1a7f37;
--as:#fff4e0;--amber:#9a6700;--rs:#fde8e8;--red:#c00}body{font-family:sans-serif;margin:1rem}</style>
<script>window.apiFetch=function(u,i){i=i||{};i.headers=Object.assign({},i.headers||{},{'Authorization':'Bearer __TOKEN__'});return fetch(u,i);};</script>
{% block head %}{% endblock %}</head><body><h2>{% block page_title %}{% endblock %}</h2>{% block content %}{% endblock %}</body></html>"""


def start_controller(tmp: Path, token: str):
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.templating import Jinja2Templates
    from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

    from domains.fleet_control.web.routes import register_routes
    from src.fleet.store import FleetStore, set_store

    st = FleetStore(tmp / "fleet.db", offline_after_sec=120)
    set_store(st)
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    def api_auth(request: Request):
        if request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="op unauthorized")

    def page_auth(request: Request):
        if request.query_params.get("t") != token:
            raise HTTPException(status_code=401, detail="page unauthorized")

    templates = Jinja2Templates(directory=str(ENGINE / "domains/fleet_control/web/templates"))
    templates.env.loader = ChoiceLoader([
        DictLoader({"base.html": STUB_BASE.replace("__TOKEN__", token)}),
        FileSystemLoader(str(ENGINE / "domains/fleet_control/web/templates")),
    ])
    app = FastAPI()
    ctx = SimpleNamespace(config_manager=SimpleNamespace(config={"fleet_control": {"public_url": base}}),
                          api_auth=api_auth, api_write_factory=lambda perm: api_auth, page_auth=page_auth,
                          templates=templates)
    register_routes(app, ctx)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("controller did not start")
    return SimpleNamespace(store=st, base=base, port=port, server=server, thread=th)


# ── agent ───────────────────────────────────────────────────────────────────
def start_agent(tmp: Path, ctl, inst_url: str):
    from src.fleet.agent import AgentConfig, NodeAgent

    state = tmp / "agent-state" / "fleet"
    cfg = AgentConfig(state)
    cfg.add_instance("wa-e2e", inst_url, domain="conversion")
    agent = NodeAgent(cfg, app_version="e2e")
    code = ctl.store.create_enroll_code(label="qr-e2e", group_name="e2e")["code"]
    agent.enroll(code, controller_url=ctl.base)
    stop = threading.Event()
    errors: List[str] = []

    def loop():
        while not stop.is_set():
            try:
                agent.run_once(wait=1)
            except Exception as e:  # keep going; the summary reports it
                errors.append(repr(e))
                time.sleep(0.5)

    agent.run_once()            # first heartbeat carries the instance list
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return SimpleNamespace(agent=agent, cfg=cfg, stop=stop, thread=th, errors=errors, state=state)


# ── console in Chromium ─────────────────────────────────────────────────────
def drive_console(ctl, token: str, platform: str, expect: str, shots: Path | None, timeout_s: int) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright

    out: Dict[str, Any] = {"qr_srcs": [], "console_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 1000})
        page.on("console", lambda m: out["console_errors"].append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: out["console_errors"].append(str(e)))
        page.goto(f"{ctl.base}/fleet/console?t={token}")
        page.wait_for_selector("#fc-list button[data-k='__login']", timeout=20000)
        page.click("#fc-list button[data-k='__login']")
        page.wait_for_selector("#fc-login", state="visible")
        out["instance_options"] = page.eval_on_selector_all("#fc-login-inst option", "els=>els.map(e=>e.value)")
        page.select_option("#fc-login-inst", "wa-e2e")
        page.fill("#fc-login-platform", platform)
        page.click("#fc-login-go")
        page.wait_for_function("()=>{const i=document.getElementById('fc-login-qr');return i.style.display!=='none'&&i.src.startsWith('data:image/png')}",
                               timeout=timeout_s * 1000)
        out["qr_srcs"].append(page.get_attribute("#fc-login-qr", "src")[:40])
        out["msg_after_qr"] = page.inner_text("#fc-login-msg")
        if shots:
            page.screenshot(path=str(shots / "1-qr.png"), full_page=True)
        page.wait_for_function(f"()=>document.getElementById('fc-login-msg').textContent.startsWith({json.dumps(expect)})",
                               timeout=timeout_s * 1000)
        out["final_msg"] = page.inner_text("#fc-login-msg")
        out["qr_hidden_at_end"] = page.eval_on_selector("#fc-login-qr", "e=>e.style.display==='none'&&!e.getAttribute('src')")
        out["go_enabled_at_end"] = page.eval_on_selector("#fc-login-go", "e=>!e.disabled")
        if shots:
            page.screenshot(path=str(shots / "2-final.png"), full_page=True)
        browser.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=("authorized", "failed"), default="authorized")
    ap.add_argument("--platform", default="whatsapp")
    ap.add_argument("--pending-polls", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=60, help="seconds per wait")
    ap.add_argument("--shots", default="", help="directory for screenshots")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args(argv)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print(json.dumps({"ok": False, "error": "playwright not installed"}))
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="fleet-qr-e2e-"))
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    token = "op-" + secrets.token_hex(16)
    inst = FakeInstance(args.scenario, args.pending_polls).start()
    ctl = start_controller(tmp, token)
    node = start_agent(tmp, ctl, f"http://127.0.0.1:{inst.port}")
    summary: Dict[str, Any] = {"tmp": str(tmp), "controller": ctl.base, "instance_port": inst.port,
                               "scenario": args.scenario}
    checks: Dict[str, bool] = {}
    try:
        expect = "登录成功" if args.scenario == "authorized" else "已结束：failed"
        try:
            ui = drive_console(ctl, token, args.platform, expect, shots, args.timeout)
        except Exception as e:
            summary["ui_error"] = repr(e)[:500]
            ui = {}
        summary["ui"] = ui
        tasks = ctl.store.list_tasks(node_id=node.cfg.node_id, limit=200)
        kinds: Dict[str, int] = {}
        for t in tasks:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
        summary["tasks"] = kinds
        summary["task_statuses"] = sorted({t["status"] for t in tasks})
        summary["instance_calls"] = inst.calls[-12:]
        summary["agent_errors"] = node.errors[:5]
        checks["ui_finished_as_expected"] = bool(ui.get("final_msg", "").startswith(expect))
        checks["qr_shown_as_png"] = bool(ui.get("qr_srcs")) and ui["qr_srcs"][0].startswith("data:image/png;base64,")
        checks["qr_cleared_at_end"] = bool(ui.get("qr_hidden_at_end"))
        checks["instance_offered"] = "wa-e2e" in (ui.get("instance_options") or [])
        checks["one_login_start"] = kinds.get("login_qr") == 1 and sum(c.startswith("POST") for c in inst.calls) == 1
        checks["status_polls_match"] = kinds.get("login_status", 0) == args.pending_polls + 1
        checks["all_tasks_done"] = summary["task_statuses"] == ["done"]
        checks["start_body_whitelisted"] = set(inst.start_body) <= {"account_id", "label", "group", "proxy_id",
                                                                   "use_fingerprint", "phone", "mode"}
        checks["no_live_ports"] = not ({ctl.port, inst.port} & set(LIVE_STREAM_PORTS))
        checks["no_page_errors"] = not ui.get("console_errors")
        checks["agent_ok"] = not node.errors
    finally:
        node.stop.set()
        node.thread.join(timeout=10)
        ctl.server.should_exit = True
        ctl.thread.join(timeout=10)
        inst.stop()
        try:
            ctl.store.close()
        except Exception:
            pass
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    summary["checks"] = checks
    summary["ok"] = bool(checks) and all(checks.values())
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
