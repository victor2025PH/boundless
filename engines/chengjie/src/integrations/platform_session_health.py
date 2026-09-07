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

import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.platform_session_health")

# 视为「不健康」的会话状态（会话不可用于收发）。
# blocked（B99 2026-08-26）＝worker 检出平台「临时封锁」页：账号在线但发送必失败，
# 边车已自冻；这里入不健康集合让横幅/看门狗/发送闸同步可见。
# forbidden（J-6 A 2026-09-05）＝WhatsApp 边车连续多轮 403 forbidden（账号受限/
# 被封）后的**终态**：Node 已停自动重连（close-policy.decideCloseAction →
# "forbidden"），再重连只会继续吃 403。入不健康集合 → 横幅红/看门狗升级提醒/
# 发送闸快速失败；ops 卡按钮语义变「解锁重连/换号重配对」（Node 侧 30min 解锁冷却）。
UNHEALTHY_STATUSES = frozenset(
    {"expired", "needs_login", "logged_out", "failed", "blocked", "forbidden"})
# 视为「健康」的状态
HEALTHY_STATUSES = frozenset({"authorized"})
# 「放弃的登录尝试」（登录窗未授权就被关掉，从来不是真实账号）——worker 上报
# ``abandoned``：刻意**不在** UNHEALTHY_STATUSES 里，因此不进横幅/看门狗/注册表
# 翻转；记录仍保留（by_status 可观测「坐席放弃了几次登录」）。2026-08-14 事故：
# 放弃登录曾报 expired → 幽灵 login_id 挂横幅 3+ 小时无人能懂。
ABANDONED_STATUS = "abandoned"
# M-2（D-M1）：从这些状态回到 authorized ＝ 人重新认证过（真重登 → 登录冷静期）；
# failed / blocked / forbidden 的恢复是通道自愈或平台解限，不算登录。
LOGIN_RECOVERY_STATUSES = frozenset({"needs_login", "logged_out", "expired"})

# 「登录尝试幽灵」TTL：不健康且**从未绑定真实账号**（key 的账号段==临时 login_id，
# 即上报时 account_id 为空只能拿 login_id 顶位）的记录，超时即清——这类 key 没有
# 任何可操作语义（横幅显示 msg_xxxx 坐席看不懂、重登按钮也无从指向），旧版 worker
# 报上来的存量幽灵靠它兜底自愈。真实账号的不健康记录永不在此清理。
_ORPHAN_LOGIN_TTL_SEC = 30 * 60

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


# J-6 B（2026-09-05）：WA 边车 close 原因分类（与 whatsapp-baileys/close-policy.js
# REASON_CLASSES 逐字对齐）。Node 侧在 postStatus 把分类以 ``[rc:<class>]`` 前缀写进
# detail（老后端零改动也能在横幅里看见），这里把它解出来成独立字段，ops 卡/看门狗
# 才能按类聚合：dns 多＝本机解析/代理问题，别再把 ENOTFOUND 当「WhatsApp 掉线」修。
_REASON_CLASSES = ("dns", "net", "server", "forbidden", "logged_out", "restart", "other")
_RC_PREFIX_RE = re.compile(r"^\[rc:([a-z_]{1,16})\]\s*")


def parse_reason_class(detail: str) -> Tuple[str, str]:
    """从 detail 前缀解 ``(reason_class, 去前缀后的 detail)``（纯函数）。

    无前缀 → ``("", detail)``；前缀值不在枚举内归 ``other``（Node 新增类别先于
    Python 上线时不丢计数）。
    """
    d = str(detail or "")
    m = _RC_PREFIX_RE.match(d)
    if not m:
        return "", d
    cls = m.group(1)
    if cls not in _REASON_CLASSES:
        cls = "other"
    return cls, d[m.end():]


# J-6 C：Bad MAC（libsignal 解密对端消息失败）自愈读数。Node 边车按 (login, 对端) 计
# 连击、达阈删对端 Signal 会话后，把**该登录的累计快照**以 ``[bm:total=T,peers=P,
# heals=H,active=A]`` 标签写进 authorized 上报的 detail（同 rc 前缀的套路：不改
# session-status 路由契约）。这里解成 dict 存每会话最新快照 → dump().bad_mac 聚合
# 进 ops 卡。快照语义（非增量）＝上报频率不影响正确性，Node 侧节流上报即可。
_BM_TAG_RE = re.compile(r"\[bm:([a-z_]+=\d+(?:,[a-z_]+=\d+){0,7})\]")
_BM_KEYS = ("total", "peers", "heals", "active")


def parse_bad_mac(detail: str) -> Dict[str, int]:
    """从 detail 里的 ``[bm:k=v,...]`` 标签解 Bad MAC 快照（纯函数）。

    无标签 → ``{}``；只收 ``_BM_KEYS`` 内的键（其余忽略），值取非负整数。
    """
    m = _BM_TAG_RE.search(str(detail or ""))
    if not m:
        return {}
    out: Dict[str, int] = {}
    for kv in m.group(1).split(","):
        k, _, v = kv.partition("=")
        if k in _BM_KEYS:
            try:
                out[k] = max(0, int(v))
            except ValueError:
                continue
    return out


class PlatformSessionHealth:
    """外部 worker 会话状态登记（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_sessions", "total_events",
                 "_by_status", "_by_reason_class", "_inbox_health",
                 "total_stall_went", "total_stall_recovered", "total_relogin")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        # key = "platform:account_id" → {status, detail, login_id, ts, changes}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self.total_events = 0
        self._by_status: Dict[str, int] = {}
        # J-6 B：不健康事件按 close 原因类聚合（进程累计；只数带 [rc:] 前缀的事件）
        self._by_reason_class: Dict[str, int] = {}
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
        # J-6 B：解 Node 侧 [rc:<class>] 前缀。detail 保留原样（含前缀）——横幅/旧
        # 消费面直显时「[rc:dns] …」本身就是对坐席有用的信息；reason_class 另存一份
        # 供聚合。健康态（authorized）不带前缀，解出空串即清掉上一轮原因。
        reason_class, _ = parse_reason_class(detail)
        bad_mac = parse_bad_mac(detail)  # J-6 C：{} = 本次上报不带快照（不清旧值）
        with self._lock:
            self.total_events += 1
            self._by_status[st] = self._by_status.get(st, 0) + 1
            if reason_class and st in UNHEALTHY_STATUSES:
                self._by_reason_class[reason_class] = (
                    self._by_reason_class.get(reason_class, 0) + 1)
            sess = self._sessions.get(key)
            if sess is None:
                if len(self._sessions) >= _MAX_KEYS:
                    # 超限：不再收新 key（防刷量），但事件计数仍累计
                    return {"changed": False, "went_unhealthy": False,
                            "recovered": False, "prev": "", "status": st,
                            "superseded": []}
                sess = {"status": "", "detail": "", "login_id": "", "ts": 0.0,
                        "changes": 0, "unhealthy_since": 0.0,
                        "last_remind_ts": 0.0, "reason_class": "",
                        "bad_mac": {}}
                self._sessions[key] = sess
            prev = str(sess.get("status") or "")
            changed = prev != st
            if changed:
                sess["changes"] = int(sess.get("changes") or 0) + 1
            now = time.time()
            sess["status"] = st
            _detail = str(detail or "")
            if bad_mac:
                sess["bad_mac"] = bad_mac
                # 标签只是运输载体，不进直显 detail（authorized 行 detail 本就少见）
                _detail = _BM_TAG_RE.sub("", _detail).strip()
            sess["detail"] = _detail[:300]
            sess["reason_class"] = reason_class
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
            # 晋级清占位（P0 2026-08-14）：login_id 授权成功晋级为真实 account_id
            # 后，同一次登录早期以 login_id 为 key 挂的占位行（authorizing/expired…）
            # 就成了永远无人更新的孤儿——不清会以「另一个号」的身份在横幅/看门狗里
            # 常亮。此前只有 _ORPHAN_LOGIN_TTL_SEC 超时兜底（要等 30 分钟）。
            superseded: list = []
            if st in HEALTHY_STATUSES:
                lid = _san(login_id)
                if lid:
                    ghost = f"{_san(platform, 24).lower()}:{lid}"
                    if ghost != key:
                        # 注意别用 or 短路：会话行/入站健康行要**各自**弹出
                        g1 = self._sessions.pop(ghost, None) is not None
                        g2 = self._inbox_health.pop(ghost, None) is not None
                        if g1 or g2:
                            logger.info("[session_health] 登录晋级 %s → %s，"
                                        "清除占位行", ghost, key)
                    # 身份接替清账（2026-08-27 实锤：登录档案 msg_* 先属 Calixa、
                    # 重登时被改登成 Wisley——旧账号在本节点已无档案，其不健康
                    # 记录永远等不到 authorized，横幅/催办就成了删不掉的僵尸）。
                    # 同一 login_id 被另一账号授权 ⇒ 挂在该 login_id 上的其他
                    # 账号行确定性地「被顶替」：翻成 logged_out（发送前快速失败
                    # 语义不变；横幅刻意不显示 logged_out、催办由注册表标记停），
                    # 入站健康行整行弹出（档案已易主，旧读数是另一个号的陈迹）。
                    # 注册表 offline_reason 落盘由调用方经
                    # ``mark_superseded_accounts`` 处理（本类保持零依赖）。
                    for okey, osess in self._sessions.items():
                        if okey == key:
                            continue
                        if _san(str(osess.get("login_id") or "")) != lid:
                            continue
                        if str(osess.get("status")) not in UNHEALTHY_STATUSES:
                            continue
                        osess["status"] = "logged_out"
                        osess["detail"] = f"superseded by {key}"[:300]
                        osess["ts"] = now
                        osess["changes"] = int(osess.get("changes") or 0) + 1
                        self._by_status["logged_out"] = (
                            self._by_status.get("logged_out", 0) + 1)
                        self._inbox_health.pop(okey, None)
                        superseded.append(okey.partition(":")[2])
                        logger.info(
                            "[session_health] 登录档案 %s 已被 %s 接替，"
                            "旧账号记录 %s 翻为 logged_out", lid, key, okey)
            return {"changed": changed, "went_unhealthy": went_unhealthy,
                    "recovered": recovered, "prev": prev, "status": st,
                    "superseded": superseded}

    def is_unhealthy(self, platform: str, account_id: str) -> bool:
        """该会话最近一次上报是否处于不健康态（未上报过 → False，不拦）。"""
        with self._lock:
            sess = self._sessions.get(self._key(platform, account_id))
            return bool(sess and str(sess.get("status")) in UNHEALTHY_STATUSES)

    def session_status(self, platform: str, account_id: str) -> Dict[str, Any]:
        """该会话最近一次上报的快照副本（未上报过 → ``{}``）。M-2 A2 通道真相读口。"""
        with self._lock:
            sess = self._sessions.get(self._key(platform, account_id))
            return dict(sess) if sess else {}

    def _sweep_login_ghosts_locked(self, now: float) -> None:
        """清理超龄「登录尝试幽灵」（须持锁调用）。

        判据三合一（缺一不清，宁可漏清不误清）：
        ① 状态不健康**或 abandoned**（放弃的登录尝试——不进横幅但行仍占 key 槽，
        _MAX_KEYS=64，反复放弃登录会把槽位吃光挤掉真实账号的登记；放弃率观测走
        ``by_status`` 事件计数，不依赖行存活）；② key 的账号段 == 记录的 login_id
        （=上报时 account_id 为空，从未晋级成真实账号——真实账号 key 是数字/平台
        id，绝不等于 msg_* 临时 id）；③ 持续超 ``_ORPHAN_LOGIN_TTL_SEC``。
        挂在读路径（横幅快照/看门狗/dump）入口惰性触发，零后台线程。
        """
        for key in list(self._sessions.keys()):
            sess = self._sessions[key]
            st = str(sess.get("status"))
            if st not in UNHEALTHY_STATUSES and st != ABANDONED_STATUS:
                continue
            lid = str(sess.get("login_id") or "")
            if not lid or key.partition(":")[2] != lid:
                continue
            since = (float(sess.get("unhealthy_since") or 0.0)
                     or float(sess.get("ts") or 0.0))
            if since and (now - since) >= _ORPHAN_LOGIN_TTL_SEC:
                self._sessions.pop(key, None)
                logger.info("[session_health] 清理登录尝试幽灵 %s（状态 %s 滞留 "
                            "%d 分钟，从未绑定真实账号）",
                            key, st, int((now - since) // 60))

    def unhealthy_sessions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._sweep_login_ghosts_locked(time.time())
            return {k: dict(v) for k, v in self._sessions.items()
                    if str(v.get("status")) in UNHEALTHY_STATUSES}

    def due_reminders(self, *, min_age_sec: float, interval_sec: float,
                      now: Optional[float] = None,
                      stale_after_sec: float = 0.0,
                      stale_interval_sec: float = 0.0,
                      ) -> Dict[str, Dict[str, Any]]:
        """待提醒的「持续不健康」会话（供 HealthWatchdog 周期复查）。

        语义（升级式）：掉线后 ``min_age_sec`` 仍未恢复 → 第一条提醒（「还没人修」），
        之后每 ``interval_sec`` 一条（防唠叨）。返回即视为「本轮要提醒」——原子标记
        ``last_remind_ts``，并发/下轮不重复。恢复时 ``record()`` 会清零两个时间戳。

        催办衰减（2026-08-27，防告警疲劳）：``stale_after_sec``/``stale_interval_sec``
        同时 >0 时，掉线超过 ``stale_after_sec`` 的会话改按 ``stale_interval_sec``
        节流（取与常规 interval 的较大者）——挂了两天没人修的号每 4h 轰一次只会让
        人把整个频道静音，降为日更保底比「响到没人听」更能保住告警可信度。
        """
        ts = time.time() if now is None else float(now)
        due: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            self._sweep_login_ghosts_locked(ts)
            for key, sess in self._sessions.items():
                if str(sess.get("status")) not in UNHEALTHY_STATUSES:
                    continue
                since = float(sess.get("unhealthy_since") or 0.0)
                if not since or (ts - since) < float(min_age_sec):
                    continue
                eff_interval = float(interval_sec)
                is_stale = (float(stale_after_sec) > 0
                            and float(stale_interval_sec) > 0
                            and (ts - since) >= float(stale_after_sec))
                if is_stale:
                    eff_interval = max(eff_interval, float(stale_interval_sec))
                last = float(sess.get("last_remind_ts") or 0.0)
                if last and (ts - last) < eff_interval:
                    continue
                sess["last_remind_ts"] = ts
                out = dict(sess)
                out["down_sec"] = ts - since
                out["stale"] = is_stale
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
        pin_state: str = "",
        pin_heal_attempts: int = -1, pin_heal_ok: int = -1,
        pin_heal_fail: int = -1,
        unread_forced_picked: int = -1,
        worker_code_stale: Optional[bool] = None,
        worker_code_fp: str = "",
        requests_suspect_streak: int = -1,
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
        # 支四（P0 2026-08-14，173 实测盲区）：worker 亲证 PIN 浮层在场而 PIN
        # 缺失/未通过（detail=e2ee_pin_required 只在 detectPinPrompt 确认浮层后
        # 才携带）→ 无条件半死。前三支全依赖占位比 ≥0.6 / 读取窗全败——173 实测
        # 占位比 0.39 时 PIN 明明缺失却一路「健康」，stall_since=0 把弹窗/横幅/
        # 看门狗全闸掉。浮层本身就是确定性证据，不需要统计旁证。
        raw_detail = str(detail or "")[:300]
        pin_required = raw_detail == "e2ee_pin_required"
        stalled = full_fail or placeholder_blind or steady_fail or pin_required
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
                          else "steady_fail" if steady_fail
                          else "pin_missing" if pin_required else "")
            h["stall_kind"] = stall_kind
            h["detail"] = raw_detail
            # P1 PIN 自愈观测（2026-08-14）：worker 侧 tryAutoE2eePin 的战绩随心跳
            # 落行（-1=老 worker 未上报，不写键——dump 消费面据缺键区分「没报」与
            # 「报了 0」）。「托管 PIN 后有没有真自愈」从翻 sidecar 日志变成看板可读。
            h["pin_state"] = str(pin_state or "")[:16]
            try:
                if int(pin_heal_attempts) >= 0:
                    h["pin_heal"] = {
                        "attempts": max(0, int(pin_heal_attempts)),
                        "ok": max(0, int(pin_heal_ok or 0)),
                        "fail": max(0, int(pin_heal_fail or 0)),
                    }
            except (TypeError, ValueError):
                pass
            # P1 2026-08-15 messenger-web 盲区修复观测（缺键=老 worker 未上报，不写——
            # dump 消费面据缺键区分「没报」与「报了 0」，与 pin_heal 同约定）：
            # unread_forced   = 未读驱动强制读取触发次数（E2EE 占位预览盲区的解药读数）；
            # worker_code_*   = worker 代码指纹 + 半部署位（磁盘代码晚于进程启动=改了没
            #                   重启 Node——「修复写好了没上线」从不可见变成看板可判）；
            # requests_suspect_streak = 消息请求页「零行且无结构证据」连续次数（区分
            #                   「真没人来」与「FB 改版读不到」）。
            try:
                if int(unread_forced_picked) >= 0:
                    h["unread_forced"] = max(0, int(unread_forced_picked))
            except (TypeError, ValueError):
                pass
            if worker_code_stale is not None:
                h["worker_code_stale"] = bool(worker_code_stale)
            fp = _san(str(worker_code_fp or ""), 16)
            if fp:
                h["worker_code_fp"] = fp
            try:
                if int(requests_suspect_streak) >= 0:
                    h["requests_suspect"] = max(0, int(requests_suspect_streak))
            except (TypeError, ValueError):
                pass
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
            self._sweep_login_ghosts_locked(time.time())
            sessions = {k: dict(v) for k, v in sorted(self._sessions.items())}
            unhealthy = [k for k, v in sessions.items()
                         if str(v.get("status")) in UNHEALTHY_STATUSES]
            inbox = {k: dict(v) for k, v in sorted(self._inbox_health.items())}
            stalled = [k for k, v in inbox.items()
                       if float(v.get("stall_since") or 0.0)]
            bad_mac = self._bad_mac_aggregate_locked()
            return {
                "started_at": self._started_at,
                "total_events": self.total_events,
                "by_status": dict(sorted(self._by_status.items())),
                # J-6 B：不健康事件按 close 原因类（dns/net/server/forbidden/…）
                "by_reason_class": dict(sorted(self._by_reason_class.items())),
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
                # J-6 C：Bad MAC 自愈聚合（各账号最新快照求和；accounts=有过 Bad MAC 的账号数）
                "bad_mac": bad_mac,
            }

    def _bad_mac_aggregate_locked(self) -> Dict[str, int]:
        agg = {k: 0 for k in _BM_KEYS}
        accounts = 0
        for sess in self._sessions.values():
            bm = sess.get("bad_mac") or {}
            if not bm:
                continue
            accounts += 1
            for k in _BM_KEYS:
                agg[k] += int(bm.get(k) or 0)
        agg["accounts"] = accounts
        return agg

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
            if self._by_reason_class:
                lines += [
                    "# HELP platform_session_close_reason_total Unhealthy session "
                    "events by close reason class (dns/net/server/forbidden/...)",
                    "# TYPE platform_session_close_reason_total counter",
                ]
                for rc, n in sorted(self._by_reason_class.items()):
                    lines.append(
                        f'platform_session_close_reason_total{{reason_class="{_esc(rc)}"}} {int(n)}')
            _bm = self._bad_mac_aggregate_locked()
            if _bm.get("accounts"):
                lines += [
                    "# HELP platform_session_bad_mac_total libsignal Bad MAC decrypt "
                    "failures reported by sidecars (sum of latest per-account snapshots)",
                    "# TYPE platform_session_bad_mac_total gauge",
                    f"platform_session_bad_mac_total {int(_bm['total'])}",
                    "# HELP platform_session_bad_mac_heals_total Peer Signal sessions "
                    "deleted by Bad MAC self-heal",
                    "# TYPE platform_session_bad_mac_heals_total gauge",
                    f"platform_session_bad_mac_heals_total {int(_bm['heals'])}",
                ]
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

    def record_relogin(self, platform: str, account_id: str,
                       *, by: str = "") -> None:
        """登记一次人工「重新登录」触发（ops/坐席 CTA）——漏斗中段。

        2026-08-27 状态中心 v2：痕迹同时落**会话行**（``last_relogin_ts/by``）——
        横幅快照读的是 _sessions（死号常无 inbox_health 行，旧版痕迹在那种账号上
        必丢）；多坐席值守时「已有人触发过重登」经快照全员可见，防重复处理。
        ``by`` 只截断不消毒（用户名可含 CJK；仅进 JSON 展示，无注入面）。
        """
        key = self._key(platform, account_id)
        now = time.time()
        actor = str(by or "")[:24]
        with self._lock:
            self.total_relogin += 1
            h = self._inbox_health.get(key)
            if h is not None:
                h["last_relogin_ts"] = now
            sess = self._sessions.get(key)
            if sess is not None:
                sess["last_relogin_ts"] = now
                if actor:
                    sess["last_relogin_by"] = actor

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


def session_expected_online(key: str) -> bool:
    """会话键（``platform:account_id``）对应账号是否仍被**期望在线**（单一策略源）。

    原为 ``HealthWatchdog._session_expected_online``（催办过滤专用）；2026-08-27
    状态中心 v2 把坐席横幅快照也接到同一判据上——此前两面口径分裂：看门狗对
    「运营已登出/已接替/已删除」的号不催，横幅却照亮（worker 重启用陈旧 cookie
    重推一次 needs_login 就能把处理完的号再点红，Calixa 僵尸的最后一条上游通路）。
    判据取注册表持久事实：

    - ``online`` → True（编排器会拉起、掉了就该有人修）；
    - ``offline`` 且 ``meta.offline_reason`` 以 ``worker:`` 开头（自灭型掉线，
      没人决定退役它）→ True 继续催/亮；``operator``（运营主动登出）、
      ``superseded:*``（登录位已被新账号接替）、无标记（来历不明）→ False；
    - 无注册表行 / ``removed`` → False（登录尝试幽灵 msg_* 也归此类——横幅上
      一个坐席看不懂、无从操作的临时 id 本就不该亮，2026-08-14 实锤）；
    - 注册表读取异常 → True（fail-open：不因巡检自身故障漏报真掉线）。
    """
    plat, _, acct = str(key or "").partition(":")
    if not plat or not acct:
        return True
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(plat, acct)
    except Exception:
        return True
    if not row:
        return False
    status = str(row.get("status") or "")
    if status == "online":
        return True
    if status == "offline":
        reason = str((row.get("meta") or {}).get("offline_reason") or "")
        return reason.startswith("worker:")
    return False


#: #196：按账号静默断线提醒的注册表 meta 键（-1＝不再提醒此账号；>0＝静默到该 epoch 秒；
#: 0/缺省＝不静默）。存服务端而非只存浏览器 localStorage——坐席换机/换浏览器不丢。
CHANDOWN_MUTE_META_KEY = "chandown_mute_until"
#: #196「标为已停用」落的 offline_reason 前缀：不以 worker: 开头 → session_expected_online
#: 判 False → 横幅不亮、看门狗不催；重新登录归位时随 offline_reason 一并清掉。
DISABLED_OFFLINE_REASON = "operator:disabled"


def channel_alert_mute_until(key: str) -> float:
    """会话键（``platform:account_id``）的断线提醒静默水位：-1 永久 / 0 无 / epoch 秒。"""
    plat, _, acct = str(key or "").partition(":")
    if not plat or not acct:
        return 0.0
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(plat, acct) or {}
        return float((row.get("meta") or {}).get(CHANDOWN_MUTE_META_KEY) or 0)
    except Exception:
        return 0.0


def channel_alert_muted(key: str, now: Optional[float] = None) -> bool:
    """断线提醒是否被按账号静默（#196）。到期的静默自然失效，不用清。"""
    until = channel_alert_mute_until(key)
    if until < 0:
        return True
    if until <= 0:
        return False
    ts = time.time() if now is None else float(now)
    return until > ts


def set_channel_alert_mute(platform: str, account_id: str, *,
                           hours: Optional[float], now: Optional[float] = None) -> float:
    """写按账号静默（#196）：``hours=None`` ＝永久（-1）；``hours<=0`` ＝取消静默；
    否则静默到 now+hours。返回落盘的水位；账号不在注册表 → 0（无处可存，调用方回落本机）。"""
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    if not plat or not acct:
        return 0.0
    ts = time.time() if now is None else float(now)
    if hours is None:
        until = -1.0
    elif float(hours) <= 0:
        until = 0.0
    else:
        until = ts + float(hours) * 3600.0
    try:
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        if reg.get(plat, acct) is None:
            return 0.0
        reg.upsert(plat, acct, meta={CHANDOWN_MUTE_META_KEY: until}, merge_meta=True)
    except Exception:
        logger.debug("[session_health] 断线静默落盘失败（忽略）", exc_info=True)
        return 0.0
    logger.info("[session_health] 断线提醒静默 %s:%s until=%s", plat, acct,
                "forever" if until < 0 else (f"{until:.0f}" if until else "cleared"))
    return until


def mark_account_disabled(platform: str, account_id: str, *, actor: str = "") -> bool:
    """#196「标为已停用」：注册表 status=offline + offline_reason=operator:disabled。

    效果链全走既有判据：``session_expected_online`` → False（横幅不亮 / 看门狗不催）；
    收件箱账号栏按 offline 灰显；编排器不再拉起（offline 不在活跃集）。凭据**不清**
    ——与登出不同，停用是「先别管它」，重新登录一次即归位（归位路径清 offline_reason）。
    账号不在注册表 → False。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    if not plat or not acct:
        return False
    try:
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        if reg.get(plat, acct) is None:
            return False
        reg.upsert(plat, acct, status="offline",
                   meta={"offline_reason": DISABLED_OFFLINE_REASON,
                         "disabled_at": time.time(), "disabled_by": str(actor or "")[:48]},
                   merge_meta=True)
    except Exception:
        logger.debug("[session_health] 标停用落盘失败（忽略）", exc_info=True)
        return False
    try:
        get_platform_session_health().record(plat, acct, "logged_out",
                                             detail="operator disabled")
    except Exception:
        logger.debug("[session_health] 标停用同步健康表失败（忽略）", exc_info=True)
    logger.info("[session_health] 账号标为已停用 %s:%s by=%s（看门狗跳过 / 横幅不亮 / 账号栏灰显）",
                plat, acct, actor or "?")
    return True


def mark_superseded_accounts(platform: str, accounts: Any,
                             new_account: str) -> None:
    """身份接替的注册表落盘（best-effort，绝不抛）。

    ``record()`` 判出「登录档案被新账号接替」后，旧账号的注册表催办标记
    ``offline_reason=worker:*`` 改写为 ``superseded:<新账号>``——看门狗的
    「死号持续提醒」只认 ``worker:`` 前缀，改写即自然停催。**只动** offline
    且带 worker:* 标记的行：``operator``（运营主动登出）与空标记本就不催、
    审计语义不该被覆盖；online 行说明注册表仍期望它在线（该不该下线是运营
    决策），不在此替人做主。
    """
    if not accounts:
        return
    try:
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        for acct in accounts:
            acct = str(acct or "")
            if not acct:
                continue
            row = reg.get(platform, acct)
            if not row or str(row.get("status") or "") != "offline":
                continue
            reason = str((row.get("meta") or {}).get("offline_reason") or "")
            if not reason.startswith("worker:"):
                continue
            reg.upsert(platform, acct, meta={
                "offline_reason": f"superseded:{new_account}"[:80],
            }, merge_meta=True)
            logger.info("[session_health] 注册表标记 %s:%s 已被 %s 接替"
                        "（停止死号催办）", platform, acct, new_account)
    except Exception:
        logger.debug("[session_health] superseded 注册表落盘失败（忽略）",
                     exc_info=True)


def report_session_transition(
    platform: str, account_id: str, status: str, *,
    detail: str = "", login_id: str = "",
) -> Dict[str, Any]:
    """进程内统一上报会话状态转移（与 ``/api/internal/protocol/session-status``
    路由同语义的库入口，供编排器等**本进程**调用方使用——不必绕 HTTP 自打自）。

    做三件事（全部 best-effort，绝不抛）：
    1. 落健康表 ``record``（→ 坐席顶栏/ops 卡/watchdog 升级提醒同一数据源）；
    2. 注册表持久化：``logged_out/needs_login/expired`` 且注册表 online → offline
       并落 ``meta.offline_reason="worker:<status>"``（自灭标记——看门狗对 offline
       账号的持续提醒只认它，与运营主动登出的 ``operator`` 标记区分；健康表是
       内存态，重启后靠 ``seed_offline_from_registry`` 接续真相）；
       ``authorized`` 且非 removed → online（重登成功自动归位并清标记）。
       ``failed`` 刻意不落——可自愈故障，标「已退出」会误导运营手动重登；
    3. 「进入不健康/恢复」两类转移经 EventBus 发 ``platform_session_alert``
       （订阅别名 ``platform_session``；rate_key 按 平台:账号 独立限流）。

    返回 ``record`` 的转移结果 dict（changed/went_unhealthy/recovered）。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    st = str(status or "").lower()
    if not plat or not acct or not st:
        return {"changed": False, "went_unhealthy": False, "recovered": False}
    trans = get_platform_session_health().record(
        plat, acct, st, detail=detail, login_id=login_id)
    if trans.get("superseded"):
        mark_superseded_accounts(plat, trans["superseded"], acct)
    # M-2（D-M1 / #233）：authorized ＝ 通道就绪的唯一确定信号 → ① 账号级登录门禁
    # 判「真实登录」（新 login_id / 从不健康恢复 → 冷静期 + 登录后默认半自动）；
    # ② 重置自动投递退避（「登录成功即解锁」）。同 login_id 重推（边车/后端重启、
    # WA 自动重连）在 note_login 内部按 False 落空，不会把在跑的号拖进冷静期。
    if st in HEALTHY_STATUSES:
        try:
            from src.inbox.account_channel_gate import note_login, reset_backoff
            # 从「要人重新认证」的状态恢复＝真重登（needs_login/logged_out/expired）；
            # 从 failed（瞬时故障）/ blocked / forbidden（平台限制）恢复不是登录，
            # 不开冷静期——WA 网络抖动重连一天几十次，按登录算就永远在冷静期。
            _relogin = (bool(trans.get("recovered"))
                        and str(trans.get("prev") or "") in LOGIN_RECOVERY_STATUSES)
            note_login(plat, acct, login_id=login_id, force=_relogin)
            reset_backoff(plat, acct, why=f"session {st}")
        except Exception:
            logger.debug("[session_health] 登录门禁登记失败（忽略）", exc_info=True)
    try:
        from src.integrations.account_registry import get_account_registry
        _reg = get_account_registry()
        _rst = str((_reg.get(plat, acct) or {}).get("status") or "")
        # forbidden（J-6 A）：403 终态与登出同待遇——账号在平台侧已不可用，注册表
        # 翻 offline + worker:forbidden 标记，编排器不再把它当「该在线却不在」反复护送
        # 重连（Node 侧另有 /reconnect 解锁冷却双保险）。
        if (st in ("logged_out", "needs_login", "expired", "forbidden")
                and _rst == "online"):
            # 自灭型掉线（worker 上报，非运营操作）：翻状态之外落 offline_reason
            # 标记（merge 不动其他 meta 键）。看门狗对 offline 账号的持续提醒
            # **只认 worker:* 标记**——没有它，死号在一次转移告警后就永远静音
            # （2026-08-16 实锤：messenger 号 7/30 崩溃循环自灭，注册表 offline
            # 被当「运营主动下线」过滤，17 天零提醒）。运营主动登出走
            # ``_clear_session_creds``，会把标记改写成 ``operator``。
            _reg.upsert(plat, acct, status="offline",
                        meta={"offline_reason": f"worker:{st}"},
                        merge_meta=True)
        elif st == "authorized" and _rst and _rst not in ("removed", "online"):
            # 重登成功归位时清掉离线原因标记，下一次自灭从头判定
            _reg.upsert(plat, acct, status="online",
                        meta={"offline_reason": ""}, merge_meta=True)
    except Exception:
        logger.debug("[session_health] 注册表状态同步失败", exc_info=True)
    if trans.get("went_unhealthy") or trans.get("recovered"):
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("platform_session_alert", {
                "platform": plat,
                "account_id": acct,
                "login_id": login_id,
                "status": st,
                "detail": detail,
                "recovered": bool(trans.get("recovered")),
                "rate_key": f"{plat}:{acct}",
            })
        except Exception:
            logger.debug("[session_health] 会话健康告警发布失败", exc_info=True)
    return trans


# ── M-2 A2（#232，2026-09-06）：账号级「通道连接真相」单一出口 ─────────────────
#: 由外部边车 / 编排器 worker 保活连接的平台——「已登录」必须以边车会话在场为准，
#: 注册表 online 只是「期望在线」。协议直连（telegram 主客户端）不在此列：其连接
#: 真相是 pyrogram 自身，编排器 worker 状态即可。
SIDECAR_PLATFORMS = frozenset({"messenger", "whatsapp", "zalo", "instagram", "line",
                               # QQ 协议登录：连接由用户自装的协议端（Milky）保活，worker 只监督
                               "qq"})

#: 连接态词汇（前端账号栏 / 会话头部 / 发送闸共用）
CHANNEL_CONNECTED = "connected"
CHANNEL_DISCONNECTED = "disconnected"
CHANNEL_RECONNECTING = "reconnecting"
CHANNEL_UNKNOWN = "unknown"

#: 阻止发送的机器可读原因码（前端据此出人话 + 显「重新登录」出路）
SEND_BLOCK_CHANNEL_DISCONNECTED = "channel_disconnected"


def channel_connection_state(platform: str, account_id: str, *,
                             now: Optional[float] = None) -> Dict[str, Any]:
    """账号通道此刻到底通不通（#232 CW6RP4「智聊界面已登录与边车会话脱节」的解药）。

    返回 ``{state, reason, detail, since}``：

    - ``disconnected``：注册表 offline/removed（运营登出 / 自灭 / 停用）；或健康表
      最近一次上报为不健康（needs_login / expired / logged_out / failed / blocked /
      forbidden——含编排器 ``healthy()`` 探到边车没有该账号会话时上报的 needs_login）；
      或编排器 worker 连败放弃（restarts ≥ MAX_RESTARTS）。
    - ``reconnecting``：编排器 worker 处于 error/starting 退避重启窗（通道短暂不可用）。
    - ``connected``：健康表 authorized，或 worker running 且健康表无不健康记录。
    - ``unknown``：无任何登记（未托管的 RPA / 主协议号 / 测试）——调用方**不拦**。

    只读、零 IO 抛出面：任何一层取数失败按该层不表态，最终至少返回 unknown。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    ts = time.time() if now is None else float(now)
    out: Dict[str, Any] = {"state": CHANNEL_UNKNOWN, "reason": "", "detail": "",
                           "since": 0.0}
    if not plat or not acct:
        return out
    # ① 注册表持久事实（peek：只读现有单例，纯单测 / 无编排器部署绝不隐式建库）
    try:
        from src.integrations.account_registry import peek_account
        row = peek_account(plat, acct) or {}
        rst = str(row.get("status") or "")
        if rst in ("offline", "removed"):
            reason = str((row.get("meta") or {}).get("offline_reason") or rst)
            out.update(state=CHANNEL_DISCONNECTED, reason=f"registry:{rst}",
                       detail=reason[:120],
                       since=float(row.get("updated_at") or 0.0))
            return out
    except Exception:
        pass
    # ② 健康表（Node push / 编排器探测上报）
    sess: Dict[str, Any] = {}
    try:
        sess = get_platform_session_health().session_status(plat, acct)
    except Exception:
        sess = {}
    sst = str(sess.get("status") or "")
    if sst in UNHEALTHY_STATUSES:
        out.update(state=CHANNEL_DISCONNECTED, reason=sst,
                   detail=str(sess.get("detail") or "")[:120],
                   since=float(sess.get("unhealthy_since") or sess.get("ts") or ts))
        return out
    # ③ 编排器 worker 状态
    m: Optional[Dict[str, Any]] = None
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is not None and hasattr(orch, "managed_state"):
            m = orch.managed_state(plat, acct)
    except Exception:
        m = None
    if m:
        mstate = str(m.get("state") or "")
        if mstate in ("error", "starting", "stopping"):
            if bool(m.get("gave_up")):
                out.update(state=CHANNEL_DISCONNECTED, reason="worker_gave_up",
                           detail=str(m.get("last_error") or "")[:120],
                           since=float(m.get("updated_at") or ts))
            else:
                out.update(state=CHANNEL_RECONNECTING, reason=f"worker_{mstate}",
                           detail=str(m.get("last_error") or "")[:120],
                           since=float(m.get("updated_at") or ts))
            return out
        if mstate == "running":
            out.update(state=CHANNEL_CONNECTED, reason="worker_running",
                       detail=str(sess.get("detail") or "")[:120],
                       since=float(sess.get("ts") or m.get("updated_at") or 0.0))
            return out
    # ④ 健康表 authorized（无编排器 / worker 已停但边车仍在）
    if sst in HEALTHY_STATUSES:
        out.update(state=CHANNEL_CONNECTED, reason="session_authorized",
                   since=float(sess.get("ts") or 0.0))
        return out
    return out


def channel_send_block_reason(platform: str, account_id: str, *,
                              now: Optional[float] = None) -> Dict[str, Any]:
    """发送前问一句「通道通不通」：``{"reason": "" | channel_disconnected, "state": …}``。

    只在 **disconnected** 时拦（确定性事实：边车没会话 / 已登出 / 放弃重连）——
    reconnecting 不拦（发出去会诚实地 5xx 失败并留痕，比在重连窗口一律拒发更少误伤）；
    unknown 不拦（未托管形态本函数不表态）。自动链与手动链**同一判定**：
    UE7VM3 ③「未连接时禁止自动投递并阻止手动发送」。
    """
    cs = channel_connection_state(platform, account_id, now=now)
    out = dict(cs)
    out["state_reason"] = str(cs.get("reason") or "")
    out["reason"] = (SEND_BLOCK_CHANNEL_DISCONNECTED
                     if cs.get("state") == CHANNEL_DISCONNECTED else "")
    return out


# B63-②（实施64 P1-4，`_314`/`_322`）：发送失败里的**会话性** reason_code →
# 会话健康登记映射。skuio 实录 7/7 全失败每条只静默 500——PIN 浮层/接受浮层/
# 登出态是账号级处境，不是逐条消息的事，必须点亮账号卡/横幅/看门狗同一数据源。
# needs_login 会连带翻注册表 offline（report_session_transition 既有语义）；
# PIN/接受浮层用 failed（会话还活着，只是要人工一步，不翻注册表）。
_SEND_FAIL_SESSION_MAP = (
    (("e2ee_pin",), "failed", "e2ee_pin_required"),
    (("needs_accept", "accept_prompt"), "failed", "needs_accept"),
    (("logged_out", "not_logged_in", "login_required", "session_expired",
      "checkpoint_required"), "needs_login", ""),
)


def note_send_auth_failure(platform: str, account_id: str,
                           error_text: Any) -> str:
    """发送失败文本含会话性 reason_code → 登记会话不健康；返回登记的 status
    （空串=普通发送失败，不登记）。重复同态由 record 去重，绝不抛。"""
    try:
        low = str(error_text or "").lower()
        if not low or not platform or not account_id:
            return ""
        for markers, status, detail in _SEND_FAIL_SESSION_MAP:
            if any(m in low for m in markers):
                report_session_transition(
                    str(platform), str(account_id), status,
                    detail=(detail or low[:120]))
                return status
    except Exception:
        logger.debug("[session_health] 发送失败会话登记异常（忽略）",
                     exc_info=True)
    return ""


__all__ = [
    "PlatformSessionHealth", "get_platform_session_health",
    "ensure_seeded_from_registry", "report_session_transition",
    "note_send_auth_failure", "mark_superseded_accounts",
    "session_expected_online",
    "channel_alert_mute_until", "channel_alert_muted", "set_channel_alert_mute",
    "mark_account_disabled", "CHANDOWN_MUTE_META_KEY", "DISABLED_OFFLINE_REASON",
    "UNHEALTHY_STATUSES", "HEALTHY_STATUSES", "ABANDONED_STATUS",
    "channel_connection_state", "channel_send_block_reason", "SIDECAR_PLATFORMS",
    "CHANNEL_CONNECTED", "CHANNEL_DISCONNECTED", "CHANNEL_RECONNECTING", "CHANNEL_UNKNOWN",
    "SEND_BLOCK_CHANNEL_DISCONNECTED",
]
