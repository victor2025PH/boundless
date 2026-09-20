"""P7 2026-08-03：主动关怀「AI 运行方案」确定性核心（纯函数）。

页面改版（机房面板 → AI 关怀管家）的后端聚合层：把 store / dispatcher / metrics /
shadow 各处已有的读数聚成**一份运行方案**——待办按日分组、引擎灯态、结构化建议
（「样本已达标可开真发」「LLM 比正则多识别 N 条」……），供 ``GET /api/care/plan``
一次性喂给前端。

设计纪律（与 capability_advisor / proactive_pacing 同族）：
- **全部纯函数、零 I/O、零 LLM**：LLM 解说层（管家一句话）属下一阶段，挂在本核心
  产出的结构化方案之上；LLM 挂了方案照出——fail-open 的根基是这里本来就不依赖它。
- **响应只出结构化码**（advice ``code`` + 数字参数），措辞由前端 i18n 渲染——
  与 /api/care/health 同一 i18n 收口纪律（CJK 棘轮账本）。
- **建议必须与行为一致**：go_live_ready 的阈值即运营该做的判断本身，不另算一套
  （与草稿预判徽标同一设计纪律：预判 ≠ 行为 比没有预判更糟）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_DAY = 86400.0

# 分组顺序即渲染顺序（前端 i18n：overdue→cs2_group_due / tomorrow→cs2_group_tmr …）
GROUP_ORDER = ("overdue", "today", "tomorrow", "week", "later")

# 建议优先级（越靠前越先出；build_advice 产出后按此排序再截断）
_ADVICE_PRIORITY = (
    "go_live_ready",        # 样本达标 → 建议开真发（最高价值动作）
    "review_more",          # 有拟稿但审得不够 → 引导点 👍👎
    "waiting_due",          # 已排卡、还没到期 → 告知等待语义
    "low_reply_rate",       # 真发回复率偏低 → 引导复盘话术
    "capture_quiet",        # 长时间没捕获 → 告知「AI 在听」+ 手动补卡出路
    "llm_capture_candidate",  # 影子对照显示 LLM 有增益 → 指路开 LLM 捕获
)

# go_live 建议阈值：审 ≥8 条且满意率 ≥85% 才劝开真发（宁保守——建议错一次，
# 运营就再也不信这个管家了）。
GO_LIVE_MIN_REVIEWED = 8
GO_LIVE_MIN_LIKE_RATE = 85.0
# 影子对照 llm_only 累计 ≥5 条才提「开 LLM 捕获」（样本太少提了也没法复核）
LLM_CANDIDATE_MIN_ONLY = 5
# 真发回复率告警线：成熟样本 ≥5 且回复率 <20% 才点名（小样本不惊扰）
LOW_REPLY_MIN_MATURED = 5
LOW_REPLY_RATE_FLOOR = 0.2
# 捕获静默判定：最近捕获距今超过 48h（或从未捕获）且当前无待办
CAPTURE_QUIET_AFTER_SEC = 48 * 3600.0


def bucket_of(due_at: float, now: float) -> str:
    """一条待办落在哪个时间桶（本地日历口径，与旧前端 _csBucket 同语义）。"""
    d = float(due_at or 0)
    n = float(now)
    if d <= n:
        return "overdue"
    try:
        dd = datetime.fromtimestamp(d).date()
        nd = datetime.fromtimestamp(n).date()
    except Exception:
        return "later"
    if dd == nd:
        return "today"
    if dd == nd + timedelta(days=1):
        return "tomorrow"
    if d - n < 7 * _DAY:
        return "week"
    return "later"


def group_plan_items(items: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """pending 待办 → 按时间桶分组（组内保持传入顺序=due_at 升序；空组不出）。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in GROUP_ORDER}
    for it in items or []:
        buckets[bucket_of(float(it.get("due_at") or 0), now)].append(it)
    return [{"key": k, "items": buckets[k]} for k in GROUP_ORDER if buckets[k]]


def build_lights(
    *,
    enabled: bool,
    dry_run: bool,
    capture_wired: bool,
    capture_config_on: bool,
    dispatch_running: bool,
    dispatch_skip: str = "",
    multiplatform_deferred: bool = False,
    messenger_rpa: bool = False,
    delivery_running: Optional[bool] = None,
) -> Dict[str, str]:
    """四灯状态码（engine/capture/dispatch/delivery），前端 i18n 渲染文案。

    与旧前端 csHealth 的判定逐条同语义——单一事实源移到后端，方案接口和
    引擎室灯条读同一份，不再各算一套。

    ``delivery_running``（N-1 D #243）：多平台 deferred 队列的 drain loop 是否真在跑。
    开关开着而 loop 没起（skuio 09-07 16:34–16:53 的状态）→ ``delivery=broken``——
    此前这灯只看配置开关，队列里没人出货它照样亮绿。None＝后端不知道（旧实例）→ 旧口径。
    """
    capture = ("ok" if capture_config_on else "standby") if capture_wired else "broken"
    if dispatch_running:
        dispatch = "ok" if enabled else "standby"
    else:
        dispatch = "ai_missing" if dispatch_skip == "ai_missing" else "broken"
    if enabled and dry_run:
        delivery = "dry"
    elif multiplatform_deferred and delivery_running is False:
        delivery = "broken"
    elif multiplatform_deferred or messenger_rpa:
        delivery = "ready"
    else:
        delivery = "off"
    return {
        "engine": "on" if enabled else "off",
        "capture": capture,
        "dispatch": dispatch,
        "delivery": delivery,
    }


def engine_overall(lights: Dict[str, str], *, enabled: bool, dry_run: bool) -> str:
    """整体一枚 chip 的状态：off / ok / warn。

    warn 的口径＝「开着但链路有断点」：捕获断、派发断、或真发模式下投递通道未开。
    试运行不要求投递通道（只拟稿），delivery=dry/off 都不算断。
    """
    if not enabled:
        return "off"
    if lights.get("capture") == "broken":
        return "warn"
    if lights.get("dispatch") in ("broken", "ai_missing"):
        return "warn"
    if not dry_run and lights.get("delivery") == "off":
        return "warn"
    return "ok"


def build_advice(
    *,
    enabled: bool,
    dry_run: bool,
    reviewed: int = 0,
    like_rate_pct: Optional[float] = None,
    samples_7d: int = 0,
    pending_total: int = 0,
    captured_24h: int = 0,
    last_captured_ts: float = 0.0,
    shadow_llm_only: int = 0,
    llm_capture_enabled: bool = False,
    effect_rate: Optional[float] = None,
    effect_matured: int = 0,
    effect_replied: int = 0,
    now: float = 0.0,
    max_items: int = 2,
) -> List[Dict[str, Any]]:
    """结构化运营建议（[{code, ...params}]，按优先级排序、最多 max_items 条）。

    只出**可执行**的建议；未开启引擎不出建议（hero 空态自己会引导开启）。
    """
    if not enabled:
        return []
    out: List[Dict[str, Any]] = []

    if dry_run:
        rate = float(like_rate_pct) if like_rate_pct is not None else None
        if (reviewed >= GO_LIVE_MIN_REVIEWED and rate is not None
                and rate >= GO_LIVE_MIN_LIKE_RATE):
            out.append({"code": "go_live_ready", "reviewed": int(reviewed),
                        "like_rate_pct": round(rate, 1)})
        elif samples_7d > 0 and reviewed < GO_LIVE_MIN_REVIEWED:
            out.append({"code": "review_more", "reviewed": int(reviewed),
                        "need": GO_LIVE_MIN_REVIEWED})
        elif samples_7d == 0 and pending_total > 0:
            out.append({"code": "waiting_due", "pending": int(pending_total)})
    else:
        if (effect_rate is not None and effect_matured >= LOW_REPLY_MIN_MATURED
                and float(effect_rate) < LOW_REPLY_RATE_FLOOR):
            out.append({"code": "low_reply_rate",
                        "rate_pct": round(float(effect_rate) * 100, 1),
                        "replied": int(effect_replied),
                        "matured": int(effect_matured)})

    quiet = (captured_24h <= 0 and pending_total <= 0
             and (last_captured_ts <= 0
                  or (float(now) - float(last_captured_ts)) > CAPTURE_QUIET_AFTER_SEC))
    if quiet:
        out.append({"code": "capture_quiet"})

    if shadow_llm_only >= LLM_CANDIDATE_MIN_ONLY and not llm_capture_enabled:
        out.append({"code": "llm_capture_candidate", "llm_only": int(shadow_llm_only)})

    out.sort(key=lambda a: _ADVICE_PRIORITY.index(a["code"])
             if a["code"] in _ADVICE_PRIORITY else 99)
    return out[:max(1, int(max_items))]


__all__ = [
    "GROUP_ORDER", "GO_LIVE_MIN_REVIEWED", "GO_LIVE_MIN_LIKE_RATE",
    "LLM_CANDIDATE_MIN_ONLY", "bucket_of", "group_plan_items",
    "build_lights", "engine_overall", "build_advice",
]
