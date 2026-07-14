# -*- coding: utf-8 -*-
"""
Conversation State Machine — manages lead conversation lifecycle on TikTok.

States:
  idle          → lead exists but no outreach yet
  greeting      → first DM sent, waiting for reply
  qualifying    → asking qualification questions (budget, needs, timeline)
  pitching      → presenting offer/value proposition
  negotiating   → lead engaged, negotiating details or scheduling
  converted     → lead converted (exchanged contacts, meeting booked)
  dormant       → lead went silent, in follow-up cycle
  rejected      → lead explicitly declined

Transitions are driven by:
  1. Intent classification of incoming messages
  2. Timeout-based auto follow-up
  3. Manual escalation from UI/API

Follow-up Rules:
  - greeting → no reply in 24h → auto follow-up (max 2 times) → dormant
  - qualifying → no reply in 48h → gentle nudge → dormant
  - dormant → re-engage after 72h with different angle
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class ConvState(str, Enum):
    IDLE = "idle"
    GREETING = "greeting"
    QUALIFYING = "qualifying"
    PITCHING = "pitching"
    NEGOTIATING = "negotiating"
    CONVERTED = "converted"
    DORMANT = "dormant"
    REJECTED = "rejected"


@dataclass
class StateConfig:
    """Per-state behavior configuration."""
    follow_up_hours: float = 24.0
    max_follow_ups: int = 2
    auto_advance_on_reply: bool = True
    next_state_on_reply: Optional[str] = None
    escalate_on_intent: Dict[str, str] = field(default_factory=dict)

    follow_up_templates: List[str] = field(default_factory=list)


_FOLLOW_UP_TEMPLATES_I18N = {
    "en": {
        ConvState.GREETING: [
            "Hey {name}, just wanted to make sure you saw my message! How are you? 😊",
            "Hi {name}! Hope you're having a great day. Would love to connect!",
        ],
        ConvState.QUALIFYING: [
            "Hey {name}, I'd love to hear more about what you're looking for. Any thoughts?",
        ],
        ConvState.PITCHING: [
            "Hi {name}, just checking in — did you get a chance to think about what I shared?",
        ],
        ConvState.NEGOTIATING: [
            "Hey {name}, still interested in connecting? Let me know if you have any questions!",
        ],
        ConvState.DORMANT: [
            "Hi {name}! Been a while — hope you're doing well! I have something new that might interest you.",
        ],
    },
    "it": {
        ConvState.GREETING: [
            # Tier-1 (follow_up #1): 温和提醒 + 轻量级引流
            "Ciao {name}, volevo assicurarmi che avessi visto il mio messaggio! Come stai? 😊 Se ti va, scrivimi: {contact}",
            # Tier-2 (follow_up #2): 关系角度 + 明确邀请
            "Ehi {name}! Ho notato che ci seguiamo — mi piacerebbe conoscerti meglio 🤝 Passami un messaggio: {contact}",
            # Tier-3 (follow_up #3+): 最后机会，低压力但有 CTA
            "Ciao {name}, ultimo messaggio promesso 😊 Se sei curioso/a, scrivimi qui: {contact} — ho qualcosa che potrebbe interessarti!",
        ],
        ConvState.QUALIFYING: [
            # 了解需求阶段：先建立联系，再了解需求
            "Ciao {name}, mi piacerebbe capire meglio cosa stai cercando! Scrivimi su {contact} 🎯",
            "Ehi {name}, nessuna fretta! Quando hai un momento, parliamone su {contact} — sono curioso/a di saperne di più.",
        ],
        ConvState.PITCHING: [
            # pitch 阶段：有具体信息要分享
            "Ciao {name}, ho delle novità interessanti per te! Scrivimi su {contact} e te le racconto 🚀",
            "Ehi {name}! Ho pensato a una cosa che potrebbe farti comodo — contattami su {contact} quando puoi 😊",
        ],
        ConvState.NEGOTIATING: [
            "Ciao {name}, sei ancora interessato/a? Sono su {contact} quando vuoi chiarire qualcosa!",
            "Ehi {name}, ci sei? Scrivimi su {contact} — voglio essere sicuro/a di non perderti 😊",
        ],
        ConvState.DORMANT: [
            "Ciao {name}! È passato un po' — spero che tu stia bene! Ho qualcosa di nuovo: {contact} 🌟",
            "Ehi {name}! Ho pensato a te — c'è qualcosa che potrebbe interessarti. Scrivimi su {contact} quando vuoi.",
            "Ciao {name}, ultima volta promesso 😄 Se mai ti venisse voglia di chattare, mi trovi su {contact}!",
        ],
    },
    "de": {
        ConvState.GREETING: [
            "Hey {name}, wollte sicherstellen, dass du meine Nachricht gesehen hast! Wie geht es dir? 😊 Schreib mir: {contact}",
            "Hallo {name}! Würde mich gerne vernetzen — erreich mich auf: {contact} 🤝",
        ],
        ConvState.QUALIFYING: [
            "Hey {name}, ich würde gerne mehr erfahren. Schreib mir auf {contact}!",
        ],
        ConvState.PITCHING: [
            "Hi {name}, ich hab Neuigkeiten für dich! Erreich mich auf {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Hey {name}, noch interessiert? Schreib mir auf {contact} — ich beantworte alle Fragen!",
        ],
        ConvState.DORMANT: [
            "Hi {name}! Schon eine Weile! Ich hab etwas Neues: {contact} 😊",
        ],
    },
    "fr": {
        ConvState.GREETING: [
            "Hey {name}, je voulais m'assurer que tu avais vu mon message ! Écris-moi: {contact} 😊",
            "Salut {name} ! J'adorerais me connecter — contact moi ici: {contact} 🤝",
        ],
        ConvState.QUALIFYING: [
            "Hey {name}, j'aimerais en savoir plus ! Écris-moi sur {contact} quand tu veux.",
        ],
        ConvState.PITCHING: [
            "Salut {name}, j'ai quelque chose d'intéressant à te montrer ! Contacte-moi: {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Hey {name}, toujours intéressé ? Réponds-moi sur {contact} — je suis là !",
        ],
        ConvState.DORMANT: [
            "Salut {name} ! Ça fait un moment — j'ai du nouveau: {contact} 🌟",
        ],
    },
    "es": {
        ConvState.GREETING: [
            "¡Hola {name}! Quería asegurarme de que vieras mi mensaje. ¡Escríbeme: {contact}! 😊",
            "¡Hola {name}! ¡Me encantaría conectar contigo! Encuéntrame en: {contact} 🤝",
        ],
        ConvState.QUALIFYING: [
            "¡Hey {name}! Me encantaría saber más. Escríbeme en {contact} cuando puedas.",
        ],
        ConvState.PITCHING: [
            "¡Hola {name}! Tengo algo interesante para ti. Contáctame en {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "¡Hey {name}! ¿Sigues interesado/a? Aquí estoy: {contact} 😊",
        ],
        ConvState.DORMANT: [
            "¡Hola {name}! Ha pasado tiempo — tengo algo nuevo: {contact} 🌟",
        ],
    },
    "tl": {
        ConvState.GREETING: [
            "Hey {name}, gusto kong tiyaking nakita mo ang aking mensahe! Kumusta ka? 😊 Makipag-ugnayan sa akin: {contact}",
            "Hi {name}! Gusto kong makilala ka — makipag-chat sa akin: {contact} 🤝",
            "Hoy {name}, huli na itong mensahe ko 😊 Kung interesado ka, makita mo ako dito: {contact}!",
        ],
        ConvState.QUALIFYING: [
            "Hey {name}, gusto kong malaman pa ang higit. Makipag-ugnayan sa {contact}!",
        ],
        ConvState.PITCHING: [
            "Hi {name}, mayroon akong isang bagay na kapaki-pakinabang para sa iyo! Makipag-ugnayan sa {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Hey {name}, interesado ka pa rin? Narito ako sa {contact} 😊",
        ],
        ConvState.DORMANT: [
            "Hi {name}! Matagal na — mayroon akong bago para sa iyo: {contact} 🌟",
        ],
    },
    "id": {
        ConvState.GREETING: [
            "Hei {name}, ingin memastikan kamu melihat pesanku! Apa kabar? 😊 Hubungi aku: {contact}",
            "Hai {name}! Senang terhubung denganmu — chat aku di: {contact} 🤝",
            "Hei {name}, pesan terakhirku ya 😊 Kalau tertarik, temukan aku di sini: {contact}!",
        ],
        ConvState.QUALIFYING: [
            "Hei {name}, ingin tahu lebih banyak. Hubungi aku di {contact}!",
        ],
        ConvState.PITCHING: [
            "Hai {name}, ada sesuatu yang bermanfaat untukmu! Hubungi aku di {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Hei {name}, masih tertarik? Aku ada di {contact} 😊",
        ],
        ConvState.DORMANT: [
            "Hai {name}! Sudah lama — ada yang baru untukmu: {contact} 🌟",
        ],
    },
    "ms": {
        ConvState.GREETING: [
            "Hai {name}, nak pastikan awak nampak mesej saya! Apa khabar? 😊 Hubungi saya: {contact}",
            "Hi {name}! Seronok berhubung dengan awak — chat saya di: {contact} 🤝",
        ],
        ConvState.QUALIFYING: [
            "Hai {name}, ingin tahu lebih lanjut. Hubungi saya di {contact}!",
        ],
        ConvState.PITCHING: [
            "Hi {name}, ada sesuatu yang berguna untuk awak! Hubungi saya di {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Hai {name}, masih berminat? Saya ada di {contact} 😊",
        ],
        ConvState.DORMANT: [
            "Hi {name}! Dah lama — ada yang baru untuk awak: {contact} 🌟",
        ],
    },
    "ar": {
        ConvState.GREETING: [
            "مرحبا {name}، أردت التأكد من أنك رأيت رسالتي! كيف حالك؟ 😊 تواصل معي: {contact}",
            "أهلاً {name}! يسعدني التواصل معك — راسلني هنا: {contact} 🤝",
            "مرحبا {name}، هذه آخر رسالة أعدك 😊 إذا كنت مهتماً، تجدني هنا: {contact}!",
        ],
        ConvState.QUALIFYING: [
            "مرحبا {name}، أريد أن أعرف أكثر. تواصل معي على {contact}!",
        ],
        ConvState.PITCHING: [
            "أهلاً {name}، لدي شيء مفيد لك! تواصل معي على {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "مرحبا {name}، لا تزال مهتماً؟ أنا هنا على {contact} 😊",
        ],
        ConvState.DORMANT: [
            "أهلاً {name}! مرت فترة — لدي شيء جديد لك: {contact} 🌟",
        ],
    },
    "pt": {
        ConvState.GREETING: [
            "Oi {name}, queria garantir que você viu minha mensagem! Como vai? 😊 Me chama: {contact}",
            "Olá {name}! Adoraria me conectar contigo — fala comigo aqui: {contact} 🤝",
            "Oi {name}, última mensagem prometo 😊 Se tiver interesse, me encontra aqui: {contact}!",
        ],
        ConvState.QUALIFYING: [
            "Oi {name}, gostaria de saber mais sobre você. Me chama no {contact}!",
        ],
        ConvState.PITCHING: [
            "Olá {name}, tenho algo útil pra te mostrar! Me contata no {contact} 🎯",
        ],
        ConvState.NEGOTIATING: [
            "Oi {name}, ainda tem interesse? Estou no {contact} 😊",
        ],
        ConvState.DORMANT: [
            "Olá {name}! Faz tempo — tenho algo novo pra você: {contact} 🌟",
        ],
    },
    "hi": {
        ConvState.GREETING: [
            "हे {name}, बस यह सुनिश्चित करना चाहता था कि आपने मेरा संदेश देखा! कैसे हैं आप? 😊 मुझसे संपर्क करें: {contact}",
            "नमस्ते {name}! आपसे जुड़ना अच्छा लगेगा — यहाँ मुझसे बात करें: {contact} 🤝",
        ],
        ConvState.QUALIFYING: [
            "हे {name}, और जानना चाहता हूँ। {contact} पर संपर्क करें!",
        ],
        ConvState.PITCHING: [
            "नमस्ते {name}, आपके लिए कुछ उपयोगी है! {contact} पर संपर्क करें 🎯",
        ],
        ConvState.NEGOTIATING: [
            "हे {name}, अभी भी रुचि है? मैं {contact} पर हूँ 😊",
        ],
        ConvState.DORMANT: [
            "नमस्ते {name}! काफी समय हो गया — आपके लिए कुछ नया है: {contact} 🌟",
        ],
    },
}

_COUNTRY_LANG = {
    "italy": "it", "germany": "de", "france": "fr", "spain": "es",
    "philippines": "tl", "indonesia": "id", "malaysia": "ms",
    "saudi arabia": "ar", "uae": "ar", "egypt": "ar",
    "brazil": "pt", "portugal": "pt",
    "india": "hi",
    # ISO code aliases
    "ph": "tl", "id": "id", "my": "ms", "sa": "ar", "ae": "ar", "eg": "ar",
    "br": "pt", "pt": "pt", "in": "hi",
    "it": "it", "de": "de", "fr": "fr", "es": "es",
    "us": "en", "gb": "en", "au": "en",
}


def _get_templates(state: ConvState, language: str = "en") -> List[str]:
    lang = language.lower()[:2]
    templates = _FOLLOW_UP_TEMPLATES_I18N.get(lang, {}).get(state)
    if templates:
        return templates
    return _FOLLOW_UP_TEMPLATES_I18N.get("en", {}).get(state, [])


DEFAULT_FSM_CONFIG: Dict[str, StateConfig] = {
    ConvState.GREETING: StateConfig(
        follow_up_hours=24,
        max_follow_ups=3,
        next_state_on_reply="qualifying",
        escalate_on_intent={
            "interested": "qualifying",
            "question": "qualifying",
            "meeting": "negotiating",
            "negative": "rejected",
        },
        follow_up_templates=_get_templates(ConvState.GREETING),
    ),
    ConvState.QUALIFYING: StateConfig(
        follow_up_hours=48,
        max_follow_ups=1,
        next_state_on_reply="pitching",
        escalate_on_intent={
            "interested": "pitching",
            "meeting": "negotiating",
            "negative": "rejected",
            "question": "qualifying",
        },
        follow_up_templates=_get_templates(ConvState.QUALIFYING),
    ),
    ConvState.PITCHING: StateConfig(
        follow_up_hours=48,
        max_follow_ups=1,
        next_state_on_reply="negotiating",
        escalate_on_intent={
            "interested": "negotiating",
            "meeting": "negotiating",
            "question": "pitching",
            "negative": "rejected",
        },
        follow_up_templates=_get_templates(ConvState.PITCHING),
    ),
    ConvState.NEGOTIATING: StateConfig(
        follow_up_hours=72,
        max_follow_ups=2,
        next_state_on_reply="negotiating",
        escalate_on_intent={
            "meeting": "converted",
            "interested": "negotiating",
            "negative": "rejected",
        },
        follow_up_templates=_get_templates(ConvState.NEGOTIATING),
    ),
    ConvState.DORMANT: StateConfig(
        follow_up_hours=72,
        max_follow_ups=1,
        next_state_on_reply="qualifying",
        escalate_on_intent={
            "interested": "qualifying",
            "meeting": "negotiating",
        },
        follow_up_templates=_get_templates(ConvState.DORMANT),
    ),
}


class ConversationFSM:
    """
    Manages conversation state for a single lead.

    State is persisted in LeadsStore via tags and metadata.
    """

    def __init__(self, lead_id: int, platform: str = "tiktok"):
        self.lead_id = lead_id
        self.platform = platform
        self._config = DEFAULT_FSM_CONFIG

    def get_state(self) -> ConvState:
        """Read current state from CRM."""
        try:
            from ..leads.store import get_leads_store
            store = get_leads_store()
            lead = store.get_lead(self.lead_id)
            if not lead:
                return ConvState.IDLE

            tags = lead.get("tags", [])
            if isinstance(tags, str):
                tags = json.loads(tags)

            for tag in tags:
                if tag.startswith("conv_state:"):
                    state_str = tag.split(":", 1)[1]
                    try:
                        return ConvState(state_str)
                    except ValueError:
                        pass
            return ConvState.IDLE
        except Exception:
            return ConvState.IDLE

    def set_state(self, new_state: ConvState, reason: str = ""):
        """Update state in CRM."""
        try:
            from ..leads.store import get_leads_store
            store = get_leads_store()
            lead = store.get_lead(self.lead_id)
            if not lead:
                return

            tags = lead.get("tags", [])
            if isinstance(tags, str):
                tags = json.loads(tags)

            tags = [t for t in tags if not t.startswith("conv_state:")]
            tags.append(f"conv_state:{new_state.value}")

            old_state = self.get_state()
            store.update_lead(self.lead_id, tags=tags)

            store.add_interaction(
                self.lead_id, self.platform, "state_transition",
                direction="system",
                content=f"{old_state.value} → {new_state.value}",
                metadata={"reason": reason, "from": old_state.value,
                          "to": new_state.value},
            )

            # Update lead status to match
            status_map = {
                ConvState.GREETING: "contacted",
                ConvState.QUALIFYING: "responded",
                ConvState.PITCHING: "responded",
                ConvState.NEGOTIATING: "qualified",
                ConvState.CONVERTED: "converted",
                ConvState.REJECTED: "blacklisted",
            }
            new_status = status_map.get(new_state)
            if new_status:
                store.update_lead(self.lead_id, status=new_status)

            log.info("[FSM] Lead #%d: %s → %s (%s)",
                     self.lead_id, old_state.value, new_state.value, reason)

        except Exception as e:
            log.debug("[FSM] State update failed: %s", e)

    def on_message_received(self, message: str, intent: str = "") -> Dict[str, Any]:
        """
        Process an incoming message — advance state based on intent.

        Returns action dict: {"new_state", "action", "follow_up_template"}
        """
        current = self.get_state()
        config = self._config.get(current)

        result = {"old_state": current.value, "new_state": current.value,
                  "action": "none", "reason": ""}

        if current in (ConvState.CONVERTED, ConvState.REJECTED):
            return result

        # Reset follow-up counter on any reply
        self._reset_follow_up_count()

        if not config:
            if current == ConvState.IDLE:
                self.set_state(ConvState.GREETING, "first_interaction")
                result["new_state"] = ConvState.GREETING.value
                result["action"] = "state_advanced"
            return result

        # Intent-based transition
        if intent and intent in config.escalate_on_intent:
            target = ConvState(config.escalate_on_intent[intent])
            self.set_state(target, f"intent={intent}")
            result["new_state"] = target.value
            result["action"] = "state_advanced"
            result["reason"] = f"intent: {intent}"
            return result

        # Default advance on reply
        if config.auto_advance_on_reply and config.next_state_on_reply:
            target = ConvState(config.next_state_on_reply)
            self.set_state(target, "reply_received")
            result["new_state"] = target.value
            result["action"] = "state_advanced"
            result["reason"] = "auto_advance_on_reply"
            return result

        return result

    def on_message_sent(self, message_type: str = "dm"):
        """Record that we sent a message — update state if idle."""
        current = self.get_state()
        if current == ConvState.IDLE:
            self.set_state(ConvState.GREETING, "first_dm_sent")

    def check_follow_up(self, language: str = "en") -> Optional[Dict[str, Any]]:
        """
        Check if this conversation needs a follow-up.

        Returns follow-up action dict or None.
        """
        current = self.get_state()
        if current in (ConvState.CONVERTED, ConvState.REJECTED, ConvState.IDLE):
            return None

        config = self._config.get(current)
        if not config:
            return None

        last_interaction = self._get_last_interaction_time()
        if not last_interaction:
            return None

        hours_since = (datetime.now(timezone.utc) - last_interaction).total_seconds() / 3600
        if hours_since < config.follow_up_hours:
            return None

        follow_up_count = self._get_follow_up_count()
        if follow_up_count >= config.max_follow_ups:
            if current != ConvState.DORMANT:
                self.set_state(ConvState.DORMANT, f"no_reply_after_{follow_up_count}_follow_ups")
            return None

        templates = _get_templates(current, language)
        template = ""
        if templates:
            # P7-B: 按 follow_up_count 顺序选取（tier-1→tier-2→tier-3+）
            # 比 random.choice 更有策略性：先温和提醒，再加强，最后收尾
            idx = min(follow_up_count, len(templates) - 1)
            template = templates[idx]

        self._increment_follow_up_count()

        return {
            "lead_id": self.lead_id,
            "state": current.value,
            "follow_up_number": follow_up_count + 1,
            "max_follow_ups": config.max_follow_ups,
            "hours_since_last": round(hours_since, 1),
            "template": template,
            "language": language,
        }

    def check_timeout_transition(self) -> Optional[str]:
        """Auto-transition leads stuck in non-terminal states too long.
        Returns new state name or None."""
        current = self.get_state()
        if current in (ConvState.CONVERTED, ConvState.REJECTED,
                       ConvState.IDLE, ConvState.DORMANT):
            return None

        last = self._get_last_interaction_time()
        if not last:
            return None

        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        config = self._config.get(current)
        if not config:
            return None

        timeout_hours = config.follow_up_hours * (config.max_follow_ups + 2)
        if hours > timeout_hours:
            self.set_state(ConvState.DORMANT,
                           f"timeout_{int(hours)}h_in_{current.value}")
            return ConvState.DORMANT.value

        return None

    def _get_last_interaction_time(self) -> Optional[datetime]:
        try:
            from ..leads.store import get_leads_store
            store = get_leads_store()
            interactions = store.get_interactions(
                self.lead_id, platform=self.platform, limit=1)
            if interactions:
                ts = interactions[0].get("created_at", "")
                if ts:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass
        return None

    def _get_follow_up_count(self) -> int:
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store(self.platform)
            return ds.get_int(f"lead:{self.lead_id}", "follow_up_count")
        except Exception:
            return 0

    def _increment_follow_up_count(self):
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store(self.platform)
            ds.increment(f"lead:{self.lead_id}", "follow_up_count")
        except Exception:
            pass

    def _reset_follow_up_count(self):
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store(self.platform)
            ds.set(f"lead:{self.lead_id}", "follow_up_count", 0)
        except Exception:
            pass


# ── Batch follow-up checker ──

def check_all_follow_ups(platform: str = "tiktok",
                         max_leads: int = 50,
                         target_country: str = "") -> List[Dict[str, Any]]:
    """
    Check all active conversations for pending follow-ups.
    Also runs timeout transitions for stuck leads.

    Returns list of follow-up actions to execute.
    """
    lang = _COUNTRY_LANG.get(target_country.lower(), "en") if target_country else "en"

    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        leads = store.list_leads(
            platform=platform,
            status="contacted",
            order_by="updated_at ASC",
            limit=max_leads,
        )

        leads.extend(store.list_leads(
            platform=platform,
            status="responded",
            order_by="updated_at ASC",
            limit=max_leads,
        ))

        follow_ups = []
        timeouts = 0
        for lead in leads:
            fsm = ConversationFSM(lead["id"], platform)

            timeout_result = fsm.check_timeout_transition()
            if timeout_result:
                timeouts += 1
                continue

            action = fsm.check_follow_up(language=lang)
            if action:
                action["lead_name"] = lead.get("name", "")
                follow_ups.append(action)

        if follow_ups:
            log.info("[FSM] Found %d pending follow-ups (lang=%s)", len(follow_ups), lang)
        if timeouts:
            log.info("[FSM] %d leads timed out → dormant", timeouts)

        return follow_ups

    except Exception as e:
        log.debug("[FSM] check_all_follow_ups failed: %s", e)
        return []


def get_conversation_summary(lead_id: int,
                             platform: str = "tiktok") -> Dict[str, Any]:
    """Get complete conversation state and history for a lead."""
    fsm = ConversationFSM(lead_id, platform)
    state = fsm.get_state()

    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        lead = store.get_lead(lead_id)
        interactions = store.get_interactions(lead_id, platform=platform, limit=20)

        transitions = [
            ix for ix in interactions
            if ix.get("action") == "state_transition"
        ]

        return {
            "lead_id": lead_id,
            "name": lead.get("name", "") if lead else "",
            "state": state.value,
            "status": lead.get("status", "") if lead else "",
            "score": lead.get("score", 0) if lead else 0,
            "follow_up_pending": fsm.check_follow_up() is not None,
            "transition_history": [
                {"from": json.loads(t.get("metadata", "{}")).get("from", ""),
                 "to": json.loads(t.get("metadata", "{}")).get("to", ""),
                 "reason": json.loads(t.get("metadata", "{}")).get("reason", ""),
                 "timestamp": t.get("created_at", "")}
                for t in transitions
            ],
            "message_count": len([i for i in interactions
                                  if i.get("action") not in ("state_transition",)]),
        }
    except Exception:
        return {"lead_id": lead_id, "state": state.value}
