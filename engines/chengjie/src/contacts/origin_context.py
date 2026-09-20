"""跨平台档案 → AI 注入块（``_origin_block``）纯函数核心 + 进程级 provider。

「客户从哪个平台来、在那边叫什么、聊过哪些话题域」升格为 AI 可消费的**叙事层**：
- 数据源＝contacts.db 的 ``contact_profiles``（坐席表单/导入写入）+ ``channel_identities``
  （引流/合并链路一直在收的来源事实：direction/linked_via/linked_at/display_name）。
- **分层原则**：本块只管叙事（从哪来/叫什么/聊过哪些话题域）；具体记忆事实走
  episodic_memory 既有轨道（salience/向量召回），两层内容天然不重叠、prompt 预算可控。
- **零输入也有产出**：无档案行时，只要该客户有 ≥2 个平台身份（引流码/合并已发生），
  仅凭 CI 数据即可渲染来源轨迹——「关系进展自动结合」的 P0 形态。

接线（与 ``_bazi_block`` 同「有键即消费」模式）：
``bootstrap.services`` 经 :func:`make_origin_provider` 注册进程级 provider →
``skill_manager._inject_origin_context``（A 线 process_message + B 线 generate_inbox_draft）
→ ``ai_client._build_context_prompt`` 消费。开关 ``contacts.origin_profile.enabled``
（默认关）在 provider 内部**按次实读**（overlay 热改可即时停注，不必重启）。

纯函数无 I/O，provider 闭包持 store；60s TTL 进程缓存把每条消息的开销压到字典查找级。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# 平台展示名（prompt 内部参考用中文；块本身是内部指令，与既有注入块口径一致）
CHANNEL_LABELS: Dict[str, str] = {
    "telegram": "Telegram",
    "line": "LINE",
    "line_rpa": "LINE",
    "messenger": "Messenger",
    "messenger_rpa": "Messenger",
    "whatsapp": "WhatsApp",
    "whatsapp_rpa": "WhatsApp",
    "web": "网页",
    "mobile": "手机端",
    "wechat": "微信",
    "imessage": "iMessage",
    "other": "其他平台",
}

# ChannelIdentity.linked_via → 人话（联入方式）
LINKED_VIA_LABELS: Dict[str, str] = {
    "token": "引流码",
    "regex": "口令匹配",
    "heuristic": "智能匹配",
    "manual": "人工确认",
}

# 表单「认识多久」选项档 → 叙事短语（自由文本原样透传）
KNOWN_SINCE_LABELS: Dict[str, str] = {
    "recent": "最近才认识",
    "months": "认识几个月了",
    "halfyear": "认识半年以上",
    "years": "认识一年以上",
}

_GUARD_LINE = (
    "· 对方默认你们认识——他提旧话题就自然接上；"
    "记不准的细节宁可含糊，绝不编造，也不要主动逐条复述上面这些背景"
)
_HEADER_LINE = "【跨平台背景】（内部参考：自然融入对话，不要提「系统/记录/档案」）"


def normalize_channel(platform: str) -> str:
    """inbox 平台名 → contacts channel 命名空间（``line_rpa`` → ``line``）。"""
    p = str(platform or "").strip().lower()
    if p.endswith("_rpa"):
        p = p[: -len("_rpa")]
    return p


def channel_label(channel: str) -> str:
    c = str(channel or "").strip().lower()
    return CHANNEL_LABELS.get(c) or CHANNEL_LABELS.get(normalize_channel(c)) or (channel or "")


def _ci_attr(ci: Any, key: str) -> str:
    if isinstance(ci, dict):
        return str(ci.get(key) or "")
    return str(getattr(ci, key, "") or "")


def _ci_ts(ci: Any) -> int:
    try:
        if isinstance(ci, dict):
            return int(ci.get("linked_at") or 0)
        return int(getattr(ci, "linked_at", 0) or 0)
    except (TypeError, ValueError):
        return 0


def derive_origin_trail(identities: List[Any]) -> List[Dict[str, Any]]:
    """CI 列表 → 来源轨迹（按渠道去重；first_seen 优先、其余按 linked_at 升序）。

    每项：``{channel, label, via, via_label, linked_at, display_name, is_origin}``。
    """
    rows: List[Dict[str, Any]] = []
    for ci in identities or []:
        ch = _ci_attr(ci, "channel")
        if not ch:
            continue
        rows.append({
            "channel": ch,
            "label": channel_label(ch),
            "via": _ci_attr(ci, "linked_via"),
            "via_label": LINKED_VIA_LABELS.get(_ci_attr(ci, "linked_via"), ""),
            "linked_at": _ci_ts(ci),
            "display_name": _ci_attr(ci, "display_name"),
            "direction": _ci_attr(ci, "direction") or "first_seen",
        })
    # first_seen 在前，其余按时间；同渠道保留最早一条
    rows.sort(key=lambda r: (0 if r["direction"] == "first_seen" else 1, r["linked_at"]))
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        key = normalize_channel(r["channel"])
        if key in seen:
            continue
        seen.add(key)
        r["is_origin"] = not out
        out.append(r)
    return out


def _ts_phrase(ts: int, now: Optional[float] = None) -> str:
    """时间戳 → 「2026-03」式月份短语；无效 → 空。"""
    try:
        t = int(ts or 0)
    except (TypeError, ValueError):
        t = 0
    if t <= 0:
        return ""
    try:
        return time.strftime("%Y-%m", time.localtime(t))
    except Exception:
        return ""


def known_since_phrase(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    return KNOWN_SINCE_LABELS.get(s.lower(), s)


def build_origin_block(
    profile: Optional[Dict[str, Any]],
    identities: List[Any],
    *,
    current_platform: str = "",
    max_chars: int = 500,
    imports: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """档案 + 渠道身份 + 导入批次 → ``_origin_block`` 文本；无可说内容 / ai_visible 关 → 空串。

    ``imports``＝已确认的聊天记录导入批次（P1）：只取最新一批的来源/条数/时间范围
    做出处叙事（「之前在微信聊过，2025-11~2026-02 共 214 条」）——内容本身在 topics
    与 episodic 事实里，这行给 AI 的是**时间与来源的锚**（防「上周聊的」类时间幻觉）。
    预算收缩顺序（超 ``max_chars`` 时）：背景叙述截短 → 话题砍到 3 个 → 弃称呼行；
    头行与守则行永不裁（守则行是防「AI 报菜名穿帮」的关键钉子）。
    """
    prof = profile or {}
    if profile is not None and not prof.get("ai_visible", True):
        return ""
    trail = derive_origin_trail(identities)
    cur_key = normalize_channel(current_platform)

    # ── 来源行 ─────────────────────────────────────────────
    imp_rows = [i for i in (imports or []) if isinstance(i, dict)]
    origin_parts: List[str] = []
    prof_origin = str(prof.get("origin_channel") or "").strip()
    origin_ch = prof_origin or (trail[0]["channel"] if trail else "")
    if not origin_ch and imp_rows:
        # 无档案来源、无轨迹，但导入了历史记录 → 导入来源即事实上的来处
        origin_ch = str(imp_rows[0].get("source_channel") or "").strip()
    if origin_ch:
        seg = f"最早来自 {channel_label(origin_ch)}"
        extras: List[str] = []
        label = str(prof.get("origin_label") or "").strip()
        if label:
            extras.append(label)
        ks = known_since_phrase(str(prof.get("known_since") or ""))
        if ks:
            extras.append(ks)
        if not extras and trail and trail[0].get("linked_at"):
            m = _ts_phrase(trail[0]["linked_at"])
            if m:
                extras.append(m)
        if extras:
            seg += f"（{'，'.join(extras)}）"
        origin_parts.append(seg)
    # 后续联入（排除来源渠道本身）
    hops: List[str] = []
    for r in trail[1:]:
        hop = f"后经{r['via_label'] or '关联'}联入 {r['label']}"
        m = _ts_phrase(r.get("linked_at") or 0)
        if m:
            hop += f"（{m}）"
        hops.append(hop)
    if hops:
        origin_parts.append("、".join(hops))
    origin_line = ("· 来源：" + "，".join(origin_parts)) if origin_parts else ""

    # ── 称呼行（原平台昵称 + 偏好称呼；当前平台昵称人设本就知道，不占预算）──
    name_bits: List[str] = []
    prior = prof.get("prior_names") or {}
    seen_name_ch: set = set()
    if isinstance(prior, dict):
        for ch, nm in prior.items():
            nm = str(nm or "").strip()
            chk = normalize_channel(str(ch))
            if not nm or chk == cur_key:
                continue
            seen_name_ch.add(chk)
            name_bits.append(f"在 {channel_label(str(ch))} 叫「{nm}」")
    for r in trail:
        chk = normalize_channel(r["channel"])
        nm = str(r.get("display_name") or "").strip()
        if not nm or chk == cur_key or chk in seen_name_ch:
            continue
        seen_name_ch.add(chk)
        name_bits.append(f"在 {r['label']} 叫「{nm}」")
    preferred = str(prof.get("preferred_name") or "").strip()
    if preferred:
        name_bits.append(f"偏好被称呼「{preferred}」")
    names_line = ("· 称呼：" + "；".join(name_bits)) if name_bits else ""

    # ── 话题行 / 导入出处行 / 背景行 ───────────────────────
    topics = [str(t).strip() for t in (prof.get("topics") or []) if str(t).strip()]
    topics_line = ("· 之前聊过的话题：" + "、".join(topics[:6])) if topics else ""
    imp_line = ""
    if imp_rows:
        latest = imp_rows[0]
        src_lbl = channel_label(str(latest.get("source_channel") or "")) or "其他平台"
        rng = ""
        if latest.get("date_from") or latest.get("date_to"):
            rng = f"{latest.get('date_from') or '?'}~{latest.get('date_to') or '?'}，"
        total = sum(int(i.get("msg_count") or 0) for i in imp_rows)
        imp_line = (f"· 你们在 {src_lbl} 有过历史聊天（{rng}共 {total} 条，"
                    "要点已在你的记忆里）——时间线别记错：那些是当时聊的，不是最近")
    note = str(prof.get("background_note") or "").strip().replace("\n", " ")
    note_line = f"· 背景：{note}" if note else ""

    # 「有可说的内容」判据：来源行以外至少还有一条实质信息，或轨迹跨 ≥2 平台，
    # 或档案显式填了来源——单平台裸 CI（每个会话都有）不值得占 prompt。
    substantial = bool(names_line or topics_line or note_line or imp_line
                       or prof_origin or len(trail) >= 2)
    if not origin_line or not substantial:
        return ""

    def _assemble(nl: str, tl: str, mml: str) -> str:
        lines = [_HEADER_LINE, origin_line]
        if mml:
            lines.append(mml)
        if tl:
            lines.append(tl)
        if imp_line:
            lines.append(imp_line)
        if nl:
            lines.append(nl)
        lines.append(_GUARD_LINE)
        return "\n".join(lines)

    budget = max(200, int(max_chars or 500))
    block = _assemble(note_line, topics_line, names_line)
    if len(block) > budget and note_line:
        overflow = len(block) - budget
        keep = max(0, len(note) - overflow - 1)
        note_line = f"· 背景：{note[:keep]}…" if keep > 8 else ""
        block = _assemble(note_line, topics_line, names_line)
    if len(block) > budget and topics_line and len(topics) > 3:
        topics_line = "· 之前聊过的话题：" + "、".join(topics[:3])
        block = _assemble(note_line, topics_line, names_line)
    if len(block) > budget and names_line:
        block = _assemble(note_line, topics_line, "")
    return block


def origin_pill(profile: Optional[Dict[str, Any]], trail: List[Dict[str, Any]]) -> str:
    """卡头 pill 摘要（折叠态一眼读懂）：跨平台轨迹「A→B」或单来源标签。

    与 :func:`build_origin_block` 的「有可说内容」判据同口径——块为空时 pill 也空，
    防止「pill 说有、展开却空」的错觉。
    """
    prof = profile or {}
    if profile is not None and not prof.get("ai_visible", True):
        return ""
    labels: List[str] = []
    oc = str(prof.get("origin_channel") or "").strip()
    if oc:
        labels.append(channel_label(oc))
    for r in trail or []:
        lb = str(r.get("label") or "")
        if lb and lb not in labels:
            labels.append(lb)
    if len(labels) >= 2:
        return "→".join(labels[:3])
    has_body = bool(
        prof.get("topics") or prof.get("background_note")
        or prof.get("preferred_name") or oc)
    if has_body:
        return labels[0] if labels else "✓"
    return ""


def resolve_contact_for_conversation(
    store: Any, *, platform: str, account_id: str, chat_key: str,
) -> str:
    """会话三元组 → contact_id（先按原样平台名，再按规范化 channel 兜底）。"""
    if store is None or not chat_key:
        return ""
    from src.contacts.identity_bridge import resolve_contact_id
    tried: set = set()
    for plat in (str(platform or "").strip(), normalize_channel(platform)):
        if not plat or plat in tried:
            continue
        tried.add(plat)
        cid = resolve_contact_id(
            store, platform=plat,
            account_id=str(account_id or "default") or "default",
            chat_key=str(chat_key),
        )
        if cid:
            return cid
    return ""


# ── 进程级观测计数（P2 2026-08-18：ops 卡「跨平台档案」读数；重启清零）────
_STATS: Dict[str, int] = {
    "lookups": 0,        # provider 被消费次数（开闸后每条消息一次，缓存命中也算）
    "cache_hits": 0,     # 60s TTL 缓存命中
    "blocks": 0,         # 真渲染出非空块（= AI 实际吃到背景的次数）
    "merge_links": 0,    # 合并联动记忆合流：新建 link 对数
    "merge_rows": 0,     # 合并联动迁移的记忆行数
}


def record_merge_stats(links: int, rows: int) -> None:
    """merge_bridge 回报合流量（best-effort 计数，绝不抛）。"""
    try:
        _STATS["merge_links"] += int(links or 0)
        _STATS["merge_rows"] += int(rows or 0)
    except Exception:
        pass


def origin_stats_snapshot() -> Dict[str, int]:
    return dict(_STATS)


# ── 进程级 provider（60s TTL 缓存；写路径调 invalidate 即时生效）──────────
_CACHE: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
_CACHE_TTL_SEC = 60.0
_CACHE_MAX = 512


def _cache_key(channel: str, account_id: str, external_id: str) -> Tuple[str, str, str]:
    return (
        str(channel or "").strip().lower(),
        str(account_id or "default") or "default",
        str(external_id or ""),
    )


def invalidate_origin_cache(
    channel: str = "", account_id: str = "", external_id: str = "",
) -> None:
    """档案写入后清缓存；无参 = 全清（合并/身份关联这类跨会话写用全清）。"""
    if not channel and not account_id and not external_id:
        _CACHE.clear()
        return
    _CACHE.pop(_cache_key(channel, account_id, external_id), None)


def make_origin_provider(store: Any, config_manager: Any) -> Callable[..., str]:
    """构造 ``origin_block_lookup`` provider（签名对齐 rpa_hooks 家族：
    关键字参数 ``channel/account_id/external_id``，返回渲染好的块或空串）。

    开关 ``contacts.origin_profile.enabled`` **每次调用实读**（支持 overlay 热关）；
    任何异常返回空串，绝不影响主回复链路。
    """

    def _provider(*, channel: str = "", account_id: str = "", external_id: str = "") -> str:
        try:
            cfg = getattr(config_manager, "config", None)
            cfg = cfg if isinstance(cfg, dict) else (
                config_manager if isinstance(config_manager, dict) else {})
            ocfg = ((cfg.get("contacts") or {}).get("origin_profile") or {})
            if not ocfg.get("enabled", False):
                return ""
            if not external_id:
                return ""
            _STATS["lookups"] += 1
            key = _cache_key(channel, account_id, external_id)
            now = time.time()
            hit = _CACHE.get(key)
            if hit is not None and (now - hit[0]) < _CACHE_TTL_SEC:
                _STATS["cache_hits"] += 1
                if hit[1]:
                    _STATS["blocks"] += 1
                return hit[1]
            block = ""
            cid = resolve_contact_for_conversation(
                store, platform=channel, account_id=account_id, chat_key=external_id)
            if cid:
                profile = store.get_contact_profile(cid)
                idents = store.list_channel_identities_of(cid)
                imports: List[Dict[str, Any]] = []
                if hasattr(store, "list_memory_imports"):
                    try:
                        imports = store.list_memory_imports(
                            cid, status="confirmed", limit=3)
                    except Exception:
                        imports = []
                block = build_origin_block(
                    profile, idents, current_platform=channel,
                    max_chars=int(ocfg.get("max_chars", 500) or 500),
                    imports=imports,
                )
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = (now, block)
            if block:
                _STATS["blocks"] += 1
            return block
        except Exception:
            return ""

    return _provider


__all__ = [
    "CHANNEL_LABELS",
    "LINKED_VIA_LABELS",
    "KNOWN_SINCE_LABELS",
    "normalize_channel",
    "channel_label",
    "known_since_phrase",
    "derive_origin_trail",
    "build_origin_block",
    "origin_pill",
    "resolve_contact_for_conversation",
    "make_origin_provider",
    "invalidate_origin_cache",
    "origin_stats_snapshot",
    "record_merge_stats",
]
