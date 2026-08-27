"""P14-B/P15-A/P15-B: Shared TTS approval-preview generator.

Extracted from LINE and WA runners to eliminate ~40 lines of duplicate code.
P15-A: lazy auto-cleanup of tmp_tts_preview/ (≤once per 3h, via run_in_executor).
P15-B: 120s timeout around synthesize() to prevent semaphore starvation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from src.ai.tts_pipeline import get_tts_pipeline
except ImportError:
    get_tts_pipeline = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

AILANGS_TO_XTTS: dict[str, str] = {
    "zh": "zh-cn", "zh-cn": "zh-cn", "zh-tw": "zh-cn",
    "en": "en", "ja": "ja", "ko": "ko", "de": "de",
    "fr": "fr", "es": "es", "ru": "ru", "ar": "ar",
    "hi": "hi", "it": "it", "pt": "pt", "nl": "nl",
    "pl": "pl", "tr": "tr", "cs": "cs", "hu": "hu",
}

# P15-A: lazy cleanup state
_TTS_DIR = Path("tmp_tts_preview")
_CLEANUP_INTERVAL_SEC: float = 3 * 3600      # run at most once per 3 hours
_DEFAULT_MAX_AGE_SEC: float = 24 * 3600      # keep files up to 24 hours
_last_cleanup_ts: float = 0.0
_SYNTHESIZE_TIMEOUT_SEC: float = 120.0       # P15-B: hard timeout per synthesis


def cleanup_tts_previews(max_age_sec: float = _DEFAULT_MAX_AGE_SEC) -> int:
    """P15-A: Delete WAV files older than max_age_sec from tmp_tts_preview/.

    Safe to call concurrently — deletes are idempotent.
    Returns the number of files removed.
    """
    removed = 0
    if not _TTS_DIR.exists():
        return 0
    cutoff = time.time() - max_age_sec
    for f in _TTS_DIR.iterdir():
        # .json = 复用契约 sidecar（见 record_preview_meta），随音频同窗清理
        if f.suffix in (".wav", ".mp3", ".ogg", ".json") and f.is_file():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
    if removed:
        logger.info("tts_preview cleanup: removed %d old file(s)", removed)
    return removed


# ── 「所听即所发」复用契约（P1 2026-08-05）────────────────────────────────────
# 试听（/api/voice/tts-test）与发送（/api/unified-inbox/send-voice）此前是两次
# 独立合成：坐席听到 A 声、客户可能收到 B 声（克隆链中途恢复/掉线时 provider
# 漂移），且同一句话烧两份 TTS/字符额度。契约＝试听落盘时写 <音频名>.json
# sidecar（文本指纹+音色键+元数据），发送带回 filename，服务端**验完整性后
# 直接把试听音频送进出站管线**。任何校验不过一律回落现场合成——复用是省钱
# 与一致性优化，绝不能成为发送失败的新原因。
#
# persona_key 用**请求侧**的 persona_id（含空串=跟随会话），而非解析后的
# 人设：试听与发送以同一组请求入参解析音色（「试听=发送」契约），请求键相同
# ⇒ 解析结果相同；解析后的真实人设/后端记进 meta 只作观测与镜像徽标。

_PREVIEW_NAME_RE = re.compile(r"^ttspreview-[0-9a-f]{10}\.(mp3|ogg|wav)$")
REUSE_MAX_AGE_SEC: float = 2 * 3600.0   # 同文本同音色的音频不会因时间变质，TTL 只为兜异常

# 复用观测（进程级，风格对齐 outbound_translation_stats）：从第一天就积累
# 「记了多少契约 / 命中多少 / 未命中卡在哪一环」——miss 原因分布直接指导调参
# （expired 多=坐席隔久才发该放宽 TTL；text_mismatch 多=改稿忘重生成该强化引导）。
_REUSE_STATS: Dict[str, Any] = {"recorded": 0, "hits": 0, "misses": {}}
_MISS_REASON_CAP = 16   # 原因种类有限且代码可控，cap 仅防未来枚举失控


def _record_miss(reason: str) -> None:
    r = str(reason or "unknown")
    m = _REUSE_STATS["misses"]
    if r in m or len(m) < _MISS_REASON_CAP:
        m[r] = int(m.get(r, 0)) + 1


def reuse_stats_snapshot() -> Dict[str, Any]:
    """观测快照（无敏感字段）：hits/recorded/misses{reason: n}/hit_rate。"""
    misses = dict(_REUSE_STATS["misses"])
    hits = int(_REUSE_STATS["hits"])
    attempts = hits + sum(misses.values())
    return {
        "recorded": int(_REUSE_STATS["recorded"]),
        "hits": hits,
        "misses": misses,
        "attempts": attempts,
        "hit_rate": round(hits / attempts, 4) if attempts else None,
    }


def preview_text_key(text: Any) -> str:
    """试听/发送两侧共用的文本指纹（两侧路由均已 strip，这里再 strip 一次防漂移）。"""
    return hashlib.sha1(str(text or "").strip().encode("utf-8")).hexdigest()


def record_preview_meta(filename: str, *, text: Any, persona_key: str,
                        meta: Optional[Dict[str, Any]] = None,
                        target_lang: str = "") -> bool:
    """试听成功后登记复用 sidecar（best-effort：失败只丢复用资格，不影响试听）。

    ``target_lang``（P0-V2 译声）：试听时的**已解析**目标语（''=未翻译）。文本
    指纹仍按**请求原文**记（试听=发送以同一组请求入参解析），语言单独成维度
    ——否则「试听日语 → 切韩语发送」会按 text+persona 命中而把日语音频发出去。
    调用方负责传入已归一的语种码（本模块只做 strip/lower，不引翻译栈依赖）。
    """
    fn = str(filename or "").strip()
    if not _PREVIEW_NAME_RE.match(fn):
        return False
    try:
        payload = {
            "v": 1,
            "text_sha1": preview_text_key(text),
            "persona_key": str(persona_key or ""),
            "target_lang": str(target_lang or "").strip().lower(),
            "created_ts": time.time(),
            "meta": dict(meta or {}),
        }
        side = _TTS_DIR / (fn + ".json")
        side.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _REUSE_STATS["recorded"] = int(_REUSE_STATS["recorded"]) + 1
        return True
    except Exception:
        logger.debug("record_preview_meta 失败（放弃复用资格）", exc_info=True)
        return False


def resolve_reusable_preview(
    filename: str, *, text: Any, persona_key: str,
    max_age_sec: float = REUSE_MAX_AGE_SEC, target_lang: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """发送侧校验入口（带观测计数）：命中/未命中原因自动进 reuse_stats_snapshot。"""
    info, why = _resolve_reusable_preview(
        filename, text=text, persona_key=persona_key, max_age_sec=max_age_sec,
        target_lang=target_lang)
    if info is not None:
        _REUSE_STATS["hits"] = int(_REUSE_STATS["hits"]) + 1
    else:
        _record_miss(why)
    return info, why


def _resolve_reusable_preview(
    filename: str, *, text: Any, persona_key: str,
    max_age_sec: float = REUSE_MAX_AGE_SEC, target_lang: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """发送侧校验：返回 ``({"path": Path, "meta": dict}, "")`` 或 ``(None, 原因)``。

    校验链（宁可回落合成不可发错内容）：文件名白名单（无路径分隔符，防穿越）→
    音频在 → sidecar 在且可解析 → 音色键一致 → 文本指纹一致 → **目标语一致**
    （P0-V2 译声维度；旧 sidecar 无键按 '' 兼容=只匹配未翻译发送）→ 未过期 → 非空壳。
    """
    fn = str(filename or "").strip()
    if not _PREVIEW_NAME_RE.match(fn):
        return None, "bad_name"
    audio = _TTS_DIR / fn
    if not audio.is_file():
        return None, "missing_audio"
    side = _TTS_DIR / (fn + ".json")
    if not side.is_file():
        return None, "no_sidecar"
    try:
        payload = json.loads(side.read_text(encoding="utf-8"))
    except Exception:
        return None, "bad_sidecar"
    if str(payload.get("persona_key") or "") != str(persona_key or ""):
        return None, "persona_mismatch"
    if str(payload.get("text_sha1") or "") != preview_text_key(text):
        return None, "text_mismatch"
    if (str(payload.get("target_lang") or "").strip().lower()
            != str(target_lang or "").strip().lower()):
        return None, "lang_mismatch"
    try:
        age = time.time() - float(payload.get("created_ts") or 0.0)
    except (TypeError, ValueError):
        return None, "bad_sidecar"
    if age < 0 or age > max_age_sec:
        return None, "expired"
    try:
        if audio.stat().st_size < 512:
            return None, "too_small"
    except OSError:
        return None, "missing_audio"
    return {"path": audio, "meta": dict(payload.get("meta") or {})}, ""


def _maybe_trigger_cleanup() -> None:
    """P15-A: Trigger cleanup at most once per _CLEANUP_INTERVAL_SEC.

    Called synchronously from generate_approval_tts before the first await,
    so there is no yield-point between the check and the update — asyncio
    cooperative scheduling guarantees atomicity here.
    """
    global _last_cleanup_ts
    now = time.monotonic()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL_SEC:
        return
    _last_cleanup_ts = now  # mark before await so concurrent calls skip


async def generate_approval_tts(
    pending_id: int,
    reply_text: str,
    reply_lang: str,
    *,
    voice_cfg: dict,
    state_store: Any,
    semaphore: asyncio.Semaphore,
    fname_prefix: str = "tts",
) -> None:
    """Generate a TTS preview WAV for an approval-queue pending row.

    - Writes the file URL back via state_store.update_pending_tts_path()
    - On any failure writes "ERROR" sentinel so the UI can offer a retry button
    - Serialised via semaphore to prevent simultaneous GPU/CPU model contention
    - P15-B: synthesize() is wrapped with 120s timeout — timeout releases the
      semaphore immediately so subsequent tasks are never permanently blocked
    - P15-A: lazily triggers cleanup of old preview files (once per 3 hours)
    """
    # P15-A: lazy cleanup — no yield point between check+mark, so safe
    _should_clean = time.monotonic() - _last_cleanup_ts >= _CLEANUP_INTERVAL_SEC
    if _should_clean:
        _maybe_trigger_cleanup()

    try:
        import uuid as _uuid

        tts_lang = AILANGS_TO_XTTS.get(str(reply_lang or "zh").lower(), "zh-cn")
        pipeline = get_tts_pipeline(voice_cfg)  # type: ignore[misc]
        _TTS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{fname_prefix}-{_uuid.uuid4().hex[:10]}.wav"
        out_path = _TTS_DIR / fname
        text_trunc = str(reply_text or "")[:400].strip()
        if not text_trunc:
            return

        # P15-A: run cleanup in background after we have an executor reference
        loop = asyncio.get_event_loop()
        if _should_clean:
            asyncio.ensure_future(loop.run_in_executor(None, cleanup_tts_previews))

        async with semaphore:
            # P15-B: hard timeout — prevents starvation if model hangs
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: pipeline.synthesize(text_trunc, str(out_path), lang=tts_lang),
                    ),
                    timeout=_SYNTHESIZE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"TTS synthesis timed out (>{_SYNTHESIZE_TIMEOUT_SEC:.0f}s)"
                )

        _sz = out_path.stat().st_size if out_path.exists() else 0
        if _sz < 512:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"TTS output too small ({_sz}B), discarded")
        state_store.update_pending_tts_path(pending_id, f"/api/voice/tts-file/{fname}")
        logger.debug(
            "approval TTS generated: pending=%d lang=%s size=%d fname=%s",
            pending_id, tts_lang, _sz, fname,
        )
    except Exception as _e:
        # P20-A: classify error type for better UX
        err_str = str(_e).lower()
        if "timeout" in err_str or "timed out" in err_str:
            err_code = "ERROR:timeout"
        elif "disk" in err_str or "no space" in err_str or "i/o" in err_str or "permission" in err_str:
            err_code = "ERROR:disk"
        elif "model" in err_str or "cuda" in err_str or "gpu" in err_str or "cuda out of memory" in err_str:
            err_code = "ERROR:model"
        else:
            err_code = "ERROR:unknown"
        logger.debug("approval TTS generation failed: %s", err_code, exc_info=True)
        try:
            state_store.update_pending_tts_path(pending_id, err_code)
        except Exception:
            pass
