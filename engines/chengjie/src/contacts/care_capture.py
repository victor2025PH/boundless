"""Phase O4：主动关怀入站捕获接线（gated）。

把 O1 抽取 + O2 入库接到统一收件箱既有的入站新消息回调 `register_new_inbound_cb`
（参数 `cb(conv_dict, text)`）。**默认关**：仅 `companion.proactive_care.enabled`
且 `capture` 为真时才捕获。绝不抛（best-effort，异常不影响 ingest）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def _care_cfg(config_manager: Any) -> Dict[str, Any]:
    try:
        full = getattr(config_manager, "config", None) or {}
        return dict((full.get("companion") or {}).get("proactive_care") or {})
    except Exception:
        return {}


def make_care_inbound_cb(
    store: Any, config_manager: Any, peer_filter: Any = None,
) -> Callable[[Dict[str, Any], str], None]:
    """构造 `cb(conv_dict, text)`：gated 捕获入站约定入 care_schedule。

    `conv_dict` 形如 ingest 提供的 {conversation_id, platform, account_id, chat_key, display_name}。
    contact_key 用稳定的 conversation_id。

    捕获卫生（2026-08-18，垃圾捕获事故沉淀：运维报告长文里的「检查」被抓成
    2027 年的关怀约定，来源=老板与自有账号的互聊）：
    - ``capture_max_chars``（默认 300）：私人约定是短句；结构化报告/广播长文
      整条不进抽取——比在抽取器里加语义判断便宜且零误伤；
    - ``peer_filter``：与派发层同一谓词（``build_peer_filter``，True=对端是
      bot/自家账号）——舰队互聊/测试号不产生「关怀约定」。None=不拦（旧行为）。

    B68（实施67 P2-i，00:12 实锤 2 条被值守现场 cancel）：
    - **群聊一律不捕**：关怀约定是私聊语义（「面试加油」是对人不对群）；报障
      群/工作群消息被捕成「关怀约定」＝对客户莫名其妙、对值守是噪音。
    - **报障会话不捕**：``bug_intake.groups`` 在册的会话（含私聊报障形态）——
      报障文本天然带「明天再看/晚点复测」类时间词，正是误捕温床。
    """
    def _cb(conv_dict: Dict[str, Any], text: str) -> None:
        try:
            cfg = _care_cfg(config_manager)
            if not cfg.get("enabled", False) or not cfg.get("capture", True):
                return
            t = (text or "").strip()
            if not t:
                return
            # B68：群聊一律不捕（ingest conv_dict 自带 chat_type，缺省按 private
            # 放行保持向后兼容——宁可漏拦不误杀正常私聊捕获）
            if str(conv_dict.get("chat_type") or "private") != "private":
                return
            # B68：报障会话不捕（bug_intake.groups 同一事实源；未启用=恒 False）
            try:
                from src.ops.bug_intake import is_bug_group
                _full_cfg = getattr(config_manager, "config", None) or {}
                if is_bug_group(_full_cfg, conv_dict.get("chat_key")):
                    return
            except Exception:
                pass
            try:
                _max_chars = int(cfg.get("capture_max_chars", 300) or 0)
            except (TypeError, ValueError):
                _max_chars = 300
            if _max_chars > 0 and len(t) > _max_chars:
                return  # 报告/广播式长文不当私人约定
            contact_key = str(conv_dict.get("conversation_id") or "")
            if not contact_key:
                return
            if peer_filter is not None:
                try:
                    if peer_filter(
                            str(conv_dict.get("platform") or ""),
                            str(conv_dict.get("account_id") or ""),
                            str(conv_dict.get("chat_key") or "")):
                        return  # 对端是 bot/自家账号：不捕获
                except Exception:
                    pass  # 谓词异常按放行（卫生闸不做新故障源）
            ids = store.add_from_text(
                t,
                contact_key=contact_key,
                platform=str(conv_dict.get("platform") or ""),
                account_id=str(conv_dict.get("account_id") or "default"),
                chat_key=str(conv_dict.get("chat_key") or ""),
                min_confidence=float(cfg.get("min_confidence", 0.6)),
                dedup_window_days=float(cfg.get("dedup_window_days", 3)),
            )
            # 实施84 P0-3：软承诺层（「下次给你看照片/等我忙完」，无时间锚）。
            # 默认关（capture_soft.enabled）；独立置信阈值；per-contact pending
            # 上限防灌（软句式多变、topic_norm 去重弱，上限才是真护栏）。
            soft_cfg = dict(cfg.get("capture_soft") or {})
            if soft_cfg.get("enabled", False) and not ids:
                try:
                    from src.contacts.care_commitment import (
                        extract_soft_commitments,
                    )
                    _max_pending = int(soft_cfg.get(
                        "max_pending_per_contact", 2) or 2)
                    if store.count_pending_by_contact(contact_key) < _max_pending:
                        for c in extract_soft_commitments(
                                t, due_hours=float(
                                    soft_cfg.get("due_hours", 72) or 72)):
                            rid = store.add_commitment(
                                c, contact_key=contact_key,
                                platform=str(conv_dict.get("platform") or ""),
                                account_id=str(
                                    conv_dict.get("account_id") or "default"),
                                chat_key=str(conv_dict.get("chat_key") or ""),
                                min_confidence=float(soft_cfg.get(
                                    "min_confidence", 0.55) or 0.55),
                                dedup_window_days=float(
                                    cfg.get("dedup_window_days", 3)),
                            )
                            if rid:
                                ids.append(rid)
                except Exception:
                    logger.debug("[care] 软承诺捕获失败（忽略）", exc_info=True)
            if ids:
                logger.info("[care] 捕获 %d 条关怀约定 contact=%s", len(ids), contact_key)
                # 实施84 P1-1：捕获的约定回流该会话活跃工作目标的事件时间线
                # （goal_link.enabled + capture_events；坐席在目标卡「AI 做了
                # 什么」可见）。best-effort，绝不影响已完成的捕获。
                try:
                    from src.contacts.care_goal_link import record_capture_event
                    record_capture_event(
                        config_manager,
                        conversation_id=contact_key,
                        platform=str(conv_dict.get("platform") or ""),
                        account_id=str(conv_dict.get("account_id") or "default"),
                        chat_key=str(conv_dict.get("chat_key") or ""),
                        text=t, count=len(ids))
                except Exception:
                    logger.debug("[care] 目标事件回流失败（忽略）", exc_info=True)
        except Exception:
            logger.debug("[care] 入站捕获失败（忽略）", exc_info=True)

    return _cb


__all__ = ["make_care_inbound_cb"]
