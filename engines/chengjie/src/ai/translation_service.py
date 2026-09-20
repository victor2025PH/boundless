"""Lightweight translation service for the unified inbox.

The first implementation is intentionally provider-optional:
- language detection is deterministic and cheap;
- translations are cached;
- if an AI client is supplied, it can translate;
- without a provider, the service returns the original text with a clear
  ``provider_unavailable`` status so UI/API flows remain usable in local tests.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 翻译/语种显示名（ISO 639-1 为主）。仅作「显示名 + 统计回退白名单」用——
# 确定性检测（脚本范围 + 拉丁关键词）只产出它已知的码，扩这张表不影响既有检测行为，
# 纯增量：① AI 翻译 prompt 的 source/target 名 ② 统计回退 `guess in LANG_NAMES` 放行面。
LANG_NAMES: Dict[str, str] = {
    # ── 原有 20 语种（保持在前，行为不变）──
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "ru": "Russian",
    "hi": "Hindi",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "th": "Thai",
    "ms": "Malay",
    "tl": "Filipino",
    "km": "Khmer",
    "he": "Hebrew",
    "el": "Greek",
    # ── 欧洲 ──
    "nl": "Dutch",
    "pl": "Polish",
    "uk": "Ukrainian",
    "ro": "Romanian",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "hu": "Hungarian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sr": "Serbian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "be": "Belarusian",
    "mk": "Macedonian",
    "sq": "Albanian",
    "is": "Icelandic",
    "ga": "Irish",
    "cy": "Welsh",
    "eu": "Basque",
    "ca": "Catalan",
    "gl": "Galician",
    # ── 中东 / 中亚 ──
    "fa": "Persian",
    "ur": "Urdu",
    "ps": "Pashto",
    "ku": "Kurdish",
    "az": "Azerbaijani",
    "kk": "Kazakh",
    "uz": "Uzbek",
    "ky": "Kyrgyz",
    "tg": "Tajik",
    "tk": "Turkmen",
    "hy": "Armenian",
    "ka": "Georgian",
    "mn": "Mongolian",
    # ── 南亚 ──
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "kn": "Kannada",
    "mr": "Marathi",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "si": "Sinhala",
    "ne": "Nepali",
    # ── 东南亚 ──
    "my": "Burmese",
    "lo": "Lao",
    "jv": "Javanese",
    # ── 非洲 ──
    "sw": "Swahili",
    "am": "Amharic",
    "zu": "Zulu",
    "af": "Afrikaans",
    "ha": "Hausa",
    "yo": "Yoruba",
    "ig": "Igbo",
    "so": "Somali",
    # ── 中文变体（2026-08-29 坐席出向翻译目标；detect 不产出这些码，纯目标语）──
    "zh-tw": "Traditional Chinese",
    "yue": "Cantonese",
    "unknown": "Unknown",
}


@dataclass
class TranslationResult:
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    ok: bool
    provider: str = "none"
    cached: bool = False
    error: str = ""
    # P0-2：确定性译文置信度 [0,1]（translation_confidence 评分）。-1 = 未评分
    # （identity/失败/空文本等无意义评分的路径），前端据此跳过低置信提示。
    confidence: float = -1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "ok": self.ok,
            "provider": self.provider,
            "cached": self.cached,
            "error": self.error,
            "confidence": self.confidence,
        }


class TranslationService:
    """Detect and translate chat text with a small in-memory TTL cache."""

    def __init__(
        self,
        *,
        ai_client: Optional[Any] = None,
        default_target_lang: str = "zh",
        cache_ttl_sec: int = 86400,
        neg_cache_ttl_sec: int = 60,
        max_cache_items: int = 1000,
        memory_store: Optional[Any] = None,
        glossary_terms: Optional[Dict[str, str]] = None,
        glossary_version: str = "",
        glossary_protect: Optional[list] = None,
        cost_tracking: bool = False,
        engines: Optional[list] = None,
        engine_router: Optional[Any] = None,
        min_confidence: float = 0.0,
        per_lang_order: Optional[Dict[str, Any]] = None,
        semantic_embed_fn: Optional[Any] = None,
        semantic_min_similarity: float = 0.65,
    ) -> None:
        self.ai_client = ai_client
        self.default_target_lang = normalize_lang(default_target_lang) or "zh"
        self.cache_ttl_sec = max(60, int(cache_ttl_sec or 86400))
        # 失败态（provider_unavailable / translate_failed）只做短 TTL 负缓存，
        # 避免配好 key / 引擎恢复后仍被旧失败结果毒化一整天。
        self.neg_cache_ttl_sec = max(1, int(neg_cache_ttl_sec or 60))
        self.max_cache_items = max(10, int(max_cache_items or 1000))
        self._cache: Dict[str, Tuple[float, TranslationResult]] = {}
        # Phase C2：持久翻译记忆（L2）+ 术语库 + 成本统计（全部可选，默认行为不变）
        self._memory_store = memory_store
        self._glossary_terms = {str(k): str(v) for k, v in (glossary_terms or {}).items()}
        self._glossary_version = str(glossary_version or "")
        self._glossary_protect = [str(t) for t in (glossary_protect or []) if t]
        self._cost_tracking = bool(cost_tracking)
        # P56：多引擎路由（默认 [AIEngine(ai_client)]，行为与改造前一致）
        from src.ai.translation_engines import AIEngine, EngineRouter

        if engine_router is not None:
            self._router = engine_router
        elif engines:
            self._router = EngineRouter(
                engines, min_confidence=min_confidence,
                per_lang_order=per_lang_order,
                semantic_embed_fn=semantic_embed_fn,
                semantic_min_similarity=semantic_min_similarity)
        else:
            self._router = EngineRouter(
                [AIEngine(ai_client)], min_confidence=min_confidence,
                per_lang_order=per_lang_order,
                semantic_embed_fn=semantic_embed_fn,
                semantic_min_similarity=semantic_min_similarity)

    def rebind_ai_client(self, ai_client: Any) -> None:
        """P0-1 首启向导：AI 凭证保存后热替换底层 client（免重启生效）。

        同步换掉路由内 ``AIEngine`` 持有的旧 client（确定性引擎 DeepL/Google 与
        client 无关，不动）。失败态结果只有短负缓存 TTL，无需清缓存。
        """
        self.ai_client = ai_client
        try:
            for eng in getattr(self._router, "_engines", []) or []:
                if getattr(eng, "name", "") == "ai" and hasattr(eng, "_ai"):
                    eng._ai = ai_client
        except Exception:
            pass

    def detect_language(self, text: str) -> str:
        return detect_language(text)

    def engine_matrix(self, target_lang: str = "") -> Dict[str, Any]:
        """指定目标语的引擎能力矩阵（供前端提前提示主引擎是否兜底）。

        P1-XM（2026-08-16）：行内附带进程期实测 ``avg_ms``/``ok_rate``
        （translation_engine_stats 同源，引擎 chips 的健康 tooltip 消费）；
        无流量的引擎不带这两键（前端不渲染），统计层异常绝不影响矩阵本身。
        """
        target = normalize_lang(target_lang) or self.default_target_lang
        try:
            matrix = self._router.describe(target)
        except Exception:
            return {"target_lang": target, "primary": "none",
                    "effective": "none", "engines": []}
        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            stats = {r["engine"]: r
                     for r in get_translation_engine_stats().dump().get("rows", [])}
            for row in matrix.get("engines", []):
                st = stats.get(row.get("engine"))
                if st and st.get("calls"):
                    row["avg_ms"] = st.get("avg_latency_ms")
                    row["ok_rate"] = st.get("success_rate")
        except Exception:
            pass
        return matrix

    async def compare_translations(
        self, text: str, *, target_lang: str = "", source_lang: str = "",
        style: str = "chat",
    ) -> Dict[str, Any]:
        """多线路对照选译：所有引擎各译一遍，返回候选列表供坐席择优。

        与 ``translate`` 同样应用术语强制 + 品牌词不译保护（mask→译→restore），
        故每条候选都已是「术语合规」的成品。不写缓存/记忆（对照是一次性比较，
        择优后由前端走正常 translate/send 落库，避免把非首选引擎结果污染记忆）。

        P0-2：每条成功候选附带确定性置信度（``confidence`` 分值 + ``confidence_tier``
        high/mid/low 分档 + ``confidence_signals`` 关键信号），供坐席对照择优时
        一眼识别「空译/未翻译/错语种/长度异常」的坏候选。失败候选不评分。
        """
        from src.ai.translation_confidence import (
            confidence_signals,
            confidence_tier,
            translation_confidence,
        )
        from src.ai.translation_engines import apply_glossary_mask, restore_protected

        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        out: Dict[str, Any] = {
            "source_lang": source, "target_lang": target,
            "original_text": src_text, "candidates": [],
        }
        if not src_text.strip() or not target:
            return out

        glossary_hint = self._glossary_hint(src_text)
        hit_terms = {k: v for k, v in self._glossary_terms.items() if k and k in src_text}
        masked, mapping = apply_glossary_mask(src_text, hit_terms, self._glossary_protect)
        try:
            results = await self._router.compare(
                masked, source_lang=source, target_lang=target,
                style=style, glossary_hint=glossary_hint,
            )
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out

        for r in results:
            text_out = restore_protected(r.text, mapping) if (r.ok and r.text) else ""
            cand: Dict[str, Any] = {
                "engine": r.engine,
                "ok": bool(r.ok and text_out),
                "translated_text": text_out,
                "error": r.error,
            }
            if cand["ok"]:
                # 评「原文 vs 还原后成品」（坐席实际看到的候选对），非引擎内部 masked 文本
                score = translation_confidence(src_text, text_out, target)
                cand["confidence"] = score
                cand["confidence_tier"] = confidence_tier(score)
                cand["confidence_signals"] = confidence_signals(src_text, text_out, target)
            out["candidates"].append(cand)
        return out

    def update_glossary(
        self,
        terms: Optional[Dict[str, str]] = None,
        protect: Optional[list] = None,
        version: str = "",
    ) -> str:
        """P59：运行时热替换术语库。version 变化 → cache_key 变 → 旧译自动失效。

        不传 version 时按内容重算 hash。返回生效后的 version。
        """
        self._glossary_terms = {str(k): str(v) for k, v in (terms or {}).items()}
        self._glossary_protect = [str(t) for t in (protect or []) if t]
        if version:
            self._glossary_version = str(version)
        else:
            from src.ai.translation_glossary import _hash
            self._glossary_version = _hash(self._glossary_terms, self._glossary_protect)
        self._cache.clear()  # 主动清 L1，避免极短窗口内读到旧译
        return self._glossary_version

    async def translate(
        self,
        text: str,
        *,
        target_lang: str = "",
        source_lang: str = "",
        style: str = "chat",
        engine: str = "",
        tier: str = "",
    ) -> TranslationResult:
        """``engine``（F+）：会话首选引擎名（如 ``deepl``）。指定且可用 → 强制走该引擎，
        失败再回落现有 failover 路由；空 / 不可用 → 维持原 failover 行为（零回归）。

        ``tier``（2026-08-19 Token 定价 P5b）：翻译服务层级——计费跟**显式请求的层级**
        走，绝不跟引擎回落走（本地引擎宕机静默回落云端时，标准请求不能被按专业价扣）：
        - ``""``/``"std"``：标准翻译＝免费（只记公平使用水表，warn-only）；
        - ``"pro"``：专业翻译（术语锁定/翻译记忆语义）＝10 Token/千字符；
        - ``"certified"``：认证翻译＝优先 DeepL 引擎（独立缓存桶），真由 DeepL 交付
          才计 40 Token/千字符；回落其它引擎按 pro 价 10 计——绝不按未交付的价值收费。
        计费只在**新鲜引擎成功**时发生（缓存/翻译记忆命中不计，与字符额度同口径）；
        总闸 licensing.token_ledger.enabled 关（默认）＝全零行为。
        """
        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        pref_engine = str(engine or "").strip().lower()
        tier = str(tier or "").strip().lower()
        # Token enforce（P6）：钱包耗尽 + enforce 开 → 专业/认证层级降级为标准档
        # （翻译照做，免费引擎链 + 公平使用水表；「永不断线」——降的是层级不是服务）。
        if tier in ("pro", "certified"):
            try:
                from src.licensing.token_ledger import should_degrade_action

                if should_degrade_action("pro_translate"):
                    logger.info("[translate] Token 钱包耗尽（enforce）→ %s 降级 std", tier)
                    tier = "std"
            except Exception:
                pass
        # certified 未显式指定引擎 → 首选 DeepL；在缓存键计算**之前**生效，
        # 认证请求走 engine=deepl 独立缓存桶，绝不命中标准桶旧译文（质量承诺）。
        if tier == "certified" and not pref_engine:
            pref_engine = "deepl"
        if not src_text.strip():
            return TranslationResult(src_text, "", source, target, True, provider="none")
        # #115（0831 钧原图 906）：纯媒体占位（「[图片]」「[语音消息]」这类整串
        # 只有一对方括号的系统占位）没有可译正文，送 LLM 只会换来「您没有提供
        # 需要翻译的消息内容」式 meta 错误响应（它此前被当译文进了会话流）。
        # identity 早退＝原样返回：入站译文行不渲染、出站发原文，所有调用链同享。
        # 带配文的「[图片] 今天拍的」不命中（占位后还有正文），照常翻译。
        if re.fullmatch(r"\[[^\[\]]{1,24}\]", src_text.strip()):
            return TranslationResult(src_text, src_text, source, target, True, provider="identity")
        # M-1 B #234（D-M3）：表情 / 单字符 / 纯标点数字**不进翻译器**。这类输入没有可译
        # 正文，LLM 引擎只能原样回显或客套一句 → 被 _output_lang_sane 判成
        # target_lang_mismatch（15:37–15:53 实录 30 余条 src_len=1 mismatch 刷屏），出站链
        # 再把它当「翻译失败」处理。identity 早退与占位同口径：入站不渲染译文行、出站放行。
        if _no_translatable_body(src_text):
            return TranslationResult(src_text, src_text, source, target, True, provider="identity")
        if source == target:
            # #139-3 粤语旁路（2026-09-02）：detect 对粤语恒回 zh（实施89 契约），
            # zh→zh 的 identity 短路曾把粤语消息原样吐回（996 图：『我今朝早起身
            # 見到個天靚到咁』无普通话译文）。目标是 zh 时按保守字表重判源语言，
            # yue≠zh → 继续走引擎真翻译；判不出仍走 identity（行为不变）。
            if target == "zh":
                source = detect_zh_variant(src_text) or source
            if source == target:
                return TranslationResult(src_text, src_text, source, target, True, provider="identity")

        key = self._cache_key(src_text, source, target, style, engine=pref_engine)
        # L1：进程内 TTL 缓存
        cached = self._cache_get(key)
        if cached is not None:
            cached.cached = True
            self._log_xlate("cache", cached)
            return cached
        # L2：持久翻译记忆（跨重启命中）
        mem = self._memory_get(key)
        if mem is not None:
            mem.cached = True
            self._cache_put(key, mem)  # 回填 L1
            self._log_xlate("memory", mem)
            return mem

        # P0-4 字符额度闸门：额度用尽且 licensing.enforce 开 → 阻断本次引擎翻译
        # （缓存/记忆命中不受影响；调用方按「翻译失败」回落原文，绝不阻断消息投递）。
        # 结果**不写缓存**——续费/换 key 后应立即恢复。闸门自身异常 → 放行。
        try:
            from src.licensing.quota_store import (
                QUOTA_EXCEEDED_ERROR,
                check_license_quota,
            )

            if not check_license_quota()["allowed"]:
                return TranslationResult(
                    src_text, src_text, source, target, False,
                    provider="license", error=QUOTA_EXCEEDED_ERROR,
                )
        except Exception:
            pass

        if not self._router.any_available():
            result = TranslationResult(
                src_text,
                src_text,
                source,
                target,
                False,
                provider="none",
                error="provider_unavailable",
            )
            self._cache_put(key, result)
            return result

        # P56/P57：术语强制（对所有引擎）+ 品牌词不译保护（mask→翻译→restore）
        # terms 还原为目标译法、protect 还原为原词；AI 引擎额外收到 glossary_hint 软提示。
        from src.ai.translation_engines import apply_glossary_mask, restore_protected

        glossary_hint = self._glossary_hint(src_text)
        hit_terms = {k: v for k, v in self._glossary_terms.items() if k and k in src_text}
        hit_protect = [w for w in self._glossary_protect if w and w in src_text]
        if hit_terms or hit_protect:
            try:
                from src.ai.glossary_hits import get_glossary_hits
                gh = get_glossary_hits()
                if hit_terms:
                    gh.record_terms(hit_terms.keys())
                if hit_protect:
                    gh.record_protect(hit_protect)
            except Exception:
                pass
        masked, mapping = apply_glossary_mask(src_text, hit_terms, self._glossary_protect)
        res = None
        # F+：会话首选引擎优先（强制单引擎，不故障转移）；失败再回落 failover
        if pref_engine:
            try:
                eng = self._router.engine_by_name(pref_engine)
                if eng is not None and getattr(eng, "available", False):
                    res = await self._router.translate_with(
                        pref_engine, masked, source_lang=source, target_lang=target,
                        style=style, glossary_hint=glossary_hint,
                    )
                    if not (res and res.ok and res.text):
                        res = None  # 首选引擎失败 → 下方 failover 兜底
            except Exception:
                res = None
        if res is None:
            res = await self._router.translate(
                masked, source_lang=source, target_lang=target,
                style=style, glossary_hint=glossary_hint,
            )
        if not res.ok or not res.text:
            result = TranslationResult(
                src_text, src_text, source, target, False,
                provider=res.engine or "none",
                error=res.error or "translate_failed",
            )
            self._cache_put(key, result)
            self._log_xlate("engine", result)
            return result

        out = restore_protected(res.text, mapping)
        # 引擎拒绝话术拦截（P1-198）：LLM 引擎对无意义短文本（"1"/表情）可能
        # 输出「没有可翻译的内容，请提供文本」类客套话——它曾被当译文直接发给
        # 客户（198 实锤）。按失败处理：出站链自动回落原文/HOLD、收件箱译文行
        # 不显示、预览如实报错。只进 L1 短缓存（防重复打引擎），绝不进翻译记忆。
        try:
            from src.ai.translation_confidence import looks_like_engine_refusal
            if looks_like_engine_refusal(src_text, out):
                result = TranslationResult(
                    src_text, src_text, source, target, False,
                    provider=res.engine or "ai", error="engine_refusal",
                )
                self._cache_put(key, result)
                self._log_xlate("engine", result)
                return result
        except Exception:
            pass
        result = TranslationResult(
            src_text, out or src_text, source, target,
            bool(out), provider=res.engine,
        )
        if result.ok:
            # P0-2：成功译文附确定性置信度（经 to_dict 透传到 /translate 响应与
            # 入站 enrich 的 message.translation，前端低置信给可见提示）。评分
            # 纯函数零网络；失败/identity 路径保持 -1（未评分）。
            try:
                from src.ai.translation_confidence import translation_confidence
                result.confidence = translation_confidence(src_text, out, target)
            except Exception:
                pass
        self._cache_put(key, result)
        if result.ok:
            self._memory_put(key, result, style, engine=res.engine)
            self._record_cost(src_text, out, source, target, engine=str(res.engine or ""))
            self._record_license_quota(src_text, tier=tier,
                                       provider=str(res.engine or ""))
            try:   # P4 埋点：翻译字符量按语向聚合计量（窗口 flush，绝不逐条发事件）
                from src.utils.telemetry import add_translated_chars
                add_translated_chars(len(src_text), source, target)
            except Exception:
                pass
        self._log_xlate("engine", result)
        return result

    # ── Q-39 B（#326 THBSHN，2026-09-15）：空串 / 超时可恢复重试 ─────────────────
    #: ``translate()`` 返回的 error 属这些族 → 值得原地重试（引擎抖动，不是语种 / 内容问题）
    RETRYABLE_ERRORS = ("empty", "timeout", "provider_unavailable", "translate_failed",
                        "unavailable", "TimeoutError", "ReadTimeout", "ConnectTimeout",
                        "ConnectionError", "ClientConnectorError", "ServerDisconnectedError")

    @classmethod
    def is_retryable_error(cls, err: str) -> bool:
        """``ai:empty`` / ``ai:TimeoutError: …`` / ``provider_unavailable`` 这类瓶颈抖动 → True；
        ``target_lang_mismatch`` / ``engine_refusal`` / ``unsupported_target`` / ``lang_unknown``
        这类**内容 / 语种**判定 → False（重打同一句只会得到同一个答案）。"""
        e = str(err or "").strip()
        if not e:
            return False
        tail = e.split(":", 1)[1].strip() if ":" in e else e
        for tok in cls.RETRYABLE_ERRORS:
            if tail == tok or tail.startswith(tok) or e == tok:
                return True
        return False

    def next_engine_after(self, failed_engine: str, target_lang: str) -> str:
        """路由表（含 per_lang_order）里 ``failed_engine`` **之后**第一个可用且支持目标语的引擎名；
        没有 → ""。``failed_engine`` 为空 / 不在表里 → 从头找第一个不同的可用引擎。"""
        target = normalize_lang(target_lang) or self.default_target_lang
        try:
            seq = list(self._router._engines_for(target))
        except Exception:
            seq = list(getattr(self._router, "_engines", []) or [])
        names = [str(getattr(e, "name", "") or "") for e in seq]
        fe = str(failed_engine or "").strip().lower()
        start = (names.index(fe) + 1) if fe in names else 0
        for eng in seq[start:]:
            name = str(getattr(eng, "name", "") or "")
            if not name or name == fe or not getattr(eng, "available", False):
                continue
            try:
                if hasattr(eng, "supports_target") and not eng.supports_target(target):
                    continue
            except Exception:
                pass
            return name
        return ""

    async def retry_once(
        self,
        text: str,
        *,
        target_lang: str = "",
        source_lang: str = "",
        style: str = "chat",
        engine: str = "",
    ) -> TranslationResult:
        """**绕过 L1 负缓存 / 翻译记忆**，直打一次指定引擎（空 → 路由 failover）。

        ``translate()`` 把失败态写进 60s 负缓存——2s 后原样重打只会命中那条缓存，「重试」
        等于没试。本口径专给出站链的可恢复重试（Q-39 B）：成功结果照常写 L1 + 翻译记忆 +
        成本 / 额度记账（与 translate 同口径），失败结果**不写缓存**（下一步换引擎不该被毒化）。
        """
        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        pref_engine = str(engine or "").strip().lower()
        if not src_text.strip() or source == target:
            return TranslationResult(src_text, src_text, source, target, True, provider="identity")
        if not self._router.any_available():
            return TranslationResult(src_text, src_text, source, target, False,
                                     provider="none", error="provider_unavailable")
        from src.ai.translation_engines import apply_glossary_mask, restore_protected

        glossary_hint = self._glossary_hint(src_text)
        hit_terms = {k: v for k, v in self._glossary_terms.items() if k and k in src_text}
        masked, mapping = apply_glossary_mask(src_text, hit_terms, self._glossary_protect)
        res = None
        if pref_engine:
            try:
                res = await self._router.translate_with(
                    pref_engine, masked, source_lang=source, target_lang=target,
                    style=style, glossary_hint=glossary_hint)
            except Exception as exc:  # noqa: BLE001
                from src.ai.translation_engines import EngineResult
                res = EngineResult("", pref_engine, False, f"{type(exc).__name__}: {exc}")
        else:
            res = await self._router.translate(
                masked, source_lang=source, target_lang=target,
                style=style, glossary_hint=glossary_hint)
        eng_name = str(getattr(res, "engine", "") or pref_engine or "none")
        if not res or not res.ok or not res.text:
            err = str(getattr(res, "error", "") or "translate_failed")
            if pref_engine and ":" not in err:
                err = f"{eng_name}:{err}"
            result = TranslationResult(src_text, src_text, source, target, False,
                                       provider=eng_name, error=err)
            self._log_xlate("retry", result)
            return result
        out = restore_protected(res.text, mapping)
        try:
            from src.ai.translation_confidence import looks_like_engine_refusal
            if looks_like_engine_refusal(src_text, out):
                result = TranslationResult(src_text, src_text, source, target, False,
                                           provider=eng_name, error="engine_refusal")
                self._log_xlate("retry", result)
                return result
        except Exception:
            pass
        result = TranslationResult(src_text, out or src_text, source, target, bool(out),
                                   provider=eng_name)
        try:
            from src.ai.translation_confidence import translation_confidence
            result.confidence = translation_confidence(src_text, out, target)
        except Exception:
            pass
        key = self._cache_key(src_text, source, target, style, engine="")
        self._cache_put(key, result)
        try:
            self._memory_put(key, result, style, engine=eng_name)
            self._record_cost(src_text, out, source, target, engine=eng_name)
            self._record_license_quota(src_text, tier="", provider=eng_name)
        except Exception:
            pass
        self._log_xlate("retry", result)
        return result

    # #176（2026-09-05 skuio 实录）：「Exactly」→「好的，请发送需要翻译的内容。」——
    # 0831 之前被 LLM 引擎写进翻译记忆的 meta 拒绝话术，每次命中记忆都原样吐出、
    # 永不重验（守卫只挂在引擎新译分支）。命中路径现在先过同一判据：坏条目当场
    # 作废（记忆删行 / L1 弹出）按 miss 处理走引擎重译。计数供看板/清洗对账。
    _refusal_purged: Dict[str, int] = {"memory": 0, "cache": 0}

    @staticmethod
    def _is_stale_refusal(result: Optional["TranslationResult"]) -> bool:
        if result is None or not result.ok:
            return False
        try:
            from src.ai.translation_confidence import looks_like_engine_refusal
            return looks_like_engine_refusal(
                result.source_text, result.translated_text)
        except Exception:
            return False

    @staticmethod
    def _log_xlate(hit: str, result: "TranslationResult") -> None:
        """出口一行 INFO（#176 顺手补：入站翻译此前不记 INFO，事故窗口 01:13–01:17
        零翻译日志）。**不记原文/译文**，只记命中层/引擎/长度/成败/错误码。"""
        try:
            logger.info(
                "[xlate] hit=%s provider=%s tgt=%s src_len=%d ok=%s%s%s",
                hit, result.provider or "-", getattr(result, "target_lang", "") or "-",
                len(result.source_text or ""),
                "1" if result.ok else "0",
                (" err=" + str(result.error)) if result.error else "",
                (" conf=%.2f" % result.confidence)
                if result.ok and result.confidence >= 0 else "",
            )
        except Exception:
            pass

    def _memory_get(self, key: str) -> Optional[TranslationResult]:
        if self._memory_store is None:
            return None
        try:
            row = self._memory_store.get(key)
        except Exception:
            return None
        if not row:
            return None
        result = TranslationResult(
            source_text=str(row.get("source_text") or ""),
            translated_text=str(row.get("translated_text") or ""),
            source_lang=str(row.get("source_lang") or ""),
            target_lang=str(row.get("target_lang") or ""),
            ok=True,
            provider=str(row.get("engine") or "ai"),
        )
        if self._is_stale_refusal(result):
            # 坏译文：删记忆行 → 本次按 miss 走引擎重译（新译成功才会重新入库；
            # 新译仍是拒绝话术则只进短负缓存，绝不再进记忆）
            try:
                self._memory_store.delete(key)
            except Exception:
                pass
            self._refusal_purged["memory"] = int(
                self._refusal_purged.get("memory", 0)) + 1
            logger.info(
                "[xlate] memory hit rejected as engine_refusal → purged & retranslate "
                "provider=%s src_len=%d", result.provider,
                len(result.source_text or ""))
            return None
        # P0-2：L2 记忆行不持久置信度（确定性评分随取随算，零成本零漂移）
        try:
            from src.ai.translation_confidence import translation_confidence
            result.confidence = translation_confidence(
                result.source_text, result.translated_text, result.target_lang)
        except Exception:
            pass
        return result

    def _memory_put(self, key: str, result: "TranslationResult", style: str,
                    engine: str = "ai") -> None:
        if self._memory_store is None:
            return
        try:
            self._memory_store.put(
                key,
                source_text=result.source_text,
                translated_text=result.translated_text,
                source_lang=result.source_lang,
                target_lang=result.target_lang,
                style=style,
                engine=engine or "ai",
                glossary_ver=self._glossary_version,
            )
        except Exception:
            pass

    def _record_license_quota(self, src: str, *, tier: str = "",
                              provider: str = "") -> None:
        """P0-4：成功引擎翻译后按源文字符记账（无额度授权零开销；绝不抛）。

        2026-08-19 Token 计量（P5b）叠加：按**请求层级**计费（见 translate docstring）
        ——std/缺省＝免费只记公平使用水表；pro＝10/千字符；certified 且真由 DeepL
        交付＝40/千字符（回落其它引擎按 pro 价）。总闸关＝零行为。
        """
        try:
            from src.licensing.quota_store import record_license_chars

            record_license_chars("translation", len(src))
        except Exception:
            pass
        try:
            t = str(tier or "").strip().lower()
            if t in ("pro", "certified"):
                from src.licensing.token_ledger import record_action_for_status

                action = ("deepl_translate"
                          if t == "certified" and str(provider).lower() == "deepl"
                          else "pro_translate")
                record_action_for_status(action, len(src))
            else:
                from src.licensing.token_ledger import note_translate_fair_use

                note_translate_fair_use(len(src))
        except Exception:
            pass

    def _record_cost(self, src: str, out: str, source: str, target: str,
                     engine: str = "") -> None:
        """翻译成本记账（tier=translation，purpose=translate）。

        2026-09-08：``ai`` 引擎走 ``generate_reply``，那里已按厂商返回的 usage 记过一笔
        真值（purpose=translate）——这里再按字数估一笔就是**重复计费**，会让对账天天
        对不上。所以真 AIClient 的 ai 引擎不再在此记账；只有老签名桩 / 非 AIClient
        引擎（LAN MT、DeepL 等不经 llm_cost 的）才落这条估算行，并标 ``suspected``。
        """
        if not self._cost_tracking:
            return
        try:
            from src.ai.llm_cost import get_llm_cost

            ai = self.ai_client
            real_ai = (ai is not None and hasattr(ai, "generate_reply")
                       and hasattr(ai, "_oa_client"))
            if real_ai and (not engine or engine == "ai"):
                return
            pt = max(1, len(src) // 4)
            ct = max(1, len(out) // 4)
            model = (str(engine) if engine and engine != "ai"
                     else (getattr(ai, "model", "") or "translate"))
            get_llm_cost().record(
                model=str(model), prompt_tokens=pt, completion_tokens=ct,
                tier="translation", purpose="translate",
                provider=("lan" if engine and engine != "ai" else ""),
                suspected=not bool(engine and engine != "ai"),
            )
        except Exception:
            pass

    def _glossary_hint(self, text: str) -> str:
        """命中术语注入提示（Phase C2）。仅注入文本中出现的术语，避免噪声。"""
        if not self._glossary_terms:
            return ""
        hits = [f"{k}->{v}" for k, v in self._glossary_terms.items() if k and k in text]
        if not hits:
            return ""
        return " Use these term translations: " + "; ".join(hits[:20]) + "."

    def _cache_key(self, text: str, source_lang: str, target_lang: str, style: str,
                   engine: str = "") -> str:
        # engine 参与 key：指定首选引擎的译文与 failover 译文分桶缓存，互不串味
        eng = str(engine or "").strip().lower()
        raw = f"{source_lang}|{target_lang}|{style}|{eng}|{self._glossary_version}|{text[:2000]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[TranslationResult]:
        row = self._cache.get(key)
        if not row:
            return None
        ts, result = row
        ttl = self.cache_ttl_sec if result.ok else self.neg_cache_ttl_sec
        if time.time() - ts > ttl:
            self._cache.pop(key, None)
            return None
        out = TranslationResult(**result.to_dict())
        if self._is_stale_refusal(out):
            # #176：L1 里也可能躺着记忆回填来的坏译文（进程重启前入的）→ 弹出按 miss
            self._cache.pop(key, None)
            self._refusal_purged["cache"] = int(
                self._refusal_purged.get("cache", 0)) + 1
            return None
        return out

    def _cache_put(self, key: str, result: TranslationResult) -> None:
        if len(self._cache) >= self.max_cache_items:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[: max(1, len(self._cache) // 5)]
            for k, _ in oldest:
                self._cache.pop(k, None)
        self._cache[key] = (time.time(), TranslationResult(**result.to_dict()))


def normalize_lang(lang: str) -> str:
    # zh-tw 是一等目标语（繁体中文，2026-08-29 起）：以前折叠成 zh 会让
    # 简体→繁体在 translate() 的 source==target 短路里恒 identity（一个字不变）。
    # zh-hant / zh-hk 作为别名归入 zh-tw（书面繁体先合并，港台用词变体后续再分）。
    code = str(lang or "").strip().lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh",
        "zh-hant": "zh-tw",
        "zh-hk": "zh-tw",
        "cn": "zh",
        "jp": "ja",
        "kr": "ko",
        "ar-ur": "ar",
        "ur": "ar",
    }
    return aliases.get(code, code)


# 唯一脚本块 → 语种（确定性强、零歧义，覆盖跨境客服常见客户语种）。
# 顺序重要：先查假名再查 CJK，否则日文汉字会被误判为中文。
_SCRIPT_RE: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("ja", re.compile(r"[\u3040-\u30ff]")),                       # 平假名/片假名
    ("ko", re.compile(r"[\uac00-\ud7af]")),                       # 韩文
    ("th", re.compile(r"[\u0e01-\u0e3a\u0e40-\u0e4e]")),          # 泰文字母/元音（排除 ฿ 泰铢符与泰数字，避免英文报价误判）
    ("km", re.compile(r"[\u1780-\u17ff]")),                       # 高棉文
    ("ar", re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")),  # 阿拉伯文
    ("ru", re.compile(r"[\u0400-\u04ff]")),                       # 西里尔文
    ("hi", re.compile(r"[\u0900-\u097f]")),                       # 天城文（印地语）
    ("he", re.compile(r"[\u0590-\u05ff]")),                       # 希伯来文
    ("el", re.compile(r"[\u0370-\u03ff]")),                       # 希腊文
)

# 越南语：拉丁字母 + 独有变音符（ăđơư + 声调块），与葡/西的 ã/â/ê/ô 区分开。
_VI_RE = re.compile(r"[\u0103\u0102\u0111\u0110\u01a1\u01a0\u01b0\u01af\u1ea0-\u1ef9]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 「有可译正文」的文字系统字符（拉丁含扩展 / 希腊 / 西里尔 / 希伯来 / 阿拉伯 / 天城 /
# 泰 / 高棉 / 假名 / 汉字 / 谚文）。与 outbound_translate._TRANSLATABLE_RE 同口径。
_SCRIPT_BODY_RE = re.compile(
    r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u0590-\u05FF"
    r"\u0600-\u06FF\u0900-\u097F\u0E00-\u0E7F\u1780-\u17FF\u3040-\u30FF"
    r"\u4E00-\u9FFF\uF900-\uFAFF\uAC00-\uD7AF]")


def _no_translatable_body(text: str) -> bool:
    """表情 / 纯标点数字 / 单字符 → True（不该进翻译器）。M-1 B #234。

    单字符含单个汉字（「好」「嗯」）：孤字无上下文，引擎回显或客套的概率远高于给出
    有用译文，且 15:37–15:53 实录 30 余条 ``src_len=1 … target_lang_mismatch`` 全是它。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if not _SCRIPT_BODY_RE.search(t):
        return True
    return len(t) <= 1

# 拉丁语种关键词（小写子串匹配）。es 置于首位以保持既有行为；
# 仅收录足够独特、不会成为英文常用词子串的词，避免误判。
_LATIN_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # ¿ / ¡ 是西语独有标点（葡/法/意都不用）：短句「Perfecto. ¿Cuánto tarda el envío?」
    # 无关键词命中 → 判 en → 会话语言翻成 en → 中文回复被译成英文发给西语客户
    # （2026-09-18 P1 重录实锤）。标点放关键词表同一层，仍是纯确定性。
    ("es", ("hola", "gracias", "estoy", "quiero", "buenos", "adios", "señor", "español", "¿", "¡")),
    ("pt", ("olá", "obrigado", "obrigada", "quero", "você", "também", "português")),
    ("fr", ("bonjour", "merci", "veux", "avec", "pourquoi", "salut", "français")),
    ("de", ("hallo", "danke", "nicht", "bitte", "warum", "guten", "deutsch")),
    ("it", ("ciao", "grazie", "voglio", "perché", "buongiorno", "italiano")),
    ("tr", ("merhaba", "teşekkür", "nasılsın", "istiyorum", "türkçe")),
    ("vi", ("xin chào", "cảm ơn", "không", "muốn")),
    ("id", ("halo", "terima kasih", "saya", "tidak", "bagaimana", "selamat")),
    ("tl", ("salamat", "kumusta", "magkano", "paano", "mahal kita")),
)


# Phase B：可选统计检测钩子（默认 None=纯确定性，行为不变）。
# 仅当确定性核心落到弱结果（en/unknown）且文本够长时才咨询，用于精修含糊拉丁。
_STATISTICAL_HOOK: Optional[Any] = None
_STATISTICAL_MIN_CHARS: int = 12


def set_statistical_detector(fn: Optional[Any], *, min_chars: int = 12) -> None:
    """注入/清除可选统计语种检测器（进程级，启动时按配置设置一次）。

    fn 形如 ``detect(text) -> Optional[str]``（ISO 639-1）；传 None 清除（回到纯确定性）。
    min_chars：低于此长度不咨询统计层（短文本统计检测不可靠）。
    """
    global _STATISTICAL_HOOK, _STATISTICAL_MIN_CHARS
    _STATISTICAL_HOOK = fn
    _STATISTICAL_MIN_CHARS = max(1, int(min_chars or 12))


def _maybe_statistical(text: str, weak_result: str) -> str:
    """弱结果（en/unknown）时尝试统计回退；任何异常/无后端都保持原结果。"""
    fn = _STATISTICAL_HOOK
    if fn is None or len(text) < _STATISTICAL_MIN_CHARS:
        return weak_result
    try:
        guess = normalize_lang(str(fn(text) or ""))
    except Exception:
        return weak_result
    if not guess or guess == "unknown":
        return weak_result
    # 仅采信库内已知语种；越南语等已被确定性核心捕获，这里主要救拉丁小语种。
    return guess if guess in LANG_NAMES else weak_result


def detect_language(text: str) -> str:
    """确定性语种检测（用于翻译路由与会话语言标注）。

    分层：唯一脚本块 → 越南语变音符 → CJK 计数 → 拉丁关键词 → 拉丁变音回退。
    弱结果（en/unknown）时若注入了统计检测器且文本够长，再做一次统计精修。
    默认零外部依赖、可复现。
    """
    t = str(text or "").strip()
    if not t:
        return "unknown"
    for lang, pat in _SCRIPT_RE:
        if pat.search(t):
            return lang
    # 越南语用拉丁字母但有独有变音符，须在通用拉丁处理前判定。
    if _VI_RE.search(t):
        return "vi"
    cjk = len(_CJK_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    if cjk and cjk >= latin:
        return "zh"
    lower = t.lower()
    for lang, hints in _LATIN_HINTS:
        if any(h in lower for h in hints):
            return lang
    # 拉丁变音快速信号（关键词未命中时的兜底）：ñ→西，ã/õ→葡。
    if "ñ" in lower:
        return "es"
    if "ã" in lower or "õ" in lower:
        return "pt"
    return _maybe_statistical(t, "en" if latin else "unknown")


# ── 粤语变体旁路判定（#139-3，2026-09-02：实施89 批次4 字表下沉服务端）──────
# 主检测契约不变：detect_language 对中文家族恒回 zh（实施89 §三-2，语言投票/
# conversations.language/lang_policy 都不吃变体码）。本表只服务「zh 源要不要
# 译到 zh 目标」这类变体敏感消费方——入站翻译链（粤语→普通话）与前端变体
# 发现提示。0830 断电 NUL 事故后 unified_inbox.html 的 _detectZhVariant 批次
# 未被重建（重建只覆盖 34 语目录/选语页），粤语入站不出中文译文即此回归缺口。
# 字表保守（宁漏勿错）：
#   - 专用语法字（基本只出现在粤文书面，普通话不会自然出现）：命中任一即判；
#   - 强特征字/词（普通话偶见或跨方言梗用法）：需 ≥2 个**不同**特征共同命中
#     （防「唔…让我想想」拟声、「冇问题」梗、「好累咩」语气词误伤）。
_YUE_EXCLUSIVE_CHARS = "嘅咗哋嗰喺嚟"
_YUE_HINT_CHARS = "唔冇乜咁噉啱嘥嗮曬囉啩畀嘢"
_YUE_HINT_WORDS = (
    "今朝", "琴日", "聽日", "听日", "而家", "宜家", "點解", "点解",
    "唔該", "唔该", "唔好", "唔係", "唔系", "掛住", "挂住", "靚到", "靓到",
    "好靚", "好靓", "邊個", "边个", "乜嘢", "咩嘢", "睇下", "睇吓",
    "得閒", "得闲", "傾偈", "倾偈", "俾我", "畀我", "食咗", "飲茶", "饮茶",
)


def detect_zh_variant(text: str) -> str:
    """中文变体旁路判定：粤语书面 → ``"yue"``；其余/判不出 → ``""``。

    与 :func:`detect_language` 的关系是旁路不是覆写——主检测对粤语仍回 zh。
    入站翻译链靠它把「detect=zh × target=zh → identity 跳过」的粤语消息重新
    捞回翻译候选（996 图实锤：『我今朝早起身見到個天靚到咁』无中文译文）。
    特征词命中后，包含在词内的单字不再重复计数（防单个语素凑双证据）。
    纯函数、绝不抛。
    """
    try:
        t = str(text or "")
        if not t or not _CJK_RE.search(t):
            return ""
        if any(ch in t for ch in _YUE_EXCLUSIVE_CHARS):
            return "yue"
        word_hits = [w for w in _YUE_HINT_WORDS if w in t]
        char_hits = [
            ch for ch in _YUE_HINT_CHARS
            if ch in t and not any(ch in w for w in word_hits)
        ]
        return "yue" if (len(word_hits) + len(char_hits)) >= 2 else ""
    except Exception:
        return ""


def _clean_translation(text: str) -> str:
    out = text.strip()
    out = re.sub(r"^```(?:\w+)?", "", out).strip()
    out = re.sub(r"```$", "", out).strip()
    for prefix in ("Translation:", "Translated:", "译文：", "翻译："):
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix):].strip()
    return out
