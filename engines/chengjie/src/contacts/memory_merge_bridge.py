"""Contact 合并 → 情景记忆合流桥（跨平台档案 P2，2026-08-18）。

修 P0 调研的「断点 1」：``ContactGateway.merge_contacts`` 只搬 contacts.db 的渠道
身份，CPI/episodic 不动——运营眼里「已合并成一个人」，AI 依然两边失忆，此前要去
ai_studio 开发者面板手工关联。本桥在**合并动作成功之后**把该客户全部会话身份的
记忆键合流到同一 canonical（复用生产验证过的 ``link_and_merge_memory``：幂等、
content_hash 去重、整簇迁移）。

关键设计：
- **会话事实优先，不猜命名空间**：CPI 的 platform 命名空间（``line_rpa``）与
  contacts channel（``line``）历史上并不同名。桥不做映射表，而是从 inbox
  ``conversations`` 反查该客户**真实存在**的会话（contact_id 回写 + CI external
  后缀匹配双通道），用真实 ``platform:account:chat_key`` 推记忆键——生产真相
  单源，永不猜。
- **uid 推导与 ``_episodic_storage_key`` 同口径**：非 default 账号加 ``acct:`` 前缀
  （双号隔离契约），否则合并后读写键对不上等于白合。
- 全程 fail-soft：桥失败只损失合流，绝不影响合并动作本身（调用方已完成合并）。
- **拆分对偶**：``unlink_identity_memory`` 把被拆出的身份 unlink 回独立 canonical
  （只影响未来读写；已混合的历史事实无法归属拆分——与 identity 路由同语义，诚实）。

开关 ``contacts.origin_profile.merge_memory``（默认关）由**路由层**判定后才调本桥；
桥本身无配置读取（纯执行器，可单测）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_PAIRS = 12   # 单客户会话身份合流上限（防病态数据整库互链）


def conv_id_to_memory_pair(conversation_id: str) -> Optional[Tuple[str, str]]:
    """``platform:account:chat_key`` → (CPI platform, CPI uid)。

    uid 口径与 ``skill_manager._episodic_storage_key`` 一致：
    default/空账号＝裸 chat_key；其余＝``account:chat_key``（双号隔离）。
    chat_key 可含冒号（只切前两段）。
    """
    s = str(conversation_id or "").strip()
    parts = s.split(":", 2)
    if len(parts) < 3 or not parts[0] or not parts[2]:
        return None
    platform, account, chat_key = parts[0], parts[1] or "default", parts[2]
    uid = chat_key if account in ("", "default") else f"{account}:{chat_key}"
    return (platform, uid)


def collect_contact_memory_pairs(
    contacts_store: Any, inbox_store: Any, contact_id: str,
    *, ci_ids: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    """该客户全部会话身份的 (platform, uid) 记忆键对（去重保序，最近活跃优先）。

    双通道反查（与 identity_bridge 已知局限对齐）：
    ① ``conversations.contact_id`` 回写行（ingest 命中过的）；
    ② 逐 CI ``find_conversation_ids_by_external``（后缀匹配，覆盖未回写/前缀形态）。
    ``ci_ids`` 非空＝只看这些 CI（拆分场景：只解被拆出的那个身份）。
    """
    if contacts_store is None or inbox_store is None or not contact_id:
        return []
    conv_ids: List[str] = []
    if ci_ids is None:
        try:
            conv_ids.extend(
                inbox_store.list_conversation_ids_for_contact(contact_id) or [])
        except Exception:
            logger.debug("merge-bridge: contact_id 反查失败", exc_info=True)
    try:
        cis = contacts_store.list_channel_identities_of(contact_id) or []
    except Exception:
        cis = []
    want = {str(c) for c in (ci_ids or [])}
    for ci in cis:
        if want and str(getattr(ci, "channel_identity_id", "")) not in want:
            continue
        try:
            found = inbox_store.find_conversation_ids_by_external(
                getattr(ci, "channel", ""), getattr(ci, "account_id", "") or "default",
                getattr(ci, "external_id", ""))
            conv_ids.extend(found or [])
        except Exception:
            continue
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for cid in conv_ids:
        pair = conv_id_to_memory_pair(cid)
        if pair is None or pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= _MAX_PAIRS:
            break
    return pairs


def merge_contact_memory(
    *,
    contacts_store: Any,
    inbox_store: Any,
    cpi: Any,
    episodic_store: Any,
    contact_id: str,
) -> Dict[str, Any]:
    """把 ``contact_id`` 名下全部会话身份的记忆合流到同一 canonical。

    以第一个（最近活跃）身份为锚，逐个 ``link_and_merge_memory``（幂等：已同
    canonical 的对子 ``already_linked`` 不计数）。返回
    ``{pairs, linked, rows_merged, skipped}``；任何缺件（cpi/store 不在）→ 全零。
    """
    out = {"pairs": 0, "linked": 0, "rows_merged": 0, "skipped": ""}
    if cpi is None:
        out["skipped"] = "no_cpi"
        return out
    pairs = collect_contact_memory_pairs(contacts_store, inbox_store, contact_id)
    out["pairs"] = len(pairs)
    if len(pairs) < 2:
        out["skipped"] = out["skipped"] or "single_identity"
        return out
    from src.utils.cross_platform_identity import link_and_merge_memory
    anchor = pairs[0]
    for other in pairs[1:]:
        try:
            res = link_and_merge_memory(
                cpi, episodic_store, anchor[0], anchor[1], other[0], other[1])
            if not res.get("already_linked"):
                out["linked"] += 1
                out["rows_merged"] += int(res.get("memory_rows_merged") or 0)
        except Exception:
            logger.warning(
                "merge-bridge: link %s <- %s 失败", anchor, other, exc_info=True)
    if out["linked"]:
        logger.info(
            "[origin] 合并联动记忆合流 contact=%s pairs=%d linked=%d rows=%d",
            contact_id, out["pairs"], out["linked"], out["rows_merged"])
        try:
            from src.contacts.origin_context import (
                invalidate_origin_cache, record_merge_stats,
            )
            record_merge_stats(out["linked"], out["rows_merged"])
            invalidate_origin_cache()
        except Exception:
            pass
    return out


def unlink_identity_memory(
    *,
    contacts_store: Any,
    inbox_store: Any,
    cpi: Any,
    contact_id: str,
    ci_id: str,
) -> Dict[str, Any]:
    """拆分对偶：把被拆出身份（新 contact 的唯一 CI）的会话记忆键 unlink 回独立
    canonical。只影响未来读写；历史混合事实留在原 canonical（诚实标注，无法归属拆分）。"""
    out = {"pairs": 0, "unlinked": 0}
    if cpi is None:
        return out
    pairs = collect_contact_memory_pairs(
        contacts_store, inbox_store, contact_id, ci_ids=[ci_id])
    out["pairs"] = len(pairs)
    for platform, uid in pairs:
        try:
            cpi.unlink(platform, uid)
            out["unlinked"] += 1
        except Exception:
            logger.debug("merge-bridge: unlink %s:%s 失败", platform, uid,
                         exc_info=True)
    if out["unlinked"]:
        try:
            from src.contacts.origin_context import invalidate_origin_cache
            invalidate_origin_cache()
        except Exception:
            pass
    return out


__all__ = [
    "conv_id_to_memory_pair",
    "collect_contact_memory_pairs",
    "merge_contact_memory",
    "unlink_identity_memory",
]
