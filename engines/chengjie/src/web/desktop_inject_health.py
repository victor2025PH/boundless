"""桌面壳注入健康信标（D1b）——汇聚各内嵌账号的「逐选择器命中」状态供运营看板。

注入脚本（``desktop/inject/tg-inject.js``）在状态变化或每 30s 心跳时上报一条健康记录；
本模块按 ``(platform, account_id)`` 保留**最新一条**，并把它分类成可读状态：

- ``ok``：注入正常（输入框/消息气泡都抓到）
- ``mismatch_composer`` / ``mismatch_bubble``：会话已开但抓不到输入框/气泡 → 官方改版选择器失配，
  运营可据此走 D1 覆写层热修（改 ``config/desktop_selector_profiles.json``）
- ``no_chat``：未登录或未进入会话（非故障）
- ``unsupported``：该平台无选择器档案

分类语义与渲染层 ``desktop/renderer/inject-status.js::deriveInjectState`` 对齐，使壳层状态条与
后端看板口径一致。``classify_inject_health`` 为纯函数，便于单测。
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Deque, Dict, List, Optional


def _ex_int(extract: Any, key: str) -> int:
    if not isinstance(extract, dict):
        return 0
    try:
        return int(extract.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def classify_inject_health(rec: Dict[str, Any]) -> str:
    """把一条上报记录分类为状态串（与渲染层 deriveInjectState 同口径）。"""
    if rec is None or not rec.get("supported", True):
        return "unsupported"
    composer = bool(rec.get("composer"))
    bubbles = int(rec.get("bubbles") or 0)
    chat_open = bool(rec.get("chatOpen"))
    if not chat_open and not composer:
        return "no_chat"
    if not composer:
        return "mismatch_composer"
    if chat_open and bubbles <= 0:
        return "mismatch_bubble"
    # ── 提取器失效（元素在、内容抓不出）────────────────────────────────────────
    # 官方改版更常见的形态是内层结构微调：``bubble`` 照样命中而 ``bubbleText``/``mid`` 提取全空。
    # 上面只数元素存在性的判据会给出 ``ok``，而实际上翻译按钮一个都不出现、消息一条都不回流。
    # 判据刻意用**比率型**（分母 > 0 而分子 == 0）：空会话分母为 0 → 天然不误报。
    # 走同一归一器：本函数既吃库内规范记录（snake）也吃原始上报（camelCase），
    # 两套键名各读一处早晚漂移成「一边判得出一边判不出」。
    extract = _norm_extract(rec.get("extract"))
    if (bubbles > 0
            and _ex_int(extract, "decorated") == 0
            and _ex_int(extract, "unresolved") > 0):
        # 正文提取塌了是根因（它一坏，ingest 必然也拿不到内容），先报它。
        return "mismatch_text"
    if (_ex_int(extract, "ingest_tried") > 0
            and _ex_int(extract, "ingest_keyed") == 0):
        return "mismatch_ingest"
    return "ok"


# 失配类状态（运营需关注：很可能官方改版导致选择器失效）
MISMATCH_STATUSES = (
    "mismatch_composer", "mismatch_bubble", "mismatch_text", "mismatch_ingest",
)

# 逐选择器键的诊断顺序（输入框/发送按钮最关键 → 影响出站；置前便于运营优先校准）
SELECTOR_KEYS = ("composer", "sendBtn", "bubble", "peerTitle")

# 提取器类失配 → 该校准哪个档案字段（selectors 布尔表只记「元素在不在」，
# 抓不出内容这类失效在那张表里全绿，必须按 status 归因，否则运营下钻到的是错的字段）。
# ``bubbleText`` 属 OVERLAYABLE_KEYS → 可经覆写层热修；``mid`` 在内置档是自定义函数，需发版。
EXTRACT_STATUS_KEYS = {
    "mismatch_text": "bubbleText",
    "mismatch_ingest": "mid",
}

# 诊断展示序（仅用于**同缺失数**时的稳定序）：沿用 SELECTOR_KEYS 原序后追加提取器键。
# 刻意不按「严重度」重排既有键——排序主键始终是影响面（缺失账号数），而「sendBtn 抓空到底
# 算不算故障」因平台而异（Telegram 的 .btn-send 空输入时也在，IG 的 Send 只在有内容时渲染），
# 没有逐平台真机事实就重排运营诊断只是换一种拍脑袋。低置信改用 advisory 标注表达。
_BREAKDOWN_ORDER = SELECTOR_KEYS + ("bubbleText", "mid")

# 低置信键：抓空未必是故障（见 selector_failure_breakdown docstring）。
_ADVISORY_KEYS = frozenset({"sendBtn"})


def selector_failure_breakdown(alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """聚合持续失配账号的「逐选择器缺失」计数（纯函数，便于单测）。

    输入 ``persistent_mismatches()`` 返回的告警列表（每条含 ``selectors{key:bool}``）。
    输出 ``[{key, missing}]``，仅含 ``missing>0`` 的键，按缺失账号数降序（同数按 SELECTOR_KEYS
    固定序稳定）。让运营从「N 个账号失配」精准下钻到「**哪个 selector key 在最多账号上抓空**」，
    优先热修该键，而非盲改整份覆写。

    仅统计**明确为 False**（抓空）的键；缺字段/非 dict 一律跳过，且经 ``missing_selectors``
    弃用与硬事实矛盾的整表（入库补默认值使「没上报」与「全坏」同形），避免误报。

    另按 ``status`` 归因提取器类失配（``bubbleText`` / ``mid``）——那类失效在 ``selectors``
    布尔表里全绿（元素确实存在），只能从状态反推该校准哪个字段。

    ``advisory: True`` 标记**低置信**项：``sendBtn`` 在多数平台是「输入框有内容才渲染」，
    空输入时抓空属正常态，若不标注它会凭恒 False 稳居榜首、把运营引向没坏的字段。
    """
    counts = {k: 0 for k in SELECTOR_KEYS}
    counts.update({k: 0 for k in EXTRACT_STATUS_KEYS.values()})
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        if isinstance(a.get("selectors"), dict):
            # 经 missing_selectors 过滤自相矛盾的表：入库时缺失键被补成 False，
            # 直接数会把「客户端没上报这张表」算成「四个键全坏」，把提取器类失配的
            # 账号在每个键上各记一票——下钻榜首就永远是没坏的那些键。
            for k in missing_selectors(a):
                counts[k] += 1
        field = EXTRACT_STATUS_KEYS.get(a.get("status"))
        if field:
            counts[field] += 1
    out = [{"key": k, "missing": counts[k], "advisory": k in _ADVISORY_KEYS}
           for k in _BREAKDOWN_ORDER if counts.get(k)]
    out.sort(key=lambda d: d["missing"], reverse=True)  # 同数按 _BREAKDOWN_ORDER 稳定
    return out

# 「持续失配」默认阈值（秒）——失配连续超过此时长升级为持续告警（区别于一闪而过的抖动）
DEFAULT_PERSIST_SEC = 300.0


def _norm_extract(raw: Any) -> Dict[str, int]:
    """归一提取器计数。上报侧是 camelCase（``ingestTried``），库内统一 snake_case。"""
    pairs = (("decorated", "decorated"), ("unresolved", "unresolved"),
             ("ingest_tried", "ingestTried"), ("ingest_keyed", "ingestKeyed"))
    out: Dict[str, int] = {}
    for snake, camel in pairs:
        v = 0
        if isinstance(raw, dict):
            v = _ex_int(raw, snake) or _ex_int(raw, camel)
        out[snake] = max(0, v)
    return out


def missing_selectors(rec: Dict[str, Any]) -> List[str]:
    """记录里**可信**的失效选择器清单（告警文案用，无可信信息即空）。

    为什么不能直接筛 ``selectors`` 里的 False：``_norm_selectors`` 把缺失的键补成
    ``False``，于是「没上报这张表」和「四个全坏」在库里长得一模一样。照直读会让
    「文字提取失效」的告警附上「缺失选择器: bubble, composer, sendBtn, peerTitle」
    ——那四个明明都在（气泡都数到了），运维照着去改一个没坏的选择器，比不给线索更糟。

    故只在**硬事实不与该表矛盾**时才采信它：数到了气泡而表说 ``bubble`` 坏、或
    ``composer`` 标志为真而表说 ``composer`` 坏，都说明这张表是补出来的默认值 → 整表
    弃用。宁可不给线索，不给错线索。
    """
    raw = (rec or {}).get("selectors")
    if not isinstance(raw, dict):
        return []
    # 只认**显式** False：键不在表里 ≠ 抓空（部分上报的表不该被补成「全坏」）。
    if int((rec or {}).get("bubbles") or 0) > 0 and raw.get("bubble") is False:
        return []
    if bool((rec or {}).get("composer")) and raw.get("composer") is False:
        return []
    return [k for k in SELECTOR_KEYS if raw.get(k) is False]


def _norm_selectors(raw: Any) -> Dict[str, bool]:
    out = {"bubble": False, "composer": False, "sendBtn": False, "peerTitle": False}
    if isinstance(raw, dict):
        for k in out:
            out[k] = bool(raw.get(k))
    return out


class InjectHealthStore:
    """线程安全的「每账号最新健康」内存存储（实时态，无需持久化）。"""

    def __init__(self, cap: int = 1000, event_cap: int = 200) -> None:
        self._cap = max(1, int(cap))
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}
        # 状态跃迁历史环（趋势/告警流用）：status 变化时各记一条
        self._events: Deque[Dict[str, Any]] = collections.deque(
            maxlen=max(1, int(event_cap)))

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return f"{platform}\t{account_id}"

    def record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """归一并落最新一条；返回带 status/ts 的规范记录。

        附带**状态跃迁追踪**：维护 `mismatch_since`（进入失配的起始 ts，跨 composer↔bubble
        子状态连续保留；恢复非失配时清空），用于「失配持续 N 分钟」判定；状态变化时写跃迁历史环。
        """
        platform = str((rec or {}).get("platform") or "").lower()
        account_id = str((rec or {}).get("account_id") or "")
        norm = {
            "platform": platform,
            "account_id": account_id,
            "supported": bool((rec or {}).get("supported", True)),
            "generic": bool((rec or {}).get("generic")),
            "can_ingest": bool((rec or {}).get("can_ingest")),
            "composer": bool((rec or {}).get("composer")),
            "bubbles": int((rec or {}).get("bubbles") or 0),
            "chatOpen": bool((rec or {}).get("chatOpen")),
            "selectors": _norm_selectors((rec or {}).get("selectors")),
            "extract": _norm_extract((rec or {}).get("extract")),
            "ts": float((rec or {}).get("ts") or time.time()),
        }
        norm["status"] = classify_inject_health(norm)
        if not platform and not account_id:
            return norm  # 无主键不入库（仍返回分类，便于探针自测）
        is_mismatch = norm["status"] in MISMATCH_STATUSES
        with self._lock:
            prev = self._latest.get(self._key(platform, account_id))
            prev_status = prev.get("status") if prev else None
            prev_mismatch_since = (prev or {}).get("mismatch_since")
            # mismatch_since：持续在失配态则沿用起点；刚进入失配记当前 ts；非失配清空
            if is_mismatch:
                _continuing = (prev_status in MISMATCH_STATUSES
                               and prev_mismatch_since)
                norm["mismatch_since"] = (
                    prev_mismatch_since if _continuing else norm["ts"]
                )
                # 提醒节流戳必须跟着一起继承：心跳每 30s 就写一条新记录，丢了它
                # 每次心跳都会被判成「还没提醒过」→ 30s 一条告警刷屏。
                norm["reminded_at"] = (prev or {}).get("reminded_at") if _continuing else None
            else:
                norm["mismatch_since"] = None
                norm["reminded_at"] = None  # 恢复即清零，下次再坏重新走首提
            # 状态跃迁 → 记历史环（含首次出现）
            if prev_status != norm["status"]:
                self._events.append({
                    "platform": platform, "account_id": account_id,
                    "status": norm["status"], "from": prev_status or "",
                    "ts": norm["ts"],
                })
            self._latest[self._key(platform, account_id)] = norm
            # 软上限：超量则丢弃最旧（按 ts）
            if len(self._latest) > self._cap:
                oldest = min(self._latest, key=lambda k: self._latest[k]["ts"])
                self._latest.pop(oldest, None)
        return norm

    def latest(self, stale_after: Optional[float] = None,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回所有账号最新记录（按 ts 倒序）。

        stale_after 给定时为每条标注 stale；并为失配账号附 `mismatch_secs`（已持续秒数）。
        """
        _now = float(now if now is not None else time.time())
        with self._lock:
            # 返回浅拷贝：避免调用方（或标注）污染存储中的原记录
            rows = [dict(r) for r in self._latest.values()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        for r in rows:
            if stale_after:
                r["stale"] = (_now - r.get("ts", 0)) > stale_after
            ms = r.get("mismatch_since")
            r["mismatch_secs"] = (max(0.0, _now - ms) if ms else 0.0)
        return rows

    def persistent_mismatches(
        self, threshold_sec: float = DEFAULT_PERSIST_SEC,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """返回失配已**持续 ≥ threshold_sec** 的账号（告警流用，按持续时长倒序）。"""
        _now = float(now if now is not None else time.time())
        thr = max(0.0, float(threshold_sec))
        out: List[Dict[str, Any]] = []
        with self._lock:
            for r in self._latest.values():
                ms = r.get("mismatch_since")
                if r.get("status") in MISMATCH_STATUSES and ms:
                    dur = _now - ms
                    if dur >= thr:
                        d = dict(r)
                        d["mismatch_secs"] = max(0.0, dur)
                        out.append(d)
        out.sort(key=lambda r: r.get("mismatch_secs", 0), reverse=True)
        return out

    #: 上报心跳间隔是 30s（见 core.js ``_HEALTH_HEARTBEAT_MS``）；超过这个宽限没再上报
    #: 就认为那个 webview 已经不在跑了。
    STALE_REPORT_SEC = 120.0

    def due_reminders(
        self, *, min_age_sec: float, interval_sec: float,
        stale_after: Optional[float] = None, now: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """返回**该发提醒**的持续失配账号（节流状态内置，恢复自动清零）。

        与 ``persistent_mismatches`` 的分工：那个是「看板/接口读数」（纯读，随便调多少次），
        这个是「告警出口」——取走即记 ``reminded_at``，同一账号在 ``interval_sec`` 内不再出。
        节流状态放在 store 内（与 ``PlatformSessionHealth.due_reminders`` 同模式），
        恢复正常时由 ``record`` 清零，故调用方无自有状态。

        ``stale_after``（默认 ``STALE_REPORT_SEC``）是**必需的误报防线**：本 store 只留每账号
        最新一条且永不过期，坐席关掉桌面壳/卸掉某账号时，最后那条若恰好是失配态就会**永久**
        挂在库里——催人去修一个根本没在跑的 webview，这种告警来两次就再没人信了。
        """
        _now = float(now if now is not None else time.time())
        min_age = max(0.0, float(min_age_sec or 0))
        interval = max(0.0, float(interval_sec or 0))
        stale = self.STALE_REPORT_SEC if stale_after is None else float(stale_after)
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, r in self._latest.items():
                if r.get("status") not in MISMATCH_STATUSES:
                    continue
                ms = r.get("mismatch_since")
                if not ms:
                    continue
                dur = _now - float(ms)
                if dur < min_age:
                    continue
                if stale > 0 and (_now - float(r.get("ts") or 0)) > stale:
                    continue  # 那个 webview 已经不在跑了
                last = float(r.get("reminded_at") or 0)
                if last and (_now - last) < interval:
                    continue
                r["reminded_at"] = _now
                d = dict(r)
                d["mismatch_secs"] = max(0.0, dur)
                d["first_reminder"] = not last
                out[key] = d
        return out

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """状态跃迁历史（最新在前）——供趋势/告警流展示。"""
        with self._lock:
            evs = list(self._events)
        evs.reverse()
        return evs[:max(1, min(int(limit or 50), 500))]

    def summary(self, persist_sec: Optional[float] = None,
                now: Optional[float] = None) -> Dict[str, int]:
        """状态计数概览（看板顶部徽标用）。

        persist_sec 给定时附 `persistent_mismatch`（失配持续超阈值的账号数）。
        """
        counts: Dict[str, int] = {}
        with self._lock:
            for r in self._latest.values():
                s = r.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        counts["mismatch"] = sum(counts.get(s, 0) for s in MISMATCH_STATUSES)
        if persist_sec is not None:
            counts["persistent_mismatch"] = len(
                self.persistent_mismatches(persist_sec, now=now))
        return counts

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._events.clear()


_STORE: Optional[InjectHealthStore] = None


def get_inject_health_store() -> InjectHealthStore:
    """进程级单例（与 account_registry 等同模式）。"""
    global _STORE
    if _STORE is None:
        _STORE = InjectHealthStore()
    return _STORE
