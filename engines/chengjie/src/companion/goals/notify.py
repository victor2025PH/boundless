"""目标完成的「发现→提醒」闭环（P0 2026-08-09）。

两个正交环节，各自独立开关（新子系统默认关，基线见 config.example.yaml）：

- **定时结算扫描** ``settle_sweep``（``companion.goals.sweep``）：settle-on-read
  的补丁——完成判定原本只在有人打开会话/读接口时发生，客户半夜付费而坐席
  次日没点开那个会话，系统就永远不知道目标完成了。扫描把「读」定时化：
  每轮按 ``updated_at`` 最旧优先取预算内的 active 目标过一遍
  ``service.refresh_goal``（幂等，与右栏卡/看板同一条结算路），陈旧度有界。

- **完成通知扫描** ``scan_and_notify``（``companion.goals.notify``）：对
  「已 done 且未通知过」的目标发布 EventBus ``goal_completed_alert``。
  刻意做成**扫描器而非跃迁点埋点**——完成路径有三条且会继续长
  （settle-on-read / 订单回流 settle_order_ref / 人工成交 status 路由），
  在每处埋发布必然漏；扫描器 + ``goal_events(kind=completed_notified)``
  幂等标记把所有现在与将来的路径一网打尽，至多一个 tick 的通知时延。
  失败/过期刻意不逐条通知（坏消息聚合进日报是 P1）——完成是低频好消息，
  逐条推才有价值。

两函数都绝不抛：目标层任何失败不能拖垮 ScheduledReporter 主循环。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.companion.goals.service import (
    goals_enabled,
    resolve_goals_cfg,
)
from src.companion.goals.store import GoalStore
from src.companion.goals.templates import get_template

logger = logging.getLogger("src.companion.goals.notify")

NOTIFIED_EVENT_KIND = "completed_notified"
MISS_EVENT_KIND = "miss_notified"
# M-7 C（#236）：到期 / 失守 / 完成当刻的结算摘要（事件 detail=紧凑 JSON）+ 工作台事件
SETTLEMENT_EVENT_KIND = "settlement"
SETTLED_ALERT = "goal_settled_alert"
SETTLEMENT_VISIBLE_SEC = 7 * 86400.0

__all__ = [
    "AUTO_CREATED_BY",
    "MISS_EVENT_KIND",
    "NOTIFIED_EVENT_KIND",
    "SETTLED_ALERT",
    "SETTLEMENT_EVENT_KIND",
    "SETTLEMENT_VISIBLE_SEC",
    "build_completion_payload",
    "build_settlement",
    "read_settlement",
    "settle_and_notify",
    "build_slots_brief",
    "resolve_agent_push_target",
    "resolve_extra_push_targets",
    "resolve_notify_cfg",
    "resolve_sweep_cfg",
    "result_kind",
    "run_scan_loop",
    "sanitize_notify_extra",
    "scan_and_notify",
    "scan_miss_digest",
    "scan_sprint_miss",
    "scan_tick",
    "settle_sweep",
    "triage_missed",
    "user_store_for",
]


def resolve_sweep_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.sweep`` 配置（缺省全关；数值夹紧防配坏）。"""
    cfg = resolve_goals_cfg(cfg_root)
    raw = cfg.get("sweep") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    try:
        budget = int(raw.get("budget", 40) or 40)
    except (TypeError, ValueError):
        budget = 40
    try:
        interval = int(raw.get("interval_ticks", 5) or 5)
    except (TypeError, ValueError):
        interval = 5
    return {
        "enabled": bool(raw.get("enabled", False)),
        "budget": max(1, min(budget, 200)),
        "interval_ticks": max(1, min(interval, 120)),
    }


def resolve_notify_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.notify`` 配置（缺省全关）。

    ``lookback_hours``：只补发这窗口内完成的目标——防「首次开闸/停机多日后」
    把陈年旧账一次性轰成告警风暴；窗口外的完成仍会被报表如实展示。

    失守日报（P1）：``miss_digest`` 默认随父开关开——failed/expired **聚合**成
    一条 ``goal_miss_alert``（坏消息逐条推=告警风暴，很快被无视）；触发判据＝
    未通报条数 ≥ ``miss_min_count`` **或** 最老一条压过 ``miss_max_age_hours``
    （小流量也保证至少按天出账，不会攒到永远）。"""
    cfg = resolve_goals_cfg(cfg_root)
    raw = cfg.get("notify") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    try:
        lookback = float(raw.get("lookback_hours", 72) or 72)
    except (TypeError, ValueError):
        lookback = 72.0
    try:
        per_scan = int(raw.get("max_per_scan", 10) or 10)
    except (TypeError, ValueError):
        per_scan = 10
    try:
        miss_min = int(raw.get("miss_min_count", 3) or 3)
    except (TypeError, ValueError):
        miss_min = 3
    try:
        miss_age = float(raw.get("miss_max_age_hours", 24) or 24)
    except (TypeError, ValueError):
        miss_age = 24.0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "lookback_hours": max(1.0, min(lookback, 24 * 30.0)),
        "max_per_scan": max(1, min(per_scan, 50)),
        "miss_digest": bool(raw.get("miss_digest", True)),
        "miss_min_count": max(1, min(miss_min, 50)),
        "miss_max_age_hours": max(1.0, min(miss_age, 24 * 7.0)),
        # P2 2026-08-18：完成推送的「责任坐席副本」——按 目标创建人→会话认领
        # 坐席 解析绑定的 Telegram 通知号随 payload 下发（webhook 侧加发一份；
        # 管理员渠道照常全量收）。默认关；坐席未绑定=天然回落只推管理员。
        "push_agent": bool(raw.get("push_agent", False)),
        # P3 2026-08-18：完成推送带「摸底要点」（已采画像事实一行，facts_line
        # 口径）。**默认关**——画像值是客户数据，出境到外部 IM 必须显式 opt-in；
        # 关时推送只有模板/金额/用时等目标层事实，不含画像原值。
        "include_profile": bool(raw.get("include_profile", False)),
    }


def settle_sweep(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    now: Optional[float] = None,
    cursor: int = 0,
    budget: Optional[int] = None,
) -> Dict[str, int]:
    """按稳定序游标结算一页 active 目标（调用方持 ``cursor`` 轮转）。

    返回 ``{settled, next_cursor}``——页满则游标推进，页短/扫空则归零重转
    （active 数超预算也没有目标会饿死；2026-08-09 修正首版「updated_at 最旧
    优先」的饿死缺陷：refresh 无变化时不写库，最旧的永远最旧）。绝不抛。"""
    out = {"settled": 0, "next_cursor": 0}
    try:
        if not goals_enabled(cfg_root):
            return out
        cfg = resolve_sweep_cfg(cfg_root)
        if not cfg["enabled"]:
            return out
        n = float(now if now is not None else time.time())
        lim = int(budget if budget is not None else cfg["budget"])
        cur = max(0, int(cursor or 0))
        rows = store.list_active_page(offset=cur, limit=lim)
        if not rows and cur > 0:            # 越过队尾 → 回卷重扫
            cur = 0
            rows = store.list_active_page(offset=0, limit=lim)
        if not rows:
            return out
        from src.companion.goals import service as goal_service
        for goal in rows:
            try:
                goal_service.refresh_goal(
                    store, cfg_root, goal, inbox_store=inbox_store, now=n)
                out["settled"] += 1
            except Exception:
                logger.debug("settle_sweep item skipped", exc_info=True)
        out["next_cursor"] = cur + len(rows) if len(rows) >= lim else 0
    except Exception:
        logger.debug("settle_sweep failed", exc_info=True)
    return out


def result_kind(result: Any) -> str:
    """result 串 → 完成方式粗分类（order 订单回流 / manual 人工成交 / auto 信号结算）。"""
    r = str(result or "")
    if r.startswith("order:"):
        return "order"
    if r.startswith("manual:"):
        return "manual"
    return "auto" if r else ""


# ── 责任坐席解析（P2 2026-08-18：完成推送的定向副本）────────────────────────

# created_by 里的「非人」来源（生命周期自建 + 批量口）；这些值不是用户名，
# 解析时一律跳过。人建目标存的是 session 用户名（goal_routes actor）。
AUTO_CREATED_BY = frozenset(
    {"", "batch", "auto_create", "winback_auto", "retention_auto",
     "reconvert_auto"})

_USER_STORE_CACHE: Dict[str, Any] = {}


def user_store_for(config_path: Any) -> Any:
    """web_users.db 惰性单例（按运行时目录缓存；任何失败返 None 绝不抛）。

    与 admin/unified_inbox_auth 同一寻址：``runtime_dir(config_path)/web_users.db``。
    桌面 YAML 在数据根内时这就是配置目录；VPS 分裂布局时落在 ``AITR_DATA_DIR``。"""
    try:
        if not config_path:
            return None
        from pathlib import Path
        from src.licensing.data_paths import runtime_dir
        key = str(runtime_dir(config_path))
        st = _USER_STORE_CACHE.get(key)
        if st is None:
            from src.utils.web_user_store import WebUserStore
            st = WebUserStore(Path(key) / "web_users.db")
            _USER_STORE_CACHE[key] = st
        return st
    except Exception:
        logger.debug("user_store_for failed", exc_info=True)
        return None


def resolve_agent_push_target(
    goal: Dict[str, Any],
    *,
    inbox_store: Any = None,
    user_store: Any = None,
) -> Dict[str, str]:
    """「这单该额外通知哪个坐席」解析：目标创建人（人建）→ 会话认领坐席 → 无。

    命中的用户须**启用中**且绑定了 Telegram 通知号（web_users.notify_tg_chat_id，
    用户管理页「通知」按钮维护），返回 ``{"agent_chat_id","agent_username"}``；
    任何一环缺失返回 ``{}``＝回落只推管理员渠道（与 P0 行为完全一致）。绝不抛。
    刻意不解析 AI 自建目标的「账号 owner」——auto_create 的目标没有责任人语义，
    硬派发只会把好消息变成无人认领的噪音。"""
    try:
        candidates: List[str] = []
        raw = str(goal.get("created_by") or "").strip()
        if raw.endswith(":batch"):
            raw = raw[: -len(":batch")].strip()
        if raw and raw not in AUTO_CREATED_BY:
            candidates.append(raw)
        conv = str(goal.get("conversation_id") or "")
        getter = getattr(inbox_store, "get_conversation_claim", None) \
            if inbox_store is not None else None
        if conv and callable(getter):
            try:
                claim = getter(conv) or {}
                agent = str(claim.get("agent_id") or "").strip()
                if agent and agent not in candidates:
                    candidates.append(agent)
            except Exception:
                logger.debug("claim lookup failed", exc_info=True)
        if user_store is None or not candidates:
            return {}
        for name in candidates:
            try:
                u = user_store.get_user(name)
            except Exception:
                u = None
            if not isinstance(u, dict) or not u.get("enabled", 1):
                continue
            chat = str(u.get("notify_tg_chat_id") or "").strip()
            if chat:
                return {"agent_chat_id": chat, "agent_username": name}
        return {}
    except Exception:
        logger.debug("resolve_agent_push_target failed", exc_info=True)
        return {}


# ── 逐目标指定收件人（P3 2026-08-18：params.notify_extra）──────────────────
# 语义：目标上显式点名的「达成后额外通知谁」——条目是 web 用户名（经
# web_users 解析绑定的 notify_tg_chat_id）或裸 Telegram chat_id（正/负整数）。
# webhook 侧借订阅了该事件的 telegram 渠道 bot 逐个加发（与 agent_chat_id
# 副本同机制）；解析不到的条目静默跳过=回落只推管理员渠道，绝不因此丢主推。

_EXTRA_TARGET_CAP = 5
_CHAT_ID_RE = None  # 惰性编译（模块 import 期零 re 开销）


def sanitize_notify_extra(raw: Any) -> List[str]:
    """目标 ``params.notify_extra``（列表或逗号/顿号分隔串）→ 清洗后的条目表。

    保序去重、单条 ≤64 字、最多 ``_EXTRA_TARGET_CAP`` 条；条目形态不在这里
    判定（用户名还是 chat_id 由解析时决定）——写入口（create/update 路由）与
    读出口（scan_and_notify）共用本函数，存进库的永远是干净形状。空 → []。"""
    import re as _re
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = _re.split(r"[,，、;；\n]+", str(raw or ""))
    out: List[str] = []
    for it in items:
        v = str(it or "").strip()[:64]
        if v and v not in out:
            out.append(v)
        if len(out) >= _EXTRA_TARGET_CAP:
            break
    return out


def resolve_extra_push_targets(
    goal: Dict[str, Any], *, user_store: Any = None,
) -> List[str]:
    """``params.notify_extra`` → 可直投的 Telegram chat_id 列表。

    条目判定：纯 ``-?\\d{4,20}`` 视为裸 chat_id 直用；其余当 web 用户名查
    ``web_users``（须启用中且绑定 notify_tg_chat_id，与责任坐席副本同判据）。
    user_store 缺席时用户名条目跳过（chat_id 条目不受影响）。去重、封顶、
    绝不抛。"""
    global _CHAT_ID_RE
    try:
        if _CHAT_ID_RE is None:
            import re as _re
            _CHAT_ID_RE = _re.compile(r"^-?\d{4,20}$")
        params = goal.get("params") if isinstance(goal.get("params"), dict) \
            else {}
        entries = sanitize_notify_extra((params or {}).get("notify_extra"))
        out: List[str] = []
        for ent in entries:
            chat = ""
            if _CHAT_ID_RE.match(ent):
                chat = ent
            elif user_store is not None:
                try:
                    u = user_store.get_user(ent)
                except Exception:
                    u = None
                if isinstance(u, dict) and u.get("enabled", 1):
                    chat = str(u.get("notify_tg_chat_id") or "").strip()
            if chat and chat not in out:
                out.append(chat)
            if len(out) >= _EXTRA_TARGET_CAP:
                break
        return out
    except Exception:
        logger.debug("resolve_extra_push_targets failed", exc_info=True)
        return []


def build_slots_brief(store: GoalStore, goal: Dict[str, Any]) -> str:
    """完成推送的「摸底要点」一行（``include_profile`` 开时才被调用）。

    与聊天注入同一口径 ``profile_slots.facts_line``（「称呼:阿龙｜业务痛点:
    客服人手」），不另造格式。无画像/异常 → ""。"""
    try:
        from src.companion.goals.profile_slots import facts_line
        prof = store.get_customer_profile(
            str(goal.get("platform") or ""), str(goal.get("chat_key") or ""))
        return facts_line(dict((prof or {}).get("fields") or {}), limit=4)
    except Exception:
        logger.debug("build_slots_brief failed", exc_info=True)
        return ""


def _display_names(
    inbox_store: Any, conversation_ids: List[str],
) -> Dict[str, str]:
    """批量客户展示名（与 goal_routes._agenda_peer_names 同口径，软失败返空）。"""
    try:
        ids = [c for c in conversation_ids if c]
        if not ids or inbox_store is None:
            return {}
        out: Dict[str, str] = {}
        for cid, row in (inbox_store.get_conversations_for_ids(ids) or {}).items():
            row = row or {}
            name = str(row.get("display_name") or row.get("name") or "").strip()
            if name:
                out[str(cid)] = name
        return out
    except Exception:
        logger.debug("goal notify names enrichment failed", exc_info=True)
        return {}


def build_completion_payload(
    goal: Dict[str, Any],
    *,
    contact_name: str = "",
    won_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """完成事件 payload（webhook formatter / 坐席铃铛 / e2e 门禁共用契约）。"""
    tid = str(goal.get("template") or "")
    template = get_template(tid) or {}
    done_at = float(goal.get("done_at") or 0.0)
    start = float(goal.get("start_ts") or 0.0)
    days = round((done_at - start) / 86400.0, 1) if done_at > 0 and start > 0 \
        else None
    meta = won_meta or {}
    account_id = str(goal.get("account_id") or "")
    gid = str(goal.get("goal_id") or "")
    return {
        "goal_id": gid,
        "conversation_id": str(goal.get("conversation_id") or ""),
        "platform": str(goal.get("platform") or ""),
        "account_id": account_id,
        "chat_key": str(goal.get("chat_key") or ""),
        "contact_name": str(contact_name or ""),
        "template": tid,
        "template_name": str(template.get("name_zh") or tid),
        "title": str(goal.get("title") or ""),
        "result": str(goal.get("result") or ""),
        "result_kind": result_kind(goal.get("result")),
        "won": result_kind(goal.get("result")) in ("order", "manual"),
        "amount": meta.get("amount"),
        "product": str(meta.get("product") or ""),
        # N-3 #241：陪伴域「标记达成」的达成结果标签（关系升温 / 见面 / 转付费陪伴 …）
        "outcome": str(meta.get("outcome") or ""),
        "days_to_done": days,
        "done_at": done_at,
        # rate_key 按「账号+目标」粒度（2026-08-18 收窄）：每个目标一生只完成一次
        # （幂等标记已保证），完成推送是收入时刻——旧的纯账号粒度会把同账号一小时内
        # **第二个客户**的成交静默吞掉（webhook _RateLimiter 窗口 1h）。带上 goal_id
        # 后：不同目标互不挤兑；同一目标「标记写失败→下轮重发」的短窗重复仍被限流窗
        # 正确压住（同 key 1h 内只出一条）。
        "rate_key": f"goal_done:{account_id}:{gid}",
    }


def scan_and_notify(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    now: Optional[float] = None,
    user_store: Any = None,
) -> Dict[str, int]:
    """扫「done 且未通知」→ 发布 ``goal_completed_alert`` → 落幂等标记。

    发布与标记的顺序＝先发布后标记（at-least-once）：进程内 EventBus 发布
    实际不会失败；若标记写失败，下一轮至多重发一次，比静默丢一次完成提醒
    （at-most-once）好。返回 ``{scanned, notified}`` 观测计数。

    ``push_agent`` 开且给了 ``user_store`` 时，payload 附责任坐席收件地址
    （``agent_chat_id``/``agent_username``，解析见 resolve_agent_push_target）；
    webhook 侧据此加发一份坐席副本，管理员渠道不受影响。"""
    out = {"scanned": 0, "notified": 0}
    try:
        if not goals_enabled(cfg_root):
            return out
        cfg = resolve_notify_cfg(cfg_root)
        if not cfg["enabled"]:
            return out
        n = float(now if now is not None else time.time())
        since = n - cfg["lookback_hours"] * 3600.0
        rows = store.list_done_unnotified(
            since_ts=since, limit=cfg["max_per_scan"],
            kind=NOTIFIED_EVENT_KIND)
        if not rows:
            return out
        out["scanned"] = len(rows)
        names = _display_names(
            inbox_store, [str(g.get("conversation_id") or "") for g in rows])
        amounts = store.won_amounts_for_goals(
            [str(g.get("goal_id") or "") for g in rows])
        if publish is None:
            from src.integrations.shared.event_bus import get_event_bus
            publish = get_event_bus().publish
        for goal in rows:
            gid = str(goal.get("goal_id") or "")
            try:
                payload = build_completion_payload(
                    goal,
                    contact_name=names.get(
                        str(goal.get("conversation_id") or ""), ""),
                    won_meta=amounts.get(gid),
                )
                if cfg["push_agent"] and user_store is not None:
                    payload.update(resolve_agent_push_target(
                        goal, inbox_store=inbox_store,
                        user_store=user_store))
                # P3：逐目标点名收件人（与 push_agent 正交——目标上显式配置
                # 即生效，webhook 侧借 telegram 渠道逐个加发）
                extra = resolve_extra_push_targets(goal, user_store=user_store)
                if extra:
                    payload["extra_chat_ids"] = extra
                # P3：摸底要点（默认关；开=已采画像事实一行随推送出境）
                if cfg["include_profile"]:
                    brief = build_slots_brief(store, goal)
                    if brief:
                        payload["slots_brief"] = brief
                publish("goal_completed_alert", payload)
                store.add_event(
                    gid, NOTIFIED_EVENT_KIND,
                    json.dumps({"at": round(n, 1)},
                               separators=(",", ":")))
                out["notified"] += 1
                logger.info(
                    "[goal-notify] 目标达成通知 goal=%s conv=%s result=%s",
                    gid, payload["conversation_id"],
                    payload["result_kind"] or "-")
            except Exception:
                logger.debug("goal notify item skipped", exc_info=True)
    except Exception:
        logger.debug("scan_and_notify failed", exc_info=True)
    return out


def triage_missed(
    store: GoalStore,
    inbox_store: Any,
    rows: List[Dict[str, Any]],
) -> Dict[str, str]:
    """失守三分法（P5 2026-08-09）：{goal_id: offered|engaged|silent}。

    里程碑 idx 有**时间兑底**（到点没推进也走格子），判不了真实进展——
    生产 28 个失守全停在 idx=4 曾被误读成「跑完弧线」。诚实判据：
    - ``offered``  开价拍真的发出过（goal_actions direct 已耗，硬证据）——
      已开价未成交＝**最该人工复核补标**的一批（官网成交发生在站外）；
    - ``engaged``  窗口内客户入站 ≥2 条（首条常是建目标的触发消息，≥2 才算
      有来有回）；inbox store 不可用时保守归 silent 之前先看 offered；
    - ``silent``   没聊起来＝获客本身失败，复盘话术/名单而不是催成交。
    优先级 offered > engaged > silent。绝不抛。"""
    out: Dict[str, str] = {}
    try:
        ids = [str(g.get("goal_id") or "") for g in rows]
        offered = store.direct_engaged_map(ids)
        count_fn = getattr(inbox_store, "count_inbound_between", None) \
            if inbox_store is not None else None
        for g in rows[:50]:                       # 探测预算（digest/报表页≤50）
            gid = str(g.get("goal_id") or "")
            if offered.get(gid):
                out[gid] = "offered"
                continue
            engaged = False
            if callable(count_fn):
                try:
                    engaged = count_fn(
                        str(g.get("conversation_id") or ""),
                        float(g.get("start_ts") or 0.0),
                        float(g.get("done_at") or 0.0)) >= 2
                except Exception:
                    engaged = False
            out[gid] = "engaged" if engaged else "silent"
    except Exception:
        logger.debug("triage_missed failed", exc_info=True)
    return out


def scan_sprint_miss(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, int]:
    """冲刺目标失守**即时单条**告警（P2 2026-08-30）：pace=today/session 的
    failed/expired 不等日报聚合门槛（3 条或 24h）——3 小时目标失败后趁热复盘
    窗口极短，日报级时延等于放弃运营抓手。

    与日报共用 ``MISS_EVENT_KIND`` 幂等标记与 ``goal_miss_alert`` 事件名，
    payload 形状=日报同构（count=1 + 单目标附加字段），webhook formatter
    零改动可渲染；先于日报跑 → 已标记的冲刺行天然不再进日报。随
    ``notify.enabled`` 总闸 + ``sprint.enabled``（冲刺没开就没有这类目标）。
    返回 ``{scanned, alerted}``。绝不抛。"""
    out = {"scanned": 0, "alerted": 0}
    try:
        if not goals_enabled(cfg_root):
            return out
        cfg = resolve_notify_cfg(cfg_root)
        if not cfg["enabled"]:
            return out
        try:
            from src.companion.goals.sprint_ticker import parse_sprint_cfg
            if not parse_sprint_cfg(
                    resolve_goals_cfg(cfg_root)).get("enabled"):
                return out
        except Exception:
            return out
        from src.companion.goals.pace import is_sprint, resolve_pace
        n = float(now if now is not None else time.time())
        since = n - cfg["lookback_hours"] * 3600.0
        rows = store.list_missed_unnotified(
            since_ts=since, limit=50, kind=MISS_EVENT_KIND)
        sprint_rows = [g for g in rows if is_sprint(resolve_pace(g))]
        if not sprint_rows:
            return out
        out["scanned"] = len(sprint_rows)
        names = _display_names(
            inbox_store,
            [str(g.get("conversation_id") or "") for g in sprint_rows])
        if publish is None:
            from src.integrations.shared.event_bus import get_event_bus
            publish = get_event_bus().publish
        for g in sprint_rows[:10]:              # 单轮限流（同 max_per_scan 量级）
            gid = str(g.get("goal_id") or "")
            try:
                st = str(g.get("status") or "")
                tid = str(g.get("template") or "")
                template = get_template(tid) or {}
                conv = str(g.get("conversation_id") or "")
                result = str(g.get("result") or "")
                payload = {
                    # 日报同构段（formatter 兼容）
                    "count": 1,
                    "failed": 1 if st == "failed" else 0,
                    "expired": 1 if st == "expired" else 0,
                    "by_template": {
                        str(template.get("name_zh") or tid): 1},
                    "by_account": {
                        f"{g.get('platform', '')}:{g.get('account_id', '')}": 1},
                    "oldest_hours": round(
                        max(0.0, n - float(g.get("done_at") or n)) / 3600.0, 1),
                    "window_hours": round(cfg["lookback_hours"], 0),
                    # 单目标附加段（冲刺即时告警专属）
                    "sprint": True,
                    "goal_id": gid,
                    "title": str(g.get("title") or ""),
                    "conversation_id": conv,
                    "contact_name": names.get(conv, ""),
                    "result": result,
                    "with_signal": result == "expired_with_signal",
                    # 每目标一生至多失守一次（幂等标记）——按目标限流即可
                    "rate_key": f"goal_miss:sprint:{gid}",
                }
                publish("goal_miss_alert", payload)
                store.add_event(
                    gid, MISS_EVENT_KIND,
                    json.dumps({"at": round(n, 1), "sprint": 1},
                               separators=(",", ":")))
                out["alerted"] += 1
                logger.info(
                    "[goal-notify] 冲刺目标失守即时告警 goal=%s status=%s%s",
                    gid, st, "（曾检出达成信号）"
                    if payload["with_signal"] else "")
            except Exception:
                logger.debug("sprint miss item skipped", exc_info=True)
    except Exception:
        logger.debug("scan_sprint_miss failed", exc_info=True)
    return out


def scan_miss_digest(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, int]:
    """失守聚合日报（P1）：窗口内未通报的 failed/expired → **一条** ``goal_miss_alert``。

    触发判据（满足其一）：条数 ≥ miss_min_count / 最老一条超 miss_max_age_hours。
    发布后逐条落 ``miss_notified`` 幂等标记——同一目标绝不进两次日报。
    返回 ``{pending, digested}``。绝不抛。"""
    out = {"pending": 0, "digested": 0}
    try:
        if not goals_enabled(cfg_root):
            return out
        cfg = resolve_notify_cfg(cfg_root)
        if not (cfg["enabled"] and cfg["miss_digest"]):
            return out
        n = float(now if now is not None else time.time())
        since = n - cfg["lookback_hours"] * 3600.0
        rows = store.list_missed_unnotified(
            since_ts=since, limit=50, kind=MISS_EVENT_KIND)
        if not rows:
            return out
        out["pending"] = len(rows)
        oldest_h = (n - float(rows[0].get("done_at") or n)) / 3600.0
        if (len(rows) < cfg["miss_min_count"]
                and oldest_h < cfg["miss_max_age_hours"]):
            return out                      # 还没攒到出账门槛，下轮再看
        by_status: Dict[str, int] = {}
        by_template: Dict[str, int] = {}
        by_account: Dict[str, int] = {}
        for g in rows:
            st = str(g.get("status") or "")
            by_status[st] = by_status.get(st, 0) + 1
            tid = str(g.get("template") or "")
            template = get_template(tid) or {}
            tname = str(template.get("name_zh") or tid)
            by_template[tname] = by_template.get(tname, 0) + 1
            acct = f"{g.get('platform', '')}:{g.get('account_id', '')}"
            by_account[acct] = by_account.get(acct, 0) + 1
        # P5 三分法：offered=已开价未成交（最该复核补标）/ engaged / silent
        tri = triage_missed(store, inbox_store, rows)
        triage = {"offered": 0, "engaged": 0, "silent": 0}
        for v in tri.values():
            triage[v] = triage.get(v, 0) + 1
        payload = {
            "count": len(rows),
            "failed": by_status.get("failed", 0),
            "expired": by_status.get("expired", 0),
            "by_template": by_template,
            "by_account": by_account,
            "triage": triage,
            "oldest_hours": round(oldest_h, 1),
            "window_hours": round(cfg["lookback_hours"], 0),
            "rate_key": "goal_miss:digest",
        }
        if publish is None:
            from src.integrations.shared.event_bus import get_event_bus
            publish = get_event_bus().publish
        publish("goal_miss_alert", payload)
        for g in rows:
            try:
                store.add_event(
                    str(g.get("goal_id") or ""), MISS_EVENT_KIND,
                    json.dumps({"at": round(n, 1)}, separators=(",", ":")))
            except Exception:
                logger.debug("miss mark skipped", exc_info=True)
        out["digested"] = len(rows)
        logger.info("[goal-notify] 失守日报已发（%d 条：failed=%d expired=%d）",
                    len(rows), payload["failed"], payload["expired"])
    except Exception:
        logger.debug("scan_miss_digest failed", exc_info=True)
    return out


# ── M-7 C（#236）：到期结算不静默 ────────────────────────────────────────────
# skuio 机 09-07：BABY BEAR 原目标（09-03 设 3 天 + 延 1 天）静默到期——自然档不进
# scan_sprint_miss（只收 today/session），进 scan_miss_digest 又卡「3 条或 24h」门槛，
# 且 goal_miss_alert 只到 webhook、工作台通知中心不认；用户重建后卡片 last=None，
# 上一目标的结局从卡上消失。这里：目标在 settle-on-read / 定时结算翻成终态的**那一
# 刻**生成结算摘要 → 事件 ``settlement``（幂等）+ ``goal_settled_alert``（SSE + 铃铛，
# 不经聚合门槛）。日报/webhook 的 goal_miss_alert 链路不动。

def _reason_code(
    *, status: str, sent: int, injected: int, blocked_by: Dict[str, int],
    engine_blockers: List[str], replies: int,
) -> str:
    """未执行 / 结局的主因（一个码，前端按 i18n 渲染成一句话）。优先级：
    引擎层没生效 > 被安全/节奏闸拦住 > 只顺势带了方向没主动出手 > 从没排上 >
    出手了客户没回 > 出手了客户有回但没达成。done 单列。"""
    if status == "done":
        return "done"
    if engine_blockers:
        return "engine:" + engine_blockers[0]
    if blocked_by:
        top = max(blocked_by.items(), key=lambda kv: kv[1])[0]
        return "blocked:" + str(top)
    if sent <= 0 and injected > 0:
        return "steered_only"
    if sent <= 0:
        return "never_due"
    if replies <= 0:
        return "no_reply"
    return "no_signal"


def build_settlement(
    store: GoalStore,
    goal: Dict[str, Any],
    *,
    cfg_root: Any = None,
    inbox_store: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """结算摘要（纯读，不写库）：计划 X 拍 / 实际 Y 拍（其中投递成功 Z）/ 顺势带方向
    N 次 / 被拦 M 次及原因 / 客户回应 R 条 / 主因码。绝不抛。"""
    n = float(now if now is not None else time.time())
    gid = str((goal or {}).get("goal_id") or "")
    out: Dict[str, Any] = {
        "v": 1, "status": str((goal or {}).get("status") or ""),
        "planned": 0, "sent": 0, "injected": 0, "blocked": 0,
        "blocked_by": {}, "replies": 0, "reason": "", "at": round(n, 1),
    }
    if not gid:
        return out
    try:
        from src.companion.goals.pace import is_sprint, resolve_pace
        from src.companion.goals.service import (
            build_beats_trace,
            sprint_engine_status,
        )
        from src.companion.goals.sprint_ticker import parse_sprint_cfg
        start = float(goal.get("start_ts") or goal.get("created_at") or 0)
        deadline = float(goal.get("deadline_ts") or 0)
        done_at = float(goal.get("done_at") or 0) or n
        pace = resolve_pace(goal)
        if is_sprint(pace):
            try:
                pts = parse_sprint_cfg(resolve_goals_cfg(cfg_root)).get(
                    "phase_points") or (0.0, 0.45, 0.75)
            except Exception:
                pts = (0.0, 0.45, 0.75)
            out["planned"] = len(pts)
        else:
            span = (deadline - start) if (deadline > start > 0) else 0.0
            out["planned"] = max(1, int(round(span / 86400.0))) if span > 0 else 0
        tr = build_beats_trace(store, goal, now=n)
        s = tr.get("summary") or {}
        out["sent"] = int(s.get("sent") or 0)
        out["injected"] = int(s.get("injected") or 0)
        out["blocked"] = int(s.get("blocked") or 0)
        bb = dict(s.get("blocked_by") or {})
        out["blocked_by"] = dict(sorted(bb.items(), key=lambda kv: -kv[1])[:3])
        replies = 0
        count_fn = getattr(inbox_store, "count_inbound_between", None) \
            if inbox_store is not None else None
        if callable(count_fn):
            try:
                replies = int(count_fn(str(goal.get("conversation_id") or ""),
                                       start, done_at) or 0)
            except Exception:
                replies = 0
        out["replies"] = replies
        eng_block: List[str] = []
        try:
            if cfg_root is not None and str(goal.get("autonomy") or "") == "auto":
                es = sprint_engine_status(
                    cfg_root, platform=str(goal.get("platform") or "")) or {}
                eng_block = list(
                    (es.get("sprint_blockers") if is_sprint(pace)
                     else es.get("natural_auto_blockers")) or [])
        except Exception:
            eng_block = []
        out["reason"] = _reason_code(
            status=out["status"], sent=out["sent"], injected=out["injected"],
            blocked_by=out["blocked_by"], engine_blockers=eng_block,
            replies=replies)
    except Exception:
        logger.debug("build_settlement failed", exc_info=True)
    return out


def read_settlement(store: GoalStore, goal_id: str) -> Optional[Dict[str, Any]]:
    """已落库的结算摘要（``settlement`` 事件 detail JSON）；无 → None。绝不抛。"""
    try:
        ev = store.last_event(str(goal_id or ""), SETTLEMENT_EVENT_KIND)
        if not ev:
            return None
        d = json.loads(str(ev.get("detail") or "{}"))
        if not isinstance(d, dict):
            return None
        d.setdefault("at", float(ev.get("ts") or 0))
        return d
    except Exception:
        return None


def settle_and_notify(
    store: GoalStore,
    goal: Dict[str, Any],
    *,
    cfg_root: Any = None,
    inbox_store: Any = None,
    publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    now: Optional[float] = None,
    notify: bool = True,
) -> Optional[Dict[str, Any]]:
    """终态那一刻：写 ``settlement`` 事件（每目标一次，幂等）+ 发 ``goal_settled_alert``
    （expired / failed 才推——done 已有 goal_completed_alert；``notify=False``＝人工
    标终态只记摘要不推铃铛）。**不经** notify 的 3 条 / 24h 聚合门槛，也不受
    ``notify.enabled`` 闸（这不是外推日报，是工作台自己的账）。返回摘要；已结算过 /
    非终态 / 异常 → None。绝不抛。"""
    try:
        gid = str((goal or {}).get("goal_id") or "")
        status = str((goal or {}).get("status") or "")
        if not gid or status not in ("expired", "failed", "done"):
            return None
        if store.last_event(gid, SETTLEMENT_EVENT_KIND) is not None:
            return None
        n = float(now if now is not None else time.time())
        summary = build_settlement(
            store, goal, cfg_root=cfg_root, inbox_store=inbox_store, now=n)
        conv = str(goal.get("conversation_id") or "")
        store.add_event(
            gid, SETTLEMENT_EVENT_KIND,
            json.dumps(summary, ensure_ascii=False, separators=(",", ":"))[:400],
            conversation_id=conv, now=n)
        logger.info(
            "[goal-settle] goal=%s conv=%s status=%s planned=%s sent=%s injected=%s "
            "blocked=%s replies=%s reason=%s title=%r",
            gid, conv, status, summary.get("planned"), summary.get("sent"),
            summary.get("injected"), summary.get("blocked"), summary.get("replies"),
            summary.get("reason"), str(goal.get("title") or "")[:30])
        if notify and status in ("expired", "failed"):
            names = _display_names(inbox_store, [conv]) if conv else {}
            tid = str(goal.get("template") or "")
            template = get_template(tid) or {}
            payload = {
                "goal_id": gid,
                "conversation_id": conv,
                "contact_name": names.get(conv, ""),
                "title": str(goal.get("title") or ""),
                "template_name": str(template.get("name_zh") or tid),
                "status": status,
                "platform": str(goal.get("platform") or ""),
                "account_id": str(goal.get("account_id") or ""),
                "settlement": summary,
                "rate_key": f"goal_settled:{gid}",
            }
            if publish is None:
                from src.integrations.shared.event_bus import get_event_bus
                publish = get_event_bus().publish
            publish(SETTLED_ALERT, payload)
        return summary
    except Exception:
        logger.debug("settle_and_notify failed", exc_info=True)
        return None


# ── 常备扫描循环（P2 2026-08-09 重构）────────────────────────────────────────
# 首版把扫描挂在 ScheduledReporter tick 上——那个调度器受 ``report.enabled``
# 闸（生产常年关），扫描于是**从未运行**（2026-08-09 只读探针实锤：9/11 个
# active 目标 updated_at 停在 17h~4.6 天前）。教训与 care 引擎同款：
# 「常备接线 + 配置热闸」——循环无条件启动，每 tick 现读配置自闸，
# 开关经 overlay 热重载 ~30s 生效免重启；goals/sweep/notify 全关时每 tick
# 只有一次 dict 读取，零 DB 开销。

def scan_tick(
    state: Dict[str, Any],
    config_manager: Any,
    inbox_store: Any = None,
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """单次 tick（循环体抽出来供测试直驱）：完成通知每 tick、失守日报每 tick
    （内部自带出账门槛）、结算扫描每 ``interval_ticks`` tick 一页（游标在
    ``state`` 里轮转）。``state`` 同时充当心跳快照（挂 app.state 可观测——
    本次事故的教训之一就是「没跑」和「没货」从外面看不出区别）。绝不抛。"""
    n = float(now if now is not None else time.time())
    state["last_tick_ts"] = n
    state.setdefault("ticks", 0)
    state.setdefault("sweep_cursor", 0)
    state.setdefault("sweep_tick", 0)
    state.setdefault("settled_total", 0)
    state.setdefault("notified_total", 0)
    state.setdefault("miss_digested_total", 0)
    state["ticks"] += 1
    try:
        cfg_root = getattr(config_manager, "config", None)
        if not isinstance(cfg_root, dict) or not goals_enabled(cfg_root):
            state["gated"] = "goals_disabled"
            return state
        sweep_cfg = resolve_sweep_cfg(cfg_root)
        notify_cfg = resolve_notify_cfg(cfg_root)
        if not (sweep_cfg["enabled"] or notify_cfg["enabled"]):
            state["gated"] = "scans_disabled"
            return state
        state["gated"] = ""
        from src.companion.goals.service import get_configured_store
        store = get_configured_store(
            cfg_root, getattr(config_manager, "config_path", None))
        if sweep_cfg["enabled"]:
            state["sweep_tick"] += 1
            if state["sweep_tick"] >= sweep_cfg["interval_ticks"]:
                state["sweep_tick"] = 0
                res = settle_sweep(
                    store, cfg_root, inbox_store=inbox_store, now=n,
                    cursor=int(state["sweep_cursor"]))
                state["sweep_cursor"] = int(res.get("next_cursor") or 0)
                state["settled_total"] += int(res.get("settled") or 0)
                state["last_sweep_ts"] = n
                if res.get("settled"):
                    logger.info("[goal-sweep] 定时结算 %d 条 active 目标",
                                res["settled"])
        if notify_cfg["enabled"]:
            # user_store 恒传（惰性单例零成本）：push_agent 副本与逐目标
            # notify_extra 的用户名解析都要它——后者不受 push_agent 开关闸。
            res = scan_and_notify(
                store, cfg_root, inbox_store=inbox_store, now=n,
                user_store=user_store_for(
                    getattr(config_manager, "config_path", None)))
            state["notified_total"] += int(res.get("notified") or 0)
            if res.get("notified"):
                logger.info("[goal-notify] 本轮发出 %d 条目标达成通知",
                            res["notified"])
            # 冲刺失守即时单条（P2 2026-08-30）先于日报：标记过的行不再进日报
            sm = scan_sprint_miss(
                store, cfg_root, inbox_store=inbox_store, now=n)
            state["sprint_miss_total"] = (
                int(state.get("sprint_miss_total") or 0)
                + int(sm.get("alerted") or 0))
            dig = scan_miss_digest(
                store, cfg_root, inbox_store=inbox_store, now=n)
            state["miss_digested_total"] += int(dig.get("digested") or 0)
    except Exception:
        logger.debug("goal scan_tick failed", exc_info=True)
    return state


async def run_scan_loop(
    config_manager: Any,
    inbox_store: Any = None,
    *,
    tick_sec: float = 60.0,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    """常备扫描循环（bootstrap 无条件启动；``state`` 传 app.state 挂的 dict
    即成心跳快照）。任何单 tick 异常都被 scan_tick 吞掉，循环永不退出。"""
    import asyncio
    st = state if state is not None else {}
    logger.info("目标结算/提醒扫描循环已启动（tick=%ss，配置热自闸）", tick_sec)
    while True:
        try:
            scan_tick(st, config_manager, inbox_store)
        except Exception:
            logger.debug("goal scan loop tick failed", exc_info=True)
        await asyncio.sleep(max(5.0, float(tick_sec)))
