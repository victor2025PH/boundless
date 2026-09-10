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

HTTP 经 :class:`BridgeClient`（``http`` 可注入）；时间/睡眠可注入；所有异常在 tick 内吞掉并计数。
"""
from __future__ import annotations

import hashlib
import json
import logging
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
from src.integrations.wechat_pc.policy import PcPolicy, may_send, resolve_policy
from src.integrations.wechat_pc.risk_screens import NONE, assess
from src.integrations.wechat_pc.send_guard import GuardedSender, SendOutcome

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

    def ack(self, item_id: int, ok: bool, error: str = "") -> bool:
        st, d = self._call("POST", "/api/desktop/outbound/ack", {"id": int(item_id), "ok": bool(ok), "error": error[:200]})
        return st == 200 and bool(d.get("ok"))

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
    offline: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


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
    ) -> None:
        self.backend = backend
        self.bridge = bridge
        self.account_id = str(account_id or "").strip() or "wechat-pc"
        self.account_label = str(account_label or "").strip()
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
        self._sent_day = ""
        self._sent_today = 0
        self._sent_today_peer: Dict[str, int] = {}
        self._last_sent_peer: Dict[str, float] = {}
        self._group_mentioned: Dict[str, bool] = {}
        self._visible_name_counts: Dict[str, int] = {}
        self._wxid_verified_at: Dict[str, float] = {}
        self._minimized_notified = False
        self._hb_thread = None
        self._hb_stop = None

    # ── 巡检 ──
    def _roll_day(self) -> None:
        day = datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d")
        if day != self._sent_day:
            self._sent_day, self._sent_today, self._sent_today_peer = day, 0, {}

    def _emit_notify(self, kind: str, detail: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(kind, detail)
        except Exception:
            logger.debug("[wechat_pc] notify 失败", exc_info=True)

    def frozen(self) -> bool:
        return self._now() < self.stats.frozen_until

    def freeze(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        self.stats.frozen_until = max(self.stats.frozen_until, self._now() + float(seconds))
        logger.warning("[wechat_pc] 自动发送冻结 %.0fs（%s）", seconds, reason)

    def _inspect_screen(self) -> bool:
        """返回是否可以继续读屏（窗口在且已登录）。"""
        st = self.backend.screen_state()
        disp = assess(st.dialog_texts, window_class=st.window_class)
        if disp.kind != NONE and disp.kind != self.stats.last_disposition:
            self.stats.screen_events += 1
            if disp.freeze_sends:
                # TTL=0 的（登录窗/手机确认/需更新）按「直到状态解除」处理：先冻 1h，下轮仍在则续
                self.freeze(disp.freeze_ttl_sec or 3600.0, disp.kind)
            if disp.mark_offline:
                self.stats.offline = True
            if disp.notify_owner:
                self._emit_notify(disp.kind, " | ".join(st.dialog_texts)[:200])
        if disp.kind == NONE and self.stats.last_disposition != NONE:
            self.stats.offline = False
            self._emit_notify("recovered", self.stats.last_disposition)
        self.stats.last_disposition = disp.kind
        readable = bool(st.window_present and st.logged_in and not disp.readonly)
        # 启动时微信收在托盘/未登录 → 自检失败被锁只读；之后窗口回来了要重新自检解锁（否则永远只读）
        if readable and getattr(self.backend, "readonly", False) and callable(getattr(self.backend, "self_check", None)):
            try:
                rep = self.backend.self_check()
                if not getattr(self.backend, "readonly", True):
                    logger.info("[wechat_pc] 微信窗口回来了，锚点自检通过，解除只读：%s", rep)
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

    def _scan_inbound(self) -> int:
        from src.integrations.wechat_pc.policy import is_system_session
        all_rows = self.backend.list_sessions()
        counts: Dict[str, int] = {}
        for r in all_rows:
            nm = normalize_display_name(r.display_name)
            counts[nm] = counts.get(nm, 0) + 1
        self._visible_name_counts = counts
        rows = [r for r in all_rows if int(r.unread or 0) > 0 and not is_system_session(r.display_name)]
        handled = 0
        for row in rows[: self.max_sessions_per_tick]:
            try:
                if not self.backend.open_session(row.display_name, index=getattr(row, "index", -1)):
                    continue
                title = self.backend.current_title()
                # 群聊：会话格只有群名、标题带成员数「群名 (12)」→ 标 is_group（群策略默认永不回复）
                if not row.is_group and is_group_title(title, row.display_name):
                    row.is_group = True
                    self.stats.groups_seen += 1
                elif normalize_display_name(title) != normalize_display_name(row.display_name) and not row.is_group:
                    continue
                chat_key = self._resolve_key(row)
                if not chat_key:
                    continue
                handled += self._ingest_bubbles(chat_key, row.display_name, row.is_group,
                                                self.backend.read_visible_messages())
            except Exception:
                self.stats.errors += 1
                logger.debug("[wechat_pc] 会话扫描失败 %s", row.display_name, exc_info=True)
        return handled

    # ── 出站 ──
    def _display_name_for(self, chat_key: str) -> str:
        name = self.identity.display_name_for(chat_key)
        if name:
            return name
        kind = chat_key_kind(chat_key)
        body = str(chat_key or "")[len(f"wx:{kind}:"):] if kind else ""
        if kind in ("name", "group"):
            return body.split("#", 1)[0]
        return ""

    def _drain_outbound(self) -> int:
        if not self.policy.sends_allowed or self.frozen():
            return 0
        items = self.bridge.pull_outbound(self.account_id, limit=3)
        done = 0
        for pos, it in enumerate(items):
            item_id = int(it.get("id") or 0)
            if self.frozen():
                # 本轮前面的命令触发了守卫冻结：已认领的剩余命令立刻回执失败进人审队列，
                # 而不是等 180s 自动回收再盲重试
                self.bridge.ack(item_id, False, "guard:frozen")
                continue
            chat_key = str(it.get("chat_key") or "")
            text = str(it.get("text") or "")
            kind = str(it.get("kind") or "text")
            self._roll_day()
            verdict = may_send(
                self.policy, kind=kind, now=datetime.fromtimestamp(self._now()),
                connected_at=self.connected_at, sent_today=self._sent_today,
                sent_today_to_peer=self._sent_today_peer.get(chat_key, 0),
                last_sent_to_peer_ts=self._last_sent_peer.get(chat_key, 0.0),
                last_inbound_from_peer_ts=self._last_inbound.get(chat_key, 0.0),
                is_group=chat_key_kind(chat_key) == "group",
                mentioned=self._group_mentioned.get(chat_key, False),
            )
            if not verdict.allowed:
                self.stats.denied += 1
                logger.warning("[wechat_pc] 拒发 id=%s chat=%s reason=%s", item_id, chat_key, verdict.reason)
                self.bridge.ack(item_id, False, f"policy:{verdict.reason}")
                continue
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
            out: SendOutcome = self.sender.send(target, text, expected_wxid=expected_wxid)
            if out.ok and expected_wxid:
                self._wxid_verified_at[normalize_display_name(target)] = self._now()
            if out.ok:
                self.stats.sent += 1
                self._sent_today += 1
                self._sent_today_peer[chat_key] = self._sent_today_peer.get(chat_key, 0) + 1
                self._last_sent_peer[chat_key] = self._now()
                self._group_mentioned[chat_key] = False
                self.bridge.ack(item_id, True, "")
                done += 1
                continue
            self.stats.send_failed += 1
            self.bridge.ack(item_id, False, f"guard:{out.stage}:{out.reason}")
            if out.stage in ("title", "send", "echo"):
                self.stats.guard_freezes += 1
                self.freeze(self.freeze_on_guard_fail_sec, f"guard_{out.stage}")
                self._emit_notify("guard_freeze", f"{out.stage}:{out.reason}")
        return done

    # ── 一轮 ──
    def _heartbeat(self) -> None:
        """每轮都报存活（含读屏不可用的轮次——工作台要知道「进程活着但微信没登录」与「进程挂了」的区别）。"""
        hb = getattr(self.bridge, "heartbeat", None)
        if not callable(hb):
            return
        try:
            st = self.stats.as_dict()
            ok = hb(self.account_id, tier=self.policy.tier,
                    readonly=bool(getattr(self.backend, "readonly", False)) or not self.policy.sends_allowed,
                    stats={k: st.get(k) for k in ("ticks", "inbound", "sent", "denied", "send_failed",
                                                   "unknown_direction", "frozen_until", "last_disposition", "offline",
                                                   "last_readable")},
                    label=self.account_label)
            if not ok:
                self.stats.heartbeat_failed += 1
        except Exception:
            self.stats.heartbeat_failed += 1

    def tick(self) -> Dict[str, Any]:
        self.stats.ticks += 1
        summary: Dict[str, Any] = {"inbound": 0, "sent": 0, "readable": False}
        try:
            readable = self._inspect_screen()
            summary["readable"] = readable
            self.stats.last_readable = bool(readable)
            if readable:
                summary["inbound"] = self._scan_inbound()
                summary["sent"] = self._drain_outbound()
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


__all__ = ["BridgeClient", "WeChatPcService", "ServiceStats", "SeenStore", "bubble_fingerprint",
           "content_fingerprint", "time_unknown"]
