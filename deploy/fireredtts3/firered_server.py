# -*- coding: utf-8 -*-
"""FireRedTTS3 → fish `/v1/tts/clone` 契约 wrapper（104 :7869，v0 待现场定稿）。

chengjie 侧零改码消费：`voice_lang_route.clone_langs.<lang>.voice_profile.
clone_base_url` 指到本服务即可（与 117 CosyVoice、104 IndexTTS 同一契约家族）。

契约（与 voice_clone_client / minicpm_clone 链对齐）：
  GET  /health
      -> {"status":"ok","engine":"fireredtts3","model_loaded":bool}
  POST /v1/tts/clone
      {"text","reference_audio_b64","reference_text"?,"language"?,"return_base64":true}
      -> {"ok":true,"audio_base64":"<WAV b64>","sample_rate":<int>,"format":"wav"}

设计与钉子：
- FireRed 是"语言 tag 前置"模型（论文：A language tag is prepended to the input
  text）——wrapper 把 language 归一成 FireRed 语言名并透传；调用方（clone_langs
  路由）另有 clone_text_prefix 机制，两者并存无害（多余前缀由 TN 清理）。
- TN：wetext 只管 zh/en；多语文本走 LLM TN（.env 指到 173:8001 vLLM chatx，
  OpenAI 兼容）。缺 .env 时回落 use_wetext=True（zh/en 正常、他语仅基础清理）。
- 单请求串行锁（12G 卡容不下并发）；90s 超时由调用方控制。
- ``# TODO-现场核对``：合成方法名/入参以 clone 下来的官方 repo README 为准，
  部署冒烟时改这一处（工具 tools/verify_clone_lang.py 首句即暴露）。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import tempfile
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("firered_wrapper")

MODEL_DIR = os.environ.get("FIRERED_MODEL_DIR", r"C:\firered\pretrained_models")
USE_LLM_TN = bool(os.environ.get("LLM_TN_API_URL"))

app = FastAPI(title="fireredtts3-wrapper")
_LOCK = threading.Lock()
_TTS = None

# BCP47 前缀 → FireRed 语言名（README 支持表；缺失=不传 tag 由模型自判）
_LANGS = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "ru": "Russian", "pt": "Portuguese", "ar": "Arabic", "th": "Thai",
    "vi": "Vietnamese", "id": "Indonesian", "hi": "Hindi", "tr": "Turkish",
    "el": "Greek", "uk": "Ukrainian", "pl": "Polish", "nl": "Dutch",
    "cs": "Czech", "fi": "Finnish", "ro": "Romanian", "yue": "Cantonese",
}


def _load():
    global _TTS
    if _TTS is not None:
        return _TTS
    from fireredtts3.core import FireRedTTS3   # noqa: PLC0415

    log.info("loading FireRedTTS3 from %s (llm_tn=%s)...", MODEL_DIR, USE_LLM_TN)
    _TTS = FireRedTTS3(
        MODEL_DIR,
        use_wetext=not USE_LLM_TN,
        use_llm_tn=USE_LLM_TN,
    )
    log.info("model loaded.")
    return _TTS


class CloneReq(BaseModel):
    text: str
    reference_audio_b64: str
    reference_text: str = ""
    language: str = ""
    return_base64: bool = True


@app.get("/health")
def health():
    return {"status": "ok", "engine": "fireredtts3",
            "model_loaded": _TTS is not None}


@app.post("/v1/tts/clone")
def clone(req: CloneReq):
    try:
        tts = _load()
    except Exception as ex:   # 加载失败如实报：调用方回落 edge/文字
        log.exception("model load failed")
        return JSONResponse({"ok": False, "error": f"load: {ex}"}, status_code=503)
    lang = _LANGS.get(str(req.language or "").strip().lower().split("-")[0], "")
    ref_path = None
    try:
        with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False) as f:
            f.write(base64.b64decode(req.reference_audio_b64))
            ref_path = f.name
        with _LOCK:   # 12G 卡：严格串行
            # TODO-现场核对：合成入口以官方 repo README 为准（clone 下来后对齐
            # 方法名与参数）。以下按 README 公开示例形态编写：
            audio, sr = tts.synthesize(
                prompt_wav=ref_path,
                prompt_text=(req.reference_text or None),
                text=req.text,
                lang=(lang or None),
            )
        buf = io.BytesIO()
        import soundfile as sf   # noqa: PLC0415  随 fireredtts3 依赖树装入

        sf.write(buf, audio, sr, format="WAV")
        return {"ok": True,
                "audio_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
                "sample_rate": int(sr), "format": "wav"}
    except Exception as ex:
        log.exception("synth failed")
        return JSONResponse({"ok": False, "error": str(ex)[:300]},
                            status_code=500)
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(
        os.environ.get("FIRERED_PORT", "7869")))
