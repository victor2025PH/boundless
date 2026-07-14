"""全自动出站翻译（L2 autosend 投递前把 AI 中文回复译成客户语言）。

补「全自动聊天翻译」闭环的最后一环：此前 autosend worker 把 AI 生成的（中文）草稿
**原样**投递到客户平台——外语客户会直接收到中文。本模块在投递前把草稿文本经统一
``TranslationService``（术语表 + TM + 语检 + 多引擎 failover）译成会话客户语言，并记录
出向译文映射（供 thread 双行展示），译完再交回 worker 真发。

设计：
  - **纯决策函数**（``normalize_target`` / ``should_translate`` / ``parse_outbound_translate_cfg``）
    零副作用、可单测，路由/worker 只做薄适配。
  - ``translate_outbound_text`` 是「译 + 记录 + 降级回落」的可复用闭包体，依赖通过参数注入
    （translation_service / store），单测可塞 fake。
  - **绝不阻塞投递**：任何异常 / 不可译 / 译文与原文相同 → 回落发原文，保证全自动链路不断。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SOURCE = "zh"
# 不可作为翻译目标的「空/未知」语言标记（与 translation_service.normalize_lang 对齐）
_SKIP_TARGETS = {"", "unknown", "und", "auto"}

# 会话语言多数决：取最近 N 条**入站**消息按新近度加权投票的窗口大小与最小样本长度。
# 单条孤立外语消息（如中文客户偶尔蹦一句英文）不足以翻转整窗多数 → 目标语言稳定，
# 修「一条英文消息把 conversations.language 标成 en → 中文回复被误翻成英文 garble
# 且语音因译文超长静默回落」的根因。
_LANG_VOTE_WINDOW = 12
_LANG_VOTE_MIN_CHARS = 2


def parse_outbound_translate_cfg(config: Any) -> Dict[str, Any]:
    """读 config.inbox.l2_autosend.translate → {enabled, source_lang, style}。缺省全关。"""
    tr = (((config or {}).get("inbox", {}) or {}).get("l2_autosend", {}) or {}
          ).get("translate", {}) or {}
    return {
        "enabled": bool(tr.get("enabled", False)),
        "source_lang": str(tr.get("source_lang") or _DEFAULT_SOURCE).strip().lower(),
        "style": str(tr.get("style") or "chat"),
    }


def normalize_target(lang: str) -> str:
    """归一化语言码：zh-CN → zh；空/未知/auto → ""（表示「不可作为目标」）。"""
    low = str(lang or "").strip().lower()
    if low in _SKIP_TARGETS:
        return ""
    return low.split("-")[0]


def should_translate(text: str, target_lang: str, source_lang: str) -> bool:
    """是否需要翻译：有正文 + 目标语言有效 + 目标 != 源。否则跳过（发原文）。"""
    if not str(text or "").strip():
        return False
    tgt = normalize_target(target_lang)
    if not tgt:
        return False
    return tgt != normalize_target(source_lang)


def vote_language(
    messages: List[Dict[str, Any]],
    *,
    detect: Any,
    window: int = _LANG_VOTE_WINDOW,
    min_chars: int = _LANG_VOTE_MIN_CHARS,
) -> str:
    """从最近若干条**入站**消息按新近度加权投票，得出会话主语言（纯函数）。

    - 只看入站（``direction != 'out'``）消息——出站是我们自己发的，不能拿来判客户语言。
    - 语音/图片等媒体消息若已被转写/识别补全（``text`` 非占位）同样计入，让「客户一直发
      中文语音」被正确判为 zh，而非被一条外插文字带偏。
    - 越新的消息权重越高（线性递减），且按**文本长度**加权——孤立短外语词很难翻转整窗。
    - 每条对 ``detect`` 得到的语言归一化后累加权重，取最高者；``unknown`` 不计。
    - 无有效样本 → 返回 ""（调用方回落 store 持久 language）。
    """
    if not messages:
        return ""
    recent = [m for m in messages if isinstance(m, dict)][-max(1, int(window)):]
    scores: Dict[str, float] = {}
    n = len(recent)
    for idx, m in enumerate(recent):
        if str(m.get("direction") or "in") == "out":
            continue
        text = str(m.get("text") or "").strip()
        # 跳过纯媒体占位（未转写）：[语音] [图片] [媒体] 等
        if not text or (text.startswith("[") and text.endswith("]") and " " not in text):
            continue
        if len(text) < int(min_chars):
            continue
        try:
            lang = normalize_target(detect(text))
        except Exception:
            continue
        if not lang:
            continue
        # 新近度权重（越靠后越大）× 长度权重（越长越可信，封顶避免长文一票独大）
        recency = 1.0 + idx / max(1, n - 1)
        length_w = min(4.0, len(text) / 20.0 + 0.5)
        scores[lang] = scores.get(lang, 0.0) + recency * length_w
    if not scores:
        return ""
    return max(scores.items(), key=lambda kv: kv[1])[0]


def _conv_language(store: Any, conversation_id: str, *, detect: Any = None) -> str:
    """best-effort 取会话客户语言。

    优先用最近入站消息**加权多数决**（需 ``detect`` 且 store 能取近窗消息）——比
    ``conversations.language`` 单值更抗「偶发一条外语消息翻转会话语言」。多数决无样本
    /不可用时回落 ``conversations.language`` 持久值（旧行为）。失败 → ""。
    """
    if store is None or not conversation_id:
        return ""
    # 加权多数决（新增主路径）
    if detect is not None and hasattr(store, "list_recent_messages"):
        try:
            recent = store.list_recent_messages(
                conversation_id, limit=_LANG_VOTE_WINDOW) or []
            voted = vote_language(recent, detect=detect)
            if voted:
                return voted
        except Exception:
            logger.debug("[outbound_translate] 语言多数决失败 conv=%s",
                         conversation_id, exc_info=True)
    # 回落 store 持久 language
    try:
        conv = store.get_conversation(conversation_id)
    except Exception:
        logger.debug("[outbound_translate] 读会话语言失败 conv=%s", conversation_id, exc_info=True)
        return ""
    return str((conv or {}).get("language") or "")


def _detect_source(translation_service: Any, text: str) -> str:
    """检测文本真实源语言（归一化）；检测器缺失/异常 → ""（表示未知）。"""
    fn = getattr(translation_service, "detect_language", None)
    if fn is None:
        return ""
    try:
        return normalize_target(fn(text))
    except Exception:
        logger.debug("[outbound_translate] detect_language 异常", exc_info=True)
        return ""


async def translate_outbound_text(
    item: Dict[str, Any],
    *,
    translation_service: Any,
    store: Any = None,
    source_lang: str = _DEFAULT_SOURCE,
    style: str = "chat",
) -> str:
    """把一条待投递文本译成会话客户语言；记录出向译文映射。**自带「已是客户语言则跳过」护栏**。

    item: ``{conversation_id, text, ...}``（AutosendWorker 的 to_deliver 载荷 / deferred 主动触达）。
    返回**应真正发出的文本**：成功译则返回译文，否则一律回落原文（绝不抛、绝不阻塞投递）。

    关键设计——**先检测真实源语言再决定是否翻译**：陪伴回复栈（skill_manager / reactivation）
    多按客户语言直接生成，盲目按 config 源语言（如 zh）翻译会把已是客户语言的文本 garble。
    故：检测文本实际语言，若已等于目标语言 → 跳过；否则用**检测到的源语言**翻译（比 config 假定更准）。
    """
    text = str(item.get("text") or "")
    cid = str(item.get("conversation_id") or "")
    if not text or translation_service is None:
        return text

    # 会话目标语言用「最近入站消息加权多数决」（detect 取自同一 translation_service，
    # 与入站落库检测同源），比 conversations.language 单值抗偶发外语翻转。
    _detect = getattr(translation_service, "detect_language", None)
    target = normalize_target(_conv_language(store, cid, detect=_detect))
    if not target:
        return text  # 目标语言未知 → 不翻译（发原文）

    # 检测真实源语言；命中目标语言即「文本已是客户语言」→ 跳过（防 garble，覆盖主动触达已 in-lang 的消息）
    detected = _detect_source(translation_service, text)
    eff_source = detected or normalize_target(source_lang) or source_lang
    if eff_source == target:
        return text

    try:
        res = await translation_service.translate(
            text, target_lang=target, source_lang=eff_source, style=style,
        )
    except Exception:
        logger.warning("[outbound_translate] 翻译调用失败，发原文 conv=%s", cid, exc_info=True)
        return text

    translated = str(getattr(res, "translated_text", "") or "")
    provider = str(getattr(res, "provider", "") or "")
    err = str(getattr(res, "error", "") or "")
    ok = bool(getattr(res, "ok", False))

    # 失败 / 空 / 与原文相同（provider=identity/none 或未真译）→ 回落原文，不记录无意义副行
    if not ok or not translated or translated == text:
        if err:
            logger.debug("[outbound_translate] 译文降级 conv=%s provider=%s err=%s",
                         cid, provider, err)
        return text

    if store is not None and cid:
        try:
            store.record_outbound_translation(
                cid, translated, text,
                source_lang=eff_source, target_lang=target,
                provider=provider, error=err,
            )
        except Exception:
            logger.debug("[outbound_translate] 记录出向译文失败 conv=%s", cid, exc_info=True)
    return translated


__all__ = [
    "parse_outbound_translate_cfg",
    "normalize_target",
    "should_translate",
    "translate_outbound_text",
    "vote_language",
]
