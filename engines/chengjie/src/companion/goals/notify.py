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

logger = logging.getLogger("GoalNotify")

NOTIFIED_EVENT_KIND = "completed_notified"
MISS_EVENT_KIND = "miss_notified"


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
    return {
        "goal_id": str(goal.get("goal_id") or ""),
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
        "days_to_done": days,
        "done_at": done_at,
        # 同账号完成潮共用限流窗（webhook 侧 rate_key 语义）
        "rate_key": f"goal_done:{account_id}",
    }


def scan_and_notify(
    store: GoalStore,
    cfg_root: Any,
    *,
    inbox_store: Any = None,
    publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, int]:
    """扫「done 且未通知」→ 发布 ``goal_completed_alert`` → 落幂等标记。

    发布与标记的顺序＝先发布后标记（at-least-once）：进程内 EventBus 发布
    实际不会失败；若标记写失败，下一轮至多重发一次，比静默丢一次完成提醒
    （at-most-once）好。返回 ``{scanned, notified}`` 观测计数。"""
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
            res = scan_and_notify(
                store, cfg_root, inbox_store=inbox_store, now=n)
            state["notified_total"] += int(res.get("notified") or 0)
            if res.get("notified"):
                logger.info("[goal-notify] 本轮发出 %d 条目标达成通知",
                            res["notified"])
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
