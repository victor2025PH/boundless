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

__all__ = [
    "identity_addendum", "persona_display_name", "account_display_name",
    "album_scene_addendum", "sent_media_addendum", "album_miss_addendum",
]

# Q-6 A：相册场景 kind 中文名（与 persona_media.SCENE_KINDS 对齐，这里只做文案）。
_KIND_LABEL_ZH = {
    "selfie": "自拍",
    "indoor": "室内",
    "outdoor": "室外风景",
    "food": "美食",
    "pet": "宠物",
    "other": "其他",
}
_KIND_ORDER = ("selfie", "indoor", "outdoor", "food", "pet", "other")


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


def album_scene_addendum(kind_counts: Any, *, lang: str = "zh") -> str:
    """相册可展示场景清单。``kind_counts`` 空 / 全 0 → ""（F：为空不出现）。

    调用方负责从相册行聚合 ``{kind: n}``；本函数纯格式化、零 I/O。
    """
    counts: dict = {}
    if isinstance(kind_counts, dict):
        for k, v in kind_counts.items():
            try:
                n = int(v or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0 and str(k) in _KIND_LABEL_ZH:
                counts[str(k)] = n
    if not counts:
        return ""
    lg = str(lang or "zh").lower()
    parts = []
    for k in _KIND_ORDER:
        if k not in counts:
            continue
        label = _KIND_LABEL_ZH[k]
        if lg.startswith("en"):
            label = k
        parts.append(f"{label} {counts[k]}")
    joined = "、".join(parts) if not lg.startswith("en") else ", ".join(parts)
    if lg.startswith("en"):
        return (
            f"【album scenes】Photos you can actually show: {joined}. "
            "Only promise scenes on this list. If it isn't here, refuse — "
            "never say you sent it or that it's loading."
        )
    return (
        f"【相册可展示场景】你现在能拿出的照片只有：{joined}。"
        "只能承诺清单内的场景；没有的必须拒绝，不许说已经发了或加载中。"
    )


def sent_media_addendum(items: Any, *, conv_known: bool = False, lang: str = "zh") -> str:
    """本会话已发媒体清单。``conv_known=False`` → ""；已知会话但 items 空 → 写死从未发过。"""
    if not conv_known:
        return ""
    lg = str(lang or "zh").lower()
    rows = [x for x in (items or []) if isinstance(x, dict)] if items else []
    if not rows:
        if lg.startswith("en"):
            return "【sent media】You have never sent TA a photo in this chat."
        return "【本会话已发媒体】你从未给 TA 发过照片。"
    bits = []
    for it in rows[:12]:
        mid = str(it.get("id") or it.get("mid") or "").strip() or "-"
        scene = str(it.get("scene") or it.get("kind") or "").strip() or "?"
        when = str(it.get("when") or it.get("ts_label") or "").strip()
        bits.append(f"{mid}/{scene}" + (f"@{when}" if when else ""))
    body = "；".join(bits)
    if lg.startswith("en"):
        return (
            f"【sent media】Photos already sent in this chat (last 7 days): {body}. "
            "Don't claim you sent anything that isn't on this list."
        )
    return (
        f"【本会话已发媒体】近 7 天已发给 TA 的：{body}。"
        "清单以外的不得声称发过。"
    )


def album_miss_addendum(scene: Any, *, lang: str = "zh") -> str:
    """无匹配回喂。scene 空仍给一句（索图但完全没图）；调用方决定是否调用。"""
    sc = str(scene or "").strip() or "照片"
    lg = str(lang or "zh").lower()
    if lg.startswith("en"):
        return (
            f"【no album photo (scene {sc})】There is no matching photo. "
            "Only a natural refusal is allowed. Forbidden: already sent / still loading "
            "/ try again / forgot / I'll send it later."
        )
    return (
        f"【无可用照片（场景 {sc}）】相册里没有能展示这个场景的图。"
        "只许自然拒绝（没有合适的/现在不方便）。"
        "禁止说已经发了、加载中、再试、忘了、稍后发。"
    )
