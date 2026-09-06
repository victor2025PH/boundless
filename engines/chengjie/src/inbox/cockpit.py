# -*- coding: utf-8 -*-
"""驾驶舱介入队列聚合器（cockpit P1，2026-08-13）。

产品语义（双面分工方案 v2 §2.3 / §3.4）：把「哪些客户现在需要人」从翻列表
凭感觉变成一个**按优先级排好序的队列**。本模块只做聚合与排序，绝不新造扫描器
——四个信号源全部复用既有可查询原语，各自 try/except 隔离（fail-soft：一个源
坏了其余照常，坏源在 ``sources`` 里如实标 error）：

  1. ``takeover_overdue``（P1 最高）——人工接管超时未交还（takeover 注册表 +
     watchdog takeover_remind.after_min 同一阈值）：接管期该客户 AI 全停，
     超时＝没人管；
  2. ``needs_human``——协议自动回复链打的「需人工」标签会话
     （HANDOFF_TAG：高风险/生成失败/配额熔断等，store.list_tagged_conversations）；
  3. ``waiting``——客户说了最后一句、超过宽限期还没人回（last_message_dirs
     末条方向口径；排除群聊/已归档/有 pending 草稿的——那是 4 号源辖区；
     **刻意不排除 manual 档会话**：manual=AI 不会回，客户在等的就是人，
     这正是本队列该点名的）；同级内按 **effective_unread**（已读水位口径，
     不是协议同步裸数字）把「没看过」排在「看过没回」前面；
  4. ``draft_pending``——AI 草稿拟好等人审超过阈值（store.list_drafts）。

同一会话命中多源 → 去重保最高优先级、其余源记进 ``flags``。快照 30s 模块级
TTL 缓存（驾驶舱 30s 轮询 + 多坐席同看不放大查询；``force=True`` 绕过）。

配置（全部代码默认，``inbox.cockpit.*`` 可覆写，无需新配置块）：
  waiting_grace_min=10 / waiting_max_age_hours=72 / draft_age_min=30 /
  scan_limit=300 / queue_cap=80 / quote_probe_cap=20（空引用补齐每快照探查上限）。
诚实边界（P1 刻意不做）：危机专项信号（消息级检测无会话级可查存储，A 线危机
已由安全网处置、B 线高危经「需人工」标签间接覆盖）与「AI 连败」（worker 熔断
是账号级不是会话级）——待有可查存储再进队列，不造假信号。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.cockpit")

#: kind → 优先级（1 最高；排序键=（priority, -age)：同级更久的在前）
KIND_PRIORITY = {
    "takeover_overdue": 1,
    "needs_human": 2,
    "waiting": 3,
    "draft_pending": 4,
}

_CACHE_TTL_SEC = 30.0
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"ts": 0.0, "snap": None}


def _cfg_block(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    blk = (((config or {}).get("inbox") or {}).get("cockpit") or {})
    return blk if isinstance(blk, dict) else {}


def _num(blk: Dict[str, Any], key: str, default: float) -> float:
    try:
        v = float(blk.get(key, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _effective_unread(row: Any) -> int:
    """与 store.effective_unread 同口径：已读水位盖住末条 → 0。

    驾驶舱 waiting 排序把 unread>0 当「没人看过」。若用同步来的裸 unread，
    坐席在工作台打开过（last_read_ts ≥ last_ts）仍会被手机端数字刷回未读，
    排成「没看」——P2 调准要的是「看过没回」靠后，不是「协议数字还在」。
    纯函数，缺字段/坏类型退回裸 unread。
    """
    try:
        last_ts = float((row or {}).get("last_ts") or 0)
        last_read = float((row or {}).get("last_read_ts") or 0)
        raw = int((row or {}).get("unread") or 0)
    except (TypeError, ValueError, AttributeError):
        try:
            return int((row or {}).get("unread") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0
    if raw <= 0:
        return 0
    return raw if last_ts > last_read else 0


def _item(kind: str, cid: str, *, name: str = "", platform: str = "",
          age_sec: float = 0.0, detail: str = "",
          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    it = {
        "kind": kind,
        "priority": KIND_PRIORITY.get(kind, 9),
        "conversation_id": cid,
        "name": name or cid,
        "platform": platform or (cid.split(":", 1)[0] if ":" in cid else ""),
        "age_sec": round(max(0.0, age_sec), 1),
        "detail": detail,
        "flags": [kind],
    }
    if extra:
        it.update(extra)
    return it


def _src_takeover_overdue(config: Optional[Dict[str, Any]],
                          now: float) -> List[Dict[str, Any]]:
    from src.inbox.takeover import overdue_takeovers
    tr = (((config or {}).get("health_watchdog") or {})
          .get("takeover_remind") or {})
    after_min = _num(tr if isinstance(tr, dict) else {}, "after_min", 120.0)
    out = []
    for e in overdue_takeovers(after_min * 60.0, now=now):
        cid = str(e.get("conversation_id") or "")
        if not cid:
            continue
        out.append(_item(
            "takeover_overdue", cid,
            platform=str(e.get("platform") or ""),
            age_sec=float(e.get("elapsed_sec") or 0),
            detail=str(e.get("by") or ""),
        ))
    return out


def _identity_extra(row: Any) -> Dict[str, Any]:
    """会话行 → 身份展示字段（P3 身份卡：头像/username 透传，缺失给空串）。

    头像可用性判定（Messenger fbcdn 直链会过期）刻意留给前端——与收件箱同一套
    渲染守卫，后端只如实透传库里的值。``account_id``/``chat_key``/``chat_type``
    是头像**代理端点**的寻址三元组（P4：telegram/whatsapp 无稳定直链，前端按
    /api/platforms/{platform}/{account_id}/avatar?chat_key= 懒加载回源）。
    """
    r = row or {}
    return {
        "avatar_url": str(r.get("avatar_url") or ""),
        "username": str(r.get("username") or ""),
        "account_id": str(r.get("account_id") or ""),
        "chat_key": str(r.get("chat_key") or ""),
        "chat_type": str(r.get("chat_type") or ""),
    }


def _src_needs_human(store: Any, now: float,
                     limit: int) -> List[Dict[str, Any]]:
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    out = []
    for row in (store.list_tagged_conversations(HANDOFF_TAG, limit=limit) or []):
        cid = str(row.get("conversation_id") or "")
        if not cid:
            continue
        last_ts = float(row.get("last_ts") or 0)
        extra = {"unread": _effective_unread(row)}
        extra.update(_identity_extra(row))
        out.append(_item(
            "needs_human", cid,
            name=str(row.get("name") or ""),
            platform=str(row.get("platform") or ""),
            age_sec=(now - last_ts) if last_ts > 0 else 0.0,
            detail=str(row.get("last_msg") or "")[:80],
            extra=extra,
        ))
    return out


def _src_waiting(store: Any, now: float, *, grace_sec: float,
                 max_age_sec: float, scan_limit: int,
                 pending_cids: set, takeover_cids: set) -> List[Dict[str, Any]]:
    convs = store.list_conversations(limit=scan_limit) or []
    cids = [str(c.get("conversation_id") or "") for c in convs]
    dirs = store.last_message_dirs([c for c in cids if c]) or {}
    try:
        archived = set(store.archived_conversation_ids() or [])
    except Exception:
        archived = set()
    out = []
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        if not cid or cid in archived or cid in pending_cids:
            continue
        if cid in takeover_cids:
            continue   # 接管中＝有人在管，不算漏球（超时另有 1 号源点名）
        ctype = str(c.get("chat_type") or "").lower()
        if ctype in ("group", "channel", "room"):
            continue
        d = dirs.get(cid) or {}
        if str(d.get("direction") or "") != "in":
            continue
        ts = float(d.get("ts") or 0)
        if ts <= 0:
            continue
        age = now - ts
        if age < grace_sec or age > max_age_sec:
            continue
        # 已读/未读细分（P2 调准）：unread>0＝坐席连看都没看过，同级排更前；
        # unread=0＝看过没回（可能是刻意不回/僵尸会话），排后但不隐藏——
        # 隐藏＝把「读了不回」的漏球藏起来，比误报更糟。
        extra = {"unread": _effective_unread(c)}
        extra.update(_identity_extra(c))
        out.append(_item(
            "waiting", cid,
            name=str(c.get("name") or ""),
            platform=str(c.get("platform") or ""),
            age_sec=age,
            detail=str(c.get("last_msg") or "")[:80],
            extra=extra,
        ))
    return out


def _src_draft_pending(store: Any, now: float, *, min_age_sec: float,
                       conv_names: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = store.list_drafts(status="pending", limit=200) or []
    out = []
    for r in rows:
        cid = str(r.get("conversation_id") or "")
        created = float(r.get("created_ts") or 0)
        if not cid or created <= 0:
            continue
        age = now - created
        if age < min_age_sec:
            continue
        out.append(_item(
            "draft_pending", cid,
            name=conv_names.get(cid, ""),
            platform=str(r.get("platform") or ""),
            age_sec=age,
            detail=str(r.get("final_text") or r.get("draft_text") or "")[:80],
            extra={"draft_id": str(r.get("draft_id") or ""),
                   "autopilot_level": str(r.get("autopilot_level") or "")},
        ))
    return out


def _backfill_quotes(store: Any, items: List[Dict[str, Any]], *,
                     cap: int) -> None:
    """空引用气泡补齐（P5）：会话行 ``last_msg`` 为空（出站镜像缺口/纯媒体消息）
    时，从消息表补末条**入站**文本——「客户说」气泡是坐席判断要不要介入的第一
    依据，空气泡＝逼人点进收件箱再看一次。纯媒体消息给 ``quote_media``（原始
    media_type，前端映射本地化占位「[图片]」——占位文案属展示层，后端不硬编码）。

    成本护栏：只对**终榜**（排序+截断后）补、每快照最多探 ``cap`` 个会话
    （每个一条走 idx_msg_conv_ts 索引的查询，30s 快照缓存再摊薄）；单卡异常
    跳过不伤整个快照。draft_pending 不补——其引用位是 AI 草稿文本，语义不同。
    """
    probed = 0
    for it in items:
        if probed >= cap:
            break
        kind = str(it.get("kind") or "")
        if kind == "draft_pending":
            continue
        field = "last_text" if kind == "takeover_overdue" else "detail"
        if it.get(field):
            continue
        cid = str(it.get("conversation_id") or "")
        if not cid:
            continue
        probed += 1
        try:
            msgs = store.list_recent_messages(cid, limit=10) or []
        except Exception:
            continue
        for m in reversed(msgs):
            if str(m.get("direction") or "") != "in":
                continue
            txt = str(m.get("text") or "").strip()
            if txt:
                it[field] = txt[:80]
            else:
                mt = str(m.get("media_type") or "").strip().lower()
                if mt:
                    it["quote_media"] = mt
            break


def collect_intervention_queue(
    store: Any, config: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """聚合四源 → 去重 → 排序。返回 ``{items, counts, sources}``（无缓存层）。"""
    ts = time.time() if now is None else float(now)
    blk = _cfg_block(config)
    grace_sec = _num(blk, "waiting_grace_min", 10.0) * 60.0
    max_age_sec = _num(blk, "waiting_max_age_hours", 72.0) * 3600.0
    draft_age_sec = _num(blk, "draft_age_min", 30.0) * 60.0
    scan_limit = int(_num(blk, "scan_limit", 300.0))
    queue_cap = int(_num(blk, "queue_cap", 80.0))

    sources: Dict[str, str] = {}
    raw: List[Dict[str, Any]] = []

    # 先取草稿源与接管集（waiting 源要用它们做排除集——顺序有意为之）
    pending_cids: set = set()
    conv_names: Dict[str, str] = {}
    takeover_cids: set = set()
    try:
        from src.inbox.takeover import list_active
        takeover_cids = {str(e.get("conversation_id") or "")
                         for e in (list_active() or [])}
    except Exception:
        logger.debug("[cockpit] takeover 活跃集读取失败（忽略）", exc_info=True)

    conv_info: Dict[str, Dict[str, Any]] = {}
    try:
        convs = store.list_conversations(limit=scan_limit) or []
        conv_info = {str(c.get("conversation_id") or ""): c for c in convs}
        conv_names = {cid: str(c.get("name") or "")
                      for cid, c in conv_info.items()}
    except Exception:
        logger.debug("[cockpit] 会话名映射读取失败（忽略）", exc_info=True)

    try:
        drafts = _src_draft_pending(store, ts, min_age_sec=draft_age_sec,
                                    conv_names=conv_names)
        pending_cids = {i["conversation_id"] for i in drafts}
        raw.extend(drafts)
        sources["draft_pending"] = "ok"
    except Exception:
        sources["draft_pending"] = "error"
        logger.debug("[cockpit] draft_pending 源失败", exc_info=True)

    try:
        raw.extend(_src_takeover_overdue(config, ts))
        sources["takeover_overdue"] = "ok"
    except Exception:
        sources["takeover_overdue"] = "error"
        logger.debug("[cockpit] takeover_overdue 源失败", exc_info=True)

    try:
        raw.extend(_src_needs_human(store, ts, limit=scan_limit))
        sources["needs_human"] = "ok"
    except Exception:
        sources["needs_human"] = "error"
        logger.debug("[cockpit] needs_human 源失败", exc_info=True)

    try:
        raw.extend(_src_waiting(
            store, ts, grace_sec=grace_sec, max_age_sec=max_age_sec,
            scan_limit=scan_limit, pending_cids=pending_cids,
            takeover_cids=takeover_cids))
        sources["waiting"] = "ok"
    except Exception:
        sources["waiting"] = "error"
        logger.debug("[cockpit] waiting 源失败", exc_info=True)

    # 去重：同会话保最高优先级项，其余源并进 flags（坐席一张卡看全所有原因）
    by_cid: Dict[str, Dict[str, Any]] = {}
    for it in raw:
        cid = it["conversation_id"]
        cur = by_cid.get(cid)
        if cur is None:
            by_cid[cid] = it
        else:
            keep, drop = (it, cur) if it["priority"] < cur["priority"] else (cur, it)
            for f in drop.get("flags") or []:
                if f not in keep["flags"]:
                    keep["flags"].append(f)
            # 展示字段择优：名字/头像/寻址字段缺失时借用另一源的
            if not keep.get("name") or keep["name"] == cid:
                if drop.get("name") and drop["name"] != cid:
                    keep["name"] = drop["name"]
            for fld in ("avatar_url", "username",
                        "account_id", "chat_key", "chat_type"):
                if not keep.get(fld) and drop.get(fld):
                    keep[fld] = drop[fld]
            by_cid[cid] = keep

    # 身份补齐（P3 身份卡）：takeover_overdue / draft_pending 源自身不带会话行，
    # 从已取的会话映射补 名字/头像/username；takeover 卡另补客户末条原话
    # （其 detail 是接管人，不覆盖——原话走独立 last_text 字段）。
    for it in by_cid.values():
        info = conv_info.get(it["conversation_id"])
        if not info:
            continue
        if (not it.get("name")) or it["name"] == it["conversation_id"]:
            nm = str(info.get("name") or "")
            if nm and nm != it["conversation_id"]:
                it["name"] = nm
        for fld in ("avatar_url", "username",
                    "account_id", "chat_key", "chat_type"):
            if not it.get(fld):
                v = str(info.get(fld) or "")
                if v:
                    it[fld] = v
        if it["kind"] == "takeover_overdue" and not it.get("last_text"):
            lm = str(info.get("last_msg") or "")[:80]
            if lm:
                it["last_text"] = lm

    # 排序：优先级 → 未读优先（unread>0＝没人看过，比「看过没回」更紧急）→ 久者在前
    items = sorted(by_cid.values(),
                   key=lambda i: (i["priority"],
                                  0 if int(i.get("unread") or 0) > 0 else 1,
                                  -float(i["age_sec"] or 0)))
    items = items[:queue_cap]
    try:
        _backfill_quotes(store, items,
                         cap=int(_num(blk, "quote_probe_cap", 20.0)))
    except Exception:
        logger.debug("[cockpit] 引用补齐失败（忽略）", exc_info=True)
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    out: Dict[str, Any] = {"items": items, "counts": counts, "sources": sources}
    # #207：「需人工」按打标原因分列（dup_guard_blocked / crisis / manual …），
    # 让看板能回答「这些红标是 AI 没回上还是真要人判断」；取数失败不影响主体。
    try:
        from src.integrations.protocol_autoreply import needs_human_by_reason
        out["needs_human_by_reason"] = needs_human_by_reason(store, limit=scan_limit)
    except Exception:
        logger.debug("[cockpit] 需人工按原因分列失败（忽略）", exc_info=True)
    return out


def cockpit_snapshot(store: Any, config: Optional[Dict[str, Any]] = None,
                     *, force: bool = False,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """驾驶舱总览（队列 + 接管统计/进行中列表），30s TTL 缓存。"""
    ts = time.time() if now is None else float(now)
    with _cache_lock:
        cached = _cache.get("snap")
        if (not force and cached is not None
                and (ts - float(_cache.get("ts") or 0)) < _CACHE_TTL_SEC):
            out = dict(cached)
            out["cache_age_sec"] = round(ts - float(_cache["ts"]), 1)
            return out
    snap: Dict[str, Any] = {"generated_at": ts}
    q = collect_intervention_queue(store, config, now=ts)
    snap.update(q)
    try:
        from src.inbox.takeover import list_active, takeover_stats
        active = list_active()
        for e in active:
            _since = e.get("since")
            e["elapsed_sec"] = round(
                ts - (float(_since) if _since is not None else ts), 1)
        snap["takeover"] = {"active": active, "stats": takeover_stats()}
    except Exception:
        logger.debug("[cockpit] takeover 统计读取失败（忽略）", exc_info=True)
        snap["takeover"] = {"active": [], "stats": {}}
    with _cache_lock:
        _cache.update({"ts": ts, "snap": snap})
    out = dict(snap)
    out["cache_age_sec"] = 0.0
    return out


def invalidate_cache() -> None:
    """写操作后强制下次重算（P2 「已处理」摘「需人工」标签用）。

    不失效的话 30s 旧缓存会把刚清掉的卡又闪回来——坐席以为没点上再点一次，
    与 cp-voice「发完消息流不动」是同一类信任破口。
    """
    with _cache_lock:
        _cache.update({"ts": 0.0, "snap": None})


def _reset_cache_for_tests() -> None:
    invalidate_cache()
