"""
语音转录服务 - Telegram MTProto AI语音识别扩展
"""

import os
import re
import asyncio
import hashlib
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable, Awaitable
import logging

logger = logging.getLogger(__name__)

# Whisper 类 ASR 在**静音/噪声/极短**片段上会「幻觉」出训练语料里的高频尾字幕短语
# （视频结尾套话），与客户实际所说毫无关系；若当真回复必然驴唇不对马嘴。这里只收录
# **绝无歧义**的幻觉套话（保守：绝不误伤正常闲聊，如单字"林"/"好"/笑声"哈哈哈"照常放行）。
_ASR_HALLUCINATION_PHRASES = (
    "请不吝点赞", "点赞订阅", "订阅转发", "打赏支持", "明镜与点点",
    "点赞、订阅", "订阅、转发", "字幕志愿者", "字幕组", "中文字幕",
    "谢谢观看", "谢谢大家观看", "感谢观看", "谢谢收看", "谢谢大家收看",
    "关注我的频道", "关注我们的频道", "下期视频再见", "我们下期再见", "下集再见",
    "本视频", "本期视频",
    "thanks for watching", "please subscribe", "subtitles by",
    "amara.org", "www.", "http",
)


def looks_like_asr_hallucination(text: str) -> bool:
    """判断转录文本是否为 ASR 幻觉/退化输出（应丢弃而非当真回复）。保守：宁漏勿误伤。

    命中条件（任一）：
      - 含**无歧义**的尾字幕幻觉套话（"请点赞订阅转发"/"谢谢观看"/"字幕组"…）；
      - **单字符霸屏**的退化输出（≥12 字且某单一字符占比 ≥90%，如乱码"。。。。。。"）。
    纯函数、防御式（空/异常→False，即不拦，保持原行为）。
    """
    try:
        t = str(text or "").strip()
        if not t:
            return False
        low = t.lower()
        for p in _ASR_HALLUCINATION_PHRASES:
            if p in low:
                return True
        compact = re.sub(r"\s+", "", t)
        if len(compact) >= 12:
            top_char, cnt = Counter(compact).most_common(1)[0]
            # 单字符占比极高＝退化（非正常闲聊；"哈哈哈"这类笑声一般远达不到 12 字且 ≥90%）
            if cnt / len(compact) >= 0.9:
                return True
        return False
    except Exception:
        return False


# ── 2026-09-12 ASR P0：转写缓存 / 动态超时 / 热词 prompt / 语种先验复核 ──────────
# 日志实锤（zhiliao 09-12 20:34:57 与 20:35:09 同一 ``3A5A99EF…ogg`` 各转一次）：协议
# 入站落库前转一遍、AutoDraft 再转一遍，38 次主 ASR 调用只对应 ~19 条语音；失败件连
# 备路一起 4 次。服务端是单把推理锁的串行 GPU，重复请求直接放大排队 → 3s 超时 →
# 静默降档。下面这组纯函数 + 缓存是几个消费口共用的地基，均不触网、可单测。

_TC_MISS = object()


class TranscriptCache:
    """进程级转写结果缓存：键＝音频内容 sha1 + 语言参数。

    - 命中直接返回，零 GPU；成功结果 TTL 1h（同字节同转写）；
    - **负缓存**只收 ``no_speech`` / ``empty_result``（内容决定、重跑同样为空），TTL 10min
      ——超时/被拒/不可达这类环境性失败绝不缓存（下一次可能就好了）；
    - 有界 LRU；线程安全；``stats()`` 供 asr_stats 观测。
    """

    def __init__(self, max_entries: int = 512, ttl_ok_sec: float = 3600.0,
                 ttl_empty_sec: float = 600.0) -> None:
        self._lock = threading.RLock()
        self._d: "OrderedDict[str, Tuple[Optional[str], Dict[str, Any], str, float]]" = OrderedDict()
        self._max = max(8, int(max_entries))
        self.ttl_ok = float(ttl_ok_sec)
        self.ttl_empty = float(ttl_empty_sec)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any:
        """返回 ``(text, meta, last_error)`` 或哨兵 ``_TC_MISS``。"""
        if not key:
            return _TC_MISS
        with self._lock:
            item = self._d.get(key)
            if item is None:
                self.misses += 1
                return _TC_MISS
            text, meta, err, expires = item
            if time.time() >= expires:
                self._d.pop(key, None)
                self.misses += 1
                return _TC_MISS
            self._d.move_to_end(key)
            self.hits += 1
            return text, dict(meta or {}), err

    def put(self, key: str, text: Optional[str], meta: Optional[Dict[str, Any]] = None,
            last_error: str = "") -> bool:
        """成功→按 ttl_ok 存；空结果→仅 no_speech/empty_result 负缓存；其余不存。"""
        if not key:
            return False
        if text:
            ttl = self.ttl_ok
        elif str(last_error or "").startswith("no_speech") or last_error == "empty_result":
            ttl = self.ttl_empty
        else:
            return False
        with self._lock:
            self._d[key] = (text or None, dict(meta or {}), str(last_error or ""),
                            time.time() + ttl)
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)
        return True

    def reset(self) -> None:
        with self._lock:
            self._d.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {"hits": self.hits, "misses": self.misses, "size": len(self._d),
                    "max": self._max,
                    "hit_rate": round(self.hits / total, 4) if total else 0.0}


_TRANSCRIPT_CACHE: Optional[TranscriptCache] = None
_TRANSCRIPT_CACHE_LOCK = threading.Lock()


def get_transcript_cache() -> TranscriptCache:
    global _TRANSCRIPT_CACHE
    if _TRANSCRIPT_CACHE is None:
        with _TRANSCRIPT_CACHE_LOCK:
            if _TRANSCRIPT_CACHE is None:
                _TRANSCRIPT_CACHE = TranscriptCache()
    return _TRANSCRIPT_CACHE


def transcript_cache_key(path: str, language: str = "auto") -> str:
    """音频内容 sha1 + 语言 → 缓存键；文件读不到 → ""（调用方跳过缓存）。"""
    try:
        h = hashlib.sha1()
        with open(str(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return f"{h.hexdigest()}:{str(language or 'auto').strip().lower() or 'auto'}"
    except Exception:
        return ""


def estimate_audio_duration(path: str) -> Optional[float]:
    """读容器头估时长（WAV 头 / OGG 末页 granule，纯 Python，不起 ffprobe 子进程）。

    热路只需要一个「大概多长」来定超时；估不出 → None（调用方用基础超时，行为同旧）。
    """
    try:
        from src.ai.audio_pipeline_intake import probe_audio_file
        d = probe_audio_file(str(path), use_ffprobe=False).get("duration_sec")
        return float(d) if isinstance(d, (int, float)) and d > 0 else None
    except Exception:
        return None


def effective_timeout(base_sec: float, duration_sec: Optional[float], *,
                      per_audio_sec: float = 0.25, overhead_sec: float = 1.5,
                      cap_sec: float = 10.0) -> float:
    """按音频时长定单次转写超时：``clamp(base, dur×per_audio + overhead, cap)``。

    背景：``timeout: 3`` 是 08-28 按 176（5090，探针 0.4~0.5s）定的；08-29 主 ASR 迁到
    198（4070，与 Lite Hub 共卡）后同一夹具要 0.7~0.9s/4.25s 却没重校——长语音主路
    必超时切备路（贪心 beam），而服务端并不取消已在跑的请求。09-12 实测 198 热态
    11s 片段 0.65s（RTF≈0.06，固定开销为主）：缺省 per_audio 0.25 已是 ~4 倍余量，
    留给排队（入站闸 3 路 × 单把推理锁）；cap 10s 与入站 25s 墙钟 + 备路 18s 联动
    （极端同时超时会越过 25s → 占位符落库、AutoDraft 稍后重试，属既有退化路径）。
    时长未知 → 原样用 base（行为不变）。
    """
    try:
        base = float(base_sec or 0) or 30.0
    except (TypeError, ValueError):
        base = 30.0
    if duration_sec is None or not isinstance(duration_sec, (int, float)) or duration_sec <= 0:
        return base
    try:
        cap = max(float(cap_sec), base)
        want = float(duration_sec) * float(per_audio_sec) + float(overhead_sec)
    except (TypeError, ValueError):
        return base
    return round(min(cap, max(base, want)), 2)


def hotwords_prompt(config: Optional[Dict[str, Any]]) -> str:
    """``voice_recognition.hotwords``（或 whisper.hotwords）→ 一条 prompt 串（「、」连接）。

    此前只有进程内 FasterWhisper 消费它；生产主路 OpenAI 兼容端点从没收到过——
    一长串产品名一直是死配置。现在 OpenAI 兼容路走标准 ``prompt`` 字段送出。
    """
    cfg = config if isinstance(config, dict) else {}
    hw = cfg.get("hotwords") or (cfg.get("whisper", {}) or {}).get("hotwords")
    if isinstance(hw, (list, tuple, set)):
        hw = "、".join(str(w).strip() for w in hw if str(w or "").strip())
    s = str(hw or "").strip()
    return s[:400]


_ECHO_STRIP_RE = re.compile(r"[\s，,、。.!！?？~～:：;；\-—_]+")


def looks_like_prompt_echo(text: str, prompt: str,
                           no_speech_prob: Optional[float] = None, *,
                           duration: Optional[float] = None,
                           language_probability: Optional[float] = None) -> bool:
    """Whisper 在静音/噪声/极短音频上会把 initial_prompt 原样吐回来（「智聊ChatX…」）。

    判定：转写去标点后**只由热词拼成**——命中 ≥2 个热词即认；只命中 1 个热词时须有
    「不像真说了这个词」的旁证之一：no_speech_prob ≥ 0.5 / 音频 < 2s（09-12 实锤：
    1.47s 片段带 prompt 直吐「智聊ChatX」）/ 语种概率 < 0.6。客户真说一句「无界科技」
    （几秒、语种清楚）不被误杀。纯函数不抛。
    """
    try:
        compact = _ECHO_STRIP_RE.sub("", str(text or ""))
        words = [w for w in re.split(r"[、,，\s]+", str(prompt or "")) if w.strip()]
        if not compact or not words:
            return False
        rest, matched = compact, 0
        for w in sorted(set(words), key=len, reverse=True):
            wc = _ECHO_STRIP_RE.sub("", w)
            if wc and wc in rest:
                matched += rest.count(wc)
                rest = rest.replace(wc, "")
        if rest or matched == 0:
            return False
        if matched >= 2:
            return True
        if no_speech_prob is not None and float(no_speech_prob) >= 0.5:
            return True
        if isinstance(duration, (int, float)) and 0 < float(duration) < 2.0:
            return True
        if isinstance(language_probability, (int, float)) and float(language_probability) < 0.6:
            return True
        return False
    except Exception:
        return False


# OpenAI 官方 verbose_json 的 language 是英文全名（"english"）；自建服务回 ISO 码。
_LANG_NAME_TO_CODE = {
    "chinese": "zh", "mandarin": "zh", "cantonese": "yue", "english": "en",
    "japanese": "ja", "korean": "ko", "thai": "th", "vietnamese": "vi",
    "indonesian": "id", "malay": "ms", "tagalog": "tl", "filipino": "tl",
    "spanish": "es", "portuguese": "pt", "french": "fr", "german": "de",
    "russian": "ru", "arabic": "ar", "hindi": "hi", "italian": "it", "turkish": "tr",
}
_META_FLOAT_KEYS = ("duration", "language_probability", "avg_logprob",
                    "no_speech_prob", "compression_ratio")


def extract_transcription_meta(resp: Any) -> Dict[str, Any]:
    """OpenAI 兼容转写响应（SDK 对象 / dict / 纯文本）→ ``{text, language, …置信度}``。

    SDK 的 pydantic 模型 ``extra=allow``：自建服务多回的 avg_logprob 等在
    ``model_extra`` 里；官方 verbose_json 的 segments 里也有逐段置信度，这里取聚合
    （avg_logprob 均值 / no_speech_prob 最大 / compression_ratio 最大）。缺什么就不给
    什么，绝不猜。
    """
    out: Dict[str, Any] = {}
    if resp is None:
        return out
    if isinstance(resp, str):
        out["text"] = resp
        return out
    if isinstance(resp, dict):
        d: Dict[str, Any] = dict(resp)
    else:
        d = {}
        try:
            extra = getattr(resp, "model_extra", None)
            if isinstance(extra, dict):
                d.update(extra)
        except Exception:
            pass
        for k in ("text", "language", "segments") + _META_FLOAT_KEYS:
            try:
                v = getattr(resp, k, None)
            except Exception:
                v = None
            if v is not None and k not in d:
                d[k] = v
    out["text"] = str(d.get("text") or "")
    lang = str(d.get("language") or "").strip().lower()
    if lang:
        out["language"] = _LANG_NAME_TO_CODE.get(lang, lang.split("-")[0] if len(lang) <= 6 else lang)
    for k in _META_FLOAT_KEYS:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    # 顶层没给、但 segments 里有逐段置信度 → 聚合（官方 whisper-1 verbose_json 形态）
    segs = d.get("segments")
    if isinstance(segs, (list, tuple)) and segs:
        lp, nsp, cr = [], [], []
        for s in segs:
            g = (lambda k: (s.get(k) if isinstance(s, dict) else getattr(s, k, None)))
            for arr, key in ((lp, "avg_logprob"), (nsp, "no_speech_prob"), (cr, "compression_ratio")):
                v = g(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    arr.append(float(v))
        if lp and "avg_logprob" not in out:
            out["avg_logprob"] = sum(lp) / len(lp)
        if nsp and "no_speech_prob" not in out:
            out["no_speech_prob"] = max(nsp)
        if cr and "compression_ratio" not in out:
            out["compression_ratio"] = max(cr)
    return out


def _lang_family(code: str) -> str:
    """zh/zh-tw/zh-hk/yue → zh；其余取主码。只用于「先验 vs 检出」冲突判定。"""
    c = str(code or "").strip().lower().replace("_", "-")
    if not c or c in ("auto", "none", "null"):
        return ""
    try:
        from src.ai.lang_policy import normalize_lang_code
        c = normalize_lang_code(c)
    except Exception:
        pass
    return "zh" if (c.startswith("zh") or c == "yue") else c.split("-")[0]


def lang_family_of_text(text: str) -> str:
    """文本文字系统 → 语种家族（只认强证据；判不出 → ""）。"""
    try:
        from src.ai.lang_policy import EvidenceStrength, classify_evidence
        code, strength = classify_evidence(str(text or ""))
        if strength != EvidenceStrength.STRONG:
            return ""
        return _lang_family(code)
    except Exception:
        return ""


def should_retry_with_lang_hint(meta: Optional[Dict[str, Any]], text: str,
                                lang_hint: str, *, min_prob: float = 0.6) -> bool:
    """会话语种先验 vs 本条转写检出语种：值不值得按先验**重转一次**。

    原则（WA RPA 线 ``_asr_language_hint`` 同契约，0830「中文客户语音被转成韩语乱码」
    实锤）：先验不直接钉死 language（Whisper 对不匹配语言会翻译而非转写），而是
    先 auto 转、检出语种与先验冲突且置信不足时再按先验重转。判定：
      - 无先验 / 检不出语种 / 同家族 → 不重转；
      - 检出语种 ≠ 先验：服务端给了 language_probability → < min_prob 才重转；
        没给（老服务 / 只有文本）→ 按文字系统与先验冲突即重转。
    """
    hint_fam = _lang_family(lang_hint)
    if not hint_fam or not str(text or "").strip():
        return False
    m = meta if isinstance(meta, dict) else {}
    detected = str(m.get("language") or "").strip()
    det_fam = _lang_family(detected) if detected else lang_family_of_text(text)
    if not det_fam or det_fam == hint_fam:
        return False
    prob = m.get("language_probability")
    if isinstance(prob, (int, float)):
        return float(prob) < float(min_prob)
    return True


def _record_asr_event(kind: str) -> None:
    try:
        from src.ai.asr_stats import get_asr_stats
        get_asr_stats().record_event(kind)
    except Exception:
        pass


class VoiceTranscriber:
    """语音转录服务基类"""

    def __init__(self, config: Dict[str, Any]):
        """初始化语音转录服务"""
        self.config = config
        self.logger = logging.getLogger(__name__)

        # 临时目录配置
        temp_dir = config.get('temp_dir', './temp/voice')
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 最大文件大小（默认16MB）
        self.max_file_size = config.get('max_file_size', 16777216)

        # ASR 幻觉守卫：静音/噪声上转出的尾字幕套话（"请点赞订阅转发"/"谢谢观看"…）
        # 与客户所说无关，当真回复必错。默认开；置 false 退回旧行为（不拦）。
        self.hallucination_guard = bool(config.get('hallucination_guard', True))

        # 中文转写字形归一（2026-09-12 GWJ2RZ 钧机）：Whisper 系 ASR 对普通话音频
        # 吐简吐繁不受控（同一客户整晚全繁体：幾乎/煮飯/廚房），繁体转写进 LLM 会把
        # 回复往港台腔带（粤语误切诱因之一）、进 synth_verify 与简体送稿逐字对不上。
        # 默认开：zh 主体且非粤文的转写统一 t2s（粤文/日文汉字豁免同
        # ``to_simplified_for_tts``；opencc 缺失恒等）。置 false 退回原样透传。
        self.zh_simplified_output = bool(config.get('zh_simplified_output', True))

        # 观测：是否为级联链中的子转录器。子级不记「顶层成败」（由 FallbackTranscriber 统一
        # 记，避免重复计数）；独立使用时（无级联）由基类自记。幻觉丢弃则不论层级都记。
        self._asr_chained_child = False

        # M-5 D（#225）：最近一次失败的原因串（``TypeName: msg`` / ``file_not_found`` /
        # ``empty_result``…）。本类的公有口径是「失败 → None」，工具箱链要把「超时 /
        # 被拒 / 不可达 / 格式」说给用户听，只有 None 不够——每次转写开头清空，
        # 失败分支写入；级联转录器把各级的串拼起来。纯观测字段，不改任何判定。
        self.last_error: str = ""

        # ASR P0（2026-09-12）：最近一次转写的元数据（检出语种 / 语种概率 / avg_logprob /
        # no_speech_prob / 时长 / 是否命中缓存 / 是否按先验重转…），随 last_error 同为
        # 纯观测字段——上层据此做低置信标记（P1 三态），本模块不据此改判。
        self.last_meta: Dict[str, Any] = {}
        _tc = config.get('transcript_cache') if isinstance(
            config.get('transcript_cache'), dict) else {}
        # 转写缓存（同一音频再来一遍直接命中；负缓存只收 no_speech/empty）。默认开。
        self.transcript_cache_enabled = bool(_tc.get('enabled', True))
        # 语种先验复核：检出语种≠先验且置信 < 此值 → 按先验重转一次
        try:
            self.lang_hint_min_prob = float(config.get('lang_hint_min_prob', 0.6) or 0.6)
        except (TypeError, ValueError):
            self.lang_hint_min_prob = 0.6

        self.logger.info(f"语音转录服务初始化，临时目录: {self.temp_dir}")

    # ── 顶层入口：缓存 → 守卫转写 → 语种先验复核（级联子级不走这层，由父级统一）──
    async def transcribe_voice_message(
        self, voice_file_path: str, language: str = "zh", *,
        lang_hint: Optional[str] = None,
    ) -> Optional[str]:
        """
        转录语音文件为文本

        Args:
            voice_file_path: 语音文件路径
            language: 语言代码（zh=中文，auto=自动检测）
            lang_hint: 会话语种先验（可选）。**不**直接钉死 language——先按 language
                转，检出语种与先验冲突且置信不足时再按先验重转一次
                （见 should_retry_with_lang_hint）。

        Returns:
            转录的文本，如果失败返回None
        """
        if self._asr_chained_child:
            # 级联子级：缓存/先验复核由 FallbackTranscriber 统一做，这里只做守卫转写
            return await self._transcribe_guarded(voice_file_path, language)
        self.last_error = ""
        self.last_meta = {}
        key = self._cache_lookup_key(voice_file_path, language)
        found, hit = self._cache_get(key)
        if found:
            return hit
        text = await self._transcribe_guarded(voice_file_path, language)
        meta = dict(self.last_meta or {})
        if text and lang_hint:
            async def _retry(lang: str) -> Tuple[Optional[str], Dict[str, Any]]:
                out = await self._transcribe_guarded(voice_file_path, lang, record=False)
                return out, dict(self.last_meta or {})
            text, meta = await self._apply_lang_hint(text, meta, lang_hint, _retry)
        meta.setdefault("provider", self.__class__.__name__)
        self.last_meta = meta
        self._cache_put(key, text, meta, self.last_error)
        return text

    def _cache_scope(self) -> str:
        """缓存作用域：转录器类型 + 端点/模型——同配置的不同实例（TelegramClient 常驻的
        与 lazy_voice_transcriber 懒建的）共享，不同后端各存各的。"""
        parts = [self.__class__.__name__]
        for attr in ("base_url", "model", "model_size", "model_dir"):
            v = getattr(self, attr, None)
            if isinstance(v, str) and v:
                parts.append(v)
        return "|".join(parts)

    def _cache_lookup_key(self, voice_file_path: str, language: str) -> str:
        if not self.transcript_cache_enabled:
            return ""
        base = transcript_cache_key(voice_file_path, language)
        return f"{base}|{self._cache_scope()}" if base else ""

    def _cache_get(self, key: str) -> Tuple[bool, Optional[str]]:
        """→ ``(found, text)``。命中时回填 last_meta（带 ``cache_hit``）与 last_error
        （负缓存带 ``cache:`` 前缀说明来源）；未命中 → ``(False, None)`` 且不动状态。"""
        if not key:
            return False, None
        got = get_transcript_cache().get(key)
        if got is _TC_MISS:
            return False, None
        text, meta, err = got
        meta = dict(meta or {})
        meta["cache_hit"] = True
        self.last_meta = meta
        self.last_error = (f"cache:{err}" if err else "")
        _record_asr_event("cache_hit")
        self.logger.info("[asr] 转写缓存命中（免重转） text=%s err=%s",
                         (text or "")[:40], err or "-")
        return True, (text or None)

    def _cache_put(self, key: str, text: Optional[str], meta: Dict[str, Any],
                   last_error: str) -> None:
        if not key:
            return
        try:
            m = {k: v for k, v in (meta or {}).items() if k != "cache_hit"}
            get_transcript_cache().put(key, text, m, last_error)
        except Exception:
            pass

    async def _apply_lang_hint(
        self, text: str, meta: Dict[str, Any], lang_hint: str,
        retry: Callable[[str], Awaitable[Tuple[Optional[str], Dict[str, Any]]]],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """检出语种 vs 会话先验冲突且置信不足 → 按先验重转一次；重转结果的文字系统
        必须与先验同家族才采用（防强制语言让 Whisper 变成翻译器），否则保留原转写并
        置 ``lang_suspect`` 标记（上层可走「听不清请确认」话术）。

        ``retry(lang)`` → ``(text, meta)``：由调用方决定在哪一级重转（单转录器＝自身
        守卫转写；级联＝胜出的那一级）。"""
        hint = str(lang_hint or "").strip().lower()
        if not hint or not text:
            return text, meta
        if not should_retry_with_lang_hint(meta, text, hint, min_prob=self.lang_hint_min_prob):
            return text, meta
        _record_asr_event("lang_retry")
        self.logger.info(
            "[asr] 检出语种 %s(p=%s) 与会话先验 %s 冲突 → 按先验重转一次",
            meta.get("language") or "?", meta.get("language_probability", "-"), hint)
        retry_text: Optional[str] = None
        retry_meta: Dict[str, Any] = {}
        try:
            retry_text, retry_meta = await retry(hint)
        except Exception as e:  # noqa: BLE001 - 复核失败不影响原转写
            self.logger.debug("[asr] 先验重转异常（保留原转写）: %s", e)
        if retry_text and str(retry_text).strip():
            fam_ok = lang_family_of_text(retry_text) in ("", _lang_family(hint))
            if fam_ok:
                out = dict(retry_meta or {})
                out.update({"lang_retry": True, "lang_retry_from": meta.get("language"),
                            "language": _lang_family(hint)})
                _record_asr_event("lang_retry_changed")
                self.logger.info("[asr] 先验重转采用: %s → %s",
                                 str(text)[:40], str(retry_text)[:40])
                return str(retry_text).strip(), out
        out = dict(meta)
        out.update({"lang_retry": True, "lang_suspect": True})
        return text, out

    async def _transcribe_guarded(
        self, voice_file_path: str, language: str = "zh", *, record: bool = True,
    ) -> Optional[str]:
        """守卫转写（原公有方法主体）：存在性/大小检查 → 具体实现 → 幻觉守卫 →
        繁简归一 → 观测计数。``record=False`` 用于先验重转（不重复计顶层成败）。"""
        self.last_error = ""
        self.last_meta = {}
        try:
            # 检查文件是否存在
            if not os.path.exists(voice_file_path):
                self.logger.error(f"语音文件不存在: {voice_file_path}")
                self.last_error = "file_not_found"
                return None

            # 检查文件大小
            file_size = os.path.getsize(voice_file_path)
            if file_size > self.max_file_size:
                self.logger.warning(f"语音文件过大: {file_size} bytes > {self.max_file_size} limit")
                self.last_error = f"file_too_large: {file_size} > {self.max_file_size}"
                return None

            # 调用具体实现
            text = await self._transcribe_impl(voice_file_path, language)

            if text:
                text = text.strip()
                # 幻觉守卫：疑似尾字幕套话/退化输出 → 丢弃（等同空结果，交上层回落
                # media_ack「听不清」，而非拿无关内容当真回复）。级联转录器据此续试下一级。
                if self.hallucination_guard and looks_like_asr_hallucination(text):
                    self.logger.warning(f"语音转录疑似幻觉，已丢弃: {text[:60]}")
                    self._record_asr(hallucination=True)
                    self.last_error = "no_speech: hallucination_guard"
                    return None
                text = self._normalize_zh_script(text)
                self.logger.info(f"语音转录成功: {text[:100]}...")
                if record and not self._asr_chained_child:
                    self._record_asr(ok=True, level=0)
                return text
            else:
                self.logger.warning("语音转录返回空结果")
                if record and not self._asr_chained_child:
                    self._record_asr(ok=False)
                if not self.last_error:
                    self.last_error = "empty_result"
                return None

        except Exception as e:
            self.logger.error(f"语音转录失败: {e}")
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    def _normalize_zh_script(self, text: str) -> str:
        """中文转写繁→简归一（``zh_simplified_output``，默认开）。

        复用 ``lang_voice_route.to_simplified_for_tts``：非中文主体（日文汉字同形）
        与粤文原样返回，opencc 缺失恒等；任何异常原样返回，绝不影响转写主链。
        """
        if not self.zh_simplified_output or not text:
            return text
        try:
            from src.ai.lang_voice_route import to_simplified_for_tts
            out = to_simplified_for_tts(text)
            if out and out != text:
                self.logger.info(
                    "[asr] 转写繁→简归一: %s → %s", text[:40], out[:40])
                return out
        except Exception:
            pass
        return text

    def _record_asr(self, *, ok: bool = False, level: int = 0,
                    hallucination: bool = False) -> None:
        """记一次 ASR 观测（best-effort，绝不阻塞/抛给转录主链路）。"""
        try:
            from src.ai.asr_stats import get_asr_stats
            stats = get_asr_stats()
            if hallucination:
                stats.record_hallucination(self.__class__.__name__)
            else:
                stats.record(ok=ok, level=level, provider=self.__class__.__name__)
        except Exception:
            pass

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """具体转录实现（由子类重写）"""
        raise NotImplementedError("子类必须实现此方法")

    async def warmup(self) -> None:
        """启动预热钩子：默认无操作。

        懒加载型子类（如 SenseVoice）重写之，在启动阶段后台预载模型，
        消掉重启后首条语音的冷启动延迟（实测 ~20-30s）。远程/云端型
        转录器保持无操作——启动时不应产生外呼流量。
        """
        return None

    def cleanup_temp_files(self):
        """清理临时文件"""
        try:
            import shutil
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
                self.logger.info(f"清理临时目录: {self.temp_dir}")
        except Exception as e:
            self.logger.warning(f"清理临时文件失败: {e}")

class WhisperLocalTranscriber(VoiceTranscriber):
    """本地Whisper模型转录服务"""

    def __init__(self, config: Dict[str, Any]):
        """初始化本地Whisper转录服务"""
        super().__init__(config)

        # Whisper配置
        whisper_config = config.get('whisper', {})
        self.model_size = whisper_config.get('model_size', 'base')
        self.device = whisper_config.get('device', 'cpu')
        self.download_root = Path(whisper_config.get('download_root', './models/whisper'))
        self.download_root.mkdir(parents=True, exist_ok=True)

        self.model = None
        self.logger.info(f"Whisper本地转录服务初始化，模型: {self.model_size}，设备: {self.device}")

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用本地Whisper模型转录"""
        try:
            # 延迟导入，只在需要时加载
            import whisper

            # 加载模型（第一次运行会下载）
            if self.model is None:
                self.logger.info(f"加载Whisper模型: {self.model_size}")
                self.model = whisper.load_model(
                    name=self.model_size,
                    device=self.device,
                    download_root=str(self.download_root)
                )

            # 转录语音
            self.logger.info(f"开始转录: {voice_file_path}")
            result = self.model.transcribe(
                audio=voice_file_path,
                language=language if language != "auto" else None,
                fp16=False  # CPU模式关闭fp16
            )

            text = result.get("text", "").strip()
            return text

        except ImportError:
            self.logger.error("Whisper未安装，请运行: pip install openai-whisper")
            return None
        except Exception as e:
            self.logger.error(f"Whisper转录失败: {e}")
            return None

class FasterWhisperTranscriber(VoiceTranscriber):
    """Faster-Whisper转录服务（更快更轻量）"""

    def __init__(self, config: Dict[str, Any]):
        """初始化Faster-Whisper转录服务"""
        super().__init__(config)

        # Faster-Whisper配置
        whisper_config = config.get('whisper', {})
        self.model_size = whisper_config.get('model_size', 'base')
        self.device = whisper_config.get('device', 'cpu')
        self.compute_type = "int8" if self.device == "cpu" else "float16"
        self.download_root = Path(whisper_config.get('download_root', './models/faster-whisper'))

        self.model = None
        self.logger.info(f"Faster-Whisper转录服务初始化，模型: {self.model_size}")

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用Faster-Whisper转录"""
        try:
            # 延迟导入
            from faster_whisper import WhisperModel

            # 加载模型
            if self.model is None:
                self.logger.info(f"加载Faster-Whisper模型: {self.model_size}")
                self.model = WhisperModel(
                    model_size_or_path=self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=str(self.download_root)
                )

            # 转录语音
            self.logger.info(f"开始转录: {voice_file_path}")
            # 热词/专有名词引导（治同音误转："智聊"→"治療"、"回复"→"恢复"）：
            # initial_prompt 把产品名/业务词喂给 whisper 当上文，显著提升专名命中。
            # 解析口径与 OpenAI 兼容路同源（hotwords_prompt）。
            _hotwords = hotwords_prompt(self.config)
            _kw = {}
            if _hotwords:
                _kw['initial_prompt'] = _hotwords
            segments, info = self.model.transcribe(
                audio=voice_file_path,
                language=language if language != "auto" else None,
                beam_size=5,
                vad_filter=True,  # 语音活动检测（滤静音，减尾部幻觉）
                condition_on_previous_text=False,  # 防跨段串读/幻觉传播（whisper 通病）
                **_kw,
            )

            # 合并所有片段（顺带聚合置信度进 last_meta，与 OpenAI 兼容路同形）
            texts, lps, nsps = [], [], []
            for segment in segments:
                texts.append(str(getattr(segment, "text", "") or ""))
                lp = getattr(segment, "avg_logprob", None)
                if isinstance(lp, (int, float)):
                    lps.append(float(lp))
                ns = getattr(segment, "no_speech_prob", None)
                if isinstance(ns, (int, float)):
                    nsps.append(float(ns))
            meta: Dict[str, Any] = {"provider": self.__class__.__name__}
            _lang = getattr(info, "language", None)
            if _lang:
                meta["language"] = str(_lang)
            _lp = getattr(info, "language_probability", None)
            if isinstance(_lp, (int, float)):
                meta["language_probability"] = float(_lp)
            if lps:
                meta["avg_logprob"] = sum(lps) / len(lps)
            if nsps:
                meta["no_speech_prob"] = max(nsps)
            self.last_meta = meta
            text = " ".join(texts)
            return text.strip()

        except ImportError:
            self.logger.error("Faster-Whisper未安装，请运行: pip install faster-whisper")
            return None
        except Exception as e:
            self.logger.error(f"Faster-Whisper转录失败: {e}")
            return None

class OpenAITranscriber(VoiceTranscriber):
    """OpenAI Whisper API转录服务"""

    def __init__(self, config: Dict[str, Any]):
        """初始化OpenAI API转录服务"""
        super().__init__(config)

        # OpenAI配置
        openai_config = config.get('openai', {})
        self.api_key = openai_config.get('api_key') or config.get('api_key')
        self.model = openai_config.get('model') or config.get('model') or 'whisper-1'
        self.base_url = openai_config.get('base_url') or config.get('base_url') or None

        # 快速失败护栏：主转录（如本机 Qwen3-ASR）不可达/慢时，限超时且不重试 →
        # 立刻回落兜底转录（见 FallbackTranscriber），既不空等重试也不阻塞理解链。
        # timeout 沿用 voice_recognition.timeout（缺省 30s）；max_retries 默认 0（连不上即回落）。
        try:
            self.timeout_sec = float(
                openai_config.get('timeout') or config.get('timeout') or 30
            )
        except (TypeError, ValueError):
            self.timeout_sec = 30.0
        try:
            self.max_retries = int(
                openai_config.get('max_retries', config.get('max_retries', 0))
            )
        except (TypeError, ValueError):
            self.max_retries = 0

        # ASR P0（2026-09-12）：按音频时长的动态超时（见 effective_timeout）。
        # timeout 仍是短/未知时长音频的基础超时（行为不变），长音频按时长放宽到 timeout_max。
        def _f(key: str, default: float) -> float:
            try:
                v = openai_config.get(key, config.get(key, default))
                return float(default if v is None else v)
            except (TypeError, ValueError):
                return float(default)
        self.timeout_per_audio_sec = _f('timeout_per_audio_sec', 0.25)
        self.timeout_overhead_sec = _f('timeout_overhead_sec', 1.5)
        self.timeout_max_sec = _f('timeout_max', 10.0)
        # 服务端 no_speech_prob（verbose_json 才有）≥ 此值 → 按无人声丢弃（与备路
        # AvatarWhisper 的 max_no_speech_prob 同口径 0.85）
        self.max_no_speech_prob = _f('max_no_speech_prob', 0.85)
        # 热词经标准 prompt 字段送出。**默认关**——2026-09-12 在 198 同模型 A/B：任何
        # 形式的 prompt（顿号列表 / 自然句 / 原生 hotwords）都让边缘音频（4.25s 探针夹具）
        # 从稳定正确变成「M-M-M-M」「Lingo、Lingo」「声音 音乐 声音」类乱码，而在干净的
        # 11s / 3s 片段上只多了标点、正文零变化。管路保留（服务端认 prompt），等评测集
        # 有含产品名的真实样本再 A/B；开启时只对 ≥ hotwords_min_duration_sec 的音频送
        # （短片段是 prompt 回声高发区：1.47s 片段直接吐回「智聊ChatX」）。
        self.send_hotwords = bool(openai_config.get(
            'send_hotwords', config.get('send_hotwords', False)))
        self.hotwords_min_duration_sec = _f('hotwords_min_duration_sec', 3.0)
        # 响应格式：verbose_json 拿检出语种/时长（自建服务另附置信度）；端点若不认
        # （400）→ 粘性降级回 json，本进程内不再尝试。
        self.response_format = str(openai_config.get(
            'response_format', config.get('response_format', 'verbose_json'))
            or 'verbose_json').strip().lower()
        self._verbose_json_ok = True

        if not self.api_key:
            self.logger.warning("OpenAI API密钥未配置")

        # 复用同一个客户端（见 _get_client 的事故说明）
        self._client: Any = None
        self._client_fp: Any = None
        self._client_lock = threading.Lock()

        _ep = self.base_url or "https://api.openai.com/v1"
        self.logger.info(f"OpenAI Whisper API转录服务初始化 endpoint={_ep} model={self.model}")

    def _get_client(self, api_key: str) -> Any:
        """取（并缓存）OpenAI 客户端。

        2026-09-04 kouxing 事故的一半根因：此前**每次转录都新建** ``openai.OpenAI``，
        每个客户端各自带一条 httpx 连接池且从不 ``close()`` → 句柄只涨不落。按凭据
        指纹缓存即可复用；托管令牌 30 天换新时指纹变化会自然重建，不必重启进程。
        """
        import openai
        fp = (api_key, self.base_url or "", self.timeout_sec, self.max_retries)
        with self._client_lock:
            if self._client is None or self._client_fp != fp:
                kwargs: Dict[str, Any] = {
                    "api_key": api_key,
                    "max_retries": self.max_retries,
                    "timeout": self.timeout_sec,
                }
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                old = self._client
                self._client = openai.OpenAI(**kwargs)
                self._client_fp = fp
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
            return self._client

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用OpenAI Whisper API转录"""
        try:
            # 延迟导入
            import openai

            # 检查API密钥
            if not self.api_key:
                self.logger.error("OpenAI API密钥未配置")
                return None

            # 托管占位值 "hosted"（hosted_gateway 注入）→ 调用时解析当前设备令牌。
            # 转写器构建一次常驻，令牌 30 天换新；在这里取 env 而非构建期取值，
            # 换新后无需重建转写器。令牌尚未领到 → 如实失败交给下一级 fallback。
            api_key = str(self.api_key or "")
            if api_key.strip().lower() == "hosted":
                api_key = str(os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
                if not api_key:
                    self.logger.warning("托管转写：设备令牌尚未就绪（AITR_HOSTED_AI_KEY 空）")
                    self.last_error = "hosted_token_missing: AITR_HOSTED_AI_KEY empty"
                    return None

            # 客户端复用（支持 OpenAI 兼容端点：Groq / SiliconFlow / 本地等）。
            # max_retries=0 + timeout：主转录不可达/慢时快速失败回落，不重试、不阻塞理解链。
            client = self._get_client(api_key)

            # 动态超时：按容器头估时长（不起子进程），长音频放宽、短音频按基础超时。
            duration = estimate_audio_duration(voice_file_path)
            eff_timeout = effective_timeout(
                self.timeout_sec, duration,
                per_audio_sec=self.timeout_per_audio_sec,
                overhead_sec=self.timeout_overhead_sec,
                cap_sec=self.timeout_max_sec,
            )
            # 热词经标准 prompt 字段：只在中文/自动检测时送（外语强制语种时送中文
            # 热词毫无意义）；服务端（asr_server.py）当 initial_prompt 用，网关透传。
            lang_param = language if language and language != "auto" else None
            prompt = ""
            if (self.send_hotwords
                    and (lang_param is None or _lang_family(lang_param) == "zh")
                    and (duration is None or duration >= self.hotwords_min_duration_sec)):
                prompt = hotwords_prompt(self.config)
            fmt = self.response_format if (
                self.response_format != "verbose_json" or self._verbose_json_ok) else "json"

            # SDK 的 transcriptions.create 是**同步阻塞**调用：直接在事件循环上跑会把
            # 整个 web 进程按住整个 ASR 时长（实录单条 25s 墙钟）——期间边车重投、坐席
            # 轮询全部堆在待处理连接上，越过 Windows ``select()`` 512 上限即整体崩。
            # 故挪进线程池；并发上限由入站侧的转录闸控制（见
            # ``unified_inbox_account_routes._ingest_asr_sem``）。
            def _call(response_format: str) -> Any:
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "language": lang_param,
                    "response_format": response_format,
                    # 请求级超时（SDK 原生支持），客户端与连接池仍复用
                    "timeout": eff_timeout,
                }
                if prompt:
                    kwargs["prompt"] = prompt
                with open(voice_file_path, 'rb') as audio_file:
                    return client.audio.transcriptions.create(file=audio_file, **kwargs)

            self.logger.info(
                "调用OpenAI Whisper API: %s (dur=%s timeout=%.1fs fmt=%s prompt=%s)",
                voice_file_path, f"{duration:.1f}s" if duration else "?",
                eff_timeout, fmt, "yes" if prompt else "no")
            t0 = time.monotonic()
            try:
                resp = await asyncio.to_thread(_call, fmt)
            except openai.BadRequestError as e:
                if fmt == "verbose_json":
                    # 端点不认 verbose_json（部分网关/云模型）→ 本进程内粘性降级为 json
                    self._verbose_json_ok = False
                    self.logger.warning(
                        "OpenAI 兼容端点拒绝 verbose_json（%s），降级为 json 重试一次", e)
                    resp = await asyncio.to_thread(_call, "json")
                else:
                    raise
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            meta = extract_transcription_meta(resp)
            text = str(meta.pop("text", "") or "").strip()
            if duration and "duration" not in meta:
                meta["duration"] = float(duration)
            meta["timeout_sec"] = eff_timeout
            meta["elapsed_ms"] = elapsed_ms
            meta["provider"] = self.__class__.__name__
            self.last_meta = meta

            nsp = meta.get("no_speech_prob")
            if text and isinstance(nsp, (int, float)) and float(nsp) >= self.max_no_speech_prob:
                # 服务端逐段 no_speech 极高＝很可能是静音上的幻觉；与备路 AvatarWhisper
                # 的 max_no_speech_prob 闸同口径，丢弃交回落/上层 ack
                self.logger.warning(
                    "[asr] no_speech_prob=%.2f ≥ %.2f，按无人声丢弃: %s",
                    float(nsp), self.max_no_speech_prob, text[:40])
                self.last_error = f"no_speech: no_speech_prob={float(nsp):.2f}"
                _record_asr_event("no_speech_gate")
                return None
            if text and prompt and looks_like_prompt_echo(
                    text, prompt, nsp,
                    duration=meta.get("duration"),
                    language_probability=meta.get("language_probability")):
                self.logger.warning("[asr] 转写疑为 prompt 回声（热词原样吐回），丢弃: %s", text[:40])
                self.last_error = "no_speech: prompt_echo"
                _record_asr_event("prompt_echo_dropped")
                return None
            lp = meta.get("avg_logprob")
            lprob = meta.get("language_probability")
            low = (isinstance(lp, (int, float)) and float(lp) < -1.0) or (
                isinstance(lprob, (int, float)) and lang_param is None and float(lprob) < 0.6)
            if text and low:
                meta["low_confidence"] = True
                _record_asr_event("low_confidence")
            self.logger.info(
                "[asr] openai ok=%s lang=%s p=%s logprob=%s nsp=%s dur=%s elapsed=%dms",
                bool(text), meta.get("language", "?"),
                f"{lprob:.2f}" if isinstance(lprob, (int, float)) else "-",
                f"{lp:.2f}" if isinstance(lp, (int, float)) else "-",
                f"{nsp:.2f}" if isinstance(nsp, (int, float)) else "-",
                f"{meta.get('duration'):.1f}s" if isinstance(meta.get("duration"), (int, float)) else "?",
                elapsed_ms)
            return text or None

        except ImportError:
            self.logger.error("OpenAI库未安装，请运行: pip install openai")
            self.last_error = "missing dependency: openai"
            return None
        except Exception as e:
            self.logger.error(f"OpenAI转录失败: {e}")
            self.last_error = f"{type(e).__name__}: {e}"
            return None

class AvatarWhisperTranscriber(VoiceTranscriber):
    """AvatarHub 集群 Whisper STT（远端 192.168.0.140:7854，跨机需 X-AH-Svc 令牌）。

    契约：POST {base_url}/transcribe_b64 {"audio_base64","language"}
          → {"ok":true,"text":"...","no_speech_prob":...}
    令牌**运行时**解析（``avatar_voice.resolve_service_token``：配置 token_file →
    网关设备令牌 → 开发机路径），绝不写进代码库/日志。令牌缺失/服务不可达 →
    返 None（级联回落下一级，不抛），且同类告警每进程只出一次。
    入站 ogg/opus 先经 ffmpeg 转 16k 单声道 WAV（识别更稳）；ffmpeg 缺失则送原始字节。
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        av = config.get('avatar') if isinstance(config.get('avatar'), dict) else {}
        # L-6 B（2026-09-06）：内网 STT 地址只是**办公室部署**的缺省——桌面客户机上它永远
        # 探不通，每条语音白吃一次连接超时后返空（skuio 机「转录双级返空」的第二级）。
        # 桌面态未显式配置 → 空串＝未配置，_transcribe_impl 直接让位下一级（网关 ASR）。
        from src.utils.desktop_mode import lan_default_or_empty
        self.base_url = lan_default_or_empty(
            av.get('base_url') or config.get('base_url'),
            'http://192.168.0.140:7854').rstrip('/')
        # #161（2026-09-03 钧机 22:54）：开发机路径不再当内置默认——客户机上它
        # 恒不存在，令牌解析却看起来「配过了」，AvatarHub STT 二级回落遂静默失败
        # （上一级刚把「타 타 타」判幻觉丢弃，这一级没接住＝整条转写链断）。
        # 空串＝未配置，由 resolve_service_token 走网关设备令牌 → 开发机路径两级
        # 回落（本机/LAN 部署行为不变）。
        self.token_file = str(
            av.get('token_file') or config.get('token_file') or '')
        try:
            self.timeout_sec = float(
                av.get('timeout') or config.get('timeout') or 30)
        except (TypeError, ValueError):
            self.timeout_sec = 30.0
        try:
            self.max_no_speech_prob = float(
                av.get('max_no_speech_prob')
                or config.get('max_no_speech_prob') or 0.85)
        except (TypeError, ValueError):
            self.max_no_speech_prob = 0.85
        self._unconfigured_warned = False
        self.logger.info(
            "AvatarHub Whisper STT 转录服务初始化 endpoint=%s",
            self.base_url or "(未配置：桌面版不带内网缺省地址)")

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        from src.ai.avatar_voice import (
            build_stt_payload,
            convert_to_wav_16k_mono,
            parse_stt_response,
            resolve_service_token,
            warn_token_missing_once,
        )

        if not self.base_url:
            if not self._unconfigured_warned:
                self._unconfigured_warned = True
                self.logger.warning(
                    "AvatarHub STT 未配置端点（桌面版不再默认指向内网 192.168.0.140:7854），"
                    "本级让位；语音转写由网关 ASR 负责")
            return None

        token = resolve_service_token(self.token_file)
        if not token:
            warn_token_missing_once("AvatarHub STT", self.token_file)
            return None

        # ogg/opus → 16k 单声道 WAV（best-effort；失败送原始字节，服务端也能解）
        send_path = voice_file_path
        cleanup: Optional[str] = None
        if not str(voice_file_path).lower().endswith('.wav'):
            wav = await asyncio.to_thread(convert_to_wav_16k_mono, voice_file_path)
            if wav:
                send_path = wav
                cleanup = wav

        try:
            audio_bytes = Path(send_path).read_bytes()
            # 语言映射（2026-07-13 实测 7854 契约）：具体语种=强制转写语言；
            # "auto"/空 → 空串=服务端自动检测。⚠ 绝不能把 auto 映射成 zh——
            # Whisper 对不匹配语言会**翻译**而非转写（英文语音+zh → 中文译文，
            # AI 会用错误语言回复外语用户）。
            lang = language if language and language != 'auto' else ''
            payload = build_stt_payload(audio_bytes, language=lang)

            def _post() -> bytes:
                import urllib.request
                req = urllib.request.Request(
                    f"{self.base_url}/transcribe_b64", data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-AH-Svc": token,
                    }, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as r:
                    return r.read()

            body = await asyncio.to_thread(_post)
            return parse_stt_response(
                body, max_no_speech_prob=self.max_no_speech_prob)
        except Exception as e:
            self.logger.warning(f"AvatarHub STT 转录失败: {e}")
            return None
        finally:
            if cleanup:
                try:
                    Path(cleanup).unlink(missing_ok=True)
                except Exception:
                    pass


class SenseVoiceTranscriber(VoiceTranscriber):
    """SenseVoice-Small（FunASR）本机转录：中文方言/粤语显著强于 Whisper。

    背景：Whisper base/small 对粤语基本失真（实测「漂唔漂亮」→「票股票量」）、
    短音频易语言误判幻觉（中文→葡语）。SenseVoice-Small 训练含大量中文方言语料，
    支持 zh/yue(粤)/en/ja/ko 自动检测，非自回归推理比 Whisper 快 ~15x，
    RTX 3060 上单条语音亚秒级。模型 ~900MB，首次使用自动从 ModelScope 拉取
    （国内网络友好，无需 HF 代理）。

    配置（voice_recognition.sensevoice.*）：
      model_dir: 模型名或本地路径（默认 iic/SenseVoiceSmall）
      device:    cuda:0 / cpu（默认自动：有 CUDA 用 CUDA）
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        sv_cfg = config.get('sensevoice', {}) or {}
        self.model_dir = str(sv_cfg.get('model_dir') or 'iic/SenseVoiceSmall')
        dev = sv_cfg.get('device')
        if not dev:
            try:
                import torch
                dev = 'cuda:0' if torch.cuda.is_available() else 'cpu'
            except Exception:
                dev = 'cpu'
        self.device = str(dev)
        self.model = None
        self._load_lock = asyncio.Lock()
        self.logger.info(
            f"SenseVoice 转录服务初始化 model={self.model_dir} device={self.device}")

    # SenseVoice 语言码映射：客户会话语言 → 模型 language 参数
    _LANG_MAP = {
        'zh': 'zh', 'yue': 'yue', 'en': 'en', 'ja': 'ja', 'ko': 'ko',
        'auto': 'auto', '': 'auto',
    }

    async def _ensure_model(self):
        if self.model is not None:
            return
        async with self._load_lock:
            if self.model is not None:
                return

            def _load():
                from funasr import AutoModel
                # vad 前置切段：长语音稳、静音不产幻觉（与 whisper vad_filter 对等）
                return AutoModel(
                    model=self.model_dir,
                    vad_model="fsmn-vad",
                    vad_kwargs={"max_single_segment_time": 30000},
                    device=self.device,
                    disable_update=True,
                )

            self.logger.info(f"加载 SenseVoice 模型: {self.model_dir}（首次会自动下载）")
            self.model = await asyncio.to_thread(_load)
            self.logger.info("SenseVoice 模型加载完成")

    async def warmup(self) -> None:
        """启动预热：提前加载模型（幂等，_ensure_model 自带锁与已载判断）。"""
        await self._ensure_model()

    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        try:
            await self._ensure_model()

            lang = self._LANG_MAP.get(str(language or 'auto').lower(), 'auto')

            def _run():
                res = self.model.generate(
                    input=str(voice_file_path),
                    cache={},
                    language=lang,       # auto 自动检测（含粤语）
                    use_itn=True,        # 数字/标点正规化
                    batch_size_s=60,
                    merge_vad=True,
                    merge_length_s=15,
                )
                if not res:
                    return ""
                raw = " ".join(str(r.get("text", "")) for r in res if r)
                try:
                    # 剥掉 <|zh|><|NEUTRAL|><|Speech|> 等富文本标记，只留正文
                    from funasr.utils.postprocess_utils import (
                        rich_transcription_postprocess,
                    )
                    return rich_transcription_postprocess(raw)
                except Exception:
                    return re.sub(r"<\|[^|]*\|>", "", raw).strip()

            text = await asyncio.to_thread(_run)
            return (text or "").strip() or None

        except ImportError:
            self.logger.error("funasr 未安装，请运行: pip install funasr")
            return None
        except Exception as e:
            self.logger.error(f"SenseVoice 转录失败: {e}")
            return None


class FallbackTranscriber(VoiceTranscriber):
    """主/备转录级联：主转录器返空或抛错 → 自动回落下一个（绝不阻塞理解链）。

    典型用法：主 = Qwen3-ASR（方言/口音强，OpenAI 兼容本机端点），
    备 = faster_whisper（本机常驻、离线可用）。主机不可达/超时/返空时无缝回落，
    使「换更强 ASR」不引入「服务一挂全链崩」的单点风险。
    """

    def __init__(self, config: Dict[str, Any], transcribers):
        super().__init__(config)
        self._chain = [t for t in transcribers if t is not None]
        # 子级不自记「顶层成败」（由本级统一记，含"胜出在第几级"=主用/回落）；
        # 幻觉丢弃仍由子级基类各自记（那是每级独立事件）。
        for t in self._chain:
            try:
                t._asr_chained_child = True
            except Exception:
                pass
        _names = " → ".join(t.__class__.__name__ for t in self._chain) or "(空)"
        self.logger.info(f"级联转录服务初始化: {_names}")

    def _cache_scope(self) -> str:
        # 级联按主级作用域缓存（回落级转出的文本也是「这段音频的转写」，同键复用）
        return ("Fallback>" + self._chain[0]._cache_scope()) if self._chain else "Fallback"

    async def warmup(self) -> None:
        """只预热主转录器（chain[0]）：回落级是小概率路径，启动即加载
        会白占显存/内存（本机 GPU 显存紧张纪律）；主级挂了再懒加载兜底。"""
        if self._chain:
            await self._chain[0].warmup()

    async def transcribe_voice_message(
        self, voice_file_path: str, language: str = "zh", *,
        lang_hint: Optional[str] = None,
    ) -> Optional[str]:
        last_err: Optional[Exception] = None
        self.last_error = ""
        self.last_meta = {}
        # 转写缓存（ASR P0）：同一音频（协议入站落库前 + AutoDraft 两条路各转一遍）
        # 第二次直接命中；负缓存只收「内容决定的空结果」。
        key = self._cache_lookup_key(voice_file_path, language)
        found, hit = self._cache_get(key)
        if found:
            return hit
        errs = []   # M-5 D：各级失败原因（工具箱链据此分类「超时/被拒/不可达」）
        for idx, t in enumerate(self._chain):
            try:
                text = await t.transcribe_voice_message(voice_file_path, language)
            except Exception as e:  # noqa: BLE001 - 逐级兜底，绝不抛给理解链
                last_err = e
                errs.append(f"{t.__class__.__name__}: {type(e).__name__}: {e}")
                self.logger.warning(
                    f"转录器 {t.__class__.__name__} 异常，尝试回落下一级: {e}"
                )
                continue
            if not text:
                errs.append(f"{t.__class__.__name__}: "
                            f"{getattr(t, 'last_error', '') or 'empty_result'}")
            if text:
                # level=idx：0=主 ASR 直接成功；>=1=回落级成功（降级信号，按胜出 provider 归类）。
                # 只记一次、且用胜出转录器类名（非本级 Fallback 类名）。
                try:
                    from src.ai.asr_stats import get_asr_stats
                    get_asr_stats().record(
                        ok=True, level=idx, provider=t.__class__.__name__)
                except Exception:
                    pass
                meta = dict(getattr(t, "last_meta", {}) or {})
                if lang_hint:
                    async def _retry(lang: str, _t: Any = t) -> Tuple[Optional[str], Dict[str, Any]]:
                        out = await _t.transcribe_voice_message(voice_file_path, lang)
                        return out, dict(getattr(_t, "last_meta", {}) or {})
                    text, meta = await self._apply_lang_hint(text, meta, lang_hint, _retry)
                meta["provider"] = t.__class__.__name__
                meta["level"] = idx
                self.last_meta = meta
                self._cache_put(key, text, meta, "")
                return text
            self.logger.warning(
                f"转录器 {t.__class__.__name__} 返空，尝试回落下一级"
            )
        if last_err is not None:
            self.logger.error(f"全部转录器失败，最后错误: {last_err}")
        self.last_error = " | ".join(errs)[:600] or "empty_result"
        try:
            from src.ai.asr_stats import get_asr_stats
            get_asr_stats().record(ok=False)
        except Exception:
            pass
        # 全链返空且每级都是「内容决定」的空（no_speech/empty_result）→ 负缓存，
        # AutoDraft 十几秒后再来不必让 GPU 再白跑两遍；任一级是超时/被拒/异常则不缓存。
        if key and errs and last_err is None and all(
                (": no_speech" in e or e.endswith(": empty_result")) for e in errs):
            self._cache_put(key, None, {"provider": self.__class__.__name__}, "no_speech: all_levels")
        return None

    async def _transcribe_impl(
        self, voice_file_path: str, language: str
    ) -> Optional[str]:  # pragma: no cover - 级联已重写公有方法
        return None


class VoiceTranscriberFactory:
    """语音转录服务工厂"""

    @staticmethod
    def _create_one(config: Dict[str, Any]) -> VoiceTranscriber:
        """按 provider 建单个转录器（不含级联）。"""
        provider = str(config.get('provider', 'whisper_local')).strip().lower()

        # qwen3_asr / funasr_api 等本机 OpenAI 兼容 ASR 服务复用 OpenAI 转录器
        # （契约相同：POST {base_url}/audio/transcriptions），仅换 base_url/model。
        if provider in ('openai', 'qwen3_asr', 'funasr_api', 'openai_compatible'):
            return OpenAITranscriber(config)
        elif provider in ('sensevoice', 'sense_voice', 'funasr'):
            # 进程内 FunASR/SenseVoice（方言/粤语主力，无需独立服务）
            return SenseVoiceTranscriber(config)
        elif provider in ('avatar_whisper', 'avatarhub'):
            return AvatarWhisperTranscriber(config)
        elif provider == 'faster_whisper':
            return FasterWhisperTranscriber(config)
        elif provider == 'whisper_local':
            return WhisperLocalTranscriber(config)
        else:
            # 默认使用本地Whisper
            return WhisperLocalTranscriber(config)

    @staticmethod
    def create_transcriber(config: Dict[str, Any]) -> VoiceTranscriber:
        """
        创建语音转录服务实例。

        若配置含 ``fallback``（dict 或 dict 列表），构建「主转录器 → 备转录器」级联，
        主机不可达/返空时自动回落（见 FallbackTranscriber）。

        Args:
            config: 语音识别配置

        Returns:
            语音转录服务实例
        """
        primary = VoiceTranscriberFactory._create_one(config)

        fb = config.get('fallback')
        if not fb:
            return primary

        fb_list = fb if isinstance(fb, list) else [fb]
        chain = [primary]
        for fb_cfg in fb_list:
            if not isinstance(fb_cfg, dict):
                continue
            # 备用转录器继承主配置的公共项（temp_dir/max_file_size 等）后再覆盖
            merged = {**config, **fb_cfg}
            merged.pop('fallback', None)
            chain.append(VoiceTranscriberFactory._create_one(merged))
        return FallbackTranscriber(config, chain)


# ── 进程级共享转写器（2026-07-24，供 TTS synth_verify 等旁路消费）─────────────
# 旁路消费方复用主链已加载的模型（SenseVoice ~900MB VRAM），绝不自行再加载一份。
# 首个登记生效（主链转写器最先创建=配置最全）；未登记时消费方必须 fail-open。
_SHARED_TRANSCRIBER: Optional[VoiceTranscriber] = None


def register_shared_transcriber(t: Optional[VoiceTranscriber]) -> None:
    """登记进程级共享转写器（首个生效，重复登记忽略）。"""
    global _SHARED_TRANSCRIBER
    if t is not None and _SHARED_TRANSCRIBER is None:
        _SHARED_TRANSCRIBER = t


def get_shared_transcriber() -> Optional[VoiceTranscriber]:
    """取共享转写器；未登记 → None（消费方自行跳过）。"""
    return _SHARED_TRANSCRIBER


# 简易测试函数
async def test_voice_transcription():
    """测试语音转录服务"""
    print("🔊 语音转录服务测试")
    print("=" * 50)

    # 测试配置
    test_config = {
        'enabled': True,
        'provider': 'whisper_local',
        'temp_dir': './temp/test_voice',
        'whisper': {
            'model_size': 'base',
            'device': 'cpu',
            'download_root': './models/test'
        }
    }

    try:
        # 创建转录服务
        transcriber = VoiceTranscriberFactory.create_transcriber(test_config)

        # 创建测试语音文件（模拟）
        test_file = Path(test_config['temp_dir']) / "test_voice.wav"
        test_file.parent.mkdir(parents=True, exist_ok=True)

        # 写入模拟数据（实际使用时是真实语音文件）
        test_file.write_bytes(b"fake voice data for testing")

        print(f"✅ 转录服务创建成功: {transcriber.__class__.__name__}")
        print(f"✅ 测试文件创建: {test_file}")

        # 尝试转录（会失败，因为不是真实语音文件）
        print("\n⚠️  注意: 测试文件不是真实语音，转录会失败")
        print("   实际使用时需要真实语音文件")

        # 清理
        transcriber.cleanup_temp_files()

        print("\n" + "=" * 50)
        print("🎯 实际使用步骤:")
        print("1. 安装依赖: pip install openai-whisper")
        print("2. 准备真实语音文件")
        print("3. 调用transcribe_voice_message()方法")
        print("4. 处理返回的文本")

    except Exception as e:
        print(f"❌ 测试失败: {e}")

if __name__ == "__main__":
    # 运行测试
    asyncio.run(test_voice_transcription())
