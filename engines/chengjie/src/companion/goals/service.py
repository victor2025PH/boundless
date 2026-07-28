"""目标服务编排层——routes / skill_manager 注入 / 主动桥的**同一入口**。

口径唯一性是本模块存在的理由：右栏卡显示的里程碑、prompt 注入的里程碑、
主动桥判定的拍，全部经 ``refresh_goal``（settle-on-read + 当日拍幂等规划）
产出——三个消费面读同一份结算结果，杜绝「卡片说第2步、prompt 说第3步」的漂移。

所有函数绝不抛：目标层任何失败都不能拖垮聊天/草稿/主动触达主链路。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.companion.goals import store as goal_store_mod
from src.companion.goals.context_block import goal_view_block
from src.companion.goals.ledger import settle_goal
from src.companion.goals.planner import day_key, effective_rejects, plan_beat
from src.companion.goals.signals import collect_signals
from src.companion.goals.stats import get_goal_stats
from src.companion.goals.store import GoalStore, get_goal_store
from src.companion.goals.templates import (
    get_template,
    milestone_label,
)

logger = logging.getLogger("GoalService")

_DAY = 86400.0

# 强负面情绪 hint 词表（user_context.user_emotion_hint / conversation last_emotion）
NEGATIVE_EMOTIONS = frozenset(
    ("sad", "angry", "anxious", "depressed", "negative", "fear", "tired",
     "frustrated", "grief"))

DEFAULT_DB_NAME = "marketing_goals.db"


def resolve_goals_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals`` 配置段（缺/异常 → {} = 默认关）。"""
    try:
        if not isinstance(cfg_root, dict):
            return {}
        return (cfg_root.get("companion") or {}).get("goals") or {}
    except Exception:
        return {}


def goals_enabled(cfg_root: Any) -> bool:
    return bool(resolve_goals_cfg(cfg_root).get("enabled", False))


def resolve_db_path(cfg_root: Any, config_path: Any = None) -> str:
    """目标库路径：``companion.goals.db_path`` 显式值 > ``<config 目录>/marketing_goals.db``。"""
    cfg = resolve_goals_cfg(cfg_root)
    explicit = str(cfg.get("db_path") or "").strip()
    if explicit == ":memory:":
        return ":memory:"
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and config_path:
            p = Path(config_path).parent / p
        return str(p)
    if config_path:
        return str(Path(config_path).parent / DEFAULT_DB_NAME)
    return str(Path("config") / DEFAULT_DB_NAME)


def get_configured_store(cfg_root: Any, config_path: Any = None) -> GoalStore:
    """进程单例 store（首次调用据配置定库路径）。"""
    return get_goal_store(resolve_db_path(cfg_root, config_path))


# P4 成交反哺选品：近窗 plan 成交计数的进程级 TTL 缓存（注入在每条消息热路上，
# 别每轮打 SQL；5min 陈旧度对「近 90 天销量做同分裁决」毫无影响）。
_SOLD_CACHE: Dict[int, tuple] = {}
_SOLD_TTL_SEC = 300.0


def sold_plan_counts_cached(
    store: GoalStore, *, now: Optional[float] = None
) -> Dict[str, int]:
    """``store.sold_plan_counts()`` 的 TTL 缓存读（键=store 身份；绝不抛）。"""
    try:
        n = float(now if now is not None else time.time())
        key = id(store)
        ent = _SOLD_CACHE.get(key)
        if ent and ent[0] > n:
            return ent[1]
        counts = store.sold_plan_counts(now=n) \
            if hasattr(store, "sold_plan_counts") else {}
        _SOLD_CACHE[key] = (n + _SOLD_TTL_SEC, counts)
        return counts
    except Exception:
        return {}


def is_negative_emotion(hint: Any) -> bool:
    return str(hint or "").strip().lower() in NEGATIVE_EMOTIONS


def refresh_goal(
    store: GoalStore,
    cfg_root: Any,
    goal: Dict[str, Any],
    *,
    inbox_store: Any = None,
    negative_emotion: bool = False,
    emotion_intensity: float = -1.0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """结算 + 当日拍规划的单一入口。返回
    ``{"goal": 最新行, "action": 今日拍|None, "hold": 原因|None}``。绝不抛。"""
    n = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"goal": goal, "action": None, "hold": None}
    try:
        gid = str(goal.get("goal_id") or "")
        tid = str(goal.get("template") or "")
        template = get_template(tid) or {}
        signals = collect_signals(
            platform=str(goal.get("platform") or ""),
            account_id=str(goal.get("account_id") or ""),
            chat_key=str(goal.get("chat_key") or ""),
            conversation_id=str(goal.get("conversation_id") or ""),
            inbox_store=inbox_store,
            negative_emotion=negative_emotion,
            emotion_intensity=emotion_intensity,
            now=n,
        )
        # 画像双轨完成度（P1）：模板声明 profile_slots → 读画像卡 → signals.extras
        # （acquire_and_convert 结算据 bant_fill 推「摸底完成」里程碑）。
        if template.get("profile_slots"):
            try:
                from src.companion.goals.profile_slots import fill_rates
                prof = store.get_customer_profile(
                    str(goal.get("platform") or ""),
                    str(goal.get("chat_key") or ""))
                rates = fill_rates((prof or {}).get("fields") or {})
                signals.extras["bant_fill"] = float(rates.get("bant") or 0.0)
                signals.extras["relation_fill"] = float(
                    rates.get("relation") or 0.0)
            except Exception:
                logger.debug("profile fill rates skipped", exc_info=True)
        stats = get_goal_stats()

        # 「已开价」证据：direct 拍进入过生成/发出（转化模板里程碑 3 的推进条件）
        direct_engaged = False
        try:
            for a in store.list_actions(gid, limit=60):
                if (str(a.get("push_level")) == "direct"
                        and str(a.get("status")) in ("consumed", "sent")):
                    direct_engaged = True
                    break
        except Exception:
            direct_engaged = False

        res = settle_goal(
            template_id=tid, template=template, goal=goal, signals=signals,
            direct_beat_engaged=direct_engaged, now=n)
        adv = max(0, int(res["milestone_idx"]) - int(goal.get("milestone_idx") or 0))
        stats.record_settle(milestones_advanced=adv)
        if res["changed"]:
            old_status = str(goal.get("status"))
            store.update_goal_fields(
                gid, status=res["status"], milestone_idx=res["milestone_idx"],
                progress=res["progress"], result=res["result"],
                done_at=(n if res["status"] in ("done", "failed", "expired")
                         and old_status == "active" else
                         float(goal.get("done_at") or 0)),
            )
            for kind, detail in res["events"]:
                store.add_event(gid, kind, detail)
            if (res["status"] != old_status
                    and res["status"] in ("done", "failed", "expired")):
                stats.record_terminal(res["status"])
            goal = store.get_goal(gid) or goal
            # P7 回流再转化：挽回目标在此转 done（=对方回话）→ 顺势起转化目标。
            # 门控在 maybe_spawn_reconvert 内（created_by/开关），非挽回目标零开销。
            if res["status"] == "done" and old_status == "active":
                try:
                    maybe_spawn_reconvert(store, cfg_root, goal, now=n)
                except Exception:
                    logger.debug("reconvert hook skipped", exc_info=True)
        out["goal"] = goal

        if str(goal.get("status")) != "active":
            return out

        # 当日拍（幂等）：已有 → 直接用；没有 → 规划（hold 则不建）
        day = day_key(n)
        action = store.get_action(gid, day)
        if action is None:
            cfg = resolve_goals_cfg(cfg_root)
            pl = (cfg.get("planner") or {}) if isinstance(cfg, dict) else {}
            engaged = store.count_engaged_since(gid, signals.last_inbound_ts)
            # 坐席驳回回流（P2）：近窗 beat_rejected 事件 → planner 降档/退避
            try:
                lookback = float(pl.get("reject_lookback_days", 7) or 7)
            except (TypeError, ValueError):
                lookback = 7.0
            since_reject = n - max(1.0, lookback) * _DAY
            # 撤销补偿（P17）：同窗口内 beat_reject_undone 逐一抵消 beat_rejected
            # ——否则「撤销驳回」只改了今天的拍状态，明天照旧被降档=撤销是假的。
            rejects = effective_rejects(
                store.count_events_since(gid, "beat_rejected", since_reject),
                store.count_events_since(
                    gid, "beat_reject_undone", since_reject))
            beat = plan_beat(
                template=template, goal=goal, signals=signals, day=day,
                engaged_since_inbound=engaged,
                backoff_after=int(pl.get("backoff_after_unanswered", 2) or 0),
                halt_after=int(pl.get("halt_after_unanswered", 4) or 0),
                recent_rejects=rejects,
            )
            if beat is None or beat.get("hold"):
                reason = str((beat or {}).get("hold") or "no_intent")
                stats.record_hold(reason)
                out["hold"] = reason
                return out
            action = store.upsert_action(
                gid, day, intent=str(beat.get("intent") or ""),
                push_level=str(beat.get("push_level") or "soft"), now=n)
            if action is not None:
                stats.record_beat_planned()
        out["action"] = action
    except Exception:
        logger.debug("refresh_goal failed", exc_info=True)
    return out


def goal_view(
    goal: Dict[str, Any],
    action: Optional[Dict[str, Any]] = None,
    hold: Optional[str] = None,
    *,
    lang: str = "zh",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """API/UI/注入共用的视图形状。"""
    n = float(now if now is not None else time.time())
    tid = str(goal.get("template") or "")
    template = get_template(tid) or {}
    start = float(goal.get("start_ts") or 0)
    deadline = float(goal.get("deadline_ts") or 0)
    total_days = int(round((deadline - start) / _DAY)) if (
        start > 0 and deadline > start) else int(template.get("default_days") or 0)
    day_index = int((n - start) // _DAY) + 1 if start > 0 else 1
    title = str(goal.get("title") or "").strip()
    if not title:
        key = "name_en" if str(lang).lower().startswith("en") else "name_zh"
        title = str(template.get(key) or tid)
        params = goal.get("params") or {}
        label = str(params.get("item_label") or "").strip()
        if label:
            title = f"{title}：{label}"
    mi = int(goal.get("milestone_idx") or 0)
    view: Dict[str, Any] = {
        "goal_id": str(goal.get("goal_id") or ""),
        "conversation_id": str(goal.get("conversation_id") or ""),
        "platform": str(goal.get("platform") or ""),
        "account_id": str(goal.get("account_id") or ""),
        "chat_key": str(goal.get("chat_key") or ""),
        "template": tid,
        "template_name": str(template.get(
            "name_en" if str(lang).lower().startswith("en") else "name_zh") or tid),
        # 模板能力位（UI 据此显隐画像卡等区块，免硬编码模板名单）
        "profile_slots": bool(template.get("profile_slots")),
        "catalog": bool(template.get("catalog")),
        "title": title,
        "params": goal.get("params") or {},
        "status": str(goal.get("status") or "active"),
        "autonomy": str(goal.get("autonomy") or "suggest"),
        "priority": int(goal.get("priority") or 1),
        "milestone_idx": mi,
        "milestone_label": milestone_label(template, mi, lang),
        "milestones": [dict(m) for m in (template.get("milestones") or [])],
        "progress": float(goal.get("progress") or 0.0),
        "day_index": day_index,
        "total_days": total_days,
        "start_ts": start,
        "deadline_ts": deadline,
        "result": str(goal.get("result") or ""),
        "created_at": float(goal.get("created_at") or 0),
        "updated_at": float(goal.get("updated_at") or 0),
        "hold": hold,
        "today": None,
    }
    if action is not None:
        view["today"] = {
            "action_id": str(action.get("action_id") or ""),
            "day": str(action.get("day") or ""),
            "intent": str(action.get("intent") or ""),
            "push_level": str(action.get("push_level") or "soft"),
            "status": str(action.get("status") or "planned"),
            # detail 透传：坐席「采纳」标记（"adopted"）/驳回原因（"rejected:*"）
            # → 右栏卡按此渲染反馈态
            "detail": str(action.get("detail") or ""),
        }
    return view


# ── 今日工作清单（P17 agenda）────────────────────────────────────────────────
# 看板给的是「计数」，坐席要的是「点名单」：今天哪几个会话该推、哪几个还没人审、
# 哪几个让路了。下面几个纯函数把 goal_view 折成清单行 + 计数 + 排序键——
# 路由只负责取数（仍走 refresh_goal 的 settle-on-read 唯一口径，不另开一条结算路）。

# state 过滤词表（路由校验与前端筛选 chips 同一份）
AGENDA_STATES = ("push", "pending", "hold", "adopted", "rejected")

# 排序用力度权重（direct 最该先看；未知力度垫底）
_PUSH_RANK = {"direct": 0, "soft": 1, "none": 2}


def beat_feedback_state(action: Optional[Dict[str, Any]]) -> str:
    """今日拍的坐席反馈态：``adopted`` / ``rejected`` / ``""``（还没人审）。

    入参既可是 store 的 action 行，也可是 ``goal_view()["today"]``——两者都带
    ``status``/``detail``。口径必须唯一：清单渲染、撤销判定读的是同一个函数，
    否则「清单说已驳回、撤销却说没反馈」。
    """
    if not isinstance(action, dict) or not action:
        return ""
    detail = str(action.get("detail") or "")
    if detail == "adopted":
        return "adopted"
    if (str(action.get("status") or "") in ("skipped", "blocked")
            or detail.startswith("rejected")):
        return "rejected"
    return ""


def agenda_item(view: Dict[str, Any]) -> Dict[str, Any]:
    """``goal_view`` → 今日清单的一行（形状对前端固定，勿随意增删键）。

    刻意**不**联表客户昵称：行里只出 conversation_id/platform/chat_key，展示名
    由前端拿已加载的会话列表自己拼——目标层不反向依赖 inbox store。
    无当日拍（hold 日/无意图）→ ``intent=""``、``push_level="none"``、
    ``beat_status=""``（前端据此渲染「今天让路」而不是假装有安排）。
    """
    today = view.get("today") if isinstance(view.get("today"), dict) else None
    return {
        "goal_id": str(view.get("goal_id") or ""),
        "conversation_id": str(view.get("conversation_id") or ""),
        "platform": str(view.get("platform") or ""),
        "account_id": str(view.get("account_id") or ""),
        "chat_key": str(view.get("chat_key") or ""),
        "title": str(view.get("title") or ""),
        "template": str(view.get("template") or ""),
        "template_name": str(view.get("template_name") or ""),
        "status": str(view.get("status") or "active"),
        "day_index": int(view.get("day_index") or 0),
        "total_days": int(view.get("total_days") or 0),
        "progress": float(view.get("progress") or 0.0),
        "milestone_idx": int(view.get("milestone_idx") or 0),
        "milestone_label": str(view.get("milestone_label") or ""),
        "intent": str((today or {}).get("intent") or ""),
        "push_level": str((today or {}).get("push_level") or "none"),
        "hold": str(view.get("hold") or ""),
        "feedback": beat_feedback_state(today),
        "beat_status": str((today or {}).get("status") or ""),
        "autonomy": str(view.get("autonomy") or ""),
    }


def agenda_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """清单徽章计数（口径写死在这里，前端与本函数同源，不各算一套）：

    - ``with_push``：今天有推进意图的（``intent`` 非空）
    - ``pending_feedback``：有意图且**还没人审**——坐席今天真正要动的那批
    - ``adopted`` / ``rejected``：坐席已表态的
    - ``hold``：情绪/沉默让路（今天不推）

    ``total``＝清单全长。四类**刻意不互斥**（驳回的也曾有意图，仍计 with_push），
    每个数字各自回答一个问题，别拿它们相加。
    """
    out = {"total": len(items or []), "with_push": 0, "pending_feedback": 0,
           "adopted": 0, "rejected": 0, "hold": 0}
    for it in (items or []):
        fb = str(it.get("feedback") or "")
        if str(it.get("intent") or ""):
            out["with_push"] += 1
            if not fb:
                out["pending_feedback"] += 1
        if fb == "adopted":
            out["adopted"] += 1
        elif fb == "rejected":
            out["rejected"] += 1
        if str(it.get("hold") or ""):
            out["hold"] += 1
    return out


def agenda_sort_key(item: Dict[str, Any]) -> tuple:
    """清单排序：待人审优先 → 力度（direct>soft>none）→ 天数倒序 → goal_id。

    末位 goal_id 是**确定性**保证（同分行的相对次序跨请求恒定，前端列表不抖）。
    """
    pending = 0 if (str(item.get("intent") or "")
                    and not str(item.get("feedback") or "")) else 1
    return (
        pending,
        _PUSH_RANK.get(str(item.get("push_level") or ""), 9),
        -int(item.get("day_index") or 0),
        str(item.get("goal_id") or ""),
    )


def agenda_state_match(item: Dict[str, Any], state: str) -> bool:
    """``state`` 筛选（空/未知 → 全放行，宁可多显示也不 500 掉整个清单）。"""
    st = str(state or "").strip().lower()
    fb = str(item.get("feedback") or "")
    if st == "push":
        return bool(str(item.get("intent") or ""))
    if st == "pending":
        return bool(str(item.get("intent") or "")) and not fb
    if st == "hold":
        return bool(str(item.get("hold") or ""))
    if st in ("adopted", "rejected"):
        return fb == st
    return True


# 成交归因 meta 的长度护栏（坐席手填，截断而非拒收——别为了字数卡住成交录入）
WON_META_PRODUCT_MAX = 80
WON_META_NOTE_MAX = 200


def sanitize_won_meta(raw: Any) -> Dict[str, Any]:
    """坐席「标成交」的可选归因 meta → 消毒后的紧凑 dict（未知键一律丢弃）。

    - ``product``：卖了什么（截 80 字）
    - ``amount``：金额（**可选**，不逼坐席填；负数/非数/NaN 一律丢弃；
      整数值落成 int 让 JSON 更紧凑）
    - ``note``：备注（截 200 字）
    非 dict / 三项全空 → ``{}``（调用方据此决定要不要落事件）。绝不抛。
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    try:
        product = str(raw.get("product") or "").strip()[:WON_META_PRODUCT_MAX]
        if product:
            out["product"] = product
        amount = raw.get("amount")
        if isinstance(amount, bool):
            amount = None                       # True/False 不是金额
        if isinstance(amount, str):
            amount = amount.strip() or None
        if amount is not None:
            try:
                val = round(float(amount), 2)
            except (TypeError, ValueError):
                val = None
            if val is not None and val == val and val >= 0 and val != float("inf"):
                out["amount"] = int(val) if float(val).is_integer() else val
        note = str(raw.get("note") or "").strip()[:WON_META_NOTE_MAX]
        if note:
            out["note"] = note
    except Exception:               # 坏输入不该拦住「标成交」这个主动作
        logger.debug("sanitize_won_meta failed", exc_info=True)
    return out


def maybe_auto_create_goal(
    store: GoalStore,
    cfg_root: Any,
    *,
    platform: str,
    chat_key: str,
    account_id: str = "",
    conversation_id: str = "",
    user_context: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """新好友自动建目标（``companion.goals.auto_create``，默认关）。

    获客账号（绑定了指定人设，如 su_wan）收到私聊首条消息 → 自动建
    acquire_and_convert 目标，当轮即出「今日拍」——批量 campaign 零人工。
    护栏（全过才建）：
    - 人设 allowlist **必填**（空=全站误开闸风险，直接不建）；按会话生效人设
      匹配（PersonaManager 账号级绑定优先，chat 绑定兜底，与回复链同口径）；
    - 群聊不建；无入站文本的调用（主动链）不建；
    - 幂等：会话历史上建过任何目标（含终态）不再建；
    - 每日预算（进库计数，跨重启稳）。
    失败/不满足 → None，绝不抛。
    """
    try:
        cfg = resolve_goals_cfg(cfg_root)
        ac = cfg.get("auto_create") or {}
        if not isinstance(ac, dict) or not ac.get("enabled", False):
            return None
        uc = user_context or {}
        if uc.get("is_group"):
            return None
        personas = [str(x).strip() for x in (ac.get("personas") or [])
                    if str(x).strip()]
        if not personas:
            return None
        platforms = [str(x).strip().lower() for x in (ac.get("platforms") or [])
                     if str(x).strip()]
        if platforms and str(platform or "").strip().lower() not in platforms:
            return None
        # 会话生效人设（与回复链 get_persona_with_tier 同口径）
        try:
            from src.utils.persona_manager import PersonaManager
            persona, _tier = PersonaManager.get_instance().get_persona_with_tier(
                str(uc.get("chat_id") or chat_key or ""),
                str(uc.get("account_persona_id") or ""),
            )
            pid = str((persona or {}).get("id") or "").strip()
        except Exception:
            return None
        if pid not in personas:
            return None
        template_id = str(ac.get("template") or "acquire_and_convert").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            return None
        conv_id = str(conversation_id or "").strip()
        if not conv_id and platform and account_id and chat_key:
            conv_id = f"{platform}:{account_id}:{chat_key}"
        if store.has_any_goal(conversation_id=conv_id, platform=platform,
                              chat_key=str(chat_key or "")):
            return None
        n = float(now if now is not None else time.time())
        budget = max(1, int(ac.get("max_per_day", 20) or 20))
        day_start = time.mktime(time.strptime(
            time.strftime("%Y-%m-%d", time.localtime(n)), "%Y-%m-%d"))
        if store.count_created_by_since("auto_create", day_start) >= budget:
            return None
        autonomy = str(ac.get("autonomy") or "auto").strip().lower()
        days = float(ac.get("days") or tmpl.get("default_days") or 0)
        goal = store.create_goal(
            conversation_id=conv_id,
            platform=str(platform or ""),
            account_id=str(account_id or ""),
            chat_key=str(chat_key or ""),
            template=template_id,
            autonomy=autonomy,
            priority=int(ac.get("priority", 1) or 1),
            deadline_days=days,
            created_by="auto_create",
            now=n,
        )
        if goal is not None:
            get_goal_stats().record_created()
            get_goal_stats().record_auto_created()
            logger.info("[goal-auto] 新会话自动建目标 %s conv=%s persona=%s",
                        goal.get("goal_id"), conv_id, pid)
        return goal
    except Exception:
        logger.debug("maybe_auto_create_goal failed", exc_info=True)
        return None


def build_block_for_chat(
    config_obj: Any,
    *,
    platform: str,
    chat_key: str,
    account_id: str = "",
    conversation_id: str = "",
    user_context: Optional[Dict[str, Any]] = None,
    chain: str = "reply",
    inbound_text: str = "",
    inbox_store: Any = None,
    ai_client: Any = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """skill_manager 注入口（单调用闭环）：找活跃目标 → 顺手采画像 →
    结算+当日拍 → 组块（+缺口提示 +目录块）→ 标记拍已进入生成。
    未启用/无目标/hold → None。绝不抛。"""
    try:
        cfg_root = getattr(config_obj, "config", None)
        if not isinstance(cfg_root, dict):
            cfg_root = config_obj if isinstance(config_obj, dict) else {}
        cfg = resolve_goals_cfg(cfg_root)
        if not cfg.get("enabled", False):
            return None
        inject_cfg = cfg.get("inject") or {}
        if not inject_cfg.get("enabled", True):
            return None
        store = get_configured_store(
            cfg_root, getattr(config_obj, "config_path", None))
        goal = store.find_active_goal(
            conversation_id=conversation_id, platform=platform,
            chat_key=str(chat_key or ""), account_id=account_id)
        # 无目标 + 获客人设新会话 → 自动建（auto_create，默认关）；
        # 建成即续走本轮流程——新好友第一条消息就带目标方向。
        # 只在真实入站消息触发（inbound_text 空=主动链/系统调用，不建）。
        if goal is None and str(inbound_text or "").strip():
            goal = maybe_auto_create_goal(
                store, cfg_root, platform=platform,
                chat_key=str(chat_key or ""), account_id=account_id,
                conversation_id=conversation_id,
                user_context=user_context, now=now)
        if goal is None:
            return None
        # observe 档：目标只作看板观测，完全不进 prompt
        if str(goal.get("autonomy") or "suggest") == "observe":
            return None
        template = get_template(str(goal.get("template") or "")) or {}
        has_slots = bool(template.get("profile_slots"))

        # P1 顺手采集：对方本条消息里的高置信画像信号 → 只填空槽（坐席手录优先）。
        # 采在结算**前**——「预算两千美金」这类信号当轮就计入 bant_fill 推里程碑。
        if has_slots and str(inbound_text or "").strip():
            try:
                from src.companion.goals.profile_slots import capture_from_text
                captured = capture_from_text(inbound_text)
                if captured:
                    store.upsert_customer_profile(
                        platform, str(chat_key or ""), dict(captured),
                        source="auto", now=now)
                    get_goal_stats().record_profile_captured(len(captured))
            except Exception:
                logger.debug("profile capture skipped", exc_info=True)
            # LLM 摘录轨（profile_llm，默认关）：正则轨盲区补丁——自由表达的
            # 痛点/职业等经小 LLM 摘录+接地后台补槽，fire-and-forget 零阻塞。
            if ai_client is not None:
                try:
                    from src.companion.goals.profile_llm import (
                        schedule_llm_capture,
                    )
                    prof0 = store.get_customer_profile(
                        platform, str(chat_key or ""))
                    schedule_llm_capture(
                        ai_client, store, cfg_root,
                        platform=platform, chat_key=str(chat_key or ""),
                        text=inbound_text,
                        fields=dict((prof0 or {}).get("fields") or {}),
                        now=now)
                except Exception:
                    logger.debug("profile llm schedule skipped", exc_info=True)

        # P8 生命周期采集：留存/挽回/回流会话里「为什么没续」→ churn_reason 槽。
        # 独立于 profile_slots 能力位——winback 的 engagement_reactivate 模板
        # 没有 bant 摸底，但流失原因恰恰只在这些会话里出现。retention 期是
        # 日常闲聊，句内须带续费锚词防误采（「这家餐厅太贵」不算）；
        # winback/reconvert 会话本身就在聊流失，免锚。auto 只填空、坐席可改。
        created_by = str(goal.get("created_by") or "")
        if created_by in _LIFECYCLE_CREATORS and str(inbound_text or "").strip():
            try:
                from src.companion.goals.profile_slots import (
                    capture_churn_reason,
                )
                reason = capture_churn_reason(
                    inbound_text,
                    require_anchor=(created_by == "retention_auto"))
                if reason:
                    before = store.get_customer_profile(
                        platform, str(chat_key or ""))
                    had = bool(_cell_value(
                        (before or {}).get("fields") or {}, "churn_reason"))
                    store.upsert_customer_profile(
                        platform, str(chat_key or ""),
                        {"churn_reason": reason}, source="auto", now=now)
                    if not had:
                        get_goal_stats().record_churn_captured()
                        logger.info("[goal-churn] %s:%s 流失原因: %s",
                                    platform, chat_key, reason)
            except Exception:
                logger.debug("churn capture skipped", exc_info=True)

        # 注意保持调用方 dict 身份（空 dict 也不换新对象）——_goal_cta 暂存要
        # 写回调用方的 user_context 才能被出站守卫读到
        uc = user_context if isinstance(user_context, dict) else {}
        # P4 链接纪律守卫档位暂存：catalog 模板的每一轮（含 hold/驳回/让位等
        # 早退路径——恰是最该守纪律的日子）先落保守档 ""=剥全部本域链接；
        # 真出目录块时再升为当日实际档。出站守卫（A线 5c3 / 拟稿 9）读后即焚。
        if template.get("catalog"):
            try:
                from src.companion.goals import site_catalog as _sc
                _site = (_sc.load_catalog(_sc.catalog_path(
                    cfg_root, getattr(config_obj, "config_path", None)))
                    .get("site") or {})
                if _site.get("base_url"):
                    uc["_goal_cta"] = {
                        "cta": "",
                        "base_url": str(_site.get("base_url") or ""),
                        "order_path": str(_site.get("order_path") or "/order"),
                        # P14：当日授权活动文案＝出站优惠守卫的白名单（说得起
                        # 的话不剥）；没授权活动＝任何折扣承诺都是编的
                        "offer_texts": _authorized_offer_texts(
                            cfg_root, getattr(config_obj, "config_path", None)),
                        # 授权免费时长（目录 claims.free_days）：真有的试用不该
                        # 被当成编的剥掉，措辞千变但天数是事实
                        "offer_free_days": _authorized_free_days(
                            cfg_root, getattr(config_obj, "config_path", None)),
                        # P16：人设归属——守卫剥离计数按人设分桶，
                        # 「哪个人设在编折扣」直接可见（best-effort，失败留空）
                        "persona_id": _account_persona(
                            cfg_root, platform, account_id),
                        # P15 事实声明守卫：目录登记的合法价格。LLM 报了目录里
                        # 没有的数（实录「团队版198美金一个月，算下来一个月168」
                        # ＝偷偷 8.5 折）即拦——措辞轴的 offer_guard 管不到裸数字。
                        "catalog_prices": _catalog_price_list(
                            cfg_root, getattr(config_obj, "config_path", None)),
                    }
            except Exception:
                logger.debug("goal cta stash skipped", exc_info=True)
        neg = is_negative_emotion(uc.get("user_emotion_hint"))
        res = refresh_goal(
            store, cfg_root, goal, inbox_store=inbox_store,
            negative_emotion=neg, now=now)
        if res.get("hold") or str((res.get("goal") or {}).get("status")) != "active":
            return None
        action = res.get("action")
        # 坐席驳回今日拍（P2）→ 今天彻底不注入（明日 planner 按驳回回流降档重排）
        if action is not None and str(action.get("status")) in ("skipped", "blocked"):
            return None

        # 画像 → prompt（P1 缺口 + P9a 档案事实）。档案对所有模板有益
        # （挽回目标没有 profile_slots 也该知道「TA是做民宿的」）→ 画像
        # 常载；缺口采集指令仍只在摸底型模板 + 破冰后出。
        profile_gap = ""
        profile_facts = ""
        prof_fields: Dict[str, Any] = {}
        try:
            from src.companion.goals.profile_slots import facts_line, gap_hint
            prof = store.get_customer_profile(platform, str(chat_key or ""))
            prof_fields = dict((prof or {}).get("fields") or {})
            profile_facts = facts_line(prof_fields)
            if has_slots and int(
                    (res.get("goal") or {}).get("milestone_idx") or 0) >= 1:
                profile_gap = gap_hint(prof_fields)
        except Exception:
            profile_gap = profile_facts = ""

        # P9b 流失应对策略：生命周期会话 + 原因在档 → 背景行拼「应对：…」。
        # 按当轮画像现值动态拼（不烤进 note——采集常发生在目标创建之后）。
        note_suffix = ""
        if created_by in _LIFECYCLE_CREATORS:
            try:
                from src.companion.goals.profile_slots import (
                    churn_strategy_hint,
                )
                churn_now = _cell_value(prof_fields, "churn_reason")
                strategy = churn_strategy_hint(churn_now)
                if strategy:
                    note_suffix = f"应对：{strategy}"
            except Exception:
                note_suffix = ""

        suppress = bool(str(uc.get("_bazi_block") or "").strip())
        view = goal_view(res["goal"], res.get("action"), now=now)
        block = goal_view_block(
            view,
            suppress_push=suppress,
            max_chars=int(inject_cfg.get("max_chars", 360) or 360),
            profile_gap=profile_gap,
            profile_facts=profile_facts,
            note_suffix=note_suffix,
        )
        if not block:
            return None

        # 官网产品目录块（P3）：模板声明 catalog + 今日拍力度 soft/direct →
        # 附「可推荐产品 + CTA 纪律」。同轮已有命理变现引导（suppress）不叠加。
        # CTA 按「力度×里程碑×BANT 资质」分级（pick_cta）：资质没聊透不甩
        # 下单链（引客服半人工收口）、种草段可给试算器——单一主 CTA 防链接大杂烩。
        if template.get("catalog") and action is not None and not suppress:
            try:
                from src.companion.goals import site_catalog as sc
                catalog = sc.load_catalog(sc.catalog_path(
                    cfg_root, getattr(config_obj, "config_path", None)))
                pinned = str((goal.get("params") or {}).get("product_id") or "")
                # P4 成交反哺：近窗真卖动的 SKU 在痛点同分时排前（TTL 缓存，
                # 零流量时空表=纯 pains 排序，行为不变）
                sold = sc.sold_boost_map(
                    catalog, sold_plan_counts_cached(store, now=now))
                prods = sc.pick_products(
                    catalog, prof_fields, pinned=pinned, sold=sold)
                # 成交归因 ref（默认开）：对客链接挂会话 id，官网下单经
                # order-hook/order_pull 按 ref 回流自动结算目标 done。
                link_ref = ""
                if (cfg.get("catalog") or {}).get("link_ref", True):
                    link_ref = str(goal.get("conversation_id")
                                   or conversation_id or "").strip()
                bant_fill = None
                if has_slots:
                    try:
                        from src.companion.goals.profile_slots import fill_rates
                        bant_fill = float(
                            fill_rates(prof_fields).get("bant") or 0.0)
                    except Exception:
                        bant_fill = None
                push_lvl = str(action.get("push_level") or "soft")
                site = catalog.get("site") or {}
                mi = int((res.get("goal") or {}).get("milestone_idx") or 0)
                # P11 流失原因驱动选品/CTA：只在生命周期会话生效（与策略门控
                # 同口径）——太贵→入门档深链+提前试算；信任问题→客服收口。
                # 不发明折扣码，公开货架内换档而已。
                plan_pref = cta_bias = ""
                churn_val = ""
                if created_by in _LIFECYCLE_CREATORS:
                    try:
                        from src.companion.goals.profile_slots import (
                            churn_offer_steer,
                        )
                        churn_val = _cell_value(prof_fields, "churn_reason")
                        steer = churn_offer_steer(churn_val)
                        plan_pref = str(steer.get("plan_pref") or "")
                        cta_bias = str(steer.get("cta_bias") or "")
                    except Exception:
                        plan_pref = cta_bias = ""
                cta = sc.pick_cta(push_level=push_lvl, milestone_idx=mi,
                                  bant_fill=bant_fill, site=site,
                                  cta_bias=cta_bias)
                # 升档链接纪律暂存为当日实际许可（守卫据此放行对应链接）
                if isinstance(uc.get("_goal_cta"), dict):
                    uc["_goal_cta"]["cta"] = cta
                # P13 运营授权活动：只在流失会话、且目录里真有未过期的在售
                # 活动时引用一条（引擎不发明折扣；无配置=行为与 P11 相同）。
                offer = None
                if churn_val:
                    try:
                        from src.companion.goals import offers as offers_mod
                        offer = offers_mod.pick_offer(
                            catalog, churn_reason=churn_val,
                            product_id=(pinned or str(
                                (prods[0] or {}).get("id") or "")
                                if prods else pinned))
                        # 同目标同天只提一次：价格信息提一遍就够，条条都提
                        # ＝促销骚扰（目标块每条入站消息都会注入）
                        if offer and not offers_mod.claim_offer_citation(
                                str(goal.get("goal_id") or ""),
                                str(offer.get("id") or ""), now=now):
                            offer = None
                    except Exception:
                        offer = None
                cat_block = sc.build_catalog_block(
                    prods,
                    push_level=push_lvl,
                    site=site,
                    link_ref=link_ref,
                    cta=cta,
                    plan_pref=plan_pref,
                    cta_bias=cta_bias,
                    offer=offer,
                )
                if cat_block:
                    block = f"{block}\n{cat_block}"
                    get_goal_stats().record_catalog_injected(cta)
                    if plan_pref == "entry" or cta_bias:
                        get_goal_stats().record_churn_steered()
                    if offer and offers_mod.offer_block_line(
                            offer, cta=str(cta or "")):
                        get_goal_stats().record_offer_cited(
                            str(offer.get("id") or ""))
            except Exception:
                logger.debug("catalog block skipped", exc_info=True)

        if action is not None and str(action.get("status")) == "planned":
            store.mark_action(
                str(action.get("action_id")), "consumed", detail=chain)
        get_goal_stats().record_injected(chain)
        return block
    except Exception:
        logger.debug("build_block_for_chat failed", exc_info=True)
        return None


# 生命周期自动目标的 created_by 集合（churn_reason 采集门控：只在这些
# 会话里「为什么没续」才是真流失信号，普通获客会话不采）
_LIFECYCLE_CREATORS = ("retention_auto", "winback_auto", "reconvert_auto")


def _account_persona(cfg_root: Any, platform: str, account_id: str) -> str:
    """账号生效人设 id（守卫观测归属用，best-effort 绝不抛）。"""
    try:
        from src.ai.persona_voice import resolve_account_persona_id
        return str(resolve_account_persona_id(
            cfg_root if isinstance(cfg_root, dict) else {},
            str(platform or ""), str(account_id or "")) or "")
    except Exception:
        return ""


def _authorized_offer_texts(cfg_root: Any, config_path: Any = None) -> List[str]:
    """出站优惠守卫的白名单（单一出口 ``offers.allowlist_texts``）。绝不抛。"""
    try:
        from src.companion.goals import offers as offers_mod
        from src.companion.goals import site_catalog as sc
        return offers_mod.allowlist_texts(
            sc.load_catalog(sc.catalog_path(cfg_root, config_path)))
    except Exception:
        return []


def _authorized_free_days(cfg_root: Any, config_path: Any = None) -> List[float]:
    """授权免费时长（单一出口 ``offers.authorized_free_days``）。绝不抛。"""
    try:
        from src.companion.goals import offers as offers_mod
        from src.companion.goals import site_catalog as sc
        return offers_mod.authorized_free_days(
            sc.load_catalog(sc.catalog_path(cfg_root, config_path)))
    except Exception:
        return []


def _catalog_price_list(cfg_root: Any, config_path: Any = None) -> List[float]:
    """目录登记的合法价格（单一出口 ``claim_guard.catalog_prices``）。绝不抛。"""
    try:
        from src.companion.goals import site_catalog as sc
        from src.companion.goals.claim_guard import catalog_prices
        return catalog_prices(
            sc.load_catalog(sc.catalog_path(cfg_root, config_path)))
    except Exception:
        return []


def catalog_guard_facts(cfg_root: Any,
                        config_path: Any = None) -> Dict[str, Any]:
    """出站事实守卫所需的**目录事实**（键与 ``_goal_cta`` 同名同源）。

    存在理由（2026-07-28 预检实锤）：出站守卫原先只在 ``_goal_cta`` 暂存存在时
    才被调到，而那份暂存只有**有漏斗目标**的会话才有。当 ``auto_create`` 日预算
    （``max_per_day``）打满、或人设不在 auto_create 名单、或会话根本没建目标时，
    「报价/试用时长/gated 线/未授权折扣」这些**与目标无关的事实正确性**守卫全部
    静默失效——预检 6 场里唯一有目标的那场 0 缺陷，其余 5 场裸奔（七折/85折/
    14 天试用/免费换脸全出街）。

    价格与试用天数是**全局商业事实**，不该由「这条会话有没有目标」决定守不守。
    本函数让守卫在无目标会话也能拿到同一份事实（``site_catalog`` 自带 mtime
    缓存 + 5s stat 节流，挂在每条回复上开销可忽略）。CTA 档位类纪律
    （``link_guard``）仍只在有目标时生效——没目标就没档位可言。
    """
    return {
        "offer_texts": _authorized_offer_texts(cfg_root, config_path),
        "offer_free_days": _authorized_free_days(cfg_root, config_path),
        "catalog_prices": _catalog_price_list(cfg_root, config_path),
    }


def _cell_value(fields: Optional[Dict[str, Any]], key: str) -> str:
    """画像 fields 里某槽的值（``{"v","src","ts"}`` 单元 → v；无/坏形状 → ""）。"""
    cell = (fields or {}).get(key)
    if isinstance(cell, dict):
        return str(cell.get("v") or "").strip()
    return ""


def _profile_churn_reason(store: GoalStore, platform: str, chat_key: str) -> str:
    """读画像里已采的流失原因标签（无 → ""，绝不抛）。挽回/回流/新留存周期
    建目标时把它织进 note → LLM 针对性回应而非泛泛挽留。"""
    try:
        prof = store.get_customer_profile(
            str(platform or ""), str(chat_key or ""))
        return _cell_value((prof or {}).get("fields") or {}, "churn_reason")[:40]
    except Exception:
        return ""


def _entry_plan_key(cfg_root: Any, product_id: str = "",
                    last_plan: str = "") -> str:
    """目录里入门档 plan key（给 note 预览用）。优先钉死 product_id /
    last_plan 所属产品；否则取目录第一款有入门档的产品。无 → ""。"""
    try:
        from src.companion.goals import site_catalog as sc
        catalog = sc.load_catalog(sc.catalog_path(cfg_root, None))
        prods = [p for p in (catalog.get("products") or [])
                 if isinstance(p, dict)]
        pid = str(product_id or "").strip()
        lp = str(last_plan or "").strip()
        ordered: list = []
        if pid:
            ordered.extend(p for p in prods if str(p.get("id") or "") == pid)
        if lp:
            for p in prods:
                keys = {str(x.get("key") or "")
                        for x in (p.get("plans") or []) if isinstance(x, dict)}
                if lp in keys and p not in ordered:
                    ordered.append(p)
        ordered.extend(p for p in prods if p not in ordered)
        for prod in ordered:
            key = sc.entry_plan_key(prod)
            if key:
                return key
        return ""
    except Exception:
        return ""


def _steer_note_suffix(churn: str, cfg_root: Any = None,
                       product_id: str = "", last_plan: str = "") -> str:
    """P12：流失原因 → note 内「选品/收口」指引（不发明折扣）。空原因 → ""。"""
    try:
        from src.companion.goals.profile_slots import churn_offer_steer
        steer = churn_offer_steer(churn)
    except Exception:
        return ""
    if steer.get("plan_pref") == "entry":
        # cfg_root 允许 {}（仍走默认 site_catalog.yaml）；仅 None 跳过查档
        ek = (_entry_plan_key(cfg_root, product_id, last_plan)
              if cfg_root is not None else "")
        if ek:
            return f"价格敏感→优先入门档 {ek}（不承诺折扣）"
        return "价格敏感→优先入门档（不承诺折扣）"
    if steer.get("cta_bias") == "cs":
        return "信任未重建→先客服收口再谈下单"
    return ""


def resolve_plan_product(cfg_root: Any, plan: str) -> tuple:
    """上单 plan → 目录 ``(product_id, 产品名)``（留存/回流选品钉死在买过的
    那款 + 标题自证）。查不到/无目录 → ("", "")，绝不抛。"""
    p = str(plan or "").strip()
    if not p:
        return "", ""
    try:
        from src.companion.goals import site_catalog as sc
        catalog = sc.load_catalog(sc.catalog_path(cfg_root, None))
        for prod in (catalog.get("products") or []):
            keys = {str(x.get("key") or "").strip()
                    for x in (prod.get("plans") or [])
                    if isinstance(x, dict)}
            if p in keys:
                return (str(prod.get("id") or ""),
                        str(prod.get("name_zh") or prod.get("name_en") or ""))
    except Exception:
        pass
    return "", ""


# 订阅周期 → 留存目标天数（P6：年付客户不该在第 24 天被催续费）。
# 官网 OrderEntry.period 词表（monthly/annual）；config retention.period_days 可覆写。
_PERIOD_DAYS_DEFAULT = {"monthly": 30, "annual": 365}


def retention_days_for_period(
    rc: Dict[str, Any], period: str = "", template: Optional[Dict[str, Any]] = None,
) -> float:
    """留存周期天数：``period_days[period]`` > ``rc.days`` > 模板默认。纯函数。"""
    p = str(period or "").strip().lower()
    if p:
        pd = rc.get("period_days")
        table = dict(_PERIOD_DAYS_DEFAULT)
        if isinstance(pd, dict):
            for k, v in pd.items():
                try:
                    table[str(k).strip().lower()] = float(v)
                except (TypeError, ValueError):
                    continue
        if p in table and table[p] > 0:
            return float(table[p])
    try:
        d = float(rc.get("days") or 0)
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    return float((template or {}).get("default_days") or 30)


def maybe_create_retention_goal(
    store: GoalStore,
    cfg_root: Any,
    base_goal: Dict[str, Any],
    *,
    plan: str = "",
    period: str = "",
    manual: bool = False,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """成交后自动起「留存/续费」目标（P5 LTV 环；``companion.goals.retention``
    默认关）。won 结算的两条路都挂：订单回流（settle_order_ref）与坐席手动
    标成交（status 路由，``manual=True``）。绝不抛。

    - ``manual=True`` 只对 ``kind=conversion`` 模板续期——engagement /
      relationship 类（含挂了 catalog 供 P11 挽回选品的 engagement_reactivate）
      「标 done」是关系终局不是成交，绝不起续费；订单路径真金白银无条件续期；
    - 同会话仍有活跃目标 → 不建（正常不会有：won 的目标刚转终态）；
    - 链式循环：续费单结算留存目标 done → 再起下一周期——由真实续费驱动，
      天然有界；expired（流失）/cancelled（放弃）不续，交 winback 扫描低频挽回；
    - 周期自适应（P6）：``period``（官网订单 monthly/annual）经 period_days
      映射定目标天数——年付=365 天弧线（相位线性缩放：季度价值确认、末月收口），
      月付=30 天；无 period 回落 ``rc.days``；
    - ``product_id``/``item_label`` 自动继承：按上单 plan 反查目录产品，续费
      收口段选品钉死在 TA 买过的那款、目标标题自带产品名（30 天后上下文窗
      早滚走，goal block 得自证「TA 买过什么」）。查不到留空走画像选品。
    """
    try:
        cfg = resolve_goals_cfg(cfg_root)
        rc = cfg.get("retention") or {}
        if not (cfg.get("enabled", False) and rc.get("enabled", False)):
            return None
        if not isinstance(base_goal, dict):
            return None
        if manual:
            # catalog≠成交语义（P11 起唤回模板也挂目录）；conversion kind 才是卖货弧线
            mt = get_template(str(base_goal.get("template") or "")) or {}
            if str(mt.get("kind") or "") != "conversion":
                return None
            # 双保险：winback done = 对方回话，交给 maybe_spawn_reconvert
            if str(base_goal.get("created_by") or "") == "winback_auto":
                return None
        conv = str(base_goal.get("conversation_id") or "").strip()
        platform = str(base_goal.get("platform") or "").strip()
        chat_key = str(base_goal.get("chat_key") or "").strip()
        if not conv and not (platform and chat_key):
            return None
        if store.find_active_goal(
                conversation_id=conv, platform=platform, chat_key=chat_key,
                account_id=str(base_goal.get("account_id") or "")) is not None:
            return None
        template_id = str(rc.get("template") or "retention_expand").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            return None
        n = float(now if now is not None else time.time())
        # 上单 plan 反查产品（续费选品钉死在买过的那款 + 标题带产品名自证）
        p = str(plan or "").strip()
        product_id, product_label = resolve_plan_product(cfg_root, p)
        params: Dict[str, Any] = {
            "product_id": product_id, "last_plan": p,
            "item_label": product_label,
            "last_period": str(period or "").strip().lower(),
            "base_goal": str(base_goal.get("goal_id") or ""),
        }
        # 历史流失原因（回流客再买 / 上周期续费前抱怨过）→ 本周期提前留意
        churn = _profile_churn_reason(store, platform, chat_key)
        if churn:
            params["note"] = f"客户历史反馈过：{churn}——本周期提前留意"
        goal = store.create_goal(
            conversation_id=conv,
            platform=platform,
            account_id=str(base_goal.get("account_id") or ""),
            chat_key=chat_key,
            template=template_id,
            autonomy=str(rc.get("autonomy")
                         or base_goal.get("autonomy") or "auto"),
            priority=int(base_goal.get("priority", 1) or 1),
            deadline_days=retention_days_for_period(rc, period, tmpl),
            params=params,
            created_by="retention_auto",
            now=n,
        )
        if goal is not None:
            get_goal_stats().record_created()
            get_goal_stats().record_retention_created()
            logger.info("[goal-retention] 成交续期：conv=%s 上单 plan=%s → 新周期 %s",
                        conv or f"{platform}:{chat_key}", p or "-",
                        goal.get("goal_id"))
        return goal
    except Exception:
        logger.debug("maybe_create_retention_goal failed", exc_info=True)
        return None


def run_winback_scan(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """流失挽回扫描（P6，``companion.goals.retention.winback`` 默认关）。

    闭环最后一段：留存目标到期没续（expired=真实流失）→ 冷却 ``cooldown_days``
    （默认 60）后自动起一个低频 ``engagement_reactivate`` 挽回目标（push_curve
    none/none/soft/soft，情绪/沉默护栏照常）——「流失≠永别」。护栏：
    - 窗口下限 ``max_age_days``（默认 180）：功能首开时不对陈年流失群发挽回；
    - 幂等：流失点（deadline）之后会话建过任何目标（挽回过/迟到续费起新周期/
      手动新盘）→ 不再挽回；一次流失只挽回一次；
    - 会话当前有活跃目标 → 跳过；每日预算 ``max_per_day``（进库计数跨重启稳）；
    - 顺手清扫：候选里 status 仍 active 的静默会话先经 refresh_goal 结算
      （settle-on-read 盲区——没人打开的会话永远不落 expired，流失报表也靠这补）。
    返回摘要 ``{candidates, swept, created, skipped, budget_hit}``，绝不抛。
    """
    summary = {"candidates": 0, "swept": 0, "created": 0,
               "skipped": 0, "budget_hit": False}
    try:
        cfg = resolve_goals_cfg(cfg_root)
        rc = cfg.get("retention") or {}
        wb = rc.get("winback") or {}
        if not (cfg.get("enabled", False) and isinstance(wb, dict)
                and wb.get("enabled", False)):
            return summary
        n = float(now if now is not None else time.time())
        cooldown = max(1.0, float(wb.get("cooldown_days", 60) or 60))
        max_age = max(cooldown, float(wb.get("max_age_days", 180) or 180))
        wb_template_id = str(wb.get("template")
                             or "engagement_reactivate").strip()
        wb_tmpl = get_template(wb_template_id)
        if wb_tmpl is None:
            return summary
        budget = max(1, int(wb.get("max_per_day", 10) or 10))
        day_start = time.mktime(time.strptime(
            time.strftime("%Y-%m-%d", time.localtime(n)), "%Y-%m-%d"))
        # 候选：留存模板 deadline 落在 [now-max_age, now-cooldown] 的到期目标
        candidates = store.list_overdue_goals(
            template=str(rc.get("template") or "retention_expand"),
            before_ts=n - cooldown * _DAY,
            after_ts=n - max_age * _DAY,
            limit=50,
        )
        summary["candidates"] = len(candidates)
        for g in candidates:
            gid = str(g.get("goal_id") or "")
            # 静默会话清扫：还挂 active 的先按现实结算（expired 落库进报表）
            if str(g.get("status")) == "active":
                res = refresh_goal(store, cfg_root, g,
                                   inbox_store=inbox_store, now=n)
                g = res.get("goal") or g
                summary["swept"] += 1
            if str(g.get("status")) != "expired":
                summary["skipped"] += 1        # 清扫后 done/paused 等=没流失
                continue
            conv = str(g.get("conversation_id") or "")
            platform = str(g.get("platform") or "")
            chat_key = str(g.get("chat_key") or "")
            deadline = float(g.get("deadline_ts") or 0.0)
            # 幂等：流失点之后建过任何目标 → 这次流失已被处理过
            if store.has_goal_created_after(
                    deadline, conversation_id=conv,
                    platform=platform, chat_key=chat_key):
                summary["skipped"] += 1
                continue
            if store.find_active_goal(
                    conversation_id=conv, platform=platform,
                    chat_key=chat_key,
                    account_id=str(g.get("account_id") or "")) is not None:
                summary["skipped"] += 1
                continue
            if store.count_created_by_since("winback_auto", day_start) >= budget:
                summary["budget_hit"] = True
                break
            p = str((g.get("params") or {}).get("last_plan") or "")
            pid = str((g.get("params") or {}).get("product_id") or "")
            note = (f"老客户流失挽回：曾购 {p}，上周期未续费"
                    if p else "老客户流失挽回：上周期未续费")
            # 留存期采到过流失原因（「太贵，不续了」）→ 挽回话术针对性回应
            churn = _profile_churn_reason(store, platform, chat_key)
            if churn:
                note += f"；TA说过原因：{churn}"
            # P12：价格敏感/信任问题写进 note（入门档 key 预览，不发明折扣）
            steer_bit = _steer_note_suffix(
                churn, cfg_root, product_id=pid, last_plan=p)
            if steer_bit:
                note += f"；{steer_bit}"
            goal = store.create_goal(
                conversation_id=conv,
                platform=platform,
                account_id=str(g.get("account_id") or ""),
                chat_key=chat_key,
                template=wb_template_id,
                autonomy=str(wb.get("autonomy")
                             or g.get("autonomy") or "auto"),
                priority=int(g.get("priority", 1) or 1),
                deadline_days=float(wb.get("days")
                                    or wb_tmpl.get("default_days") or 14),
                # P11：继承 product_id，挽回 soft/direct 日目录选品可钉住上单
                params={"note": note, "last_plan": p, "product_id": pid,
                        "base_goal": gid},
                created_by="winback_auto",
                now=n,
            )
            if goal is not None:
                summary["created"] += 1
                get_goal_stats().record_created()
                get_goal_stats().record_winback_created()
                logger.info("[goal-winback] 流失挽回：conv=%s（源 %s）→ %s",
                            conv or f"{platform}:{chat_key}", gid,
                            goal.get("goal_id"))
        return summary
    except Exception:
        logger.debug("run_winback_scan failed", exc_info=True)
        return summary


def maybe_spawn_reconvert(
    store: GoalStore,
    cfg_root: Any,
    base_goal: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """挽回成功 → 再转化衔接（P7，``retention.winback.reconvert`` 默认关）。

    winback 目标 done（=流失客户真的回话了，ledger result="replied"）→ 顺势起
    一个短周期转化目标，把「回来了」变成「再成交」——生命周期从此真正成环：
    获客→成交→留存→(流失→挽回→回流)→再转化→成交→留存…

    克制护栏：
    - **只对 ``created_by=winback_auto`` 的 done 目标**触发——坐席手工建的普通
      唤回完成后不自动转卖（那是关系目标，不是销售目标）；
    - 产品/plan 从挽回目标 params 继承（挽回又继承自留存周期），note 明示
      「回流老客户，别当新客户从头摸底」——画像本就已填，结算器会按 bant_fill
      快进过摸底段，直接进种草/收口；
    - 同会话有活跃目标不叠；失败/过期的挽回不触发（人没回来，别追）；
    - 数量天然有界：1:1 跟随挽回 done，上游 winback 已有每日预算。绝不抛。
    """
    try:
        cfg = resolve_goals_cfg(cfg_root)
        wb = ((cfg.get("retention") or {}).get("winback") or {})
        rc = (wb.get("reconvert") or {}) if isinstance(wb, dict) else {}
        if not (cfg.get("enabled", False) and isinstance(rc, dict)
                and rc.get("enabled", False)):
            return None
        if not isinstance(base_goal, dict):
            return None
        if str(base_goal.get("created_by") or "") != "winback_auto":
            return None
        if str(base_goal.get("status") or "") != "done":
            return None
        conv = str(base_goal.get("conversation_id") or "").strip()
        platform = str(base_goal.get("platform") or "").strip()
        chat_key = str(base_goal.get("chat_key") or "").strip()
        if not conv and not (platform and chat_key):
            return None
        if store.find_active_goal(
                conversation_id=conv, platform=platform, chat_key=chat_key,
                account_id=str(base_goal.get("account_id") or "")) is not None:
            return None
        template_id = str(rc.get("template") or "acquire_and_convert").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            return None
        n = float(now if now is not None else time.time())
        p = str((base_goal.get("params") or {}).get("last_plan") or "").strip()
        product_id, product_label = resolve_plan_product(cfg_root, p)
        what = product_label or p
        note = (f"回流老客户：曾购 {what}，流失后被挽回回来了——衔接旧关系，"
                "别当新客户从头摸底，聊聊TA当初为什么没续、现在什么变了"
                if what else
                "回流老客户：流失后被挽回回来了——衔接旧关系，别当新客户从头摸底")
        # 挽回期采到的流失原因 → 再转化直接对症（降价/新功能/稳定性改进…）
        churn = _profile_churn_reason(store, platform, chat_key)
        if churn:
            note = (f"回流老客户：曾购 {what}，当初因「{churn}」没续——"
                    "针对这点回应变化，别再踩同一个坑"
                    if what else
                    f"回流老客户：当初因「{churn}」流失——针对这点回应变化")
        steer_bit = _steer_note_suffix(
            churn, cfg_root, product_id=product_id, last_plan=p)
        if steer_bit:
            note += f"；{steer_bit}"
        goal = store.create_goal(
            conversation_id=conv,
            platform=platform,
            account_id=str(base_goal.get("account_id") or ""),
            chat_key=chat_key,
            template=template_id,
            autonomy=str(rc.get("autonomy")
                         or base_goal.get("autonomy") or "auto"),
            priority=int(base_goal.get("priority", 1) or 1),
            deadline_days=float(rc.get("days")
                                or tmpl.get("default_days") or 10),
            params={"note": note, "product_id": product_id,
                    "item_label": product_label, "last_plan": p,
                    "base_goal": str(base_goal.get("goal_id") or "")},
            created_by="reconvert_auto",
            now=n,
        )
        if goal is not None:
            get_goal_stats().record_created()
            get_goal_stats().record_reconvert_created()
            logger.info("[goal-reconvert] 回流再转化：conv=%s（挽回 %s）→ %s",
                        conv or f"{platform}:{chat_key}",
                        base_goal.get("goal_id"), goal.get("goal_id"))
        return goal
    except Exception:
        logger.debug("maybe_spawn_reconvert failed", exc_info=True)
        return None


def settle_order_ref(
    store: GoalStore,
    *,
    ref: str,
    order_id: str = "",
    plan: str = "",
    period: str = "",
    now: Optional[float] = None,
    cfg_root: Any = None,
) -> Dict[str, Any]:
    """按会话归因串 ``ref`` 把活跃目标结算为 done（成交闭环的**单一入口**）。

    两个消费方共用：``POST /api/goals/order-hook``（官网推）与
    ``order_pull``（引擎拉官网订单，NAT 后无公网入口的实际部署形态）。
    幂等：同 ``order_id`` 已回流过 → ``dup=True`` 不重复结算；
    未匹配到活跃目标 → ``matched=False``（订单本可来自非目标流量，非错误）。
    返回 ``{matched, goal_id, dup, updated}``。
    ``cfg_root`` 非空时成交后顺手过留存环（maybe_create_retention_goal）。
    """
    ts = float(now if now is not None else time.time())
    ref = str(ref or "").strip()
    if ref.lower().startswith("chat:"):
        ref = ref[5:]
    order_id = str(order_id or "").strip()[:80]
    plan = str(plan or "").strip()[:60]
    out: Dict[str, Any] = {
        "matched": False, "goal_id": None, "dup": False, "updated": False}
    if not ref:
        return out

    goal = store.find_active_goal(conversation_id=ref)
    if goal is None:
        parts = ref.split(":", 2)
        if len(parts) == 3 and parts[0].strip() and parts[1].strip():
            goal = store.find_active_goal(
                platform=parts[0].strip(), chat_key=parts[2].strip(),
                account_id=parts[1].strip())
    try:
        get_goal_stats().record_order(matched=goal is not None)
    except Exception:
        pass
    if goal is None:
        logger.info("[goal-order] 未匹配到活跃目标 ref=%s order=%s",
                    ref, order_id or "-")
        return out

    gid = str(goal.get("goal_id") or "")
    out["matched"] = True
    out["goal_id"] = gid
    # 幂等升级为**会话级**（P5）：留存环 spawn 后会话常年有活跃目标，旧单重放
    # （order_pull paid→activated 二次出现 / 重启后 _SEEN 清空）若只查当前目标
    # 的事件，会把历史单误认成续费单假结算新周期。
    if order_id and store.order_event_exists(
            order_id=order_id,
            conversation_id=str(goal.get("conversation_id") or ""),
            platform=str(goal.get("platform") or ""),
            chat_key=str(goal.get("chat_key") or "")):
        out["dup"] = True
        return out

    fields: Dict[str, Any] = {
        "status": "done", "done_at": ts, "progress": 1.0,
        "result": f"order:{plan or '-'}:{order_id or '-'}"[:200],
    }
    try:
        ms = (get_template(str(goal.get("template") or "")) or {}).get(
            "milestones") or []
        if ms:
            fields["milestone_idx"] = len(ms) - 1
    except Exception:
        pass
    if not store.update_goal_fields(gid, **fields):
        return out
    out["updated"] = True
    store.add_event(gid, "order", f"{order_id or '-'}|{plan or '-'}")
    try:
        get_goal_stats().record_terminal("done")
    except Exception:
        pass
    logger.info("[goal-order] 目标 %s 经订单回流结算 done（order=%s plan=%s）",
                gid, order_id or "-", plan or "-")
    # P5 留存环：成交即起下一周期（retention 默认关；cfg 未传=旧行为）
    if cfg_root is not None:
        maybe_create_retention_goal(
            store, cfg_root, goal, plan=plan, period=period, now=ts)
    return out


__all__ = [
    "AGENDA_STATES",
    "DEFAULT_DB_NAME",
    "NEGATIVE_EMOTIONS",
    "WON_META_NOTE_MAX",
    "WON_META_PRODUCT_MAX",
    "agenda_counts",
    "agenda_item",
    "agenda_sort_key",
    "agenda_state_match",
    "beat_feedback_state",
    "build_block_for_chat",
    "catalog_guard_facts",
    "get_configured_store",
    "goal_view",
    "goals_enabled",
    "is_negative_emotion",
    "maybe_auto_create_goal",
    "maybe_create_retention_goal",
    "maybe_spawn_reconvert",
    "refresh_goal",
    "resolve_plan_product",
    "retention_days_for_period",
    "run_winback_scan",
    "resolve_db_path",
    "resolve_goals_cfg",
    "sanitize_won_meta",
    "settle_order_ref",
    "sold_plan_counts_cached",
]
