"""
nemotron_stt_server.py — Nemotron 3.5 ASR 语音转文字服务（端口 7857）
运行环境: nemoasr conda env（nemo_toolkit[asr] >= 26.06, torch cu128, GPU）

为什么引入：NVIDIA Nemotron 3.5 ASR(OpenMDW-1.1 可商用) 是 600M Cache-Aware FastConformer-RNNT，
**流式原生**（块 80ms~1.12s 可配）、单 checkpoint 覆盖 40 语种、原生标点/大小写、H100 上较缓冲式
多 ~17× 并发流。相对现网 faster-whisper（批处理/离线、整句出文本）在「首音前段延迟 + 并发 +
增量字幕」上结构性更优，故作为**与 Whisper 并存的可灰度 STT 引擎**接入。

与 stt_server.py 完全对齐的 HTTP 契约（Hub / 通译 LingoX 可零改动 drop-in，仅切 SVC_STT/STT_URL）：
  GET  /health             → {ok, loaded, model, service, backend, mt_loaded}
  POST /transcribe         multipart: audio, language, task      → {ok, text, elapsed_s}
  POST /transcribe_b64     json: {audio_base64, language, task}  → {ok, text, elapsed_s}
  POST /translate          json: {text, src, dest}               → {ok, text, elapsed_ms}
新增（相对 Whisper 的核心增量）：
  WS   /ws/transcribe      二进制 PCM16/16k/mono 分帧上行 → 增量 {partial}/{final} 下行（cache-aware）

说明：
- ASR 用 Nemotron；翻译沿用与 stt_server 相同的本地 MarianMT（opus-mt），保证 /translate 逐字节兼容。
- task="translate"（X→英）：Nemotron 为转写非语音翻译，按「转写 → MarianMT 译英」组合，保持契约行为。
"""
import os, sys, io, base64, time, logging, asyncio, functools, json
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [NemoSTT] %(message)s")
logger = logging.getLogger("nemo_stt")

MODEL_NAME = os.environ.get("NEMO_STT_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b")
# 精度：默认 fp32(准确率优先)。真凶排查记录：真实音频曾必现 CUDA illegal access,
# 最初怀疑 fp16/bf16,实为 RNNT 解码的 CUDA-Graph 内核(见下)——关图后 fp32/bf16 均稳定实测通过。
# bf16 省 ~1.3G 但最嘈样本有个别词差("一直"→"即使"),显存 fp32 也够 → 不换。可选: fp32|bf16|fp16。
NEMO_PRECISION = os.environ.get("NEMO_PRECISION", "fp32").strip().lower()
# RNNT 解码的 CUDA-Graph 内核默认关(真凶)：与 ollama/fish/lipsync 同卡时 graph 捕获/重放对并发
# 显存变动极敏感,任何精度的真实长语音都触发 illegal access 且毒化 CUDA 上下文(需重启进程);
# 静音/短噪声测不出。关图换普通内核后全精度稳定(每段慢 ~10-20ms,段级延迟无感)。独卡可设 1 换回。
NEMO_CUDA_GRAPHS = os.environ.get("NEMO_CUDA_GRAPHS", "0") == "1"
# 本服务的 /translate(MarianMT) 是与 Whisper 契约兼容的备用路——现网翻译走 LLM/远端 STT 节点，
# 这里默认放 CPU：省 ~0.6-1G 显存给同卡的口型/TTS，备用路 CPU 200-400ms 完全够用。
NEMO_MT_DEVICE = os.environ.get("NEMO_MT_DEVICE", "cpu").strip().lower()
# 流式块时长（ms）：Nemotron 支持 80/160/320/560/1120；越小越早出字、越大越省算力。
STREAM_CHUNK_MS = int(os.environ.get("NEMO_STREAM_CHUNK_MS", "320"))
SAMPLE_RATE = 16000
# 6-A 流式步进：partial 只解码「最近 N 秒」窗口，避免随句子增长整窗重转写(O(N²)→O(窗口))；
# final 仍解码整段保证准确。窗口足够大(默认 8s)时短句行为不变，长句 partial 反映近端（字幕/barge 足够）。
PARTIAL_WINDOW_SEC = float(os.environ.get("NEMO_PARTIAL_WINDOW_SEC", "8"))

app = FastAPI(title="STT Server (Nemotron 3.5 ASR)", version="1.0")
try:
    import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
    service_auth.secure(app, name="stt")                 # 与 Whisper 同名（同为 STT 面，令牌/来源策略一致）
except Exception as _e:
    logger.warning(f"service_auth 未启用: {_e}")

_model = None
_loaded = False
_load_error = ""
_gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nemo-gpu")
_mt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nemo-mt")
_mt_stream = None


# ── ASR 模型加载 ─────────────────────────────────────────────────────
def _load_model():
    global _model, _loaded, _load_error
    if _loaded:
        return True
    try:
        import torch
        from nemo.collections.asr.models import ASRModel
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"loading Nemotron ASR '{MODEL_NAME}' on {dev}...")
        m = ASRModel.from_pretrained(model_name=MODEL_NAME)
        try:
            m = m.to(dev).eval()
        except Exception:
            pass
        # 关 CUDA-Graph 解码内核(见 NEMO_CUDA_GRAPHS 注释)。摸配置层级(不同 NeMo 版本略异),
        # 找到 use_cuda_graph_decoder / allow_cuda_graphs 就置 False,再 change_decoding_strategy 生效。
        if dev == "cuda" and not NEMO_CUDA_GRAPHS:
            try:
                from omegaconf import open_dict
                dec = m.cfg.decoding
                with open_dict(dec):
                    for sect in ("greedy", "greedy_batch"):
                        if sect in dec and dec[sect] is not None:
                            dec[sect]["use_cuda_graph_decoder"] = False
                            dec[sect]["allow_cuda_graphs"] = False
                m.change_decoding_strategy(dec)
                logger.info("RNNT CUDA-Graph 解码已关闭(共卡稳定性优先)")
            except Exception as e:
                logger.warning(f"关闭 CUDA-Graph 解码失败(继续,默认内核): {e}")
        # 低精度(见 NEMO_PRECISION 注释)：自检必须用「有内容的音频」——静音走不到 RNNT
        # label-loop 内核，测不出低精度的 illegal access(实测踩过)。噪声能产出非空解码路径。
        if NEMO_PRECISION in ("fp16", "bf16") and dev == "cuda":
            try:
                m = m.half() if NEMO_PRECISION == "fp16" else m.bfloat16()
                probe = (np.random.RandomState(0).randn(SAMPLE_RATE) * 0.1).astype(np.float32)
                m.transcribe([probe], verbose=False)
                logger.info(f"{NEMO_PRECISION} 低精度已启用(噪声自检通过)")
            except Exception as e:
                logger.warning(f"{NEMO_PRECISION} 自检失败，回退 fp32: {e}")
                m = m.float()
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
        # 预热（避开 sm_120 等首推冷启动）：转写 0.5s 静音
        try:
            m.transcribe([np.zeros(SAMPLE_RATE // 2, dtype=np.float32)], verbose=False)
        except Exception:
            pass
        # 归还分配器缓存块给驱动(配合 expandable_segments)：加载/预热期的峰值不变成常驻
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        _model = m
        _loaded = True
        logger.info("Nemotron ASR loaded + warmed up")
        return True
    except Exception as e:
        _load_error = str(e)
        logger.exception("ASR load failed")
        return False


def _read_audio(raw: bytes) -> np.ndarray:
    """字节 → float32 单声道 16k（soundfile，不依赖 ffmpeg）。"""
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SAMPLE_RATE:
        n = int(round(len(data) * SAMPLE_RATE / sr))
        if n > 0:
            data = np.interp(np.linspace(0, len(data), n, endpoint=False),
                             np.arange(len(data)), data).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32)


def _norm_lang(language: str):
    """归一 2 字母码：zh-cn/zh_tw → zh，en-us → en，空/auto → None(自动)。"""
    if not language:
        return None
    code = language.strip().lower().replace("_", "-").split("-")[0]
    return None if code in ("", "auto") else code


def _asr_decode(audio: np.ndarray, language: str) -> str:
    """整段转写（Nemotron）。语言用 source_lang 提示（40 语种单 checkpoint）。"""
    if not _loaded and not _load_model():
        raise RuntimeError(_load_error or "model not loaded")
    if audio.size < 1600:        # < 0.1s 视为空
        return ""
    lang = _norm_lang(language)
    # 不同 NeMo 版本 transcribe 签名略有差异：source_lang 不被识别时退回默认调用，保证鲁棒。
    try:
        out = _model.transcribe([audio], verbose=False,
                                **({"source_lang": lang} if lang else {}))
    except TypeError:
        out = _model.transcribe([audio], verbose=False)
    return _strip_tags(_first_text(out))


import re as _re
_re_lang_tag = _re.compile(r"\s*<[a-zA-Z]{1,3}(?:-[a-zA-Z]{2,4})?>\s*")   # Nemotron 末尾语言标签 如 <zh-CN>/<en>

def _strip_tags(s: str) -> str:
    """去掉 Nemotron 输出里的语言标签(<zh-CN> 等)，两端留白也清掉。"""
    return _re_lang_tag.sub(" ", s or "").strip()


def _first_text(out) -> str:
    """兼容 NeMo transcribe 多种返回形态：list[str] / list[Hypothesis] / (hyps, _)。"""
    if out is None:
        return ""
    if isinstance(out, tuple) and out:
        out = out[0]
    if isinstance(out, (list, tuple)) and out:
        item = out[0]
        if isinstance(item, str):
            return item.strip()
        return (getattr(item, "text", "") or "").strip()
    if isinstance(out, str):
        return out.strip()
    return (getattr(out, "text", "") or "").strip()


def _transcribe_sync(audio: np.ndarray, language: str, task: str = "transcribe") -> str:
    """task=translate（X→英）：Nemotron 转写 + MarianMT 译英，保持与 Whisper 契约一致的行为。"""
    text = _asr_decode(audio, language)
    if str(task).lower() == "translate" and text:
        try:
            text = _translate_sync(text, _mt_pair(language) or "zh", "en")
        except Exception as e:
            logger.warning(f"translate 组合失败，回退原文: {e}")
    return text


async def _run_gpu(fn, *a, **k):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_gpu_pool, functools.partial(fn, *a, **k))


async def _run_mt(fn, *a, **k):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_mt_pool, functools.partial(fn, *a, **k))


# ── 本地 MarianMT（与 stt_server.py 一致，保证 /translate 逐字节兼容）──────
_MT_MODELS = {
    ("zh", "en"): "Helsinki-NLP/opus-mt-zh-en",
    ("en", "zh"): "Helsinki-NLP/opus-mt-en-zh",
}
_mt_cache = {}


def _mt_pair(language: str) -> str:
    code = (language or "").strip().lower().replace("_", "-").split("-")[0]
    return "zh" if code in ("zh", "cmn", "yue") else ("en" if code in ("en", "") else code)


def _translate_sync(text: str, src: str, dest: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    s, d = _mt_pair(src), _mt_pair(dest)
    if s == d:
        return text
    name = _MT_MODELS.get((s, d))
    if not name:
        raise RuntimeError(f"不支持的翻译方向: {s}->{d}")
    import torch
    if (s, d) not in _mt_cache:
        from transformers import MarianMTModel, MarianTokenizer
        dev = NEMO_MT_DEVICE if NEMO_MT_DEVICE in ("cpu", "cuda") else "cpu"
        if dev == "cuda" and not torch.cuda.is_available():
            dev = "cpu"
        logger.info(f"loading MT '{name}' on {dev}...")
        tok = MarianTokenizer.from_pretrained(name)
        mdl = MarianMTModel.from_pretrained(name).to(dev).eval()
        _mt_cache[(s, d)] = (tok, mdl)
        logger.info(f"MT {s}->{d} loaded")
    tok, mdl = _mt_cache[(s, d)]
    dev = next(mdl.parameters()).device
    batch = tok([text], return_tensors="pt", padding=True, truncation=True, max_length=256).to(dev)
    global _mt_stream
    if dev.type == "cuda":
        if _mt_stream is None:
            _mt_stream = torch.cuda.Stream()
        with torch.no_grad(), torch.cuda.stream(_mt_stream):
            out = mdl.generate(**batch, max_length=256, num_beams=1)
        _mt_stream.synchronize()
    else:
        with torch.no_grad():
            out = mdl.generate(**batch, max_length=256, num_beams=1)
    return tok.decode(out[0], skip_special_tokens=True).strip()


class TranslateReq(BaseModel):
    text: str
    src: str = "zh"
    dest: str = "en"


class B64Req(BaseModel):
    audio_base64: str
    language: str = "zh"
    task: str = "transcribe"


# ── REST 端点（契约同 stt_server.py）─────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "loaded": _loaded, "model": MODEL_NAME, "service": "stt",
            "backend": "nemotron-3.5-asr",
            "mt_loaded": list(map(lambda k: f"{k[0]}->{k[1]}", _mt_cache.keys()))}


@app.post("/translate")
async def translate(req: TranslateReq):
    t = time.time()
    try:
        text = await _run_mt(_translate_sync, req.text, req.src, req.dest)
    except Exception as e:
        raise HTTPException(500, f"翻译失败: {e}")
    return {"ok": True, "text": text, "elapsed_ms": int((time.time() - t) * 1000)}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str = Form("zh"),
                     task: str = Form("transcribe")):
    raw = await audio.read()
    try:
        arr = _read_audio(raw)
    except Exception as e:
        raise HTTPException(400, f"无法解码音频: {e}")
    t = time.time()
    text = await _run_gpu(_transcribe_sync, arr, language, task)
    logger.info(f"transcribe {len(raw)//1024}KB -> '{text[:40]}' ({time.time()-t:.1f}s)")
    return {"ok": True, "text": text, "elapsed_s": round(time.time() - t, 2)}


@app.post("/transcribe_b64")
async def transcribe_b64(req: B64Req):
    try:
        raw = base64.b64decode(req.audio_base64)
        arr = _read_audio(raw)
    except Exception as e:
        raise HTTPException(400, f"无法解码音频: {e}")
    t = time.time()
    text = await _run_gpu(_transcribe_sync, arr, req.language, req.task)
    logger.info(f"transcribe_b64 -> '{text[:40]}' ({time.time()-t:.1f}s)")
    return {"ok": True, "text": text, "elapsed_s": round(time.time() - t, 2)}


# ── 流式增量转写（WebSocket）——Whisper 没有、Nemotron 的核心增量能力 ──────
# 协议：客户端上行二进制帧 = PCM16 little-endian / 16kHz / mono（任意长度，建议 ~每 STREAM_CHUNK_MS 一帧）；
#       上行文本 JSON 控制：{"event":"eou"} 触发本段定稿、{"event":"reset"} 清空、{"event":"close"} 结束。
#       下行 JSON：{"partial": "<增量假设>"} 持续刷新；{"final": "<本段定稿>"} 段末（auto_eou 时附 "auto":true）。
# 实现：累积窗口 + 周期重转写产出 partial（鲁棒、后端无关）；真·cache-aware FastConformer 流式
#       步进可在 _stream_decode 内替换（保持本协议不变）。
# 服务端自动定稿（4-A 架构收敛）：查询参数 auto_eou=1（或环境 NEMO_AUTO_EOU=1）开启后，服务端用能量
#       VAD 检测「说话→静音 sil_ms」自动产出 final 并清缓冲——瘦客户端只需推流、无需自带 VAD/发 eou。
#       默认关闭以兼容既有「客户端发 eou」的前端（避免双触发定稿）。sil_ms/min_voice_ms 可经查询参数调。
@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    lang = ws.query_params.get("language", "zh")
    buf = bytearray()
    min_step = max(1, int(SAMPLE_RATE * STREAM_CHUNK_MS / 1000)) * 2   # bytes（PCM16=2B/样本）
    last_emitted = ""
    last_decode_len = 0

    # 服务端自动定稿（静音 VAD）开关与阈值：默认关，避免与「客户端发 eou」的前端双触发
    def _truthy(v): return str(v).strip().lower() in ("1", "true", "yes", "on")
    auto_eou = _truthy(ws.query_params.get("auto_eou", os.environ.get("NEMO_AUTO_EOU", "0")))
    try:
        sil_ms_thr = float(ws.query_params.get("sil_ms", os.environ.get("NEMO_SIL_MS", "600")))
    except Exception:
        sil_ms_thr = 600.0
    try:
        min_voice_ms = float(ws.query_params.get("min_voice_ms", os.environ.get("NEMO_MIN_VOICE_MS", "250")))
    except Exception:
        min_voice_ms = 250.0
    # VAD 运行态
    speaking = False; voiced_ms = 0.0; sil_acc_ms = 0.0; noise_floor = 0.0008

    def _decode(pcm_bytes: bytes) -> str:
        arr = np.frombuffer(bytes(pcm_bytes), dtype=np.int16).astype("float32") / 32768.0
        return _asr_decode(np.ascontiguousarray(arr), lang)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                buf.extend(data)
                if auto_eou:
                    # 帧能量 VAD：跟踪「说话/静音」时长，用于服务端自动定稿
                    fr = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
                    if fr.size:
                        rms = float(np.sqrt(np.mean(fr * fr)))
                        fr_ms = fr.size / SAMPLE_RATE * 1000.0
                        if rms < noise_floor * 1.5:
                            noise_floor = noise_floor * 0.97 + rms * 0.03   # 静默期自适应噪声底
                        thr = max(0.006, noise_floor * 3.0)
                        if rms > thr:
                            speaking = True; voiced_ms += fr_ms; sil_acc_ms = 0.0
                        elif speaking:
                            sil_acc_ms += fr_ms
                        if not speaking:
                            # 未进入说话：仅保留 ~300ms 预滚动，避免长静音撑大缓冲/空转解码
                            keep = int(SAMPLE_RATE * 0.3) * 2
                            if len(buf) > keep:
                                del buf[:-keep]; last_decode_len = len(buf)
                # 自适应步进：句子越长，partial 间隔越大（长句 GPU 占用↓），但句首仍尽快出首块
                # （barge 延迟不变）。配合下方「窗口解码」把每步成本封顶，整体由 O(N²) 降到 ~O(N)。
                growth = 1 + min(len(buf) // (min_step * 6), 4)        # 1..5（随累积长度增大）
                if len(buf) - last_decode_len >= min_step * growth:
                    last_decode_len = len(buf)
                    win_bytes = int(SAMPLE_RATE * PARTIAL_WINDOW_SEC) * 2
                    slice_ = buf[-win_bytes:] if len(buf) > win_bytes else buf
                    text = await _run_gpu(_decode, bytes(slice_))      # 仅解码近端窗口，成本封顶
                    if text and text != last_emitted:
                        last_emitted = text
                        await ws.send_text(json.dumps({"partial": text}, ensure_ascii=False))
                # 服务端自动定稿：说话后静音超阈值 → 出 final 并清缓冲（瘦客户端无需发 eou）
                if auto_eou and speaking and sil_acc_ms >= sil_ms_thr and voiced_ms >= min_voice_ms:
                    final = await _run_gpu(_decode, bytes(buf)) if buf else ""
                    await ws.send_text(json.dumps({"final": final, "auto": True}, ensure_ascii=False))
                    buf.clear(); last_emitted = ""; last_decode_len = 0
                    speaking = False; voiced_ms = 0.0; sil_acc_ms = 0.0
                continue
            txt = msg.get("text")
            if txt:
                try:
                    evt = json.loads(txt).get("event", "")
                except Exception:
                    evt = txt.strip()
                if evt in ("eou", "final"):
                    final = await _run_gpu(_decode, bytes(buf)) if buf else ""
                    await ws.send_text(json.dumps({"final": final}, ensure_ascii=False))
                    buf.clear(); last_emitted = ""; last_decode_len = 0
                    speaking = False; voiced_ms = 0.0; sil_acc_ms = 0.0
                elif evt == "reset":
                    buf.clear(); last_emitted = ""; last_decode_len = 0
                    speaking = False; voiced_ms = 0.0; sil_acc_ms = 0.0
                elif evt == "close":
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"ws_transcribe 异常: {e}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _warmup_mt():
    try:
        _translate_sync("你好", "zh", "en")
        _translate_sync("hello", "en", "zh")
        logger.info("MT warmed up (zh<->en)")
    except Exception:
        logger.exception("MT warmup failed")


@app.on_event("startup")
async def _startup():
    import threading
    # 预热 transformers 完整初始化(主线程串行)：下面 ASR 线程(经 lightning→torchmetrics 取
    # transformers.AutoModel)与 MT 线程(取 MarianMTModel)会并发 import transformers；4.57 的惰性
    # 模块在半初始化态被另一线程取 AutoModel 会抛 ImportError(AutoModel 单独导入却正常)。先在主线程
    # 完整导入一次、令 sys.modules 落定，之后并发取属性即安全。
    try:
        import transformers  # noqa: F401
        from transformers import AutoModel, AutoTokenizer  # noqa: F401
    except Exception as _e:
        logger.warning(f"transformers 预热失败(继续): {_e}")
    threading.Thread(target=_load_model, daemon=True).start()
    threading.Thread(target=_warmup_mt, daemon=True).start()


if __name__ == "__main__":
    _port = int(os.environ.get("NEMO_STT_PORT", "7857"))
    uvicorn.run(app, host="0.0.0.0", port=_port, log_level="info")
