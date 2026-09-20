"""每日「拍」规划器（纯函数，零 IO）。

职责：给定目标 + 信号 + 历史消耗计数，决定**这一槽**该怎么推进：

- ``hold``（不注入推进块）：强负面情绪 / 连发多拍对方一直没回（沉默熔断）。
- ``push_level=none``（退避陪伴日）：连发未回达退避阈值 → 今日意图换成纯陪伴。
- 正常拍：从模板意图池确定性取「今日意图」+ 里程碑力度曲线。

``day`` 参数是槽位键，由调用方经 ``pace.slot_key`` 算好再传入
（natural=日历日 / today=小时 / session=入站回合）。本模块**不得** import
``pace``——``pace.slot_key`` 的 natural 分支回落 ``day_key``，循环导入。
退避/熔断阈值由调用方经 ``pace.planner_thresholds`` 传入；本函数对
``natural`` 缺省（backoff=2 / halt=4）一字不变。

信号自适应对齐 AI SDR 「adaptive cadence」思想，但所有判定确定性可单测；
情绪红线沿用既有 ``proactive_emotion_gate`` 的保守精神（宁静默不冒犯）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.companion.goals.signals import GoalSignals
from src.companion.goals.templates import (
    PROBE_CLAUSE_SEP,
    pick_care_intent,
    pick_intent,
    push_for_milestone,
)

# 强负面情绪判定线（与 proactive_emotion_gate 的 min_negative_intensity 同刻度）
NEGATIVE_INTENSITY_HOLD = 0.5

# O-3 B（#236）：客户活跃判据——30 分钟内 ≥5 条入站（HM7XBA 00:11–00:43 九条）。
# 活跃时摸底目标的计划自适应提前：第 1 天的「先暖场」直接换成「顺着话头带出想
# 了解的那件事」（里程碑 1 的意图池），不再等日历走到第 3 天才开口问。
ACTIVE_WINDOW_SEC = 1800.0
ACTIVE_INBOUND_MIN = 5


def customer_active(inbound_recent: int, *, threshold: int = ACTIVE_INBOUND_MIN) -> bool:
    """近 30 分钟入站条数 ≥ 阈值 → 客户活跃。纯函数。"""
    try:
        return int(inbound_recent or 0) >= int(threshold)
    except (TypeError, ValueError):
        return False


def with_probe(intent: str, ask: str) -> str:
    """把「今天至少自然问一个：<未填槽问法>」钉进今日意图（O-3 B）。

    摸底类目标此前第 1 天意图池是「先暖场、不急着问」，缺口只在注入时软合流——
    3 天目标 1/3 时间零摸底。现在计划层就写死：每天 ≥1 个摸底问句，第 1 天也要。
    ask 已在意图里 → 原样返回（不重复）；ask 空 → 原样返回。"""
    it = str(intent or "").strip()
    a = str(ask or "").strip()
    if not a or a in it:
        return it
    return f"{it}{PROBE_CLAUSE_SEP}{a}" if it else f"今天至少自然问一个：{a}"


def day_key(now: Optional[float] = None) -> str:
    """本地日键 YYYY-MM-DD（拍的幂等键；与运营口径一致用本地时区）。"""
    t = time.localtime(now if now is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def effective_rejects(rejected: int, undone: int = 0) -> int:
    """近窗**有效**驳回数 ＝ 驳回数 − 撤销数（下限 0）。纯函数。

    坐席「撤销驳回」必须真正撤销退避：只把今日拍改回 planned 而不补偿这里的
    计数，明天照旧被降档 —— 那样的撤销是骗人的。事件台账只增不删（审计要留痕），
    所以补偿走「同窗口内 ``beat_reject_undone`` 事件数相减」这条路。
    """
    return max(0, int(rejected or 0) - int(undone or 0))


def plan_beat(
    *,
    template: Dict[str, Any],
    goal: Dict[str, Any],
    signals: GoalSignals,
    day: str,
    engaged_since_inbound: int = 0,
    backoff_after: int = 2,
    halt_after: int = 4,
    recent_rejects: int = 0,
    probe_asks: Optional[List[str]] = None,
    active: bool = False,
) -> Optional[Dict[str, Any]]:
    """规划今天的拍。返回 ``{intent, push_level}``；hold 时返回
    ``{"hold": reason}``（调用方不建拍、不注入推进块）。

    Args:
        engaged_since_inbound: 对方最近一次开口之后，我们已带目标进入生成/发出的拍数
            （store.count_engaged_since 提供）。对方一直不回还连着推 = 骚扰，熔断。
        backoff_after: 达到该数 → 退避陪伴日（push none，只陪伴不推进）。
        halt_after: 达到该数 → 完全 hold（连陪伴式的目标块也不注入，彻底让路）。
        recent_rejects: 坐席近窗驳回今日拍的**有效**次数（调用方经
            ``effective_rejects(驳回数, 撤销数)`` 算好再传——撤销过的不算）。
            人审说「推得不对」是最强的负反馈信号：
            1 次 → 力度封顶 soft（direct 降档）；≥2 次 → 退避陪伴日。
            回流只降不升——坐席采纳不加码，防正反馈螺旋。
        probe_asks（O-3 B #236）: 摸底类目标（模板 ``gap_in_intent``）当前**未填**勾选
            槽位的问法，按优先级排（调用方经 profile_slots 算好）。非空 → 今日意图
            钉进「今天至少自然问一个：<第一个>」并回 ``probes=1 / probe_slots``；
            第 1 天也钉（退避陪伴日 / hold 除外——安全语义优先于摸底 KPI）。
        active（O-3 B）: 客户活跃（30min ≥5 条入站）。摸底目标里程碑 0 的「先暖场」
            意图池提前换成里程碑 1 的「顺着话头带出想了解的那件事」——客户正聊得
            热乎就是问的最好时机，不该按日历等到第 3 天。
    """
    # 1) 情绪红线：强负面 → 彻底放下目标（危机场景另有 crisis safety net 兜底）
    if signals.negative_emotion and (
        signals.emotion_intensity < 0
        or signals.emotion_intensity >= NEGATIVE_INTENSITY_HOLD
    ):
        return {"hold": "emotion"}

    # 2) 沉默熔断：对方一直没回还连着带目标说话 → 停
    engaged = max(0, int(engaged_since_inbound or 0))
    if halt_after > 0 and engaged >= int(halt_after):
        return {"hold": "silent"}

    gid = str(goal.get("goal_id") or "")
    mi = int(goal.get("milestone_idx") or 0)
    rejects = max(0, int(recent_rejects or 0))

    # 摸底类模板（gap_in_intent）才带 probes / probe_slots / advanced 三个观测键；
    # 其余模板返回形状逐字不变（{intent, push_level}）。
    discovery = bool(template.get("gap_in_intent"))

    # 3) 退避陪伴日：连发未回 或 坐席连续驳回 → 推进降为纯陪伴
    if (backoff_after > 0 and engaged >= int(backoff_after)) or rejects >= 2:
        care: Dict[str, Any] = {"intent": pick_care_intent(gid, day), "push_level": "none"}
        if discovery:
            care.update(probes=0, probe_slots=[], advanced=False)
        return care

    # 4) 正常拍：模板意图池确定性轮换 + 里程碑力度
    asks = [str(a).strip() for a in (probe_asks or []) if str(a or "").strip()]
    # O-3 B：客户活跃 → 摸底目标暖场段（里程碑 0）提前用「自然带出」段（里程碑 1）的意图
    mi_intent = mi
    if discovery and active and mi == 0 and asks:
        mi_intent = 1
    intent = pick_intent(template, mi_intent, gid, day, params=goal.get("params") or {})
    if not intent:
        return {"hold": "no_intent"}
    push = push_for_milestone(template, mi)
    if rejects >= 1 and push == "direct":
        push = "soft"          # 坐席驳回过 → 力度封顶 soft
    out: Dict[str, Any] = {"intent": intent, "push_level": push}
    if discovery:
        out.update(probes=0, probe_slots=[], advanced=mi_intent != mi)
        if asks:
            out["intent"] = with_probe(intent, asks[0])
            out["probes"] = 1
            out["probe_slots"] = asks[:1]
    return out


# ── Q-8 B（#264 #263）：关系阶段自带推进计划——无手建目标的会话也有「今日主线」 ──────────
# 阶段来自 intimacy_engine 既有判定（0-25 stranger / 25-55 friend / 55-80 close / 80+ soulmate，
# 与 relationship_stager._intim_band 同刻度）；意图池复用 relationship_stage 模板四段弧线
# （engage / warm / trust / ready 恰与四档一一对应），不建目标行、不落库，纯函数确定性轮换。
STAGE_BANDS = ("stranger", "friend", "close", "soulmate")
STAGE_LABELS = {
    "stranger": ("陌生", "New"),
    "friend": ("朋友", "Friend"),
    "close": ("亲近", "Close"),
    "soulmate": ("知心", "Soulmate"),
}
#: 客户连续 N 轮无新信息 → 今日主线升 must（Q-8 C）
NO_NEW_INFO_MUST_STREAK = 2


def intimacy_stage(score: Any) -> str:
    """亲密度分 → 阶段档；未知 / 非法 → stranger（新客最保守）。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "stranger"
    if s < 0:
        return "stranger"
    if s >= 80:
        return "soulmate"
    if s >= 55:
        return "close"
    if s >= 25:
        return "friend"
    return "stranger"


def stage_label(stage: str, lang: str = "zh") -> str:
    pair = STAGE_LABELS.get(str(stage or "").lower()) or STAGE_LABELS["stranger"]
    return pair[1] if str(lang or "").lower().startswith("en") else pair[0]


def plan_stage_beat(
    *, stage: str, conversation_id: str, day: str, no_new_info_streak: int = 0,
    missed_x2: bool = False,
) -> Dict[str, Any]:
    """阶段计划的「今日主线」：``{stage, intent, intent_en, level}``。

    ``level``：soft（默认）/ must（客户连续 ≥2 轮无新信息、或 Q-1 missed×2）——must 时注入链
    把主线写成硬约束（【本轮必做】），仍受「已知即不问」与每槽每日上限约束（那是槽位层的事）。
    纯函数：同会话同日恒定、跨日轮换。"""
    from src.companion.goals.templates import TEMPLATES, _format_intent, _intent_en, _intent_zh
    st = str(stage or "").lower()
    if st not in STAGE_BANDS:
        st = "stranger"
    idx = STAGE_BANDS.index(st)
    pool = ((TEMPLATES.get("relationship_stage") or {}).get("intents") or {}).get(idx) or ()
    intent = intent_en = ""
    if pool:
        import zlib
        h = zlib.crc32(f"stage:{conversation_id}:{day}".encode("utf-8", "ignore"))
        entry = pool[h % len(pool)]
        intent = _format_intent(_intent_zh(entry), {})
        intent_en = _format_intent(_intent_en(entry), {}, en=True)
    level = "must" if (int(no_new_info_streak or 0) >= NO_NEW_INFO_MUST_STREAK or missed_x2) else "soft"
    return {"stage": st, "intent": intent, "intent_en": intent_en, "level": level}


__all__ = [
    "ACTIVE_INBOUND_MIN",
    "NO_NEW_INFO_MUST_STREAK",
    "STAGE_BANDS",
    "STAGE_LABELS",
    "intimacy_stage",
    "plan_stage_beat",
    "stage_label",
    "ACTIVE_WINDOW_SEC",
    "NEGATIVE_INTENSITY_HOLD",
    "customer_active",
    "day_key",
    "effective_rejects",
    "plan_beat",
    "with_probe",
]
