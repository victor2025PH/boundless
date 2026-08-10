# -*- coding: utf-8 -*-
"""qwen3_vd_server.py — Qwen3-TTS VoiceDesign 文字造声服务（默认端口 7859）。

用途（P2 音色管线，2026-08-02）：按一句自然语言描述凭空生成全新音色的样本音频，
产物作为「参考音」喂给既有克隆栈（hub Fish / CosyVoice）注册成人设专属声——
新增人设不再依赖真人录音，也没有名人克隆的合规风险。

运行环境：qwen3tts conda env（与 faceX qwen3_tts_server 同配方：py310 + torch cu128 +
pip install -U qwen-tts）。模型 Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign（bf16 约 4-5GB
显存，建议放 32GB 空闲卡，如 .173；**不要**装在已满载的 117/3060）。

接口（与 faceX 服务风格一致，懒加载 + 未就绪 503 优雅降级）：
  GET  /health            {status, engine:"qwen3_vd", model_loaded}
  POST /v1/voice_design   {text, instruct, language="zh", return_base64=true}
                          → {ok, audio_base64(wav), sample_rate, elapsed_ms}
自测：python qwen3_vd_server.py --selftest "温柔清亮的年轻女声，语速稍慢，带一点笑意"
"""
from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import sys
import threading
import time
import wave

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [Qwen3VD] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("Qwen3VD")

PORT = int(os.environ.get("QWEN3_VD_PORT", "7859"))
MODEL_ID = os.environ.get("QWEN3_VD_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
MODEL_DIR = os.environ.get("QWEN3_VD_MODEL_DIR", "")
DEVICE = os.environ.get("QWEN3_VD_DEVICE", "cuda:0")
DTYPE = os.environ.get("QWEN3_VD_DTYPE", "bfloat16")

_model = None
_lock = threading.Lock()
_load_error = ""


def _load():
    global _model, _load_error
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
            src = MODEL_DIR if MODEL_DIR and os.path.isdir(MODEL_DIR) else MODEL_ID
            logger.info("loading %s on %s (%s)…", src, DEVICE, DTYPE)
            _model = Qwen3TTSModel.from_pretrained(
                src, device_map=DEVICE,
                torch_dtype=getattr(torch, DTYPE, torch.bfloat16))
            _load_error = ""
            logger.info("model loaded")
        except Exception as e:  # noqa: BLE001
            _load_error = f"{type(e).__name__}: {e}"
            logger.error("load failed: %s", _load_error)
            raise
    return _model


def _wav_bytes(audio, sr: int) -> bytes:
    import numpy as np
    x = np.asarray(audio, dtype="float32")
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize(text: str, instruct: str, language: str = "zh"):
    m = _load()
    wavs, sr = m.generate_voice_design(
        text=text, instruct=instruct or "",
        language=language or "zh", non_streaming_mode=True)
    if not wavs:
        raise RuntimeError("empty output")
    return _wav_bytes(wavs[0], sr), int(sr)


def build_app():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="Qwen3-TTS VoiceDesign", version="1.0.0")

    class VDRequest(BaseModel):
        text: str
        instruct: str = ""
        language: str = "zh"
        return_base64: bool = True

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": "qwen3_vd",
                "model_loaded": _model is not None,
                "model": MODEL_DIR or MODEL_ID,
                "load_error": _load_error or None}

    @app.post("/v1/voice_design")
    def voice_design(req: VDRequest):
        t = (req.text or "").strip()
        if not t:
            raise HTTPException(400, "text empty")
        t0 = time.time()
        try:
            wav, sr = synthesize(t, (req.instruct or "").strip(), req.language)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"VD 合成失败: {type(e).__name__}: {e}")
        return {"ok": True, "audio_base64": base64.b64encode(wav).decode("ascii"),
                "sample_rate": sr, "elapsed_ms": int((time.time() - t0) * 1000)}

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", default="", metavar="INSTRUCT",
                    help="不起服务，按描述合成一句样本到 vd_selftest.wav")
    ap.add_argument("--text", default="你好呀，很高兴认识你，今天过得怎么样？")
    args = ap.parse_args()
    if args.selftest:
        wav, sr = synthesize(args.text, args.selftest)
        out = os.path.join(os.getcwd(), "vd_selftest.wav")
        with open(out, "wb") as fh:
            fh.write(wav)
        print(f"OK {out} ({len(wav)}B, {sr}Hz)")
        return 0
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
