"""限时目标「冲刺推进器」——时间驱动的主动拍（P0 2026-08-30）。

限时档（today/session）此前只有「对方开口才拍」一条通道：bridge 明确把
sprint 拒于主动顺风车之外，而 proactive_topic 的沉默阈值（12–48h）在
2–12 小时的目标窗口内结构上永不放行——客户不说话＝全程零动作，到期必
expired。本模块是冲刺档自己的时刻表：

- 按**剩余时间相位**（开场/跟进/收口，``phase_points`` 占总时长比例）把
  「主动拍」排进 care 派发管线（``add_scheduled_care``，
  ``topic_norm=goal:{gid}:p{n}``——相位独立去重，一相位一发）；
- 发送走 care 全套链路（LLM 拟稿引用目标背景 + deferred 队列投递 + 对端
  bot 过滤），拟稿模板由派发器按冲刺行切换（催化版，见 care_dispatcher）；
- 「骚扰型」闸门（care 每联系人预算 / 安静时段顺延 / 慢抖动）按配置豁免
  （``goal_row_policy`` 注入派发器）——限时档是用户显式拍板的全力决定；
- 「安全型」闸门**绝不豁免**：危机情绪 block、opt-out 静默、会话档位
  （仅 ``auto_ai`` 会话自发）、平台白名单（对齐 impl72 主动触达收敛）。

默认关（``companion.goals.sprint.enabled=false``）；开着也只影响
pace=today/session 且 autonomy=auto 的目标。全部 best-effort：任何异常
只影响本轮扫描，绝不拖垮 care/goals 主链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("GoalSprintTicker")

_DAY = 86400.0

# 相位指令（source_text / 拍意图回落用；与 templates.intents_sprint 同语义分层）
PHASE_DIRECTIVES = (
    "自然接住对方近况，把这件事轻轻带进来，先看反应",
    "把这件事说具体：给一个明确的说法或提议，看对方态度",
    "时间不多了：大方求一个明确答复，给出下一步怎么做；犹豫就留台阶",
)

DEFAULT_PHASE_POINTS = (0.0, 0.45, 0.75)

# D1b P0-2（2026-09-05）：出厂白名单从 ("telegram",) 扩到三大私聊平台——此前
# sprint.enabled=true 的桌面种子/cloud_light 在 WhatsApp / LINE（付费主力平台）
# 上静默不出手，卡片却写「自动推进」。messenger 刻意不入：platform_modes 把它
# 封顶 review（网页链路不稳），自发消息本就不该在那条链上出手。
DEFAULT_SPRINT_PLATFORMS = ("telegram", "whatsapp", "line")


def parse_sprint_cfg(goals_cfg: Any) -> Dict[str, Any]:
    """``companion.goals.sprint`` → 全键齐备的配置 dict（坏值回默认，绝不抛）。"""
    raw = {}
    try:
        if isinstance(goals_cfg, dict):
            raw = goals_cfg.get("sprint") or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}

    def _f(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        return max(lo, min(v, hi))

    pts: List[float] = []
    try:
        for p in (raw.get("phase_points") or DEFAULT_PHASE_POINTS):
            v = float(p)
            if 0.0 <= v <= 0.95:
                pts.append(v)
    except (TypeError, ValueError):
        pts = []
    pts = sorted(set(pts))
    if not pts:
        pts = list(DEFAULT_PHASE_POINTS)

    platforms = tuple(
        str(x).strip().lower()
        for x in (raw.get("platforms") or DEFAULT_SPRINT_PLATFORMS)
        if str(x).strip())
    jit = raw.get("jitter_sec")
    try:
        jitter = (max(5.0, float(jit[0])), max(10.0, float(jit[1])))
        if jitter[0] > jitter[1]:
            jitter = (jitter[1], jitter[0])
    except (TypeError, ValueError, IndexError):
        jitter = (30.0, 180.0)

    def _opt_int(key: str, lo: int, hi: int) -> Optional[int]:
        """可选整型覆写：缺省/坏值 → None（消费方回内置基线）。"""
        if raw.get(key) is None:
            return None
        try:
            return max(lo, min(int(raw[key]), hi))
        except (TypeError, ValueError):
            return None

    return {
        "enabled": bool(raw.get("enabled", False)),
        # D1b P0-1：冲刺自己的灰度开关（只拟稿不真发）。冲刺行不再吃
        # proactive_care.dry_run（那是 care 业务的灰度），要灰度冲刺请开这个。
        "dry_run": bool(raw.get("dry_run", False)),
        # 「platforms 是否显式配置」——出厂默认与显式配置行为一致，但启动
        # WARNING 要能区分「运营没想过白名单」（sprint_engine_status 消费）
        "platforms_explicit": bool(raw.get("platforms")),
        "interval_sec": _f("interval_sec", 120.0, 30.0, 1800.0),
        "max_per_tick": int(_f("max_per_tick", 6, 1, 50)),
        "phase_points": tuple(pts),
        "silence_min_sec": _f("silence_min_sec", 600.0, 0.0, 86400.0),
        "min_gap_sec": _f("min_gap_sec", 1500.0, 0.0, 86400.0),
        "min_remaining_sec": _f("min_remaining_sec", 180.0, 0.0, 86400.0),
        "exempt_contact_budget": bool(raw.get("exempt_contact_budget", True)),
        "ignore_quiet_hours": bool(raw.get("ignore_quiet_hours", False)),
        "jitter_sec": jitter,
        "platforms": platforms or DEFAULT_SPRINT_PLATFORMS,
        # P1 力度体系：收口升档阈 + 退避/熔断/封顶覆写（None=内置基线）
        "escalate_at": _f("escalate_at", 0.35, 0.05, 0.9),
        "backoff_after": _opt_int("backoff_after", 0, 20),
        "halt_after": _opt_int("halt_after", 0, 20),
        "today_cap": _opt_int("today_cap", 1, 24),
        "session_cap": _opt_int("session_cap", 1, 24),
        # P2 完成提速：高置信联系方式信号 → 冲刺目标直接结算 done（默认关）
        "auto_settle_contact": bool(raw.get("auto_settle_contact", False)),
        # D1b P0-3：自然档「每日一拍」也由本推进器出手（默认开，随 sprint.enabled）
        # ——此前自然档 auto 只能搭 proactive_topic 顺风车（bridge），而那条链
        # 沉默阈 12–48h + 仅 telegram，几天期的目标结构上等不到一拍。
        "natural_daily": bool(raw.get("natural_daily", True)),
        "natural_window": _hour_window(raw.get("natural_window")),
        "natural_silence_min_sec": _f(
            "natural_silence_min_sec", 6 * 3600.0, 0.0, 3 * 86400.0),
        "natural_min_gap_sec": _f(
            "natural_min_gap_sec", 6 * 3600.0, 0.0, 3 * 86400.0),
    }


DEFAULT_NATURAL_WINDOW = (10, 20)


def _hour_window(raw: Any) -> Tuple[int, int]:
    """``[start_hour, end_hour)`` 本地小时窗；坏值回默认 (10, 20)。"""
    try:
        a, b = int(raw[0]), int(raw[1])
        a = max(0, min(a, 23))
        b = max(1, min(b, 24))
        if a < b:
            return a, b
    except (TypeError, ValueError, IndexError):
        pass
    return DEFAULT_NATURAL_WINDOW


def phase_norm(goal_id: str, phase: int) -> str:
    """冲刺相位的 care ``topic_norm``（``add_scheduled_care`` 按它一相位一发）。"""
    return f"goal:{str(goal_id or '')[:48]}:p{max(0, int(phase))}"


def daily_norm(goal_id: str, day: str) -> str:
    """自然档每日拍的 care ``topic_norm``（一日一发；``day``=YYYY-MM-DD 本地日键）。"""
    d = str(day or "").replace("-", "")[:8]
    return f"goal:{str(goal_id or '')[:48]}:d{d}"


def parse_goal_care_kind(tnorm: Any) -> Tuple[str, str, Any]:
    """``goal:*`` 行分型 → ``(kind, goal_id, arg)``：

    - ``("sprint", gid, phase:int)``   ``goal:{gid}:p{n}`` 冲刺相位行
    - ``("daily", gid, day:"YYYY-MM-DD")`` ``goal:{gid}:d{YYYYMMDD}`` 自然档每日拍
    - ``("care", gid, None)``         impl84 到期关怀行（无后缀）
    - ``("", "", None)``              非 goal 行
    """
    s = str(tnorm or "").strip()
    if not s.startswith("goal:"):
        return "", "", None
    body = s[len("goal:"):]
    if not body:
        return "", "", None
    head, sep, tail = body.rpartition(":p")
    if sep and head and tail.isdigit():
        return "sprint", head, int(tail)
    head, sep, tail = body.rpartition(":d")
    if sep and head and tail.isdigit() and len(tail) == 8:
        return "daily", head, f"{tail[:4]}-{tail[4:6]}-{tail[6:]}"
    return "care", body, None


def parse_goal_care_norm(tnorm: Any) -> Tuple[str, Optional[int]]:
    """``goal:{gid}[:p{n}]`` → ``(goal_id, phase|None)``；非 goal 行 → ``("", None)``。

    兼容 impl84 旧格式（无相位后缀=到期关怀行）与 D1b 每日拍行（``:d…`` 后缀
    → phase=None，goal_id 正确剥出）。goal_id 本体不含冒号（store 生成的
    hex id）。
    """
    kind, gid, arg = parse_goal_care_kind(tnorm)
    if not kind:
        return "", None
    return gid, (int(arg) if kind == "sprint" else None)


def phase_directive(phase: int, total_phases: int = 3) -> str:
    """相位 → 推进指令（末相位恒为收口语义，越界夹末段）。"""
    idx = max(0, int(phase))
    if total_phases > 0 and idx >= int(total_phases) - 1:
        return PHASE_DIRECTIVES[-1]
    return PHASE_DIRECTIVES[min(idx, len(PHASE_DIRECTIVES) - 1)]


def remaining_phrase(remaining_sec: float) -> str:
    """剩余时长的人话（<1min 归「不到1分钟」；≥100min 按小时一位小数）。"""
    s = max(0.0, float(remaining_sec or 0))
    mins = int(round(s / 60.0))
    if mins < 1:
        return "不到1分钟"
    if mins < 100:
        return f"约{mins}分钟"
    return f"约{s / 3600.0:.1f}小时"


def sprint_source_text(
    goal: Dict[str, Any], phase: int, now: float,
    total_phases: int = 3,
) -> str:
    """care 行 ``source_text``（进拟稿 prompt 的 {source_text} 槽，≤160 字）。"""
    title = str(goal.get("title") or "").strip() or "这件事"
    deadline = float(goal.get("deadline_ts") or 0)
    rp = remaining_phrase(deadline - now) if deadline > now else "即将到期"
    return (f"限时推进「{title[:40]}」剩余{rp}；本拍：" +
            phase_directive(phase, total_phases))[:160]


def due_phase(
    goal: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    now: float,
    last_inbound_ts: float = 0.0,
    last_outbound_ts: float = 0.0,
) -> Optional[int]:
    """当前该排哪个相位（None=本 tick 不排）。纯函数。

    判定序（每条都可单测）：
    1. 目标时间窗合法且未到期、剩余 ≥ ``min_remaining_sec``；
    2. 相位点：已过时间占比 ≥ ``phase_points[i]`` 的最大 i（早相位在客户
       活跃期自然错过就不补发——补发过时的开场比不发更怪）；
    3. 沉默闸：对方 ``silence_min_sec`` 内开过口 → 让回复链接管（settle-on-read
       正在拍），主动拍只接管「对话停了」的时段；
    4. 出站间隔闸：距我方上一条出站 < ``min_gap_sec`` → 等下一 tick
       （连珠炮不是推进是刷屏——这是节奏参数不是骚扰刹车，全力档也要留）。
    已排过的相位由 care ``topic_norm`` 去重兜底（本函数不查库）。
    """
    if not isinstance(goal, dict):
        return None
    try:
        start = float(goal.get("start_ts") or 0)
        deadline = float(goal.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        return None
    span = deadline - start
    if start <= 0 or span <= 0:
        return None
    if now >= deadline:
        return None
    if (deadline - now) < float(cfg.get("min_remaining_sec") or 0):
        return None
    frac = (now - start) / span
    pts = cfg.get("phase_points") or DEFAULT_PHASE_POINTS
    candidate = -1
    for i, p in enumerate(pts):
        if frac >= float(p):
            candidate = i
    if candidate < 0:
        return None
    sil = float(cfg.get("silence_min_sec") or 0)
    if sil > 0 and last_inbound_ts > 0 and (now - last_inbound_ts) < sil:
        return None
    gap = float(cfg.get("min_gap_sec") or 0)
    if gap > 0 and last_outbound_ts > 0 and (now - last_outbound_ts) < gap:
        return None
    return candidate


def due_daily(
    goal: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    now: float,
    last_inbound_ts: float = 0.0,
    last_outbound_ts: float = 0.0,
    today_action: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """自然档「今天该不该主动拍一次」→ 本地日键 ``YYYY-MM-DD``（None=不拍）。纯函数。

    判定序（每条可单测；D1b P0-3 2026-09-05）：
    1. 目标活跃且今天不是建目标当天（day 0 由建目标那轮对话/回复链自己接住，
       主动拍从第二天起——当天再追一句是连珠炮）；有 deadline 且已过 → 不拍；
    2. 本地小时落在 ``natural_window`` 内（默认 10–20 点：自然档是日常节奏，
       不像冲刺有时限压力，深夜/清晨不出手）；
    3. 今日拍已 consumed/sent（回复链顺势拍过 / 今天已主动拍过）→ 不拍；
       今日拍被坐席驳回（detail 以 ``rejected`` 起）→ 不拍（人已否决）；
    4. 沉默闸：对方 ``natural_silence_min_sec``（默认 6h）内开过口 → 让回复链；
    5. 出站间隔闸：距我方上条出站 < ``natural_min_gap_sec``（默认 6h）→ 不拍。
    一日一发由 care ``topic_norm=goal:{gid}:d{日}`` 去重兜底（本函数不查库）。
    """
    if not isinstance(goal, dict):
        return None
    try:
        created = float(goal.get("created_at") or goal.get("start_ts") or 0)
        deadline = float(goal.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        return None
    if deadline > 0 and now >= deadline:
        return None
    lt = time.localtime(now)
    day = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
    if created > 0:
        ct = time.localtime(created)
        if (ct.tm_year, ct.tm_yday) == (lt.tm_year, lt.tm_yday):
            return None
    w0, w1 = cfg.get("natural_window") or DEFAULT_NATURAL_WINDOW
    if not (int(w0) <= lt.tm_hour < int(w1)):
        return None
    if isinstance(today_action, dict):
        st = str(today_action.get("status") or "")
        if st in ("consumed", "sent"):
            return None
        if str(today_action.get("detail") or "").startswith("rejected"):
            return None
    sil = float(cfg.get("natural_silence_min_sec") or 0)
    if sil > 0 and last_inbound_ts > 0 and (now - last_inbound_ts) < sil:
        return None
    gap = float(cfg.get("natural_min_gap_sec") or 0)
    if gap > 0 and last_outbound_ts > 0 and (now - last_outbound_ts) < gap:
        return None
    return day


def natural_source_text(goal: Dict[str, Any],
                        today_action: Optional[Dict[str, Any]] = None) -> str:
    """自然档每日拍的 care ``source_text``（≤160 字）：优先用规划器已排的今日
    拍意图（与回复链「同一拍」口径），没有就按里程碑给一句通用推进语。"""
    title = str(goal.get("title") or "").strip() or "这件事"
    intent = ""
    if isinstance(today_action, dict):
        intent = str(today_action.get("intent") or "").strip()
    if not intent:
        intent = "自然接住对方近况，把这件事轻轻带进来一步，先看反应"
    return (f"日常推进「{title[:40]}」；本拍：" + intent)[:160]


def _beat_trace_detail(base: str, care_id: Any) -> str:
    """``beat_sent`` detail：既有前缀（``sprint:pN`` / ``daily:auto``）不变，
    有 care 行 id 就追加 `` care#<id>``——拍清单据此反查 care 表的话术快照与
    deferred 投递真相（M-7 A #236：事件此前只有相位，发了什么/到没到无从追）。"""
    try:
        cid = int(care_id or 0)
    except (TypeError, ValueError):
        cid = 0
    return f"{base} care#{cid}" if cid > 0 else base


def record_natural_beat_sent(
    gstore: Any, goal_id: str, day: str, *, now: Optional[float] = None,
    conversation_id: str = "", care_id: Any = 0, text_head: str = "",
) -> bool:
    """自然档每日主动拍真发成功（care sent_hook 调）→ 今日拍行落 ``sent``
    （已有 planned 行则直接改状态；没有则以通用意图新建）+ 事件 + 计数。绝不抛。

    与回复链的当日拍**共用同一日键**：主动拍发出后，回复链本日再命中同目标
    会看到 ``sent`` 行而不再重拍——「一天一拍」跨两条链成立。
    M-7 A：``conversation_id`` / ``care_id`` / ``text_head`` 落进事件追溯列。
    """
    try:
        n = float(now if now is not None else time.time())
        gid = str(goal_id or "")
        d = str(day or "").strip()
        if not gid or not d:
            return False
        goal = gstore.get_goal(gid)
        if goal is None:
            return False
        row = gstore.get_action(gid, d)
        if row is None:
            row = gstore.upsert_action(
                gid, d, intent="自然推进一步", push_level="soft",
                status="sent", detail="daily:auto", now=n)
        elif str(row.get("status") or "") not in ("consumed", "sent"):
            gstore.mark_action(str(row.get("action_id") or ""), "sent",
                               detail="daily:auto")
        gstore.add_event(
            gid, "beat_sent", _beat_trace_detail("daily:auto", care_id),
            conversation_id=str(conversation_id
                                or goal.get("conversation_id") or ""),
            text_head=str(text_head or ""), now=n)
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_sprint_sent()
        except Exception:
            pass
        return True
    except Exception:
        logger.debug("record_natural_beat_sent failed", exc_info=True)
        return False


def record_sprint_beat_sent(
    gstore: Any, goal_id: str, phase: int, *, now: Optional[float] = None,
    conversation_id: str = "", care_id: Any = 0, text_head: str = "",
) -> bool:
    """冲刺拍真发成功（care sent_hook 调）→ 落拍行 + 事件 + 计数。

    拍行 ``day=z:{ts}``（时间戳键：不与回复链的小时/回合槽撞车；``z:`` 前缀
    在 list_actions 的 day DESC 排序里恒排最前——终局复盘 ``last_beat_intent``
    会优先显示最近的主动拍，混流时序有已知偏差，接受）。status 直落 ``sent``
    ——它计入 ``count_engaged_since`` 的未回退避口径（主动拍连发无回应该
    推高退避，这是节奏语义不是骚扰刹车）。绝不抛。
    M-7 A：``conversation_id`` / ``care_id`` / ``text_head`` 落进事件追溯列。
    """
    try:
        n = float(now if now is not None else time.time())
        goal = gstore.get_goal(str(goal_id or ""))
        if goal is None:
            return False
        from src.companion.goals.templates import (
            get_template,
            pick_sprint_intent,
        )
        template = get_template(str(goal.get("template") or "")) or {}
        day = f"z:{int(n)}"
        pts = DEFAULT_PHASE_POINTS
        intent = ""
        try:
            intent = pick_sprint_intent(
                template, int(phase), str(goal_id), day,
                params=goal.get("params") or {})
        except Exception:
            intent = ""
        if not intent:
            intent = phase_directive(int(phase), len(pts))
        # 相位→力度：开场 soft、中段 direct、收口 close（与回复链升档同刻度）
        if int(phase) <= 0:
            push = "soft"
        elif int(phase) >= len(pts) - 1:
            push = "close"
        else:
            push = "direct"
        gstore.upsert_action(
            str(goal_id), day, intent=intent, push_level=push,
            status="sent", detail=f"sprint:p{int(phase)}", now=n)
        gstore.add_event(
            str(goal_id), "beat_sent",
            _beat_trace_detail(f"sprint:p{int(phase)}", care_id),
            conversation_id=str(conversation_id
                                or goal.get("conversation_id") or ""),
            text_head=str(text_head or ""), now=n)
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_sprint_sent()
        except Exception:
            pass
        return True
    except Exception:
        logger.debug("record_sprint_beat_sent failed", exc_info=True)
        return False


# 拦下原因 → 人话（拍清单 / 卡片「今天 X 次想出手，被 Y 拦下」共用；i18n 键在
# goals.py ``goal.blocked.<reason>``，这里只是键名清单与回落文案）
BLOCKED_REASONS = (
    "silence", "min_gap", "pace_cap", "platform", "automation_mode",
    "crisis", "optout", "no_conversation", "no_inbox", "autonomy",
    "goal_no_product", "first_send_preview", "dedup",
)


def record_beat_blocked(
    gstore: Any, goal_id: str, reason: str, *, slot: str,
    conversation_id: str = "", now: Optional[float] = None,
) -> bool:
    """「想出手但被闸拦下」落事件 ``beat_blocked``（M-7 A #236）。

    detail=``<reason>@<slot>``（slot＝冲刺相位 ``pN`` / 自然档日键 ``dYYYY-MM-DD``
    / 回复链 ``inject``）。**按目标×原因×槽位去重**（查最近一条同前缀事件）——
    ticker 每 120s 一轮，同一相位被同一道闸拦住只记一次，跨重启也不重复；
    换了相位/换了原因才再记。绝不抛。返回是否新记了一条。"""
    try:
        gid = str(goal_id or "")
        r = str(reason or "").strip() or "unknown"
        s = str(slot or "").strip() or "-"
        if not gid:
            return False
        detail = f"{r}@{s}"
        last = gstore.last_event(gid, "beat_blocked", detail_prefix=detail)
        if last is not None:
            return False
        goal = gstore.get_goal(gid) or {}
        gstore.add_event(
            gid, "beat_blocked", detail,
            conversation_id=str(conversation_id
                                or goal.get("conversation_id") or ""),
            now=now)
        return True
    except Exception:
        logger.debug("record_beat_blocked failed", exc_info=True)
        return False


def schedule_nudge(
    care_store: Any,
    goal: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    now: Optional[float] = None,
) -> Optional[Tuple[int, int]]:
    """坐席「立即推进」（P3 2026-08-30）：从当前应处相位起，把第一个还没排过
    的相位**立即**排入 care 队列（due=now+2s；沉默/出站间隔两道节奏闸对人工
    显式触发不适用——人拍板就是要现在出手）。

    安全闸不在这里（危机/opt-out/档位由路由层查后才调本函数）。
    返回 ``(phase, care_row_id)``；时间窗非法/所有相位都排过 → None。
    """
    n = float(now if now is not None else time.time())
    relaxed = dict(cfg or {})
    relaxed["silence_min_sec"] = 0.0
    relaxed["min_gap_sec"] = 0.0
    base = due_phase(goal, cfg=relaxed, now=n)
    if base is None:
        return None
    pts = relaxed.get("phase_points") or DEFAULT_PHASE_POINTS
    gid = str(goal.get("goal_id") or "")
    for idx in range(int(base), len(pts)):
        rid = care_store.add_scheduled_care(
            contact_key=str(goal.get("conversation_id") or ""),
            platform=str(goal.get("platform") or ""),
            account_id=str(goal.get("account_id") or ""),
            chat_key=str(goal.get("chat_key") or ""),
            due_at=n + 2.0,
            event_at=float(goal.get("deadline_ts") or 0) or (n + 2.0),
            topic=(str(goal.get("title") or "").strip() or "限时推进")[:40],
            topic_norm=phase_norm(gid, idx),
            source_text=sprint_source_text(goal, idx, n, len(pts)),
            confidence=1.0,
            dedup_days=3.0,
        )
        if rid:
            return idx, int(rid)
    return None


def next_phase_ts(goal: Dict[str, Any], cfg: Dict[str, Any],
                  now: Optional[float] = None) -> float:
    """下一个未来相位点的时刻（冲刺状态行「下一主动拍 ≈ xx:xx」）；无 → 0。
    纯时间推算（不查排期库）——已错过的相位不算「下一拍」。"""
    n = float(now if now is not None else time.time())
    try:
        start = float(goal.get("start_ts") or 0)
        deadline = float(goal.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        return 0.0
    span = deadline - start
    if start <= 0 or span <= 0 or n >= deadline:
        return 0.0
    for p in (cfg.get("phase_points") or DEFAULT_PHASE_POINTS):
        ts = start + float(p) * span
        if ts > n:
            return ts
    return 0.0


def next_daily_ts(cfg: Optional[Dict[str, Any]] = None,
                  now: Optional[float] = None) -> float:
    """自然档下一拍时刻：今天窗口起点（若还没到）或明天窗口起点。"""
    n = float(now if now is not None else time.time())
    try:
        w0, _w1 = (cfg or {}).get("natural_window") or DEFAULT_NATURAL_WINDOW
        lt = time.localtime(n)
        day0 = n - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        cand = day0 + int(w0) * 3600
        return cand if cand > n else cand + 86400.0
    except Exception:
        return 0.0


def load_optout_mutes(config_path: Any) -> Dict[str, dict]:
    """opt-out 静默注册表（与 proactive_topic 同文件；读失败=空表）。"""
    try:
        if not config_path:
            return {}
        p = Path(config_path).parent / "companion_optout_mute.json"
        if not p.exists():
            return {}
        raw = json.loads(p.read_text("utf-8")) or {}
        n = time.time()
        return {str(k): dict(v) for k, v in raw.items()
                if isinstance(v, dict) and float(v.get("until") or 0) > n}
    except Exception:
        return {}


def _last_in_out_ts(msgs: Any) -> Tuple[float, float]:
    """inbox ``list_recent_messages`` 行 → (最新入站 ts, 最新出站 ts)。绝不抛。"""
    last_in = 0.0
    last_out = 0.0
    for m in (msgs or []):
        try:
            ts = float(m.get("ts") or 0)
            if ts <= 0:
                continue
            if str(m.get("direction") or "") == "in":
                last_in = max(last_in, ts)
            else:
                last_out = max(last_out, ts)
        except Exception:
            continue
    return last_in, last_out


# 运行时闸的固定顺序（ticker 短路顺序 = preflight 展示顺序，安全型闸在前）。
RUNTIME_GATE_ORDER: Tuple[str, ...] = (
    "autonomy", "platform", "no_conversation", "no_inbox",
    "automation_mode", "crisis", "optout",
)


def goal_runtime_gates(
    goal: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    inbox: Any,
    mutes: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    emotion_gate: Optional[Callable[[dict, dict], str]] = None,
    stop_on_fail: bool = False,
) -> Dict[str, Any]:
    """单个目标的**运行时**安全闸（D1b P0-4：ticker 与 preflight 的单一事实源）。

    返回 ``{"gates": {name: bool}, "skip": 首个未过闸名或 "", "last_in", "last_out"}``。
    ``stop_on_fail=True``（ticker 用）在首个未过闸处短路——与旧 ``run_once`` 逐条
    ``continue`` 语义等价、零多余 I/O；``False``（preflight 用）把能判的闸都判完，
    坐席一次看全「还差哪几道」而不是修一道再撞下一道。

    fail-closed 口径与旧实现一致：读不到 inbox / 档位 → 该闸不过；
    情绪闸/opt-out 判定**自身异常**→ 放行（那两处历来如此，避免探针故障把全部
    目标判死）。绝不抛。
    """
    n = float(now if now is not None else time.time())
    gates: Dict[str, bool] = {}
    skip = ""
    last_in = last_out = 0.0

    def _fail(name: str) -> bool:
        nonlocal skip
        gates[name] = False
        if not skip:
            skip = name
        return stop_on_fail

    gates["autonomy"] = str(goal.get("autonomy") or "") == "auto"
    if not gates["autonomy"] and _fail("autonomy"):
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    platform = str(goal.get("platform") or "").strip().lower()
    gates["platform"] = platform in (cfg.get("platforms") or ())
    if not gates["platform"] and _fail("platform"):
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    conv = str(goal.get("conversation_id") or "").strip()
    gates["no_conversation"] = bool(conv)
    if not conv:
        # 后面几道全依赖会话 id，无会话时一律视为未过（preflight 全列）
        _fail("no_conversation")
        for k in ("no_inbox", "automation_mode", "crisis", "optout"):
            gates.setdefault(k, False)
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    # 自发消息的安全前提：能读到会话档位与消息流。读不到＝fail-closed
    gates["no_inbox"] = inbox is not None
    if inbox is None:
        _fail("no_inbox")
        for k in ("automation_mode", "crisis", "optout"):
            gates.setdefault(k, False)
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    try:
        mode_ok = str(inbox.get_automation_mode(conv) or "") == "auto_ai"
    except Exception:
        mode_ok = False
    gates["automation_mode"] = mode_ok
    if not mode_ok and _fail("automation_mode"):
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    # 危机 block（安全型，绝不豁免）；soft（普通负面）放行——
    # 冲刺拟稿模板自带「对方明确拒绝/情绪低落就收住」纪律。
    try:
        meta = inbox.get_conv_meta(conv) or {}
    except Exception:
        meta = {}
    crisis_ok = True
    try:
        if emotion_gate is not None:
            verdict = str(emotion_gate(dict(goal), dict(meta)))
        else:
            from src.utils.wellbeing_guard import proactive_emotion_gate
            intensity = meta.get("last_emotion_intensity")
            verdict = proactive_emotion_gate(
                None, now=n,
                last_emotion=str(meta.get("last_emotion") or ""),
                last_emotion_intensity=(
                    float(intensity) if intensity is not None else None),
            )
        crisis_ok = verdict != "block"
    except Exception:
        crisis_ok = True
    gates["crisis"] = crisis_ok
    if not crisis_ok and _fail("crisis"):
        return {"gates": gates, "skip": skip, "last_in": 0.0, "last_out": 0.0}

    try:
        msgs = inbox.list_recent_messages(conv, limit=10) or []
    except Exception:
        msgs = []
    last_in, last_out = _last_in_out_ts(msgs)

    optout_ok = True
    if mutes and conv in mutes:
        try:
            from src.utils.proactive_optout import optout_active
            optout_ok = not optout_active(mutes.get(conv), now=n,
                                          last_in_ts=last_in)
        except Exception:
            optout_ok = True
    gates["optout"] = optout_ok
    if not optout_ok:
        _fail("optout")

    return {"gates": gates, "skip": skip,
            "last_in": last_in, "last_out": last_out}


def preflight_goal(
    goal: Dict[str, Any],
    *,
    cfg_root: Any,
    inbox: Any,
    care_store: Any = None,
    mutes: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    ticker_running: Optional[bool] = None,
    dispatcher_running: Optional[bool] = None,
) -> Dict[str, Any]:
    """「这条目标现在能不能真的自动出手」一次说全（D1b P0-4）。

    三层合一：① 引擎配置层（``sprint_engine_status``：ticker 开关 / dry_run / 平台
    白名单 / 出站真发闸）；② 进程层（ticker / dispatcher 线程是否真在跑——配置说开
    ≠ 进程活着）；③ 目标运行时层（``goal_runtime_gates`` 全列不短路）。
    ``ok`` 只在三层全过时为 True；``blockers`` 为按修复优先级排好的闸名列表，
    ``next_due`` 给「下一拍几点」（仅 ok 时有意义）。绝不抛。
    """
    n = float(now if now is not None else time.time())
    try:
        from src.companion.goals.service import resolve_goals_cfg
        goals_cfg = resolve_goals_cfg(cfg_root)
    except Exception:
        goals_cfg = {}
    cfg = parse_sprint_cfg(goals_cfg)
    out: Dict[str, Any] = {
        "ok": False, "goal_id": str(goal.get("goal_id") or ""),
        "pace": "", "blockers": [], "engine": {}, "runtime": {},
        "process": {}, "next_due": 0.0,
    }
    try:
        from src.companion.goals.pace import is_sprint, resolve_pace
        pace = resolve_pace(goal)
        sprint = is_sprint(pace)
        out["pace"] = str(pace)
    except Exception:
        sprint = False

    # ① 引擎配置层
    engine_blockers: list = []
    try:
        from src.companion.goals.service import sprint_engine_status
        eng = sprint_engine_status(
            cfg_root, platform=str(goal.get("platform") or "")) or {}
        out["engine"] = eng
        engine_blockers = [str(b) for b in (eng.get("sprint_blockers") or [])]
    except Exception:
        out["engine"] = {"error": True}
    if not sprint and not bool(cfg.get("natural_daily", True)):
        engine_blockers.append("natural_daily_off")

    # ② 进程层（None＝调用方不知道，不当 blocker，只如实透传）
    out["process"] = {"ticker_running": ticker_running,
                      "dispatcher_running": dispatcher_running}
    proc_blockers: list = []
    if ticker_running is False:
        proc_blockers.append("ticker_not_running")
    if dispatcher_running is False:
        proc_blockers.append("dispatcher_not_running")

    # ③ 目标运行时层
    rg = goal_runtime_gates(goal, cfg=cfg, inbox=inbox, mutes=mutes, now=n,
                            stop_on_fail=False)
    out["runtime"] = rg["gates"]
    rt_blockers = [k for k in RUNTIME_GATE_ORDER
                   if k in rg["gates"] and not rg["gates"][k]
                   and k not in engine_blockers]   # platform 已由引擎层点名

    # 已完成/已归档的目标本就不该出手，如实标出
    status = str(goal.get("status") or "active")
    if status != "active":
        rt_blockers.insert(0, "goal_not_active")

    out["blockers"] = engine_blockers + proc_blockers + rt_blockers
    out["ok"] = not out["blockers"]

    # 下一拍（只在 ok 时算，避免给「不能发」的目标一个时间承诺）：
    # due_now=本 tick 就会排（相位/日拍已到、沉默/间隔闸都过）；next_due=下一相位点。
    out["due_now"] = False
    out["pending_rows"] = 0
    if out["ok"]:
        try:
            if care_store is not None:
                try:
                    out["pending_rows"] = sum(
                        1 for r in (care_store.list_recent(limit=200) or [])
                        if str(r.get("status") or "") == "pending"
                        and str(r.get("topic_norm") or "").startswith(
                            f"goal:{out['goal_id']}:"))
                except Exception:
                    out["pending_rows"] = 0
            if sprint:
                out["due_now"] = due_phase(
                    goal, cfg=cfg, now=n,
                    last_inbound_ts=rg["last_in"],
                    last_outbound_ts=rg["last_out"]) is not None
                out["next_due"] = float(next_phase_ts(goal, cfg, now=n))
            else:
                out["due_now"] = due_daily(
                    goal, cfg=cfg, now=n,
                    last_inbound_ts=rg["last_in"],
                    last_outbound_ts=rg["last_out"]) is not None
                # 自然档：下一拍＝今天窗口起点（若还没到）或明天窗口起点
                out["next_due"] = next_daily_ts(cfg, now=n)
        except Exception:
            out["next_due"] = 0.0
    return out


class SprintGoalTicker:
    """后台循环：按相位把冲刺主动拍排进 care 管线（常备接线 + 配置热闸，
    ``sprint.enabled=false`` 时每 tick 空转零副作用——与 CareGoalScanner 同哲学）。"""

    def __init__(
        self,
        *,
        care_store: Any,
        config_obj: Any,
        goals_store: Any = None,
        inbox_store_getter: Optional[Callable[[], Any]] = None,
        emotion_gate: Optional[Callable[[Dict[str, Any], Dict[str, Any]], str]] = None,
        interval_sec: float = 120.0,
    ) -> None:
        self._care_store = care_store
        self._config_obj = config_obj
        self._goals_store = goals_store
        self._inbox_getter = inbox_store_getter
        # 情绪/危机档位回调 (goal, conv_meta) -> "block"|"soft"|""。生产接
        # skill_manager._proactive_emotion_gate（能查 crisis_event_store，
        # block 真正可达）；未注入回落纯 meta 判定（只出 soft/""）。
        self._emotion_gate = emotion_gate
        self._interval = max(30.0, float(interval_sec))
        self.last_tick_ts: float = 0.0
        self.ticks: int = 0          # 累计拍数（停摆巡检 payload 用，关闸也计）
        self.last_scheduled: int = 0
        self.last_skips: Dict[str, int] = {}
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    # ── 配置/依赖解析（每 tick 现读=热闸）─────────────────────────────────
    def _cfg_root(self) -> dict:
        cfg = getattr(self._config_obj, "config", None)
        if isinstance(cfg, dict):
            return cfg
        return self._config_obj if isinstance(self._config_obj, dict) else {}

    def _sprint_cfg(self) -> Dict[str, Any]:
        try:
            from src.companion.goals.service import resolve_goals_cfg
            gcfg = resolve_goals_cfg(self._cfg_root())
            if not gcfg.get("enabled", False):
                return {"enabled": False}
            return parse_sprint_cfg(gcfg)
        except Exception:
            return {"enabled": False}

    def _gstore(self):
        if self._goals_store is not None:
            return self._goals_store
        from src.companion.goals.service import get_configured_store
        return get_configured_store(
            self._cfg_root(), getattr(self._config_obj, "config_path", None))

    def _optout_mutes(self) -> Dict[str, dict]:
        return load_optout_mutes(getattr(self._config_obj, "config_path", None))

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def snapshot(self) -> dict:
        cfg = self._sprint_cfg()
        return {
            "running": self.is_running(),
            "enabled": bool(cfg.get("enabled", False)),
            "interval_sec": self._interval,
            "last_tick_ts": self.last_tick_ts,
            "ticks": self.ticks,
            "last_scheduled": self.last_scheduled,
            "last_skips": dict(self.last_skips),
        }

    @staticmethod
    def _note_pace_block(
        gstore: Any, g: Dict[str, Any], gid: str, conv: str, *,
        sprint: bool, cfg: Dict[str, Any], relaxed: Dict[str, Any],
        now: float, last_in: float, last_out: float,
        today_row: Optional[Dict[str, Any]],
    ) -> None:
        """安全闸全过、但 ``not_due``：区分「相位/日键没到」（不算想出手，不记）
        与「到点了被沉默闸 / 出站间隔闸拦住」（记 ``beat_blocked`` silence /
        min_gap，按槽位去重）。M-7 A #236：BABY BEAR 这类白天一直在聊的会话，
        自然档 6h 沉默闸每天都拦、卡上却只写「自动推进」——现在拦一次留一条。"""
        try:
            if sprint:
                p = due_phase(g, cfg=relaxed, now=now)
                slot = f"p{p}" if p is not None else ""
                sil = float(cfg.get("silence_min_sec") or 0)
                gap = float(cfg.get("min_gap_sec") or 0)
            else:
                d = due_daily(g, cfg=relaxed, now=now, today_action=today_row)
                slot = f"d{d}" if d else ""
                sil = float(cfg.get("natural_silence_min_sec") or 0)
                gap = float(cfg.get("natural_min_gap_sec") or 0)
            if not slot:
                return
            reason = ""
            if sil > 0 and last_in > 0 and (now - last_in) < sil:
                reason = "silence"
            elif gap > 0 and last_out > 0 and (now - last_out) < gap:
                reason = "min_gap"
            if reason:
                record_beat_blocked(gstore, gid, reason, slot=slot,
                                    conversation_id=conv, now=now)
        except Exception:
            logger.debug("note_pace_block failed", exc_info=True)

    # ── 主扫描 ────────────────────────────────────────────────────────────
    def run_once(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        n = float(now if now is not None else time.time())
        self.last_tick_ts = n
        self.ticks += 1
        skips: Dict[str, int] = {}
        summary: Dict[str, Any] = {"enabled": True, "scanned": 0,
                                   "scheduled": 0, "skips": skips}

        def _skip(reason: str) -> None:
            skips[reason] = skips.get(reason, 0) + 1

        cfg = self._sprint_cfg()
        if not cfg.get("enabled", False):
            self.last_scheduled = 0
            self.last_skips = {}
            return {"enabled": False, "scanned": 0, "scheduled": 0, "skips": {}}
        try:
            from src.companion.goals.pace import is_sprint, resolve_pace
            gstore = self._gstore()
            goals = gstore.list_goals(status="active", limit=200) or []
        except Exception:
            logger.debug("sprint tick 取目标失败", exc_info=True)
            return summary
        inbox = None
        if self._inbox_getter is not None:
            try:
                inbox = self._inbox_getter()
            except Exception:
                inbox = None
        mutes = self._optout_mutes()
        natural_daily = bool(cfg.get("natural_daily", True))
        # 松闸副本：只看相位/日键到没到，不看沉默/出站间隔——用来判断「本来想
        # 出手」（M-7 A：被安全闸拦下时也要知道这一拍是不是到点了，否则每 tick
        # 都记一条「被拦」就是刷屏，不到点被拦不算「想出手」）
        relaxed = dict(cfg)
        relaxed["silence_min_sec"] = 0.0
        relaxed["min_gap_sec"] = 0.0
        relaxed["natural_silence_min_sec"] = 0.0
        relaxed["natural_min_gap_sec"] = 0.0
        scheduled = 0
        for g in goals:
            try:
                sprint = is_sprint(resolve_pace(g))
                if not sprint and not natural_daily:
                    continue
                summary["scanned"] += 1
                gid = str(g.get("goal_id") or "")
                conv = str(g.get("conversation_id") or "").strip()
                today_row = None
                if not sprint:
                    try:
                        from src.companion.goals.planner import day_key
                        today_row = gstore.get_action(gid, day_key(n))
                    except Exception:
                        today_row = None

                def _due_slot_relaxed() -> str:
                    """到点的槽位（忽略节奏闸）；未到点 → ""。"""
                    if sprint:
                        p = due_phase(g, cfg=relaxed, now=n)
                        return f"p{p}" if p is not None else ""
                    d = due_daily(g, cfg=relaxed, now=n, today_action=today_row)
                    return f"d{d}" if d else ""

                # 运行时闸（D1b P0-4 起与 preflight 同一函数：卡片/预检说「能出手」
                # 与 ticker 真出手用同一套判定，不再各算一套）
                rg = goal_runtime_gates(
                    g, cfg=cfg, inbox=inbox, mutes=mutes, now=n,
                    emotion_gate=self._emotion_gate, stop_on_fail=True)
                if rg["skip"]:
                    _skip(rg["skip"])
                    _slot = _due_slot_relaxed()
                    if _slot:
                        record_beat_blocked(
                            gstore, gid, str(rg["skip"]), slot=_slot,
                            conversation_id=conv, now=n)
                    continue
                last_in, last_out = rg["last_in"], rg["last_out"]
                if sprint:
                    phase = due_phase(
                        g, cfg=cfg, now=n,
                        last_inbound_ts=last_in, last_outbound_ts=last_out)
                    if phase is None:
                        _skip("not_due")
                        self._note_pace_block(
                            gstore, g, gid, conv, sprint=True, cfg=cfg,
                            relaxed=relaxed, now=n, last_in=last_in,
                            last_out=last_out, today_row=None)
                        continue
                    tnorm = phase_norm(gid, phase)
                    topic = (str(g.get("title") or "").strip() or "限时推进")[:40]
                    src = sprint_source_text(
                        g, phase, n, len(cfg.get("phase_points") or ()))
                    evt = f"p{phase}"
                else:
                    # D1b P0-3 自然档每日一拍：与回复链共用今日拍行（consumed/
                    # sent 即让位），窗口内一日一发。
                    day = due_daily(
                        g, cfg=cfg, now=n, last_inbound_ts=last_in,
                        last_outbound_ts=last_out, today_action=today_row)
                    if day is None:
                        _skip("not_due")
                        self._note_pace_block(
                            gstore, g, gid, conv, sprint=False, cfg=cfg,
                            relaxed=relaxed, now=n, last_in=last_in,
                            last_out=last_out, today_row=today_row)
                        continue
                    tnorm = daily_norm(gid, day)
                    topic = (str(g.get("title") or "").strip() or "日常推进")[:40]
                    src = natural_source_text(g, today_row)
                    evt = f"d{day}"
                rid = self._care_store.add_scheduled_care(
                    contact_key=conv,
                    platform=str(g.get("platform") or ""),
                    account_id=str(g.get("account_id") or ""),
                    chat_key=str(g.get("chat_key") or ""),
                    due_at=n + 5.0,
                    event_at=float(g.get("deadline_ts") or 0) or (n + 5.0),
                    topic=topic,
                    topic_norm=tnorm,
                    source_text=src,
                    confidence=1.0,
                    dedup_days=3.0,
                )
                if rid is None:
                    _skip("dedup")
                    continue
                scheduled += 1
                try:
                    gstore.add_event(
                        gid, "sprint_scheduled", f"{evt} care#{int(rid)}")
                except Exception:
                    pass
                try:
                    from src.companion.goals.stats import get_goal_stats
                    get_goal_stats().record_sprint_scheduled()
                except Exception:
                    pass
                if scheduled >= int(cfg.get("max_per_tick") or 6):
                    break
            except Exception:
                logger.debug("sprint tick 单目标异常（跳过）", exc_info=True)
        summary["scheduled"] = scheduled
        self.last_scheduled = scheduled
        self.last_skips = dict(skips)
        if scheduled:
            logger.info("[goal-sprint] 本轮排入 %d 条冲刺主动拍（skips=%s）",
                        scheduled, skips)
        return summary

    # ── 循环骨架（与 CareGoalScanner 同款）─────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="goal_sprint_ticker")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    self.run_once()
                except Exception:
                    logger.exception("goal_sprint_ticker run_once 异常")
                # interval 支持热调（每轮现读配置；enabled=false 时也照常慢转）
                try:
                    itv = float(self._sprint_cfg().get("interval_sec")
                                or self._interval)
                except Exception:
                    itv = self._interval
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(
                            self._stop_evt.wait(),
                            timeout=max(30.0, itv))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("goal_sprint_ticker 退出")


__all__ = [
    "BLOCKED_REASONS",
    "DEFAULT_PHASE_POINTS",
    "PHASE_DIRECTIVES",
    "SprintGoalTicker",
    "due_phase",
    "record_beat_blocked",
    "record_natural_beat_sent",
    "load_optout_mutes",
    "next_phase_ts",
    "next_daily_ts",
    "parse_goal_care_norm",
    "parse_sprint_cfg",
    "phase_directive",
    "phase_norm",
    "record_sprint_beat_sent",
    "remaining_phrase",
    "schedule_nudge",
    "sprint_source_text",
    "RUNTIME_GATE_ORDER",
    "goal_runtime_gates",
    "preflight_goal",
]
