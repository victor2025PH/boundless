# -*- coding: utf-8 -*-
"""Messenger 聊天意图分类器 (P4 — B 机聊天机器人专用)。

和已存在的 ``src/ai/intent_classifier.py`` (A 机 TikTok 用) 区分:
  * 这里的 ``intent`` 聚焦 "Messenger 对话决策" 场景 (引流时机/冷场识别等)
  * 那里的 ``Intent`` 聚焦 "lead 获取漏斗归类" 场景 (INTERESTED/MEETING/REFERRAL 等)

设计原则:
  * **Rule-first**: referral_ask / buying / cold / closing / opening 这些
    有明显词法特征的意图,用关键词+正则直接判,~60% 对话零 LLM 调用完成分类。
  * **LLM fallback**: smalltalk / interest / objection 语义边界模糊的才调 LLM,
    轻量 prompt < 400 token。
  * **Graceful**: LLM 不可用时降级返回 ``smalltalk`` + confidence=0.3,不中断主流程。
  * **Multilingual**: ja / zh-CN / en / it 四种语言的规则均覆盖 (和 B 机
    Messenger 自动化的已知 persona 对齐)。

输出对下游决策的价值:
  * intent=buying / referral_ask   → P5 引流时机规则闸触发 wa_referral
  * intent=cold                     → _ai_reply_and_send 可 skip / 发短句
  * intent=objection                → LLM prompt 加"温和回应消除疑虑"
  * intent=closing                  → 发简短告别
  * intent=opening / smalltalk / interest → 走常规 reply 流程
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 契约: 8 类意图 (稳定公开,改名需先 INTEGRATION_CONTRACT §二 约定)
# ─────────────────────────────────────────────────────────────────────────────

INTENTS = (
    "opening",      # 开场破冰 (peer 首次发言)
    "smalltalk",    # 日常闲聊 (无明显意图)
    "interest",     # 表现兴趣 (问产品/服务细节)
    "objection",    # 反对/疑虑 (价格/怀疑/犹豫)
    "buying",       # 强购买信号 (报价/下单)
    "referral_ask", # 对方主动要联系方式 (WhatsApp/LINE 等)
    "closing",      # 对话收尾 (告别)
    "cold",         # 冷场 (单字/表情/无内容)
)
"""稳定契约 — 下游 (P5 引流规则闸) 按这 8 个 tag 做决策。"""


@dataclass
class ChatIntentResult:
    intent: str           # 必须 ∈ INTENTS
    confidence: float     # 0.0-1.0 (rule 命中高置信,LLM 由模型给出)
    source: str           # "rule" | "llm" | "fallback"
    reason: str = ""      # 规则命中原因 / LLM 简短解释


# ─────────────────────────────────────────────────────────────────────────────
# Rule patterns (pre-compiled, 多语言)
# ─────────────────────────────────────────────────────────────────────────────

# referral_ask: 对方主动要求加其他联系方式
_REFERRAL_RE = re.compile(
    # whatsapp / wa / line / telegram / 微信 / 联系方式 / 連絡 / contatto
    r"\b(whats[\s\-]?app|\bwa\b|line(?![a-z])|telegram|viber|"
    r"signal(?![a-z])|skype|wechat|qq号?)\b"
    r"|微信|\b(加|换|给我)你的?|联系方式|联系(方式|电话)|"
    r"(LINE|ライン|連絡先)|"
    # 2026-04-24 batch_dryrun 矩阵跑发现意大利语覆盖不足,扩充:
    # - 原 "numero di telefono" 不覆盖 "qual è il tuo numero" / "il tuo numero"
    # - contatt[io] 已覆盖 "contatto"
    r"contatt[io]|numero di (telefono|cellulare)|"
    r"\b(qual|dammi|dimmi)\b[^.?!]{0,20}\b(numero|contatto)\b|"
    r"\b(il tuo|tuo) (numero|contatto|whatsapp|telefono)\b|"
    r"swap contact|exchange contact",
    re.IGNORECASE,
)

# buying: 强购买信号 / 询价 / 下单
_BUYING_RE = re.compile(
    r"多少钱|价格|报价|怎么买|怎么下单|订购|下单|购买|付款|"
    r"\b(price|cost|how much|purchase|buy(?:ing)?|order|checkout|"
    r"quote|pay(?:ment)?)\b|"
    r"(値段|価格|購入|注文|予約|支払い)|"
    r"\b(quanto costa|prezzo|comprare|acquisto|pagamento)\b",
    re.IGNORECASE,
)

# closing: 告别
_CLOSING_RE = re.compile(
    r"再见|回聊|晚安|早休息|明天聊|回头聊|拜拜|"
    r"\b(bye(?:bye)?|goodbye|good night|see you|later|talk later|"
    r"gotta go|have to go|take care)\b|"
    r"(またね|バイバイ|おやすみ|また明日|また今度|では)|"
    r"\b(ciao|a presto|buona notte|a dopo|arrivederci)\b",
    re.IGNORECASE,
)

# "cold" 特殊处理 — 长度 / 纯表情 / 单字贫瘠回应
_COLD_TOKENS = frozenset({
    "ok", "okay", "okk", "hmm", "hm", "uh", "uhh", "yup", "yep", "nope",
    "yes", "no", "sure", "cool", "nice", "haha", "lol", "lmao",
    "嗯", "哦", "好", "行", "对", "是", "不", "额", "啊",
    "はい", "いいえ", "そう", "うん", "ああ",
    "si", "sì", "boh",
})

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F]"
)


def _is_cold(text: str) -> bool:
    """判 cold: 极短 / 只有表情 / 或仅含贫瘠应答 token。"""
    s = (text or "").strip()
    if not s:
        return True
    # 去掉所有表情后还剩的内容
    no_emoji = _EMOJI_RE.sub("", s).strip()
    if not no_emoji:
        return True  # 全表情
    lower = no_emoji.lower()
    if len(no_emoji) <= 3:
        # 极短 — 进一步看是不是贫瘠 token
        if lower in _COLD_TOKENS:
            return True
        # 纯标点/符号
        if re.fullmatch(r"[\W_]+", no_emoji, re.UNICODE):
            return True
        # 1-2 个 CJK 字符 (单字"嗯"/"哦"类,但已在 _COLD_TOKENS 也命中)
        if re.fullmatch(r"[一-鿿ぁ-んァ-ン]{1,2}", no_emoji):
            return True
    # 多 token 但全是贫瘠 token (如 "ok ok", "嗯嗯")
    tokens = re.split(r"[\s,.!?~。,!?]+", lower)
    tokens = [t for t in tokens if t]
    if tokens and all(t in _COLD_TOKENS for t in tokens):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based first pass
# ─────────────────────────────────────────────────────────────────────────────

def _rule_classify(text: str,
                   history: List[Dict[str, Any]]) -> Optional[ChatIntentResult]:
    """按优先级规则判意图,命中返回 ChatIntentResult,否则 None 让 LLM 接手。

    优先级 (高到低):
      1. opening       — peer 从未发言 (history 中无 incoming)
      2. cold          — 极短/表情/贫瘠 token (优先级高,避免被其他词表误伤)
      3. referral_ask  — 对方要联系方式
      4. buying        — 强购买信号
      5. closing       — 告别
    interest / objection / smalltalk 交给 LLM。
    """
    s = (text or "").strip()

    # 1. opening: peer 从未发言
    peer_turns = [r for r in (history or []) if r.get("direction") == "incoming"]
    if not peer_turns:
        return ChatIntentResult(
            intent="opening", confidence=0.95,
            source="rule", reason="peer 首次发言")

    # 2. cold 优先 (避免 "nice" 被误判 smalltalk 而遗漏冷场信号)
    if _is_cold(s):
        return ChatIntentResult(
            intent="cold", confidence=0.9,
            source="rule", reason="极短/表情/贫瘠 token")

    # 3. referral_ask
    m = _REFERRAL_RE.search(s)
    if m:
        return ChatIntentResult(
            intent="referral_ask", confidence=0.9,
            source="rule",
            reason=f"匹配联系方式关键词: {m.group()[:20]}")

    # 4. buying
    m = _BUYING_RE.search(s)
    if m:
        return ChatIntentResult(
            intent="buying", confidence=0.85,
            source="rule",
            reason=f"匹配购买/询价关键词: {m.group()[:20]}")

    # 5. closing
    m = _CLOSING_RE.search(s)
    if m:
        return ChatIntentResult(
            intent="closing", confidence=0.85,
            source="rule",
            reason=f"匹配告别关键词: {m.group()[:20]}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# LLM fallback
# ─────────────────────────────────────────────────────────────────────────────

_LLM_PROMPT = """You classify a single Messenger message from a user into ONE
of these intents: smalltalk, interest, objection. Output ONLY a JSON object.

Definitions:
- smalltalk: casual chitchat, greetings, weather, compliments, no product intent
- interest: asks about your product/service/availability/details (but not yet
  asking for price or to buy)
- objection: expresses doubt, concern, price resistance, hesitation, complaint

Return JSON:
{"intent": "<one of above>",
 "confidence": <float 0..1>,
 "reason": "<short phrase>"}"""


def _llm_classify(text: str,
                  history: List[Dict[str, Any]],
                  lang_hint: str = "") -> Optional[ChatIntentResult]:
    """调 LLM 精排模糊意图 (smalltalk / interest / objection)。失败返回 None。"""
    try:
        from src.ai.llm_client import LLMClient
        client = LLMClient()
    except Exception as e:
        logger.debug("[chat_intent] LLMClient 不可用: %s", e)
        return None

    # 组装简洁 history context (最近 3 轮即可,省 token)
    ctx_lines: List[str] = []
    for r in (history or [])[-3:]:
        tag = "User" if r.get("direction") == "incoming" else "Bot"
        snippet = (r.get("message_text") or "").strip()[:80]
        if snippet:
            ctx_lines.append(f"{tag}: {snippet}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(no prior turns)"

    user_msg = (
        f"Prior turns (most recent last):\n{ctx_block}\n\n"
        f"Current message from user: {text[:400]}\n"
    )
    if lang_hint:
        user_msg += f"Language hint: {lang_hint}\n"

    try:
        resp = client.chat_with_system(
            system=_LLM_PROMPT,
            user=user_msg,
            temperature=0.1,
            max_tokens=120,
        )
        if not resp:
            return None
        s = resp.strip()
        if s.startswith("```"):
            s = "\n".join(ln for ln in s.splitlines()
                          if not ln.strip().startswith("```"))
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
        data = json.loads(s)
    except Exception as e:
        logger.debug("[chat_intent] LLM 调用/解析失败: %s", e)
        return None

    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("smalltalk", "interest", "objection"):
        return None
    try:
        conf = float(data.get("confidence", 0.6))
    except Exception:
        conf = 0.6
    conf = max(0.0, min(1.0, conf))
    return ChatIntentResult(
        intent=intent,
        confidence=conf,
        source="llm",
        reason=str(data.get("reason", ""))[:120],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 对外主入口
# ─────────────────────────────────────────────────────────────────────────────

def classify_intent(text: str, *,
                    history: Optional[List[Dict[str, Any]]] = None,
                    lang_hint: str = "",
                    use_llm_fallback: bool = True) -> ChatIntentResult:
    """主入口 — 返回结构化 ChatIntentResult,永远有结果 (graceful)。

    流程:
      1. Rule-based first pass (opening/cold/referral_ask/buying/closing) → 命中直接返
      2. LLM fallback (smalltalk/interest/objection) → 命中返 source=llm
      3. 都失败 → fallback 返 smalltalk + confidence=0.3 (不中断主流程)

    Args:
        text: 当前 incoming 消息
        history: 来自 ``chat_memory.get_history`` 的历史 (用于判 opening + LLM context)
        lang_hint: 对方语言 (影响 LLM 分类时 reason 语种;rule pass 不需要)
        use_llm_fallback: 关掉可跳过 LLM,省成本但 smalltalk/interest/objection 全返 smalltalk
    """
    history = list(history or [])
    rule_result = _rule_classify(text, history)
    if rule_result is not None:
        return rule_result

    if use_llm_fallback:
        llm_result = _llm_classify(text, history, lang_hint)
        if llm_result is not None:
            return llm_result

    # 保底: 非规则命中 + LLM 失败/关闭 → smalltalk 低置信
    return ChatIntentResult(
        intent="smalltalk",
        confidence=0.3,
        source="fallback",
        reason="rule 未命中且 LLM 不可用/关闭",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 下游决策辅助
# ─────────────────────────────────────────────────────────────────────────────

def should_trigger_referral(intent: str) -> bool:
    """P5 引流规则闸的参考信号 — 这些 intent 应该提高引流倾向。"""
    return intent in ("buying", "referral_ask")


def format_intent_for_llm_hint(result: ChatIntentResult) -> str:
    """把 ChatIntentResult 拼成 system prompt 片段,供 _ai_reply_and_send 注入
    ab_style_hint 让生成 LLM 知道当前轮意图。"""
    if not result or result.intent == "smalltalk":
        # smalltalk 是默认状态,不值得占 prompt
        return ""
    tag = {
        "opening":      "【当前轮意图】opening(首轮破冰) — 保持 persona 预设口吻",
        "interest":     "【当前轮意图】interest(对方表现兴趣) — 可简短介绍并回问一个探索性问题",
        "objection":    "【当前轮意图】objection(对方有疑虑) — 先共情,温和回应,不要立刻推进",
        "buying":       "【当前轮意图】buying(强购买信号) — 可引流到主私域(LINE/WhatsApp)",
        "referral_ask": "【当前轮意图】referral_ask(对方主动要联系方式) — 直接给对应渠道 ID",
        "closing":      "【当前轮意图】closing(对方告别) — 简短友好回应,不要再开新话题",
        "cold":         "【当前轮意图】cold(对方冷场) — 本轮可短回一句或跳过",
    }.get(result.intent, "")
    return tag
