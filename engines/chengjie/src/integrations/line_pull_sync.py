# -*- coding: utf-8 -*-
"""LINE 收消息「位点锚定拉取兜底」（impl85 阶段1，2026-08-29，修 #44/#46 P1）。

事故：LINE 账号「只能发、收不到」——okline 的 SSE 流（``/api/operation/receive``）
每 ~10s 正常轮回、只回 ping、一条 operation 都不推（客户机与本机 zhiliao 两账号
同症；本机实测 receiver 轮回 1440+ 次入站恒 0，而 RPC 面完全正常）。SSE 的服务端
位点我们无法控制（该端点没有任何 revision 参数可传），但网关的 RPC 面全部可用，
且实测**自发一条消息 getLastOpRevision 从 60 → 61**——位点前进可用廉价 RPC 探测。

设计（与本仓 telegram ``poll_fallback``/``thread gap backfill`` 同哲学的拉取兜底）：

  1. **廉价探测**：每 tick 先打 ``getLastOpRevision``（一个整数）；与上次持平且已
     初始化 → 直接返回，什么都不扫（空闲期每 tick 只花一个最小 RPC）。
  2. **变更扫描**：位点前进 → ``getMessageBoxes(lastMessagesPerBox=1)`` 扫会话盒，
     对「末条消息 id 越过水位」的盒子按盒补拉 ``getRecentMessagesV2``，只取水位
     之上的新消息，按 id 升序交给 ``emit`` 回调（worker 侧复用 SSE 路径同一条
     落库/自动回复链；落库层按 platform_msg_id 去重 → 与 SSE 路径重叠零成本）。
  3. **首跑不回灌**：初始化只记录当前水位不投递（防把陈年历史当新消息灌进收件箱
     触发自动回复）；初始化之后出现的新盒子（新客户首次开口）按新消息补拉。
  4. **SSE 活着就休眠**：worker 侧传入「SSE 最近一次真收到 op 的时刻」，窗口内
     （默认 90s）本兜底完全不动——SSE 恢复健康时零双路负载。
  5. **token 腐化自愈**（同一事故链的第二个坑，本机 Uk4bhj 实锤）：RPC 抛
     code=119「Access token refresh required」时 okline **不会**自动刷新（它只认
     HTTP 401），本模块显式刷新一次并回写会话文件。刷新走
     ``line_token_refresh.refresh_line_tokens``（2026-09-06）：okline 自带的
     ``refresh_access_token`` 不带 ``X-Line-Access`` 头、网关一律回
     REQUEST_NEED_LOGIN——此前每一次「刷新失败」都是这个请求缺陷，却被当成
     「refresh token 也死了」上报 expired，把 7 天到期的号一个个判死（详见该模块头）。
     现在只有网关**明说** REQUEST_NEED_LOGIN（code=10004）才上报
     ``platform_session_health``（status=expired，接通坐席横幅/ops 卡/watchdog
     告警链）并进长冷却；网络等非终局失败只冷却重试，不判死。

状态落盘：``<tokens>.pullsync.json``（原子写；坏文件/缺文件 → 按未初始化重来，
最坏效果=重新锚定当前水位，绝不重复投递已去重的消息）。

配置 ``platform_login.line.pull_sync``：本模块**默认启用**——这是核心收消息路径的
缺陷修复而非新能力，默认关等于客户升级后照旧收不到消息；``enabled: false`` 为
紧急停用开关。其余键 interval_sec/boxes_limit/fetch_per_chat/sse_quiet_sec 见
``resolve_line_pull_cfg``。

门禁 tests/test_line_pull_sync.py。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.integrations.line_token_refresh import (
    is_relogin_error,
    is_token_stale_error,
    refresh_line_tokens,
)

logger = logging.getLogger(__name__)

#: 水位表体积上限（一个账号的活跃会话盒远小于此；防异常膨胀）
_MAX_BOX_WATERMARKS = 800
#: token 刷新尝试之间的最小间隔（防对着死 refresh token 反复打）
_REFRESH_COOLDOWN_SEC = 600.0
#: refresh token 已死（需重新扫码）后的重试冷却
_RELOGIN_COOLDOWN_SEC = 3600.0


def resolve_line_pull_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``platform_login.line.pull_sync``（缺省=启用，见模块 docstring 的理由）。"""
    line_cfg = (((config or {}).get("platform_login") or {}).get("line") or {})
    raw = line_cfg.get("pull_sync") if isinstance(line_cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    def _num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(raw.get(key, default))
        except Exception:
            v = default
        return max(lo, min(hi, v))

    return {
        "enabled": bool(raw.get("enabled", True)),
        "interval_sec": _num("interval_sec", 20.0, 5.0, 600.0),
        "boxes_limit": int(_num("boxes_limit", 50, 5, 200)),
        "fetch_per_chat": int(_num("fetch_per_chat", 10, 1, 50)),
        "sse_quiet_sec": _num("sse_quiet_sec", 90.0, 10.0, 3600.0),
    }


# ── token 续期（判定函数与实现收口在 line_token_refresh：worker 保活线程 / 复活扫描 /
# OBS 下载同一套；这里保留同名导出给既有调用方与门禁） ────────────────────────────

def refresh_client_token_result(client: Any) -> Dict[str, Any]:
    """显式续期 access token 并回写会话文件（带 X-Line-Access 头；绝不抛）。

    返回 ``line_token_refresh.refresh_line_tokens`` 的结果 dict：``ok`` 成功；
    ``relogin=True`` 是网关明说 REQUEST_NEED_LOGIN（refresh token 真死，只能重新扫码），
    其余失败（网络/网关抖动）调用方应冷却重试而不是判死。
    """
    return refresh_line_tokens(client, reason="rpc119")


def refresh_client_token(client: Any) -> bool:
    """布尔薄封装（``line_media`` OBS 下载 401 路径等只关心成败的调用方）。"""
    return bool(refresh_client_token_result(client).get("ok"))


# ── 归一化（网关响应形态防御：dict 包裹 / 裸 list 都见过） ─────────────────────

def _norm_revision(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    if isinstance(raw, dict):
        for v in raw.values():
            got = _norm_revision(v)
            if got is not None:
                return got
    return None


def _norm_boxes(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("messageBoxes", "messageBoxList", "boxes"):
            v = raw.get(key)
            if isinstance(v, list):
                return [b for b in v if isinstance(b, dict)]
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    return []


def _norm_messages(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        v = raw.get("messages")
        if isinstance(v, list):
            return [m for m in v if isinstance(m, dict)]
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    return []


def box_id_of(box: Dict[str, Any]) -> str:
    return str(box.get("id") or box.get("chatMid") or box.get("mid") or "").strip()


def box_last_message(box: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    last = box.get("lastMessages")
    if isinstance(last, list) and last and isinstance(last[0], dict):
        return last[0]
    lm = box.get("lastMessage")
    return lm if isinstance(lm, dict) else None


def is_group_mid(mid: str) -> bool:
    return (mid or "")[:1].lower() in ("c", "r", "s")


def msg_id_newer(msg_id: Any, watermark: Any) -> bool:
    """LINE 消息 id 是单调数字串；比不动数字时退回字符串比较（防形态漂移卡死）。"""
    a = str(msg_id or "").strip()
    b = str(watermark or "").strip()
    if not a:
        return False
    if not b:
        return True
    try:
        return int(a) > int(b)
    except Exception:
        return a > b


class LinePullSync:
    """单账号的拉取兜底状态机（线程内使用；worker 每 interval 调一次 ``tick``）。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        self_mid: str,
        state_path: str,
        emit: Callable[..., None],
        cfg: Optional[Dict[str, Any]] = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client_factory = client_factory
        self._self_mid = str(self_mid or "")
        self._state_path = Path(state_path)
        self._emit = emit
        self._cfg = dict(cfg or resolve_line_pull_cfg(None))
        self._now = now
        self._client: Any = None
        self._lock = threading.Lock()
        # 运行态（进程内观测）
        self.pulled_total = 0
        self.tick_total = 0
        self.last_status = ""
        self.last_pull_ts = 0.0
        self.relogin_until = 0.0
        self._refresh_attempt_ts = 0.0
        # 位点状态（落盘）
        self._rev: Optional[int] = None
        self._boxes: Dict[str, str] = {}
        self._initialized = False
        self._load_state()

    # ── 状态持久化 ────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            rev = raw.get("rev")
            boxes = raw.get("boxes")
            if isinstance(rev, int) and isinstance(boxes, dict):
                self._rev = rev
                self._boxes = {str(k): str(v) for k, v in boxes.items()}
                self._initialized = True
        except Exception:  # noqa: BLE001 —— 缺文件/坏文件都按未初始化
            self._rev, self._boxes, self._initialized = None, {}, False

    def _save_state(self) -> None:
        try:
            if len(self._boxes) > _MAX_BOX_WATERMARKS:
                # 只留 id 最大（最近活跃）的那批
                keep = sorted(
                    self._boxes.items(),
                    key=lambda kv: (len(kv[1]), kv[1]),
                    reverse=True,
                )[:_MAX_BOX_WATERMARKS]
                self._boxes = dict(keep)
            payload = json.dumps(
                {"rev": self._rev, "boxes": self._boxes, "ts": round(self._now(), 1)},
                ensure_ascii=False)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception:  # noqa: BLE001
            logger.debug("[line-pull] 状态落盘失败（忽略）", exc_info=True)

    # ── 客户端生命周期 ────────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _drop_client(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001
            logger.debug("[line-pull] client close 失败（忽略）", exc_info=True)
        self._client = None

    def reset_client(self) -> None:
        """丢掉当前 client，下轮 tick 从会话文件重建（worker 主连接续期后调用：
        本实例内存里还是旧 token，重建即跟上文件里的新 token）。线程安全。"""
        with self._lock:
            self._drop_client()

    def close(self) -> None:
        self._drop_client()

    # ── 主循环入口 ────────────────────────────────────────────────────────────

    def tick(self, *, sse_last_op_ts: float = 0.0) -> Dict[str, Any]:
        """跑一轮探测/补拉。返回 ``{"status": ..., "pulled": N, ...}``，绝不抛。"""
        with self._lock:
            return self._tick_locked(sse_last_op_ts=float(sse_last_op_ts or 0.0))

    def _tick_locked(self, *, sse_last_op_ts: float) -> Dict[str, Any]:
        self.tick_total += 1
        now = self._now()
        if sse_last_op_ts > 0 and (now - sse_last_op_ts) < float(self._cfg["sse_quiet_sec"]):
            return self._done("dormant")
        if self.relogin_until > now:
            return self._done("relogin_required")
        try:
            return self._pull_once()
        except Exception as exc:  # noqa: BLE001
            handled = self._handle_auth_error(exc)
            if handled is not None:
                return handled
            logger.debug("[line-pull] tick 异常", exc_info=True)
            self._drop_client()
            return self._done("error", error=f"{type(exc).__name__}: {exc}"[:200])

    def _handle_auth_error(self, exc: BaseException) -> Optional[Dict[str, Any]]:
        """token 类错误的自愈路径；非 token 错误返回 None 交上层。"""
        if is_relogin_error(exc):
            self.relogin_until = self._now() + _RELOGIN_COOLDOWN_SEC
            self._drop_client()
            logger.warning(
                "[line-pull] LINE 登录凭据已过期（REQUEST_NEED_LOGIN），需重新扫码 mid=%s",
                self._self_mid[:12])
            return self._done("relogin_required")
        if is_token_stale_error(exc):
            now = self._now()
            if (now - self._refresh_attempt_ts) < _REFRESH_COOLDOWN_SEC:
                return self._done("token_stale")
            self._refresh_attempt_ts = now
            client = self._client
            res = (refresh_client_token_result(client) if client is not None
                   else {"ok": False, "relogin": False, "error": "no client"})
            if res.get("ok"):
                logger.info("[line-pull] access token 已刷新 mid=%s", self._self_mid[:12])
                try:
                    return self._pull_once()
                except Exception as exc2:  # noqa: BLE001
                    if is_relogin_error(exc2):
                        return self._handle_auth_error(exc2)
                    logger.debug("[line-pull] 刷新后重试仍失败", exc_info=True)
                    return self._done("error", error=str(exc2)[:200])
            if res.get("relogin"):
                # 网关明说 REQUEST_NEED_LOGIN：refresh token 真死，只能重新扫码
                self.relogin_until = self._now() + _RELOGIN_COOLDOWN_SEC
                self._drop_client()
                logger.warning(
                    "[line-pull] access token 过期且 refresh token 已失效（%s），需重新扫码 mid=%s",
                    res.get("error") or "-", self._self_mid[:12])
                return self._done("relogin_required")
            # 非终局失败（网络/网关抖动/桥故障）：冷却后再试，**不**判死——2026-09-05 前
            # 正是把 okline 请求缺陷造成的必败当成「refresh token 死了」，才把 7 天到期
            # 的号一个个标成 expired。丢 client 让下轮从文件重建（worker 侧可能已续期）。
            logger.warning("[line-pull] access token 续期失败（%s），%.0fs 后重试 mid=%s",
                           res.get("error") or "-", _REFRESH_COOLDOWN_SEC, self._self_mid[:12])
            self._drop_client()
            return self._done("token_stale", error=str(res.get("error") or "")[:200])
        return None

    # ── 拉取核心 ─────────────────────────────────────────────────────────────

    def _pull_once(self) -> Dict[str, Any]:
        client = self._get_client()
        rev = _norm_revision(client.get_last_op_revision())
        if rev is None:
            return self._done("error", error="getLastOpRevision 返回无法解析")
        if self._initialized and self._rev is not None and rev == self._rev:
            return self._done("unchanged", rev=rev)

        boxes = _norm_boxes(client.get_message_boxes(
            limit=int(self._cfg["boxes_limit"]), last_messages_per_box=1))

        if not self._initialized:
            # 首跑锚定：只记水位不投递（防历史回灌触发自动回复）
            for box in boxes:
                bid = box_id_of(box)
                lm = box_last_message(box)
                if bid and lm and lm.get("id"):
                    self._boxes[bid] = str(lm.get("id"))
            self._rev = rev
            self._initialized = True
            self._save_state()
            return self._done("initialized", rev=rev, boxes=len(self._boxes))

        if self._rev is not None and rev < self._rev:
            # 服务端位点回退（换设备/重置）：重新锚定，不回灌
            self._boxes = {}
            for box in boxes:
                bid = box_id_of(box)
                lm = box_last_message(box)
                if bid and lm and lm.get("id"):
                    self._boxes[bid] = str(lm.get("id"))
            self._rev = rev
            self._save_state()
            return self._done("reanchored", rev=rev)

        pulled = 0
        for box in boxes:
            bid = box_id_of(box)
            if not bid:
                continue
            lm = box_last_message(box)
            last_id = str((lm or {}).get("id") or "")
            if not last_id:
                continue
            watermark = self._boxes.get(bid, "")
            if not msg_id_newer(last_id, watermark):
                continue
            pulled += self._pull_box(client, bid, watermark)
            self._boxes[bid] = last_id

        self._rev = rev
        self._save_state()
        if pulled:
            self.pulled_total += pulled
            self.last_pull_ts = self._now()
            logger.info("[line-pull] 兜底补拉 %d 条 mid=%s（SSE 流没送到的消息）",
                        pulled, self._self_mid[:12])
        return self._done("pulled" if pulled else "scanned", rev=rev, pulled=pulled)

    def _pull_box(self, client: Any, box_id: str, watermark: str) -> int:
        """补拉单个会话盒水位之上的消息，升序 emit。返回投递条数。"""
        try:
            rows = _norm_messages(client.get_recent_messages(
                box_id, int(self._cfg["fetch_per_chat"])))
        except Exception:  # noqa: BLE001 —— 单盒失败跳过，别拖垮整轮
            logger.debug("[line-pull] 补拉盒子失败 box=%s", box_id[:14], exc_info=True)
            return 0
        fresh = [m for m in rows if msg_id_newer(m.get("id"), watermark)]
        fresh.sort(key=lambda m: self._sort_key(m.get("id")))
        is_group = is_group_mid(box_id)
        n = 0
        for msg in fresh:
            if str(msg.get("from") or "") == self._self_mid:
                continue  # 自己（含其他设备）发的不当入站（与 SSE 路径 ignore_self 同口径）
            if msg.get("chunks"):
                try:
                    msg = client.decrypt_message(msg)
                except Exception:  # noqa: BLE001 —— 解不开保留密文形态，交 emit 层兜底
                    logger.debug("[line-pull] 解密失败 msg=%s", msg.get("id"), exc_info=True)
            try:
                self._emit(dict(msg), chat_key=box_id, is_group=is_group)
                n += 1
            except Exception:  # noqa: BLE001 —— 单条投递失败不阻断其余
                logger.debug("[line-pull] emit 失败 msg=%s", msg.get("id"), exc_info=True)
        return n

    @staticmethod
    def _sort_key(msg_id: Any):
        s = str(msg_id or "")
        try:
            return (0, int(s))
        except Exception:
            return (1, s)

    # ── 观测 ─────────────────────────────────────────────────────────────────

    def _done(self, status: str, **extra: Any) -> Dict[str, Any]:
        self.last_status = status
        out: Dict[str, Any] = {"status": status}
        out.update(extra)
        return out

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._cfg.get("enabled", True)),
            "tick_total": self.tick_total,
            "pulled_total": self.pulled_total,
            "last_status": self.last_status,
            "last_pull_ts": round(self.last_pull_ts, 1),
            "relogin_required": self.relogin_until > self._now(),
            "initialized": self._initialized,
            "boxes_tracked": len(self._boxes),
            "rev": self._rev,
        }


__all__ = [
    "LinePullSync",
    "resolve_line_pull_cfg",
    "refresh_client_token",
    "refresh_client_token_result",
    "refresh_line_tokens",
    "is_token_stale_error",
    "is_relogin_error",
    "box_id_of",
    "box_last_message",
    "is_group_mid",
    "msg_id_newer",
]
