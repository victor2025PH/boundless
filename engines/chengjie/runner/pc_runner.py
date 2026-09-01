# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」服务本体（实施91 手②）。

在受控机上常驻的独立进程：小智后端经 ``src/assistant/runner_client.py``（Bearer
token）调用 ``GET /health`` 与 ``POST /call {tool, args}``，本进程用 Win32（ctypes）
枚举窗口 + uiautomation 读控件树 + 键鼠 + subprocess 真正操作这台 Windows。
**独立可打包**：仅依赖标准库（Win32 走 ctypes）+ 可选 ``uiautomation`` / ``Pillow``，
可整目录拷到目标机 ``python pc_runner.py`` 直接跑（不 import src）。

⚠ 本文件被并行线（实施91）与其消费方（tests/test_pc_actions.py、
tests/test_pc_runner_preflight.py、tools/pc_runner_preflight.py、
tools/pc_runner_dryrun.py、src/assistant/runner_client.py）依赖，**契约由它们钉死**：
``VERSION`` / ``READONLY_TOOLS`` / ``ACTION_TOOLS`` / ``TREE_MAX_NODES`` /
``win32_available()`` / ``uia_available()`` / ``probe_desktop()`` / ``dispatch(tool, args)`` /
``/health`` 字段（win32_available/uia_available/actions_enabled/shell_enabled/actions_frozen）。

安全（docs/实施88 §2.5 全行业红线 + 实施91 §4）——**逐级 env 开闸**（与 tools/
pc_runner_preflight 五级一致），而非把全能力一次放开：
- **只读侦察永远可用**（list_windows/read_tree/screenshot）；
- **动作工具**（launch/focus/click/type/keys/launch_path）需 ``PC_RUNNER_ACTIONS=1``；
- **命令执行**（run_command，最高危）在动作基础上再需 ``PC_RUNNER_SHELL=1``；
- **冻结急停** ``actions_frozen``（PC_RUNNER_FROZEN=1 或冻结文件在）→ 所有动作工具立即拒；
- **token 鉴权**：HTTP 层无有效 Bearer 一律 401，只监听内网；
- **审计**：``PC_RUNNER_AUDIT`` 配了就每次调用落 JSONL（谁在这台机做了什么）；
- **白名单启动**：launch_app 只吃 ``PC_RUNNER_APPS`` 登记的 app_id→路径，任意路径走
  launch_path（高危动作档）；
- **每操作超时**：UIA 操作走带超时的工作执行，无交互桌面/卡死回 ``uia_timeout`` 不挂死；
- **Administrator**：枚举/操作需提权，以 Administrator 运行是部署选择。

门禁 ``tests/test_pc_runner.py``（纯逻辑 + mock handler，非 Windows 也跑）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("pc_runner")

VERSION = "2026-08-31"

# ── 工具集（契约：test_pc_actions 断言 pc_inspect.tools ⊆ READONLY_TOOLS 且
#    ∩ ACTION_TOOLS == ∅；launch_app/focus_window/run_command ∈ ACTION_TOOLS）──
READONLY_TOOLS: Tuple[str, ...] = ("list_windows", "read_tree", "screenshot")
ACTION_TOOLS: Tuple[str, ...] = (
    "launch_app", "focus_window", "launch_path", "click_control", "click_point",
    "type_text", "send_keys", "run_command",
)
# 最高危（在 PC_RUNNER_ACTIONS 之上再需 PC_RUNNER_SHELL）
SHELL_TOOLS: Tuple[str, ...] = ("run_command",)

TREE_MAX_DEPTH = 6
TREE_MAX_NODES = 400
UIA_OP_TIMEOUT = 15.0

# 可选依赖（缺失优雅降级；模块仍可 import + 单测）
try:  # pragma: no cover - 环境相关
    import uiautomation as _uia  # type: ignore
    _UIA_OK = True
except Exception:  # pragma: no cover
    _uia = None
    _UIA_OK = False


# ── 能力探测 ─────────────────────────────────────────────────────────────
def win32_available() -> bool:
    """能否用 Win32 枚举窗口/截图（Windows + ctypes.windll 可达）。"""
    if os.name != "nt":
        return False
    try:  # pragma: no cover - 环境相关
        import ctypes
        return bool(ctypes.windll.user32)
    except Exception:
        return False


def uia_available() -> bool:
    return _UIA_OK


def is_elevated() -> bool:
    try:  # pragma: no cover - 环境相关
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def probe_desktop() -> bool:
    """当前会话是否有活动桌面（能枚举到 ≥1 个可见窗口）。无头/服务上下文 → False。"""
    if not win32_available():
        return False
    try:  # pragma: no cover - 需桌面
        return len(_enum_windows()) > 0
    except Exception:
        return False


# ── env 开闸（逐级灰度；tools/pc_runner_preflight 五级判据同源）──────────────
def actions_enabled() -> bool:
    return os.environ.get("PC_RUNNER_ACTIONS", "") == "1"


def shell_enabled() -> bool:
    return os.environ.get("PC_RUNNER_SHELL", "") == "1"


def actions_frozen() -> bool:
    """急停（kill-switch，presence-based，与 start_runner.ps1 同契约）：``PC_RUNNER_KILL``
    指向的文件存在即冻结全部动作工具；或 ``PC_RUNNER_FROZEN=1``。启动脚本会先清陈旧 kill 文件。"""
    if os.environ.get("PC_RUNNER_FROZEN", "") == "1":
        return True
    kill = os.environ.get("PC_RUNNER_KILL", "")
    if not kill:
        return False
    try:
        return os.path.exists(kill)
    except Exception:
        return False


def app_whitelist() -> Dict[str, str]:
    """PC_RUNNER_APPS='notepad=C:\\Windows\\notepad.exe;calc=...' → {app_id: path}。"""
    wl: Dict[str, str] = {}
    for pair in str(os.environ.get("PC_RUNNER_APPS", "")).split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() and v.strip():
                wl[k.strip()] = v.strip()
    return wl


def tier_of(tool: str) -> str:
    if tool in READONLY_TOOLS:
        return "readonly"
    if tool in SHELL_TOOLS:
        return "shell"
    if tool in ACTION_TOOLS:
        return "action"
    return "unknown"


# ── 每操作超时执行（无交互桌面/卡死 → uia_timeout，绝不挂死）──────────────────
def _run_with_timeout(fn: Callable[[], Any], timeout: float = UIA_OP_TIMEOUT):
    import threading
    box: Dict[str, Any] = {}

    def _target():
        try:
            box["r"] = fn()
        except Exception as ex:  # noqa: BLE001
            box["e"] = ex

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, "uia_timeout"
    if "e" in box:
        return None, box["e"]
    return box.get("r"), None


# ── Win32 窗口枚举（ctypes，无 pywin32 依赖）──────────────────────────────
def _enum_windows() -> List[Dict[str, Any]]:  # pragma: no cover - 需桌面
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    out: List[Dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if title:
            out.append({"title": title, "hwnd": int(hwnd),
                        "foreground": int(hwnd) == int(fg)})
        return True

    user32.EnumWindows(_cb, 0)
    return out


# ── OS 工具处理器（handler(args) -> dict）；测试用 mock 覆盖 ────────────────
def _need(err: str) -> Dict[str, Any]:
    return {"ok": False, "error": err}


def _h_list_windows(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 需桌面
    if not win32_available():
        return _need("win32_unavailable")
    res, err = _run_with_timeout(_enum_windows, 5.0)
    if err:
        return {"ok": False, "error": err if isinstance(err, str) else "list_failed"}
    return {"ok": True, "windows": (res or [])[:100]}


def _walk(ctrl: Any, depth: int, nodes: List[Dict[str, Any]]) -> None:  # pragma: no cover
    if depth > TREE_MAX_DEPTH or len(nodes) >= TREE_MAX_NODES:
        return
    for c in ctrl.GetChildren():
        try:
            nodes.append({"role": c.ControlTypeName, "name": (c.Name or "")[:120],
                          "automationId": getattr(c, "AutomationId", "") or "",
                          "depth": depth})
        except Exception:
            continue
        if len(nodes) >= TREE_MAX_NODES:
            return
        _walk(c, depth + 1, nodes)


def _h_read_tree(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 需桌面
    if not _UIA_OK:
        return _need("uia_unavailable")
    title = str(args.get("window") or "")

    def _do():
        win = (_uia.WindowControl(searchDepth=1, Name=title) if title
               else _uia.GetForegroundControl())
        if not win or not win.Exists(0.5):
            return {"ok": False, "error": "window_not_found", "detail": title}
        nodes: List[Dict[str, Any]] = []
        _walk(win, 0, nodes)
        return {"ok": True, "window": win.Name,
                "tree": {"count": len(nodes), "nodes": nodes,
                         "truncated": len(nodes) >= TREE_MAX_NODES}}

    res, err = _run_with_timeout(_do)
    if err:
        return {"ok": False, "error": err if isinstance(err, str) else "read_failed"}
    return res


def _h_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 需桌面
    """截屏落盘 + magic bytes 验证（媒体产物纪律：先验 PNG 头才报 ok）。"""
    try:
        from PIL import ImageGrab
    except Exception:
        return _need("pillow_unavailable")
    shots_dir = os.environ.get("PC_RUNNER_SHOTS") or os.path.join(
        os.environ.get("TEMP", "."), "pc_runner_shots")
    try:
        os.makedirs(shots_dir, exist_ok=True)
    except Exception:
        pass
    path = os.path.join(shots_dir, f"shot_{int(time.time() * 1000)}.png")
    try:
        ImageGrab.grab().save(path, "PNG")
        with open(path, "rb") as f:
            head = f.read(8)
        if head != b"\x89PNG\r\n\x1a\n":
            return {"ok": False, "error": "bad_png", "detail": "magic bytes 校验失败"}
        return {"ok": True, "path": path, "bytes": os.path.getsize(path)}
    except Exception as ex:
        return {"ok": False, "error": "screenshot_failed", "detail": str(ex)[:160]}


def _h_launch_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """仅白名单 app_id → 绝对路径换出；任意路径请用 launch_path（高危动作档）。"""
    app_id = str(args.get("app_id") or "")
    path = app_whitelist().get(app_id)
    if not path:
        return {"ok": False, "error": "app_not_whitelisted", "detail": app_id}
    try:  # pragma: no cover - 副作用
        subprocess.Popen([path])
        return {"ok": True, "launched": app_id}
    except Exception as ex:  # pragma: no cover
        return {"ok": False, "error": "launch_failed", "detail": str(ex)[:160]}


def _h_launch_path(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 副作用
    path = str(args.get("path") or "")
    if not path:
        return {"ok": False, "error": "path_required"}
    extra = args.get("args") or []
    try:
        cmd = [path] + [str(x) for x in extra] if isinstance(extra, list) else path
        subprocess.Popen(cmd, shell=not isinstance(cmd, list))
        return {"ok": True, "launched": path[:200]}
    except Exception as ex:
        return {"ok": False, "error": "launch_failed", "detail": str(ex)[:160]}


def _h_focus_window(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 需桌面
    if not _UIA_OK:
        return _need("uia_unavailable")
    title = str(args.get("window") or "")

    def _do():
        win = _uia.WindowControl(searchDepth=1, Name=title)
        if not win.Exists(0.5):
            return {"ok": False, "error": "window_not_found", "detail": title}
        win.SetActive()
        return {"ok": True, "focused": win.Name}

    res, err = _run_with_timeout(_do)
    return res if not err else {"ok": False,
                                "error": err if isinstance(err, str) else "focus_failed"}


def _h_click_control(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 需桌面
    if not _UIA_OK:
        return _need("uia_unavailable")
    target = str(args.get("target") or args.get("name") or "")
    if not target:
        return {"ok": False, "error": "target_required"}

    def _do():
        ctrl = _uia.Control(searchDepth=TREE_MAX_DEPTH, Name=target)
        if not ctrl.Exists(1.0):
            return {"ok": False, "error": "control_not_found", "detail": target}
        ctrl.Click(simulateMove=False)
        return {"ok": True, "clicked": target}

    res, err = _run_with_timeout(_do)
    return res if not err else {"ok": False,
                                "error": err if isinstance(err, str) else "click_failed"}


def _h_click_point(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 副作用
    """按坐标点击（VLM grounding 导出后由路由喂入；坐标非 LLM 自由文本）。"""
    try:
        x = int(args.get("x"))
        y = int(args.get("y"))
    except Exception:
        return {"ok": False, "error": "xy_required"}
    try:
        import ctypes
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)  # left down
        ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)  # left up
        return {"ok": True, "clicked_at": [x, y]}
    except Exception as ex:
        return {"ok": False, "error": "click_failed", "detail": str(ex)[:160]}


def _h_type_text(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 副作用
    if not _UIA_OK:
        return _need("uia_unavailable")
    text = str(args.get("text") or "")
    res, err = _run_with_timeout(lambda: _uia.SendKeys(text, waitTime=0) or True)
    return {"ok": True, "typed_len": len(text)} if not err else {
        "ok": False, "error": err if isinstance(err, str) else "type_failed"}


def _h_send_keys(args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 副作用
    if not _UIA_OK:
        return _need("uia_unavailable")
    keys = str(args.get("keys") or "")
    res, err = _run_with_timeout(lambda: _uia.SendKeys(keys, waitTime=0) or True)
    return {"ok": True, "sent": keys[:80]} if not err else {
        "ok": False, "error": err if isinstance(err, str) else "keys_failed"}


def _h_run_command(args: Dict[str, Any]) -> Dict[str, Any]:
    """任意 PowerShell/shell（最高危；需 PC_RUNNER_SHELL）。捕获 stdout/stderr/rc + 超时。"""
    cmd = str(args.get("command") or "")
    if not cmd:
        return {"ok": False, "error": "command_required"}
    timeout = float(os.environ.get("PC_RUNNER_CMD_TIMEOUT", "60") or 60)
    try:  # pragma: no cover - 副作用
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "stdout": (p.stdout or "")[:8000], "stderr": (p.stderr or "")[:4000]}
    except subprocess.TimeoutExpired:  # pragma: no cover
        return {"ok": False, "error": "timeout", "detail": f">{timeout}s"}
    except Exception as ex:  # pragma: no cover
        return {"ok": False, "error": "command_failed", "detail": str(ex)[:200]}


HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "list_windows": _h_list_windows,
    "read_tree": _h_read_tree,
    "screenshot": _h_screenshot,
    "launch_app": _h_launch_app,
    "launch_path": _h_launch_path,
    "focus_window": _h_focus_window,
    "click_control": _h_click_control,
    "click_point": _h_click_point,
    "type_text": _h_type_text,
    "send_keys": _h_send_keys,
    "run_command": _h_run_command,
}


def _audit(tool: str, ok: Any, actor: str = "", error: str = "") -> None:
    path = os.environ.get("PC_RUNNER_AUDIT", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "tool": tool,
                                "tier": tier_of(tool), "ok": bool(ok),
                                "actor": actor, "error": error},
                               ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - IO
        logger.debug("audit write failed", exc_info=True)


def dispatch(tool: str, args: Optional[Dict[str, Any]] = None, *,
             handlers: Optional[Dict[str, Callable]] = None,
             actor: str = "") -> Dict[str, Any]:
    """核心分发（纯逻辑，OS 副作用在 handler 里）。返回 runner 响应 dict（含 ok）。

    逐级开闸＝结构性防线：未知工具拒 → 动作需 PC_RUNNER_ACTIONS 且非冻结 →
    命令再需 PC_RUNNER_SHELL → handler → 审计。
    """
    handlers = handlers if handlers is not None else HANDLERS
    tool = str(tool or "")
    args = dict(args or {})
    tier = tier_of(tool)
    if tier == "unknown":
        _audit(tool, False, actor, "unknown_tool")
        return {"ok": False, "error": "unknown_tool"}
    if tier in ("action", "shell"):
        if actions_frozen():
            _audit(tool, False, actor, "actions_frozen")
            return {"ok": False, "error": "actions_frozen"}
        if not actions_enabled():
            _audit(tool, False, actor, "actions_disabled")
            return {"ok": False, "error": "actions_disabled",
                    "detail": "set PC_RUNNER_ACTIONS=1"}
        if tier == "shell" and not shell_enabled():
            _audit(tool, False, actor, "shell_disabled")
            return {"ok": False, "error": "shell_disabled",
                    "detail": "set PC_RUNNER_SHELL=1"}
    h = handlers.get(tool)
    if h is None:
        return {"ok": False, "error": "not_implemented", "detail": tool}
    try:
        res = h(args)
    except Exception as ex:  # noqa: BLE001
        _audit(tool, False, actor, "handler_exception")
        return {"ok": False, "error": "handler_exception", "detail": str(ex)[:200]}
    if not isinstance(res, dict):
        res = {"ok": False, "error": "bad_handler_result"}
    res.setdefault("ok", True)
    _audit(tool, res.get("ok"), actor, str(res.get("error") or ""))
    return res


def health() -> Dict[str, Any]:
    return {"ok": True, "version": VERSION,
            "win32_available": win32_available(),
            "uia_available": uia_available(),
            "elevated": is_elevated(),
            "actions_enabled": actions_enabled(),
            "shell_enabled": shell_enabled(),
            "actions_frozen": actions_frozen()}


# ── HTTP 服务（stdlib，Bearer=PC_RUNNER_TOKEN，只监听内网）────────────────
def make_handler(token: str):  # pragma: no cover - IO
    import hmac
    from http.server import BaseHTTPRequestHandler

    def _authed(hdr: str) -> bool:
        return bool(token) and hmac.compare_digest(str(hdr or ""), "Bearer " + token)

    class _H(BaseHTTPRequestHandler):
        def _send(self, status: int, obj: Dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            return

        def do_GET(self):
            if not _authed(self.headers.get("Authorization", "")):
                return self._send(401, {"ok": False, "error": "unauthorized"})
            if self.path.split("?", 1)[0] == "/health":
                return self._send(200, health())
            return self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            if not _authed(self.headers.get("Authorization", "")):
                return self._send(401, {"ok": False, "error": "unauthorized"})
            if self.path.split("?", 1)[0] != "/call":
                return self._send(404, {"ok": False, "error": "not_found"})
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return self._send(400, {"ok": False, "error": "bad_json"})
            actor = str(self.headers.get("X-PC-Actor", ""))[:60]
            out = dispatch(str(body.get("tool") or ""), body.get("args") or {}, actor=actor)
            return self._send(200, out)

    return _H


def main() -> None:  # pragma: no cover - 入口
    from http.server import ThreadingHTTPServer
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("PC_RUNNER_TOKEN", "")
    if not token:
        raise SystemExit("PC_RUNNER_TOKEN 未设置——拒绝裸奔启动")
    host = os.environ.get("PC_RUNNER_BIND", "127.0.0.1")
    port = int(os.environ.get("PC_RUNNER_PORT", "18760") or 18760)
    srv = ThreadingHTTPServer((host, port), make_handler(token))
    logger.info("pc_runner %s 监听 %s:%s elevated=%s win32=%s uia=%s "
                "actions=%s shell=%s", VERSION, host, port, is_elevated(),
                win32_available(), uia_available(), actions_enabled(), shell_enabled())
    srv.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
