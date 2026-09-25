"""本机「舰队节点」页：只绑定 127.0.0.1 的静态页，不代替舰队控制台。

``chatx-agent ui`` 若发现计划任务里的进程已经在听固定端口，就只打开浏览器。
否则在当前进程兜底起一个页面（测试、以及还没装上服务的时候）。
``run`` / ``run --service`` 会在后台挂同一个端口，且不会因为没人看页面而退出。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from .local_status import build_local_status, mask_secrets
from .service import TASK_NAME

logger = logging.getLogger("fleet.panel")

PANEL_PORT = 47321
SERVICE_NAME = "chatx-fleet-panel"
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

Opener = Callable[[Path], None]
StatusFn = Callable[[], dict]

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>舰队节点</title>
<style>
  :root { color-scheme: light; --ink:#1b2038; --muted:#5c6578; --line:#e4e7ee; --bg:#f3f5f8; --card:#fff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; }
  main { max-width:720px; margin:28px auto 48px; padding:0 16px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .lead { color:var(--muted); margin:0 0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin:0 0 12px; }
  .badge { display:inline-block; font-size:20px; font-weight:700; padding:4px 14px; border-radius:999px; }
  .s-pending { background:#fff7ed; color:#c2410c; }
  .s-online { background:#ecfdf3; color:#15803d; }
  .s-offline { background:#f1f5f9; color:#475569; }
  .s-rejected { background:#fef2f2; color:#b91c1c; }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
  button { font:inherit; border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:8px 12px; cursor:pointer; }
  button.primary { background:#1b2038; color:#fff; border-color:#1b2038; }
  button:disabled { opacity:.5; cursor:default; }
  dl { display:grid; grid-template-columns:7.5em 1fr; gap:6px 10px; margin:0; }
  dt { color:var(--muted); }
  dd { margin:0; word-break:break-all; }
  .code { font:16px/1.4 ui-monospace,Consolas,monospace; letter-spacing:.08em; }
  .note { min-height:1.4em; color:var(--muted); margin:8px 0 0; }
  details { margin-top:4px; }
  summary { cursor:pointer; color:var(--muted); }
  pre { white-space:pre-wrap; word-break:break-all; background:#f8fafc; border-radius:8px; padding:10px; font:12px/1.45 ui-monospace,Consolas,monospace; }
  .inline { margin-left:8px; padding:2px 8px; }
</style>
</head>
<body>
<main>
  <h1>舰队节点</h1>
  <p class="lead">本页只管理这台电脑。</p>
  <section class="card">
    <div id="badge" class="badge s-offline">离线</div>
    <p id="code-row" hidden>配对码 <span id="pairing" class="code"></span></p>
    <div class="row">
      <button id="primary" class="primary" type="button">检查连接</button>
    </div>
    <p id="note" class="note"></p>
  </section>
  <section class="card">
    <dl id="summary"></dl>
  </section>
  <div class="row">
    <button id="refresh" type="button">刷新状态</button>
    <button id="diag" type="button">复制诊断</button>
    <button id="logs" type="button">打开日志</button>
  </div>
  <details>
    <summary>详情</summary>
    <dl id="meta"></dl>
    <pre id="raw"></pre>
  </details>
</main>
<script>
(function () {
  var LABELS = {pending: "待批准", online: "在线", offline: "离线", rejected: "已拒绝"};
  var state = null;
  var busy = false;
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function setText(id, text) { $(id).textContent = text == null ? "" : String(text); }
  function copyText(text) {
    var value = text == null ? "" : String(text);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).catch(function () { fallbackCopy(value); });
      return;
    }
    fallbackCopy(value);
  }
  function fallbackCopy(value) {
    var ta = document.createElement("textarea");
    ta.value = value;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }
  function note(text) { setText("note", text || ""); }
  function addRow(parent, name, value, extra) {
    parent.appendChild(el("dt", "", name));
    var dd = el("dd", "", value);
    if (extra) dd.appendChild(extra);
    parent.appendChild(dd);
  }
  function primaryOf(s) {
    if (s.ui_state === "pending") return {act: "copy-code", label: "复制配对码"};
    if (s.ui_state === "online") return {act: "console", label: "打开舰队控制台"};
    if (s.ui_state === "rejected") return {act: "logs", label: "打开日志"};
    return {act: "check", label: "检查连接"};
  }
  function render(s) {
    state = s || {};
    var ui = state.ui_state || "offline";
    var badge = $("badge");
    badge.className = "badge s-" + (LABELS[ui] ? ui : "offline");
    badge.textContent = state.ui_label || LABELS[ui] || "离线";
    var codeRow = $("code-row");
    if (ui === "pending" && state.pairing_code) {
      codeRow.hidden = false;
      setText("pairing", state.pairing_code);
    } else {
      codeRow.hidden = true;
      setText("pairing", "");
    }
    var action = primaryOf(state);
    var primary = $("primary");
    primary.textContent = action.label;
    primary.onclick = function () { doAct(action.act); };
    var summary = $("summary");
    clear(summary);
    addRow(summary, "计算机名", state.host_name || "");
    addRow(summary, "版本", state.agent_version || "");
    var copyUrl = el("button", "inline", "复制");
    copyUrl.type = "button";
    copyUrl.onclick = function () { copyText(state.controller_url || ""); note("已复制主控地址"); };
    addRow(summary, "主控地址", state.controller_url || "未配置", copyUrl);
    addRow(summary, "主控", state.controller_reachable ? "可达" : "不可达");
    addRow(summary, "最近心跳", state.last_heartbeat_at || "尚无记录");
    addRow(summary, "计划任务", state.task_label || "");
    var meta = $("meta");
    clear(meta);
    addRow(meta, "machine_id", state.machine_id || "");
    addRow(meta, "proto", state.proto_version == null ? "" : String(state.proto_version));
    addRow(meta, "enrollment", state.enrollment || "");
    $("raw").textContent = JSON.stringify(state, null, 2);
  }
  function refresh() {
    if (busy) return;
    busy = true;
    fetch("/api/local/status", {cache: "no-store"})
      .then(function (r) { if (!r.ok) throw new Error("bad"); return r.json(); })
      .then(function (s) { render(s); })
      .catch(function () { note("暂时读不到本机状态"); })
      .then(function () { busy = false; });
  }
  function openLogs() {
    fetch("/api/local/open-logs", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, body: d}; }); })
      .then(function (res) {
        var path = (res.body && res.body.path) || "";
        if (res.ok && res.body && res.body.ok) { note(path ? ("已打开 " + path) : "已打开日志文件夹"); return; }
        if (path) copyText(path);
        note(path ? "未能打开文件夹，路径已复制" : "未能打开日志文件夹");
      })
      .catch(function () { note("未能打开日志文件夹"); });
  }
  function doAct(act) {
    if (!state) return;
    if (act === "copy-code") {
      if (!state.pairing_code) { note("还没有配对码"); return; }
      copyText(state.pairing_code);
      note("已复制配对码");
      return;
    }
    if (act === "console") {
      if (!state.console_url) { note("还没有主控地址"); return; }
      window.open(state.console_url, "_blank", "noopener");
      return;
    }
    if (act === "logs") { openLogs(); return; }
    note("");
    refresh();
  }
  $("refresh").onclick = function () { note(""); refresh(); };
  $("diag").onclick = function () {
    if (!state) return;
    copyText(JSON.stringify(state, null, 2));
    note("已复制诊断");
  };
  $("logs").onclick = openLogs;
  refresh();
  setInterval(refresh, 8000);
})();
</script>
</body>
</html>
"""


def request_allowed(client_ip: str, host_header: str) -> bool:
    """只接受回环来源，并且 Host 必须是本机名字。挡住局域网和恶意 Host。"""
    ip = str(client_ip or "").split("%", 1)[0].strip()
    if ip not in {"127.0.0.1", "::1"}:
        return False
    host = str(host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        end = host.find("]")
        name = host[1:end] if end > 1 else ""
    else:
        name = host.split(":", 1)[0]
    return name in _ALLOWED_HOSTS


def safe_log_dir(state_dir: Path) -> Path:
    root = Path(state_dir).resolve()
    logs = (root / "logs").resolve()
    if logs != root and root not in logs.parents:
        raise ValueError("log directory is outside the state directory")
    return logs


def open_folder(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    subprocess.Popen(
        ["xdg-open", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_browser(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    webbrowser.open(url)


def panel_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/"


def panel_health_ok(port: int, *, timeout: float = 0.4) -> bool:
    url = f"http://127.0.0.1:{int(port)}/api/local/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if int(getattr(resp, "status", 0) or 0) != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("service") == SERVICE_NAME


def kick_service_panel(port: int, *, wait_sec: float = 8.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    """让计划任务里的进程把页面拉起来。非 Windows 或任务不存在时马上返回。"""
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", TASK_NAME],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    deadline = time.time() + float(wait_sec)
    while time.time() < deadline:
        if panel_health_ok(port):
            return True
        sleep(0.4)
    return False


class PanelServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, server_address, handler) -> None:
        host = server_address[0]
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("fleet panel binds to loopback only")
        super().__init__(server_address, handler)
        self.last_hit = time.time()

    def server_bind(self) -> None:
        host = self.server_address[0]
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("fleet panel binds to loopback only")
        super().server_bind()


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, request, client_address, server, *, state_dir: Path, machine_id: str,
                 status_fn: Optional[StatusFn], opener: Opener) -> None:
        self.state_dir = Path(state_dir)
        self.machine_id = str(machine_id or "")
        self.status_fn = status_fn
        self.opener = opener
        super().__init__(request, client_address, server)

    def version_string(self) -> str:
        return SERVICE_NAME

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _touch(self) -> None:
        server = self.server
        if isinstance(server, PanelServer):
            server.last_hit = time.time()

    def _client_ok(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return request_allowed(ip, self.headers.get("Host", ""))

    def _send(self, code: int, payload: Any, *, content_type: str) -> None:
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, payload, content_type="application/json; charset=utf-8")

    def _forbid(self) -> None:
        self._json(403, {"ok": False})

    def do_GET(self) -> None:  # noqa: N802
        self._touch()
        if not self._client_ok():
            self._forbid()
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE_HTML, content_type="text/html; charset=utf-8")
            return
        if path == "/api/local/health":
            self._json(200, {"ok": True, "service": SERVICE_NAME, "bind": "127.0.0.1"})
            return
        if path == "/api/local/status":
            self._json(200, self._status())
            return
        self._json(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        self._touch()
        if not self._client_ok():
            self._forbid()
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            self._json(413, {"ok": False})
            return
        if length:
            self.rfile.read(length)
        if path != "/api/local/open-logs":
            self._json(404, {"ok": False})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"ok": False, "detail": "content-type"})
            return
        self._open_logs()

    def _status(self) -> dict:
        if self.status_fn is not None:
            snap = self.status_fn()
        else:
            from .agent import AgentConfig
            try:
                cfg = AgentConfig(self.state_dir)
                snap = build_local_status(cfg, machine_id=self.machine_id)
            except Exception:
                logger.debug("[fleet] local status failed", exc_info=True)
                snap = {
                    "ui_state": "offline", "ui_label": "离线", "enrollment": "none",
                    "enrolled": False, "controller_reachable": False, "task_running": False,
                    "task_label": "未能查询", "last_heartbeat_at": None, "machine_id": self.machine_id,
                    "log_dir": str(self.state_dir / "logs"),
                }
        if not isinstance(snap, dict):
            snap = {"ui_state": "offline", "ui_label": "离线"}
        return mask_secrets(snap)

    def _open_logs(self) -> None:
        try:
            logs = safe_log_dir(self.state_dir)
            logs.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._json(500, {"ok": False, "detail": "未能打开日志文件夹"})
            return
        try:
            self.opener(logs)
        except Exception:
            logger.debug("[fleet] open logs failed", exc_info=True)
            self._json(200, {"ok": False, "path": str(logs), "detail": "未能打开日志文件夹"})
            return
        self._json(200, {"ok": True, "path": str(logs)})


def make_panel(
    state_dir: Path,
    machine_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = PANEL_PORT,
    status_fn: Optional[StatusFn] = None,
    opener: Optional[Opener] = None,
) -> PanelServer:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("fleet panel binds to loopback only")
    root = Path(state_dir)
    mid = str(machine_id or "")
    open_logs = opener or open_folder

    def handler(request, client_address, server):
        PanelHandler(
            request, client_address, server,
            state_dir=root, machine_id=mid, status_fn=status_fn, opener=open_logs,
        )

    return PanelServer((host, int(port)), handler)


def _attach_or_none(port: int, *, open_browser: bool) -> bool:
    if not panel_health_ok(port):
        return False
    if open_browser:
        launch_browser(panel_url(port))
    return True


def run_panel(
    cfg: Any,
    *,
    machine_id: str,
    port: int,
    open_browser: bool = True,
    idle_sec: float = 1800,
    try_service: bool = True,
) -> int:
    """打开本机页。已经有人在听就只开浏览器；否则自己绑端口。

    ``idle_sec`` > 0 时，页面停了这么久没再来拉状态就退出（快捷方式兜底进程）。
    服务进程应走 ``start_panel_background``，那个不会闲置退出。
    """
    port = int(port or 0) or PANEL_PORT
    if _attach_or_none(port, open_browser=open_browser):
        return 0
    if try_service and kick_service_panel(port) and _attach_or_none(port, open_browser=open_browser):
        return 0
    try:
        httpd = make_panel(Path(cfg.state_dir), machine_id, port=port)
    except OSError:
        if _attach_or_none(port, open_browser=open_browser):
            return 0
        logger.warning("[fleet] panel port %s is busy", port)
        return 1
    actual = int(httpd.server_address[1])
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
        name="fleet-panel", daemon=True,
    )
    thread.start()
    if open_browser:
        launch_browser(panel_url(actual))
    if idle_sec and idle_sec > 0:
        try:
            while thread.is_alive():
                time.sleep(0.5)
                if time.time() - httpd.last_hit >= float(idle_sec):
                    break
        finally:
            httpd.shutdown()
            httpd.server_close()
        return 0
    try:
        thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd.server_close()
    return 0


def start_panel_background(cfg: Any, machine_id: str) -> None:
    """``run`` 时在后台听固定端口。测试进程里不绑定，避免占端口。不会闲置退出。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    def _go() -> None:
        try:
            if panel_health_ok(PANEL_PORT):
                return
            httpd = make_panel(Path(cfg.state_dir), machine_id, port=PANEL_PORT)
        except OSError:
            logger.info("[fleet] panel port %s already in use", PANEL_PORT)
            return
        except Exception:
            logger.debug("[fleet] panel did not start", exc_info=True)
            return
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception:
            logger.debug("[fleet] panel stopped", exc_info=True)

    threading.Thread(target=_go, name="fleet-panel", daemon=True).start()
