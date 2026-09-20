"""GuardContext（Q-23 #303，2026-09-12）：入站守卫的**场景维度**。

事故：报障群（Telegram 负 peer、``mode=review``）里我方值守的一句「给我看你的日志」被成人词表命中，
Q-15 软回应经「坐席人工通过」直投链发进群 4 条（mid 1445 1454 1459 1461）。根因之一是三处守卫
（``adult_grader`` / ``risk_grader`` / ``drafts.keyword_risk_hits``）都只看文本，不知道**这是群、
说话的是同事、会话是人审档**。

本模块只回答三个问题，不做任何出站 / 打标：

- ``is_group``：群 / 频道 / 报障群 / 运维群 → **不评估、不出站、不打标、不持有**；
- ``sender_kind``：``customer`` 才评估；``agent``（同事账号 / 值守 bot）/ ``self``（我方出站）/
  ``peer_bot``（对端机器人）一律不评估；
- ``mode``：``manual`` / ``review`` / ``multi_choice`` 档任何**自动**出站只能是「候选进审核稿」。

守卫签名全部是 ``ctx: Optional[GuardContext] = None``——不传 = 逐字旧行为（私聊全自动的真高风险
行为一字不变）。上下文由 ``drafts.auto_generate_draft`` 风险入口 / ``protocol_autoreply`` 钩子
各**构造一次**（``build``）再传给三处守卫。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("ai_chat_assistant.guard_context")

SENDER_KINDS = ("customer", "agent", "self", "peer_bot")
#: 人在环的档位：任何自动出站改成审核稿候选
REVIEW_MODES = ("manual", "review", "multi_choice")


@dataclass(frozen=True)
class GuardContext:
    is_group: bool = False
    mode: str = "auto_ai"
    sender_kind: str = "customer"
    lang: str = ""
    chat_id: str = ""
    platform: str = ""
    account_id: str = ""
    conversation_id: str = ""
    #: is_group / sender_kind 的判定依据（日志用）：group / bug_group / ops_group / colleague:<id> …
    reason: str = ""

    @property
    def evaluable(self) -> bool:
        """三处守卫要不要跑：私聊 × 客户发送方。"""
        return (not self.is_group) and self.sender_kind == "customer"

    @property
    def in_review_mode(self) -> bool:
        return str(self.mode or "") in REVIEW_MODES

    @property
    def auto_outbound_allowed(self) -> bool:
        """自动出站（软回应等）可否直接发：可评估 × 全自动档。"""
        return self.evaluable and str(self.mode or "") == "auto_ai"

    def skip_reason(self) -> str:
        """``""`` = 放行评估；否则 ``group`` / ``sender:<kind>``。"""
        if self.is_group:
            return "group"
        if self.sender_kind != "customer":
            return f"sender:{self.sender_kind}"
        return ""

    def log_tag(self) -> str:
        return (f"group={int(self.is_group)} sender={self.sender_kind} mode={self.mode or '-'}"
                + (f" why={self.reason}" if self.reason else ""))


def _chat_id(conv: Dict[str, Any]) -> str:
    ck = str(conv.get("chat_key") or "").strip()
    if ck:
        return ck.split(":")[-1].strip()
    cid = str(conv.get("conversation_id") or "")
    return cid.split(":")[-1].strip() if cid else ""


def _split_cid(conv: Dict[str, Any]) -> Dict[str, str]:
    cid = str(conv.get("conversation_id") or "")
    platform = str(conv.get("platform") or "")
    account_id = str(conv.get("account_id") or "")
    chat_key = str(conv.get("chat_key") or "")
    if cid and (not platform or not chat_key):
        parts = cid.split(":", 2)
        if len(parts) == 3:
            platform = platform or parts[0]
            account_id = account_id or parts[1]
            chat_key = chat_key or parts[2]
    return {"conversation_id": cid, "platform": platform,
            "account_id": account_id or "default", "chat_key": chat_key}


def _group_reason(conv: Dict[str, Any], cfg: Any, chat_id: str) -> str:
    try:
        from src.inbox.ingest import is_group_conversation
        if is_group_conversation(conv):
            return "group"
    except Exception:
        pass
    if chat_id:
        try:
            from src.ops.bug_intake import is_bug_group
            if is_bug_group(cfg, chat_id):
                return "bug_group"
        except Exception:
            pass
        try:
            from src.inbox import peer_bot_guard as _pbg
            fn = getattr(_pbg, "never_auto_reply_reason", None)
            if fn is not None:
                r = str(fn(cfg, platform=str(conv.get("platform") or ""),
                           account_id=str(conv.get("account_id") or ""),
                           chat_key=chat_id) or "")
                if r.startswith("ops_group"):
                    return r
        except Exception:
            pass
    return ""


def _sender_kind(conv: Dict[str, Any], cfg: Any, *, sender_id: str, direction: str,
                 is_group: bool) -> "tuple[str, str]":
    if str(direction or "in") == "out":
        return "self", "direction:out"
    account_id = str(conv.get("account_id") or "")
    sid = str(sender_id or "").strip()
    # 私聊：发送方就是对端 chat_key；群里拿不到 sender_id 时按客户处理（群本身已不评估）
    if not sid and not is_group:
        sid = _chat_id(conv)
    if sid and account_id and sid == account_id:
        return "self", "sender:self"
    if sid:
        try:
            from src.inbox import peer_bot_guard as _pbg
            fn = getattr(_pbg, "never_auto_reply_reason", None)
            if fn is not None:
                r = str(fn(cfg, platform=str(conv.get("platform") or ""),
                           account_id=account_id, chat_key="", sender_id=sid) or "")
                if r.startswith("colleague"):
                    return "agent", r
        except Exception:
            pass
        try:
            from src.inbox.peer_bot_guard import TELEGRAM_KNOWN_SERVICE_BOT_IDS
            if str(conv.get("platform") or "").lower().startswith("telegram") \
                    and sid in TELEGRAM_KNOWN_SERVICE_BOT_IDS:
                return "peer_bot", "service_bot"
        except Exception:
            pass
    ct = str(conv.get("chat_type") or "").strip().lower()
    if ct == "bot":
        return "peer_bot", "chat_type:bot"
    try:
        if int(conv.get("peer_is_bot") or 0) == 1:
            return "peer_bot", "peer_is_bot"
    except (TypeError, ValueError):
        pass
    return "customer", ""


def build(conv: Optional[Dict[str, Any]], *, automation_mode: str = "", lang: str = "",
          cfg: Any = None, store: Any = None, sender_id: str = "",
          direction: str = "in") -> GuardContext:
    """构造一次、三处守卫共用。任何异常 → 「私聊 × 客户 × 给定档位」的中性上下文（不改旧行为）。

    ``automation_mode`` 缺省时按 ``automation_mode.resolve_automation_mode``（显式设置 > 全局缺省，
    与拟稿 / A 线同一解析口径）；解析不了 → ``review``（fail-closed：不知道档位就不许自动出站）。
    """
    base = _split_cid(conv or {})
    mode = str(automation_mode or "").strip().lower()
    try:
        c = dict(conv or {})
        c.update({k: v for k, v in base.items() if v})
        chat_id = _chat_id(c)
        if not mode and store is not None and base["conversation_id"]:
            try:
                from src.inbox.automation_mode import resolve_automation_mode
                mode = str(resolve_automation_mode(store, base["conversation_id"], cfg or None) or "") \
                    .strip().lower()
            except Exception:
                mode = ""
        if mode not in ("manual", "review", "multi_choice", "auto_ai"):
            mode = "review"
        g_reason = _group_reason(c, cfg, chat_id)
        is_group = bool(g_reason)
        kind, s_reason = _sender_kind(c, cfg, sender_id=sender_id, direction=direction,
                                      is_group=is_group)
        why = ";".join(x for x in (g_reason, s_reason) if x)
        return GuardContext(is_group=is_group, mode=mode, sender_kind=kind, lang=str(lang or ""),
                            chat_id=chat_id, platform=base["platform"], account_id=base["account_id"],
                            conversation_id=base["conversation_id"], reason=why)
    except Exception:
        logger.debug("[guard-ctx] build 异常（按私聊客户处理）", exc_info=True)
        return GuardContext(mode=mode or "review", lang=str(lang or ""),
                            platform=base["platform"], account_id=base["account_id"],
                            conversation_id=base["conversation_id"], chat_id=_chat_id(base))


def from_row(row: Optional[Dict[str, Any]], *, cfg: Any = None, store: Any = None) -> GuardContext:
    """投递载荷（软回应 row / 草稿行）→ ctx：优先取载荷里已构造好的 ``guard_ctx``。"""
    row = row or {}
    ctx = row.get("guard_ctx")
    if isinstance(ctx, GuardContext):
        return ctx
    if isinstance(ctx, dict):
        try:
            return GuardContext(**{k: v for k, v in ctx.items()
                                   if k in GuardContext.__dataclass_fields__})
        except Exception:
            pass
    return build(row, automation_mode=str(row.get("automation_mode") or ""),
                 lang=str(row.get("lang") or ""), cfg=cfg, store=store)


__all__ = ["GuardContext", "SENDER_KINDS", "REVIEW_MODES", "build", "from_row"]
