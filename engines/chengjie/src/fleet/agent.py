"""chatx-agent —— 装在每台受控电脑上的节点端（只用标准库，可单独打包）。

    python -m src.fleet.agent enroll --controller https://bd2026.cc/fleet --code 12345678 \\
        --instance player=http://127.0.0.1:18797 --config-path config_player/config.yaml
    python -m src.fleet.agent run            # 前台常驻：心跳 + 长轮询领任务 + 执行 + ack
    python -m src.fleet.agent run --once     # 跑一轮就退（联调 / 计划任务）
    python -m src.fleet.agent status         # 本机摘要（含主控是否可达、计划任务、最近心跳）
    python -m src.fleet.agent ui             # 打开本机「舰队节点」页（只监听 127.0.0.1）
    python -m src.fleet.agent install-service   # 开机自启（Windows 计划任务 SYSTEM / Linux systemd）+ 立即启动
    python -m src.fleet.agent run --service     # 服务实际入口：监督循环（见 service.py）

流程（契约 docs/FLEET_CONTROL_CONTRACT.md）：
    enroll(code, machine_id) → node_key 存 <state_dir>/agent.json（只在本机）
    loop: heartbeat（本机实例摘要，无聊天原文）→ pull(wait=25s 长轮询) → 逐条 execute → ack（幂等）
    任何一步失败：指数退避（2s → 60s），不崩、不丢 node_key；401 → 标记 revoked 停止（等重新注册）。

任务执行只调本机智聊实例已有的 HTTP API（Bearer web_admin.auth_token），不直接碰数据库：
    ping            → 本地回 pong
    pull_overview   → GET  /api/player-care/overview
    account_health  → GET  /api/accounts/fleet-health（裁成 total / lifecycle / fleet / 每号 stage+quota）
    login_qr        → POST /api/platforms/{platform}/login/start → {login_id, qr_image, ...}
    login_status    → GET  /api/platforms/{platform}/login/{login_id}/status
    stop_account    → POST /api/player-care/commands {kind: stop, account, phone}
    restart_instance→ 实例条目配了 restart_cmd 才执行，否则 rejected
    upgrade         → 下载+sha256 校验+换文件后重启（updater.py；仅冻结 exe，源码态拒绝）
    push_config     → v1 仍 rejected: not_supported
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import platform as _platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .login_qr import safe_qr_data_url, start_payload as login_start_payload, valid_login_id, valid_platform
from .detect import (
    LIVE_STREAM_PORTS, detect_instances, is_live_port_url, is_live_stream_host, is_loopback_url, sanitize_instances,
    url_port,
)
from .identity import (
    StateDirLockError, _is_reparse, assign_owner_admins, default_state_dir,
    discard_untrusted_secret, host_name, lock_state_dir, node_machine_id, os_label,
    state_dir_is_locked,
)
from .local_status import build_local_status, record_heartbeat
from .service import acquire_single_instance, install_service, service_status, supervise, uninstall_service
from .updater import apply_upgrade
from .protocol import (
    DEFAULT_HEARTBEAT_SEC, MAX_LONGPOLL_WAIT_SEC, PROTO_VERSION, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_ACCOUNT_HEALTH, TASK_LOGIN_QR, TASK_LOGIN_STATUS, TASK_PING, TASK_PULL_OVERVIEW, TASK_PUSH_CONFIG,
    TASK_RESTART_INSTANCE, TASK_STOP_ACCOUNT, TASK_UPGRADE,
)

logger = logging.getLogger("fleet.agent")

AGENT_VERSION = "0.3.3"
CONFIG_NAME = "agent.json"
HTTP_TIMEOUT = 15
LOCAL_TIMEOUT = 8
BACKOFF_MIN, BACKOFF_MAX = 2.0, 60.0
REENROLL_BACKOFF_SEC = 30
ENV_CONTROLLER = "CHATX_FLEET_CONTROLLER"

HttpFn = Callable[[str, str, Optional[Dict[str, Any]], Dict[str, str], float], Tuple[int, Dict[str, Any]]]


# ── HTTP（标准库；可注入替身做测试） ────────────────────────────────────────
def http_json(method: str, url: str, body: Optional[Dict[str, Any]], headers: Dict[str, str],
              timeout: float) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": f"chatx-agent/{AGENT_VERSION}", **headers}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        code = e.code
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        parsed = {"raw": raw[:200].decode("utf-8", "replace")}
    return code, parsed if isinstance(parsed, dict) else {"data": parsed}


class AgentError(RuntimeError):
    pass


class Unauthorized(AgentError):
    pass


# Kept across an upgrade from an unlocked directory. restart_cmd and config_path
# are always dropped; config_path is rediscovered by ``detect``.
_IDENTITY_FIELDS = ("node_id", "node_key", "heartbeat_sec", "enroll_secret",
                    "pending_request_id", "pairing_code")
# Per-instance fields carried over from an unlocked agent.json. The embedded
# auth_token is kept so a 0.2.x instance without config_path keeps working
# (FLEET_ISSUES_FOR_DEVIN g2). It is only written back after the new state
# directory is locked. restart_cmd (runs as SYSTEM) and config_path (read as
# SYSTEM) are never carried over from an unlocked file.
_MIGRATED_INSTANCE_LIMIT = 8


def _migrated_instances(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_list = source.get("instances") if isinstance(source, dict) else None
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_list, list):
        return out
    seen = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        url = raw.get("base_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        name = name.strip()[:40]
        url = url.strip().rstrip("/")
        if not name or name in seen or not is_loopback_url(url) or is_live_port_url(url):
            continue
        tok = raw.get("auth_token")
        domain = raw.get("domain")
        role = raw.get("role")
        seen.add(name)
        out.append({
            "name": name,
            "base_url": url[:160],
            "auth_token": tok if isinstance(tok, str) and len(tok) <= 512 else "",
            "config_path": "",
            "domain": domain[:40] if isinstance(domain, str) else "",
            "restart_cmd": "",
            "role": role[:20] if isinstance(role, str) else "",
        })
        if len(out) >= _MIGRATED_INSTANCE_LIMIT:
            break
    return out


def migrated_agent_data(controller_url: str, source: Dict[str, Any]) -> Dict[str, Any]:
    """Identity fields plus the installer controller.

    ``instances`` keeps only name / loopback base_url / auth_token / domain / role
    of each old instance. restart_cmd and config_path are dropped.
    """
    data: Dict[str, Any] = {
        "controller_url": str(controller_url or "").rstrip("/"),
        "node_id": "",
        "node_key": "",
        "heartbeat_sec": DEFAULT_HEARTBEAT_SEC,
        "instances": [],
    }
    if not isinstance(source, dict):
        return data
    for key in _IDENTITY_FIELDS:
        if key == "heartbeat_sec":
            continue
        val = source.get(key)
        if isinstance(val, str) and val:
            data[key] = val
    # Only a real integer in the agent range is kept. Strings and out-of-range
    # values fall back to the default so a planted file cannot stall the service.
    hb = source.get("heartbeat_sec")
    if isinstance(hb, int) and not isinstance(hb, bool) and 5 <= hb <= 3600:
        data["heartbeat_sec"] = hb
    data["instances"] = _migrated_instances(source)
    return data


def migrate_legacy_agent(state_dir: Path, controller_url: str, source: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite agent.json after the state dir is locked. Returns the stored data."""
    cfg = AgentConfig(state_dir)
    cfg._legacy_source = None
    cfg.data = migrated_agent_data(controller_url, source)
    cfg.save()
    return dict(cfg.data)


# ── 配置 ────────────────────────────────────────────────────────────────────
class AgentConfig:
    """<state_dir>/agent.json。instances: [{name, base_url, auth_token, config_path, domain, restart_cmd}]"""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.path = self.state_dir / CONFIG_NAME
        self.data: Dict[str, Any] = {"controller_url": "", "node_id": "", "node_key": "",
                                     "heartbeat_sec": DEFAULT_HEARTBEAT_SEC, "instances": []}
        self._legacy_source: Optional[Dict[str, Any]] = None
        self._legacy_bytes: Optional[bytes] = None
        self.load()

    def load(self) -> None:
        self._legacy_source = None
        self._legacy_bytes = None
        # Sample before discard. An unlocked file is not merged: identity only.
        if (self.path.is_file() and not _is_reparse(self.state_dir) and not _is_reparse(self.path)
                and not state_dir_is_locked(self.state_dir)):
            try:
                blob = self.path.read_bytes()
                raw = json.loads(blob.decode("utf-8-sig"))
            except Exception:
                blob, raw = None, None
            if isinstance(raw, dict):
                self._legacy_source = raw
                self._legacy_bytes = blob
                self.data = migrated_agent_data(str(raw.get("controller_url") or ""), raw)
        if self._legacy_source is not None:
            # Do not delete the old agent.json here. It stays on disk until
            # save() has locked the directory and written the identity-only
            # copy; if that fails the old file is restored (issue f).
            env = (os.environ.get(ENV_CONTROLLER) or "").strip()
            if env:
                self.data["controller_url"] = env
            return
        try:
            # Same rule as discard_untrusted_secret: on Windows keep the file only
            # when the state directory is locked and the owner is SYSTEM or Admins.
            discard_untrusted_secret(self.path)
            if self._legacy_source is None:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self.data.update(d)
        except StateDirLockError:
            raise
        except Exception:
            pass
        env = (os.environ.get(ENV_CONTROLLER) or "").strip()
        if env:
            self.data["controller_url"] = env

    @property
    def migrating(self) -> bool:
        """True while an unlocked agent.json is held in memory and not yet rewritten."""
        return self._legacy_source is not None

    def save(self) -> None:
        if self._legacy_source is None:
            self._write_locked()
            return
        try:
            self._write_locked()
        except Exception as e:
            restored = self._restore_legacy_file()
            note = "old agent.json kept" if restored else "old agent.json NOT restored"
            logger.error("[agent] migration of the unlocked state directory failed: %s (%s)", e, note)
            raise StateDirLockError(f"migration failed: {e}; {note}") from e
        self._legacy_source = None
        self._legacy_bytes = None

    def _restore_legacy_file(self) -> bool:
        """Put an agent.json with the old identity back after a failed migration.

        If the rename never happened the old file is still there. If the
        directory was already swapped for a locked one, only the identity-only
        rewrite goes back, never the unlocked original (it may carry a planted
        restart_cmd). Returns True when an agent.json with the identity exists.
        """
        try:
            if self.path.is_file() and not _is_reparse(self.path):
                return True
            if _is_reparse(self.state_dir) or _is_reparse(self.state_dir.parent):
                return False
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if state_dir_is_locked(self.state_dir) or self._legacy_bytes is None:
                body = json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")
            else:
                body = self._legacy_bytes
            tmp = self.path.with_suffix(".restore")
            tmp.write_bytes(body)
            os.replace(tmp, self.path)
            try:
                assign_owner_admins(self.path)
            except Exception as e:
                logger.debug("[agent] assign_owner_admins failed: %s", e)
            return self.path.is_file()
        except Exception:
            logger.error("[agent] could not restore agent.json after a failed migration", exc_info=True)
            return False

    def _write_locked(self) -> None:
        lock_state_dir(self.state_dir)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError as e:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise StateDirLockError(f"chmod agent.json failed: {e}") from e
        else:
            assign_owner_admins(self.path)

    @property
    def controller_url(self) -> str:
        return str(self.data.get("controller_url") or "").rstrip("/")

    @property
    def node_key(self) -> str:
        return str(self.data.get("node_key") or "")

    @property
    def node_id(self) -> str:
        return str(self.data.get("node_id") or "")

    @property
    def heartbeat_sec(self) -> int:
        try:
            return max(5, int(self.data.get("heartbeat_sec") or DEFAULT_HEARTBEAT_SEC))
        except (TypeError, ValueError):
            return DEFAULT_HEARTBEAT_SEC

    @property
    def instances(self) -> List[Dict[str, Any]]:
        v = self.data.get("instances")
        return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []

    def add_instance(self, name: str, base_url: str, *, auth_token: str = "", config_path: str = "",
                     domain: str = "", restart_cmd: str = "", role: str = "",
                     allow_live_port: bool = False) -> None:
        if not is_loopback_url(base_url):
            raise AgentError("instance URL must be loopback (127.0.0.1 / localhost / ::1)")
        if is_live_port_url(base_url) and not allow_live_port:
            raise AgentError(f"port {url_port(base_url)} is a live-stream port "
                             f"({', '.join(str(p) for p in sorted(LIVE_STREAM_PORTS))}); "
                             "pass --allow-live-port to register it anyway")
        inst = [i for i in self.instances if i.get("name") != name]
        item = {"name": name, "base_url": base_url.rstrip("/"), "auth_token": auth_token,
                "config_path": config_path, "domain": domain, "restart_cmd": restart_cmd,
                "role": role}
        if allow_live_port and is_live_port_url(base_url):
            item["allow_live_port"] = True
        inst.append(item)
        self.data["instances"] = inst

    def remove_instance(self, name: str) -> bool:
        """Drop the instance called ``name``. True when something was removed."""
        before = self.instances
        after = [i for i in before if i.get("name") != name]
        if len(after) == len(before):
            return False
        self.data["instances"] = after
        return True


def _instance_token(inst: Dict[str, Any]) -> str:
    """优先 auth_token；否则从 config_path 的 web_admin.auth_token 读（不复制密钥进 agent.json）。"""
    tok = str(inst.get("auth_token") or "")
    if tok:
        return tok
    cp = str(inst.get("config_path") or "")
    if not cp:
        return ""
    try:
        import yaml  # 智聊环境必有；冻结 exe 也随包，缺了走下面的正则回落

        with open(cp, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(((cfg.get("web_admin") or {}).get("auth_token")) or "")
    except ImportError:
        return _grep_yaml_scalar(cp, "web_admin", "auth_token")
    except Exception:
        return ""


def _grep_yaml_scalar(path: str, section: str, key: str) -> str:
    """无 PyYAML 时读 ``section:\n  key: value`` 两级标量（够用于 auth_token / domain）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(rf"^{re.escape(section)}:\s*$((?:\n[ \t]+.*)*)", text, re.M)
    block = m.group(1) if m else text
    m2 = re.search(rf"^[ \t]*{re.escape(key)}:[ \t]*['\"]?([^'\"\n#]*)", block, re.M)
    return m2.group(1).strip() if m2 else ""


def _health_only(inst: Dict[str, Any]) -> bool:
    if str(inst.get("role") or "") == "health":
        return True
    if str(inst.get("name") or "") == "avatarhub":
        return True
    return _instance_domain(inst) == "avatar_hub"


def _instance_domain(inst: Dict[str, Any]) -> str:
    d = str(inst.get("domain") or "")
    if d:
        return d
    cp = str(inst.get("config_path") or "")
    if cp:
        try:
            import yaml

            with open(cp, "r", encoding="utf-8") as f:
                return str((yaml.safe_load(f) or {}).get("domain") or "")
        except Exception:
            return ""
    return ""


# ── Agent ──────────────────────────────────────────────────────────────────
class NodeAgent:
    def __init__(self, cfg: AgentConfig, *, http: HttpFn = http_json, app_version: str = "",
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.http = http
        self.clock = clock
        self.app_version = app_version or _detect_app_version()
        # Persist the identity-only rewrite before machine_id locks the directory.
        if getattr(cfg, "_legacy_source", None) is not None:
            cfg.save()
            cfg._legacy_source = None
        self.machine_id = node_machine_id(cfg.state_dir)
        self.started_at = clock()
        self.revoked = False
        self.exit_requested = False   # upgrade 换文件：ack 后由循环退出，服务层重新拉起
        self.last_error = ""
        self.stats = {"heartbeats": 0, "tasks_done": 0, "tasks_failed": 0, "tasks_rejected": 0, "errors": 0}

    # ── 主控调用 ──
    def _ctrl(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, *, auth: bool = True,
              bearer: str = "", timeout: float = HTTP_TIMEOUT) -> Dict[str, Any]:
        if not self.cfg.controller_url:
            raise AgentError("controller_url 未配置（先 enroll）")
        headers = {"X-Fleet-Proto": str(PROTO_VERSION)}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        elif auth:
            if not self.cfg.node_key:
                raise AgentError("尚未注册（无 node_key）")
            headers["Authorization"] = f"Bearer {self.cfg.node_key}"
        code, data = self.http(method, self.cfg.controller_url + path, body, headers, timeout)
        if code == 401:
            raise Unauthorized(str(data.get("detail") or "unauthorized"))
        if code >= 400:
            raise AgentError(f"{path} → HTTP {code}: {data.get('detail') or data}")
        return data

    def _detect_and_add(self) -> List[Dict[str, str]]:
        found = detect_instances(search=(os.name == "nt"),
                                 live_stream=is_live_stream_host(self.cfg.state_dir))
        # Belt and braces: never register a live-stream port from detect.
        found = [i for i in found if not is_live_port_url(str(i.get("base_url") or ""))]
        added = False
        for inst in found:
            url = str(inst.get("base_url") or "").rstrip("/")
            # An instance already registered at the same URL (migrated or added by
            # hand) keeps its name and auth_token; detect only fills empty fields.
            same = [i for i in self.cfg.instances if str(i.get("base_url") or "").rstrip("/") == url]
            if url and same:
                cur = same[0]
                for key in ("config_path", "domain", "role"):
                    val = str(inst.get(key) or "")
                    if val and not str(cur.get(key) or ""):
                        cur[key] = val
                        added = True
                continue
            try:
                self.cfg.add_instance(str(inst.get("name") or "chatx"), url,
                                      config_path=str(inst.get("config_path") or ""),
                                      domain=str(inst.get("domain") or ""), role=str(inst.get("role") or ""))
                added = True
            except AgentError:
                continue
        if added:
            self.cfg.save()
        return found

    def _store_enrollment(self, res: Dict[str, Any]) -> None:
        self.cfg.data.update({
            "node_id": res["node_id"], "node_key": res["node_key"],
            "heartbeat_sec": int(res.get("heartbeat_sec") or DEFAULT_HEARTBEAT_SEC),
        })
        self.cfg.data.pop("pending_request_id", None)
        self.cfg.data.pop("enroll_rejected", None)
        self.cfg.data.pop("reenroll_not_before", None)
        self.cfg.save()
        self.revoked = False

    def _enroll_secret(self) -> str:
        sec = str(self.cfg.data.get("enroll_secret") or "")
        if len(sec) < 16:
            sec = "es_" + secrets.token_urlsafe(32)
            self.cfg.data["enroll_secret"] = sec
            self.cfg.save()
        return sec

    def _write_pairing(self, code: str) -> None:
        if not code:
            return
        try:
            lock_state_dir(self.cfg.state_dir)
            (self.cfg.state_dir / "pairing.txt").write_text(str(code).strip() + "\n", encoding="ascii")
        except Exception:
            logger.debug("[agent] pairing code file not written", exc_info=True)

    def enroll(self, code: str = "", *, controller_url: str = "", room_key: str = "",
               detect: bool = False) -> Dict[str, Any]:
        """有注册码或机房密钥则立刻拿到 node_key；都没有则登记为待批准（不抛错）。"""
        if controller_url:
            self.cfg.data["controller_url"] = controller_url.rstrip("/")
        if detect:
            self._detect_and_add()
        code = str(code or "").strip()
        room_key = str(room_key or "").strip()
        if not code and not room_key and self.cfg.node_key:
            # Already enrolled (e.g. identity migrated by the installer). A pending
            # request here would only sit in the console and, if approved, rotate
            # this node's key (FLEET_ISSUES g4). Only a code or room key re-enrolls.
            if controller_url:
                self.cfg.save()
            return {"node_id": self.cfg.node_id, "status": "active", "already_enrolled": True}
        body: Dict[str, Any] = {
            "machine_id": self.machine_id, "host_name": host_name(),
            "proto_version": PROTO_VERSION, "agent_version": AGENT_VERSION, "app_version": self.app_version,
            "os": os_label(), "instances": sanitize_instances(self.cfg.instances),
            "meta": {"python": _platform.python_version()},
            "enroll_secret": self._enroll_secret(),
        }
        if room_key:
            body["room_key"] = room_key
            bearer = room_key
        elif code:
            body["code"] = code
            bearer = code
        else:
            body["mode"] = "pending"
            bearer = "pending"
        res = self._ctrl("POST", "/api/fleet/enroll", body, auth=False, bearer=bearer)
        if res.get("node_key"):
            self._store_enrollment(res)
            return {"node_id": res["node_id"], "label": res.get("label"), "group_name": res.get("group_name"),
                    "status": "active"}
        if res.get("ok") and res.get("request_id"):
            self.cfg.data["pending_request_id"] = res["request_id"]
            self.cfg.data["pairing_code"] = str(res.get("pairing_code") or "")
            self.cfg.data.pop("enroll_rejected", None)
            self.cfg.data.pop("reenroll_not_before", None)
            self.cfg.save()
            self._write_pairing(str(res.get("pairing_code") or ""))
            return {"status": "pending", "request_id": res["request_id"], "expires_at": res.get("expires_at"),
                    "pairing_code": res.get("pairing_code") or ""}
        raise AgentError("注册失败")

    def poll_enrollment(self) -> Dict[str, Any]:
        """待批准时问一次主控。过期则重新登记；被拒绝则停在本机，不再自动重试。"""
        if self.cfg.node_key:
            return {"ok": True, "status": "active", "node_id": self.cfg.node_id}
        rid = str(self.cfg.data.get("pending_request_id") or "")
        if not rid:
            if self.cfg.data.get("enroll_rejected"):
                if "reenroll_not_before" in self.cfg.data:
                    self.cfg.data.pop("reenroll_not_before", None)
                    self.cfg.save()
                return {"ok": False, "status": "idle"}
            if self.cfg.data.get("reenroll_not_before"):
                return self._restart_enroll("backoff")
            return {"ok": False, "status": "idle"}
        res = self._ctrl("POST", "/api/fleet/enroll/poll",
                         {"request_id": rid, "machine_id": self.machine_id,
                          "enroll_secret": self._enroll_secret()}, auth=False, bearer=rid)
        if res.get("node_key"):
            self._store_enrollment(res)
            return {"ok": True, "status": "active", "node_id": res.get("node_id"), "label": res.get("label"),
                    "group_name": res.get("group_name")}
        status = str(res.get("status") or "")
        if status == "rejected":
            self.cfg.data.pop("pending_request_id", None)
            self.cfg.data.pop("reenroll_not_before", None)
            self.cfg.data["enroll_rejected"] = True
            self.cfg.save()
        elif status == "expired":
            self.cfg.data.pop("pending_request_id", None)
            self.cfg.save()
            if self.cfg.controller_url:
                try:
                    return self.enroll("", controller_url=self.cfg.controller_url)
                except AgentError as e:
                    logger.warning("[agent] re-request after expiry failed: %s", e)
        elif status in ("unknown", "already_claimed"):
            return self._restart_enroll(status)
        return {"ok": bool(res.get("ok")), "status": status or "pending"}

    def _restart_enroll(self, why: str) -> Dict[str, Any]:
        """Drop a dead pending id and start enroll again, with a backoff between tries.

        ``unknown`` and ``already_claimed`` used to leave ``pending_request_id`` set,
        so the service polled that id forever. A failed restart keeps
        ``reenroll_not_before`` so the next loop waits instead of spinning.
        """
        now = float(self.clock())
        not_before = float(self.cfg.data.get("reenroll_not_before") or 0)
        rejected = bool(self.cfg.data.get("enroll_rejected"))
        self.cfg.data.pop("pending_request_id", None)
        if rejected:
            self.cfg.data.pop("reenroll_not_before", None)
            self.cfg.save()
            return {"ok": False, "status": "idle"}
        if now < not_before:
            self.cfg.save()
            return {"ok": False, "status": "backoff"}
        self.cfg.data["reenroll_not_before"] = now + REENROLL_BACKOFF_SEC
        self.cfg.save()
        if not self.cfg.controller_url:
            return {"ok": False, "status": "backoff"}
        try:
            return self.enroll("", controller_url=self.cfg.controller_url)
        except AgentError as e:
            logger.warning("[agent] re-enroll after %s failed: %s", why, e)
            return {"ok": False, "status": "backoff"}

    def build_heartbeat(self) -> Dict[str, Any]:
        """本机摘要：实例是否活、账号数 / 生命周期分布、看板核心数字。**不含任何聊天内容。**"""
        instances, errors = [], []
        acc_total = acc_online = 0
        by_state: Dict[str, int] = {}
        overview_sum: Dict[str, Any] = {}
        for inst in self.cfg.instances:
            domain = _instance_domain(inst)
            if _health_only(inst):
                entry = {"name": inst.get("name"), "domain": "avatar_hub", "up": False, "role": "health"}
                last_err = ""
                for path in ("/health", "/api/health"):
                    try:
                        self._local(inst, "GET", path)
                        entry["up"] = True
                        last_err = ""
                        break
                    except Exception as e:
                        last_err = str(e)
                if last_err:
                    errors.append(f"{inst.get('name')}: avatar health {last_err}")
                instances.append(entry)
                continue
            entry = {"name": inst.get("name"), "domain": domain, "up": False}
            try:
                fh = self._local(inst, "GET", "/api/accounts/fleet-health")
                entry["up"] = True
                red = reduce_fleet_health(fh)
                entry["accounts"] = red["total"]
                acc_total += red["total"]
                acc_online += red["online"]
                for k, v in red["lifecycle"].items():
                    by_state[k] = by_state.get(k, 0) + int(v or 0)
            except Exception as e:
                errors.append(f"{inst.get('name')}: fleet-health {e}")
            if entry["domain"] == "player_care" and entry["up"]:
                try:
                    ov = self._local(inst, "GET", "/api/player-care/overview")
                    entry["player_overview"] = reduce_player_overview(ov)
                    for k, v in entry["player_overview"].items():
                        if isinstance(v, int):
                            overview_sum[k] = overview_sum.get(k, 0) + v
                    overview_sum["gateway"] = entry["player_overview"].get("gateway")
                except Exception as e:
                    errors.append(f"{inst.get('name')}: overview {e}")
            instances.append(entry)
        return {
            "agent_version": AGENT_VERSION, "proto_version": PROTO_VERSION, "app_version": self.app_version,
            "host_name": host_name(), "os": os_label(), "python": _platform.python_version(),
            "uptime_sec": int(self.clock() - self.started_at),
            "instances": instances,
            "accounts": {"total": acc_total, "online": acc_online},
            "fleet_health": {"by_state": by_state},
            "player_overview": overview_sum,
            "metrics": _metrics(),
            "errors": errors[:10],
        }

    def heartbeat(self) -> Dict[str, Any]:
        res = self._ctrl("POST", "/api/fleet/heartbeat", self.build_heartbeat())
        self.stats["heartbeats"] += 1
        record_heartbeat(self.cfg.state_dir, self.clock())
        hb = int(res.get("heartbeat_sec") or 0)
        if hb and hb != self.cfg.heartbeat_sec:
            self.cfg.data["heartbeat_sec"] = hb
            self.cfg.save()
        return res

    def pull(self, *, wait: int = MAX_LONGPOLL_WAIT_SEC, limit: int = 10) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode({"wait": int(wait), "limit": int(limit)})
        res = self._ctrl("GET", f"/api/fleet/tasks/pull?{q}", timeout=HTTP_TIMEOUT + wait)
        return [t for t in (res.get("tasks") or []) if isinstance(t, dict)]

    def ack(self, task_id: str, status: str, result: Optional[Dict[str, Any]] = None, detail: str = "") -> None:
        self._ctrl("POST", "/api/fleet/tasks/ack", {"task_id": task_id, "status": status,
                                                    "result": result or {}, "detail": detail[:500]})

    # ── 本机实例调用 ──
    def _local(self, inst: Dict[str, Any], method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = str(inst.get("base_url") or "").rstrip("/")
        if not base:
            raise AgentError("instance base_url 为空")
        if is_live_port_url(base) and not inst.get("allow_live_port"):
            # e.g. an 'avatarhub' at :9000 added by an older detect on a live machine.
            raise AgentError(f"live-stream port {url_port(base)} not probed (remove-instance {inst.get('name')})")
        headers = {}
        tok = _instance_token(inst)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        code, data = self.http(method, base + path, body, headers, LOCAL_TIMEOUT)
        if code >= 400:
            raise AgentError(f"local {path} → HTTP {code}: {data.get('detail') or data}")
        return data

    def _pick_instance(self, task: Dict[str, Any], *, prefer_domain: str = "",
                       allow_health: bool = False) -> Optional[Dict[str, Any]]:
        want = str((task.get("target") or {}).get("instance") or (task.get("payload") or {}).get("instance") or "")
        insts = self.cfg.instances if allow_health else [i for i in self.cfg.instances if not _health_only(i)]
        if want:
            for i in insts:
                if i.get("name") == want:
                    return i
            return None
        if prefer_domain:
            for i in insts:
                if _instance_domain(i) == prefer_domain:
                    return i
        return insts[0] if insts else None

    # ── 执行器 ──
    def execute(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """返回 (status, result, detail)。永不抛：异常 → failed。"""
        kind = str(task.get("kind") or "")
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        try:
            exp = float(task.get("expires_at") or 0)
            if exp and self.clock() > exp:
                return STATUS_REJECTED, {}, "expired_on_arrival"
            if kind == TASK_PING:
                return STATUS_DONE, {"pong": True, "agent_version": AGENT_VERSION, "app_version": self.app_version,
                                     "machine_id": self.machine_id, "host_name": host_name(),
                                     "time": self.clock(), "echo": payload.get("echo")}, "pong"
            if kind == TASK_PULL_OVERVIEW:
                inst = self._pick_instance(task, prefer_domain="player_care")
                if inst is None:
                    return STATUS_REJECTED, {}, "no_instance"
                ov = self._local(inst, "GET", "/api/player-care/overview")
                return STATUS_DONE, {"instance": inst.get("name"), "overview": reduce_player_overview(ov, full=True)}, "ok"
            if kind == TASK_ACCOUNT_HEALTH:
                inst = self._pick_instance(task)
                if inst is None:
                    return STATUS_REJECTED, {}, "no_instance"
                fh = self._local(inst, "GET", "/api/accounts/fleet-health")
                return STATUS_DONE, {"instance": inst.get("name"), **reduce_fleet_health(fh, with_accounts=True)}, "ok"
            if kind == TASK_LOGIN_QR:
                inst = self._pick_instance(task)
                if inst is None:
                    return STATUS_REJECTED, {}, "no_instance"
                # platform goes into a local URL path: whitelist shape only
                platform = valid_platform(payload.get("platform") or target.get("platform") or "whatsapp")
                if not platform:
                    return STATUS_REJECTED, {}, "bad_platform"
                res = self._local(inst, "POST", f"/api/platforms/{platform}/login/start", login_start_payload(payload))
                if not res.get("ok", True) and not res.get("login_id"):
                    return STATUS_FAILED, {"detail": res.get("detail"), "reason_code": res.get("reason_code")}, "login_start_failed"
                out = {k: res.get(k) for k in ("login_id", "status", "detail", "mode", "kind", "account_id", "qr_url",
                                               "instruction", "reason_code") if k in res}
                if "login_id" in out:
                    out["login_id"] = valid_login_id(out["login_id"])
                out["instance"] = inst.get("name")
                out["platform"] = platform
                out["qr_data_url"] = safe_qr_data_url(res.get("qr_image"))
                return STATUS_DONE, out, "qr_ready" if out["qr_data_url"] else "started"
            if kind == TASK_LOGIN_STATUS:
                inst = self._pick_instance(task)
                lid = valid_login_id(payload.get("login_id"))
                if inst is None or not lid:
                    return STATUS_REJECTED, {}, "no_instance_or_login_id"
                platform = valid_platform(payload.get("platform") or target.get("platform") or "whatsapp")
                if not platform:
                    return STATUS_REJECTED, {}, "bad_platform"
                res = self._local(inst, "GET", f"/api/platforms/{platform}/login/{lid}/status")
                out = {k: res.get(k) for k in ("login_id", "status", "detail", "account_id", "qr_url", "reason_code",
                                               "retry_after_sec") if k in res}
                out["login_id"] = lid
                out["instance"] = inst.get("name")
                out["platform"] = platform
                out["qr_data_url"] = safe_qr_data_url(res.get("qr_image"))
                return STATUS_DONE, out, str(res.get("status") or "ok")
            if kind == TASK_STOP_ACCOUNT:
                inst = self._pick_instance(task, prefer_domain="player_care")
                phone = str(target.get("phone") or "")
                if inst is None or not phone:
                    return STATUS_REJECTED, {}, "no_instance_or_phone"
                body = {"kind": "stop", "phone": phone, "account": str(target.get("account") or ""),
                        "text": str(payload.get("reason") or "fleet stop")}
                res = self._local(inst, "POST", "/api/player-care/commands", body)
                return STATUS_DONE, {"instance": inst.get("name"), "command": res.get("command")}, "stop_enqueued"
            if kind == TASK_RESTART_INSTANCE:
                inst = self._pick_instance(task, allow_health=True)
                if inst is not None and _health_only(inst):
                    return STATUS_REJECTED, {}, "health_only"
                cmd = str((inst or {}).get("restart_cmd") or "")
                if inst is None or not cmd:
                    return STATUS_REJECTED, {}, "no_restart_cmd"
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
                st = STATUS_DONE if proc.returncode == 0 else STATUS_FAILED
                return st, {"instance": inst.get("name"), "returncode": proc.returncode,
                            "stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}, f"rc={proc.returncode}"
            if kind == TASK_UPGRADE:
                status, result, detail = apply_upgrade(payload, self.cfg.state_dir)
                if status == STATUS_DONE:
                    self.exit_requested = True
                return status, result, detail
            if kind == TASK_PUSH_CONFIG:
                return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
            return STATUS_REJECTED, {}, f"unknown_kind:{kind}"
        except Exception as e:
            logger.warning("[agent] task %s %s failed: %s", task.get("task_id"), kind, e)
            return STATUS_FAILED, {"error": str(e)[:300]}, "exception"

    # ── 主循环 ──
    def run_once(self, *, wait: int = 0) -> Dict[str, Any]:
        """一轮：心跳 → 领任务 → 执行 → ack。返回本轮摘要。抛 Unauthorized 表示被吊销。"""
        hb = self.heartbeat()
        tasks = self.pull(wait=wait) if (hb.get("has_tasks") or wait) else []
        handled = []
        for t in tasks:
            status, result, detail = self.execute(t)
            self.stats["tasks_done" if status == STATUS_DONE else ("tasks_rejected" if status == STATUS_REJECTED else "tasks_failed")] += 1
            try:
                self.ack(str(t.get("task_id")), status, result, detail)
            except Unauthorized:
                raise
            except Exception as e:
                logger.warning("[agent] ack %s failed: %s", t.get("task_id"), e)
            handled.append({"task_id": t.get("task_id"), "kind": t.get("kind"), "status": status, "detail": detail})
            if self.exit_requested:
                break
        return {"heartbeat": {k: hb.get(k) for k in ("ok", "has_tasks", "server_proto")}, "tasks": handled}

    def run_forever(self, stop: Optional[threading.Event] = None, *, sleep: Callable[[float], None] = time.sleep) -> None:
        stop = stop or threading.Event()
        backoff = BACKOFF_MIN
        next_hb = 0.0
        while not stop.is_set():
            try:
                now = self.clock()
                if now >= next_hb:
                    self.heartbeat()
                    next_hb = now + self.cfg.heartbeat_sec
                tasks = self.pull(wait=min(MAX_LONGPOLL_WAIT_SEC, max(1, int(next_hb - self.clock()))))
                for t in tasks:
                    status, result, detail = self.execute(t)
                    self.ack(str(t.get("task_id")), status, result, detail)
                    if self.exit_requested:
                        logger.info("[agent] 升级就位，退出让服务层重启")
                        return
                backoff = BACKOFF_MIN
                self.last_error = ""
            except Unauthorized as e:
                self.revoked = True
                self.last_error = f"unauthorized: {e}"
                logger.error("[agent] node_key 被拒（已吊销？）——停止轮询，等待重新 enroll")
                return
            except Exception as e:
                self.stats["errors"] += 1
                self.last_error = str(e)[:300]
                logger.warning("[agent] loop error: %s (retry in %.0fs)", e, backoff)
                sleep(backoff)
                backoff = min(BACKOFF_MAX, backoff * 2)


# ── 摘要裁剪（只留数字 / 状态） ──────────────────────────────────────────────
def reduce_fleet_health(fh: Any, *, with_accounts: bool = False) -> Dict[str, Any]:
    fh = fh if isinstance(fh, dict) else {}
    lifecycle = fh.get("lifecycle") if isinstance(fh.get("lifecycle"), dict) else {}
    accounts = fh.get("accounts") if isinstance(fh.get("accounts"), list) else []
    total = int(fh.get("total") or len(accounts) or 0)
    online = sum(1 for a in accounts if isinstance(a, dict) and str(a.get("stage") or "") in ("active", "warming"))
    out: Dict[str, Any] = {"total": total, "online": online, "lifecycle": {str(k): _int(v) for k, v in lifecycle.items()}}
    fleet = fh.get("fleet")
    if isinstance(fleet, dict):
        out["fleet"] = {k: v for k, v in fleet.items() if isinstance(v, (int, float, str, bool)) or k in ("by_state", "counts")}
    if with_accounts:
        out["accounts"] = [{"platform": a.get("platform"), "account_id": a.get("account_id"), "stage": a.get("stage"),
                            "quota": a.get("quota"), "profile_churn_7d": a.get("profile_churn_7d")}
                           for a in accounts if isinstance(a, dict)]
    return out


def reduce_player_overview(ov: Any, *, full: bool = False) -> Dict[str, Any]:
    ov = ov if isinstance(ov, dict) else {}
    if full:
        return {k: v for k, v in ov.items() if k != "ok"}
    c = ov.get("contacts") if isinstance(ov.get("contacts"), dict) else {}
    td = ov.get("today") if isinstance(ov.get("today"), dict) else {}
    gw = ov.get("gateway") if isinstance(ov.get("gateway"), dict) else {}
    return {"contacts": _int(c.get("total")), "active_7d": _int(c.get("active_7d")), "inbound_today": _int(td.get("inbound")),
            "visible_today": _int(td.get("visible")), "gate_hits_today": _int(td.get("gate_hits")),
            "gateway": str(gw.get("status") or "")}


def _metrics() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import psutil  # 可选

        out["cpu_pct"] = psutil.cpu_percent(interval=None)
        out["mem_pct"] = psutil.virtual_memory().percent
    except Exception:
        pass
    try:
        import shutil

        du = shutil.disk_usage(str(default_state_dir().anchor or "/"))
        out["disk_free_gb"] = round(du.free / 1e9, 1)
    except Exception:
        pass
    return out


def _detect_app_version() -> str:
    for p in (Path("desktop/package.json"), Path(__file__).resolve().parents[2] / "desktop" / "package.json"):
        try:
            return str(json.loads(p.read_text(encoding="utf-8")).get("version") or "")
        except Exception:
            continue
    return ""


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _load_room_key(cfg: AgentConfig, raw: Path) -> str:
    """Read a room key. The owner check applies only inside the state directory.

    An operator path such as ``.\\room.key`` is owned by the user who launched
    the installer. Copy it in after the directory is locked, then check the
    copy. The source is removed only after that copy is accepted.
    """
    raw = Path(raw)
    state = cfg.state_dir.resolve()
    try:
        inside = raw.resolve().is_relative_to(state)
    except OSError:
        inside = False
    if inside:
        target = raw
    else:
        text = raw.read_text(encoding="utf-8")
        lock_state_dir(cfg.state_dir)
        target = cfg.state_dir / "room.key"
        target.write_text(text, encoding="utf-8")
        assign_owner_admins(target)
    if discard_untrusted_secret(target):
        raise StateDirLockError("untrusted room key file")
    room = target.read_text(encoding="utf-8").strip()
    try:
        target.unlink()
    except OSError:
        pass
    if not inside:
        try:
            raw.unlink()
        except OSError:
            pass
    return room


# ── CLI ─────────────────────────────────────────────────────────────────────
def _parse_instance(spec: str) -> Tuple[str, str]:
    if "=" not in spec:
        raise SystemExit(f"--instance 格式 name=http://host:port，收到 {spec!r}")
    name, url = spec.split("=", 1)
    return name.strip(), url.strip()


def main(argv: Optional[List[str]] = None) -> int:
    held: List[Any] = []
    try:
        return _main(argv, held)
    finally:
        for guard in held:
            guard.release()


def _main(argv: Optional[List[str]], held: List[Any]) -> int:
    ap = argparse.ArgumentParser(prog="chatx-agent", description="智控节点 Agent")
    ap.add_argument("--state-dir", default="", help="状态目录（默认 %%ProgramData%%\\ChatX\\fleet）")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll", help="接入主控：注册码 / 机房密钥 / 无码待批准")
    e.add_argument("--controller", default="", help="主控地址，如 https://bd2026.cc/fleet")
    e.add_argument("--code", default="", help="一次性注册码；留空则待管理员批准")
    e.add_argument("--room-key-file", default="", help="只含机房密钥的文件，读完即删")
    e.add_argument("--detect", action="store_true", help="登记前探测本机智聊与幻颜；什么都没有也继续（纯心跳）")
    e.add_argument("--instance", action="append", default=[], help="本机实例 name=http://127.0.0.1:18797（可多次）")
    e.add_argument("--auth-token", default="", help="实例 web_admin.auth_token（或用 --config-path）")
    e.add_argument("--config-path", default="", help="实例 config.yaml 路径（运行时读 auth_token / domain）")
    a = sub.add_parser("add-instance", help="登记本机实例")
    a.add_argument("spec", help="name=http://127.0.0.1:18797")
    a.add_argument("--auth-token", default="")
    a.add_argument("--config-path", default="")
    a.add_argument("--domain", default="")
    a.add_argument("--restart-cmd", default="")
    a.add_argument("--allow-live-port", action="store_true",
                   help="register a live-stream port (7910/7916/7920/8000/8080/8766/9000) on purpose")
    rm = sub.add_parser("remove-instance", help="remove a local instance from agent.json")
    rm.add_argument("name", help="instance name, as shown by status")
    r = sub.add_parser("run", help="常驻：心跳 + 领任务")
    r.add_argument("--once", action="store_true")
    r.add_argument("--wait", type=int, default=0, help="--once 时长轮询秒数")
    r.add_argument("--service", action="store_true", help="监督模式：未注册/被吊销/异常都不退出（计划任务 / systemd 用）")
    r.add_argument("--log-file", default="", help="日志文件（默认 <state_dir>/logs/agent.log，--service 时自动启用）")
    sub.add_parser("install-service", help="开机自启 + 立即启动（Windows 计划任务 SYSTEM / Linux systemd）")
    sub.add_parser("uninstall-service")
    sub.add_parser("service-status")
    sub.add_parser("status")
    ui = sub.add_parser("ui", help="打开本机「舰队节点」页面（仅监听 127.0.0.1）")
    ui.add_argument("--port", type=int, default=0, help="0 表示使用面板固定端口")
    ui.add_argument("--no-browser", action="store_true", help="只启动页面，不打开浏览器")
    sub.add_parser("heartbeat", help="只发一次心跳并打印")
    mig = sub.add_parser("migrate-legacy", help="rewrite an unlocked agent.json down to identity fields")
    mig.add_argument("--controller", default="", help="controller URL written into agent.json")
    mig.add_argument("--snapshot", default="", help="agent.json copied before the directory was locked")
    sub.add_parser("detect", help="rediscover local instances; does not enroll")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        cfg = AgentConfig(Path(args.state_dir) if args.state_dir else None)
    except StateDirLockError:
        print("Could not lock the fleet state directory. Run as Administrator.", file=sys.stderr)
        return 1
    if args.cmd == "migrate-legacy":
        snap = Path(args.snapshot) if args.snapshot else None
        try:
            try:
                source = json.loads(snap.read_text(encoding="utf-8")) if snap is not None else None
            except Exception:
                print("could not read the migration snapshot", file=sys.stderr)
                return 1
            if not isinstance(source, dict):
                print("could not read the migration snapshot", file=sys.stderr)
                return 1
            cfg._legacy_source = None
            cfg.data = migrated_agent_data(args.controller or cfg.controller_url, source)
            try:
                cfg.save()
            except StateDirLockError:
                print("Could not lock the fleet state directory. Run as Administrator.", file=sys.stderr)
                return 1
            print(json.dumps({"ok": True}))
            return 0
        finally:
            if snap is not None:
                try:
                    snap.unlink()
                except OSError:
                    pass
    want_log = args.cmd == "run" and bool(getattr(args, "service", False) or getattr(args, "log_file", ""))
    log_file = str(getattr(args, "log_file", "") or "")
    log_path = Path(log_file) if log_file else cfg.state_dir / "logs" / "agent.log"
    # An open log file inside an unlocked fleet dir blocks its rename on Windows
    # (WinError 5), so the log is attached only after the migration (issue f).
    defer_log = bool(want_log) and cfg.migrating
    if want_log and not defer_log:
        _attach_file_log(log_path)
    if args.cmd == "install-service":
        res = install_service(cfg.state_dir)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "uninstall-service":
        res = uninstall_service()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "service-status":
        print(json.dumps(service_status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run" and not args.once:
        # One run per state dir (issue g3). Held until main() returns.
        guard = acquire_single_instance(cfg.state_dir)
        if guard is None:
            print("Another chatx-agent run is already active for this state directory; exiting.",
                  file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        held.append(guard)
    try:
        agent = NodeAgent(cfg)
    except StateDirLockError as e:
        _note_migration_failure(cfg.state_dir, e)
        print(f"Could not lock the fleet state directory. Run as Administrator. ({e})", file=sys.stderr)
        return 1
    if defer_log:
        _attach_file_log(log_path)
    if args.cmd == "detect":
        found = agent._detect_and_add()
        print(json.dumps({"ok": True, "count": len(found)}))
        return 0
    if args.cmd == "enroll":
        for spec in args.instance:
            name, url = _parse_instance(spec)
            cfg.add_instance(name, url, auth_token=args.auth_token, config_path=args.config_path)
        room = ""
        if args.room_key_file:
            try:
                room = _load_room_key(cfg, Path(args.room_key_file))
            except StateDirLockError as e:
                if str(e).startswith("untrusted"):
                    print("room key file owner is not trusted; refusing to use it", file=sys.stderr)
                else:
                    print(f"room key file refused: {e}", file=sys.stderr)
                return 1
            except OSError as e:
                print(f"room key file unreadable: {e}", file=sys.stderr)
                return 1
        try:
            res = agent.enroll(args.code, controller_url=args.controller or cfg.controller_url,
                               room_key=room, detect=bool(args.detect))
        except AgentError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "machine_id": agent.machine_id, **res, "state_dir": str(cfg.state_dir)},
                         ensure_ascii=False))
        return 0
    if args.cmd == "add-instance":
        name, url = _parse_instance(args.spec)
        try:
            cfg.add_instance(name, url, auth_token=args.auth_token, config_path=args.config_path, domain=args.domain,
                             restart_cmd=args.restart_cmd, allow_live_port=bool(args.allow_live_port))
        except AgentError as e:
            print(str(e), file=sys.stderr)
            return 1
        cfg.save()
        print(json.dumps({"ok": True, "instances": [i["name"] for i in cfg.instances]}, ensure_ascii=False))
        return 0
    if args.cmd == "remove-instance":
        removed = cfg.remove_instance(str(args.name))
        if removed:
            cfg.save()
        else:
            print(f"no instance named {args.name!r}", file=sys.stderr)
        print(json.dumps({"ok": removed, "removed": str(args.name) if removed else "",
                          "instances": [i.get("name") for i in cfg.instances]}, ensure_ascii=False))
        return 0 if removed else 1
    if args.cmd == "status":
        snap = build_local_status(cfg, machine_id=agent.machine_id)
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "ui":
        from .panel import PANEL_PORT, run_panel
        port = int(args.port or 0) or PANEL_PORT
        return run_panel(cfg, machine_id=agent.machine_id, port=port, open_browser=not args.no_browser)
    if args.cmd == "heartbeat":
        print(json.dumps(agent.heartbeat(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run":
        if not args.once:
            from .panel import start_panel_background
            start_panel_background(cfg, agent.machine_id)
        if args.service:
            stop = threading.Event()
            try:
                return supervise(lambda: NodeAgent(AgentConfig(cfg.state_dir)), stop)
            except KeyboardInterrupt:
                stop.set()
                return 0
        if args.once:
            print(json.dumps(agent.run_once(wait=args.wait), ensure_ascii=False, indent=2))
            return 0
        stop = threading.Event()
        try:
            agent.run_forever(stop)
        except KeyboardInterrupt:
            stop.set()
        return 2 if agent.revoked else 0
    return 1


MIGRATE_LOG_NAME = "fleet-migrate.log"
EXIT_ALREADY_RUNNING = 4


def _note_migration_failure(state_dir: Path, err: BaseException) -> None:
    """One line next to the state dir. The dir itself may be half-moved. No secrets."""
    try:
        path = Path(state_dir).parent / MIGRATE_LOG_NAME
        if _is_reparse(path.parent) or _is_reparse(path):
            return
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} agent {AGENT_VERSION} state dir lock failed: {str(err)[:300]}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.debug("[agent] could not write lock-failure log: %s", e)


def _attach_file_log(path: Path) -> None:
    from logging.handlers import RotatingFileHandler

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(str(path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
    except Exception:
        logger.warning("日志文件不可写 %s", path, exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
