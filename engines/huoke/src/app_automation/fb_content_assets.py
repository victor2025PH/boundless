# -*- coding: utf-8 -*-
"""Facebook 文案包读取层（2026-04-22 P0 Sprint 新建）。

职责
----
把 automation 层需要的 4 类本地化文案集中起来：

1. **加好友验证语**（``verification_note`` / ``friend_request_notes``）
2. **首次开场 DM**（``greeting_messages``）
3. **群内评论模板**（``comment_templates``）
4. **引流切换话术**（``referral_{line,whatsapp,instagram,telegram}``）

调用方只需要传入 ``persona_key`` 或 ``country_code``，本模块负责：

* 按 ``persona.country_code`` 查 ``chat_messages.yaml.countries[cc]``，
  未命中时回退到全局 legacy 字段（旧版 greeting_messages 等）。
* 支持 ``{name}`` / ``{interest}`` / ``{line}`` / ``{whatsapp}`` 等占位符。
* 文案库空时仍然返回 **语言合规** 的最小兜底（日本人群返回日文兜底，其他默认英文），
  保证"文案丢失不会让业务退化成发意大利文给日本女性"。

配置热加载：走 ``_yaml_cache.YamlCache``，改完保存即生效。

设计原则
--------
* **纯函数** —— 不修改传入参数、无全局状态、易于单测。
* **不抛异常** —— 任何失败返回合理兜底，因为业务方法的失败链路不应该因为
  "文案读不到"而崩。日志 warning，但继续。
* **单一入口** —— 不让 automation 层各自实现读取 yaml 的逻辑。
"""

from __future__ import annotations

import copy
import logging
import random
from typing import Any, Dict, List, Optional

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_chat_msg_path = config_file("chat_messages.yaml")


# ── 最小语种兜底 ─────────────────────────────────────────────────────
# 当 YAML 读取失败 + persona 对应的国家没配文案时，用这里的内置值。
# 故意做得"够用但保守"，让业务方即使完全拿不到 YAML 也能运行。
_LOCALE_FALLBACKS: Dict[str, Dict[str, Any]] = {
    "ja": {
        "friend_request_notes": [
            "はじめまして🌸よろしくお願いします",
            "こんにちは！仲良くしてください🌿",
        ],
        "greeting_messages": [
            "はじめまして😊つながれて嬉しいです🌸",
            "友達になってくれてありがとうございます🌿",
        ],
        "comment_templates": [
            "素敵ですね🌸",
            "ありがとうございます🌿",
            "共感します✨",
        ],
        "referral_line": ["LINE もやっています、よかったらどうぞ → {value}"],
        "referral_instagram": ["Instagram でもお話できます → {value}"],
        "referral_whatsapp": ["WhatsApp でもどうぞ → {value}"],
        "referral_telegram": ["Telegram でもどうぞ → {value}"],
    },
    "it": {
        "friend_request_notes": [
            "Ciao {name}! Piacere di conoscerti 🌟",
        ],
        "greeting_messages": [
            "Ciao {name}! Grazie per l'accettazione, piacere!",
        ],
        "comment_templates": [
            "Interessante!",
            "Grazie per la condivisione.",
        ],
        "referral_whatsapp": ["Scrivimi su WhatsApp: {value}"],
        "referral_telegram": ["Contattami su Telegram: {value}"],
        "referral_line": ["LINE: {value}"],
        "referral_instagram": ["Su Instagram: {value}"],
    },
    # 默认（英文）兜底：任何未特别处理的语言都走这个
    "default": {
        "friend_request_notes": [
            "Hi {name}, nice to meet you!",
        ],
        "greeting_messages": [
            "Hi {name}! Thanks for the connection.",
        ],
        "comment_templates": [
            "Interesting!",
            "Thanks for sharing.",
            "Great point.",
        ],
        "referral_line": ["Feel free to reach me on LINE: {value}"],
        "referral_instagram": ["Also on Instagram: {value}"],
        "referral_whatsapp": ["Or WhatsApp me: {value}"],
        "referral_telegram": ["Or on Telegram: {value}"],
    },
}


# ── YAML 缓存 ────────────────────────────────────────────────────────

def _post_process(raw: Any) -> Dict[str, Any]:
    """把 chat_messages.yaml 标准化成固定结构。"""
    if not isinstance(raw, dict):
        return {"countries": {}, "legacy": {}}
    return {
        "countries": raw.get("countries") or {},
        # legacy 字段：顶层 greeting_messages / referral_whatsapp 等（意大利项目遗留）
        "legacy": {
            "country": raw.get("country"),
            "greeting_messages": raw.get("greeting_messages") or [],
            "message_variants": raw.get("message_variants") or [],
            "messages": raw.get("messages") or [],
            "referral_telegram": raw.get("referral_telegram") or [],
            "referral_whatsapp": raw.get("referral_whatsapp") or [],
        },
        "device_referrals": raw.get("device_referrals") or {},
    }


_CACHE = YamlCache(
    path=_chat_msg_path,
    defaults={"countries": {}, "legacy": {}, "device_referrals": {}},
    post_process=_post_process,
    log_label="chat_messages.yaml(content_assets)",
    logger=logger,
)


def load_assets(force_reload: bool = False) -> Dict[str, Any]:
    return _CACHE.get(force_reload=force_reload)


def reload_assets() -> Dict[str, Any]:
    return _CACHE.reload()


# ── persona 解析辅助 ─────────────────────────────────────────────────
# 国家 → 主流语言（仅当未显式传 language 时用，避免 US+默认 persona 误绑成日文）
_COUNTRY_DEFAULT_LANG: Dict[str, str] = {
    "jp": "ja", "ja": "ja",
    "it": "it",
    "us": "en", "gb": "en", "au": "en", "ca": "en", "nz": "en", "ie": "en",
    "de": "de", "fr": "fr", "es": "es", "br": "pt", "mx": "es",
}


def _resolve_context(persona_key: Optional[str],
                     country_code: Optional[str] = None,
                     language: Optional[str] = None) -> Dict[str, str]:
    """把 persona_key 转换成 ``{country_code, language, persona_key}``。

    查询优先级：
      1. **显式传入 ``language``（非 None）** → 语种不被 persona 覆盖
      2. 仅有 ``country_code``、未显式 language → ``_COUNTRY_DEFAULT_LANG`` → 再有缺口则 persona
      3. 通过 ``get_persona_display`` 补全剩余字段
      4. language 仍空则 ``en``
    """
    cc = (country_code or "").lower()
    lang_explicit = language is not None
    lang = (language or "").lower() if lang_explicit else ""
    if cc and not lang_explicit and not lang:
        lang = _COUNTRY_DEFAULT_LANG.get(cc, "")
    if not cc or not lang_explicit and not lang:
        try:
            from src.host.fb_target_personas import get_persona_display
            p = get_persona_display(persona_key)
            if not cc:
                cc = str(p.get("country_code") or "").lower()
            if not lang_explicit:
                lang = (lang or str(p.get("language") or "")).lower()
            persona_key = persona_key or p.get("persona_key") or ""
        except Exception as e:
            logger.debug("[content_assets] persona 解析失败: %s", e)
    return {
        "country_code": cc,
        "language": lang or "en",
        "persona_key": persona_key or "",
    }


def _fallback_for_lang(lang: str) -> Dict[str, Any]:
    """返回语种兜底包；未命中用 default（英文）。"""
    return copy.deepcopy(_LOCALE_FALLBACKS.get(lang) or _LOCALE_FALLBACKS["default"])


def _country_bundle(cc: str) -> Dict[str, Any]:
    """读 YAML 里 countries[cc] 的整段。没有返回 {}。"""
    cc = (cc or "").lower()
    if not cc:
        return {}
    assets = load_assets()
    bundle = (assets.get("countries") or {}).get(cc)
    return bundle if isinstance(bundle, dict) else {}


def _safe_format(template: str, **fields) -> str:
    """像 str.format 但缺字段不会 KeyError。空字段被替换成空串。"""
    class _SafeDict(dict):
        def __missing__(self, key):
            return ""
    try:
        return str(template).format_map(_SafeDict(**fields))
    except Exception:
        return str(template)


# ── 对外 API（automation 层只调这些）─────────────────────────────────

def get_verification_note(persona_key: Optional[str] = None,
                          name: str = "",
                          interest_hint: str = "",
                          country_code: Optional[str] = None,
                          language: Optional[str] = None) -> str:
    """加好友验证语（单条，随机选）。

    参数
    ----
    persona_key
        目标客群 key。为空时走默认客群（通常 = jp_female_midlife）。
    name
        对方昵称，用于替换模板里的 {name}。
    interest_hint
        共同兴趣提示词（日文 persona 常用）；YAML 模板可以写 {interest}。
    country_code
        显式覆盖（测试用）。
    language
        显式语种（``ja``/``en``/…）；非 ``None`` 时**不**用 persona 覆盖语种。

    返回
    ----
    始终返回 **非空字符串**（即使 YAML 完全读不到也从内置兜底取）。
    """
    ctx = _resolve_context(persona_key, country_code=country_code, language=language)
    # 从 YAML 国家包取
    notes = (_country_bundle(ctx["country_code"]).get("friend_request_notes")
             or [])
    if not notes:
        # 回退到语种兜底
        notes = _fallback_for_lang(ctx["language"]).get("friend_request_notes") or []
    if not notes:
        return ""
    template = random.choice(notes)
    return _safe_format(template,
                        name=name or "",
                        interest=interest_hint or "")


def get_greeting_message(persona_key: Optional[str] = None,
                         name: str = "",
                         country_code: Optional[str] = None,
                         language: Optional[str] = None) -> str:
    """首次开场 DM（Messenger 对话第一条）。"""
    text, _tid = get_greeting_message_with_id(
        persona_key=persona_key, name=name,
        country_code=country_code, language=language)
    return text


def get_greeting_message_with_id(persona_key: Optional[str] = None,
                                 name: str = "",
                                 country_code: Optional[str] = None,
                                 language: Optional[str] = None
                                 ) -> tuple:
    """同 :func:`get_greeting_message` 但额外返回模板 ID,供 A/B 统计。

    Returns:
        (text, template_id):
          * text = 渲染后的文案字符串(可能为空)
          * template_id = "<src>:<cc_or_lang>:<idx>" 格式,如
            "yaml:jp:3"(YAML 国家包第 3 条) / "fallback:ja:1"(日文兜底第 1 条)
            / "" 表示根本没有拿到模板
    """
    ctx = _resolve_context(persona_key, country_code=country_code, language=language)
    src = "yaml"
    key = ctx["country_code"]
    msgs = (_country_bundle(ctx["country_code"]).get("greeting_messages")
            or [])
    if not msgs:
        src = "fallback"
        key = ctx["language"]
        msgs = _fallback_for_lang(ctx["language"]).get("greeting_messages") or []
    if not msgs:
        return "", ""
    idx = random.randrange(len(msgs))
    tpl = msgs[idx]
    template_id = f"{src}:{key}:{idx}"
    return _safe_format(tpl, name=name or ""), template_id


def get_comment_pool(persona_key: Optional[str] = None,
                     country_code: Optional[str] = None,
                     min_size: int = 3,
                     language: Optional[str] = None) -> List[str]:
    """群内/帖子下评论模板池。

    返回完整的列表（不随机选，让调用方自己 choose），
    如果不足 min_size 条，会从语种兜底补齐。
    """
    ctx = _resolve_context(persona_key, country_code=country_code, language=language)
    pool = list((_country_bundle(ctx["country_code"]).get("comment_templates")
                 or []))
    if len(pool) < min_size:
        fb = _fallback_for_lang(ctx["language"]).get("comment_templates") or []
        for item in fb:
            if item not in pool:
                pool.append(item)
            if len(pool) >= min_size:
                break
    return pool


def get_referral_snippet(channel: str,
                         value: str,
                         persona_key: Optional[str] = None,
                         country_code: Optional[str] = None,
                         language: Optional[str] = None,
                         peer_name: str = "",
                         age_band: str = "",
                         gender: str = "") -> str:
    """引流切换话术（在 DM 里发 LINE ID / WA 号等场景用）。

    channel 可以是 ``line`` / ``instagram`` / ``whatsapp`` / ``telegram``。
    value 是要发送的 ID/号码。返回拼好的完整句子。

    Phase 12.1 (2026-04-25): 支持 {peer_name} / {age_band} / {gender} 占位符,
    让引流话术个性化 (例 "{peer_name}さん、LINE で繋がりませんか: {line}").
    上层 dispatcher 可把 canonical.metadata 传入. 占位符缺失用空串不报错.
    """
    channel = (channel or "").lower().strip()
    if not value:
        return ""
    ctx = _resolve_context(persona_key, country_code=country_code, language=language)
    cb = _country_bundle(ctx["country_code"])
    tpls: List[str] = []
    for mv in cb.get("message_variants") or []:
        ref = mv.get(f"referral_{channel}") if isinstance(mv, dict) else None
        if ref:
            tpls.extend(ref)
    direct = cb.get(f"referral_{channel}") or cb.get(f"referral_{channel}_templates")
    if direct:
        tpls.extend(direct)
    if not tpls:
        tpls = _fallback_for_lang(ctx["language"]).get(f"referral_{channel}") or []
    if not tpls:
        return f"{channel}: {value}"
    template = random.choice(tpls)
    # 模板占位符: {value} / {line}/{whatsapp}/... (channel 自身), 以及
    # {peer_name}/{age_band}/{gender}/{name} (Phase 12.1 个性化)
    return _safe_format(template,
                        value=value,
                        line=value if channel == "line" else "",
                        whatsapp=value if channel == "whatsapp" else "",
                        instagram=value if channel == "instagram" else "",
                        telegram=value if channel == "telegram" else "",
                        peer_name=peer_name or "",
                        name=peer_name or "",  # alias
                        age_band=age_band or "",
                        gender=gender or "")


# ── 诊断辅助（供 /facebook/content-assets/debug 接口或单测使用）──────

def describe_assets_for_persona(persona_key: Optional[str] = None,
                                language: Optional[str] = None) -> Dict[str, Any]:
    """返回诊断包：对当前 persona 能拿到哪些文案、走了哪个来源。

    不做随机选择，方便 e2e 测试 stable 断言。
    """
    ctx = _resolve_context(persona_key, language=language)
    cb = _country_bundle(ctx["country_code"])
    fb = _fallback_for_lang(ctx["language"])
    return {
        "context": ctx,
        "source": "yaml.countries" if cb else f"fallback.locale.{ctx['language']}",
        "verification_note_count": len(
            (cb.get("friend_request_notes") if cb else None)
            or fb.get("friend_request_notes") or []
        ),
        "greeting_count": len(
            (cb.get("greeting_messages") if cb else None)
            or fb.get("greeting_messages") or []
        ),
        "comment_pool_size": len(get_comment_pool(persona_key, language=language)),
        "referral_channels_available": sorted([
            ch for ch in ("line", "instagram", "whatsapp", "telegram")
            if bool(
                (cb.get(f"referral_{ch}") if cb else None)
                or fb.get(f"referral_{ch}")
            )
            or any(
                isinstance(mv, dict) and mv.get(f"referral_{ch}")
                for mv in (cb.get("message_variants") or [])
            )
        ]),
    }
