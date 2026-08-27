# -*- coding: utf-8 -*-
"""全局默认人设的「生效范围」推导（实施49 P0-3，内测反馈 B3 根治）。

B3 实录：客户在「默认人设」页改完名字/性格，保存成功提示也弹了，聊天里的
AI 却一点没变——因为**账号级绑定优先于域级默认人设**，而
``platform_login.default_persona_id`` 会在账号上线时自动补一条绑定
（``ensure_account_default_persona``），于是真实部署里「全局默认」常常
一个账号都覆盖不到。保存成功 ≠ 生效，而这个差别此前对客户完全不可见
（页面文案还写着「都使用这套人设」）。

本模块只回答一个问题：**这份全局默认，现在到底管着谁、被谁盖住**。
优先级口径与 ``persona_voice.resolve_effective_persona`` /
``PersonaManager.get_persona_with_tier`` 完全一致：
会话覆写 > 账号绑定 > legacy 会话绑定 > 域默认 > 内置兜底。

纯推导（``summarize_default_persona_scope``）与真实取数
（``collect_default_persona_scope``）分开：前者可单测，后者 fail-open
——任何一环取不到都退化成「说不出覆盖情况」而不是让保存链路报错。
"""

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

__all__ = [
    "summarize_default_persona_scope",
    "collect_default_persona_scope",
]

_SAMPLE_CAP = 6


def summarize_default_persona_scope(
    accounts: Iterable[Mapping[str, Any]],
    *,
    conv_overrides: int = 0,
    chat_bindings: int = 0,
    conv_override_enabled: bool = False,
    default_persona_id: str = "",
    profile_name: Optional[Callable[[str], str]] = None,
    sample_cap: int = _SAMPLE_CAP,
) -> Dict[str, Any]:
    """按注册表账号行推导域级默认人设的覆盖面。

    ``accounts``：``AccountRegistry.list()`` 形态的行（platform / account_id /
    label / meta），已排除 removed。账号 meta 里有任何 persona 绑定 → 该账号
    **不跟随**全局默认（``persona_id_auto=True`` 的自动绑定同样压制域默认——
    解析链并不区分它是谁写的，所以这里也不许把它算成「跟随」，否则读数会
    比现实乐观，正是 B3 那种「看着生效其实没有」）。
    """
    from src.integrations.account_registry import parse_persona_ids

    name_of = profile_name or (lambda pid: "")
    total = 0
    following = 0
    overrides: List[Dict[str, Any]] = []
    by_profile: Dict[str, int] = {}

    for row in accounts or []:
        total += 1
        meta = row.get("meta") or {}
        pids = parse_persona_ids(meta)
        if not pids:
            following += 1
            continue
        pid = pids[0]
        by_profile[pid] = by_profile.get(pid, 0) + 1
        if len(overrides) < max(0, int(sample_cap)):
            overrides.append({
                "platform": str(row.get("platform") or ""),
                "account_id": str(row.get("account_id") or ""),
                "label": str(row.get("label") or ""),
                "profile_id": pid,
                "profile_name": str(name_of(pid) or ""),
                # 自动补绑（上线时按 default_persona_id 写入）vs 人工显式绑定：
                # 客户看到「我没绑过啊」时，这一位是唯一能解释清楚的信息。
                "auto": bool(meta.get("persona_id_auto") is True),
            })

    overridden = total - following
    profiles = [
        {"profile_id": pid, "name": str(name_of(pid) or ""), "count": n}
        for pid, n in sorted(by_profile.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    convs = int(conv_overrides) if conv_override_enabled else 0
    return {
        "level": "domain",
        "accounts_total": total,
        "accounts_following": following,
        "accounts_overridden": overridden,
        "overrides": overrides,
        "override_profiles": profiles,
        "conv_override_enabled": bool(conv_override_enabled),
        "conv_overrides": convs,
        "chat_bindings": int(chat_bindings or 0),
        "auto_bind_profile_id": str(default_persona_id or ""),
        "auto_bind_profile_name": str(name_of(default_persona_id) or "")
        if default_persona_id else "",
        # 三态判词交给前端，但「一个账号都管不到」这句结论在这里定——
        # 两个消费面（保存后 toast / 打开页面时的常驻条）必须同口径。
        "reaches_nobody": bool(total > 0 and following == 0),
        "fully_effective": bool(
            total > 0 and overridden == 0 and convs == 0
            and int(chat_bindings or 0) == 0
        ),
        "available": True,
    }


def _cfg_get(config: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = config or {}
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def collect_default_persona_scope(
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """真实取数（注册表 + PersonaManager + 配置）。

    fail-open：注册表未初始化 / PM 不可用 / 配置缺键 → 返回
    ``{"available": False}``，调用方据此**不显示**覆盖提示（宁可少说一句，
    也不能对着残缺数据宣布「全局已全量生效」——那正是 B3 的错觉本身）。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        from src.utils.persona_manager import PersonaManager

        rows = get_account_registry().list()
        pm = PersonaManager.get_instance()

        conv_n = 0
        legacy_n = 0
        conv_on = False
        try:
            from src.ai.persona_voice import (
                conv_override_enabled, is_conv_binding_key,
            )
            conv_on = conv_override_enabled(dict(config or {}))
            keys = set(getattr(pm, "_chat_bindings", {}) or {})
            keys |= set(getattr(pm, "_chat_personas", {}) or {})
            for k in keys:
                if is_conv_binding_key(str(k)):
                    conv_n += 1
                else:
                    legacy_n += 1
        except Exception:
            pass

        def _name(pid: str) -> str:
            try:
                p = pm.get_persona_by_id(str(pid or "")) or {}
                return str(p.get("name") or "")
            except Exception:
                return ""

        cfg = config or {}
        default_pid = (
            _cfg_get(cfg, "platform_login.default_persona_id", "")
            or _cfg_get(cfg, "accounts.default_persona_id", "")
        )
        return summarize_default_persona_scope(
            rows,
            conv_overrides=conv_n,
            chat_bindings=legacy_n,
            conv_override_enabled=conv_on,
            default_persona_id=str(default_pid or ""),
            profile_name=_name,
        )
    except Exception:
        return {"available": False}
