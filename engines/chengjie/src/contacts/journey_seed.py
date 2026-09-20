# -*- coding: utf-8 -*-
"""历史旅程种子化（RH-P2，2026-08-01）——把 inbox 存量会话回放进 contacts。

背景：contacts 子系统 2026-08-01 才开闸（流失预警全量榜的数据地基），
journey/事件只从开闸后的新消息开始积累——存量 200+ 会话在全量榜上不可见，
榜单要等多日才自然「长满」。本模块把 inbox 既有私聊会话一次性回放成旅程：

- Contact/Journey 经 ``ContactStore.ensure_channel_identity``（onboarding 唯一
  入口）创建——与线上 hooks 同一路径，绝不旁路建行；
- 消息按**真实历史时间戳**写成 ``msg_in``/``msg_out`` 事件（IntimacyEngine 的
  重放口径；沉默天数/近期活跃度全部忠于史实），event_id 确定性
  （``seed:<conversation_id>:<message_id>``）+ ``INSERT OR IGNORE`` ⇒ 重跑幂等；
- ``journeys.created_at`` 回溯到首条消息（「相识时长」里程碑取自它）；
- ``conversations.contact_id`` **精确**回写（contact 就是从这条会话建的，
  不走 suffix 启发式），且仅在原值为空时写，绝不覆盖既有关联；
- 种子化后逐 journey ``refresh_journey_intimacy`` 物化 stored 分数
  （全量榜按 stored 列扫描排序，不刷新则全部 0 分排不出先后）。

铁律：
- 只挑 ``chat_type='private'`` 且平台在 ``VALID_CHANNELS`` 的会话——群聊/频道
  不是客户旅程（telegram 群的负数 chat_key 再兜一道）；
- 默认 dry-run 只出统计供人审，``apply=True`` 才写；
- 事件 payload 只带 ``{"seed": 1}`` 标记，不复制消息文本（contacts 侧不需要，
  少一份隐私副本；文本仍在 inbox 单一事实源）。

调用方：``scripts/relations_seed_journeys.py``（CLI，data-root 契约）。
门禁：``tests/test_journey_seed.py``。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import VALID_CHANNELS

logger = logging.getLogger(__name__)

# 与 IntimacyEngine.compute_intimacy 的事件读取上限同源（>500 的历史重放
# 引擎侧也只看最近 500 条，多写只涨库不涨分）。
DEFAULT_PER_CONV_CAP = 500

_SEED_PAYLOAD = json.dumps({"seed": 1})


def _ro_conn(inbox_db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(
        f"file:{Path(inbox_db_path).as_posix()}?mode=ro", uri=True, timeout=10,
    )
    con.row_factory = sqlite3.Row
    return con


def list_seed_candidates(
    inbox_conn: sqlite3.Connection,
    *,
    platforms: Optional[Sequence[str]] = None,
    limit: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """扫 inbox 私聊会话，返回 (候选列表, 跳过统计)。

    候选＝``chat_type='private'`` 且平台合法且至少 1 条 in/out 消息；
    telegram 群残留（负数 chat_key）与自会话（``me``）剔除。
    """
    valid = {str(p).strip().lower() for p in (platforms or [])} & VALID_CHANNELS \
        if platforms else set(VALID_CHANNELS)
    rows = inbox_conn.execute(
        "SELECT c.conversation_id, c.platform, c.account_id, c.chat_key, "
        "       c.display_name, c.language, c.contact_id, "
        "       COUNT(m.message_id) AS n_msgs, "
        "       MIN(m.ts) AS first_ts, MAX(m.ts) AS last_ts "
        "FROM conversations c "
        "JOIN messages m ON m.conversation_id = c.conversation_id "
        "WHERE c.chat_type = 'private' AND m.direction IN ('in','out') "
        "GROUP BY c.conversation_id "
        "ORDER BY last_ts DESC",
    ).fetchall()
    out: List[Dict[str, Any]] = []
    skipped = {"platform": 0, "group_like": 0}
    for r in rows:
        plat = str(r["platform"] or "").lower()
        ck = str(r["chat_key"] or "")
        if plat not in valid:
            skipped["platform"] += 1
            continue
        if not ck or ck.startswith("-") or ck == "me":
            skipped["group_like"] += 1
            continue
        out.append(dict(r))
    if limit and limit > 0:
        out = out[:limit]
    return out, skipped


def plan_events(
    inbox_conn: sqlite3.Connection,
    conversation_id: str,
    *,
    cap: int = DEFAULT_PER_CONV_CAP,
) -> List[Tuple[str, str, int]]:
    """取该会话最近 ``cap`` 条 in/out 消息 → [(message_id, event_type, ts)] 升序。"""
    rows = inbox_conn.execute(
        "SELECT message_id, direction, ts FROM messages "
        "WHERE conversation_id=? AND direction IN ('in','out') "
        "ORDER BY ts DESC LIMIT ?",
        (conversation_id, int(cap)),
    ).fetchall()
    out: List[Tuple[str, str, int]] = []
    for r in reversed(rows):
        ts = int(float(r["ts"] or 0))
        if ts <= 0:
            continue
        etype = "msg_in" if str(r["direction"]) == "in" else "msg_out"
        out.append((str(r["message_id"]), etype, ts))
    return out


def seed_conversation(
    store: Any,
    conv: Dict[str, Any],
    events: Sequence[Tuple[str, str, int]],
    *,
    apply: bool = False,
) -> Dict[str, Any]:
    """把单个会话回放进 contacts（apply=False 只出预览计数，零写）。"""
    conversation_id = str(conv["conversation_id"])
    if not apply:
        return {
            "conversation_id": conversation_id,
            "events_planned": len(events),
            "applied": False,
        }
    contact, _ci, created = store.ensure_channel_identity(
        channel=str(conv["platform"]).lower(),
        account_id=str(conv["account_id"] or "default"),
        external_id=str(conv["chat_key"]),
        display_name=str(conv.get("display_name") or ""),
        language_hint=str(conv.get("language") or ""),
    )
    journey = store.get_journey_by_contact(contact.contact_id)
    if journey is None:  # ensure_channel_identity 契约上必建 journey；防御兜底
        logger.warning("seed: journey missing for contact %s", contact.contact_id)
        return {
            "conversation_id": conversation_id,
            "events_planned": len(events),
            "events_inserted": 0,
            "applied": True,
            "error": "journey_missing",
        }
    first_ts = min((ts for _mid, _et, ts in events), default=0)
    with store._lock:  # noqa: SLF001 — 与仓内测试/引擎同款包内协作
        before = store._conn.total_changes  # noqa: SLF001
        for mid, etype, ts in events:
            store._conn.execute(  # noqa: SLF001
                "INSERT OR IGNORE INTO journey_events"
                "(event_id, journey_id, trace_id, event_type, payload_json, ts) "
                "VALUES (?, ?, '', ?, ?, ?)",
                (f"seed:{conversation_id}:{mid}", journey.journey_id,
                 etype, _SEED_PAYLOAD, ts),
            )
        inserted = store._conn.total_changes - before  # noqa: SLF001
        if first_ts > 0:
            # 相识时间回溯到首条消息（幂等：只往更早改）
            store._conn.execute(  # noqa: SLF001
                "UPDATE journeys SET created_at=? WHERE journey_id=? AND created_at>?",
                (first_ts, journey.journey_id, first_ts),
            )
        store._conn.commit()  # noqa: SLF001
    return {
        "conversation_id": conversation_id,
        "journey_id": journey.journey_id,
        "contact_id": contact.contact_id,
        "contact_created": bool(created),
        "events_planned": len(events),
        "events_inserted": int(inserted),
        "applied": True,
    }


def write_contact_ids(
    inbox_db_path: Path,
    mapping: Dict[str, str],
) -> int:
    """精确回写 conversations.contact_id（仅原值为空时；短事务，WAL 下与服务共存）。"""
    if not mapping:
        return 0
    con = sqlite3.connect(str(inbox_db_path), timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        written = 0
        for conversation_id, contact_id in mapping.items():
            cur = con.execute(
                "UPDATE conversations SET contact_id=? "
                "WHERE conversation_id=? AND (contact_id IS NULL OR contact_id='')",
                (contact_id, conversation_id),
            )
            written += max(0, cur.rowcount)
        con.commit()
        return written
    finally:
        con.close()


def run_seed(
    inbox_db_path: Path,
    store: Any,
    engine: Any = None,
    *,
    apply: bool = False,
    per_conv_cap: int = DEFAULT_PER_CONV_CAP,
    platforms: Optional[Sequence[str]] = None,
    limit: int = 0,
    write_contact_id: bool = True,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """整库种子化编排（默认 dry-run）。返回汇总统计 dict。"""
    t0 = time.time()
    inbox_db_path = Path(inbox_db_path)
    ro = _ro_conn(inbox_db_path)
    try:
        candidates, skipped = list_seed_candidates(
            ro, platforms=platforms, limit=limit)
        events_by_conv: Dict[str, List[Tuple[str, str, int]]] = {}
        for conv in candidates:
            evs = plan_events(ro, str(conv["conversation_id"]), cap=per_conv_cap)
            if evs:
                events_by_conv[str(conv["conversation_id"])] = evs
    finally:
        ro.close()

    summary: Dict[str, Any] = {
        "apply": bool(apply),
        "inbox_db": str(inbox_db_path),
        "candidates": len(candidates),
        "skipped": skipped,
        "conversations_seeded": 0,
        "contacts_created": 0,
        "events_planned": sum(len(v) for v in events_by_conv.values()),
        "events_inserted": 0,
        "contact_ids_written": 0,
        "intimacy_refreshed": 0,
        "errors": 0,
    }
    if not apply:
        summary["conversations_seeded"] = len(events_by_conv)
        summary["duration_s"] = round(time.time() - t0, 2)
        return summary

    contact_id_by_conv: Dict[str, str] = {}
    jids: List[str] = []
    for conv in candidates:
        cid = str(conv["conversation_id"])
        evs = events_by_conv.get(cid)
        if not evs:
            continue
        try:
            res = seed_conversation(store, conv, evs, apply=True)
        except Exception:
            logger.warning("seed: conversation %s failed", cid, exc_info=True)
            summary["errors"] += 1
            continue
        if res.get("error"):
            summary["errors"] += 1
            continue
        summary["conversations_seeded"] += 1
        summary["events_inserted"] += int(res.get("events_inserted") or 0)
        if res.get("contact_created"):
            summary["contacts_created"] += 1
        jid = str(res.get("journey_id") or "")
        if jid:
            jids.append(jid)
        # 原值为空才回写（list_seed_candidates 已带原 contact_id）
        if write_contact_id and not str(conv.get("contact_id") or ""):
            contact_id_by_conv[cid] = str(res.get("contact_id") or "")

    if write_contact_id and contact_id_by_conv:
        try:
            summary["contact_ids_written"] = write_contact_ids(
                inbox_db_path,
                {k: v for k, v in contact_id_by_conv.items() if v},
            )
        except Exception:
            logger.warning("seed: contact_id writeback failed", exc_info=True)

    if engine is not None:
        ts_now = int(now if now is not None else time.time())
        for jid in jids:
            try:
                engine.refresh_journey_intimacy(jid, now=ts_now)
                summary["intimacy_refreshed"] += 1
            except Exception:
                logger.debug("seed: intimacy refresh failed jid=%s", jid,
                             exc_info=True)
    summary["duration_s"] = round(time.time() - t0, 2)
    return summary
