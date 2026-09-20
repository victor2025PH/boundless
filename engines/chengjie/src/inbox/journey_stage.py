# -*- coding: utf-8 -*-
"""实施92 P0-2：客户旅程阶段脊柱（journey stage，业务成交轴）。

「打招呼 → 成交」在产品里此前是碎的：proactive 管打招呼、SOP 链管跟进、
goals 管方向、monetization 管收钱——没有一根统一的「这个客户生意走到哪了」
的状态脊柱。本模块补上它：

    new → contacted → nurturing → quoting → deal → repeat

- **与 rel_stage_*（陪伴亲密度轴）正交**：那是关系深浅，这是成交进度；
- **规则推导 + 前进棘轮**：自动推导只前进不后退（contacted/nurturing 由
  往来统计推导、quoting 由报价关键词命中推导）；deal/repeat 只由成交事件
  或人工设置进入（自动推导绝不宣称成交）；
- **人工优先**：src=manual 的阶段自动推导不覆盖（唯一例外：成交事件）；
  且 manual 之后的关键词扫描只看设置时刻之后的新消息——坐席刚把误判的
  quoting 降回 nurturing，旧报价消息不得把它立刻顶回去；
- **懒扫描**：不挂 ingest 热路径，随 workflow_autorun 60s tick 增量扫
  「上次扫描后有新消息的会话」（P0 实测 14 天 39 活跃会话，每 tick 通常
  0~3 条）——阶段是天级语义，一分钟内到位绰绰有余，换来入站零改动；
- **成交事件**（P0-3）：record_deal / revoke_deal 带 prev_stage 回退语义，
  金额进 deal_events 台账供漏斗营收归因。

配置 ``inbox.workflows.journey``（新子系统默认关）：
    enabled: false
    nurturing_min_days: 2      # 互动跨 ≥N 个自然日 → nurturing
    nurturing_min_msgs: 6      # 且往来总条数 ≥N
    quote_lookback_days: 14    # 报价关键词回看窗
    quote_keywords: [...]      # 缺省保守词表，可按行业覆写
    scan_limit: 120            # 每 tick 增量扫描上限

成交标记/撤销 **不受 enabled 闸**（记账是事实陈述，随时可用）；
enabled 只闸自动推导扫描与 stage_enter 自动挂链的消费面。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGE_ORDER = ["new", "contacted", "nurturing", "quoting", "deal", "repeat"]
_STAGE_IDX = {s: i for i, s in enumerate(STAGE_ORDER)}

# 保守缺省词表（可经 journey.quote_keywords 按行业覆写）：出现即视为
# 「报价/价格谈判已发生」。误升的代价只是提前进入报价跟单节奏，可接受；
# 漏判由坐席手动设阶段兜底。
DEFAULT_QUOTE_KEYWORDS = [
    "报价", "价格", "多少钱", "怎么收费", "收费标准", "费用", "付款",
    "下单", "定金", "首付", "套餐价", "优惠价", "总价", "一共多少",
    "price", "quote", "how much", "payment", "deposit", "invoice",
    "total is", "cost is",
]

# 首次开闸/重启后的扫描回看窗（秒）：没有 last_scan 状态时别把全库当增量
_BOOTSTRAP_LOOKBACK_SEC = 6 * 3600


def resolve_journey_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``inbox.workflows.journey`` 配置段（缺省关 + 保守参数）。绝不抛。

    ``flavor``（实施93）：``sales``（默认，报价→成交，价格关键词推导 quoting）
    | ``guide``（引导→转化：quoting/deal 位改由 CTA 铸链/点击事件驱动，
    关键词推导停用；阶段内部 id 不变，标签由前端按 flavor 换文案）。
    """
    out: Dict[str, Any] = {
        "enabled": False,
        "flavor": "sales",
        "nurturing_min_days": 2,
        "nurturing_min_msgs": 6,
        "quote_lookback_days": 14,
        "quote_keywords": list(DEFAULT_QUOTE_KEYWORDS),
        "scan_limit": 120,
    }
    try:
        if not isinstance(cfg_root, dict):
            return out
        j = (((cfg_root.get("inbox") or {}).get("workflows") or {})
             .get("journey") or {})
        if not isinstance(j, dict):
            return out
        out["enabled"] = bool(j.get("enabled", False))
        fl = str(j.get("flavor") or "sales").strip().lower()
        out["flavor"] = fl if fl in ("sales", "guide") else "sales"
        out["nurturing_min_days"] = max(1, int(j.get("nurturing_min_days", 2)))
        out["nurturing_min_msgs"] = max(2, int(j.get("nurturing_min_msgs", 6)))
        out["quote_lookback_days"] = max(1, int(j.get("quote_lookback_days", 14)))
        kw = j.get("quote_keywords")
        if isinstance(kw, list) and kw:
            out["quote_keywords"] = [str(k).strip() for k in kw if str(k).strip()]
        out["scan_limit"] = max(10, min(500, int(j.get("scan_limit", 120))))
    except Exception:
        pass
    return out


def stage_rank(stage: str) -> int:
    return _STAGE_IDX.get(str(stage or ""), -1)


def _texts_hit_quote(texts: List[Dict[str, Any]], keywords: List[str]) -> bool:
    for m in texts:
        t = str(m.get("text") or "").lower()
        if not t:
            continue
        for kw in keywords:
            if kw and kw.lower() in t:
                return True
    return False


def derive_stage(
    current_stage: str,
    stats: Dict[str, Any],
    *,
    quote_hit: bool,
    cfg: Dict[str, Any],
) -> str:
    """纯函数：由往来统计推导目标阶段（前进棘轮，永不给出 deal/repeat）。"""
    in_n = int(stats.get("in_n") or 0)
    out_n = int(stats.get("out_n") or 0)
    days = int(stats.get("active_days") or 0)
    candidate = "new"
    if in_n > 0 and out_n > 0:
        candidate = "contacted"
        if (days >= int(cfg.get("nurturing_min_days", 2))
                and in_n + out_n >= int(cfg.get("nurturing_min_msgs", 6))):
            candidate = "nurturing"
    if quote_hit and in_n > 0 and out_n > 0:
        candidate = "quoting"
    cur_rank = stage_rank(current_stage)
    return candidate if stage_rank(candidate) > cur_rank else str(current_stage or "new")


def _publish_stage_event(
    conversation_id: str, frm: str, to: str, src: str,
) -> None:
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("journey_stage_advance", {
            "conversation_id": conversation_id,
            "from": frm,
            "to": to,
            "src": src,
            "ts": time.time(),
        })
    except Exception:
        logger.debug("journey_stage_advance 事件发布失败", exc_info=True)


def _eval_conversation(
    store: Any, cid: str, cfg: Dict[str, Any], now: float,
) -> Optional[Dict[str, str]]:
    """单会话评估。返回 {from,to} 变迁或 None。"""
    cur = store.get_journey_stage(cid)
    cur_stage = str(cur.get("stage") or "")
    # 成交后的阶段只由成交事件推进（deal→repeat）；自动推导止步
    if cur_stage in ("deal", "repeat"):
        return None
    stats = store.conv_exchange_stats(cid)
    quote_hit = False
    # guide 风味（实施93）：quoting 位（=已引导）只由 CTA 铸链事件驱动，
    # 价格关键词推导整体停用——「聊到价格」在导流业务里不代表任何阶段。
    if str(cfg.get("flavor") or "sales") != "guide":
        quote_since = now - float(cfg["quote_lookback_days"]) * 86400
        # 人工设置的阶段：只认设置时刻之后的新证据（防「刚降级就被旧消息顶回」）
        if str(cur.get("src") or "") == "manual":
            quote_since = max(quote_since, float(cur.get("ts") or 0))
        texts = store.list_message_texts_since(cid, quote_since, limit=60)
        quote_hit = _texts_hit_quote(texts, cfg["quote_keywords"])
    new_stage = derive_stage(cur_stage, stats, quote_hit=quote_hit, cfg=cfg)
    if new_stage == cur_stage or stage_rank(new_stage) <= stage_rank(cur_stage):
        return None
    store.set_journey_stage(cid, new_stage, src="auto", ts=now)
    _publish_stage_event(cid, cur_stage, new_stage, "auto")
    return {"from": cur_stage, "to": new_stage, "conversation_id": cid}


def scan_and_update(
    store: Any, cfg_root: Any, state: Dict[str, Any], *,
    now: Optional[float] = None,
) -> List[Dict[str, str]]:
    """增量扫描（workflow_autorun 每 tick 调）。返回本轮阶段变迁列表。绝不抛。"""
    n = float(now if now is not None else time.time())
    cfg = resolve_journey_cfg(cfg_root)
    if not cfg["enabled"] or store is None:
        return []
    transitions: List[Dict[str, str]] = []
    try:
        last_scan = float(state.get("journey_scan_ts") or 0)
        since = last_scan if last_scan > 0 else (n - _BOOTSTRAP_LOOKBACK_SEC)
        convs = store.list_recent_active_conversations(
            since, limit=cfg["scan_limit"])
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            if not cid:
                continue
            try:
                tr = _eval_conversation(store, cid, cfg, n)
                if tr:
                    transitions.append(tr)
            except Exception:
                logger.debug("journey 单会话评估失败（已忽略）%s", cid,
                             exc_info=True)
        state["journey_scan_ts"] = n
        if transitions:
            logger.info("[journey] 阶段推进 %d 条: %s", len(transitions),
                        ", ".join(f"{t['conversation_id']}:{t['from'] or 'new'}"
                                  f"->{t['to']}" for t in transitions[:5]))
    except Exception:
        logger.debug("journey scan 失败（已忽略）", exc_info=True)
    return transitions


def backfill_stages(store: Any, cfg_root: Any, *, limit: int = 300) -> int:
    """一次性回填（成交引擎预设开启时调）：对最近活跃的存量会话推导阶段。

    与增量扫描同一评估函数；enabled 关也可跑（预设 apply 在写开关前后调用
    顺序无关紧要——这里显式绕过 enabled 闸，因为调用本身就是开启动作）。
    """
    if store is None:
        return 0
    cfg = resolve_journey_cfg(cfg_root)
    n = time.time()
    updated = 0
    try:
        convs = store.list_recent_active_conversations(0, limit=limit)
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            if not cid:
                continue
            try:
                if _eval_conversation(store, cid, cfg, n):
                    updated += 1
            except Exception:
                continue
    except Exception:
        logger.debug("journey backfill 失败", exc_info=True)
    return updated


def set_stage_manual(
    store: Any, conversation_id: str, stage: str, *, by: str = "",
) -> Dict[str, Any]:
    """坐席显式设置阶段（任意方向，含降级）。返回 {ok, stage} 或 {ok:False}。"""
    st = str(stage or "").strip().lower()
    if st not in STAGE_ORDER:
        return {"ok": False, "error": "bad_stage"}
    cur = store.get_journey_stage(conversation_id)
    store.set_journey_stage(conversation_id, st, src="manual")
    _publish_stage_event(conversation_id, str(cur.get("stage") or ""), st, "manual")
    return {"ok": True, "stage": st, "prev": str(cur.get("stage") or "")}


def record_deal(
    store: Any, conversation_id: str, *,
    amount: float = 0.0, currency: str = "", note: str = "",
    recorded_by: str = "", source: str = "manual", ref: str = "",
) -> Dict[str, Any]:
    """成交标记（P0-3）：落台账 + 阶段推进 deal/repeat + 事件。

    刻意不受 journey.enabled 闸——记一笔成交是事实记账；阶段字段随手更新，
    自动化消费面（stage_enter 挂链）仍由 enabled 闸住。
    ref＝外部幂等键（93b webhook；重推去重由调用方 find_deal_event_by_ref 先查）。
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"ok": False, "error": "bad_conversation"}
    cur = store.get_journey_stage(cid)
    prior_deals = int(store.count_active_deals(cid))
    _kw = {"ref": ref} if ref else {}
    try:
        deal_id = store.record_deal_event(
            cid, amount=amount, currency=currency, note=note,
            recorded_by=recorded_by, source=source,
            prev_stage=str(cur.get("stage") or ""), **_kw)
    except TypeError:
        # 旧 store 替身无 ref 形参——幂等键丢失但记账不丢（测试替身/灰度期）
        deal_id = store.record_deal_event(
            cid, amount=amount, currency=currency, note=note,
            recorded_by=recorded_by, source=source,
            prev_stage=str(cur.get("stage") or ""))
    new_stage = "repeat" if prior_deals >= 1 else "deal"
    store.set_journey_stage(cid, new_stage, src="deal")
    _publish_stage_event(cid, str(cur.get("stage") or ""), new_stage, "deal")
    _cancel_obsolete_stage_chains(store, cid, str(cur.get("stage") or ""))
    return {"ok": True, "deal_id": deal_id, "stage": new_stage}


def _cancel_obsolete_stage_chains(store: Any, cid: str, old_stage: str) -> int:
    """实施93：阶段前进后，因**旧阶段**自动挂上的在途链让路取消（点击促发链
    的使命随点击/转化完成而结束）。绝不抛；store 无该方法（旧替身）＝0。"""
    if not old_stage:
        return 0
    try:
        fn = getattr(store, "cancel_running_chains_with_stage_trigger", None)
        return int(fn(cid, old_stage)) if callable(fn) else 0
    except Exception:
        return 0


def mark_guided_by_cta(
    store: Any, conversation_id: str, *, now: Optional[float] = None,
) -> bool:
    """实施93：铸链即引导——阶段推进到 quoting 位（guide 风味标签＝已引导）。
    前进棘轮：已在更高阶段（deal/repeat）不动；重复铸链幂等。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return False
    cur = store.get_journey_stage(cid)
    cur_stage = str(cur.get("stage") or "")
    if stage_rank("quoting") <= stage_rank(cur_stage):
        return False
    store.set_journey_stage(cid, "quoting", src="cta",
                            ts=float(now if now is not None else time.time()))
    _publish_stage_event(cid, cur_stage, "quoting", "cta")
    return True


def record_conversion(
    store: Any, conversation_id: str, *, source: str = "cta_click",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """实施93：轻转化（短链点击）——阶段推进 deal 位（guide 标签＝已转化），
    **不写 deal_events**（点击不是钱；深转化 webhook/人工标记才进营收台账）。
    已在 deal/repeat：幂等不动（重复点击不刷 repeat——复购/复访语义留给
    真实二次转化事件）。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"ok": False, "error": "bad_conversation"}
    cur = store.get_journey_stage(cid)
    cur_stage = str(cur.get("stage") or "")
    if stage_rank(cur_stage) >= stage_rank("deal"):
        return {"ok": True, "stage": cur_stage, "advanced": False}
    store.set_journey_stage(cid, "deal", src="cta",
                            ts=float(now if now is not None else time.time()))
    _publish_stage_event(cid, cur_stage, "deal", source)
    _cancel_obsolete_stage_chains(store, cid, cur_stage)
    return {"ok": True, "stage": "deal", "advanced": True}


def revoke_deal(
    store: Any, conversation_id: str, deal_id: int,
) -> Dict[str, Any]:
    """撤销成交（误标回退）：软删台账行；若撤后无剩余成交且当前处于
    deal/repeat，则回退到该事件记录的 prev_stage（还原误标前的状态）。"""
    cid = str(conversation_id or "").strip()
    ev = store.get_deal_event(int(deal_id))
    if not ev or str(ev.get("conversation_id") or "") != cid:
        return {"ok": False, "error": "not_found"}
    if int(ev.get("revoked") or 0):
        return {"ok": False, "error": "already_revoked"}
    if not store.revoke_deal_event(int(deal_id)):
        return {"ok": False, "error": "revoke_failed"}
    remaining = int(store.count_active_deals(cid))
    cur = store.get_journey_stage(cid)
    cur_stage = str(cur.get("stage") or "")
    new_stage = cur_stage
    if cur_stage in ("deal", "repeat"):
        if remaining == 0:
            new_stage = str(ev.get("prev_stage") or "")
        elif remaining == 1 and cur_stage == "repeat":
            new_stage = "deal"
    if new_stage != cur_stage:
        store.set_journey_stage(cid, new_stage, src="auto")
        _publish_stage_event(cid, cur_stage, new_stage, "auto")
    return {"ok": True, "stage": new_stage, "remaining": remaining}
