"""用户时区推断的 **IO 侧**：采集信号 → 调纯函数 → 两级缓存 → 落库。

`src/companion/user_clock.py` 是纯函数事实源（零 IO、绝不 raise，见其模块 docstring 的
trust 三档语义）；本模块只负责它周围那圈脏活：

1. **采信号**：自述居住地（episodic 记忆的 residence 槽 **+ 入站原文挖掘**）、
   电话国码（仅 WhatsApp）、入站消息的 UTC 小时分布、会话语种；
2. **调纯函数** `resolve_user_clock`；
3. **两级缓存**：进程级 dict（带容量上限）+ `conversation_meta.tz_*` 落库；
4. **给主动触达链一个便宜的查询入口**——planner 每 tick 会对几十个会话问「对方那边几点」，
   不缓存就等于每 tick 重扫几十遍消息表。

## 设计要点

- **负结果也缓存**：推不出来（新会话、样本不足、语种是 en/es/pt/fr 这类刻意不猜的）是
  **常态而非异常**。若只缓存成功结果，这些会话每 tick 都要重扫一遍消息表白烧 CPU，
  而且它们恰恰是占比最大的一群。负结果与正结果同 TTL。
- **DB 缓存跨重启生效**：进程缓存重启即空，但 `conversation_meta.tz_*` 还在——重启后
  第一轮 tick 不会把全部会话重算一遍。
- **进程缓存必须有上限**：常驻进程跑几个月，会话数只增不减；无上限的 dict 就是慢性内存
  泄漏。超限按写入顺序（≈ resolved_at 顺序）淘汰最旧项，O(1)。
- **`tz_*` 六列是缓存、不是事实源**：全部信号（记忆行/平台号码/入站消息）本身都在别处
  持久化，TTL 到点即可重算。故写入用低频的 `patch_conv_meta`（不碰 `update_conv_meta`
  的入站热路径 SQL），且落库失败只记数不影响返回值。
- **绝不 raise、未启用零成本**：配置门控在最前，未启用时一次 store 调用都不发生；
  每个信号各自 try/except（episodic 库挂了不该拖垮行为推断）；任何异常 → debug + None，
  即退化成「没有时区推断」＝本模块上线前的行为。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.companion.user_clock import (
    TRUST_ADVISORY,
    TRUST_NARROW,
    TRUST_REPLACE,
    UserClock,
    clock_from_tz_name,
    infer_from_stated_place,
    resolve_user_clock,
    schedule_clock,
)
from src.utils.memory_slots import SLOT_RESIDENCE, extract_slot

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_for_conversation",
    "resolve_peer_locale",
    "resolve_schedule_clock",
    "resolve_schedule_user_clock",
    "utc_hours_for_conversation",
    "utc_hours_from_rows",
    "stated_place_from_inbound_text",
    "stated_place_from_inbound_rows",
    "stated_place_from_reply_text",
    "invalidate",
    "dump_stats",
    "distribution",
    "reset_stats_for_tests",
    "clear_cache_for_tests",
]

# 默认值（配置缺省时用）。TTL 6 小时：时区不会一天变几次，而跨时区搬家属于罕见事件，
# 迟 6 小时纠正的代价远小于每 tick 重扫消息。
DEFAULT_TTL_SEC = 21600.0
# min_samples=12 是**在生产库上标定**出来的（2026-07-28，205 个会话 / 91 个有入站消息）：
# 门槛 8 与 12 收下的会话数完全相同（10 个），16 掉到 8 个、24 掉到 7 个、32 只剩 4 个。
# 说明真正在把关的是 `infer_from_activity` 的「≥4 个不同小时 + 胜出边际」两道闸，
# 样本数只是粗筛——定在 24 白丢掉 43% 的可推断会话（7→10）而换不到任何质量。
DEFAULT_MIN_SAMPLES = 12
DEFAULT_MIN_MARGIN = 0.35

# 进程缓存容量上限（见模块 docstring：无上限＝慢性内存泄漏）。
CACHE_CAPACITY = 2000

# 只有这些平台的 chat_key 才是**真电话号码**（baileys 的 peer id 就是号码）。
# ⚠️ 铁律：其它平台一律不传 phone——Telegram 的数字 user_id 形似 E.164
# （`user_clock._phone_digits` 的 docstring 专门警告过），误喂会推出完全错误的国家。
_PHONE_PLATFORMS = frozenset({"whatsapp", "wa"})

# source → trust 的反推表（落库只存 source，回读时重建 trust）。
_TRUST_BY_SOURCE: Dict[str, str] = {
    "stated_city": TRUST_REPLACE,
    "phone_cc": TRUST_REPLACE,
    "behavior_corroborated": TRUST_REPLACE,
    "peer_tz": TRUST_REPLACE,
    "persona_tz": TRUST_REPLACE,
    "explicit": TRUST_REPLACE,
    "behavior": TRUST_NARROW,
    "lang_default": TRUST_ADVISORY,
}

# resolve_schedule_clock 的四元 source。narrow/advisory 一律不当 explicit
# （08-19 事故后 schedule_clock trust=replace-only，本函数不放宽）。
_EXPLICIT_SOURCES = frozenset({
    "stated_city", "phone_cc", "behavior_corroborated", "explicit",
})
_SCHEDULE_SOURCES = frozenset({"peer_tz", "persona_tz", "server", "explicit"})

# 进程缓存：{conversation_id: (resolved_at, UserClock | None)}。None = 已缓存的负结果。
_CACHE: Dict[str, Tuple[float, Optional[UserClock]]] = {}
# prompt 注入侧的独立缓存（键是记忆桶而非会话，且信号集不同——见 resolve_peer_locale）。
# 刻意与 _CACHE 分开：两者 TTL 语义相同但**信任级不同**，混在一张表里会让
# distribution() 的读数把「敢用来排期的钟」和「只敢写进 prompt 的钟」搅成一锅。
_PEER_CACHE: Dict[str, Tuple[float, Optional[UserClock]]] = {}
# `invalidate` 打的「下次必须重算」标记（用完即消）。见 invalidate 的 docstring：
# 光清进程缓存挡不住 DB 那层的新鲜副本，强信号会被旧推断压住最长一个 TTL。
_FORCE_NEXT: Dict[str, float] = {}
_CACHE_LOCK = threading.Lock()

# 观测计数。两把独立锁且**绝不嵌套**（threading.Lock 不可重入，嵌套即死锁）。
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "cache_hit_mem": 0,
    "cache_hit_db": 0,
    "resolved": 0,
    "unresolved": 0,
    "persist_ok": 0,
    "persist_fail": 0,
    "disabled": 0,
    "inbound_stated": 0,
    "cache_stale_inbound": 0,
}


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------

def _bump(key: str, n: int = 1) -> None:
    """best-effort 计数；未知键忽略，绝不影响调用链。"""
    try:
        with _STATS_LOCK:
            if key in _STATS:
                _STATS[key] += int(n)
    except Exception:
        pass


def dump_stats() -> Dict[str, int]:
    """导出计数快照（ops 卡 / metrics 消费）。返回拷贝，改不动内部状态。"""
    try:
        with _STATS_LOCK:
            return dict(_STATS)
    except Exception:
        return {}


def reset_stats_for_tests() -> None:
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = 0


def distribution() -> Dict[str, int]:
    """进程缓存里按 ``source`` 聚合的计数（推不出来的记 ``"none"``）。

    给 ops 卡看「客户时区都是靠什么推出来的」——behavior 占比过高说明显式信号采集不足，
    none 占比过高说明大量会话根本推不出来（该考虑主动问一句所在城市）。
    """
    out: Dict[str, int] = {}
    try:
        with _CACHE_LOCK:
            entries = list(_CACHE.values())
    except Exception:
        return out
    for _resolved_at, clock in entries:
        try:
            key = "none" if clock is None else (str(getattr(clock, "source", "")) or "none")
        except Exception:
            key = "none"
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 进程缓存
# ---------------------------------------------------------------------------

def _cache_get(
    cid: str, cache: Optional[Dict[str, Tuple[float, Optional[UserClock]]]] = None,
) -> Optional[Tuple[float, Optional[UserClock]]]:
    try:
        with _CACHE_LOCK:
            return (_CACHE if cache is None else cache).get(cid)
    except Exception:
        return None


def _cache_put(
    cid: str, resolved_at: float, clock: Optional[UserClock],
    cache: Optional[Dict[str, Tuple[float, Optional[UserClock]]]] = None,
) -> None:
    """写进程缓存并维持容量上限。

    先 pop 再插入 → 键落到 dict 末尾（插入序即写入序），超限时从头淘汰＝FIFO 淘汰最旧项。
    刻意不做「扫全表找 min(resolved_at)」：写入序与 resolved_at 序在实际调用下一致，
    而 O(1) 淘汰在 >2000 活跃会话时不会每次插入都吃一遍全表。
    """
    try:
        target = _CACHE if cache is None else cache
        with _CACHE_LOCK:
            target.pop(cid, None)
            target[cid] = (float(resolved_at), clock)
            while len(target) > CACHE_CAPACITY:
                try:
                    target.pop(next(iter(target)))
                except StopIteration:
                    break
    except Exception:
        pass


def invalidate(conversation_id: str) -> None:
    """作废某会话的缓存（客户刚说了所在城市之类的强信号 → 下次真的重算）。

    只清进程缓存是**不够**的：上一轮 resolve 已把结果写进 `conversation_meta.tz_*`，
    那份副本在 TTL 内照样新鲜，下次调用会从库里把旧推断捞回来——强信号被压住最长一个
    TTL（6 小时），invalidate 等于白调。而本函数按契约拿不到 store（签名里没有），
    清不了库。故改为打一个**用完即消**的重算标记：下次 `resolve_for_conversation`
    越过两级缓存直接重推，并顺手把新结果写回库（旧副本自然被覆盖）。
    """
    try:
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        with _CACHE_LOCK:
            _CACHE.pop(cid, None)
            _FORCE_NEXT.pop(cid, None)
            _FORCE_NEXT[cid] = time.time()
            # 标记同样要有上限：被 invalidate 后再也没人查的会话不能永久占内存。
            while len(_FORCE_NEXT) > CACHE_CAPACITY:
                try:
                    _FORCE_NEXT.pop(next(iter(_FORCE_NEXT)))
                except StopIteration:
                    break
    except Exception:
        pass


def _consume_force_mark(cid: str) -> bool:
    """取走并清除重算标记（一次性；取不到 → False）。"""
    try:
        with _CACHE_LOCK:
            return _FORCE_NEXT.pop(cid, None) is not None
    except Exception:
        return False


def clear_cache_for_tests() -> None:
    try:
        with _CACHE_LOCK:
            _CACHE.clear()
            _PEER_CACHE.clear()
            _FORCE_NEXT.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置门控
# ---------------------------------------------------------------------------

def _enabled(cfg: Any) -> bool:
    """``companion.user_clock`` 段是否启用。缺省/空段/非映射 → False（零成本短路）。"""
    try:
        if not isinstance(cfg, Mapping) or not cfg:
            return False
        return bool(cfg.get("enabled"))
    except Exception:
        return False


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    """读数值配置项；**缺省/非法/≤0 一律回落默认值**。

    ≤0 刻意不当「关闭该门槛」讲：`ttl_sec: 0` 会让每个 tick 对每个会话重扫消息表
    （正是本模块要消灭的开销），`min_samples/min_margin: 0` 会让行为推断放弃采纳门槛
    收下垃圾推断（那道门槛正是它敢默认启用的前提）。要绕缓存走 ``force=True``。
    """
    try:
        raw = cfg.get(key)  # type: ignore[union-attr]
        if raw is None or isinstance(raw, bool):
            return float(default)
        val = float(raw)
        return val if val > 0 else float(default)
    except Exception:
        return float(default)


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    """整数配置项；口径同 `_cfg_float`（缺省/非法/≤0 → 默认值）。"""
    try:
        raw = cfg.get(key)  # type: ignore[union-attr]
        if raw is None or isinstance(raw, bool):
            return int(default)
        val = int(raw)
        return val if val > 0 else int(default)
    except Exception:
        return int(default)


# ---------------------------------------------------------------------------
# DB 缓存层（conversation_meta.tz_*）
# ---------------------------------------------------------------------------

def _trust_for_source(source: str, tz_name: str) -> str:
    """落库的 ``source`` → ``trust``；未知 source → ""（视为缓存未命中，重算）。

    ``phone_cc`` 是**双档**的（见 `user_clock._phone_clock`）：单时区国带 IANA 名 →
    replace；多时区国（US/CA/AU/ID/RU…）只定位到国家、``tz_name=""`` → advisory。
    只按 source 反推会把后者错升成 replace，故这里补一道「无 tz_name 一律降 advisory」——
    与原判据完全一致，且方向永远是**往保守降**，不可能凭空放宽调度权限。
    """
    trust = _TRUST_BY_SOURCE.get(str(source or ""), "")
    if not trust:
        return ""
    if trust != TRUST_ADVISORY and not str(tz_name or "").strip():
        return TRUST_ADVISORY
    return trust


def _from_conv_meta(
    inbox_store: Any, cid: str, now_ts: float, ttl_sec: float,
) -> Tuple[bool, Optional[UserClock], float]:
    """读库里的 ``tz_*`` 缓存 → ``(是否命中, UserClock|None, resolved_at)``。

    「命中且 clock 为 None」= 库里存着**新鲜的负结果**，与「没命中」是两件事，故用
    独立的 hit 标志而非拿 None 兼表两义。
    """
    try:
        meta = inbox_store.get_conv_meta(cid) or {}
    except Exception:
        logger.debug("[user_clock] 读 conv_meta 失败 cid=%s", cid, exc_info=True)
        return (False, None, 0.0)
    try:
        resolved_at = float(meta.get("tz_resolved_at") or 0)
    except Exception:
        resolved_at = 0.0
    if resolved_at <= 0 or (now_ts - resolved_at) > float(ttl_sec):
        return (False, None, 0.0)
    try:
        source = str(meta.get("tz_source") or "").strip()
        if not source:
            return (True, None, resolved_at)   # 新鲜的负结果
        tz_name = str(meta.get("tz_hint") or "").strip()
        trust = _trust_for_source(source, tz_name)
        if not trust:
            return (False, None, 0.0)          # 未知 source（别的版本写的）→ 重算
        raw_conf = meta.get("tz_confidence")
        return (
            True,
            UserClock(
                tz_name=tz_name,
                offset_hours=float(meta.get("tz_offset") or 0.0),
                source=source,
                confidence=float(raw_conf) if raw_conf is not None else -1.0,
                country=str(meta.get("tz_country") or ""),
                city_slug="",   # 不落库（对下游消费无用，省一列）
                trust=trust,
            ),
            resolved_at,
        )
    except Exception:
        logger.debug("[user_clock] 重建 UserClock 失败 cid=%s", cid, exc_info=True)
        return (False, None, 0.0)


def _persist(inbox_store: Any, cid: str, clock: Optional[UserClock], now_ts: float) -> None:
    """把结果（含负结果）写回 ``conversation_meta``；失败只记数，绝不影响返回值。

    负结果写 ``tz_source=""`` + ``tz_confidence=-1`` + ``tz_resolved_at=now``：读侧据此
    重建出 None，于是「推不出来」这件事本身也被缓存住，不会每 tick 重扫消息。
    """
    fields: Dict[str, Any] = {"tz_resolved_at": float(now_ts)}
    if clock is None:
        fields.update({
            "tz_hint": "", "tz_confidence": -1.0, "tz_source": "",
            "tz_country": "", "tz_offset": 0.0,
        })
    else:
        fields.update({
            "tz_hint": str(clock.tz_name or ""),
            "tz_confidence": float(clock.confidence),
            "tz_source": str(clock.source or ""),
            "tz_country": str(clock.country or ""),
            "tz_offset": float(clock.offset_hours),
        })
    try:
        inbox_store.patch_conv_meta(cid, fields)
        _bump("persist_ok")
    except Exception:
        _bump("persist_fail")
        logger.debug("[user_clock] 落库失败 cid=%s", cid, exc_info=True)


# ---------------------------------------------------------------------------
# 信号采集
# ---------------------------------------------------------------------------

def _list_recent_messages(
    inbox_store: Any, conversation_id: str, *, limit: int = 120,
) -> List[Mapping[str, Any]]:
    """取最近消息；失败 → []。供小时序列与入站城市挖掘共用，避免 cache miss 双扫。"""
    try:
        rows = inbox_store.list_recent_messages(
            str(conversation_id or ""), limit=int(limit)) or []
        return list(rows)
    except Exception:
        logger.debug("[user_clock] 取消息失败 cid=%s", conversation_id, exc_info=True)
        return []


def utc_hours_from_rows(rows: Any) -> List[int]:
    """已取到的消息行 → 入站 UTC 小时序列（纯函数，供 resolver / 覆盖率工具共用）。"""
    out: List[int] = []
    for row in rows or []:
        try:
            if str((row or {}).get("direction") or "") != "in":
                continue
            ts = float((row or {}).get("ts") or 0)
            if ts <= 0:
                continue
            out.append(int(datetime.fromtimestamp(ts, tz=timezone.utc).hour))
        except Exception:
            continue
    return out


def utc_hours_for_conversation(
    inbox_store: Any, conversation_id: str, *, limit: int = 120,
) -> List[int]:
    """会话最近 ``limit`` 条消息里**入站**消息的 UTC 小时序列（行为推断的输入）。

    只算入站：出站是我们自己的发送节奏（受 worker/排班驱动），拿它推客户作息等于推自己。
    ``ts <= 0`` 的行跳过（未知时间的占位行会污染直方图）。异常 → ``[]``（行为推断这一路
    静默退场，其它信号照常）。
    """
    return utc_hours_from_rows(
        _list_recent_messages(inbox_store, conversation_id, limit=limit))


# 英文/正式中文的居住地线索（**刻意只放在本模块**，不进 `memory_slots`）。
#
# 为什么不去加宽 `memory_slots._RESIDENCE_PATTERNS`：那个 `extract_slot` 还被**记忆矛盾
# 消解**消费——在那里放宽正则，一个误判会直接**覆盖掉**用户的真记忆（旧值被标历史）。
# 放在这里，误判的最坏结果只是时区推断偏一点，而且下游 `infer_from_stated_place` 的
# 35 城白名单会再挡一道。血溅半径差一个数量级。
#
# 该补这一层的实证依据（2026-07-28 生产库勘察）：`memory_slots` 的居住地正则**只认中文**
# （住在/家在/定居/搬到），而生产会话里 en 与未定语种占多数——英文用户的自述城市这个
# **最强信号**此前永远采不到。刻意不收 "I'm from X"（出身地 ≠ 当前时区，而本产品用户
# 大量是移民/外派，问的是「你那边现在几点」而不是「你老家在哪」）。
_RESIDENCE_HINT_RE = re.compile(
    r"(?:i\s+live\s+in|i'?m\s+living\s+in|i\s+am\s+living\s+in|living\s+in|"
    r"based\s+in|i\s+stay\s+in|居住地[：:]?\s*|现居|长居|常住)"
    r"\s*(?P<v>[A-Za-z][A-Za-z .'-]{1,28}|[\u4e00-\u9fa5]{2,10})",
    re.IGNORECASE,
)


def _place_hint_from_text(text: Any) -> str:
    """从一条记忆原文里抽居住地线索（`extract_slot` 未命中时的多语兜底）。异常 → ""。"""
    try:
        match = _RESIDENCE_HINT_RE.search(str(text or ""))
        return str(match.group("v")).strip() if match else ""
    except Exception:
        return ""


# 入站原文挖掘（2026-08-19）：记忆槽经常空着，但客户聊里已经说过「我在曼谷」。
# 血溅半径刻意比 `infer_from_stated_place(整句)` 小——那条会对「曼谷那家店」子串命中，
# 拿来当 replace 时钟会把路过提及升级成居住地。这里只认三种高置信句式：
# residence 槽 / live-in 线索 / 「我在 / I'm in」+ 35 城白名单。
# 出行/老家一律弃权（「明天去纽约」「I'm from Manila」不是「你那边现在几点」）。
_HERE_RE = re.compile(
    r"(?:我(?:现在)?(?:就)?在|现在在|"
    r"i(?:['’]?m|\s+am)(?:\s+currently)?\s+in)\s*"
    r"(?P<v>[\u4e00-\u9fa5]{2,12}|[A-Za-z][A-Za-z .'-]{1,28})",
    re.IGNORECASE,
)
_SKIP_INBOUND_RE = re.compile(
    r"(?:要去|打算去|准备去|飞往|飞去|出差|旅游|旅行|路过|"
    r"明天去|下周去|下次去|去过|"
    r"老家(?:在|是)|故乡|出身|家乡|"
    r"going\s+to|flying\s+to|headed\s+to|trip\s+to|"
    r"will\s+be\s+in|next\s+week|"
    r"i['’]?m\s+from|i\s+was\s+born|hometown)",
    re.IGNORECASE,
)
_HERE_TRAIL_RE = re.compile(r"(?:这边|那边|这里|那里|呢|啊|哈|呀|的)$")


def _place_ok(value: Any) -> bool:
    """Q-19（#294）：居住地候选必须像个地名——与画像槽 location 校验同一单点
    （``profile_slots.slot_validate``）。0911 实锤 ``memory_slots`` 的裸「住」正则把
    「我住在去嵐山散步 / 我住在得慣呀 / 我住在河邊行」整句写成 user_stated 事实。
    单点不可用时放行（不因校验器缺失把整条居住地链打死）。"""
    try:
        from src.companion.goals.profile_slots import place_value_ok
        return bool(place_value_ok(value))
    except Exception:
        return True


def stated_place_from_inbound_text(text: Any) -> str:
    """从一条**入站原文**抽可当 replace 时钟的地点；抽不出 / 出行 / 老家 → ""。

    纯函数、绝不 raise。记忆槽路径仍走 `_stated_place`（事实已经过抽取），本函数
    只服务「聊天里说过但没进 episodic」这条漏网。
    """
    try:
        raw = str(text or "").strip()
        if len(raw) < 2:
            return ""
        if _SKIP_INBOUND_RE.search(raw):
            return ""
        slot = extract_slot(raw)
        if slot and slot[0] == SLOT_RESIDENCE and slot[1]:
            if _place_ok(slot[1]):
                return str(slot[1])
            return ""
        hint = _place_hint_from_text(raw)
        if hint:
            return hint if _place_ok(hint) else ""
        match = _HERE_RE.search(raw)
        if not match:
            return ""
        value = _HERE_TRAIL_RE.sub("", str(match.group("v") or "").strip())
        if not value or infer_from_stated_place(value) is None:
            return ""
        return value
    except Exception:
        return ""


_REPLY_PUNCT_RE = re.compile(r"[，。！？、.!?\s~～]+")


def _is_bare_whitelist_city(text: str) -> bool:
    """整段文字就是 35 城白名单里的一个城名（不是句子里碰巧含城名）。"""
    clock = infer_from_stated_place(text)
    if clock is None:
        return False
    slug = str(getattr(clock, "city_slug", "") or "").strip().lower()
    if not slug:
        return False
    try:
        from src.companion.persona_location import CITY_PRESETS
        preset = CITY_PRESETS.get(slug) or {}
    except Exception:
        preset = {}
    aliases = {
        slug,
        str(preset.get("city_zh") or "").strip().lower(),
        str(preset.get("city_en") or "").strip().lower(),
    }
    return str(text or "").strip().lower() in aliases


def stated_place_from_reply_text(text: Any) -> str:
    """问城市之后的短答（「曼谷」「Bangkok」）也能抽；长句仍走入站挖掘。

    整句先走 ``stated_place_from_inbound_text``（我在/住在/I'm in）。抽不出
    且原文很短（≤24）时，**整段必须就是白名单城名**——「明天去纽约」
    「曼谷那家店」含城名但不等于城名，弃权。出行/老家句式仍弃权。
    """
    try:
        raw = str(text or "").strip()
        if not raw:
            return ""
        place = stated_place_from_inbound_text(raw)
        if place:
            return place
        if len(raw) > 24:
            return ""
        cleaned = _REPLY_PUNCT_RE.sub("", raw)
        if not cleaned or not _is_bare_whitelist_city(cleaned):
            return ""
        return cleaned
    except Exception:
        return ""


def stated_place_from_inbound_rows(rows: Any) -> Tuple[str, float]:
    """最近消息里最新一条合格入站地点 ``(place, ts)``；没有 → ``("", 0)``。"""
    best_place = ""
    best_ts = 0.0
    for row in rows or []:
        try:
            if str((row or {}).get("direction") or "") != "in":
                continue
            ts = float((row or {}).get("ts") or 0)
            if ts <= 0:
                continue
            text = (row or {}).get("text") or (row or {}).get("content") or ""
            place = stated_place_from_inbound_text(text)
            if place and ts >= best_ts:
                best_ts, best_place = ts, place
        except Exception:
            continue
    return best_place, best_ts


def _stated_place_with_ts(episodic_store: Any, memory_key: str) -> Tuple[str, float]:
    """episodic 记忆里客户自述的居住地（取 ``created_at`` 最大的一条）。

    ``source="user_stated"`` 优先——AI 推断出来的居住地不该反过来当成客户亲口说的强信号。
    该筛选返回空（或旧版 store 不认这个入参）时退化为一次不带 source 的查询：拿不到
    最优信号也比整条路径静默失效好。
    返回 ``(place, created_at)``；没有 → ``("", 0)``。
    """
    if episodic_store is None:
        return "", 0.0
    key = str(memory_key or "").strip()
    if not key:
        return "", 0.0
    rows: List[Any] = []
    try:
        rows = list(episodic_store.list_rows(
            prefix=key, limit=80, source="user_stated") or [])
    except Exception:
        logger.debug("[user_clock] list_rows(user_stated) 失败 key=%s", key, exc_info=True)
        rows = []
    if not rows:
        try:
            rows = list(episodic_store.list_rows(prefix=key, limit=80) or [])
        except Exception:
            logger.debug("[user_clock] list_rows 失败 key=%s", key, exc_info=True)
            rows = []
    best_place = ""
    best_ts: Optional[float] = None
    # 多语兜底的候选单独记一档：`extract_slot` 命中（中文居住地槽，语义最硬）永远优先，
    # 只有一条都没有时才用本模块的宽口径线索。
    hint_place = ""
    hint_ts: Optional[float] = None
    for row in rows:
        try:
            content = str((row or {}).get("content") or "")
            try:
                created = float((row or {}).get("created_at") or 0)
            except Exception:
                created = 0.0
            slot = extract_slot(content)
            if slot and slot[0] == SLOT_RESIDENCE and slot[1]:
                # 不依赖 list_rows 的排序（各实现口径可能不同）：显式取 created_at 最大者，
                # 「后来搬家」才能盖住旧地址。
                if best_ts is None or created > best_ts:
                    best_ts, best_place = created, str(slot[1])
                continue
            cand = _place_hint_from_text(content)
            if cand and (hint_ts is None or created > hint_ts):
                hint_ts, hint_place = created, cand
        except Exception:
            continue
    if best_place:
        return best_place, float(best_ts or 0.0)
    if hint_place:
        return hint_place, float(hint_ts or 0.0)
    return "", 0.0


def _stated_place(episodic_store: Any, memory_key: str) -> str:
    place, _ts = _stated_place_with_ts(episodic_store, memory_key)
    return place


def _phone_from_conv(conv: Mapping[str, Any]) -> str:
    """会话 → 电话号码，**仅 WhatsApp 系平台**（见 `_PHONE_PLATFORMS` 的铁律注释）。"""
    try:
        platform = str((conv or {}).get("platform") or "").strip().lower()
        if platform not in _PHONE_PLATFORMS:
            return ""
        return str((conv or {}).get("chat_key") or "").strip()
    except Exception:
        return ""


def _language_from_conv(conv: Mapping[str, Any]) -> str:
    """会话语种；``"unknown"``（探测失败的占位值）视为空，别拿它去查语种默认国家。"""
    try:
        lang = str((conv or {}).get("language") or "").strip()
        return "" if lang.lower() in ("", "unknown") else lang
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def resolve_for_conversation(
    conversation_id: str,
    *,
    inbox_store: Any,
    episodic_store: Any = None,
    memory_key: str = "",
    cfg: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    force: bool = False,
    last_inbound_ts: float = 0.0,
) -> Optional[UserClock]:
    """推断某会话客户的时钟（带两级缓存的唯一入口）。

    ``cfg`` = ``companion.user_clock`` 配置段；未启用 → 直接 None 且**一次 store 调用
    都不发**。``force=True`` 越过两级缓存强制重算（客户刚说了城市之类的场景）。
    ``last_inbound_ts``：规划器快照里对方最后开口的时间；比缓存 ``resolved_at`` 新
    则视为信号可能变了（新说了城市 / 新的作息样本），越过两级缓存重推——否则入站
    挖掘要等满 TTL（默认 6h）才生效，上午说了「我在曼谷」晚上还按北京钟发晚安。
    返回 None = 推不出来，调用方按「没有推断」处理（`user_clock` 各消费函数吃 None 会
    逐位回落服务器钟＝等价旧行为）。**绝不 raise**。
    """
    try:
        if not _enabled(cfg):
            _bump("disabled")
            return None
        cid = str(conversation_id or "").strip()
        if not cid:
            return None

        ttl_sec = _cfg_float(cfg, "ttl_sec", DEFAULT_TTL_SEC)
        min_samples = _cfg_int(cfg, "min_samples", DEFAULT_MIN_SAMPLES)
        min_margin = _cfg_float(cfg, "min_margin", DEFAULT_MIN_MARGIN)
        try:
            now_ts = float(now) if now is not None else time.time()
        except Exception:
            now_ts = time.time()
        try:
            inbound_ts = float(last_inbound_ts or 0.0)
        except Exception:
            inbound_ts = 0.0

        def _inbound_stale(resolved_at: float) -> bool:
            return inbound_ts > 0 and inbound_ts > float(resolved_at or 0) + 1e-6

        # 无条件取走标记（force 本轮也会重算，留着它只会让下一轮白重算一次）。
        marked = _consume_force_mark(cid)
        if not (force or marked):
            cached = _cache_get(cid)
            if cached is not None and (now_ts - cached[0]) <= ttl_sec:
                if not _inbound_stale(cached[0]):
                    _bump("cache_hit_mem")
                    return cached[1]
                _bump("cache_stale_inbound")
            else:
                hit, db_clock, db_at = _from_conv_meta(inbox_store, cid, now_ts, ttl_sec)
                if hit and not _inbound_stale(db_at):
                    # 沿用库里的 resolved_at（而非 now）：否则跨重启回读会把 TTL 一次次续期，
                    # 一个陈旧推断可能永远不过期。
                    _cache_put(cid, db_at, db_clock)
                    _bump("cache_hit_db")
                    return db_clock
                if hit and _inbound_stale(db_at):
                    _bump("cache_stale_inbound")

        # 每路信号各自兜底：某个库挂了不该拖垮其它信号（get_conversation 一次取到，
        # phone 与 language 共用，省一次查询）。
        try:
            conv = inbox_store.get_conversation(cid) or {}
        except Exception:
            logger.debug("[user_clock] 取会话失败 cid=%s", cid, exc_info=True)
            conv = {}

        rows = _list_recent_messages(inbox_store, cid)
        mem_place, mem_ts = _stated_place_with_ts(episodic_store, memory_key)
        inb_place, inb_ts = stated_place_from_inbound_rows(rows)
        stated = mem_place
        if inb_place and (not mem_place or inb_ts >= mem_ts):
            stated = inb_place
            _bump("inbound_stated")

        clock = resolve_user_clock(
            stated_place=stated,
            phone=_phone_from_conv(conv),
            activity_hours=utc_hours_from_rows(rows),
            language=_language_from_conv(conv),
            min_samples=min_samples,
            min_margin=min_margin,
            now=datetime.fromtimestamp(now_ts, tz=timezone.utc),
        )
        _cache_put(cid, now_ts, clock)
        _bump("resolved" if clock is not None else "unresolved")
        _persist(inbox_store, cid, clock, now_ts)
        return clock
    except Exception:
        logger.debug(
            "[user_clock] resolve_for_conversation 失败 cid=%s",
            conversation_id, exc_info=True)
        return None


def resolve_peer_locale(
    cache_key: str,
    *,
    episodic_store: Any = None,
    memory_key: str = "",
    phone: Any = None,
    platform: str = "",
    language: Any = None,
    cfg: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Optional[UserClock]:
    """给 **prompt 注入** 用的时钟：只吃显式信号，刻意**不吃**行为统计推断。

    与 `resolve_for_conversation` 的信任级差异是有意的，也是本模块最重要的一条设计判断：

    - **调度侧**（主动触达择时/安静时段）敢吃行为推断，因为 `user_clock.in_quiet_hours`
      对 ``narrow`` 档只做「收窄」——统计推断错了最多是少发一条，绝不会新开一个凌晨窗口。
    - **prompt 侧**没有这层安全网：一旦写进「对方那边现在约 06:12」，LLM 就会把它当事实
      复述给客户（「你那边天还没亮吧」）。猜错＝当着客户面说错事实，比不说话糟得多。
      故这里只接受客户自述城市 / WhatsApp 号码国码 / 语种默认国家，宁缺勿错。

    无显式信号 → None（不注入任何时间行）。``activity_hours`` 一律不传。**绝不 raise**。

    Args:
        cache_key: 缓存键（用记忆桶 key，跨平台同一人共用一份推断）。
        phone/platform: 仅 `_PHONE_PLATFORMS` 内的平台会真的把 phone 当号码用。
        cfg: ``companion.user_clock`` 段；未启用 → None 且不查记忆库。
    """
    try:
        if not _enabled(cfg):
            _bump("disabled")
            return None
        key = str(cache_key or "").strip()
        if not key:
            return None
        ttl_sec = _cfg_float(cfg, "ttl_sec", DEFAULT_TTL_SEC)
        try:
            now_ts = float(now) if now is not None else time.time()
        except Exception:
            now_ts = time.time()

        cached = _cache_get(key, _PEER_CACHE)
        if cached is not None and (now_ts - cached[0]) <= ttl_sec:
            _bump("cache_hit_mem")
            return cached[1]

        phone_txt = ""
        try:
            if str(platform or "").strip().lower() in _PHONE_PLATFORMS:
                phone_txt = str(phone or "").strip()
        except Exception:
            phone_txt = ""
        lang_txt = ""
        try:
            lang_txt = str(language or "").strip()
            if lang_txt.lower() == "unknown":
                lang_txt = ""
        except Exception:
            lang_txt = ""

        clock = resolve_user_clock(
            stated_place=_stated_place(episodic_store, memory_key),
            phone=phone_txt,
            activity_hours=None,   # 见 docstring：prompt 侧不吃统计推断
            language=lang_txt,
            now=datetime.fromtimestamp(now_ts, tz=timezone.utc),
        )
        _cache_put(key, now_ts, clock, _PEER_CACHE)
        _bump("resolved" if clock is not None else "unresolved")
        return clock
    except Exception:
        logger.debug(
            "[user_clock] resolve_peer_locale 失败 key=%s", cache_key, exc_info=True)
        return None


def resolve_schedule_user_clock(
    conversation_id: str = "",
    *,
    now: Optional[float] = None,
    inbox_store: Any = None,
    episodic_store: Any = None,
    memory_key: str = "",
    cfg: Optional[Dict[str, Any]] = None,
    goal_store: Any = None,
    profile: Any = None,
    persona: Any = None,
    last_inbound_ts: float = 0.0,
) -> Tuple[Optional[UserClock], str]:
    """主动触达 / 关怀共用的调度钟：客户核实钟 → 人设钟 → 服务器钟。

    返回 ``(clock, source)``，``source ∈ peer_tz|explicit|persona_tz|server``。
    ``clock`` 仅在非 server 时非空，且 ``trust=replace``（不放宽 08-19 语义）。

    1. Q-29 ``resolve_peer_tz(profile)``（confirmed > mentioned，location >
       residence）命中 → ``peer_tz``；
    2. 现有 ``resolve_for_conversation`` 若 ``trust==replace``（自述城 / 国码 /
       行为佐证）→ ``explicit``；narrow / advisory **不升档**；
    3. 人设 ``resolve_place_with_fallback`` → ``persona_tz``；
    4. 否则 ``(None, "server")``。

    人设钟**不**写入 ``conversation_meta.tz_*``（那是客户钟缓存，串味会把
    人设美东当成客户钟）。本函数绝不 raise。
    """
    try:
        try:
            now_ts = float(now) if now is not None else time.time()
        except Exception:
            now_ts = time.time()
        cid = str(conversation_id or "").strip()

        prof = profile
        if prof is None and cid:
            try:
                from src.companion.peer_time import load_profile
                prof = load_profile(cid, goal_store)
            except Exception:
                prof = None
        if prof:
            try:
                from src.companion.peer_time import resolve_peer_tz
                tz_name = resolve_peer_tz(prof)
            except Exception:
                tz_name = None
            if tz_name:
                clock = clock_from_tz_name(
                    tz_name, source="peer_tz", now=now_ts, trust=TRUST_REPLACE)
                if clock is not None:
                    return clock, "peer_tz"

        if inbox_store is not None and cid:
            try:
                existing = resolve_for_conversation(
                    cid,
                    inbox_store=inbox_store,
                    episodic_store=episodic_store,
                    memory_key=memory_key,
                    cfg=cfg,
                    now=now_ts,
                    last_inbound_ts=last_inbound_ts,
                )
            except Exception:
                existing = None
            if (existing is not None
                    and str(getattr(existing, "trust", "") or "") == TRUST_REPLACE):
                src = str(getattr(existing, "source", "") or "")
                if src in ("peer_tz", "persona_tz"):
                    return existing, src
                if src in _EXPLICIT_SOURCES or src:
                    # 未知 replace 源也按 explicit（已核实），narrow 走不到这里
                    return existing, "explicit"

        if persona is not None:
            try:
                from src.companion.persona_location import resolve_place_with_fallback
                place = resolve_place_with_fallback(persona)
            except Exception:
                place = None
            tz_name = str(getattr(place, "tz_name", "") or "").strip() if place else ""
            if tz_name:
                clock = clock_from_tz_name(
                    tz_name, source="persona_tz", now=now_ts, trust=TRUST_REPLACE)
                if clock is not None:
                    return clock, "persona_tz"
        return None, "server"
    except Exception:
        logger.debug(
            "[user_clock] resolve_schedule_user_clock 失败 cid=%s",
            conversation_id, exc_info=True)
        return None, "server"


def resolve_schedule_clock(
    conversation_id: str = "",
    *,
    now: Optional[float] = None,
    inbox_store: Any = None,
    episodic_store: Any = None,
    memory_key: str = "",
    cfg: Optional[Dict[str, Any]] = None,
    goal_store: Any = None,
    profile: Any = None,
    persona: Any = None,
    last_inbound_ts: float = 0.0,
) -> Tuple[int, str]:
    """``resolve_schedule_user_clock`` 的小时视图：``(hour, source)``。

    ``source ∈ peer_tz|persona_tz|server|explicit``。小时经 ``schedule_clock``
    （replace-only）算出；server 源 = 服务器本地小时。绝不 raise。
    """
    try:
        try:
            now_ts = float(now) if now is not None else time.time()
        except Exception:
            now_ts = time.time()
        clock, source = resolve_schedule_user_clock(
            conversation_id,
            now=now_ts,
            inbox_store=inbox_store,
            episodic_store=episodic_store,
            memory_key=memory_key,
            cfg=cfg,
            goal_store=goal_store,
            profile=profile,
            persona=persona,
            last_inbound_ts=last_inbound_ts,
        )
        hour = int(schedule_clock(clock, now_ts)[0])
        src = source if source in _SCHEDULE_SOURCES else (
            "server" if clock is None else "explicit")
        return hour, src
    except Exception:
        try:
            fallback = int(time.localtime(
                float(now) if now is not None else time.time()).tm_hour)
        except Exception:
            fallback = int(datetime.now().hour)
        return fallback, "server"
