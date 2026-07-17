# -*- coding: utf-8 -*-
"""
voxcpm_server.py — VoxCPM2 TTS 适配服务（端口 7856）
运行环境: voxcpm conda env（`pip install voxcpm`，PyTorch + cu128）

为什么引入：VoxCPM2(OpenBMB) 是 Apache-2.0 可商用的无分词器扩散-AR TTS，
30 语种 + 9 方言、48kHz 录音棚级、文字 Voice Design / 风格可控克隆、RTF~0.13(Nano-vLLM)。
相对 fish-speech-1.5(CC-BY-NC-SA，商用受限) 在「商用许可 + 多语视频配音 + 表现力标签 +
多租户并发」四点上结构性更优，故作为**与 fish 并存的可灰度 TTS 引擎**接入。

设计要点（与 fish_speech_server.py 完全对齐的 HTTP 契约，做到 Hub 侧 drop-in）：
  GET  /health
  GET  /v1/status
  POST /v1/tts            {text, language, return_base64, ...}            → {"audio_base64": ...}
  POST /v1/tts/clone      {text, reference_audio_b64, reference_text, references[], ...}
  POST /v1/tts/clone/stream  二进制流：4字节小端长度前缀 + PCM16 mono；末尾 0 长度帧结束；X-Sample-Rate 头
  POST /v1/tts/instruct   {text, instruct}  → instruct 作为 Voice Design / 风格描述（VoxCPM2 原生强项）
  POST /v1/refs/prewarm   预热参考音 → 落临时 wav 缓存，降低后续克隆 TTFA

双后端（按 VOXCPM_BACKEND 选择，单进程一种）：
  - inproc（默认）：进程内加载 VoxCPM2，支持全部能力 + generate_streaming 原生流式（首音最优）。
  - vllm：把请求代理到 vLLM-Omni 的 OpenAI 兼容 /v1/audio/speech（PagedAttention + 连续批处理，
           面向多租户高并发；克隆能力依赖后端扩展，缺失时建议该机用 inproc）。
"""
from __future__ import annotations
import sys, os, io, base64, time, wave, struct, hashlib, tempfile, threading
import logging

# GPU 推理为主、CPU 仅轻量分词/拼接：限线程 + 被动等待，避免单卡多服务叠加时 CPU 忙等拖卡桌面。
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
    format="%(asctime)s [VoxCPM] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("VoxCPM")

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

import app_config

# ── 配置 ─────────────────────────────────────────────────────────────
PORT        = int(os.environ.get("VOXCPM_PORT", "7856"))
# 模型来源：本地目录优先；不存在则回退 HuggingFace id（首次会触发下载）。
_LOCAL_DIR  = os.environ.get("VOXCPM_MODEL_DIR", str(app_config.MODELS_DIR / "VoxCPM2"))
MODEL_SRC   = _LOCAL_DIR if os.path.isdir(_LOCAL_DIR) else os.environ.get("VOXCPM_MODEL_ID", "openbmb/VoxCPM2")
DEVICE      = os.environ.get("VOXCPM_DEVICE", "cuda")
BACKEND     = os.environ.get("VOXCPM_BACKEND", "inproc").lower()      # inproc | vllm
VLLM_URL    = os.environ.get("VOXCPM_VLLM_URL", "http://127.0.0.1:8000").rstrip("/")
VLLM_MODEL  = os.environ.get("VOXCPM_VLLM_MODEL", "openbmb/VoxCPM2")
LOAD_DENOISE = os.environ.get("VOXCPM_DENOISER", "0") == "1"          # 默认关，省显存/降时延
# 质量/速度旋钮（cfg 越大越贴文本/越慢；timesteps 越多越精细/越慢）。流式下用更少步数压 TTFA。
CFG_VALUE   = float(os.environ.get("VOXCPM_CFG", "2.0"))
TIMESTEPS   = int(os.environ.get("VOXCPM_TIMESTEPS", "10"))
STREAM_TIMESTEPS = int(os.environ.get("VOXCPM_STREAM_TIMESTEPS", "8"))

# ── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="VoxCPM2 TTS", version="2.0.0")
try:
    import service_auth
    service_auth.secure(app, name="voxcpm")           # 复用 GPU 服务面加固（鉴权 + CORS 收敛）
except Exception as _e:                                # 加固模块缺失不致命（本机回环仍可用）
    logger.warning(f"service_auth 未启用: {_e}")

# ── 请求 Schema（字段为 fish 契约超集，未用字段安全忽略，保证 drop-in） ──
class TTSRequest(BaseModel):
    text: str
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True
    instruct: str = ""                      # 风格/Voice Design 描述（可选）
    cfg_value: float = CFG_VALUE
    inference_timesteps: int = TIMESTEPS

class RefSegment(BaseModel):
    audio_b64: str
    text: str = ""

class CloneTTSRequest(BaseModel):
    text: str
    reference_audio_b64: str = ""
    reference_text: str = ""
    references: list[RefSegment] | None = None
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True
    instruct: str = ""                      # 叠加风格控制（保留音色的同时调情绪/语速）
    # —— fish 兼容字段（VoxCPM 不直接用到的安全忽略）——
    temperature: float = 0.7
    top_p: float = 0.7
    chunk_length: int = 200
    repetition_penalty: float = 1.2
    seed: int | None = None
    # —— VoxCPM 原生旋钮 ——
    cfg_value: float = CFG_VALUE
    inference_timesteps: int = TIMESTEPS

class InstructTTSRequest(BaseModel):
    text: str
    instruct: str = ""
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True

class PrewarmRequest(BaseModel):
    references: list[RefSegment]

# ── 参考音临时文件缓存（按内容哈希复用，避免每次落盘 + 供 prewarm 预建）──
_REF_DIR = os.path.join(tempfile.gettempdir(), "voxcpm_refs")
os.makedirs(_REF_DIR, exist_ok=True)
_ref_paths: dict[str, str] = {}
_ref_lock = threading.Lock()

def _ref_to_path(audio_b64: str) -> str:
    """把 base64 WAV 落成临时 .wav 文件并按内容哈希缓存，返回路径。"""
    raw = base64.b64decode(audio_b64)
    h = hashlib.md5(raw).hexdigest()
    with _ref_lock:
        p = _ref_paths.get(h)
        if p and os.path.exists(p):
            return p
        p = os.path.join(_REF_DIR, f"{h}.wav")
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(raw)
        _ref_paths[h] = p
        return p

def _styled_text(text: str, instruct: str) -> str:
    """VoxCPM2 通过文本前缀的括号描述控制风格/Voice Design：'(young, cheerful)正文'。
    已自带括号前缀则不重复包裹。"""
    instruct = (instruct or "").strip()
    if not instruct:
        return text
    if text.lstrip().startswith("("):
        return text
    return f"({instruct}){text}"

# ── 后端抽象 ─────────────────────────────────────────────────────────
class _Backend:
    sample_rate = 48000
    def ready(self) -> bool: ...
    def synth(self, text: str, *, ref_path: str = "", prompt_text: str = "",
              instruct: str = "", cfg: float = CFG_VALUE, steps: int = TIMESTEPS):
        """返回 (pcm_float_ndarray, sample_rate)。"""
        ...
    def synth_stream(self, text: str, *, ref_path: str = "", prompt_text: str = "",
                     instruct: str = "", cfg: float = CFG_VALUE, steps: int = STREAM_TIMESTEPS):
        """生成 PCM16(mono) bytes 段（默认基于整段合成切片，子类可覆盖为原生流式）。"""
        import numpy as np
        wav, _sr = self.synth(text, ref_path=ref_path, prompt_text=prompt_text,
                              instruct=instruct, cfg=cfg, steps=steps)
        pcm16 = (np.asarray(wav) * 32767).clip(-32768, 32767).astype(np.int16)
        step = max(1, self.sample_rate // 5)            # ~200ms 一段
        for i in range(0, len(pcm16), step):
            yield pcm16[i:i + step].tobytes()


class InProcBackend(_Backend):
    """进程内加载 VoxCPM2：能力最全（含原生 generate_streaming），首音最优。"""
    def __init__(self):
        self._model = None
        self._load_lock = threading.Lock()

    def ready(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is not None:
            return self._model
        # 非阻塞抢锁：加载中(下载/初始化)的请求线程拿不到锁即返回 None（调用方走兜底/重试）。
        if not self._load_lock.acquire(blocking=False):
            return None
        try:
            if self._model is not None:
                return self._model
            logger.info(f"加载 VoxCPM2: {MODEL_SRC} (device={DEVICE}, denoiser={LOAD_DENOISE})")
            from voxcpm import VoxCPM
            self._model = VoxCPM.from_pretrained(MODEL_SRC, load_denoiser=LOAD_DENOISE)
            try:
                self.sample_rate = int(self._model.tts_model.sample_rate)
            except Exception:
                self.sample_rate = 48000
            try:
                import torch as _torch
                _torch.set_num_threads(1)
            except Exception:
                pass
            logger.info(f"VoxCPM2 就绪，采样率 {self.sample_rate}Hz")
            return self._model
        except Exception as e:
            logger.error(f"VoxCPM2 加载失败: {e}", exc_info=True)
            return None
        finally:
            self._load_lock.release()

    def _gen_kwargs(self, text, ref_path, prompt_text, instruct, cfg, steps):
        kw = {"text": _styled_text(text, instruct),
              "cfg_value": float(cfg), "inference_timesteps": int(steps)}
        if ref_path:
            kw["reference_wav_path"] = ref_path                 # 隔离参考克隆
            if prompt_text:                                      # 极致克隆：同一参考兼作续写提示
                kw["prompt_wav_path"] = ref_path
                kw["prompt_text"] = prompt_text
        return kw

    def synth(self, text, *, ref_path="", prompt_text="", instruct="",
              cfg=CFG_VALUE, steps=TIMESTEPS):
        m = self._load()
        if m is None:
            raise RuntimeError("VoxCPM2 模型未就绪（加载中或失败）")
        import numpy as np
        wav = m.generate(**self._gen_kwargs(text, ref_path, prompt_text, instruct, cfg, steps))
        return np.asarray(wav, dtype="float32"), self.sample_rate

    def synth_stream(self, text, *, ref_path="", prompt_text="", instruct="",
                     cfg=CFG_VALUE, steps=STREAM_TIMESTEPS):
        m = self._load()
        if m is None:
            raise RuntimeError("VoxCPM2 模型未就绪")
        import numpy as np
        kw = self._gen_kwargs(text, ref_path, prompt_text, instruct, cfg, steps)
        gen = getattr(m, "generate_streaming", None)
        if gen is None:                                          # 老版本无流式 → 回退整段切片
            yield from super().synth_stream(text, ref_path=ref_path, prompt_text=prompt_text,
                                            instruct=instruct, cfg=cfg, steps=steps)
            return
        for chunk in gen(**kw):
            arr = np.asarray(chunk, dtype="float32")
            if arr.size == 0:
                continue
            yield (arr * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


class VLLMOmniBackend(_Backend):
    """代理到 vLLM-Omni 的 OpenAI 兼容 /v1/audio/speech（多租户高并发）。
    克隆依赖后端扩展字段；若后端不支持，建议该机改用 inproc。"""
    def __init__(self):
        self._ok = None

    def ready(self) -> bool:
        if self._ok is None:
            try:
                import requests
                r = requests.get(f"{VLLM_URL}/health", timeout=2)
                self._ok = r.status_code == 200
            except Exception:
                self._ok = False
        return bool(self._ok)

    def synth(self, text, *, ref_path="", prompt_text="", instruct="",
              cfg=CFG_VALUE, steps=TIMESTEPS):
        import requests, numpy as np
        payload = {"model": VLLM_MODEL, "input": _styled_text(text, instruct),
                   "voice": "default", "response_format": "wav"}
        if ref_path:                                             # best-effort：透传扩展字段
            try:
                with open(ref_path, "rb") as f:
                    payload["reference_audio_b64"] = base64.b64encode(f.read()).decode()
            except Exception:
                pass
        r = requests.post(f"{VLLM_URL}/v1/audio/speech", json=payload, timeout=120,
                          headers=app_config.service_headers())
        if r.status_code != 200:
            raise RuntimeError(f"vLLM-Omni HTTP {r.status_code}: {r.text[:160]}")
        wav, sr = _wav_to_float(r.content)
        self.sample_rate = sr
        return wav, sr


_backend: _Backend = VLLMOmniBackend() if BACKEND == "vllm" else InProcBackend()

# ── 音频工具 ─────────────────────────────────────────────────────────
def _wav_bytes(pcm_float, sr: int) -> bytes:
    import numpy as np
    pcm16 = (np.asarray(pcm_float) * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()

def _wav_to_float(wav_bytes: bytes):
    import numpy as np
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    arr = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    return arr, sr

def _b64_wav(wav_bytes: bytes) -> str:
    return base64.b64encode(wav_bytes).decode()

def _refs_from_request(req: CloneTTSRequest):
    """返回 (ref_path, prompt_text)。多段时取首段为主参考（VoxCPM 单参考克隆）。"""
    if req.references:
        seg = next((s for s in req.references if s.audio_b64), None)
        if seg:
            return _ref_to_path(seg.audio_b64), (seg.text or "")
    if req.reference_audio_b64:
        return _ref_to_path(req.reference_audio_b64), (req.reference_text or "")
    return "", ""

# ── 端点 ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "engine": "voxcpm2",
                         "backend": BACKEND, "model_loaded": _backend.ready()})

@app.get("/v1/status")
async def status():
    return JSONResponse({"status": "ok", "engine": "voxcpm2", "backend": BACKEND,
                         "model_src": MODEL_SRC, "model_loaded": _backend.ready(),
                         "sample_rate": _backend.sample_rate})

@app.post("/v1/tts")
async def tts(req: TTSRequest):
    """基础合成 / Voice Design（instruct 非空时按文字描述造声，无需参考音）。"""
    try:
        wav, sr = _backend.synth(req.text, instruct=req.instruct,
                                 cfg=req.cfg_value, steps=req.inference_timesteps)
        return JSONResponse({"audio_base64": _b64_wav(_wav_bytes(wav, sr)),
                             "sample_rate": sr, "ok": True})
    except Exception as e:
        logger.error(f"TTS 失败: {e}")
        raise HTTPException(500, str(e))

@app.post("/v1/tts/clone")
async def clone_tts(req: CloneTTSRequest):
    """零样本声音克隆（隔离参考；带 reference_text 时启用极致续写克隆；instruct 叠加风格）。"""
    try:
        ref_path, prompt_text = _refs_from_request(req)
        if not ref_path:
            raise HTTPException(400, "缺少参考音频")
        wav, sr = _backend.synth(req.text, ref_path=ref_path, prompt_text=prompt_text,
                                 instruct=req.instruct, cfg=req.cfg_value,
                                 steps=req.inference_timesteps)
        return JSONResponse({"audio_base64": _b64_wav(_wav_bytes(wav, sr)),
                             "sample_rate": sr, "ok": True,
                             "n_refs": len(req.references or [1])})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"克隆 TTS 失败: {e}")
        raise HTTPException(500, str(e))

@app.post("/v1/tts/clone/stream")
def clone_tts_stream(req: CloneTTSRequest):
    """流式克隆 TTS：协议同 fish_speech_server —— 二进制流，每块 = 4字节小端长度 + PCM16 mono，
    末尾 0 长度帧结束，采样率在 X-Sample-Rate 头。供 Hub 单句内流式喂口型，压低 TTFA。"""
    ref_path, prompt_text = _refs_from_request(req)
    if not ref_path:
        raise HTTPException(400, "缺少参考音频")

    def gen():
        t0 = time.time(); n = 0
        try:
            for pcm in _backend.synth_stream(req.text, ref_path=ref_path,
                                             prompt_text=prompt_text, instruct=req.instruct,
                                             cfg=req.cfg_value, steps=STREAM_TIMESTEPS):
                if not pcm:
                    continue
                n += 1
                if n == 1:
                    logger.info(f"stream first chunk {len(pcm)//2} samp @ "
                                f"{(time.time()-t0)*1000:.0f}ms")
                yield struct.pack("<I", len(pcm)) + pcm
        except GeneratorExit:
            # 客户端提前断流。GeneratorExit 后再 yield 结束帧= RuntimeError: generator
            # ignored GeneratorExit(fish 侧 2026-07-06 实锤过)；对端已断,直接收尾。
            logger.info(f"stream client-abort: {n} chunks, {len(req.text)}chars, "
                        f"{(time.time()-t0)*1000:.0f}ms")
            raise
        except Exception as e:
            logger.exception(f"流式克隆 TTS 失败: {e}")
        # 结束帧只在客户端仍在收时发；不能放 finally——断流路径禁止 yield
        yield struct.pack("<I", 0)
        logger.info(f"stream done: {n} chunks, {len(req.text)}chars, "
                    f"{(time.time()-t0)*1000:.0f}ms")

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(_backend.sample_rate)})

@app.post("/v1/tts/instruct")
async def instruct_tts(req: InstructTTSRequest):
    """指令式 TTS / Voice Design：instruct 为自然语言风格描述（VoxCPM2 原生强项）。"""
    try:
        wav, sr = _backend.synth(req.text, instruct=req.instruct)
        return JSONResponse({"audio_base64": _b64_wav(_wav_bytes(wav, sr)),
                             "sample_rate": sr, "ok": True})
    except Exception as e:
        logger.error(f"指令 TTS 失败: {e}")
        raise HTTPException(500, str(e))

@app.post("/v1/refs/prewarm")
async def prewarm_refs(req: PrewarmRequest):
    """预热参考音：把多段参考预先落临时 wav 缓存（按内容哈希），降低随后首次克隆 TTFA。不生成音频。"""
    try:
        paths = [_ref_to_path(s.audio_b64) for s in req.references if s.audio_b64]
        if not paths:
            return JSONResponse({"ok": False, "detail": "无参考"}, status_code=400)
        return JSONResponse({"ok": True, "n_refs": len(paths), "cached": True})
    except Exception as e:
        logger.error(f"参考预热失败: {e}")
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=500)

@app.on_event("startup")
async def startup():
    if BACKEND == "inproc":
        import asyncio
        asyncio.get_event_loop().run_in_executor(None, _backend._load)  # type: ignore[attr-defined]
    logger.info(f"VoxCPM2 TTS server 启动，端口 {PORT}，后端 {BACKEND}（模型后台加载中...）")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
