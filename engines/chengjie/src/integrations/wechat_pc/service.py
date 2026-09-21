# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 轮询服务骨架（实施97 线 B）。

一次 :meth:`WeChatPcService.tick`：

1. 巡检：``backend.screen_state()`` → :func:`risk_screens.assess`——登录窗/手机确认/环境异常/限频
   → 冻结发送（TTL）、标记 offline、通知主人（``notify`` 回调）；只读态下仍允许读屏。
2. 入站：有未读的会话（每 tick 最多 ``max_sessions_per_tick`` 个）→ 打开 → 校标题 → 读可见气泡 →
   按 RuntimeId/指纹去重 → 对方新气泡 ``direction=in``、己方新气泡（主人在手机/电脑亲手回的）
   ``direction=out`` 镜像 → ``POST /api/desktop/ingest``（平台 ``wechat``，首见即以 mode=desktop 入册）。
3. 出站（仅 semi/auto_reply 档且未冻结）：``GET /api/desktop/outbound`` 认领 → :func:`policy.may_send`
   裁决 → :class:`GuardedSender` 五步发送 → ``ack``。守卫在 title/echo 步失败 → **冻结本账号自动发送**
   ``freeze_on_guard_fail_sec``，绝不盲重试；裁决不通过 → ack 失败（进人审队列可见）。

每 tick 顺序：巡检 → 读会话列表 → **先出站**（上轮已生成的回复不等本轮逐个开未读会话）→ 入站 → 有新入站再补一次出站。

HTTP 经 :class:`BridgeClient`（``http`` 可注入）；时间/睡眠可注入；所有异常在 tick 内吞掉并计数。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib import request as _urlreq

from src.integrations.wechat_pc import BRIDGE_KIND, PLATFORM
from src.integrations.wechat_pc.backend import Bubble, WeChatPcBackend
from src.integrations.wechat_pc.identity import (
    ChatIdentityCache, chat_key_kind, is_group_title, normalize_display_name,
)
from src.integrations.wechat_pc.policy import PcPolicy, caps_relaxed, may_send, resolve_policy
from src.integrations.wechat_pc.risk_screens import NONE, assess, disposition_for
from src.integrations.wechat_pc.desktop_input import desktop_input
from src.integrations.wechat_pc.send_guard import GuardedSender, SendOutcome, verify_title

logger = logging.getLogger(__name__)


class BridgeClient:
    """智聊后端桌面桥 HTTP 客户端（Bearer token）。``http(method, url, body|None) -> (status, dict)`` 可注入。"""

    def __init__(self, base_url: str, token: str, *, http: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
                 timeout_sec: float = 15.0) -> None:
        self.base_url = str(base_url or "http://127.0.0.1:18799").rstrip("/")
        self.token = str(token or "")
        self._http = http
        self.timeout_sec = timeout_sec

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        url = self.base_url + path
        if self._http is not None:
            return self._http(method, url, body)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = _urlreq.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with _urlreq.urlopen(req, timeout=self.timeout_sec) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw or "{}")
                except Exception:
                    return resp.status, {"raw": raw[:300]}
        except Exception as exc:  # noqa: BLE001
            return 0, {"error": str(exc)}

    def ingest(self, payload: Dict[str, Any]) -> bool:
        st, d = self._call("POST", "/api/desktop/ingest", payload)
        ok = st == 200 and bool(d.get("ok"))
        if not ok:
            logger.warning("[wechat_pc.bridge] ingest HTTP %s: %s", st, str(d)[:200])
        return ok

    def pull_outbound(self, account_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        st, d = self._call("GET", f"/api/desktop/outbound?platform={PLATFORM}&account_id={account_id}&limit={int(limit)}")
        if st != 200:
            return []
        items = d.get("items") or []
        return [x for x in items if isinstance(x, dict)]

    def ack(self, item_id: int, ok: bool, error: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        body: Dict[str, Any] = {"id": int(item_id), "ok": bool(ok), "error": error[:200]}
        if extra:
            body.update(extra)
        st, d = self._call("POST", "/api/desktop/outbound/ack", body)
        return st == 200 and bool(d.get("ok"))

    def fetch_media(self, url: str) -> Optional[bytes]:
        """拉取出站媒体（``/static/...`` 相对 URL 或绝对 URL）；失败 None。带 Bearer（static 通常不鉴权，带上无害）。"""
        u = str(url or "")
        if not u:
            return None
        if u.startswith("/"):
            u = self.base_url + u
        req = _urlreq.Request(u, method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _urlreq.urlopen(req, timeout=max(self.timeout_sec, 30.0)) as resp:  # noqa: S310
                if resp.status != 200:
                    return None
                return resp.read()
        except Exception:  # noqa: BLE001
            logger.warning("[wechat_pc.bridge] 拉取媒体失败 %s", u[:120])
            return None

    def thread_texts(self, account_id: str, chat_key: str, limit: int = 80) -> Optional[Set[Tuple[str, str]]]:
        """后端线程里已有的 ``(direction, 归一正文)`` 集合——可见区顶部没有时间条的旧气泡靠它去重。
        取不到（旧后端/网络）→ None（调用方按「无信息」处理，不据此跳过）。"""
        from urllib.parse import quote
        st, d = self._call("GET", f"/api/unified-inbox/thread?platform={PLATFORM}&account_id={quote(account_id)}"
                                  f"&chat_key={quote(chat_key, safe='')}&limit={int(limit)}")
        if st != 200:
            return None
        msgs = d.get("messages") if isinstance(d.get("messages"), list) else d.get("items")
        if not isinstance(msgs, list):
            return None
        out: Set[Tuple[str, str]] = set()
        for m in msgs:
            if isinstance(m, dict):
                out.add((str(m.get("direction") or "in"), " ".join(str(m.get("text") or "").split())))
        return out

    def thread_last_inbound_ts(self, account_id: str, chat_key: str, limit: int = 40) -> Optional[float]:
        """后端线程里对方最近一条来信的时间戳；线程取不到 → None，线程里没有来信 → 0.0。

        驱动重启后 ``_last_inbound`` 是空的，而「仅回复」档要看「对方说过话」——会话没有未读就不会再被
        扫屏，重启前的来信全部忘光，排到该联系人的回复一律 ``no_inbound_from_peer``。后端收件箱里有驱动自己
        之前入站的那些消息，问一次就够。"""
        from urllib.parse import quote
        st, d = self._call("GET", f"/api/unified-inbox/thread?platform={PLATFORM}&account_id={quote(account_id)}"
                                  f"&chat_key={quote(chat_key, safe='')}&limit={int(limit)}")
        if st != 200:
            return None
        msgs = d.get("messages") if isinstance(d.get("messages"), list) else d.get("items")
        if not isinstance(msgs, list):
            return None
        last = 0.0
        for m in msgs:
            if not isinstance(m, dict) or str(m.get("direction") or "in") != "in":
                continue
            try:
                last = max(last, float(m.get("ts") or 0.0))
            except (TypeError, ValueError):
                continue
        return last

    def heartbeat(self, account_id: str, *, tier: str, readonly: bool, stats: Dict[str, Any],
                  label: str = "") -> bool:
        """驱动存活心跳 → 工作台账号卡「副驾在线/离线」。老后端没有该端点（404）也当成功，不刷日志。"""
        body: Dict[str, Any] = {"platform": PLATFORM, "account_id": account_id, "bridge": BRIDGE_KIND,
                                "tier": tier, "readonly": bool(readonly), "stats": stats}
        if label:
            body["label"] = str(label)[:64]
        st, d = self._call("POST", "/api/desktop/heartbeat", body)
        return st in (200, 404) and (st == 404 or bool(d.get("ok")))


def bubble_fingerprint(b: Bubble) -> str:
    """屏内去重指纹——**按内容**（方向+正文+类别+分钟），不用 UIA RuntimeId。

    真机实锤（2026-09-08）：4.x 消息列表是 RecyclerListView，条目视图会回收复用，聊天变长后最新一条气泡可能
    拿到一个早先见过的 RuntimeId → 被当「已读过」静默跳掉（工作台发出的回复回显丢失）。
    """
    minute = _fp_minute(b)
    h = hashlib.sha1(f"{int(b.is_self)}|{b.sender}|{b.text}|{b.kind}|{minute}".encode("utf-8")).hexdigest()[:16]
    return f"fp:{h}"


def time_unknown(b: Bubble) -> bool:
    """气泡没有可靠时间：没有时间条（ts_hint=0）或只是上界（ts_is_upper_bound）。这类气泡去重不能带分钟。"""
    return (b.ts_hint or 0) <= 0 or bool(getattr(b, "ts_is_upper_bound", False))


def _fp_minute(b: Bubble) -> int:
    return 0 if time_unknown(b) else int(b.ts_hint // 60)


def content_fingerprint(chat_key: str, b: Bubble) -> str:
    """跨重启稳定的内容指纹：会话 + 方向 + 正文 + 分钟级时间（RuntimeId 每次重启都变，不能用）；时间未知＝分钟 0。"""
    minute = _fp_minute(b)
    h = hashlib.sha1(f"{chat_key}|{int(b.is_self)}|{b.sender}|{b.text}|{b.kind}|{minute}".encode("utf-8")).hexdigest()[:20]
    return f"cf:{h}"


class SeenStore:
    """已入站内容指纹的落盘集合（JSON，容量上限；损坏即当空）。``path`` 为空＝仅进程内。"""

    def __init__(self, path: str = "", max_items: int = 20000) -> None:
        self.path = str(path or "")
        self.max_items = max_items
        self._set: Set[str] = set()
        self._dirty = False
        if self.path:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    self._set = set(str(x) for x in data[-max_items:])
            except Exception:
                self._set = set()

    def __contains__(self, fp: str) -> bool:
        return fp in self._set

    def add(self, fp: str) -> None:
        if fp in self._set:
            return
        self._set.add(fp)
        self._dirty = True
        if len(self._set) > self.max_items:
            self._set = set(list(self._set)[-self.max_items // 2:])

    def flush(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            import os
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._set), fh)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            logger.debug("[wechat_pc] seen 落盘失败", exc_info=True)


@dataclass
class ServiceStats:
    ticks: int = 0
    inbound: int = 0
    self_mirrored: int = 0
    sent: int = 0
    send_failed: int = 0
    denied: int = 0
    guard_freezes: int = 0
    screen_events: int = 0
    unknown_direction: int = 0
    ingest_failed: int = 0
    deduped: int = 0
    profile_lookups: int = 0
    groups_seen: int = 0
    heartbeat_failed: int = 0
    last_readable: bool = False   # 上一轮能否读屏（微信主窗可见且已登录）——进程活着 ≠ 能干活
    errors: int = 0
    last_disposition: str = NONE
    frozen_until: float = 0.0
    freeze_reason: str = ""         # 当前生效的账号级冻结原因（登录窗 / guard_echo / limit …）；"" ＝ 未冻结
    chat_freezes: int = 0           # 会话级冻结次数（单个会话守卫失败，只停这个人）
    chat_frozen: int = 0            # 此刻仍在冻结中的会话数
    offline: bool = False
    voice_ready: bool = False     # 「发语音」锚点在 + 虚拟声卡通路就绪（后端据此决定要不要给这台机排语音）
    voice_sent: int = 0
    voice_failed: int = 0
    voice_mic_busy: bool = False  # 坐席麦克风正被别的程序录着（开会/通话）→ 后端暂不排语音、驱动不切麦
    voice_mic_busy_by: str = ""   # 占用进程名（逗号分隔）
    #: 因瞬态原因（同一联系人最小间隔未到）本地挂起、稍后再试的出站命令数
    deferred: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class AccountBinding:
    """account_id ↔ 登录微信真实身份（微信号）的落盘绑定（JSON）。``path`` 为空＝仅进程内。

    首次读到微信号即绑定；之后读到别的微信号 → 说明这个驱动盯的窗口里登的是另一个账号（双开互换 / 换号登录），
    工作台上会两个账号混成一个、回复发进错的微信 → service 据此冻结发送并提醒主人。"""

    def __init__(self, path: str = "", expected_wxid: str = "") -> None:
        self.path = str(path or "")
        self.wxid = str(expected_wxid or "").strip()
        self.nick = ""
        self.bound_at = 0.0
        if self.path and not self.wxid:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.wxid = str(data.get("wxid") or "").strip()
                    self.nick = str(data.get("nick") or "")
                    self.bound_at = float(data.get("bound_at") or 0.0)
            except Exception:
                self.wxid = ""

    def bind(self, wxid: str, nick: str, now: float) -> None:
        self.wxid, self.nick, self.bound_at = str(wxid or "").strip(), str(nick or ""), float(now)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"wxid": self.wxid, "nick": self.nick, "bound_at": self.bound_at}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            logger.debug("[wechat_pc] 账号绑定落盘失败", exc_info=True)


class WeChatPcService:
    def __init__(
        self,
        backend: WeChatPcBackend,
        bridge: BridgeClient,
        *,
        account_id: str,
        policy: Optional[PcPolicy] = None,
        config: Optional[Dict[str, Any]] = None,
        identity: Optional[ChatIdentityCache] = None,
        sender: Optional[GuardedSender] = None,
        notify: Optional[Callable[[str, str], None]] = None,
        now: Callable[[], float] = time.time,
        connected_at: Optional[float] = None,
        max_sessions_per_tick: int = 5,
        freeze_on_guard_fail_sec: float = 600.0,
        lookup_wxid: bool = True,
        seen_store: Optional["SeenStore"] = None,
        account_label: str = "",
        voice: Any = None,
        media_dir: str = "",
        sleep: Callable[[float], None] = time.sleep,
        account_binding: Optional[AccountBinding] = None,
    ) -> None:
        self.backend = backend
        self.bridge = bridge
        self._sleep = sleep
        self.account_id = str(account_id or "").strip() or "wechat-pc"
        self.account_label = str(account_label or "").strip()
        # 语音通路句柄（audio_cable.VoiceCable 同形：ready() / mic_switched() / prepare(path)）；None＝本机不发语音
        self.voice = voice
        self.media_dir = str(media_dir or "")
        self.policy = policy or resolve_policy(config)
        # 注意不能写 ``identity or ChatIdentityCache()``：空缓存 __len__==0 为假，会把传入的落盘缓存丢掉
        self.identity = identity if identity is not None else ChatIdentityCache()
        self.sender = sender or GuardedSender(backend)
        self._notify = notify
        self._now = now
        self.connected_at = float(connected_at if connected_at is not None else now())
        self.max_sessions_per_tick = max(1, int(max_sessions_per_tick))
        self.freeze_on_guard_fail_sec = max(0.0, float(freeze_on_guard_fail_sec))
        self.lookup_wxid = bool(lookup_wxid)
        self.stats = ServiceStats()
        self._seen: Dict[str, Set[str]] = {}
        self._seen_store = seen_store or SeenStore("")
        self._last_inbound: Dict[str, float] = {}
        self._inbound_recover_at: Dict[str, float] = {}
        self._sent_day = ""
        self._sent_today = 0
        self._sent_today_peer: Dict[str, int] = {}
        self._last_sent_peer: Dict[str, float] = {}
        # 语音单独计数（同时也计入上面两项）；对该联系人上一条已发出命令的 reply_group（分条节奏用）
        self._voice_sent_today = 0
        self._voice_sent_today_peer: Dict[str, int] = {}
        self._last_sent_group: Dict[str, str] = {}
        self._group_mentioned: Dict[str, bool] = {}
        self._visible_name_counts: Dict[str, int] = {}
        self._wxid_verified_at: Dict[str, float] = {}
        self._minimized_notified = False
        self._hb_thread = None
        self._hb_stop = None
        # 刚发出的语音 → 等它的回显气泡（「语音N秒」）被扫到时用念稿/音频 URL/人设名充实镜像行
        self._pending_voice_mirror: Dict[str, List[Dict[str, Any]]] = {}
        self._voice_ready_checked = 0.0
        self._a11y_rebuild_at = 0.0
        # 已认领但被 min_gap 挡住的出站命令：本地挂起等间隔过去（id → item / 首次挂起时刻）。
        # 2026-09-19 真机：分条语音两条同一轮被认领，第二条紧跟第一条 → min_gap 拒发 → 回执失败进人审，
        # 分条形同虚设。min_gap 是「等一会就行」的瞬态原因，不该当永久失败。
        self._deferred: Dict[int, Dict[str, Any]] = {}
        self._deferred_since: Dict[int, float] = {}
        # 账号级冻结按来源分别记到期时刻（reason → until）：登录窗这类「状态解除即恢复」的来源可以单独解冻，
        # 不会顺手把限频 2h / 环境异常 24h 这种真风控信号也解掉
        self._freezes: Dict[str, float] = {}
        # 会话级冻结（chat_key → until）与近期守卫失败时刻：单个会话发不出去只停它，多个会话接连失败才停账号
        self._chat_frozen_until: Dict[str, float] = {}
        self._guard_fail_at: List[float] = []
        # 登录微信的真实身份（昵称/微信号）：从资料卡读；与 account_binding 里绑定的微信号不一致＝串号
        self.account_binding = account_binding if account_binding is not None else AccountBinding()
        self.account_nick = ""
        self.account_wxid = ""
        self._identity_read_at = 0.0
        self._identity_dirty = True

    #: 登录身份复核间隔（秒）；登录窗关闭后（可能换了号）立即复核
    ACCOUNT_IDENTITY_RECHECK_SEC = 1800.0
    #: 读不到身份（资料卡没打开）时的重试间隔：每次读都要点头像开弹窗，不能每轮点
    ACCOUNT_IDENTITY_RETRY_SEC = 120.0
    #: 串号冻结的来源名（无 TTL：身份对上即解除）
    ACCOUNT_MISMATCH = "account_mismatch"

    #: 守卫失败升级为账号级冻结的判据：GUARD_FAIL_WINDOW_SEC 内 ≥ GUARD_FAIL_ESCALATE 个**不同会话**失败
    GUARD_FAIL_ESCALATE = 2
    GUARD_FAIL_WINDOW_SEC = 900.0

    #: 瞬态拒发本地挂起的上限：队列对「认领未回执」的命令 180s 后自动回收重派，挂起必须明显短于它，
    #: 否则同一条命令会被驱动与队列两头处理。超过上限仍发不出去 → 按原样回执 policy:min_gap。
    DEFER_MAX_SEC = 150.0
    #: 会被挂起而非立即失败的拒发原因（其余：档位/日上限/非工作时段/对方没来过信 → 等也没用，照旧失败）
    TRANSIENT_DENIALS = frozenset({"min_gap"})

    #: 空壳无障碍树自愈（隐藏→托盘单击）最短间隔：失败不狂点托盘
    A11Y_REBUILD_COOLDOWN_SEC = 300.0

    def _maybe_rebuild_accessibility(self) -> None:
        """窗口可见、已登录，但自检仍缺 session_list → 多半是 ShowWindow 拉回的空壳树（4.1.13 实锤），
        走托盘单击让微信重建。带冷却；托盘图标找不到就不动窗口。"""
        if self._now() - self._a11y_rebuild_at < self.A11Y_REBUILD_COOLDOWN_SEC:
            return
        hwnd_fn = getattr(self.backend, "main_hwnd", None)
        hwnd = 0
        try:
            hwnd = int(hwnd_fn() or 0) if callable(hwnd_fn) else 0
        except Exception:
            hwnd = 0
        if not hwnd:
            return
        self._a11y_rebuild_at = self._now()
        try:
            from src.integrations.wechat_pc.env_check import accessibility_tree_empty, rebuild_accessibility_via_tray
            if accessibility_tree_empty(hwnd) is not True:
                return
            res = rebuild_accessibility_via_tray(hwnd)
            logger.warning("[wechat_pc] 主窗无障碍树为空壳（ShowWindow 拉回）→ 托盘重建：%s", res)
            if res.get("tree_rebuilt"):
                self._emit_notify("a11y_rebuilt", res.get("via") or "")
        except Exception:
            logger.debug("[wechat_pc] 无障碍树重建异常", exc_info=True)

    # ── 巡检 ──
    def _roll_day(self) -> None:
        day = datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d")
        if day != self._sent_day:
            self._sent_day, self._sent_today, self._sent_today_peer = day, 0, {}
            self._voice_sent_today, self._voice_sent_today_peer = 0, {}

    def _emit_notify(self, kind: str, detail: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(kind, detail)
        except Exception:
            logger.debug("[wechat_pc] notify 失败", exc_info=True)

    def _sync_freeze_stats(self) -> None:
        now = self._now()
        for k in [k for k, until in self._freezes.items() if until <= now]:
            self._freezes.pop(k, None)
        if self._freezes:
            reason, until = max(self._freezes.items(), key=lambda kv: kv[1])
            self.stats.frozen_until, self.stats.freeze_reason = until, reason
        else:
            self.stats.frozen_until, self.stats.freeze_reason = 0.0, ""
        for k in [k for k, until in self._chat_frozen_until.items() if until <= now]:
            self._chat_frozen_until.pop(k, None)
        self.stats.chat_frozen = len(self._chat_frozen_until)

    def frozen(self) -> bool:
        self._sync_freeze_stats()
        return self._now() < self.stats.frozen_until

    def freeze(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        reason = str(reason or "manual")[:64]
        until = self._now() + float(seconds)
        self._freezes[reason] = max(self._freezes.get(reason, 0.0), until)
        self._sync_freeze_stats()
        logger.warning("[wechat_pc] 自动发送冻结 %.0fs（%s）", seconds, reason)

    def release_freeze(self, reason: str) -> bool:
        """解除某一来源的账号级冻结（登录窗关了 / 手机确认完了）；其它来源不受影响。返回是否真有东西被解掉。"""
        hit = self._freezes.pop(str(reason or "")[:64], None) is not None
        self._sync_freeze_stats()
        if hit:
            logger.info("[wechat_pc] 解除冻结（%s）", reason)
        return hit

    def chat_frozen(self, chat_key: str) -> bool:
        until = self._chat_frozen_until.get(chat_key, 0.0)
        if until and self._now() < until:
            return True
        if until:
            self._chat_frozen_until.pop(chat_key, None)
            self.stats.chat_frozen = len(self._chat_frozen_until)
        return False

    def freeze_chat(self, chat_key: str, seconds: float, reason: str) -> None:
        if seconds <= 0 or not chat_key:
            return
        self._chat_frozen_until[chat_key] = max(self._chat_frozen_until.get(chat_key, 0.0), self._now() + float(seconds))
        self.stats.chat_freezes += 1
        self.stats.chat_frozen = len(self._chat_frozen_until)
        logger.warning("[wechat_pc] 会话冻结 %.0fs chat=%s（%s）", seconds, chat_key, reason)

    def _on_guard_failure(self, chat_key: str, stage: str, reason: str) -> None:
        """守卫失败的处置。``cancel``（录音态卡住，之后连文字都发不出去）→ 账号级冻结；
        title/send/echo 只证明**这个会话**发不出去/发错 → 先冻这个会话；窗口内多个不同会话接连失败
        才说明是整个微信出了问题（窗口被挡 / 版本变了 / 控件树坏了）→ 升级为账号级冻结。"""
        self.stats.guard_freezes += 1
        if stage == "cancel":
            self.freeze(self.freeze_on_guard_fail_sec, f"guard_{stage}")
            self._emit_notify("voice_recording_stuck", f"{stage}:{reason}")
            return
        now = self._now()
        self.freeze_chat(chat_key, self.freeze_on_guard_fail_sec, f"guard_{stage}")
        self._guard_fail_at = [t for t in self._guard_fail_at if now - t <= self.GUARD_FAIL_WINDOW_SEC] + [now]
        distinct = len([k for k in self._chat_frozen_until if k != chat_key]) + 1
        if len(self._guard_fail_at) >= self.GUARD_FAIL_ESCALATE and distinct >= self.GUARD_FAIL_ESCALATE:
            self.freeze(self.freeze_on_guard_fail_sec, f"guard_{stage}")
            self._emit_notify("guard_freeze", f"{stage}:{reason}")
        else:
            self._emit_notify("guard_chat_freeze", f"{chat_key}:{stage}:{reason}")

    def _inspect_screen(self) -> bool:
        """返回是否可以继续读屏（窗口在且已登录）。"""
        st = self.backend.screen_state()
        disp = assess(st.dialog_texts, window_class=st.window_class)
        prev = self.stats.last_disposition
        if disp.kind != NONE and disp.kind != prev:
            self.stats.screen_events += 1
            if disp.freeze_sends:
                # TTL=0 的（登录窗/手机确认/需更新）＝「直到状态解除」：先冻 1h，状态还在就续，状态解除即解冻
                self.freeze(disp.freeze_ttl_sec or 3600.0, disp.kind)
            if disp.mark_offline:
                self.stats.offline = True
            if disp.notify_owner:
                self._emit_notify(disp.kind, " | ".join(st.dialog_texts)[:200])
        elif disp.kind != NONE and disp.freeze_sends and not disp.freeze_ttl_sec:
            self.freeze(3600.0, disp.kind)
        if disp.kind == NONE and prev != NONE:
            self.stats.offline = False
            prev_disp = disposition_for(prev)
            if prev_disp.freeze_sends and not prev_disp.freeze_ttl_sec:
                # 登录窗关了 / 手机确认完了：解除的是这一来源的冻结，限频/环境异常等带 TTL 的风控信号照旧等到期
                self.release_freeze(prev)
                # 重新登录过 → 可能换了号，下一轮立即复核身份
                self._identity_dirty, self._identity_read_at = True, 0.0
            self._emit_notify("recovered", prev)
        self.stats.last_disposition = disp.kind
        readable = bool(st.window_present and st.logged_in and not disp.readonly)
        # 启动时微信收在托盘/未登录 → 自检失败被锁只读；之后窗口回来了要重新自检解锁（否则永远只读）
        if readable and getattr(self.backend, "readonly", False) and callable(getattr(self.backend, "self_check", None)):
            try:
                with desktop_input():
                    rep = self.backend.self_check()
                if not getattr(self.backend, "readonly", True):
                    logger.info("[wechat_pc] 微信窗口回来了，锚点自检通过，解除只读：%s", rep)
                elif "session_list" in (rep.get("missing") or []):
                    self._maybe_rebuild_accessibility()
            except Exception:
                logger.debug("[wechat_pc] 重新自检失败", exc_info=True)
        return readable

    # ── 入站 ──
    #: 唯一显示名的微信号核对有效期：期内再来消息不再开资料卡（每次核对＝侧栏+弹窗闪 4 秒）
    WXID_RECHECK_SEC = 600.0

    def _name_ambiguous(self, display_name: str) -> bool:
        """这个显示名是否必须逐格核对：当前列表里有同名格，或缓存里它对应 ≥2 个微信号（曾经同名过就永远核对）。"""
        name = normalize_display_name(display_name)
        if self._visible_name_counts.get(name, 0) > 1:
            return True
        return len(self.identity.wxid_keys_for_name(name)) > 1

    def _needs_wxid_lookup(self, display_name: str) -> bool:
        if not self.lookup_wxid:
            return False
        if self._name_ambiguous(display_name):
            return True
        name = normalize_display_name(display_name)
        if not self.identity.wxid_keys_for_name(name):
            return True
        return self._now() - self._wxid_verified_at.get(name, 0.0) > self.WXID_RECHECK_SEC

    def _resolve_key(self, row) -> str:
        """当前**已打开**会话的稳定 chat_key。

        私聊按需读资料卡微信号（真机实锤：两个「无界科技BOUNDELESS」显示名、AutomationId 完全相同，只按名解析会把
        两个人并成一个会话）：同名歧义 / 未知联系人 / 超过 :attr:`WXID_RECHECK_SEC` 未复核时才开资料卡，
        否则用缓存里该名字的微信号级 key。读不到才回落显示名级身份。群聊按群名。
        """
        if row.is_group:
            return self.identity.resolve(display_name=row.display_name, is_group=True)
        wxid = ""
        if self._needs_wxid_lookup(row.display_name):
            try:
                wxid = self.backend.read_profile_wxid(row.display_name) or ""
            except Exception:
                wxid = ""
            if wxid:
                self.stats.profile_lookups += 1
                self._wxid_verified_at[normalize_display_name(row.display_name)] = self._now()
        if wxid:
            return self.identity.resolve(display_name=row.display_name, wxid=wxid)
        return self.identity.resolve(display_name=row.display_name)

    def _maybe_notify_minimized(self) -> None:
        """方向判不出且主窗最小化 → 提醒主人一次（恢复后再次最小化会再提醒）。"""
        probe = getattr(self.backend, "window_minimized", None)
        try:
            minimized = bool(callable(probe) and probe())
        except Exception:
            minimized = False
        if minimized and not self._minimized_notified:
            self._minimized_notified = True
            self._emit_notify("window_minimized", "微信窗口已最小化：副驾读不到消息方向，新消息暂不入站，请还原窗口（可被其他窗口遮挡，但不能最小化）")
        elif not minimized:
            self._minimized_notified = False

    def _thread_texts(self, chat_key: str) -> Optional[Set[Tuple[str, str]]]:
        fn = getattr(self.bridge, "thread_texts", None)
        if not callable(fn):
            return None
        try:
            return fn(self.account_id, chat_key)
        except Exception:
            return None

    def _ingest_bubbles(self, chat_key: str, display_name: str, is_group: bool, bubbles: List[Bubble]) -> int:
        seen = self._seen.setdefault(chat_key, set())
        n = 0
        now = self._now()
        # 可见区顶部**没有时间条**的旧气泡（聊天变长后时间条被卷出可见区）拿不到时间：不能按「现在」入站
        # （真机实锤：旧消息被打上新时间重复入库）——这类气泡以后端线程里「同向同文是否已存在」去重
        existing: Optional[Set[Tuple[str, str]]] = None
        if any(time_unknown(b) and b.kind != "system" for b in bubbles):
            existing = self._thread_texts(chat_key)
        for b in bubbles:
            fp = bubble_fingerprint(b)
            if fp in seen:
                continue
            seen.add(fp)
            if b.kind == "system":
                continue
            if not getattr(b, "direction_known", True):
                # 4.x 气泡无方向属性、像素也没判出来（窗口最小化/离屏）：宁漏不错——把主人自己的话
                # 当客户消息会触发 AI 回自己，比漏一条严重得多
                self.stats.unknown_direction += 1
                self._maybe_notify_minimized()
                continue
            text = str(b.text or "")
            if not text.strip() and b.kind == "text":
                continue
            if not b.is_self:
                # 「对方最近来信时间」按屏幕所见更新（含重启后被指纹去重的旧气泡）——仅回复档
                # 的判定依据是「对方说过话」，不该因为驱动重启就忘了
                self._last_inbound[chat_key] = max(self._last_inbound.get(chat_key, 0.0), b.ts_hint or now)
            cfp = content_fingerprint(chat_key, b)
            if cfp in self._seen_store:
                self.stats.deduped += 1
                continue
            if time_unknown(b) and existing is not None:
                key = ("out" if b.is_self else "in", " ".join(text.split()))
                if key in existing:
                    self._seen_store.add(cfp)
                    self.stats.deduped += 1
                    continue
            # 己方气泡（含服务自己刚发出的回显）照常镜像为 out：桌面桥账号的出站镜像由桥端回流，
            # 工作台发送路由/受控队列不落镜像（与桌面壳 DOM 同步同一惯例，一句话只此一行）
            # 只有真媒体才标 media_type；链接/名片/位置/未知类型按正文（气泡 Name 即描述文字）入站，
            # 让 AI 看得见内容而不是一个「[媒体]」占位
            media_type = b.kind if b.kind in ("image", "voice", "file", "video", "sticker") else ""
            payload: Dict[str, Any] = {
                "platform": PLATFORM, "account_id": self.account_id, "chat_key": chat_key,
                "name": display_name, "text": text, "ts": b.ts_hint or now,
                "direction": "out" if b.is_self else "in",
                "media_type": media_type, "media_ref": "",
                "bridge": BRIDGE_KIND,
            }
            if is_group and b.sender:
                payload["sender_name"] = b.sender
                payload["chat_type"] = "group"
            if b.is_self and media_type == "voice":
                # 服务自己刚发出的语音：后端在 ack 时已镜像了带念稿/音频/人设名的出站行（P0-6），
                # 屏上这颗「语音N秒」占位气泡不再落第二行——命中待回显记录即只标已见。
                # 没命中（坐席亲手发的语音 / 驱动重启丢了记录）照常按占位入站。
                if self._pop_voice_mirror(chat_key, now):
                    self._seen_store.add(cfp)
                    self.stats.self_mirrored += 1
                    continue
            if self.bridge.ingest(payload):
                n += 1
                self._seen_store.add(cfp)
                if b.is_self:
                    self.stats.self_mirrored += 1
                else:
                    self.stats.inbound += 1
                    self._last_inbound[chat_key] = b.ts_hint or now
                    if is_group and "@" in text:
                        self._group_mentioned[chat_key] = True
            else:
                self.stats.ingest_failed += 1
                logger.warning("[wechat_pc] 入站投递失败 chat=%s text=%r（后端不可达/鉴权失败？）",
                               chat_key, text[:30])
                seen.discard(fp)   # 下轮重试
        self._seen_store.flush()
        if len(seen) > 4000:
            self._seen[chat_key] = set(list(seen)[-2000:])
        return n

    def _list_sessions(self) -> List[Any]:
        """读一次会话列表并刷新同名计数（出站的同名歧义守卫依赖它）；便宜，贵的是逐个打开未读会话。"""
        all_rows = list(self.backend.list_sessions())
        counts: Dict[str, int] = {}
        for r in all_rows:
            nm = normalize_display_name(r.display_name)
            counts[nm] = counts.get(nm, 0) + 1
        self._visible_name_counts = counts
        return all_rows

    def _scan_inbound(self, all_rows: Optional[List[Any]] = None) -> int:
        from src.integrations.wechat_pc.policy import is_system_session
        if all_rows is None:
            all_rows = self._list_sessions()
        rows = [r for r in all_rows if int(r.unread or 0) > 0 and not is_system_session(r.display_name)]
        handled = 0
        for row in rows[: self.max_sessions_per_tick]:
            try:
                with desktop_input():
                    handled += self._scan_one_session(row)
            except Exception:
                self.stats.errors += 1
                logger.debug("[wechat_pc] 会话扫描失败 %s", row.display_name, exc_info=True)
        return handled

    def _scan_one_session(self, row: Any) -> int:
        """开一个未读会话→校标题→读气泡（一段连续桌面交互，调用方持桌面输入锁）。"""
        if not self.backend.open_session(row.display_name, index=getattr(row, "index", -1)):
            return 0
        title = self.backend.current_title()
        # 群聊：会话格只有群名、标题带成员数「群名 (12)」→ 标 is_group（群策略默认永不回复）
        if not row.is_group and is_group_title(title, row.display_name):
            row.is_group = True
            self.stats.groups_seen += 1
        elif normalize_display_name(title) != normalize_display_name(row.display_name) and not row.is_group:
            return 0
        chat_key = self._resolve_key(row)
        if not chat_key:
            return 0
        return self._ingest_bubbles(chat_key, row.display_name, row.is_group, self.backend.read_visible_messages())

    # ── 出站 ──
    #: 语音回显最多等多久被扫到（超过就当丢了，不再充实后来的语音）
    VOICE_MIRROR_TTL_SEC = 600.0

    def _pop_voice_mirror(self, chat_key: str, now: float) -> Optional[Dict[str, Any]]:
        q = self._pending_voice_mirror.get(chat_key) or []
        q = [m for m in q if now - float(m.get("ts") or 0.0) <= self.VOICE_MIRROR_TTL_SEC]
        hit = q.pop(0) if q else None
        if q:
            self._pending_voice_mirror[chat_key] = q
        else:
            self._pending_voice_mirror.pop(chat_key, None)
        return hit

    def _refresh_voice_ready(self) -> bool:
        """语音能力＝后端有「发语音」锚点 ∧ 声卡通路就绪；每 60s 复查一次（心跳带给后端）。"""
        now = self._now()
        if now - self._voice_ready_checked < 60.0 and self._voice_ready_checked > 0:
            return self.stats.voice_ready
        self._voice_ready_checked = now
        ready = False
        try:
            probe = getattr(self.backend, "voice_ready", None)
            ready = bool(self.voice is not None and self.voice.ready() and callable(probe) and probe())
        except Exception:
            ready = False
        self.stats.voice_ready = ready
        return ready

    def _resolve_voice_media(self, it: Dict[str, Any]) -> str:
        """出站语音的本地文件：``media_ref`` 是同机路径直接用；否则按 ``media_url`` 拉到 media_dir。空串＝拿不到。"""
        ref = str(it.get("media_ref") or "")
        if ref:
            try:
                import os
                if os.path.isfile(ref):
                    return ref
            except Exception:
                pass
        url = str(it.get("media_url") or "")
        fetch = getattr(self.bridge, "fetch_media", None)
        if not url or not callable(fetch):
            return ""
        data = fetch(url)
        if not data:
            return ""
        try:
            import os
            d = self.media_dir or os.path.join(os.environ.get("TEMP") or ".", "chatx_wechat_pc_voice")
            os.makedirs(d, exist_ok=True)
            ext = os.path.splitext(url.split("?", 1)[0])[1] or ".bin"
            path = os.path.join(d, f"out_{int(it.get('id') or 0)}{ext}")
            with open(path, "wb") as fh:
                fh.write(data)
            return path
        except Exception:
            logger.debug("[wechat_pc] 落语音媒体失败", exc_info=True)
            return ""

    def _refresh_mic_busy(self) -> Dict[str, Any]:
        """坐席麦克风是否正被别的程序录着（开会/通话）。每次调用都问系统（COM 枚举 ≈10ms）；
        空闲→占用的那一刻通知主人一次。检测不了（COM 不可用/异常）按不占用，不能因为看不见就一直不发语音。"""
        probe = getattr(self.voice, "mic_busy", None) if self.voice is not None else None
        res: Dict[str, Any] = {"busy": False, "sessions": []}
        if callable(probe):
            try:
                res = dict(probe() or {})
            except Exception:
                logger.debug("[wechat_pc] 麦克风占用检测异常", exc_info=True)
                res = {"busy": False, "sessions": []}
        busy = bool(res.get("busy"))
        procs = sorted({str(s.get("process") or s.get("pid") or "?") for s in (res.get("sessions") or [])})
        if busy and not self.stats.voice_mic_busy:
            logger.warning("[wechat_pc] 坐席麦克风被占用（%s）→ 语音暂改文字", ",".join(procs) or "?")
            self._emit_notify("voice_mic_busy", ",".join(procs))
        elif not busy and self.stats.voice_mic_busy:
            logger.info("[wechat_pc] 坐席麦克风已空闲，恢复语音")
        self.stats.voice_mic_busy = busy
        self.stats.voice_mic_busy_by = ",".join(procs)[:80]
        return res

    def _send_voice_item(self, it: Dict[str, Any], target: str, expected_wxid: str) -> SendOutcome:
        """一条 voice 命令：解析媒体 → 切默认麦克风到声卡 → 五步守卫（record/play/send/echo）→ 还麦克风。

        切麦克风之前先看坐席是否正在用麦（开会/通话）：是 → ``record/mic_busy:<进程>`` 失败（能力型，server 端
        同稿改发文字），**不切默认设备**——跟随「默认设备」的软件会在切换瞬间把对方听到的换成我们的合成音。
        """
        if self.voice is None or not self._refresh_voice_ready():
            return SendOutcome(False, "record", "voice_not_ready")
        busy = self._refresh_mic_busy()
        if busy.get("busy"):
            return SendOutcome(False, "record", f"mic_busy:{self.stats.voice_mic_busy_by or '?'}"[:120])
        path = self._resolve_voice_media(it)
        if not path:
            return SendOutcome(False, "record", "media_unavailable")
        try:
            play, dur = self.voice.prepare(path)
        except Exception as exc:  # noqa: BLE001
            return SendOutcome(False, "record", f"prepare_failed:{exc}"[:120])
        expected_sec = int(round(dur)) if dur > 0 else None
        try:
            with self.voice.mic_switched():
                return self.sender.send_voice(target, play, expected_wxid=expected_wxid, expected_sec=expected_sec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wechat_pc] 语音发送异常：%s", exc)
            return SendOutcome(False, "record", f"mic_switch_failed:{exc}"[:120])

    def _display_name_for(self, chat_key: str) -> str:
        name = self.identity.display_name_for(chat_key)
        if name:
            return name
        kind = chat_key_kind(chat_key)
        body = str(chat_key or "")[len(f"wx:{kind}:"):] if kind else ""
        if kind in ("name", "group"):
            return body.split("#", 1)[0]
        return ""

    def _defer(self, item_id: int, it: Dict[str, Any], reason: str) -> bool:
        """瞬态拒发 → 本地挂起到下轮（不回执、命令保持认领态）。超过 DEFER_MAX_SEC 仍不行 → False（调用方照旧回执失败）。"""
        now = self._now()
        since = self._deferred_since.get(item_id)
        if since is not None and now - since > self.DEFER_MAX_SEC:
            self._deferred.pop(item_id, None)
            self._deferred_since.pop(item_id, None)
            return False
        if since is None:
            self._deferred_since[item_id] = now
            self.stats.deferred += 1
            logger.info("[wechat_pc] 挂起 id=%s chat=%s reason=%s（同联系人间隔未到，下轮再试）",
                        item_id, it.get("chat_key"), reason)
        self._deferred[item_id] = it
        return True

    def _forget_deferred(self, item_id: int) -> None:
        self._deferred.pop(item_id, None)
        self._deferred_since.pop(item_id, None)

    #: 分条连发的条间抖动：真人连发几条语音，间隔不会精确一样
    PART_GAP_JITTER = (0.0, 1.2)

    #: 同一联系人向后端补查「最近来信」的最小间隔（后端线程里也没有 → 别每轮都问）
    INBOUND_RECOVER_RETRY_SEC = 120.0
    #: 同一稿子的下一条分条：上一条刚核对过身份并发出，多久内不再开资料卡复核
    PART_IDENTITY_TTL_SEC = 60.0

    def _identity_fresh_for_part(self, chat_key: str, group: str, target: str) -> bool:
        """同一 reply_group 的后续分条要不要跳过资料卡核对（同名歧义联系人每条都核对 → 每条多 6–8s，
        真机 2026-09-19：5s 语音端到端 15.6s，其中 open 7.6s；三条分条之间被拉成 18s 一条，毫无连发感）。

        条件全满足才跳：本条是上一条已发出命令的同组分条、上一条发出不到 :attr:`PART_IDENTITY_TTL_SEC`、
        **当前打开的会话标题就是目标**（同一 tick 内顺序处理、中间不扫屏，选中的会话格就是刚核对过的那个人；
        ``open_session`` 对同名多格也优先用已选中的格，不会再按列表位置去点另一个同名人）。
        """
        if not group or self._last_sent_group.get(chat_key) != group:
            return False
        last = self._last_sent_peer.get(chat_key, 0.0)
        if last <= 0 or self._now() - last > self.PART_IDENTITY_TTL_SEC:
            return False
        try:
            return verify_title(self.backend.current_title(), target)
        except Exception:
            return False

    def _recover_last_inbound(self, chat_key: str) -> None:
        """驱动重启后 ``_last_inbound`` 为空：先问后端线程补回「对方最近来信时间」，再做仅回复判定。
        重启前来信的会话没有未读就不会再被扫屏——不补的话排给它的回复全被 ``no_inbound_from_peer`` 拒掉
        （2026-09-19 真机验收撞上）。取不到/没有来信都不改判定，只是记一下时间别每轮重复问。"""
        last_try = self._inbound_recover_at.get(chat_key, 0.0)
        if last_try and self._now() - last_try < self.INBOUND_RECOVER_RETRY_SEC:
            return
        self._inbound_recover_at[chat_key] = self._now()
        fn = getattr(self.bridge, "thread_last_inbound_ts", None)
        if not callable(fn):
            return
        try:
            ts = fn(self.account_id, chat_key)
        except Exception:
            logger.debug("[wechat_pc] 补查最近来信失败 chat=%s", chat_key, exc_info=True)
            return
        if ts:
            self._last_inbound[chat_key] = float(ts)
            logger.info("[wechat_pc] 重启后从后端补回对方最近来信时间 chat=%s ts=%.0f", chat_key, float(ts))

    def _pace_between_parts(self, chat_key: str) -> None:
        """同一稿子的下一条分条：距上一条发出不足 ``voice_part_gap_sec`` 就在本轮**原地等**到点（加一点抖动），
        而不是挂起到下轮——下轮先扫会话再发，条间会被拉到 5–8s，像隔了一轮对话；这里等出来的是「几秒一条」的连发感。
        等待上限就是 gap 本身（间隔已过就不等）。"""
        last = self._last_sent_peer.get(chat_key, 0.0)
        if last <= 0:
            return
        gap = float(self.policy.voice_part_gap_sec)
        remain = gap - (self._now() - last)
        if remain <= 0 or remain > gap:
            return
        import random
        wait = remain + random.uniform(*self.PART_GAP_JITTER)
        logger.debug("[wechat_pc] 分条节奏：等 %.1fs 再发下一条 chat=%s", wait, chat_key)
        try:
            self._sleep(wait)
        except Exception:
            pass

    def _drain_outbound(self) -> int:
        if not self.policy.sends_allowed or self.frozen():
            if self._deferred and self.frozen():
                # 冻结期可长达 1 小时，远超队列回收窗：挂起的命令立刻回执进人审，别让两头各处理一次
                for item_id in sorted(self._deferred):
                    self.bridge.ack(item_id, False, "guard:frozen")
                self._deferred.clear()
                self._deferred_since.clear()
            return 0
        pulled = self.bridge.pull_outbound(self.account_id, limit=3)
        # 挂起的先于新认领的，按 id（入队顺序）；队列若已把挂起项回收重派，按 id 去重只处理一份
        items = [self._deferred[i] for i in sorted(self._deferred)]
        items += [it for it in pulled if int(it.get("id") or 0) not in self._deferred]
        done = 0
        for pos, it in enumerate(items):
            item_id = int(it.get("id") or 0)
            chat_key = str(it.get("chat_key") or "")
            if self.frozen() or self.chat_frozen(chat_key):
                # 本轮前面的命令触发了守卫冻结（账号级或就这个会话）：已认领的剩余命令立刻回执失败进人审队列，
                # 而不是等 180s 自动回收再盲重试
                self._forget_deferred(item_id)
                self.bridge.ack(item_id, False, "guard:frozen")
                continue
            text = str(it.get("text") or "")
            kind = str(it.get("kind") or "text")
            self._roll_day()
            group = str(it.get("reply_group") or "")
            if group and self._last_sent_group.get(chat_key) == group:
                self._pace_between_parts(chat_key)
            if self.policy.reply_only and chat_key not in self._last_inbound:
                self._recover_last_inbound(chat_key)
            verdict = may_send(
                self.policy, kind=kind, now=datetime.fromtimestamp(self._now()),
                connected_at=self.connected_at, sent_today=self._sent_today,
                sent_today_to_peer=self._sent_today_peer.get(chat_key, 0),
                last_sent_to_peer_ts=self._last_sent_peer.get(chat_key, 0.0),
                last_inbound_from_peer_ts=self._last_inbound.get(chat_key, 0.0),
                is_group=chat_key_kind(chat_key) == "group",
                mentioned=self._group_mentioned.get(chat_key, False),
                voice_sent_today=self._voice_sent_today,
                voice_sent_today_to_peer=self._voice_sent_today_peer.get(chat_key, 0),
                continues_last_group=bool(group) and self._last_sent_group.get(chat_key) == group,
            )
            if not verdict.allowed:
                if verdict.reason in self.TRANSIENT_DENIALS and self._defer(item_id, it, verdict.reason):
                    continue
                self._forget_deferred(item_id)
                self.stats.denied += 1
                logger.warning("[wechat_pc] 拒发 id=%s chat=%s reason=%s", item_id, chat_key, verdict.reason)
                self.bridge.ack(item_id, False, f"policy:{verdict.reason}")
                continue
            self._forget_deferred(item_id)
            target = self._display_name_for(chat_key)
            if not target:
                self.stats.denied += 1
                logger.warning("[wechat_pc] 拒发 id=%s chat=%s reason=unknown_target（身份映射缺失）", item_id, chat_key)
                self.bridge.ack(item_id, False, "policy:unknown_target")
                continue
            # 微信号级身份 → 发送前逐格核对资料卡（同名不同号只有这一道能防发错人）；
            # 显示名唯一且 10 分钟内刚核对过的跳过（省掉一次侧栏+弹窗），有歧义的永远核对
            expected_wxid = chat_key[len("wx:id:"):] if chat_key_kind(chat_key) == "id" else ""
            if expected_wxid and not self._needs_wxid_lookup(target):
                expected_wxid = ""
            if expected_wxid and self._identity_fresh_for_part(chat_key, group, target):
                expected_wxid = ""
            is_voice = kind == "voice"
            if is_voice:
                out: SendOutcome = self._send_voice_item(it, target, expected_wxid)
            else:
                out = self.sender.send(target, text, expected_wxid=expected_wxid)
            if out.ok and expected_wxid:
                self._wxid_verified_at[normalize_display_name(target)] = self._now()
            if out.ok:
                self.stats.sent += 1
                self._sent_today += 1
                self._sent_today_peer[chat_key] = self._sent_today_peer.get(chat_key, 0) + 1
                self._last_sent_peer[chat_key] = self._now()
                self._last_sent_group[chat_key] = group
                self._group_mentioned[chat_key] = False
                if is_voice:
                    self.stats.voice_sent += 1
                    self._voice_sent_today += 1
                    self._voice_sent_today_peer[chat_key] = self._voice_sent_today_peer.get(chat_key, 0) + 1
                    self._pending_voice_mirror.setdefault(chat_key, []).append({
                        "inbox_text": str(it.get("inbox_text") or text), "media_url": str(it.get("media_url") or ""),
                        "sender_name": str(it.get("sender_name") or ""), "ts": self._now()})
                    # 语音一天几十条，成功也留一行（trace 里有 play 秒数 / 尾部静音 tail=…ms / echo 命中轮次）——
                    # 真机验收与日后「语音为什么慢」都靠这行，不用复现
                    logger.info("[wechat_pc] 语音已发 id=%s chat=%s group=%s elapsed=%sms trace=%s",
                                item_id, chat_key, group or "-", out.elapsed_ms, out.trace)
                    self.bridge.ack(item_id, True, "", {"delivered_as": "voice", "echo": out.echo_text[:40]})
                else:
                    self.bridge.ack(item_id, True, "")
                done += 1
                continue
            self.stats.send_failed += 1
            if is_voice:
                self.stats.voice_failed += 1
            logger.warning("[wechat_pc] 发送失败 id=%s kind=%s chat=%s stage=%s reason=%s elapsed=%sms trace=%s",
                           item_id, kind, chat_key, out.stage, out.reason, out.elapsed_ms, out.trace)
            self.bridge.ack(item_id, False, f"guard:{out.stage}:{out.reason}")
            # 语音的 record/play 步失败（声卡没就绪/媒体拿不到/进不了录音态）是本机能力问题、没碰到会话，不冻结
            if out.stage in ("title", "send", "echo", "cancel"):
                self._on_guard_failure(chat_key, out.stage, out.reason)
        return done

    # ── 一轮 ──
    def _heartbeat(self) -> None:
        """每轮都报存活（含读屏不可用的轮次——工作台要知道「进程活着但微信没登录」与「进程挂了」的区别）。"""
        hb = getattr(self.bridge, "heartbeat", None)
        if not callable(hb):
            return
        try:
            if self.stats.voice_ready:
                self._refresh_mic_busy()   # 每轮问一次（≈10ms）：后端据此决定这一刻要不要给这台机排语音
            self._sync_freeze_stats()
            st = self.stats.as_dict()
            stats = {k: st.get(k) for k in ("ticks", "inbound", "sent", "denied", "deferred", "send_failed",
                                             "unknown_direction", "frozen_until", "freeze_reason", "chat_frozen",
                                             "last_disposition", "offline",
                                             "last_readable", "voice_ready", "voice_sent", "voice_failed",
                                             "voice_mic_busy", "voice_mic_busy_by")}
            # 语音配额用量（今日已发 / 日上限）：工作台据此提示「今天语音快用完了」
            stats["voice_today"] = int(self._voice_sent_today)
            stats["voice_daily_cap"] = int(self.policy.voice_daily_cap)
            # 比默认宽松的配额项（测试值）：上客户前还回的提醒，随心跳给工作台
            stats["caps_relaxed"] = ",".join(sorted(caps_relaxed(self.policy)))
            stats.update(self._window_binding_stats())
            stats.update(self._account_identity_stats())
            ok = hb(self.account_id, tier=self.policy.tier,
                    readonly=bool(getattr(self.backend, "readonly", False)) or not self.policy.sends_allowed,
                    stats=stats, label=self.account_label)
            if not ok:
                self.stats.heartbeat_failed += 1
        except Exception:
            self.stats.heartbeat_failed += 1

    @property
    def account_mismatch(self) -> bool:
        return bool(self.account_wxid and self.account_binding.wxid and self.account_wxid != self.account_binding.wxid)

    def _refresh_account_identity(self) -> None:
        """读登录微信的昵称/微信号（后端可选能力 ``read_self_identity``）并与绑定核对。

        首次读到 → 绑定（落盘）；读到别的微信号 → 冻结发送（``account_mismatch``，直到身份对上）并提醒主人；
        读不到（资料卡打不开）→ 保留上次结果，不冻结。"""
        fn = getattr(self.backend, "read_self_identity", None)
        if not callable(fn):
            return
        now = self._now()
        wait = self.ACCOUNT_IDENTITY_RETRY_SEC if self._identity_dirty else self.ACCOUNT_IDENTITY_RECHECK_SEC
        if self._identity_read_at and now - self._identity_read_at < wait:
            return
        self._identity_read_at = now
        try:
            with desktop_input():
                ident = fn() or {}
        except Exception:
            logger.debug("[wechat_pc] 读登录身份失败", exc_info=True)
            return
        wxid = str(ident.get("wxid") or "").strip()
        nick = str(ident.get("nick") or "").strip()
        if not wxid and not nick:
            return
        self._identity_dirty = False
        if nick:
            self.account_nick = nick
        if not wxid:
            return
        self.account_wxid = wxid
        if not self.account_binding.wxid:
            self.account_binding.bind(wxid, nick, now)
            logger.info("[wechat_pc] 账号 %s 绑定登录微信：%s（%s）", self.account_id, nick, wxid)
            return
        if self.account_mismatch:
            self.freeze(3600.0, self.ACCOUNT_MISMATCH)
            self._emit_notify(self.ACCOUNT_MISMATCH, f"{self.account_binding.wxid}->{wxid}:{nick}"[:200])
        else:
            self.release_freeze(self.ACCOUNT_MISMATCH)

    def _account_identity_stats(self) -> Dict[str, Any]:
        return {"account_nick": self.account_nick, "account_wxid": self.account_wxid,
                "account_bound_wxid": self.account_binding.wxid, "account_mismatch": self.account_mismatch}

    def _window_binding_stats(self) -> Dict[str, Any]:
        """驱动盯的微信窗口/进程、是否显式绑定、桌面上有几个微信主窗（>1 且没绑 → 工作台提醒主人）。后端没这能力 → 空。"""
        fn = getattr(self.backend, "window_binding", None)
        if not callable(fn):
            return {}
        try:
            b = fn() or {}
            return {"window_hwnd": int(b.get("window_hwnd") or 0), "window_pid": int(b.get("window_pid") or 0),
                    "window_bound": bool(b.get("window_bound")), "main_windows": int(b.get("main_windows") or 0)}
        except Exception:
            return {}

    def tick(self) -> Dict[str, Any]:
        self.stats.ticks += 1
        summary: Dict[str, Any] = {"inbound": 0, "sent": 0, "readable": False}
        try:
            readable = self._inspect_screen()
            summary["readable"] = readable
            self.stats.last_readable = bool(readable)
            if readable:
                self._refresh_account_identity()
                if self.account_mismatch and self._freezes.get(self.ACCOUNT_MISMATCH, 0.0) <= self._now():
                    self.freeze(3600.0, self.ACCOUNT_MISMATCH)
                self._refresh_voice_ready()
                # 出站优先：上一轮已生成的回复先发，不等本轮逐个打开未读会话（最多 5 个×几秒）再发；
                # 同名计数用刚读的列表刷新过，守卫不失准。扫完入站若有新消息再补一次认领，保留「同一轮内秒回」的机会
                rows = self._list_sessions()
                summary["sent"] = self._drain_outbound()
                summary["inbound"] = self._scan_inbound(rows)
                if summary["inbound"] > 0:
                    summary["sent"] += self._drain_outbound()
        except Exception:
            self.stats.errors += 1
            logger.debug("[wechat_pc] tick 异常", exc_info=True)
        if self._hb_thread is None:
            # 常驻模式由独立线程按固定间隔报心跳（一轮引导读取/资料卡核对可能 30s+，不能让在线态跟着抖）；
            # 单轮调用（测试/--ticks）仍在轮末报一次
            self._heartbeat()
        return summary

    def start_heartbeat_thread(self, interval_sec: float = 10.0) -> None:
        """后台守护线程：固定间隔报心跳，与 tick 时长解耦。重复调用无效。"""
        if self._hb_thread is not None:
            return
        import threading
        self._hb_stop = threading.Event()
        iv = max(2.0, float(interval_sec))

        def _loop() -> None:
            while not self._hb_stop.is_set():
                self._heartbeat()
                self._hb_stop.wait(iv)

        self._hb_thread = threading.Thread(target=_loop, name="wechat_pc-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat_thread(self) -> None:
        if self._hb_thread is None:
            return
        self._hb_stop.set()
        self._hb_thread.join(timeout=3.0)
        self._hb_thread = None

    def run_forever(self, *, interval_sec: float = 3.0, sleep: Callable[[float], None] = time.sleep,
                    stop: Optional[Callable[[], bool]] = None, heartbeat_interval_sec: float = 10.0) -> None:
        self.start_heartbeat_thread(heartbeat_interval_sec)
        try:
            while not (stop and stop()):
                self.tick()
                sleep(max(0.5, float(interval_sec)))
        finally:
            self.stop_heartbeat_thread()


__all__ = ["AccountBinding", "BridgeClient", "WeChatPcService", "ServiceStats", "SeenStore", "bubble_fingerprint",
           "content_fingerprint", "time_unknown"]
