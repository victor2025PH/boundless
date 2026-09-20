# -*- coding: utf-8 -*-
"""案例中心纯函数核心（/cases 页 2026-08-03 改造，P0+P1）。

「案例」= AI 判定**需要人工跟进**的会话，状态寄存在 ContextStore 会话上下文的
``_case_*`` 键族里（随 ContextStore 持久化到 SQLite），由 /cases 页展示、备注、结案。

本模块收拢案例的全部生命周期语义，供两侧消费：
- ``skill_manager``：各信号源开案（:func:`open_case`）+ 保守入站检测器
  （:func:`detect_human_request` / :func:`detect_ai_doubt`）；
- ``cases_routes`` / ``monitoring_routes``：列表行构建（:func:`case_row`）、
  缓存+SQLite 合并（:func:`collect_case_rows`）、结案（:func:`close_case`）。

设计不变量（改动前先读）：
- **同会话至多一个活跃案例**：活跃期再来信号只累计 ``_case_signal_count``；
  更高严重度信号把案例**升级**（换 source/reason，**不换案号**——案号是运营
  跟进的锚点），严重度只升不降。
- **结案后可复发**：结案后新信号 → 旧案归档进 ``_case_history``（截尾 5 条）
  另立新案。修旧缺陷「结案不清案号 → 同会话永远开不出第二张案子」。
- **案例跟人不跟话题**：话题切换不销案（skill_manager 旧行为已移除）。
- 检测器**宁可漏报不误报**（与 persona_guard / media_complaint 同哲学）：
  词表全部要求明确指向「我方服务/对话对象」，第三人称叙事不命中。
- reason 落库存 **i18n 键 + 参数**（``_case_reason_code`` / ``_case_reason_params``），
  文案在 API 层按请求语言渲染——上下文里绝不写死单语文案。
- 观测/告警是 open/close 内的 **best-effort 旁路**（case_stats 计数 +
  severity≥2 开案/升级发 EventBus ``case_alert``，工作台铃铛立即可见、
  webhook 别名 ``cases`` 可外发）——任何异常吞掉，绝不影响业务状态变换。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# 严重度：3=紧急（危机）、2=重要（明确的人工诉求/穿帮/升级）、1=关注（链模式）。
CASE_SEVERITY: Dict[str, int] = {
    "crisis": 3,
    "human_request": 2,
    "ai_doubt": 2,
    "media_complaint": 2,
    "escalation": 2,
    "intent_chain": 1,
}

# 演练/测试保留号段（scripts/duel_nightly.ps1 ``$KeyBase=990001000``，注释明言
# "reserved test range; never real chats"）。夜跑对练走真 process_message 链，
# 质疑台词会触发真实立案（2026-08-09 生产实锤：/cases 页 4 张「媒体质疑」全是
# 演练残影，且对练会话不进收件箱 → 「打开会话」注定落空）。原则与「测试绝不能
# 写生产 config」同族：**测试数据不得污染生产跟进面**。处理方式＝开案照常
# （演练要能覆盖案例机制本身）但打 ``_case_drill`` 标记 + 观测/告警全静音，
# 消费侧（collect_case_rows）默认排除、案例页经 include_drill 显式可见。
DRILL_UID_RANGES: Tuple[Tuple[int, int], ...] = ((990_001_000, 990_001_999),)

# 每案例信号事件时间线上限（{ts, code, quote, mid?}；回答「这 N 次质疑分别是什么
# 时候、说了什么、对应哪条入站」——此前只有 _case_signal_count 一个裸计数）。
# mid＝平台消息 id（Telegram message.id / WA wamid…），深链 ``&mid=`` 直达气泡；
# 缺 mid 的旧事件仍靠 quote 子串匹配（best-effort 回落）。
_EVENTS_CAP = 10

# 演练案例自动善后：同一演练会话距上次信号超过该窗仍有新信号 → 视为**新一轮
# 夜跑**，旧案自动结案归档另立新案（每晚的案例互不粘连、archive 路径顺带被
# 演练回归覆盖；窗口内的连续信号仍走正常去重/升级累计）。
DRILL_RESET_SEC = 6 * 3600.0

# duel_nightly 跑完后批量结案演练案例用的默认 resolution（ASCII，PS5.1 友好）。
DRILL_CLEANUP_RESOLUTION = "drill nightly cleanup"

# 结案 resolution 分桶（对齐案例页快捷标签；自由文本 → other，空 → empty）。
# 用子串匹配兼容「已安抚，客户接受补偿」这类坐席加注写法；宁可进 other 不误分桶。
_RESOLUTION_BUCKETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("soothed", ("已安抚", "soothed", "calmed")),
    ("handoff", ("已转人工", "handoff", "escalated to human")),
    ("false_alarm", ("误报", "false alarm", "false_alarm")),
)


def classify_resolution(resolution: str) -> str:
    """结案原因 → 稳定分桶键（soothed/handoff/false_alarm/empty/other）。"""
    r = str(resolution or "").strip().lower()
    if not r:
        return "empty"
    for bucket, aliases in _RESOLUTION_BUCKETS:
        for a in aliases:
            if a.lower() in r:
                return bucket
    return "other"

# 已结案案例在列表里的滞留窗（之后从列表淡出，数据仍在上下文里）。
CLOSED_LINGER_SEC = 7 * 86400.0

# 媒体质疑未结「超龄」阈值（小时）——ops 卡黄灯 / 周报跟进行同源；
# 4h＝坐席班次内该回的穿帮类信号，过了就是「客户还在等解释」。
DEFAULT_MEDIA_STALE_HOURS = 4.0

# 误报静默（P5）：结案时运营显式勾选「同类信号 N 小时内不再自动立案」的上限，
# 与永不静默来源。crisis 硬排除——自伤类信号被静默哪怕一次都是安全事故。
FALSE_ALARM_MUTE_MAX_HOURS = 168.0
_NEVER_MUTE_SOURCES = ("crisis",)


def mute_allowed(source: str) -> bool:
    """该来源可否被误报静默（close 与路由共用同一条规则，防口径漂移）。"""
    return str(source or "") not in _NEVER_MUTE_SOURCES

# 结案/归档时清理的键族（_case_history 刻意不在内——它是归档本身）。
_CASE_KEYS = (
    "_case_id", "_case_source", "_case_reason_code", "_case_reason_params",
    "_case_created_at", "_case_note", "_case_closed", "_case_resolution",
    "_case_resolution_bucket", "_case_closed_at", "_case_signal_count",
    "_case_claimed_by", "_case_claimed_at",
    "_case_events", "_case_drill",
)

_HISTORY_CAP = 5

# severity ≥ 该值的开案/升级才发告警事件（intent_chain=1 刻意不发：低危且量大）。
ALERT_MIN_SEVERITY = 2

# 旧数据（改造前只有 _case_id + _chain_pattern）的 4 个已知链模式 → 专属 reason 键。
_KNOWN_CHAIN_PATTERNS = frozenset({
    "escalation_complaint", "repeated_failure", "refund_flow",
    "channel_troubleshoot",
})


# ── 开案 / 结案 / 归档 ───────────────────────────────────────────────────────

def is_drill_uid(uid: str) -> bool:
    """uid 尾段（store 键 ``acct:chat`` / 三段式 conv id / 裸 chat id 均兼容）
    落在演练保留号段内 → True。非纯数字尾段（LINE U 号等）恒 False。"""
    tail = str(uid or "").rsplit(":", 1)[-1].strip()
    if not tail.isdigit():
        return False
    try:
        n = int(tail)
    except Exception:
        return False
    return any(lo <= n <= hi for lo, hi in DRILL_UID_RANGES)


def resolve_inbound_mid(
    ctx: Optional[Dict[str, Any]] = None, mid: str = "",
) -> str:
    """归一化入站消息 id，供案例事件锚点 / 深链 ``&mid=``。

    优先显式 ``mid``，否则从会话上下文常见键回落（``user_msg_id``＝TG A 线；
    ``message_id`` / ``platform_msg_id``＝协议/B 线）。``0``/空串视为无锚点——
    旧链路没带 id 时仍靠 quote 子串高亮，绝不编造假 mid 害滚动找错气泡。
    """
    cands: List[Any] = [mid]
    if isinstance(ctx, dict):
        cands.extend((
            ctx.get("user_msg_id"),
            ctx.get("message_id"),
            ctx.get("platform_msg_id"),
        ))
    for cand in cands:
        s = str(cand or "").strip()
        if s and s != "0":
            return s[:64]
    return ""


def _append_event(
    ctx: Dict[str, Any], ts: float, code: str, quote: str,
    params: Optional[Dict[str, Any]] = None,
    mid: str = "",
) -> None:
    """信号事件进时间线（capped；quote 截 80 字防上下文膨胀）。

    ``params``：该信号 reason 键的格式参数（如 pat.generic 的 {desc}）——事件
    label 在 API 层按请求语言渲染，缺参会把 ``{desc}`` 占位符原样漏给前端。
    ``mid``：可选平台消息 id；有则前端深链/预览优先按 id 定位，无则回落 quote。
    """
    evs = ctx.get("_case_events")
    if not isinstance(evs, list):
        evs = []
    ev: Dict[str, Any] = {
        "ts": ts,
        "code": str(code or ""),
        "quote": str(quote or "")[:80],
    }
    mid_s = resolve_inbound_mid(None, mid)
    if mid_s:
        ev["mid"] = mid_s
    if params:
        ev["params"] = dict(params)
    evs.append(ev)
    ctx["_case_events"] = evs[-_EVENTS_CAP:]


def open_case(
    ctx: Dict[str, Any],
    uid: str,
    source: str,
    reason_code: str,
    reason_params: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    quote: str = "",
    mid: str = "",
) -> Tuple[str, str]:
    """为会话开案（或对既有活跃案例去重/升级）。

    返回 ``(case_id, action)``，action ∈ created / upgraded / repeat。
    ``quote``：触发本次信号的客户原话（进 ``_case_events`` 时间线，回答
    「什么时间、说了什么」；空串=旧调用方，仍记时间戳）。
    ``mid``：触发信号的入站消息 id（空则从 ctx 的 user_msg_id 等键自吸）。
    演练号段（:func:`is_drill_uid`）照常开案但打 ``_case_drill`` 标记，
    且观测/告警旁路全静音——测试数据不进生产跟进面。
    """
    ts = float(now if now is not None else time.time())
    params = dict(reason_params or {})
    mid_s = resolve_inbound_mid(ctx, mid)
    drill = bool(ctx.get("_case_drill")) or is_drill_uid(uid)

    # 演练案例跨夜跑不粘连：上一轮的活跃演练案例（最近信号早于重置窗）在新信号
    # 到来时自动结案归档，随后按「无活跃案例」路径另立新案。真实客户案例不受影响。
    if drill and ctx.get("_case_id") and not ctx.get("_case_closed"):
        evs = ctx.get("_case_events")
        last_ts = 0.0
        if isinstance(evs, list) and evs and isinstance(evs[-1], dict):
            last_ts = float(evs[-1].get("ts") or 0)
        if last_ts <= 0:
            last_ts = float(ctx.get("_case_created_at") or 0)
        if last_ts > 0 and (ts - last_ts) >= DRILL_RESET_SEC:
            ctx["_case_drill"] = True   # legacy 演练行无标记键：补打再结，保证结案统计同样静音
            close_case(ctx, "drill auto-reset", now=ts)
            archive_closed_case(ctx)

    if ctx.get("_case_id") and not ctx.get("_case_closed"):
        ctx["_case_signal_count"] = int(ctx.get("_case_signal_count") or 1) + 1
        _append_event(ctx, ts, reason_code, quote, params, mid=mid_s)
        cur_sev = CASE_SEVERITY.get(str(ctx.get("_case_source") or "intent_chain"), 1)
        new_sev = CASE_SEVERITY.get(source, 1)
        if new_sev > cur_sev:
            ctx["_case_source"] = source
            ctx["_case_reason_code"] = reason_code
            ctx["_case_reason_params"] = params
            if not drill:
                _record_stat("upgraded", source, now=ts)
                _maybe_emit_alert(ctx, uid, str(ctx["_case_id"]), source,
                                  "upgraded", now=ts)
            return str(ctx["_case_id"]), "upgraded"
        if not drill:
            _record_stat("repeat", source, now=ts)
        return str(ctx["_case_id"]), "repeat"

    # 误报静默（P5，仅拦「新立案」这一步）：运营结案时勾了「同类 N 小时内别再
    # 自动立案」→ 同来源新信号在窗内不再开新案（跨来源照常；活跃案例的 dedupe
    # 追加事件不受影响——信号轨迹仍保留，只是不再吵人）。过期键顺手清掉。
    mute_src = str(ctx.get("_case_mute_source") or "")
    mute_until = float(ctx.get("_case_mute_until") or 0)
    if mute_src and mute_until > 0:
        if mute_until <= ts:
            ctx.pop("_case_mute_source", None)
            ctx.pop("_case_mute_until", None)
        elif source == mute_src and mute_allowed(source):
            if not drill:
                _record_stat("suppressed", source, now=ts)
            return "", "muted"

    if ctx.get("_case_id") and ctx.get("_case_closed"):
        archive_closed_case(ctx)

    case_id = f"CASE-{str(uid or '')[-6:]}-{int(ts) % 100000}"
    ctx["_case_id"] = case_id
    ctx["_case_source"] = source
    ctx["_case_reason_code"] = reason_code
    ctx["_case_reason_params"] = params
    ctx["_case_created_at"] = ts
    ctx["_case_signal_count"] = 1
    _append_event(ctx, ts, reason_code, quote, params, mid=mid_s)
    if drill:
        ctx["_case_drill"] = True
        return case_id, "created"
    _record_stat("opened", source, now=ts)
    _maybe_emit_alert(ctx, uid, case_id, source, "created", now=ts)
    return case_id, "created"


def _record_stat(kind: str, source: str, now: Optional[float] = None) -> None:
    """观测计数（best-effort，异常吞掉）：进程内计数 + 按日趋势落库。

    ``now`` 透传给趋势日键（与 open_case 的 ``ts`` 同钟——测试冻结时钟 / 回放
    场景下开案与结案必须落在同一 UTC 日，否则周报窗会拆成两行假象）。
    """
    try:
        from src.utils.case_stats import get_case_stats
        s = get_case_stats()
        if kind == "opened":
            s.record_opened(source)
        elif kind == "upgraded":
            s.record_upgraded(source)
        elif kind == "repeat":
            s.record_repeat()
        elif kind == "suppressed":
            s.record_suppressed(source)
    except Exception:
        pass
    if kind == "opened":
        try:
            from src.utils.case_trend_store import get_case_trend_store
            store = get_case_trend_store()
            if store is not None:
                store.add_opened(source, now=now)
        except Exception:
            pass


def prior_resolution_summary(
    ctx: Dict[str, Any], source: str, now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """该会话同来源的历史处置摘要（P8，开案告警的上下文）。

    返回 ``{buckets: {分桶: 次数}, last_closed_ago_hours}``；没有同源历史结案
    → None（告警文案不出空行）。接警的人开局就知道「旧招用过没用」。
    """
    ts = float(now if now is not None else time.time())
    buckets: Dict[str, int] = {}
    last_closed = 0.0
    hist = ctx.get("_case_history")
    if isinstance(hist, list):
        for h in hist:
            if not isinstance(h, dict) or str(h.get("source") or "") != source:
                continue
            b = str(h.get("resolution_bucket") or "") or "other"
            buckets[b] = buckets.get(b, 0) + 1
            last_closed = max(last_closed, float(h.get("closed_at") or 0))
    if not buckets:
        return None
    return {
        "buckets": buckets,
        "last_closed_ago_hours": (
            round((ts - last_closed) / 3600.0, 1) if last_closed > 0 else None),
    }


def _maybe_emit_alert(
    ctx: Dict[str, Any], uid: str, case_id: str, source: str, action: str,
    now: Optional[float] = None,
) -> None:
    """severity≥阈值的开案/升级 → EventBus ``case_alert``（铃铛/webhook 的原料）。

    每个案例的 created 天然只发一次；upgraded 只在严重度真上升时到达这里。
    ``rate_key`` 按会话隔离，防同一用户风暴挤占其他告警的限流窗。
    ``now`` 与 open_case 的 ts 同钟（历史处置「多久前」在冻结时钟/回放下才对）。
    """
    try:
        if CASE_SEVERITY.get(source, 1) < ALERT_MIN_SEVERITY:
            return
        payload = {
            "case_id": case_id,
            "source": source,
            "severity": CASE_SEVERITY.get(source, 1),
            "action": action,
            "user_id": str(uid or ""),
            "last_message": str(ctx.get("last_message") or "")[:80],
            "conv_ref": conv_ref(ctx),
            "rate_key": f"case:{uid}",
        }
        # P8：同源历史处置上下文——「安抚过 2 次又立案」和「头一回」处置策略不同
        prior = prior_resolution_summary(ctx, source, now=now)
        if prior:
            payload["prior_resolutions"] = prior
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("case_alert", payload)
        try:
            from src.utils.case_stats import get_case_stats
            get_case_stats().record_alert()
        except Exception:
            pass
        # 危机级（severity 3）另走集团 TG 中继直达运营手机（ops_alert：30min 防抖、
        # 无 EVENT_INGEST_KEY 自动降级只落日志）——webhook 常年 0 配置，危机只进
        # 铃铛=报进虚空；send_guard 同款先例，属「号出事」级关键事件通道。
        if CASE_SEVERITY.get(source, 1) >= 3:
            try:
                from src.ops.ops_alert import notify as _ops_notify
                _ops_notify(
                    "case_crisis",
                    (f"🚨 危机案例 {case_id}：用户…{str(uid)[-8:]} "
                     f"最近消息「{str(ctx.get('last_message') or '')[:40]}」"
                     "需真人立即跟进 → 后台「案例跟进」页"),
                    account_id=str(uid),
                    reason="crisis_case",
                )
            except Exception:
                pass
    except Exception:
        pass


def chain_reason(pattern_name: str, desc: str = "") -> Tuple[str, Dict[str, Any]]:
    """意图链模式 → reason（已知模式用专属键，未知走 generic+desc 参数）。"""
    name = str(pattern_name or "")
    if name in _KNOWN_CHAIN_PATTERNS:
        return f"case.reason.pat.{name}", {}
    if desc:
        return "case.reason.pat.generic", {"desc": str(desc)}
    return "case.reason.legacy", {}


def archive_closed_case(ctx: Dict[str, Any]) -> None:
    """把已结案案例归档进 ``_case_history``（截尾）并清空 ``_case_*`` 键族。"""
    if not ctx.get("_case_id"):
        return
    hist = ctx.get("_case_history")
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "case_id": str(ctx.get("_case_id") or ""),
        "source": str(ctx.get("_case_source") or "intent_chain"),
        "created_at": float(ctx.get("_case_created_at") or 0),
        "closed_at": float(ctx.get("_case_closed_at") or 0),
        "resolution": str(ctx.get("_case_resolution") or "")[:200],
        "resolution_bucket": str(ctx.get("_case_resolution_bucket") or ""),
        "note": str(ctx.get("_case_note") or "")[:200],
        "claimed_by": str(ctx.get("_case_claimed_by") or ""),
    })
    ctx["_case_history"] = hist[-_HISTORY_CAP:]
    for k in _CASE_KEYS:
        ctx.pop(k, None)


def close_case(
    ctx: Dict[str, Any], resolution: str = "", now: Optional[float] = None,
    mute_same_source_hours: float = 0.0,
) -> bool:
    """结案（保留在上下文里供列表淡出显示；复发时由 open_case 归档）。

    ``mute_same_source_hours`` > 0（P5，运营在结案弹窗显式勾选）＝本会话同来源
    信号在窗内不再自动开新案；crisis 硬排除（:func:`mute_allowed`），上限
    :data:`FALSE_ALARM_MUTE_MAX_HOURS`。静默键不进 ``_CASE_KEYS``——归档后仍需
    生效，那正是它存在的意义。
    """
    if not ctx.get("_case_id"):
        return False
    already = bool(ctx.get("_case_closed"))
    ctx["_case_closed"] = True
    ctx["_case_resolution"] = str(resolution or "")[:500]
    # 分桶始终落盘（含演练）——列表/关闭回包可读；观测计数仍对演练静音。
    bucket = classify_resolution(ctx["_case_resolution"])
    ctx["_case_resolution_bucket"] = bucket
    ctx["_case_closed_at"] = float(now if now is not None else time.time())
    src_now = str(ctx.get("_case_source") or "")
    mute_h = min(max(0.0, float(mute_same_source_hours or 0.0)),
                 FALSE_ALARM_MUTE_MAX_HOURS)
    if mute_h > 0 and mute_allowed(src_now):
        ctx["_case_mute_source"] = src_now
        ctx["_case_mute_until"] = ctx["_case_closed_at"] + mute_h * 3600.0
    # 演练案例的结案与开案同口径静音（不进 closed 计数/趋势——否则演练自动善后
    # 会每晚给「平均结案时长」掺假数据）。
    if not already and not ctx.get("_case_drill"):
        source = str(ctx.get("_case_source") or "unknown")
        try:
            from src.utils.case_stats import get_case_stats
            created = float(ctx.get("_case_created_at") or 0)
            hours = ((ctx["_case_closed_at"] - created) / 3600.0) if created > 0 else -1.0
            get_case_stats().record_closed(
                hours, source=source, resolution_bucket=bucket)
        except Exception:
            pass
        try:
            from src.utils.case_trend_store import get_case_trend_store
            store = get_case_trend_store()
            if store is not None:
                store.add_closed(
                    now=ctx["_case_closed_at"],
                    source=source, resolution_bucket=bucket)
        except Exception:
            pass
    return True


def claim_case(
    ctx: Dict[str, Any], actor: str, now: Optional[float] = None,
) -> Tuple[bool, str]:
    """认领案例（多坐席分工的最小语义：谁在跟、别重复跟）。

    返回 ``(ok, holder)``：他人已认领 → ``(False, 现认领人)``；同人重复认领幂等
    成功（保留首次认领时刻）。已结案/无案例不可认领。刻意不做角色权限——
    小团队工具，审计日志追责即可。
    """
    if not ctx.get("_case_id") or ctx.get("_case_closed"):
        return False, ""
    a = str(actor or "").strip() or "agent"
    holder = str(ctx.get("_case_claimed_by") or "")
    if holder and holder != a:
        return False, holder
    ctx["_case_claimed_by"] = a
    if not ctx.get("_case_claimed_at"):
        ctx["_case_claimed_at"] = float(now if now is not None else time.time())
    return True, a


def release_case(ctx: Dict[str, Any]) -> bool:
    """释放认领（任何人可释放——顶班/交接常态，审计日志记 actor）。"""
    if not ctx.get("_case_id"):
        return False
    had = bool(ctx.get("_case_claimed_by"))
    ctx.pop("_case_claimed_by", None)
    ctx.pop("_case_claimed_at", None)
    return had


# ── 入站检测器（保守词表，宁漏不误） ─────────────────────────────────────────

# 明确「要求人工/真人服务」：全部为指向性短语；刻意不收裸「找客服/联系客服」
# ——陪聊里客户转述自己生活（「我昨天找客服退货」）会误命中。
_HUMAN_REQ_TOKENS = (
    "转人工", "找人工", "要人工", "接人工", "换人工", "叫人工",
    "有人工吗", "有没有人工", "人工客服", "真人客服", "人工服务",
    "找你们客服", "你们客服呢", "找你们经理", "投诉你们",
    "talk to a human", "speak to a human", "talk to a real person",
    "speak to a real person", "human agent", "live agent", "real agent",
    "transfer me to a human", "customer service please",
    "let me talk to a person",
)

# 「怀疑对话对象是机器人」：全部带第二人称/对话指向，防「扫地机器人」类闲聊误伤；
# 「你是不是机器人」是疑问=怀疑，先于否定判断。
_AI_DOUBT_Q_TOKENS = ("你是不是机器人", "你是不是ai", "妳是不是机器人")
_AI_DOUBT_NEG_TOKENS = ("不是机器人", "不是ai", "not a bot", "not a robot", "not an ai")
_AI_DOUBT_TOKENS = (
    "你是机器人", "妳是机器人", "你是个机器人", "你就是机器人", "你就是个机器人",
    "你是ai", "你是个ai", "你就是ai", "你是机器人吧", "机器人回复", "ai生成的吧",
    "跟机器人聊天", "和机器人聊天", "对面是机器人", "在跟机器人说话",
    "are you a bot", "are you an ai", "are you a robot",
    "you're a bot", "you are a bot", "you're an ai", "you are an ai",
    "you're a robot", "you are a robot",
    "am i talking to a bot", "chatting with a bot", "talking to a machine",
)


def detect_human_request(text: str) -> bool:
    """客户明确要求人工/真人服务（保守：只认指向我方的短语）。"""
    t = str(text or "").casefold()
    if not t:
        return False
    return any(tok in t for tok in _HUMAN_REQ_TOKENS)


def detect_ai_doubt(text: str) -> bool:
    """客户怀疑对话对象是机器人/AI（第二人称指向；否定句放行）。"""
    t = str(text or "").casefold()
    if not t:
        return False
    if any(tok in t for tok in _AI_DOUBT_Q_TOKENS):
        return True
    masked = t
    for tok in _AI_DOUBT_Q_TOKENS:
        masked = masked.replace(tok, "")
    if any(tok in masked for tok in _AI_DOUBT_NEG_TOKENS):
        return False
    return any(tok in masked for tok in _AI_DOUBT_TOKENS)


# ── 列表行构建 / 排序 / 合并 ─────────────────────────────────────────────────

def conv_ref(ctx: Dict[str, Any]) -> str:
    """会话深链引用（工作台 ``/workspace?conv=`` 的取值）。

    B 线（RPA/收件箱）上下文带 ``conversation_id``（platform:account:chat_key）
    直接用；否则按收件箱镜像口径拼 ``{platform}:{acct}:{chat_id}``——platform 取
    ctx 软标记（A 线 process_message 与 B 线均会落，见 skill_manager），缺省
    telegram（原生主线）。拼不出返回空串，前端隐藏按钮。
    """
    cid = str(ctx.get("conversation_id") or "").strip()
    if cid:
        return cid
    chat = str(ctx.get("chat_id") or "").strip() or str(ctx.get("user_id") or "").strip()
    if not chat:
        return ""
    plat = str(ctx.get("platform") or "").strip() or "telegram"
    acct = str(ctx.get("account_id") or "").strip() or "default"
    return f"{plat}:{acct}:{chat}"


def _legacy_reason(ctx: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """改造前的存量案例（无 reason 键）→ 按链模式回推 reason。"""
    pattern = ctx.get("_chain_pattern")
    if isinstance(pattern, dict):
        return chain_reason(
            str(pattern.get("pattern") or ""), str(pattern.get("desc") or ""))
    return "case.reason.legacy", {}


def case_row(uid: str, ctx: Dict[str, Any], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """把一条会话上下文变换成 /api/cases/active 的行（无案例返回 None）。

    刻意保留旧字段名（satisfaction/at_risk/pattern/...）——徽标、待办条、
    dashboard 均消费该接口，改造必须向后兼容。
    """
    case_id = ctx.get("_case_id")
    if not case_id:
        return None
    ts = float(now if now is not None else time.time())
    chain = ctx.get("_intent_chain", [])
    if not isinstance(chain, list):
        chain = []
    pattern = ctx.get("_chain_pattern", {})
    if not isinstance(pattern, dict):
        pattern = {}
    profile = ctx.get("_user_profile", {})
    if not isinstance(profile, dict):
        profile = {}

    source = str(ctx.get("_case_source") or "intent_chain")
    reason_code = str(ctx.get("_case_reason_code") or "")
    reason_params = ctx.get("_case_reason_params")
    if not isinstance(reason_params, dict):
        reason_params = {}
    if not reason_code:
        reason_code, reason_params = _legacy_reason(ctx)

    created_at = float(ctx.get("_case_created_at") or 0)
    closed = bool(ctx.get("_case_closed", False))
    ref = conv_ref(ctx)
    # 身份行三元组：显式软标记优先，缺了从 conv_ref 反解（同一事实源，避免
    # 「卡片显示一套、深链另一套」的口径分裂）。
    plat = str(ctx.get("platform") or "").strip()
    acct = str(ctx.get("account_id") or "").strip()
    if (not plat or not acct) and ref.count(":") >= 2:
        _p0, _a0 = ref.split(":", 2)[:2]
        plat = plat or _p0
        acct = acct or _a0
    events_raw = ctx.get("_case_events")
    events: List[Dict[str, Any]] = []
    if isinstance(events_raw, list):
        for e in events_raw[-_EVENTS_CAP:]:
            if isinstance(e, dict):
                ev = {
                    "ts": float(e.get("ts") or 0),
                    "code": str(e.get("code") or ""),
                    "quote": str(e.get("quote") or ""),
                }
                mid_e = resolve_inbound_mid(None, str(e.get("mid") or ""))
                if mid_e:
                    ev["mid"] = mid_e
                if isinstance(e.get("params"), dict):
                    ev["params"] = e["params"]
                events.append(ev)
    last_signal_at = events[-1]["ts"] if events else created_at
    # 深链锚点＝最近一次带 mid 的信号（从尾往前）；无则空串，前端回落 quote 闪烁。
    anchor_mid = ""
    for e in reversed(events):
        m = str(e.get("mid") or "")
        if m:
            anchor_mid = m
            break
    return {
        "case_id": str(case_id),
        "user_id": str(uid),
        "chat_id": str(ctx.get("chat_id") or ""),
        "chat_title": str(ctx.get("chat_title") or ""),
        "source": source,
        "severity": CASE_SEVERITY.get(source, 1),
        "reason_code": reason_code,
        "reason_params": reason_params,
        "created_at": created_at,
        "signal_count": int(ctx.get("_case_signal_count") or 1),
        "intent_chain": chain[-8:],
        "pattern": str(pattern.get("pattern") or ""),
        "pattern_desc": str(pattern.get("desc") or ""),
        "satisfaction": profile.get("satisfaction", 80),
        "at_risk": bool(profile.get("at_risk", False)),
        "consecutive_same": ctx.get("_consecutive_same_intent", 0),
        "last_message": str(ctx.get("last_message") or "")[:100],
        "last_reply": str(ctx.get("last_reply") or "")[:100],
        "last_active": ctx.get("last_reply_time", 0),
        "escalation": bool(ctx.get("_escalation_ts")) or bool(ctx.get("_crisis_escalation_ts")),
        "claimed_by": str(ctx.get("_case_claimed_by") or ""),
        "claimed_at": float(ctx.get("_case_claimed_at") or 0),
        "closed": closed,
        "closed_at": float(ctx.get("_case_closed_at") or 0),
        "resolution": str(ctx.get("_case_resolution") or "")[:200],
        "resolution_bucket": str(
            ctx.get("_case_resolution_bucket")
            or (classify_resolution(ctx.get("_case_resolution") or "")
                if closed else "")
        ),
        # 误报静默生效中 → 给前端标注「同类已静默至」；过期/未设恒 0
        "mute_until": (
            float(ctx.get("_case_mute_until") or 0)
            if float(ctx.get("_case_mute_until") or 0) > ts else 0
        ),
        "note": str(ctx.get("_case_note") or ""),
        "conv_ref": ref,
        "age_hours": round(max(0.0, (ts - created_at) / 3600.0), 1) if created_at else None,
        "platform": plat,
        "account_id": acct,
        "peer_name": str(ctx.get("chat_title") or ""),
        "events": events,
        "last_signal_at": last_signal_at,
        "anchor_mid": anchor_mid,
        "drill": bool(ctx.get("_case_drill")) or is_drill_uid(uid),
    }


def should_hide(row: Dict[str, Any], now: Optional[float] = None) -> bool:
    """已结案超过滞留窗 → 从列表淡出（无 closed_at 的旧数据不淡出，宁可多显）。"""
    if not row.get("closed"):
        return False
    closed_at = float(row.get("closed_at") or 0)
    if closed_at <= 0:
        return False
    ts = float(now if now is not None else time.time())
    return (ts - closed_at) > CLOSED_LINGER_SEC


def sort_key(row: Dict[str, Any]) -> Tuple:
    """排序：未结案在前 → 严重度降序 → 高风险在前 → 最近信号在前。

    时效锚点＝``max(created_at, last_signal_at)``——老案刚复发（新信号）应浮上来，
    否则「4 天前立的案昨晚又炸了」会沉在列表底部无人看见。无 events 的旧行
    退化为纯 created_at（向后兼容）。
    """
    return (
        bool(row.get("closed")),
        -int(row.get("severity") or 1),
        not bool(row.get("at_risk")),
        -max(float(row.get("created_at") or 0),
             float(row.get("last_signal_at") or 0)),
    )


def collect_case_rows(
    ctx_store: Any,
    now: Optional[float] = None,
    db_limit: int = 300,
    include_drill: bool = False,
) -> List[Dict[str, Any]]:
    """内存缓存 + SQLite 兜底合并出全部可见案例行（已排序，未截断）。

    重启后内存缓存为空，但案例必须能被跟进到底——SQLite 里最后一次 flush 的
    上下文兜底补位（缓存里已有的 uid 以缓存为准，更新鲜）。ctx_store 为鸭子类型：
    需要 ``_cache`` dict 与可选 ``iter_persisted_case_rows``（旧实例没有则退化为
    纯缓存口径）。

    ``include_drill``：演练号段案例默认**排除**——看门狗/徽标/监控等所有旁路
    消费方零改动自动免疫测试数据；只有案例页列表显式带 True（在「演练」筛选
    下可见可清账）。
    """
    ts = float(now if now is not None else time.time())
    rows: List[Dict[str, Any]] = []
    seen = set()
    cache = getattr(ctx_store, "_cache", None) or {}
    for uid, ctx in list(cache.items()):
        if not isinstance(ctx, dict):
            continue
        row = case_row(uid, ctx, ts)
        if row is not None:
            rows.append(row)
        seen.add(uid)
    iter_db = getattr(ctx_store, "iter_persisted_case_rows", None)
    if callable(iter_db):
        try:
            for uid, ctx in iter_db(exclude=seen, limit=db_limit):
                row = case_row(uid, ctx, ts)
                if row is not None:
                    rows.append(row)
        except Exception:
            pass
    rows = [r for r in rows if not should_hide(r, ts)]
    if not include_drill:
        rows = [r for r in rows if not r.get("drill")]
    rows.sort(key=sort_key)
    return rows


def count_open_cases(ctx_store: Any) -> int:
    """未结案案例数（徽标/待办条口径，与列表同源）。"""
    return sum(1 for r in collect_case_rows(ctx_store) if not r.get("closed"))


def count_open_drill_cases(ctx_store: Any, now: Optional[float] = None) -> int:
    """未结演练案例数（夜跑残影卫生；真实客户永不计入）。"""
    return sum(
        1 for r in collect_case_rows(ctx_store, now=now, include_drill=True)
        if r.get("drill") and not r.get("closed")
    )


def summarize_case_board(
    rows: List[Dict[str, Any]],
    now: Optional[float] = None,
    media_stale_hours: float = DEFAULT_MEDIA_STALE_HOURS,
) -> Dict[str, Any]:
    """案例看板摘要（P4）：ops 卡 / ``/api/cases/active`` / 周报跟进行**同源**。

    演练行只进 ``open_drill``，不掺真实 open/urgent/media 计数——与
    collect_case_rows 默认排除演练的运营口径一致。
    """
    ts = float(now if now is not None else time.time())
    stale_h = max(0.5, float(media_stale_hours or DEFAULT_MEDIA_STALE_HOURS))
    open_n = urgent = media_open = media_stale = open_drill = 0
    oldest_media_h: Optional[float] = None
    for r in rows or []:
        if r.get("drill"):
            if not r.get("closed"):
                open_drill += 1
            continue
        if r.get("closed"):
            continue
        open_n += 1
        if int(r.get("severity") or 1) >= 3:
            urgent += 1
        if str(r.get("source") or "") == "media_complaint":
            media_open += 1
            age = r.get("age_hours")
            if age is None:
                created = float(r.get("created_at") or 0)
                age = ((ts - created) / 3600.0) if created > 0 else 0.0
            age_f = float(age or 0)
            if oldest_media_h is None or age_f > oldest_media_h:
                oldest_media_h = round(age_f, 1)
            if age_f >= stale_h:
                media_stale += 1
    return {
        "open": open_n,
        "urgent": urgent,
        "media_open": media_open,
        "media_stale": media_stale,
        "media_stale_hours": stale_h,
        "oldest_media_hours": oldest_media_h,
        "open_drill": open_drill,
    }


def case_board_text_lines(summary: Dict[str, Any]) -> List[str]:
    """看板摘要 → 周报/ops_report 可读跟进行（有待办才出，零噪音）。"""
    s = summary or {}
    lines: List[str] = []
    urgent = int(s.get("urgent") or 0)
    if urgent:
        lines.append(f"紧急未结案例 {urgent} 条需立即跟进")
    media_stale = int(s.get("media_stale") or 0)
    if media_stale:
        thr = s.get("media_stale_hours") or DEFAULT_MEDIA_STALE_HOURS
        oldest = s.get("oldest_media_hours")
        tail = f"，最老 {oldest}h" if oldest is not None else ""
        lines.append(f"媒体质疑超龄未结 {media_stale} 条（≥{thr}h{tail}）")
    open_drill = int(s.get("open_drill") or 0)
    if open_drill:
        lines.append(f"演练案例残影未清 {open_drill} 条（可一键 close-drill）")
    return lines


def _drill_last_activity(ctx: Dict[str, Any]) -> float:
    """演练案例最近活动时刻（末条信号 ts，缺省开案时刻）——判「是否还在跑」。"""
    evs = ctx.get("_case_events")
    if isinstance(evs, list) and evs and isinstance(evs[-1], dict):
        ts = float(evs[-1].get("ts") or 0)
        if ts > 0:
            return ts
    return float(ctx.get("_case_created_at") or 0)


def close_open_drill_cases(
    ctx_store: Any,
    resolution: str = DRILL_CLEANUP_RESOLUTION,
    now: Optional[float] = None,
    persist: bool = True,
    min_age_hours: float = 0.0,
) -> int:
    """批量结案**未结**演练号段案例（duel_nightly 收尾钩子 / watchdog 自动清扫）。

    与 ``DRILL_RESET_SEC`` 互补：跨夜重置只在「下一轮信号到来」时触发——若当晚
    对练后没人再碰那些 uid，残影会一直挂到人工清账。夜跑末尾主动扫一遍，把
    「测试数据不得污染生产跟进面」收成闭环。

    ``min_age_hours`` > 0（P6 watchdog 自动清扫用）＝只清**最近活动**早于该窗的
    残影——正在跑的演练（信号还在进）不被中途搅局；0=全清（夜跑收尾/一键按钮
    的旧语义不变）。返回结案数。``persist=True`` 时对每个被结案 uid 调
    ``mark_dirty``+``flush``。真实客户案例从不命中号段，零误伤面。
    """
    ts = float(now if now is not None else time.time())
    res = str(resolution or DRILL_CLEANUP_RESOLUTION)[:500]
    age_cut = max(0.0, float(min_age_hours or 0.0)) * 3600.0

    def _skip_fresh(ctx: Dict[str, Any]) -> bool:
        return age_cut > 0 and (ts - _drill_last_activity(ctx)) < age_cut

    closed_n = 0
    cache = getattr(ctx_store, "_cache", None) or {}
    touched: List[str] = []
    for uid, ctx in list(cache.items()):
        if not isinstance(ctx, dict):
            continue
        if not ctx.get("_case_id") or ctx.get("_case_closed"):
            continue
        if not (ctx.get("_case_drill") or is_drill_uid(uid)):
            continue
        if _skip_fresh(ctx):
            continue
        ctx["_case_drill"] = True  # legacy 行补标记，结案统计保持静音
        if close_case(ctx, res, now=ts):
            closed_n += 1
            touched.append(str(uid))
    # SQLite 兜底：重启后内存空、演练残影只在持久层——一并扫（排除已处理 uid）。
    iter_db = getattr(ctx_store, "iter_persisted_case_rows", None)
    if callable(iter_db):
        seen = set(touched) | set(cache.keys())
        try:
            for uid, ctx in iter_db(exclude=seen, limit=500):
                if not isinstance(ctx, dict):
                    continue
                if not ctx.get("_case_id") or ctx.get("_case_closed"):
                    continue
                if not (ctx.get("_case_drill") or is_drill_uid(uid)):
                    continue
                if _skip_fresh(ctx):
                    continue
                ctx["_case_drill"] = True
                if close_case(ctx, res, now=ts):
                    closed_n += 1
                    touched.append(str(uid))
                    # 写回缓存，便于随后 flush
                    try:
                        cache[str(uid)] = ctx
                    except Exception:
                        pass
        except Exception:
            pass
    if persist and touched:
        mark = getattr(ctx_store, "mark_dirty", None)
        flush = getattr(ctx_store, "flush", None)
        for uid in touched:
            try:
                if callable(mark):
                    mark(uid)
                if callable(flush):
                    flush(uid)
            except Exception:
                pass
    return closed_n


def _ctx_case_timeline(ctx: Dict[str, Any], source: str) -> List[Dict[str, float]]:
    """该会话某来源的案例时间线（归档史 + 当前案例，按开案时刻升序）。

    条目：``{created, closed, bucket}``（closed=0 表示仍未结）。history 容量
    截尾（_HISTORY_CAP=5）——超出的远古案例自然滚出观察面，可接受。
    """
    out: List[Dict[str, Any]] = []
    hist = ctx.get("_case_history")
    if isinstance(hist, list):
        for h in hist:
            if not isinstance(h, dict) or str(h.get("source") or "") != source:
                continue
            out.append({
                "created": float(h.get("created_at") or 0),
                "closed": float(h.get("closed_at") or 0),
                "bucket": str(h.get("resolution_bucket") or "") or "other",
            })
    if ctx.get("_case_id") and str(ctx.get("_case_source") or "") == source:
        out.append({
            "created": float(ctx.get("_case_created_at") or 0),
            "closed": (float(ctx.get("_case_closed_at") or 0)
                       if ctx.get("_case_closed") else 0.0),
            "bucket": str(ctx.get("_case_resolution_bucket") or "") or "other",
        })
    out.sort(key=lambda x: x["created"])
    return out


def media_resolution_effectiveness(
    ctx_store: Any,
    window_days: float = 7.0,
    lookback_days: float = 30.0,
    now: Optional[float] = None,
    source: str = "media_complaint",
) -> Dict[str, Any]:
    """「处置管用吗」回环（P6）：同源结案后 window 内复发率，按结案分桶分组。

    对每条会话取该来源案例时间线，凡 ``closed_at`` 落在 lookback 窗内的已结案
    条目：其后 window 内又开了同源新案 → 记复发（recurred）。``immature``＝
    结案至今还没满 window 的条目——它们「还没来得及复发」，读复发率时该剔出
    分母（如实分开报，不做静默扣除）。演练会话整体跳过；纯读不写。
    """
    ts = float(now if now is not None else time.time())
    win = max(0.1, float(window_days or 7.0)) * 86400.0
    lb = max(win, float(lookback_days or 30.0) * 86400.0)
    by_bucket: Dict[str, Dict[str, int]] = {}
    total_closed = total_recurred = 0
    cache = getattr(ctx_store, "_cache", None) or {}
    for uid, ctx in list(cache.items()):
        if not isinstance(ctx, dict):
            continue
        if ctx.get("_case_drill") or is_drill_uid(uid):
            continue
        timeline = _ctx_case_timeline(ctx, source)
        if not timeline:
            continue
        for i, entry in enumerate(timeline):
            closed_at = entry["closed"]
            if closed_at <= 0 or closed_at < ts - lb:
                continue
            bucket = entry["bucket"]
            slot = by_bucket.setdefault(
                bucket, {"closed": 0, "recurred": 0, "immature": 0})
            slot["closed"] += 1
            total_closed += 1
            recurred = any(
                0 < later["created"] - closed_at <= win
                for later in timeline[i + 1:]
            )
            if recurred:
                slot["recurred"] += 1
                total_recurred += 1
                # P8：复发会话样本（建议条点击→按会话过滤的深链原料，capped）
                samples = slot.setdefault("sample_uids", [])
                if len(samples) < 8 and str(uid) not in samples:
                    samples.append(str(uid))
            elif closed_at + win > ts:
                slot["immature"] += 1
    return {
        "source": source,
        "window_days": round(win / 86400.0, 1),
        "lookback_days": round(lb / 86400.0, 1),
        "closed": total_closed,
        "recurred": total_recurred,
        "by_bucket": by_bucket,
    }


def effectiveness_alerts(
    eff: Dict[str, Any],
    min_matured: int = 3,
    min_rate: float = 0.5,
) -> List[Dict[str, Any]]:
    """复发回环 → 需要人检视的分桶（P7）：样本够（≥min_matured 已满窗）且复发率
    ≥min_rate 才点名——小样本 1/1=100% 不该吓人。分母＝已满窗（closed-immature），
    与 ops 卡展示同口径（单一规则，前端/周报共用本函数防阈值漂移）。"""
    out: List[Dict[str, Any]] = []
    for bucket, s in sorted((eff.get("by_bucket") or {}).items()):
        if not isinstance(s, dict):
            continue
        matured = max(0, int(s.get("closed") or 0) - int(s.get("immature") or 0))
        rec = int(s.get("recurred") or 0)
        if matured >= max(1, int(min_matured)) and (rec / matured) >= float(min_rate):
            out.append({
                "bucket": bucket,
                "recurred": rec,
                "matured": matured,
                "rate": round(rec / matured, 2),
                # 复发会话样本透传（前端点击建议条→过滤出这些会话）
                "sample_uids": list(s.get("sample_uids") or []),
            })
    return out


_BUCKET_ZH = {
    "soothed": "安抚", "handoff": "转人工", "false_alarm": "判误报",
    "empty": "未填原因", "other": "其他处置",
}


def effectiveness_text_lines(eff: Dict[str, Any]) -> List[str]:
    """复发回环 → 周报/ops_report 可读行（有告警才出，零噪音；zh 口径与
    case_board_text_lines 同族——周报推送本就是中文栈）。"""
    lines: List[str] = []
    for a in effectiveness_alerts(eff or {}):
        label = _BUCKET_ZH.get(str(a["bucket"]), str(a["bucket"]))
        pct = int(round(float(a["rate"]) * 100))
        lines.append(
            f"媒体质疑「{label}」后 7 天复发 {a['recurred']}/{a['matured']}"
            f"（{pct}%）——处置没起效，检查回应话术/素材供给")
    return lines


def summarize_takeover_episodes(
    episodes: List[Dict[str, Any]], now: Optional[float] = None,
) -> Dict[str, Any]:
    """人工接管闭环观测（P7）：store.takeover_episodes 的行 → 卡片一行读数。

    ``end``=0 表示还没回流（进行中）；时长只算已回流集，中位数够用（均值会被
    忘了回流的长尾拖飞）。纯函数零 IO。"""
    ts = float(now if now is not None else time.time())
    eps = [e for e in (episodes or []) if isinstance(e, dict)]
    done = [e for e in eps if float(e.get("end") or 0) > 0]
    durs = sorted(
        max(0.0, (float(e["end"]) - float(e.get("start") or 0)) / 3600.0)
        for e in done)
    median = durs[len(durs) // 2] if durs else None
    open_eps = [e for e in eps if not float(e.get("end") or 0)]
    oldest_open_h = max(
        ((ts - float(e.get("start") or ts)) / 3600.0 for e in open_eps),
        default=0.0)
    return {
        "count": len(eps),
        "open": len(open_eps),
        "median_hours": round(median, 1) if median is not None else None,
        "oldest_open_hours": round(oldest_open_h, 1) if open_eps else None,
    }
