"""
singing_server.py — Phase 3: GPT-SoVITS v4 高质量TTS/歌词朗读服务
端口: 7853

功能:
  - /v1/tts          基础TTS (使用参考音频克隆音色)
  - /v1/tts/sing     歌词模式TTS (慢速、抑扬顿挫，配合歌词节奏)
  - /health          健康检查
  - /v1/status       模型状态

GPT-SoVITS v4 推理流程:
  TTS.run(inputs) 是 generator，yield (sr: int, audio: np.ndarray[int16])
  累积后用 soundfile 写 WAV bytes
"""
import os, sys, io, base64, time, threading, logging, tempfile
import numpy as np

# ── 路径配置 ──────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
GPTSVITS_DIR = os.path.join(BASE_DIR, "GPT-SoVITS")

# GPT-SoVITS 内部模块（AR/BigVGAN/feature_extractor 等）以 GPT_SoVITS/ 为根
GPTSVITS_INNER = os.path.join(GPTSVITS_DIR, "GPT_SoVITS")
os.chdir(GPTSVITS_DIR)
sys.path.insert(0, GPTSVITS_INNER)   # AR, BigVGAN, feature_extractor, module...
sys.path.insert(0, GPTSVITS_DIR)     # GPT_SoVITS package (顶层)

# ── FastAPI ──────────────────────────────────────────────────────────
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Singing] %(message)s")
logger = logging.getLogger("singing")

app = FastAPI(title="GPT-SoVITS Singing/TTS Server", version="1.0.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="singing")

# ── 全局模型状态 ──────────────────────────────────────────────────────
_lock          = threading.Lock()
_models_loaded = False
_load_error    = ""
_tts_pipeline  = None   # GPT-SoVITS TTS pipeline 实例

# 备用参考音频（CosyVoice 仓库里的中文示例音频）
FALLBACK_REF_AUDIO = os.path.join(BASE_DIR, "CosyVoice", "asset", "zero_shot_prompt.wav")
FALLBACK_REF_TEXT  = "希望你以后的每一天都能快快乐乐的，没有烦恼，每天开开心心的。"
FALLBACK_REF_LANG  = "zh"
CFG_PATH = os.path.join(GPTSVITS_DIR, "GPT_SoVITS", "configs", "tts_infer.yaml")


def _load_models():
    global _models_loaded, _load_error, _tts_pipeline
    with _lock:
        if _models_loaded:
            return True
        try:
            logger.info("Loading GPT-SoVITS v4 pipeline...")
            if not os.path.exists(CFG_PATH):
                raise FileNotFoundError(f"tts_infer.yaml 不存在: {CFG_PATH}")
            from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
            config = TTS_Config(CFG_PATH)
            _tts_pipeline = TTS(config)
            _models_loaded = True
            logger.info("GPT-SoVITS v4 loaded OK (version=%s device=%s)" %
                        (config.version, config.device))
            return True
        except Exception as e:
            _load_error = str(e)
            logger.error(f"Model load failed: {e}", exc_info=True)
            return False


def _ensure_models():
    if not _models_loaded:
        if _load_error:
            raise HTTPException(503, f"模型加载失败: {_load_error}")
        raise HTTPException(503, "模型未加载，请先调用 /v1/tts/preload 或重启服务")


def _collect_wav_bytes(gen) -> tuple:
    """
    消费 TTS.run() generator，合并所有 (sr, np.ndarray[int16]) 块为 WAV bytes。
    返回 (wav_bytes: bytes, sr: int)
    """
    import soundfile as sf
    chunks = []
    sr_out = 32000
    for sr_out, audio_chunk in gen:
        if audio_chunk is None or len(audio_chunk) == 0:
            continue
        if isinstance(audio_chunk, np.ndarray):
            chunks.append(audio_chunk.astype(np.int16))
        else:
            chunks.append(np.frombuffer(audio_chunk, dtype=np.int16))
    if not chunks:
        raise ValueError("TTS 生成为空，请检查参考音频和文本")
    audio = np.concatenate(chunks, axis=0)
    buf = io.BytesIO()
    sf.write(buf, audio, sr_out, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read(), sr_out


# ── 请求/响应模型 ────────────────────────────────────────────────────

class TTSRequest(BaseModel):
    text: str
    text_lang: str = "zh"              # zh / en / ja / ko
    reference_audio_b64: str = ""      # base64 WAV 参考音频（3-10s）
    reference_text: str = ""           # 参考音频对应文字（空=zero-shot）
    reference_lang: str = "zh"
    speed: float = 1.0                 # 语速（0.5-2.0）
    return_base64: bool = False

class SingRequest(BaseModel):
    lyrics: str                        # 歌词文本
    text_lang: str = "zh"
    reference_audio_b64: str = ""
    reference_text: str = ""
    reference_lang: str = "zh"
    speed: float = 0.85                # 歌词模式默认稍慢
    return_base64: bool = False


# ── 端点 ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "models_loaded": _models_loaded, "service": "singing"}


@app.get("/meminfo")
def meminfo():
    info = {"service": "singing"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        import torch as _t
        if _t.cuda.is_available():
            info["gpu_alloc_mb"] = round(_t.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(_t.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info


@app.post("/gc")
def gc_endpoint():
    """非侵入式回收：gc + 释放显存缓存，不卸载模型。供看门狗优先调用以避免重启打断业务。"""
    import gc as _gc
    before = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            before = _t.cuda.memory_reserved()
    except Exception:
        before = None
    n = _gc.collect()
    freed_mb = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            _t.cuda.empty_cache()
            _t.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - _t.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}


@app.get("/v1/status")
def status():
    return {
        "models_loaded": _models_loaded,
        "load_error": _load_error,
        "config_path": CFG_PATH,
    }


@app.post("/v1/tts/preload")
def preload(background_tasks: BackgroundTasks):
    if _models_loaded:
        return {"ok": True, "detail": "已加载"}
    background_tasks.add_task(_load_models)
    return {"ok": True, "detail": "后台加载中"}


@app.post("/v1/tts")
def tts_generate(req: TTSRequest):
    """
    GPT-SoVITS v4 高质量 TTS，支持零样本音色克隆。
    提供 reference_audio_b64 可克隆音色，留空则使用内置参考音频。
    """
    _ensure_models()

    ref_path = None
    try:
        if req.reference_audio_b64:
            audio_bytes = base64.b64decode(req.reference_audio_b64)
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(audio_bytes)
            tmp.close()
            ref_path = tmp.name
            prompt_wav  = ref_path
            prompt_text = req.reference_text
            prompt_lang = req.reference_lang
        elif os.path.exists(FALLBACK_REF_AUDIO):
            prompt_wav  = FALLBACK_REF_AUDIO
            # v3/v4 requires non-empty prompt_text; use known transcript for fallback audio
            prompt_text = req.reference_text or FALLBACK_REF_TEXT
            prompt_lang = req.reference_lang or FALLBACK_REF_LANG
        else:
            raise HTTPException(400, "请提供 reference_audio_b64（参考音频）")

        t0 = time.time()
        gen = _tts_pipeline.run({
            "text": req.text,
            "text_lang": req.text_lang,
            "ref_audio_path": prompt_wav,
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "speed_factor": req.speed,
            "batch_size": 1,
            "parallel_infer": True,
            "streaming_mode": False,
            "return_fragment": False,
        })
        wav_bytes, sr = _collect_wav_bytes(gen)
        elapsed = time.time() - t0
        logger.info(f"TTS OK: {len(req.text)}chars lang={req.text_lang} {elapsed:.1f}s sr={sr}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("TTS error")
        raise HTTPException(500, str(e))
    finally:
        if ref_path:
            try: os.unlink(ref_path)
            except: pass
        # 每请求释放本轮累积的 CPU/GPU 内存，抑制长跑稳态增长
        try:
            import gc as _gc, torch as _t
            _gc.collect()
            if _t.cuda.is_available():
                _t.cuda.empty_cache(); _t.cuda.ipc_collect()
        except Exception:
            pass

    if req.return_base64:
        return {"audio_base64": base64.b64encode(wav_bytes).decode(),
                "sample_rate": sr, "elapsed_ms": int(elapsed * 1000)}
    return Response(content=wav_bytes, media_type="audio/wav",
                    headers={"X-Processing-Time": f"{elapsed:.2f}s",
                             "X-Sample-Rate": str(sr)})


@app.post("/v1/tts/sing")
def tts_sing(req: SingRequest):
    """
    歌词朗读模式：以角色音色演绎歌词（慢速、抑扬顿挫）。
    speed 默认 0.85 模拟歌词韵律感。
    """
    tts_req = TTSRequest(
        text=req.lyrics,
        text_lang=req.text_lang,
        reference_audio_b64=req.reference_audio_b64,
        reference_text=req.reference_text,
        reference_lang=req.reference_lang,
        speed=req.speed,
        return_base64=req.return_base64,
    )
    return tts_generate(tts_req)


if __name__ == "__main__":
    threading.Thread(target=_load_models, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=7853, log_level="info")
