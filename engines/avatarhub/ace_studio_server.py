# -*- coding: utf-8 -*-
"""
ACE Studio — 原创歌生成服务（端口 7859，conda env: ymsvc）

引擎：ACE-Step v1-3.5B（Apache-2.0，2025-05 开源）整曲文本成曲
  输入：风格标签(prompt) + 结构化歌词([verse]/[chorus]…) + 时长
  输出：48kHz 立体声整曲（人声+编曲一体生成）

设计要点（与 song_studio 同构）：
  * 权重走本地绝对路径（models/ace_step/ACE-Step-v1-3.5B），运行期零 HF 网络依赖；
  * 懒加载 + 任务后默认卸载（ACE_KEEP_LOADED=1 关闭卸载）——bf16 峰值 ~8.4GB，
    与直播三件套不共存，靠 O6 让路机制在空卡档期跑；
  * 单 worker 线程串行消费；生成前问 Hub /api/song/yield（直播中挂起，可取消，
    Hub 不可达 = fail-open 不让路）；
  * /health 能力旗标：权重不齐 capabilities.create=False，不假在线。

真跑基准（5090 空卡，2026-07-07 冒烟）：30s 歌 27 步 = 2.6s 生成，加载 ~13s。
"""
import base64
import gc
import logging
import os
import queue
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
ACE_REPO = BASE / "ACE-Step"
CKPT = BASE / "models" / "ace_step" / "ACE-Step-v1-3.5B"
LORAS_DIR = BASE / "models" / "ace_step" / "loras"   # P6: 唱腔 LoRA（<name>/pytorch_lora_weights.safetensors）

if str(ACE_REPO) not in sys.path:
    sys.path.insert(0, str(ACE_REPO))
os.environ.setdefault("HF_HUB_OFFLINE", "1")     # 权重全本地，禁 HF 网络

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ace_studio")

PORT = int(os.environ.get("ACE_STUDIO_PORT", "7859"))
KEEP_LOADED = os.environ.get("ACE_KEEP_LOADED", "0") == "1"
CPU_OFFLOAD = os.environ.get("ACE_CPU_OFFLOAD", "0") == "1"
MAX_SEC = int(os.environ.get("ACE_MAX_SEC", "240"))              # 4 分钟
TASK_TTL_SEC = 3600 * 6

# O6 直播让路（与 song_studio 完全同一协议）：生成是重活（8GB+ 峰值），
# 开工前问 Hub；直播/对话中挂起等待，结束自动继续。
HUB_URL = os.environ.get("SONG_HUB_URL", "http://127.0.0.1:9000").rstrip("/")
LIVE_YIELD = os.environ.get("SONG_LIVE_YIELD", "1") != "0"
YIELD_POLL_S = float(os.environ.get("SONG_YIELD_POLL_S", "5"))
_YIELD_LAST = {"yield": False, "reason": "", "ts": 0.0}

TMP_ROOT = Path(os.environ.get("ACE_STUDIO_TMP", str(BASE / "tmp" / "ace_studio")))
TMP_ROOT.mkdir(parents=True, exist_ok=True)

TASKS: dict = {}
_task_q: "queue.Queue[str]" = queue.Queue()
_pipe = None                     # 懒加载的 ACEStepPipeline
_load_lock = threading.Lock()


def _weights_ok() -> bool:
    need = ("ace_step_transformer", "music_dcae_f8c8", "music_vocoder", "umt5-base")
    return all((CKPT / d).exists() for d in need)


def _loras_avail() -> list:
    """就绪的唱腔 LoRA 清单（目录含 pytorch_lora_weights.safetensors 才算，绝不假在线）。"""
    out = []
    try:
        if LORAS_DIR.is_dir():
            for d in sorted(LORAS_DIR.iterdir()):
                if d.is_dir() and (d / "pytorch_lora_weights.safetensors").exists():
                    out.append(d.name)
    except Exception:
        pass
    return out


def _cap() -> dict:
    # P5/F5: remix = audio2audio 风格魔改（同权重同管线，参考音频引导生成）
    # P6: loras = 就绪唱腔 LoRA 名单（如实上报，空=没装）
    return {"create": _weights_ok(), "remix": _weights_ok(), "loras": _loras_avail()}


class CancelledError(RuntimeError):
    pass


# ─────────────────────── O6 直播让路 ───────────────────────

def _yield_probe() -> dict:
    if not LIVE_YIELD:
        return {"yield": False, "reason": "", "source": ""}
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(HUB_URL + "/api/song/yield", timeout=4) as r:
            j = _json.loads(r.read().decode("utf-8"))
        st = {"yield": bool(j.get("yield")), "reason": str(j.get("reason", "")),
              "source": str(j.get("source", ""))}
    except Exception:
        st = {"yield": False, "reason": "", "source": ""}
    _YIELD_LAST.update(st, ts=time.time())
    return st


# vram 源的让路只等一小会儿：显存高压若非直播（live/converse/hold 都会单独报出来），
# 多半是闲置引擎常驻占卡——死等等不来，放行给 _ensure_vram 去「请 Hub 腾闲置引擎」。
# （2026-07-07 真跑实锤：31.5G 被闲置 latentsync 等占满，任务在 vram 让路里挂了 20 分钟）
VRAM_YIELD_GRACE_S = float(os.environ.get("SONG_VRAM_YIELD_GRACE_S", "30"))


def _yield_wait(tid: str, t: dict) -> int:
    st = _yield_probe()
    if not st["yield"]:
        return 0
    t0 = time.time()
    logger.info(f"[{tid}] 直播让路挂起: {st['reason']}")
    while st["yield"]:
        if st.get("source") == "vram" and time.time() - t0 > VRAM_YIELD_GRACE_S:
            logger.info(f"[{tid}] 让路源=显存高压且非直播，转交显存清场处理")
            break
        _set(tid, status="queued",
             detail=f"直播让路中（{st['reason'] or '直播进行中'}）——原创歌不抢直播算力，结束后自动继续")
        for _ in range(int(max(1.0, YIELD_POLL_S))):
            if t.get("cancel"):
                raise CancelledError()
            time.sleep(1.0)
        st = _yield_probe()
    waited = int((time.time() - t0) * 1000)
    if waited > 1000:
        logger.info(f"[{tid}] 让路 {waited/1000:.0f}s 后继续")
    return waited


# ── 显存清场：让路解决「直播中别抢」，这里解决「不直播但闲置引擎占着卡」────────
# ACE 3.5B bf16 峰值 ~8.4GB，直播三件套常驻时只剩 ~6GB。加载前查空闲显存，
# 不够先请 Hub「腾显存」（只停当前用不到的闲置引擎，核心不动、被调用时自愈复活），
# 再不够就人话报错——绝不放 CUDA OOM 出去砸在用户脸上。
ACE_MIN_FREE_MB = int(os.environ.get("ACE_MIN_FREE_MB",
                                     "7500" if CPU_OFFLOAD else "11000"))


def _vram_free_mb() -> int:
    try:
        import torch
        if not torch.cuda.is_available():
            return 1 << 20            # 无 CUDA 环境（CPU 跑）：不拦
        free, _ = torch.cuda.mem_get_info()
        return int(free / (1024 * 1024))
    except Exception:
        return 1 << 20                # 探测失败不拦（fail-open，交给加载去试）


def _ensure_vram(tid: str, t: dict) -> None:
    if _pipe is not None:             # 已加载（KEEP_LOADED）：不需要再腾
        return
    free = _vram_free_mb()
    if free >= ACE_MIN_FREE_MB:
        return
    logger.info(f"[{tid}] 显存不足({free}MB < {ACE_MIN_FREE_MB}MB)，请 Hub 腾显存(free_unused)")
    _set(tid, status="queued", detail="显存吃紧——正在请闲置引擎腾位（不影响直播核心）")
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(HUB_URL + "/api/gpu/free_unused", method="POST",
                                     data=b"", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"[{tid}] free_unused 调用失败(继续按现状检查): {e}")
    for _ in range(10):               # 停服务→显存归还有秒级延迟，最多等 20s
        if t.get("cancel"):
            raise CancelledError()
        time.sleep(2.0)
        free = _vram_free_mb()
        if free >= ACE_MIN_FREE_MB:
            logger.info(f"[{tid}] 腾出显存，现空闲 {free}MB")
            return
    raise RuntimeError(
        f"显存不足（空闲 {free}MB，需约 {ACE_MIN_FREE_MB}MB）：直播引擎正占着显卡。"
        f"请在运维看板点「释放显存」后重试，或等下播时段自动空闲")


# ─────────────────────── 模型加载/卸载 ───────────────────────

def _load_pipe():
    global _pipe
    with _load_lock:
        if _pipe is not None:
            return _pipe
        from acestep.pipeline_ace_step import ACEStepPipeline
        t0 = time.time()
        p = ACEStepPipeline(checkpoint_dir=str(CKPT), dtype="bfloat16",
                            cpu_offload=CPU_OFFLOAD, torch_compile=False)
        p.load_checkpoint(str(CKPT))
        _pipe = p
        logger.info(f"ACE-Step 加载完成 {time.time()-t0:.1f}s (offload={CPU_OFFLOAD})")
        return _pipe


def _unload():
    global _pipe
    with _load_lock:
        if _pipe is None:
            return
        _pipe = None
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info("ACE-Step 已卸载（显存归还）")


# ─────────────────────── 任务执行 ───────────────────────

def _task_dir(tid: str) -> Path:
    d = TMP_ROOT / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set(tid: str, **kw):
    t = TASKS.get(tid)
    if t is not None:
        t.update(kw)


def _cleanup_old():
    now = time.time()
    for tid, t in list(TASKS.items()):
        if now - t.get("created", now) > TASK_TTL_SEC:
            shutil.rmtree(TMP_ROOT / tid, ignore_errors=True)
            TASKS.pop(tid, None)


def _worker():
    while True:
        tid = _task_q.get()
        t = TASKS.get(tid)
        if t is None or t.get("cancel"):
            _set(tid, status="cancelled")
            continue
        d = _task_dir(tid)
        t0 = time.time()
        timings = {}
        pipe = None
        try:
            # 让路在加载模型之前问：加载本身就要 8GB+，直播中一点不碰
            timings["yield_ms"] = _yield_wait(tid, t)
            # 不直播但闲置引擎占卡：请 Hub 腾显存，不够则人话拒（绝不裸 OOM）
            _ensure_vram(tid, t)

            _set(tid, status="loading", progress=8, detail="正在加载作曲引擎")
            ts = time.time()
            pipe = _load_pipe()
            timings["load_ms"] = int((time.time() - ts) * 1000)
            if t.get("cancel"):
                raise CancelledError()

            ref_wav = d / "ref.wav"
            is_remix = ref_wav.exists()
            _lora = (t.get("lora") or "").strip()
            _set(tid, status="generating", progress=20,
                 detail=("正在按参考歌重新编曲演绎" if is_remix
                         else "正在作曲编曲并演唱（整曲一体生成）")
                        + (f"（唱腔：{_lora}）" if _lora else ""))
            ts = time.time()
            out_wav = str(d / "result.wav")
            seeds = [int(t["seed"])] if t.get("seed") is not None else None
            kw = {}
            if is_remix:
                # P5/F5: audio2audio——参考歌 latents 加噪后去噪；strength 越高越像原曲
                # （管线内 frame_length 跟随 ref，长度以参考段为准）
                kw = {"audio2audio_enable": True,
                      "ref_audio_input": str(ref_wav),
                      "ref_audio_strength": float(t.get("ref_strength", 0.5))}
            if _lora:
                # P6: 唱腔 LoRA——管线 __call__ 内部 load_lora 幂等（同路径同权不重载）；
                # 本服务任务后整管线卸载，不存在跨任务串味
                kw.update(lora_name_or_path=str(LORAS_DIR / _lora),
                          lora_weight=float(t.get("lora_weight", 1.0)))
            pipe(
                format="wav",
                audio_duration=float(t["duration_s"]),
                prompt=t["prompt"],
                lyrics=t["lyrics"],
                infer_step=int(t["steps"]),
                guidance_scale=float(t.get("guidance_scale", 15.0)),
                scheduler_type="euler",
                cfg_type="apg",
                omega_scale=10.0,
                manual_seeds=seeds,
                save_path=out_wav,
                **kw,
            )
            timings["generate_ms"] = int((time.time() - ts) * 1000)

            import soundfile as sf
            info = sf.info(out_wav)
            dur = info.duration
            elapsed = int((time.time() - t0) * 1000)
            _set(tid, status="done", progress=100, detail="",
                 result={
                     "duration_s": round(dur, 1),
                     "sample_rate": info.samplerate,
                     "steps": int(t["steps"]),
                     "seed": t.get("seed"),
                     "mode": "remix" if is_remix else "create",
                     "ref_strength": t.get("ref_strength") if is_remix else None,
                     "lora": _lora or None,
                     "lora_weight": float(t.get("lora_weight", 1.0)) if _lora else None,
                     "timings": timings,
                     "elapsed_ms": elapsed,
                     "rtf": round(elapsed / 1000.0 / max(0.1, dur), 2),
                 })
            logger.info(f"[{tid}] done {dur:.0f}s原创歌 用时{elapsed/1000:.1f}s "
                        f"(gen {timings.get('generate_ms', 0)}ms)")
        except CancelledError:
            _set(tid, status="cancelled", detail="已取消")
            logger.info(f"[{tid}] cancelled")
        except Exception as e:
            logger.exception(f"[{tid}] failed")
            _set(tid, status="error", detail=f"{type(e).__name__}: {e}")
        finally:
            # 先断本地引用再卸载：否则 worker 的 pipe 局部变量仍攥着 8.4GB 模型，
            # _unload 里 gc+empty_cache 收不回显存，下个任务预检看到的是假占用
            # （2026-07-07 真跑实锤：任务后显存停在 27.4G 不回落，连环任务被误拒）
            pipe = None
            if not KEEP_LOADED:
                _unload()
            _cleanup_old()


threading.Thread(target=_worker, daemon=True, name="ace_worker").start()


# ─────────────────────── FastAPI ───────────────────────

from fastapi import FastAPI, HTTPException          # noqa: E402
from fastapi.responses import FileResponse          # noqa: E402
from pydantic import BaseModel                      # noqa: E402

app = FastAPI(title="ACE Studio (ACE-Step)", version="1.0")
try:
    import service_auth
    service_auth.secure(app, name="ace_studio")
except Exception as _e:
    logger.warning(f"service_auth 未接入: {_e}")


class CreateRequest(BaseModel):
    prompt:      str                       # 风格标签，如 "pop, mandarin, female vocal, 90 bpm"
    lyrics:      str = ""                  # 结构化歌词（[verse]/[chorus]…）；空=纯音乐
    duration_s:  float = 60.0
    steps:       int = 27                  # 27 turbo / 60 fine
    seed:        Optional[int] = None
    guidance_scale: float = 15.0
    song_name:   str = ""
    # P5/F5 风格魔改（audio2audio）：给参考歌 → 按新风格标签重新编曲演绎
    ref_b64:     str = ""                  # 参考音频（wav b64；长度决定成曲长度）
    ref_strength: float = 0.5              # 0~1：越高越贴原曲结构，越低越放飞
    # P6 唱腔 LoRA：models/ace_step/loras/ 下的目录名（如 ACE-Step-v1-chinese-rap-LoRA）
    lora:        str = ""
    lora_weight: float = 1.0               # 0~1：官方建议特化唱腔 0.9~1.0


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "ace_studio",
        "engine": "ace_step_v1_3.5b",
        "capabilities": _cap(),
        "loaded": _pipe is not None,
        "cpu_offload": CPU_OFFLOAD,
        "live_yield": dict(_YIELD_LAST),
        "queue": _task_q.qsize(),
        "active": [tid for tid, t in TASKS.items()
                   if t.get("status") in ("queued", "loading", "generating")],
        "max_duration_s": MAX_SEC,
    }


@app.post("/v1/create")
def submit_create(req: CreateRequest):
    if not _weights_ok():
        raise HTTPException(503, "原创歌引擎权重未就绪，请先运行 tools/setup_ace_step.py")
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "风格标签不能为空（如：pop, mandarin, female vocal）")
    if len(prompt) > 600 or len(req.lyrics or "") > 6000:
        raise HTTPException(400, "风格/歌词过长（风格≤600字符，歌词≤6000字符）")
    dur = max(10.0, min(float(MAX_SEC), float(req.duration_s)))

    tid = uuid.uuid4().hex[:12]
    d = _task_dir(tid)
    if req.ref_b64:
        # P5/F5: 参考歌落盘（成曲长度跟随参考段；Hub 侧已按需截副歌窗）
        try:
            ref_bytes = base64.b64decode(req.ref_b64)
        except Exception:
            raise HTTPException(400, "参考音频 base64 解码失败")
        if len(ref_bytes) > 60 * 1024 * 1024:
            raise HTTPException(400, "参考音频过大（上限 60MB），请先截段")
        (d / "ref.wav").write_bytes(ref_bytes)
    lora = (req.lora or "").strip()
    if lora and lora not in _loras_avail():
        raise HTTPException(400, f"唱腔 LoRA「{lora}」未安装"
                                 f"（可用：{_loras_avail() or '无'}），"
                                 f"补齐: python tools/setup_ace_lora.py")
    TASKS[tid] = {
        "status": "queued", "progress": 2, "detail": "排队中",
        "created": time.time(), "cancel": False,
        "song_name": req.song_name or "未命名原创歌",
        "prompt": prompt,
        "lyrics": (req.lyrics or "").strip(),
        "duration_s": dur,
        "steps": max(10, min(100, req.steps)),
        "seed": req.seed,
        "guidance_scale": max(1.0, min(30.0, req.guidance_scale)),
        "ref_strength": max(0.05, min(0.95, float(req.ref_strength or 0.5))),
        "lora": lora,
        "lora_weight": max(0.0, min(1.0, float(req.lora_weight or 1.0))),
    }
    _task_q.put(tid)
    return {"ok": True, "task_id": tid, "queue_pos": _task_q.qsize(),
            "duration_s": dur}


@app.get("/v1/task/{tid}")
def task_status(tid: str):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    out = {"task_id": tid, "status": t["status"], "progress": t.get("progress", 0),
           "detail": t.get("detail", ""), "song_name": t.get("song_name", ""),
           "result": t.get("result"),
           "elapsed_ms": int((time.time() - t["created"]) * 1000)}
    if t["status"] == "queued":
        ahead = sum(1 for x in TASKS.values()
                    if x.get("status") == "queued"
                    and x.get("created", 0) < t.get("created", 0))
        busy = any(x.get("status") in ("loading", "generating")
                   for x in TASKS.values())
        out["queue_ahead"] = ahead + (1 if busy else 0)
        if out["queue_ahead"] and not t.get("detail", "").startswith("直播让路"):
            out["detail"] = f"排队中：前面还有 {out['queue_ahead']} 首"
    return out


@app.get("/v1/task/{tid}/audio")
def task_audio(tid: str):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    if t.get("status") != "done":
        raise HTTPException(400, f"任务未完成: {t.get('status')}")
    p = TMP_ROOT / tid / "result.wav"
    if not p.exists():
        raise HTTPException(404, "产物文件已清理")
    return FileResponse(str(p), media_type="audio/wav", filename=f"create_{tid}.wav")


@app.post("/v1/task/{tid}/cancel")
def task_cancel(tid: str):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    t["cancel"] = True
    if t.get("status") == "queued":
        _set(tid, status="cancelled", detail="已取消")
    return {"ok": True, "task_id": tid}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"ACE Studio 启动 :{PORT} capabilities={_cap()} "
                f"ckpt={'OK' if _weights_ok() else '缺失'}")
    if not _weights_ok():
        logger.warning("权重缺失 → 原创歌能力关闭（/health 如实上报），"
                       "补齐: python tools/setup_ace_step.py")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
