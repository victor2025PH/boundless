"""D1b P0-5：自动推进「有货却 24h 零真发」判据（纯函数）。

进程心跳停走已由 ``_check_scan_loop_stall`` 盯 ticker/dispatcher。
本模块盯另一面：引擎配置说能发、库里有足够老的 auto 目标，但 24h 内
``beat_sent`` 一条都没有——卡上仍可能写着「下一次跟进」，
实际一条都没出去（会话全是人审档 / 排拍后派发吞了 / 代码没装载）。

只减误报：引擎没开、目标太新、已经有真发，一律不算停摆。

M-7（#236，2026-09-07）两处改口径：
- ``SENT_KINDS`` 只剩 ``beat_sent``。此前同时数 ``care_sent``，而 care 真发钩子
  每次都把两种事件各写一条 → 一次真发计 2（skuio 机 14:01 ``sent_24h=4`` 实为
  两次）。goal_link 到期关怀现在也补写 ``beat_sent``（detail=care:link），单一
  kind 覆盖全部主动出手；卡片「已推进 N 拍」与本模块同表同 kind。
- 读事件按 kind 过滤（``list_events(kinds=...)``）——``beat_injected`` /
  ``beat_blocked`` 事件多了以后，40 行窗口不再把真发挤出去。
- 新增单目标粒度 ``goal_stall_verdict``：卡片红字 + 看门狗带 goal_id/会话。

O-3 A（#236，2026-09-08，HM7XBA）：
- 全局判据此前按「建目标起算的年龄」（``oldest_age_sec`` ≥ 4h）→ 自然档目标 14:45
  建、当天设计上就不排主动拍（day 0 让回复链接住、10–20 点窗口），18:42 就被喊
  ``stalled: auto=3 sent_24h=0 oldest_h=4.0``——**结构上还没到第一个可出手时刻**，
  不是「能发没发」。现在快照另附 ``oldest_eligible_sec``＝自「第一个可出手时刻」起
  的时长（自然档＝建目标 +24h；冲刺＝建目标即刻），``stall_verdict`` 优先读它。
- 逐目标 stalled 在本模块直接落一行 **WARNING**（此前只有看门狗 INFO），诊断包
  按级别过滤也能看到。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("src.companion.goals.liveness")

SENT_KINDS = ("beat_sent",)

# 单目标 stalled 判据窗口（秒）：有排期却 24h 零真发
GOAL_STALL_WINDOW_SEC = 86400.0
# 新目标宽限：建目标当天（<24h）不判 stalled——自然档主动拍从第二天起才排
GOAL_STALL_MIN_AGE_SEC = 86400.0


def first_eligible_delay_sec(goal: Any) -> float:
    """建目标后多久才到「第一个可出手时刻」：自然档 ``GOAL_STALL_MIN_AGE_SEC``
    （day 0 不排 + 次日白天窗），冲刺（today/session）即刻。纯函数、绝不抛。"""
    try:
        from src.companion.goals.pace import is_sprint, resolve_pace
        if is_sprint(resolve_pace(goal or {})):
            return 0.0
    except Exception:
        pass
    return GOAL_STALL_MIN_AGE_SEC


def _count_sent_since(store: Any, gid: str, since_ts: float) -> int:
    try:
        evs = store.list_events(gid, limit=300, kinds=SENT_KINDS,
                                since_ts=since_ts) or []
    except TypeError:
        # 旧 store 签名（无 kinds/since_ts）——回落全读再过滤
        try:
            evs = [e for e in (store.list_events(gid, limit=300) or [])
                   if str((e or {}).get("kind") or "") in SENT_KINDS
                   and float((e or {}).get("ts") or 0) >= since_ts]
        except Exception:
            evs = []
    except Exception:
        evs = []
    return len(evs)


def collect_send_liveness(store: Any, *, now: float,
                          lookback_sec: float = 86400.0) -> Dict[str, Any]:
    """活跃 auto 目标盘点 + 近窗真发条数。读失败回空快照，绝不抛。

    M-7：附 ``stalled_goals``——逐目标 ``goal_stall_verdict`` 为 stalled 的
    ``{goal_id, conversation_id, title}`` 清单（看门狗告警点名到目标）。"""
    out: Dict[str, Any] = {
        "active_auto": 0, "sent_24h": 0, "oldest_age_sec": 0.0,
        "oldest_eligible_sec": 0.0, "stalled_goals": [],
    }
    try:
        goals = store.list_goals(status="active", limit=200) or []
    except Exception:
        return out
    sent = 0
    oldest = 0.0
    oldest_eligible = 0.0
    auto_n = 0
    stalled = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if str(g.get("autonomy") or "") != "auto":
            continue
        auto_n += 1
        try:
            born = float(g.get("created_at") or g.get("start_ts") or 0)
        except (TypeError, ValueError):
            born = 0.0
        if born > 0:
            age = now - born
            oldest = max(oldest, age)
            oldest_eligible = max(
                oldest_eligible, age - first_eligible_delay_sec(g))
        gid = str(g.get("goal_id") or "")
        if not gid:
            continue
        n_sent = _count_sent_since(store, gid, now - lookback_sec)
        sent += n_sent
        if goal_stall_verdict(g, sent_in_window=n_sent, now=now) == "stalled":
            conv = str(g.get("conversation_id") or "")
            stalled.append({
                "goal_id": gid,
                "conversation_id": conv,
                "title": str(g.get("title") or "")[:40],
            })
            # O-3 A（#236）：升 WARNING——「有排期却 24h 零真发」是要人看的事故，
            # 不该埋在 INFO 里等人翻。看门狗那条 INFO + 事件总线照旧。
            logger.warning(
                "[goal-liveness] stalled goal=%s conv=%s title=%r age_h=%.1f "
                "sent_24h=0 ——auto 档、建 ≥24h、期限未到，仍一拍未发；卡片已标红",
                gid[:12], conv, str(g.get("title") or "")[:30],
                (now - born) / 3600.0 if born > 0 else -1.0)
    out["active_auto"] = auto_n
    out["sent_24h"] = sent
    out["oldest_age_sec"] = oldest
    out["oldest_eligible_sec"] = max(0.0, oldest_eligible)
    out["stalled_goals"] = stalled
    return out


def stall_verdict(
    engine: Any,
    snap: Any,
    *,
    min_active: int = 2,
    min_age_sec: float = 4 * 3600.0,
) -> Optional[str]:
    """``sprint_effective`` + 够老的 auto 目标 + 近窗零真发 → ``\"stalled\"``。

    O-3 A：「够老」按 ``oldest_eligible_sec``（自第一个可出手时刻起）判；旧快照
    没有该键时回落 ``oldest_age_sec``。自然档建目标当天不再触发全局告警。"""
    if not isinstance(engine, dict) or not engine.get("sprint_effective"):
        return None
    if not isinstance(snap, dict):
        return None
    try:
        if int(snap.get("active_auto") or 0) < int(min_active):
            return None
        age_key = ("oldest_eligible_sec" if "oldest_eligible_sec" in snap
                   else "oldest_age_sec")
        if float(snap.get(age_key) or 0) < float(min_age_sec):
            return None
        if int(snap.get("sent_24h") or 0) > 0:
            return None
    except (TypeError, ValueError):
        return None
    return "stalled"


def goal_stall_verdict(
    goal: Any,
    *,
    sent_in_window: int,
    now: Optional[float] = None,
    window_sec: float = GOAL_STALL_WINDOW_SEC,
    min_age_sec: float = GOAL_STALL_MIN_AGE_SEC,
) -> Optional[str]:
    """单目标粒度（M-7 D #236）：auto 档、活跃、建了 ≥24h、期限未到、近 24h 零
    ``beat_sent`` → ``"stalled"``；否则 None。纯函数、绝不抛。

    与全局 ``stall_verdict`` 的区别：那边要 ≥2 个 auto 目标才敢判（防单目标误报
    拉响全局告警）；这边是给**这一张卡**用的——BABY BEAR 一个目标 4 天 0 拍，
    全局判据永远够不着它。引擎配置层的闸（sprint 关 / 平台不在白名单）由调用方
    另判，这里只看「该出手却没出手」。"""
    if not isinstance(goal, dict):
        return None
    if str(goal.get("autonomy") or "") != "auto":
        return None
    if str(goal.get("status") or "active") != "active":
        return None
    n = float(now if now is not None else time.time())
    try:
        born = float(goal.get("created_at") or goal.get("start_ts") or 0)
    except (TypeError, ValueError):
        born = 0.0
    if born <= 0 or (n - born) < float(min_age_sec):
        return None
    try:
        deadline = float(goal.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        deadline = 0.0
    if deadline > 0 and n >= deadline:
        return None
    try:
        if int(sent_in_window or 0) > 0:
            return None
    except (TypeError, ValueError):
        return None
    return "stalled"


__all__ = [
    "GOAL_STALL_MIN_AGE_SEC",
    "GOAL_STALL_WINDOW_SEC",
    "SENT_KINDS",
    "collect_send_liveness",
    "first_eligible_delay_sec",
    "goal_stall_verdict",
    "stall_verdict",
]
