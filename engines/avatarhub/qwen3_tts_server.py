# -*- coding: utf-8 -*-
"""
qwen3_tts_server.py — Qwen3-TTS 克隆音 TTS 适配服务（端口 7858）
运行环境: qwen3tts conda env（Python 3.10+，PyTorch cu128，pip install -U qwen-tts）

设计目标（2026 升级 · Phase 6 语音引擎升级落地）：
  把阿里 Qwen3-TTS（双轨混合流式架构、首包延迟低至 ~97ms、3 秒极速克隆、10 语种）
  以「与 Fish-Speech 服务完全一致的 HTTP 契约」接入，使 AvatarHub 中枢**零特判**即可路由：
  请求/响应/流式协议逐字段对齐 fish_speech_server.py，故只需在中枢登记一个「fish 兼容引擎」。

接口（与 fish_speech_server.py 逐一对齐；/batch 为本服务超集）：
  GET  /health                  {status, engine, model_loaded}
  GET  /v1/status               {status, engine, model_dir, model_loaded, sample_rate}
  POST /v1/tts                  {text, language, return_base64, ...}      → {audio_base64, sample_rate, ok}
  POST /v1/tts/clone            {text, reference_audio_b64|references[], reference_text, ...}
  POST /v1/tts/clone/stream     二进制流：4字节小端长度前缀 + PCM16 mono；末尾 0 长度帧结束；X-Sample-Rate 头
  POST /v1/tts/clone/batch      {texts[], reference_audio_b64, ...} 同参考音批推理（离线配音吞吐档，
                                批内共享逐 token 循环开销——0.6B 的瓶颈在循环不在算力，批>>单条快）
  POST /v1/tts/instruct         501。qwen_tts 源码明确：克隆链路无 instruct 入口、0.6B 连 CustomVoice
                                的 instruct 都被强制置 None——诚实返回不支持，让中枢回退，胜过静默假装。
  POST /v1/refs/prewarm         {references:[{audio_b64,text}]}  预构建可复用克隆 prompt（降 TTFA）

零下载 / 优雅降级：模型/依赖未就位时 /health 返回 model_loaded=false，合成端点返回 503，
  中枢据此自动回退到 Fish/CosyVoice（与 fish 缺权重时行为一致）——**绝不崩、可一键回退**。
"""
from __future__ import annotations
import sys, os, io, base64, time, wave, struct, hashlib
import logging

# 与 fish 一致：限制 CPU 线程忙等，避免单卡多服务叠加烧核导致桌面卡顿（GPU 推理为主）。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("KMP_BLOCKTIME", "0")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Qwen3TTS] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Qwen3TTS")

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

# ── 配置 ─────────────────────────────────────────────────────────────
import app_config
PORT       = int(os.environ.get("QWEN3_TTS_PORT", "7858"))   # 多卡：每副本一端口（配 CUDA_VISIBLE_DEVICES 绑卡）
# 模型：HF/ModelScope id（推荐）或本机下载目录。Base 模型支持 3 秒克隆。
MODEL_ID   = os.environ.get("QWEN3_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
MODEL_DIR  = os.environ.get("QWEN3_TTS_MODEL_DIR", "")        # 设了优先用本地目录（离线/内网）
DEVICE     = os.environ.get("QWEN3_TTS_DEVICE", "cuda:0")
# 评测推荐 bfloat16；显卡不支持 bf16 时设 QWEN3_TTS_DTYPE=float16。
DTYPE_NAME = os.environ.get("QWEN3_TTS_DTYPE", "bfloat16")
# flash_attention_2 需 flash-attn；缺失时设 QWEN3_TTS_ATTN=sdpa（PyTorch 2.x 自带，免装）。
# 2026-07-05 调研结论：本环境(py3.10+torch2.11+cu128+Windows)无社区预编译轮子(kingbri1 只到
# torch2.9)；且实测瓶颈在逐 token Python 循环(3060 GPU 利用率仅~20%)，换注意力核收益<10%，
# 不值得为它降级 torch——提吞吐走 /v1/tts/clone/batch 批推理。
ATTN_IMPL  = os.environ.get("QWEN3_TTS_ATTN", "flash_attention_2")
MAX_NEW    = int(os.environ.get("QWEN3_TTS_MAX_NEW_TOKENS", "2048"))
# 文本喂入方式：0=模拟流式逐步喂(qwen_tts 默认)；1=全文预填。
# 2026-07-05 .117(3060) A/B(logs/qwen3_ab_batch_20260705.json)：
#   单条: 预填 RTF 3.02 vs 逐步喂 2.76(慢9%)，cos 持平 → 单条维持 qwen_tts 默认(0)
#   批量: 预填 RTF 0.649 vs 逐步喂 0.872(快26%)，cos 持平 → 批量默认预填(1)
NON_STREAMING_DEFAULT = os.environ.get("QWEN3_TTS_NON_STREAMING", "0").strip().lower() not in ("0", "false", "no")
BATCH_NON_STREAMING_DEFAULT = os.environ.get("QWEN3_TTS_BATCH_NON_STREAMING", "1").strip().lower() not in ("0", "false", "no")
# 批推理上限：3060(12G,预算3.2G) 实测 batch=8 KV+激活 ~1G 有余量；更大批自动分片。
MAX_BATCH  = max(1, int(os.environ.get("QWEN3_TTS_MAX_BATCH", "8")))

# ── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="Qwen3-TTS Clone TTS", version="3.0.0")
import service_auth                                   # GPU 服务面加固：鉴权 + CORS 收敛（与 fish 一致）
service_auth.secure(app, name="qwen3_tts")

# ── 请求/响应 Schema（与 fish_speech_server.py 对齐）────────────────────
class TTSRequest(BaseModel):
    text: str
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True
    chunk_length: int = 200
    temperature: float = 0.8
    top_p: float = 0.8

class RefSegment(BaseModel):
    audio_b64: str
    text: str = ""

class CloneTTSRequest(BaseModel):
    text: str
    reference_audio_b64: str = ""               # 单段（向后兼容）
    reference_text: str = ""
    references: list[RefSegment] | None = None  # 多段（取首段作克隆参考；与 fish 接口对齐）
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True
    temperature: float = 0.7
    top_p: float = 0.7
    chunk_length: int = 200
    repetition_penalty: float = 1.2
    seed: int | None = None
    non_streaming: bool | None = None           # None→服务默认(QWEN3_TTS_NON_STREAMING)；fish 会忽略此字段

class BatchCloneTTSRequest(BaseModel):
    """同一参考音批量克隆（离线配音吞吐档）。参考音三选一：reference_audio_b64 / references[0]。"""
    texts: list[str]
    reference_audio_b64: str = ""
    reference_text: str = ""
    references: list[RefSegment] | None = None
    language: str = "zh"
    temperature: float = 0.7
    top_p: float = 0.7
    repetition_penalty: float = 1.2
    seed: int | None = None
    non_streaming: bool | None = None

class InstructTTSRequest(BaseModel):
    text: str
    instruct: str = ""
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True

class PrewarmRequest(BaseModel):
    references: list[RefSegment]

# ── 语种映射：Fish/中枢用 zh/en/ja...；Qwen3-TTS 用 Chinese/English/...（auto 自适应）──
_LANG_MAP = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-tw": "Chinese", "cmn": "Chinese",
    "en": "English", "en-us": "English",
    "ja": "Japanese", "jp": "Japanese",
    "ko": "Korean", "de": "German", "fr": "French", "ru": "Russian",
    "pt": "Portuguese", "es": "Spanish", "it": "Italian",
    "auto": "Auto", "": "Auto",
}

def _qwen_lang(lang: str) -> str:
    return _LANG_MAP.get((lang or "").strip().lower(), "Auto")

# ── 模型状态 ─────────────────────────────────────────────────────────
_model = None
_sample_rate = 24000          # 实际以模型返回 sr 为准；初值仅占位
import threading as _threading
_LOAD_LOCK = _threading.Lock()
# 可复用克隆 prompt 缓存：按「参考音频内容哈希 + 参考文本」缓存 create_voice_clone_prompt 结果，
# 跨句/跨轮复用，免每次重抽取参考特征 → 直接压低 TTFA（对齐 fish 的 use_memory_cache 思路）。
_prompt_cache: dict = {}
_PROMPT_CACHE_MAX = int(os.environ.get("QWEN3_TTS_PROMPT_CACHE", "32"))


def _torch_dtype():
    import torch
    return {"bfloat16": torch.bfloat16, "float16": torch.float16,
            "fp16": torch.float16, "bf16": torch.bfloat16,
            "float32": torch.float32}.get(DTYPE_NAME.lower(), torch.bfloat16)


def _model_ref() -> str:
    """模型引用：本地目录优先（离线/内网），否则用 HF/ModelScope id。"""
    return MODEL_DIR if (MODEL_DIR and os.path.isdir(MODEL_DIR)) else MODEL_ID


def _load_engine():
    """加载 Qwen3TTSModel。模型/依赖缺失 → 返回 None（调用方走 503，中枢自动回退）。"""
    global _model, _sample_rate
    if _model is not None:
        return _model
    if not _LOAD_LOCK.acquire(blocking=False):
        return None                      # 加载进行中：请求线程非阻塞返回 None → 中枢回退
    try:
        if _model is not None:
            return _model
        import torch
        from qwen_tts import Qwen3TTSModel
        ref = _model_ref()
        logger.info(f"加载 Qwen3-TTS: {ref} (device={DEVICE}, dtype={DTYPE_NAME}, attn={ATTN_IMPL})")
        try:
            mdl = Qwen3TTSModel.from_pretrained(
                ref, device_map=DEVICE, dtype=_torch_dtype(),
                attn_implementation=ATTN_IMPL,
            )
        except Exception as e_attn:
            # flash-attn 缺失等 → 回退 sdpa（PyTorch 自带），保证可起服务
            logger.warning(f"attn={ATTN_IMPL} 加载失败({e_attn})，回退 sdpa")
            mdl = Qwen3TTSModel.from_pretrained(
                ref, device_map=DEVICE, dtype=_torch_dtype(),
                attn_implementation="sdpa",
            )
        try:
            # GPU 推理为主，但小模型(0.6B)的 kernel 提交/采样循环有可观 CPU 份额——
            # 1 线程会饿死 GPU(实测 .117 3060 利用率仅 ~20%)。默认 4，可用环境变量调。
            torch.set_num_threads(int(os.environ.get("QWEN3_TTS_TORCH_THREADS", "4")))
        except Exception:
            pass
        _model = mdl
        # 采样率：模型常见 24kHz；首次合成后会用真实返回 sr 校正 _sample_rate。
        _sample_rate = int(os.environ.get("QWEN3_TTS_SR", str(_sample_rate)))
        logger.info(f"Qwen3-TTS 加载完成（采样率以首次合成返回为准，初值 {_sample_rate}Hz）")
        return _model
    except Exception as e:
        logger.error(f"模型加载失败（缺依赖/权重？将降级，由中枢回退）: {e}")
        return None
    finally:
        _LOAD_LOCK.release()


def _decode_ref(audio_b64: str):
    """base64 wav → (numpy_float32, sr) 元组（Qwen3-TTS ref_audio 接受的最稳形式）。"""
    import soundfile as sf
    import numpy as np
    raw = base64.b64decode(audio_b64)
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    if getattr(data, "ndim", 1) > 1:        # 立体声 → 单声道
        data = data.mean(axis=1)
    return np.asarray(data, dtype=np.float32), int(sr)


def _ref_key(audio_b64: str, ref_text: str) -> str:
    h = hashlib.sha1(audio_b64.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{h}:{hashlib.sha1((ref_text or '').encode('utf-8','ignore')).hexdigest()[:8]}"


def _get_clone_prompt(model, audio_b64: str, ref_text: str):
    """取/建可复用克隆 prompt（带缓存）。失败返回 None（调用方退化为逐次传 ref_audio）。"""
    if not audio_b64:
        return None
    key = _ref_key(audio_b64, ref_text)
    if key in _prompt_cache:
        return _prompt_cache[key]
    try:
        ref = _decode_ref(audio_b64)
        prompt = model.create_voice_clone_prompt(
            ref_audio=ref, ref_text=ref_text or "",
            x_vector_only_mode=(not bool(ref_text)),   # 无参考文本 → 仅说话人嵌入，免 ref_text
        )
        if len(_prompt_cache) >= _PROMPT_CACHE_MAX:
            _prompt_cache.pop(next(iter(_prompt_cache)))   # FIFO 淘汰
        _prompt_cache[key] = prompt
        return prompt
    except Exception as e:
        logger.warning(f"构建克隆 prompt 失败，退化为直传 ref_audio: {e}")
        return None


def _gen_kwargs(temperature=None, top_p=None, repetition_penalty=None, seed=None) -> dict:
    """收集透传给 model.generate 的采样参数（仅显式值，空则用 checkpoint 默认）。"""
    kw: dict = {"max_new_tokens": MAX_NEW}
    if temperature is not None: kw["temperature"] = max(0.1, min(1.5, float(temperature)))
    if top_p is not None:       kw["top_p"] = max(0.1, min(1.0, float(top_p)))
    if repetition_penalty is not None:
        kw["repetition_penalty"] = max(0.9, min(2.0, float(repetition_penalty)))
    if seed is not None and int(seed) >= 0:
        try:
            import torch
            torch.manual_seed(int(seed))   # 固定采样路径 → 同文本可复现、降延迟抖动（对齐 fish seed 思路）
        except Exception:
            pass
    return kw


def _to_float_sr(wavs, sr):
    """generate_* 返回 (wavs, sr)，wavs 为 list[np]；取首条 → (float32 np, int sr)，并校正全局 sr。"""
    global _sample_rate
    import numpy as np
    w = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
    arr = np.asarray(w, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    _sample_rate = int(sr)
    return arr, int(sr)


def _free_cuda():
    """每次合成后归还 CUDA 缓存。
    事故复盘(2026-07-05 .117)：本服务与 emotion_tts 同挤 12G 卡，PyTorch 缓存分配器把
    释放的激活/KV 块一直攥在手里（实测涨到 ~8G，0.6B 权重本身才 ~1.2G），叠加桌面占用后
    整卡见顶——下一次采样/合成的瞬时分配直接 CUDA OOM，进程硬死无 traceback，看门狗每小时
    拉一次。本服务请求频率低(质量采样/离线批)，empty_cache 的毫秒级代价可忽略，换来
    稳态显存回落到权重附近，与邻居服务长期共存。"""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _synth(text: str, *, language: str, ref_audio_b64: str = "", ref_text: str = "",
           temperature=None, top_p=None,
           repetition_penalty=None, seed=None, non_streaming: bool | None = None):
    """统一合成入口 → (float32 np, sr)。有参考音→克隆；否则明确报错让中枢回退。

    注：不接 instruct——qwen_tts 的克隆链路（generate_voice_clone→generate）没有 instruct_ids
    入口，传了会被 **kwargs 静默吞掉零效果；与其假装支持不如在端点层 501。"""
    model = _load_engine()
    if model is None:
        raise RuntimeError("Qwen3-TTS 模型未加载（缺权重/依赖）")
    qlang = _qwen_lang(language)
    kw = _gen_kwargs(temperature, top_p, repetition_penalty, seed)
    kw["non_streaming_mode"] = NON_STREAMING_DEFAULT if non_streaming is None else bool(non_streaming)
    if ref_audio_b64:
        try:
            prompt = _get_clone_prompt(model, ref_audio_b64, ref_text)
            if prompt is not None:
                wavs, sr = model.generate_voice_clone(
                    text=text, language=qlang, voice_clone_prompt=prompt, **kw)
            else:
                wavs, sr = model.generate_voice_clone(
                    text=text, language=qlang,
                    ref_audio=_decode_ref(ref_audio_b64), ref_text=ref_text or "", **kw)
        finally:
            _free_cuda()   # 成败都归还缓存：OOM 后不还，下次必再 OOM（进程硬死的元凶）
    else:
        # 无参考音：Base 模型无内置 speaker → 仍需参考；此处给出明确错误，让中枢回退。
        raise RuntimeError("Qwen3-TTS Base 需参考音频克隆；无参考音请配置角色克隆音或改用其它引擎")
    return _to_float_sr(wavs, sr)


def _synth_batch(texts: list[str], *, language: str, ref_audio_b64: str, ref_text: str = "",
                 temperature=None, top_p=None, repetition_penalty=None, seed=None,
                 non_streaming: bool | None = None):
    """同参考音批量合成 → (list[float32 np], sr)。

    为什么值得单开一档：0.6B 的耗时大头是逐 codec-token 的 Python 循环（每步嵌套一次
    code_predictor.generate），单条 RTF 受循环步数支配；官方 generate 原生支持批（左 padding
    + 批注意力），批内 N 句共享同一循环 → 吞吐近似 ×N。离线配音永远该走这里。
    超过 MAX_BATCH 自动分片，防 KV 撑爆小卡。"""
    global _sample_rate
    import numpy as np
    model = _load_engine()
    if model is None:
        raise RuntimeError("Qwen3-TTS 模型未加载（缺权重/依赖）")
    if not ref_audio_b64:
        raise RuntimeError("批量克隆需要参考音频")
    qlang = _qwen_lang(language)
    kw = _gen_kwargs(temperature, top_p, repetition_penalty, seed)
    kw["non_streaming_mode"] = BATCH_NON_STREAMING_DEFAULT if non_streaming is None else bool(non_streaming)
    prompt = _get_clone_prompt(model, ref_audio_b64, ref_text)
    out: list = []
    sr = _sample_rate
    try:
        for i in range(0, len(texts), MAX_BATCH):
            chunk = [t for t in texts[i:i + MAX_BATCH]]
            if prompt is not None:
                wavs, sr = model.generate_voice_clone(
                    text=chunk, language=qlang, voice_clone_prompt=prompt, **kw)
            else:
                wavs, sr = model.generate_voice_clone(
                    text=chunk, language=qlang,
                    ref_audio=_decode_ref(ref_audio_b64), ref_text=ref_text or "", **kw)
            for w in wavs:
                arr = np.asarray(w, dtype=np.float32)
                out.append(arr.reshape(-1) if arr.ndim > 1 else arr)
    finally:
        _free_cuda()   # 批推理 KV 峰值更高，更要及时归还
    _sample_rate = int(sr)
    return out, int(sr)


def _wav_bytes(pcm_float, sr: int) -> bytes:
    import numpy as np
    pcm16 = (np.asarray(pcm_float) * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _b64_wav(wav_bytes: bytes) -> str:
    return base64.b64encode(wav_bytes).decode()


# ── 端点（契约与 fish_speech_server.py 一致）──────────────────────────
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "engine": "qwen3_tts",
                         "model_loaded": _model is not None})

@app.get("/v1/status")
async def status():
    return JSONResponse({"status": "ok", "engine": "qwen3_tts",
                         "model": _model_ref(),
                         "model_loaded": _model is not None,
                         "sample_rate": _sample_rate})

@app.post("/v1/tts")
async def tts(req: TTSRequest):
    """基础 TTS（Base 模型需参考音；无参考音将 503 → 中枢回退）。"""
    try:
        pcm, sr = _synth(req.text, language=req.language,
                         temperature=req.temperature, top_p=req.top_p)
        return JSONResponse({"audio_base64": _b64_wav(_wav_bytes(pcm, sr)),
                             "sample_rate": sr, "ok": True})
    except Exception as e:
        logger.error(f"TTS 失败: {e}")
        raise HTTPException(503, str(e))

@app.post("/v1/tts/clone")
async def clone_tts(req: CloneTTSRequest):
    """零样本克隆（3 秒参考即可）：单段 reference_audio_b64 或多段 references[]（取首段）。"""
    try:
        ref_b64, ref_txt = req.reference_audio_b64, req.reference_text
        if req.references:
            seg = next((s for s in req.references if s.audio_b64), None)
            if seg:
                ref_b64, ref_txt = seg.audio_b64, (seg.text or ref_txt)
        if not ref_b64:
            raise HTTPException(400, "缺少参考音频")
        pcm, sr = _synth(req.text, language=req.language,
                         ref_audio_b64=ref_b64, ref_text=ref_txt,
                         temperature=req.temperature, top_p=req.top_p,
                         repetition_penalty=req.repetition_penalty, seed=req.seed,
                         non_streaming=req.non_streaming)
        return JSONResponse({"audio_base64": _b64_wav(_wav_bytes(pcm, sr)),
                             "sample_rate": sr, "ok": True, "n_refs": 1})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"克隆 TTS 失败: {e}")
        raise HTTPException(503, str(e))

@app.post("/v1/tts/clone/batch")
async def clone_tts_batch(req: BatchCloneTTSRequest):
    """同参考音批量克隆（离线配音吞吐档）。响应 results 与 texts 等长且顺序一致。"""
    try:
        ref_b64, ref_txt = req.reference_audio_b64, req.reference_text
        if req.references:
            seg = next((s for s in req.references if s.audio_b64), None)
            if seg:
                ref_b64, ref_txt = seg.audio_b64, (seg.text or ref_txt)
        if not ref_b64:
            raise HTTPException(400, "缺少参考音频")
        texts = [t for t in req.texts if (t or "").strip()]
        if not texts:
            raise HTTPException(400, "texts 为空")
        t0 = time.time()
        pcms, sr = _synth_batch(texts, language=req.language,
                                ref_audio_b64=ref_b64, ref_text=ref_txt,
                                temperature=req.temperature, top_p=req.top_p,
                                repetition_penalty=req.repetition_penalty, seed=req.seed,
                                non_streaming=req.non_streaming)
        wall = time.time() - t0
        results = [{"audio_base64": _b64_wav(_wav_bytes(p, sr)),
                    "seconds": round(len(p) / max(sr, 1), 3)} for p in pcms]
        audio_s = sum(r["seconds"] for r in results)
        logger.info(f"batch clone: {len(texts)}句 audio={audio_s:.1f}s wall={wall:.1f}s "
                    f"RTF={wall / max(audio_s, 0.001):.2f}")
        return JSONResponse({"ok": True, "sample_rate": sr, "results": results,
                             "wall_seconds": round(wall, 2),
                             "audio_seconds": round(audio_s, 2),
                             "rtf": round(wall / max(audio_s, 0.001), 3)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量克隆 TTS 失败: {e}")
        raise HTTPException(503, str(e))

@app.post("/v1/tts/instruct")
async def instruct_tts(req: InstructTTSRequest):
    """诚实 501：当前挂载的 Base 克隆模型不支持 instruct。

    依据（qwen_tts 0.1.1 源码）：generate_voice_clone 无 instruct 入口（**kwargs 会静默吞掉），
    instruct 仅 VoiceDesign/CustomVoice 模型可用，且 0.6B 连 CustomVoice 的 instruct 都强制置
    None。之前版本把 instruct 透传进克隆调用=零效果的"幻觉接口"，现改为明确不支持 → 中枢回退
    CosyVoice 情感链路。若未来换挂 ≥1.7B 的 VoiceDesign/CustomVoice 权重，再恢复实现。"""
    raise HTTPException(501, "Qwen3-TTS Base(0.6B) 不支持 instruct 语气控制；"
                             "请走 CosyVoice 情感 TTS 或换挂 VoiceDesign/CustomVoice 模型")


def _pcm16_frames(pcm_float, sr: int, frame_ms: int = 120):
    """整段 float → 按 frame_ms 切成 PCM16 bytes 段（供流式逐帧吐出，下游可即时起播/喂口型）。"""
    import numpy as np
    pcm16 = (np.asarray(pcm_float) * 32767).clip(-32768, 32767).astype(np.int16)
    step = max(1, int(sr * frame_ms / 1000))
    for i in range(0, len(pcm16), step):
        yield pcm16[i:i + step].tobytes()


@app.post("/v1/tts/clone/stream")
def clone_tts_stream(req: CloneTTSRequest):
    """流式零样本克隆（协议同 fish /v1/tts/clone/stream）：二进制流，每块=4字节小端长度前缀+PCM16 mono；
    末尾 0 长度帧结束；采样率在 X-Sample-Rate 头。供中枢单句内流式（边合成边喂口型）。

    说明：当前 qwen_tts 的 Python 端到端流式接口尚在完善（官方 day-0 走 vLLM-Omni 离线）；此处采用
    「整段合成 → 按帧切分逐帧吐」的稳健实现，保证中枢流式链路可用且不静音。模型侧暴露原生 token 级
    流式后，仅替换本函数内层生成即可获得 ~97ms 首包（中枢侧无需改动）。"""
    ref_b64, ref_txt = req.reference_audio_b64, req.reference_text
    if req.references:
        seg = next((s for s in req.references if s.audio_b64), None)
        if seg:
            ref_b64, ref_txt = seg.audio_b64, (seg.text or ref_txt)
    if not ref_b64:
        raise HTTPException(400, "缺少参考音频")

    def gen():
        t0 = time.time()
        n = 0
        try:
            pcm, sr = _synth(req.text, language=req.language,
                             ref_audio_b64=ref_b64, ref_text=ref_txt,
                             temperature=req.temperature, top_p=req.top_p,
                             repetition_penalty=req.repetition_penalty, seed=req.seed,
                             non_streaming=req.non_streaming)
            for frame in _pcm16_frames(pcm, sr):
                if not frame:
                    continue
                n += 1
                if n == 1:
                    logger.info(f"stream first chunk {len(frame)//2} samp @ "
                                f"{(time.time()-t0)*1000:.0f}ms")
                yield struct.pack("<I", len(frame)) + frame
        except GeneratorExit:
            # 客户端提前断流。GeneratorExit 后再 yield 结束帧= RuntimeError: generator
            # ignored GeneratorExit(fish 侧 2026-07-06 实锤过)；对端已断,直接收尾。
            logger.info(f"stream client-abort: {n} chunks, {len(req.text)}chars, "
                        f"{(time.time()-t0)*1000:.0f}ms")
            raise
        except Exception as e:
            logger.warning(f"流式克隆 TTS 失败: {e}")
        # 结束帧只在客户端仍在收时发；不能放 finally——断流路径禁止 yield
        yield struct.pack("<I", 0)
        logger.info(f"stream done: {n} chunks, {len(req.text)}chars, "
                    f"{(time.time()-t0)*1000:.0f}ms")

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(_sample_rate)})


@app.post("/v1/refs/prewarm")
async def prewarm_refs(req: PrewarmRequest):
    """预热：把参考音按内容哈希预构建为可复用克隆 prompt（首句合成跳过参考抽取 → 降 TTFA）。"""
    try:
        model = _load_engine()
        if model is None:
            return JSONResponse({"ok": False, "detail": "模型未加载"}, status_code=503)
        import asyncio as _aio
        n = 0
        for s in req.references:
            if not s.audio_b64:
                continue
            ok = await _aio.get_event_loop().run_in_executor(
                None, lambda b=s.audio_b64, t=s.text: _get_clone_prompt(model, b, t))
            if ok is not None:
                n += 1
        return JSONResponse({"ok": True, "n_refs": n, "cached": True})
    except Exception as e:
        logger.error(f"参考预热失败: {e}")
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=500)


# ── 自监控兜底（accept 循环死亡自救）──────────────────────────────────
# 事故复盘(2026-07-08 .117)：网络瞬断触发监听套接字 accept 循环 WinError 64
# （"指定的网络名不再可用"），Windows Proactor 事件循环从此**停止 accept**——进程仍活、
# CUDA 上下文还在、却再也不接受任何连接。旧实例就这样僵活近 3 天不监听 7858，中枢每 30s
# 探测判掉线，而看门狗"只重启计划任务"杀不到它 → 空转 60+ 次永远救不回。
# 兜底：后台线程周期性探自己的 127.0.0.1/health；连续不可达即认定"僵死"，主动 os._exit
# 干净释放端口，交由 boot 脚本/看门狗拉起新实例——把"静默僵死"变成"崩溃即重启"。
_SELF_WD_GRACE    = int(os.environ.get("QWEN3_TTS_SELF_WD_GRACE", "120"))     # 启动宽限(等模型加载/首次绑定)
_SELF_WD_INTERVAL = int(os.environ.get("QWEN3_TTS_SELF_WD_INTERVAL", "30"))   # 探测间隔秒
_SELF_WD_FAILS    = int(os.environ.get("QWEN3_TTS_SELF_WD_FAILS", "3"))       # 连续失败次数→自退出(0=关闭)
_self_wd_started  = False


def _self_watchdog_loop():
    import time as _t
    import urllib.request as _u
    url = f"http://127.0.0.1:{PORT}/health"
    _t.sleep(_SELF_WD_GRACE)
    fails = 0
    while True:
        alive = False
        try:
            with _u.urlopen(url, timeout=8) as r:
                alive = (r.status == 200)
                r.read(64)
        except Exception:
            alive = False
        if alive:
            fails = 0
        else:
            fails += 1
            logger.warning(f"[self-wd] 本机 /health 不可达 {fails}/{_SELF_WD_FAILS}（accept 循环疑似死亡）")
            if fails >= _SELF_WD_FAILS:
                logger.error("[self-wd] 服务已僵死（进程在但不再服务）→ 主动退出让守护重启")
                os._exit(1)
        _t.sleep(_SELF_WD_INTERVAL)


def _start_self_watchdog():
    global _self_wd_started
    if _self_wd_started or _SELF_WD_FAILS <= 0:
        return
    _self_wd_started = True
    _threading.Thread(target=_self_watchdog_loop, name="qwen3-self-wd", daemon=True).start()
    logger.info(f"[self-wd] 自监控已启动（宽限{_SELF_WD_GRACE}s / 间隔{_SELF_WD_INTERVAL}s / 连败{_SELF_WD_FAILS}次即自重启）")


@app.on_event("startup")
async def startup():
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_engine)
    _start_self_watchdog()
    logger.info(f"Qwen3-TTS server 启动，端口 {PORT}（模型后台加载中…）")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
