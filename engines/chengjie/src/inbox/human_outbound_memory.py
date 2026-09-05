# -*- coding: utf-8 -*-
"""人工接管期间坐席以人设身份发出的话 → 进人设自述记忆（#177，2026-09-05）。

事故（skuio Cursor 报告 66SFWD / 32HB5P）：WhatsApp Olivia ↔ BABY BEAR 中途人工接管
回复（告知有女儿、邀请未来同住），切回全自动后 AI 只记得自己发过的，不记得坐席替
它说过的——窗口一滚（B 线 ``list_recent_messages(limit=30)``，该会话 13 分钟 14 轮
拟稿，30 条只够十来分钟）就自相矛盾。

现状：``persona_reply`` 把 ``direction=out`` 一律映射成 ``assistant`` → 人工出站
**在历史窗口内可见**；出窗后靠记忆，而所有记忆钩子（episodic 抽取 / ``last_reply``
环 / 自述状态）都只挂在 **AI 回复之后**（``skill_manager._update_after_reply``），
手动发送路由（``record_agent_send`` 成功分支）不触发任何记忆钩子。

本模块＝手动发送成功后的**一次调用**，零阻断发送（任何异常吞掉只记 debug）：

1. ``last_reply`` / ``last_reply_time`` / ``recent_replies`` 环（防复读 + 「上轮我
   说了什么」与真实出站一致）；
2. ``record_self_state``（B52 短期自述状态：要睡了/去健身…，与 AI 侧同口径）；
3. **人设自述事实**（本模块新增，``user_context["_human_said_log"]`` 持久、无 TTL、
   bounded）：保守正则只认第一人称、现在时、高置信的自述（家庭成员/居住地/年龄/
   婚姻/职业/同住邀约/明确喜好），排除否定/疑问/转述/假设；每条**必须过
   ``memory_grounding`` 接地护栏**（与 AI 侧同口径，事实必须锚定在原话上，宁漏勿错）。
   注入＝``skill_manager._inject_self_state`` 每轮把 :func:`human_said_note` 拼进
   ``_self_state_block``（A/B 两线同经此口）。

刻意**不写 episodic 用户事实库**：那张表的语义是「关于对方的事实」（prompt 标题
「用户长期记忆要点」、``source`` 只有 user_stated/ai_inferred、启发式抽取器全部
以「用户…」开头）——坐席替人设说的「我有个女儿」若落进去，下轮就会被当成
「对方有个女儿」注入，正是 Phase8 幻觉家族（AI 自述被抽成用户事实）的镜像事故。
``author=human`` 标记落在 ``_human_said_log`` 条目上（``author`` 字段），与 AI 侧
``_self_state_log``（author 隐含 ai）并列成两路人设自述来源。

语音/媒体人工出站只记「[语音]/[图片]」占位进 ``last_reply``，不抽事实。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOG_KEY = "_human_said_log"
_LOG_CAP = 8
_QUOTE_MAX = 120
_TEXT_MAX = 2000

# ── 自述事实抽取（第一人称、现在时、高置信；宁漏勿错）─────────────────────────
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?\n;；]+|(?<=[a-zA-Z0-9\)])\.(?=\s|$)")

_ZH_FACT_PATTERNS = [
    # 家庭成员/宠物：我有(一)个女儿 / 我家有两只猫
    re.compile(
        r"我(?:家)?(?:有|养了|養了)\s*(?:一|两|兩|三|四|1|2|3|4|个|個|只|隻)?\s*(?:个|個|只|隻|位)?\s*"
        r"(?:小)?(?:女儿|女兒|儿子|兒子|孩子|小孩|闺女|閨女|娃|宝宝|寶寶|猫|貓|狗|"
        r"弟弟|妹妹|哥哥|姐姐|老公|老婆|男朋友|女朋友|双胞胎|雙胞胎)"),
    # 居住/来处（不收裸「我在」——「我在忙」会误伤）
    re.compile(r"我(?:现在|現在|目前|老家)?(?:住在|住|来自|來自|老家在|家在|搬到了?)\s*"
               r"[\u4e00-\u9fff]{2,10}"),
    # 年龄
    re.compile(r"我(?:今年|现在|現在|都)?\s*\d{2}\s*岁"),
    # 婚姻状况
    re.compile(r"我(?:是|现在|現在|已经|已經)?\s*(?:单身|單身|离过婚|離過婚|离婚了|離婚了|"
               r"结婚了|結婚了|已婚|未婚|离异|離異|丧偶|喪偶|一个人住|一個人住)"),
    # 职业（只认「工作/职业是」定式）
    re.compile(r"我(?:的)?(?:工作|职业|職業)是\s*[\u4e00-\u9fff]{2,12}"),
    # 同住/未来邀约（实录：邀请未来同住）
    re.compile(r"(?:以后|以後|将来|將來|等|到时候|到時候|有机会|有機會|下次)[^，。！？!?\n]{0,14}"
               r"(?:一起住|一起生活|搬(?:过|過)?来(?:和|跟)我住|住到一起|住在一起|同居|"
               r"来我这(?:儿|里)住|來我這(?:兒|裡)住)"),
    re.compile(r"(?:搬(?:过|過)?来(?:和|跟)我(?:一起)?住|来(?:和|跟)我一起住|"
               r"來(?:和|跟)我一起住|我们一起住|我們一起住|"
               r"来我这(?:儿|里|边|裡)住|來我這(?:兒|裡|邊)住)"),
    # 明确喜好（排除对象是「你」的情话，那不是画像事实）
    re.compile(r"我(?:最|特别|特別|超|很|真的)?(?:喜欢|喜歡|爱吃|愛吃|讨厌|討厭|不喜欢|不喜歡)\s*"
               r"(?!你|妳|您|这样|這樣|这个|這個|那样|那樣)[\u4e00-\u9fff]{1,10}"),
]

_EN_FACT_PATTERNS = [
    re.compile(
        r"\bI(?:'ve|’ve| have)\s+(?:got\s+)?(?:a|an|one|two|three|\d)\s+"
        r"(?:(?:little|young|teenage|grown|adult|older|younger|baby)\s+)?"
        r"(?:daughters?|sons?|kids?|children|child|boys?|girls?|dogs?|cats?|puppy|kitten|"
        r"brothers?|sisters?|twins)\b", re.I),
    re.compile(r"\bmy\s+(?:little\s+|young\s+|teenage\s+|baby\s+)?"
               r"(?:daughter|son|kids?|children|dog|cat|husband|wife|boyfriend|girlfriend)\b",
               re.I),
    re.compile(r"\bI(?:'m|’m| am)\s+(?:originally\s+)?from\s+[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,2}"),
    re.compile(r"\bI\s+live\s+in\s+[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,2}"),
    re.compile(r"\bI(?:'m|’m| am)\s+\d{2}(?:\s+years?\s+old)?\b"),
    re.compile(r"\bI(?:'m|’m| am)\s+(?:single|divorced|married|widowed|separated)\b", re.I),
    re.compile(r"\bI\s+work\s+as\s+(?:a|an)\s+[a-z][\w -]{2,24}", re.I),
    re.compile(r"\b(?:move\s+in\s+with\s+me|come\s+live\s+with\s+me|live\s+together|"
               r"live\s+with\s+me|you\s+(?:could|can|should)\s+move\s+in|"
               r"we\s+(?:could|can|should|will|'ll)\s+live\s+together)\b", re.I),
    re.compile(r"\bI\s+(?:really\s+|absolutely\s+)?(?:love|hate|can(?:'|’)?t\s+stand)\s+"
               r"(?!you\b|u\b|it\b|that\b|this\b|how\b|when\b|the\s+way\b)[a-z][\w' -]{2,20}", re.I),
]

# 排除：否定/假设/转述/过去已不成立/疑问
_EXCLUDE_RE = re.compile(
    r"没有|沒有|不是|要是|如果|假如|假设|假設|曾经|曾經|以前有|本来|本來|骗你|騙你|开玩笑|開玩笑|"
    r"你说|你說|他说|他說|她说|她說|听说|聽說|"
    r"\b(?:if|wish|used\s+to|no\s+longer|don(?:'|’)?t|doesn(?:'|’)?t|didn(?:'|’)?t|not|never|"
    r"kidding|joking|he\s+said|she\s+said|you\s+said|they\s+said|pretend|imagine|what\s+if)\b",
    re.I,
)
_QUESTION_TAIL_RE = re.compile(r"[?？]\s*$")


def _clauses(text: str) -> List[str]:
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(str(text or "")) if c and c.strip()]


def extract_self_facts(text: str) -> List[str]:
    """坐席（以人设身份）发出的文本 → 人设自述事实短句列表（原话子句，≤120 字）。

    只认高置信第一人称自述；每条以**子句原文**为内容（不改写、不换主语——改写
    就有再次幻觉的机会，原话本身就是最可靠的记忆）。认不出返回 []。
    """
    t = str(text or "").strip()
    if not t or len(t) > _TEXT_MAX:
        return []
    out: List[str] = []
    seen = set()
    for clause in _clauses(t):
        if len(clause) < 2 or _QUESTION_TAIL_RE.search(clause):
            continue
        if _EXCLUDE_RE.search(clause):
            continue
        hit = any(p.search(clause) for p in _ZH_FACT_PATTERNS) or any(
            p.search(clause) for p in _EN_FACT_PATTERNS)
        if not hit:
            continue
        fact = clause[:_QUOTE_MAX]
        k = re.sub(r"\s+", "", fact).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(fact)
    return out


def _grounded(facts: List[str], source_text: str) -> List[str]:
    """接地护栏（与 AI 侧 ``_ground_extracted_fact_items`` 同口径，J-10 A1 引文级）：
    这里的事实**就是原话子句**，故以自身为引文——归一化后必须是原话子串（或 token
    重叠 ≥60%），语言无关；坐席用英文替人设说的话同样过得去。"""
    try:
        from src.ai.memory_grounding import ground_fact_items
        items = [{"text": f, "evidence": f} for f in (facts or []) if str(f or "").strip()]
        kept, dropped = ground_fact_items(items, source_text)
        if dropped:
            logger.info("[human_memory] 接地护栏丢弃 %d 条未锚定事实", len(dropped))
        return [str(k["text"]) for k in kept if k.get("text")]
    except Exception:
        return []   # 护栏自身异常 → 宁漏勿错：不落库


def record_human_said(
    user_context: Dict[str, Any], facts: List[str], *, quote: str = "",
    now: Optional[float] = None,
) -> int:
    """把已接地的自述事实写进 ``_human_said_log``（去重、bounded）。返回新增条数。"""
    if not isinstance(user_context, dict) or not facts:
        return 0
    ts = float(now if now is not None else time.time())
    log = user_context.get(LOG_KEY)
    if not isinstance(log, list):
        log = []
    known = {re.sub(r"\s+", "", str((e or {}).get("fact") or "")).lower() for e in log}
    added = 0
    for f in facts:
        f = str(f or "").strip()[:_QUOTE_MAX]
        if not f:
            continue
        k = re.sub(r"\s+", "", f).lower()
        if k in known:
            continue
        known.add(k)
        log.append({"ts": ts, "fact": f, "author": "human",
                    "quote": str(quote or "")[:_QUOTE_MAX]})
        added += 1
    if added:
        user_context[LOG_KEY] = log[-_LOG_CAP:]
    return added


def human_said_note(user_context: Dict[str, Any]) -> str:
    """供 prompt 注入的块（``_inject_self_state`` 拼进 ``_self_state_block``）；无记录 → ''。"""
    try:
        log = user_context.get(LOG_KEY) if isinstance(user_context, dict) else None
        if not isinstance(log, list) or not log:
            return ""
        lines: List[str] = []
        for e in log[-_LOG_CAP:]:
            f = str((e or {}).get("fact") or "").strip()
            if not f:
                continue
            ts = float((e or {}).get("ts") or 0)
            stamp = time.strftime("%m-%d", time.localtime(ts)) if ts > 0 else ""
            lines.append(f"- {stamp + ' ' if stamp else ''}{f}")
        if not lines:
            return ""
        return (
            "【你亲口对 TA 说过的事（人工接管期间由坐席以你的身份发出，是你们之间"
            "的既定事实：必须当真、后续不得矛盾或装作没说过；只在自然相关时提及）】\n"
            + "\n".join(lines)
        )
    except Exception:
        return ""


def _effective_account_id(account_id: str, conversation_id: str) -> str:
    """与 ``generate_inbox_draft`` 同口径：空 / ``default`` 时从 ``platform:acct:chat``
    的 conversation_id 补出真实账号——记忆键必须和 B 线拟稿读的是同一个桶。"""
    acct = str(account_id or "").strip()
    if (not acct or acct == "default") and conversation_id:
        parts = str(conversation_id).split(":", 2)
        if len(parts) >= 3 and parts[1]:
            acct = str(parts[1]).strip()
    return acct


def resolve_skill_manager(app_state: Any) -> Any:
    """从 ``app.state`` 找 SkillManager：直挂 ``skill_manager`` → ``telegram_client.skill_manager``。"""
    try:
        sm = getattr(app_state, "skill_manager", None)
        if sm is not None:
            return sm
        tc = getattr(app_state, "telegram_client", None)
        return getattr(tc, "skill_manager", None) if tc is not None else None
    except Exception:
        return None


def on_human_outbound(
    platform: str, account_id: str, chat_key: str, text: str, *,
    conversation_id: str = "", persona_id: str = "", channel: str = "inbox",
    media_type: str = "", skill_manager: Any = None, now: Optional[float] = None,
    sent_text: str = "",
) -> Dict[str, Any]:
    """手动发送成功后调用（``record_agent_send`` 之后一行）。绝不抛、零阻断发送。

    ``text``＝坐席敲的原文（事实抽取 + 接地都对它做）；``sent_text``＝经出站翻译
    后实际发给客户的文本（非空时用它填 ``last_reply`` 环——防复读要对照客户真看到
    的那句）。返回 ``{"ok", "facts", "reason"}`` 供测试/观测。``skill_manager``
    为 None（app.state 未装配）时直接跳过（reason=no_skill_manager）。
    """
    res: Dict[str, Any] = {"ok": False, "facts": [], "reason": ""}
    try:
        sm = skill_manager
        if sm is None:
            res["reason"] = "no_skill_manager"
            return res
        get_ctx = getattr(sm, "_get_user_context", None)
        store = getattr(sm, "_context_store", None)
        if not callable(get_ctx) or store is None:
            res["reason"] = "no_context_store"
            return res
        peer = str(chat_key or "").strip()
        if not peer:
            res["reason"] = "no_chat_key"
            return res
        acct = _effective_account_id(account_id, conversation_id)
        ctx = get_ctx(peer, account_id=acct)
        if not isinstance(ctx, dict):
            res["reason"] = "bad_context"
            return res
        ts = float(now if now is not None else time.time())
        is_text = str(channel or "inbox") == "inbox" and not media_type
        body = str(text or "").strip()
        if not is_text:
            body = "[语音]" if str(media_type or "") in ("voice", "audio") else "[图片]"
        if not body:
            res["reason"] = "empty"
            return res
        shown = (str(sent_text or "").strip() or body) if is_text else body
        # 1) last_reply 环（与 _update_after_reply 同键；记客户实际看到的那句）
        ctx["last_reply"] = shown[:500]
        ctx["last_reply_time"] = ts
        ctx["_last_human_outbound_ts"] = ts
        try:
            push = getattr(sm, "_push_recent_reply", None)
            if callable(push):
                push(ctx, shown)
        except Exception:
            pass
        facts: List[str] = []
        if is_text:
            # 2) 短期自述状态（要睡了/去健身…）
            try:
                from src.companion.self_state import record_self_state
                record_self_state(ctx, body, now=ts)
            except Exception:
                pass
            # 2b) J-10 二期：坐席替人设做的承诺（「明天给你打电话」）进承诺账本，author=human
            try:
                from src.utils.memory_promises import record_promises
                record_promises(ctx, body, author="human", now=ts)
            except Exception:
                pass
            # 3) 人设自述事实 → 接地护栏 → 持久 log
            facts = _grounded(extract_self_facts(body), body)
            if facts:
                record_human_said(ctx, facts, quote=body, now=ts)
        # 持久化（ContextStore 键由 _get_user_context 挂在 ctx 上）
        key = str(ctx.get("_context_store_key") or "")
        try:
            if key:
                store.mark_dirty(key)
                store.flush(key)
        except Exception:
            logger.debug("[human_memory] 持久化失败（已忽略）", exc_info=True)
        res.update({"ok": True, "facts": facts})
        if facts:
            logger.info(
                "[human_memory] 人工出站入记忆 platform=%s acct=%s facts=%d",
                platform, acct or "-", len(facts))
        return res
    except Exception:
        logger.debug("[human_memory] on_human_outbound 异常（已忽略）", exc_info=True)
        res["reason"] = "error"
        return res


def record_human_outbound(
    app_state: Any, platform: str, account_id: str, chat_key: str, text: str, *,
    conversation_id: str = "", channel: str = "inbox", media_type: str = "",
    sent_text: str = "",
) -> Dict[str, Any]:
    """路由侧一行调用：自己从 ``app.state`` 解析 SkillManager，其余同
    :func:`on_human_outbound`。绝不抛。"""
    try:
        return on_human_outbound(
            platform, account_id, chat_key, text,
            conversation_id=conversation_id, channel=channel, media_type=media_type,
            sent_text=sent_text, skill_manager=resolve_skill_manager(app_state))
    except Exception:
        return {"ok": False, "facts": [], "reason": "error"}


__all__ = [
    "LOG_KEY", "extract_self_facts", "record_human_said", "human_said_note",
    "on_human_outbound", "record_human_outbound", "resolve_skill_manager",
]
