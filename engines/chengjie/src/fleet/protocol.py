"""主控 ↔ 节点 协议常量与信封（契约 docs/FLEET_CONTROL_CONTRACT.md 的代码事实源）。

版本策略：``PROTO_VERSION`` 整数递增；主控接受 ``MIN_PROTO_VERSION..PROTO_VERSION`` 的节点，
低于下限的节点只允许领 ``upgrade`` / ``ping``（引导它升级），其余任务一律不下发。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

PROTO_VERSION = 1
MIN_PROTO_VERSION = 1

# ── 任务种类 ────────────────────────────────────────────────────────────────
TASK_PING = "ping"                    # 连通性；节点回 pong + 版本
TASK_PULL_OVERVIEW = "pull_overview"  # 拉节点上 player_care 看板摘要（/api/player-care/overview）
TASK_ACCOUNT_HEALTH = "account_health"  # 拉节点账号健康（/api/accounts/fleet-health）
TASK_LOGIN_QR = "login_qr"            # 在节点实例上起一次扫码登录，回传二维码（集中扫码）
TASK_LOGIN_STATUS = "login_status"    # 查扫码会话状态
TASK_STOP_ACCOUNT = "stop_account"    # 对某手机号停止一切主动触达（→ 节点 player_care commandbus stop）
TASK_RESTART_INSTANCE = "restart_instance"
TASK_PUSH_CONFIG = "push_config"
TASK_UPGRADE = "upgrade"

TASK_KINDS = (
    TASK_PING, TASK_PULL_OVERVIEW, TASK_ACCOUNT_HEALTH, TASK_LOGIN_QR, TASK_LOGIN_STATUS,
    TASK_STOP_ACCOUNT, TASK_RESTART_INSTANCE, TASK_PUSH_CONFIG, TASK_UPGRADE,
)

# 低版本节点仍可领的种类（用于引导升级）
LEGACY_ALLOWED_KINDS = (TASK_PING, TASK_UPGRADE)

# 数值越小越先领；stop 类最先（与 player_care commandbus 同口径）
TASK_PRIORITY: Dict[str, int] = {
    TASK_STOP_ACCOUNT: 0,
    TASK_UPGRADE: 2,
    TASK_RESTART_INSTANCE: 3,
    TASK_PUSH_CONFIG: 4,
    TASK_LOGIN_QR: 5,
    TASK_LOGIN_STATUS: 5,
    TASK_PING: 7,
    TASK_ACCOUNT_HEALTH: 8,
    TASK_PULL_OVERVIEW: 9,
}

# ── 任务状态 ────────────────────────────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_PULLED = "pulled"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
ACK_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_REJECTED)
FINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, STATUS_EXPIRED, STATUS_CANCELLED)

DEFAULT_TASK_TTL_SEC = 15 * 60
MAX_TASK_TTL_SEC = 24 * 3600
DEFAULT_HEARTBEAT_SEC = 30
DEFAULT_OFFLINE_AFTER_SEC = 120
DEFAULT_ENROLL_CODE_TTL_MIN = 60
MAX_PULL_LIMIT = 20
MAX_LONGPOLL_WAIT_SEC = 25

# ── 节点状态（主控按 last_seen 推导） ────────────────────────────────────────
NODE_ONLINE = "online"
NODE_OFFLINE = "offline"
NODE_REVOKED = "revoked"
NODE_PENDING = "pending"        # 已发注册码未注册

# 心跳里允许携带的顶层键（白名单：多余的键主控直接丢弃，防止把聊天原文塞进来）
HEARTBEAT_KEYS = (
    "agent_version", "proto_version", "app_version", "host_name", "os", "python",
    "uptime_sec", "instances", "accounts", "metrics", "fleet_health", "player_overview", "errors",
)


def proto_compatible(proto: Any) -> bool:
    try:
        v = int(proto)
    except (TypeError, ValueError):
        return False
    return MIN_PROTO_VERSION <= v <= PROTO_VERSION


def clamp_ttl(ttl: Any) -> int:
    try:
        v = int(ttl)
    except (TypeError, ValueError):
        v = DEFAULT_TASK_TTL_SEC
    return max(10, min(MAX_TASK_TTL_SEC, v))


def task_envelope(*, task_id: str, kind: str, node_id: str, payload: Optional[Dict[str, Any]] = None,
                  target: Optional[Dict[str, Any]] = None, ttl_sec: int = DEFAULT_TASK_TTL_SEC,
                  created_at: Optional[float] = None) -> Dict[str, Any]:
    """节点只消费的任务信封（契约 §4）。"""
    ts = float(created_at if created_at is not None else time.time())
    return {
        "task_id": str(task_id),
        "kind": str(kind),
        "node_id": str(node_id),
        "target": dict(target or {}),
        "payload": dict(payload or {}),
        "ttl_sec": int(ttl_sec),
        "expires_at": ts + int(ttl_sec),
        "proto_version": PROTO_VERSION,
    }


def sanitize_heartbeat(body: Any) -> Dict[str, Any]:
    """只保留白名单键；非 dict → {}。"""
    if not isinstance(body, dict):
        return {}
    return {k: body[k] for k in HEARTBEAT_KEYS if k in body}


__all__ = [
    "PROTO_VERSION", "MIN_PROTO_VERSION", "TASK_KINDS", "TASK_PRIORITY", "LEGACY_ALLOWED_KINDS",
    "TASK_PING", "TASK_PULL_OVERVIEW", "TASK_ACCOUNT_HEALTH", "TASK_LOGIN_QR", "TASK_LOGIN_STATUS",
    "TASK_STOP_ACCOUNT", "TASK_RESTART_INSTANCE", "TASK_PUSH_CONFIG", "TASK_UPGRADE",
    "STATUS_QUEUED", "STATUS_PULLED", "STATUS_DONE", "STATUS_FAILED", "STATUS_REJECTED",
    "STATUS_EXPIRED", "STATUS_CANCELLED", "ACK_STATUSES", "FINAL_STATUSES",
    "DEFAULT_TASK_TTL_SEC", "DEFAULT_HEARTBEAT_SEC", "DEFAULT_OFFLINE_AFTER_SEC",
    "DEFAULT_ENROLL_CODE_TTL_MIN", "MAX_PULL_LIMIT", "MAX_LONGPOLL_WAIT_SEC",
    "NODE_ONLINE", "NODE_OFFLINE", "NODE_REVOKED", "NODE_PENDING", "HEARTBEAT_KEYS",
    "proto_compatible", "clamp_ttl", "task_envelope", "sanitize_heartbeat",
]
