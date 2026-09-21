"""player_care 域 hook —— 「普通朋友」人设 + 玩家网关只读事实的两道闸。

人设不是客服：一个也在玩的普通人，游戏只作为「自己在玩的东西」在对话里自然出现。
一个普通朋友不可能知道你的余额和打码，所以事实分两种用法：

* **暗用（默认）**：``player_sync`` / 本轮查到的资料只进 ``user_context["_player_facts"]``
  （画像与阶段用），提示词里只给一条**不含任何数字**的隐藏提示——对方最近玩过哪些游戏，
  且明确「你不知道这些信息的来源、不能提及」，只用来在对方先聊到游戏时自然接话。
* **明用（对方主动问自己账户 且 给过手机号 / 会员号）**：``chatx_text`` 原样作为
  「只读事实」块进提示词，口吻是「帮你问了一下」，不是后台报数。

两道闸（``on_message_pre_process`` / ``on_reply_post_process``）由 SkillManager.
generate_inbox_draft 3f / 9d 派发（2026-09-20 接线）：
1. 查不到 / 超时 → 提示词切「没资料」分支，明说没查到，不编数字；
2. 数字闸：对方问账户那一轮，回复里出现事实里没有的 ≥3 位数字 → 整句换成安全句。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from src.hooks.base import DomainHook, HookContext

from .gateway import (
    PlayerGateway,
    extract_games,
    extract_phone,
    extract_uid,
    normalize_ph_phone,
    numbers_not_in_facts,
    resolve_gateway_cfg,
)
from .goal_templates import register_goal_templates
from .profile import PlayerProfileService, get_profile_service
from .commandbus import CommandOutbox, get_outbox, is_stop_message, resolve_commandbus_cfg, send_stop
from .handoff import HANDOFF_CONTEXT_BLOCK, PlayerHandoffService, get_handoff_service

logger = logging.getLogger("PlayerCareHook")

# 对方在问「自己的账户」：余额 / 充提 / 打码 / 奖金 / 记录（Taglish + 英 + 中）。
ACCOUNT_QUERY_RE = re.compile(
    r"\b(?:balance|balanse|bal\b|wallet|pera\s+ko|laman|deposit|nag-?deposit|na-?deposit|"
    r"cash[\s\-]?in|cash[\s\-]?out|withdraw(?:al)?|na-?withdraw|payout|turnover|rollover|"
    r"bonus|points?|credits?|history\s+ko|account\s+ko|acc(?:ount)?\s+ko|transaction|"
    r"top[\s\-]?up|load\s+ko|nawala\s+(?:ang\s+)?pera|hindi\s+pumasok)\b"
    r"|余额|打码|流水|充值|提现|充提|奖金|积分|我的账户|到账",
    re.IGNORECASE,
)

# 金额样式（数字闸只在「问账户」那一轮启用，这里不单独用；留作扩展）
_MONEY_RE = re.compile(r"(?:₱|php|peso|pesos|piso|k\b)", re.IGNORECASE)

_SAFE_LINE = {
    "tl": "Teka, hindi ko pa nakuha nang maayos 'yung detalye — i-check ko ulit mamaya, ha.",
    "en": "Hold on, I didn't get the details right — let me check again later.",
    "zh": "等等，刚才没看清细节，我待会再确认一下哈。",
}

_FACTS_KEY = "_player_facts"
_VISIBLE_KEY = "_player_facts_visible"
_ROUND_KEY = "_player_facts_round"
_PROFILE_KEY = "_player_profile_key"
_HANDOFF_KEY = "_player_handoff"


def _reply_lang(ctx: HookContext) -> str:
    lang = str(ctx.reply_lang or (ctx.user_context or {}).get("reply_lang") or "").strip().lower()
    if lang.startswith("zh"):
        return "zh"
    if lang.startswith("en"):
        return "en"
    return "tl"


def _history_user_texts(ctx: HookContext, limit: int = 12) -> List[str]:
    hist = (ctx.user_context or {}).get("_conversation_history") or []
    out: List[str] = []
    for m in hist[-limit:]:
        if isinstance(m, dict) and m.get("role") == "user":
            out.append(str(m.get("content") or ""))
    return out


class PlayerCareDomainHook(DomainHook):
    """Friend persona on WA / TG with read-only player facts from the gateway."""

    def __init__(self, config=None, *, gateway: Optional[PlayerGateway] = None,
                 outbox: Optional[CommandOutbox] = None,
                 handoff: Optional[PlayerHandoffService] = None):
        super().__init__(config)
        self._outbox_override = outbox
        self._handoff_override = handoff
        self._gateway_override = gateway
        self._gateway: Optional[PlayerGateway] = gateway
        self._gateway_sig: str = ""
        # 本域 goals 模板只在本域 hook 随真实实例装载时挂进注册表（story_matrix 实例不装本域 →
        # 看不到；裸 dict / None 配置的单测不挂，免得污染核心模板表断言）
        if getattr(config, "config_path", None):
            register_goal_templates()

    # ── 网关实例（配置变了就重建；测试可注入）────────────────────────────
    def gateway(self) -> PlayerGateway:
        if self._gateway_override is not None:
            return self._gateway_override
        cfg = resolve_gateway_cfg(self._config)
        sig = f"{cfg['enabled']}|{cfg['url']}|{bool(cfg['key'])}|{cfg['timeout_sec']}|{cfg['cache_ttl_sec']}"
        if self._gateway is None or sig != self._gateway_sig:
            self._gateway = PlayerGateway(cfg)
            self._gateway_sig = sig
        return self._gateway

    # ── 身份线索：手机号 / 会员号 ─────────────────────────────────────────
    @staticmethod
    def resolve_identity(ctx: HookContext) -> Dict[str, str]:
        uc = ctx.user_context or {}
        phone = normalize_ph_phone(uc.get("peer_phone") or (ctx.extra or {}).get("peer_phone") or "")
        stored = uc.get(_FACTS_KEY) if isinstance(uc.get(_FACTS_KEY), dict) else {}
        if not phone:
            phone = normalize_ph_phone((stored or {}).get("phone") or "")
        if not phone:
            # WhatsApp：chat_id / user_id 就是号码（JID）；Telegram 的数字 id 归一会失败 → 空
            phone = normalize_ph_phone(ctx.chat_id) or normalize_ph_phone(ctx.user_id)
        if not phone:
            phone = extract_phone(ctx.text)
        if not phone:
            for t in reversed(_history_user_texts(ctx)):
                phone = extract_phone(t)
                if phone:
                    break
        uid = extract_uid(ctx.text) or str((stored or {}).get("uid") or "")
        if not uid:
            for t in reversed(_history_user_texts(ctx)):
                uid = extract_uid(t)
                if uid:
                    break
        return {"phone": phone, "uid": uid}

    # ── 事实块 ───────────────────────────────────────────────────────────
    @staticmethod
    def _facts_block_visible(chatx_text: str, lang: str) -> str:
        tone = {
            "tl": "口吻：像帮朋友顺手问了一下（\"tinanong ko\" / \"na-check ko\"），不是客服；不列表、不用表格。",
            "en": "Tone: like a friend who asked around for them (\"I checked for you\"), not support staff; no lists.",
            "zh": "口吻：像帮朋友顺手问了一下，不是客服；不列表。",
        }[lang]
        return (
            "【只读事实——对方在问自己的账户。以下内容只能照抄复述：数字、时间、游戏名一律"
            "不得计算、推断、四舍五入或补全；platform 是厂商、game 是游戏名。事实里没有的"
            "东西一律说没查到。】\n"
            f"{chatx_text.strip()}\n"
            f"{tone}"
        )

    @staticmethod
    def _facts_block_missing(lang: str) -> str:
        return (
            "【账户查询无结果】对方在问自己的账户，但这次没有查到资料（或查询超时）。"
            "明确说现在没查到 / 稍后再帮看，绝不编任何数字、金额、时间或游戏名。"
        )

    @staticmethod
    def _facts_block_hidden(games: List[Dict[str, str]]) -> str:
        names = [g["game"] + (f"（{g['platform']}）" if g.get("platform") else "") for g in games if g.get("game")]
        if not names:
            return ""
        return (
            "【隐藏画像——你不知道这些信息的来源，绝不能提及你知道、也不能问对方账户；"
            "只有当对方自己先聊到游戏 / 无聊 / 想玩时，才可以自然地接话说你自己也在玩其中一个。"
            "不要报任何数字。】对方近期玩过：" + "、".join(names[:5])
        )

    # ── 画像落库（B2）：每轮入站都记，查不查网关都记；失败不影响回复 ────────
    def _profile(self) -> Optional[PlayerProfileService]:
        try:
            return get_profile_service(self._config)
        except Exception:
            logger.debug("[player_care] profile service 不可用", exc_info=True)
            return None

    def _persist_round(self, ctx: HookContext, ident: Dict[str, str], *, looked_up: bool) -> None:
        svc = self._profile()
        if svc is None:
            return
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        extra = ctx.extra or {}
        platform = str(extra.get("platform") or uc.get("platform") or "")
        account_id = str(extra.get("account_id") or uc.get("account_id") or "")
        external_id = str(ctx.user_id or ctx.chat_id or "")
        try:
            key = svc.resolve_key(
                phone=ident.get("phone", ""), platform=platform, external_id=external_id,
                prev_key=str(uc.get(_PROFILE_KEY) or ""),
            )
            uc[_PROFILE_KEY] = key
            facts = uc.get(_FACTS_KEY) if isinstance(uc.get(_FACTS_KEY), dict) else None
            svc.record_inbound(
                key=key, text=str(ctx.text or ""), platform=platform, account_id=account_id,
                external_id=external_id, phone=ident.get("phone", ""), uid=ident.get("uid", ""),
                contact_id=str(uc.get("contact_id") or ""),
                facts=facts, looked_up=looked_up, round_kind=str(uc.get(_ROUND_KEY) or ""),
            )
        except Exception:
            logger.debug("[player_care] 画像落库失败（忽略）", exc_info=True)

    # ── B4：对方说 STOP → 给手机侧先发 stop 指令（失败全部忽略，不影响回复）──────────
    def _outbox(self) -> Optional[CommandOutbox]:
        if self._outbox_override is not None:
            return self._outbox_override
        try:
            return get_outbox(self._config)
        except Exception:
            logger.debug("[player_care] commandbus 出箱不可用", exc_info=True)
            return None

    def _maybe_send_stop(self, ctx: HookContext, ident: Dict[str, str]) -> Optional[Dict[str, Any]]:
        if not is_stop_message(ctx.text):
            return None
        if not resolve_commandbus_cfg(self._config)["stop_on_keyword"]:
            return None
        phone = str(ident.get("phone") or "").strip()
        if not phone:
            return None
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        extra = ctx.extra or {}
        account_id = str(extra.get("account_id") or uc.get("account_id") or "")
        env = send_stop(self._outbox(), account=account_id or "unknown", phone=phone, reason="user_stop")
        if env is not None:
            uc["_player_stop_sent"] = env.get("command_id")
            logger.info("[player_care] STOP → commandbus stop %s", env.get("command_id"))
        return env

    # ── B5：Messenger → WA/TG 引流码 → 合并身份（手机号主键）；同一画像只合一次，失败全吞 ─────
    def _handoff(self) -> Optional[PlayerHandoffService]:
        if self._handoff_override is not None:
            return self._handoff_override
        try:
            return get_handoff_service(self._config, self._profile())
        except Exception:
            logger.debug("[player_care] handoff service 不可用", exc_info=True)
            return None

    def _maybe_handoff(self, ctx: HookContext, ident: Dict[str, str]) -> Optional[Dict[str, Any]]:
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        if isinstance(uc.get(_HANDOFF_KEY), dict) and uc[_HANDOFF_KEY].get("token"):
            return None
        svc = self._handoff()
        if svc is None:
            return None
        extra = ctx.extra or {}
        res = svc.try_merge(
            text=str(ctx.text or ""),
            platform=str(extra.get("platform") or uc.get("platform") or ""),
            account_id=str(extra.get("account_id") or uc.get("account_id") or ""),
            external_id=str(ctx.user_id or ctx.chat_id or ""),
            phone=ident.get("phone", ""),
            display_name=str(extra.get("display_name") or uc.get("display_name") or ""),
            profile=self._profile(),
            profile_key=str(uc.get(_PROFILE_KEY) or ""),
        )
        if res is None:
            return None
        uc[_HANDOFF_KEY] = res
        if res.get("profile_key"):
            uc[_PROFILE_KEY] = res["profile_key"]
        if res.get("merged_contact_id"):
            uc["contact_id"] = res["merged_contact_id"]
        if res.get("phone") and not uc.get("peer_phone"):
            uc["peer_phone"] = res["phone"]
        return res

    # ── hook 1：入站预处理 → 查网关、决定明用 / 暗用，再落画像 ────────────────
    async def on_message_pre_process(self, ctx: HookContext) -> Optional[Dict[str, Any]]:
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        prev_ts = float((uc.get(_FACTS_KEY) or {}).get("ts") or 0) if isinstance(uc.get(_FACTS_KEY), dict) else 0.0
        result = await self._decide(ctx)
        facts = uc.get(_FACTS_KEY) if isinstance(uc.get(_FACTS_KEY), dict) else {}
        looked_up = float((facts or {}).get("ts") or 0) > prev_ts
        ident = self.resolve_identity(ctx)
        self._persist_round(ctx, ident, looked_up=looked_up)
        try:
            if self._maybe_handoff(ctx, ident) is not None:
                block = str((result or {}).get("_domain_context_block") or "")
                result = dict(result or {})
                result["_domain_context_block"] = (block + "\n" + HANDOFF_CONTEXT_BLOCK).strip()
        except Exception:
            logger.debug("[player_care] handoff 处理失败（忽略）", exc_info=True)
        try:
            self._maybe_send_stop(ctx, ident)
        except Exception:
            logger.debug("[player_care] STOP 处理失败（忽略）", exc_info=True)
        return result

    async def _decide(self, ctx: HookContext) -> Optional[Dict[str, Any]]:
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        uc[_VISIBLE_KEY] = False
        uc[_ROUND_KEY] = ""
        text = str(ctx.text or "")
        asks_account = bool(ACCOUNT_QUERY_RE.search(text))
        ident = self.resolve_identity(ctx)
        phone, uid = ident["phone"], ident["uid"]
        lang = _reply_lang(ctx)

        gw = self.gateway()
        if not gw.configured:
            if asks_account:
                # 网关没配：对方问账户也只能老实说没资料
                uc[_ROUND_KEY] = "unconfigured"
                return {"_domain_context_block": self._facts_block_missing(lang)}
            return None

        if not (phone or uid or asks_account):
            return None  # 没身份线索也没问账户：不打网关

        try:
            res = await asyncio.to_thread(gw.lookup, text, phone=phone, uid=uid)
        except Exception:
            logger.debug("[player_care] lookup 异常（按无资料）", exc_info=True)
            res = None

        found = bool(res and res.usable)
        facts_text = res.chatx_text if found else ""
        games = extract_games(facts_text) if found else []
        prev = uc.get(_FACTS_KEY) if isinstance(uc.get(_FACTS_KEY), dict) else {}
        uc[_FACTS_KEY] = {
            "phone": phone or str(prev.get("phone") or ""),
            "uid": uid or str(prev.get("uid") or ""),
            "found": found,
            "text": facts_text if found else str(prev.get("text") or ""),
            "games": games or list(prev.get("games") or []),
            "ts": time.time(),
            "error": "" if found else str(getattr(res, "error", "") or ("not_found" if res else "exception")),
            "cached": bool(getattr(res, "cached", False)),
        }

        if asks_account and (phone or uid):
            if found:
                uc[_VISIBLE_KEY] = True
                uc[_ROUND_KEY] = "visible"
                return {"_domain_context_block": self._facts_block_visible(facts_text, lang)}
            uc[_ROUND_KEY] = "missing"
            return {"_domain_context_block": self._facts_block_missing(lang)}
        if asks_account:
            # 问账户但我们不知道是谁：让人设自然地问一句号码 / 会员号，不编
            uc[_ROUND_KEY] = "need_identity"
            return {"_domain_context_block": (
                "【账户查询】对方在问自己的账户，但你还不知道对方的手机号或会员号。"
                "不要编任何数字；用朋友口吻自然地问一句是哪个号码 / 会员号，再帮他看。")}
        hidden = self._facts_block_hidden(uc[_FACTS_KEY].get("games") or [])
        if hidden:
            uc[_ROUND_KEY] = "hidden"
            return {"_domain_context_block": hidden}
        return None

    # ── hook 2：回复后处理 → 数字闸 ───────────────────────────────────────
    async def on_reply_post_process(self, reply: str, ctx: HookContext) -> str:
        text = str(reply or "")
        if not text.strip():
            return reply
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        round_kind = str(uc.get(_ROUND_KEY) or "")
        lang = _reply_lang(ctx)
        if round_kind == "visible":
            facts = str((uc.get(_FACTS_KEY) or {}).get("text") or "")
            bad = numbers_not_in_facts(text, facts)
            if bad:
                logger.info("[player_care] 数字闸：回复含事实外数字 %s → 安全句", bad[:5])
                uc["_player_numeric_gate_hits"] = int(uc.get("_player_numeric_gate_hits") or 0) + 1
                self._record_gate_hit(ctx)
                return _SAFE_LINE[lang]
        elif round_kind in ("missing", "need_identity", "unconfigured"):
            # 没资料那一轮：任何 ≥3 位数字都是编的
            bad = numbers_not_in_facts(text, "")
            if bad:
                logger.info("[player_care] 数字闸（无资料轮）：%s → 安全句", bad[:5])
                uc["_player_numeric_gate_hits"] = int(uc.get("_player_numeric_gate_hits") or 0) + 1
                self._record_gate_hit(ctx)
                return _SAFE_LINE[lang]
        return reply

    def _record_gate_hit(self, ctx: HookContext) -> None:
        svc = self._profile()
        if svc is None:
            return
        uc = ctx.user_context if isinstance(ctx.user_context, dict) else {}
        key = str(uc.get(_PROFILE_KEY) or "")
        if not key:
            return
        try:
            svc.record_gate_hit(key, str((ctx.extra or {}).get("account_id") or ""))
        except Exception:
            logger.debug("[player_care] gate_hit 落库失败（忽略）", exc_info=True)

    def get_escalation_line(self) -> str:
        # 基类默认是中文客服话术；朋友人设绝不能带出去。
        return ""
