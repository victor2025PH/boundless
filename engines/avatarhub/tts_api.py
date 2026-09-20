"""
TTS API Server  —  端口 7851
基于本地 XTTS-v2 模型，完全离线，支持声音克隆（参考音频 → 模仿该声音）
支持：
  1. OpenAI 兼容接口  POST /v1/audio/speech  （用 voices/ 目录参考音频）
  2. 声音克隆接口    POST /v1/audio/clone    （上传参考音频 base64）
  3. 健康检查        GET  /health
  4. 模型列表        GET  /v1/models
"""
import io
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault("COQUI_TOS_AGREED", "1")

# PyTorch >= 2.6 默认 weights_only=True 会阻止加载 XTTS 模型，此处打补丁
import torch as _torch
_orig_load = _torch.load
def _patched_load(*a, **kw):
    kw.setdefault("weights_only", False)
    return _orig_load(*a, **kw)
_torch.load = _patched_load

import base64
import re
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional, Generator

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel
import uvicorn

# ── 配置 ────────────────────────────────────────────────────────
import app_config
VOICES_DIR = app_config.BASE / "alltalk_tts" / "voices"
PORT       = int(os.environ.get("TTS_PORT") or app_config.port("tts") or 7851)

app = FastAPI(title="TTS API (XTTS-v2 Local)", version="3.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="tts")                 # 替代原 CORS:* 无鉴权

# ── 模型加载（启动时预热）───────────────────────────────────────
_tts = None

def _get_tts():
    global _tts
    if _tts is None:
        from TTS.api import TTS
        print("[TTS] 加载本地 XTTS-v2 模型...", flush=True)
        _tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
        print("[TTS] 模型加载完成 ✅", flush=True)
    return _tts


def _synthesize(text: str, speaker_wav: str, language: str = "zh-cn") -> bytes:
    tts = _get_tts()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out_path = tmp.name
    tts.tts_to_file(text=text, speaker_wav=speaker_wav,
                    language=language, file_path=out_path)
    data = Path(out_path).read_bytes()
    Path(out_path).unlink(missing_ok=True)
    return data


def _split_sentences(text: str) -> list:
    """将长文本切分为句子，用于分句流式合成"""
    # 中文标点 + 英文标点
    parts = re.split(r'(?<=[。！？；\n.!?;])', text)
    sentences = [s.strip() for s in parts if s.strip()]
    if not sentences:
        sentences = [text]
    return sentences


def _make_wav_header(sample_rate: int = 22050, bits: int = 16, channels: int = 1,
                     data_size: int = 0) -> bytes:
    """生成 WAV 文件头"""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
        b'data', data_size)
    return header


def _stream_sentences(text: str, speaker_wav: str, language: str = "zh-cn") -> Generator[bytes, None, None]:
    """分句流式合成：每合成一句就立即返回 WAV 数据块"""
    sentences = _split_sentences(text)
    for i, sentence in enumerate(sentences):
        if not sentence:
            continue
        try:
            chunk = _synthesize(sentence, speaker_wav, language)
            yield chunk
        except Exception as e:
            print(f"[TTS Stream] 句{i} 合成失败: {e}", flush=True)


def _list_voices():
    if not VOICES_DIR.exists():
        return []
    return [f.name for f in sorted(VOICES_DIR.glob("*.wav"))]


# ── 路由 ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "engine": "xtts_v2_local",
            "model_loaded": _tts is not None, "ready": True}


@app.get("/meminfo")
def meminfo():
    info = {"service": "tts_api"}
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


@app.get("/v1/models")
def models():
    return {"object": "list",
            "data": [{"id": v, "object": "voice"} for v in _list_voices()]}


@app.get("/voices")
def voices():
    return {"voices": _list_voices()}


# ── 1. OpenAI 兼容接口（用 voices/ 目录参考音频）────────────────

class SpeechRequest(BaseModel):
    model:           str = "xtts_v2"
    input:           str
    voice:           str = "female_01.wav"   # voices/ 目录下文件名
    language:        str = "zh-cn"
    response_format: str = "wav"


@app.post("/v1/audio/speech")
def create_speech(req: SpeechRequest):
    voice_file = VOICES_DIR / req.voice
    if not voice_file.exists():
        wavs = list(VOICES_DIR.glob("*.wav"))
        if not wavs:
            raise HTTPException(400, f"voices 目录无参考音频: {VOICES_DIR}")
        voice_file = wavs[0]
    try:
        audio = _synthesize(req.input, str(voice_file), req.language)
    except Exception as e:
        raise HTTPException(500, f"合成失败: {e}")
    return Response(content=audio, media_type="audio/wav",
                    headers={"Content-Disposition": "attachment; filename=speech.wav"})


# ── 2. 声音克隆接口（base64 参考音频）──────────────────────────

class CloneRequest(BaseModel):
    text: str
    language: str = "zh-cn"
    reference_audio_base64: str


@app.post("/v1/audio/clone")
def clone_voice(req: CloneRequest):
    b64 = req.reference_audio_base64.split(",", 1)[-1]
    try:
        ref_bytes = base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(400, f"base64 解码失败: {e}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(ref_bytes); ref_path = tmp.name
    try:
        audio = _synthesize(req.text, ref_path, req.language)
    except Exception as e:
        raise HTTPException(500, f"克隆失败: {e}")
    finally:
        Path(ref_path).unlink(missing_ok=True)
    return JSONResponse({"audio_base64": base64.b64encode(audio).decode(), "format": "wav"})


# ── 3. 流式合成接口 ─────────────────────────────────────────────

class StreamRequest(BaseModel):
    model:    str = "xtts_v2"
    input:    str
    voice:    str = "female_01.wav"
    language: str = "zh-cn"


@app.post("/v1/audio/speech/stream")
def stream_speech(req: StreamRequest):
    """分句流式 TTS：长文本切句，逐句合成并以 multipart 方式推送"""
    voice_file = VOICES_DIR / req.voice
    if not voice_file.exists():
        wavs = list(VOICES_DIR.glob("*.wav"))
        if not wavs:
            raise HTTPException(400, "无参考音频")
        voice_file = wavs[0]

    def generate():
        for i, wav_data in enumerate(_stream_sentences(
                req.input, str(voice_file), req.language)):
            # 每个 chunk 是完整 WAV，前端可逐段播放
            yield (b"--boundary\r\n"
                   b"Content-Type: audio/wav\r\n"
                   b"X-Chunk-Index: " + str(i).encode() + b"\r\n\r\n"
                   + wav_data + b"\r\n")
        yield b"--boundary--\r\n"

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=boundary",
        headers={"X-Stream": "true"})


@app.post("/v1/audio/speech/stream_sse")
def stream_speech_sse(req: StreamRequest):
    """SSE 流式 TTS：逐句合成，以 Server-Sent Events 推送 base64 音频块"""
    voice_file = VOICES_DIR / req.voice
    if not voice_file.exists():
        wavs = list(VOICES_DIR.glob("*.wav"))
        if not wavs:
            raise HTTPException(400, "无参考音频")
        voice_file = wavs[0]

    def generate():
        sentences = _split_sentences(req.input)
        for i, sentence in enumerate(sentences):
            if not sentence:
                continue
            t0 = time.time()
            try:
                audio = _synthesize(sentence, str(voice_file), req.language)
                b64 = base64.b64encode(audio).decode()
                elapsed = int((time.time() - t0) * 1000)
                data = f'{{"index":{i},"text":"{sentence}","audio_base64":"{b64}","elapsed_ms":{elapsed}}}'
                yield f"data: {data}\n\n"
            except Exception as e:
                yield f"data: {{\"error\":\"{str(e)}\",\"index\":{i}}}\n\n"
        yield "data: {\"done\":true}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 4. AvatarHub 简化接口 ────────────────────────────────────────

class SynthRequest(BaseModel):
    text:     str
    voice:    str = "female_01.wav"
    language: str = "zh-cn"


@app.post("/synthesize")
def synthesize_api(req: SynthRequest):
    voice_file = VOICES_DIR / req.voice
    if not voice_file.exists():
        wavs = list(VOICES_DIR.glob("*.wav"))
        voice_file = wavs[0] if wavs else None
    if voice_file is None:
        raise HTTPException(400, "无参考音频")
    try:
        audio = _synthesize(req.text, str(voice_file), req.language)
    except Exception as e:
        raise HTTPException(500, str(e))
    return JSONResponse({"audio_base64": base64.b64encode(audio).decode(), "format": "wav"})


if __name__ == "__main__":
    print("=" * 55)
    print(" TTS API 服务 (本地 XTTS-v2 声音克隆)")
    print(f" 监听: http://0.0.0.0:{PORT}")
    print(f" 参考音频目录: {VOICES_DIR}")
    print(f" 可用声音: {', '.join(_list_voices()[:5])}...")
    print(" 完全离线，无需联网")
    print("=" * 55)
    _get_tts()   # 启动时预热模型
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
