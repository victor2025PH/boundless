"""本机状态快照：给 ``chatx-agent status`` 和本机「舰队节点」页用。

只在原有字段上追加。不包含 node_key / enroll_secret / 实例 token。
最近一次心跳写在 ``last_heartbeat.json``（只有时间戳），不回写 agent.json。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .identity import host_name
from .protocol import PROTO_VERSION
from .service import TASK_NAME, service_status

logger = logging.getLogger("fleet.local_status")

HEARTBEAT_STAMP = "last_heartbeat.json"
STALE_FLOOR_SEC = 120
PROBE_TIMEOUT_SEC = 2.0
TASK_QUERY_TIMEOUT_SEC = 5.0

UI_PENDING = "pending"
UI_ONLINE = "online"
UI_OFFLINE = "offline"
UI_REJECTED = "rejected"
UI_LABELS = {
    UI_PENDING: "待批准",
    UI_ONLINE: "在线",
    UI_OFFLINE: "离线",
    UI_REJECTED: "已拒绝",
}

_SECRET_KEYS = frozenset({
    "node_key", "enroll_secret", "auth_token", "token", "secret", "room_key",
    "password", "bearer", "authorization",
})

ServiceFn = Callable[[], Dict[str, Any]]
ProbeFn = Callable[[str], bool]


def enrollment_of(data: Dict[str, Any]) -> str:
    """与 CLI 旧口径一致：有 node_key 即为 enrolled，其余再看拒绝 / 待批准。"""
    if str(data.get("node_key") or ""):
        return "enrolled"
    if data.get("enroll_rejected"):
        return "rejected"
    if data.get("pending_request_id"):
        return "pending"
    return "none"


def stale_after_sec(heartbeat_sec: int) -> int:
    try:
        hb = int(heartbeat_sec)
    except (TypeError, ValueError):
        hb = 30
    if hb < 5:
        hb = 5
    return max(STALE_FLOOR_SEC, hb * 3)


def classify_ui_state(
    enrollment: str,
    *,
    controller_reachable: bool,
    last_heartbeat_at: Optional[float],
    now: float,
    stale_after: float,
) -> str:
    """四态：待批准 / 在线 / 离线 / 已拒绝。

    在线 = 已登记、主控这次探得通、并且本地心跳戳还在有效期内。
    未登记（none）显示为离线。
    """
    if enrollment == "rejected":
        return UI_REJECTED
    if enrollment == "pending":
        return UI_PENDING
    if enrollment != "enrolled":
        return UI_OFFLINE
    fresh = False
    if isinstance(last_heartbeat_at, (int, float)) and not isinstance(last_heartbeat_at, bool):
        age = float(now) - float(last_heartbeat_at)
        fresh = 0 <= age <= float(stale_after)
    if controller_reachable and fresh:
        return UI_ONLINE
    return UI_OFFLINE


def task_state_running(state: str) -> bool:
    """schtasks 英文 Running / 中文 正在运行，systemd 的 active。Ready / 就绪 不算在跑。"""
    return str(state or "").strip().lower() in {"running", "active", "正在运行"}


def task_label_of(*, installed: bool, running: bool, query: str) -> str:
    """计划任务「ChatX Fleet Agent」在本机页上的一句中文。超时不算未安装。"""
    if str(query or "") != "ok":
        return "未能查询"
    if not installed:
        return "未安装"
    if running:
        return "运行中"
    return "未运行"


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def record_heartbeat(state_dir: Path, at: float) -> None:
    """心跳成功后落一个时间戳。失败不影响心跳本身。"""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return
    path = Path(state_dir) / HEARTBEAT_STAMP
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"at": round(float(at), 3)}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.debug("[fleet] heartbeat stamp not written", exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass


def read_heartbeat_at(state_dir: Path) -> Optional[float]:
    try:
        raw = json.loads((Path(state_dir) / HEARTBEAT_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if at <= 0:
        return None
    return float(at)


def probe_controller(url: str, *, timeout: float = PROBE_TIMEOUT_SEC, urlopen=None) -> bool:
    """GET 主控根地址。不带 Authorization。任何 <500 的 HTTP 响应都算可达。"""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "*/*",
        "User-Agent": "chatx-agent-status",
    })
    if req.has_header("Authorization"):
        return False
    try:
        with opener(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200) < 500
    except urllib.error.HTTPError as e:
        return int(getattr(e, "code", 500) or 500) < 500
    except Exception:
        return False


def _run_quick(cmd, timeout: float = TASK_QUERY_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def query_task_status(*, timeout: float = TASK_QUERY_TIMEOUT_SEC) -> Dict[str, Any]:
    """短超时查计划任务 / systemd。超时不当成「未安装」。"""
    try:
        got = service_status(run=lambda c: _run_quick(c, timeout))
        got = dict(got)
        got["query"] = "ok"
        return got
    except subprocess.TimeoutExpired:
        return {"installed": False, "kind": "", "task_name": TASK_NAME, "state": "", "query": "timeout"}
    except Exception:
        return {"installed": False, "kind": "", "task_name": TASK_NAME, "state": "", "query": "error"}


def _is_secret_key(key: str) -> bool:
    k = str(key or "").lower()
    if k in _SECRET_KEYS:
        return True
    return k.endswith(("_secret", "_token", "_password"))


def mask_secrets(obj: Any) -> Any:
    """诊断输出里的密钥换成 ***。空字符串保持空，避免把「没配」显示成掩码。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_secret_key(str(k)):
                out[k] = "***" if v else ""
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    return obj


def _agent_version() -> str:
    from .agent import AGENT_VERSION
    return AGENT_VERSION


def build_local_status(
    cfg: Any,
    *,
    machine_id: str,
    now: Optional[float] = None,
    probe: Optional[ProbeFn] = None,
    service_status_fn: Optional[ServiceFn] = None,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    """旧 status 字段原样保留，并追加本机页需要的可达性 / 任务 / 心跳 / 中文状态。"""
    now_f = time.time() if now is None else float(now)
    data = cfg.data if isinstance(getattr(cfg, "data", None), dict) else {}
    enrollment = enrollment_of(data)
    hb_at = read_heartbeat_at(Path(cfg.state_dir))
    url = str(getattr(cfg, "controller_url", "") or "").rstrip("/")
    do_probe = probe if probe is not None else probe_controller
    reachable = bool(do_probe(url)) if url else False
    query = service_status_fn if service_status_fn is not None else query_task_status
    try:
        svc = query() or {}
    except Exception:
        svc = {"installed": False, "state": "", "task_name": TASK_NAME, "query": "error"}
    if not isinstance(svc, dict):
        svc = {"installed": False, "state": "", "task_name": TASK_NAME, "query": "error"}
    task_query = str(svc.get("query") or "ok")
    state = str(svc.get("state") or "").strip()
    installed = bool(svc.get("installed"))
    running = installed and task_query == "ok" and task_state_running(state)
    task_label = task_label_of(installed=installed, running=running, query=task_query)
    window = stale_after_sec(int(getattr(cfg, "heartbeat_sec", 30) or 30))
    ui = classify_ui_state(
        enrollment,
        controller_reachable=reachable,
        last_heartbeat_at=hb_at,
        now=now_f,
        stale_after=window,
    )
    instances = []
    for inst in cfg.instances:
        item = dict(inst)
        item["auth_token"] = "***" if item.get("auth_token") else ""
        instances.append(item)
    log_dir = str(Path(cfg.state_dir) / "logs")
    out: Dict[str, Any] = {
        "controller_url": url,
        "node_id": str(getattr(cfg, "node_id", "") or ""),
        "enrolled": bool(getattr(cfg, "node_key", "") or ""),
        "enrollment": enrollment,
        "pending": enrollment == "pending",
        "pairing_code": str(data.get("pairing_code") or ""),
        "machine_id": str(machine_id or ""),
        "instances": instances,
        "state_dir": str(cfg.state_dir),
        "agent_version": _agent_version(),
        "proto_version": PROTO_VERSION,
        "host_name": host if host is not None else host_name(),
        "controller_reachable": reachable,
        "task_installed": installed,
        "task_running": running,
        "task_state": state,
        "task_name": str(svc.get("task_name") or TASK_NAME),
        "task_query": task_query,
        "task_label": task_label,
        "last_heartbeat_at": _iso(hb_at) if hb_at else None,
        "heartbeat_stale_after_sec": window,
        "ui_state": ui,
        "ui_label": UI_LABELS[ui],
        "log_dir": log_dir,
        "console_url": (url + "/console") if url else "",
    }
    return mask_secrets(out)
