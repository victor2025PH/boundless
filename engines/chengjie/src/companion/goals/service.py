"""目标服务编排层——routes / skill_manager 注入 / 主动桥的**同一入口**。

口径唯一性是本模块存在的理由：右栏卡显示的里程碑、prompt 注入的里程碑、
主动桥判定的拍，全部经 ``refresh_goal``（settle-on-read + 当日拍幂等规划）
产出——三个消费面读同一份结算结果，杜绝「卡片说第2步、prompt 说第3步」的漂移。

所有函数绝不抛：目标层任何失败都不能拖垮聊天/草稿/主动触达主链路。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.companion.goals import store as goal_store_mod
from src.companion.goals.context_block import goal_view_block
from src.companion.goals.ledger import settle_goal
from src.companion.goals.planner import (
    customer_active,
    effective_rejects,
    plan_beat,
)
from src.companion.goals.signals import collect_signals
from src.companion.goals.stats import get_goal_stats
from src.companion.goals.store import GoalStore, get_goal_store
from src.companion.goals.templates import (
    get_template,
    intent_en_for,
    milestone_label,
    pick_sprint_intent,
    strip_probe_clause,
)

logger = logging.getLogger("src.companion.goals.service")

_DAY = 86400.0

# 强负面情绪 hint 词表（user_context.user_emotion_hint / conversation last_emotion）
NEGATIVE_EMOTIONS = frozenset(
    ("sad", "angry", "anxious", "depressed", "negative", "fear", "tired",
     "frustrated", "grief"))

DEFAULT_DB_NAME = "marketing_goals.db"

# #166（2026-09-05）注入口「无目标 / 异常」可见性节流：同一会话内只落一行
# INFO/WARNING（拟稿链每条入站都走注入口，高频会话不节流＝刷屏）。
# M-7 B（#236）：600s → 60s。skuio 机 3h 59 稿零行的真因是 logger 名不在 src.*
# 命名空间（见 logger 定义处），节流本身没错；但 10 分钟一行在「目标到期→重建」
# 这种分钟级操作面前太粗，60s 既够诊断包逐稿归因又不刷屏。
# 键 = 原因|会话 id；有上限防长期运行撑爆（超限一次性清空重来，宁可多打一行）。
_INJECT_LOG_THROTTLE_SEC = 60.0
_INJECT_LOG_CAP = 4000
_inject_log_seen: Dict[str, float] = {}


def _inject_log_allowed(key: str, now: Optional[float] = None) -> bool:
    """会话级节流：距上次放行 ≥ ``_INJECT_LOG_THROTTLE_SEC`` 才放行。绝不抛。"""
    try:
        n = float(now if now is not None else time.time())
        k = str(key or "")
        last = _inject_log_seen.get(k)
        if last is not None and (n - last) < _INJECT_LOG_THROTTLE_SEC:
            return False
        if len(_inject_log_seen) >= _INJECT_LOG_CAP:
            _inject_log_seen.clear()
        _inject_log_seen[k] = n
        return True
    except Exception:
        return True


def _last_goal_hint(store: Any, conversation_id: str) -> str:
    """no_goal 日志行的尾注：该会话最近一条**终态**目标（M-7 B）。
    「刚到期所以查无目标」与「从没建过」在诊断包里必须能分辨。查不到/异常 → ""。"""
    try:
        if store is None or not conversation_id:
            return ""
        recent = [g for g in (store.list_goals(limit=30) or [])
                  if str(g.get("conversation_id") or "") == str(conversation_id)]
        if not recent:
            return " last_goal=none"
        g = recent[0]
        return (f" last_goal={str(g.get('goal_id') or '')[:12]}"
                f" status={g.get('status')}"
                f" done_at={int(float(g.get('done_at') or 0))}")
    except Exception:
        return ""


def resolve_inject_lookup_keys(
    *, platform: str = "", chat_key: str = "", account_id: str = "",
    conversation_id: str = "",
) -> Dict[str, str]:
    """注入口查找键归一（#166 键一致性）：右栏卡走 ``goal_routes._split_conversation_id
    (conv)`` 拆三元组，拟稿链传的是 ``(conversation_id, platform, chat_key,
    account_id)`` 四个散值——两边喂同一个 ``find_active_goal`` 却可能因「conv 有、
    account 是 ''/'default'」或「三元组齐、conv 空」而走到不同的匹配分支。

    规则（只补齐，绝不放宽账号锁）：
    - conv 形如 ``platform:account:chat_key`` → platform/chat_key 缺则从 conv 补；
      account 为空或占位 ``default`` 时**以 conv 内的账号为准**（卡片口径）；
    - conv 空而三元组齐 → 合成 ``platform:account:chat_key`` 作 conv（让精确命中
      这一层也参与，与建目标落库的 conversation_id 同形态）；
    - 调用方显式给了与 conv 不同的真实账号 → 尊重调用方（多账号同 peer 防串号）。
    纯函数、绝不抛；不满足任一规则时原样返回。
    """
    plat = str(platform or "").strip()
    ck = str(chat_key or "").strip()
    acct = str(account_id or "").strip()
    conv = str(conversation_id or "").strip()
    try:
        parts = conv.split(":", 2) if conv else []
        if len(parts) == 3 and parts[0].strip() and parts[1].strip():
            c_plat, c_acct, c_ck = (p.strip() for p in parts)
            if not plat:
                plat = c_plat
            if not ck:
                ck = c_ck
            if not acct or acct.lower() == "default":
                acct = c_acct
        elif not conv and plat and acct and ck and acct.lower() != "default":
            conv = f"{plat}:{acct}:{ck}"
    except Exception:
        pass
    return {"platform": plat, "chat_key": ck, "account_id": acct,
            "conversation_id": conv}


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


def sprint_engine_status(cfg_root: Any, *, platform: str = "") -> Dict[str, Any]:
    """目标引擎真相（#166，2026-09-05）——启动日志 / ``GET /api/goals/engine-status`` /
    右栏卡 ``sprint_live.ticker_on`` / 建目标表单 caps **同一出口**。

    「自动推进」档能不能真的自己出手，取决于一整条链而不是一个开关：
    - 限时档（today/session）：``goals.enabled`` → ``goals.sprint.enabled`` →
      ``goals.sprint.dry_run`` 关 → ``sprint.platforms`` 白名单。**D1b P0-1
      （2026-09-05）起 care 派发器的 enabled/dry_run 不再是冲刺的闸**——冲刺行
      在派发器里按 ``goal_row_policy.live`` 绕过 care 灰度门（借管线不借开关）；
      ``care_enabled``/``care_dry_run`` 仍回传供看板参考，但不进 blockers。
    - 自然档（natural）：``goals.sprint.natural_daily`` 开＝推进器自己每日一拍
      （D1b P0-3，与冲刺同链同闸）；否则回落 ``goals.bridge.enabled`` 搭
      ``proactive_topic`` 顺风车（``proactive_topic.enabled`` 关或 ``dry_run``
      开＝不真发）。
    skuio 88MP86 实锤：sprint 关、bridge 关、care dry_run 开——卡片却写「自动推进」。
    ``sprint_blockers`` / ``natural_auto_blockers`` 把每一道没过的闸点名；
    ``platform`` 非空时白名单判定计入 blockers。纯函数、绝不抛。
    """
    out: Dict[str, Any] = {
        "enabled": False, "inject_enabled": True,
        "sprint_enabled": False, "sprint_dry_run": False,
        "sprint_platforms": [], "sprint_platforms_explicit": False,
        "natural_daily": False,
        "care_enabled": False, "care_dry_run": False,
        "sprint_blockers": [], "sprint_effective": False,
        "bridge_enabled": False, "proactive_enabled": False,
        "proactive_dry_run": False,
        "natural_auto_blockers": [], "natural_auto_effective": False,
        "natural_auto_via": "",
        "platform": str(platform or "").strip().lower(),
    }
    try:
        root = cfg_root if isinstance(cfg_root, dict) else {}
        companion = root.get("companion") if isinstance(
            root.get("companion"), dict) else {}
        gcfg = resolve_goals_cfg(root)
        out["enabled"] = bool(gcfg.get("enabled", False))
        inject_cfg = gcfg.get("inject") if isinstance(gcfg.get("inject"), dict) else {}
        out["inject_enabled"] = bool(inject_cfg.get("enabled", True))
        from src.companion.goals.sprint_ticker import parse_sprint_cfg
        scfg = parse_sprint_cfg(gcfg)
        out["sprint_enabled"] = bool(scfg.get("enabled", False))
        out["sprint_dry_run"] = bool(scfg.get("dry_run", False))
        out["sprint_platforms"] = [str(p) for p in (scfg.get("platforms") or ())]
        out["sprint_platforms_explicit"] = bool(scfg.get("platforms_explicit"))
        out["natural_daily"] = bool(scfg.get("natural_daily", True))
        care = companion.get("proactive_care") if isinstance(
            companion.get("proactive_care"), dict) else {}
        out["care_enabled"] = bool(care.get("enabled", False))
        out["care_dry_run"] = bool(care.get("dry_run", False))
        bridge = gcfg.get("bridge") if isinstance(gcfg.get("bridge"), dict) else {}
        out["bridge_enabled"] = bool(bridge.get("enabled", False))
        pt = companion.get("proactive_topic") if isinstance(
            companion.get("proactive_topic"), dict) else {}
        out["proactive_enabled"] = bool(pt.get("enabled", False))
        out["proactive_dry_run"] = bool(pt.get("dry_run", False))

        # 冲刺链（D1b P0-1 起 care 的 enabled/dry_run 不再入闸：冲刺行 live）
        sb: List[str] = []
        if not out["enabled"]:
            sb.append("goals_disabled")
        if not out["sprint_enabled"]:
            sb.append("sprint_disabled")
        elif out["sprint_dry_run"]:
            sb.append("sprint_dry_run")
        if out["platform"] and out["platform"] not in out["sprint_platforms"]:
            sb.append("platform")
        out["sprint_blockers"] = sb
        out["sprint_effective"] = not sb

        # 自然档：推进器每日拍（与冲刺同链同闸）优先；关了才看 bridge 顺风车
        nb: List[str] = []
        if out["sprint_enabled"] and out["natural_daily"]:
            out["natural_auto_via"] = "daily"
            nb = list(sb)
        else:
            out["natural_auto_via"] = "bridge"
            if not out["enabled"]:
                nb.append("goals_disabled")
            if not out["bridge_enabled"]:
                nb.append("bridge_disabled")
            if not out["proactive_enabled"]:
                nb.append("proactive_disabled")
            elif out["proactive_dry_run"]:
                nb.append("proactive_dry_run")
        out["natural_auto_blockers"] = nb
        out["natural_auto_effective"] = not nb
    except Exception:
        logger.debug("sprint_engine_status failed", exc_info=True)
    return out


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


# ── C：工作链 × 目标弱联动（链生命周期回写目标事件台账）──────────────────────

def record_chain_event(
    cfg_root: Any,
    config_path: Any,
    conversation_id: str,
    kind: str,
    detail: str = "",
) -> bool:
    """把工作链生命周期（chain_started/completed/failed/cancelled）记到当前会话
    **活跃目标**的事件台账（goal_events）——目标详情 ``/api/goals/{id}`` 的
    ``events`` 即刻可见「这个目标进行期间 SOP 干了什么」。

    弱联动边界（刻意）：只写台账、不改目标状态/进度/拍——链完成≠目标推进，
    两套调度语义不合并。无活跃目标 / goals 未启用 / 任何异常 → False 静默，
    绝不影响链主流程。**取库必须经 get_configured_store**：裸调 get_goal_store()
    会在 goals 未初始化时把进程单例毒化成 :memory:（后续真数据全进内存）。
    """
    try:
        cfg = cfg_root or {}
        if not goals_enabled(cfg):
            return False
        store = get_configured_store(cfg, config_path)
        ref = str(conversation_id or "").strip()
        if not ref:
            return False
        goal = store.find_active_goal(conversation_id=ref)
        if goal is None:
            parts = ref.split(":", 2)
            if len(parts) == 3 and parts[0].strip() and parts[1].strip():
                goal = store.find_active_goal(
                    platform=parts[0].strip(), chat_key=parts[2].strip(),
                    account_id=parts[1].strip())
        if not goal:
            return False
        store.add_event(str(goal.get("goal_id") or ""), str(kind or "chain"),
                        str(detail or "")[:200])
        return True
    except Exception:
        return False


def chain_event_recorder(config_manager: Any):
    """从 config_manager 构造 ``(conversation_id, kind, detail) -> bool`` 钩子
    （WorkflowRunner / 路由注入用；配置在调用时刻现读，热更新自然生效）。"""
    def _hook(conversation_id: str, kind: str, detail: str = "") -> bool:
        try:
            return record_chain_event(
                getattr(config_manager, "config", None) or {},
                getattr(config_manager, "config_path", None),
                conversation_id, kind, detail)
        except Exception:
            return False
    return _hook


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
    inbound_turn: bool = False,
) -> Dict[str, Any]:
    """结算 + 当日拍规划的单一入口。返回
    ``{"goal": 最新行, "action": 今日拍|None, "hold": 原因|None}``。绝不抛。

    ``inbound_turn``（#65 C1）：本次调用由对方**本条入站**触发（注入链传
    ``inbound_text`` 非空）。inbox 侧读不到入站时刻（standalone / 镜像未开 /
    store 未就绪）时把「对方刚开口」锚在 ``now``——否则 session 档整段只有
    ``s:0`` 一个槽、退避计数把历史全部算成「没回还在推」，两三拍后永久 hold。
    inbox 有值时一律以 inbox 为准（不动既有口径）。
    """
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
        if inbound_turn and float(signals.last_inbound_ts or 0) <= 0:
            signals.last_inbound_ts = n
        # 节奏档在结算前就要知道（#65 C1）：限时档的进度按「已出手的拍」爬，
        # 结算器要吃 sprint_beats/sprint_cap 信号；natural 不带这两个键 → 结算
        # 器零感知（回归钉：natural 行为逐字不变）。
        from src.companion.goals.pace import (
            DEFAULT_ESCALATE_AT,
            count_beats_for_cap,
            effective_beat_cap,
            is_sprint,
            planner_thresholds,
            remaining_sec as _rem_sec,
            resolve_pace,
            slot_key,
            sprint_push,
            total_sec as _tot_sec,
        )
        pace = resolve_pace(goal)
        sprint = is_sprint(pace)
        # P1 推进力加度：冲刺配置覆写 + 全力模式（params.sprint_mode，用户逐
        # 目标显式拍板）+ 剩余占比（收口升档判据）。
        scfg: Dict[str, Any] = {}
        sprint_mode = ""
        if sprint:
            try:
                from src.companion.goals.sprint_ticker import parse_sprint_cfg
                scfg = parse_sprint_cfg(resolve_goals_cfg(cfg_root))
            except Exception:
                scfg = {}
            sprint_mode = str(
                (goal.get("params") or {}).get("sprint_mode") or "")
        # 画像双轨完成度（P1）：模板声明 profile_slots → 读画像卡 → signals.extras
        # （acquire_and_convert 结算据 bant_fill 推「摸底完成」里程碑）。
        if template.get("profile_slots"):
            try:
                from src.companion.goals.profile_slots import (
                    fill_rates,
                    parse_selected_slots,
                    selected_fill_rate,
                )
                prof = store.get_customer_profile(
                    str(goal.get("platform") or ""),
                    str(goal.get("chat_key") or ""))
                _pf = (prof or {}).get("fields") or {}
                rates = fill_rates(_pf)
                signals.extras["bant_fill"] = float(rates.get("bant") or 0.0)
                signals.extras["relation_fill"] = float(
                    rates.get("relation") or 0.0)
                # P26 摸底目标：勾选槽位填充率（-1=没勾选，ledger 按信号缺失
                # 处理）——「客户说了职业」当轮变成里程碑推进/完成判定
                _sel = parse_selected_slots(
                    (goal.get("params") or {}).get("slots"))
                if _sel:
                    signals.extras["selected_fill"] = selected_fill_rate(
                        _pf, _sel)
            except Exception:
                logger.debug("profile fill rates skipped", exc_info=True)
        stats = get_goal_stats()

        # 「已开价」证据：direct 拍进入过生成/发出（转化模板里程碑 3 的推进条件）
        # + 已出手拍数（#65 C1：限时档进度信号——consumed/sent 且非陪伴 none 拍）
        direct_engaged = False
        engaged_beats = 0
        try:
            for a in store.list_actions(gid, limit=60):
                if str(a.get("status")) not in ("consumed", "sent"):
                    continue
                lvl = str(a.get("push_level") or "")
                if lvl != "none":
                    engaged_beats += 1
                if lvl == "direct":
                    direct_engaged = True
        except Exception:
            direct_engaged = False
            engaged_beats = 0
        if sprint:
            signals.extras["sprint_beats"] = engaged_beats
            signals.extras["sprint_cap"] = effective_beat_cap(
                pace, overrides=scfg, mode=sprint_mode)

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
            # M-7 C（#236）：到期/失守/完成的**那一刻**结算——摘要落事件 + 工作台
            # 事件（不经日报聚合门槛）。best-effort，绝不拖垮结算主路。
            if (res["status"] != old_status
                    and res["status"] in ("done", "failed", "expired")):
                try:
                    from src.companion.goals.notify import settle_and_notify
                    settle_and_notify(
                        store, goal, cfg_root=cfg_root,
                        inbox_store=inbox_store, now=n)
                except Exception:
                    logger.debug("settle_and_notify skipped", exc_info=True)
            # P7 回流再转化：挽回目标在此转 done（=对方回话）→ 顺势起转化目标。
            # 门控在 maybe_spawn_reconvert 内（created_by/开关），非挽回目标零开销。
            if res["status"] == "done" and old_status == "active":
                try:
                    maybe_spawn_reconvert(store, cfg_root, goal, now=n)
                except Exception:
                    logger.debug("reconvert hook skipped", exc_info=True)
            # 冲刺插队回程票：任意终态（含 expired）恢复被暂停的长线目标
            if (res["status"] in ("done", "failed", "expired")
                    and old_status == "active"):
                try:
                    maybe_resume_linked_goal(store, goal)
                except Exception:
                    logger.debug("linked resume skipped", exc_info=True)
        out["goal"] = goal

        if str(goal.get("status")) != "active":
            return out

        # 拍槽位：natural=日历日；today=小时；session=对方本条入站。
        # 限时档绝不能再走「每天一拍」——60 分钟目标否则整段只有一拍、到期必 expired。
        # （pace / sprint / scfg / sprint_mode 已在结算前算好，见上）
        rem_ratio: Optional[float] = None
        closing = False
        if sprint:
            _tot = _tot_sec(goal)
            if _tot > 0:
                rem_ratio = _rem_sec(goal, n) / _tot
                closing = rem_ratio < float(
                    scfg.get("escalate_at") or DEFAULT_ESCALATE_AT)
        day = slot_key(pace, n, float(signals.last_inbound_ts or 0),
                       closing=closing)
        action = store.get_action(gid, day)
        if action is None:
            cfg = resolve_goals_cfg(cfg_root)
            pl = (cfg.get("planner") or {}) if isinstance(cfg, dict) else {}
            existing = store.list_actions(gid, limit=120)
            n_existing = count_beats_for_cap(existing, pace, n) if sprint else 0
            cap = (effective_beat_cap(pace, overrides=scfg, mode=sprint_mode)
                   if sprint else 0)
            if cap and n_existing >= cap:
                # M-7 B（#236 取证问 4）：拍数上限**只管主动出手**。全仓只有这里读
                # cap，而它此前把客户来消息时的顺势带方向也一并掐断——「白天 2/1 拍」
                # 超额后 AI 回复里连方向都没了，卡上却写着自动推进。现在：
                # - inbound_turn（reply/draft 链，客户刚说话）→ 不 hold，照常带方向
                #   （不再新开计数拍：复用/新建当日槽只为让块有「今日意图」）；
                # - 主动链 / 系统调用 → 仍 hold=pace_cap，并记 beat_blocked(pace_cap@槽)
                #   （按槽位去重），卡片「今天被拦 N 次」能看见。
                if not inbound_turn:
                    stats.record_hold("pace_cap")
                    out["hold"] = "pace_cap"
                    try:
                        from src.companion.goals.sprint_ticker import (
                            record_beat_blocked,
                        )
                        record_beat_blocked(
                            store, gid, "pace_cap", slot=day,
                            conversation_id=str(goal.get("conversation_id") or ""),
                            now=n)
                    except Exception:
                        logger.debug("pace_cap beat_blocked skipped", exc_info=True)
                    return out
                out["cap_reached"] = True
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
            th = planner_thresholds(pace, overrides=scfg, mode=sprint_mode)
            try:
                backoff = int(th.get(
                    "backoff_after", pl.get("backoff_after_unanswered", 2) or 0))
            except (TypeError, ValueError):
                backoff = 2
            try:
                halt = int(th.get(
                    "halt_after", pl.get("halt_after_unanswered", 4) or 0))
            except (TypeError, ValueError):
                halt = 4
            # O-3 B（#236）：摸底目标把「今天至少问一个：<未填槽>」钉进计划（第 1 天也钉）
            probe_asks = discovery_probe_asks(store, template, goal, inbox_store=inbox_store)
            active = customer_active(signals.extras.get("inbound_30m", 0))
            beat = plan_beat(
                template=template, goal=goal, signals=signals, day=day,
                engaged_since_inbound=engaged,
                backoff_after=backoff,
                halt_after=halt,
                recent_rejects=rejects,
                probe_asks=probe_asks,
                active=active,
            )
            if beat is None or beat.get("hold"):
                reason = str((beat or {}).get("hold") or "no_intent")
                stats.record_hold(reason)
                out["hold"] = reason
                return out
            push = str(beat.get("push_level") or "soft")
            intent = str(beat.get("intent") or "")
            if template.get("gap_in_intent"):
                _start = float(goal.get("start_ts") or goal.get("created_at") or n)
                logger.info(
                    "[goal-plan] goal=%s conv=%s day=%d probes=%d slots=%s active=%s "
                    "inbound_30m=%s advanced=%s push=%s",
                    gid[:12], str(goal.get("conversation_id") or ""),
                    int((n - _start) // _DAY) + 1, int(beat.get("probes") or 0),
                    list(beat.get("probe_slots") or []), active,
                    signals.extras.get("inbound_30m", 0),
                    bool(beat.get("advanced")), push)
            if sprint:
                push = sprint_push(
                    pace, n_existing, push,
                    remaining_ratio=rem_ratio,
                    escalate_at=float(
                        scfg.get("escalate_at") or DEFAULT_ESCALATE_AT),
                    mode=sprint_mode)
                # 限时档意图换 sprint 池（按拍序）：日历池的「隔天补一句/
                # 改天再聊」在 60 分钟目标里穿帮。push=none（退避陪伴日）
                # 不换——陪伴意图与节奏无关；无 sprint 池的模板保持原意图。
                # close（收口升档）钉收口池（99 夹到末段）——力度收口而意图
                # 还在「先探探兴趣」是自相矛盾的稿。
                if push != "none":
                    si = pick_sprint_intent(
                        template,
                        99 if push == "close" else n_existing,
                        gid, day,
                        params=goal.get("params") or {})
                    if si:
                        intent = si
            action = store.upsert_action(
                gid, day, intent=intent, push_level=push, now=n)
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
    from src.companion.goals.pace import remaining_sec, resolve_pace, total_sec
    pace = resolve_pace(goal)
    if start > 0 and deadline > start:
        total_days = int(round((deadline - start) / _DAY))
    else:
        total_days = int(template.get("default_days") or 0)
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
        # P22：来源标识（auto_create / winback_auto / retention_auto / agent / …）
        # UI「AI 自建」徽标据此显隐；缺省空串=旧库兼容。
        "created_by": str(goal.get("created_by") or ""),
        "priority": int(goal.get("priority") or 1),
        "milestone_idx": mi,
        "milestone_label": milestone_label(template, mi, lang),
        "milestones": [dict(m) for m in (template.get("milestones") or [])],
        "progress": float(goal.get("progress") or 0.0),
        "day_index": day_index,
        "total_days": total_days,
        "pace": pace,
        "remaining_sec": round(remaining_sec(goal, n), 1),
        "total_sec": round(total_sec(goal), 1),
        "start_ts": start,
        "deadline_ts": deadline,
        "result": str(goal.get("result") or ""),
        # 终局时刻（done/failed/expired 统一落在 done_at；0=未终局或旧行）——
        # 终局卡「用时 X 天」显示用（P0 2026-08-18），additive 字段零消费方破坏。
        "done_at": float(goal.get("done_at") or 0),
        "created_at": float(goal.get("created_at") or 0),
        "updated_at": float(goal.get("updated_at") or 0),
        "hold": hold,
        "today": None,
    }
    if action is not None:
        _intent = str(action.get("intent") or "")
        view["today"] = {
            "action_id": str(action.get("action_id") or ""),
            "day": str(action.get("day") or ""),
            "intent": _intent,
            # i18n P0（2026-08-19）：英文展示态文案（同池反查；匹配不到=""，
            # 前端回落中文原文）。intent 本体保持中文权威口径不动。
            "intent_en": intent_en_for(template, goal.get("params") or {}, _intent),
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
        # additive（i18n P0）：英文展示态；旧消费方不读此键零影响
        "intent_en": str((today or {}).get("intent_en") or ""),
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
WON_META_OUTCOME_MAX = 40


def sanitize_won_meta(raw: Any) -> Dict[str, Any]:
    """坐席「标成交 / 标记达成」的可选归因 meta → 消毒后的紧凑 dict（未知键一律丢弃）。

    - ``product``：卖了什么（截 80 字）
    - ``amount``：金额（**可选**，不逼坐席填；负数/非数/NaN 一律丢弃；
      整数值落成 int 让 JSON 更紧凑）
    - ``outcome``：达成结果标签（N-3 #241 陪伴域「标记达成」：关系升温 / 见面 /
      转付费陪伴 / 其他…，截 40 字；销售域也可带）
    - ``note``：备注（截 200 字）
    非 dict / 各项全空 → ``{}``（调用方据此决定要不要落事件）。绝不抛。
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    try:
        product = str(raw.get("product") or "").strip()[:WON_META_PRODUCT_MAX]
        if product:
            out["product"] = product
        outcome = str(raw.get("outcome") or "").strip()[:WON_META_OUTCOME_MAX]
        if outcome:
            out["outcome"] = outcome
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


def _maybe_auto_settle_outcome(
    store: GoalStore,
    cfg_root: Any,
    goal: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """冲刺目标的达成信号自动结算（P2 2026-08-30，``sprint.auto_settle_contact``
    默认关）。前置：目标是限时档 + 冲刺功能开 + 信号已写进 params。

    成功 → 返回最新目标行（status=done，result=``signal:<kind>:<v>``）；
    不满足/失败 → None（提示行为不变，确认权仍在人）。绝不抛。"""
    try:
        if not isinstance(goal, dict):
            return None
        from src.companion.goals.pace import is_sprint, resolve_pace
        if not is_sprint(resolve_pace(goal)):
            return None
        from src.companion.goals.sprint_ticker import parse_sprint_cfg
        scfg = parse_sprint_cfg(resolve_goals_cfg(cfg_root))
        if not (scfg.get("enabled") and scfg.get("auto_settle_contact")):
            return None
        gid = str(goal.get("goal_id") or "")
        sig = (goal.get("params") or {}).get("outcome_signal")
        if not gid or not isinstance(sig, dict) or not sig.get("v"):
            return None
        n = float(now if now is not None else time.time())
        fields: Dict[str, Any] = {
            "status": "done", "done_at": n, "progress": 1.0,
            "result": (f"signal:{str(sig.get('kind') or 'contact')}:"
                       f"{str(sig.get('v'))[:60]}")[:200],
        }
        try:
            ms = (get_template(str(goal.get("template") or "")) or {}).get(
                "milestones") or []
            if ms:
                fields["milestone_idx"] = len(ms) - 1
        except Exception:
            pass
        if not store.update_goal_fields(gid, **fields):
            return None
        store.add_event(gid, "status", "active->done:signal_auto")
        try:
            get_goal_stats().record_terminal("done")
            get_goal_stats().record_sprint_auto_settled()
        except Exception:
            pass
        logger.info("[goal-sprint] 达成信号自动结算 done：%s（%s）",
                    gid, fields["result"][:60])
        fresh = store.get_goal(gid) or dict(goal, **fields)
        # 冲刺插队闭环：终态即恢复被暂停的长线目标（有链接才动）
        maybe_resume_linked_goal(store, fresh)
        return fresh
    except Exception:
        logger.debug("_maybe_auto_settle_outcome failed", exc_info=True)
        return None


def maybe_resume_linked_goal(store: GoalStore, goal: Dict[str, Any]) -> bool:
    """冲刺插队的回程票（P2/P3 2026-08-30）：目标带 ``params.resume_goal_id``
    且已到任意终态 → 把那条 **paused** 的长线目标恢复 active。

    只认 paused（人工又动过的不碰）；恢复与否都不影响本目标终态。绝不抛。"""
    try:
        if not isinstance(goal, dict):
            return False
        if str(goal.get("status") or "") not in (
                "done", "failed", "expired", "cancelled"):
            return False
        rid = str((goal.get("params") or {}).get("resume_goal_id") or "").strip()
        if not rid:
            return False
        linked = store.get_goal(rid)
        if linked is None or str(linked.get("status") or "") != "paused":
            return False
        if not store.update_goal_fields(rid, status="active"):
            return False
        store.add_event(rid, "status", "paused->active:sprint_return")
        store.add_event(str(goal.get("goal_id") or ""), "linked_resume", rid)
        logger.info("[goal-sprint] 冲刺终局，恢复长线目标 %s", rid)
        return True
    except Exception:
        logger.debug("maybe_resume_linked_goal failed", exc_info=True)
        return False


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
        # 自聊排除（P4 2026-08-09 实锤：auto_create 把账号自己的收藏消息
        # （chat_key='me'）当新好友建了获客转化目标——演练/自检消息都发那里，
        # 自己不是获客对象）。
        if str(chat_key or "").strip().lower() == "me":
            return None
        # 夜跑演练号段（990001xxx）排除（2026-09-13 实锤：duel 假会话被当成
        # 新好友建了 auto 获客目标 → 拍排了发不出 → 运维群「服务器常备循环停摆」）。
        try:
            from src.utils.case_center import is_drill_uid
            if (is_drill_uid(str(chat_key or ""))
                    or is_drill_uid(str(conversation_id or ""))
                    or is_drill_uid(str(uc.get("chat_id") or ""))):
                return None
        except Exception:
            pass
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
        if _lifecycle_template_blocked(cfg_root, template_id, "auto_create"):
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


def merged_beat_intent(intent: str, gap: str) -> str:
    """P27 意图×缺口合流（纯函数）：把「本轮最优先缺口」并进今日意图。

    「今日意图」是 opener 转向（goal_applied.intent）与主动桥 directive 唯一
    携带的载荷——缺口只挂独立的【画像缺口】行到不了那两处，摸底目标的
    开场/主动触达就只剩泛化模式没有具体方向。合流后独立缺口行由调用方
    取消（防同块重复）。gap 空 → 原样返回。"""
    g = str(gap or "").strip()
    # O-3 B：planner 把「今天至少自然问一个：<今日目标槽>」钉在今日意图上（卡片给人看）；
    # 注入链按**本轮**缺口（逐轮轮换 / 线索词命中）重新合流——先剥掉计划子句，一轮只带
    # 一个问法，不出现「今天问坐标」「本轮问职业」两句打架。
    it = strip_probe_clause(str(intent or "").strip())
    if not g:
        return it
    tail = f"本轮顺势了解：{g}（像朋友闲聊，不要像查户口，一轮只问一个）"
    return f"{it}；{tail}" if it else tail


def discovery_gap_for_goal(
    store: Any, template: Dict[str, Any], goal: Dict[str, Any],
    *, lang: str = "zh",
) -> str:
    """摸底类目标（模板声明 ``gap_in_intent``）当前最优先的单个缺口短语；
    非摸底/无勾选/全填/异常 → ""。注入链与主动桥共用（同源保证两处
    「本轮问什么」永远一致）。"""
    try:
        if not template.get("gap_in_intent"):
            return ""
        from src.companion.goals.profile_slots import (
            parse_selected_slots,
            resolve_inject_gap,
        )
        sel = parse_selected_slots((goal.get("params") or {}).get("slots"))
        if not sel:
            return ""
        prof = store.get_customer_profile(
            str(goal.get("platform") or ""), str(goal.get("chat_key") or ""))
        phrase, _key, _patch = resolve_inject_gap(
            dict((prof or {}).get("fields") or {}),
            goal.get("params") or {},
            include=sel, lang=lang)
        return phrase
    except Exception:
        logger.debug("discovery_gap_for_goal failed", exc_info=True)
        return ""


def recent_history(
    inbox_store: Any, conversation_id: str, *, limit: int = 30,
) -> List[Dict[str, Any]]:
    """最近 N 条会话消息 ``[{direction, text, ts}]``（时间正序；Q-1 A 三态扫描 / 深化判定
    共用）。无 inbox / 无会话 / 异常 → []。绝不抛。"""
    conv = str(conversation_id or "").strip()
    if inbox_store is None or not conv:
        return []
    try:
        rows = inbox_store.list_recent_messages(conv, limit=max(1, int(limit or 30))) or []
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        t = str(m.get("text") or m.get("content") or "").strip()
        if not t:
            continue
        try:
            ts = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        out.append({"direction": str(m.get("direction") or "in"), "text": t, "ts": ts})
    return out


# ── Q-1 E（#264）：「暂停全部摸底目标」总开关 + 槽位人工确认 ──────────────────────
# 总开关落 InboxStore app_settings KV（goals 库 store.py 默认不碰）；开着 → 一切带 profile_slots
# 的目标不注入（reason=discovery_paused），卡片 / 计划句照常显示。无 InboxStore → 视为未暂停。
DISCOVERY_PAUSE_KEY = "goals:discovery_paused"


def _kv_store(inbox_store: Any = None) -> Any:
    if inbox_store is not None:
        return inbox_store
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def discovery_paused(inbox_store: Any = None) -> bool:
    """总开关现状（KV ``goals:discovery_paused`` == "1"）。异常 / 无 store → False。"""
    st = _kv_store(inbox_store)
    if st is None or not hasattr(st, "get_app_setting"):
        return False
    try:
        return str(st.get_app_setting(DISCOVERY_PAUSE_KEY, "") or "").strip() in ("1", "true", "on")
    except Exception:
        return False


def set_discovery_paused(paused: bool, *, inbox_store: Any = None, by: str = "") -> bool:
    st = _kv_store(inbox_store)
    if st is None or not hasattr(st, "set_app_setting"):
        return False
    try:
        ok = bool(st.set_app_setting(DISCOVERY_PAUSE_KEY, "1" if paused else "0",
                                     updated_by=str(by or "goal_routes")))
        logger.info("[goal-inject] discovery_paused=%s by=%s", int(bool(paused)), by or "-")
        return ok
    except Exception:
        logger.debug("set_discovery_paused failed", exc_info=True)
        return False


def confirm_profile_slot(
    store: Any, platform: str, chat_key: str, slot: str, *, value: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """坐席点「确认」：槽位从 mentioned → confirmed（旧形 cell ``src=agent``，接口约定①
    ``cell_view`` 读作 confirmed）。``value`` 缺省用现值；无现值且未给值 → ``{"ok": False,
    "reason": "no_value"}``。store 同值覆盖会被跳过，故先清再写两步。"""
    from src.companion.goals.profile_slots import cell_view, get_slot, slot_value
    k = str(slot or "").strip().lower()
    if get_slot(k) is None:
        return {"ok": False, "reason": "unknown_slot"}
    prof = store.get_customer_profile(platform, chat_key) or {}
    fields = dict(prof.get("fields") or {})
    v = str(value or "").strip()[:80] or slot_value(fields, k)
    if not v:
        return {"ok": False, "reason": "no_value"}
    try:
        store.upsert_customer_profile(platform, chat_key, {k: ""}, source="agent",
                                      overwrite=True, now=now)
        store.upsert_customer_profile(platform, chat_key, {k: v}, source="agent",
                                      overwrite=True, now=now)
    except Exception:
        logger.debug("confirm_profile_slot write failed", exc_info=True)
        return {"ok": False, "reason": "write_failed"}
    after = (store.get_customer_profile(platform, chat_key) or {}).get("fields") or {}
    _v, _src, state = cell_view(after.get(k))
    logger.info("[goal-inject] slot_confirm plat=%s chat=%s slot=%s state=%s", platform, chat_key, k, state)
    return {"ok": state == "confirmed", "slot": k, "value": v, "state": state}


def discovery_probe_asks(
    store: Any, template: Dict[str, Any], goal: Dict[str, Any],
    *, lang: str = "zh", inbox_store: Any = None,
) -> List[str]:
    """摸底类目标当前**全部 unknown** 勾选槽位的问法（勾选序＝优先级；已问过仍空的排后）。
    planner 据此把「今天至少自然问一个：<第一个>」钉进今日意图（O-3 B #236）。
    Q-1 A（#264）：给了 ``inbox_store`` 时按最近 30 轮三态过滤——mentioned / confirmed 的槽
    不进计划句（「今天至少问一个：做什么工作」钉在今日意图上，客户昨晚刚说过就是打脸）。
    非摸底 / 无勾选 / 全填 / 异常 → []。"""
    try:
        if not template.get("gap_in_intent"):
            return []
        from src.companion.goals.profile_slots import (
            SLOT_STATE_UNKNOWN,
            asked_slot_keys,
            inject_ask,
            missing_slots,
            parse_selected_slots,
            slot_states,
        )
        sel = parse_selected_slots((goal.get("params") or {}).get("slots"))
        if not sel:
            return []
        prof = store.get_customer_profile(
            str(goal.get("platform") or ""), str(goal.get("chat_key") or ""))
        fields = dict((prof or {}).get("fields") or {})
        asked = asked_slot_keys(goal.get("params") or {})
        miss = missing_slots(fields, include=sel, limit=32, exclude=asked)
        keys = [str(m.get("key") or "") for m in miss if str(m.get("key") or "")]
        if inbox_store is not None and keys:
            hist = recent_history(inbox_store, str(goal.get("conversation_id") or ""))
            if hist:
                st = slot_states(keys, fields, hist)
                keys = [k for k in keys if st.get(k, ("unknown", ""))[0] == SLOT_STATE_UNKNOWN]
        return [inject_ask(k, lang) for k in keys]
    except Exception:
        logger.debug("discovery_probe_asks failed", exc_info=True)
        return []


# ── O-3 C（#236 HM7XBA）：注入变硬——线索词定槽 + 出站校验 + 下轮重试 ──────────────
PROBE_PENDING_PARAM = "_probe_pending"
INJECT_COUNT_PARAM = "_inject_count"
PROBE_PENDING_TTL_SEC = 86400.0
PROBE_EVENT_ASKED = "probe_asked"
PROBE_EVENT_MISSED = "probe_missed"
PROBE_EVENT_COVERED = "probe_covered"
# Q-1 B（#264 T9KN8X）：每槽每日 missed 上限——O-3 C 的 retry 无上限是「十小时 asked 8 次」的
# 机械根源；到上限该槽当日休眠（不 retry / 不 cue / 不 floor）。
PROBE_RETRY_PER_DAY = 2


def _local_day_start(now: float) -> float:
    lt = time.localtime(now)
    return now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def verify_pending_probe(
    store: Any, goal: Dict[str, Any], *, inbox_store: Any, now: float,
) -> Dict[str, Any]:
    """上一轮硬注入（``params._probe_pending``）的问句到底问出去没有——按会话里
    **该时刻之后的出站消息**判：有问句 + 带该槽问法关键词 → ``asked``；出站了但没问
    → ``missed``（本轮同槽重试）；还没出站 → ``pending``（保留）；超 24h → ``expired``
    （静默丢弃）；没有挂起 → ``none``。

    这是 goals 包唯一能看到出站正文的钩子（回复主链不属本线文件），所以校验落在
    **下一次**注入时刻——日志 ``[goal-inject] target= cue= result=asked|missed`` 随之出。
    绝不抛；返回 ``{"result", "slot", "cue", "patch"(params 去掉挂起)}``。"""
    out: Dict[str, Any] = {"result": "none", "slot": "", "cue": "", "patch": None,
                           "reply_head": ""}
    try:
        params = dict((goal or {}).get("params") or {})
        pend = params.get(PROBE_PENDING_PARAM)
        if not isinstance(pend, dict):
            return out
        slot = str(pend.get("slot") or "").strip()
        cue = str(pend.get("cue") or "").strip()
        ts = float(pend.get("ts") or 0)
        out.update(slot=slot, cue=cue)
        cleared = dict(params)
        cleared.pop(PROBE_PENDING_PARAM, None)
        if not slot or ts <= 0 or (now - ts) > PROBE_PENDING_TTL_SEC:
            out.update(result="expired", patch=cleared)
            return out
        conv = str(goal.get("conversation_id") or "").strip()
        if inbox_store is None or not conv:
            out["result"] = "pending"
            return out
        try:
            msgs = inbox_store.list_recent_messages(conv, limit=20) or []
        except Exception:
            msgs = []
        outs: List[str] = []
        for m in msgs:
            if not isinstance(m, dict) or str(m.get("direction") or "") != "out":
                continue
            try:
                mts = float(m.get("ts") or 0)
            except (TypeError, ValueError):
                mts = 0.0
            if mts > ts:
                body = str(m.get("text") or m.get("content") or "").strip()
                if body:
                    outs.append(body)
        if not outs:
            out["result"] = "pending"
            return out
        from src.companion.goals.profile_slots import reply_asks_slot, reply_covers_slot
        text = " ".join(outs)
        asked = reply_asks_slot(text, slot)
        # Q-1 B（#264）：没问出来但回复顺着该槽话头聊了（cue=job → 回复带 job / work /
        # carpenter）→ covered：不计 missed、不触发 retry（O-3 C 这里把「AI 自己说
        # it's a job… convention center」也判 missed→retry）
        covered = (not asked) and reply_covers_slot(text, slot)
        result = "asked" if asked else ("covered" if covered else "missed")
        out.update(result=result, patch=cleared, reply_head=text[:120])
        gid = str(goal.get("goal_id") or "")
        store.add_event(
            gid, {"asked": PROBE_EVENT_ASKED, "covered": PROBE_EVENT_COVERED}.get(
                result, PROBE_EVENT_MISSED),
            f"{slot}@{cue or '-'}", conversation_id=conv,
            text_head=text[:120], now=now)
        if asked:
            # A 段的让位钥匙：当日拍 detail=asked:<slot>（consumed≠asked）
            try:
                from src.companion.goals.planner import day_key
                from src.companion.goals.sprint_ticker import PROBE_ASKED_DETAIL_PREFIX
                row = store.get_action(gid, day_key(now))
                if row and str(row.get("status") or "") in ("planned", "consumed"):
                    store.mark_action(str(row.get("action_id") or ""),
                                      str(row.get("status") or "consumed"),
                                      detail=f"{PROBE_ASKED_DETAIL_PREFIX}{slot}")
            except Exception:
                logger.debug("mark asked action skipped", exc_info=True)
        return out
    except Exception:
        logger.debug("verify_pending_probe failed", exc_info=True)
        return out


PROBE_EVENT_DEEPEN = "probe_deepen"


def _event_detail_slots_today(
    store: Any, goal_id: str, kind: str, now: float,
) -> Dict[str, int]:
    """今日某类事件按 detail 首段（``slot@cue`` 的 slot）计数 → ``{slot: n}``。绝不抛。"""
    out: Dict[str, int] = {}
    try:
        rows = store.list_events(str(goal_id or ""), limit=120, kinds=(kind,),
                                 since_ts=_local_day_start(now))
    except Exception:
        return out
    for e in rows or []:
        s = str((e or {}).get("detail") or "").split("@", 1)[0].strip().lower()
        if s:
            out[s] = out.get(s, 0) + 1
    return out


def decide_probe_target(
    store: Any, goal: Dict[str, Any], *, prof_fields: Dict[str, Any],
    sel_slots: List[str], inbound_text: str, retry_slot: str, now: float,
    history: Any = None,
) -> Tuple[str, str, str, List[Tuple[str, str]], Dict[str, Tuple[str, str]]]:
    """本轮硬注入目标 ``(slot, cue, mode, cues, states)``。

    Q-1 A（#264 #269）：候选只来自 **unknown** 槽（三态见 ``profile_slots.slot_state``：
    字段 + 最近 30 轮客户 / 我方文本）——mentioned 只许「深化式」提法（mode=deepen，每槽
    每日 ≤1、只跟线索）、confirmed 永不问。O-3 C 的「问过的加回末尾」已撤：``asked_slot_keys``
    只排序，不再把问过的槽补回候选；所以「已问过 + 无线索」不会再被 floor 兜回来。
    不必问 → ``("", "", "", cues, states)``。绝不抛。"""
    empty: Dict[str, Tuple[str, str]] = {}
    try:
        from src.companion.goals.profile_slots import (
            SLOT_STATE_MENTIONED,
            SLOT_STATE_UNKNOWN,
            asked_slot_keys,
            detect_cues,
            missing_slots,
            pick_probe_target,
            slot_states,
        )
        all_unfilled = [str(m.get("key") or "") for m in missing_slots(
            prof_fields, include=sel_slots, limit=32) if m.get("key")]
        states = slot_states(list(sel_slots or []), prof_fields, history)
        if not all_unfilled:
            return "", "", "", [], states
        unknown = [k for k in all_unfilled
                   if states.get(k, (SLOT_STATE_UNKNOWN, ""))[0] == SLOT_STATE_UNKNOWN]
        mentioned = [k for k in all_unfilled
                     if states.get(k, ("", ""))[0] == SLOT_STATE_MENTIONED]
        asked = set(asked_slot_keys(goal.get("params") or {}))
        # 只排序（问过的排后），**不**把问过的加回末尾——那一行是 O-3 C 的病根
        ordered = [k for k in unknown if k not in asked] + [k for k in unknown if k in asked]
        gid = str(goal.get("goal_id") or "")
        cues = detect_cues(inbound_text, slots=all_unfilled)
        asked_today = int(store.count_events_since(
            gid, PROBE_EVENT_ASKED, _local_day_start(now)) or 0) > 0
        # B 段：每槽每日 missed ≤2——连续两次没问出来当日休眠（不再 retry、不再 floor / cue）
        missed_today = _event_detail_slots_today(store, gid, PROBE_EVENT_MISSED, now)
        awake = [k for k in ordered if missed_today.get(k, 0) < PROBE_RETRY_PER_DAY]
        r = str(retry_slot or "").strip()
        if r and missed_today.get(r, 0) >= PROBE_RETRY_PER_DAY:
            r = ""
        deepened_today = _event_detail_slots_today(store, gid, PROBE_EVENT_DEEPEN, now)
        deepen_ok = [k for k in mentioned if deepened_today.get(k, 0) < 1]
        slot, cue, mode = pick_probe_target(
            cues=cues, unfilled=awake, asked_today=asked_today,
            retry_slot=r, mentioned=mentioned, deepen_ok=deepen_ok)
        return slot, cue, mode, cues, states
    except Exception:
        logger.debug("decide_probe_target failed", exc_info=True)
        return "", "", "", [], empty


# ── Q-8 B/C/E（#264 #263）：阶段计划 + 注入分级 must + 无新信息×2 投话题 ─────────────────
MUST_DISCIPLINE_ZH = ("回复里必须真的把这个问题问出来（以问句收尾），不许只回应夸赞、不许自己"
                      "告别或说要去忙。")
TOPIC_USED_KEY_PREFIX = "goals:topic_used:"
#: 人设话题库为空时的兜底话题（客户导向的开放式话头；zh 权威 / en 展示）
_FALLBACK_TOPICS = (
    ("问TA今天过得怎么样、最忙的是哪一段", "ask how their day went and which part was busiest"),
    ("问TA周末一般怎么过", "ask what their weekends usually look like"),
    ("聊最近吃到的好东西，反问TA最爱吃什么", "talk about something tasty you had lately and ask their favourite food"),
    ("聊最近在听的歌或看的剧，问TA在看什么", "mention a song or show you're into and ask what they're watching"),
    ("问TA那边今天天气怎么样、平时喜欢晴天还是雨天", "ask about the weather there and whether they like sun or rain"),
    ("问TA小时候最喜欢做的一件事", "ask one thing they loved doing as a kid"),
)

# ── Q-8 G（#264 #263）：坐席「现在就问一个」硬注入下一条回复（客户活跃窗内不单发）────────
PROBE_MUST_PARAM = "_probe_must"
PROBE_MUST_TTL_SEC = 1800
PROBE_MUST_EVENT_SET = "probe_must_set"
PROBE_MUST_EVENT_CONSUMED = "probe_must_consumed"
PROBE_MUST_EVENT_EXPIRED = "probe_must_expired"


def agent_probe_line(text: str) -> str:
    """坐席指定问句的硬注入行：文本可顺着语境微调措辞，问题不能少。"""
    t = " ".join(str(text or "").split())[:300]
    return ("【本轮必问·硬性】坐席指定现在就把这一句问出去（可顺着语境微调措辞，问题本身不能少，"
            f"以问句收尾）：{t}")


def set_agent_probe_must(store: Any, goal: Dict[str, Any], *, slot: str, text: str, by: str = "",
                         now: Optional[float] = None) -> Dict[str, Any]:
    """把坐席的问句挂到目标 ``params._probe_must``（覆盖旧的）；下一条回复消费。返回记录。"""
    n = float(now if now is not None else time.time())
    gid = str(goal.get("goal_id") or "")
    rec = {"slot": str(slot or ""), "text": " ".join(str(text or "").split())[:300], "ts": n,
           "by": str(by or ""), "expires_at": n + PROBE_MUST_TTL_SEC}
    params = dict(goal.get("params") or {})
    params[PROBE_MUST_PARAM] = rec
    store.update_goal_fields(gid, params=params)
    try:
        store.add_event(gid, PROBE_MUST_EVENT_SET, f"{rec['slot']} by={rec['by'] or '-'}",
                        conversation_id=str(goal.get("conversation_id") or ""), text_head=rec["text"][:120], now=n)
    except Exception:
        logger.debug("probe_must_set event skipped", exc_info=True)
    return rec


def take_agent_probe_must(store: Any, goal: Dict[str, Any], *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """读取待消费的坐席问句：TTL 内 → 返回记录（调用方在本轮 patch 里摘键 + 记 consumed）；
    过期 → 当场摘键 + 记 ``probe_must_expired``，返回 None；没有 → None。绝不抛。"""
    try:
        params = dict((goal or {}).get("params") or {})
        rec = params.get(PROBE_MUST_PARAM)
        if not isinstance(rec, dict) or not str(rec.get("text") or "").strip():
            return None
        n = float(now if now is not None else time.time())
        gid = str(goal.get("goal_id") or "")
        conv = str(goal.get("conversation_id") or "")
        exp = float(rec.get("expires_at") or (float(rec.get("ts") or 0) + PROBE_MUST_TTL_SEC))
        if n > exp:
            params.pop(PROBE_MUST_PARAM, None)
            store.update_goal_fields(gid, params=params)
            store.add_event(gid, PROBE_MUST_EVENT_EXPIRED, str(rec.get("slot") or "-"), conversation_id=conv,
                            text_head=str(rec.get("text") or "")[:120], now=n)
            logger.info("[goal-probe] must expired goal=%s conv=%s slot=%s age=%.0fs", gid[:12], conv,
                        rec.get("slot") or "-", n - float(rec.get("ts") or n))
            return None
        store.add_event(gid, PROBE_MUST_EVENT_CONSUMED, str(rec.get("slot") or "-"), conversation_id=conv,
                        text_head=str(rec.get("text") or "")[:120], now=n)
        logger.info("[goal-inject] level=must reason=agent_probe conv=%s goal=%s target=%s text=%r", conv,
                    gid[:12], rec.get("slot") or "-", str(rec.get("text") or "")[:80])
        return dict(rec)
    except Exception:
        logger.debug("take_agent_probe_must failed", exc_info=True)
        return None


def stage_plan_enabled(cfg_root: Any) -> bool:
    """阶段计划只在**陪伴域**默认开（销售域会话有自己的目标弧线）；``companion.goals.stage_plan.enabled``
    显式 false 可关。"""
    try:
        cfg = resolve_goals_cfg(cfg_root)
        sp = cfg.get("stage_plan") if isinstance(cfg.get("stage_plan"), dict) else {}
        if sp.get("enabled") is False:
            return False
        if sp.get("enabled") is True:
            return True
        from src.utils.business_domain import active_business_domain
        return active_business_domain(cfg_root if isinstance(cfg_root, dict) else None) == "companion"
    except Exception:
        return False


def resolve_stage(platform: str, account_id: str, chat_key: str) -> Tuple[str, float]:
    """``(stage, intimacy)``：亲密度来自 intimacy_engine 既有判定（companion_context provider）。"""
    from src.companion.goals.planner import intimacy_stage
    score = -1.0
    try:
        from src.utils.companion_context import resolve_intimacy_score
        v = resolve_intimacy_score(account_id or "default", chat_key, channel=platform or "telegram")
        if v is not None:
            score = float(v)
    except Exception:
        score = -1.0
    return intimacy_stage(score), score


def persona_topic_pool(persona: Any) -> List[str]:
    """人设话题库：hobbies / interests / tastes / topics / openers / life_arc（list 或 dict.beats）。"""
    out: List[str] = []
    if not isinstance(persona, dict):
        return out
    for k in ("topics", "openers", "hobbies", "interests", "tastes"):
        v = persona.get(k)
        if isinstance(v, str) and v.strip():
            out.extend([s.strip() for s in re.split(r"[,，;；\n]", v) if s.strip()])
        elif isinstance(v, (list, tuple)):
            out.extend([str(s).strip() for s in v if str(s or "").strip()])
    arc = persona.get("life_arc")
    if isinstance(arc, (list, tuple)):
        out.extend([str(s).strip() for s in arc if str(s or "").strip()])
    elif isinstance(arc, dict):
        beats = arc.get("beats")
        if isinstance(beats, (list, tuple)):
            out.extend([str(s).strip() for s in beats if str(s or "").strip()])
    seen: set = set()
    uniq: List[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t[:80])
    return uniq


_WEATHER_TOPIC_MARKS = ("天气", "weather", "晴天", "雨天", "sun or rain")
_WEATHER_DONE_MARKS = (
    "天气", "weather", "下雨", "下雨了", "raining", "rainy", "sunny", "how was the weather",
)


def _topic_is_weather(topic: str) -> bool:
    t = str(topic or "").lower()
    return any(m.lower() in t for m in _WEATHER_TOPIC_MARKS)


def _conv_weather_answered(inbox_store: Any, conversation_id: str) -> bool:
    """近史里已经聊过天气 → 兜底话题不再问天气。"""
    try:
        hist = recent_history(inbox_store, conversation_id, limit=16) or []
    except Exception:
        return False
    blob = " ".join(str((m or {}).get("text") or "") for m in hist).lower()
    if not blob:
        return False
    return any(m.lower() in blob for m in _WEATHER_DONE_MARKS)


def pick_proactive_topic(
    conversation_id: str, persona: Any, *, inbox_store: Any = None, now: Optional[float] = None,
    lang: str = "zh",
) -> str:
    """无新信息×2 时投给本轮的**新**话题：人设话题库优先（避开本会话近 10 次用过的），库空回落
    兜底池；按 (conv, day, 已用数) 确定性轮换。落 KV ``goals:topic_used:<conv>``。绝不抛。"""
    try:
        n = float(now if now is not None else time.time())
        used: List[str] = []
        key = f"{TOPIC_USED_KEY_PREFIX}{conversation_id}"
        st = _kv_store(inbox_store)
        if st is not None and hasattr(st, "get_app_setting"):
            try:
                raw = st.get_app_setting(key, "") or ""
                used = [str(x) for x in (json.loads(raw) if raw else [])][-10:]
            except Exception:
                used = []
        pool = [t for t in persona_topic_pool(persona) if t not in used]
        en = str(lang or "").lower().startswith("en")
        if not pool:
            fb = [(zh, e) for zh, e in _FALLBACK_TOPICS if (e if en else zh) not in used]
            if not fb:
                fb = list(_FALLBACK_TOPICS)
            pool = [e if en else zh for zh, e in fb]
        if _conv_weather_answered(inbox_store, conversation_id):
            pool = [t for t in pool if not _topic_is_weather(t)]
            if not pool:
                fb = [(zh, e) for zh, e in _FALLBACK_TOPICS
                      if not _topic_is_weather(zh) and not _topic_is_weather(e)]
                pool = [e if en else zh for zh, e in fb if (e if en else zh) not in used]
                if not pool:
                    pool = [e if en else zh for zh, e in fb]
        if not pool:
            return ""
        day = time.strftime("%Y-%m-%d", time.localtime(n))
        import zlib
        h = zlib.crc32(f"topic:{conversation_id}:{day}:{len(used)}".encode("utf-8", "ignore"))
        topic = pool[h % len(pool)]
        if st is not None and hasattr(st, "set_app_setting"):
            try:
                st.set_app_setting(key, json.dumps((used + [topic])[-10:], ensure_ascii=False),
                                   updated_by="goal_service")
            except Exception:
                pass
        return topic
    except Exception:
        logger.debug("pick_proactive_topic failed", exc_info=True)
        return ""


def proactive_topic_allowed(inbox_store: Any, conversation_id: str, *, now: Optional[float] = None) -> str:
    """回复链内投话题的约束（停联冻结 / risk_hold）；返回拦截原因，"" 为放行。节奏 / 预算属外呼
    链（这里是顺着客户来信回，不新增出站条数）。"""
    st = _kv_store(inbox_store)
    if st is None or not conversation_id:
        return ""
    try:
        from src.inbox.stop_contact import frozen_reason
        r = frozen_reason(st, conversation_id)
        if r:
            return f"frozen:{r}"
    except Exception:
        pass
    try:
        from src.inbox import risk_hold
        r = risk_hold.active(st, conversation_id, now=now)
        if r:
            return f"risk_hold:{r}"
    except Exception:
        pass
    return ""


def build_stage_block(
    *, conversation_id: str, platform: str, account_id: str, chat_key: str,
    inbound_text: str, inbox_store: Any, user_context: Optional[Dict[str, Any]],
    cfg_root: Any, now: Optional[float] = None, chain: str = "reply",
) -> Optional[str]:
    """无手建目标的会话：阶段计划「今日主线」块（B）+ 无新信息×2 → must + 投人设新话题（C/E）。
    不建目标行；``user_context._goal_inject_meta`` 写 ``reason=stage_plan``。绝不抛。"""
    try:
        from src.companion.goals.planner import day_key, plan_stage_beat, stage_label
        from src.companion.goals.signals import no_new_info_streak
        n = float(now if now is not None else time.time())
        stage, score = resolve_stage(platform, account_id, chat_key)
        hist = recent_history(inbox_store, conversation_id, limit=12)
        # 本条入站可能尚未落库：把它并进流末尾再数
        if str(inbound_text or "").strip():
            hist = list(hist) + [{"direction": "in", "text": str(inbound_text), "ts": n}]
        streak = no_new_info_streak(hist)
        beat = plan_stage_beat(stage=stage, conversation_id=conversation_id, day=day_key(n),
                               no_new_info_streak=streak)
        if not beat.get("intent"):
            return None
        lines = [f"【关系阶段 · 今日主线】阶段：{stage_label(stage)}。今日主线：{beat['intent']}。"]
        level = str(beat.get("level") or "soft")
        topic = ""
        if level == "must":
            block_why = proactive_topic_allowed(inbox_store, conversation_id, now=n)
            if not block_why:
                persona: Any = None
                try:
                    from src.utils.persona_manager import PersonaManager
                    uc = user_context or {}
                    persona, _t = PersonaManager.get_instance().get_persona_with_tier(
                        str(uc.get("chat_id") or chat_key or ""),
                        str(uc.get("account_persona_id") or ""))
                except Exception:
                    persona = None
                topic = pick_proactive_topic(conversation_id, persona, inbox_store=inbox_store, now=n)
                if topic:
                    lines.append(f"【本轮必做】对方连续 {streak} 轮只有夸赞 / 应答、没有新信息："
                                 f"接住一句就换你来带话题——{topic}；{MUST_DISCIPLINE_ZH}")
                    logger.info("[proactive_topic] conv=%s topic=%r reason=no_new_info_x2 streak=%d chain=%s",
                                conversation_id, topic[:40], streak, chain)
            else:
                logger.info("[proactive_topic] conv=%s topic=- reason=blocked:%s streak=%d",
                            conversation_id, block_why, streak)
            if not topic:
                lines.append(f"【本轮必做】对方连续 {streak} 轮没有新信息：用一个开放式问题把话题转到TA身上；"
                             f"{MUST_DISCIPLINE_ZH}")
        logger.info("[goal-inject] level=%s reason=%s conv=%s stage=%s intimacy=%.0f intent=%r chain=%s",
                    level, ("no_new_info" if level == "must" else "stage_plan"), conversation_id,
                    stage, score, str(beat["intent"])[:40], chain)
        if isinstance(user_context, dict):
            user_context["_goal_inject_meta"] = {
                "injected": True, "reason": "stage_plan", "stage": stage, "level": level,
                "intent": beat["intent"], "topic": topic, "no_new_info_streak": streak}
        return "\n".join(lines)
    except Exception:
        logger.debug("build_stage_block failed", exc_info=True)
        return None


def capture_status(cfg_root: Any) -> Dict[str, Any]:
    """摸底「采集链」当前能不能自动填槽（O-3 D #236，卡片黄条数据源）。纯函数、绝不抛。

    按代码真相写两条独立原因（报告把 0/4 归咎「情景记忆抽取死」——槽位采集其实是另一条链）：
    - ``profile_llm_off``：``companion.goals.profile_llm.enabled`` 关（clean 包基线未开）——
      客户自由表达的回答（「I run a small bakery」）不会自动记入画像，只剩固定句式正则
      （宁漏不错、坐标只认白名单地名）；这是 0/4 里「答了却没记」的真正依赖。
    - ``memory_extract_off``：``memory.extract`` 总闸关 / 白名单空且未 match_all（O-2 #256）
      ——客户透露的私事不进情景记忆，AI 下一轮就忘、追问接不上话头。
    ``ok=True`` 仅当两条都不成立。"""
    out: Dict[str, Any] = {"ok": True, "reasons": [], "profile_llm": False,
                           "memory_extract": True}
    try:
        root = cfg_root if isinstance(cfg_root, dict) else {}
        goals = ((root.get("companion") or {}).get("goals") or {}) \
            if isinstance(root.get("companion"), dict) else {}
        pl = goals.get("profile_llm") if isinstance(goals, dict) else None
        llm_on = bool(isinstance(pl, dict) and pl.get("enabled"))
        out["profile_llm"] = llm_on
        if not llm_on:
            out["reasons"].append("profile_llm_off")
        mem = root.get("memory") if isinstance(root.get("memory"), dict) else {}
        ex = mem.get("extract") if isinstance(mem.get("extract"), dict) else {}
        ex_on = bool(ex.get("enabled", True))
        whitelist = [str(x).strip() for x in (ex.get("intents") or []) if str(x).strip()]
        mem_ok = ex_on and (bool(ex.get("match_all")) or bool(whitelist))
        out["memory_extract"] = mem_ok
        if not mem_ok:
            out["reasons"].append("memory_extract_off")
        out["ok"] = not out["reasons"]
    except Exception:
        logger.debug("capture_status failed", exc_info=True)
    return out


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
    未启用/无目标/hold → None。绝不抛。

    P25 观测：每条判定路径把「注没注入、为什么」写回 ``user_context
    ["_goal_inject_meta"]``（生成链透传成 API 的 ``goal_applied``）——坐席端
    「设了目标为什么没切入」从黑箱变成可读原因。user_context 非 dict 时静默。
    """
    # #166 键一致性：查找键先归一（conv ↔ 三元组互补、占位账号以 conv 为准），
    # 下面的 find_active_goal 与日志都用归一后的键——日志里印的就是真正查库的键。
    _keys = resolve_inject_lookup_keys(
        platform=platform, chat_key=chat_key, account_id=account_id,
        conversation_id=conversation_id)
    platform = _keys["platform"] or str(platform or "")
    chat_key = _keys["chat_key"] or str(chat_key or "")
    account_id = _keys["account_id"]
    conversation_id = _keys["conversation_id"]

    def _conv_label() -> str:
        return conversation_id or f"{platform}:{account_id}:{chat_key}"

    def _note_meta(injected: bool, reason: str, **extra: Any) -> None:
        if isinstance(user_context, dict):
            m: Dict[str, Any] = {
                "injected": bool(injected), "reason": str(reason or "")}
            m.update(extra)
            user_context["_goal_inject_meta"] = m
        # #152 F2（0902 skuio「工作目标完全没执行」）：此前注入判定只写内存 meta，
        # 日志零痕迹——诊断包里翻遍找不到「这一轮目标为什么没进 prompt」。有目标
        # 却未注入是坐席最需要知道的事：早退原因一律 INFO 落日志。
        # #166（0905 skuio 88MP86：9.5h 零 [goal-inject] 行，右栏卡却显示目标进行中）：
        # no_goal 升 INFO 并带**全部查找键**。
        # M-7 B（#236，0907 skuio 第五次复报：3h 59 稿零行）：真因是本模块 logger 名
        # "GoalService" 不在 src.* 命名空间、INFO 从源头被 root=WARNING 丢掉——已改名。
        # 顺手把口径钉死：**每一稿一行**（自动链 / 手动链 / 主动链同口径）：
        # - disabled / no_goal 同为 INFO，按「原因|会话」60s 节流（不再有 DEBUG 死角；
        #   无会话 id 的系统调用也打，键回落三元组）；
        # - no_goal 附带该会话最近一条终态目标（goal=… status=expired done_at=…）——
        #   「到期了→查无目标」与「从没建过」在日志里必须能分辨；
        # - 目标刚被本轮 settle-on-read 翻成终态 → reason=goal_expired / goal_done /
        #   goal_failed（不再是笼统的 inactive）。
        try:
            key = conversation_id or f"{platform}:{account_id}:{chat_key}"
            if reason in ("disabled", "inject_disabled"):
                if _inject_log_allowed(f"{reason}|{key}", now):
                    logger.info(
                        "[goal-inject] NOT injected reason=%s conv=%s plat=%s "
                        "chat_key=%s acct=%s chain=%s（同会话 %ds 内不重复）",
                        reason, key, platform, chat_key, account_id or "-",
                        chain, int(_INJECT_LOG_THROTTLE_SEC))
            elif reason == "no_goal":
                if _inject_log_allowed(f"no_goal|{key}", now):
                    logger.info(
                        "[goal-inject] NOT injected reason=no_goal conv=%s plat=%s "
                        "chat_key=%s acct=%s chain=%s%s（同会话 %ds 内不重复）",
                        key, platform, chat_key, account_id or "-", chain,
                        _last_goal_hint(extra.get("_store"), key),
                        int(_INJECT_LOG_THROTTLE_SEC))
            elif injected:
                logger.info(
                    "[goal-inject] injected conv=%s goal=%s status=active ms=%s "
                    "push=%s chain=%s intent=%s",
                    key,
                    str(extra.get("goal_id") or "")[:12],
                    extra.get("milestone_idx", "-"),
                    extra.get("push_level", "-"),
                    chain,
                    str(extra.get("intent") or "")[:40])
            else:
                logger.info(
                    "[goal-inject] NOT injected reason=%s%s conv=%s goal=%s status=%s "
                    "chain=%s title=%r",
                    reason,
                    (f"({extra['hold_reason']})" if extra.get("hold_reason") else ""),
                    key,
                    str(extra.get("goal_id") or "")[:12],
                    str(extra.get("status") or "active"),
                    chain,
                    str(extra.get("title") or "")[:30])
        except Exception:
            pass

    try:
        cfg_root = getattr(config_obj, "config", None)
        if not isinstance(cfg_root, dict):
            cfg_root = config_obj if isinstance(config_obj, dict) else {}
        cfg = resolve_goals_cfg(cfg_root)
        if not cfg.get("enabled", False):
            _note_meta(False, "disabled")
            return None
        inject_cfg = cfg.get("inject") or {}
        if not inject_cfg.get("enabled", True):
            _note_meta(False, "inject_disabled")
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
        # Q-8 A（#264 #263）：账号级 / 人设级默认目标——新会话首条真实入站自动挂
        # （B9D8NW：7 条目标全手建、新客 Enrique 无目标 → no_goal → AI 自己退场）
        if goal is None and str(inbound_text or "").strip():
            from src.companion.goals.defaults import maybe_attach_default_goal
            goal = maybe_attach_default_goal(
                store, cfg_root, platform=platform,
                chat_key=str(chat_key or ""), account_id=account_id,
                conversation_id=conversation_id,
                user_context=user_context, inbox_store=inbox_store, now=now)
        if goal is None:
            _note_meta(False, "no_goal", _store=store)
            if isinstance(user_context, dict):
                # _store 只给日志用，不透传到 API 的 goal_applied
                (user_context.get("_goal_inject_meta") or {}).pop("_store", None)
            # Q-8 B（#264 #263）：陪伴域无目标会话仍有「阶段 · 今日主线」（不建目标行）；
            # 只在真实入站回合出块（主动链 / 系统调用不出）
            if str(inbound_text or "").strip() and stage_plan_enabled(cfg_root):
                return build_stage_block(
                    conversation_id=_conv_label(), platform=platform, account_id=account_id,
                    chat_key=str(chat_key or ""), inbound_text=inbound_text,
                    inbox_store=inbox_store, user_context=user_context, cfg_root=cfg_root,
                    now=now, chain=chain)
            return None
        # Q-1 D（#264 #269）识破守卫：对方本条说「you're a bot / 20th time / 你又问」→ 该目标
        # 停 24h（params._paused_until）+ 会话侧 needs_human / 打标「疑似识破」/ KV 标记（出站
        # 只出一句挽回），本轮**不注入**。停牌期内每轮不注入（reason=goals_paused）。
        try:
            from src.inbox import exposure_guard as _xg
            _gid = str(goal.get("goal_id") or "")
            _hit = _xg.detect_exposure(inbound_text) if str(inbound_text or "").strip() else ""
            if _hit:
                _xg.pause_goal(store, goal, hit=_hit, now=now, conversation_id=_conv_label())
                _xg.handle_inbound(inbound_text, conversation_id=_conv_label(),
                                   inbox_store=inbox_store, now=now)
                _note_meta(False, "exposure_paused", goal_id=_gid,
                           title=str(goal.get("title") or ""), hit=_hit)
                return None
            _pu = _xg.goal_paused_until(goal)
            if _pu > float(now if now is not None else time.time()):
                _note_meta(False, "goals_paused", goal_id=_gid,
                           title=str(goal.get("title") or ""),
                           paused_until=int(_pu))
                return None
        except Exception:
            logger.debug("exposure guard skipped", exc_info=True)
        # P1 达成信号（2026-08-29）：自定义目标在要联系方式、对方本条真给了
        # → 记 outcome_signal（params+事件，右栏卡出「像是达成了」提示行）。
        # 只提示不自动结算——确认权在人。放在 observe 早退**之前**：观察档
        # 恰恰只看不动，达成信号是它最需要的观测。
        if str(inbound_text or "").strip():
            try:
                from src.companion.goals.outcome import (
                    maybe_outcome_shadow,
                    maybe_outcome_signal,
                )
                sig_hit = maybe_outcome_signal(
                    store, goal, inbound_text, now=now)
                # 影子轨（P2）：约时间/付款只记事件不出提示——攒精度数据
                maybe_outcome_shadow(store, goal, inbound_text, now=now)
                # P2 2026-08-30 冲刺自动结算（sprint.auto_settle_contact，
                # 默认关）：限时目标 + 高置信联系方式信号 → 直接 done。
                # 3 小时窗里「绿条等人点」常常等到过期——用户拍板要最快时，
                # 检出即结算；result=signal:* 单列分桶，报表不冒充人工确认。
                if sig_hit:
                    settled = _maybe_auto_settle_outcome(
                        store, cfg_root, goal, now=now)
                    if settled is not None:
                        _note_meta(False, "outcome_auto_settled",
                                   goal_id=str(goal.get("goal_id") or ""),
                                   title=str(goal.get("title") or ""))
                        return None
            except Exception:
                logger.debug("outcome signal skipped", exc_info=True)
        # observe 档：目标只作看板观测，完全不进 prompt
        if str(goal.get("autonomy") or "suggest") == "observe":
            _note_meta(False, "observe",
                       goal_id=str(goal.get("goal_id") or ""),
                       title=str(goal.get("title") or ""))
            return None
        template = get_template(str(goal.get("template") or "")) or {}
        has_slots = bool(template.get("profile_slots"))
        # Q-1 E（#264）：目标页「暂停全部摸底目标」总开关开着 → 摸底类目标整体不注入
        if has_slots and discovery_paused(inbox_store):
            _note_meta(False, "discovery_paused",
                       goal_id=str(goal.get("goal_id") or ""),
                       title=str(goal.get("title") or ""))
            return None
        # 勾选槽位（摸底目标 params.slots）一次解析全程复用：LLM 摘录聚焦 /
        # 缺口指令 / 意图合流三处同一口径
        sel_slots: list = []
        if has_slots:
            try:
                from src.companion.goals.profile_slots import (
                    parse_selected_slots as _pss,
                )
                sel_slots = _pss((goal.get("params") or {}).get("slots"))
            except Exception:
                sel_slots = []

        # P1 顺手采集：对方本条消息里的高置信画像信号 → 只填空槽（坐席手录优先）。
        # 采在结算**前**——「预算两千美金」这类信号当轮就计入 bant_fill 推里程碑。
        if has_slots and str(inbound_text or "").strip():
            reserved = set()
            try:
                from src.companion.goals.profile_fill import resolve_reserved_self_names
                reserved = resolve_reserved_self_names(
                    cfg_root=cfg_root, platform=platform, account_id=account_id,
                    chat_key=str(chat_key or ""), conversation_id=conversation_id,
                    inbox_store=inbox_store)
            except Exception:
                reserved = set()
            try:
                from src.companion.goals.profile_slots import capture_from_text
                captured = capture_from_text(inbound_text, reserved_self_names=reserved)
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
                    # Q-25 B（#295 Y39D8U）：「AI 问 → 客户只答时长 / 是 / 数字」不是回填原料——
                    # 「couple months」里没有任何实体，LLM 只会把 AI 问句里的 inspection / AI
                    # 补成职业。命中即不调 LLM 回填，只留 slot_state 的 mentioned:answered 线索
                    # （无值）；`[profile] drop … reason=answer_only:<kind>` 记一行。
                    from src.companion.goals.profile_slots import answer_only_kind
                    _ans_kind = answer_only_kind(inbound_text)
                    if _ans_kind:
                        logger.info(
                            "[profile] drop conv=%s slot=* value=%s source=ai_inferred "
                            "reason=answer_only:%s", _conv_label(),
                            str(inbound_text or "")[:40].replace("\n", " "), _ans_kind)
                    else:
                        prof0 = store.get_customer_profile(
                            platform, str(chat_key or ""))
                        schedule_llm_capture(
                            ai_client, store, cfg_root,
                            platform=platform, chat_key=str(chat_key or ""),
                            text=inbound_text,
                            fields=dict((prof0 or {}).get("fields") or {}),
                            # P27：摸底目标只问坐席勾选的缺口——全 11 槽提示词
                            # 又长又散，抽取面越大误摘面越大
                            include=(sel_slots or None),
                            now=now,
                            reserved_self_names=reserved)
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
        # 本轮账号生效人设：**选品与出站守卫必须同源**。守卫（foreign_product_names）
        # 按这个 id 判「哪些产品名属于别的人设」，选品（pick_products）按同一个 id
        # 决定「哪些货能进注入」——两处取不同来源时，选中的绑定货会被守卫当他人设
        # 的货剥掉名字，等于自己拆自己的台。算一次，两处共用。
        catalog_persona_id = (
            _account_persona(cfg_root, platform, account_id)
            if template.get("catalog") else "")
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
                        "persona_id": catalog_persona_id,
                        # P15 事实声明守卫：目录登记的合法价格。LLM 报了目录里
                        # 没有的数（实录「团队版198美金一个月，算下来一个月168」
                        # ＝偷偷 8.5 折）即拦——措辞轴的 offer_guard 管不到裸数字。
                        "catalog_prices": _catalog_price_list(
                            cfg_root, getattr(config_obj, "config_path", None)),
                        # #147：自家阵营词表（登记活动 + 当日活动 + 产品名）——
                        # 出站贬损守卫在带货会话也读这份暂存，与无目标兜底同源
                        "camp_terms": _camp_term_list(
                            cfg_root, getattr(config_obj, "config_path", None)),
                    }
            except Exception:
                logger.debug("goal cta stash skipped", exc_info=True)
        neg = is_negative_emotion(uc.get("user_emotion_hint"))
        # P1-198 续（2026-08-02）：坐席人工「情绪低落」标注（TTL 窗内）视同强负面
        # → 今日让路（hold 文案沿用 inbox.goal.hold.emotion）。判据与拟稿指令 /
        # NBA 徽标同源（effective_mood）；「积极开朗」刻意不反向解锁——不对称覆写：
        # 人可以让 AI 更谨慎，不能替客户宣布心情好了就加速推进。
        if not neg and inbox_store is not None:
            try:
                from src.inbox.effective_mood import (
                    manual_negative_active,
                    record_mood_consume,
                    resolve_mood_steering_cfg,
                )
                _ms = resolve_mood_steering_cfg(cfg_root)
                _cid_m = str(conversation_id or "").strip()
                if not _cid_m and platform and account_id and chat_key:
                    _cid_m = f"{platform}:{account_id}:{chat_key}"
                if _ms["enabled"] and _cid_m:
                    _meta_m = inbox_store.get_conv_meta(_cid_m) or {}
                    if manual_negative_active(
                            _meta_m,
                            now=float(now if now is not None else time.time()),
                            ttl_hours=_ms["ttl_hours"]):
                        neg = True
                        record_mood_consume("goal_hold")
            except Exception:
                logger.debug("goal mood hold override skipped", exc_info=True)
        res = refresh_goal(
            store, cfg_root, goal, inbox_store=inbox_store,
            negative_emotion=neg, now=now,
            inbound_turn=bool(str(inbound_text or "").strip()))
        _st_after = str((res.get("goal") or {}).get("status") or "active")
        if res.get("hold") or _st_after != "active":
            # M-7 B：本轮 settle-on-read 刚把目标翻成终态 → 原因直说 goal_expired /
            # goal_done / goal_failed（09-07 BABY BEAR 原目标就是这样静默到期的）
            _reason = "hold" if res.get("hold") else (
                f"goal_{_st_after}" if _st_after in ("expired", "done", "failed")
                else "inactive")
            _note_meta(
                False, _reason,
                hold_reason=str(res.get("hold") or ""),
                goal_id=str(goal.get("goal_id") or ""),
                status=_st_after,
                title=str(goal.get("title") or ""))
            return None
        action = res.get("action")
        # 坐席驳回今日拍（P2）→ 今天彻底不注入（明日 planner 按驳回回流降档重排）
        if action is not None and str(action.get("status")) in ("skipped", "blocked"):
            _note_meta(False, "beat_rejected",
                       goal_id=str(goal.get("goal_id") or ""),
                       title=str(goal.get("title") or ""))
            return None

        # 画像 → prompt（P1 缺口 + P9a 档案事实）。档案对所有模板有益
        # （挽回目标没有 profile_slots 也该知道「TA是做民宿的」）→ 画像
        # 常载；缺口采集指令仍只在摸底型模板 + 破冰后出。
        profile_gap = ""
        profile_facts = ""
        prof_fields: Dict[str, Any] = {}
        gap_patch: Optional[Dict[str, Any]] = None
        _gap_key = ""
        try:
            from src.companion.goals.profile_slots import (
                facts_line,
                resolve_inject_gap,
            )
            prof = store.get_customer_profile(platform, str(chat_key or ""))
            prof_fields = dict((prof or {}).get("fields") or {})
            profile_facts = facts_line(prof_fields)
            # 缺口起始里程碑随模板（P26）：acquire 先破冰再摸底（默认 1）；
            # profile_discovery 摸底即全部目的（0，破冰当天就带方向）
            _gap_from = int(template.get("gap_from_milestone", 1) or 0)
            if has_slots and int(
                    (res.get("goal") or {}).get("milestone_idx") or 0
            ) >= _gap_from:
                # C2：每轮硬带**一个**未填槽（勾选序 / 登记序第一空槽）；
                # 有入站则轮换已问仍空的槽，防连五轮追问同一句。
                profile_gap, _gap_key, gap_patch = resolve_inject_gap(
                    prof_fields,
                    (res.get("goal") or goal).get("params") or {},
                    inbound_text=inbound_text,
                    include=sel_slots or None)
        except Exception:
            profile_gap = profile_facts = ""
            gap_patch = None

        # O-3 C（#236 HM7XBA）：注入变硬。① 先验上一轮硬注入的问句问出去没有
        # （probe_asked / probe_missed 事件 + 日志）；② 客户本轮线索词对上未填槽 /
        # 上轮 missed 同槽重试 / 今天还没真问过一个 → 本轮【本轮必问】硬约束行，软的
        # 缺口合流让位（一轮只带一个问法）；③ 挂起 params._probe_pending 等下轮校验。
        probe_slot = probe_cue = probe_mode = ""
        probe_line = ""
        probe_patch: Optional[Dict[str, Any]] = None
        _is_discovery = bool(has_slots and template.get("gap_in_intent") and sel_slots)
        if _is_discovery:
            try:
                from src.companion.goals.profile_slots import (
                    SLOT_STATE_UNKNOWN,
                    deepen_line,
                    known_slots_line,
                    probe_hard_line,
                    slot_label,
                )
                _g_now = res.get("goal") or goal
                _gid_p = str(_g_now.get("goal_id") or "")
                _conv_p = conversation_id or f"{platform}:{account_id}:{chat_key}"
                _n_p = float(now if now is not None else time.time())
                ver = verify_pending_probe(store, _g_now, inbox_store=inbox_store, now=_n_p)
                if ver.get("patch") is not None:
                    probe_patch = dict(ver["patch"])
                if ver["result"] in ("asked", "missed", "covered"):
                    logger.info(
                        "[goal-inject] target=%s cue=%s result=%s conv=%s goal=%s reply=%r",
                        ver["slot"], ver["cue"] or "-", ver["result"], _conv_p,
                        _gid_p[:12], str(ver.get("reply_head") or "")[:60])
                    get_goal_stats().record_probe(ver["result"])
                # Q-1 A（#264 #269）：注入前扫最近 30 轮，槽位三态——unknown 才是候选；
                # mentioned / confirmed 进「已知不再问」负向清单，软缺口行也只许指向 unknown。
                _hist = recent_history(inbox_store, _conv_p, limit=30)
                # Q-8 G（#264 #263）：坐席「现在就问一个」在客户活跃窗内点的 → 硬注入下一条回复
                # （params._probe_must，TTL 内一次性消费；过期即丢并记事件）。
                _agent_must = take_agent_probe_must(store, _g_now, now=_n_p) if str(inbound_text or "").strip() else None
                if str(inbound_text or "").strip():
                    probe_slot, probe_cue, probe_mode, _cues, _states = decide_probe_target(
                        store, _g_now, prof_fields=prof_fields, sel_slots=sel_slots,
                        inbound_text=inbound_text,
                        retry_slot=(ver["slot"] if ver["result"] == "missed" else ""),
                        now=_n_p, history=_hist)
                    if _agent_must:
                        probe_slot = str(_agent_must.get("slot") or probe_slot or "")
                        probe_cue, probe_mode = "agent", "must"
                else:
                    from src.companion.goals.profile_slots import slot_states as _ss
                    _states = _ss(list(sel_slots), prof_fields, _hist)
                _known = [k for k, (st, _r) in _states.items() if st != SLOT_STATE_UNKNOWN]
                if _known:
                    # 软缺口行只许指向 unknown 槽（resolve_inject_gap 只看字段空不空）
                    if _gap_key and _gap_key in _known:
                        profile_gap = ""
                        gap_patch = None
                    from src.companion.goals.profile_slots import slot_value as _sv
                    _known_line = known_slots_line([
                        slot_label(k) for k in _known if not _sv(prof_fields, k)])
                else:
                    _known_line = ""
                if probe_slot:
                    _st_p, _rs_p = _states.get(probe_slot, (SLOT_STATE_UNKNOWN, ""))
                    if probe_mode == "deepen":
                        probe_line = deepen_line(probe_slot, probe_cue)
                        # 深化不挂 _probe_pending（不校验、不 retry）；记事件计每槽每日 ≤1
                        try:
                            store.add_event(_gid_p, PROBE_EVENT_DEEPEN,
                                            f"{probe_slot}@{probe_cue or '-'}",
                                            conversation_id=_conv_p, now=_n_p)
                        except Exception:
                            logger.debug("probe_deepen event skipped", exc_info=True)
                        get_goal_stats().record_probe("deepen")
                        logger.info(
                            "[goal-inject] target=%s cue=%s state=%s decision=deepen reason=%s "
                            "conv=%s goal=%s chain=%s",
                            probe_slot, probe_cue or "-", _st_p, _rs_p or "-", _conv_p,
                            _gid_p[:12], chain)
                    else:
                        probe_line = probe_hard_line(probe_slot, probe_cue)
                        # Q-8 C（#264 #263）：注入分级——客户连续 ≥2 轮无新信息（纯夸赞 / 应答）
                        # 或今日 missed 已 ≥2（跨槽累计）→ level=must：硬约束再加一句「必须以问句
                        # 收尾、不许只回应夸赞 / 不许自己退场」。候选槽仍只来自 Q-1 的 unknown +
                        # 每槽每日上限过滤（decide_probe_target），must 不越过「已知即不问」。
                        _must_reason = ""
                        if _agent_must:
                            # Q-8 G：坐席指定的这一句就是要问的（可微调措辞、问题不能少）
                            _must_reason = "agent_probe"
                            probe_line = agent_probe_line(str(_agent_must.get("text") or ""))
                        else:
                            try:
                                from src.companion.goals.signals import no_new_info_streak as _nnis
                                _h2 = list(_hist) + [{"direction": "in", "text": str(inbound_text), "ts": _n_p}]
                                _streak = _nnis(_h2)
                                _missed_n = sum(_event_detail_slots_today(
                                    store, _gid_p, PROBE_EVENT_MISSED, _n_p).values())
                                if _streak >= 2:
                                    _must_reason = "no_new_info"
                                elif _missed_n >= 2:
                                    _must_reason = "missed_x2"
                            except Exception:
                                _must_reason = ""
                        if _must_reason:
                            probe_mode = "must"
                            probe_line = f"{probe_line} {MUST_DISCIPLINE_ZH}"
                        logger.info(
                            "[goal-inject] level=%s reason=%s conv=%s goal=%s target=%s",
                            "must" if _must_reason else "soft", _must_reason or "probe",
                            _conv_p, _gid_p[:12], probe_slot)
                        base_p = dict(probe_patch if probe_patch is not None
                                      else (gap_patch or (_g_now.get("params") or {})))
                        if _agent_must:
                            base_p.pop(PROBE_MUST_PARAM, None)
                        base_p[PROBE_PENDING_PARAM] = {
                            "slot": probe_slot, "cue": probe_cue, "mode": probe_mode,
                            "ts": _n_p, "chain": str(chain or "")}
                        asked_l = [str(x) for x in (base_p.get("_gap_asked") or [])
                                   if str(x or "").strip()]
                        if probe_slot not in asked_l:
                            asked_l.append(probe_slot)
                        base_p["_gap_asked"] = asked_l
                        probe_patch = base_p
                        logger.info(
                            "[goal-inject] target=%s cue=%s mode=%s state=unknown decision=probe "
                            "result=pending conv=%s goal=%s chain=%s",
                            probe_slot, probe_cue or "-", probe_mode, _conv_p,
                            _gid_p[:12], chain)
                    if _known_line:
                        probe_line = f"{_known_line} {probe_line}"
                elif str(inbound_text or "").strip():
                    # 判不准就不问：把「为什么这轮没定目标」落一行（每槽状态可读）
                    logger.info(
                        "[goal-inject] target=- state=%s decision=skip reason=%s conv=%s goal=%s",
                        ",".join(f"{k}:{v[0]}" for k, v in _states.items())[:160] or "-",
                        "all_known" if _states and all(
                            v[0] != SLOT_STATE_UNKNOWN for v in _states.values())
                        else "no_cue_or_asked_today",
                        _conv_p, _gid_p[:12])
                    if _known_line:
                        # 负向清单 + 软缺口（若仍有 unknown）合成一行：已知的别问、要问只问这一个
                        from src.companion.goals.profile_slots import HARD_ASK_DISCIPLINE
                        probe_line = (f"{_known_line} 【画像缺口】{HARD_ASK_DISCIPLINE}：{profile_gap}"
                                      if profile_gap else _known_line)
            except Exception:
                logger.debug("probe hard-inject skipped", exc_info=True)
                probe_slot = probe_cue = probe_mode = ""
                probe_line = ""

        # Q-8 E（#264 #263）：有目标但本轮没定硬注入问句、客户连续 ≥2 轮无新信息（纯夸赞 /
        # 应答）→ 投人设话题库新话题（陪伴域；受停联冻结 / risk_hold 约束）。
        if (str(inbound_text or "").strip() and not probe_line
                and stage_plan_enabled(cfg_root)):
            try:
                from src.companion.goals.signals import no_new_info_streak as _nnis2
                _n_e = float(now if now is not None else time.time())
                _hs = list(recent_history(inbox_store, _conv_label(), limit=12)) + [
                    {"direction": "in", "text": str(inbound_text), "ts": _n_e}]
                _stk = _nnis2(_hs)
                if _stk >= 2:
                    _why_e = proactive_topic_allowed(inbox_store, _conv_label(), now=_n_e)
                    if not _why_e:
                        _persona_e: Any = None
                        try:
                            from src.utils.persona_manager import PersonaManager as _PM
                            _persona_e, _t_e = _PM.get_instance().get_persona_with_tier(
                                str(uc.get("chat_id") or chat_key or ""),
                                str(uc.get("account_persona_id") or ""))
                        except Exception:
                            _persona_e = None
                        _topic_e = pick_proactive_topic(_conv_label(), _persona_e,
                                                        inbox_store=inbox_store, now=_n_e)
                        if _topic_e:
                            probe_line = (f"【本轮必做】对方连续 {_stk} 轮只有夸赞 / 应答、没有新信息："
                                          f"接住一句就换你来带话题——{_topic_e}；{MUST_DISCIPLINE_ZH}")
                            probe_mode = probe_mode or "must"
                            logger.info(
                                "[proactive_topic] conv=%s topic=%r reason=no_new_info_x2 streak=%d goal=%s",
                                _conv_label(), _topic_e[:40], _stk, str(goal.get("goal_id") or "")[:12])
                    else:
                        logger.info("[proactive_topic] conv=%s topic=- reason=blocked:%s streak=%d",
                                    _conv_label(), _why_e, _stk)
            except Exception:
                logger.debug("proactive topic (goal path) skipped", exc_info=True)

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
        # M-5 A（#217 / D-M6，追问②）：首拍预览不扩到回复链，但「无产品禁报价」要让
        # 客户来消息时的顺势推进也吃到——目标未绑产品（参数空 / 目录无货 / custom 没写
        # 卖什么）→ 注入块加一行产品边界（prompt 级），direct/close 力度降 soft。
        # 与出站拦截 wrap_care_send 同一判定函数 goal_product_binding。
        product_rule = ""
        try:
            from src.companion.goals.product_guard import no_product_prompt_rule
            _cat_has = False
            if template.get("catalog"):
                try:
                    from src.companion.goals import site_catalog as _sc
                    _cat = _sc.load_catalog(_sc.catalog_path(
                        cfg_root, getattr(config_obj, "config_path", None)))
                    _cat_has = bool((_cat or {}).get("products"))
                except Exception:
                    _cat_has = False
            product_rule = no_product_prompt_rule(
                res.get("goal") or goal, catalog_has_products=_cat_has)
        except Exception:
            product_rule = ""
        # P27 意图×缺口合流（gap_in_intent 模板=摸底）：缺口并进今日意图——
        # 「今日意图」是 opener 转向与主动桥唯一携带的载荷，独立缺口行到不了
        # 那两处；合流后取消独立行防同块重复。none 力度日不合（「今天只陪伴」
        # 与缺口发问相矛盾）；meta 保留原始缺口值供观测（本轮瞄准哪个槽）。
        gap_for_meta = profile_gap
        if probe_line:
            # 硬注入接管本轮：意图只留基础句（剥计划子句，防「今天问坐标」与「必问职业」
            # 两句打架），软缺口行不再出
            _today_m = dict(view.get("today") or {})
            _today_m["intent"] = strip_probe_clause(str(_today_m.get("intent") or ""))
            view = dict(view)
            view["today"] = _today_m
            gap_for_meta = profile_gap or gap_for_meta
            profile_gap = ""
        elif profile_gap and template.get("gap_in_intent"):
            _today_m = dict(view.get("today") or {})
            if str(_today_m.get("push_level") or "soft") != "none":
                _today_m["intent"] = merged_beat_intent(
                    str(_today_m.get("intent") or ""), profile_gap)
                view = dict(view)
                view["today"] = _today_m
                profile_gap = ""
        block = goal_view_block(
            view,
            suppress_push=suppress,
            max_chars=int(inject_cfg.get("max_chars", 360) or 360),
            profile_gap=profile_gap,
            profile_facts=profile_facts,
            note_suffix=note_suffix,
            product_rule=product_rule,
            probe_line=probe_line,
        )
        if not block:
            _note_meta(False, "empty_block",
                       goal_id=str(goal.get("goal_id") or ""),
                       title=str(goal.get("title") or ""))
            return None
        _beat = view.get("today") or {}
        _note_meta(
            True, "",
            goal_id=str(goal.get("goal_id") or ""),
            title=str(view.get("title") or ""),
            template=str(goal.get("template") or ""),
            push_level=str(_beat.get("push_level") or "soft"),
            intent=str(_beat.get("intent") or ""),
            milestone_idx=int(view.get("milestone_idx") or 0),
            profile_gap=gap_for_meta,
            no_product=bool(product_rule),
            target_slot=probe_slot,
            probe_mode=probe_mode,
        )
        # params 落盘一次：缺口轮换（gap_patch）+ 硬注入挂起 / 清除（probe_patch）+
        # 每目标注入计数（D 段卡片「注入 N 次」——每稿 +1，与 beat_injected 每槽一次不同口径）
        params_patch: Optional[Dict[str, Any]] = None
        if probe_patch is not None:
            params_patch = dict(probe_patch)
        elif gap_patch:
            params_patch = dict(gap_patch)
        base_cnt = params_patch if params_patch is not None else dict(
            (res.get("goal") or goal).get("params") or {})
        try:
            base_cnt[INJECT_COUNT_PARAM] = int(base_cnt.get(INJECT_COUNT_PARAM) or 0) + 1
        except (TypeError, ValueError):
            base_cnt[INJECT_COUNT_PARAM] = 1
        params_patch = base_cnt
        if params_patch is not None:
            try:
                gid = str((res.get("goal") or goal).get("goal_id") or "")
                if gid:
                    store.update_goal_fields(gid, params=params_patch)
            except Exception:
                logger.debug("persist gap_asked failed", exc_info=True)

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
                # persona_id 必须显式传（#145③ 修复的另一半）：pick_products 的
                # 人设过滤是 fail-closed——不知道当前人设时，**绑定了任何人设的
                # 货一条都不入选**。此前这里不传，回落 fields._persona_id 又从没
                # 有人写过 → 绑定货在选品链上全黑，运营配了也永远推不出来。
                prods = sc.pick_products(
                    catalog, prof_fields, pinned=pinned, sold=sold,
                    persona_id=catalog_persona_id)
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
            # M-7 A（#236）：这一槽位的方向第一次被织进拟稿 → 落一条可追溯事件
            # （每槽位一次，不按每稿刷）。拍清单据此把「回复带方向」与「主动真发」
            # 并排列出——卡片上「已推进 2 拍」到底是两条主动消息还是两次顺势带入，
            # 用户能看见。
            try:
                store.add_event(
                    str(goal.get("goal_id") or ""), "beat_injected",
                    f"{chain}:{str(action.get('day') or '')}"[:60],
                    conversation_id=conversation_id
                    or f"{platform}:{account_id}:{chat_key}",
                    text_head=str((view.get("today") or {}).get("intent")
                                  or "")[:120],
                    now=now)
            except Exception:
                logger.debug("beat_injected event skipped", exc_info=True)
        get_goal_stats().record_injected(chain)
        return block
    except Exception as exc:  # noqa: BLE001
        # #166：此前 DEBUG 吞掉＝注入链任何一环炸了都与「无目标」在日志里同样
        # 无声（skill_manager._inject_goal_context 那层也是 debug）。升 WARNING
        # 带异常类名 + 查找键；首次带堆栈，同会话 10 分钟内只记一行。契约不变：
        # 绝不抛（目标层挂了不能拖垮回复主链）。
        try:
            _first = _inject_log_allowed(f"error|{_conv_label()}", now)
            logger.warning(
                "[goal-inject] ERROR %s: %s conv=%s plat=%s chat_key=%s acct=%s "
                "chain=%s",
                type(exc).__name__, str(exc)[:160], conversation_id or "-",
                platform, chat_key, account_id or "-", chain,
                exc_info=_first)
        except Exception:
            pass
        try:
            # 直接覆写：异常路径返回 None（块没出去），哪怕成功元数据已写过
            # 也已失真——统一按 error 记，绝不留「injected=True 却没块」的谎
            if isinstance(user_context, dict):
                user_context["_goal_inject_meta"] = {
                    "injected": False, "reason": "error",
                    "error": type(exc).__name__}
        except Exception:
            pass
        return None


# 生命周期自动目标的 created_by 集合（churn_reason 采集门控：只在这些
# 会话里「为什么没续」才是真流失信号，普通获客会话不采）
_LIFECYCLE_CREATORS = ("retention_auto", "winback_auto", "reconvert_auto")

# M-5 A 追问①（#217，老板拍板：闸）：用户版（client 形态）后台生命周期自建也不许建
# 「已下线」类目的模板——否则 auto_create / retention / reconvert 一开就建出一张
# 用户版建不了、卡上标「已下线」的转化目标，冲刺引擎照样拿它推销。按 (creator,
# template) 每进程只记一次 INFO，不刷屏（这些开关默认全关，命中本就少）。
_LIFECYCLE_BLOCK_LOGGED: set = set()


def _lifecycle_template_blocked(cfg_root: Any, template_id: str, creator: str) -> bool:
    try:
        from src.companion.goals.product_guard import lifecycle_template_blocked
        blocked = lifecycle_template_blocked(cfg_root, template_id)
    except Exception:
        return False
    if blocked:
        key = (str(creator), str(template_id))
        if key not in _LIFECYCLE_BLOCK_LOGGED:
            _LIFECYCLE_BLOCK_LOGGED.add(key)
            logger.info("[goal-auto] 用户版已下线模板 %s：%s 自建跳过（#217 D-M6，"
                        "改用自定义目标或 partner/internal 形态）", template_id, creator)
    return blocked


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


def _camp_term_list(cfg_root: Any, config_path: Any = None) -> List[str]:
    """自家阵营词表（#147；单一出口 ``offers.camp_terms``：登记活动 + 当日 P13
    活动 + 目录产品名）。绝不抛。"""
    try:
        from src.companion.goals import offers as offers_mod
        from src.companion.goals import site_catalog as sc
        return offers_mod.camp_terms(
            sc.load_catalog(sc.catalog_path(cfg_root, config_path)))
    except Exception:
        return []


def camp_guard_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.camp_guard`` 段（缺/坏 → {}；默认全开）。

    - ``enabled``（默认 True）：出站贬损守卫总开关；
    - ``prompt``（默认 True）：人设 prompt 注入「自家在推活动」块；
    - ``prompt_mode``（``always`` | ``on_mention``，默认 always）：块是每轮都注还是
      只在客户本条/近轮提到登记词时注。always 才兜得住实录里「客户说『跟垃圾邮件
      竞争』、AI 接『I'll take that as a win』」那种不点名的后续轮；块本身只在有
      登记时存在，空登记零 token。
    """
    try:
        if not isinstance(cfg_root, dict):
            return {}
        cg = (((cfg_root.get("companion") or {}).get("goals") or {})
              .get("camp_guard"))
        return cg if isinstance(cg, dict) else {}
    except Exception:
        return {}


def build_camp_block_for_chat(
    cfg_root: Any, config_path: Any = None, *,
    lang: str = "zh", inbound_text: str = "",
    history_texts: Optional[List[str]] = None,
) -> str:
    """#147 第一层：人设 prompt 的「自家阵营在推活动」硬约束块（无登记 → ""）。

    ``on_mention`` 模式下只在客户本条 / 近轮（``history_texts``）提到登记词时给块；
    ``always``（默认）有登记即给。任何异常 → ""（prompt 组装绝不因目录层挂掉）。
    """
    try:
        cg = camp_guard_cfg(cfg_root)
        if not bool(cg.get("prompt", True)):
            return ""
        from src.companion.goals import offers as offers_mod
        from src.companion.goals import site_catalog as sc
        catalog = sc.load_catalog(sc.catalog_path(cfg_root, config_path))
        block = offers_mod.camp_block(catalog, lang=lang)
        if not block:
            return ""
        mode = str(cg.get("prompt_mode") or "always").strip().lower()
        if mode == "on_mention":
            terms = [t.lower() for t in offers_mod.camp_terms(catalog)]
            pool = [str(inbound_text or "")] + [str(x or "") for x in (history_texts or [])]
            low = "\n".join(pool).lower()
            if not any(t in low for t in terms):
                return ""
        return block
    except Exception:
        return ""


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
        # #147：自家阵营词表——出站贬损守卫的命中面（与上面三项同源同文件）
        "camp_terms": _camp_term_list(cfg_root, config_path),
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
        if _lifecycle_template_blocked(cfg_root, template_id, "retention_auto"):
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
        if _lifecycle_template_blocked(cfg_root, wb_template_id, "winback_auto"):
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
        if _lifecycle_template_blocked(cfg_root, template_id, "reconvert_auto"):
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

    parts = ref.split(":", 2)
    has_parts = len(parts) == 3 and parts[0].strip() and parts[1].strip()
    goal = store.find_active_goal(conversation_id=ref)
    if goal is None and has_parts:
        goal = store.find_active_goal(
            platform=parts[0].strip(), chat_key=parts[2].strip(),
            account_id=parts[1].strip())

    # ── 迟到订单对账（P6 2026-08-09）：ref 匹配不上**活跃**目标时，找近窗内
    # expired/failed 的同会话目标复活为 done——付费是硬事实，到期只是跟踪窗
    # 先关了（生产实锤：acquire 10 天到期批量「失守」，而官网成交在站外，
    # 晚到的单此前只记一行 matched=False 日志＝赢单被永久记成流失）。
    # cancelled 刻意不复活（运营手动叫停是人的明示决定）；窗宽
    # ``companion.goals.order_late_settle_days``（默认 14，0=关）。
    late = False
    if goal is None:
        try:
            late_days = float(resolve_goals_cfg(cfg_root).get(
                "order_late_settle_days", 14) or 0)
        except (TypeError, ValueError):
            late_days = 14.0
        if late_days > 0:
            since = ts - late_days * 86400.0
            goal = store.find_recent_terminal_goal(
                conversation_id=ref, since_ts=since)
            if goal is None and has_parts:
                goal = store.find_recent_terminal_goal(
                    platform=parts[0].strip(), chat_key=parts[2].strip(),
                    account_id=parts[1].strip(), since_ts=since)
            late = goal is not None

    try:
        get_goal_stats().record_order(matched=goal is not None, late=late)
    except Exception:
        pass
    if goal is None:
        logger.info("[goal-order] 未匹配到活跃/近窗终态目标 ref=%s order=%s",
                    ref, order_id or "-")
        return out

    gid = str(goal.get("goal_id") or "")
    out["matched"] = True
    out["goal_id"] = gid
    out["late"] = late
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
    if late:
        # 复活痕迹显式进台账（报表/周审能看见「这单是迟到结算捞回来的」）
        store.add_event(
            gid, "status",
            f"{goal.get('status')}->done:late_order")
    try:
        get_goal_stats().record_terminal("done")
    except Exception:
        pass
    logger.info("[goal-order] 目标 %s 经订单回流结算 done（order=%s plan=%s%s）",
                gid, order_id or "-", plan or "-",
                " late=1" if late else "")
    # P5 留存环：成交即起下一周期（retention 默认关；cfg 未传=旧行为）
    if cfg_root is not None:
        maybe_create_retention_goal(
            store, cfg_root, goal, plan=plan, period=period, now=ts)
    return out


# ── M-7 A（#236）：每拍可追溯 ────────────────────────────────────────────────

# 拍清单读的事件种类（``beat_sent`` 主动真发 / ``beat_injected`` 回复链带方向 /
# ``beat_blocked`` 想出手被拦 / product_guard 的两种拦下）——白名单查询，别的事件
# （created/updated/miss_notified…）再多也挤不掉拍。
BEAT_TRACE_KINDS = (
    "beat_sent", "beat_injected", "beat_blocked",
    "goal_no_product", "first_send_preview",
    PROBE_EVENT_ASKED, PROBE_EVENT_MISSED, PROBE_EVENT_COVERED, PROBE_EVENT_DEEPEN,
)

_DEFERRED_STATUS_MAP = {
    "sent": "sent", "pending": "queued", "failed": "failed",
    "expired": "expired", "cancelled": "cancelled",
}


def _parse_beat_detail(detail: str) -> Tuple[str, int]:
    """``sprint:p1 care#12`` → ("sprint:p1", 12)；无 care# → (detail, 0)。"""
    s = str(detail or "").strip()
    care_id = 0
    if " care#" in s:
        head, _, tail = s.rpartition(" care#")
        try:
            care_id = int(tail.strip())
            s = head
        except ValueError:
            care_id = 0
    return s, care_id


def _resolve_outbox_row(care_row: Dict[str, Any], outbox_store: Any) -> Dict[str, Any]:
    """care 行 ``note=deferred:<row>`` → deferred_outbox 行（投递真相）。"""
    note = str((care_row or {}).get("note") or "")
    if not note.startswith("deferred:") or outbox_store is None:
        return {}
    try:
        rid = int(note.split(":", 1)[1].strip())
    except (TypeError, ValueError):
        return {}
    try:
        rows = outbox_store.get_by_ids([rid]) or {}
        return dict(rows.get(rid) or {})
    except Exception:
        return {}


def _find_outbound_message_id(
    inbox_store: Any, conversation_id: str, text: str, around_ts: float,
    *, window_sec: float = 1800.0,
) -> str:
    """按「同会话 + 出站 + 正文相同/同头 + 时刻邻近」找平台消息行 id（跳转用）。
    找不到 → ""。best-effort，绝不抛。"""
    if inbox_store is None or not conversation_id or not str(text or "").strip():
        return ""
    try:
        msgs = inbox_store.list_recent_messages(conversation_id, limit=60) or []
    except Exception:
        return ""
    want = str(text or "").strip()
    head = want[:40]
    best = ""
    best_dt = None
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if str(m.get("direction") or "") != "out":
            continue
        body = str(m.get("text") or m.get("content") or "").strip()
        if not body:
            continue
        if body != want and not (head and body.startswith(head)):
            continue
        try:
            ts = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if around_ts > 0 and ts > 0 and abs(ts - around_ts) > window_sec:
            continue
        dt = abs(ts - around_ts) if (around_ts > 0 and ts > 0) else 0.0
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = str(m.get("message_id") or m.get("platform_msg_id") or "")
    return best


def build_beats_trace(
    store: Any,
    goal: Dict[str, Any],
    *,
    care_store: Any = None,
    outbox_store: Any = None,
    inbox_store: Any = None,
    now: Optional[float] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """目标「每一拍」清单 + 汇总（``GET /api/goals/{id}/beats`` / 卡片展开共用）。

    数据源只有 gstore 事件（与看门狗 ``collect_send_liveness`` 同一表同一 kind），
    卡片上的 N 与看门狗的 sent_24h 从此一个口径。每一拍：
    - ``sent``：主动真发（``beat_sent``）。经 ``care#`` 反查 care 行拿话术快照
      （``sent_text``）与 ``note=deferred:<row>`` → deferred_outbox 行 → 真投递状态
      （sent / queued / failed / expired）；投出去的再按正文+时刻在 inbox 里找
      出站消息行 id（``message_id``，找到即回填事件列，下次直读）。
    - ``injected``：客户来消息时把方向织进拟稿（``beat_injected``，每槽位记一次）。
    - ``blocked``：到点想出手被闸拦下（``beat_blocked`` + product_guard 两种）。
    - ``preview``：首条真发进 L1 草稿待坐席过目（``first_send_preview``）。
    绝不抛：任何反查失败只让该拍少几个字段。
    """
    n = float(now if now is not None else time.time())
    gid = str((goal or {}).get("goal_id") or "")
    out: Dict[str, Any] = {
        "goal_id": gid, "beats": [],
        "summary": {"sent": 0, "delivered": 0, "injected": 0, "blocked": 0,
                    "preview": 0, "probe_asked": 0, "probe_missed": 0,
                    "probe_covered": 0, "probe_deepen": 0,
                    "blocked_by": {}, "today": {
                        "sent": 0, "injected": 0, "blocked": 0,
                        "probe_asked": 0, "probe_missed": 0,
                        "probe_covered": 0, "probe_deepen": 0,
                        "blocked_by": {}}},
    }
    if not gid:
        return out
    try:
        events = store.list_events(gid, limit=limit, kinds=BEAT_TRACE_KINDS) or []
    except Exception:
        events = []
    lt = time.localtime(n)
    day0 = n - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    conv_default = str((goal or {}).get("conversation_id") or "")
    beats: List[Dict[str, Any]] = []
    n_sent = 0
    summ = out["summary"]
    today = summ["today"]
    for ev in reversed(events):            # list_events 倒序 → 时间正序
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        try:
            ts = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        item: Dict[str, Any] = {
            "event_id": ev.get("id"), "ts": round(ts, 1),
            "conversation_id": str(ev.get("conversation_id") or conv_default),
            "text_head": str(ev.get("text_head") or ""),
            "message_id": str(ev.get("message_id") or ""),
            "phase": "", "reason": "", "status": "", "care_id": 0,
        }
        is_today = ts >= day0
        if kind == "beat_sent":
            n_sent += 1
            item["kind"] = "sent"
            item["n"] = n_sent
            phase, care_id = _parse_beat_detail(ev.get("detail"))
            item["phase"] = phase
            item["care_id"] = care_id
            item["status"] = "queued"
            care_row: Dict[str, Any] = {}
            if care_store is not None and care_id > 0:
                try:
                    care_row = dict(care_store.get(care_id) or {})
                except Exception:
                    care_row = {}
            if care_row:
                if not item["text_head"]:
                    item["text_head"] = str(care_row.get("sent_text") or "")[:120]
                # O-3 E：坐席「现在就问一个」行（goal:{gid}:q{ts}）——sent_hook 走通用分支
                # 记成 care:link，这里按 care 行 topic_norm 回标 probe:manual
                try:
                    from src.companion.goals.sprint_ticker import parse_goal_care_kind
                    if parse_goal_care_kind(care_row.get("topic_norm"))[0] == "probe":
                        item["phase"] = "probe:manual"
                except Exception:
                    pass
                if str(care_row.get("status") or "") == "skipped":
                    # 入队后又被守卫/坐席拦下（note 带原因）
                    item["status"] = "blocked"
                    item["reason"] = str(care_row.get("note") or "")[:60]
                ob = _resolve_outbox_row(care_row, outbox_store)
                if ob:
                    item["status"] = _DEFERRED_STATUS_MAP.get(
                        str(ob.get("status") or ""), item["status"])
                    if not item["text_head"]:
                        item["text_head"] = str(ob.get("reply_text") or "")[:120]
                    try:
                        sa = float(ob.get("sent_at") or 0)
                    except (TypeError, ValueError):
                        sa = 0.0
                    if sa > 0:
                        item["sent_at"] = round(sa, 1)
                    if item["status"] == "failed":
                        item["reason"] = str(ob.get("error") or "")[:80]
                    if item["status"] == "sent" and not item["message_id"]:
                        mid = _find_outbound_message_id(
                            inbox_store, item["conversation_id"],
                            str(ob.get("reply_text") or care_row.get("sent_text") or ""),
                            sa or ts)
                        if mid:
                            item["message_id"] = mid
                            try:
                                store.set_event_message_id(int(ev.get("id")), mid)
                            except Exception:
                                pass
            summ["sent"] += 1
            if item["status"] == "sent":
                summ["delivered"] += 1
            if is_today:
                today["sent"] += 1
        elif kind == "beat_injected":
            item["kind"] = "injected"
            item["phase"] = "reply"
            item["status"] = "injected"
            item["reason"] = str(ev.get("detail") or "")[:60]
            summ["injected"] += 1
            if is_today:
                today["injected"] += 1
        elif kind == "beat_blocked":
            item["kind"] = "blocked"
            det = str(ev.get("detail") or "")
            reason, _, slot = det.partition("@")
            item["reason"] = reason.strip() or "unknown"
            item["phase"] = slot.strip()
            item["status"] = "blocked"
        elif kind == "goal_no_product":
            item["kind"] = "blocked"
            item["reason"] = "goal_no_product"
            item["status"] = "blocked"
            item["text_head"] = str(ev.get("detail") or "")[:120]
        elif kind == "first_send_preview":
            item["kind"] = "preview"
            item["reason"] = "first_send_preview"
            item["status"] = "preview_pending"
            item["text_head"] = str(ev.get("detail") or "")[:120]
            summ["preview"] += 1
        elif kind in (PROBE_EVENT_ASKED, PROBE_EVENT_MISSED, PROBE_EVENT_COVERED,
                      PROBE_EVENT_DEEPEN):
            # O-3 C：硬注入的摸底问句出站校验——asked（真问了）/ missed（回复没带问题）；
            # Q-1：covered（没问但顺着话头盖到了槽位词）/ deepen（已提及槽的深化提法）
            item["kind"] = "probe"
            det = str(ev.get("detail") or "")
            slot, _, cue = det.partition("@")
            item["phase"] = slot.strip()
            item["reason"] = cue.strip() if cue.strip() not in ("", "-") else ""
            item["status"] = {PROBE_EVENT_ASKED: "asked", PROBE_EVENT_COVERED: "covered",
                              PROBE_EVENT_DEEPEN: "deepen"}.get(kind, "missed")
            key = f"probe_{item['status']}"
            summ[key] = int(summ.get(key) or 0) + 1
            if is_today:
                today[key] = int(today.get(key) or 0) + 1
        else:
            continue
        if item["kind"] == "blocked":
            summ["blocked"] += 1
            summ["blocked_by"][item["reason"]] = (
                summ["blocked_by"].get(item["reason"], 0) + 1)
            if is_today:
                today["blocked"] += 1
                today["blocked_by"][item["reason"]] = (
                    today["blocked_by"].get(item["reason"], 0) + 1)
        beats.append(item)
    out["beats"] = beats
    return out


__all__ = [
    "AGENDA_STATES",
    "BEAT_TRACE_KINDS",
    "DEFAULT_DB_NAME",
    "NEGATIVE_EMOTIONS",
    "WON_META_NOTE_MAX",
    "WON_META_PRODUCT_MAX",
    "agenda_counts",
    "agenda_item",
    "agenda_sort_key",
    "agenda_state_match",
    "beat_feedback_state",
    "INJECT_COUNT_PARAM",
    "PROBE_EVENT_ASKED",
    "PROBE_EVENT_MISSED",
    "PROBE_PENDING_PARAM",
    "build_beats_trace",
    "build_block_for_chat",
    "capture_status",
    "catalog_guard_facts",
    "decide_probe_target",
    "discovery_gap_for_goal",
    "discovery_probe_asks",
    "recent_history",
    "DISCOVERY_PAUSE_KEY",
    "discovery_paused",
    "set_discovery_paused",
    "confirm_profile_slot",
    "PROBE_EVENT_COVERED",
    "PROBE_EVENT_DEEPEN",
    "PROBE_RETRY_PER_DAY",
    "verify_pending_probe",
    "get_configured_store",
    "goal_view",
    "merged_beat_intent",
    "goals_enabled",
    "is_negative_emotion",
    "maybe_auto_create_goal",
    "maybe_create_retention_goal",
    "maybe_resume_linked_goal",
    "maybe_spawn_reconvert",
    "refresh_goal",
    "resolve_plan_product",
    "retention_days_for_period",
    "run_winback_scan",
    "resolve_db_path",
    "resolve_goals_cfg",
    "resolve_inject_lookup_keys",
    "sanitize_won_meta",
    "settle_order_ref",
    "sold_plan_counts_cached",
    "sprint_engine_status",
]
