"""平台会话健康登记表（进程级单例）——外部 worker 微服务的会话状态闭环（P0-2）。

背景：Messenger 网页会话（``services/messenger-web`` Node 微服务）掉线/cookie 失效/
崩溃循环放弃自愈时，此前只写 Node 自己的日志，Python 侧毫不知情——编排器要等健康
轮询才后知后觉，运营则完全看不见（「会话死了还在装在线」）。

本模块是接收 Node 主动 push 的 ``/api/internal/protocol/session-status`` 事件的落点：
- 记住每个 ``platform:account`` 的最新会话状态（authorized / needs_login / expired /
  logged_out / failed），供发送前快速判活与 ops 看板展示；
- ``record()`` 返回状态迁移语义（进入不健康 / 恢复），路由层据此发 EventBus 告警
  （``platform_session_alert``，订阅别名 ``platform_session``）；
- ``dump()`` → ``/api/workspace/metrics.platform_sessions``、``dump_prom()`` → Prometheus。

风格对齐 ``src/inbox/send_route_stats.py``：零依赖、线程安全、进程级单例、
distinct key 有上限（防脏数据撑爆内存）。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

# 视为「不健康」的会话状态（会话不可用于收发）
UNHEALTHY_STATUSES = frozenset({"expired", "needs_login", "logged_out", "failed"})
# 视为「健康」的状态
HEALTHY_STATUSES = frozenset({"authorized"})

_MAX_KEYS = 64
_SAN_RE = re.compile(r"[^a-zA-Z0-9_\-\.:@]")


def _san(value: str, limit: int = 48) -> str:
    v = _SAN_RE.sub("", str(value or "").strip())
    return v[:limit]


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# 坐席/ops 可操作的入站半死产品码（与 messenger-web classifyInboxHint 对齐）。
# e2ee_relogin      = 需在服务器完整重登恢复设备密钥（cookie 快照不够）；
# e2ee_pin_required = worker 已确认 PIN 浮层在场但缺 PIN/输入未通过——托管 PIN 即自愈，
#                     比泛泛「重登」更精确（坐席该做的是填 PIN 而非走完整登录流程）。
_INBOX_HINT_CODES = frozenset({"e2ee_relogin", "e2ee_pin_required"})


def derive_inbox_hint(*, stalled: bool, detail: str = "",
                      stall_kind: str = "") -> str:
    """半死产品提示码（纯函数）：优先 worker 显式 detail，否则按 stall_kind 推导。"""
    d = str(detail or "").strip()
    if d in _INBOX_HINT_CODES:
        return d
    if not stalled:
        return ""
    # read_fail / e2ee_placeholder / steady_fail 对坐席同一动作：服务器完整重登
    if str(stall_kind or "") in ("read_fail", "e2ee_placeholder", "steady_fail", ""):
        return "e2ee_relogin"
    return d[:64]


class PlatformSessionHealth:
    """外部 worker 会话状态登记（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_sessions", "total_events",
                 "_by_status", "_inbox_health",
                 "total_stall_went", "total_stall_recovered", "total_relogin")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        # key = "platform:account_id" → {status, detail, login_id, ts, changes}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self.total_events = 0
        self._by_status: Dict[str, int] = {}
        # 入站健康心跳（P0，2026-08-04「Messenger 登录态在、消息读不到」半死态解药）：
        # key = "platform:account_id" → {unread, read_attempts, read_fails,
        #   last_inbound_ts, ts, first_seen, stall_since, last_remind_ts}。
        # 与会话状态正交——账号 status=authorized（登录探测绿）不代表能读到消息内容
        # （cookie 快照恢复登录但 E2EE 设备密钥没恢复 → 消息区永久卡 Loading）。
        # 这里记「侧栏有未读、进线程读取却持续全失败」的确定性背离信号。
        self._inbox_health: Dict[str, Dict[str, Any]] = {}
        # P4 漏斗：半死进入 / 恢复 / 人工触发重登（进程累计，重启清零）
        self.total_stall_went = 0
        self.total_stall_recovered = 0
        self.total_relogin = 0

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return f"{_san(platform, 24).lower()}:{_san(account_id)}"

    def record(self, platform: str, account_id: str, status: str,
               *, detail: str = "", login_id: str = "") -> Dict[str, Any]:
        """登记一次会话状态事件。

        返回迁移语义：``{"changed", "went_unhealthy", "recovered", "prev", "status"}``
        —— 路由层据此决定是否发告警（进入不健康）/恢复通知（回到健康），
        重复同态事件不重复告警。
        """
        st = _san(status, 24).lower() or "unknown"
        key = self._key(platform, account_id)
        with self._lock:
            self.total_events += 1
            self._by_status[st] = self._by_status.get(st, 0) + 1
            sess = self._sessions.get(key)
            if sess is None:
                if len(self._sessions) >= _MAX_KEYS:
                    # 超限：不再收新 key（防刷量），但事件计数仍累计
                    return {"changed": False, "went_unhealthy": False,
                            "recovered": False, "prev": "", "status": st}
                sess = {"status": "", "detail": "", "login_id": "", "ts": 0.0,
                        "changes": 0, "unhealthy_since": 0.0,
                        "last_remind_ts": 0.0}
                self._sessions[key] = sess
            prev = str(sess.get("status") or "")
            changed = prev != st
            if changed:
                sess["changes"] = int(sess.get("changes") or 0) + 1
            now = time.time()
            sess["status"] = st
            sess["detail"] = str(detail or "")[:300]
            sess["login_id"] = _san(login_id)
            sess["ts"] = now
            # 持续不健康起点：进入不健康时打点，期间同态重推（如放弃自愈的周期重报）
            # 不刷新 → 「已掉线多久」可信；恢复即清零（连带提醒节流点）。
            if st in UNHEALTHY_STATUSES:
                if not float(sess.get("unhealthy_since") or 0.0):
                    sess["unhealthy_since"] = now
            else:
                sess["unhealthy_since"] = 0.0
                sess["last_remind_ts"] = 0.0
            went_unhealthy = (st in UNHEALTHY_STATUSES
                              and prev not in UNHEALTHY_STATUSES)
            recovered = (st in HEALTHY_STATUSES
                         and prev in UNHEALTHY_STATUSES)
            return {"changed": changed, "went_unhealthy": went_unhealthy,
                    "recovered": recovered, "prev": prev, "status": st}

    def is_unhealthy(self, platform: str, account_id: str) -> bool:
        """该会话最近一次上报是否处于不健康态（未上报过 → False，不拦）。"""
        with self._lock:
            sess = self._sessions.get(self._key(platform, account_id))
            return bool(sess and str(sess.get("status")) in UNHEALTHY_STATUSES)

    def unhealthy_sessions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._sessions.items()
                    if str(v.get("status")) in UNHEALTHY_STATUSES}

    def due_reminders(self, *, min_age_sec: float, interval_sec: float,
                      now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """待提醒的「持续不健康」会话（供 HealthWatchdog 周期复查）。

        语义（升级式）：掉线后 ``min_age_sec`` 仍未恢复 → 第一条提醒（「还没人修」），
        之后每 ``interval_sec`` 一条（防唠叨）。返回即视为「本轮要提醒」——原子标记
        ``last_remind_ts``，并发/下轮不重复。恢复时 ``record()`` 会清零两个时间戳。
        """
        ts = time.time() if now is None else float(now)
        due: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, sess in self._sessions.items():
                if str(sess.get("status")) not in UNHEALTHY_STATUSES:
                    continue
                since = float(sess.get("unhealthy_since") or 0.0)
                if not since or (ts - since) < float(min_age_sec):
                    continue
                last = float(sess.get("last_remind_ts") or 0.0)
                if last and (ts - last) < float(interval_sec):
                    continue
                sess["last_remind_ts"] = ts
                out = dict(sess)
                out["down_sec"] = ts - since
                due[key] = out
        return due

    # ── 入站健康心跳（读得到未读却读不到内容 = 半死态）─────────────────────────
    #: record_inbox_health 里判「此刻是否 stalled」的最小尝试数——低于此不判，
    #: 防单次导航超时被当黑洞（宁可漏报不误报，与本仓告警纪律一致）。
    _STALL_MIN_ATTEMPTS = 2

    #: 「无读取样本」盲区的第二支判定阈值：会话列表 ≥ 该数 且 E2EE 占位预览占比 ≥
    #: 比例阈值才判 stall（小账号/加载抖动不误报）。第三支（steady_fail）复用同两阈值。
    _STALL_MIN_CONVS = 5
    _STALL_PLACEHOLDER_RATIO = 0.6

    def record_inbox_health(
        self, platform: str, account_id: str, *,
        unread: int = 0, read_attempts: int = 0, read_fails: int = 0,
        last_inbound_ts: float = 0.0, detail: str = "",
        min_attempts: Optional[int] = None,
        e2ee_ratio: float = -1.0, conv_count: int = 0,
    ) -> Dict[str, Any]:
        """登记一次入站健康心跳（外部 worker 周期 push）。

        ``read_attempts`` / ``read_fails`` 是 worker 侧**滚动窗**（最近 N 次进线程
        读取）的成败计数。stall 判定两支（任一命中即半死态）：

        - **读取全败**：「侧栏有未读 且 窗口内读取全失败」= 看得到读不到
          （E2EE 卡 Loading / 选择器失配 / 密钥丢失都命中，比专测 E2EE 更通用）；
        - **占位盲区**（P1 补，修被动信号的样本盲区）：读取窗**零样本**（存量未读、
          预览不变 → 轮询永不进线程，第一支永无数据）但「有未读 + 会话列表大面积是
          E2EE 加密占位预览」——健康会话的预览是解密后的正文，大面积占位＝解密不可用。
          ``e2ee_ratio``（0..1，-1=worker 未上报，旧版兼容）与 ``conv_count`` 由
          worker 从侧栏零成本统计，无导航、无标已读副作用。

        返回 ``{"stalled", "went_stalled", "recovered", "down_sec"}``——``went_stalled``
        / ``recovered`` 是边沿，供调用方决定是否即时告警（当前告警统一走看门狗
        ``due_inbox_stalls`` 升级式提醒，本方法只记录 + 计时）。
        """
        key = self._key(platform, account_id)
        thr = int(self._STALL_MIN_ATTEMPTS if min_attempts is None else min_attempts)
        try:
            unread_i = max(0, int(unread or 0))
            att = max(0, int(read_attempts or 0))
            fail = max(0, min(att, int(read_fails or 0)))
            last_in = float(last_inbound_ts or 0.0)
            ratio = float(e2ee_ratio if e2ee_ratio is not None else -1.0)
            convs = max(0, int(conv_count or 0))
        except (TypeError, ValueError):
            unread_i, att, fail, last_in, ratio, convs = 0, 0, 0, 0.0, -1.0, 0
        now = time.time()
        ratio_high = (convs >= self._STALL_MIN_CONVS
                      and 0.0 <= ratio
                      and ratio >= self._STALL_PLACEHOLDER_RATIO)
        # 支一：有未读 + 达到最小尝试数 + 窗口内全部失败。
        full_fail = unread_i >= 1 and att >= thr and fail >= att
        # 支二：零样本盲区——有未读 + 无读取样本 + 会话列表大面积加密占位。
        placeholder_blind = (
            unread_i >= 1 and att == 0 and ratio_high)
        # 支三：稳态盲区（P3，198 实测）——读取窗全败 + 大面积占位，但 unread 已被
        # 失败读取消费成 0（打开线程即 FB 侧标已读）。此时支一（要 unread≥1）漏判、
        # 支二（要零样本）也漏判，账号一路「零告警」拖着。占位占比作旁证防误报：
        # 真空会话/对端撤回不会大面积占位。
        steady_fail = (att >= thr and fail >= att and ratio_high)
        stalled = full_fail or placeholder_blind or steady_fail
        with self._lock:
            h = self._inbox_health.get(key)
            if h is None:
                if len(self._inbox_health) >= _MAX_KEYS:
                    return {"stalled": stalled, "went_stalled": False,
                            "recovered": False, "down_sec": 0.0}
                h = {"first_seen": now, "stall_since": 0.0, "last_remind_ts": 0.0}
                self._inbox_health[key] = h
            was_stalled = bool(float(h.get("stall_since") or 0.0))
            h["unread"] = unread_i
            h["read_attempts"] = att
            h["read_fails"] = fail
            h["last_inbound_ts"] = last_in
            h["e2ee_ratio"] = ratio
            h["conv_count"] = convs
            stall_kind = ("read_fail" if full_fail
                          else "e2ee_placeholder" if placeholder_blind
                          else "steady_fail" if steady_fail else "")
            h["stall_kind"] = stall_kind
            raw_detail = str(detail or "")[:300]
            h["detail"] = raw_detail
            h["hint_code"] = derive_inbox_hint(
                stalled=stalled, detail=raw_detail, stall_kind=stall_kind)
            h["ts"] = now
            went = stalled and not was_stalled
            recovered = (not stalled) and was_stalled
            if stalled:
                if not float(h.get("stall_since") or 0.0):
                    h["stall_since"] = now
            else:
                h["stall_since"] = 0.0
                h["last_remind_ts"] = 0.0
            if went:
                self.total_stall_went += 1
            if recovered:
                self.total_stall_recovered += 1
            since = float(h.get("stall_since") or 0.0)
            return {
                "stalled": stalled,
                "went_stalled": went,
                "recovered": recovered,
                "down_sec": (now - since) if since else 0.0,
                "hint_code": h["hint_code"],
            }

    def due_inbox_stalls(self, *, min_age_sec: float, interval_sec: float,
                         now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """待提醒的「持续 stalled」入站会话（供 HealthWatchdog 周期复查）。

        与 ``due_reminders`` 同升级式语义：stalled 持续 ``min_age_sec`` → 首提，
        之后每 ``interval_sec`` 一条；返回即原子标记 ``last_remind_ts`` 防重复。
        恢复（下一次心跳非 stalled）时 ``record_inbox_health`` 清零两个时间戳。
        """
        ts = time.time() if now is None else float(now)
        due: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, h in self._inbox_health.items():
                since = float(h.get("stall_since") or 0.0)
                if not since or (ts - since) < float(min_age_sec):
                    continue
                last = float(h.get("last_remind_ts") or 0.0)
                if last and (ts - last) < float(interval_sec):
                    continue
                h["last_remind_ts"] = ts
                out = dict(h)
                out["down_sec"] = ts - since
                due[key] = out
        return due

    def inbox_health(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in sorted(self._inbox_health.items())}

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            sessions = {k: dict(v) for k, v in sorted(self._sessions.items())}
            unhealthy = [k for k, v in sessions.items()
                         if str(v.get("status")) in UNHEALTHY_STATUSES]
            inbox = {k: dict(v) for k, v in sorted(self._inbox_health.items())}
            stalled = [k for k, v in inbox.items()
                       if float(v.get("stall_since") or 0.0)]
            return {
                "started_at": self._started_at,
                "total_events": self.total_events,
                "by_status": dict(sorted(self._by_status.items())),
                "sessions": sessions,
                "unhealthy": unhealthy,
                "unhealthy_count": len(unhealthy),
                "inbox_health": inbox,
                "inbox_stalled": stalled,
                "inbox_stalled_count": len(stalled),
                # P4 漏斗：半死进入 → 人工重登 → 恢复（进程累计）
                "stall_funnel": {
                    "went": int(self.total_stall_went),
                    "recovered": int(self.total_stall_recovered),
                    "relogin": int(self.total_relogin),
                    "open": len(stalled),
                },
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP platform_session_events_total Session status events "
                "pushed by external workers, by status",
                "# TYPE platform_session_events_total counter",
            ]
            for st, n in sorted(self._by_status.items()):
                lines.append(
                    f'platform_session_events_total{{status="{_esc(st)}"}} {int(n)}')
            lines += [
                "# HELP platform_session_unhealthy Whether the session's last "
                "reported status is unhealthy (1) or healthy (0)",
                "# TYPE platform_session_unhealthy gauge",
            ]
            for key, sess in sorted(self._sessions.items()):
                val = 1 if str(sess.get("status")) in UNHEALTHY_STATUSES else 0
                lines.append(
                    f'platform_session_unhealthy{{session="{_esc(key)}"}} {val}')
            lines += [
                "# HELP platform_inbox_stalled Whether the account can see unread "
                "but thread reads keep failing (1) or not (0)",
                "# TYPE platform_inbox_stalled gauge",
            ]
            for key, h in sorted(self._inbox_health.items()):
                val = 1 if float(h.get("stall_since") or 0.0) else 0
                lines.append(
                    f'platform_inbox_stalled{{session="{_esc(key)}"}} {val}')
            lines += [
                "# HELP platform_inbox_stall_went_total Times an account "
                "entered inbox-stall (half-dead inbound)",
                "# TYPE platform_inbox_stall_went_total counter",
                f"platform_inbox_stall_went_total {int(self.total_stall_went)}",
                "# HELP platform_inbox_stall_recovered_total Times an account "
                "left inbox-stall",
                "# TYPE platform_inbox_stall_recovered_total counter",
                f"platform_inbox_stall_recovered_total "
                f"{int(self.total_stall_recovered)}",
                "# HELP platform_session_relogin_total Manual re-login "
                "triggers from ops/seat",
                "# TYPE platform_session_relogin_total counter",
                f"platform_session_relogin_total {int(self.total_relogin)}",
            ]
        return "\n".join(lines) + "\n"

    def record_relogin(self, platform: str, account_id: str) -> None:
        """登记一次人工「重新登录」触发（ops/坐席 CTA）——漏斗中段。"""
        key = self._key(platform, account_id)
        with self._lock:
            self.total_relogin += 1
            h = self._inbox_health.get(key)
            if h is not None:
                h["last_relogin_ts"] = time.time()

    def reset(self) -> None:
        with self._lock:
            self.total_events = 0
            self._by_status.clear()
            self._sessions.clear()
            self._inbox_health.clear()
            self.total_stall_went = 0
            self.total_stall_recovered = 0
            self.total_relogin = 0

    def seed_offline_from_registry(
        self, rows: Any, *, detail: str = "seeded from registry offline",
    ) -> int:
        """用注册表 ``status=offline`` 行种子化健康表（跨重启接续「已退出」真相）。

        健康表是进程内存态——服务一重启，登出时 Node 推过的 ``logged_out`` 全丢，
        坐席顶栏「通道离线」/ ops 卡/发送闸的健康判据都会对死号失明（注册表仍记着
        offline，但读健康表的消费方看不见）。启动时把在册 offline 号灌成
        ``logged_out``，``unhealthy_since`` 取注册表 ``updated_at``（登出落库时刻）。

        不变量：
        - **只补不覆盖**：该 key 已有事件（Node 真 push / 先前种子）则跳过——
          运行时真相优先于启动快照；
        - **不发 EventBus**：种子不是状态转移，催人修「自己刚下的号」是噪音
          （看门狗另有 ``_session_expected_online`` 过滤）；
        - 非法/空 account_id 跳过；超 ``_MAX_KEYS`` 与 ``record`` 同语义拒收。

        返回新种子条数。
        """
        n = 0
        if not rows:
            return 0
        with self._lock:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") != "offline":
                    continue
                plat = str(row.get("platform") or "")
                acct = str(row.get("account_id") or "")
                if not plat or not acct:
                    continue
                key = self._key(plat, acct)
                if key in self._sessions:
                    continue
                if len(self._sessions) >= _MAX_KEYS:
                    break
                try:
                    since = float(row.get("updated_at") or 0.0)
                except (TypeError, ValueError):
                    since = 0.0
                now = time.time()
                if since <= 0:
                    since = now
                self._sessions[key] = {
                    "status": "logged_out",
                    "detail": str(detail or "")[:300],
                    "login_id": "",
                    "ts": now,
                    "changes": 0,
                    "unhealthy_since": since,
                    "last_remind_ts": 0.0,
                    "seeded": True,
                }
                self._by_status["logged_out"] = (
                    self._by_status.get("logged_out", 0) + 1)
                n += 1
        return n


_SINGLETON: Optional[PlatformSessionHealth] = None
_LOCK = threading.Lock()
_SEEDED = False


def get_platform_session_health() -> PlatformSessionHealth:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PlatformSessionHealth()
    return _SINGLETON


def ensure_seeded_from_registry() -> int:
    """幂等：进程内只种子化一次（启动后首次有人读健康表时懒加载）。

    放在 getter 旁而非强绑 main.py——测试/CLI 场景也能拿到正确种子，且编排器/
    看门狗/chats 任一先触达即生效。返回新种子条数（已种子过 → 0）。

    注意：singleton 必须在 ``_LOCK`` 外取——``get_platform_session_health`` 同锁
    且非 RLock，持锁再取会自死锁（门禁曾实锤卡死）。
    """
    global _SEEDED
    if _SEEDED:
        return 0
    health = get_platform_session_health()
    with _LOCK:
        if _SEEDED:
            return 0
        try:
            from src.integrations.account_registry import get_account_registry
            rows = [
                r for r in (get_account_registry().list() or [])
                if str(r.get("status") or "") == "offline"
            ]
        except Exception:
            rows = []
        n = health.seed_offline_from_registry(rows)
        _SEEDED = True
        return n


__all__ = [
    "PlatformSessionHealth", "get_platform_session_health",
    "ensure_seeded_from_registry",
    "UNHEALTHY_STATUSES", "HEALTHY_STATUSES",
]
