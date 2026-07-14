"""
SAPI TTS Server —— 零下载临时 TTS 后端（Windows 内置语音）。
═══════════════════════════════════════════════════════════════
背景：原 XTTS(alltalk_env) / CosyVoice(源码) / GPT-SoVITS(模型) 环境均已被清理，
当前机器无可运行的神经 TTS。本服务用 Windows SAPI(System.Speech) 顶替，
实现与 XTTS(tts_api.py) 完全一致的端点契约，让"对话闭环"今天就能出声。
神经 TTS 恢复后，停掉本服务、启动真服务即可，AvatarHub 无需任何改动。

端点（与 tts_api.py 对齐）：
  GET  /health                  健康检查
  GET  /v1/models               兼容
  POST /v1/audio/speech         {input,voice,language} -> WAV 原始字节
  POST /v1/audio/clone          {text,language,...}    -> JSON {audio_base64}
                                （SAPI 无法克隆音色，忽略参考音频，用系统语音）
监听端口：7851（可用 SAPI_TTS_PORT 覆盖）
"""
import os
import io
import base64
import tempfile
import logging

import pythoncom
import win32com.client
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SAPI-TTS] %(message)s")
logger = logging.getLogger("sapi_tts")

PORT = int(os.environ.get("SAPI_TTS_PORT", "7851"))
app = FastAPI(title="SAPI TTS (interim)", version="1.0")

# SSFMCreateForWrite
_SSFM_CREATE = 3


def _pick_voice_token(voice_com, language: str):
    """按语言挑选系统语音：zh* → 中文(Huihui)，否则默认。返回 token 或 None。"""
    want_zh = (language or "").lower().startswith("zh")
    try:
        toks = voice_com.GetVoices()
        # 优先：中文请求挑中文语音；英文请求挑英文语音
        for i in range(toks.Count):
            t = toks.Item(i)
            desc = (t.GetDescription() or "").lower()
            is_zh = ("chinese" in desc or "huihui" in desc or "zh-cn" in desc)
            if want_zh and is_zh:
                return t
            if (not want_zh) and (not is_zh):
                return t
        # 兜底：返回第一个
        if toks.Count > 0:
            return toks.Item(0)
    except Exception as e:
        logger.warning(f"voice 选择失败，用默认: {e}")
    return None


def _synthesize(text: str, language: str = "zh-cn") -> bytes:
    """用 SAPI 合成为 WAV 字节（PCM）。每次请求独立 COM，线程安全。"""
    pythoncom.CoInitialize()
    path = None
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tok = _pick_voice_token(voice, language)
        if tok is not None:
            voice.Voice = tok
        fs = win32com.client.Dispatch("SAPI.SpFileStream")
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        fs.Open(path, _SSFM_CREATE)
        voice.AudioOutputStream = fs
        voice.Speak(text or " ")
        fs.Close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        pythoncom.CoUninitialize()


class SpeechReq(BaseModel):
    model: str = "sapi"
    input: str = ""
    voice: str = ""
    language: str = "zh-cn"


class CloneReq(BaseModel):
    text: str = ""
    language: str = "zh-cn"
    reference_audio_base64: str = ""
    voice_name: str = ""


@app.get("/health")
def health():
    return {"status": "ok", "engine": "sapi", "note": "interim zero-download TTS"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "sapi", "object": "model"}]}


@app.post("/v1/audio/speech")
def speech(req: SpeechReq):
    wav = _synthesize(req.input, req.language)
    return Response(content=wav, media_type="audio/wav")


@app.post("/v1/audio/clone")
def clone(req: CloneReq):
    # SAPI 无法克隆：忽略参考音频，用系统语音合成，保持端点契约
    wav = _synthesize(req.text, req.language)
    return JSONResponse({"audio_base64": base64.b64encode(wav).decode()})


if __name__ == "__main__":
    print("=" * 55)
    print(" SAPI TTS（临时·零下载·Windows 系统语音）")
    print(f" 监听: http://0.0.0.0:{PORT}")
    print(" 端点契约与 tts_api.py(XTTS) 一致，可直接顶替")
    print(" 注意：音质为系统语音，仅供闭环演示；恢复神经 TTS 后停用本服务")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
