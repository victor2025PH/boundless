# -*- coding: utf-8 -*-
"""P2.1 (2026-04-30): 逐人个性化话术 AI 生成 — Facebook 引流核心模块。

定位
─────
解决"千人一面 batch 模板话术"问题。每位目标用户 → 拿其 name/bio/recent_posts/group_context →
调本地 Ollama (qwen2.5:7b 默认) → 生成 **她个人会感兴趣 + 强制目标语言** 的验证语 / 打招呼。

公开 API
─────
    generate_message(target, persona, purpose, output_lang) -> (text, metadata)

    target:        TargetUser dataclass (name, bio, recent_posts, group_context)
    persona:       PersonaContext dataclass (bio, language, interest_topics)
    purpose:       'verification_note' | 'first_greeting' | 'followup'
    output_lang:   'ja-JP' | 'zh-CN' | 'en-US' (强制断言，不符自动 retry 1 次)

    返回 (text:str, metadata:dict). text 失败时 fallback 到模板, metadata 标 fallback=True.

设计关键
─────
1. **语言后检 (lang_verify)**: 用 unicode 脚本占比断言，不依赖 langdetect (零依赖)。
   日语必须含至少 1 个假名 (ひらがな/カタカナ)。
2. **长度上限**: verification_note ≤ 60 字符 (FB 限制), greeting ≤ 200。
3. **内容审计**: 黑名单关键字 (链接 / 促销词 / @ 账号引导)。
4. **fallback 链**: LLM 失败 → 模板兜底 (按 persona.language 选)。
5. **零依赖部署**: urllib.request + Ollama HTTP 即可, 不引入 langchain。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 配置 ───────────────────────────────────────────────────────────
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_TIMEOUT_S = 30
OLLAMA_TIMEOUT_S_FALLBACK_TRY = 15  # 重试时缩短超时

# 模型优先级 — 列表里靠前的命中即用。文本生成场景偏好纯文本模型
# (qwen2.5 / gemma) 而非 VLM (qwen2.5vl)。运营可在不同环境改 ollama 时无需改代码。
OLLAMA_MODEL_PREFERENCE = [
    "qwen2.5:7b", "qwen2.5:latest", "qwen2.5:14b",
    "gemma4:latest", "gemma2:9b", "llama3.1:8b", "llama3.2:3b",
]
OLLAMA_MODEL_DEFAULT = OLLAMA_MODEL_PREFERENCE[0]  # 历史兼容: 让外部 import 仍可读

# 模型探测结果缓存. None=未探测; ""=探测过但 Ollama 不可用; "model:tag"=已确定
_RESOLVED_MODEL: Optional[str] = None
_RESOLVED_AT_TS: float = 0.0
_RESOLVE_TTL_S = 300  # 5 分钟内复用探测结果, 避免每条 message 调一次 /api/tags


def _resolve_model(preferred: Optional[str] = None) -> Optional[str]:
    """探测 Ollama 上可用的最优模型。

    - preferred: 调用方显式传的模型名, 验证存在则直接用
    - 否则: 按 OLLAMA_MODEL_PREFERENCE 顺序选第一个存在的
    - 5 分钟缓存; ollama 不可用返 None (调用方走 fallback)
    """
    global _RESOLVED_MODEL, _RESOLVED_AT_TS
    import time as _t
    now = _t.time()
    # 缓存仍有效且未指定显式 preferred → 直接返
    if (preferred is None and _RESOLVED_MODEL is not None
            and (now - _RESOLVED_AT_TS) < _RESOLVE_TTL_S):
        return _RESOLVED_MODEL or None

    # 探测 ollama
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            available = {m.get("name", "") for m in data.get("models", [])}
    except Exception as e:
        logger.debug("[personalized_message] _resolve_model: Ollama 不可用 (%s)", e)
        _RESOLVED_MODEL = ""  # 标记探测过但失败, 避免重复打扰
        _RESOLVED_AT_TS = now
        return None

    # 显式传的优先
    if preferred and preferred in available:
        _RESOLVED_MODEL = preferred
        _RESOLVED_AT_TS = now
        return preferred

    # 按优先级匹配
    for cand in OLLAMA_MODEL_PREFERENCE:
        if cand in available:
            _RESOLVED_MODEL = cand
            _RESOLVED_AT_TS = now
            logger.info("[personalized_message] resolved Ollama model: %s "
                         "(available: %d)", cand, len(available))
            return cand

    # 一个都没匹配上 → 返第一个 ollama 上的非 vlm 文本模型
    for name in sorted(available):
        if "vl" not in name.lower() and "embed" not in name.lower():
            _RESOLVED_MODEL = name
            _RESOLVED_AT_TS = now
            logger.warning("[personalized_message] no preference match, "
                            "falling back to first text model: %s", name)
            return name

    _RESOLVED_MODEL = ""
    _RESOLVED_AT_TS = now
    return None

LENGTH_LIMITS = {
    "verification_note": 60,    # FB 好友请求验证语字数限制
    "first_greeting": 200,      # 通过后打招呼
    "followup": 300,
}

# 内容审计黑名单 — 命中即视为 LLM 输出"违规", 走 fallback
# 包含基本链接、促销引导、敏感符号。
BLACKLIST_PATTERNS = [
    r"https?://",
    r"wechat:|line:|telegram:|whatsapp:",
    r"@[a-zA-Z0-9_]{3,}",        # @username 引导
    r"免费|限时|优惠|赚钱|代购",       # 中文促销
    r"無料|限定|お得|稼ぐ",            # 日文促销
    r"free\s+money|earn\s+\$",   # 英文
]
_BLACKLIST_RE = re.compile("|".join(BLACKLIST_PATTERNS), re.IGNORECASE)


# ── 数据类 ─────────────────────────────────────────────────────────
@dataclass
class TargetUser:
    """目标用户的可用上下文。任何字段缺失都允许 (LLM 会自适应)。"""
    name: str = ""
    bio: str = ""
    recent_posts: List[str] = field(default_factory=list)
    group_context: str = ""    # 例如 "ママ友サークル"
    avatar_hint: str = ""      # VLM 头像提示 (可选, 例如 "smiling middle-aged Japanese woman")


@dataclass
class PersonaContext:
    """我方人设上下文。"""
    bio: str = ""              # 我方人设描述, 例如 "日本东京的中年家庭主妇,喜欢..."
    language: str = "ja-JP"
    interest_topics: List[str] = field(default_factory=list)


# ── 语言后检 ──────────────────────────────────────────────────────
def _has_hiragana_or_katakana(text: str) -> bool:
    """日语必备特征: 至少 1 个假名 (汉字单独不够,中日同形)。"""
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F:   # ひらがな
            return True
        if 0x30A0 <= cp <= 0x30FF:   # カタカナ
            return True
    return False


def _has_chinese(text: str) -> bool:
    """中文: 至少 1 个 CJK 汉字 (无假名)"""
    has_han = any(0x4E00 <= ord(c) <= 0x9FFF for c in text)
    return has_han and not _has_hiragana_or_katakana(text)


def _is_mostly_ascii(text: str, threshold: float = 0.8) -> bool:
    """英语/罗马字: ASCII 字符占比 > threshold"""
    if not text:
        return False
    ascii_ct = sum(1 for c in text if ord(c) < 128)
    return ascii_ct / len(text) >= threshold


def verify_language(text: str, expected: str) -> bool:
    """断言文本符合目标语言。
    - ja-JP: 必须含假名
    - zh-CN: 必须有 CJK 但不含假名
    - en-US/en: 必须 ASCII 占比 ≥80%
    其他语言: 直接返回 True (暂不检测)。
    """
    if not text or not expected:
        return False
    e = expected.lower()
    if e.startswith("ja"):
        return _has_hiragana_or_katakana(text)
    if e.startswith("zh"):
        return _has_chinese(text)
    if e.startswith("en"):
        return _is_mostly_ascii(text)
    return True


# ── 内容审计 ──────────────────────────────────────────────────────
def audit_content(text: str) -> Tuple[bool, str]:
    """返回 (合规, 违规原因)。"""
    if not text:
        return False, "empty"
    m = _BLACKLIST_RE.search(text)
    if m:
        return False, f"blacklist:{m.group(0)[:30]}"
    return True, ""


# ── Prompt 构建 ────────────────────────────────────────────────────
def _lang_label(code: str) -> str:
    """language code → LLM prompt 中的自然语描述"""
    m = {"ja-JP": "日本語 (Japanese)", "zh-CN": "简体中文",
         "en-US": "English", "ko-KR": "한국어"}
    return m.get(code, code)


def _purpose_label(purpose: str, lang: str) -> str:
    """purpose 在 prompt 中的语义描述"""
    table = {
        "verification_note": {
            "ja": "Facebook 友達リクエスト時の挨拶メッセージ (60字以内)",
            "zh": "Facebook 好友请求验证语 (60 字以内)",
            "en": "Facebook friend request verification note (under 60 chars)",
        },
        "first_greeting": {
            "ja": "友達承認後の最初の挨拶メッセージ",
            "zh": "好友通过后第一句打招呼话术",
            "en": "First greeting after friend request accepted",
        },
        "followup": {
            "ja": "数日後のフォローアップメッセージ",
            "zh": "几天后的跟进话术",
            "en": "Follow-up message after a few days",
        },
    }
    short = "ja" if lang.startswith("ja") else "zh" if lang.startswith("zh") else "en"
    return table.get(purpose, {}).get(short, purpose)


def build_prompt(target: TargetUser, persona: PersonaContext,
                 purpose: str, output_lang: str) -> str:
    """构造给 LLM 的 prompt。强调:
    1. 输出语言锁定
    2. 个性化提点 (target 的具体信息)
    3. 长度限制
    4. 反营销审计
    """
    lim = LENGTH_LIMITS.get(purpose, 100)
    posts_snippet = "; ".join((target.recent_posts or [])[:3]) or "（无）"
    return f"""You are role-playing as: {persona.bio or "a friendly user"}.
Your output language MUST be: {_lang_label(output_lang)}.
Target task: {_purpose_label(purpose, output_lang)}.

Target user info (write a message that resonates with HER specifically):
- Display name: {target.name or "Unknown"}
- Bio: {target.bio or "(none)"}
- Recent posts/topics: {posts_snippet}
- Group context (where you met): {target.group_context or "(none)"}
- Avatar hint: {target.avatar_hint or "(none)"}

Strict rules:
1. Write ONLY the message text. No quotes, no headers, no explanations.
2. {lim} characters maximum (count carefully).
3. Output language: {_lang_label(output_lang)}. NEVER use any other language.
4. Reference at least one specific detail from her info (group / topic / bio).
5. Use 1-2 emojis naturally; tone friendly, NOT salesy.
6. NEVER include URLs, account handles, or promotional words.
7. Keep it natural and conversational, like a real person."""


# ── LLM 调用 ──────────────────────────────────────────────────────
def _call_ollama_generate(prompt: str, model: str = OLLAMA_MODEL_DEFAULT,
                           timeout: int = OLLAMA_TIMEOUT_S) -> Optional[str]:
    """直连 Ollama /api/generate, 非流式。失败返 None。"""
    try:
        body = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.85, "top_p": 0.9, "num_predict": 200},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            text = (data.get("response") or "").strip()
            return text or None
    except Exception as e:
        logger.warning("[personalized_message] Ollama 调用失败: %s", e)
        return None


# ── Fallback 模板 ─────────────────────────────────────────────────
# LLM 不可用时按 (purpose, language) 选择安全兜底文本。运营可后续抽到 YAML。
_FALLBACK_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "verification_note": {
        "ja": [
            "はじめまして🌸 同じグループでお見かけして、よろしければお友達に♪",
            "こんにちは✨ グループ繋がりです、よろしくお願いします😊",
            "突然すみません🌷 同じ趣味のようで、仲良くしていただけたら嬉しいです",
        ],
        "zh": [
            "你好🌸 看到我们在同一个群里，方便加个好友吗？",
            "嗨✨ 群里看到你，希望可以认识一下😊",
        ],
        "en": [
            "Hi 🌸 saw you in the same group, would love to connect!",
            "Hello ✨ found you through our shared group, nice to meet you 😊",
        ],
    },
    "first_greeting": {
        "ja": [
            "ご承認ありがとうございます😊 これからよろしくお願いします🌸",
            "リクエスト通していただき嬉しいです✨ 仲良くしてくださいね",
        ],
        "zh": [
            "谢谢通过🌸 以后多多交流哦😊",
        ],
        "en": [
            "Thanks for accepting 🌸 nice to meet you!",
        ],
    },
    "followup": {
        "ja": ["お元気ですか？✨ 最近いかがお過ごしですか？"],
        "zh": ["最近怎么样呀？✨"],
        "en": ["How have you been? ✨"],
    },
}


def _fallback_text(purpose: str, output_lang: str,
                   target: TargetUser) -> str:
    short = "ja" if output_lang.startswith("ja") else \
            "zh" if output_lang.startswith("zh") else "en"
    candidates = _FALLBACK_TEMPLATES.get(purpose, {}).get(short, [])
    if not candidates:
        candidates = _FALLBACK_TEMPLATES["verification_note"][short]
    # 用 target.name 长度做简单 hash, 让同一个目标拿到稳定的模板
    idx = (len(target.name or "") + sum(map(ord, target.name or ""))) \
        % len(candidates)
    return candidates[idx]


# ── 公开 API ──────────────────────────────────────────────────────
def generate_message(target: TargetUser, persona: PersonaContext,
                     purpose: str = "verification_note",
                     output_lang: str = "ja-JP",
                     model: Optional[str] = None,
                     ) -> Tuple[str, Dict[str, Any]]:
    """逐人话术生成 — 主入口。

    返回 (text, metadata):
      metadata = {
        "model": "qwen2.5:latest",   # 实际解析到的模型
        "lang_verified": True/False,
        "audit_ok": True/False,
        "fallback": False,            # True 表示用了模板兜底
        "fallback_reason": "...",
        "attempts": 1,
        "length": 42,
      }

    model 参数为 None 时自动解析 Ollama 上可用的最优模型 (按优先级)。
    传具体值时仍按显式优先, 不存在则解析失败走 fallback。
    """
    out_lang = output_lang or persona.language or "ja-JP"
    prompt = build_prompt(target, persona, purpose, out_lang)
    # P2.1 修复 (2026-04-30): lazy 解析 Ollama 模型, 避免硬编码不存在的 qwen2.5:7b
    resolved = _resolve_model(model)
    metadata: Dict[str, Any] = {
        "model": resolved or (model or "(none)"),
        "fallback": False, "attempts": 0,
        "length": 0, "lang_verified": False, "audit_ok": False,
        "fallback_reason": "",
    }

    if not resolved:
        # Ollama 不可用 / 无可用模型 → 直接走 fallback, 不浪费 HTTP 调用
        metadata["fallback_reason"] = "first:no_model_available"
        fb_text = _fallback_text(purpose, out_lang, target)
        metadata.update({
            "fallback": True,
            "lang_verified": verify_language(fb_text, out_lang),
            "audit_ok": audit_content(fb_text)[0],
            "length": len(fb_text),
        })
        return fb_text, metadata

    # 第 1 次尝试
    metadata["attempts"] = 1
    text = _call_ollama_generate(prompt, model=resolved)
    if text and verify_language(text, out_lang):
        ok, why = audit_content(text)
        if ok and len(text) <= LENGTH_LIMITS.get(purpose, 100):
            metadata.update({"lang_verified": True, "audit_ok": True,
                             "length": len(text)})
            return text, metadata
        metadata["fallback_reason"] = f"first:{why or 'too_long'}"
    elif text:
        metadata["fallback_reason"] = "first:lang_mismatch"
    else:
        metadata["fallback_reason"] = "first:llm_unavailable"

    # 第 2 次尝试 — prompt 加强语言约束 (语言 mismatch 场景才有意义)
    if text and not verify_language(text, out_lang):
        retry_prompt = (
            prompt
            + f"\n\n[CRITICAL] Previous attempt was in wrong language. "
              f"Output MUST be in {_lang_label(out_lang)} ONLY."
        )
        metadata["attempts"] = 2
        text2 = _call_ollama_generate(retry_prompt, model=resolved,
                                      timeout=OLLAMA_TIMEOUT_S_FALLBACK_TRY)
        if text2 and verify_language(text2, out_lang):
            ok2, why2 = audit_content(text2)
            if ok2 and len(text2) <= LENGTH_LIMITS.get(purpose, 100):
                metadata.update({"lang_verified": True, "audit_ok": True,
                                 "length": len(text2)})
                return text2, metadata

    # 兜底模板
    fb_text = _fallback_text(purpose, out_lang, target)
    metadata.update({
        "fallback": True,
        "lang_verified": verify_language(fb_text, out_lang),
        "audit_ok": audit_content(fb_text)[0],
        "length": len(fb_text),
    })
    if not metadata["fallback_reason"]:
        metadata["fallback_reason"] = "unknown"
    return fb_text, metadata


__all__ = [
    "TargetUser", "PersonaContext",
    "generate_message", "verify_language", "audit_content", "build_prompt",
]
