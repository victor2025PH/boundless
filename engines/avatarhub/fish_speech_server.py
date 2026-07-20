# -*- coding: utf-8 -*-
"""
fish_speech_server.py — Fish-Speech S2 TTS 适配服务（端口 7855）
运行环境: fishspeech conda env (Python 3.12, PyTorch 2.8.0+cu128)

接口兼容 emotion_tts_server.py：
  GET  /health
  GET  /v1/status
  POST /v1/tts          {text, language, return_base64, speed, ...}
  POST /v1/tts/clone    {text, reference_audio_b64, reference_text, ...}
  POST /v1/tts/instruct {text, instruct, ...}

返回: {"audio_base64": "<wav_bytes_b64>"}
功能: 零样本声音克隆（10~30s 参考音频），业界最佳中/英 WER（0.54%/0.99%）。

依赖：conda env fishspeech 中安装了 fish-speech (pip install -e C:\模仿音色\fish-speech[cu128])
模型：需下载到 MODEL_DIR（见下方配置）
"""
from __future__ import annotations
import sys, os, io, base64, json, time, wave, struct
import logging

# 抑制「推理后 OpenMP 线程常驻空转」：Fish-Speech 为 GPU 推理(llama AR + FireflyGAN)，
# CPU 仅做轻量分词/拼接。多线程时主线程会忙等(spin-wait)其余线程，单卡多服务叠加可烧 1~2.5 核
# → 桌面/打字全局卡顿。限 1 线程 + PASSIVE 后，空闲(含推理后)CPU≈0，合成吞吐几乎不受影响。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("KMP_BLOCKTIME", "0")

# torch.compile on Windows needs two fixes baked in (proven on 5090: RTF 1.1→0.3, ~3-4x):
#  1) SHORT triton/inductor cache dirs — the deep AppData\Temp cache path + fish's long fused
#     kernel name overflow Windows' 260-char PATH limit (FileNotFoundError tmp.pid_*.json).
#  2) plain inductor mode (NOT cudagraphs) — "reduce-overhead" hits an LLP64 overflow on
#     Windows ("int too large to convert to C long", C long is 32-bit); plain inductor is clean.
# All setdefault so a launcher / Linux node can still override. Must stay BEFORE torch import.
os.environ.setdefault("TRITON_CACHE_DIR", r"C:\tc")
os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", r"C:\ic")
os.environ.setdefault("FISH_COMPILE_MODE", "default")
os.environ.setdefault("TORCHINDUCTOR_FALLBACK_RANDOM", "1")
os.environ.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "1")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FishTTS] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("FishTTS")

import pyrootutils
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional

# ── 配置 ─────────────────────────────────────────────────────────────
import app_config
PORT       = int(os.environ.get("FISH_PORT", "7855"))   # 多卡：每副本一端口(配 CUDA_VISIBLE_DEVICES 绑卡)
# v1.5 代码根目录（与 fish-speech-1.5 checkpoint 兼容的 FireflyGAN 架构）
FISH_ROOT  = str(app_config.BASE / "fish-speech-v1.5")
MODEL_DIR  = os.environ.get("FISH_MODEL_DIR",
                            str(app_config.BASE / "fish-speech" / "checkpoints" / "fish-speech-1.5"))
DEVICE     = os.environ.get("FISH_DEVICE", "cuda")
HALF       = os.environ.get("FISH_HALF", "1") == "1"
# torch.compile：在 Windows 上需 triton-windows(匹配 torch 版本)+MSVC。实测 5090 上
# 把自回归解码从 ~10 tok/s 提到数倍。若环境缺 triton/编译器导致启动失败，设 FISH_COMPILE=0 回退。
COMPILE    = os.environ.get("FISH_COMPILE", "1") == "1"

# ── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="Fish-Speech S2 TTS", version="1.5.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="fish_tts")            # 替代原 CORS:* 无鉴权

# ── 请求/响应 Schema ─────────────────────────────────────────────────
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
    reference_audio_b64: str = ""          # 单段（向后兼容）
    reference_text: str = ""
    references: list[RefSegment] | None = None  # 多段融合（优先于单段）
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True
    temperature: float = 0.7
    top_p: float = 0.7
    chunk_length: int = 200
    repetition_penalty: float = 1.2
    seed: int | None = None

class InstructTTSRequest(BaseModel):
    text: str
    instruct: str = ""
    language: str = "zh"
    speed: float = 1.0
    return_base64: bool = True

# ── 模型状态 ─────────────────────────────────────────────────────────
_engine = None
_sample_rate = 44100
import threading as _threading
# 加载锁：后台启动线程持锁编译(~3min)期间，请求线程非阻塞抢锁失败即返回 None →
# 调用方(Hub)走 CosyVoice 兜底，绝不在请求线程触发第二次加载或被阻塞 3 分钟。
_LOAD_LOCK = _threading.Lock()
_LOCK = None

def _get_sample_rate():
    global _sample_rate
    return _sample_rate

def _wav_bytes(pcm_float: "np.ndarray", sr: int) -> bytes:
    """numpy float32/float64 → WAV bytes (16-bit PCM)"""
    import numpy as np
    pcm16 = (pcm_float * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _load_engine():
    global _engine, _sample_rate
    if _engine is not None:
        return _engine

    if not os.path.exists(MODEL_DIR):
        logger.warning(f"模型目录不存在: {MODEL_DIR}，等待下载...")
        return None

    # 非阻塞抢锁：编译/加载进行中的请求线程拿不到锁 → 立刻返回 None（调用方走兜底）
    if not _LOAD_LOCK.acquire(blocking=False):
        return None
    try:
        if _engine is not None:   # 等锁期间已被后台线程加载好
            return _engine
        # 使用 v1.5 代码目录（兼容 FireflyGAN checkpoint）
        pyrootutils.setup_root(FISH_ROOT, indicator=".project-root", pythonpath=True)
        if FISH_ROOT not in sys.path:
            sys.path.insert(0, FISH_ROOT)

        logger.info(f"加载 Fish-Speech-1.5 模型: {MODEL_DIR}")

        # 动态导入 v1.5 的 ModelManager
        import importlib.util, types

        def _load_module(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod

        # 用 v1.5 的 ModelManager
        from tools.server.model_manager import ModelManager as ModelManager15

        decoder_cfg = os.environ.get("FISH_DECODER_CFG", "firefly_gan_vq")
        llama_ckpt  = MODEL_DIR  # llama 模型目录（含 model.pth 和 config.json）

        # 自动找 firefly decoder checkpoint
        decoder_ckpt = None
        for cname in sorted(os.listdir(MODEL_DIR)):
            if cname.endswith(".pth") and "model" not in cname.lower():
                decoder_ckpt = os.path.join(MODEL_DIR, cname)
                break
        if decoder_ckpt is None:
            # 回退到任意 .pth
            for cname in os.listdir(MODEL_DIR):
                if cname.endswith(".pth"):
                    decoder_ckpt = os.path.join(MODEL_DIR, cname)
                    break
        logger.info(f"decoder ckpt: {decoder_ckpt}")

        mm = ModelManager15(
            mode="tts",
            device=DEVICE,
            half=HALF,
            compile=COMPILE,
            asr_enabled=False,
            llama_checkpoint_path=llama_ckpt,
            decoder_checkpoint_path=decoder_ckpt,
            decoder_config_name=decoder_cfg,
        )
        _engine = mm.tts_inference_engine
        try:
            import torch as _torch
            _torch.set_num_threads(1)   # GPU 推理为主，消除 CPU 线程协调忙等
        except Exception:
            pass
        # FireflyArchitecture (v1.5) 采样率来自配置，不在模型属性上
        try:
            _sample_rate = _engine.decoder_model.sample_rate
        except AttributeError:
            _sample_rate = 44100  # fish-speech-1.5 FireflyGAN 固定 44100Hz
        logger.info(f"Fish-Speech 模型加载完成，采样率 {_sample_rate}Hz")
        return _engine
    except Exception as e:
        logger.error(f"模型加载失败: {e}", exc_info=True)
        return None
    finally:
        _LOAD_LOCK.release()


def _run_tts(text: str, references: list[dict], instruct: str = "",
             temperature: float = 0.7, top_p: float = 0.7,
             chunk_length: int = 200, repetition_penalty: float = 1.2,
             seed: int | None = None) -> bytes:
    """调用 fish-speech-1.5 inference，返回 WAV bytes。"""
    from tools.schema import ServeTTSRequest, ServeReferenceAudio
    from tools.server.inference import inference_wrapper as inference_fn

    eng = _load_engine()
    if eng is None:
        raise RuntimeError("Fish-Speech 模型未加载（模型不存在或加载失败）")

    refs = [ServeReferenceAudio(audio=r["audio"], text=r["text"]) for r in references]
    req = ServeTTSRequest(
        text=text,
        references=refs,
        streaming=False,
        format="wav",
        normalize=True,
        use_memory_cache="on",   # 按音频内容哈希缓存参考编码：多段融合时跨句/跨轮复用，省去每次重编码（降 TTFA）
        temperature=max(0.1, min(1.0, temperature)),
        top_p=max(0.1, min(1.0, top_p)),
        chunk_length=max(100, min(300, chunk_length)),
        repetition_penalty=max(0.9, min(2.0, repetition_penalty)),
        seed=seed,
    )
    import numpy as np
    audio_np = next(inference_fn(req, eng))
    return _wav_bytes(audio_np, _sample_rate)


def _b64_wav(wav_bytes: bytes) -> str:
    return base64.b64encode(wav_bytes).decode()


# ── 端点 ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "engine": "fish_speech_s2",
                         "model_loaded": _engine is not None})

@app.get("/v1/status")
async def status():
    return JSONResponse({"status": "ok", "engine": "fish_speech_s2",
                         "model_dir": MODEL_DIR,
                         "model_loaded": _engine is not None,
                         "sample_rate": _sample_rate})

@app.post("/v1/tts")
async def tts(req: TTSRequest):
    """基本 TTS（无参考音频，使用模型默认音色）"""
    try:
        wav = _run_tts(req.text, references=[], instruct="")
        return JSONResponse({"audio_base64": _b64_wav(wav),
                             "sample_rate": _sample_rate, "ok": True})
    except Exception as e:
        logger.error(f"TTS 失败: {e}")
        raise HTTPException(500, str(e))

@app.post("/v1/tts/clone")
async def clone_tts(req: CloneTTSRequest):
    """零样本声音克隆：支持单段(reference_audio_b64) 或多段融合(references[])。
    多段时把每段作为独立 in-context 说话人提示，比拼接音频更稳更像。"""
    try:
        # 多段融合优先：references 非空时用全部片段
        if req.references:
            refs = [{"audio": base64.b64decode(s.audio_b64), "text": s.text}
                    for s in req.references if s.audio_b64]
        else:
            refs = [{"audio": base64.b64decode(req.reference_audio_b64),
                     "text": req.reference_text}]
        if not refs:
            raise HTTPException(400, "缺少参考音频")
        wav = _run_tts(req.text, references=refs,
                       temperature=req.temperature, top_p=req.top_p,
                       chunk_length=req.chunk_length,
                       repetition_penalty=req.repetition_penalty,
                       seed=req.seed)
        return JSONResponse({"audio_base64": _b64_wav(wav),
                             "sample_rate": _sample_rate, "ok": True,
                             "n_refs": len(refs)})
    except Exception as e:
        logger.error(f"克隆 TTS 失败: {e}")
        raise HTTPException(500, str(e))


def _run_tts_stream(text: str, references: list[dict],
                    temperature: float = 0.7, top_p: float = 0.7,
                    chunk_length: int = 200, repetition_penalty: float = 1.2,
                    seed: int | None = None):
    """流式克隆合成：engine 以 streaming=True 逐段产出 PCM16 → 边出边吐。
    yield 每段为 raw PCM16(int16, mono) bytes；final 段可能是 float np → 统一转 int16。"""
    from tools.schema import ServeTTSRequest, ServeReferenceAudio
    from tools.server.inference import inference_wrapper as inference_fn
    import numpy as np
    eng = _load_engine()
    if eng is None:
        raise RuntimeError("Fish-Speech 模型未加载")
    refs = [ServeReferenceAudio(audio=r["audio"], text=r["text"]) for r in references]
    sreq = ServeTTSRequest(
        text=text, references=refs, streaming=True, format="wav", normalize=True,
        use_memory_cache="on",
        temperature=max(0.1, min(1.0, temperature)),
        top_p=max(0.1, min(1.0, top_p)),
        # 流式下放开 chunk_length 下限到 20 字节(~6-7 个汉字)：让短首句也能被按字节切成
        # 多段，使首段在生成 ~首块字数 token 后即可 vocode 吐出 → 直接压低 TTFA。
        # 整句(非流式)路径仍用 100 下限(见 _run_tts)，不受影响。
        chunk_length=max(20, min(300, chunk_length)),
        repetition_penalty=max(0.9, min(2.0, repetition_penalty)),
        seed=seed,
    )
    for seg in inference_fn(sreq, eng):
        if seg is None:
            continue
        if isinstance(seg, (bytes, bytearray)):
            yield bytes(seg)                                  # 已是 PCM16 bytes（segment）
        else:
            yield (np.asarray(seg) * 32768).astype(np.int16).tobytes()  # float np（final）→ PCM16


@app.post("/v1/tts/clone/stream")
def clone_tts_stream(req: CloneTTSRequest):
    """流式零样本克隆 TTS（附加端点，不改原 /v1/tts/clone）。协议同 emotion_tts /v1/tts/stream：
    二进制流，每块 = 4字节小端长度前缀 + PCM16 mono；末尾 0 长度帧表示结束。采样率在 X-Sample-Rate。
    供 Hub 单句内流式（边合成边喂口型）使用，破整句合成固定延迟。"""
    if req.references:
        refs = [{"audio": base64.b64decode(s.audio_b64), "text": s.text}
                for s in req.references if s.audio_b64]
    else:
        refs = [{"audio": base64.b64decode(req.reference_audio_b64),
                 "text": req.reference_text}]
    if not refs or not refs[0]["audio"]:
        raise HTTPException(400, "缺少参考音频")

    def gen():
        t0 = time.time()
        n = 0
        try:
            for pcm in _run_tts_stream(req.text, references=refs,
                                       temperature=req.temperature, top_p=req.top_p,
                                       chunk_length=req.chunk_length,
                                       repetition_penalty=req.repetition_penalty,
                                       seed=req.seed):
                if not pcm:
                    continue
                n += 1
                if n == 1:
                    logger.info(f"stream first chunk {len(pcm)//2} samp @ "
                                f"{(time.time()-t0)*1000:.0f}ms")
                yield struct.pack("<I", len(pcm)) + pcm
        except GeneratorExit:
            # 客户端提前断流(拿够了/被打断)。GeneratorExit 语义要求立即退出：此时再 yield
            # 结束帧会抛 RuntimeError: generator ignored GeneratorExit(2026-07-06 监控噪音)，
            # 且对端已断、结束帧无人收——记日志直接收尾。
            logger.info(f"stream client-abort: {n} chunks, {len(req.text)}chars, "
                        f"{(time.time()-t0)*1000:.0f}ms")
            raise
        except Exception as e:
            logger.exception(f"流式克隆 TTS 失败: {e}")
        # 结束帧只走正常完成/合成异常(客户端仍在收)两条路；不能放 finally——断流路径禁止 yield
        yield struct.pack("<I", 0)
        logger.info(f"stream done: {n} chunks, {len(req.text)}chars, "
                    f"{(time.time()-t0)*1000:.0f}ms")

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(_sample_rate)})

class PrewarmRequest(BaseModel):
    references: list[RefSegment]

@app.post("/v1/refs/prewarm")
async def prewarm_refs(req: PrewarmRequest):
    """预热参考编码缓存：把多段参考按内容哈希预先编码进 ref_by_hash，
    使随后首句合成跳过参考编码，显著降低 TTFA。不生成音频。"""
    try:
        from tools.schema import ServeReferenceAudio
        eng = _load_engine()
        if eng is None:
            return JSONResponse({"ok": False, "detail": "模型未加载"}, status_code=503)
        refs = [ServeReferenceAudio(audio=base64.b64decode(s.audio_b64), text=s.text)
                for s in req.references if s.audio_b64]
        if not refs:
            return JSONResponse({"ok": False, "detail": "无参考"}, status_code=400)
        import asyncio as _aio
        # 编码在 CPU/GPU，放线程池避免阻塞事件循环
        await _aio.get_event_loop().run_in_executor(
            None, lambda: eng.load_by_hash(refs, "on"))
        return JSONResponse({"ok": True, "n_refs": len(refs), "cached": True})
    except Exception as e:
        logger.error(f"参考预热失败: {e}")
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=500)

@app.post("/v1/tts/instruct")
async def instruct_tts(req: InstructTTSRequest):
    """指令式 TTS（Fish-Speech 自然语言控制语气/情感）"""
    try:
        # Fish-Speech S2 支持 [instruct] tag 内联或通过 references 的文本提示
        # 此处将 instruct 拼在文本前作为提示（兼容写法）
        full_text = req.text
        if req.instruct:
            full_text = f"[{req.instruct}] {req.text}"
        wav = _run_tts(full_text, references=[])
        return JSONResponse({"audio_base64": _b64_wav(wav),
                             "sample_rate": _sample_rate, "ok": True})
    except Exception as e:
        logger.error(f"指令 TTS 失败: {e}")
        raise HTTPException(500, str(e))


@app.on_event("startup")
async def startup():
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_engine)
    logger.info(f"Fish-Speech TTS server 启动，端口 {PORT}（模型后台加载中...）")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
