"""Q-8 A（#264 #263）：账号级 / 人设级**默认目标**——新会话来消息自动挂 + 存量批量挂。

事故（B9D8NW / 7C7SV2 / R6YW5D）：7 条 profile_discovery 目标全是手建，Enrique 这类新客没人
建目标 → ``goal-inject NOT injected reason=no_goal``，AI 四轮纯夸赞后自己「get back to work」
退场。默认目标把「有没有目标」从逐会话手工变成账号 / 人设一次设定：

- 落点：InboxStore ``app_settings`` KV（goals 库 ``store.py`` 默认不碰，与 Q-1 E 的暂停开关同仓）：
  ``goals:default:acct:<platform>:<account_id>`` / ``goals:default:persona:<persona_id>``，
  值 = JSON ``{template, params, autonomy, days, enabled, by, ts}``。账号级优先于人设级。
- 新会话：``maybe_attach_default_goal`` 在注入链 ``goal is None`` 且有真实入站时调用（与
  auto_create 同位置、同护栏：群聊 / 自聊 me / 停联冻结 / 需人工 / 幂等 / 每日预算）；摸底类模板
  受 Q-1 E ``discovery_paused`` 约束（暂停中不挂）。日志 ``[goal-default] account=… attached=1``。
- 存量：``attach_existing`` 先 ``dry_run`` 预览 N 个会话（默认**不勾**已停联 / 自聊 / 需人工 /
  群聊 / 已有目标），再按勾选批量建；日志 ``[goal-default] account=… attached=n skipped=m``。

纯 KV + 既有 store API，绝不抛。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("src.companion.goals.defaults")

KV_PREFIX = "goals:default:"
CREATED_BY_ACCOUNT = "account_default"
CREATED_BY_PERSONA = "persona_default"
#: 每账号每日自动挂上限（跨重启稳：按 created_by 进库计数）
MAX_ATTACH_PER_DAY = 60
#: 存量预览默认上限
PREVIEW_LIMIT = 200

EXCLUDE_FROZEN = "frozen"
EXCLUDE_SELF = "self_chat"
EXCLUDE_HUMAN = "needs_human"
EXCLUDE_GROUP = "group"
EXCLUDE_HAS_GOAL = "has_goal"
#: 预览里**默认不勾**的排除码（用户可手动勾回 has_goal 以外的三类）
DEFAULT_UNCHECKED = (EXCLUDE_FROZEN, EXCLUDE_SELF, EXCLUDE_HUMAN, EXCLUDE_GROUP, EXCLUDE_HAS_GOAL)


def account_key(platform: str, account_id: str) -> str:
    return f"{KV_PREFIX}acct:{str(platform or '').strip().lower()}:{str(account_id or '').strip()}"


def persona_key(persona_id: str) -> str:
    return f"{KV_PREFIX}persona:{str(persona_id or '').strip()}"


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(str(raw or "") or "{}")
    except Exception:
        return None
    if not isinstance(d, dict) or not str(d.get("template") or "").strip():
        return None
    return d


def normalize_spec(spec: Any) -> Optional[Dict[str, Any]]:
    """表单 → 落库形状；模板未知 / 空 → None。``params`` 只收 dict。"""
    from src.companion.goals.templates import AUTONOMY_LEVELS, get_template
    if not isinstance(spec, dict):
        return None
    tid = str(spec.get("template") or "").strip()
    tmpl = get_template(tid)
    if tmpl is None:
        return None
    auto = str(spec.get("autonomy") or "auto").strip().lower()
    if auto not in AUTONOMY_LEVELS:
        auto = "auto"
    try:
        days = float(spec.get("days") or tmpl.get("default_days") or 0)
    except (TypeError, ValueError):
        days = float(tmpl.get("default_days") or 0)
    params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    return {
        "template": tid,
        "params": {str(k): v for k, v in params.items()},
        "autonomy": auto,
        "days": max(0.0, days),
        "title": str(spec.get("title") or "").strip()[:120],
        "enabled": bool(spec.get("enabled", True)),
    }


def get_default(inbox_store: Any, *, platform: str = "", account_id: str = "",
                persona_id: str = "") -> Optional[Dict[str, Any]]:
    """账号级优先，其次人设级；关着（enabled=false）视为无。附 ``scope`` / ``scope_key``。"""
    if inbox_store is None or not hasattr(inbox_store, "get_app_setting"):
        return None
    cands: List[tuple] = []
    if platform and account_id:
        cands.append(("account", account_key(platform, account_id)))
    if persona_id:
        cands.append(("persona", persona_key(persona_id)))
    for scope, key in cands:
        try:
            d = _parse(inbox_store.get_app_setting(key, ""))
        except Exception:
            d = None
        if d and d.get("enabled", True):
            out = dict(d)
            out["scope"] = scope
            out["scope_key"] = key
            return out
    return None


def set_default(inbox_store: Any, key: str, spec: Optional[Dict[str, Any]], *, by: str = "") -> bool:
    """写 / 清（spec=None 或 enabled=False 且 clear → 删键）。"""
    if inbox_store is None or not hasattr(inbox_store, "set_app_setting") or not key.startswith(KV_PREFIX):
        return False
    try:
        if spec is None:
            ok = bool(inbox_store.set_app_setting(key, "", updated_by=by or "goal_routes"))
            logger.info("[goal-default] cleared key=%s by=%s", key, by or "-")
            return ok
        rec = dict(spec)
        rec["by"] = str(by or "")
        rec["ts"] = time.time()
        ok = bool(inbox_store.set_app_setting(
            key, json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
            updated_by=by or "goal_routes"))
        logger.info("[goal-default] set key=%s template=%s autonomy=%s days=%s by=%s",
                    key, rec.get("template"), rec.get("autonomy"), rec.get("days"), by or "-")
        return ok
    except Exception:
        logger.debug("set_default failed", exc_info=True)
        return False


def list_defaults(inbox_store: Any) -> List[Dict[str, Any]]:
    if inbox_store is None or not hasattr(inbox_store, "list_app_settings"):
        return []
    out: List[Dict[str, Any]] = []
    try:
        rows = inbox_store.list_app_settings(KV_PREFIX) or []
    except Exception:
        return []
    for r in rows:
        key = str((r or {}).get("key") or "")
        d = _parse((r or {}).get("value"))
        if not d:
            continue
        rest = key[len(KV_PREFIX):]
        if rest.startswith("acct:"):
            _p, _, tail = rest.partition(":")
            plat, _, acct = tail.partition(":")
            d.update(scope="account", platform=plat, account_id=acct)
        elif rest.startswith("persona:"):
            d.update(scope="persona", persona_id=rest.split(":", 1)[1])
        else:
            continue
        d["scope_key"] = key
        out.append(d)
    return out


def _conv_persona_id(user_context: Optional[Dict[str, Any]], chat_key: str) -> str:
    uc = user_context or {}
    try:
        from src.utils.persona_manager import PersonaManager
        persona, _tier = PersonaManager.get_instance().get_persona_with_tier(
            str(uc.get("chat_id") or chat_key or ""),
            str(uc.get("account_persona_id") or ""),
        )
        return str((persona or {}).get("id") or "").strip()
    except Exception:
        return ""


def _exclusion(inbox_store: Any, store: Any, *, conversation_id: str, platform: str,
               chat_key: str, chat_type: str = "") -> str:
    """该会话为什么不该挂：frozen / self_chat / needs_human / group / has_goal / ""。"""
    if str(chat_key or "").strip().lower() == "me":
        return EXCLUDE_SELF
    if str(chat_type or "").lower() == "group" or ":group:" in str(conversation_id or ""):
        return EXCLUDE_GROUP
    try:
        if store.has_any_goal(conversation_id=conversation_id, platform=platform,
                              chat_key=str(chat_key or "")):
            return EXCLUDE_HAS_GOAL
    except Exception:
        pass
    if inbox_store is not None:
        try:
            from src.inbox.stop_contact import frozen_reason
            if frozen_reason(inbox_store, conversation_id):
                return EXCLUDE_FROZEN
        except Exception:
            pass
        try:
            from src.integrations.protocol_autoreply import HANDOFF_TAG
            tags = [str(t) for t in (inbox_store.get_conv_tags(conversation_id) or [])]
            if HANDOFF_TAG in tags:
                return EXCLUDE_HUMAN
        except Exception:
            pass
    return ""


def _day_start(now: float) -> float:
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"))


def _create(store: Any, spec: Dict[str, Any], *, conversation_id: str, platform: str,
            account_id: str, chat_key: str, created_by: str, now: float) -> Optional[Dict[str, Any]]:
    from src.companion.goals.templates import get_template
    tmpl = get_template(str(spec.get("template") or "")) or {}
    days = float(spec.get("days") or tmpl.get("default_days") or 0)
    return store.create_goal(
        conversation_id=conversation_id, platform=platform, account_id=account_id,
        chat_key=chat_key, template=str(spec.get("template") or ""),
        title=str(spec.get("title") or ""), params=dict(spec.get("params") or {}),
        autonomy=str(spec.get("autonomy") or "auto"), priority=1,
        deadline_days=days, created_by=created_by, now=now)


def maybe_attach_default_goal(
    store: Any, cfg_root: Any, *, platform: str, chat_key: str, account_id: str = "",
    conversation_id: str = "", user_context: Optional[Dict[str, Any]] = None,
    inbox_store: Any = None, now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """新会话首条真实入站 → 按账号 / 人设默认目标自动建一条。护栏全过才建；失败 → None，绝不抛。"""
    try:
        uc = user_context or {}
        if uc.get("is_group"):
            return None
        conv_id = str(conversation_id or "").strip()
        if not conv_id and platform and account_id and chat_key:
            conv_id = f"{platform}:{account_id}:{chat_key}"
        pid = _conv_persona_id(uc, chat_key)
        spec = get_default(inbox_store, platform=platform, account_id=account_id, persona_id=pid)
        if not spec:
            return None
        from src.companion.goals.templates import get_template
        tmpl = get_template(str(spec.get("template") or ""))
        if tmpl is None:
            return None
        if tmpl.get("profile_slots"):
            from src.companion.goals.service import discovery_paused
            if discovery_paused(inbox_store):
                logger.info("[goal-default] account=%s:%s conv=%s attached=0 reason=discovery_paused",
                            platform, account_id or "-", conv_id)
                return None
        why = _exclusion(inbox_store, store, conversation_id=conv_id, platform=platform,
                         chat_key=chat_key)
        if why:
            if why != EXCLUDE_HAS_GOAL:
                logger.info("[goal-default] account=%s:%s conv=%s attached=0 reason=%s",
                            platform, account_id or "-", conv_id, why)
            return None
        n = float(now if now is not None else time.time())
        created_by = CREATED_BY_ACCOUNT if spec.get("scope") == "account" else CREATED_BY_PERSONA
        if store.count_created_by_since(created_by, _day_start(n)) >= MAX_ATTACH_PER_DAY:
            logger.info("[goal-default] account=%s:%s conv=%s attached=0 reason=daily_cap",
                        platform, account_id or "-", conv_id)
            return None
        goal = _create(store, spec, conversation_id=conv_id, platform=platform,
                       account_id=account_id, chat_key=chat_key, created_by=created_by, now=n)
        if goal is not None:
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_created()
                get_goal_stats().record_auto_created()
            except Exception:
                pass
            logger.info("[goal-default] account=%s:%s conv=%s attached=1 goal=%s template=%s scope=%s",
                        platform, account_id or "-", conv_id, str(goal.get("goal_id") or "")[:12],
                        spec.get("template"), spec.get("scope"))
        return goal
    except Exception:
        logger.debug("maybe_attach_default_goal failed", exc_info=True)
        return None


def preview_existing(
    store: Any, inbox_store: Any, *, platform: str, account_id: str, limit: int = PREVIEW_LIMIT,
) -> List[Dict[str, Any]]:
    """存量会话预览：``[{conversation_id, chat_key, name, exclude, checked}]``（exclude 空＝默认勾选）。"""
    out: List[Dict[str, Any]] = []
    if inbox_store is None or not hasattr(inbox_store, "list_conversations"):
        return out
    try:
        rows = inbox_store.list_conversations(limit=max(1, int(limit or PREVIEW_LIMIT)),
                                              platform=platform, account_id=account_id) or []
    except Exception:
        return out
    for c in rows:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("conversation_id") or "").strip()
        if not cid:
            continue
        ck = str(c.get("chat_key") or cid.split(":", 2)[-1])
        why = _exclusion(inbox_store, store, conversation_id=cid, platform=platform,
                         chat_key=ck, chat_type=str(c.get("chat_type") or ""))
        out.append({
            "conversation_id": cid, "chat_key": ck,
            "name": str(c.get("display_name") or c.get("name") or c.get("peer_name") or ck)[:60],
            "exclude": why, "checked": not why,
        })
    return out


def attach_existing(
    store: Any, inbox_store: Any, *, platform: str, account_id: str, spec: Dict[str, Any],
    conversation_ids: Optional[List[str]] = None, now: Optional[float] = None,
) -> Dict[str, Any]:
    """按勾选批量挂。``conversation_ids`` 缺省＝预览里默认勾选的全部。已有目标的会话永不重复建。
    返回 ``{attached, skipped, goals[], skipped_reasons{}}``。"""
    n = float(now if now is not None else time.time())
    prev = preview_existing(store, inbox_store, platform=platform, account_id=account_id)
    by_id = {p["conversation_id"]: p for p in prev}
    want = [str(x) for x in (conversation_ids if conversation_ids is not None
                             else [p["conversation_id"] for p in prev if p["checked"]])]
    attached: List[str] = []
    skipped: Dict[str, str] = {}
    created_by = CREATED_BY_ACCOUNT
    for cid in want:
        p = by_id.get(cid)
        if p is None:
            skipped[cid] = "unknown"
            continue
        if p["exclude"] == EXCLUDE_HAS_GOAL:
            skipped[cid] = EXCLUDE_HAS_GOAL
            continue
        g = _create(store, spec, conversation_id=cid, platform=platform, account_id=account_id,
                    chat_key=p["chat_key"], created_by=created_by, now=n)
        if g is None:
            skipped[cid] = "create_failed"
            continue
        attached.append(str(g.get("goal_id") or ""))
    logger.info("[goal-default] account=%s:%s attached=%d skipped=%d template=%s mode=batch",
                platform, account_id or "-", len(attached), len(skipped), spec.get("template"))
    return {"attached": len(attached), "skipped": len(skipped), "goals": attached,
            "skipped_reasons": skipped}


__all__ = [
    "CREATED_BY_ACCOUNT", "CREATED_BY_PERSONA", "DEFAULT_UNCHECKED", "KV_PREFIX",
    "MAX_ATTACH_PER_DAY", "account_key", "attach_existing", "get_default", "list_defaults",
    "maybe_attach_default_goal", "normalize_spec", "persona_key", "preview_existing", "set_default",
]
