"""死 peer 统一登记表 — 永久不可达对象的跨发送链共享黑名单（2026-07-29）。

背景（P1 事故，生产日志实锤）：同一个已注销用户 7/27 晚被主动触达队列每
15 分钟重试一次、连发 2 小时+（``INPUT_USER_DEACTIVATED`` ×17），同类还有
``PEER_ID_INVALID`` / ``CHANNEL_INVALID``。根因＝**死 peer 拉黑逻辑散落各处**：
``proactive_topic`` 有一套自己的 ``companion_bad_peers.json``，但 A 线
``sender.send_message`` / B 线 autosend 各自失败后不拉黑 → 无效重发累积风控
信号（此前已有 kill-switch 误冻主账号 1h 的同源事故）。

本模块把「哪些错误算永久死 peer」收敛为**单一事实源纯函数**，配一个进程级
+ 持久化的共享登记表，让所有发送链共用同一黑名单：

- ``classify_send_error(exc)`` → reason 或 None（纯函数，可脱离运行时单测）；
  **区分永久 vs 可自愈**是关键——``PEER_ID_INVALID`` / ``CHANNEL_INVALID`` 是
  本地缓存缺 access_hash 的症状，dialogs 预热能救（见 sender._warm_group_peer），
  **不该**永久拉黑；只有账号注销 / 被拉黑 / 写禁止才是真·永久。
- ``DeadPeerRegistry``：record/is_blocked/unblock，平台命名空间 + 落盘 + 可选
  TTL + 容量上限；**只对永久 reason 落黑名单**（可自愈类 record 直接忽略）。

Feature flag ``ops.dead_peer_registry.enabled`` 默认关（新子系统铁律）：关时
接入点全部 no-op（查询恒放行、记录不写），生产行为零变化 → 可安全合入、灰度开启。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# reason → 是否永久（该落黑名单）。可自愈类留 False：交给既有预热自愈，不拉黑。
_PERMANENT_REASONS = {
    "deactivated": True,     # 账号注销/删除（INPUT_USER_DEACTIVATED）——绝对永久
    "blocked": True,         # 被对方拉黑 / 你拉黑了对方（USER_IS_BLOCKED / YOU_BLOCKED_USER）
    "write_forbidden": True,  # 群/频道写禁止（CHAT_WRITE_FORBIDDEN）——可能解除，配 TTL
    "peer_unresolved": False,  # PEER_ID_INVALID / CHANNEL_INVALID：本地缓存问题，可预热自愈
}

# 分类词表（大小写/下划线无关匹配）。顺序＝优先级：先判最硬的注销。
_REASON_MARKERS = (
    ("deactivated", ("INPUT_USER_DEACTIVATED", "USER_DEACTIVATED",
                     "USERDEACTIVATED", "USER_IS_DELETED")),
    ("blocked", ("USER_IS_BLOCKED", "USERISBLOCKED", "YOU_BLOCKED_USER",
                 "YOUBLOCKEDUSER")),
    # write_forbidden＝「无权限对该会话说话」，语义上**可能解除**（配 TTL 探路恢复）：
    # 群禁言解除 / 重新被拉进群 / 拿到管理员权限都属正常业务变化。
    # 2026-07-29：并入 B 线 autosend 原本独有的四项，让本词表成为三条发送链的
    # 单一事实源（此前 sender / proactive / autosend 各持一份，漂移无人察觉）。
    ("write_forbidden", ("CHAT_WRITE_FORBIDDEN", "CHATWRITEFORBIDDEN",
                         "CHAT_SEND_PLAIN_FORBIDDEN",
                         "CHAT_ADMIN_REQUIRED",        # 需管理员权限才能发
                         "CHANNEL_PRIVATE",            # 被踢出/频道私有
                         "USER_BANNED_IN_CHANNEL",     # 被频道封禁
                         "HAVE RIGHTS TO SEND")),      # "You don't have rights to send…"
    ("peer_unresolved", ("PEER_ID_INVALID", "PEERIDINVALID", "PEER ID INVALID",
                         "CHANNEL_INVALID", "CHANNELINVALID")),
)


def classify_send_error(exc: Any) -> Optional[str]:
    """发送异常 → 死 peer reason（纯函数）。非 peer 类错误返回 None。

    输入可为异常对象或字符串；同时看 ``type(exc).__name__`` 与文本
    （pyrogram 的 PeerIdInvalid/ChannelInvalid 类名 + Telegram 400 文案两种形态）。
    """
    name = type(exc).__name__ if not isinstance(exc, str) else ""
    blob = (name + " " + str(exc or "")).upper().replace(" ", "_")
    # 类名形态（pyrogram 异常）单独补一遍（上面已并入 blob，此处仅为可读性锚点）
    for reason, markers in _REASON_MARKERS:
        for m in markers:
            if m.replace(" ", "_") in blob:
                return reason
    # pyrogram 类名（无下划线）兜底
    if "PEERIDINVALID" in name.upper() or "CHANNELINVALID" in name.upper():
        return "peer_unresolved"
    return None


def is_permanent_reason(reason: Optional[str]) -> bool:
    """该 reason 是否属于「永久死 peer」（应落黑名单）。未知 reason → False。"""
    return bool(_PERMANENT_REASONS.get(str(reason or ""), False))


def peer_of(conversation_or_peer: Any) -> str:
    """统一 peer 口径：``conversation_id``（``platform:account:peer``）或裸 peer id → peer。

    两条发送链天然口径不同——A 线 sender 手里是裸 ``chat_id``，proactive 手里是
    ``conversation_id``。不归一化就会各写各的 key，共享黑名单名存实亡
    （2026-07-29 收敛时读真实 legacy 数据发现）。取末段对两种形态都正确。
    """
    s = str(conversation_or_peer or "").strip()
    return s.rsplit(":", 1)[-1] if ":" in s else s


def _norm_peer(platform: str, peer: Any) -> str:
    return f"{str(platform or 'telegram').strip().lower()}:{peer_of(peer)}"


class DeadPeerRegistry:
    """永久死 peer 共享黑名单（线程安全 + 落盘 + 容量上限 + 可选 TTL）。"""

    def __init__(self, path: Optional[str] = None, *,
                 max_entries: int = 5000, ttl_sec: float = 0.0,
                 ttl_by_reason: Optional[Dict[str, float]] = None,
                 legacy_paths: Optional[list] = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self._max = max(100, int(max_entries or 5000))
        self._ttl = max(0.0, float(ttl_sec or 0.0))   # 全局回落 TTL（0=永久）
        # 按 reason 分级 TTL（秒）：语义上并非所有「永久错误」都真永久——
        #   deactivated（账号注销）不可逆，恒永久（无视任何配置，安全第一）；
        #   blocked（被对方拉黑）/ write_forbidden（群禁言）**可能解除** → 配 TTL 到期后
        #   释放，允许下次探路重试一次（防「客户解除拉黑后我们永久放弃」的留存误伤）。
        # 未配则回落全局 _ttl（默认 0=永久，向后兼容旧行为）。
        self._ttl_by_reason: Dict[str, float] = {
            str(k): max(0.0, float(v or 0.0))
            for k, v in (ttl_by_reason or {}).items()
        }
        # 旧格式黑名单（如 proactive 的 companion_bad_peers.json 裸 peer 列表）：
        # 主文件无数据时一次性导入并落到新主文件——收敛各链独立黑名单时不丢历史。
        self._legacy_paths = [Path(p) for p in (legacy_paths or []) if p]
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    #: #73（0830 UDEKBY 实锤）：未配置时的**内置** TTL——blocked/write_forbidden
    #: 语义上可解除（对方取消拉黑/群禁言解除），旧默认「永久」让误标/陈旧标
    #: 变成「客户从此收不到任何自动回复且无人察觉」。deactivated 仍恒永久。
    _DEFAULT_REASON_TTL = {
        "blocked": 7 * 86400.0,          # 7 天后放行探路一次
        "write_forbidden": 3 * 86400.0,  # 3 天（群权限变化更频繁）
    }

    def _ttl_for_reason(self, reason: Optional[str]) -> float:
        """该 reason 的有效 TTL（秒）。deactivated 恒 0=永久；其余 reason 级优先、
        回落全局；全局也没配（0=旧「永久」语义）→ 内置默认 TTL（#73）。"""
        r = str(reason or "")
        if r == "deactivated":
            return 0.0   # 账号注销不可逆——即便误配 TTL 也不释放
        v = self._ttl_by_reason.get(r)
        if v is not None:
            return v
        if self._ttl > 0:
            return self._ttl
        return float(self._DEFAULT_REASON_TTL.get(r, 0.0))

    # ── 持久化 ────────────────────────────────────────────────
    def _ingest_file(self, f: Path) -> None:
        """读一个黑名单文件并并入 _data（新 dict 格式 / 旧裸 list 格式都认）。"""
        try:
            raw = json.loads(f.read_text("utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            # 新格式：{key: {reason, ts, first_ts, count}}
            for k, v in raw.items():
                if isinstance(v, dict):
                    self._data.setdefault(str(k), dict(v))
        elif isinstance(raw, list):
            # 旧格式列表 → 归 telegram；reason 未知按可解除的 blocked 记（保守：
            # 不假定是不可逆的注销，将来可配 TTL 探路恢复）。
            # 条目形态有两种：裸 peer id（A 线口径）与 conversation_id（proactive
            # 口径 ``platform:account:peer``）——``_norm_peer`` 统一归一化，故两者
            # 都落同一 key（否则共享名存实亡，2026-07-29 读真实 legacy 数据发现）。
            now = time.time()
            for x in raw:
                s = str(x).strip()
                if s:
                    self._data.setdefault(_norm_peer("telegram", s), {
                        "reason": "blocked", "ts": now, "first_ts": now, "count": 1})

    def _load(self) -> None:
        if self._path and self._path.is_file():
            self._ingest_file(self._path)
        self._ingest_legacy_locked(self._legacy_paths)

    def _ingest_legacy_locked(self, paths: list) -> bool:
        """从旧格式黑名单迁移（**仅在主表为空时**，幂等）。返回是否真导入了数据。

        「表非空就不导入」是关键：否则运营手工清理过的旧条目会被反复复活。
        """
        if self._data or not paths:
            return False
        for lp in paths:
            p = Path(lp)
            if p.is_file():
                self._ingest_file(p)
        if self._data:
            self._persist()
            return True
        return False

    def ingest_legacy(self, paths: list) -> bool:
        """公开的幂等 legacy 导入（供单例已被他人先建时补迁移，见 get_dead_peer_registry）。"""
        with self._lock:
            return self._ingest_legacy_locked([Path(p) for p in (paths or []) if p])

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False), "utf-8")
        except Exception:
            pass

    def _evict_if_needed(self) -> None:
        if len(self._data) <= self._max:
            return
        # 超限：按 ts 最旧淘汰到 90% 水位（防脏数据/被刷爆内存）
        target = int(self._max * 0.9)
        for k, _ in sorted(self._data.items(),
                           key=lambda kv: kv[1].get("ts", 0))[:len(self._data) - target]:
            self._data.pop(k, None)

    # ── 写 ────────────────────────────────────────────────────
    def record(self, platform: str, peer: Any, reason: str,
               evidence: str = "") -> bool:
        """登记一个死 peer。返回 True=已落黑名单；False=可自愈/未知 reason 忽略。

        ``evidence``（#73 溯源）：触发标记的原始错误摘要——「这个标哪来的」
        此前无人能答，排障只能猜。截 160 字防日志巨物入库。
        """
        if not is_permanent_reason(reason):
            return False
        key = _norm_peer(platform, peer)
        now = time.time()
        ev = str(evidence or "").strip()[:160]
        with self._lock:
            cur = self._data.get(key)
            if cur:
                cur["reason"] = reason
                cur["ts"] = now
                cur["count"] = int(cur.get("count", 0)) + 1
                if ev:
                    cur["evidence"] = ev
            else:
                self._data[key] = {
                    "reason": reason, "ts": now, "first_ts": now, "count": 1,
                    **({"evidence": ev} if ev else {})}
            self._evict_if_needed()
            self._persist()
        return True

    # ── 读 ────────────────────────────────────────────────────
    def is_blocked(self, platform: str, peer: Any) -> bool:
        """该 peer 是否在黑名单内（TTL 过期视为不再拉黑，惰性清理）。"""
        key = _norm_peer(platform, peer)
        with self._lock:
            ent = self._data.get(key)
            if not ent:
                return False
            ttl = self._ttl_for_reason(ent.get("reason"))
            if ttl > 0 and (time.time() - float(ent.get("ts", 0))) > ttl:
                self._data.pop(key, None)
                self._persist()
                return False
            return True

    def reason_of(self, platform: str, peer: Any) -> Optional[str]:
        with self._lock:
            ent = self._data.get(_norm_peer(platform, peer))
            return str(ent.get("reason")) if ent else None

    def info_of(self, platform: str, peer: Any) -> Optional[Dict[str, Any]]:
        """#73 可见面：该 peer 的标记详情（reason/ts/count/evidence）；无标 → None。

        刻意不做 TTL 惰性清理（只读快照，别在读路径写盘）；过期与否由
        ``is_blocked`` 判定，调用方要联判就两个都问。
        """
        with self._lock:
            ent = self._data.get(_norm_peer(platform, peer))
            return dict(ent) if ent else None

    def unblock(self, platform: str, peer: Any) -> bool:
        key = _norm_peer(platform, peer)
        with self._lock:
            if key in self._data:
                self._data.pop(key, None)
                self._persist()
                return True
        return False

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            by_reason: Dict[str, int] = {}
            by_platform: Dict[str, int] = {}
            for k, v in self._data.items():
                by_reason[v.get("reason", "?")] = by_reason.get(v.get("reason", "?"), 0) + 1
                plat = k.split(":", 1)[0]
                by_platform[plat] = by_platform.get(plat, 0) + 1
            return {
                "total": len(self._data),
                "by_reason": dict(sorted(by_reason.items())),
                "by_platform": dict(sorted(by_platform.items())),
                "ttl_sec": self._ttl,
                "ttl_by_reason": dict(self._ttl_by_reason),
            }

    def entries_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """全部条目只读快照（#88 存量核销迁移的输入；键=``platform:peer``）。"""
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# ── feature flag + 路径解析 + 单例 ─────────────────────────────

def dead_peer_enabled(config: Any) -> bool:
    """``ops.dead_peer_registry.enabled``（默认 False）。config 可为 dict/ConfigManager。"""
    cfg = getattr(config, "config", None)
    if cfg is None and isinstance(config, dict):
        cfg = config
    if not isinstance(cfg, dict):
        return False
    return bool(((cfg.get("ops") or {}).get("dead_peer_registry") or {}).get(
        "enabled", False))


def _dead_peer_cfg(config: Any) -> Dict[str, Any]:
    cfg = getattr(config, "config", None)
    if cfg is None and isinstance(config, dict):
        cfg = config
    if not isinstance(cfg, dict):
        return {}
    return ((cfg.get("ops") or {}).get("dead_peer_registry") or {})


_SINGLETON: Optional[DeadPeerRegistry] = None
_LOCK = threading.Lock()


def get_dead_peer_registry(*, path: Optional[str] = None,
                           ttl_sec: float = 0.0,
                           ttl_by_reason: Optional[Dict[str, float]] = None,
                           legacy_paths: Optional[list] = None,
                           ) -> DeadPeerRegistry:
    """进程级单例。首次调用决定路径/TTL；后续调用复用（path 变更被忽略）。

    ``legacy_paths`` 例外：单例可能已被**别的调用方**先建好（哪条链先发消息不确定
    ——sender 发送时建 vs proactive tick 时建），若「先到者定终身」，后到者带来的
    历史黑名单就永远迁不进共享表。故已存在时补一次幂等导入（仅表空时真导入）。
    """
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = DeadPeerRegistry(
                    path=path, ttl_sec=ttl_sec, ttl_by_reason=ttl_by_reason,
                    legacy_paths=legacy_paths)
                return _SINGLETON
    if legacy_paths:
        _SINGLETON.ingest_legacy(legacy_paths)
    return _SINGLETON


def peek_dead_peer_registry() -> Optional[DeadPeerRegistry]:
    """返回**已建立**的单例；未建立 → None（刻意不创建）。

    供拿不到落盘路径的调用方只读接入——如 ``AutosendWorker``，它的 config 是纯 dict
    没有 ``config_path``，若让它自己 ``get_...()`` 会建出一个「无路径纯内存」实例，
    共享当场失效。路径由持 ConfigManager 的 A 线 sender / proactive 首次建立时决定；
    单例尚未建立时本函数返回 None，调用方按「无共享数据」处理（各自本地机制照常）。
    """
    return _SINGLETON


def clear_on_delivery(platform: str, peer: Any) -> bool:
    """#73（0830 UDEKBY 实锤）：真实送达即清标——送达成功是「可达」的最硬证据。

    事故：peer 被 blocked 标 → 自动链静默跳过；手动链（不经守卫）05:47 实际
    送达成功，标却继续挂着=「AI 永久不理这位客户且坐席无感知」。任何一条链
    真发成功都应立即解除标记。单例未建立/未标记 → False（no-op，绝不抛）。
    """
    try:
        reg = peek_dead_peer_registry()
        if reg is None:
            return False
        return reg.unblock(platform, peer)
    except Exception:
        return False


def registry_from_config(config: Any) -> Optional[DeadPeerRegistry]:
    """按全局配置建/取共享单例；flag 关或解析失败 → None（绝不抛）。

    与 ``sender._dead_peer_guard`` 同一套路径/TTL 解析——#88 启动核销迁移需要
    在**任何发送链首次建单例之前**拿到带落盘路径的 registry，若各写一份解析
    参数会漂移（首建定终身）。config 须带 ``config_path``（ConfigManager）。
    """
    try:
        if not dead_peer_enabled(config):
            return None
        path = None
        cp = getattr(config, "config_path", None)
        if cp:
            path = str(Path(cp).parent / "dead_peers.json")
        dpc = _dead_peer_cfg(config)
        ttl = float((dpc.get("ttl_sec") or 0) or 0)
        tbr = dpc.get("ttl_by_reason")
        return get_dead_peer_registry(
            path=path, ttl_sec=ttl,
            ttl_by_reason=tbr if isinstance(tbr, dict) else None)
    except Exception:
        return None


def reconcile_stale_marks(registry: DeadPeerRegistry, inbox_db_path: Any,
                          *, log: Any = None) -> Dict[str, int]:
    """#88 存量旧标记核销：标记时间之后存在任一成功出站 → 清标（幂等迁移）。

    背景（0830 skuio 实锤，工单 #88）：#73 补齐送达清标钩子之前累积的旧标
    （Kate/Yhang「曾被对方拉黑」）没人核销——客户明明恢复可达（消息带勾送达）
    黄条仍常驻、自动回复停摆。证据面＝``messages`` 出站镜像（**只在成功路径
    写入**，见 sender._postsend_mirror_and_record / 编排器成功回写），故
    「标记 ts 之后有 direction='out' 行」＝真实送达发生过。

    只读打开 inbox.db（URI mode=ro，对活体生产库零写事务）；deactivated
    （账号注销，不可逆）刻意**不核销**——注销后不可能有真送达，若出站镜像
    晚于标记只可能是镜像时钟异常，宁可保守。返回 ``{checked, cleared}``。
    """
    import sqlite3

    out = {"checked": 0, "cleared": 0}
    try:
        entries = registry.entries_snapshot()
    except Exception:
        return out
    if not entries:
        return out
    try:
        con = sqlite3.connect(
            f"file:{Path(str(inbox_db_path)).as_posix()}?mode=ro",
            uri=True, timeout=5)
    except Exception:
        return out
    try:
        for key, ent in entries.items():
            out["checked"] += 1
            reason = str(ent.get("reason") or "")
            if reason == "deactivated":
                continue
            platform, _, peer = key.partition(":")
            if not platform or not peer:
                continue
            mark_ts = float(ent.get("ts") or 0)
            # conversation_id = platform:account:peer → 后缀匹配该 peer 的全部会话
            esc = (peer.replace("\\", "\\\\")
                       .replace("%", r"\%").replace("_", r"\_"))
            try:
                row = con.execute(
                    "SELECT 1 FROM messages WHERE direction='out' AND ts > ? "
                    "AND conversation_id LIKE ? ESCAPE '\\' LIMIT 1",
                    (mark_ts, f"{platform}:%:{esc}")).fetchone()
            except Exception:
                continue
            if row and registry.unblock(platform, peer):
                out["cleared"] += 1
                if log is not None:
                    try:
                        log.info(
                            "[dead-peer] 存量核销：%s（标记后有成功出站，黄条解除）",
                            key)
                    except Exception:
                        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def reconcile_peer_mark(registry: DeadPeerRegistry, inbox_db_path: Any,
                        platform: str, account_id: str, peer: Any,
                        *, log: Any = None) -> bool:
    """#88 二轮：单会话 lazy 核销——打开会话时若「标记后有成功出站」即清标。

    背景（0831 skuio v1.0.64 复测实锤）：启动一次性迁移只覆盖「boot 那一刻
    已可核销」的标记；之后才满足条件的（或走了无清标钩子的旁路送达）要等
    下一次重启才被扫到，黄条在此期间赖着。本函数挂在 send-caps 的 dead_peer
    可见块**之前**（前端每次打开会话都会带 chat_key 打它）——打开即复核，
    核销时机与重启解耦。

    与 ``reconcile_stale_marks`` 同判据（messages 出站镜像只在成功路径写入；
    deactivated 恒不核销），但用**精确** conversation_id 等值查询（platform:
    account:peer 三元组在调用点齐备）——LIKE 后缀匹配走不了索引，放进每次
    开会话的请求路径会变成全表扫。返回 True=本次真的清了标。
    """
    import sqlite3

    try:
        if registry is None or not registry.is_blocked(platform, peer):
            return False
        ent = registry.info_of(platform, peer) or {}
        if str(ent.get("reason") or "") == "deactivated":
            return False
        mark_ts = float(ent.get("ts") or 0)
        conv_id = f"{str(platform or '').lower()}:{account_id}:{peer_of(peer)}"
        con = sqlite3.connect(
            f"file:{Path(str(inbox_db_path)).as_posix()}?mode=ro",
            uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? "
                "AND direction='out' AND ts > ? LIMIT 1",
                (conv_id, mark_ts)).fetchone()
        finally:
            con.close()
        if row and registry.unblock(platform, peer):
            if log is not None:
                try:
                    log.info(
                        "[dead-peer] lazy 核销：%s:%s（打开会话复核到标记后"
                        "有成功出站，黄条解除）", platform, peer)
                except Exception:
                    pass
            return True
    except Exception:
        pass
    return False


def reset_singleton() -> None:
    """仅供测试：清空单例，让下次 get 重新按新 path 建。"""
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None


__all__ = [
    "classify_send_error", "is_permanent_reason", "peer_of", "DeadPeerRegistry",
    "dead_peer_enabled", "get_dead_peer_registry", "peek_dead_peer_registry",
    "clear_on_delivery", "registry_from_config", "reconcile_stale_marks",
    "reconcile_peer_mark", "reset_singleton",
]
