"""主控 ↔ 节点 协议常量与信封（契约 docs/FLEET_CONTROL_CONTRACT.md 的代码事实源）。

版本策略：``PROTO_VERSION`` 整数递增；主控接受 ``MIN_PROTO_VERSION..PROTO_VERSION`` 的节点，
低于下限的节点只允许领 ``upgrade`` / ``ping``（引导它升级），其余任务一律不下发。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

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
    "phones", "phones_error",   # 0.3.6 只读手机清点 [{serial, state, model, transport}] + 错误原因
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
    """只保留白名单键；非 dict → {}。phones 只留 serial/state/model/transport 四个字段。"""
    if not isinstance(body, dict):
        return {}
    out = {k: body[k] for k in HEARTBEAT_KEYS if k in body}
    if "phones" in out or "phones_error" in out:
        from .phones import sanitize_phones, sanitize_phones_error

        out["phones"] = sanitize_phones(out.get("phones"))
        out["phones_error"] = sanitize_phones_error(out.get("phones_error"))
    if "caps" in out:
        out["caps"] = sanitize_caps(out.get("caps"))
    return out


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


# ── 0.3.7 节点能力（caps）+ 远程手机操作 ─────────────────────────────────────
# 独立追加块：别的分支往上面的 TASK_KINDS / TASK_PRIORITY / __all__ 里加种类时，
# 改动落在不同行，rebase 不会和这里冲突（这里只做「在原值上追加」）。
CAP_PHONE_OPS_V1 = "phone_ops_v1"
TASK_PHONE_SCREENSHOT = "phone_screenshot"
TASK_PHONE_TAP = "phone_tap"
TASK_PHONE_SWIPE = "phone_swipe"
TASK_PHONE_TEXT = "phone_text"
TASK_PHONE_KEY = "phone_key"
PHONE_TASK_KINDS = (TASK_PHONE_SCREENSHOT, TASK_PHONE_TAP, TASK_PHONE_SWIPE, TASK_PHONE_TEXT, TASK_PHONE_KEY)
PHONE_TASK_TTL_SEC = 60          # 过期的点击比不点更危险：一分钟没被领走就作废

TASK_KINDS = TASK_KINDS + tuple(k for k in PHONE_TASK_KINDS if k not in TASK_KINDS)
TASK_PRIORITY.update({k: 6 for k in PHONE_TASK_KINDS})
# 种类 → 节点心跳 caps 里必须有的能力；不在表里的种类不看能力（老种类照旧）
TASK_REQUIRED_CAPS: Dict[str, str] = {k: CAP_PHONE_OPS_V1 for k in PHONE_TASK_KINDS}
HEARTBEAT_KEYS = HEARTBEAT_KEYS + ("caps",)
MAX_CAPS = 16
_CAP_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def sanitize_caps(value: Any) -> List[str]:
    """心跳 caps：小写标识符列表，去重，最多 MAX_CAPS 个；其他形状 → []。"""
    out: List[str] = []
    if not isinstance(value, (list, tuple)):
        return out
    for item in value:
        s = str(item or "").strip().lower() if isinstance(item, str) else ""
        if s and _CAP_RE.match(s) and s not in out:
            out.append(s)
        if len(out) >= MAX_CAPS:
            break
    return out


def required_cap(kind: Any) -> str:
    return TASK_REQUIRED_CAPS.get(str(kind or ""), "")


def missing_cap(kind: Any, caps: Any) -> str:
    """该种类要求、而节点 caps 里没有的能力名；不缺 → ""。"""
    need = required_cap(kind)
    if need and need not in sanitize_caps(caps):
        return need
    return ""


# 回传结果上限（JSON UTF-8 字节）：截图单独放宽（nginx /fleet/api/ client_max_body_size 2m），其余 64 KB。
# 超限的结果不入库，只留 {error: result_too_large, bytes, limit}。
RESULT_MAX_BYTES = 64 * 1024
RESULT_MAX_BYTES_BY_KIND: Dict[str, int] = {TASK_PHONE_SCREENSHOT: 1_900_000}
# 结果保留：截图 1 小时后清空（只留行做审计），其余已结束任务的结果 7 天后清空
SCREENSHOT_RESULT_KEEP_SEC = 3600
TASK_RESULT_KEEP_SEC = 7 * 86400
PRUNED_RESULT = {"pruned": True}


def result_limit(kind: Any) -> int:
    return RESULT_MAX_BYTES_BY_KIND.get(str(kind or ""), RESULT_MAX_BYTES)


def bound_result(kind: Any, result: Any) -> Dict[str, Any]:
    """ack 结果按种类限大小；非 dict → {}；不能序列化 → {error: result_not_json}。"""
    if not isinstance(result, dict):
        return {}
    limit = result_limit(kind)
    try:
        n = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return {"error": "result_not_json"}
    if n <= limit:
        return result
    return {"error": "result_too_large", "bytes": n, "limit": limit}


__all__ += [
    "RESULT_MAX_BYTES", "RESULT_MAX_BYTES_BY_KIND", "SCREENSHOT_RESULT_KEEP_SEC", "TASK_RESULT_KEEP_SEC",
    "PRUNED_RESULT", "result_limit", "bound_result",
]

__all__ += [
    "CAP_PHONE_OPS_V1", "TASK_PHONE_SCREENSHOT", "TASK_PHONE_TAP", "TASK_PHONE_SWIPE", "TASK_PHONE_TEXT",
    "TASK_PHONE_KEY", "PHONE_TASK_KINDS", "PHONE_TASK_TTL_SEC", "TASK_REQUIRED_CAPS", "MAX_CAPS",
    "sanitize_caps", "required_cap", "missing_cap",
]


# ── 0.3.8 社交动作（发帖 / 点赞 / 评论 / 关注）────────────────────────────────
# 仍是追加块。低层 phone_screenshot 等继续只要求 phone_ops_v1；这四个种类另要
# phone_flows_v1。PHONE_TASK_KINDS 不扩，避免把复合动作塞进单步执行器。
CAP_PHONE_FLOWS_V1 = "phone_flows_v1"
TASK_PHONE_POST = "phone_post"
TASK_PHONE_LIKE = "phone_like"
TASK_PHONE_COMMENT = "phone_comment"
TASK_PHONE_FOLLOW = "phone_follow"
PHONE_FLOW_KINDS = (TASK_PHONE_POST, TASK_PHONE_LIKE, TASK_PHONE_COMMENT, TASK_PHONE_FOLLOW)
PHONE_FLOW_TTL_SEC = 180
SOCIAL_APPS = ("facebook", "instagram", "tiktok")
FLOW_NAMES = ("post", "like", "comment", "follow")

TASK_KINDS = TASK_KINDS + tuple(k for k in PHONE_FLOW_KINDS if k not in TASK_KINDS)
TASK_PRIORITY.update({k: 6 for k in PHONE_FLOW_KINDS})
TASK_REQUIRED_CAPS.update({k: CAP_PHONE_FLOWS_V1 for k in PHONE_FLOW_KINDS})
# 远程操作开关盖住低层点击和社交动作；关掉时两类排队任务一起作废
REMOTE_PHONE_KINDS = PHONE_TASK_KINDS + PHONE_FLOW_KINDS

__all__ += [
    "CAP_PHONE_FLOWS_V1", "TASK_PHONE_POST", "TASK_PHONE_LIKE", "TASK_PHONE_COMMENT", "TASK_PHONE_FOLLOW",
    "PHONE_FLOW_KINDS", "PHONE_FLOW_TTL_SEC", "SOCIAL_APPS", "FLOW_NAMES", "REMOTE_PHONE_KINDS",
]


# ── 养号 / 私信 / 看短视频（仍走 phone_flows_enabled，另要 phone_flows_v2）────────
# 不改 PHONE_FLOW_KINDS / FLOW_NAMES：旧的四个社交动作和它们的能力位保持原样。
# 这三项默认也关着；只有 phone_flows_enabled 为 JSON true 且已有 phone_ops_v1 时，
# 心跳才同时带上 phone_flows_v1 和 phone_flows_v2。
CAP_PHONE_FLOWS_V2 = "phone_flows_v2"
TASK_PHONE_WARMUP = "phone_warmup"
TASK_PHONE_DM = "phone_dm"
TASK_PHONE_WATCH = "phone_watch"
PHONE_SESSION_KINDS = (TASK_PHONE_WARMUP, TASK_PHONE_DM, TASK_PHONE_WATCH)
SESSION_FLOW_NAMES = ("warmup", "dm", "watch")

TASK_KINDS = TASK_KINDS + tuple(k for k in PHONE_SESSION_KINDS if k not in TASK_KINDS)
TASK_PRIORITY.update({k: 6 for k in PHONE_SESSION_KINDS})
TASK_REQUIRED_CAPS.update({k: CAP_PHONE_FLOWS_V2 for k in PHONE_SESSION_KINDS})
REMOTE_PHONE_KINDS = REMOTE_PHONE_KINDS + PHONE_SESSION_KINDS

__all__ += [
    "CAP_PHONE_FLOWS_V2", "TASK_PHONE_WARMUP", "TASK_PHONE_DM", "TASK_PHONE_WATCH",
    "PHONE_SESSION_KINDS", "SESSION_FLOW_NAMES",
]


# ── 远程打开机房自带 adb（不进 REMOTE_PHONE_KINDS：机房节点可能还没开远程操作）──
# 也不进 LEGACY_ALLOWED_KINDS。旧节点收不到这个种类；要先用 upgrade 换上认识它的 exe。
TASK_ENABLE_PHONE_ADB = "enable_phone_adb"
TASK_KINDS = TASK_KINDS + ((TASK_ENABLE_PHONE_ADB,) if TASK_ENABLE_PHONE_ADB not in TASK_KINDS else ())
TASK_PRIORITY.setdefault(TASK_ENABLE_PHONE_ADB, 3)

__all__ += ["TASK_ENABLE_PHONE_ADB"]


# ── 0.3.16 只读操作员告警诊断（不进 REMOTE_PHONE_KINDS，也不进心跳）──────────
# 机房节点即使没开远程手机操作也能领。回执只有开关、语言、刷新间隔和壁纸编号。
TASK_OPERATOR_ALERT_DIAG = "operator_alert_diag"
TASK_KINDS = TASK_KINDS + ((TASK_OPERATOR_ALERT_DIAG,) if TASK_OPERATOR_ALERT_DIAG not in TASK_KINDS else ())
TASK_PRIORITY.setdefault(TASK_OPERATOR_ALERT_DIAG, 8)

__all__ += ["TASK_OPERATOR_ALERT_DIAG"]


# ── 0.3.18 只读网络体检（不进 REMOTE_PHONE_KINDS，不要求 caps）──────────────
# 机房节点没开远程手机操作也能领。回执只有壁纸编号和网络字段，没有序列号。
TASK_NET_HEALTH = "net_health"
TASK_KINDS = TASK_KINDS + ((TASK_NET_HEALTH,) if TASK_NET_HEALTH not in TASK_KINDS else ())
TASK_PRIORITY.setdefault(TASK_NET_HEALTH, 8)

__all__ += ["TASK_NET_HEALTH"]


# ── 0.3.22 机房现场待办（不进 REMOTE_PHONE_KINDS，不要求 caps）──────────────
# 主控只发给这一台机房电脑。内容是类别 + 壁纸号，没有序列号。直播机拒绝。
TASK_SITE_TODO = "site_todo"
TASK_KINDS = TASK_KINDS + ((TASK_SITE_TODO,) if TASK_SITE_TODO not in TASK_KINDS else ())
TASK_PRIORITY.setdefault(TASK_SITE_TODO, 8)

__all__ += ["TASK_SITE_TODO"]
