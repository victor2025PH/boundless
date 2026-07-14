"""
STT Server - Whisper 语音转文字服务（手机麦克风输入 → 文本）
端口: 7854（7853 已被 singing_server 占用，STT 独占 7854，可用 STT_PORT 覆盖）
运行环境: cosytts (whisper + soundfile + torch cu128, GPU)

API:
  GET  /healthz           - 轻量存活检查（不触碰模型/GPU，供 Hub 高频探测）
  GET  /health            - 健康检查
  POST /transcribe        - multipart: audio(WAV/任意可被 soundfile 读取的格式) → {text}
  POST /transcribe_b64    - json: {audio_base64, language} → {text}

设计:
- 用 soundfile 读音频（不依赖 ffmpeg）→ float32 单声道 16k → whisper.transcribe(numpy)
- 所有 GPU 推理固定单线程 + 启动预热（与 lipsync_server 同思路，避开 sm_120 冷启动）
"""
import os, sys, io, base64, time, logging, asyncio, functools, tempfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [STT] %(message)s")
logger = logging.getLogger("stt")

MODEL_NAME = os.environ.get("STT_MODEL", "small")  # tiny/base/small/medium
# 转写后端：faster=faster-whisper(CTranslate2，同权重更快，~3-5x)，回退 openai-whisper
STT_BACKEND = os.environ.get("STT_BACKEND", "faster").lower()
STT_FW_COMPUTE = os.environ.get("STT_FW_COMPUTE", "float16")   # float16/int8_float16/int8
STT_BEAM   = int(os.environ.get("STT_BEAM", "1"))              # beam search 宽度(>1 更准但更慢; 5 为常用高准确档)
STT_PROMPT = os.environ.get("STT_PROMPT", "").strip()          # initial_prompt 引导(如简体普通话/领域词), 仅 transcribe 生效
app = FastAPI(title="STT Server (Whisper)", version="1.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="stt")

_model = None
_backend = ""          # 实际生效的后端："faster" / "openai"
_loaded = False
_load_error = ""
_gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-gpu")
# MT 独立执行器 + 专用 CUDA stream：与 whisper 解耦，70ms 的翻译不必排在 600ms 转写之后
# (消除 Python 队列头阻塞；两者各用独立 stream，GPU 上可重叠 H2D/小核)。
_mt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-mt")
_mt_stream = None


def _load_model():
    global _model, _backend, _loaded, _load_error
    if _loaded:
        return True
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # 优先 faster-whisper（CTranslate2，同权重更快）
    if STT_BACKEND == "faster":
        try:
            from faster_whisper import WhisperModel
            ct = STT_FW_COMPUTE if dev == "cuda" else "int8"
            logger.info(f"loading faster-whisper '{MODEL_NAME}' on {dev} ({ct})...")
            _model = WhisperModel(MODEL_NAME, device=dev, compute_type=ct)
            list(_model.transcribe(np.zeros(16000, dtype=np.float32), language="zh", beam_size=1)[0])
            _backend = "faster"; _loaded = True
            logger.info("faster-whisper loaded + warmed up")
            return True
        except Exception as e:
            logger.warning(f"faster-whisper 加载失败，回退 openai-whisper: {e}")
    # 回退 openai-whisper
    try:
        import whisper
        logger.info(f"loading whisper '{MODEL_NAME}' on {dev}...")
        _model = whisper.load_model(MODEL_NAME, device=dev)
        _model.transcribe(np.zeros(16000, dtype=np.float32), language="zh", fp16=(dev == "cuda"))
        _backend = "openai"; _loaded = True
        logger.info("whisper(openai) loaded + warmed up")
        return True
    except Exception as e:
        _load_error = str(e)
        logger.exception("load failed")
        return False


def _unload_model():
    """释放 Whisper 模型显存(按需重载)。经 _gpu_pool 串行调用→不会与转写并发置空模型。
    流式逐词(Nemotron)模式下 Whisper 不参与转写,卸载它给口型/TTS 让显存;MT 不受影响。"""
    global _model, _loaded, _backend
    if not _loaded and _model is None:
        return False
    _model = None; _loaded = False; _backend = ""
    try:
        import torch, gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    logger.info("whisper 模型已卸载,显存释放(下次转写自动重载)")
    return True


def _read_audio(raw: bytes) -> np.ndarray:
    """字节 → float32 单声道 16k。"""
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        # 线性重采样（轻量，避免再引依赖）
        n = int(round(len(data) * 16000 / sr))
        if n > 0:
            data = np.interp(np.linspace(0, len(data), n, endpoint=False),
                             np.arange(len(data)), data).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32)


def _norm_lang(language: str) -> str:
    """whisper 只认 2 字母码：zh-cn/zh_tw → zh，en-us → en，空/auto → 自动检测(None)。"""
    if not language:
        return None
    code = language.strip().lower().replace("_", "-").split("-")[0]
    return None if code in ("", "auto") else code


def _pack_conf(text: str, nsp: list, lp: list, cr: list) -> dict:
    """把逐段置信度聚合成「最坏值」：no_speech 取最大、avg_logprob 取最小、
    compression_ratio 取最大——客户端据此对短输出(≤2词)的幻听加一道更严的门。
    无 segment 时给中性/保守默认，保证字段恒在(向后兼容)。"""
    return {
        "text": text,
        "no_speech_prob": (max(nsp) if nsp else 0.0),
        "avg_logprob":    (min(lp)  if lp  else 0.0),
        "compression_ratio": (max(cr) if cr else 0.0),
    }


def _transcribe_sync(audio: np.ndarray, language: str, task: str = "transcribe",
                     initial_prompt: str = "") -> dict:
    import torch
    if not _loaded and not _load_model():
        raise RuntimeError(_load_error or "model not loaded")
    if audio.size < 1600:  # < 0.1s 视为空
        return _pack_conf("", [], [], [])
    # task="translate"：Whisper 内置「任意语种语音 → 英文文本」直出（只支持 X→英；不强制 language）
    _task = "translate" if str(task).lower() == "translate" else "transcribe"
    _lang = None if _task == "translate" else _norm_lang(language)
    # 热词引导：逐请求 initial_prompt(客户端注入术语表/人名) 优先于机器级 STT_PROMPT 兜底。
    _prompt = (initial_prompt or "").strip() or STT_PROMPT
    if _backend == "faster":
        # 贪心解码(beam=1)最快；逐句独立(condition_on_previous_text=False)断开幻听自我循环；
        # vad_filter：内置 Silero VAD 先剔除无人声段 → 从源头消除静音幻听(感谢观看/Thank you)；
        # compression_ratio 收紧以丢弃退化重复输出(。。。。)。
        segs, _ = _model.transcribe(audio, language=_lang, task=_task, beam_size=STT_BEAM,
                                    temperature=0.0, condition_on_previous_text=False,
                                    initial_prompt=(_prompt or None) if _task == "transcribe" else None,
                                    no_speech_threshold=0.85, log_prob_threshold=-2.0,
                                    compression_ratio_threshold=2.2,
                                    vad_filter=True,
                                    vad_parameters=dict(min_silence_duration_ms=300))
        texts, nsp, lp, cr = [], [], [], []
        for s in segs:
            texts.append(s.text)
            nsp.append(float(getattr(s, "no_speech_prob", 0.0) or 0.0))
            lp.append(float(getattr(s, "avg_logprob", 0.0) or 0.0))
            cr.append(float(getattr(s, "compression_ratio", 0.0) or 0.0))
        return _pack_conf("".join(texts).strip(), nsp, lp, cr)
    # openai-whisper：补齐抗幻觉参数。condition_on_previous_text=False 是关键——断开幻听
    # 自我循环(此前同一句"Thank you/感谢观看"连刷数十次正是逐句条件叠加所致)；no_speech/
    # compression 阈值收紧静音与退化重复，从源头压制静音幻听。
    res = _model.transcribe(audio, language=_lang, task=_task,
                            fp16=torch.cuda.is_available(), temperature=0.0,
                            condition_on_previous_text=False,
                            no_speech_threshold=0.6,
                            compression_ratio_threshold=2.2)
    segs = res.get("segments") or []
    nsp = [float(s.get("no_speech_prob", 0.0) or 0.0) for s in segs]
    lp  = [float(s.get("avg_logprob", 0.0) or 0.0) for s in segs]
    cr  = [float(s.get("compression_ratio", 0.0) or 0.0) for s in segs]
    return _pack_conf((res.get("text") or "").strip(), nsp, lp, cr)


async def _run_gpu(fn, *a, **k):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_gpu_pool, functools.partial(fn, *a, **k))


async def _run_mt(fn, *a, **k):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_mt_pool, functools.partial(fn, *a, **k))


# ── 多语种神经机器翻译：Marian(opus-mt) 覆盖的语向走 Marian(快~30-60ms)，其余交给 ──
# NLLB-200(离线多语向)。独立 _mt_pool + 专用 CUDA stream，与 whisper 解耦；均按需懒加载。
# 调度(auto)：覆盖语向优先 Marian；其余用 NLLB；两者都不可用则抛错 → 由调用方回退 Google。
_MT_MODELS = {
    ("zh", "en"): "Helsinki-NLP/opus-mt-zh-en",
    ("en", "zh"): "Helsinki-NLP/opus-mt-en-zh",
}
_mt_cache = {}            # (src,dest) -> (tokenizer, model)   Marian 缓存

# TRANSLATE_ENGINE: auto(默认,引擎感知路由) / marian(仅中英) / nllb(强制多语向)
TRANSLATE_ENGINE = os.environ.get("TRANSLATE_ENGINE", "auto").strip().lower()
# NLLB-200(许可 CC-BY-NC，研究/内部可用；商用需自行核实)：distilled-600M 显存友好，
# 仅当出现「非 Marian 覆盖」语向时才首次加载(会下载~2.4GB)。置空则禁用 NLLB(非中英直接回退 Google)。
NLLB_MODEL = os.environ.get("NLLB_MODEL", "facebook/nllb-200-distilled-600M").strip()
_nllb = {"tok": None, "mdl": None}

# 语言别名 → 内部基码
_LANG_ALIAS = {
    "zh": "zh", "cmn": "zh", "yue": "zh", "zh-cn": "zh", "zh-hans": "zh",
    "zh-tw": "zh", "zh-hant": "zh", "en": "en", "eng": "en", "": "en",
}


def _norm_lang(language: str) -> str:
    code = (language or "").strip().lower().replace("_", "-")
    if code in _LANG_ALIAS:
        return _LANG_ALIAS[code]
    return code.split("-")[0]


_mt_pair = _norm_lang   # 向后兼容旧名

# FLORES-200 语码(NLLB 用)：内部基码 -> flores
_FLORES = {
    "zh": "zho_Hans", "en": "eng_Latn", "vi": "vie_Latn", "th": "tha_Thai",
    "id": "ind_Latn", "ms": "zsm_Latn", "ar": "arb_Arab", "es": "spa_Latn",
    "fr": "fra_Latn", "de": "deu_Latn", "ru": "rus_Cyrl", "ja": "jpn_Jpan",
    "ko": "kor_Hang", "pt": "por_Latn", "it": "ita_Latn", "hi": "hin_Deva",
    "tr": "tur_Latn", "nl": "nld_Latn", "pl": "pol_Latn", "uk": "ukr_Cyrl",
    "tl": "tgl_Latn", "km": "khm_Khmr", "my": "mya_Mymr", "fa": "pes_Arab",
}
# UI 展示名统一用中文(语向选择器面向中文操作者)；链路一律走 code，展示名可随意改。
_LANG_NAMES = {
    "zh": "中文", "en": "英语", "vi": "越南语", "th": "泰语", "id": "印尼语",
    "ms": "马来语", "ar": "阿拉伯语", "es": "西班牙语", "fr": "法语", "de": "德语",
    "ru": "俄语", "ja": "日语", "ko": "韩语", "pt": "葡萄牙语", "it": "意大利语",
    "hi": "印地语", "tr": "土耳其语", "nl": "荷兰语", "pl": "波兰语", "uk": "乌克兰语",
    "tl": "菲律宾语", "km": "高棉语", "my": "缅甸语", "fa": "波斯语",
}


def _translate_marian_sync(text: str, s: str, d: str) -> str:
    name = _MT_MODELS.get((s, d))
    if not name:
        raise RuntimeError(f"Marian 不支持 {s}->{d}")
    import torch
    if (s, d) not in _mt_cache:
        from transformers import MarianMTModel, MarianTokenizer
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"loading MT '{name}' on {dev}...")
        tok = MarianTokenizer.from_pretrained(name)
        mdl = MarianMTModel.from_pretrained(name).to(dev).eval()
        _mt_cache[(s, d)] = (tok, mdl)
        logger.info(f"MT {s}->{d} loaded")
    tok, mdl = _mt_cache[(s, d)]
    dev = next(mdl.parameters()).device
    batch = tok([text], return_tensors="pt", padding=True, truncation=True, max_length=256).to(dev)
    # no_repeat_ngram_size=3：greedy 解码对超短输入(如裸 "hello")会退化成
    # "你好 你好 你好…"×几十遍(实测 .140 复现)；3-gram 禁重复直接掐死循环，长句零影响。
    global _mt_stream
    if dev.type == "cuda":
        if _mt_stream is None:
            _mt_stream = torch.cuda.Stream()
        with torch.no_grad(), torch.cuda.stream(_mt_stream):
            out = mdl.generate(**batch, max_length=256, num_beams=1, no_repeat_ngram_size=3)
        _mt_stream.synchronize()           # 读结果前确保本 stream 完成
    else:
        with torch.no_grad():
            out = mdl.generate(**batch, max_length=256, num_beams=1, no_repeat_ngram_size=3)
    return tok.decode(out[0], skip_special_tokens=True).strip()


def _load_nllb():
    if _nllb["mdl"] is not None:
        return True
    if not NLLB_MODEL:
        raise RuntimeError("NLLB 未启用(NLLB_MODEL 为空)")
    import torch
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"loading NLLB '{NLLB_MODEL}' on {dev} (首次会下载权重)...")
    tok = AutoTokenizer.from_pretrained(NLLB_MODEL)
    mdl = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL).to(dev).eval()
    _nllb["tok"], _nllb["mdl"] = tok, mdl
    logger.info("NLLB loaded")
    return True


def _nllb_bos_id(tok, flores: str):
    """兼容不同 transformers 版本获取目标语言 BOS token id。"""
    lc = getattr(tok, "lang_code_to_id", None)
    if isinstance(lc, dict) and flores in lc:
        return lc[flores]
    tid = tok.convert_tokens_to_ids(flores)
    if tid is None or tid == getattr(tok, "unk_token_id", -1):
        raise RuntimeError(f"NLLB 未知语码 {flores}")
    return tid


def _translate_nllb_sync(text: str, s: str, d: str) -> str:
    fs, fd = _FLORES.get(s), _FLORES.get(d)
    if not fs or not fd:
        raise RuntimeError(f"NLLB 不支持 {s}->{d}")
    _load_nllb()
    import torch
    tok, mdl = _nllb["tok"], _nllb["mdl"]
    try:
        tok.src_lang = fs
    except Exception:
        pass
    dev = next(mdl.parameters()).device
    batch = tok([text], return_tensors="pt", padding=True, truncation=True, max_length=256).to(dev)
    with torch.no_grad():
        out = mdl.generate(**batch, forced_bos_token_id=_nllb_bos_id(tok, fd),
                           max_length=256, num_beams=1, no_repeat_ngram_size=3)
    return tok.batch_decode(out, skip_special_tokens=True)[0].strip()


def _supported_langs():
    """UI 语向选择器用：可作源/目标的语言(Marian 或 NLLB 覆盖)。"""
    codes = set(_FLORES.keys()) | {"zh", "en"}
    return [{"code": c, "name": _LANG_NAMES.get(c, c)} for c in sorted(codes)]


def _translate_sync(text: str, src: str, dest: str) -> str:
    """多引擎调度：覆盖语向优先 Marian(快)，其余 NLLB；均不可用则抛错(调用方回退 Google)。"""
    text = (text or "").strip()
    if not text:
        return ""
    s, d = _norm_lang(src), _norm_lang(dest)
    if s == d:
        return text
    marian_ok = (s, d) in _MT_MODELS
    if TRANSLATE_ENGINE == "marian":
        attempts = ["marian"]
    elif TRANSLATE_ENGINE == "nllb":
        attempts = ["nllb", "marian"]
    else:  # auto：覆盖语向先 Marian(更快)，否则先 NLLB
        attempts = ["marian", "nllb"] if marian_ok else ["nllb", "marian"]
    last_err = None
    for eng in attempts:
        try:
            if eng == "marian":
                return _translate_marian_sync(text, s, d)
            return _translate_nllb_sync(text, s, d)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"翻译不可用 {s}->{d}: {last_err}")


class TranslateReq(BaseModel):
    text: str
    src: str = "zh"
    dest: str = "en"


@app.get("/health")
def health():
    return {"ok": True, "loaded": _loaded, "model": MODEL_NAME, "service": "stt",
            "backend": _backend,
            "translate_engine": TRANSLATE_ENGINE,
            "nllb": {"model": NLLB_MODEL, "loaded": _nllb["mdl"] is not None},
            "mt_loaded": list(map(lambda k: f"{k[0]}->{k[1]}", _mt_cache.keys()))}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "stt"}


@app.post("/asr/unload")
async def asr_unload():
    """卸载 Whisper(释放显存)。经 GPU 单线程池串行,确保不与进行中的转写冲突。MT 保持加载。"""
    ok = await _run_gpu(_unload_model)
    return {"ok": True, "unloaded": bool(ok), "loaded": _loaded}


@app.post("/asr/load")
async def asr_load():
    """主动加载/预热 Whisper(回退分段模式前调用,避免首句重载延迟)。"""
    ok = await _run_gpu(_load_model)
    return {"ok": bool(ok), "loaded": _loaded, "backend": _backend}


@app.post("/translate")
async def translate(req: TranslateReq):
    t = time.time()
    try:
        text = await _run_mt(_translate_sync, req.text, req.src, req.dest)
    except Exception as e:
        raise HTTPException(500, f"翻译失败: {e}")
    logger.info(f"translate {req.src}->{req.dest} -> '{text[:40]}' ({(time.time()-t)*1000:.0f}ms)")
    return {"ok": True, "text": text, "elapsed_ms": int((time.time() - t) * 1000)}


@app.get("/translate/langs")
def translate_langs():
    """可选语言清单 + 引擎覆盖情况，供同传 UI 语向选择器渲染。"""
    return {"ok": True, "langs": _supported_langs(),
            "marian_pairs": [f"{a}->{b}" for (a, b) in _MT_MODELS.keys()],
            "nllb_enabled": bool(NLLB_MODEL), "engine": TRANSLATE_ENGINE}


class PreloadReq(BaseModel):
    pairs: list = []          # ["ja:zh","zh:ja",...] 客户端(通译)按真实使用频率统计而来


_preload_state = {"running": False, "done": [], "at": 0.0}


@app.post("/translate/preload")
def translate_preload(req: PreloadReq):
    """P6-5 预载自学习：通译端最了解真实语对分布(会话日志)，启动时把 top 语对推过来，
    本服务后台逐对预跑一句(懒加载模型提前付清)。幂等、best-effort、串行不抢推理池。"""
    pairs = []
    for p in (req.pairs or [])[:12]:
        try:
            s, d = (str(p).split(":", 1) if ":" in str(p) else (None, None))
            s, d = _norm_lang(s or ""), _norm_lang(d or "")
            if s and d and s != d and (s, d) not in pairs:
                pairs.append((s, d))
        except Exception:
            continue
    if not pairs:
        return {"ok": False, "detail": "no valid pairs"}
    if _preload_state["running"]:
        return {"ok": True, "already_running": True, "state": dict(_preload_state)}

    def _job():
        _preload_state.update({"running": True, "done": [], "at": time.time()})
        try:
            for s, d in pairs:
                try:
                    _translate_sync(_PRELOAD_SAMPLE.get(s, "hello"), s, d)
                    _preload_state["done"].append(f"{s}->{d}")
                except Exception:
                    logger.warning(f"preload pair failed: {s}->{d}")
            logger.info(f"client-driven preload done: {','.join(_preload_state['done']) or '-'}")
        finally:
            _preload_state["running"] = False

    import threading
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "accepted": [f"{s}->{d}" for s, d in pairs]}


@app.get("/translate/preload")
def translate_preload_state():
    return {"ok": True, "state": dict(_preload_state)}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str = Form("zh"),
                     task: str = Form("transcribe")):
    raw = await audio.read()
    try:
        arr = _read_audio(raw)
    except Exception as e:
        raise HTTPException(400, f"无法解码音频: {e}")
    t = time.time()
    out = await _run_gpu(_transcribe_sync, arr, language, task)
    text = out["text"]
    logger.info(f"transcribe {len(raw)//1024}KB -> '{text[:40]}' "
                f"(nsp={out['no_speech_prob']:.2f} lp={out['avg_logprob']:.2f} "
                f"{time.time()-t:.1f}s)")
    return {"ok": True, "text": text, "elapsed_s": round(time.time() - t, 2),
            "no_speech_prob": round(out["no_speech_prob"], 4),
            "avg_logprob": round(out["avg_logprob"], 4),
            "compression_ratio": round(out["compression_ratio"], 4)}


class B64Req(BaseModel):
    audio_base64: str
    language: str = "zh"
    task: str = "transcribe"     # "transcribe" 或 "translate"(→英文直出)
    initial_prompt: str = ""     # 热词引导(术语表/人名/领域词)；空=用机器级 STT_PROMPT


@app.post("/transcribe_b64")
async def transcribe_b64(req: B64Req):
    try:
        raw = base64.b64decode(req.audio_base64)
        arr = _read_audio(raw)
    except Exception as e:
        raise HTTPException(400, f"无法解码音频: {e}")
    t = time.time()
    out = await _run_gpu(_transcribe_sync, arr, req.language, req.task, req.initial_prompt)
    text = out["text"]
    logger.info(f"transcribe_b64 -> '{text[:40]}' "
                f"(nsp={out['no_speech_prob']:.2f} lp={out['avg_logprob']:.2f} "
                f"{time.time()-t:.1f}s)")
    return {"ok": True, "text": text, "elapsed_s": round(time.time() - t, 2),
            "no_speech_prob": round(out["no_speech_prob"], 4),
            "avg_logprob": round(out["avg_logprob"], 4),
            "compression_ratio": round(out["compression_ratio"], 4)}


# ── P5-5 启动预载：NLLB(600M)按需懒加载实测首调 73s——多语向兜底必须是热的，
#   否则同传 LLM 熔断的瞬间撞上冷加载=通话中断一分钟。启动即后台预载模型本体，
#   并对常用语对各预跑一句 generate(消除逐语对首句开销,热态 ~0.2s)。
#   STT_PRELOAD_NLLB=0 回到纯懒加载(省 ~2.4G 显存/内存)。
STT_PRELOAD_NLLB  = os.environ.get("STT_PRELOAD_NLLB", "1") == "1"
STT_PRELOAD_PAIRS = os.environ.get("STT_PRELOAD_PAIRS",
                                   "ja:zh,zh:ja,ko:zh,zh:ko,ru:zh,zh:ru").strip()
_PRELOAD_SAMPLE = {"zh": "你好", "ja": "こんにちは", "ko": "안녕하세요", "ru": "Привет"}


def _warmup_mt():
    try:
        _translate_sync("你好", "zh", "en")
        _translate_sync("hello", "en", "zh")
        logger.info("MT warmed up (zh<->en)")
    except Exception:
        logger.exception("MT warmup failed")
    if not (STT_PRELOAD_NLLB and NLLB_MODEL):
        return
    try:
        t0 = time.time()
        _load_nllb()
        done = []
        for p in (STT_PRELOAD_PAIRS or "").split(","):
            p = p.strip()
            if ":" not in p:
                continue
            s, d = (x.strip() for x in p.split(":", 1))
            try:
                _translate_nllb_sync(_PRELOAD_SAMPLE.get(s, "hello"), s, d)
                done.append(f"{s}->{d}")
            except Exception:
                logger.warning(f"NLLB pair warm failed: {s}->{d}")
        logger.info(f"NLLB preloaded in {time.time()-t0:.0f}s, warmed pairs: {','.join(done) or '-'}")
    except Exception:
        logger.exception("NLLB preload failed")


@app.on_event("startup")
async def _startup():
    import threading
    threading.Thread(target=_load_model, daemon=True).start()
    threading.Thread(target=_warmup_mt, daemon=True).start()


if __name__ == "__main__":
    _port = int(os.environ.get("STT_PORT", "7854"))
    uvicorn.run(app, host="0.0.0.0", port=_port, log_level="info")
