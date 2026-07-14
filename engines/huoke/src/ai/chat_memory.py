# -*- coding: utf-8 -*-
"""对话长久记忆层 (P3 — B 机 Messenger 聊天机器人记忆子系统)。

设计原则 (2026-04-23):
  * **零共享区 schema 改动**: 直接把 ``facebook_inbox_messages`` 当作记忆底层,
    派生画像用 SQL 聚合实时计算。对比"加 fb_chat_memory 摘要表"方案:
      - 没有 schema migration 风险,不 block A 机 review
      - 没有"派生缓存 vs 底层消息"的一致性问题
      - 代价: 每次生成回复多几个 ms 的 SQL (实测 p99 < 15ms/peer)
  * **纯读**: 不调任何 fb_store 写入函数,不污染契约共享区
  * **graceful degrade**: DB 异常/空 peer 一律返回空 dict/list,调用方无需特判

记忆分层:
  L1 — 会话历史回放: 同 peer 最近 N 条消息按时间排序,LLM 可读格式
  L2 — 派生画像: 累计轮数/对方语言偏好/活跃时段/引流历史/最近话题片段
  L3 — (未来)结构化抽取: 靠 LLM 把消息内容提炼成"对方生日/兴趣/职业"等
        —— 这一层才需要新表,留给 P3b 做,届时加 fb_chat_memory 表

与 _ai_reply_and_send 的集成契约:
  在生成 reply 之前调 ``build_context_block(device_id, peer_name)``,
  把 ``history_text`` + ``profile_text`` 拼进 ChatBrain 的 ``ab_style_hint``。
  profile_text 对 LLM 的价值: 语言偏好 / 轮数 / 上次引流结果 / 话题延续。
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L1: 会话历史
# ─────────────────────────────────────────────────────────────────────────────

_HISTORY_FIELDS = (
    "id", "direction", "message_text", "seen_at", "sent_at",
    "language_detected", "ai_decision", "template_id", "peer_type",
)


def get_history(device_id: str, peer_name: str, *,
                limit: int = 5) -> List[Dict[str, Any]]:
    """拉同 peer 最近 ``limit`` 条消息,按插入顺序升序(旧→新)。

    用 ``id`` 列做排序而非 ``seen_at`` — id 是 AUTOINCREMENT 严格单调递增,
    生产高并发时同秒写入的多条 seen_at 相同但 id 不同,用 id 排序才稳定。

    返回空列表若 peer 无历史或 DB 异常。只读,不会抛出。

    Args:
        device_id: 目标设备
        peer_name: 目标 peer (和 ``facebook_inbox_messages.peer_name`` 精确匹配)
        limit: 拉多少条,建议 5-10;单轮 prompt token 预算内
    """
    if not device_id or not peer_name or limit <= 0:
        return []
    try:
        from src.host.database import _connect
        with _connect() as conn:
            cols = ", ".join(_HISTORY_FIELDS)
            # 先 id DESC LIMIT 取到最近 N,再外层 ORDER BY id ASC 转正序
            sql = (
                f"SELECT {cols} FROM ("
                f"  SELECT {cols} FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=?"
                " ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC"
            )
            rows = conn.execute(sql, (device_id, peer_name, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("[chat_memory.get_history] DB 失败(降级空历史): %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# L2: 派生画像
# ─────────────────────────────────────────────────────────────────────────────

def get_derived_profile(device_id: str, peer_name: str) -> Dict[str, Any]:
    """实时聚合 peer 画像 (零 LLM 调用,纯 SQL)。

    返回结构:
      total_turns:            int — 累计消息条数 (in + out)
      peer_reply_count:       int — 对方发来条数 (direction=incoming)
      bot_reply_count:        int — 机器自动回复条数 (outgoing & ai_decision IN ('reply','wa_referral'))
      greeting_count:         int — A 机发过 greeting 条数 (outgoing & ai_decision='greeting')
      language_stats:         Dict[lang, count] — 对方 incoming 语言检测分布
      language_pref:          str — 出现次数最多的语言 (众数);空则 ''
      first_seen_at:          str — 最早一条消息时间戳 (ISO);空则 ''
      last_seen_at:           str — 最近一条消息时间戳 (ISO);空则 ''
      last_incoming_at:       str — 对方最近一次发言时间
      last_outgoing_at:       str — 机器最近一次发言时间
      active_hours_utc:       List[int] — 对方 incoming 最常见的 UTC 小时 top-3
      referral_attempts:      int — 历史引流尝试次数 (ai_decision='wa_referral')
      last_referral_at:       str — 最近一次引流时间
      referral_got_reply:     bool — 上次引流后对方有无发新 incoming
      recent_topics_snippet:  str — 最近 5 条 incoming 原文拼接,截断到 400 字
      greeting_template_ids:  List[str] — A 机发过的 greeting 模板 ID (去重)

    Peer 不存在/DB 异常时返回全部字段默认值的 dict (数值 0/空串/空列表)。
    """
    out = _empty_profile()
    if not device_id or not peer_name:
        return out
    try:
        from src.host.database import _connect
        with _connect() as conn:
            # 一次拉全部相关行 (peer 历史一般不超过几百条)
            # 用 id 排序保证同秒写入的行顺序稳定
            sql = (
                "SELECT id, direction, message_text, seen_at, sent_at,"
                "  language_detected, ai_decision, template_id"
                " FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=?"
                " ORDER BY id ASC"
            )
            rows = conn.execute(sql, (device_id, peer_name)).fetchall()
    except Exception as e:
        logger.debug("[chat_memory.get_derived_profile] DB 失败(降级空画像): %s", e)
        return out

    if not rows:
        return out

    all_msgs = [dict(r) for r in rows]
    in_msgs = [r for r in all_msgs if r["direction"] == "incoming"]
    out_msgs = [r for r in all_msgs if r["direction"] == "outgoing"]

    out["total_turns"] = len(all_msgs)
    out["peer_reply_count"] = len(in_msgs)
    out["bot_reply_count"] = sum(
        1 for r in out_msgs
        if (r.get("ai_decision") or "") in ("reply", "wa_referral"))
    out["greeting_count"] = sum(
        1 for r in out_msgs if (r.get("ai_decision") or "") == "greeting")

    lang_counter: Counter = Counter()
    for r in in_msgs:
        lang = (r.get("language_detected") or "").strip()
        if lang:
            lang_counter[lang] += 1
    out["language_stats"] = dict(lang_counter)
    out["language_pref"] = lang_counter.most_common(1)[0][0] if lang_counter else ""

    out["first_seen_at"] = all_msgs[0].get("seen_at") or ""
    out["last_seen_at"] = all_msgs[-1].get("seen_at") or ""
    out["last_incoming_at"] = in_msgs[-1].get("seen_at") if in_msgs else ""
    out["last_outgoing_at"] = (
        out_msgs[-1].get("sent_at") or out_msgs[-1].get("seen_at")
        if out_msgs else ""
    )

    hour_counter: Counter = Counter()
    for r in in_msgs:
        h = _iso_to_hour(r.get("seen_at") or "")
        if h is not None:
            hour_counter[h] += 1
    out["active_hours_utc"] = [h for h, _ in hour_counter.most_common(3)]

    referrals = [
        r for r in out_msgs
        if (r.get("ai_decision") or "") == "wa_referral"
    ]
    out["referral_attempts"] = len(referrals)
    if referrals:
        last_ref = referrals[-1]
        out["last_referral_at"] = (
            last_ref.get("sent_at") or last_ref.get("seen_at") or "")
        # 引流后有没有新 incoming — 用 id 比较保证同秒写入也正确
        last_ref_id = int(last_ref.get("id") or 0)
        out["referral_got_reply"] = any(
            int(r.get("id") or 0) > last_ref_id for r in in_msgs
        )

    # 最近 5 条 incoming 原文拼接
    recent_in = in_msgs[-5:]
    snippet_parts = [
        (r.get("message_text") or "").strip().replace("\n", " ")
        for r in recent_in if (r.get("message_text") or "").strip()
    ]
    snippet = " | ".join(snippet_parts)
    if len(snippet) > 400:
        snippet = snippet[:397] + "..."
    out["recent_topics_snippet"] = snippet

    template_ids = sorted({
        r.get("template_id") for r in out_msgs
        if (r.get("ai_decision") or "") == "greeting"
        and (r.get("template_id") or "").strip()
    })
    out["greeting_template_ids"] = template_ids

    return out


def _empty_profile() -> Dict[str, Any]:
    return {
        "total_turns": 0,
        "peer_reply_count": 0,
        "bot_reply_count": 0,
        "greeting_count": 0,
        "language_stats": {},
        "language_pref": "",
        "first_seen_at": "",
        "last_seen_at": "",
        "last_incoming_at": "",
        "last_outgoing_at": "",
        "active_hours_utc": [],
        "referral_attempts": 0,
        "last_referral_at": "",
        "referral_got_reply": False,
        "recent_topics_snippet": "",
        "greeting_template_ids": [],
    }


def _iso_to_hour(iso: str) -> Optional[int]:
    """从 ISO 时间戳抽小时数 (UTC)。容错 seen_at 的两种格式。"""
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(iso, fmt).hour
        except ValueError:
            continue
    # 宽松兜底
    try:
        return int(iso[11:13])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 格式化 — 喂给 LLM 的可读文本块
# ─────────────────────────────────────────────────────────────────────────────

def format_history_for_llm(history: List[Dict[str, Any]]) -> str:
    """把历史消息组装成 LLM 容易理解的单字符串。

    格式:
      【历史对话 · 最近 N 轮】
      [2026-04-22 10:30] 对方: こんにちは
      [2026-04-22 10:35] 我方: はじめまして
    """
    if not history:
        return ""
    lines = [f"【历史对话 · 最近 {len(history)} 轮】"]
    for r in history:
        ts = (r.get("seen_at") or "").replace("T", " ").replace("Z", "")[:16]
        tag = "对方" if r.get("direction") == "incoming" else "我方"
        text = (r.get("message_text") or "").strip().replace("\n", " ")
        if not text:
            continue
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(f"[{ts}] {tag}: {text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_profile_for_llm(profile: Dict[str, Any]) -> str:
    """把画像中对 LLM 决策有价值的信号,拼成 system prompt 附加段。

    只输出非默认值的字段,避免噪声。新手(total_turns=0)直接返回空串,
    让 LLM 按破冰逻辑走。
    """
    if not profile or profile.get("total_turns", 0) == 0:
        return ""

    parts: List[str] = ["【Peer 画像提示】"]
    turns = profile.get("total_turns", 0)
    peer_n = profile.get("peer_reply_count", 0)
    bot_n = profile.get("bot_reply_count", 0)
    if turns:
        parts.append(
            f"- 累计 {turns} 条消息 (对方 {peer_n} / 机器 {bot_n} / 打招呼"
            f" {profile.get('greeting_count', 0)})"
        )

    lang_pref = profile.get("language_pref", "")
    if lang_pref:
        parts.append(f"- 对方最常用语言: {lang_pref} (务必用同语种回复)")

    hours = profile.get("active_hours_utc") or []
    if hours:
        parts.append(f"- 对方常发言 UTC 时段: {hours}")

    if profile.get("referral_attempts", 0) > 0:
        got = profile.get("referral_got_reply", False)
        parts.append(
            f"- 历史引流尝试 {profile['referral_attempts']} 次,最近一次"
            f" {profile.get('last_referral_at', '')},对方{'有' if got else '未'}回复"
        )
        if not got:
            parts.append("  ⚠ 本轮**不要再重复引流**,先自然闲聊建立关系")

    snippet = profile.get("recent_topics_snippet", "")
    if snippet:
        parts.append(f"- 最近对方话题片段: {snippet}")

    return "\n".join(parts) if len(parts) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# P10: L3 结构化记忆读侧 (MVP — 复用 A 的 fb_contact_events.meta_json)
# ─────────────────────────────────────────────────────────────────────────────
#
# 设计理念:
#   * **零新 schema**: 不建 fb_chat_memory 表。复用 A Phase 5 的
#     fb_contact_events.meta_json 的 reserved key `extracted_facts`。
#   * **只做读侧**: 本 commit 不做 LLM 抽取写入 (那需要 careful sampling
#     + 成本控制, 留给后续 P10b)。读侧接口就绪, 任意 event 的 meta_json
#     里有 extracted_facts 字段, chat_memory 就能读出来合并进 prompt。
#   * **跨事件合并**: 遍历 peer 的所有 contact events, 按时序合并
#     extracted_facts dict, 最新值覆盖旧值。
#
# 约定 extracted_facts 的 schema (开放,可扩展):
#   {
#     "birthday": "YYYY-MM-DD" | "YYYY-MM",   # 对方透露的生日 (完整或月份)
#     "occupation": "设计师" | "老师" | ...,    # 职业
#     "interests": ["摄影", "旅游", ...],       # 兴趣标签列表
#     "location": "Tokyo" | "Rome" | ...,      # 所在地
#     "family": "已婚|单身|有孩子" | ...,        # 家庭状态
#     "pain_points": ["失眠", "肩颈痛"],        # 痛点 (健康/生活类客群)
#     "budget_signal": "high"|"mid"|"low",     # 消费力信号
#     "timezone_hint": "UTC+9",                # 时区线索
#     "<任意 key>": "<任意 value>",             # 业务可扩展
#   }
#

def get_peer_extracted_facts(device_id: str, peer_name: str,
                             max_events: int = 200) -> Dict[str, Any]:
    """读 A 的 fb_contact_events.meta_json 合并 peer 的 extracted_facts。

    遍历 peer 最近 ``max_events`` 条 contact events, 收集每个事件 meta_json
    里的 ``extracted_facts`` dict, 按时序合并 — 新事件的同名 key 覆盖旧事件。

    Args:
        device_id: 设备 ID
        peer_name: peer 姓名
        max_events: 扫描事件数上限 (防止扫太多历史 events)

    Returns:
        合并后的 facts dict。Phase 5 未 merge 或 peer 无 events 时返 ``{}``。
        永不抛。

    如果一个字段在多个 event 里出现:
      * 标量(str/int/bool): 新覆盖旧
      * 列表: 去重合并 (最新列表放前面)
      * dict: 浅合并 (新 key 覆盖旧 key)
    """
    if not device_id or not peer_name:
        return {}
    # Phase 5 未 merge 时 list_contact_events_by_peer 不存在 → 返空
    try:
        from src.host.fb_store import list_contact_events_by_peer
    except ImportError:
        return {}
    try:
        events = list_contact_events_by_peer(device_id, peer_name,
                                              limit=max_events) or []
    except Exception as e:
        logger.debug("[L3] list_contact_events_by_peer 失败: %s", e)
        return {}

    # 按 id/detected_at 升序处理 (旧→新, 新覆盖旧)
    try:
        events = sorted(events, key=lambda e: (e.get("id") or 0,
                                                 e.get("detected_at") or ""))
    except Exception:
        pass

    import json as _json
    merged: Dict[str, Any] = {}
    for ev in events:
        raw_meta = ev.get("meta_json") or ev.get("meta") or ""
        if isinstance(raw_meta, dict):
            meta = raw_meta
        elif isinstance(raw_meta, str) and raw_meta:
            try:
                meta = _json.loads(raw_meta)
            except Exception:
                continue
        else:
            continue
        facts = meta.get("extracted_facts")
        if not isinstance(facts, dict):
            continue
        for k, v in facts.items():
            if isinstance(v, list):
                # 列表: 去重合并, 新值优先
                existing = merged.get(k)
                if isinstance(existing, list):
                    combined: List[Any] = list(v)
                    for item in existing:
                        if item not in combined:
                            combined.append(item)
                    merged[k] = combined
                else:
                    merged[k] = list(v)
            elif isinstance(v, dict):
                existing = merged.get(k)
                if isinstance(existing, dict):
                    merged[k] = {**existing, **v}
                else:
                    merged[k] = dict(v)
            else:
                merged[k] = v  # 标量覆盖
    return merged


def format_extracted_facts_for_llm(facts: Dict[str, Any]) -> str:
    """把 extracted_facts 拼成 LLM 可读段。空 facts 返空串。

    输出示例:
      【对方已知事实 (L3 结构化记忆)】
      - 生日: 1988-05
      - 职业: 设计师
      - 兴趣: 摄影, 旅游, 日本料理
      - 所在地: Tokyo
      - 痛点: 失眠, 肩颈痛

    字段名中文化 + 只显示非空字段。未知字段直接 k=v 打出来。
    """
    if not facts:
        return ""

    # 字段中文化映射 (和 extracted_facts schema 对齐)
    field_names = {
        "birthday": "生日",
        "occupation": "职业",
        "interests": "兴趣",
        "location": "所在地",
        "family": "家庭",
        "pain_points": "痛点",
        "budget_signal": "消费力",
        "timezone_hint": "时区",
    }
    lines = ["【对方已知事实 (L3 结构化记忆)】"]
    for k, v in facts.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        label = field_names.get(k, k)
        if isinstance(v, list):
            value = ", ".join(str(x) for x in v if x)
        elif isinstance(v, dict):
            value = ", ".join(f"{kk}={vv}" for kk, vv in v.items())
        else:
            value = str(v)
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# 统一入口 — _ai_reply_and_send 调用
# ─────────────────────────────────────────────────────────────────────────────

def build_context_block(device_id: str, peer_name: str, *,
                        history_limit: int = 5,
                        include_l3_facts: bool = True) -> Dict[str, Any]:
    """一站式拼装记忆块 — 调用方通常只看 ``hint_text``。

    返回:
      history:      List[Dict]         — 原始历史 (供调用方另用)
      profile:      Dict[str, Any]     — 原始派生画像 (L1+L2)
      history_text: str                — LLM 可读历史段
      profile_text: str                — LLM 可读画像段
      facts:        Dict[str, Any]     — P10 L3 extracted_facts (Phase 5 未 merge 时 {})
      facts_text:   str                — L3 facts 的 LLM 可读段
      hint_text:    str                — 三段拼接 (注入 ab_style_hint)
      should_block_referral: bool      — 是否要在本轮阻止引流 (来自画像判断)

    无历史时 hint_text='', should_block_referral=False (由 caller 走 cold-start 路径)。
    """
    history = get_history(device_id, peer_name, limit=history_limit)
    profile = get_derived_profile(device_id, peer_name)

    history_text = format_history_for_llm(history)
    profile_text = format_profile_for_llm(profile)

    # P10 L3 读侧 (Phase 5 未 merge 时 facts={}, facts_text="")
    facts: Dict[str, Any] = {}
    facts_text = ""
    if include_l3_facts:
        try:
            facts = get_peer_extracted_facts(device_id, peer_name)
            facts_text = format_extracted_facts_for_llm(facts)
        except Exception as e:
            logger.debug("[L3] build_context_block 读取 facts 失败: %s", e)

    pieces = [p for p in (history_text, profile_text, facts_text) if p]
    hint_text = "\n\n".join(pieces)

    # 引流阻塞: 上次引流后对方没回 → 本轮不要再引
    should_block_referral = bool(
        profile.get("referral_attempts", 0) > 0
        and not profile.get("referral_got_reply", False)
    )

    return {
        "history": history,
        "profile": profile,
        "history_text": history_text,
        "profile_text": profile_text,
        "facts": facts,
        "facts_text": facts_text,
        "hint_text": hint_text,
        "should_block_referral": should_block_referral,
    }
