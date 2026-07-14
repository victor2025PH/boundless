# -*- coding: utf-8 -*-
"""
sbv2_tts_server.py — Style-Bert-VITS2 JP-Extra 日语情感 TTS（端口 7861）

Fish-Speech 兼容 HTTP 契约，供 live_interpreter / AvatarHub 零特判路由。
专为林小玲东京女声人设训练（LinXiaoling_JP），五情绪 style 向量内建。

接口：
  GET  /health
  GET  /v1/status
  POST /v1/tts          {text, language, style, emotion, speed, ...}
  POST /v1/tts/clone    同上（忽略 reference，用训练模型）
  POST /v1/tts/clone/stream  伪流式：合成后分块推送 PCM16
  POST /v1/tts/instruct {text, instruct, ...}  instruct→style 映射

运行：C:\\SBV2\\venv 环境
  python sbv2_tts_server.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import struct
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Optional

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SBV2TTS] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SBV2TTS")

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import app_config
import service_auth

SBV2_ROOT = Path(os.environ.get("SBV2_ROOT", r"C:\SBV2"))
MODEL_DIR = Path(os.environ.get(
    "SBV2_MODEL_DIR",
    str(SBV2_ROOT / "Data" / "LinXiaoling_JP"),
))
MODEL_NAME = os.environ.get("SBV2_MODEL_NAME", "LinXiaoling_JP")
SPEAKER = os.environ.get("SBV2_SPEAKER", "LinXiaolingJP")
PORT = int(os.environ.get("SBV2_TTS_PORT", "7861"))
DEVICE = os.environ.get("SBV2_DEVICE", "cuda:0")

# instruct / emotion → SBV2 style（Fearful=P8 JVNV 融合新增；模型缺该 style 时 _synth 自动回退）
_STYLE_MAP = {
    "neutral": "Neutral", "calm": "Neutral", "gentle": "Neutral", "serious": "Neutral",
    "happy": "Happy", "excited": "Happy",
    "sad": "Sad", "fearful": "Fearful",
    "angry": "Angry", "disgusted": "Angry",
    "surprised": "Surprised",
}
_STYLE_FALLBACK = {"Fearful": "Sad"}
_INSTRUCT_STYLE = {
    "开心": "Happy", "愉快": "Happy", "兴奋": "Happy", "激动": "Happy",
    "悲伤": "Sad", "难过": "Sad", "伤心": "Sad",
    "愤怒": "Angry", "生气": "Angry",
    "惊讶": "Surprised", "吃惊": "Surprised",
}

# ── P7 情感表现力预设(2026-07-10 用户反馈"情感还不够") ──────────────────────
#   SBV2 的三个原生表现力旋钮 + BERT 语义助推,只对情感 style 生效(Neutral 保持稳定音质)：
#   · sdp_ratio ↑   : 韵律节奏起伏更大(官方: 比率高→トーンのばらつき大)
#   · noise_w  ↑    : 音素时长随机性↑ → 不那么"匀速播音"
#   · intonation_scale: 抑扬(F0 摆幅)按 style_weight 联动放大——徽章上的强度数字直接变声调幅度
#   · assist_text   : JP-Extra 韵律跟随 BERT 语义,给一句"情绪画外音"把整句往该情绪推
EMO_SDP      = float(os.environ.get("SBV2_EMO_SDP", "0.45"))
EMO_NOISEW   = float(os.environ.get("SBV2_EMO_NOISEW", "0.9"))
EMO_INTON_MAX = float(os.environ.get("SBV2_EMO_INTON_MAX", "1.45"))
EMO_ASSIST_ON = os.environ.get("SBV2_EMO_ASSIST", "1") == "1"
EMO_ASSIST_W  = float(os.environ.get("SBV2_EMO_ASSIST_W", "0.7"))
_EMO_ASSIST_TEXT = {
    "Happy":     "本当に嬉しくて、声が弾んでいる！",
    "Sad":       "悲しくて、今にも泣きそうな声…",
    "Angry":     "怒りで思わず声を荒げている！",
    "Surprised": "びっくりして、思わず大きな声が出た！",
    "Fearful":   "怖くて、声が震えている…",
}
_EMO_SPEED_MULT = {   # 情绪节奏:开心/惊讶偏快,难过偏慢(乘在调用方 speed 上)
    "Happy": 1.06, "Surprised": 1.08, "Angry": 1.05, "Sad": 0.90, "Fearful": 0.97,
}

app = FastAPI(title="SBV2 JP-Extra TTS", version="1.0.0")
service_auth.secure(app, name="sbv2_tts")

_model = None
_model_lock = threading.Lock()
_sample_rate = 44100
_load_error = ""
_loaded_ckpt = ""          # 当前已加载检查点绝对路径
_loaded_mtime = 0.0
# P6.1 热加载：训练每 eval_interval 步落盘新 G_*.safetensors，watcher 检测到「更新且写完
# (mtime 稳定>15s)」即后台换载——训练全程无需人工重启，通译侧无感切换到更好的模型。
RELOAD_WATCH = os.environ.get("SBV2_RELOAD_WATCH", "1") == "1"
RELOAD_EVERY = float(os.environ.get("SBV2_RELOAD_EVERY", "60"))


ASSETS_DIR = Path(os.environ.get(
    "SBV2_ASSETS_DIR", str(SBV2_ROOT / "model_assets" / MODEL_NAME)))


def _latest_ckpt() -> Optional[Path]:
    """最新可推理检查点：训练每 eval_interval 步把「发布版 safetensors」写进
    model_assets\\<model>\\，resume 版 .pth 写进 Data\\models\\(TTSModel 不认)。
    两处 safetensors 一起看、按 mtime 取最新——G_0 预训练底模只在无更新时兜底。"""
    cands: list[Path] = []
    cands += list((MODEL_DIR / "models").glob("G_*.safetensors"))
    if ASSETS_DIR.is_dir():
        cands += list(ASSETS_DIR.glob("*.safetensors"))
    cands = [p for p in cands if p.is_file()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


class TTSRequest(BaseModel):
    text: str
    language: str = "ja"
    speed: float = 1.0
    style: str = ""
    emotion: str = ""
    return_base64: bool = True
    sdp_ratio: float = 0.2
    noise: float = 0.6
    noise_w: float = 0.8
    style_weight: float = 1.0
    intonation_scale: float = 0.0      # P9.1 韵律跟随:>0 时覆盖(与情感预设取较大者)


class CloneTTSRequest(TTSRequest):
    reference_audio_b64: str = ""
    reference_text: str = ""


class InstructTTSRequest(BaseModel):
    text: str
    instruct: str = ""
    language: str = "ja"
    speed: float = 1.0
    return_base64: bool = True
    emotion: str = ""


def _resolve_style(style: str = "", emotion: str = "", instruct: str = "") -> str:
    if style:
        cap = style.strip().capitalize()
        if cap in ("Neutral", "Happy", "Sad", "Angry", "Surprised", "Fearful"):
            return cap
    emo = (emotion or "").strip().lower()
    if emo in _STYLE_MAP:
        return _STYLE_MAP[emo]
    ins = instruct or ""
    for key, st in _INSTRUCT_STYLE.items():
        if key in ins:
            return st
    return "Neutral"


def _speed_to_length(speed: float) -> float:
    """SBV2 length: 大=慢。speed 1.0 → length 1.0；speed 1.2 → length 0.83。"""
    s = max(0.5, min(2.0, float(speed or 1.0)))
    return round(1.0 / s, 3)


def _load_model(force: bool = False):
    """加载(或热换载)最新检查点。force=True 时即便已有模型也重建并原子换指针。"""
    global _model, _load_error, _sample_rate, _loaded_ckpt, _loaded_mtime
    with _model_lock:
        if _model is not None and not force:
            return
        if str(SBV2_ROOT) not in sys.path:
            sys.path.insert(0, str(SBV2_ROOT))
        from style_bert_vits2.constants import Languages
        from style_bert_vits2.nlp.japanese.user_dict import update_dict
        from style_bert_vits2.nlp import bert_models
        from style_bert_vits2.tts_model import TTSModel

        if _model is None:
            # Windows 上 pyopenjtalk worker 子进程易崩；不启 worker，走内联 pyopenjtalk
            try:
                update_dict()
            except Exception as _de:
                logger.warning(f"user_dict update skipped: {_de}")
            bert_models.load_model(Languages.JP, device_map=DEVICE)
            bert_models.load_tokenizer(Languages.JP)

        ckpt = _latest_ckpt()
        if ckpt is None:
            raise FileNotFoundError(f"无 G 检查点: {MODEL_DIR / 'models'}")
        cfg = MODEL_DIR / "config.json"
        style_vec = MODEL_DIR / "style_vectors.npy"
        if not cfg.is_file():
            raise FileNotFoundError(f"config 缺失: {cfg}")
        if not style_vec.is_file():
            raise FileNotFoundError(f"style_vectors 缺失: {style_vec}")

        m = TTSModel(model_path=ckpt, config_path=cfg,
                     style_vec_path=style_vec, device=DEVICE)
        m.load()
        old = _model
        _model = m                     # 原子换指针;在飞请求持旧引用播完即释放
        _loaded_ckpt = str(ckpt)
        _loaded_mtime = ckpt.stat().st_mtime
        _sample_rate = int(getattr(m.hyper_parameters.data, "sampling_rate", 44100) or 44100)
        logger.info(f"loaded {ckpt.name} styles={list(m.style2id.keys())} sr={_sample_rate}"
                    + (" (hot-reload)" if old is not None else ""))
        if old is not None:
            try:
                old.unload()
            except Exception:
                pass


def _ensure_model():
    if _model is not None:
        return
    try:
        _load_model()
    except Exception as e:
        global _load_error
        _load_error = str(e)
        logger.error(f"load failed: {e}")
        raise HTTPException(503, f"SBV2 模型未就绪: {_load_error}")


def _ckpt_watcher():
    """P6.1 训练检查点热加载：新 G_* 且 mtime 稳定(写完)>15s → 后台换载。"""
    while True:
        time.sleep(RELOAD_EVERY)
        try:
            if _model is None:
                continue               # 首次加载由请求触发,watcher 只管热换
            ck = _latest_ckpt()
            if ck is None:
                continue
            mt = ck.stat().st_mtime
            if (str(ck) != _loaded_ckpt or mt > _loaded_mtime) and time.time() - mt > 15:
                logger.info(f"检测到新检查点 {ck.name} → 热加载")
                _load_model(force=True)
        except Exception as e:
            logger.warning(f"ckpt watcher: {e}")


def _synth(text: str, *, style: str, speed: float = 1.0,
           sdp_ratio: float = 0.2, noise: float = 0.6, noise_w: float = 0.8,
           style_weight: float = 1.0, intonation: float = 0.0) -> tuple[bytes, int]:
    _ensure_model()
    m = _model                        # 取一次引用：热换载期间本次合成用旧模型走完
    assert m is not None
    from style_bert_vits2.constants import Languages
    t0 = time.time()
    if style not in m.style2id:          # 模型未含该 style(如旧检查点无 Fearful) → 降级近义/中性
        style = _STYLE_FALLBACK.get(style, "Neutral")
        if style not in m.style2id:
            style = "Neutral"
    sw = max(0.3, min(3.0, float(style_weight or 1.0)))
    inton = 1.0
    assist = None
    if style != "Neutral":            # P7 情感表现力预设(Neutral 走稳定参数不动)
        sdp_ratio = max(sdp_ratio, EMO_SDP)
        noise_w = max(noise_w, EMO_NOISEW)
        # 抑扬随强度联动: sw=1.2→1.02, 1.8→1.10, 2.4→1.17 … 封顶 EMO_INTON_MAX
        inton = min(EMO_INTON_MAX, 1.0 + 0.12 * max(0.0, sw - 1.0))
        speed = float(speed or 1.0) * _EMO_SPEED_MULT.get(style, 1.0)
        if EMO_ASSIST_ON:
            assist = _EMO_ASSIST_TEXT.get(style)
    if intonation and intonation > 0:  # P9.1 韵律跟随覆盖(平叙句也生效,与情感预设取较大)
        inton = max(inton, min(1.6, float(intonation)))
    sr, audio = m.infer(
        text=text.strip(),
        language=Languages.JP,
        speaker_id=m.spk2id.get(SPEAKER, 0),
        style=style,
        length=_speed_to_length(speed),
        sdp_ratio=sdp_ratio,
        noise=noise,
        noise_w=noise_w,
        style_weight=sw,
        intonation_scale=inton,
        assist_text=assist,
        assist_text_weight=EMO_ASSIST_W,
        use_assist_text=bool(assist),
        line_split=True,
    )
    # model.infer 返回 16bit PCM 量纲(int16 或 int16 尺度的 float)——按量纲转换,
    # 误按 float[-1,1] 处理会全程削波成方波(2026-07-10 A/B 探针 rms=1.000 实锤)。
    audio = np.asarray(audio)
    if audio.dtype == np.int16:
        pcm = audio
    else:
        a = audio.astype(np.float32)
        if float(np.max(np.abs(a)) if a.size else 0.0) > 2.0:
            pcm = np.clip(a, -32768, 32767).astype(np.int16)
        else:
            pcm = (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    logger.info(f"synth style={style} w={sw:.1f} inton={inton:.2f} assist={'Y' if assist else 'N'} "
                f"len={len(text)} dur={len(pcm)/sr:.2f}s ms={int((time.time()-t0)*1000)}")
    return buf.getvalue(), sr


def _stream_chunks(wav_bytes: bytes, chunk_samples: int = 4096):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    if ch != 1:
        raise ValueError("mono only")
    n = len(raw) // 2
    step = chunk_samples * 2
    for i in range(0, len(raw), step):
        chunk = raw[i:i + step]
        yield struct.pack("<I", len(chunk)) + chunk
    yield struct.pack("<I", 0)


@app.get("/health")
def health():
    ck = _latest_ckpt() if MODEL_DIR.is_dir() else None
    return {"status": "ok", "engine": "sbv2", "model_loaded": _model is not None,
            "ready": bool(ck and (MODEL_DIR / "style_vectors.npy").is_file()),
            "checkpoint": ck.name if ck else "",
            "loaded_checkpoint": Path(_loaded_ckpt).name if _loaded_ckpt else "",
            "model_dir": str(MODEL_DIR),
            "speaker": SPEAKER, "load_error": _load_error or None}


@app.post("/v1/reload")
def reload_model():
    """手动触发热加载最新检查点(watcher 之外的显式入口)。"""
    try:
        _load_model(force=True)
        return {"ok": True, "loaded": Path(_loaded_ckpt).name if _loaded_ckpt else ""}
    except Exception as e:
        raise HTTPException(503, f"reload 失败: {e}")


@app.get("/v1/status")
def status():
    h = health()
    styles = list(_model.style2id.keys()) if _model else []
    return {**h, "sample_rate": _sample_rate, "styles": styles}


@app.post("/v1/tts")
def tts(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(400, "text empty")
    style = _resolve_style(req.style, req.emotion)
    wav, sr = _synth(req.text, style=style, speed=req.speed,
                     sdp_ratio=req.sdp_ratio, noise=req.noise, noise_w=req.noise_w,
                     style_weight=req.style_weight, intonation=req.intonation_scale)
    if req.return_base64:
        return {"ok": True, "audio_base64": base64.b64encode(wav).decode(),
                "sample_rate": sr, "style": style, "engine": "sbv2"}
    return JSONResponse(content={"ok": True, "sample_rate": sr, "style": style})


@app.post("/v1/tts/clone")
def tts_clone(req: CloneTTSRequest):
    return tts(req)


@app.post("/v1/tts/instruct")
def tts_instruct(req: InstructTTSRequest):
    style = _resolve_style(emotion=req.emotion, instruct=req.instruct)
    wav, sr = _synth(req.text, style=style, speed=req.speed)
    if req.return_base64:
        return {"ok": True, "audio_base64": base64.b64encode(wav).decode(),
                "sample_rate": sr, "style": style, "engine": "sbv2"}
    return {"ok": True, "sample_rate": sr, "style": style}


@app.post("/v1/tts/clone/stream")
def tts_clone_stream(req: CloneTTSRequest):
    if not req.text.strip():
        raise HTTPException(400, "text empty")
    style = _resolve_style(req.style, req.emotion)
    wav, sr = _synth(req.text, style=style, speed=req.speed,
                     sdp_ratio=req.sdp_ratio, noise=req.noise, noise_w=req.noise_w,
                     style_weight=req.style_weight, intonation=req.intonation_scale)

    def gen():
        for pkt in _stream_chunks(wav):
            yield pkt

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(sr), "X-Style": style,
                                      "X-Engine": "sbv2"})


if __name__ == "__main__":
    logger.info(f"SBV2 TTS starting port={PORT} model={MODEL_DIR}")
    if RELOAD_WATCH:
        threading.Thread(target=_ckpt_watcher, daemon=True, name="ckpt-watcher").start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
