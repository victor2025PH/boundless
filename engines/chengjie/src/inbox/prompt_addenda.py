# -*- coding: utf-8 -*-
"""生成链 prompt 附加段（Q-1 F · #269 · 2026-09-10）。

事故（#269 8FJDUK / TDNJHQ）：账号昵称 Vanessa、人设名 Mizuki——客户按头像旁的名字喊「Vanessa」，
AI 按人设答「我不是 Vanessa，我是 Mizuki」→ 当场穿帮。O-1 E 做了配置页比对提醒
（``utils.account_name_check``），但坐席不改名 / 平台改不了名（LINE / Messenger 官方无写口）时，
生成链得**自己知道这个昵称**：对方叫 X 就是在叫你。

本模块只出**纯函数**、零 I/O；由 Q-6 在 ``persona_reply`` 组 system prompt 时接线
（``identity_addendum(persona, account)`` 非空即追加）。本线**不动** persona_reply。
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["identity_addendum", "persona_display_name", "account_display_name"]


def _get(obj: Any, *keys: str) -> str:
    """dict / 对象属性 里取第一个非空字符串字段。"""
    for k in keys:
        v = None
        if isinstance(obj, dict):
            v = obj.get(k)
        else:
            v = getattr(obj, k, None)
        if isinstance(v, dict):
            continue
        s = str(v or "").strip()
        if s:
            return s
    return ""


def persona_display_name(persona: Any) -> str:
    """人设名：``persona.name`` / ``display_name`` / ``profile.name``；裸字符串即名字。"""
    if persona is None:
        return ""
    if isinstance(persona, str):
        return persona.strip()
    n = _get(persona, "name", "display_name", "persona_name")
    if n:
        return n
    prof = persona.get("profile") if isinstance(persona, dict) else getattr(persona, "profile", None)
    return _get(prof, "name", "display_name") if prof is not None else ""


def account_display_name(account: Any) -> str:
    """账号在平台上的显示名：``meta.self_name``（account_registry 富集）/ ``self_name`` /
    ``display_name`` / ``name`` / ``nickname``；裸字符串即名字。"""
    if account is None:
        return ""
    if isinstance(account, str):
        return account.strip()
    meta = account.get("meta") if isinstance(account, dict) else getattr(account, "meta", None)
    n = _get(meta, "self_name", "display_name", "name", "nickname") if meta is not None else ""
    if n:
        return n
    return _get(account, "self_name", "display_name", "name", "nickname", "account_name")


def identity_addendum(persona: Any, account: Any, *, lang: str = "zh") -> str:
    """账号显示名 ≠ 人设名 → 一句身份附加段；一致 / 任一未知 / 判不出 → ""。

    一致性用 ``utils.account_name_check.names_consistent``（昵称含人设名算一致：「Mizuki 🌸」是
    Mizuki）。绝不抛。"""
    try:
        p = persona_display_name(persona)
        a = account_display_name(account)
        if not p or not a:
            return ""
        from src.utils.account_name_check import names_consistent
        ok: Optional[bool] = names_consistent(a, p)
        if ok is None or ok:
            return ""
        lg = str(lang or "zh").lower()
        if lg.startswith("en"):
            return (f"The other person may call you \"{a}\" — that is your display name on this "
                    f"platform. Never deny it and don't explain it; just answer as yourself.")
        if lg.startswith("ja"):
            return (f"相手はあなたを「{a}」と呼ぶかもしれない。それはこのプラットフォームでの"
                    f"あなたの表示名。否定せず、説明もせず、そのまま自分として答える。")
        return f"对方可能用「{a}」称呼你，那是你在这个平台的昵称，不得否认、不必解释，照常以自己的身份回应。"
    except Exception:
        return ""
