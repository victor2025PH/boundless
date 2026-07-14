# -*- coding: utf-8 -*-
"""
s1_tts_server.py — OpenAudio S1-mini 适配服务（端口 7863）

把官方 api_server(7862, msgpack 契约)翻译成本项目 Fish 兼容 HTTP 契约，
使 live_interpreter/AvatarHub 零特判路由。S1 特性直通：
  · 文本内联情感标记 (excited)/(laughing)/(sobbing)/(whispering)…50+ 种
  · emotion 字段 → 自动映射为句首标记(调用方无需懂 S1 标记语法)
  · 零样本克隆(参考音随请求带,服务端 memory cache 命中后极快)

接口（与 fish_speech_server.py 对齐）：
  GET  /health
  POST /v1/tts/clone          {text, reference_audio_b64, reference_text, emotion?} → {audio_base64}
  POST /v1/tts/clone/stream   4字节小端长度前缀 + PCM16 mono；0 长度帧结束；X-Sample-Rate 头

运行：sbv2 env（仅需 requests/fastapi/ormsgpack）
  python s1_tts_server.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import struct
import sys
import wave

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [S1TTS] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("S1TTS")

import ormsgpack
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import service_auth

PORT = int(os.environ.get("S1_TTS_PORT", "7863"))
UPSTREAM = os.environ.get("S1_UPSTREAM", "http://127.0.0.1:7862").rstrip("/")
S1_SR = 44100                       # S1 codec 输出采样率(dac 44.1k)

# 本系统情绪标签 → S1 标记(调用方给 emotion 字段时自动注入;文本已含 (xxx) 标记则不重复)
_EMO_MARK = {
    "excited": "(excited)", "happy": "(delighted)", "angry": "(angry)",
    "sad": "(sad)", "surprised": "(astonished)", "fearful": "(scared)",
    "gentle": "(comforting)", "calm": "(relaxed)", "serious": "(serious)",
    "disgusted": "(disgusted)",
}

app = FastAPI(title="OpenAudio S1-mini Adapter", version="1.0.0")
service_auth.secure(app, name="s1_tts")


class CloneTTSRequest(BaseModel):
    text: str
    reference_audio_b64: str = ""
    reference_text: str = ""
    references: list | None = None
    language: str = "ja"
    speed: float = 1.0                # S1 不支持;保留字段兼容契约
    emotion: str = ""
    return_base64: bool = True
    temperature: float = 0.8
    top_p: float = 0.8
    chunk_length: int = 300
    repetition_penalty: float = 1.1
    seed: int | None = 42
    max_new_tokens: int = 1024


def _mk_text(req: CloneTTSRequest) -> str:
    text = (req.text or "").strip()
    if req.emotion and "(" not in text[:14]:
        mark = _EMO_MARK.get(req.emotion.strip().lower())
        if mark:
            text = f"{mark}{text}"
    return text


def _refs(req: CloneTTSRequest) -> list:
    out = []
    if req.references:
        for r in req.references:
            a = r.get("audio_b64") if isinstance(r, dict) else ""
            t = (r.get("text") if isinstance(r, dict) else "") or ""
            if a:
                out.append({"audio": base64.b64decode(a), "text": t})
    if not out and req.reference_audio_b64:
        out.append({"audio": base64.b64decode(req.reference_audio_b64),
                    "text": req.reference_text or ""})
    return out


def _upstream_payload(req: CloneTTSRequest, fmt: str, streaming: bool) -> bytes:
    payload = {
        "text": _mk_text(req),
        "references": _refs(req),
        "format": fmt,
        "streaming": streaming,
        "max_new_tokens": req.max_new_tokens,
        "chunk_length": max(100, min(1000, req.chunk_length)),
        "temperature": max(0.1, min(1.0, req.temperature)),
        "top_p": max(0.1, min(1.0, req.top_p)),
        "repetition_penalty": max(0.9, min(2.0, req.repetition_penalty)),
        "seed": req.seed,
        "use_memory_cache": "on",     # 同参考音复用编码(逐句通话的关键提速)
        "normalize": False,           # ja 不做 en/zh 正则化,防标记/假名被改写
    }
    return ormsgpack.packb(payload)


@app.get("/health")
def health():
    up = False
    try:
        up = requests.get(f"{UPSTREAM}/v1/health", timeout=3).ok
    except Exception:
        pass
    return {"status": "ok", "engine": "s1_mini", "model_loaded": up,
            "upstream": UPSTREAM, "markers": sorted(_EMO_MARK)}


@app.post("/v1/tts/clone")
def tts_clone(req: CloneTTSRequest):
    if not req.text.strip():
        raise HTTPException(400, "text empty")
    r = requests.post(f"{UPSTREAM}/v1/tts", data=_upstream_payload(req, "wav", False),
                      headers={"Content-Type": "application/msgpack"}, timeout=300)
    if r.status_code != 200:
        raise HTTPException(502, f"S1 upstream {r.status_code}: {r.text[:120]}")
    logger.info(f"clone ok emotion={req.emotion or '-'} bytes={len(r.content)}")
    return {"ok": True, "audio_base64": base64.b64encode(r.content).decode(),
            "sample_rate": S1_SR, "engine": "s1_mini"}


@app.post("/v1/tts/clone/stream")
def tts_clone_stream(req: CloneTTSRequest):
    """上游 pcm 流式 → 重组为 4字节长度前缀协议(与 fish/qwen3/sbv2 服务一致)。"""
    if not req.text.strip():
        raise HTTPException(400, "text empty")
    up = requests.post(f"{UPSTREAM}/v1/tts", data=_upstream_payload(req, "pcm", True),
                       headers={"Content-Type": "application/msgpack"},
                       stream=True, timeout=300)
    if up.status_code != 200:
        raise HTTPException(502, f"S1 upstream {up.status_code}: {up.text[:120]}")

    def gen():
        try:
            for chunk in up.iter_content(chunk_size=8192):
                if chunk:
                    yield struct.pack("<I", len(chunk)) + chunk
            yield struct.pack("<I", 0)
        finally:
            up.close()

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(S1_SR), "X-Engine": "s1_mini"})


if __name__ == "__main__":
    logger.info(f"S1 adapter starting port={PORT} upstream={UPSTREAM}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
