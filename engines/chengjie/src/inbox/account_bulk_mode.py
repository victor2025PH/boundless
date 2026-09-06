"""账号级批量切档（M-2 B，D-M1 ② / UE7VM3 ④，2026-09-06）。

「此账号全部会话 → 手动 / 半自动 / 全自动」：全自动**只能**按账号显式开启并确认
「将对 N 个会话开启，覆盖 M 个个别设置」。此前只有逐会话下拉（多客户场景不可操作）
与主管「全部降为草稿审」（全局、只降不升）。

两步：``plan``（干跑：算 N / M / 跳过的群）→ ``apply``（写会话行 + 账号级决策 +
登录门禁确认时刻）。会话行写 ``source=account_bulk``；群/频道在升 auto_ai 时**跳过**
（与 confirm_group 闸、``align_account_conversations`` 同一铁律：群的 auto_ai 只能逐会话
经确认闸写）。降档方向取消该会话待投递 L2（与下拉降档同语义）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.inbox.store import AUTOMATION_MODES

logger = logging.getLogger("ai_chat_assistant.account_bulk_mode")

BULK_SOURCE = "account_bulk"
#: 视为「坐席个别设置」的写入来源（会被覆盖，但要在确认文案里如实计数）
_INDIVIDUAL_SOURCES_PREFIX = ("human", "takeover", "snooze", "rearm")
_PLAN_LIMIT = 5000


def _is_group_row(row: Dict[str, Any]) -> bool:
    try:
        from src.inbox.ingest import is_group_conversation
        return bool(is_group_conversation(row or {}))
    except Exception:
        return str((row or {}).get("chat_type") or "") in ("group", "channel")


def plan_account_bulk(store: Any, platform: str, account_id: str,
                      target_mode: str) -> Dict[str, Any]:
    """干跑：该账号会话总数 / 将改动数 / 覆盖的个别设置数 / 跳过的群数 / 目标 cid 列表。"""
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip() or "default"
    mode = str(target_mode or "").strip().lower()
    out: Dict[str, Any] = {
        "platform": plat, "account_id": acct, "mode": mode,
        "total": 0, "will_change": 0, "override_individual": 0,
        "skipped_groups": 0, "already": 0, "targets": [],
    }
    if store is None or mode not in AUTOMATION_MODES or not plat:
        return out
    try:
        convs = store.list_conversations(limit=_PLAN_LIMIT, platform=plat, account_id=acct) or []
    except TypeError:
        convs = store.list_conversations(limit=_PLAN_LIMIT) or []
        convs = [c for c in convs
                 if str(c.get("platform") or "") == plat
                 and str(c.get("account_id") or "default") == acct]
    except Exception:
        logger.debug("[account_bulk] 会话列表读取失败", exc_info=True)
        return out
    settings: Dict[str, Dict[str, Any]] = {}
    try:
        prefix = f"{plat}:{acct}:"
        for r in (store.list_automation_mode_rows() or []):
            cid = str(r.get("conversation_id") or "")
            if cid.startswith(prefix):
                settings[cid] = r
    except Exception:
        settings = {}
    out["total"] = len(convs)
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        if mode == "auto_ai" and _is_group_row(c):
            out["skipped_groups"] += 1
            continue
        row = settings.get(cid)
        cur = str((row or {}).get("automation_mode") or "")
        if cur == mode:
            out["already"] += 1
            continue
        src = str((row or {}).get("source") or "")
        if row is not None and src.startswith(_INDIVIDUAL_SOURCES_PREFIX):
            out["override_individual"] += 1
        out["targets"].append(cid)
    out["will_change"] = len(out["targets"])
    return out


def apply_account_bulk(store: Any, platform: str, account_id: str,
                       target_mode: str, *, actor: str = "",
                       config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """落地：写会话行（source=account_bulk）+ 账号级决策 + 门禁确认；返回 plan + changed/cancelled_l2。"""
    plan = plan_account_bulk(store, platform, account_id, target_mode)
    mode = plan["mode"]
    res = dict(plan)
    res.update({"changed": 0, "cancelled_l2": 0, "failed": 0})
    if store is None or mode not in AUTOMATION_MODES:
        return res
    downgrade = mode != "auto_ai"
    for cid in plan["targets"]:
        try:
            prev = None
            try:
                prev = store.get_automation_mode_if_set(cid)
            except Exception:
                prev = None
            try:
                store.set_automation_mode(cid, mode, source=BULK_SOURCE)
            except TypeError:
                store.set_automation_mode(cid, mode)
            res["changed"] += 1
            if downgrade and prev == "auto_ai" and hasattr(store, "cancel_pending_l2_drafts"):
                try:
                    res["cancelled_l2"] += int(store.cancel_pending_l2_drafts(
                        cid, decided_by="account_bulk_downgrade") or 0)
                except Exception:
                    pass
        except Exception:
            res["failed"] += 1
            logger.debug("[account_bulk] 写档失败 cid=%s", cid, exc_info=True)
    # 账号层：新会话跟随本次决策（onboarding 决策表 + 登录门禁确认时刻）
    try:
        from src.inbox.account_mode_onboarding import decide_account_mode
        decide_account_mode(plan["platform"], plan["account_id"], mode, actor=actor)
    except Exception:
        logger.debug("[account_bulk] 账号决策落盘失败（忽略）", exc_info=True)
    try:
        from src.inbox.account_channel_gate import confirm_account_mode
        confirm_account_mode(plan["platform"], plan["account_id"], mode, actor=actor)
    except Exception:
        logger.debug("[account_bulk] 门禁确认落盘失败（忽略）", exc_info=True)
    try:
        from src.inbox.account_channel_gate import account_snapshot
        res["gate"] = account_snapshot(plan["platform"], plan["account_id"], config=config)
    except Exception:
        res["gate"] = {}
    logger.info(
        "[account_bulk] %s:%s → %s by=%s：改 %d 个会话（覆盖个别设置 %d，跳过群 %d，"
        "已是 %d），取消待投递 L2 %d 条（D-M1 ②）",
        plan["platform"], plan["account_id"], mode, actor or "?", res["changed"],
        plan["override_individual"], plan["skipped_groups"], plan["already"],
        res["cancelled_l2"])
    res.pop("targets", None)
    return res


def account_mode_summary(store: Any, platform: str, account_id: str) -> Dict[str, int]:
    """账号下各档会话数（账号菜单显示「全自动 N / 半自动 M / 手动 K」）。未显式设置的按 unset 计。"""
    out = {"auto_ai": 0, "review": 0, "multi_choice": 0, "manual": 0, "unset": 0, "total": 0}
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip() or "default"
    if store is None or not plat:
        return out
    try:
        convs = store.list_conversations(limit=_PLAN_LIMIT, platform=plat, account_id=acct) or []
        prefix = f"{plat}:{acct}:"
        modes = {str(r.get("conversation_id") or ""): str(r.get("automation_mode") or "")
                 for r in (store.list_automation_mode_rows() or [])
                 if str(r.get("conversation_id") or "").startswith(prefix)}
    except Exception:
        return out
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        m = modes.get(cid, "")
        out["total"] += 1
        if m in out:
            out[m] += 1
        else:
            out["unset"] += 1
    return out


__all__ = ["BULK_SOURCE", "plan_account_bulk", "apply_account_bulk", "account_mode_summary"]
