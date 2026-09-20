# -*- coding: utf-8 -*-
"""生成链 prompt 附加段（Q-1 F · #269 · 2026-09-10）。

事故（#269 8FJDUK / TDNJHQ）：账号昵称 Vanessa、人设名 Mizuki——客户按头像旁的名字喊「Vanessa」，
AI 按人设答「我不是 Vanessa，我是 Mizuki」→ 当场穿帮。O-1 E 做了配置页比对提醒
（``utils.account_name_check``），但坐席不改名 / 平台改不了名（LINE / Messenger 官方无写口）时，
生成链得**自己知道这个昵称**：对方叫 X 就是在叫你。

本模块只出**纯函数**、零 I/O；由 Q-6 在 ``persona_reply`` 组 system prompt 时接线
（``identity_addendum(persona, account)`` 非空即追加）。本线**不动** persona_reply。

Q-20 B（#178，2026-09-11）：``identity_addendum`` 加可选 ``conversation_id`` / ``peer_calls_you``
——联系人级「对方怎么叫我」≠ 人设名 → 「对方一直叫你 X，绝不纠正、自我介绍用 X」。只有传了
``conversation_id`` 才做一次联系人级读取；旧签名调用零 I/O、输出逐字不变。
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "identity_addendum", "client_side_name_sentence",
    "persona_display_name", "account_display_name",
    "album_scene_addendum", "sent_media_addendum", "album_miss_addendum",
    "time_schedule_addendum",
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


def _account_name_sentence(a: str, lg: str) -> str:
    if lg.startswith("en"):
        return (f"The other person may call you \"{a}\" — that is your display name on this "
                f"platform. Never deny it and don't explain it; just answer as yourself.")
    if lg.startswith("ja"):
        return (f"相手はあなたを「{a}」と呼ぶかもしれない。それはこのプラットフォームでの"
                f"あなたの表示名。否定せず、説明もせず、そのまま自分として答える。")
    return f"对方可能用「{a}」称呼你，那是你在这个平台的昵称，不得否认、不必解释，照常以自己的身份回应。"


def client_side_name_sentence(x: str, self_name: str, *, lang: str = "zh") -> str:
    """「对方一直叫你 X」硬约束句（Q-20 B · #178 Q9GDEH/2PKKM6）。纯格式化。

    与 ``persona_manager.client_side_name_pin`` 同义（那边进 ``【称呼·硬约束】``，
    这边进 Q-1 F 身份补丁段）；措辞里必须出现「绝不纠正」（门禁锚词）。
    """
    x = str(x or "").strip()
    y = str(self_name or "").strip()
    lg = str(lang or "zh").lower()
    if lg.startswith("en"):
        deny = f"never say \"I'm {y}, not {x}\"" if y else f"never say \"I'm not {x}\""
        return (f"The other person has always called you \"{x}\" — that IS your name to them. "
                f"Never correct it, {deny}, don't explain; when introducing yourself or asked "
                f"your name, use \"{x}\".")
    if lg.startswith("ja"):
        deny = f"「私は{y}、{x}じゃない」とは絶対に言わない" if y else f"「私は{x}じゃない」とは絶対に言わない"
        return (f"相手はずっとあなたを「{x}」と呼んでいる。それが相手にとってのあなたの名前。"
                f"絶対に訂正しない、{deny}、説明もしない。自己紹介や名前を聞かれたら「{x}」を使う。")
    deny = f"绝不说『我是 {y} 不是 {x}』" if y else f"绝不说『我不是 {x}』"
    return (f"对方一直叫你「{x}」，这是你在 TA 那里的名字：绝不纠正、{deny}、不解释；"
            f"自我介绍 / 被问名字时就用「{x}」。")


def _resolve_client_side_name(conversation_id: str) -> str:
    """联系人级「对方怎么叫我」（``contact_names``）。仅在给了会话 id 时做这一次 I/O；
    store 不可用 / 未配置 / 异常 → ""。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return ""
    try:
        from src.inbox.contact_names import get_contact_names
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None:
            return ""
        return str(get_contact_names(store, cid).get("peer_calls_you") or "").strip()
    except Exception:
        return ""


def identity_addendum(
    persona: Any, account: Any, *, lang: str = "zh",
    conversation_id: str = "", peer_calls_you: str = "",
) -> str:
    """身份附加段：账号显示名 ≠ 人设名 → 「对方可能用 X 称呼你」；**联系人级「对方怎么叫我」
    ≠ 人设名 → 「对方一直叫你 X，绝不纠正、自我介绍用 X」**（Q-20 B，#178）。两句可叠加；
    一致 / 未知 / 判不出 → 该句不出；全空 → ""。

    一致性用 ``utils.account_name_check.names_consistent``（昵称含人设名算一致：「Mizuki 🌸」是
    Mizuki）。``conversation_id`` / ``peer_calls_you`` 都不传＝Q-1 F 旧签名、零 I/O、输出逐字
    不变（Q-6 现有接线 ``identity_addendum(persona, account, lang=lang)`` 不受影响）。
    ``peer_calls_you`` 显式给值优先；否则有 ``conversation_id`` 才查联系人级（一次 I/O）。
    生产上同句已由 ``persona_manager._build_address_pin`` 进 system prompt（full / compact），
    本参数供 Q-6 在 ``_prompt_addenda`` ③ 传 ``conversation_id=conv`` 时叠进补丁段。绝不抛。"""
    try:
        p = persona_display_name(persona)
        lg = str(lang or "zh").lower()
        parts = []
        a = account_display_name(account)
        if p and a:
            from src.utils.account_name_check import names_consistent
            ok: Optional[bool] = names_consistent(a, p)
            if ok is False:
                parts.append(_account_name_sentence(a, lg))
        x = str(peer_calls_you or "").strip() or _resolve_client_side_name(conversation_id)
        if x:
            differs = True
            if p:
                try:
                    from src.utils.account_name_check import names_consistent
                    differs = names_consistent(x, p) is False
                except Exception:
                    differs = True
            if differs:
                parts.append(client_side_name_sentence(x, p, lang=lg))
        return "\n".join(parts)
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


def _recent_media_claim(conv_key: str, lang: str, inbound: str = "") -> str:
    """Q-20 D：30 分钟内我方（人工或 AI）刚发过媒体 → 「刚发过一张…认领」句；否则 ""。

    ``conv_key`` 空时读 ``image_autosend.bound_prompt_conv()``（persona_reply 组 prompt 前
    调过 ``last_media_receipt(cid)`` 的桥，同 task 内 20s TTL）——本线不动 persona_reply，
    Q-6 显式传 ``conv_key`` 后桥可拆。任何异常 → ""（回到原文案）。"""
    try:
        from src.inbox.image_autosend import bound_prompt_conv, recent_media_claim_note
        ck = str(conv_key or "").strip() or bound_prompt_conv()
        if not ck:
            return ""
        return recent_media_claim_note(ck, lang=lang, inbound=inbound)
    except Exception:
        return ""


def sent_media_addendum(
    items: Any, *, conv_known: bool = False, lang: str = "zh",
    conv_key: str = "", inbound: str = "",
) -> str:
    """本会话已发媒体清单。``conv_known=False`` → ""；已知会话但 items 空 → 写死从未发过——
    **除非** 30 分钟内我方刚发过媒体（Q-20 D：人工手发的图不在相册台账 ``items`` 里，
    VDUJX6 就是据「从未发过」拒绝的）→ 改为「刚发过一张…认领」；有清单且刚发过 → 清单后
    追加认领句。``conv_key`` 不传时经 ``image_autosend.bound_prompt_conv`` 桥取当前会话。"""
    if not conv_known:
        return ""
    lg = str(lang or "zh").lower()
    rows = [x for x in (items or []) if isinstance(x, dict)] if items else []
    claim = _recent_media_claim(conv_key, lg, inbound)
    if not rows:
        if claim:
            return claim
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
        out = (
            f"【sent media】Photos already sent in this chat (last 7 days): {body}. "
            "Don't claim you sent anything that isn't on this list."
        )
    else:
        out = (
            f"【本会话已发媒体】近 7 天已发给 TA 的：{body}。"
            "清单以外的不得声称发过。"
        )
    return out + ("\n" + claim if claim else "")


def time_schedule_addendum(
    local_time: str, itinerary: Any, *, lang: str = "zh", current_bucket: str = "",
    work_excuse_used: int = 0, work_excuse_cap: int = 1,
) -> str:
    """Q-8 F（#264）：当地时间 + 今日日程一段；借口只能从日程取；「工作」类借口同客户每日 ≤ cap。

    ``local_time`` 已格式化的当地时间短句（空 → 不写时间行）；``itinerary`` =
    ``companion_selfie.build_day_itinerary`` 输出 ``[(时段, 场景), ...]``（空 → 不写动线行）；
    ``work_excuse_used`` 今日已对该客户用过的「工作忙」借口次数（≥ cap → 追加禁用句）。
    三者皆空且未超额 → ""。纯格式化、零 I/O。
    """
    lt = str(local_time or "").strip()
    rows = [(str(a).strip(), str(b).strip()) for a, b in (
        r for r in (itinerary or []) if isinstance(r, (list, tuple)) and len(r) == 2)
        if str(b).strip()]
    try:
        used = int(work_excuse_used or 0)
        cap = max(0, int(work_excuse_cap if work_excuse_cap is not None else 1))
    except (TypeError, ValueError):
        used, cap = 0, 1
    over = used >= cap
    if not lt and not rows and not over:
        return ""
    lg = str(lang or "zh").lower()
    en = lg.startswith("en")
    cur = str(current_bucket or "").strip()
    lines = []
    if en:
        if lt:
            lines.append(f"Your local time now: {lt}.")
        if rows:
            seq = " -> ".join(f"{a}{' (now)' if a == cur else ''}: {b}" for a, b in rows)
            lines.append(f"Your day today: {seq}.")
        lines.append(
            "If you say you're busy, stepping away or will reply later, the reason must come from "
            "this schedule only. Don't invent a different one (no 'at work' unless work is on it)."
            if rows else
            "If you say you're busy or stepping away, don't invent a reason; if you have no real "
            "one, just don't give one.")
        if over:
            lines.append(
                f"You have already used a work excuse with this person today ({used}/{cap}). "
                "This turn: no work / office / meeting / overtime as the reason. Pick something "
                "else from the schedule or skip the excuse.")
        return "【local time & today's schedule】" + " ".join(lines)
    if lt:
        lines.append(f"现在当地时间：{lt}。")
    if rows:
        seq = "→".join(f"{a}{'(现在)' if a == cur else ''}:{b}" for a, b in rows)
        lines.append(f"你今天的日程：{seq}。")
    lines.append(
        "如果要说自己在忙 / 走不开 / 晚点回，理由**只能取自这条日程**，不许另编"
        "（日程里没有上班就别说在上班）。" if rows else
        "如果要说自己在忙 / 走不开，不许现编理由；没有真实理由就不给理由。")
    if over:
        lines.append(
            f"今天已经对 TA 用过「工作忙」这个理由（{used}/{cap}），本轮**不许再以工作 / 上班 / "
            "加班 / 开会为借口**：从日程里换别的，或者干脆不找理由。")
    return "【当地时间与今日日程】" + "".join(lines)


def album_miss_addendum(
    scene: Any, *, lang: str = "zh", conv_key: str = "", inbound: str = "",
) -> str:
    """无匹配回喂。scene 空仍给一句（索图但完全没图）；调用方决定是否调用。

    Q-20 D：相册为空 / 无匹配**但 30 分钟内我方刚发过一张**（多半是人工手发、不在相册）
    → 「无可用照片…只许拒绝」改为「刚发过一张（描述）：TA 问是不是你就认领；要新的照片才按
    没有合适的婉拒，但绝不说不能发图 / 没这个功能」。``conv_key`` 不传时走
    ``image_autosend.bound_prompt_conv`` 桥。"""
    sc = str(scene or "").strip() or "照片"
    lg = str(lang or "zh").lower()
    claim = _recent_media_claim(conv_key, lg, inbound)
    if claim:
        if lg.startswith("en"):
            return (claim + " If TA wants a NEW photo of another scene, gently say you don't have "
                    "a good one right now — still never 'I can't send photos / no such feature', "
                    "never 'already sent / loading / later'.")
        if lg.startswith("ja"):
            return (claim + " 別のシーンの新しい写真を求められたら「今ちょうどいいのがない」と"
                    "やわらかく断る——それでも「写真は送れない / 機能がない」「送った / 読み込み中 / あとで」は禁止。")
        return (claim + " 如果 TA 要的是**另一张新照片**（别的场景），就说现在没有合适的、"
                "轻轻带过——照样禁止说「不能发图 / 没这个功能」，禁止说已经发了、加载中、稍后发。")
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
