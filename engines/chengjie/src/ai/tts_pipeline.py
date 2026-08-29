"""Text-to-speech pipeline for Messenger voice replies.

The pipeline is deliberately lazy and soft-failing, matching audio_pipeline:
local providers are cheap for testing, online providers are better for voice
quality, and failures return structured errors so Messenger can fall back to
text/approval without blocking the RPA loop.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# emoji / 图形符号（合成前剔除——克隆 TTS(如 IndexTTS2) 遇 emoji 可能停读/截断，且 emoji
# 本就不该被朗读）。覆盖主要 emoji 平面 + 杂项符号 + 区域指示符 + 变体选择符。
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF"
    "]+",
    flags=re.UNICODE,
)


def clean_text_for_tts(text: str) -> str:
    """合成前文本清洗：剔除 emoji/零宽符 + 把换行折成空格（不用逗号）。

    根治「语音念到一半就断」：多行回复(含 emoji/换行)送到 CosyVoice/IndexTTS 时，
    模型常在换行、emoji 或**人工插入的逗号停顿**处早停，产出半截音频。
    2026-07-14 真机：换行折「，」后第二句仍被截在首句（2.85s≈只念前半句）。
    优化：换行 → **空格**（保留语义连续、降低「句末」误判）；原句内标点逗号不动。
    另做疑问语调还原（P0 2026-08-05）：「…吗。」→「…吗？」——LLM 常把疑问句
    写成句号收尾，TTS 按平调陈述念（「昨晚睡得还好吗。」实锤），问号才有疑问语调。
    「吧/呢」语义两可（「走吧。」是祈使），刻意不动。
    """
    try:
        t = str(text or "")
        t = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", t)  # 零宽符
        t = _EMOJI_RE.sub("", t)
        t = re.sub(r"[ \t]*\r?\n+[ \t]*", " ", t)  # 换行 → 空格（避免假逗号触发早停）
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"[，,]{2,}", "，", t)
        t = re.sub(r"\s+([，。！？,.!?])", r"\1", t)
        t = re.sub(r"吗[。.]+", "吗？", t)  # 疑问语调还原（仅「吗」，零误伤面）
        return t.strip().strip("，,").strip()
    except Exception:
        return str(text or "").strip()


def flatten_tts_clauses(text: str) -> str:
    """截断重试专用：把句读停顿压成空格，逼 CosyVoice 一口气念完。

    仅在 ``suspect_tts_truncation`` 命中后的二次合成使用；正常路径仍走
    ``clean_text_for_tts`` 保留自然停顿。
    """
    try:
        t = clean_text_for_tts(text) or str(text or "")
        t = re.sub(r"[，,、；;]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t
    except Exception:
        return str(text or "").strip()


# CosyVoice3 副语言标记——IndexTTS/hub 会当正文念出，hub 路径必须剥掉。
_COSY_PARA_MARK_RE = re.compile(
    r"\[(?:sigh|breath|laughter|laughs|laugh|strong)\]", re.IGNORECASE,
)


def polish_hub_speak_text(text: str) -> str:
    """Hub/IndexTTS 送稿清洗：去 Cosy 专属标记 + 书面标点改口语换气。

    读稿感一半来自「书面句号收束 / 一口气念完长句」。处理：
      - 剥 Cosy ``[sigh]`` 等（IndexTTS 会当正文念出）；
      - 非句末 ``。`` → ``……``；句末播音腔句号去掉；
      - 长句（≥18 字且 ≥2 个逗号）把**第一个** ``，`` 改成 ``……`` 换气
        （确定性；已是省略号则跳过）——逼 IndexTTS 喘一口气，减念稿腔。
    """
    try:
        t = str(text or "")
        t = _COSY_PARA_MARK_RE.sub("", t)
        # 句首连环假笑（哈哈哈/嘿嘿嘿）→ 去掉；开心靠 emotion 标签，不靠念笑字
        t = re.sub(
            r"^\s*[「『\"']?\s*(?:哈{2,}|嘿{2,}|呵{2,}|嘻{2,})[，,！!\s]*",
            "", t)
        t = t.replace("；", "……").replace(";", "……")
        # 句中句号 → 换气省略号（后面还有字才改）
        t = re.sub(r"。(?=\S)", "……", t)
        # 句末播音腔句号/叹号收束去掉（问号保留——真疑问）
        t = re.sub(r"[。！]+$", "", t)
        # 长句首逗号 → 换气（已有 …… 开场则改第二个逗号，防「嗯……」叠加重）
        if len(t) >= 18 and t.count("，") >= 2 and "……" not in t[:12]:
            t = t.replace("，", "……", 1)
        elif len(t) >= 22 and t.count("，") >= 2:
            # 已有开场省略号：改第二个逗号
            first = t.find("，")
            second = t.find("，", first + 1) if first >= 0 else -1
            if second > 0:
                t = t[:second] + "……" + t[second + 1:]
        t = re.sub(r"[…]{4,}", "……", t)  # 省略号过长收成两个
        t = re.sub(r"\s+", " ", t).strip()
        return t
    except Exception:
        return str(text or "").strip()


_NON_CJK_RE = re.compile(r"[^\u4e00-\u9fff]")


def _cer_cjk(hyp: str, ref: str) -> float:
    """CJK-only 字错率 = 编辑距离 / len(ref)。ref 无 CJK → -1.0（不可评）。

    只留汉字再比：标点/省略号/副语言标记（[sigh] 等）/拉丁字符天然剥离，
    专抓「错别字/含混/幻觉插句」这类内容级劣化，不被格式差异干扰。
    """
    h = _NON_CJK_RE.sub("", str(hyp or ""))
    r = _NON_CJK_RE.sub("", str(ref or ""))
    if not r:
        return -1.0
    m, n = len(h), len(r)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1,
                        prev + (0 if h[i - 1] == r[j - 1] else 1))
            prev = cur
    return dp[n] / n


async def verify_and_retry_synth(
    av_out: Path,
    synth_text: str,
    sv_cfg: Optional[Dict[str, Any]],
    transcriber: Any,
    resynth: Any,
    *,
    budget: float = 120.0,
) -> Optional[Dict[str, Any]]:
    """合成后 ASR 回转校验门（synth_verify，2026-07-24）——fail-open。

    零样本克隆单次合成方差大：同参考音同文本 CER 可从 0.08 摆到 0.20+
    （v5 烘焙实测；偶发开头幻觉「好,」/结尾含混「反馈→把盔」）。把合成产物
    转写回来与送稿比 CJK 字错率，超阈值自动重合成取较优——只在坏抽样时
    付第二次 GPU 代价，比无脑 best_of×N 省。

    - ``transcriber``：进程内共享转写器（get_shared_transcriber，SenseVoice
      亚秒级）；未登记/转写失败/超时 → 静默跳过，绝不阻塞发声链。
    - ``resynth``：同参数重合成回调（覆写 av_out）；失败自动回滚上一版字节。
    - 返回观测 dict {cer, retried}（进 rv.extra.synth_verify）；不适用 → None。
    """
    try:
        cfg = sv_cfg if isinstance(sv_cfg, dict) else {}
        if not cfg.get("enabled", False) or transcriber is None:
            return None
        threshold = float(cfg.get("cer_threshold", 0.30) or 0.30)
        min_chars = max(1, int(cfg.get("min_chars", 6) or 6))
        retries = max(0, min(2, int(cfg.get("max_retries", 1) or 0)))
        stt_timeout = float(cfg.get("stt_timeout_sec", 15.0) or 15.0)
        if len(_NON_CJK_RE.sub("", str(synth_text or ""))) < min_chars:
            return None                     # 短句/非中文 → 指标不可靠，不评

        async def _stt() -> Optional[str]:
            try:
                return await asyncio.wait_for(
                    transcriber.transcribe_voice_message(str(av_out), "zh"),
                    timeout=stt_timeout)
            except Exception:
                return None

        hyp = await _stt()
        if hyp is None:
            return None                     # 转写不可用 → fail-open
        best_cer = _cer_cjk(hyp, synth_text)
        if best_cer < 0:
            return None
        attempt = 0
        while best_cer > threshold and attempt < retries:
            attempt += 1
            try:
                best_bytes: Optional[bytes] = av_out.read_bytes()
            except Exception:
                best_bytes = None
            logger.warning(
                "[tts] synth_verify CER=%.3f > %.2f → 重合成(第%d次) text=%s",
                best_cer, threshold, attempt, synth_text[:40])
            try:
                await asyncio.wait_for(asyncio.to_thread(resynth), timeout=budget)
            except Exception:
                if best_bytes is not None:
                    try:
                        av_out.write_bytes(best_bytes)
                    except Exception:
                        pass
                break                       # 重合成失败 → 保留上一版
            hyp2 = await _stt()
            c2 = _cer_cjk(hyp2, synth_text) if hyp2 is not None else -1.0
            if 0 <= c2 < best_cer:
                best_cer = c2               # 新版更好 → 保留新文件
            else:
                if best_bytes is not None:  # 新版更差/不可评 → 回滚
                    try:
                        av_out.write_bytes(best_bytes)
                    except Exception:
                        pass
        return {"cer": round(best_cer, 3), "retried": attempt}
    except Exception:
        return None


def suspect_tts_truncation(
    text: str,
    duration_ms: int,
    *,
    provider: str = "",
) -> bool:
    """启发式判断「多行/长句却只合成了前半截」。

    纯函数门禁：源文本含换行 + 清洗后够长 + 克隆类 provider + 时长明显偏短。
    宁可少报（短句/edge 不误伤）——命中后由上层重试 ``flatten_tts_clauses`` 一次。
    """
    try:
        raw = str(text or "")
        if "\n" not in raw and "\r" not in raw:
            return False
        prov = str(provider or "").strip().lower()
        if prov in ("edge_tts", "openai", "elevenlabs", "pyttsx3", ""):
            return False
        dur = int(duration_ms or 0)
        if dur <= 0:
            return False
        cleaned = clean_text_for_tts(raw) or raw.strip()
        if len(cleaned) < 16:
            return False
        # 克隆中文 ~150–220ms/字；整段应明显长于「只念第一句」
        expected_full = max(3000, len(cleaned) * 165)
        ratio_short = dur < int(expected_full * 0.63)
        # 多行源文本 + 音频 <3.1s 但清洗后 ≥20 字 → 典型「只念前半句」
        absolute_short = dur < 3100 and len(cleaned) >= 20
        return ratio_short or absolute_short
    except Exception:
        return False


# ── 不可达主机短路缓存 ───────────────────────────────────────────────────────
# coqui_http / voice_clone 等局域网主机离线时，若每次合成都做 3s TCP 预检，会白等
# 3s + 刷一条 WARNING。这里缓存「最近探测不可达」的 host:port，冷却期内直接秒回落，
# 不再触网；主机恢复后（冷却到期再探一次成功）自动清缓存复用。进程级共享、线程安全。
_TTS_UNREACHABLE_TTL_SEC = 60.0
_tts_unreachable_lock = threading.Lock()
_tts_unreachable_until: Dict[str, float] = {}  # "host:port" -> monotonic 解禁时刻


# 这些错误源于配置/授权问题（非传输故障），不应被「兜底合成」掩盖——
# 否则会用通用音色悄悄绕过 owner_consent 等门禁，或藏住缺文件/缺命令的配置错误。
_NON_FALLBACK_ERROR_MARKERS = (
    "voice_profile_requires_owner_consent",
    "voice_profile_missing_reference_audio_path",
    "voice_profile_reference_audio_missing",
    "voice_profile_missing_command",
    "backend disabled",      # 用户显式关闭 TTS（backend: disabled）→ 不得兜底
    "unknown backend",       # 配置写错后端名 → 暴露而非掩盖
    "empty_text",
    "pipeline_disabled",
    # ElevenLabs 本地配置错误（缺 key/voice_id）：本地即可判定的 misconfig，
    # 应暴露给运营修正，而非用通用音色静默掩盖（API 侧 401/配额错误仍走兜底出声）。
    "elevenlabs_missing_api_key",
    "elevenlabs_missing_voice_id",
)


def _is_non_fallback_error(err: Optional[str]) -> bool:
    """判断错误是否属于「配置/授权类」——是则不走兜底，直接暴露。"""
    if not err:
        return False
    return any(m in err for m in _NON_FALLBACK_ERROR_MARKERS)


# 坐席可行动的失败分类（2026-08-05 P0，「默认音色不可用」修复配套）：
# 坐席手动链（tts-test 试听 / send-voice 发送）此前把管线错误码原样透出，
# 前端只能显示「生成失败: hub_voice_source_unavailable」——坐席不知道下一步
# 该换音色、该重录、还是该等运维。分类词表与 _NON_FALLBACK_ERROR_MARKERS
# 同址维护（同一批错误码的两种消费口径：要不要兜底 / 怎么向人解释）。
# 「hub 音色源用不了」的机器码族（单一事实源；voice_outage 台账原因、坐席错误分类、
# hub 风险预告共用）。**码分四种、桶只有一个**：hub 挂了 / hub 目录说引擎离线 /
# 产物指纹实锤换了引擎 / 引擎在岗但连续超时（熔断开路）——运维处置完全不同
# （第一个看 /health；中间两个 /health 正绿着、要查引擎目录；最后一个目录与
# /health **双绿**、要去腾显存），必须在原因里分得开；而坐席能做的事一样
# （改用系统通用音色或等运维），所以归同一个可行动桶。
HUB_SOURCE_ERROR_MARKERS = (
    "hub_voice_source_unavailable",
    "hub_engine_offline",
    "hub_engine_mismatch",
    "hub_synth_timing_out",
)
_VOICE_ERR_HUB_MARKERS = HUB_SOURCE_ERROR_MARKERS
_VOICE_ERR_NOT_READY_MARKERS = (
    "voice_profile_requires_owner_consent",
    "voice_profile_missing_reference_audio_path",
    "voice_profile_reference_audio_missing",
)


def classify_voice_error(err: Optional[str]) -> str:
    """把管线错误码归类为坐席可行动的桶（纯函数）。

    返回：``hub_source_down``（音色源 hub 不可用且一致性策略拒绝顶包——
    换「系统通用音色」或其他就绪音色即可发）/ ``profile_not_ready``（该音色
    登记不完整：缺授权确认或参考音——去语音面板重新登记）/ ``""``（其他，
    保持原始错误码透出）。
    """
    e = str(err or "")
    if not e:
        return ""
    if any(m in e for m in _VOICE_ERR_HUB_MARKERS):
        return "hub_source_down"
    if any(m in e for m in _VOICE_ERR_NOT_READY_MARKERS):
        return "profile_not_ready"
    return ""


def hub_strict_scope(avatar_voice_cfg: Any, persona_id: Any) -> bool:
    """该人设的语音是否处于「hub 音色源 + 严格一致性」辖区（纯函数）。

    与 ``_try_hub_fish`` 的门控**同口径**：hub_fish.enabled 且人设命中
    allowlist（空表=全员）且 ``voice_consistency=strict``。命中＝hub 挂时
    该人设的语音会被「宁缺毋滥」硬拒发（hub_voice_source_unavailable）——
    坐席选音色前的风险预告（effective-config 的 hub_risk 字段）据此判定，
    预告与真实拒发行为不一致比没有预告更糟，所以逻辑必须同源镜像。
    """
    cfg = avatar_voice_cfg if isinstance(avatar_voice_cfg, dict) else {}
    hf = cfg.get("hub_fish") if isinstance(cfg.get("hub_fish"), dict) else {}
    if not bool(hf.get("enabled", False)):
        return False
    pid = str(persona_id or "").strip()
    if not pid:
        return False
    allow = hf.get("persona_allowlist") or []
    if allow and pid not in allow:
        return False
    return str(cfg.get("voice_consistency") or "lenient").strip().lower() == "strict"


def probe_hub_reachable(avatar_voice_cfg: Any, *, timeout: float = 1.5) -> bool:
    """hub 网关 TCP 可达性（True=可达/无法判定，False=确定不可达）。

    复用 ``_assert_http_reachable`` 的 60s 不可达负缓存——status 面高频轮询
    不放大探测流量。**单边确定性**：TCP 不通 ⇒ hub 必挂（高置信红）；TCP 通
    不代表音色档存在（404 类由 voice_outage 台账的事后证据补位），拿不准一律
    返 True 不告警（宁可漏报不误报）。
    """
    cfg = avatar_voice_cfg if isinstance(avatar_voice_cfg, dict) else {}
    hf = cfg.get("hub_fish") if isinstance(cfg.get("hub_fish"), dict) else {}
    base = str(hf.get("base_url") or "").strip()
    if not base:
        return True
    try:
        _assert_http_reachable(base, timeout=timeout)
        return True
    except RuntimeError:
        return False
    except Exception:
        return True


def hub_engine_offline(avatar_voice_cfg: Any, *, timeout: float = 2.0) -> bool:
    """点名的 hub 引擎在目录里明确不可用（True=确定离线，False=可用/判不了）。

    2026-08-22「fish 冒充 IndexTTS-2」事故的**前兆信号**：hub 引擎解析是 prefer
    语义，目录里 ``available:false`` 就意味着下一次合成会被静默换成别的引擎。
    与 ``probe_hub_reachable`` 一样是**单边确定性**——只有目录明确否认才返 True，
    目录不可达/引擎未登记/没钉引擎/关了校验一律返 False（宁可漏报不误报）。

    合成前预检（``_try_hub_fish``）与坐席风险预告（effective-config 的 hub_risk）
    共用本函数：预告与真实拒发行为不一致比没有预告更糟。
    """
    cfg = avatar_voice_cfg if isinstance(avatar_voice_cfg, dict) else {}
    hf = cfg.get("hub_fish") if isinstance(cfg.get("hub_fish"), dict) else {}
    if not bool(hf.get("verify_engine", True)):
        return False  # 排障档：既然不拦，就别预告
    try:
        from src.ai.avatar_voice import hub_engine_available
        return hub_engine_available(
            hf.get("base_url"), hf.get("tts_engine"), timeout=timeout) is False
    except Exception:
        return False


def _assert_http_reachable(base_url: str, timeout: float = 3.0) -> None:
    """对 base_url 的 host:port 做一次短超时 TCP 连接预检；不可达则抛异常。

    用于在真正发起（可能 300s 超时的）合成请求前快速判断局域网/云主机是否在线，
    把「主机离线」从 OS 级 ~21s 连接超时（Windows WinError 10060）缩短到 ~3s，
    让上层兜底（edge_tts）几乎即时生效。
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return  # 解析不出主机名就不预检，交给后续请求自然报错
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = f"{host}:{port}"
    now = time.monotonic()
    # 冷却期内：跳过 TCP 探测，直接抛「缓存命中」错（上层据此秒回落且不刷 WARNING）。
    with _tts_unreachable_lock:
        until = _tts_unreachable_until.get(key, 0.0)
    if until and now < until:
        raise RuntimeError(
            f"tts_host_unreachable_cached:{key}:retry_in_{int(until - now)}s")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            with _tts_unreachable_lock:
                _tts_unreachable_until.pop(key, None)  # 探测成功 → 解除短路
            return
    except OSError as exc:
        with _tts_unreachable_lock:
            _tts_unreachable_until[key] = now + _TTS_UNREACHABLE_TTL_SEC
        raise RuntimeError(
            f"tts_host_unreachable:{host}:{port}:{type(exc).__name__}") from exc


# ── TTS 输出缓存（进程级、有界 LRU、线程安全）────────────────────────────────
# 问候 / FAQ / 常用句会被反复合成——同一 (text, voice, backend, format, emotion,
# 参考音频指纹) 命中缓存可直接复用字节，省外部调用与延迟。只缓存**字节**（短语音
# 几 KB~几百 KB），neutral 情绪 == 与升级前完全一致的输出，缓存对行为无副作用。
_TTS_CACHE_LOCK = threading.Lock()
_TTS_CACHE: "OrderedDict[str, Tuple[bytes, str, str, str]]" = OrderedDict()
# value = (audio_bytes, fmt, provider, voice)
_TTS_CACHE_TS: Dict[str, float] = {}  # key -> 写入时刻（供 TTL 过期判定）
_TTS_CACHE_MAX = 128


def _tts_cache_get(
    key: str, *, ttl_sec: float = 0.0,
) -> Optional[Tuple[bytes, str, str, str]]:
    """取缓存字节。``ttl_sec>0`` 时超龄条目视为未命中并顺手清理——防「昨天合成的
    同一句今天/隔久了还复用同一段音频」（复读语音事故的防线之一）。"""
    if not key:
        return None
    with _TTS_CACHE_LOCK:
        item = _TTS_CACHE.get(key)
        if item is None:
            return None
        if ttl_sec and ttl_sec > 0:
            ts = _TTS_CACHE_TS.get(key, 0.0)
            if ts <= 0 or (time.time() - ts) > ttl_sec:
                _TTS_CACHE.pop(key, None)
                _TTS_CACHE_TS.pop(key, None)
                return None
        _TTS_CACHE.move_to_end(key)
        return item


def _tts_cache_put(key: str, value: Tuple[bytes, str, str, str], *, max_entries: int) -> None:
    if not key or not value or not value[0]:
        return
    with _TTS_CACHE_LOCK:
        _TTS_CACHE[key] = value
        _TTS_CACHE_TS[key] = time.time()
        _TTS_CACHE.move_to_end(key)
        cap = max(1, int(max_entries))
        while len(_TTS_CACHE) > cap:
            _old, _ = _TTS_CACHE.popitem(last=False)
            _TTS_CACHE_TS.pop(_old, None)


def _tts_cache_evict(key: str) -> None:
    """移除单条缓存（发现缓存的是截断坏音时驱逐，防坏音被反复复用）。"""
    if not key:
        return
    with _TTS_CACHE_LOCK:
        _TTS_CACHE.pop(key, None)
        _TTS_CACHE_TS.pop(key, None)


def reset_tts_cache() -> None:
    """清空 TTS 输出缓存（测试用 / 音色变更后强制重合成）。"""
    with _TTS_CACHE_LOCK:
        _TTS_CACHE.clear()
        _TTS_CACHE_TS.clear()


# 户外场景关键词（场景文本是英文短语，见 selfie scene_rotation / persona selfie_scenes）
_OUTDOOR_SCENE_RE = re.compile(
    r"beach|park|street|riverside|rooftop|outdoor|garden|lake|mountain|"
    r"night city|walk|seaside|market|trail|harbor|plaza", re.IGNORECASE)


def classify_scene_ambience(scene: str) -> str:
    """场景短语 → 底噪 profile（``outdoor``/``room``）。纯函数，未知/空 → room。"""
    s = str(scene or "")
    if s and _OUTDOOR_SCENE_RE.search(s):
        return "outdoor"
    return "room"


def resolve_ambience_amplitude(
    amb: Optional[Dict[str, Any]], *, profile: str = "room",
    hour: Optional[int] = None,
) -> float:
    """底噪振幅解析（纯函数）：profile 分档 → 深夜衰减 → 限幅。

    ``outdoor_amplitude``（可选）：户外档独立振幅（盲听校准：户外环境本就
    比室内响，同增益听感偏轻）——缺省回落 ``amplitude``。
    """
    a = amb or {}
    base = float(a.get("amplitude", 0.03) or 0.03)
    if str(profile or "").strip().lower() == "outdoor":
        base = float(a.get("outdoor_amplitude", base) or base)
    try:
        from src.ai.voice_emotion import is_quiet_hour
        if is_quiet_hour(hour):
            base *= float(a.get("night_factor", 0.6) or 0.6)
    except Exception:
        pass
    return max(0.001, min(0.12, base))


def build_ambience_cmd(
    src: Any, dst: Any, *, amplitude: float = 0.03,
    lowpass_hz: Optional[int] = None, profile: str = "room",
) -> List[str]:
    """ffmpeg 混底噪命令（纯函数供单测）。``seed=7`` 固定 → 同输入同输出
    （确定性，TTS 缓存安全）；``normalize=0`` 保持人声电平不被 amix 压低。

    配方（两档）：
      - ``room``：brown noise 低通 400Hz＝室内空调/房间闷响（默认）；
      - ``outdoor``：pink noise 低通 1100Hz + 0.3Hz 慢速 tremolo＝户外风声/
        远处车流的起伏感（频带更宽、能量缓慢波动）。
    ``lowpass_hz=None`` → 按 profile 取默认；显式给值则覆盖。
    """
    amp = max(0.001, min(0.12, float(amplitude or 0.03)))
    prof = str(profile or "room").strip().lower()
    if prof == "outdoor":
        lp = max(100, min(4000, int(lowpass_hz or 1100)))
        chain = (f"anoisesrc=colour=pink:amplitude={amp}:seed=7[nz];"
                 f"[nz]lowpass=f={lp},tremolo=f=0.3:d=0.35[nl];"
                 "[0:a][nl]amix=inputs=2:duration=first:normalize=0[out]")
    else:
        lp = max(100, min(2000, int(lowpass_hz or 400)))
        chain = (f"anoisesrc=colour=brown:amplitude={amp}:seed=7[nz];"
                 f"[nz]lowpass=f={lp}[nl];"
                 "[0:a][nl]amix=inputs=2:duration=first:normalize=0[out]")
    return [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex", chain,
        "-map", "[out]",
        str(dst),
    ]


def _reference_fingerprint(voice_profile: Dict[str, Any]) -> str:
    """参考音频指纹（路径+大小+mtime）——换了参考音频则缓存键自动失效。"""
    try:
        ref = str((voice_profile or {}).get("reference_audio_path") or "").strip()
        if not ref:
            return ""
        st = os.stat(ref)
        return f"{ref}:{st.st_size}:{int(st.st_mtime)}"
    except Exception:
        return ""


@dataclass
class TTSResult:
    ok: bool = False
    audio_path: str = ""
    text: str = ""
    provider: str = ""
    voice: str = ""
    format: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # P3-A: 合成完成后测得的音频时长（秒）；-1.0 = 未能测量
    duration_sec: float = -1.0
    # P3-A: 时长测量来源："wave_header" | "mp3_frame" | "ffprobe" | "mutagen" | "unknown"
    duration_source: str = "unknown"


#: edge-tts 合法音色形如 ``zh-CN-XiaoxiaoNeural`` / ``en-US-EmmaMultilingualNeural``
#: （区域段可多级，如 ``zh-CN-liaoning-XiaobeiNeural``）。
_EDGE_VOICE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]+)+$")
_EDGE_VOICE_DEFAULT = "zh-CN-XiaoxiaoNeural"


def safe_edge_voice(voice: Any, fallback: str = _EDGE_VOICE_DEFAULT) -> str:
    """B62（2026-08-23）：edge_tts 音色白形校验——克隆名绝不透传。

    托管机上人设绑着克隆音色（voice=登记名如 ``steven``），克隆引擎不可用
    回落 edge / 档位路由降级 edge 时，把登记名当 edge voice 传下去会
    ``ValueError: Invalid voice 'Steven'`` 裸抛到 UI（``_312`` 实录）。
    形不合法 → 映射到合法通用音色（fallback 自身不合法再落内置缺省），
    纯函数可单测。
    """
    v = str(voice or "").strip()
    if v and v.endswith("Neural") and _EDGE_VOICE_RE.match(v):
        return v
    fb = str(fallback or "").strip()
    if fb and fb.endswith("Neural") and _EDGE_VOICE_RE.match(fb):
        return fb
    return _EDGE_VOICE_DEFAULT


def _with_hosted_voice_endpoint(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """合成时兜底：托管态必须让网关端点出现在克隆候选里（B124，2026-08-28）。

    ``hosted_gateway.ensure_hosted_voice`` 在启动与热重载回放时把
    ``{site}/api/ai/hub`` 注入 ``avatar_voice.base_urls``。但那条注入链前面串了
    五道闸（``_wants_hosted`` / ``enabled`` 或 ``_hosted_auto`` / ``_lan_seed`` /
    未 ``hosted_opt_out`` / ``ai.api_key`` 已是 ``cx.``），任何一道当时没过——或者
    本管线握着注入**之前**解析出来的那份配置——合成就仍然只打 LAN。外网客户的
    ``192.168.0.x:7852`` 不可达 → 连接超时 → ``avatar_clone_unreachable`` → 回落
    ``edge_tts``，用户侧表现就是「克隆登记成功、发出来却是默认音」（0828 钧/skuio
    一夜双复现）。启动日志此时还写着「克隆语音已接入网关兜底」，两边对不上账，
    正是这次要靠诊断包才定位的原因。

    这里按 ``ensure_hosted_voice`` 落下的 env 契约（``AITR_HOSTED_VOICE_*``）再兜
    一次：网关地址已知却不在候选里就补进去，``_FIRST=1``（注入时 LAN 探测不可达）
    时排首位。env 未设＝非托管部署，原样返回，零行为变更。
    """
    hub = str(os.environ.get("AITR_HOSTED_VOICE_BASE_URL") or "").strip().rstrip("/")
    if not hub:
        return cfg
    bases = [
        str(b).strip().rstrip("/")
        for b in (cfg.get("base_urls") or [])
        if str(b).strip()
    ]
    if not bases:
        single = str(cfg.get("base_url") or "").strip().rstrip("/")
        bases = [single] if single else []
    if hub in bases:
        return cfg
    gateway_first = str(os.environ.get("AITR_HOSTED_VOICE_FIRST") or "").strip() == "1"
    merged = dict(cfg)
    merged["base_urls"] = ([hub] + bases) if gateway_first else (bases + [hub])
    merged["base_url"] = merged["base_urls"][0]
    logger.info(
        "[tts] 克隆候选补入托管网关 %s（%s）——启动注入未覆盖本次合成配置",
        hub, "排首位" if gateway_first else "作兜底")
    return merged


class TTSPipeline:
    """Generate speech from text.

    Config:
        enabled: true/false
        backend: edge_tts | pyttsx3 | openai | elevenlabs | voice_clone_command | coqui_http | minicpm_clone | disabled
        voice: provider-specific voice id
        model: online model name, defaults to gpt-4o-mini-tts
        format: mp3 | wav | opus
        out_dir: tmp_voice_replies
        api_key/base_url: online provider credentials
        voice_profile:
          enabled: true
          owner_consent: true
          speaker_id: my_voice
          reference_audio_path: D:/voice/me.wav
          backend: voice_clone_command
          command_args: [python, tools/glm_tts_infer.py, --text, "{text}", --ref, "{reference_audio}", --out, "{out}"]
          command_template: python tools/glm_tts_infer.py --text {text} --ref {reference_audio} --out {out}
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "edge_tts")).strip().lower()
        # 缺省音色＝中文女声（业务主力语言）。历史值 ja-JP-NanamiNeural 是 2026-07-23
        # 「新好友被日语腔轰炸」事故的静默地雷之一：任何一层配置漏填 voice，
        # 全站语音就默默变日语。
        self.voice = str(
            cfg.get("voice")
            or ("zh-CN-XiaoxiaoNeural" if self.backend == "edge_tts" else "alloy")
        ).strip()
        self.model = str(cfg.get("model") or "gpt-4o-mini-tts").strip()
        self.format = str(cfg.get("format") or "mp3").strip().lower()
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_voice_replies"))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.dashscope_api_key = str(cfg.get("dashscope_api_key") or "").strip()
        self.dashscope_region = str(cfg.get("dashscope_region") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.instructions = str(cfg.get("instructions") or "").strip()
        self.voice_profile = (
            cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
        )
        # 局域网克隆主机配置（LAN 优先 → 云端兜底）；由 resolve_voice_cfg 注入
        self.voice_clone_lan = (
            cfg.get("voice_clone_lan")
            if isinstance(cfg.get("voice_clone_lan"), dict)
            else {}
        )
        # MiniCPM-o 情感克隆主机（与 fish_speech 共用 /v1/tts/clone 契约，独立 base_url）；
        # 由 resolve_voice_cfg 注入。作 backend=minicpm_clone 时的远程情感克隆主机（产 WAV）。
        # 慢于实时（~0.5–0.7x）→ 仅用于**异步语音消息**（可等待），不用于实时通话。
        self.minicpm_clone = (
            cfg.get("minicpm_clone")
            if isinstance(cfg.get("minicpm_clone"), dict)
            else {}
        )
        # AvatarHub CosyVoice3 情感克隆（本机 7852，backend=avatar_clone 时消费；
        # 由 resolve_voice_cfg 注入）。在线回复主力：2~4s/句、原生 emotion 标签、
        # register_spk 预热。仅 HTTP 调用，绝不在本进程加载模型（显存纪律）。
        self.avatar_voice = (
            cfg.get("avatar_voice")
            if isinstance(cfg.get("avatar_voice"), dict)
            else {}
        )
        # RVC 变声（.176:6242）：人设 voice_profile.rvc_voice 指定 66 音色之一时，在**克隆
        # 输出 WAV 上再变声**成该音色（如让某人设用丁真/古天乐音色）。仅 WAV 输入生效、
        # best-effort（变声失败保留原合成音，绝不丢声）。令牌走 rvc.svc_token_env（secrets）。
        self.rvc = cfg.get("rvc") if isinstance(cfg.get("rvc"), dict) else {}
        # 人设 id（由 resolve_voice_cfg 注入）：预渲染语音命中层的查找键。
        self.persona_id = str(cfg.get("persona_id") or "").strip()
        # 人设口头禅/说话习惯（quirks）：口语化句首词 + LLM 改写语气提示。
        self.persona_quirks = str(cfg.get("persona_quirks") or "").strip()
        # 会话口味键（voice_opener_guard，P0-2 2026-08-03）：同一会话跨消息的
        # 开场词去重。由 persona_voice.resolve_effective_voice_context 统一注入
        # （platform:account:chat）；预渲染/试听等无会话上下文的调用天然为空=不去重。
        self.variety_key = str(cfg.get("variety_key") or "").strip()
        # ── 后端不可达/失败时的兜底合成 ──────────────────────────────────────
        # 主后端（如 coqui_http / voice_clone_command 指向的局域网/云主机）连不上时，
        # 回落到免额外基建的在线 edge_tts，避免「生成失败 + WinError 10060」直接抛给用户。
        # 兜底会丢掉克隆音色（换成通用音色），但「有声音」远胜「硬失败」。
        self.fallback_on_error = bool(cfg.get("fallback_on_error", True))
        self.fallback_backend = str(cfg.get("fallback_backend") or "edge_tts").strip().lower()
        self.fallback_voice = str(cfg.get("fallback_voice") or "zh-CN-XiaoxiaoNeural").strip()
        # ── P0：TTS 输出缓存（默认开；neutral 输出与升级前一致，缓存无行为副作用）──
        cache_cfg = cfg.get("tts_cache") if isinstance(cfg.get("tts_cache"), dict) else {}
        self.cache_enabled = bool(cache_cfg.get("enabled", True))
        self.cache_max_entries = int(cache_cfg.get("max_entries", _TTS_CACHE_MAX) or _TTS_CACHE_MAX)
        # TTL（秒）：0=不过期（旧行为）。>0 时超龄缓存视为未命中并重合成——防复读语音
        # 「隔久了还复用同一段音频」。会话语音（autosend）可按需传短 TTL / 关缓存。
        try:
            self.cache_ttl_sec = float(cache_cfg.get("ttl_sec", 0) or 0)
        except (TypeError, ValueError):
            self.cache_ttl_sec = 0.0
        # ── P1：情感层（默认关 → 不传 emotion 即 neutral，零行为变更）──
        emo_cfg = cfg.get("emotion") if isinstance(cfg.get("emotion"), dict) else {}
        self.emotion_enabled = bool(emo_cfg.get("enabled", False))
        self.emotion_default = str(emo_cfg.get("default") or "warm").strip().lower()
        # ── P2-Cloud：ElevenLabs v3 付费情感旗舰档配置 ──
        self.elevenlabs = (
            cfg.get("elevenlabs") if isinstance(cfg.get("elevenlabs"), dict) else {}
        )
        # ── P3：可观测（provider_stats "tts" namespace）+ 成本费率 ──
        self.metrics_enabled = bool(cfg.get("metrics_enabled", True))
        self.cost_rates = (
            cfg.get("cost_per_1k_chars")
            if isinstance(cfg.get("cost_per_1k_chars"), dict) else {}
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "voice": self.voice,
            "model": self.model,
            "format": self.format,
            "out_dir": str(self.out_dir),
            "voice_profile_enabled": bool(self.voice_profile.get("enabled", False)),
            "voice_profile_speaker": str(self.voice_profile.get("speaker_id") or ""),
        }

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        timeout_sec: float = 30.0,
        emotion: Any = None,
        colloquial_lead: bool = True,
        pre_colloquialized: bool = False,
        skip_llm_colloquial: bool = False,
        split_part: bool = False,
        interactive: bool = False,
        total_budget_sec: Optional[float] = None,
    ) -> TTSResult:
        """合成语音。``emotion`` 可为 None / 情绪字符串 / dict / EmotionSpec。

        ``interactive``（2026-08-10 坐席手动链提速）：有人正盯着等这次合成
        （收件箱直发语音）→ hub 候选数封顶（缺省 1，可配 hub_fish.
        best_of_interactive）——synth_verify 已兜坏 take，第二候选对交互路径
        是 ~1×hub 往返的纯延迟税。不进缓存键（候选数不改变音频身份）。

        - 不传 ``emotion`` 且未开 ``emotion.enabled`` → neutral（与升级前完全一致）。
        - 命中 TTS 缓存（同 text+voice+backend+format+情绪+参考音频指纹）→ 直接复用字节。
        - ``colloquial_lead``：是否允许口语化改写加「句首迟疑词」。分条发送时只对首条
          传 True，后续条 False——避免连发 2-3 条都以「其实，/话说，」开头的做作。
        - ``pre_colloquialized``：文本已是生成层口语版（Phase G）→ 跳过 TTS 前
          口语化改写（防二次改写叠加/白烧一次本地 LLM），副语言标记照常注入。
        - ``skip_llm_colloquial``：调用方已用 ``prepass_colloquial_llm`` 对**整段**
          做过一次 LLM 口语化（分条场景省 N-1 次往返）→ 本条只跑免费的规则档/微特征，
          不再打 LLM。与 ``pre_colloquialized`` 的区别：后者整段跳过改写链。
        - ``split_part``（2026-08-01 GPU 减负）：本条是分条发送的其中一条 →
          hub_fish 按 ``best_of_parts``（缺省沿用 best_of）取候选数。分条 2-3 条
          × best_of=2 是 hub GPU 的主要放大器，而 synth_verify（CER 回验重合成）
          已兜坏 take，分条降为 1 候选把 hub 压力砍半、直播共卡更稳。
        - ``total_budget_sec``（2026-07-28 试听超时复盘）：**调用方 opt-in 的全链总预算**。
          交互式端点（tts-test / voice preview）外面套 ``wait_for``，而链内各级预算之和
          （LLM 口语化 25s + hub 45+15s + 本机克隆 90s×2…）远超外闸 → 外层 TimeoutError
          把协程掐死，str() 为空、不知道卡在哪级。传入后各级 wait_for 按剩余预算收口：
          要么在预算内出货，要么带着**具体卡住的级**诚实失败。缺省 None＝生产发送链
          旧行为完全不变（B 线长文本慢 GPU 需要超预算跑完，见 6af80c3）。
        """
        from src.ai.voice_emotion import NEUTRAL, coerce_emotion, derive_emotion

        # 合成前清洗：剔除 emoji + 换行折成停顿，防克隆 TTS 在换行/emoji 处截断音频
        # （「语音念一半就断」的根因）。
        # 2026-07-15 收紧：清洗后为空 = 全 emoji/符号（如尾条只剩"💭 ✨"）——旧行为
        # 回落原文会把 emoji 硬送克隆 TTS 合成出 ~1s 杂音（当日 4 连发事故中两条
        # "无意义语音"的本体）。emoji 本就不该被朗读 → 直接判失败，调用方回落文字。
        _cleaned = clean_text_for_tts(text)
        if not _cleaned and str(text or "").strip():
            return TTSResult(
                ok=False, text=str(text or ""),
                provider=self._effective_backend(),
                voice=voice or self._effective_voice(),
                format=self.format, error="no_speakable_text",
            )
        text_s = _cleaned if _cleaned else str(text or "")
        if emotion is not None:
            spec = coerce_emotion(emotion)
        elif self.emotion_enabled:
            spec = derive_emotion(text=text_s, default=self.emotion_default)
        else:
            spec = NEUTRAL

        # ── 预渲染命中层（AvatarHub Phase 2）：固定台词直接复用夜间预合成的
        # OGG 语音条——零 GPU、零延迟、音色最像（7858 离线档质量 > 7852 在线档）。
        # 排在内存缓存之前：内存缓存可能存的是 edge 兜底的通用音色，预渲染更优。
        pre_rv = self._try_prerendered(text_s)
        if pre_rv is not None:
            return pre_rv

        # ── P0：缓存查找（命中即秒回，省外部调用）──
        if self.enabled and self.cache_enabled and text_s.strip():
            eff_backend = self._effective_backend()
            eff_voice = voice or self._effective_voice()
            # 改写变体维度（2026-08-10）：原文直念（pre_colloquialized）与改写链
            # （口语化可改词）产出的不是同一份音频——不分键会让手动「原文直念」
            # 命中自动链缓存的「改过词」音频，改词穿帮借尸还魂。
            cache_key = self._cache_key(
                text_s, eff_voice, eff_backend, spec,
                variant=("verbatim" if pre_colloquialized else ""))
            hit = _tts_cache_get(cache_key, ttl_sec=self.cache_ttl_sec)
            if hit is not None:
                cached = self._result_from_cache(hit, text_s)
                if cached is not None:
                    # 缓存卫生：修复前入缓存的截断坏音（时长低于文本物理最快
                    # 语速）→ 驱逐并当未命中重合成，防坏音被无限复用。
                    if self._looks_truncated(text_s, cached.duration_sec):
                        _tts_cache_evict(cache_key)
                        try:
                            Path(cached.audio_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        logger.warning(
                            "[tts] 缓存命中疑似截断音（%.1fs / %d字）→ 驱逐重合成",
                            max(cached.duration_sec, 0.0), len(text_s))
                    else:
                        self._record_stats(
                            cached, text_s, cache_hit=True, spec=spec)
                        return cached
        else:
            cache_key = ""

        # ── P0-4 字符额度闸门：额度用尽且 licensing.enforce 开 → 阻断本次合成 ──
        # （缓存命中不扣：无新增合成成本）。返回稳定错误码，autosend/路由按 TTS 失败
        # 回落文字/出 i18n 提示，绝不阻断消息投递。闸门自身异常 → 放行。
        if self.enabled and text_s.strip():
            try:
                from src.licensing.quota_store import (
                    QUOTA_EXCEEDED_ERROR,
                    check_license_quota,
                )

                if not check_license_quota()["allowed"]:
                    return TTSResult(
                        ok=False, text=text_s,
                        provider=self._effective_backend(),
                        voice=voice or self._effective_voice(),
                        format=self.format, error=QUOTA_EXCEEDED_ERROR,
                    )
            except Exception:
                pass

        rv = await self._synthesize_uncached(
            text_s, voice=voice, timeout_sec=timeout_sec, spec=spec,
            colloquial_lead=colloquial_lead,
            pre_colloquialized=pre_colloquialized,
            skip_llm_colloquial=skip_llm_colloquial,
            split_part=split_part,
            interactive=interactive,
            total_budget_sec=total_budget_sec)

        # ── RVC 变声（可选）：把克隆输出 WAV 再变成人设选定的 66 音色之一 ──
        rv = await self._maybe_apply_rvc(rv)

        # ── 环境底噪（可选，⑤ 活人感）：极低增益房间底噪，去「录音棚干净感」──
        rv = await self._maybe_apply_ambience(rv)

        # 截断嫌疑标记（不改 ok——判定保守但不武断；发送层闸门按配置决定拦不拦）
        if rv.ok and self._looks_truncated(text_s, rv.duration_sec):
            rv.extra["suspect_truncated"] = True

        # ── 成功且非缓存命中 → 写入缓存（截断嫌疑音不入缓存，防坏音被复用）──
        if (self.cache_enabled and cache_key and rv.ok and rv.audio_path
                and not rv.extra.get("cache_hit")
                and not rv.extra.get("suspect_truncated")):
            try:
                data = Path(rv.audio_path).read_bytes()
                if data:
                    _tts_cache_put(
                        cache_key, (data, rv.format, rv.provider, rv.voice),
                        max_entries=self.cache_max_entries)
            except Exception:
                pass
        # P0-4：成功合成后按文本字符记额度（无额度授权零开销；绝不抛）
        if rv.ok:
            try:
                from src.licensing.quota_store import record_license_chars

                record_license_chars("tts", len(text_s))
            except Exception:
                pass
            # 2026-08-19 Token 计量（P5a 观测接线，licensing.token_ledger.enabled
            # 默认关=零行为）：只对**克隆声引擎**计 voice_clone（10 Token/100 字符）；
            # 预渲染命中不进本路径、edge 等兜底声=免费路径不计——兑现对外承诺
            # 「Token 用尽自动降级，降级路径免费」。
            try:
                prov = str(rv.provider or "")
                if prov in ("avatar_clone", "minicpm_clone") or prov.endswith("_clone"):
                    from src.licensing.token_ledger import record_action_for_status

                    record_action_for_status("voice_clone", len(text_s))
            except Exception:
                pass
        self._record_stats(rv, text_s, cache_hit=False, spec=spec)
        return rv

    @staticmethod
    def _looks_truncated(text: str, duration_sec: float) -> bool:
        """截断坏音判定（缓存卫生用，保守阈值零误杀；判定失败视为正常）。"""
        try:
            from src.ai.tts_quality import looks_truncated
            bad, _ = looks_truncated(text, duration_sec)
            return bad
        except Exception:
            return False

    def _try_prerendered(self, text: str) -> Optional["TTSResult"]:
        """预渲染语音命中层：命中返回 TTSResult（provider=prerendered），未命中 None。

        条件：pipeline 启用 + avatar_voice.enabled + prerender.enabled（默认开）
        + 有 persona_id。文件复制成一次性副本（调用方发送后 unlink，原件保住）。
        任何异常 → None（回落正常合成，绝不阻塞）。
        """
        try:
            if not (self.enabled and self.persona_id and str(text or "").strip()):
                return None
            av = self.avatar_voice or {}
            if not av.get("enabled"):
                return None
            pre_cfg = av.get("prerender") if isinstance(av.get("prerender"), dict) else {}
            if not pre_cfg.get("enabled", True):
                return None
            from src.ai.voice_prerender import (
                copy_for_send,
                find_prerendered,
                normalize_prerender_text,
            )
            # 当前参考音路径 → 备货指纹比对（人设换声后旧备货拒绝命中，防发错声）
            ref_now = str((self.voice_profile or {}).get(
                "reference_audio_path") or "").strip()
            hit = find_prerendered(
                self.persona_id, text,
                base_dir=str(pre_cfg.get("base_dir") or "assets/voices"),
                ref_path=ref_now)
            if hit is None:
                # 备货缺口观测：短句（预渲染候选体裁）未命中 → 记 Top-N 供运营补台词库。
                # 长句是动态对话内容，不算缺口。阈值 16 字＝固定台词体裁上限
                # （台词库最长「你好呀，今天想我了吗？」11 字；再长基本是动态句）。
                try:
                    norm = normalize_prerender_text(text)
                    max_chars = int(pre_cfg.get("miss_track_max_chars", 16) or 0)
                    if norm and max_chars > 0 and len(norm) <= max_chars:
                        from src.ai.avatar_voice_stats import get_avatar_voice_stats
                        get_avatar_voice_stats().record_prerender_miss(
                            norm, persona_id=self.persona_id)
                except Exception:
                    pass
                return None
            self.out_dir.mkdir(parents=True, exist_ok=True)
            sent_copy = copy_for_send(hit, self.out_dir)
            rv = TTSResult(
                ok=True, text=str(text or ""), provider="prerendered",
                voice=self.persona_id, format="ogg", audio_path=str(sent_copy))
            rv.extra["bytes"] = sent_copy.stat().st_size
            rv.extra["prerendered_from"] = str(hit)
            try:
                from src.client.voice_sender import probe_audio_duration_ms
                ms = probe_audio_duration_ms(str(sent_copy))
                if ms and ms > 0:
                    rv.duration_sec = ms / 1000.0
                    rv.duration_source = "ffprobe"
            except Exception:
                pass
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                get_avatar_voice_stats().record_prerender_hit()
            except Exception:
                pass
            logger.info("[tts] 预渲染命中 persona=%s → %s（零合成）",
                        self.persona_id, hit.name)
            return rv
        except Exception:
            return None

    def _record_stats(self, rv: "TTSResult", text: str, *, cache_hit: bool,
                      spec: Any = None) -> None:
        """记 TTS 用量到 provider_stats "tts" namespace（成功/失败/成本/缓存命中/情绪分布）。绝不抛。"""
        if not self.metrics_enabled:
            return
        try:
            from src.ai.provider_stats import get_provider_stats
            from src.ai.tts_cost_store import record_tts_cost
            stats = get_provider_stats("tts", "tts")
            # 情绪分布（非中性才记，避免 neutral 淹没分布）——反映实际投递的情感面貌。
            if spec is not None and not spec.is_neutral():
                stats.record_label(spec.emotion)
            if cache_hit:
                stats.record_cache_hit()
                record_tts_cost("", cache_hit=True)   # 旁路落库（默认关时 no-op）
                return
            # 仅在该轮真正发生过合成（含失败）时记一次
            if not rv.text.strip():
                return
            provider = rv.provider or self._effective_backend()
            if rv.ok:
                from src.ai.voice_routing import estimate_tts_cost
                cost = estimate_tts_cost(provider, len(text or ""), self.cost_rates)
                stats.record(provider, ok=True, latency_ms=rv.latency_ms, cost_usd=cost)
                record_tts_cost(provider, ok=True, cost_usd=cost)
                if rv.extra.get("fallback_from"):
                    stats.record_fallback()
            elif rv.error not in ("pipeline_disabled", "empty_text"):
                stats.record(provider, ok=False, latency_ms=rv.latency_ms)
                record_tts_cost(provider, ok=False)
        except Exception:
            pass

    def _cache_key(self, text: str, voice: str, backend: str, spec: Any,
                   *, hour: Optional[int] = None, variant: str = "") -> str:
        """TTS 缓存键：克隆类后端额外并入参考音频指纹（换音频自动失效）。

        另并入：① 深夜桶（夜间语速 ×0.96 与白天是两份音频，防深夜命中白天缓存）；
        ② 情绪分库参考音键（同文本不同情绪 ref 必须分缓存，防串「说话状态」）；
        ③ ``variant``＝改写变体（"verbatim"=原文直念 vs ""=可经口语化改词）——
        两者同文本不是同一份音频，不分键会跨链串播（2026-08-10）。
        """
        ref_fp = ""
        emo_ref = ""
        hub_fp = ""
        if backend in ("voice_clone_lan", "voice_clone_command", "coqui_http",
                       "minicpm_clone", "avatar_clone"):
            ref_fp = _reference_fingerprint(self.voice_profile)
            # hub Fish 高保真优先时音频来源不同（.176 Fish vs 本机 CosyVoice3）→ 并入
            # 键，使 toggle hub_fish / 改 base_url / 改 response_format 自动失效缓存，
            # 防串用旧来源/旧格式音频（缓存 replay 按 rv.format 复原后缀）。
            _hf = (self.avatar_voice or {}).get("hub_fish")
            if isinstance(_hf, dict) and _hf.get("enabled"):
                # 并入解析后的 hub 档名 + 引擎钉：改 profile_map / tts_engine 必须失效
                # 旧缓存，否则换绑人设后仍会复播上一档音色。
                _pmap = (_hf.get("profile_map")
                         if isinstance(_hf.get("profile_map"), dict) else {})
                _pid = str(self.persona_id or "").strip()
                _hprof = str(_pmap.get(_pid) or _pid).strip()
                _heng = str(_hf.get("tts_engine") or "").strip()
                hub_fp = ("hub:" + str(_hf.get("base_url") or "") + ":"
                          + (str(_hf.get("response_format") or "wav").strip().lower() or "wav")
                          + ":" + _hprof + ":" + _heng)
            try:
                from src.ai.voice_emotion import pick_emotion_reference
                _thr = float((self.avatar_voice or {}).get(
                    "emotion_channel_threshold", 0.7) or 0.7)
                _er = pick_emotion_reference(
                    self.voice_profile, spec, threshold=_thr)
                if _er:
                    _p = Path(_er[0])
                    emo_ref = f"{_er[1]}:{_p.stat().st_size}" if _p.is_file() else _er[1]
            except Exception:
                emo_ref = ""
        emo = spec.cache_key() if spec is not None else ""
        try:
            from src.ai.voice_emotion import is_quiet_hour
            _h = datetime.datetime.now().hour if hour is None else hour
            night = "n1" if is_quiet_hour(_h) else "n0"
        except Exception:
            night = "n0"
        # RVC 目标音色并入键：同文本不同 rvc_voice 必须分缓存（否则串音）。
        rvc_v = self._rvc_target_voice()
        base = "|".join([
            backend, voice or "", self.format, self.model or "",
            self.instructions or "", emo, ref_fp, emo_ref, night, rvc_v,
            hub_fp, str(variant or ""), text,
        ])
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    async def _maybe_apply_ambience(self, rv: "TTSResult") -> "TTSResult":
        """给克隆 WAV 混一层**生成式**房间底噪（⑤ 活人感，best-effort）。

        「录音棚般干净」本身就是 AI 味——真人语音条永远带一点环境声。用 ffmpeg
        ``anoisesrc``（固定 seed=确定性）极低增益混入，无需底噪素材文件。
        深夜时段增益再降（夜里更静）。仅 WAV（克隆主链）；任何失败保留原音。

        ``ambience.profile``：``room``（室内闷响，默认）/ ``outdoor``（户外风声
        起伏）/ ``auto``＝按场景单一事实源（Phase18 resolve_current_scene，
        与聊天 prompt/生图同源）分类——文本说在海边、照片是海边、语音背景也
        是户外感，三通道一致。
        """
        amb = ((self.avatar_voice or {}).get("ambience")
               if isinstance((self.avatar_voice or {}).get("ambience"), dict)
               else {})
        if not (amb.get("enabled") and rv.ok and rv.audio_path):
            return rv
        src = Path(rv.audio_path)
        if src.suffix.lower() != ".wav" or not src.is_file():
            return rv
        try:
            profile = str(amb.get("profile", "room") or "room").strip().lower()
            if profile == "auto":
                profile = self._ambience_profile_from_scene()
            amplitude = resolve_ambience_amplitude(
                amb, profile=profile, hour=datetime.datetime.now().hour)
            _lp = amb.get("lowpass_hz")
            dst = src.with_name(src.stem + "_amb.wav")
            cmd = build_ambience_cmd(
                src, dst, amplitude=amplitude,
                lowpass_hz=int(_lp) if _lp else None, profile=profile)

            def _run() -> bool:
                r = subprocess.run(cmd, capture_output=True, timeout=30)
                return r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0

            ok = await asyncio.to_thread(_run)
            if ok:
                os.replace(dst, src)          # 覆盖原路径（调用方路径不变）
                rv.extra["ambience"] = round(amplitude, 4)
                rv.extra["ambience_profile"] = profile
            else:
                dst.unlink(missing_ok=True)
        except Exception:
            logger.debug("[tts] ambience 混入失败（保留原音）", exc_info=True)
        return rv

    def _ambience_profile_from_scene(self) -> str:
        """auto 档：按人设「此刻场景」（Phase18 单一事实源）选底噪 profile。

        best-effort：persona 无场景配置/解析失败 → room（保守）。深夜恒 room
        （深夜场景轮换本就偏室内，且夜里户外风声反而突兀）。
        """
        try:
            from src.ai.voice_emotion import is_quiet_hour
            if is_quiet_hour(datetime.datetime.now().hour):
                return "room"
            if not self.persona_id:
                return "room"
            from src.ai.companion_selfie import resolve_current_scene
            from src.utils.persona_manager import PersonaManager
            persona = PersonaManager.get_instance().get_persona_by_id(
                str(self.persona_id))
            if not isinstance(persona, dict):
                return "room"
            scene = resolve_current_scene(persona, {})
            return classify_scene_ambience(scene)
        except Exception:
            return "room"

    def _rvc_target_voice(self) -> str:
        """人设选定的 RVC 目标音色名（66 音色之一）；未配 / rvc 未启用 → ""。"""
        if not (self.rvc and self.rvc.get("enabled")):
            return ""
        return str((self.voice_profile or {}).get("rvc_voice") or "").strip()

    async def _maybe_apply_rvc(self, rv: "TTSResult") -> "TTSResult":
        """成功合成的 WAV 上再跑 RVC 变声成人设选定音色（best-effort，失败保留原音）。

        仅在 rv.ok + WAV + 人设配了 ``voice_profile.rvc_voice`` + ``rvc.enabled`` 时生效。
        RVC 只吃 WAV（edge 兜底出 mp3 不变声）。变声后覆盖同路径文件；异常/空 → 原音不动。
        """
        try:
            rvc_voice = self._rvc_target_voice()
            if not (rvc_voice and rv.ok and rv.audio_path):
                return rv
            if str(rv.format or "").lower() != "wav":
                return rv
            from src.ai.rvc_client import RvcClient
            client = RvcClient(self.rvc)
            src = Path(rv.audio_path)

            def _do() -> bool:
                data = src.read_bytes()
                out = client.convert(data, rvc_voice)
                if out:
                    src.write_bytes(out)
                    return True
                return False

            ok = await asyncio.wait_for(
                asyncio.to_thread(_do),
                timeout=float(self.rvc.get("timeout_sec") or 90))
            if ok:
                rv.extra["rvc_voice"] = rvc_voice
                rv.provider = f"{rv.provider}+rvc:{rvc_voice}"
                rv.extra["bytes"] = src.stat().st_size
        except Exception as ex:
            logger.warning("[tts] RVC 变声失败（保留原合成音）: %s", ex)
        return rv

    def _result_from_cache(
        self, hit: Tuple[bytes, str, str, str], text: str,
    ) -> Optional["TTSResult"]:
        """把缓存字节落盘成新文件并构造 TTSResult。失败返回 None（回落正常合成）。"""
        data, fmt, provider, voice = hit
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            suffix = fmt or self.format
            out = self.out_dir / (
                f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{suffix}")
            out.write_bytes(data)
        except Exception:
            return None
        rv = TTSResult(
            ok=True, text=text, provider=provider, voice=voice,
            format=fmt, audio_path=str(out))
        rv.extra["bytes"] = len(data)
        rv.extra["cache_hit"] = True
        try:
            dur, src = compute_audio_duration_sec(str(out), fmt)
            rv.duration_sec = float(dur)
            rv.duration_source = str(src)
        except Exception:
            rv.duration_sec = -1.0
            rv.duration_source = "unknown"
        return rv

    async def _synthesize_uncached(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        timeout_sec: float = 30.0,
        spec: Any = None,
        colloquial_lead: bool = True,
        pre_colloquialized: bool = False,
        skip_llm_colloquial: bool = False,
        split_part: bool = False,
        interactive: bool = False,
        total_budget_sec: Optional[float] = None,
    ) -> TTSResult:
        rv = TTSResult(
            text=str(text or ""),
            provider=self._effective_backend(),
            voice=voice or self._effective_voice(),
            format=self.format,
        )
        if not self.enabled:
            rv.error = "pipeline_disabled"
            return rv
        if not rv.text.strip():
            rv.error = "empty_text"
            return rv
        self.out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "wav" if self.backend == "pyttsx3" else self.format
        out = self.out_dir / f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{suffix}"
        t0 = time.monotonic()
        # 全链截止线（调用方 opt-in）：各级 wait_for 按剩余预算收口，见 synthesize docstring。
        deadline = (t0 + float(total_budget_sec)
                    if total_budget_sec and total_budget_sec > 0 else None)

        def _cap(t: float, *, floor: float = 6.0) -> float:
            """把某级超时压进剩余预算（floor 保底给该级一个起码的尝试窗口）。"""
            if deadline is None:
                return t
            return min(t, max(floor, deadline - time.monotonic()))
        # ── 局域网克隆优先：在线则走 LAN 零样本克隆；不可用/失败按配置回落云端 ──
        if self._should_try_lan():
            lan_rv = await self._try_lan_clone(rv, out, t0, spec=spec)
            if lan_rv is not None:
                return lan_rv  # LAN 成功 或 硬失败(未开兜底)；None = 回落云端

        # ── 主后端合成 ──
        primary_backend = self._effective_backend()
        # 2026-08-19 Token enforce（P6）：钱包耗尽 + enforce 开 + 主后端是克隆声
        # （计费引擎）→ 注入合成失败原因，交给下方**既有** edge 兜底块出免费兜底声
        # （voice/format 语义全复用，语音永不哑）；兜底不可用则照走克隆（永不断线 > 计费）。
        # 兜底声 provider=edge → 计费钩天然不记（克隆引擎集合外）。
        _token_skip_clone = False
        if (primary_backend in ("avatar_clone", "minicpm_clone")
                or primary_backend.endswith("_clone")):
            try:
                from src.licensing.token_ledger import should_degrade_action

                _token_skip_clone = bool(
                    self.fallback_on_error and self.fallback_backend
                    and self.fallback_backend != primary_backend
                    and should_degrade_action("voice_clone"))
            except Exception:
                _token_skip_clone = False
        # minicpm_clone：与 fish 同 /v1/tts/clone 契约的远程情感克隆主机（产 WAV），作为
        # 可显式选择的克隆后端（异步语音消息专用，慢于实时但不阻塞）。成功直接定稿；
        # 失败且允许兜底 → 落到下方 edge 回落（绝不卡死出站）。
        if _token_skip_clone:
            err = "token_wallet_exhausted"
            logger.info(
                "[tts] Token 钱包耗尽（enforce）→ 跳过克隆声 '%s'，走 '%s' 兜底",
                primary_backend, self.fallback_backend)
        elif primary_backend == "avatar_clone":
            # AvatarHub CosyVoice3（本机 7852）：情感克隆在线主力（2~4s/句）。
            av_rv = await self._try_avatar_clone(
                rv, out, t0, spec=spec, colloquial_lead=colloquial_lead,
                pre_colloquialized=pre_colloquialized,
                skip_llm_colloquial=skip_llm_colloquial,
                split_part=split_part,
                interactive=interactive,
                deadline=deadline)
            if av_rv is not None:
                return av_rv
            err = "avatar_clone_unreachable"
        elif primary_backend == "minicpm_clone":
            mc_rv = await self._try_minicpm_clone(rv, out, t0, spec=spec)
            if mc_rv is not None:
                return mc_rv
            err = "minicpm_clone_unreachable"
        else:
            err = await self._run_backend(
                rv, rv.text, out, rv.voice, primary_backend, rv.format,
                _cap(timeout_sec),
                spec=spec)
            if err is None:
                rv.latency_ms = int((time.monotonic() - t0) * 1000)
                return rv

        # ── 主后端失败 → 回落到免基建的在线 edge_tts（避免硬失败 / WinError 10060）──
        # 仅对「传输/运行时」失败兜底；配置/授权类错误（缺同意、缺参考音频等）应直接
        # 暴露给用户，不能用通用音色悄悄掩盖。
        fb = self.fallback_backend
        if (self.fallback_on_error and fb and fb != primary_backend
                and not _is_non_fallback_error(err)):
            # 冷却期内的「缓存命中不可达」是已知稳态 → DEBUG，避免主机长时间离线时
            # 每次语音合成都刷 WARNING；首次探测失败（刚写入缓存）仍按 WARNING 记。
            _cached_dead = "tts_host_unreachable_cached:" in (err or "")
            (logger.debug if _cached_dead else logger.warning)(
                "[tts] backend '%s' failed (%s) → 回落 '%s'", primary_backend, err, fb)
            fb_fmt = "mp3" if fb == "edge_tts" else self.format
            fb_out = out.with_suffix(f".{fb_fmt}")
            fb_err = await self._run_backend(
                rv, rv.text, fb_out, self.fallback_voice, fb, fb_fmt,
                _cap(timeout_sec),
                spec=spec)
            if fb_err is None:
                rv.provider = fb
                rv.format = fb_fmt
                rv.voice = (safe_edge_voice(self.fallback_voice)
                            if fb == "edge_tts" else self.fallback_voice)
                rv.extra["fallback_from"] = primary_backend
                rv.extra["primary_error"] = err
                rv.latency_ms = int((time.monotonic() - t0) * 1000)
                return rv
            err = f"{err} | fallback({fb}):{fb_err}"

        rv.error = err
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    async def _run_backend(
        self,
        rv: "TTSResult",
        text: str,
        out: Path,
        voice: str,
        backend: str,
        fmt: str,
        timeout_sec: float,
        *,
        spec: Any = None,
    ) -> Optional[str]:
        """用指定 backend 合成到 out。成功 → 写回 rv 并返回 None；失败 → 返回错误串。"""
        if backend == "edge_tts":
            # B62：克隆名/任意非法音色绝不透传给 edge（ValueError 裸抛 UI 的根子）；
            # 映射记进 extra，预览层据此明示「本条用通用音色」。
            _eff = str(voice or self.voice or "").strip()
            voice = safe_edge_voice(_eff, self.fallback_voice)
            if _eff and voice != _eff:
                rv.extra["voice_mapped_from"] = _eff
                rv.voice = voice
                logger.warning(
                    "[tts] edge_tts 音色 '%s' 不合法 → 映射通用音色 '%s'"
                    "（克隆名不透传）", _eff, voice)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._synthesize_sync, text, out, voice, backend, spec),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return f"tts_timeout({timeout_sec:.0f}s)"
        except Exception as ex:
            return f"{type(ex).__name__}: {ex}"
        if not (out.exists() and out.stat().st_size > 0):
            return "empty_audio"
        rv.ok = True
        rv.audio_path = str(out)
        rv.extra["bytes"] = out.stat().st_size
        # ── P3-A：合成后立即测时长，带上 duration_source 供上游审计 ──
        try:
            dur, src = compute_audio_duration_sec(str(out), fmt)
            rv.duration_sec = float(dur)
            rv.duration_source = str(src)
        except Exception:
            rv.duration_sec = -1.0
            rv.duration_source = "unknown"
        return None

    def _effective_backend(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("backend") or self.backend).strip().lower()
        return self.backend

    def _effective_voice(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("speaker_id") or self.voice).strip()
        return self.voice

    def _should_try_lan(self) -> bool:
        """是否应尝试局域网克隆：LAN 启用 + 已请求克隆(有同意+参考音频文件)。"""
        lan = self.voice_clone_lan or {}
        if not lan.get("enabled"):
            return False
        vp = self.voice_profile or {}
        if not (vp.get("enabled") and vp.get("owner_consent")):
            return False
        ref = str(vp.get("reference_audio_path") or "").strip()
        return bool(ref) and Path(ref).is_file()

    async def _try_lan_clone(
        self, rv: "TTSResult", out: Path, t0: float, *, spec: Any = None,
    ) -> Optional["TTSResult"]:
        """尝试局域网零样本克隆。

        返回值语义：
          - TTSResult：LAN 成功，或硬失败且未开云端兜底（直接定稿）
          - None：局域网不可用/失败且允许兜底 → 调用方回落云端
        """
        from src.ai.voice_clone_client import VoiceCloneClient

        lan = VoiceCloneClient(self.voice_clone_lan)
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        ref_text = str(self.voice_profile.get("reference_text") or "").strip()
        # fish_speech 返回 WAV：用 .wav 产物并据此标记格式/测时长
        lan_out = out.with_suffix(".wav")

        # P5：情感 → 克隆主机。两条互补通道：
        #  (a) instructions：结构化自然语言语气指令（不会被读出，零 garble），MiniCPM-o
        #      等支持的主机据此带情绪；不支持的主机忽略 → **默认开**（情感非中性即带）。
        #  (b) 内联标记（如 "(joyful) 你好"）：fish_speech S2 专用，未支持会被读出，故
        #      **opt-in**（voice_clone_lan.emotion_inline_tags），默认关。
        lan_text = rv.text
        lan_instructions = ""
        try:
            if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
                from src.ai.voice_emotion import to_fish_text, to_qwen_instructions
                lan_instructions = to_qwen_instructions(spec, base=self.instructions)
                if bool((self.voice_clone_lan or {}).get("emotion_inline_tags", False)):
                    lan_text = to_fish_text(rv.text, spec)
        except Exception:
            lan_text = rv.text
            lan_instructions = ""

        def _finalize_err(msg: str) -> "TTSResult":
            rv.error = msg
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # 健康探测（短超时 + 进程缓存）
        if not await asyncio.to_thread(lan.health_ok):
            if lan.cloud_fallback:
                logger.info("[tts] voice_clone_lan unreachable → 回落云端")
                return None
            return _finalize_err("voice_clone_lan_unreachable")

        def _do_clone() -> None:
            lan.synthesize_clone(
                lan_text, ref, lan_out, reference_text=ref_text,
                instructions=lan_instructions)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_do_clone),
                timeout=lan.synth_timeout_sec,
            )
        except Exception as ex:
            try:
                lan_out.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            if lan.cloud_fallback:
                logger.warning("[tts] voice_clone_lan failed (%s) → 回落云端", ex)
                return None
            return _finalize_err(f"voice_clone_lan_failed:{str(ex)[:200]}")

        if lan_out.exists() and lan_out.stat().st_size > 0:
            rv.ok = True
            rv.provider = "voice_clone_lan"
            rv.format = "wav"
            rv.audio_path = str(lan_out)
            rv.extra["bytes"] = lan_out.stat().st_size
            rv.extra["lan_base_url"] = lan.base_url
            try:
                dur, src = compute_audio_duration_sec(str(lan_out), "wav")
                rv.duration_sec = float(dur)
                rv.duration_source = str(src)
            except Exception:
                rv.duration_sec = -1.0
                rv.duration_source = "unknown"
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # 产物为空
        if lan.cloud_fallback:
            return None
        return _finalize_err("voice_clone_lan_empty")

    async def _try_minicpm_clone(
        self, rv: "TTSResult", out: Path, t0: float, *, spec: Any = None,
    ) -> Optional["TTSResult"]:
        """MiniCPM-o 情感克隆（与 fish_speech 共用 /v1/tts/clone 契约，复用 VoiceCloneClient）。

        用人设 ``voice_profile.reference_audio_path`` 作参考音克隆音色；情感经
        ``to_qwen_instructions`` 作结构化语气指令（系统侧风格，**绝不读出** → 零 garble）。
        输出 **WAV**（与 fish 同）。MiniCPM-o 慢于实时（~0.5–0.7x），仅用于**异步语音消息**
        （可接受等待），绝不用于实时通话。

        返回值语义（与 ``_try_lan_clone`` 一致）：
          - ``TTSResult``：成功，或**配置类硬失败**（缺同意/参考音，应暴露而非用通用音色掩盖）
          - ``None``：主机不可达 / 传输失败且 ``cloud_fallback`` → 调用方回落（edge）
        """
        from src.ai.voice_clone_client import VoiceCloneClient

        def _finalize_err(msg: str) -> "TTSResult":
            rv.error = msg
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # 克隆必须有同意 + 参考音文件：缺则配置类硬失败（暴露，不绕过 owner_consent）
        vp = self.voice_profile or {}
        ref = str(vp.get("reference_audio_path") or "").strip()
        if not bool(vp.get("owner_consent", False)):
            return _finalize_err("voice_profile_requires_owner_consent")
        if not ref:
            return _finalize_err("voice_profile_missing_reference_audio_path")
        if not Path(ref).is_file():
            return _finalize_err(f"voice_profile_reference_audio_missing:{ref}")

        cfg = dict(self.minicpm_clone or {})
        cfg["enabled"] = True
        cloud_fallback = bool(cfg.get("cloud_fallback", True))
        client = VoiceCloneClient(cfg)
        ref_text = str(vp.get("reference_text") or "").strip()
        if not ref_text:
            # sidecar 自动发现（2026-08-29，与 `_try_avatar_clone` 对齐）：全部人设的
            # 逐字稿只落在 `ref.wav` 旁的同名 `.txt`，没写进 voice_profile 字段。本路径
            # 此前缺这一层回落 → ref_text 恒空 → IndexTTS-2 掉出 inference_zero_shot
            # 保真路径（逐字稿是它保音色的前提），克隆相似度无声下降。
            from src.ai.avatar_voice import find_reference_text
            ref_text = find_reference_text(ref)

        # 情感 → instructions（结构化语气，绝不读出）；neutral 则用运营基线 instructions
        instr = self.instructions
        if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
            try:
                from src.ai.voice_emotion import to_qwen_instructions
                instr = to_qwen_instructions(spec, base=self.instructions)
            except Exception:
                instr = self.instructions

        mc_out = out.with_suffix(".wav")

        # 健康探测（短超时 + 进程缓存）；不可达且允许兜底 → 回落
        if not await asyncio.to_thread(client.health_ok):
            # 区分「可达但模型未载入」(惰性主机常见：supervisor 常驻但 worker 未起) 与「彻底不可达」：
            # 前者后台触发一次载入自愈（本条仍回落 edge，约 20–30s 后自动恢复克隆声，无需人工干预）。
            if client.auto_load:
                try:
                    detail = await asyncio.to_thread(client.probe_health_detail)
                    if (detail.get("reachable") and detail.get("model_loaded") is False
                            and not detail.get("loading")):
                        if await asyncio.to_thread(client.request_model_load_async):
                            logger.warning(
                                "[tts] minicpm_clone 模型未载入，已触发后台载入"
                                "（本条回落 edge，约 20–30s 后自动恢复克隆声）")
                except Exception:
                    pass
            if cloud_fallback:
                logger.info("[tts] minicpm_clone unreachable → 回落兜底")
                return None
            return _finalize_err("minicpm_clone_unreachable")

        def _do_clone() -> None:
            client.synthesize_clone(
                rv.text, ref, mc_out, reference_text=ref_text, instructions=instr)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_do_clone), timeout=client.synth_timeout_sec)
        except Exception as ex:
            try:
                mc_out.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            if cloud_fallback:
                logger.warning("[tts] minicpm_clone failed (%s) → 回落兜底", ex)
                return None
            return _finalize_err(f"minicpm_clone_failed:{str(ex)[:200]}")

        if mc_out.exists() and mc_out.stat().st_size > 0:
            rv.ok = True
            rv.provider = "minicpm_clone"
            rv.format = "wav"
            rv.audio_path = str(mc_out)
            rv.extra["bytes"] = mc_out.stat().st_size
            rv.extra["minicpm_base_url"] = client.base_url
            try:
                dur, src = compute_audio_duration_sec(str(mc_out), "wav")
                rv.duration_sec = float(dur)
                rv.duration_source = str(src)
            except Exception:
                rv.duration_sec = -1.0
                rv.duration_source = "unknown"
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        if cloud_fallback:
            return None
        return _finalize_err("minicpm_clone_empty")

    async def _try_hub_fish(
        self, rv: "TTSResult", out: Path, t0: float, *, spec: Any = None,
        text: Optional[str] = None, budget_cap: Optional[float] = None,
        split_part: bool = False, interactive: bool = False,
    ) -> Optional["TTSResult"]:
        """幻声 hub IndexTTS-2 高保真克隆（.176:9000 /api/tts_only）。

        gated 于 ``avatar_voice.hub_fish.enabled``（默认关）。以人设 id 作 hub 声纹
        档名（同名 1:1）；hub 侧已注册参考音，本地无需 ref 文件。

        ``text``：调用方应传入**已口语化**的送稿（减读稿感）；缺省回落 ``rv.text``。
        内部再经 ``polish_hub_speak_text``——剥 Cosy 副语言标记、书面句号改换气，
        绝不下发 ``[sigh]`` 等 Cosy 专属标签（IndexTTS 会当正文念出）。

        ``budget_cap``：调用方剩余预算（秒）。hub 自身预算（timeout×best_of+15，
        本机配置下 60s）可能大于交互式调用方的总预算 → 按剩余收口；不足 2s 直接
        跳过（省一次注定被掐死的往返）。None＝旧行为。

        返回：成功 TTSResult(provider=hub_fish)；未启用/不在名单/不可达/失败 → None
        （调用方贯穿回落本地 CosyVoice3，绝不阻塞出站）。
        """
        cfg = dict(self.avatar_voice or {})
        hf = cfg.get("hub_fish") if isinstance(cfg.get("hub_fish"), dict) else {}
        if not bool(hf.get("enabled", False)):
            return None
        pid = str(self.persona_id or "").strip()
        if not pid:
            return None
        allow = hf.get("persona_allowlist") or []
        if allow and pid not in allow:
            return None
        # 人设 → hub 声纹档映射（2026-07-27 音色一致性）：原先硬编 profile=persona_id，
        # 于是「同人设在 hub 上有多个档、我们恰好指到质量最差那个」无从纠正。映射表让
        # 「换角色音色」变成改一行配置，且与幻影共用同一档时音色天然一致。
        pmap = hf.get("profile_map") if isinstance(hf.get("profile_map"), dict) else {}
        profile = str(pmap.get(pid) or pid).strip()
        if not profile:
            return None
        raw = str(text if text is not None else (rv.text or "")).strip()
        text = polish_hub_speak_text(raw)
        if not text:
            return None
        if text != raw:
            rv.extra["hub_fish_polished"] = True
        if text != str(rv.text or "").strip():
            rv.extra["hub_fish_spoken_text"] = text
        base_url = str(hf.get("base_url") or "http://192.168.0.176:9000").rstrip("/")
        timeout_sec = float(hf.get("timeout_sec", 30.0) or 30.0)
        try:
            best_of = int(hf.get("best_of", 1) or 1)
        except (TypeError, ValueError):
            best_of = 1
        # 分条发送的单条（split_part）按 best_of_parts 取候选（缺省=沿用 best_of，
        # 行为不变）：2-3 条 × best_of=2 是 hub GPU 的主要放大器，synth_verify 已
        # 兜坏 take，分条降 1 候选可把 hub 压力近乎砍半（与直播/出图共卡更稳）。
        if split_part and hf.get("best_of_parts") is not None:
            try:
                best_of = max(1, int(hf.get("best_of_parts")))
            except (TypeError, ValueError):
                pass
        # 交互式（坐席盯着等：收件箱试听/直发）：候选数封顶（缺省 1，可配
        # best_of_interactive）——synth_verify 已兜坏 take，第二候选对交互路径
        # 是 ~1×hub 往返的纯延迟税。试听与直发同参（interactive 一致），试听
        # 产物经「所听即所发」复用为出站音频时零分叉。
        if interactive:
            try:
                best_of = min(best_of, max(1, int(
                    hf.get("best_of_interactive", 1) or 1)))
            except (TypeError, ValueError):
                best_of = 1
        # ogg 直出（2026-07-25）：请求 hub 侧转 opus 48k，省本机发送前一次 ffmpeg 转码
        # （voice_sender.convert_to_ogg_opus 对 .ogg 直接放行）。基线 wav=零行为变化；
        # 实际落盘格式以响应字节魔数为准（hub ffmpeg 异常会静默回退 wav）。
        response_format = str(hf.get("response_format") or "wav").strip().lower() or "wav"

        # 引擎显式钉住（空=沿用 hub 档上配置=旧行为）：同一 hub 上智聊与幻影用同一
        # 引擎，才不会出现「同人设两种音色」。
        hub_engine = str(hf.get("tts_engine") or "").strip()
        # 引擎冒名＝按合成失败处理（默认开）。置 false 只记录不拦，用于 hub 侧排障。
        verify_engine = bool(hf.get("verify_engine", True))
        strict_voice = str(
            cfg.get("voice_consistency") or "lenient").strip().lower() == "strict"
        # strict 人设放弃 ogg 直出换「可验证」（2026-08-22）：hub 转 opus 会重采样到
        # 48k 并改写 OpusHead 的原采样率字段 → 引擎指纹被抹平，换声根本查不出来。要
        # wav 只是多一次本地 ffmpeg 转码（长回复的分段路本就一直这么走），这点开销
        # 换「绝不把别人的声音发给客户」。非 strict / 关校验 / 引擎无指纹 → 不变。
        if response_format != "wav" and strict_voice and verify_engine and hub_engine:
            try:
                from src.ai.avatar_voice import (
                    merged_rate_table,
                    normalize_engine_name,
                )
                # cached_only：本决策点在 event loop 里同步执行，绝不为它发网络
                # （目录常被上一轮预检/合成暖过；冷缓存时退化成内置三引擎＝旧行为）
                _fingerprintable = normalize_engine_name(hub_engine) in \
                    merged_rate_table(hf.get("base_url"), cached_only=True)
            except Exception:
                _fingerprintable = False
            if _fingerprintable:
                response_format = "wav"
                rv.extra["hub_format_forced_wav"] = True

        # 语言（防中文声纹念外语）+ 情绪（弱情绪归 neutral 保真，与本地链同口径）
        language = "zh"
        try:
            from src.ai.voice_clone_client import effective_clone_language
            language = effective_clone_language(text, default="zh") or "zh"
        except Exception:
            language = "zh"
        emotion = ""
        if spec is not None:
            try:
                from src.ai.voice_emotion import (
                    STRONG_EMOTION_THRESHOLD,
                    to_cosyvoice_emotion,
                )
                _thr = float(cfg.get(
                    "emotion_channel_threshold", STRONG_EMOTION_THRESHOLD)
                    or STRONG_EMOTION_THRESHOLD)
                # hub 专属情感阈值（P0-3 2026-08-03）：全局阈值是 7852 CosyVoice
                # 的「情感标签会掉 instruct2 → 音色漂移」权衡；hub 底座 IndexTTS-2
                # 情感与音色**解耦**（emo 通道不吃音色税），弱情绪塌缩成 neutral
                # 只剩平板念稿的代价。``hub_fish.emotion_threshold`` 单独放低阈值
                # （overlay 0.35），日常闲聊情绪（0.4-0.7）也带情感标签；缺省
                # 沿用全局阈值=旧行为。7852 回落路径不受影响（仍走全局阈值）。
                _hub_thr = hf.get("emotion_threshold")
                if _hub_thr is not None:
                    try:
                        _thr = float(_hub_thr)
                    except (TypeError, ValueError):
                        pass
                emotion = to_cosyvoice_emotion(
                    spec, default="neutral", strong_threshold=_thr)
            except Exception:
                emotion = ""

        # 落盘后缀按**实际返回格式**起（默认 wav；请求 ogg 而 hub 回退 wav 时仍落 .wav）
        synth: Dict[str, Any] = {"path": out.with_suffix(".wav"), "fmt": "wav"}

        def _do_synth() -> None:
            from src.ai.avatar_voice import (
                detect_silent_audio, hub_fish_synthesize)
            audio, fmt = b"", "wav"
            # B61 哑音兜底（2026-08-23 `_309`/`_311`）：hub 在显存高压下会产出
            # 「合法容器、零能量」的哑音，200/字节数/时长全绿——能量阈值是唯一
            # 能在工程侧拦住它的判据。首次命中重试一次（换一发大概率恢复），
            # 复发→按合成失败抛出走既有回落链（本地克隆/文字），绝不把无声
            # 音频发给客户。ogg 也在此覆盖（指纹守卫只认 wav，ogg 此前是盲区）。
            for _attempt in (1, 2):
                audio, fmt = hub_fish_synthesize(
                    base_url, profile, text, language=language, emotion=emotion,
                    best_of=best_of, timeout_sec=timeout_sec,
                    audio_format=response_format, tts_engine=hub_engine)
                fmt = str(fmt or "wav").strip().lower() or "wav"
                if detect_silent_audio(audio, fmt) is True:
                    rv.extra["hub_silent_audio"] = _attempt
                    logger.warning(
                        "[tts] hub 产物疑似哑音（无能量，attempt=%d fmt=%s "
                        "bytes=%d profile=%s）%s", _attempt, fmt, len(audio),
                        profile, "→ 重试一次" if _attempt == 1 else "→ 判合成失败")
                    if _attempt == 1:
                        continue
                    raise RuntimeError("hub_silent_audio: no energy in output")
                break
            self._engine_gate(
                rv, audio, fmt, expected=hub_engine, verify=verify_engine,
                profile=profile, base_url=base_url)
            synth["fmt"] = fmt
            synth["path"] = out.with_suffix(f".{fmt}")
            synth["path"].write_bytes(audio)

        # 音色一致性 strict（2026-07-27）：该人设的音色事实源＝hub 档。hub 挂了就**不许**
        # 让本机 7852（另一份参考音=另一种音色）顶班——静默换声比不发语音更伤，与
        # no_edge_fallback「宁缺毋滥」同一方针。调用方据 ok=False 回落文字。
        # （置于预算跳过之前：没预算尝试 hub ≠ hub 不是音色事实源。）
        if strict_voice:
            rv.extra["hub_fish_required"] = True

        # ── 合成前：hub 引擎目录预检（2026-08-22，比采样率指纹更早更准）───────
        # hub 的引擎解析是 prefer 语义，点名引擎不可用就静默换一个——目录里那句
        # ``available:false`` 就是「下一次合成会被顶包」的确定性前兆。提前拦下：
        # ① 不必先烧一次 GPU 再拒发；② ogg 直出（指纹被转码抹平）同样有效；
        # ③ 错误码直接说出根因（引擎离线），不必让运维从采样率反推。
        # **拿不准一律放行**（目录不可达/引擎未登记/没钉引擎 → None）；关校验
        # （verify_engine=false）时完全不预检，保留 hub 侧排障通道。
        if verify_engine and hub_engine:
            try:
                _offline = await asyncio.to_thread(hub_engine_offline, cfg)
            except Exception:
                _offline = False
            if _offline:
                rv.extra["hub_engine_offline"] = hub_engine
                try:
                    from src.ai.avatar_voice_stats import get_avatar_voice_stats
                    get_avatar_voice_stats().record_engine_check(
                        "mismatch", f"{hub_engine} 在 hub 目录里 available=false",
                        profile=profile)
                except Exception:
                    pass
                # 需求驱动唤醒（2026-08-22）：hub 的 idle_park 是「按需换回」的设计，
                # 而「有人现在要合成」就是那个需求信号——只靠看门狗（宽限 20min）
                # 意味着客户侧降级窗口最坏 20 分钟，挂在这里收缩到一次冷载（~18s）
                # 即下一条消息。后台线程 + 90s 节流，**绝不阻塞本条**（本条照旧
                # strict 拒发 / lenient 顶包）。`auto_wake=false` 可关（共享 hub 上
                # 运营可能是刻意泊掉的，此时不该跟人抢显存）。
                if bool(hf.get("auto_wake", True)):
                    try:
                        from src.ai.avatar_voice import hub_engine_wake_bg
                        hub_engine_wake_bg(hf.get("base_url"), hub_engine)
                    except Exception:
                        pass
                logger.warning(
                    "[tts] hub 目录称引擎 %s 不可用 → 跳过 hub（再打就是别人的"
                    "声音）persona=%s", hub_engine, self.persona_id or "")
                return None

        # ── 合成前：超时熔断（2026-08-22「在岗但答不上来」实测）─────────────
        # 目录 available:true + 引擎 /health 200，但宿主显存 97% 满 → 短句 36~69s
        # vs 预算 30s ⇒ 每一发都超时。前两道闸都不响（预检看 available、指纹闸要
        # 先拿到音频），于是每条消息白等 30 秒。开路期直接跳过：strict 的最终结果
        # 与「等满 30s 再拒发」完全相同，只是客户**立刻**拿到文字。
        # 刻意**不**挂在 verify_engine 上：那个键管的是「引擎身份要不要验」，本闸
        # 管的是「它答不答得上来」——两件正交的事。运维为排 hub 路由关掉指纹校验时，
        # 不该连超时保护一起失去（要关本闸走 timeout_breaker）。
        if hub_engine and bool(hf.get("timeout_breaker", True)):
            try:
                from src.ai.avatar_voice import hub_synth_breaker_open
                _tripped = hub_synth_breaker_open(base_url, hub_engine)
            except Exception:
                _tripped = False
            if _tripped:
                rv.extra["hub_synth_timing_out"] = hub_engine
                logger.warning(
                    "[tts] hub 引擎 %s 连续超时（熔断开路）→ 跳过 hub，不让客户"
                    "白等一轮 persona=%s", hub_engine, self.persona_id or "")
                return None

        if budget_cap is not None and budget_cap < 2.0:
            logger.info(
                "[tts] hub_fish skipped: 剩余预算不足（%.1fs）", max(budget_cap, 0.0))
            return None

        # ── 慢速拟人编排分支（pacing，2026-08-19 tmp_voice_eval v3 投产）────────
        # 分句逐段合成+真停顿+分段情绪+降速+底床/真呼吸。注意送 raw（polish 前
        # 口语稿）——polish 会把句中句号改省略号，先 polish 会毁掉分句；编排内
        # 每段单独 polish。任何不适用/失败 → None，贯穿到下方整段单发（同档同
        # 引擎，不构成换声；宁快勿哑）。
        paced = await self._hub_fish_paced(
            rv, out, t0, hf=hf, raw_text=raw, profile=profile,
            base_url=base_url, timeout_sec=timeout_sec, hub_engine=hub_engine,
            language=language, emotion=emotion, budget_cap=budget_cap,
            split_part=split_part, interactive=interactive, spec=spec)
        if paced is not None:
            return paced
        if rv.extra.get("hub_engine_blocked"):
            # 分段路已实锤 hub 换了引擎：整段单发是同一 hub 同一档（且多为 ogg＝指纹
            # 被转码抹平，查不出来），回落只会把冒牌音色发出去。直接判 hub 失败。
            return None

        try:
            budget = timeout_sec * (best_of if best_of > 1 else 1) + 15
            if budget_cap is not None:
                budget = min(budget, max(2.0, budget_cap))
            await asyncio.wait_for(asyncio.to_thread(_do_synth), timeout=budget)
            self._note_hub_timing(base_url, hub_engine, timed_out=False)
        except Exception as ex:
            try:
                synth["path"].unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            # 只有**超时**喂熔断（其余是快败，没有延迟税可省；引擎冒名另有指纹闸）
            if isinstance(ex, (asyncio.TimeoutError, TimeoutError)):
                self._note_hub_timing(base_url, hub_engine, timed_out=True)
            _exs = f"{type(ex).__name__}: {ex}".rstrip(": ")
            logger.info("[tts] hub_fish failed (%s) → 回落本地克隆", _exs)
            return None

        av_out: Path = synth["path"]
        av_fmt: str = synth["fmt"]
        if av_out.exists() and av_out.stat().st_size > 0:
            rv.ok = True
            rv.provider = "hub_fish"
            rv.format = av_fmt
            rv.audio_path = str(av_out)
            rv.extra["bytes"] = av_out.stat().st_size
            rv.extra["hub_fish_profile"] = profile
            rv.extra["hub_fish_base_url"] = base_url
            if emotion and emotion != "neutral":
                rv.extra["hub_fish_emotion"] = emotion
            if av_fmt == "wav":
                try:
                    dur, src = compute_audio_duration_sec(str(av_out), "wav")
                    rv.duration_sec = float(dur)
                    rv.duration_source = str(src)
                except Exception:
                    rv.duration_sec = -1.0
                    rv.duration_source = "unknown"
            else:
                # ogg 无轻量解析器 → ffprobe（预渲染层同款）；取不到按 -1/unknown fail-open
                rv.duration_sec = -1.0
                rv.duration_source = "unknown"
                try:
                    from src.client.voice_sender import probe_audio_duration_ms
                    ms = probe_audio_duration_ms(str(av_out))
                    if ms and ms > 0:
                        rv.duration_sec = ms / 1000.0
                        rv.duration_source = "ffprobe"
                except Exception:
                    pass
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv
        return None

    @staticmethod
    def _note_hub_timing(base_url: str, engine: str, *, timed_out: bool) -> None:
        """把本次 hub 合成的时间结局喂给超时熔断（异常一律吞掉）。

        两条路（整段单发 / 分段编排）都要喂——分段路是本机生产主路，漏掉它熔断就
        看不到任何超时。
        """
        if not engine:
            return
        try:
            from src.ai.avatar_voice import note_hub_synth_outcome
            note_hub_synth_outcome(base_url, engine, timed_out=timed_out)
        except Exception:
            pass

    def _engine_gate(
        self, rv: "TTSResult", audio: bytes, fmt: str, *,
        expected: str, verify: bool, profile: str, base_url: str = "",
    ) -> None:
        """引擎归属闸（2026-08-22「fish 冒充 IndexTTS-2」事故）。

        hub 是 prefer 语义：点名的引擎在目录里不可用就**静默换一个**合成，信封里
        没有任何引擎字段（实测），于是「合成成功」全绿而客户听到的是另一个人的
        声音。这里按采样率指纹反查真实引擎——判不了一律放行（见
        ``avatar_voice.engine_attribution``），只有拿到「明确是另一个已登记引擎」
        的正面反证才拦。

        拦下后：strict 人设由 ``hub_fish_required`` 挡住本地顶班 → 回落文字；
        lenient 回落本地克隆（同人设参考音＝仍是本人的声音）。两者都好过把别人的
        声音发给客户——与 ``no_edge_fallback``「宁缺毋滥」同一方针。
        """
        from src.ai.avatar_voice import (
            ENGINE_SAMPLE_RATES,
            HubEngineMismatch,
            engine_attribution,
            merged_rate_table,
            normalize_engine_name,
        )
        # 内置三引擎＝真机量过的指纹，热路零网络；其余引擎（gptsovits/qwen3_tts/
        # voxcpm2…）此前一律「未登记指纹」＝完全不受校验，改从 hub 目录自报的
        # capabilities.sample_rate 补齐（60s 缓存，且预检刚拉过通常是热的）。
        rate_table: Optional[Dict[str, int]] = None
        if normalize_engine_name(expected) not in ENGINE_SAMPLE_RATES:
            try:
                rate_table = merged_rate_table(base_url, timeout=1.5)
            except Exception:
                rate_table = None
        verdict, detail = engine_attribution(
            audio, fmt, expected, rate_table=rate_table)
        rv.extra["hub_engine_verdict"] = verdict
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            get_avatar_voice_stats().record_engine_check(
                verdict, detail, profile=profile)
        except Exception:
            pass
        if verdict != "mismatch":
            return
        rv.extra["hub_engine_mismatch"] = detail
        logger.warning(
            "[tts] hub 引擎冒名：%s（profile=%s）→ %s", detail, profile,
            "按合成失败处理" if verify else "仅记录（verify_engine=false）")
        if verify:
            # 分段路的调用方据此**不再回落整段单发**（见 `_try_hub_fish`）：同一 hub
            # 同一档只会拿到同样的冒牌音色，而整段常是 ogg＝无指纹可查，回落等于
            # 「查不出来就发出去」。
            rv.extra["hub_engine_blocked"] = detail
            raise HubEngineMismatch(detail)

    async def _hub_fish_paced(
        self, rv: "TTSResult", out: Path, t0: float, *, hf: Dict[str, Any],
        raw_text: str, profile: str, base_url: str, timeout_sec: float,
        hub_engine: str, language: str, emotion: str,
        budget_cap: Optional[float], split_part: bool, interactive: bool,
        spec: Any = None,
    ) -> Optional["TTSResult"]:
        """慢速拟人编排（``hub_fish.pacing``，默认关）：不适用/失败一律 None。

        排除面（都回落整段单发）：分条发送的单条（split_send 已管消息级节奏，
        条内本就短）；交互链默认关（坐席在等，``pacing.interactive: true`` 可开）；
        非中文（停顿策略按中文标点校准）；短文本；预算不足；numpy 缺席。
        """
        pc = hf.get("pacing") if isinstance(hf.get("pacing"), dict) else {}
        if not bool(pc.get("enabled", False)):
            return None
        if split_part:
            return None
        if interactive and not bool(pc.get("interactive", False)):
            return None
        if not str(language or "").lower().startswith("zh"):
            return None
        try:
            from src.ai import voice_pacing as vpac
            from src.ai.avatar_voice import HubEngineMismatch
        except Exception:
            return None
        if not vpac.numpy_available():
            return None
        text = str(raw_text or "").strip()
        try:
            min_chars = int(pc.get("min_chars", 24) or 24)
        except (TypeError, ValueError):
            min_chars = 24
        if len(text) < min_chars:
            return None
        try:
            max_chunks = max(2, int(pc.get("max_chunks", 8) or 8))
        except (TypeError, ValueError):
            max_chunks = 8
        est_chunks = min(max(len(vpac.split_chunks(text)), 1), max_chunks)
        if est_chunks < 2:
            return None
        try:
            chunk_timeout = float(pc.get("chunk_timeout_sec", 30.0) or 30.0)
        except (TypeError, ValueError):
            chunk_timeout = 30.0
        chunk_timeout = min(chunk_timeout, float(timeout_sec or 30.0))
        est_total = est_chunks * float(pc.get("est_chunk_sec", 8.0) or 8.0) + 8.0
        if budget_cap is not None and budget_cap < est_total:
            logger.info(
                "[tts] pacing skipped: 预算不足（%.0fs < 预估 %.0fs）→ 整段单发",
                budget_cap, est_total)
            return None

        # ── 实施65 语音剧本：colloquial.script 开启时先让 LLM 产出带 ‖ 停顿/
        # [breath] 标记的剧本，paced_synthesize 自动识别并按语义执行；失败/校验
        # 不过 → 原 text 走旧 crc 编排。剧本只活在本分支内，其他合成路径永远
        # 见不到标记（PacingSkip 回落整段单发时用的也是调用方原 text）。
        _av_cfg = self.avatar_voice if isinstance(self.avatar_voice, dict) else {}
        _sc_col = (_av_cfg.get("colloquial")
                   if isinstance(_av_cfg.get("colloquial"), dict) else {})
        if bool(_sc_col.get("script")) and spec is not None:
            try:
                _sc = await self.prepass_colloquial_llm(
                    text, spec=spec, script=True)
            except Exception:
                logger.debug("[tts] 语音剧本生成异常（回落 crc 编排）", exc_info=True)
                _sc = None
            if _sc:
                text = _sc

        vp = self.voice_profile or {}
        expressive = vpac.is_expressive(
            str(vp.get("instruct_style") or ""), str(vp.get("emotion") or ""))
        try:
            best_of_chunk = max(1, int(pc.get("best_of_parts", 1) or 1))
        except (TypeError, ValueError):
            best_of_chunk = 1

        # 分段路是**唯一**天然带引擎指纹的 hub 路径：每段显式要 wav（下方本地拼接
        # 需要 PCM），而 wav 头里的采样率就是引擎身份证。整段单发常配 ogg，hub 转码
        # 恒重采样 48k、连 OpusHead 的原采样率字段都被改写 → 指纹已被抹平。
        verify_engine = bool(hf.get("verify_engine", True))

        def _synth_chunk(chunk_text: str, chunk_emo: str) -> bytes:
            from src.ai.avatar_voice import hub_fish_synthesize
            audio, fmt = hub_fish_synthesize(
                base_url, profile, chunk_text, language=language,
                emotion=chunk_emo, best_of=best_of_chunk,
                timeout_sec=chunk_timeout, audio_format="wav",
                tts_engine=hub_engine)
            if str(fmt or "").lower() != "wav":
                raise vpac.PacingSkip(f"chunk format {fmt} != wav")
            self._engine_gate(
                rv, audio, "wav", expected=hub_engine, verify=verify_engine,
                profile=profile, base_url=base_url)
            return audio

        breath_loader = None
        if bool(pc.get("breath", True)):
            def breath_loader(sr: int):  # noqa: F811
                # 缓存落 CWD 相对 config/（服务进程 CWD=实例数据根，C 类数据落点）
                return vpac.load_breath_for_profile(
                    base_url, profile, Path("config/voice_pacing_refs"), sr)

        # 实施65 定稿（2026-08-24 老板耳测）：语速按人设覆写——voice_profile.
        # pacing_tempo 优先于全局 pacing.tempo（男声沉稳音色叠全局降速会拖沓，
        # 陈默定 1.06；女声走全局 1.0）。think_tempo 未显式配时随 tempo 略降。
        try:
            _tempo = float(vp.get("pacing_tempo") or pc.get("tempo", 0.93) or 0.93)
        except (TypeError, ValueError):
            _tempo = float(pc.get("tempo", 0.93) or 0.93)
        try:
            _think_tempo = float(pc.get("think_tempo") or 0.0) or max(
                0.90, _tempo - 0.05)
        except (TypeError, ValueError):
            _think_tempo = max(0.90, _tempo - 0.05)

        def _build():
            return vpac.paced_synthesize(
                text, synth_chunk=_synth_chunk, base_emotion=emotion,
                expressive=expressive, polish=polish_hub_speak_text,
                tempo=_tempo,
                think_tempo=_think_tempo,
                min_chars=min_chars, max_chunks=max_chunks,
                breath_loader=breath_loader, bed=bool(pc.get("bed", True)),
                inject_think=bool(pc.get("inject_think", True)),
                seed_key=profile)

        total_budget = est_chunks * chunk_timeout + 20.0
        if budget_cap is not None:
            total_budget = min(total_budget, max(2.0, budget_cap))
        try:
            audio, meta = await asyncio.wait_for(
                asyncio.to_thread(_build), timeout=total_budget)
            self._note_hub_timing(base_url, hub_engine, timed_out=False)
        except vpac.PacingSkip as ex:
            logger.info("[tts] pacing skip（%s）→ 整段单发", ex)
            return None
        except HubEngineMismatch as ex:
            # 编排本身没问题，是 hub 换了引擎——绝不回落整段单发（那条路查不出指纹）。
            logger.warning("[tts] pacing aborted：hub 引擎冒名（%s）→ 判 hub 失败", ex)
            return None
        except Exception as ex:
            # 分段路是生产主路（zhiliao pacing 常开）：它不喂熔断，熔断就永远看不到
            # 「宿主吃紧」这类超时（本机实测正是从这里流失的）。
            if isinstance(ex, (asyncio.TimeoutError, TimeoutError)):
                self._note_hub_timing(base_url, hub_engine, timed_out=True)
            logger.info("[tts] pacing failed（%s: %s）→ 整段单发",
                        type(ex).__name__, str(ex)[:160])
            return None

        av_out = out.with_suffix(".wav")
        av_out.write_bytes(audio)
        rv.ok = True
        rv.provider = "hub_fish"
        rv.format = "wav"
        rv.audio_path = str(av_out)
        rv.extra["bytes"] = len(audio)
        rv.extra["hub_fish_profile"] = profile
        rv.extra["hub_fish_base_url"] = base_url
        if emotion and emotion != "neutral":
            rv.extra["hub_fish_emotion"] = emotion
        rv.extra["hub_fish_paced"] = meta
        rv.duration_sec = float(meta.get("dur_sec") or -1.0)
        rv.duration_source = "pacing_meta"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "[tts] hub_fish paced ok: %s段 情绪%s 呼吸%s 床=%s %.1fs 用时%dms",
            meta.get("chunks"), "/".join(sorted(set(meta.get("chunk_emotions") or [])))
            or "-", meta.get("breaths"), meta.get("bed"),
            float(meta.get("dur_sec") or -1.0), rv.latency_ms)
        return rv

    async def prepass_colloquial_llm(
        self, text: str, *, spec: Any = None, colloquial_lead: bool = True,
        script: bool = False,
    ) -> Optional[str]:
        """整段先做一次 LLM 口语化（分条发送前调用）→ 改写后文本，或 None=没改。

        分条语音原本逐条各打一次 LLM 改写（云端一次往返实测 ~5s ×N 条）——2 条就把
        整链推过 text-first 25s 预算，客户先收到占位文字再收语音。这里把改写提到切条
        之前只做一次，各条 ``synthesize(skip_llm_colloquial=True)`` 只跑免费的规则档
        与 C+ 微特征。附带收益：整段一次改写在条间口吻连贯（逐条独立改写各自起头）。

        不满足条件（无情绪档/未开口语化/非 llm 模式/人设 opt-out）、异常、原样返回
        一律 None —— 调用方照旧逐条改写，行为不劣于旧链。
        """
        av = self.avatar_voice if isinstance(self.avatar_voice, dict) else {}
        col_cfg = (av.get("colloquial")
                   if isinstance(av.get("colloquial"), dict) else {})
        vp = self.voice_profile or {}
        if spec is None or not col_cfg.get("enabled", False):
            return None
        if vp.get("colloquial", True) is False:
            return None
        if str(col_cfg.get("mode") or "rule").strip().lower() != "llm":
            return None
        src = str(text or "")
        try:
            import zlib as _zlib

            from src.ai.voice_colloquial import build_voice_style_hint
            from src.ai.voice_colloquial_llm import llm_colloquialize
            _catch = str(vp.get("catchphrase") or "").strip()
            _style = build_voice_style_hint(
                str(vp.get("instruct_style") or ""), self.persona_quirks,
                catchphrase=_catch,
                dialect=str(vp.get("dialect_flavor") or ""))
            _every = int(col_cfg.get("disfluency_every", 5) or 5)
            _every = max(2, min(20, _every))
            _disf = (bool(col_cfg.get("disfluency", False))
                     and _zlib.crc32(src.encode("utf-8")) % _every == 0)
            out = await llm_colloquialize(
                src, emotion=str(getattr(spec, "emotion", "neutral")),
                lead=colloquial_lead, style=_style,
                min_chars=int(col_cfg.get("min_chars", 12) or 12),
                timeout_sec=float(col_cfg.get("llm_timeout_sec", 8.0) or 8.0),
                disfluency=_disf,
                intensity=str(col_cfg.get("rewrite_intensity", "natural")
                              or "natural"),
                provider=str(col_cfg.get("provider", "local") or "local"),
                llm_endpoints=col_cfg.get("llm_endpoints"),
                temperature=float(col_cfg.get("temperature", 0.5) or 0.5),
                script=bool(script))
        except Exception:
            logger.debug("[tts] 整段口语化预处理异常（回落逐条改写）", exc_info=True)
            return None
        return out if (out and out != src) else None

    async def _try_avatar_clone(
        self, rv: "TTSResult", out: Path, t0: float, *, spec: Any = None,
        colloquial_lead: bool = True, pre_colloquialized: bool = False,
        skip_llm_colloquial: bool = False, split_part: bool = False,
        interactive: bool = False,
        deadline: Optional[float] = None,
    ) -> Optional["TTSResult"]:
        """AvatarHub CosyVoice3 情感克隆（本机 7852，backend=avatar_clone）。

        ``deadline``（monotonic 时刻，None=不限）：来自 synthesize(total_budget_sec=)
        的全链截止线——LLM 口语化 / hub / 本机克隆三级各自按剩余预算收口，防止
        「各级预算之和 ≫ 调用方外闸」时被外层 wait_for 掐死在不知道哪一级。

        用人设 ``voice_profile.reference_audio_path`` 克隆音色；情绪两条通道：
          - ``voice_profile.instruct`` 显式配置 → 走 /v1/tts/instruct 自由语气
            （比标签更细腻，如「用小声耳语的语气说」）；
          - 否则 EmotionSpec → ``to_cosyvoice_emotion`` 映射为服务端 emotion 标签
            （neutral 回落每角色可配的 ``emotion_default``，默认 gentle）。
        参考音逐字稿：``voice_profile.reference_text`` 显式配置优先，否则自动读
        参考音旁的同名 ``.txt``（见 avatar_voice.find_reference_text）。
        合成在模块级 GPU 串行锁内执行（并发纪律），产 WAV。

        返回值语义（与 ``_try_minicpm_clone`` 一致）：
          - ``TTSResult``：成功，或配置类硬失败（缺同意/参考音——暴露不掩盖）
          - ``None``：不可达/传输失败且 ``cloud_fallback`` → 调用方回落 edge
        """
        from src.ai.avatar_voice import (
            AvatarVoiceClient,
            find_reference_text,
            load_reference_b64,
        )

        def _finalize_err(msg: str) -> "TTSResult":
            rv.error = msg
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        def _remaining() -> Optional[float]:
            if deadline is None:
                return None
            return deadline - time.monotonic()

        vp = self.voice_profile or {}
        ref = str(vp.get("reference_audio_path") or "").strip()
        if not bool(vp.get("owner_consent", False)):
            return _finalize_err("voice_profile_requires_owner_consent")

        cfg = _with_hosted_voice_endpoint(dict(self.avatar_voice or {}))
        cfg["enabled"] = True

        # ── 口语化必须在 hub / 本地合成之前（2026-07-24 读稿音根因）────────
        # 旧序：hub_fish 先命中 → 直接用 rv.text 书面稿 → IndexTTS「念稿感」。
        # 新序：先口语化（LLM vivid / 规则档）→ hub 与 7852 共用同一口语送稿；
        # Cosy 副语言标记只给本地 7852（hub 会当正文念出，见 polish_hub_speak_text）。
        synth_text = str(rv.text or "")
        col_cfg = (cfg.get("colloquial")
                   if isinstance(cfg.get("colloquial"), dict) else {})
        if pre_colloquialized:
            rv.extra["colloquial_generated"] = True
        elif (spec is not None and col_cfg.get("enabled", False)
                and vp.get("colloquial", True) is not False):
            _col = None
            _emo_name = getattr(spec, "emotion", "neutral")
            _catch = str(vp.get("catchphrase") or "").strip()
            try:
                from src.ai.voice_colloquial import (
                    build_voice_style_hint,
                    colloquialize,
                    parse_persona_lead_phrases,
                )
                _persona_leads = parse_persona_lead_phrases(
                    self.persona_quirks, catchphrase=_catch)
                _dialect = str(vp.get("dialect_flavor") or "")
                _style = build_voice_style_hint(
                    str(vp.get("instruct_style") or ""),
                    self.persona_quirks,
                    catchphrase=_catch,
                    dialect=_dialect)
                if _dialect:
                    # 观测锚：方言档生效与否零流量可查（rv.extra 随语音结果落日志）
                    rv.extra["dialect_flavor"] = _dialect
            except Exception:
                _persona_leads = ()
                _style = str(vp.get("instruct_style") or "")
            if skip_llm_colloquial:
                # 调用方已对整段做过一次 LLM 口语化（分条省 N-1 次往返）：如实记账，
                # 本条只往下走免费的规则档补刀 + C+ 微特征，不再打 LLM。
                rv.extra["colloquial"] = True
                rv.extra["colloquial_llm"] = True
            elif str(col_cfg.get("mode") or "rule").strip().lower() == "llm":
                try:
                    import zlib as _zlib

                    from src.ai.voice_colloquial_llm import llm_colloquialize
                    _every = int(col_cfg.get("disfluency_every", 5) or 5)
                    _every = max(2, min(20, _every))
                    _disf = (bool(col_cfg.get("disfluency", False))
                             and _zlib.crc32(
                                 synth_text.encode("utf-8")) % _every == 0)
                    # 预算收口：LLM 改写只是锦上添花，剩余预算得先保住合成本体
                    # （预留 12s）。压缩后窗口 <2s → 直接走免费规则档，不打 LLM。
                    _llm_t = float(col_cfg.get("llm_timeout_sec", 8.0) or 8.0)
                    _rem = _remaining()
                    if _rem is not None:
                        _llm_t = min(_llm_t, _rem - 12.0)
                    if _rem is not None and _llm_t < 2.0:
                        logger.info(
                            "[tts] 剩余预算不足（%.1fs）→ 跳过 LLM 口语化走规则档",
                            max(_rem, 0.0))
                        _col = None
                    else:
                        _col = await llm_colloquialize(
                            synth_text, emotion=_emo_name, lead=colloquial_lead,
                            style=_style,
                            min_chars=int(col_cfg.get("min_chars", 12) or 12),
                            timeout_sec=_llm_t,
                            disfluency=_disf,
                            intensity=str(
                                col_cfg.get("rewrite_intensity", "natural")
                                or "natural"),
                            provider=str(col_cfg.get("provider", "local") or "local"),
                            llm_endpoints=col_cfg.get("llm_endpoints"),
                            temperature=float(
                                col_cfg.get("temperature", 0.5) or 0.5))
                    # 原样返回 ≠ 成功：让规则档再救一刀（因此/您/无需 等书面词）
                    if _col and _col != synth_text:
                        rv.extra["colloquial_llm"] = True
                        try:
                            from src.ai.voice_colloquial_llm import get_last_provider
                            _lp = get_last_provider()
                            if _lp:
                                rv.extra["colloquial_provider"] = _lp
                        except Exception:
                            pass
                    else:
                        _col = None
                except Exception:
                    _col = None
            if not _col:
                try:
                    _lead_prob = float(col_cfg.get("lead_prob", 0.5) or 0.5)
                    if _persona_leads:
                        _lead_prob = min(0.85, _lead_prob + 0.15)
                    # human_ticks：思考重复词 + 轻笑声（ChatGPT 式真人感，默认随 vivid 开）
                    _ticks = col_cfg.get("human_ticks")
                    if _ticks is None:
                        _ticks = (str(col_cfg.get("rewrite_intensity", "")
                                      or "").strip().lower() == "vivid")
                    _col = colloquialize(
                        synth_text, spec,
                        min_chars=int(col_cfg.get("min_chars", 12) or 12),
                        max_inserts=int(col_cfg.get("max_inserts", 2) or 2),
                        enable_fillers=(col_cfg.get("fillers", True) is not False
                                        and colloquial_lead),
                        enable_sentence_final=bool(
                            col_cfg.get("sentence_final", False)),
                        enable_lexical=col_cfg.get("lexical", True) is not False,
                        enable_thinking_repeat=bool(_ticks),
                        enable_soft_laugh=bool(_ticks),
                        lead_prob=_lead_prob,
                        think_prob=float(col_cfg.get("think_prob", 0.22) or 0.22),
                        laugh_prob=float(col_cfg.get("laugh_prob", 0.18) or 0.18),
                        persona_leads=_persona_leads)
                except Exception:
                    _col = None
            if _col and _col != synth_text:
                synth_text = _col
                rv.extra["colloquial"] = True

            # C+：LLM/规则都没带上微特征时，确定性后补一层（互斥，低频）
            _ticks = col_cfg.get("human_ticks")
            if _ticks is None:
                _ticks = (str(col_cfg.get("rewrite_intensity", "")
                              or "").strip().lower() == "vivid")
            if (bool(_ticks) and spec is not None
                    and not re.match(r"^\s*(哈{2,}|嘿|呵{2,}|嘻{2,})", synth_text)
                    and not re.search(
                        r"([\u4e00-\u9fff]{2})……\1", synth_text or "")):
                try:
                    from src.ai.voice_colloquial import (
                        _soft_laugh as _sl,
                        _thinking_repeat as _tr,
                        normalize_colloquial_emotion as _nce,
                    )
                    import zlib as _z2
                    _emo2, _ = _nce(spec)
                    _seed2 = _z2.crc32(synth_text.encode("utf-8"))
                    _hit = False
                    # 仅 happy/playful/excited 偶发「嘿」；warm 不加笑
                    if (_seed2 & 1) == 0 and _emo2 in (
                            "happy", "playful", "excited"):
                        _nt, _hit = _sl(
                            synth_text, _emo2, _seed2,
                            prob=float(col_cfg.get("laugh_prob", 0.08) or 0.08))
                        if _hit:
                            synth_text = _nt
                            rv.extra["soft_laugh"] = True
                            rv.extra["colloquial"] = True
                    if not _hit:
                        _nt, _hit = _tr(
                            synth_text, _seed2,
                            prob=float(col_cfg.get("think_prob", 0.22) or 0.22))
                        if _hit:
                            synth_text = _nt
                            rv.extra["thinking_repeat"] = True
                            rv.extra["colloquial"] = True
                except Exception:
                    pass
            elif re.match(r"^\s*嘿，", synth_text or ""):
                rv.extra["soft_laugh"] = True
            elif re.search(r"([\u4e00-\u9fff]{2})……\1", synth_text or ""):
                rv.extra["thinking_repeat"] = True

        # ── 开场词会话级去重（P0-2 2026-08-03「嘿病」）────────────────────────
        # 生产实锤：同一会话连续多条语音都以同一感叹词开场（嘿/哎呀/哈哈…）＝
        # 新的机械感。口语化整条链（生成层口语版/LLM 改写/规则档/C+ 轻笑）都是
        # 句级无状态，跨消息重复只有这里能看见（variety_key=会话）。任何来源的
        # 开场词一视同仁；仅 colloquial_lead=True（整条/分条首条）时参与——分条
        # 第 2/3 条本就不该有开场词，也不该重复占历史窗。剥除是安全方向：丢弃的
        # 是语义近零的话语标记，且剥后余文过短会拒剥。
        # interactive（坐席手动链）豁免：手打文字「所打即所念」，一个字都不动
        # ——去重剥词只该作用于机器产文（自动链/生成层口语版）。
        if (self.variety_key and colloquial_lead and not interactive
                and col_cfg.get("opener_dedupe", True) is not False):
            try:
                from src.ai.voice_opener_guard import guard_opener
                _og = guard_opener(self.variety_key, synth_text)
                if _og != synth_text:
                    rv.extra["opener_deduped"] = True
                    synth_text = _og
            except Exception:
                pass

        # hub IndexTTS-2 优先（口语化后的送稿）；失败贯穿回落本地 CosyVoice3。
        _rem = _remaining()
        hub_rv = await self._try_hub_fish(
            rv, out, t0, spec=spec, text=synth_text, split_part=split_part,
            interactive=interactive,
            # 留 6s 给 hub 失败后的本机克隆回落
            budget_cap=(None if _rem is None else _rem - 6.0))
        if hub_rv is not None and hub_rv.ok:
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                get_avatar_voice_stats().record_synth(
                    ok=True, latency_ms=int(hub_rv.latency_ms or 0),
                    channel="hub_fish",
                    emotion=str(hub_rv.extra.get("hub_fish_emotion") or "neutral"),
                    colloquial=bool(rv.extra.get("colloquial")),
                    colloquial_llm=bool(rv.extra.get("colloquial_llm")),
                    colloquial_generated=bool(rv.extra.get("colloquial_generated")),
                    paralinguistic=False)
            except Exception:
                pass
            return hub_rv
        if rv.extra.pop("hub_fish_required", False):
            # 错误码分两种（2026-08-22）：hub **挂了** 与 hub **换了引擎** 的处置完全
            # 不同，混成一个码会让告警把运维引向 /health（此时正绿着）——本次事故
            # 潜伏两小时就是这么来的。该码经 voice_outage 台账进断档告警的原因 Top。
            _to = str(rv.extra.get("hub_synth_timing_out") or "")
            if _to:
                # 目录与 /health **双绿**、只是答不上来（宿主显存被挤爆）：这一档
                # 若混进 hub_voice_source_unavailable，运维会去看两个绿灯然后困惑
                # 两小时——与 hub_engine_offline 分码同一个理由。
                logger.warning(
                    "[tts] hub 引擎 %s 连续超时（熔断开路）且 voice_consistency="
                    "strict → 拒发语音（客户立刻拿到文字，不白等一轮）persona=%s",
                    _to, self.persona_id or "")
                return _finalize_err(f"hub_synth_timing_out:{_to}")
            _off = str(rv.extra.get("hub_engine_offline") or "")
            if _off:
                # 合成前预检就拦下了：根因最明确的一档（hub 目录自己说该引擎离线）
                logger.warning(
                    "[tts] hub 引擎 %s 离线且 voice_consistency=strict → 拒发语音"
                    "persona=%s", _off, self.persona_id or "")
                return _finalize_err(f"hub_engine_offline:{_off}")
            _mm = str(rv.extra.get("hub_engine_mismatch") or "")
            if _mm:
                logger.warning(
                    "[tts] hub 引擎冒名且 voice_consistency=strict → 拒发语音"
                    "（%s）persona=%s", _mm, self.persona_id or "")
                return _finalize_err(f"hub_engine_mismatch:{_mm}")
            logger.warning(
                "[tts] hub 音色源不可用且 voice_consistency=strict "
                "→ 拒发语音（不用本机不同音色顶班）persona=%s",
                self.persona_id or "")
            return _finalize_err("hub_voice_source_unavailable")

        if not ref:
            return _finalize_err("voice_profile_missing_reference_audio_path")
        if not Path(ref).is_file():
            return _finalize_err(f"voice_profile_reference_audio_missing:{ref}")

        cloud_fallback = bool(cfg.get("cloud_fallback", True))
        client = AvatarVoiceClient(cfg)

        # 健康探测（短超时 + 进程缓存）；不可达且允许兜底 → 回落 edge（绝不阻塞）
        if not await asyncio.to_thread(client.health_ok):
            # 端点清单必须进日志（B124，2026-08-28）：旧文案只写死「7852」，
            # 于是「回落成默认音」的现场既看不出试过哪几个地址、也看不出托管网关
            # 到底在不在候选里——0828 那次只能靠远程取诊断包才定位。
            _tried = ", ".join(client.base_urls) or "(空)"
            if cloud_fallback:
                logger.info(
                    "[tts] 克隆端点全不可达 → 回落兜底音色（已试：%s）persona=%s",
                    _tried, self.persona_id or "")
                return None
            logger.warning(
                "[tts] 克隆端点全不可达且禁用兜底 → 拒发（已试：%s）persona=%s",
                _tried, self.persona_id or "")
            return _finalize_err("avatar_clone_unreachable")

        ref_text = str(vp.get("reference_text") or "").strip()
        if not ref_text:
            ref_text = find_reference_text(ref)

        # 情绪分库参考音（①，2026-07-14）：强情绪轮次换用对应「说话状态」的参考音
        # （开心的参考音合出来每句自带笑意——比 emotion 标签自然，且走纯 zero_shot
        # 保真路径零音色代价）。命中 → 换 ref + 该 ref 自己的逐字稿 sidecar，
        # emotion 标签强制 neutral（韵律情绪已由参考音承担，标签叠加=过度表达）。
        _emo_ref_key = ""
        if spec is not None:
            try:
                from src.ai.voice_emotion import (
                    STRONG_EMOTION_THRESHOLD as _SET,
                )
                from src.ai.voice_emotion import pick_emotion_reference
                _er = pick_emotion_reference(
                    vp, spec,
                    threshold=float(cfg.get("emotion_channel_threshold", _SET)
                                    or _SET))
                if _er:
                    ref, _emo_ref_key = _er
                    ref_text = find_reference_text(ref)   # 情绪 ref 用自己的逐字稿
                    rv.extra["emotion_ref"] = _emo_ref_key
            except Exception:
                _emo_ref_key = ""

        # 情绪通道选择（⚠ 音色保真优先，2026-07-13 事故复盘）：
        # 7852 收到非 neutral emotion 或走 /v1/tts/instruct 都会进 instruct2 路径
        # ——该路径**忽略 reference_text 逐字稿**，音色显著漂移（"像豆包"）。
        #   1. voice_profile.instruct 静态配置（运营显式要语气，接受音色代价）
        #   2. dynamic_instruct 开启（默认关）且情绪非中性 → 模板库自由语气指令
        #   3. EmotionSpec → to_cosyvoice_emotion：弱情绪(<threshold) 归 neutral
        #      =保真路径（zero_shot+逐字稿，音色最像；情绪由副语言标记+speed 表达），
        #      强情绪(≥threshold) 才出情感标签换表现力。
        instruct = str(vp.get("instruct") or "").strip()
        emotion_default = str(
            vp.get("emotion_default") or cfg.get("default_emotion") or "neutral")
        emotion = emotion_default
        speed = 1.0
        if spec is not None:
            try:
                from src.ai.voice_emotion import (
                    STRONG_EMOTION_THRESHOLD,
                    cosyvoice_speed,
                    to_cosyvoice_emotion,
                    to_cosyvoice_instruct,
                )
                _thr = float(cfg.get(
                    "emotion_channel_threshold", STRONG_EMOTION_THRESHOLD)
                    or STRONG_EMOTION_THRESHOLD)
                emotion = to_cosyvoice_emotion(
                    spec, default=emotion_default, strong_threshold=_thr)
                # 深夜悄悄话（④）：23–6 点整体 ×0.96（真人深夜说话轻而慢）
                speed = cosyvoice_speed(
                    spec, hour=datetime.datetime.now().hour)
                if _emo_ref_key:
                    # 情绪参考音已承担韵律情绪 → 标签归 neutral（纯保真路径）
                    emotion = "neutral"
                # 守卫：情感标签只有配合逐字稿才走服务端「混合保真」路径
                # （zero_shot 全条件+情感前缀）；没有逐字稿时发情感标签会掉进
                # instruct2（忽略语音条件→音色漂移）→ 强制 neutral 保音色。
                if emotion != "neutral" and not ref_text:
                    logger.debug(
                        "[tts] avatar_clone 无逐字稿 → 情感标签 %s 降为 neutral 保音色",
                        emotion)
                    emotion = "neutral"
                if not instruct and bool(cfg.get("dynamic_instruct", False)):
                    instruct = to_cosyvoice_instruct(
                        spec, seed_text=rv.text,
                        style=str(vp.get("instruct_style") or ""))
            except Exception:
                emotion = emotion_default

        # 副语言标记注入（仅本地 CosyVoice3）：基于**口语化后**的 synth_text。
        # hub 路径已在上方返回，不会走到这里。标记在 CosyVoice3 tokenizer 层消费
        # 绝不读出（2026-07-13 真机 STT 回转验证）。
        para_cfg = (cfg.get("paralinguistic")
                    if isinstance(cfg.get("paralinguistic"), dict) else {})
        if (spec is not None and para_cfg.get("enabled", False)
                and vp.get("paralinguistic", True) is not False):
            try:
                from src.ai.voice_emotion import inject_paralinguistic
                _before_para = synth_text
                synth_text = inject_paralinguistic(
                    synth_text, spec,
                    max_marks=int(para_cfg.get("max_marks", 2) or 2))
                if synth_text != _before_para:
                    rv.extra["paralinguistic"] = True
            except Exception:
                pass

        av_out = out.with_suffix(".wav")

        def _do_synth() -> None:
            ref_b64 = load_reference_b64(ref)
            if instruct:
                audio = client.tts_instruct(
                    synth_text, reference_audio_b64=ref_b64, instruct=instruct)
            else:
                audio = client.tts(
                    synth_text, reference_audio_b64=ref_b64,
                    reference_text=ref_text, emotion=emotion, speed=speed)
            av_out.write_bytes(audio)

        def _record_avatar(ok: bool, latency_ms: int = 0) -> None:
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                get_avatar_voice_stats().record_synth(
                    ok=ok, latency_ms=latency_ms,
                    channel=("instruct" if instruct else "emotion"),
                    emotion=emotion,
                    colloquial=bool(rv.extra.get("colloquial")),
                    colloquial_llm=bool(rv.extra.get("colloquial_llm")),
                    colloquial_generated=bool(rv.extra.get("colloquial_generated")),
                    paralinguistic=bool(rv.extra.get("paralinguistic")))
            except Exception:
                pass

        try:
            # 总预算 = 单请求超时 ×(1+重试) × 预估块数 + 余量；wait_for 只兜底极端卡死。
            # tts() 内部按 chunk_max_chars 切块**逐块** POST——旧公式没乘块数，长文在慢
            # GPU（3060 实测 RTF≈2.2）上必被误杀：2026-07-21 00:41 实测 169 字 3 块，
            # 服务端 85s 合成成功，程序 75s 预算先放弃 → 白合成 + 回落。
            try:
                _n_chunks = max(1, len(client._split(synth_text)))
            except Exception:
                _n_chunks = 1
            budget = (client.synth_timeout_sec
                      * (1 + max(0, client.retries)) * _n_chunks + 30)
            _rem = _remaining()
            if _rem is not None:
                if _rem < 3.0:
                    _record_avatar(False)
                    logger.warning(
                        "[tts] avatar_clone 剩余预算不足（%.1fs）→ 放弃本级",
                        max(_rem, 0.0))
                    if cloud_fallback:
                        return None
                    return _finalize_err("avatar_clone_no_budget")
                budget = min(budget, max(8.0, _rem))
            await asyncio.wait_for(asyncio.to_thread(_do_synth), timeout=budget)
        except Exception as ex:
            try:
                av_out.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            _record_avatar(False)
            # TimeoutError 的 str() 为空 → 旧日志打出 "failed ()" 无从排查；带上异常类型名
            _exs = f"{type(ex).__name__}: {ex}".rstrip(": ")
            if cloud_fallback:
                logger.warning("[tts] avatar_clone failed (%s) → 回落兜底", _exs)
                return None
            return _finalize_err(f"avatar_clone_failed:{_exs[:200]}")

        # 合成后 ASR 回转校验门（synth_verify）：复用进程内共享转写器抓偶发
        # 错别字/含混，超阈值同参数重合成取较优；未登记/异常 fail-open 零阻塞。
        if av_out.exists() and av_out.stat().st_size > 0:
            try:
                from src.voice_transcriber import get_shared_transcriber
                _sv = await verify_and_retry_synth(
                    av_out, synth_text, cfg.get("synth_verify"),
                    get_shared_transcriber(), _do_synth, budget=budget)
                if _sv:
                    rv.extra["synth_verify"] = _sv
            except Exception:
                pass

        if av_out.exists() and av_out.stat().st_size > 0:
            rv.ok = True
            rv.provider = "avatar_clone"
            rv.format = "wav"
            rv.audio_path = str(av_out)
            rv.extra["bytes"] = av_out.stat().st_size
            rv.extra["avatar_base_url"] = client.base_url
            rv.extra["avatar_emotion"] = instruct or emotion
            rv.extra["avatar_channel"] = "instruct" if instruct else "emotion"
            try:
                dur, src = compute_audio_duration_sec(str(av_out), "wav")
                rv.duration_sec = float(dur)
                rv.duration_source = str(src)
            except Exception:
                rv.duration_sec = -1.0
                rv.duration_source = "unknown"
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            _record_avatar(True, rv.latency_ms)
            return rv

        _record_avatar(False)
        if cloud_fallback:
            return None
        return _finalize_err("avatar_clone_empty")

    def _validate_voice_profile(self) -> None:
        if not bool(self.voice_profile.get("enabled", False)):
            return
        if not bool(self.voice_profile.get("owner_consent", False)):
            raise RuntimeError("voice_profile_requires_owner_consent")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        if not ref:
            raise RuntimeError("voice_profile_missing_reference_audio_path")
        if not Path(ref).is_file():
            raise RuntimeError(f"voice_profile_reference_audio_missing:{ref}")

    def _synthesize_sync(
        self, text: str, out: Path, voice: str, backend: Optional[str] = None,
        spec: Any = None,
    ) -> None:
        backend = (backend or self._effective_backend())
        if backend == "edge_tts":
            asyncio.run(self._edge_tts(text, out, voice, spec))
            return
        if backend == "pyttsx3":
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            if voice:
                for v in engine.getProperty("voices") or []:
                    if voice.lower() in (str(getattr(v, "id", "")) + str(getattr(v, "name", ""))).lower():
                        engine.setProperty("voice", getattr(v, "id", ""))
                        break
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            return
        if backend == "openai":
            from openai import OpenAI  # type: ignore

            if not self.api_key:
                raise RuntimeError("missing api_key for openai TTS")
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            client = OpenAI(**kwargs)
            req: Dict[str, Any] = {
                "model": self.model,
                "voice": voice or self.voice or "alloy",
                "input": text,
                "response_format": self.format,
            }
            # P1：情感 → instructions（在运营已配置的 instructions 之后追加，不覆盖）
            instr = self.instructions
            if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
                try:
                    from src.ai.voice_emotion import to_openai_instructions
                    instr = to_openai_instructions(spec, base=self.instructions)
                except Exception:
                    instr = self.instructions
            if instr:
                req["instructions"] = instr
            resp = client.audio.speech.create(**req)
            if hasattr(resp, "write_to_file"):
                resp.write_to_file(str(out))
            else:
                data = getattr(resp, "content", b"")
                out.write_bytes(data)
            return
        if backend == "voice_clone_command":
            self._synthesize_voice_clone_command(text, out, spec)
            return
        if backend == "coqui_http":
            self._synthesize_coqui_http(text, out)
            return
        if backend == "elevenlabs":
            self._synthesize_elevenlabs(text, out, voice, spec)
            return
        if backend == "disabled":
            raise RuntimeError("backend disabled")
        raise RuntimeError(f"unknown backend {backend}")

    def _synthesize_voice_clone_command(
        self, text: str, out: Path, spec: Any = None,
    ) -> None:
        self._validate_voice_profile()
        tpl = str(self.voice_profile.get("command_template") or "").strip()
        raw_args = self.voice_profile.get("command_args")
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("voice_profile_missing_command")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        speaker = str(self.voice_profile.get("speaker_id") or "my_voice").strip()
        # P5：情感 → Qwen ``instructions`` 自然语言声音指令（DashScope API 字段，
        # 不会被读出 → 零 garble，可常开）。在运营已配置的 instructions 后追加，不覆盖。
        instr = self.instructions
        if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
            try:
                from src.ai.voice_emotion import to_qwen_instructions
                instr = to_qwen_instructions(spec, base=self.instructions)
            except Exception:
                instr = self.instructions
        raw_values = {
            "text": text,
            "out": str(out),
            "reference_audio": ref,
            "speaker": speaker,
            "model": str(self.voice_profile.get("model") or self.model),
            "instructions": instr or "",
        }
        timeout = float(self.voice_profile.get("command_timeout_sec", 120) or 120)
        if isinstance(raw_args, list):
            cmd_args = [str(x).format(**raw_values) for x in raw_args]
            # 操作员未显式用 {instructions} 占位、但用的是自带 --instructions 的 qwen
            # 包装器，且本轮有情感指令 → 针对性自动补一段（不触碰其它克隆脚本）。
            joined = " ".join(str(x) for x in raw_args)
            if (instr and "{instructions}" not in joined
                    and "--instructions" not in cmd_args
                    and "qwen_tts_wrapper" in joined.lower()):
                cmd_args += ["--instructions", instr]
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd_args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        else:
            quoted_values = {k: shlex.quote(v) for k, v in raw_values.items()}
            cmd = tpl.format(**quoted_values)
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "")[:500]
            raise RuntimeError(f"voice_clone_command_failed:{msg}")

    def _synthesize_elevenlabs(
        self, text: str, out: Path, voice: str, spec: Any = None,
    ) -> None:
        """ElevenLabs v3 合成（情感 + 克隆音色）。voice_id 取 voice_profile.voice 优先。"""
        from src.ai.elevenlabs_client import ElevenLabsClient, output_format_for

        el = self.elevenlabs or {}
        # model：优先 elevenlabs.model_id；其次顶层 model（若以 eleven 开头）；否则默认 v3
        model_id = str(el.get("model_id") or "").strip()
        if not model_id and str(self.model or "").lower().startswith("eleven"):
            model_id = self.model
        client = ElevenLabsClient({
            "api_key": el.get("api_key") or self.api_key,
            "base_url": el.get("base_url") or "",
            "model_id": model_id or "eleven_v3",
            "timeout_sec": el.get("timeout_sec") or 120,
            "similarity_boost": el.get("similarity_boost") or 0.75,
        })
        # voice_id：人设 voice_profile.voice（云端音色 ID）优先，回落 voice/全局
        vp = self.voice_profile or {}
        voice_id = str(vp.get("voice") or voice or self.voice or "").strip()
        out_fmt = output_format_for(self.format)
        client.synthesize(text, voice_id, out, emotion=spec, output_format=out_fmt)

    def _synthesize_coqui_http(self, text: str, out: Path) -> None:
        """Call Coqui XTTS-v2 server (custom OpenAI-compatible API).

        voice_profile.enabled + reference_audio_path
            → POST /v1/audio/clone  (JSON: text + reference_audio_base64)
        otherwise
            → POST /v1/audio/speech (JSON: model + input + voice + language)
        """
        import base64 as _b64
        import json as _json
        import urllib.request as _ur

        base = (self.base_url or "http://127.0.0.1:7851").rstrip("/")
        # 快速可达性预检：主机离线时 ~3s 失败并触发兜底，避免 OS TCP 连接超时
        # （WinError 10060）拖到 ~21s。预检通过才走正常合成（合成本身仍给足超时）。
        _assert_http_reachable(base, timeout=3.0)
        vp = self.voice_profile
        fmt = (self.format or "wav").lower()
        language = str(vp.get("language") or "zh-cn")
        auth_header = f"Bearer {self.api_key or 'coqui'}"

        use_clone = (
            bool(vp.get("enabled"))
            and bool(vp.get("owner_consent"))
            and bool(vp.get("reference_audio_path"))
            and Path(str(vp.get("reference_audio_path", ""))).is_file()
        )

        if use_clone:
            ref_path = str(vp["reference_audio_path"])
            ref_b64 = _b64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
            payload = _json.dumps({
                "text": text,
                "language": language,
                "reference_audio_base64": ref_b64,
            }).encode()
            url = f"{base}/v1/audio/clone"
        else:
            voice_id = str(vp.get("speaker_id") or self.voice or "female_01")
            payload = _json.dumps({
                "model": self.model or "xtts_v2",
                "input": text,
                "voice": voice_id,
                "language": language,
                "response_format": fmt,
            }).encode()
            url = f"{base}/v1/audio/speech"

        req = _ur.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth_header,
            },
        )
        with _ur.urlopen(req, timeout=300) as resp:
            response_bytes = resp.read()
        if not response_bytes:
            raise RuntimeError("coqui_http: empty response from TTS server")
        # /v1/audio/clone returns JSON: {"audio_base64": "..."}
        # /v1/audio/speech returns raw audio bytes
        if use_clone:
            try:
                resp_json = _json.loads(response_bytes.decode("utf-8"))
                audio_b64 = resp_json.get("audio_base64") or resp_json.get("audio")
                if not audio_b64:
                    raise RuntimeError(f"coqui_http: no audio_base64 in response: {list(resp_json.keys())}")
                audio_bytes = _b64.b64decode(audio_b64)
            except (_json.JSONDecodeError, KeyError, ValueError) as e:
                raise RuntimeError(f"coqui_http: failed to parse clone response: {e}")
        else:
            audio_bytes = response_bytes
        out.write_bytes(audio_bytes)

    async def _edge_tts(
        self, text: str, out: Path, voice: str, spec: Any = None,
    ) -> None:
        import edge_tts  # type: ignore

        kwargs: Dict[str, Any] = {}
        # P1：情感 → edge_tts rate/pitch 近似情绪（neutral 不调，行为不变）
        if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
            try:
                from src.ai.voice_emotion import edge_prosody
                kwargs.update(edge_prosody(spec))
            except Exception:
                kwargs = {}
        # B62 最后防线：任何绕过 _run_backend 的调用同样不许把克隆名喂给 edge。
        communicate = edge_tts.Communicate(
            text, safe_edge_voice(voice or self.voice, self.fallback_voice),
            **kwargs)
        await communicate.save(str(out))


# ── P3-A: 音频时长测量 ──────────────────────────────────────────
# 目的：synthesize 成功后拿到 audio_path，立刻测 duration，交给上游做范围校验。
# 之前的 745:39 WAV bug 源于 DataSize=INT_MAX（头被填 0x7FFFFFFF）。
# 这里哪怕是 header 坏的文件，`wave.open()` 也会抛异常或返回极大值，
# 调用方应对照 voice_output.max_seconds 做硬上限，超了就回退文字。

def _duration_from_wave(path: str) -> float:
    """stdlib wave 模块解析 WAV：nframes / framerate。

    若 header 声明的 DataSize 异常（如 2147483647 = INT_MAX 的 bug），
    nframes 会被报为天文数字 —— 调用方据此可直接判无效。
    """
    import wave
    with wave.open(path, "rb") as w:
        frames = int(w.getnframes())
        rate = int(w.getframerate() or 0)
        if rate <= 0:
            return -1.0
        return float(frames) / float(rate)


def _duration_from_mp3(path: str) -> float:
    """轻量级 MP3 时长估算：扫描前几帧拿 bitrate，再 filesize / bitrate。

    对 CBR MP3 精度 ±1s；VBR 不够准但够做上限校验。
    无第三方依赖 —— mutagen / pydub 都不装。
    """
    try:
        import os
        size = os.path.getsize(path)
        if size <= 128:
            return -1.0
        # MPEG-1/2 Layer 3 bitrate table（kbps）
        bitrate_tab_v1_l3 = [
            None, 32, 40, 48, 56, 64, 80, 96,
            112, 128, 160, 192, 224, 256, 320, None,
        ]
        bitrate_tab_v2_l3 = [
            None, 8, 16, 24, 32, 40, 48, 56,
            64, 80, 96, 112, 128, 144, 160, None,
        ]
        samplerate_tab = {
            (3, 0): 44100, (3, 1): 48000, (3, 2): 32000,
            (2, 0): 22050, (2, 1): 24000, (2, 2): 16000,
            (0, 0): 11025, (0, 1): 12000, (0, 2): 8000,
        }
        with open(path, "rb") as f:
            head = f.read(10)
            # 跳过 ID3v2
            offset = 0
            if head[:3] == b"ID3":
                tag_size = (
                    (head[6] & 0x7F) << 21
                    | (head[7] & 0x7F) << 14
                    | (head[8] & 0x7F) << 7
                    | (head[9] & 0x7F)
                )
                offset = 10 + tag_size
                f.seek(offset)
            # 扫描同步字
            buf = f.read(4096)
            for i in range(len(buf) - 4):
                if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
                    b1 = buf[i + 1]
                    b2 = buf[i + 2]
                    version = (b1 >> 3) & 0x03  # 0=v2.5, 2=v2, 3=v1
                    layer = (b1 >> 1) & 0x03    # 1=layer3
                    if layer != 1:
                        continue
                    br_idx = (b2 >> 4) & 0x0F
                    sr_idx = (b2 >> 2) & 0x03
                    if br_idx in (0, 15) or sr_idx == 3:
                        continue
                    tab = bitrate_tab_v1_l3 if version == 3 else bitrate_tab_v2_l3
                    bitrate_kbps = tab[br_idx]
                    samplerate = samplerate_tab.get((version, sr_idx))
                    if not bitrate_kbps or not samplerate:
                        continue
                    # 音频 payload 长度（毛估：减去可能的 ID3v1 128 字节）
                    audio_bytes = size - offset
                    tail_chk = max(0, audio_bytes - 128)
                    if tail_chk > 0:
                        audio_bytes = tail_chk
                    return (audio_bytes * 8.0) / (bitrate_kbps * 1000.0)
        return -1.0
    except Exception:
        return -1.0


def compute_audio_duration_sec(
    path: str, fmt: str = "",
) -> tuple[float, str]:
    """测量音频文件时长，返回 (seconds, source_tag)。

    source_tag ∈ {"wave_header", "mp3_frame", "unknown"}；
    -1.0 表示未能测量，调用方应把此视为"不可信"。
    """
    if not path or not os.path.isfile(path):
        return -1.0, "unknown"
    fmt_low = (fmt or Path(path).suffix.lstrip(".")).lower()
    # WAV 先试 stdlib
    if fmt_low in ("wav", "wave") or path.lower().endswith(".wav"):
        try:
            d = _duration_from_wave(path)
            if d > 0:
                return d, "wave_header"
        except Exception:
            pass
    # MP3
    if fmt_low == "mp3" or path.lower().endswith(".mp3"):
        d = _duration_from_mp3(path)
        if d > 0:
            return d, "mp3_frame"
    # 其他格式（opus/m4a/aac）暂无轻量解析器
    return -1.0, "unknown"


_tts_singleton: Optional[TTSPipeline] = None


def get_tts_pipeline(cfg: Optional[Dict[str, Any]] = None) -> TTSPipeline:
    global _tts_singleton
    if _tts_singleton is None:
        _tts_singleton = TTSPipeline(cfg or {})
    return _tts_singleton


def reset_tts_pipeline() -> None:
    global _tts_singleton
    _tts_singleton = None
