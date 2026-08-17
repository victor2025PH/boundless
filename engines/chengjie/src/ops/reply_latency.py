# -*- coding: utf-8 -*-
"""回复时延 SLO 聚合（P1-8，2026-08-09）——「客户等了多久才被回」的持久库口径。

背景：本日修完「被吞回复等 11 分钟」后，回复时延成为已被证明会静默劣化的
核心体验指标，但它此前不在任何看板上（A 线的「响应时间统计」只是散日志，
且只覆盖它自己发出的那条）。本模块从 inbox 持久库直接推导：

- **episode（等待段）**＝会话内一段连续入站（burst）到下一条出站之间的等待；
  时延 = 出站 ts − 该 burst **首条**入站 ts（客户视角的最坏等待）。
- 归因窗口按 episode 的首条入站时间落窗；重启不清零（与 value_report 同哲学：
  进程内计数器刻意不用）。
- **零回复**＝burst 首条入站已超过宽限期（默认 10 分钟）仍无任何出站回应——
  这正是本日事故的直接可观测面（修复前 600s 兜底，修复后应 <90s 或正常回复）。

口径纪律（改动前先读）：
- 只统计**私聊**（chat_type IN ('private','')，与 store 联系人口径同注释来源）；
  群聊回不回本就不承诺 SLO。
- 剔 bot 会话（peer_is_bot=1 / telegram chat_type='bot' / username LIKE '%bot'，
  与 store._NOT_BOT_PEER_SQL 同一判定要素——那个片段钉死外层别名 o 无法直用，
  此处以会话集合预筛实现同语义）；剔 Telegram 系统会话（Saved Messages/服务号，
  给自己发消息不构成客户等待）。
- 出站不区分「AI 发的还是人发的」——SLO 是客户体验口径，谁回的都算回了；
  「AI 占多大比例」由 value_report/autosend-status 另行回答，刻意不在此重复。
- 时延只统计**已回复**的 episode（未回复的进 unanswered 计数，不混进分位数
  拉爆 p95）；分位数用线性插值（样本 <2 时 p50=p95=该样本）。

访问模式：与 value_report 同惯例——经 store 的 ``_lock``/``_conn`` 只读查询，
任何异常返回 {}（软失败绝不拖垮 metrics 端点）；消费方走
``reply_latency_snapshot``（进程级 TTL 缓存，默认 300s——metrics 被 ops 页
周期轮询，逐次全量扫消息表没有必要）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 零回复宽限期：入站不足这个时长未回不算「零回复」（可能还在生成/人在打字）。
GRACE_UNANSWERED_S = 600.0
# 时延分桶（秒）上界；最后追加一个 ≥ 最末上界的溢出桶。选点对齐产品语感：
# 秒回 / 半分钟 / 一分钟 / 五分钟 / 半小时 / 更久。
BUCKET_EDGES_S = (10.0, 30.0, 60.0, 300.0, 1800.0)

_SNAPSHOT_TTL_S = 300.0
_snapshot_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def _percentile(sorted_vals: List[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]）；空表返回 0。零依赖，与 proactive 侧同法。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo_i = int(pos)
    hi_i = min(lo_i + 1, len(sorted_vals) - 1)
    frac = pos - lo_i
    return float(sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac)


def build_episodes(
    rows: Iterable[Tuple[str, float]],
) -> List[Tuple[float, Optional[float]]]:
    """把单会话按 ts 升序的 (direction, ts) 折成等待段列表。

    返回 [(burst 首条入站 ts, 回复出站 ts | None)]；末尾未回复的 burst 以
    None 结尾。纯函数，供测试单独钉语义：
    - 连续多条入站折进同一段（首条时间为准）；
    - 出站关闭当前段；无在途入站的出站（我方主动开口）不产生段。
    """
    episodes: List[Tuple[float, Optional[float]]] = []
    first_in: Optional[float] = None
    for direction, ts in rows:
        if direction == "in":
            if first_in is None:
                first_in = float(ts)
        elif direction == "out":
            if first_in is not None:
                episodes.append((first_in, float(ts)))
                first_in = None
    if first_in is not None:
        episodes.append((first_in, None))
    return episodes


def _window_stats(
    episodes: List[Tuple[float, Optional[float], str]],
    lo: float, hi: float, now: float,
) -> Dict[str, Any]:
    """episodes=(first_in_ts, reply_ts|None, platform)；按 first_in 落窗聚合。"""
    lat: List[float] = []
    unanswered = 0
    pending = 0
    by_platform: Dict[str, Dict[str, Any]] = {}
    buckets = [0] * (len(BUCKET_EDGES_S) + 1)
    for first_in, reply_ts, plat in episodes:
        if not (lo <= first_in < hi):
            continue
        p = by_platform.setdefault(
            plat or "?", {"episodes": 0, "unanswered": 0, "_lat": []})
        p["episodes"] += 1
        if reply_ts is None:
            if now - first_in >= GRACE_UNANSWERED_S:
                unanswered += 1
                p["unanswered"] += 1
            else:
                pending += 1
            continue
        v = max(0.0, float(reply_ts) - float(first_in))
        lat.append(v)
        p["_lat"].append(v)
        for i, edge in enumerate(BUCKET_EDGES_S):
            if v < edge:
                buckets[i] += 1
                break
        else:
            buckets[-1] += 1
    lat.sort()
    for p in by_platform.values():
        pl = sorted(p.pop("_lat"))
        p["replied"] = len(pl)
        p["p50_s"] = round(_percentile(pl, 0.50), 1)
        p["p95_s"] = round(_percentile(pl, 0.95), 1)
    return {
        "episodes": len(lat) + unanswered + pending,
        "replied": len(lat),
        "unanswered": unanswered,
        "pending_grace": pending,
        "p50_s": round(_percentile(lat, 0.50), 1),
        "p90_s": round(_percentile(lat, 0.90), 1),
        "p95_s": round(_percentile(lat, 0.95), 1),
        "avg_s": round(sum(lat) / len(lat), 1) if lat else 0.0,
        "max_s": round(lat[-1], 1) if lat else 0.0,
        "buckets": buckets,
        "by_platform": by_platform,
    }


def _eligible_conversations(conn) -> Dict[str, str]:
    """SLO 适用会话集合：私聊、非 bot、非系统会话 → {conversation_id: platform}。"""
    try:
        from src.inbox.store import TELEGRAM_SERVICE_CHAT_KEYS
        svc = set(str(k) for k in TELEGRAM_SERVICE_CHAT_KEYS)
    except Exception:
        svc = set()
    out: Dict[str, str] = {}
    rows = conn.execute(
        "SELECT conversation_id, platform, chat_key, chat_type, account_id,"
        "       COALESCE(peer_is_bot, 0) AS pib, lower(COALESCE(username,'')) AS uname"
        " FROM conversations"
        " WHERE COALESCE(chat_type,'') IN ('private','')").fetchall()
    for r in rows:
        pib = int(r["pib"] or 0)
        if pib == 1:
            continue
        plat = str(r["platform"] or "")
        uname = str(r["uname"] or "")
        # 与 _NOT_BOT_PEER_SQL 同要素：TG 且未显式豁免(-1) 时按 username 后缀判 bot
        if pib != -1 and plat.lower() == "telegram" and uname.endswith("bot"):
            continue
        chat_key = str(r["chat_key"] or "")
        if not chat_key:
            continue
        if plat.lower() == "telegram" and (
                chat_key in svc or chat_key == str(r["account_id"] or "")):
            continue  # Saved Messages / 服务号：自话不构成客户等待
        out[str(r["conversation_id"])] = plat
    return out


def build_reply_latency(store: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """三窗聚合：近 24h（主 SLO）/ 前一个 24h（环比）/ 近 7 天。异常返回 {}。"""
    try:
        t = float(now if now is not None else time.time())
        d1_lo, d7_lo = t - 86400.0, t - 7 * 86400.0
        # 取数下限再前推一个窗：让窗口边缘的 burst 能归到真实的首条入站
        fetch_lo = d7_lo - 86400.0
        with store._lock:  # noqa: SLF001 —— 与 value_report 读 store._conn 同惯例
            conn = store._conn
            eligible = _eligible_conversations(conn)
            msg_rows = conn.execute(
                "SELECT conversation_id, direction, ts FROM messages"
                " WHERE ts >= ? AND ts < ? ORDER BY conversation_id, ts",
                (fetch_lo, t)).fetchall()
        per_conv: Dict[str, List[Tuple[str, float]]] = {}
        for r in msg_rows:
            cid = str(r["conversation_id"])
            plat = eligible.get(cid)
            if plat is None:
                continue
            per_conv.setdefault(cid, []).append(
                (str(r["direction"]), float(r["ts"])))
        episodes: List[Tuple[float, Optional[float], str]] = []
        for cid, rows in per_conv.items():
            plat = eligible.get(cid) or "?"
            for first_in, reply_ts in build_episodes(rows):
                episodes.append((first_in, reply_ts, plat))
        return {
            "generated_at": t,
            "grace_s": GRACE_UNANSWERED_S,
            "bucket_edges_s": list(BUCKET_EDGES_S),
            "d1": _window_stats(episodes, d1_lo, t, t),
            "d1_prev": _window_stats(episodes, d1_lo - 86400.0, d1_lo, t),
            "d7": _window_stats(episodes, d7_lo, t, t),
        }
    except Exception:
        return {}


def reply_latency_snapshot(store: Any, *, ttl_s: float = _SNAPSHOT_TTL_S,
                           now: Optional[float] = None) -> Dict[str, Any]:
    """TTL 缓存版（metrics 端点消费口）：ops 页轮询不至于每 30s 全扫消息表。"""
    t = float(now if now is not None else time.time())
    cached = _snapshot_cache.get("data")
    if cached is not None and (t - float(_snapshot_cache.get("ts", 0))) < ttl_s:
        return cached
    data = build_reply_latency(store, now=now)
    if data:
        _snapshot_cache["ts"] = t
        _snapshot_cache["data"] = data
    return data


def reset_snapshot_cache() -> None:
    """测试钩子：清 TTL 缓存（生产无调用面）。"""
    _snapshot_cache["ts"] = 0.0
    _snapshot_cache["data"] = None
