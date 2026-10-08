"""智聊读取无界玩家网关：只为**渠道层核实过的本人号**注入只读后台资料（智安 P0-3，2026-10-08）。

红线（用户原话）：「博彩召回回述真人余额」只留痕、绝不真发。旧实现的三个口子在这里堵上：

1. **身份**：旧版对消息里任意 6 位以上数字都去查网关，任何人打一句「uid 12345678 balance?」
   就能拿到别人的账户资料。现在**只认渠道层核实的本人号**——WhatsApp 私聊的 JID
   （``6391…@s.whatsapp.net`` / ``@c.us``，就是对方本人的手机号）。正文里的手机号 / UID、
   Telegram / Messenger 等没有手机号 JID 的渠道、群（``@g.us``）、LID（``@lid``，不是号码）
   一律不查。
2. **不绑定**：请求体 ``bind=False``，不再让网关把「正文里出现的号码 ↔ UID」写进别名表。
   请求只带核实过的 ``phone``，不再把对话原文 ``q`` 发给网关。
3. **金额脱敏**：注入块与出站回复里的余额 / 充值 / 提现 / 打码 / 流水金额一律 ``***``
   （:func:`redact_financials`；出站那道在 ``SkillManager._apply_outbound_text_guard``）。

开关（全部默认关）::

    player_gateway:
      enabled: false          # 总开关（旧键，语义不变）
      visible_facts: false    # 新：核实本人号后是否注入网关事实（金额仍脱敏）；关 → 一律不查网关
      url: ""                 # 网关地址
      key: ""                 # 网关 key（建议走环境变量注入，勿写进仓库）

审计：每次「跳过 / 查询」写一行日志 ``[player-gateway]``（只记平台、动作、原因、号码末 4 位），
不记对话原文与网关返回内容。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

#: 只有这些平台的会话键能当「本人号」——WA JID 就是号码本身
_VERIFIED_PHONE_PLATFORMS = frozenset({"whatsapp"})
_WA_PERSONAL_DOMAINS = ("@s.whatsapp.net", "@c.us")
_WA_NON_PERSONAL = ("@g.us", "@broadcast", "@newsletter", "@lid", "status@")


def player_gateway_cfg(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return dict(config.get("player_gateway") or {})
    raw = getattr(config, "config", None)
    if isinstance(raw, dict):
        return dict(raw.get("player_gateway") or {})
    return {}


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return v is True


def gateway_ready(cfg: Dict[str, Any]) -> bool:
    """总开关开、且配了 url + key。"""
    return bool(_truthy(cfg.get("enabled")) and cfg.get("url") and cfg.get("key"))


def visible_facts_enabled(cfg: Dict[str, Any]) -> bool:
    """``player_gateway.visible_facts``（默认关）。"""
    v = cfg.get("visible_facts")
    if isinstance(v, dict):
        v = v.get("enabled")
    return _truthy(v)


def verified_phone(user_context: Optional[Dict[str, Any]]) -> str:
    """渠道层核实的本人号：WhatsApp 私聊 JID → 号码数字；其它一律 ""。纯函数。

    只看会话身份字段（``platform`` + ``chat_id`` / ``chat_key`` / ``peer_jid``），**不看正文**。
    纯数字的 WA chat_key（协议层已剥域名）也认；``@g.us`` / ``@lid`` / 广播 / 频道不认。
    """
    ctx = user_context if isinstance(user_context, dict) else {}
    plat = str(ctx.get("platform") or "").strip().lower()
    if plat not in _VERIFIED_PHONE_PLATFORMS:
        return ""
    jid = ""
    for k in ("peer_jid", "chat_id", "chat_key"):
        v = str(ctx.get(k) or "").strip()
        if v:
            jid = v
            break
    if not jid:
        return ""
    low = jid.lower()
    if any(x in low for x in _WA_NON_PERSONAL):
        return ""
    if "@" in low and not low.endswith(_WA_PERSONAL_DOMAINS):
        return ""
    head = low.split("@", 1)[0].split(":", 1)[0]      # 去设备后缀 6391…:12
    if not re.fullmatch(r"\+?\d{7,15}", head):
        return ""
    return head.lstrip("+")


def should_lookup(user_context: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> bool:
    """是否查网关：总开关 + visible_facts 都开，且有渠道核实的本人号。**不再看正文数字**。"""
    if not gateway_ready(cfg) or not visible_facts_enabled(cfg):
        return False
    return bool(verified_phone(user_context))


def fetch_lookup(cfg: Dict[str, Any], *, phone: str) -> Optional[Dict[str, Any]]:
    """只按核实过的本人号查；``bind=False``；不发对话原文。"""
    if not phone:
        return None
    base = str(cfg.get("url") or "").rstrip("/") + "/"
    url = urljoin(base, "lookup")
    payload = json.dumps({"phone": phone, "bind": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gateway-Key": str(cfg.get("key") or ""),
            "Accept": "application/json",
            "User-Agent": "chatx-player-gateway/1.0",
        },
    )
    timeout = float(cfg.get("timeout_sec") or 8)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None
    return None


# ── 金额脱敏 ──────────────────────────────────────────────────────────────
_FIN_KW = (
    r"(?:balance|bal|deposit(?:ed|s)?|withdraw(?:al|als|n|ed)?|cash\s*-?\s*(?:out|in)|top\s*-?\s*up"
    r"|reload|rollover|turnover|wager(?:ing)?|play\s*-?\s*through|bonus|wallet|credits?|winnings?"
    r"|bet(?:s|ting)?\s+amount|valid\s+bets?|panalo|natalo|laman|pera\s+mo|load"
    r"|余额|餘額|充值|提现|提現|打码|打碼|流水|存款|取款|充提|赢了|输了|下注额)"
)
_NUM = r"(?:[₱$¥€]\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:k|php|pesos?|piso|usd|usdt|rmb)(?![a-z])|\s*(?:元|块))?"
_KW_THEN_NUM = re.compile(_FIN_KW + r"(?P<gap>[^\n\d]{0,25}?)(?P<num>" + _NUM + r")", re.IGNORECASE)
_NUM_THEN_KW = re.compile(r"(?P<num>" + _NUM + r")(?P<gap>\s{0,3}(?:sa\s+|in\s+|of\s+)?)" + _FIN_KW, re.IGNORECASE)
_CURRENCY_AMT = re.compile(
    r"(?:[₱$¥€]\s*\d[\d,]*(?:\.\d+)?|(?:php|usd|usdt|rmb)\s*\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:php|pesos?|piso|usdt|元))(?![\w])",
    re.IGNORECASE,
)
MASK = "***"


def redact_financials(text: Any) -> Tuple[str, int]:
    """余额 / 充提 / 打码 / 流水 / 带币种的金额 → ``***``。返回 ``(脱敏后文本, 替换次数)``。纯函数、绝不抛。"""
    s = str(text or "")
    if not s:
        return s, 0
    n = 0
    try:
        def _kw_num(m: "re.Match[str]") -> str:
            nonlocal n
            n += 1
            whole = m.group(0)
            return whole[: m.start("num") - m.start(0)] + MASK

        def _num_kw(m: "re.Match[str]") -> str:
            nonlocal n
            n += 1
            whole = m.group(0)
            return MASK + whole[m.end("num") - m.start(0):]

        def _cur(m: "re.Match[str]") -> str:
            nonlocal n
            n += 1
            return MASK

        s = _KW_THEN_NUM.sub(_kw_num, s)
        s = _NUM_THEN_KW.sub(_num_kw, s)
        s = _CURRENCY_AMT.sub(_cur, s)
    except Exception:
        logger.debug("[player-gateway] 脱敏异常（保留原文）", exc_info=True)
        return str(text or ""), 0
    return s, n


def redaction_required(user_context: Any, config: Any = None) -> bool:
    """出站是否必须做金额脱敏（红线「博彩召回回述真人余额」）。

    本轮注入过玩家网关事实（``_player_data_block``）**或**本实例开了 ``player_gateway.enabled``
    （前几轮的事实可能还在对话历史里被模型复述）→ True。与任何守卫跳过开关无关。绝不抛。"""
    try:
        if isinstance(user_context, dict) and user_context.get("_player_data_block"):
            return True
        return bool(_truthy(player_gateway_cfg(config).get("enabled")))
    except Exception:
        return False


def enforce_outbound_redaction(reply: Any, user_context: Any = None, config: Any = None,
                               *, path: str = "") -> Tuple[str, int]:
    """出站金额脱敏的**唯一**执法入口（A/B 两线都在守卫跳过开关之外调用）。

    命中即替换为 ``***`` 并留痕（WARNING ``[player-gateway] action=redact``，不记原文）；
    返回 ``(文本, 替换次数)``。不要求脱敏 / 无命中 → 原样返回。绝不抛。"""
    s = str(reply or "")
    if not s or not redaction_required(user_context, config):
        return s, 0
    red, n = redact_financials(s)
    if n:
        logger.warning("[player-gateway] action=redact path=%s count=%d platform=%s",
                       path or "-", n, str((user_context or {}).get("platform") or "-")
                       if isinstance(user_context, dict) else "-")
    return red, n


_FACTS_RULE = (
    "【玩家后台资料 · 只读】身份已由 WhatsApp 本人号核实。金额类信息（余额 / 充值 / 提现 / 打码 / 流水）"
    "一律不在对话里复述，已打码为 ***；玩家问到时请引导到官方 App 内查看。"
)


def _tail4(phone: str) -> str:
    return ("…" + phone[-4:]) if phone else "-"


def inject_player_block(user_context: Dict[str, Any], text: str, config: Any) -> None:
    """有键即消费：写入 ``_player_data_block``；不满足条件或失败则清残留。不抛。

    ``text``（对话原文）**不再参与**是否查询的判定，也不发给网关——保留形参只为调用方兼容。
    """
    user_context.pop("_player_data_block", None)
    try:
        cfg = player_gateway_cfg(config)
        if not gateway_ready(cfg):
            return
        plat = str(user_context.get("platform") or "-")
        phone = verified_phone(user_context)
        if not visible_facts_enabled(cfg):
            logger.info("[player-gateway] action=skip reason=visible_facts_off platform=%s", plat)
            return
        if not phone:
            logger.info("[player-gateway] action=skip reason=unverified_identity platform=%s", plat)
            return
        data = fetch_lookup(cfg, phone=phone)
        chatx = (data or {}).get("chatx_text") if isinstance(data, dict) else ""
        if not chatx:
            logger.info("[player-gateway] action=lookup result=empty platform=%s phone=%s", plat, _tail4(phone))
            return
        red, n = redact_financials(str(chatx)[:4000])
        user_context["_player_data_block"] = _FACTS_RULE + "\n" + red
        logger.info("[player-gateway] action=lookup result=injected platform=%s phone=%s redacted=%d",
                    plat, _tail4(phone), n)
    except Exception:
        user_context.pop("_player_data_block", None)
        return


__all__ = [
    "player_gateway_cfg", "gateway_ready", "visible_facts_enabled", "verified_phone",
    "should_lookup", "fetch_lookup", "redact_financials", "inject_player_block", "MASK",
    "redaction_required", "enforce_outbound_redaction",
]
