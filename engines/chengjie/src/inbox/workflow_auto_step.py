# -*- coding: utf-8 -*-
"""P2 2026-08-13：链自动推进（``exec_mode=auto``）——话术步自动拟稿「投料」。

设计铁律：**链层绝不自建发送**。自动步只做一件事：按坐席手动「采用并拟稿」
同一条产线（``generate_persona_reply`` + 同款 SOP 指令）生成草稿，落成
L2 pending 草稿（``source_kind='inbox'`` + ``source_id='wf:{exec}:{step}'``
——``uq_drafts_source`` 唯一键＝步级幂等，卡死/重放不双发），由既有
AutosendWorker 管线接走：deliver 双重 opt-in、风控分级、出站翻译、发送幂等、
拟人节奏、send-gate 配额、近重复守卫**全部继承**，链层零发送权。

四重闸（全过才自动，任何一道不过＝回落 remind 提醒档，绝不静默丢拍）：
1. 全局 ``inbox.workflows.auto_advance.enabled``（默认关）；
2. 链 ``exec_mode == 'auto'``（默认 remind；runner 侧判，本模块复核）；
3. 会话 ``automation_mode == auto_ai``（人审/手动会话绝不自动推进营销话术）；
4. 静默时段外（窗内 → 顺延到窗口结束 + 按会话确定性抖动，防清晨齐射）。

预算双层：每 tick 上限（防单轮 LLM 齐射拖垮循环）+ 每日上限（DB 口径跨重启稳）。
生成失败 → 发经典「该跟进了」提醒 toast（人接手），失败可见绝不黑洞。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import zlib
from types import SimpleNamespace
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 顺延抖动上限（秒）：静默窗结束后 0..15min 按会话散开
_QUIET_JITTER_SEC = 900

# B41 通道互斥顺延窗（秒）：常规回复通道活跃时链步平移这么久再试（+会话抖动）。
_MUTEX_DEFER_SEC = 900


def resolve_auto_advance_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``inbox.workflows.auto_advance`` 配置段（缺省全关 + 保守预算）。"""
    out = {"enabled": False, "quiet_start": 23, "quiet_end": 8,
           "max_per_tick": 3, "max_per_day": 30,
           # B41 通道互斥：会话最近一条出站在此窗口内 → 链步顺延（0=关出站判据）
           "mutex_outbound_cooldown_sec": 600}
    try:
        if not isinstance(cfg_root, dict):
            return out
        aa = (((cfg_root.get("inbox") or {}).get("workflows") or {})
              .get("auto_advance") or {})
        if not isinstance(aa, dict):
            return out
        out["enabled"] = bool(aa.get("enabled", False))
        out["quiet_start"] = int(aa.get("quiet_start", 23))
        out["quiet_end"] = int(aa.get("quiet_end", 8))
        out["max_per_tick"] = max(1, int(aa.get("max_per_tick", 3)))
        out["max_per_day"] = max(1, int(aa.get("max_per_day", 30)))
        out["mutex_outbound_cooldown_sec"] = max(0.0, float(
            aa.get("mutex_outbound_cooldown_sec", 600)))
    except Exception:
        pass
    return out


def regular_channel_active(
    store: Any, conv_id: str, *, now: float,
    outbound_cooldown_sec: float = 600.0,
) -> str:
    """B41 通道互斥判据（链让常规）：常规回复通道是否正对该会话「说着话」。

    命中任一 → 返回原因（链步该顺延）：
      - ``pending_draft``：会话已有待处置/停泊中的**非链**草稿（常规链正在
        出稿或等人审）——此刻再投一份链稿＝同问双答（impl49 B41 _241 实录）；
      - ``recent_outbound``：会话刚有出站（窗口内）——紧跟着再来一条营销跟进
        ＝背靠背双发的观感，节奏拍平移比抢话诚实。
    判定 fail-open（查询异常返回空串放行）：互斥是降噪护栏，绝不闸死链功能。
    """
    cid = str(conv_id or "")
    if not cid or store is None:
        return ""
    try:
        rows: list = []
        if hasattr(store, "list_drafts"):
            for _status in ("pending", "enriching"):
                try:
                    rows.extend(store.list_drafts(
                        status=_status, conversation_id=cid, limit=10) or [])
                except TypeError:
                    # 旧 store/测试替身不认 conversation_id 形参 → 跳过该判据
                    rows = []
                    break
        for r in rows:
            if not str(r.get("source_id") or "").startswith("wf:"):
                return "pending_draft"
    except Exception:
        logger.debug("regular_channel_active: 草稿查询异常（放行）", exc_info=True)
    if outbound_cooldown_sec > 0:
        try:
            msgs = store.list_recent_messages(cid, limit=5) or []
            for m in msgs:
                if str(m.get("direction") or "") != "out":
                    continue
                ts = float(m.get("ts") or 0)
                if ts > 0 and now - ts < float(outbound_cooldown_sec):
                    return "recent_outbound"
        except Exception:
            logger.debug(
                "regular_channel_active: 消息查询异常（放行）", exc_info=True)
    return ""


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """静默窗判定（支持跨午夜：23→8）。start==end 视为不启用窗。"""
    h, s, e = int(hour) % 24, int(start) % 24, int(end) % 24
    if s == e:
        return False
    if s < e:
        return s <= h < e
    return h >= s or h < e


def quiet_defer_until(now: float, end_hour: int, conv_id: str = "") -> float:
    """静默窗内 → 顺延到今天/明天的窗口结束时刻 + 按会话确定性抖动。"""
    lt = time.localtime(now)
    end_h = int(end_hour) % 24
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        end_h, 0, 0, 0, 0, -1))
    if base <= now:
        base += 86400.0
    jitter = (zlib.crc32(str(conv_id).encode("utf-8")) % _QUIET_JITTER_SEC)
    return base + float(jitter)


# ── 进程级观测（workflow_tick 每轮抓快照进 autorun state → chain-funnel API）──

_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "attempted": 0,      # hook 判定走自动档的次数（task 已调度）
    "staged": 0,         # 生成成功、草稿已落库（等 autosend 接走）
    "fallback_remind": 0,  # 生成失败/无上下文 → 降级提醒 toast
    "deferred_quiet": 0,   # 静默窗顺延
    "budget_skipped": 0,   # 预算拦下（当轮回落提醒档）
    # B41 通道互斥（2026-08-22）：常规通道活跃 → 链步顺延 / 生成后让位丢弃
    "deferred_mutex": 0,
    "dropped_mutex": 0,
}


def _bump(key: str) -> None:
    with _STATS_LOCK:
        _STATS[key] = int(_STATS.get(key, 0)) + 1


def stats_snapshot() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


def build_auto_instruction(chain_name: str, step_idx: int, note: str) -> str:
    """自动步的拟稿指令——与右栏「采用并拟稿」同语义（cp.chain.drive_instruction），
    附「自动跟进」提示防复读已聊过的话题。中文固定：系统提示词全链中文，
    正文语言由 generate_persona_reply 按会话语言自动决策。"""
    return (
        f"按跟进 SOP「{chain_name}」第{int(step_idx) + 1}步推进：{note}"
        "（自然融入当前对话，别生硬转折、别像模板群发；这是按节奏的主动跟进，"
        "若最近对话已经聊过这个话题，就顺着已有话头自然延续，不要复读）"
    )


def _day_start_ts(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def make_auto_step_hook(app_state: Any, cfg_root: Any):
    """构造 runner 的 ``auto_step_hook``（每 tick 新建＝每 tick 预算自然复位）。

    返回 None＝功能关（runner 零开销走旧行为）。hook 签名
    ``(conv_id, step, ex) -> {"action": "remind"|"defer"|"auto", "until"?: ts}``，
    绝不抛。
    """
    cfg = resolve_auto_advance_cfg(cfg_root)
    if not cfg["enabled"]:
        return None
    store = getattr(app_state, "inbox_store", None)
    if store is None:
        return None
    tick_used = {"n": 0}

    def _hook(conv_id: str, step: Dict[str, Any], ex: Dict[str, Any]) -> Dict[str, Any]:
        try:
            now = time.time()
            note = str((step or {}).get("note") or (step or {}).get("text") or "").strip()
            if not note:
                return {"action": "remind"}
            # 闸 3：会话必须全自动档（人审/手动会话的营销推进必须过人）
            try:
                mode = str(store.get_automation_mode(str(conv_id)) or "")
            except Exception:
                mode = ""
            if mode != "auto_ai":
                return {"action": "remind"}
            # B41 通道互斥（链让常规）：会话已有非链待处置稿 / 刚有出站 → 顺延。
            # 同会话同一时间只允许一条通道出稿——常规回复回应真实入站，优先级
            # 天然高于按节奏的营销跟进；链的拍平移到互斥窗后再试，不丢不抢。
            _mx = regular_channel_active(
                store, str(conv_id), now=now,
                outbound_cooldown_sec=float(
                    cfg.get("mutex_outbound_cooldown_sec", 600)))
            if _mx:
                _bump("deferred_mutex")
                _jit = (zlib.crc32(str(conv_id).encode("utf-8")) % 300)
                return {"action": "defer",
                        "until": now + _MUTEX_DEFER_SEC + float(_jit)}
            # 闸 4：静默窗 → 顺延（不执行不提醒，节奏平移到窗口后）
            lt_hour = time.localtime(now).tm_hour
            if in_quiet_hours(lt_hour, cfg["quiet_start"], cfg["quiet_end"]):
                _bump("deferred_quiet")
                return {"action": "defer",
                        "until": quiet_defer_until(now, cfg["quiet_end"], conv_id)}
            # 预算：每 tick（防 LLM 齐射）+ 每日（DB 口径）——超了回落提醒档，
            # 拍不丢、人接手
            if tick_used["n"] >= cfg["max_per_tick"]:
                _bump("budget_skipped")
                return {"action": "remind"}
            try:
                today_n = int(store.count_workflow_auto_drafts_since(
                    _day_start_ts(now)))
            except Exception:
                today_n = 0
            if today_n >= cfg["max_per_day"]:
                _bump("budget_skipped")
                return {"action": "remind"}
            tick_used["n"] += 1
            _bump("attempted")
            ex_snap = {
                "exec_id": str(ex.get("exec_id") or ""),
                "chain_id": str(ex.get("chain_id") or ""),
                "chain_name": str(ex.get("chain_name") or ex.get("chain_id") or ""),
                "conversation_id": str(conv_id),
                "current_step": int(ex.get("current_step") or 0),
            }
            # workflow_tick 在事件循环线程内同步执行 → 此处必有 running loop；
            # 无 loop（异常调用姿势）→ RuntimeError 被外层捕获降级 remind
            asyncio.get_running_loop().create_task(
                _generate_and_stage(app_state, store, ex_snap, note))
            return {"action": "auto"}
        except Exception:
            logger.debug("auto_step_hook failed（回落提醒档）", exc_info=True)
            return {"action": "remind"}

    return _hook


async def _generate_and_stage(
    app_state: Any, store: Any, ex: Dict[str, Any], note: str,
) -> bool:
    """异步：生成 → 落 L2 草稿。失败 → 发经典提醒 toast（人接手）。绝不抛。"""
    conv_id = str(ex.get("conversation_id") or "")
    step_idx = int(ex.get("current_step") or 0)
    try:
        parts = conv_id.split(":", 2)
        platform = parts[0] if len(parts) == 3 else ""
        account_id = parts[1] if len(parts) == 3 else "default"
        chat_key = parts[2] if len(parts) == 3 else conv_id
        # 上下文：最近消息 → history + 最后一条客户原话（生成器的必需入参；
        # 全程零入站的会话没有可回应的语境 → 诚实降级提醒档，人来定怎么开口）
        history = []
        last_inbound = ""
        try:
            rows = store.list_recent_messages(conv_id, limit=20)
        except Exception:
            rows = []
        for r in rows:
            txt = str(r.get("text") or "").strip()
            if not txt:
                continue
            direction = str(r.get("direction") or "")
            history.append({"role": "user" if direction == "in" else "assistant",
                            "content": txt})
            if direction == "in":
                last_inbound = txt
        if not last_inbound:
            _bump("fallback_remind")
            _publish_step_event(conv_id, ex, note, auto=False)
            return False
        from src.inbox.persona_reply import generate_persona_reply
        out = await generate_persona_reply(
            app=SimpleNamespace(state=app_state),
            platform=platform,
            chat_key=chat_key,
            last_inbound=last_inbound,
            history=history,
            conversation_id=conv_id,
            account_id=account_id,
            agent_instruction=build_auto_instruction(
                str(ex.get("chain_name") or ""), step_idx, note),
        )
        reply = str((out or {}).get("reply") or "").strip()
        if not (out or {}).get("ok") or not reply:
            _bump("fallback_remind")
            _publish_step_event(conv_id, ex, note, auto=False)
            return False
        # B41 通道互斥二次复查：生成的十几秒里常规通道可能已对同一入站出稿
        # （hook 判定与本协程之间的竞态窗）——让位丢弃本稿 + 降级经典提醒
        # （拍不黑洞，人看到「该跟进了」自行决定），绝不与常规稿背靠背双发。
        try:
            if regular_channel_active(store, conv_id, now=time.time()):
                _bump("dropped_mutex")
                _publish_step_event(conv_id, ex, note, auto=False)
                logger.info(
                    "[workflow-auto] guard=channel_mutex 常规通道已出稿，"
                    "链稿让位丢弃 conv=%s step=%s", conv_id, step_idx)
                return False
        except Exception:
            logger.debug("[workflow-auto] 互斥二次复查异常（放行）", exc_info=True)
        # L2 pending 落库：source_id 唯一键幂等；autosend 管线（含 register_l2_callback
        # 事件唤醒）从这里接管——deliver 闸门/翻译/风控/节奏全在那条链上
        store.upsert_draft({
            "source_kind": "inbox",
            "source_id": f"wf:{ex.get('exec_id')}:{step_idx}",
            "conversation_id": conv_id,
            "platform": platform,
            "account_id": account_id,
            "chat_key": chat_key,
            "peer_text": last_inbound,
            "draft_text": reply,
            "draft_lang": str((out or {}).get("reply_lang") or ""),
            "risk_level": "low",
            "autopilot_level": "L2",
            "status": "pending",
            "trace_id": f"wfauto:{ex.get('exec_id')}:{step_idx}",
        })
        _bump("staged")
        _publish_step_event(conv_id, ex, reply, auto=True)
        logger.info("[workflow-auto] SOP 自动拟稿已投料 conv=%s chain=%s step=%s",
                    conv_id, ex.get("chain_id"), step_idx)
        return True
    except Exception:
        logger.warning("[workflow-auto] 自动步生成失败，降级提醒 conv=%s step=%s",
                       conv_id, step_idx, exc_info=True)
        _bump("fallback_remind")
        try:
            _publish_step_event(conv_id, ex, note, auto=False)
        except Exception:
            pass
        return False


def _publish_step_event(
    conv_id: str, ex: Dict[str, Any], text: str, *, auto: bool,
) -> None:
    """与 runner._publish_step_event 同形状（+auto 标记）：auto=True 前端出
    「已自动拟稿」蓝绿 toast；auto=False＝经典「该跟进了」提醒（降级路径）。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        payload = {
            "conversation_id": conv_id,
            "exec_id": ex.get("exec_id"),
            "chain_id": ex.get("chain_id"),
            "chain_name": ex.get("chain_name", ""),
            "action_type": "template",
            "suggested_text": str(text or ""),
            "step": int(ex.get("current_step") or 0),
            "ts": time.time(),
        }
        if auto:
            payload["auto"] = True
        get_event_bus().publish("workflow_step", payload)
    except Exception:
        logger.debug("workflow_step 事件发布失败", exc_info=True)


__all__ = [
    "build_auto_instruction",
    "in_quiet_hours",
    "make_auto_step_hook",
    "quiet_defer_until",
    "regular_channel_active",
    "resolve_auto_advance_cfg",
    "stats_snapshot",
]
