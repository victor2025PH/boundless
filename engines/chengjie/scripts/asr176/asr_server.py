"""GPU audio service for 192.168.0.176 (RTX 5090) — ASR + speech emotion.

Contract:
    POST /v1/audio/transcriptions   multipart/form-data   (OpenAI-compatible,
        matches src/voice_transcriber.py::OpenAITranscriber)
        file:            audio file (ogg/opus from Telegram, wav, mp3, ...)
        model:           ignored (single loaded model serves all requests)
        language:        optional ISO code; omit/auto -> autodetect
        prompt:          optional initial_prompt (hotwords / domain terms; OpenAI
                         standard field; ASR P0 2026-09-12 -- the client had a
                         hotword list for months that never reached this server)
        response_format: "text" -> text/plain body; "json"/default -> {"text": ...};
                         "verbose_json" -> {"text","language","duration",
                         "segments":[{start,end,text}...],
                         "language_probability","avg_logprob","no_speech_prob",
                         "compression_ratio","vad"}  (P3 SRT consumers +
                         ASR P0 confidence for the client-side suspect gate)
    POST /v1/audio/emotion          multipart/form-data
        file: audio file -> {"labels":[...9 emotion2vec labels...],
                             "scores":[...], "model":..., "latency_ms":...}
        Raw label/score arrays only; canonical mapping stays client-side in
        src/ai/speech_emotion.py (single source of truth).
    GET /health -> {"status":"ok","model":...,"device":...,"ser_model":...}

Runs faster-whisper large-v3-turbo + funasr emotion2vec on CUDA. Env overrides:
    AITR_ASR_MODEL   (default large-v3-turbo)
    AITR_ASR_DEVICE  (default cuda)
    AITR_ASR_COMPUTE (default float16; fallback int8_float16 on OOM is manual)
    AITR_ASR_PORT    (default 8765)
    AITR_SER_MODEL   (default iic/emotion2vec_plus_large; "off" disables endpoint)
    AITR_SER_DEVICE  (default cuda)
    AITR_WARMUP      (default 1: preload ASR+SER models in background at boot,
                      removing the ~15s/~6s first-request cold start after restarts)
    AITR_ASR_BEAM            (default 5)
    AITR_ASR_SHORT_RESCUE_SEC (default 2.0: a clip shorter than this that VAD judged
                              speech-free gets ONE extra decode without VAD, kept only
                              if confident -- see accept_short_rescue; 0 disables)
    AITR_ASR_SHORT_RESCUE_MIN_LOGPROB / _MIN_LANGPROB / _MAX_CR (default -0.6 / 0.7 / 2.0)
    AITR_ASR_VAD_MIN_SILENCE_MS (default 300 = what the old server passed)
    AITR_ASR_VAD_THRESHOLD / AITR_ASR_VAD_SPEECH_PAD_MS / AITR_ASR_VAD_MIN_SPEECH_MS
                             (unset = library defaults; A/B showed 0.35 / 100 harm, and
                              250 for min_speech (an OLD library default) drops "嗯嗯")
    AITR_ASR_DOWNLOAD_ROOT   (optional: faster-whisper download_root / HF cache dir)
    MODELSCOPE_CACHE should point at the machine-wide cache (start_asr.ps1 sets
    D:\\cache\\modelscope); prefetch via prefetch_emotion.py as the interactive
    user — SYSTEM-context downloads are unreliable on this host.

Design notes:
- Single global model per task, requests serialized through locks: RTX 5090
  handles a 10s clip in well under 1s, so queues are simpler and safer than
  multi-instance VRAM juggling alongside Ollama models.
- VAD filter on for ASR: Telegram voice notes often carry leading/trailing
  silence; VAD trims it, which also suppresses whisper hallucination on silence.
  A short clip VAD judged speech-free gets one confident-only rescue pass
  without VAD (AITR_ASR_SHORT_RESCUE_*), never a blanket bypass.
- Decoding discipline (ASR P0 2026-09-12): condition_on_previous_text=False
  (no cross-segment hallucination carry-over -- the in-process client already
  did this, the server never did), whisper's own no_speech / log_prob /
  compression_ratio thresholds set explicitly, initial_prompt from ``prompt``.
- Emotion model load/inference failures degrade to HTTP 500; the client
  (SpeechEmotionRecognizer) falls back to local CPU funasr on any error.
"""

import asyncio
import logging
import os
import tempfile
import time

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("asr176")

MODEL_NAME = os.environ.get("AITR_ASR_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("AITR_ASR_DEVICE", "cuda")
COMPUTE = os.environ.get("AITR_ASR_COMPUTE", "float16")
# SER 模型默认取**本地目录**（117 下载后 scp 过来；两边 hub 在本机网络都不可靠：
# modelscope 实测 179kB/s、hf-mirror 直接失败）。目录不存在时 funasr 会按 hub 名解析。
SER_MODEL = os.environ.get("AITR_SER_MODEL", r"C:\aitr_asr\models\emotion2vec_plus_large")
SER_HUB = os.environ.get("AITR_SER_HUB", "hf")   # 仅当 SER_MODEL 非本地路径时生效
SER_DEVICE = os.environ.get("AITR_SER_DEVICE", "cuda")
DOWNLOAD_ROOT = os.environ.get("AITR_ASR_DOWNLOAD_ROOT", "").strip() or None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return int(default)


BEAM = max(1, _env_int("AITR_ASR_BEAM", 5))
# VAD parameters: exactly what the old server passed (min_silence 300) on top of the
# library defaults -- everything else is only sent when its env knob is explicitly set.
# 2026-09-12 A/B on 198 (same model, real clips): loosening to threshold 0.35 /
# min_speech 100 turned the probe fixture from a stable correct transcript into
# nondeterministic "。。。。。"/"啊啊啊啊啊…"; "pinning" min_speech to 250 (the *old*
# library default -- faster-whisper 1.2.1 uses 0) dropped the leading "嗯嗯" chunk and
# gave a stable but wrong "這件衣服也不錯". Do not pin what you have not measured.
VAD_MIN_SILENCE_MS = _env_int("AITR_ASR_VAD_MIN_SILENCE_MS", 300)
VAD_THRESHOLD = _env_float("AITR_ASR_VAD_THRESHOLD", 0.5) if os.environ.get("AITR_ASR_VAD_THRESHOLD") else None
VAD_SPEECH_PAD_MS = _env_int("AITR_ASR_VAD_SPEECH_PAD_MS", 400) if os.environ.get("AITR_ASR_VAD_SPEECH_PAD_MS") else None
VAD_MIN_SPEECH_MS = _env_int("AITR_ASR_VAD_MIN_SPEECH_MS", 0) if os.environ.get("AITR_ASR_VAD_MIN_SPEECH_MS") else None
# Short-clip rescue (2-pass): VAD found *no* speech in a clip shorter than this ->
# decode once more without VAD and accept only a *confident* result. Same A/B: a 1.47s
# clip decoded without VAD gave "Thank you." at lang_prob 0.42 / logprob -0.90 (whisper
# hallucination) -> the confidence floor below rejects exactly that; a real "嗯/好/在吗"
# decoded confidently gets rescued instead of dying as "[语音]".
SHORT_RESCUE_SEC = _env_float("AITR_ASR_SHORT_RESCUE_SEC", 2.0)
SHORT_RESCUE_MIN_LOGPROB = _env_float("AITR_ASR_SHORT_RESCUE_MIN_LOGPROB", -0.6)
SHORT_RESCUE_MIN_LANGPROB = _env_float("AITR_ASR_SHORT_RESCUE_MIN_LANGPROB", 0.7)
SHORT_RESCUE_MAX_CR = _env_float("AITR_ASR_SHORT_RESCUE_MAX_CR", 2.0)
SAMPLE_RATE = 16000
PROMPT_MAX_CHARS = 400


def decode_options(prompt=None, *, beam=None, vad=True, vad_threshold=None,
                   vad_min_silence_ms=None, vad_speech_pad_ms=None, vad_min_speech_ms=None):
    """Pure: transcribe() kwargs for one pass (unit-testable without a model).

    - ``vad``: VAD filter on/off for this pass (the short-clip rescue pass is the only
      caller that turns it off);
    - condition_on_previous_text=False always (cross-segment hallucination carry-over;
      the in-process client set this for months, the server never did);
    - whisper's own quality thresholds pinned to the library defaults;
    - ``prompt`` -> initial_prompt (trimmed to PROMPT_MAX_CHARS; empty -> omitted).
      NB 2026-09-12 A/B: any prompt form (list / sentence / native hotwords) damaged the
      marginal 4.25s fixture and only added punctuation on clean clips -> the client
      keeps it off by default; the server merely honours the field when sent.
    """
    beam = int(BEAM if beam is None else beam)
    opts = {
        "beam_size": max(1, beam),
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "compression_ratio_threshold": 2.4,
        "vad_filter": bool(vad),
    }
    if vad:
        vp = {"min_silence_duration_ms": int(
            VAD_MIN_SILENCE_MS if vad_min_silence_ms is None else vad_min_silence_ms)}
        thr = VAD_THRESHOLD if vad_threshold is None else vad_threshold
        if thr is not None:
            vp["threshold"] = float(thr)
        pad = VAD_SPEECH_PAD_MS if vad_speech_pad_ms is None else vad_speech_pad_ms
        if pad is not None:
            vp["speech_pad_ms"] = int(pad)
        msd = VAD_MIN_SPEECH_MS if vad_min_speech_ms is None else vad_min_speech_ms
        if msd is not None:
            vp["min_speech_duration_ms"] = int(msd)
        opts["vad_parameters"] = vp
    p = str(prompt or "").strip()
    if p:
        opts["initial_prompt"] = p[:PROMPT_MAX_CHARS]
    return opts


def accept_short_rescue(text, conf, *, min_logprob=None, min_langprob=None, max_cr=None):
    """Pure: should a no-VAD rescue pass on a short clip be trusted?

    Requires non-empty text AND avg_logprob >= floor AND language_probability >= floor
    AND compression_ratio <= cap. Any missing metric -> reject (no evidence, no rescue).
    """
    if not str(text or "").strip():
        return False
    c = conf if isinstance(conf, dict) else {}
    lp = c.get("avg_logprob")
    pr = c.get("language_probability")
    cr = c.get("compression_ratio")
    if not isinstance(lp, (int, float)) or not isinstance(pr, (int, float)):
        return False
    if lp < float(SHORT_RESCUE_MIN_LOGPROB if min_logprob is None else min_logprob):
        return False
    if pr < float(SHORT_RESCUE_MIN_LANGPROB if min_langprob is None else min_langprob):
        return False
    if isinstance(cr, (int, float)) and cr > float(SHORT_RESCUE_MAX_CR if max_cr is None else max_cr):
        return False
    return True


def summarize_segments(segments, info=None):
    """Pure: iterate faster-whisper segments once -> (text, seg_list, confidence dict).

    confidence: avg_logprob = mean over segments, no_speech_prob = max,
    compression_ratio = max, language / language_probability / duration from info.
    Missing values are simply absent (never guessed).
    """
    seg_list, parts, lps, nsps, crs = [], [], [], [], []
    for s in segments:
        txt = getattr(s, "text", None)
        if txt is None and isinstance(s, dict):
            txt = s.get("text")
        txt = str(txt or "")
        parts.append(txt)
        st = txt.strip()

        def _g(name):
            v = getattr(s, name, None) if not isinstance(s, dict) else s.get(name)
            return float(v) if isinstance(v, (int, float)) else None

        if st:
            seg_list.append({
                "start": round(_g("start") or 0.0, 3),
                "end": round(_g("end") or 0.0, 3),
                "text": st,
            })
        for arr, key in ((lps, "avg_logprob"), (nsps, "no_speech_prob"), (crs, "compression_ratio")):
            v = _g(key)
            if v is not None:
                arr.append(v)
    text = "".join(parts).strip()
    conf = {}
    if lps:
        conf["avg_logprob"] = round(sum(lps) / len(lps), 4)
    if nsps:
        conf["no_speech_prob"] = round(max(nsps), 4)
    if crs:
        conf["compression_ratio"] = round(max(crs), 4)
    if info is not None:
        lang = getattr(info, "language", None)
        if lang:
            conf["language"] = str(lang)
        lp = getattr(info, "language_probability", None)
        if isinstance(lp, (int, float)):
            conf["language_probability"] = round(float(lp), 4)
        d = getattr(info, "duration", None)
        if isinstance(d, (int, float)) and d > 0:
            conf["duration"] = round(float(d), 3)
    return text, seg_list, conf


app = FastAPI(title="aitr-asr-176")
_model = None
_model_lock = asyncio.Lock()
_infer_lock = asyncio.Lock()
_ser_model = None
_ser_model_lock = asyncio.Lock()
_ser_infer_lock = asyncio.Lock()


async def _get_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        t0 = time.time()
        log.info("loading %s device=%s compute=%s ...", MODEL_NAME, DEVICE, COMPUTE)
        kwargs = {"device": DEVICE, "compute_type": COMPUTE}
        if DOWNLOAD_ROOT:
            kwargs["download_root"] = DOWNLOAD_ROOT
        _model = await asyncio.to_thread(WhisperModel, MODEL_NAME, **kwargs)
        log.info("model loaded in %.1fs", time.time() - t0)
        return _model


def _transcribe_sync(model, path: str, language, prompt=None):
    """Decode audio once (duration known up-front), transcribe with VAD; on a VAD-empty
    *short* clip run one rescue pass without VAD and keep it only if confident.

    Returns (text, info, seg_list, conf) — conf carries duration / vad / rescue plus the
    per-segment confidence aggregates (see summarize_segments).
    """
    from faster_whisper import decode_audio

    audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
    duration = float(len(audio)) / float(SAMPLE_RATE)
    opts = decode_options(prompt, vad=True)
    segments, info = model.transcribe(audio, language=language, **opts)
    # P3 2026-08-18: keep per-segment timing (SRT subtitle consumers request
    # response_format=verbose_json; default json response unchanged).
    text, seg_list, conf = summarize_segments(segments, info)
    conf["vad"] = True
    conf["rescue"] = False
    if not text and 0 < duration < SHORT_RESCUE_SEC:
        opts2 = decode_options(prompt, vad=False)
        segments2, info2 = model.transcribe(audio, language=language, **opts2)
        text2, seg_list2, conf2 = summarize_segments(segments2, info2)
        if accept_short_rescue(text2, conf2):
            text, seg_list, conf, info = text2, seg_list2, conf2, info2
            conf["vad"] = False
            conf["rescue"] = True
        else:
            log.info("short-clip rescue rejected dur=%.2fs text=%r lp=%s p=%s cr=%s",
                     duration, text2[:40], conf2.get("avg_logprob"),
                     conf2.get("language_probability"), conf2.get("compression_ratio"))
            conf["rescue"] = False
    conf.setdefault("duration", round(duration, 3))
    return text, info, seg_list, conf


async def _get_ser_model():
    global _ser_model
    if _ser_model is not None:
        return _ser_model
    async with _ser_model_lock:
        if _ser_model is not None:
            return _ser_model
        from funasr import AutoModel

        t0 = time.time()
        log.info("loading SER %s hub=%s device=%s ...", SER_MODEL, SER_HUB, SER_DEVICE)
        _ser_model = await asyncio.to_thread(
            AutoModel, model=SER_MODEL, hub=SER_HUB, device=SER_DEVICE,
            disable_update=True,
        )
        log.info("SER model loaded in %.1fs", time.time() - t0)
        return _ser_model


def _ser_sync(model, path: str):
    res = model.generate(path, granularity="utterance", extract_embedding=False)
    item = res[0] if isinstance(res, (list, tuple)) and res else res
    labels = list((item or {}).get("labels") or [])
    scores = [float(s) for s in ((item or {}).get("scores") or [])]
    return labels, scores


@app.on_event("startup")
async def _warmup():
    """Preload models in the background so the first real request is already hot.

    Best-effort: any load failure is logged and left for the per-request lazy
    path to retry (e.g. transient GPU/driver hiccup at boot).
    """
    if os.environ.get("AITR_WARMUP", "1").strip().lower() in ("0", "false", "off"):
        return

    async def _bg():
        try:
            await _get_model()
        except Exception:
            log.exception("ASR warmup failed (lazy path will retry)")
        if SER_MODEL.lower() not in ("off", "none", ""):
            try:
                await _get_ser_model()
            except Exception:
                log.exception("SER warmup failed (lazy path will retry)")
        log.info("warmup done asr=%s ser=%s", _model is not None, _ser_model is not None)

    asyncio.get_running_loop().create_task(_bg())


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_NAME, "device": DEVICE,
            "compute": COMPUTE,
            "ser_model": "" if SER_MODEL.lower() in ("off", "none", "") else SER_MODEL,
            "asr_loaded": _model is not None, "ser_loaded": _ser_model is not None,
            # ASR P0 decode discipline visible to probes (which build is running)
            "beam": BEAM, "short_rescue_sec": SHORT_RESCUE_SEC,
            "vad_min_silence_ms": VAD_MIN_SILENCE_MS,
            "vad_threshold": VAD_THRESHOLD, "prompt_supported": True}


@app.post("/v1/audio/emotion")
async def emotion(file: UploadFile = File(...)):
    if SER_MODEL.lower() in ("off", "none", ""):
        return JSONResponse({"error": {"message": "ser disabled"}}, status_code=503)
    suffix = os.path.splitext(file.filename or "audio.ogg")[1] or ".ogg"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        m = await _get_ser_model()
        t0 = time.time()
        async with _ser_infer_lock:
            labels, scores = await asyncio.to_thread(_ser_sync, m, tmp.name)
        ms = int((time.time() - t0) * 1000)
        log.info("emotion %s bytes -> top=%s elapsed=%dms",
                 len(data), labels[0] if labels else "?", ms)
        return {"labels": labels, "scores": scores,
                "model": SER_MODEL, "latency_ms": ms}
    except Exception as e:  # noqa: BLE001 - client falls back to local CPU on 500
        log.exception("emotion recognition failed")
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(None),
    language: str = Form(None),
    prompt: str = Form(None),
    response_format: str = Form("json"),
):
    lang = (language or "").strip().lower() or None
    if lang in ("auto", "none", "null"):
        lang = None
    prompt_s = (prompt or "").strip()[:PROMPT_MAX_CHARS] or None

    suffix = os.path.splitext(file.filename or "audio.ogg")[1] or ".ogg"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        m = await _get_model()
        t0 = time.time()
        async with _infer_lock:
            text, info, seg_list, conf = await asyncio.to_thread(
                _transcribe_sync, m, tmp.name, lang, prompt_s)
        log.info(
            "transcribed %s bytes lang=%s->%s(p=%s) dur=%.1fs vad=%s rescue=%s prompt=%s "
            "logprob=%s nsp=%s elapsed=%.2fs chars=%d",
            len(data), lang or "auto", conf.get("language", "?"),
            conf.get("language_probability", "-"), conf.get("duration", 0.0),
            "on" if conf.get("vad") else "off", "yes" if conf.get("rescue") else "no",
            "yes" if prompt_s else "no",
            conf.get("avg_logprob", "-"), conf.get("no_speech_prob", "-"),
            time.time() - t0, len(text),
        )
    except Exception as e:  # noqa: BLE001 - surface as 500 with reason
        log.exception("transcription failed")
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    fmt = (response_format or "").lower()
    if fmt == "text":
        return PlainTextResponse(text)
    if fmt == "verbose_json":
        # OpenAI verbose_json 兼容子集：text/language/duration/segments（SRT 消费方所需）
        # + ASR P0 置信度聚合（客户端 OpenAITranscriber 据此做 no_speech 闸 / 低置信标记 /
        # 语种先验复核）。字段缺失＝本次算不出，客户端按「无证据」处理。
        out = {
            "text": text,
            "language": str(conf.get("language") or getattr(info, "language", "") or ""),
            "duration": float(conf.get("duration") or getattr(info, "duration", 0.0) or 0.0),
            "segments": seg_list,
            "vad": bool(conf.get("vad")),
            "rescue": bool(conf.get("rescue")),
        }
        for k in ("language_probability", "avg_logprob", "no_speech_prob", "compression_ratio"):
            if k in conf:
                out[k] = conf[k]
        return out
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AITR_ASR_PORT", "8765")))
