"""
EmotionTTS Server - CosyVoice 3 驱动的情感语音合成服务
端口: 7852
API:
  GET  /healthz                   - 轻量存活检查（不触碰模型/GPU，供 Hub 高频探测）
  GET  /health                    - 健康检查
  POST /v1/tts                    - 情感 TTS（返回 WAV 字节）
  POST /v1/tts/clone              - 克隆音色 + 情感 TTS
  POST /v1/tts/instruct           - 指令式 TTS（自然语言情感描述）
  GET  /v1/emotions               - 支持的情感列表
  GET  /v1/status                 - 模型加载状态
"""
import os, sys, io, time, tempfile, logging, threading, base64, gc, struct, hashlib
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional

# ── 路径设置 ─────────────────────────────────────────────────────────────
COSYVOICE_DIR = os.path.join(os.path.dirname(__file__), "CosyVoice")
sys.path.insert(0, COSYVOICE_DIR)
sys.path.insert(0, os.path.join(COSYVOICE_DIR, "third_party", "Matcha-TTS"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [EmotionTTS] %(message)s")
logger = logging.getLogger("emotion_tts")

app = FastAPI(title="EmotionTTS Server", version="1.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="emotion_tts")

# ── 情感映射 ─────────────────────────────────────────────────────────────
# CosyVoice3 instruct 模式的情感标签格式: <|emotion|>
EMOTION_MAP = {
    "neutral":   "",
    "happy":     "<|happy|>",
    "sad":       "<|sad|>",
    "angry":     "<|angry|>",
    "fearful":   "<|fearful|>",
    "surprised": "<|surprised|>",
    "disgusted": "<|disgusted|>",
    "gentle":    "<|gentle|>",
    "excited":   "<|excited|>",
    "calm":      "<|calm|>",
    "serious":   "<|serious|>",
}

# CosyVoice2 指令模式描述
EMOTION_INSTRUCT = {
    "neutral":   "",
    "happy":     "用开心愉快的语气说",
    "sad":       "用悲伤难过的语气说",
    "angry":     "用愤怒生气的语气说",
    "fearful":   "用恐惧害怕的语气说",
    "surprised": "用惊讶的语气说",
    "disgusted":  "用厌恶的语气说",
    "gentle":    "用温柔轻柔的语气说",
    "excited":   "用兴奋激动的语气说",
    "calm":      "用平静沉着的语气说",
    "serious":   "用严肃认真的语气说",
}

# CosyVoice3 instruct2 格式: 'You are a helpful assistant. [描述]<|endofprompt|>'
def _fmt_instruct(emotion: str, custom_instruct: str = "") -> str:
    desc = custom_instruct or EMOTION_INSTRUCT.get(emotion, "")
    if not desc:
        return ""
    return f"You are a helpful assistant. {desc}<|endofprompt|>"

MODEL_DIR = os.path.join(COSYVOICE_DIR, "pretrained_models", "Fun-CosyVoice3-0.5B")
SAMPLE_RATE = 22050

# 预注册说话人 id：把默认参考音频的说话人特征(speech_token/feat/embedding)缓存进
# spk2info，跳过每次 TTS 调用对参考音频的重复抽取(onnx campplus + speech_tokenizer
# + speech_feat)，这部分是与文本长度无关的固定开销，是短句 TTFA 的主要成本之一。
# 仅中性(cross_lingual)路径可用：instruct2 情感路径需把 instruct 作为 prompt_text，
# 命中缓存会丢失 instruct，故情感路径仍走实时抽取。
_DEFAULT_SPK = "_default_ref"
_spk_cached = False

# 克隆音色 spk 缓存：按参考音频字节哈希缓存已抽取的说话人特征。对话用固定角色音色，
# 首句抽取后整个会话(服务生命周期内)的后续句/后续轮首句均命中缓存，跳过 onnx 抽取，
# 直接压低生产环境 TTFA。仅中性(cross_lingual/zero_shot)路径用缓存；情感 instruct2
# 需把 instruct 作 prompt_text，命中缓存会丢失情感，故情感路径仍实时抽取。
_ref_spk_cache: dict[str, str] = {}
_ref_spk_lock = threading.Lock()
_REF_SPK_CACHE_ON = os.environ.get("EMOTION_TTS_REF_SPK_CACHE", "1") == "1"


def _get_or_register_ref_spk(ref_bytes: bytes, ref_path: str) -> str:
    """按参考音频哈希返回缓存的 zero-shot spk id；未命中则注册并缓存。失败返回 ''。"""
    if not _REF_SPK_CACHE_ON:
        return ""
    h = hashlib.md5(ref_bytes).hexdigest()
    with _ref_spk_lock:
        sid = _ref_spk_cache.get(h)
        if sid:
            return sid
        sid = f"_ref_{h[:12]}"
        try:
            _cosyvoice.add_zero_shot_spk("", ref_path, sid)
            _ref_spk_cache[h] = sid
            logger.info(f"ref spk cached '{sid}' (refs={len(_ref_spk_cache)})")
            return sid
        except Exception as _e:
            logger.warning(f"ref spk register failed: {_e}")
            return ""

# ── 全局模型状态 ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_models_loaded = False
_load_error = ""
_cosyvoice = None
_device = "cuda" if torch.cuda.is_available() else "cpu"


def _load_models():
    global _models_loaded, _load_error, _cosyvoice
    with _lock:
        if _models_loaded:
            return True
        try:
            logger.info(f"Loading CosyVoice3 from {MODEL_DIR} ...")
            from cosyvoice.cli.cosyvoice import AutoModel
            # fp16：实测在本机(5090/cu128)CosyVoice3 fp16 反而更慢（长句 rtf 0.49→0.75，
            #   自回归 LLM 部分不吃 fp16 + autocast 转换开销）→ 默认 fp32。
            #   仅在确有收益的机器上设 EMOTION_TTS_FP16=1 启用。
            _use_fp16 = (_device == "cuda") and os.environ.get("EMOTION_TTS_FP16", "0") == "1"
            try:
                _cosyvoice = AutoModel(model_dir=MODEL_DIR, fp16=_use_fp16)
            except TypeError:
                _cosyvoice = AutoModel(model_dir=MODEL_DIR)  # 老版签名无 fp16
            _models_loaded = True
            logger.info(f"CosyVoice3 loaded on {_device} (fp16={_use_fp16}), "
                        f"sample_rate={_cosyvoice.sample_rate}")
            # 预注册默认参考说话人，缓存其特征以省去每次合成的固定抽取开销
            try:
                _ref0 = os.path.join(COSYVOICE_DIR, "asset", "zero_shot_prompt.wav")
                if os.path.exists(_ref0):
                    global _spk_cached
                    _cosyvoice.add_zero_shot_spk("", _ref0, _DEFAULT_SPK)
                    _spk_cached = True
                    logger.info(f"default spk registered as '{_DEFAULT_SPK}' (特征已缓存)")
            except Exception as _se:
                logger.warning(f"spk register skipped: {_se}")
            # 启动自热：跑一次短句合成，触发 cudnn/cutlass 自调，
            # 避免首轮对话首句冷启动(实测冷启动会与口型抢 GPU 拖到秒级/帧)。
            try:
                _ref = os.path.join(COSYVOICE_DIR, "asset", "zero_shot_prompt.wav")
                if os.path.exists(_ref):
                    _t = time.time()
                    for _ in _cosyvoice.inference_cross_lingual("你好。", _ref, stream=False, speed=1.0):
                        pass
                    logger.info(f"warmup synth done in {time.time()-_t:.1f}s")
            except Exception as _we:
                logger.warning(f"warmup skipped: {_we}")
            return True
        except Exception as e:
            _load_error = str(e)
            logger.error(f"Model load failed: {e}")
            return False


# ── 空闲自动卸载（默认关闭；设环境变量 EMOTION_TTS_IDLE_UNLOAD=秒 开启）──
_IDLE_UNLOAD = float(os.environ.get("EMOTION_TTS_IDLE_UNLOAD", "0"))
_last_used = time.time()
_inflight = 0
_inflight_lock = threading.Lock()


def _touch():
    global _last_used
    _last_used = time.time()


def _ensure_models():
    _touch()
    if not _models_loaded:
        ok = _load_models()
        if not ok:
            raise HTTPException(503, f"模型未就绪: {_load_error}")
    global _inflight
    with _inflight_lock:
        _inflight += 1


def _cleanup():
    """每次合成后释放累积的 GPU 显存缓存与 Python 垃圾，防止长时间运行内存增长。
    同时与 _ensure_models 配对维护 in-flight 计数（用于空闲卸载判定）。"""
    global _inflight
    with _inflight_lock:
        if _inflight > 0:
            _inflight -= 1
    _touch()
    try:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass


def _unload_models():
    global _models_loaded, _cosyvoice
    with _lock:
        if not _models_loaded:
            return
        _cosyvoice = None
        _models_loaded = False
    try:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    logger.info("空闲卸载: CosyVoice3 已释放，下次请求将自动重载")


def _idle_watch():
    while True:
        time.sleep(30)
        try:
            if _IDLE_UNLOAD <= 0:
                continue
            with _inflight_lock:
                busy = _inflight > 0
            if _models_loaded and not busy and (time.time() - _last_used) > _IDLE_UNLOAD:
                _unload_models()
        except Exception:
            pass


def _audio_to_wav_bytes(audio_tensor: torch.Tensor, sample_rate: int) -> bytes:
    """将 torch Tensor 转为 WAV bytes"""
    import soundfile as sf
    audio_np = audio_tensor.squeeze().cpu().numpy()
    buf = io.BytesIO()
    sf.write(buf, audio_np, sample_rate, format="WAV")
    buf.seek(0)
    return buf.read()


# ── 请求/响应模型 ─────────────────────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    emotion: str = "neutral"       # 情感标签
    speaker: str = "zh"            # 说话人/语言
    speed: float = 1.0             # 语速 (0.5-2.0)
    return_base64: bool = False    # True 时返回 JSON+base64，否则返回 WAV bytes

class CloneTTSRequest(BaseModel):
    text: str
    reference_audio_b64: str       # base64 参考音频（WAV）
    reference_text: str = ""       # 参考文本（空时用 zero-shot）
    emotion: str = "neutral"
    speed: float = 1.0
    return_base64: bool = False

class InstructTTSRequest(BaseModel):
    text: str
    instruct: str = ""             # 自然语言情感描述，如 "用激动的语气"
    reference_audio_b64: str = ""  # 可选参考音频
    speed: float = 1.0
    return_base64: bool = False


# ── FastAPI 端点 ──────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "ok": True,
        "models_loaded": _models_loaded,
        "device": _device,
        "service": "emotion_tts"
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "emotion_tts"}

@app.get("/meminfo")
def meminfo():
    info = {"service": "emotion_tts"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        if torch.cuda.is_available():
            info["gpu_alloc_mb"] = round(torch.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(torch.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info

@app.post("/gc")
def gc_endpoint():
    """非侵入式回收：gc + 释放显存缓存，不卸载模型。供看门狗优先调用以避免重启打断业务。"""
    before = None
    try:
        if torch.cuda.is_available():
            before = torch.cuda.memory_reserved()
    except Exception:
        before = None
    n = gc.collect()
    freed_mb = None
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - torch.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}

@app.get("/v1/status")
def status():
    sr = _cosyvoice.sample_rate if _cosyvoice else None
    return {
        "models_loaded": _models_loaded,
        "load_error": _load_error,
        "device": _device,
        "sample_rate": sr,
        "model_dir": MODEL_DIR,
    }

@app.get("/v1/emotions")
def list_emotions():
    return {
        "emotions": list(EMOTION_MAP.keys()),
        "descriptions": EMOTION_INSTRUCT
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
    CosyVoice3 TTS: 需要参考音频。无 speaker/reference 时使用内置参考音频。
    emotion 可选: neutral/happy/sad/angry/fearful/surprised/gentle/excited/calm
    """
    _ensure_models()

    # CosyVoice3 不支持 SFT 预设音色，需要用 zero_shot 或 instruct2
    # 使用内置参考音频 (CosyVoice 仓库 asset/zero_shot_prompt.wav)
    default_ref = os.path.join(COSYVOICE_DIR, "asset", "zero_shot_prompt.wav")
    if not os.path.exists(default_ref):
        raise HTTPException(503, "内置参考音频不存在，请提供 reference_audio_b64")

    try:
        t0 = time.time()
        audio_chunks = []
        instruct_str = _fmt_instruct(req.emotion)
        if instruct_str:
            for result in _cosyvoice.inference_instruct2(
                    req.text, instruct_str, default_ref, stream=False, speed=req.speed):
                audio_chunks.append(result["tts_speech"])
        else:
            for result in _cosyvoice.inference_cross_lingual(
                    req.text, default_ref,
                    zero_shot_spk_id=(_DEFAULT_SPK if _spk_cached else ""),
                    stream=False, speed=req.speed):
                audio_chunks.append(result["tts_speech"])
        if not audio_chunks:
            raise HTTPException(500, "TTS 输出为空")
        audio = torch.cat(audio_chunks, dim=-1)
        wav_bytes = _audio_to_wav_bytes(audio, _cosyvoice.sample_rate)
        elapsed = time.time() - t0
        logger.info(f"TTS OK: {len(req.text)}chars emotion={req.emotion} {elapsed:.1f}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("TTS error")
        raise HTTPException(500, str(e))
    finally:
        _cleanup()

    if req.return_base64:
        return {"audio_base64": base64.b64encode(wav_bytes).decode(),
                "sample_rate": _cosyvoice.sample_rate,
                "elapsed_ms": int(elapsed * 1000)}
    return Response(content=wav_bytes, media_type="audio/wav",
                    headers={"X-Processing-Time": f"{elapsed:.2f}s",
                             "X-Sample-Rate": str(_cosyvoice.sample_rate)})


def _pcm16_bytes(audio_tensor: torch.Tensor) -> bytes:
    """torch float[-1,1] → int16 PCM little-endian bytes。"""
    a = audio_tensor.squeeze().detach().cpu().numpy().astype(np.float32)
    a = np.clip(a, -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


class RegisterSpkRequest(BaseModel):
    reference_audio_b64: str


@app.post("/v1/tts/register_spk")
def register_spk(req: RegisterSpkRequest):
    """预注册参考音色 spk（供角色激活时预热，消除首句抽取开销 → 压低首轮 TTFA）。"""
    _ensure_models()
    if not req.reference_audio_b64:
        raise HTTPException(400, "reference_audio_b64 不能为空")
    ref_path = None
    try:
        audio_bytes = base64.b64decode(req.reference_audio_b64)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            ref_path = f.name
        sid = _get_or_register_ref_spk(audio_bytes, ref_path)
        return {"ok": bool(sid), "spk_id": sid, "cached_refs": len(_ref_spk_cache)}
    finally:
        if ref_path:
            try: os.unlink(ref_path)
            except: pass
        _cleanup()


@app.post("/v1/tts/stream")
def tts_stream(req: TTSRequest):
    """流式 TTS：边合成边吐音频块（破 TTFA 固定延迟）。
    协议：二进制流，每块 = 4字节小端长度前缀 + PCM16 mono 裸数据。
    采样率在响应头 X-Sample-Rate。客户端可在收到首块即播放。
    """
    _ensure_models()
    default_ref = os.path.join(COSYVOICE_DIR, "asset", "zero_shot_prompt.wav")
    if not os.path.exists(default_ref):
        raise HTTPException(503, "内置参考音频不存在")
    sr = _cosyvoice.sample_rate
    text = req.text
    emotion = req.emotion
    speed = req.speed

    def gen():
        t0 = time.time()
        first = True
        nchunks = 0
        try:
            instruct_str = _fmt_instruct(emotion)
            if instruct_str:
                it = _cosyvoice.inference_instruct2(
                    text, instruct_str, default_ref, stream=True, speed=speed)
            else:
                it = _cosyvoice.inference_cross_lingual(
                    text, default_ref,
                    zero_shot_spk_id=(_DEFAULT_SPK if _spk_cached else ""),
                    stream=True, speed=speed)
            for result in it:
                pcm = _pcm16_bytes(result["tts_speech"])
                if not pcm:
                    continue
                nchunks += 1
                if first:
                    first = False
                    logger.info(f"stream first chunk {len(pcm)//2} samp "
                                f"@ {(time.time()-t0)*1000:.0f}ms")
                yield struct.pack("<I", len(pcm)) + pcm
        except Exception as e:
            logger.exception("stream TTS error")
            # 末尾发 0 长度帧表示异常结束（客户端据此停止）
            yield struct.pack("<I", 0)
        finally:
            logger.info(f"stream done: {nchunks} chunks, {len(text)}chars, "
                        f"{(time.time()-t0)*1000:.0f}ms total")
            _cleanup()

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(sr)})


@app.post("/v1/tts/clone")
def tts_clone(req: CloneTTSRequest):
    """
    克隆音色 + 情感 TTS
    reference_audio_b64: base64 WAV，3-10秒参考音频
    """
    _ensure_models()
    if not req.reference_audio_b64:
        raise HTTPException(400, "reference_audio_b64 不能为空")

    ref_path = None
    try:
        audio_bytes = base64.b64decode(req.reference_audio_b64)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            ref_path = f.name

        t0 = time.time()
        audio_chunks = []
        instruct_str = _fmt_instruct(req.emotion)
        if instruct_str:
            # 情感 instruct2 模式（带参考音频）
            for result in _cosyvoice.inference_instruct2(
                    req.text, instruct_str, ref_path, stream=False,
                    speed=req.speed):
                audio_chunks.append(result["tts_speech"])
        elif req.reference_text:
            # zero_shot：克隆音色（reference_text 用于 CosyVoice3 的 prompt 格式）
            zs_ref_text = "You are a helpful assistant.<|endofprompt|>" + req.reference_text
            for result in _cosyvoice.inference_zero_shot(
                    req.text, zs_ref_text, ref_path, stream=False,
                    speed=req.speed):
                audio_chunks.append(result["tts_speech"])
        else:
            # 中性克隆：按参考音频哈希命中/注册 spk，跳过重复抽取（压低会话 TTFA）
            _sid = _get_or_register_ref_spk(audio_bytes, ref_path)
            for result in _cosyvoice.inference_cross_lingual(
                    req.text, ref_path, zero_shot_spk_id=_sid,
                    stream=False, speed=req.speed):
                audio_chunks.append(result["tts_speech"])

        if not audio_chunks:
            raise HTTPException(500, "TTS 输出为空")
        audio = torch.cat(audio_chunks, dim=-1)
        wav_bytes = _audio_to_wav_bytes(audio, _cosyvoice.sample_rate)
        elapsed = time.time() - t0
        logger.info(f"Clone TTS OK: emotion={req.emotion} {elapsed:.1f}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Clone TTS error")
        raise HTTPException(500, str(e))
    finally:
        try: os.unlink(ref_path)
        except: pass
        _cleanup()

    if req.return_base64:
        return {"audio_base64": base64.b64encode(wav_bytes).decode(),
                "sample_rate": _cosyvoice.sample_rate,
                "elapsed_ms": int(elapsed * 1000)}
    return Response(content=wav_bytes, media_type="audio/wav",
                    headers={"X-Processing-Time": f"{elapsed:.2f}s"})


class CloneStreamTTSRequest(BaseModel):
    text: str
    reference_audio_b64: str = ""
    reference_text: str = ""
    emotion: str = ""              # 情感标签(EMOTION_INSTRUCT 键)；与 instruct 二选一
    instruct: str = ""             # 自然语言情感描述(优先于 emotion)
    speed: float = 1.0


# (参考音hash, 情感指令hash) → 已注册 spk_id。inference_instruct2 走 spk 缓存时会跳过
# 参考音特征抽取(fbank/speech_token/说话人嵌入,最贵的一段)；add_zero_shot_spk 把注册时的
# prompt_text(即情感指令)连同音频特征一起缓存 → 按「参考音+指令」为键,情感语义与非缓存路径逐字节一致。
_instr_spk_cache: dict = {}
_INSTR_SPK_MAX = 64


def _get_or_register_instruct_spk(ref_bytes: bytes, ref_path: str, instruct_str: str) -> str:
    try:
        key = "in_" + hashlib.md5(ref_bytes + b"|" + instruct_str.encode("utf-8")).hexdigest()[:16]
        if key in _instr_spk_cache:
            return key
        _cosyvoice.add_zero_shot_spk(instruct_str, ref_path, key)
        _instr_spk_cache[key] = time.time()
        if len(_instr_spk_cache) > _INSTR_SPK_MAX:   # 简单逐出:删最旧(spk2info 同步删防显存膨胀)
            old = min(_instr_spk_cache, key=_instr_spk_cache.get)
            _instr_spk_cache.pop(old, None)
            _cosyvoice.frontend.spk2info.pop(old, None)
        logger.info(f"instruct-spk 注册: {key} ({instruct_str[:20]}...) 共{len(_instr_spk_cache)}")
        return key
    except Exception:
        logger.exception("instruct-spk 注册失败(本次走实时抽取)")
        return ""


@app.post("/v1/tts/clone/stream")
def tts_clone_stream(req: CloneStreamTTSRequest):
    """P2a(2026-07-09)：克隆音色(+可选情感指令)流式合成。
    协议与 fish/qwen3 同款：二进制流，每块=4字节小端长度前缀+PCM16 mono，0长度帧结束，
    采样率在 X-Sample-Rate 头 → 同传中枢的流式客户端可直接消费。
    动机：情感句此前只能整句非流式(实测 2~4s 才出声)；CosyVoice3 的 inference_instruct2
    本身支持 stream=True，本端点把克隆参考音接进流式，首包不用等整句。
    加速：情感路径按(参考音+指令)注册 spk 缓存 → 复调时跳过参考特征抽取。"""
    _ensure_models()
    if not req.reference_audio_b64:
        raise HTTPException(400, "reference_audio_b64 不能为空")
    audio_bytes = base64.b64decode(req.reference_audio_b64)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        ref_path = f.name
    sr = _cosyvoice.sample_rate
    instruct_str = _fmt_instruct(req.emotion, custom_instruct=req.instruct)
    speed = max(0.5, min(2.0, float(req.speed or 1.0)))
    text, ref_text = req.text, req.reference_text

    def gen():
        t0 = time.time()
        n = 0
        try:
            if instruct_str:
                sid = _get_or_register_instruct_spk(audio_bytes, ref_path, instruct_str)
                it = _cosyvoice.inference_instruct2(text, instruct_str, ref_path,
                                                    zero_shot_spk_id=sid,
                                                    stream=True, speed=speed)
            elif ref_text:
                # zero_shot 同样走 (参考音+参考文本) spk 缓存：省去每句 ~1s+ 的参考特征重抽取
                zs_ref_text = "You are a helpful assistant.<|endofprompt|>" + ref_text
                sid = _get_or_register_instruct_spk(audio_bytes, ref_path, zs_ref_text)
                it = _cosyvoice.inference_zero_shot(text, zs_ref_text, ref_path,
                                                    zero_shot_spk_id=sid,
                                                    stream=True, speed=speed)
            else:
                _sid = _get_or_register_ref_spk(audio_bytes, ref_path)
                it = _cosyvoice.inference_cross_lingual(text, ref_path,
                                                        zero_shot_spk_id=_sid,
                                                        stream=True, speed=speed)
            first = True
            for result in it:
                pcm = _pcm16_bytes(result["tts_speech"])
                if not pcm:
                    continue
                n += 1
                if first:
                    first = False
                    logger.info(f"clone/stream first chunk @ {(time.time()-t0)*1000:.0f}ms "
                                f"(instruct={bool(instruct_str)})")
                yield struct.pack("<I", len(pcm)) + pcm
            yield struct.pack("<I", 0)
        except Exception:
            logger.exception("clone/stream error")
            yield struct.pack("<I", 0)      # 异常也发结束帧,客户端按部分音频收尾
        finally:
            try:
                os.unlink(ref_path)
            except Exception:
                pass
            logger.info(f"clone/stream done: {n} chunks {len(text)}chars "
                        f"{(time.time()-t0)*1000:.0f}ms")
            _cleanup()

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(sr)})


@app.post("/v1/tts/instruct")
def tts_instruct(req: InstructTTSRequest):
    """
    指令式 TTS：通过自然语言描述情感风格
    例如 instruct="用非常激动兴奋的语气，语速稍快"
    """
    _ensure_models()
    instruct_text = req.instruct or EMOTION_INSTRUCT.get("neutral", "")

    ref_path = None
    try:
        if req.reference_audio_b64:
            audio_bytes = base64.b64decode(req.reference_audio_b64)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                ref_path = f.name
            prompt_wav = ref_path
        else:
            prompt_wav = os.path.join(COSYVOICE_DIR, "asset", "zero_shot_prompt.wav")

        t0 = time.time()
        audio_chunks = []
        instruct_str = _fmt_instruct("", custom_instruct=req.instruct) if req.instruct else ""
        if not instruct_str:
            instruct_str = _fmt_instruct("gentle")  # 无指令时默认温柔语气
        for result in _cosyvoice.inference_instruct2(
                req.text, instruct_str, prompt_wav, stream=False,
                speed=req.speed):
            audio_chunks.append(result["tts_speech"])

        if not audio_chunks:
            raise HTTPException(500, "TTS 输出为空")
        audio = torch.cat(audio_chunks, dim=-1)
        wav_bytes = _audio_to_wav_bytes(audio, _cosyvoice.sample_rate)
        elapsed = time.time() - t0
        logger.info(f"Instruct TTS OK: {instruct_str!r} {elapsed:.1f}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Instruct TTS error")
        raise HTTPException(500, str(e))
    finally:
        if ref_path:
            try: os.unlink(ref_path)
            except: pass
        _cleanup()

    if req.return_base64:
        return {"audio_base64": base64.b64encode(wav_bytes).decode(),
                "sample_rate": _cosyvoice.sample_rate,
                "elapsed_ms": int(elapsed * 1000)}
    return Response(content=wav_bytes, media_type="audio/wav",
                    headers={"X-Processing-Time": f"{elapsed:.2f}s"})


@app.on_event("startup")
async def startup():
    logger.info("Starting EmotionTTS Server, preloading models in background...")
    t = threading.Thread(target=_load_models, daemon=True)
    t.start()
    if _IDLE_UNLOAD > 0:
        logger.info(f"空闲自动卸载已启用: {_IDLE_UNLOAD:.0f}s")
        threading.Thread(target=_idle_watch, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("EMOTION_TTS_PORT", "7852")), log_level="info")
