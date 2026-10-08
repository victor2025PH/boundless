"""chatx-agent —— 装在每台受控电脑上的节点端（只用标准库，可单独打包）。

    python -m src.fleet.agent enroll --controller https://bd2026.cc/fleet --code 12345678 \\
        --instance player=http://127.0.0.1:18797 --config-path config_player/config.yaml
    python -m src.fleet.agent run            # 前台常驻：心跳 + 长轮询领任务 + 执行 + ack
    python -m src.fleet.agent run --once     # 跑一轮就退（联调 / 计划任务）
    python -m src.fleet.agent status         # 本机摘要（含主控是否可达、计划任务、最近心跳）
    python -m src.fleet.agent ui             # 打开本机「智拓群控节点」页（只监听 127.0.0.1）
    python -m src.fleet.agent install-service   # 开机自启（Windows 计划任务 SYSTEM / Linux systemd）+ 立即启动
    python -m src.fleet.agent run --service     # 服务实际入口：监督循环（见 service.py）

流程（契约 docs/FLEET_CONTROL_CONTRACT.md）：
    enroll(code, machine_id) → node_key 存 <state_dir>/agent.json（只在本机）
    loop: heartbeat（本机实例摘要 + 本机 adb 手机清点 phones，无聊天原文）→ pull(wait=25s 长轮询) → 逐条 execute → ack（幂等）
    0.3.15 本机操作员告警（默认关）：手机不能干活或本机 adb 异常时，只在这台电脑上弹窗，不进心跳。
    0.3.16 push_config 可以写 operator_alert_* / wallpaper_map（直播机仍拒绝，告警保持关闭）。
    0.3.17 Facebook 点赞在截图上找赞（模板 + 动作条 + 可选文字），不再点固定 like_button。
    0.3.18 net_health 只读体检：上网、WiFi/移动数据、SIM 与信号、流量计数、Facebook 是否安装。
           剩余话费/流量没有稳定接口，默认不查。公开 latest 仍是 0.3.7。
    0.3.18 只有图标的动作条也能定位赞（结构 + 模板/轮廓，不靠文字）。打开 Facebook 会轮询约 9 秒，并把信息流拉回顶部再找赞。只读 uiautomator dump 的 Like/赞/React 标签要和模板一致才点。
    0.3.19 Facebook 点赞/评论/关注/发帖按账号限速（compliance.yaml）。超出上限、间隔太短或不在活跃时段就跳过。like_probe 不计数。金丝雀版本是 0.3.19。
    0.3.20 like_probe 带回每个信号的诊断（无障碍标签、动作条属性、模板分、结构是否命中），只含节点文字属性，不含截图、不含序列号。图标动作条放宽标签 / resource-id / 最左按钮，仍要两个信号一致并且点完复核。手机任务的错误文字在回执前去掉原始序列号，改成壁纸号或打码。公开 latest 仍是 0.3.7。
    0.3.22 机房现场待办：主控按类别下发 site_todo，只在这台电脑弹窗（中英文）。直播机拒绝。公开 latest 仍是 0.3.7。
    任何一步失败：指数退避（2s → 60s），不崩、不丢 node_key；401 → 标记 revoked 停止（等重新注册）。
    machine_id 换了（克隆盘 / 主控报冲突）→ 丢掉旧 node_key，以新 machine_id 重新登记待批准，绝不顶掉别的电脑。

任务执行只调本机智聊实例已有的 HTTP API（Bearer web_admin.auth_token），不直接碰数据库：
    ping            → 本地回 pong
    pull_overview   → GET  /api/player-care/overview
    account_health  → GET  /api/accounts/fleet-health（裁成 total / lifecycle / fleet / 每号 stage+quota）
    login_qr        → POST /api/platforms/{platform}/login/start → {login_id, qr_image, ...}
    login_status    → GET  /api/platforms/{platform}/login/{login_id}/status
    stop_account    → POST /api/player-care/commands {kind: stop, account, phone}
                      + 本机 huoke 实例（domain=huoke）→ POST /outreach/stop-account（X-API-Key；号码/账号进 STOP 表、暂停设备触达）
    restart_instance→ 实例条目配了 restart_cmd 才执行，否则 rejected
    upgrade         → 下载+sha256 校验+换文件后重启（updater.py；仅冻结 exe，源码态拒绝）
                      带 setup_url + setup_sha256 时改为校验后静默跑安装包（直播机拒绝）
    enable_phone_adb→ 已有自带 platform-tools 时打开 adb_manage_server 并拉起 adb（直播机拒绝）
    push_config     → phone_flows_enabled true/false, and/or phone_ui_map (existing file) or
                      phone_ui_map_json / phone_ui_map_b64 (written under the state dir, 256KiB,
                      validate_ui_map), and/or operator_alert_enabled / operator_alert_refresh_sec /
                      operator_alert_language / operator_alert_fail_streak / wallpaper_map.
                      Other patches rejected not_supported_in_agent_v1.
                      Live-stream host refused before any write. Map body and wallpaper serials are not logged.
    operator_alert_diag → read-only redacted snapshot (wallpaper number + reason). No adb serials.
    net_health      → read-only per-phone reachability, transport, SIM/signal, usage, Facebook
                      installed/login. Remaining carrier data is not available by default.
                      Optional USSD is off unless net_health_ussd_enabled is JSON true.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import os
import re
import secrets
import stat
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
    LIVE_STREAM_PORTS, detect_instances, is_blocked_live_port, is_live_port_url, is_live_stream_host, is_loopback_url,
    sanitize_instances, url_port,
)
from .identity import (
    ORIGIN_GENERATED, StateDirLockError, _is_reparse, assign_owner_admins, default_state_dir,
    discard_untrusted_secret, host_name, identity_meta, lock_state_dir, node_machine_id, os_label,
    regenerate_machine_id, resolve_machine_identity, short_machine_id, state_dir_is_locked,
)
from .local_status import build_local_status, record_heartbeat
from .operator_alert import OperatorAlert, operator_alert_diag, parse_wallpaper_map
from .phones import PhoneCollector
from .phone_flow_robust import parse_jitter_ms
from .phone_flows import PhoneFlows, _MAX_MAP_BYTES, validate_ui_map
from .phone_rules import PhoneOpError, scrub_phone_error_text, scrub_phone_tree
from .phone_ops import PHONE_TASK_KINDS, PhoneOps
from .service import (
    acquire_single_instance, install_service, service_status, start_parent_watch, supervise, uninstall_service,
)
from .updater import apply_upgrade
from .protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2, CAP_PHONE_OPS_V1, DEFAULT_HEARTBEAT_SEC, MAX_LONGPOLL_WAIT_SEC,
    PHONE_FLOW_KINDS, PHONE_SESSION_KINDS,
    PROTO_VERSION, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_ACCOUNT_HEALTH, TASK_LOGIN_QR, TASK_LOGIN_STATUS, TASK_PING, TASK_PULL_OVERVIEW, TASK_PUSH_CONFIG,
    TASK_ENABLE_PHONE_ADB, TASK_NET_HEALTH, TASK_OPERATOR_ALERT_DIAG, TASK_RESTART_INSTANCE, TASK_SITE_TODO,
    TASK_STOP_ACCOUNT, TASK_UPGRADE,
)

logger = logging.getLogger("fleet.agent")

AGENT_VERSION = "0.3.22"
# push_config may set these and nothing else. Map content keys are not stored;
# they become a file under the state dir and phone_ui_map is set to that path.
# Operator-alert keys are stored as agent.json operator keys (hot-reloaded).
_PUSH_CONFIG_KEYS = frozenset({
    "phone_flows_enabled", "phone_ui_map", "phone_ui_map_b64", "phone_ui_map_json",
    "operator_alert_enabled", "operator_alert_refresh_sec", "operator_alert_language",
    "operator_alert_fail_streak", "wallpaper_map",
})
_PUSH_OPERATOR_ALERT_KEYS = frozenset({
    "operator_alert_enabled", "operator_alert_refresh_sec", "operator_alert_language",
    "operator_alert_fail_streak", "wallpaper_map",
})
_ALERT_LANGS = frozenset({"zh", "en"})
_WALLPAPER_MAP_MAX_BYTES = 64 * 1024
_PUSH_MAP_CONTENT_KEYS = frozenset({"phone_ui_map_b64", "phone_ui_map_json"})
_REMOTE_UI_MAP_NAME = "phone_ui_map.remote.json"
_UI_MAP_PATH_MAX = 1024
CONFIG_NAME = "agent.json"
HTTP_TIMEOUT = 15
LOCAL_TIMEOUT = 8
BACKOFF_MIN, BACKOFF_MAX = 2.0, 60.0
REENROLL_BACKOFF_SEC = 30
ENV_CONTROLLER = "CHATX_FLEET_CONTROLLER"
HUOKE_DOMAIN = "huoke"            # 本机获客（手机群控）实例：stop_account 同步落到它的 STOP 表 / 设备暂停
HUOKE_STOP_PATH = "/outreach/stop-account"
HUOKE_HEALTH_PATH = "/health"

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


class IdentityConflict(AgentError):
    """The controller says this machine_id belongs to another PC (HTTP 409 / conflict error)."""


# Error strings a controller may use for "machine_id already held by different hardware".
# The current controller never sends them; 0.3.5 agents already react if a later one does.
IDENTITY_CONFLICT_ERRORS = frozenset({
    "machine_id_conflict", "machine_id_in_use", "duplicate_machine", "hw_fingerprint_mismatch",
})
# Enrollment state that belongs to one machine_id and is dropped when the id changes.
_ENROLLMENT_FIELDS = ("node_id", "node_key", "pending_request_id", "pairing_code", "enroll_rejected")


def _is_identity_conflict(code: int, data: Dict[str, Any]) -> bool:
    if code == 409:
        return True
    if code < 400 or not isinstance(data, dict):
        return False
    err = data.get("error") or data.get("detail") or ""
    if isinstance(err, dict):
        err = err.get("error") or ""
    return str(err) in IDENTITY_CONFLICT_ERRORS


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
        domain = raw.get("domain")
        if (not name or name in seen or not is_loopback_url(url)
                or is_blocked_live_port(url, domain if isinstance(domain, str) else "")):
            continue
        tok = raw.get("auth_token")
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

    # 只由人手改的键（agent 自己从不写）：save() 以磁盘上的为准，运行中的 agent 不会用内存旧值
    # 盖掉手工改动；NodeAgent 每次心跳前按 mtime 热加载（2026-10-06 176 改 phones_exclude 需重启的教训）。
    OPERATOR_KEYS = ("phones_exclude", "phones_enabled", "adb_path", "phone_ops_enabled", "phone_ops_allow_tcp",
                     "adb_manage_server", "phone_flows_enabled", "phone_ui_map",
                     "phone_flow_verify", "phone_flow_jitter_ms",
                     "operator_alert_enabled", "operator_alert_refresh_sec", "operator_alert_language",
                     "operator_alert_fail_streak", "wallpaper_map",
                     "net_health_ussd_enabled", "net_health_ussd_codes")

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

    def _read_trusted_disk(self) -> Optional[Dict[str, Any]]:
        """Same trust rule as load(): only a locked state dir, no reparse points."""
        try:
            if (not self.path.is_file() or _is_reparse(self.state_dir) or _is_reparse(self.path)
                    or not state_dir_is_locked(self.state_dir)):
                return None
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001 - unreadable edit: keep the in-memory config
            logger.debug("[agent] agent.json not re-read: %s", e)
            return None
        return d if isinstance(d, dict) else None

    def refresh_operator_keys(self) -> bool:
        """把磁盘上的 OPERATOR_KEYS 抄进内存（磁盘上删掉的键内存里也删）。有变化 → True。"""
        if self._legacy_source is not None:
            return False
        disk = self._read_trusted_disk()
        if disk is None:
            return False
        changed = False
        for k in self.OPERATOR_KEYS:
            if k in disk:
                if k not in self.data or self.data[k] != disk[k]:
                    self.data[k] = disk[k]
                    changed = True
            elif k in self.data:
                del self.data[k]
                changed = True
        return changed

    def write_operator_keys(self, updates: Dict[str, Any]) -> None:
        """Persist operator settings. Other operator keys already on disk are kept."""
        bad = [k for k in updates if k not in self.OPERATOR_KEYS]
        if bad:
            raise AgentError(f"not an operator setting: {bad[0]}")
        if self._legacy_source is not None:
            raise AgentError("state directory is not locked yet")
        disk = self._read_trusted_disk()
        if isinstance(disk, dict):
            for k in self.OPERATOR_KEYS:
                if k in updates:
                    continue
                if k in disk:
                    self.data[k] = disk[k]
                elif k in self.data:
                    del self.data[k]
        for k, v in updates.items():
            self.data[k] = v
        self._write_locked()

    def save(self) -> None:
        if self._legacy_source is None:
            self.refresh_operator_keys()
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
        if is_blocked_live_port(base_url, domain, self.state_dir) and not allow_live_port:
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
        self.identity_reset = self._bind_identity()
        self.started_at = clock()
        self.revoked = False
        self.exit_requested = False   # upgrade 换文件：ack 后由循环退出，服务层重新拉起
        self.last_error = ""
        self.stats = {"heartbeats": 0, "tasks_done": 0, "tasks_failed": 0, "tasks_rejected": 0, "errors": 0}
        # 0.3.6 只读手机清点：只跑 adb devices -l；agent.json 可配 phones_exclude / adb_path / phones_enabled
        # 0.3.8 adb_manage_server（默认关）才允许用安装目录里的 adb 把本机 server 拉起来；直播机除外
        self.phones = PhoneCollector(clock=clock, **_phone_collector_settings(cfg.data, cfg.state_dir))
        # 0.3.7 远程手机操作：主控只给心跳里声明了 phone_ops_v1 的节点排 phone_* 任务；
        # agent.json phone_ops_enabled=false 关掉（不声明能力、全部拒绝），phone_ops_allow_tcp=true 才操作无线手机
        self.phone_ops = PhoneOps(**_phone_ops_settings(cfg.data, cfg.state_dir))
        # 0.3.8 社交动作缺省关：只有 agent.json phone_flows_enabled=true 才声明 phone_flows_v1
        # 0.3.14: pass state_dir by name. A loose **dict into an older __init__
        # crashed startup with unexpected keyword argument 'state_dir'.
        self.phone_flows = PhoneFlows.from_agent_settings(
            _phone_flows_settings(cfg.data, cfg.state_dir), self.phone_ops)
        # 0.3.15 local desktop alert. Off unless operator_alert_enabled is JSON true.
        # A live-stream host never raises it, so node 176 and the protected phone stay as they are.
        self.operator_alert = OperatorAlert(
            cfg.state_dir, clock=clock, live_stream=is_live_stream_host(cfg.state_dir))
        self._cfg_mtime = _mtime_ns(cfg.path)

    def reload_operator_config(self) -> bool:
        """agent.json 被改过（mtime 变了）→ 重读手机相关键并重建清点 / 操作设置，不用重启。"""
        mtime = _mtime_ns(self.cfg.path)
        if mtime == self._cfg_mtime:
            return False
        self._cfg_mtime = mtime
        if not self.cfg.refresh_operator_keys():
            return False
        d = self.cfg.data
        self.phones = PhoneCollector(clock=self.clock, **_phone_collector_settings(d, self.cfg.state_dir))
        self.phone_ops.configure(**_phone_ops_settings(d, self.cfg.state_dir))
        self.phone_flows.configure(**_phone_flows_settings(d, self.cfg.state_dir), ops=self.phone_ops)
        logger.info("[agent] agent.json 手机设置已热加载：phones_exclude %d 条，phone_ops %s，phone_flows %s，adb_manage_server %s，operator_alert %s",
                    len(d.get("phones_exclude") or []), "on" if self.phone_ops.enabled else "off",
                    "on" if self.phone_flows.enabled else "off",
                    "on" if self.phone_ops.manage_server else "off",
                    "on" if d.get("operator_alert_enabled") is True else "off")
        return True

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
        if _is_identity_conflict(code, data):
            raise IdentityConflict(str(data.get("error") or data.get("detail") or "machine_id_conflict")[:200])
        if code >= 400:
            raise AgentError(f"{path} → HTTP {code}: {data.get('detail') or data}")
        return data

    # ── 机器标识与注册绑定 ──
    def _bind_identity(self) -> bool:
        """Keep agent.json's enrollment only for the machine_id it was made under.

        Returns True when an old enrollment was dropped. That happens when the
        id changed (cloned disk caught by the hardware fingerprint, installer
        re-derived a 0.3.4 cache, or a controller conflict). The node_key of the
        old id is never reused, so this PC cannot keep heartbeating as, or
        rotating the key of, another PC's node. A new pending request follows.
        """
        data = self.cfg.data
        mid = self.machine_id
        bound = str(data.get("machine_id") or "")
        if bound == mid:
            return False
        has_key = bool(self.cfg.node_key)
        if not has_key and not (bound and data.get("pending_request_id")):
            # Nothing that belongs to another id. (An unbound pending request filed
            # under an older id is answered "unknown" by the controller and restarts.)
            data["machine_id"] = mid  # saved with the next enrollment
            return False
        meta: Dict[str, Any] = {}
        stale = bool(bound)
        if not stale:
            # 0.3.4 agent.json has no binding. Keep it when the id itself was kept
            # (adopted legacy cache); drop it when the id was freshly generated.
            meta = identity_meta(self.cfg.state_dir)
            stale = meta.get("machine_id") == mid and meta.get("origin") == ORIGIN_GENERATED
        if not stale:
            data["machine_id"] = mid
            try:
                self.cfg.save()
            except Exception:  # opportunistic: the next real save reports lock problems
                logger.debug("[agent] machine_id binding not saved", exc_info=True)
            return False
        prev = bound or str(meta.get("previous_machine_id") or "")
        logger.warning("[agent] machine_id changed %s -> %s: old enrollment dropped, requesting approval again",
                       prev or "?", mid)
        for key in _ENROLLMENT_FIELDS:
            data.pop(key, None)
        data["machine_id"] = mid
        if prev:
            data["previous_machine_id"] = prev
        data["reenroll_not_before"] = 1.0  # service loop re-requests approval right away
        self.cfg.save()
        try:
            (self.cfg.state_dir / "pairing.txt").unlink()
        except OSError:
            pass
        return True

    def _identity_meta_for_enroll(self) -> Dict[str, Any]:
        try:
            meta = identity_meta(self.cfg.state_dir)
        except Exception:
            meta = {}
        out: Dict[str, Any] = {"python": _platform.python_version(), "id_scheme": 2}
        if meta.get("machine_id") == self.machine_id:
            for src, dst in (("hw_fp", "hw_fp"), ("source", "id_source"), ("origin", "id_origin")):
                if meta.get(src):
                    out[dst] = str(meta[src])[:40]
        prev = str(self.cfg.data.get("previous_machine_id") or "")
        if prev:
            out["previous_machine_id"] = prev[:40]
        return out

    def handle_identity_conflict(self, why: str = "") -> Dict[str, Any]:
        """Controller conflict: new hardware-bound id, drop the old key, ask for approval (never rotate)."""
        old = self.machine_id
        self.machine_id = regenerate_machine_id(self.cfg.state_dir, reason="conflict")
        logger.warning("[agent] controller reported machine_id conflict (%s): %s -> %s", why or "-", old, self.machine_id)
        if str(self.cfg.data.get("machine_id") or "") != old:
            self.cfg.data["machine_id"] = old
        self.identity_reset = self._bind_identity() or self.identity_reset
        return {"machine_id": self.machine_id, "previous": old}

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
            "machine_id": self.machine_id,
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
               detect: bool = False, _after_conflict: bool = False) -> Dict[str, Any]:
        """有注册码或机房密钥则立刻拿到 node_key；都没有则登记为待批准（不抛错）。

        主控回 machine_id 冲突时：换新的硬件 machine_id，只以待批准重登一次（不带码 / 密钥，
        不会换掉另一台电脑的 key）。
        """
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
            "meta": self._identity_meta_for_enroll(),
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
        try:
            res = self._ctrl("POST", "/api/fleet/enroll", body, auth=False, bearer=bearer)
        except IdentityConflict as e:
            if _after_conflict:
                raise AgentError(f"machine_id conflict persists: {e}") from e
            self.handle_identity_conflict(str(e))
            return self.enroll("", _after_conflict=True)
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

    def _observe_operator_alert(self, phones: Any, phones_error: Any) -> None:
        try:
            self.operator_alert.observe(phones, phones_error, self.cfg.data, now=self.clock())
        except Exception:
            logger.warning("[agent] operator alert skipped", exc_info=True)

    def _note_phone_action(self, target: Any, result: Any, status: str) -> None:
        try:
            serial = ""
            if isinstance(result, dict):
                serial = str(result.get("serial") or "")
            if not serial and isinstance(target, dict):
                serial = str(target.get("serial") or "")
            self.operator_alert.note_action(serial, status)
        except Exception:
            logger.debug("[agent] operator alert action note failed", exc_info=True)

    def _phone_wallpaper(self, target: Any, result: Any, serial: str) -> str:
        for src in (target, result if isinstance(result, dict) else None):
            if not isinstance(src, dict):
                continue
            for key in ("wallpaper", "wallpaper_no"):
                text = str(src.get(key) or "").strip()
                if text.isdigit() and 1 <= int(text) <= 9999:
                    return text
        if not serial:
            return ""
        mapped = parse_wallpaper_map(self.cfg.data.get("wallpaper_map") if isinstance(self.cfg.data, dict) else None)
        return mapped.get(serial.strip().upper(), "")

    def _scrub_phone_report(self, target: Any, result: Any, detail: str) -> Tuple[Dict[str, Any], str]:
        """Strip the raw serial from phone-task error text before it is sent."""
        serial = ""
        if isinstance(target, dict):
            serial = str(target.get("serial") or "").strip()
        if not serial and isinstance(result, dict):
            serial = str(result.get("serial") or "").strip()
        wallpaper = self._phone_wallpaper(target, result, serial)
        detail_out = scrub_phone_error_text(str(detail or ""), serial=serial, wallpaper=wallpaper)
        if not isinstance(result, dict):
            return {}, detail_out
        cleaned = dict(result)
        for key in ("error", "stderr"):
            if isinstance(cleaned.get(key), str):
                cleaned[key] = scrub_phone_error_text(cleaned[key], serial=serial, wallpaper=wallpaper)
        if isinstance(cleaned.get("like_diag"), dict):
            cleaned["like_diag"] = scrub_phone_tree(cleaned["like_diag"], serial=serial, wallpaper=wallpaper)
        return cleaned, detail_out

    def _phone_caps(self) -> List[str]:
        """低层能力来自当前 phone_ops；社交能力只有两边都开着才加。"""
        caps = list(self.phone_ops.caps())
        if self.phone_flows.enabled and CAP_PHONE_OPS_V1 in caps:
            for cap in (CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2):
                if cap not in caps:
                    caps.append(cap)
        return caps

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
            if domain == HUOKE_DOMAIN:
                # huoke 没有智聊的 /api/accounts/fleet-health（一直 404）；探活走它自己的 /health
                try:
                    self._local(inst, "GET", HUOKE_HEALTH_PATH)
                    entry["up"] = True
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{inst.get('name')}: huoke health {e}")
                instances.append(entry)
                continue
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
        phones, phones_error = self.phones.collect()
        self._observe_operator_alert(phones, phones_error)
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
            "phones": phones,
            "phones_error": phones_error,
            "caps": self._phone_caps(),
        }

    def heartbeat(self) -> Dict[str, Any]:
        try:
            self.reload_operator_config()
        except Exception as e:  # noqa: BLE001 - a bad edit must not stop the heartbeat
            logger.warning("[agent] agent.json 热加载失败：%s", e)
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
        if (is_live_port_url(base) and not inst.get("allow_live_port")
                and is_blocked_live_port(base, _instance_domain(inst), self.cfg.state_dir)):
            # e.g. an 'avatarhub' at :9000 added by an older detect on a live machine.
            raise AgentError(f"live-stream port {url_port(base)} not probed (remove-instance {inst.get('name')})")
        headers = {}
        tok = _instance_token(inst)
        if tok:
            if _instance_domain(inst) == HUOKE_DOMAIN:
                headers["X-API-Key"] = tok          # huoke verify_api_key: X-API-Key（Bearer 只认会话 token）
            else:
                headers["Authorization"] = f"Bearer {tok}"
        code, data = self.http(method, base + path, body, headers, LOCAL_TIMEOUT)
        if code >= 400:
            raise AgentError(f"local {path} → HTTP {code}: {data.get('detail') or data}")
        return data

    def _pick_instance(self, task: Dict[str, Any], *, prefer_domain: str = "",
                       allow_health: bool = False, allow_huoke: bool = False) -> Optional[Dict[str, Any]]:
        want = str((task.get("target") or {}).get("instance") or (task.get("payload") or {}).get("instance") or "")
        insts = self.cfg.instances if allow_health else [i for i in self.cfg.instances if not _health_only(i)]
        if not want and prefer_domain != HUOKE_DOMAIN:
            insts = [i for i in insts if _instance_domain(i) != HUOKE_DOMAIN]   # 智聊任务不落到 huoke
        if want:
            for i in insts:
                if i.get("name") == want:
                    if _instance_domain(i) == HUOKE_DOMAIN and prefer_domain != HUOKE_DOMAIN and not allow_huoke:
                        # 智聊接口 + X-API-Key 绝不打到 huoke：指名 huoke 实例的智聊任务一律拒绝
                        return None
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
            if kind in PHONE_FLOW_KINDS or kind in PHONE_SESSION_KINDS:
                status, result, detail = self.phone_flows.execute(kind, payload, target, ops=self.phone_ops)
                self._note_phone_action(target, result, status)
                result, detail = self._scrub_phone_report(target, result, detail)
                return status, result, detail
            if kind in PHONE_TASK_KINDS:
                status, result, detail = self.phone_ops.execute(kind, payload, target)
                self._note_phone_action(target, result, status)
                result, detail = self._scrub_phone_report(target, result, detail)
                return status, result, detail
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
                return self._stop_account(task, target, payload)
            if kind == TASK_RESTART_INSTANCE:
                # restart_cmd 是本机命令、不走 HTTP / API key，指名 huoke 实例时照旧可重启
                inst = self._pick_instance(task, allow_health=True, allow_huoke=True)
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
            if kind == TASK_ENABLE_PHONE_ADB:
                return self._enable_phone_adb()
            if kind == TASK_PUSH_CONFIG:
                return self._push_phone_flows(payload)
            if kind == TASK_OPERATOR_ALERT_DIAG:
                return self._operator_alert_diag()
            if kind == TASK_NET_HEALTH:
                return self._net_health(target, payload)
            if kind == TASK_SITE_TODO:
                return self._site_todo(payload)
            return STATUS_REJECTED, {}, f"unknown_kind:{kind}"
        except Exception as e:
            logger.warning("[agent] task %s %s failed: %s", task.get("task_id"), kind, e)
            return STATUS_FAILED, {"error": str(e)[:300]}, "exception"

    def _enable_phone_adb(self) -> Tuple[str, Dict[str, Any], str]:
        """Set adb_manage_server and start the bundled server. Does not exit the agent.

        Any serial in the task payload is ignored. The protected phone is never addressed.
        """
        from . import adb_bundle

        already = self.cfg.data.get("adb_manage_server") is True

        def persist() -> None:
            self.cfg.write_operator_keys({"adb_manage_server": True})
            data = self.cfg.data
            self.phones = PhoneCollector(clock=self.clock, **_phone_collector_settings(data, self.cfg.state_dir))
            self.phone_ops.configure(**_phone_ops_settings(data, self.cfg.state_dir))
            self._cfg_mtime = _mtime_ns(self.cfg.path)

        return adb_bundle.enable_phone_adb(self.cfg.state_dir, persist=persist, already=already)

    def _net_health(self, target: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """Read-only network probe. Does not change adb server settings."""
        from .net_health import run_net_health

        status, result, detail = run_net_health(
            self.phones, self.cfg.data, target, payload,
            live_stream=bool(self.operator_alert.live_stream),
        )
        try:
            phones = result.get("phones") if isinstance(result, dict) else None
            self.operator_alert.note_network(phones or [], self.cfg.data)
        except Exception:
            logger.warning("[agent] net_health alert skipped", exc_info=True)
        return status, result, detail

    def _site_todo(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """Write this PC's site list and show the local panel. A live-stream host is refused first."""
        if self.operator_alert.live_stream:
            return STATUS_REJECTED, {}, "live_stream_host"
        from .site_todo import sanitize_site_todo_payload, sanitize_site_todo_result

        clean = sanitize_site_todo_payload(payload)
        self.operator_alert.apply_todos(clean.get("todos") or [], self.cfg.data)
        result = sanitize_site_todo_result({"ok": True, "count": len(clean.get("todos") or [])})
        return STATUS_DONE, result, "ok"

    def _operator_alert_diag(self) -> Tuple[str, Dict[str, Any], str]:
        """Read-only redacted operator-alert summary. No serials, no secrets."""
        summary = operator_alert_diag(
            self.cfg.state_dir, self.cfg.data, live_stream=bool(self.operator_alert.live_stream))
        return STATUS_DONE, summary, "ok"

    def _push_phone_flows(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """Write agent.json phone flow settings and/or operator-alert keys.

        A live-stream host is refused before any file is touched. Map bytes are
        checked for size and schema, then stored only as a path. The map body
        and wallpaper serials are not logged and are not returned.
        """
        patch = payload.get("patch") if isinstance(payload, dict) else None
        if not isinstance(patch, dict) or not patch or not set(patch) <= _PUSH_CONFIG_KEYS:
            return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
        content_keys = set(patch) & _PUSH_MAP_CONTENT_KEYS
        if len(content_keys) > 1 or (content_keys and "phone_ui_map" in patch):
            return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
        updates: Dict[str, Any] = {}
        if "phone_flows_enabled" in patch:
            enabled = patch.get("phone_flows_enabled")
            if not isinstance(enabled, bool):
                return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
            updates["phone_flows_enabled"] = enabled
        map_path = None
        map_body = None
        if "phone_ui_map" in patch:
            raw_path = patch.get("phone_ui_map")
            if not isinstance(raw_path, str):
                return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
            map_path = raw_path
        elif "phone_ui_map_b64" in patch:
            raw_b64 = patch.get("phone_ui_map_b64")
            if not isinstance(raw_b64, str):
                return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
            map_body = ("b64", raw_b64)
        elif "phone_ui_map_json" in patch:
            raw_json = patch.get("phone_ui_map_json")
            if not isinstance(raw_json, (str, dict)):
                return STATUS_REJECTED, {}, "not_supported_in_agent_v1"
            map_body = ("json", raw_json)
        alert_updates, alert_err = _accept_operator_alert_patch(patch)
        if alert_err:
            return STATUS_REJECTED, {}, alert_err
        updates.update(alert_updates)
        if is_live_stream_host(self.cfg.state_dir):
            return STATUS_REJECTED, {}, "live_stream_host"
        nbytes = 0
        try:
            if map_path is not None:
                checked, nbytes = _accept_ui_map_path(map_path)
                updates["phone_ui_map"] = checked
            elif map_body is not None:
                blob = _decode_ui_map_body(map_body)
                nbytes = len(blob)
                updates["phone_ui_map"] = str(_write_remote_ui_map(self.cfg.state_dir, blob))
        except _UiMapReject as e:
            return STATUS_REJECTED, {}, e.code
        self.cfg.write_operator_keys(updates)
        self.phone_flows.configure(**_phone_flows_settings(self.cfg.data, self.cfg.state_dir), ops=self.phone_ops)
        self._cfg_mtime = _mtime_ns(self.cfg.path)
        result: Dict[str, Any] = {}
        if "phone_flows_enabled" in updates:
            result["phone_flows_enabled"] = self.phone_flows.enabled
        if "phone_ui_map" in updates:
            result["phone_ui_map"] = updates["phone_ui_map"]
            result["phone_ui_map_bytes"] = nbytes
            logger.info("[agent] push_config set phone_ui_map (%d bytes)", nbytes)
        if "operator_alert_enabled" in updates:
            result["operator_alert_enabled"] = updates["operator_alert_enabled"] is True
            logger.info("[agent] push_config set operator_alert %s",
                        "on" if result["operator_alert_enabled"] else "off")
        if "operator_alert_refresh_sec" in updates:
            result["operator_alert_refresh_sec"] = updates["operator_alert_refresh_sec"]
        if "operator_alert_language" in updates:
            result["operator_alert_language"] = updates["operator_alert_language"]
        if "operator_alert_fail_streak" in updates:
            result["operator_alert_fail_streak"] = updates["operator_alert_fail_streak"]
        if "wallpaper_map" in updates:
            result["wallpaper_map_entries"] = len(parse_wallpaper_map(updates["wallpaper_map"]))
            logger.info("[agent] push_config set wallpaper_map (%d entries)", result["wallpaper_map_entries"])
        return STATUS_DONE, result, "ok"

    def _huoke_instances(self, want: str = "") -> List[Dict[str, Any]]:
        out = [i for i in self.cfg.instances if _instance_domain(i) == HUOKE_DOMAIN]
        return [i for i in out if i.get("name") == want] if want else out

    def _stop_account(self, task: Dict[str, Any], target: Dict[str, Any],
                      payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """智聊 commandbus ``stop``（原行为，需 phone）+ 本机 huoke 实例的 STOP 表 / 设备暂停。

        * ``target.instance`` 指名 huoke 实例 → 只落 huoke；指名智聊实例 → 只走智聊（原行为）。
        * 未指名 → 智聊（有 phone 且有实例时）+ 每个 huoke 实例（best-effort）；任一侧落地即 done，
          失败侧写进 result（只存状态，不存号码）。
        """
        want = str(target.get("instance") or payload.get("instance") or "")
        phone = str(target.get("phone") or "")
        account = str(target.get("account") or "")
        device_id = str(target.get("device_id") or "")
        reason = str(payload.get("reason") or "fleet stop")
        huoke = self._huoke_instances(want)
        chatx = None
        if not (want and huoke):
            chatx = self._pick_instance(task, prefer_domain="player_care")
        result: Dict[str, Any] = {}
        ok_any = False
        errors: List[str] = []
        if chatx is not None and phone:
            body = {"kind": "stop", "phone": phone, "account": account, "text": reason}
            try:
                res = self._local(chatx, "POST", "/api/player-care/commands", body)
                result.update({"instance": chatx.get("name"), "command": res.get("command")})
                ok_any = True
            except Exception as e:  # noqa: BLE001
                if not huoke:
                    raise
                errors.append(f"{chatx.get('name')}: {str(e)[:120]}")
        if huoke and (phone or account or device_id):
            hres = []
            for inst in huoke:
                body = {"phone": phone, "account": account, "device_id": device_id,
                        "reason": reason[:80], "source": "fleet"}
                try:
                    r = self._local(inst, "POST", HUOKE_STOP_PATH, body)
                    item = {"instance": inst.get("name"), "ok": bool(r.get("ok")),
                            "stopped": len(r.get("stopped") or []),
                            "paused_devices": len(r.get("paused_devices") or [])}
                    if not r.get("ok"):
                        # 200 但 ok:false：保留 huoke 给的原因，任务记 failed（不是 rejected）
                        why = str(r.get("detail") or r.get("error") or r.get("reason") or r.get("message") or "ok=false")
                        item["detail"] = why[:200]
                        errors.append(f"{inst.get('name')}: {why[:120]}")
                    hres.append(item)
                    ok_any = ok_any or bool(r.get("ok"))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{inst.get('name')}: {str(e)[:120]}")
                    hres.append({"instance": inst.get("name"), "ok": False})
            result["huoke"] = hres
        if errors:
            result["errors"] = errors
        if ok_any:
            return STATUS_DONE, result, "stop_enqueued"
        if errors:
            return STATUS_FAILED, result, "stop_failed"
        return STATUS_REJECTED, {}, "no_instance_or_phone"

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
            except IdentityConflict as e:
                self.last_error = f"machine_id conflict: {e}"
                self.handle_identity_conflict(str(e))
                if self.cfg.controller_url:
                    try:
                        self.enroll("", _after_conflict=True)
                    except AgentError as err:
                        logger.warning("[agent] pending request after conflict failed: %s", err)
                return
            except Exception as e:
                self.stats["errors"] += 1
                self.last_error = str(e)[:300]
                logger.warning("[agent] loop error: %s (retry in %.0fs)", e, backoff)
                sleep(backoff)
                backoff = min(BACKOFF_MAX, backoff * 2)


def _mtime_ns(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _phone_collector_settings(data: Dict[str, Any], state_dir: Optional[Path] = None) -> Dict[str, Any]:
    return {
        "adb_path": str(data.get("adb_path") or ""),
        "exclude": data.get("phones_exclude") or [],
        "enabled": data.get("phones_enabled", True) is not False,
        "manage_server": data.get("adb_manage_server") is True,
        "state_dir": state_dir,
    }


def _phone_ops_settings(data: Dict[str, Any], state_dir: Optional[Path] = None) -> Dict[str, Any]:
    return {
        "adb_path": str(data.get("adb_path") or ""),
        "exclude": data.get("phones_exclude") or [],
        "enabled": data.get("phone_ops_enabled", True) is not False,
        "allow_tcp": data.get("phone_ops_allow_tcp") is True,
        "manage_server": data.get("adb_manage_server") is True,
        "state_dir": state_dir,
    }


def _phone_flows_settings(data: Dict[str, Any], state_dir: Optional[Path] = None) -> Dict[str, Any]:
    raw = data.get("phone_ui_map", "")
    if isinstance(raw, str):
        path = raw.strip()
    elif not raw:
        path = ""
    else:
        path = "invalid"
    verify = None
    if "phone_flow_verify" in data:
        verify = data.get("phone_flow_verify") is True
    jitter = None
    if "phone_flow_jitter_ms" in data:
        jitter = parse_jitter_ms(data.get("phone_flow_jitter_ms"))
    return {"enabled": data.get("phone_flows_enabled") is True, "ui_map_path": path,
            "verify": verify, "jitter_ms": jitter, "state_dir": state_dir}


def _accept_operator_alert_patch(patch: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Type-check operator-alert keys. Empty error means the updates may be written.

    Wrong types use the same rejection as any other unsupported patch. Values are
    stored as given (language lowercased). Observe clamps refresh and streak.
    """
    if not (set(patch) & _PUSH_OPERATOR_ALERT_KEYS):
        return {}, ""
    updates: Dict[str, Any] = {}
    if "operator_alert_enabled" in patch:
        enabled = patch.get("operator_alert_enabled")
        if not isinstance(enabled, bool):
            return {}, "not_supported_in_agent_v1"
        updates["operator_alert_enabled"] = enabled
    if "operator_alert_refresh_sec" in patch:
        raw = patch.get("operator_alert_refresh_sec")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return {}, "not_supported_in_agent_v1"
        updates["operator_alert_refresh_sec"] = raw
    if "operator_alert_language" in patch:
        raw = patch.get("operator_alert_language")
        if not isinstance(raw, str):
            return {}, "not_supported_in_agent_v1"
        lang = raw.strip().lower()
        if lang not in _ALERT_LANGS:
            return {}, "not_supported_in_agent_v1"
        updates["operator_alert_language"] = lang
    if "operator_alert_fail_streak" in patch:
        raw = patch.get("operator_alert_fail_streak")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return {}, "not_supported_in_agent_v1"
        updates["operator_alert_fail_streak"] = raw
    if "wallpaper_map" in patch:
        raw = patch.get("wallpaper_map")
        if not isinstance(raw, (dict, list)):
            return {}, "not_supported_in_agent_v1"
        try:
            blob = json.dumps(raw, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return {}, "not_supported_in_agent_v1"
        if len(blob) > _WALLPAPER_MAP_MAX_BYTES:
            return {}, "not_supported_in_agent_v1"
        updates["wallpaper_map"] = raw
    return updates, ""


class _UiMapReject(Exception):
    """push_config map rejection. The message is only the reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require_ui_map_bytes(blob: bytes) -> None:
    if len(blob) > _MAX_MAP_BYTES:
        raise _UiMapReject("ui_map_too_large")
    if not blob:
        raise _UiMapReject("ui_map_invalid")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _UiMapReject("ui_map_invalid")
    try:
        validate_ui_map(data)
    except PhoneOpError:
        raise _UiMapReject("ui_map_invalid")


def _accept_ui_map_path(raw: str) -> Tuple[str, int]:
    """Absolute path of an existing regular file that already passes validate_ui_map."""
    text = raw.strip()
    if not text:
        raise _UiMapReject("ui_map_missing")
    if len(text) > _UI_MAP_PATH_MAX or "\x00" in text or "\n" in text or "\r" in text:
        raise _UiMapReject("ui_map_invalid")
    path = Path(text)
    if not path.is_absolute():
        raise _UiMapReject("ui_map_missing")
    if _is_reparse(path):
        raise _UiMapReject("ui_map_invalid")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _UiMapReject("ui_map_missing")
    except OSError:
        raise _UiMapReject("ui_map_invalid")
    if not stat.S_ISREG(st.st_mode):
        raise _UiMapReject("ui_map_missing")
    if int(st.st_size) > _MAX_MAP_BYTES:
        raise _UiMapReject("ui_map_too_large")
    try:
        blob = path.read_bytes()
    except OSError:
        raise _UiMapReject("ui_map_missing")
    _require_ui_map_bytes(blob)
    return str(path), len(blob)


def _decode_ui_map_body(spec: Tuple[str, Any]) -> bytes:
    kind, raw = spec
    if kind == "b64":
        compact = "".join(str(raw).split())
        max_b64 = ((_MAX_MAP_BYTES + 2) // 3) * 4
        if len(compact) > max_b64:
            raise _UiMapReject("ui_map_too_large")
        try:
            blob = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise _UiMapReject("ui_map_invalid")
    elif isinstance(raw, str):
        blob = raw.encode("utf-8")
        if len(blob) > _MAX_MAP_BYTES:
            raise _UiMapReject("ui_map_too_large")
    else:
        try:
            blob = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise _UiMapReject("ui_map_invalid")
        if len(blob) > _MAX_MAP_BYTES:
            raise _UiMapReject("ui_map_too_large")
    _require_ui_map_bytes(blob)
    return blob


def _write_remote_ui_map(state_dir: Path, blob: bytes) -> Path:
    """Write the map under the fleet state dir. The file name is fixed."""
    root = Path(state_dir)
    if _is_reparse(root) or not root.is_dir():
        raise _UiMapReject("ui_map_invalid")
    try:
        root_resolved = root.resolve()
    except OSError:
        raise _UiMapReject("ui_map_invalid")
    dest = root / _REMOTE_UI_MAP_NAME
    tmp = root / (_REMOTE_UI_MAP_NAME + ".tmp")
    if dest.parent != root or dest.name != _REMOTE_UI_MAP_NAME or _is_reparse(dest) or _is_reparse(tmp):
        raise _UiMapReject("ui_map_invalid")
    try:
        tmp.write_bytes(blob)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except OSError:
        try:
            if tmp.is_file() and not _is_reparse(tmp):
                tmp.unlink()
        except OSError:
            pass
        raise _UiMapReject("ui_map_invalid")
    if os.name == "nt":
        try:
            assign_owner_admins(dest)
        except Exception:
            logger.debug("[agent] phone_ui_map owner not adjusted")
    try:
        written = dest.resolve()
    except OSError:
        raise _UiMapReject("ui_map_invalid")
    if _is_reparse(written) or written.parent != root_resolved:
        raise _UiMapReject("ui_map_invalid")
    return written


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
    from .textio import utf8_stdio

    utf8_stdio()   # ssh 会话里默认 GBK；status / service-status 的中文统一按 UTF-8 输出
    held: List[Any] = []
    try:
        return _main(argv, held)
    finally:
        for guard in held:
            guard.release()


def _main(argv: Optional[List[str]], held: List[Any]) -> int:
    ap = argparse.ArgumentParser(prog="chatx-agent", description="智拓群控节点 Agent")
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
    sub.add_parser("enable-phone-adb",
                   help="机房节点：允许用安装目录里自带的 adb 在没有 server 时把它拉起来（直播机拒绝）")
    sub.add_parser("install-service", help="开机自启 + 立即启动（Windows 计划任务 SYSTEM / Linux systemd）")
    sub.add_parser("uninstall-service")
    sub.add_parser("service-status")
    sub.add_parser("status")
    ui = sub.add_parser("ui", help="打开本机「智拓群控节点」页面（仅监听 127.0.0.1）")
    ui.add_argument("--port", type=int, default=0, help="0 表示使用面板固定端口")
    ui.add_argument("--no-browser", action="store_true", help="只启动页面，不打开浏览器")
    sub.add_parser("heartbeat", help="只发一次心跳并打印")
    mig = sub.add_parser("migrate-legacy", help="rewrite an unlocked agent.json down to identity fields")
    mig.add_argument("--controller", default="", help="controller URL written into agent.json")
    mig.add_argument("--snapshot", default="", help="agent.json copied before the directory was locked")
    sub.add_parser("detect", help="rediscover local instances; does not enroll")
    ident = sub.add_parser("identity", help="show this PC's machine_id; --reinstall re-derives a cache "
                                            "without a hardware fingerprint (installer)")
    ident.add_argument("--reinstall", action="store_true",
                       help="installer mode: a machine_id cached by 0.3.4 or copied from a cloned disk is "
                            "re-derived from this PC's hardware; an enrollment bound to the old id is dropped")
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
    if args.cmd == "identity":
        try:
            if cfg.migrating:
                cfg.save()  # same order as NodeAgent: rewrite an unlocked agent.json before machine_id
            info = resolve_machine_identity(cfg.state_dir, reinstall=bool(args.reinstall))
            agent = NodeAgent(cfg)
        except StateDirLockError as e:
            _note_migration_failure(cfg.state_dir, e)
            print(f"Could not lock the fleet state directory. Run as Administrator. ({e})", file=sys.stderr)
            return 1
        print(json.dumps({
            "ok": True, "machine_id": agent.machine_id, "short_id": short_machine_id(agent.machine_id),
            "host_name": host_name(), "changed": bool(info.get("changed")), "previous": info.get("previous") or "",
            "reason": info.get("reason") or "", "source": info.get("source") or "", "origin": info.get("origin") or "",
            "enrollment_reset": bool(agent.identity_reset),
        }, ensure_ascii=False))
        return 0
    if args.cmd == "enable-phone-adb":
        if is_live_stream_host(cfg.state_dir):
            print("live-stream host: bundled adb server management stays off", file=sys.stderr)
            return 2
        try:
            cfg.write_operator_keys({"adb_manage_server": True})
        except (AgentError, StateDirLockError) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "adb_manage_server": True}))
        return 0
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
            # PyInstaller 单文件：计划任务结束的是引导进程，本进程会变孤儿继续跑并占住单实例锁
            # （176 2026-10-06 Stop-ScheduledTask 停不掉）。父进程一走本进程就退出。
            start_parent_watch()
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
