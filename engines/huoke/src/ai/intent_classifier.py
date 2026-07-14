"""
Intent Classifier — analyzes incoming messages to determine lead intent
and automatically decide the next action in the acquisition pipeline.

Intent Categories:
  INTERESTED    — lead shows interest, wants to know more
  QUESTION      — lead asks a question (needs answer)
  POSITIVE      — generic positive response (thanks, great, etc.)
  NEGATIVE      — explicit rejection or disinterest
  SPAM          — irrelevant or automated response
  NEUTRAL       — ambiguous, no clear signal
  MEETING       — wants to schedule a call/meeting
  REFERRAL      — refers to someone else
  UNSUBSCRIBE   — wants to stop receiving messages

Pipeline Integration:
  message_received → classify_intent → update_lead_status → trigger_next_action
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class Intent(str, Enum):
    INTERESTED = "interested"
    QUESTION = "question"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    SPAM = "spam"
    NEUTRAL = "neutral"
    MEETING = "meeting"
    REFERRAL = "referral"
    UNSUBSCRIBE = "unsubscribe"


INTENT_PRIORITY = {
    Intent.MEETING: 10,
    Intent.INTERESTED: 9,
    Intent.QUESTION: 8,
    Intent.REFERRAL: 7,
    Intent.POSITIVE: 5,
    Intent.NEUTRAL: 3,
    Intent.NEGATIVE: 1,
    Intent.UNSUBSCRIBE: 0,
    Intent.SPAM: -1,
}

NEXT_ACTION_MAP = {
    Intent.INTERESTED: "send_detailed_info",
    Intent.QUESTION: "answer_question",
    Intent.POSITIVE: "follow_up_gentle",
    Intent.MEETING: "schedule_meeting",
    Intent.REFERRAL: "contact_referral",
    Intent.NEUTRAL: "send_follow_up",
    Intent.NEGATIVE: "respect_and_pause",
    Intent.UNSUBSCRIBE: "blacklist",
    Intent.SPAM: "ignore",
}


@dataclass
class ClassificationResult:
    intent: Intent
    confidence: float
    reasoning: str = ""
    next_action: str = ""
    keywords: List[str] = None
    processing_time_ms: float = 0.0

    def __post_init__(self):
        if not self.next_action:
            self.next_action = NEXT_ACTION_MAP.get(self.intent, "")
        if self.keywords is None:
            self.keywords = []

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "next_action": self.next_action,
            "keywords": self.keywords,
            "processing_time_ms": self.processing_time_ms,
        }


# ── Rule-based patterns (fast, no API cost) ──────────────────────────────

_INTERESTED_PATTERNS = [
    # 「not interested」中含单词 interested，须排除 not 前缀，否则会压过 NEGATIVE
    r"(?<!not )\binterested\b",
    r"\b(tell me more|sounds? good|love to|would like|want to know)\b",
    r"\b(can you|could you|please send|share more|more info|details)\b",
    r"\b(how much|pricing|cost|rate|budget)\b",
    r"\b(sign me up|count me in|i'm in|let's do it|let's go)\b",
    r"(感兴趣|想了解|告诉我更多|多少钱|怎么收费|想知道|有兴趣)",
    # 意大利语兴趣信号
    r"\b(interessato|interessante|voglio sapere|dimmi di più|come funziona)\b",
    r"\b(quanto costa|che prezzo|vorrei sapere|puoi dirmi)\b",
    r"\b(mostrami|spiegami|raccontami)\b",
]

_QUESTION_PATTERNS = [
    r"\?$",  # 仅保留一个问号匹配，去掉重复的\?\s*$
    r"^(what|how|when|where|why|who|which|can|could|would|is|are|do|does)\b",
    r"(什么|怎么|哪里|为什么|谁|能不能|可以吗|请问|吗\s*$|呢\s*$)",
    # 意大利语疑问词
    r"^(cosa|come|quando|dove|perché|chi|quale|puoi|potresti|hai|sei|sai)\b",
    r"\b(cosa fai|come mai|dove sei|chi sei|cosa vendi|come si fa)\b",
]

_NEGATIVE_PATTERNS = [
    r"\b(not interested|no thanks|no thank you|don't contact|stop messaging)\b",
    r"\b(leave me alone|go away|unsubscribe|remove me)\b",
    r"(不需要|不感兴趣|别联系|不要|别打扰|别发了)",
    # 意大利语拒绝信号
    r"\b(non mi interessa|non voglio|lasciami stare|smettila|basta così)\b",
    r"\b(no grazie|non grazie|non ho bisogno|vai via|non disturbarmi)\b",
    r"\b(spam|segnala|blocco|ti blocco)\b",
]

_MEETING_PATTERNS = [
    r"\b(meet|call|zoom|schedule|calendar|availability|let's chat|coffee)\b",
    r"(见面|通话|约个时间|视频会议|打个电话|聊聊)",
    # 意大利语会面信号
    r"\b(ci vediamo|incontriamoci|chiamata|videocall|appuntamento)\b",
    r"\b(possiamo parlare|sentiamoci|facciamo una call)\b",
]

# NEW: 高意图联系方式请求 → 立即引流
_REFERRAL_PATTERNS = [
    r"\b(telegram|whatsapp|signal|wa\.me)\b",
    r"\b(instagram|facebook|fb\.me|t\.me)\b",
    r"\b(contattami|scrivimi|trovami|contatto|contatti)\b",
    r"\b(come ti contatto|dove ti trovo|hai un contatto)\b",
    r"\b(mandami il link|dammi il link|inviami il link)\b",
    r"\b(hai telegram|hai whatsapp|sei su telegram)\b",
    r"\b(numero di telefono|tel\.|cellulare|numero)\b",
    r"\b(dove posso trovarti|come posso contattarti)\b",
    r"\b(add me|follow me|link me|send link|contact info)\b",
]

_SPAM_PATTERNS = [
    r"\b(click here|buy now|discount|free money|lottery|winner)\b",
    r"^[\s\U0001F600-\U0001F64F]+$",
]


class IntentClassifier:
    """
    Hybrid intent classifier: rule-based (fast) + LLM (accurate).

    Rule-based runs first (0 cost, <1ms). If confidence < threshold,
    falls back to LLM for nuanced classification.
    """

    def __init__(self, llm_fallback_threshold: float = 0.7):
        self._threshold = llm_fallback_threshold
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            try:
                from .llm_client import get_llm_client
                self._llm = get_llm_client()
            except Exception:
                pass
        return self._llm

    def classify(self, message: str, context: Optional[Dict[str, Any]] = None) -> ClassificationResult:
        """
        Classify message intent.

        Args:
            message: the received message text
            context: optional context (platform, sender, conversation_history)

        Returns: ClassificationResult with intent, confidence, and next_action
        """
        start = time.time()

        # Phase 1: Rule-based (fast)
        result = self._rule_based(message)

        # REFERRAL和NEGATIVE是高确定性意图：有模式匹配就直接返回，不允许LLM覆盖
        if result.intent in (Intent.REFERRAL, Intent.NEGATIVE) and result.confidence >= 0.35:
            result.confidence = 0.88  # 提升为高置信度，跳过LLM
            result.processing_time_ms = round((time.time() - start) * 1000, 1)
            return result

        # Phase 2: LLM fallback for ambiguous cases
        if result.confidence < self._threshold and self.llm:
            llm_result = self._llm_classify(message, context)
            if llm_result and llm_result.confidence > result.confidence:
                result = llm_result

        result.processing_time_ms = round((time.time() - start) * 1000, 1)
        return result

    def _rule_based(self, message: str) -> ClassificationResult:
        """Fast rule-based classification using regex patterns."""
        msg = message.strip().lower()
        if not msg or len(msg) < 2:
            return ClassificationResult(Intent.SPAM, 0.9, "empty or too short")

        # Check each category
        scores: Dict[Intent, float] = {}
        keywords: List[str] = []

        for pattern in _NEGATIVE_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                scores[Intent.NEGATIVE] = scores.get(Intent.NEGATIVE, 0) + 0.4
                keywords.append("negative_pattern")

        for pattern in _MEETING_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                scores[Intent.MEETING] = scores.get(Intent.MEETING, 0) + 0.4
                keywords.append("meeting_pattern")

        for pattern in _INTERESTED_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                scores[Intent.INTERESTED] = scores.get(Intent.INTERESTED, 0) + 0.75
                keywords.append("interest_pattern")

        for pattern in _QUESTION_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                scores[Intent.QUESTION] = scores.get(Intent.QUESTION, 0) + 0.3
                keywords.append("question_pattern")

        for pattern in _SPAM_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                scores[Intent.SPAM] = scores.get(Intent.SPAM, 0) + 0.4
                keywords.append("spam_pattern")

        # 高意图联系方式请求 → REFERRAL（直接触发引流，高权重确保胜过问句）
        _referral_hits = 0
        for pattern in _REFERRAL_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                _referral_hits += 1
                keywords.append("referral_pattern")
        if _referral_hits > 0:
            # 每次匹配 0.35，最低确保超过双重问句分数(0.60+)
            scores[Intent.REFERRAL] = _referral_hits * 0.35 + 0.30

        # Positive signals
        positive_words = {"thanks", "thank you", "great", "awesome", "cool",
                          "nice", "good", "ok", "sure", "yeah", "yes",
                          "谢谢", "好的", "嗯", "是的", "可以"}
        if any(w in msg for w in positive_words):
            scores[Intent.POSITIVE] = scores.get(Intent.POSITIVE, 0) + 0.3

        # Message length heuristic
        if len(msg) > 100:
            scores[Intent.INTERESTED] = scores.get(Intent.INTERESTED, 0) + 0.15
        if len(msg) < 5 and "?" not in msg:
            scores[Intent.NEUTRAL] = scores.get(Intent.NEUTRAL, 0) + 0.2

        if not scores:
            return ClassificationResult(Intent.NEUTRAL, 0.3, "no pattern matched",
                                         keywords=keywords)

        best_intent = max(scores, key=scores.get)
        best_score = min(scores[best_intent], 0.95)
        return ClassificationResult(best_intent, best_score,
                                     f"rule_based: {len(scores)} patterns",
                                     keywords=keywords)

    def _llm_classify(self, message: str,
                       context: Optional[Dict[str, Any]] = None) -> Optional[ClassificationResult]:
        """Use LLM for nuanced intent classification."""
        if not self.llm:
            return None

        platform = (context or {}).get("platform", "")
        history = (context or {}).get("history", "")

        prompt = (
            f"Classify the intent of this message received on {platform or 'social media'}.\n\n"
            f"Message: \"{message}\"\n"
        )
        if history:
            prompt += f"\nConversation context:\n{history}\n"
        prompt += (
            "\nClassify as EXACTLY one of: interested, question, positive, negative, "
            "spam, neutral, meeting, referral, unsubscribe\n"
            "Respond in JSON: {\"intent\": \"...\", \"confidence\": 0.0-1.0, \"reasoning\": \"...\"}"
        )

        try:
            response = self.llm.chat(prompt, max_tokens=150)
            import json
            data = json.loads(response)
            intent_str = data.get("intent", "neutral").lower()
            try:
                intent = Intent(intent_str)
            except ValueError:
                intent = Intent.NEUTRAL
            return ClassificationResult(
                intent=intent,
                confidence=float(data.get("confidence", 0.6)),
                reasoning=data.get("reasoning", "llm_classification"),
            )
        except Exception as e:
            log.debug("LLM intent classification failed: %s", e)
            return None

    def classify_and_act(self, message: str, lead_id: int,
                          platform: str = "",
                          context: Optional[Dict[str, Any]] = None) -> ClassificationResult:
        """Classify and automatically update lead + trigger pipeline action."""
        result = self.classify(message, context)

        try:
            from ..leads.store import get_leads_store
            store = get_leads_store()

            store.add_interaction(
                lead_id, platform, "message_received",
                direction="inbound", content=message[:500],
                metadata={"intent": result.intent.value,
                          "confidence": result.confidence},
            )

            status_map = {
                Intent.INTERESTED: "qualified",
                Intent.MEETING: "qualified",
                Intent.QUESTION: "responded",
                Intent.POSITIVE: "responded",
                Intent.NEGATIVE: "blacklisted" if result.confidence > 0.8 else None,
                Intent.UNSUBSCRIBE: "blacklisted",
            }
            new_status = status_map.get(result.intent)
            if new_status:
                store.update_lead(lead_id, status=new_status)

            store.update_score(lead_id)

            from ..workflow.event_bus import get_event_bus
            get_event_bus().emit_simple(
                f"{platform}.message_received" if platform else "lead.message_received",
                source="intent_classifier",
                lead_id=lead_id, intent=result.intent.value,
                next_action=result.next_action,
            )

        except Exception as e:
            log.warning("Intent-action integration failed: %s", e)

        return result


# ── Singleton ─────────────────────────────────────────────────────────────

_classifier: Optional[IntentClassifier] = None

def get_intent_classifier() -> IntentClassifier:
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier
