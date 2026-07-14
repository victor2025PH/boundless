"""
LatentSync 服务 —— 离线高清(512)口型生成。画质高于 MuseTalk，但基于扩散，非实时。
端口: 8091   运行环境: latentsync (独立 env，torch cu128 + diffusers + decord + mediapipe…)
工作目录必须是 C:\\模仿音色\\LatentSync （以便 import latentsync.*）

与 MuseTalk 服务同接口，便于 Hub 经 LipSyncAdapter 切换:
  GET  /health
  POST /lipsync/generate  multipart: audio(WAV) + face(图片) [+ fps, steps, guidance]
                          → 静态头像按音频时长铺成驱动视频 → 扩散生成 → MP4(无音轨)

说明: LatentSync 本是 视频→视频。数字人是单张照片，这里把照片铺成等时长视频作为驱动帧。
"""
import os, sys, time, tempfile, logging, threading, math, gc
# 减少显存碎片/保留增长（须在 torch 初始化 CUDA 前设置；本服务 torch 为懒加载，置于此处即可）
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import numpy as np
import cv2

# Windows + 中文路径(C:\模仿音色\...): cv2.imread/imwrite 不支持非 ASCII 路径 → 用 fromfile/imdecode 兜底
_orig_imread, _orig_imwrite = cv2.imread, cv2.imwrite


def _safe_imread(path, flags=cv2.IMREAD_COLOR):
    try:
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, flags) if data.size else None
        return img if img is not None else _orig_imread(path, flags)
    except Exception:
        return _orig_imread(path, flags)


def _safe_imwrite(path, img, *a):
    try:
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, img, *a)
        if ok:
            buf.tofile(path)
            return True
        return _orig_imwrite(path, img, *a)
    except Exception:
        return _orig_imwrite(path, img, *a)


cv2.imread, cv2.imwrite = _safe_imread, _safe_imwrite
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [LatentSync] %(message)s")
logger = logging.getLogger("latentsync")

import app_config
LS_ROOT = str(app_config.BASE / "LatentSync")
UNET_CONFIG = os.path.join(LS_ROOT, "configs", "unet", "stage2_512.yaml")
UNET_CKPT = os.path.join(LS_ROOT, "checkpoints", "latentsync_unet.pt")

# LatentSync 内部用 shutil.which('ffmpeg')，把随包的 imageio-ffmpeg 暴露成 PATH 上的 ffmpeg.exe
_FFMPEG_DIR = os.path.join(LS_ROOT, "_bin")
if os.path.isfile(os.path.join(_FFMPEG_DIR, "ffmpeg.exe")):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

app = FastAPI(title="LatentSync Server", version="1.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="latentsync")

_pipeline = None
_config = None
_dtype = None
_loaded = False
_load_error = ""
_load_lock = threading.Lock()

# 单线程串行化 GPU 推理：即使 Hub 并发投递多句，也逐句跑，避免共享 pipeline 争用/OOM
from concurrent.futures import ThreadPoolExecutor
_GEN_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ls-gen")

# ── 空闲自动卸载（默认关闭；设环境变量 LATENTSYNC_IDLE_UNLOAD=秒 开启）──
_IDLE_UNLOAD = float(os.environ.get("LATENTSYNC_IDLE_UNLOAD", "0"))
_last_used = time.time()
_inflight = 0
_inflight_lock = threading.Lock()


def _touch():
    global _last_used
    _last_used = time.time()


def _infer_enter():
    global _inflight
    with _inflight_lock:
        _inflight += 1
    _touch()


def _infer_leave():
    global _inflight
    with _inflight_lock:
        if _inflight > 0:
            _inflight -= 1
    _touch()


def _unload_models():
    global _pipeline, _loaded
    with _load_lock:
        if not _loaded:
            return
        _pipeline = None
        _loaded = False
    try:
        import torch
        gc.collect(); torch.cuda.empty_cache(); torch.cuda.ipc_collect()
    except Exception:
        pass
    logger.info("空闲卸载: LatentSync 已释放，下次请求将自动重载")


def _idle_watch():
    while True:
        time.sleep(30)
        try:
            if _IDLE_UNLOAD <= 0:
                continue
            with _inflight_lock:
                busy = _inflight > 0
            if _loaded and not busy and (time.time() - _last_used) > _IDLE_UNLOAD:
                _unload_models()
        except Exception:
            pass


def _load_models():
    global _pipeline, _config, _dtype, _loaded, _load_error
    with _load_lock:
        if _loaded:
            return True
        try:
            os.chdir(LS_ROOT)
            if LS_ROOT not in sys.path:
                sys.path.insert(0, LS_ROOT)
            import torch
            from omegaconf import OmegaConf
            from diffusers import AutoencoderKL, DDIMScheduler
            from latentsync.models.unet import UNet3DConditionModel
            from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
            from latentsync.whisper.audio2feature import Audio2Feature

            _config = OmegaConf.load(UNET_CONFIG)
            # sm_120(RTX 5090)上 fp16 扩散可能数值不稳→嘴部鬼影；LATENTSYNC_FP32=1 强制 fp32 排查/规避
            if os.environ.get("LATENTSYNC_FP32", "0") == "1":
                _dtype = torch.float32
            else:
                is_fp16 = torch.cuda.is_available() and torch.cuda.get_device_capability()[0] > 7
                _dtype = torch.float16 if is_fp16 else torch.float32

            scheduler = DDIMScheduler.from_pretrained(os.path.join(LS_ROOT, "configs"))
            cad = _config.model.cross_attention_dim
            whisper_path = os.path.join(LS_ROOT, "checkpoints", "whisper",
                                        "small.pt" if cad == 768 else "tiny.pt")
            audio_encoder = Audio2Feature(model_path=whisper_path, device="cuda",
                                          num_frames=_config.data.num_frames,
                                          audio_feat_length=_config.data.audio_feat_length)
            _vae_local = os.path.join(LS_ROOT, "checkpoints", "sd-vae-ft-mse")
            _vae_src = _vae_local if os.path.isdir(_vae_local) else "stabilityai/sd-vae-ft-mse"
            vae = AutoencoderKL.from_pretrained(_vae_src, torch_dtype=_dtype)
            vae.config.scaling_factor = 0.18215
            vae.config.shift_factor = 0
            unet, _ = UNet3DConditionModel.from_pretrained(
                OmegaConf.to_container(_config.model), UNET_CKPT, device="cpu")
            unet = unet.to(dtype=_dtype)
            _pipeline = LipsyncPipeline(vae=vae, audio_encoder=audio_encoder,
                                        unet=unet, scheduler=scheduler).to("cuda")

            # DeepCache: 跨去噪步缓存 UNet 上/中块，提速 ~1.5-2x（默认开，可用 env 关）
            if os.environ.get("LATENTSYNC_DEEPCACHE", "1") == "1":
                try:
                    from DeepCache import DeepCacheSDHelper
                    _ci = int(os.environ.get("LATENTSYNC_CACHE_INTERVAL", "3"))
                    _helper = DeepCacheSDHelper(pipe=_pipeline)
                    _helper.set_params(cache_interval=_ci, cache_branch_id=0)
                    _helper.enable()
                    logger.info(f"DeepCache 已启用 (cache_interval={_ci})")
                except Exception as _dce:
                    logger.warning(f"DeepCache 启用失败(忽略，按全步推理): {_dce}")

            _loaded = True
            logger.info(f"LatentSync 已加载 (res={_config.data.resolution}, dtype={_dtype})")
            return True
        except Exception as e:
            _load_error = f"{type(e).__name__}: {e}"
            logger.exception("加载失败")
            return False


def _audio_duration(path: str) -> float:
    info = sf.info(path)
    return info.frames / float(info.samplerate)


_FACE_DET = None
_FACE_DET_FAILED = False


def _get_face_det():
    """惰性初始化 insightface(buffalo_l，随 LatentSync 捆绑)用于人脸居中裁剪。"""
    global _FACE_DET, _FACE_DET_FAILED
    if _FACE_DET is not None or _FACE_DET_FAILED:
        return _FACE_DET
    try:
        from insightface.app import FaceAnalysis
        root = os.path.join(LS_ROOT, "checkpoints", "auxiliary")
        det = FaceAnalysis(name="buffalo_l", root=root,
                           providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        det.prepare(ctx_id=0, det_size=(640, 640))
        _FACE_DET = det
        logger.info("人脸检测器(buffalo_l)已就绪，HD 将人脸居中裁剪")
    except Exception as e:
        _FACE_DET_FAILED = True
        logger.warning(f"人脸检测器初始化失败，HD 回退整图居中裁剪: {e}")
    return _FACE_DET


def _detect_face_box(img_bgr: np.ndarray, margin: float = 1.9):
    """检测最大人脸 → 返回居中正方形框 (cx, cy, half)；无脸返回 None。"""
    det = _get_face_det()
    if det is None:
        return None
    try:
        faces = det.get(img_bgr)
        if not faces:
            return None
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        x1, y1, x2, y2 = f.bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        half = max(x2 - x1, y2 - y1) * margin / 2
        return (cx, cy, half)
    except Exception as e:
        logger.warning(f"人脸检测失败,回退整图: {e}")
        return None


def _crop_to_box(img_bgr: np.ndarray, box, res: int) -> np.ndarray:
    """按 (cx,cy,half) 裁正方形并缩放到 res；越界用 reflect 补齐保持人脸居中不变形。
    box=None → 整图居中裁。"""
    h, w = img_bgr.shape[:2]
    if box is None:
        scale = res / min(h, w)
        rz = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        yh, xw = rz.shape[:2]
        y0, x0 = (yh - res) // 2, (xw - res) // 2
        return rz[y0:y0 + res, x0:x0 + res]
    cx, cy, half = box
    x0, y0 = int(round(cx - half)), int(round(cy - half))
    x1, y1 = int(round(cx + half)), int(round(cy + half))
    pad_l, pad_t = max(0, -x0), max(0, -y0)
    pad_r, pad_b = max(0, x1 - w), max(0, y1 - h)
    if pad_l or pad_t or pad_r or pad_b:
        img_bgr = cv2.copyMakeBorder(img_bgr, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_REFLECT)
        x0 += pad_l; x1 += pad_l; y0 += pad_t; y1 += pad_t
    crop = img_bgr[y0:y1, x0:x1]
    return cv2.resize(crop, (res, res), interpolation=cv2.INTER_AREA)


def _make_driving_video(face_bgr: np.ndarray, n_frames: int, fps: int, out_path: str):
    """把单张头像铺成 n_frames 帧的驱动视频（512 见方，人脸居中裁/缩放）。"""
    res = int(_config.data.resolution) if _config else 512
    crop = _crop_to_box(face_bgr, _detect_face_box(face_bgr), res)
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (res, res))
    for _ in range(n_frames):
        writer.write(crop)
    writer.release()


def _make_driving_video_from_video(src_path: str, out_path: str, fps: int):
    """把驱动视频(如 MuseTalk 256 说话头)逐帧人脸居中裁到 512 作为 LatentSync 驱动。
    级联用：MuseTalk 提供真实嘴动/帧间变化 → LatentSync 有自然动态,口型生成稳定不糊。
    人脸框在首帧检测一次后复用(说话头人脸基本固定),保证时序稳定。"""
    res = int(_config.data.resolution) if _config else 512
    cap = cv2.VideoCapture(src_path)
    box, writer, n = None, None, 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if box is None:
            box = _detect_face_box(fr)
        crop = _crop_to_box(fr, box, res)
        if writer is None:
            writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (res, res))
        writer.write(crop)
        n += 1
    cap.release()
    if writer is not None:
        writer.release()
    return n


def _run_generate(face_bgr, audio_path, fps, steps, guidance, driver_video_path=None) -> bytes:
    if not _loaded and not _load_models():
        raise RuntimeError(_load_error or "model not loaded")
    if not steps or steps <= 0:                 # steps=0 → 用服务端默认(env 可调；10≈26s/15≈31s/20≈38s)
        steps = int(os.environ.get("LATENTSYNC_STEPS", "15"))
    import torch
    import shutil
    dur = _audio_duration(audio_path)
    n = max(_config.data.num_frames, math.ceil(dur * fps) + 2)
    work_dir = tempfile.mkdtemp(prefix="ls_")          # pipeline 会 rmtree(temp_dir)，给它独立目录
    tmp_vid = os.path.join(work_dir, "drive.mp4")
    out_vid = tempfile.NamedTemporaryFile(suffix="_out.mp4", delete=False).name  # 必须在 work_dir 之外
    _infer_enter()
    try:
        if driver_video_path and os.path.isfile(driver_video_path):
            # 级联模式：用 MuseTalk 说话头视频作驱动(有真实动态)→ 口型稳定不糊
            nv = _make_driving_video_from_video(driver_video_path, tmp_vid, fps)
            logger.info(f"驱动视频(级联)已裁剪: {nv} 帧")
        else:
            _make_driving_video(face_bgr, n, fps, tmp_vid)
        _pipeline(video_path=tmp_vid, audio_path=audio_path, video_out_path=out_vid,
                  num_frames=_config.data.num_frames, num_inference_steps=steps,
                  guidance_scale=guidance, weight_dtype=_dtype,
                  width=_config.data.resolution, height=_config.data.resolution,
                  mask_image_path=os.path.join(LS_ROOT, _config.data.mask_image_path),
                  temp_dir=work_dir)
        with open(out_vid, "rb") as f:
            return f.read()
    finally:
        try: os.unlink(out_vid)
        except Exception: pass
        shutil.rmtree(work_dir, ignore_errors=True)
        # 每次生成后释放本轮累积的 CPU/GPU 内存，避免长时间运行内存被吃爆
        face_bgr = None
        try:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        _infer_leave()


@app.get("/health")
def health():
    return {"ok": True, "service": "latentsync", "loaded": _loaded,
            "load_error": _load_error, "realtime": False, "resolution": "512"}


@app.get("/meminfo")
def meminfo():
    info = {"service": "latentsync"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu_alloc_mb"] = round(torch.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(torch.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info


@app.post("/gc")
def gc_endpoint():
    """非侵入式回收：gc + 释放显存缓存，不卸载模型。供看门狗优先调用以避免重启打断业务。"""
    before = None
    try:
        import torch
        if torch.cuda.is_available():
            before = torch.cuda.memory_reserved()
    except Exception:
        before = None
    n = gc.collect()
    freed_mb = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - torch.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}


@app.post("/lipsync/preload")
def preload():
    threading.Thread(target=_load_models, daemon=True).start()
    return {"ok": True, "detail": "后台加载中"}


@app.post("/unload")
def unload():
    """显式卸载模型释放显存。供 Hub 在 HD 渲染结束后调用，把 ~6GB 还给 LLM 回暖。"""
    was = _loaded
    _unload_models()
    return {"ok": True, "was_loaded": was, "loaded": _loaded}


@app.post("/lipsync/generate")
async def generate(audio: UploadFile = File(...), face: UploadFile = File(None),
                   driver_video: UploadFile = File(None),
                   face_id: str = Form(""), fps: int = Form(25),
                   steps: int = Form(0), guidance: float = Form(1.5),
                   batch_size: int = Form(0)):
    if face is None:
        raise HTTPException(400, "LatentSync 需要 face 图片")
    face_bytes = await face.read()
    face_bgr = cv2.imdecode(np.frombuffer(face_bytes, np.uint8), cv2.IMREAD_COLOR)
    if face_bgr is None:
        raise HTTPException(400, "无法解码人脸图片")
    audio_bytes = await audio.read()
    tmp_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    with open(tmp_audio, "wb") as f:
        f.write(audio_bytes)
    tmp_driver = None
    if driver_video is not None:
        dv = await driver_video.read()
        if dv:
            tmp_driver = tempfile.NamedTemporaryFile(suffix="_drv.mp4", delete=False).name
            with open(tmp_driver, "wb") as f:
                f.write(dv)
    try:
        t = time.time()
        import asyncio, functools
        loop = asyncio.get_event_loop()
        video = await loop.run_in_executor(
            _GEN_POOL, functools.partial(_run_generate, face_bgr, tmp_audio, fps,
                                         steps, guidance, tmp_driver))
        logger.info(f"generate done {time.time()-t:.1f}s {len(video)//1024}KB"
                    + (" (级联驱动)" if tmp_driver else ""))
        return Response(content=video, media_type="video/mp4",
                        headers={"X-Processing-Time": f"{time.time()-t:.2f}s"})
    except Exception as e:
        logger.exception("generate error")
        raise HTTPException(500, str(e))
    finally:
        for _p in (tmp_audio, tmp_driver):
            if _p:
                try: os.unlink(_p)
                except Exception: pass


def _warmup():
    """启动预热：用一张人脸 + 极短合成音频跑一窗 2 步推理，把首前向的 CUDA kernel/cuDNN 编译
    提前到启动期（5090 上首前向冷编译可达 ~80s）→ 让首个真实出片请求即热。best-effort，失败不影响服务。
    步数只影响迭代次数、不影响 kernel 形状，故用极小步数(默认 2)即可完成编译、把预热开销压到最低。"""
    if os.environ.get("LATENTSYNC_WARMUP", "1") == "0":
        return
    wav = None
    try:
        import glob as _glob
        face_path = os.environ.get("LATENTSYNC_WARMUP_FACE", "").strip()
        if not face_path or not os.path.isfile(face_path):
            cands = sorted(_glob.glob(str(app_config.BASE / "avatar_videos" / "*.jpg")))
            face_path = cands[0] if cands else ""
        if not face_path or not os.path.isfile(face_path):
            logger.info("启动预热跳过(未找到预热人脸；可设 LATENTSYNC_WARMUP_FACE 指定)")
            return
        face_bgr = cv2.imread(face_path)
        if face_bgr is None:
            logger.info("启动预热跳过(预热人脸无法解码)")
            return
        wav = tempfile.NamedTemporaryFile(suffix="_warm.wav", delete=False).name
        sr = 16000
        t = np.linspace(0, 0.8, int(sr * 0.8), endpoint=False)
        sf.write(wav, (0.05 * np.sin(2 * np.pi * 180 * t)).astype("float32"), sr)
        steps = int(os.environ.get("LATENTSYNC_WARMUP_STEPS", "2"))
        t0 = time.time()
        _run_generate(face_bgr, wav, 25, steps, 1.5)
        logger.info(f"启动预热完成({time.time()-t0:.1f}s)：首个出片请求即热")
    except Exception as e:
        logger.warning(f"启动预热跳过(不影响按需加载): {e}")
    finally:
        if wav:
            try: os.unlink(wav)
            except Exception: pass


def _load_and_warmup():
    if _load_models():
        _warmup()


@app.on_event("startup")
async def _startup():
    # 显存紧张时(LLM 常驻 ~17.8G)启动期预载会 OOM；设 LATENTSYNC_PRELOAD=0 改为按需加载。
    if os.environ.get("LATENTSYNC_PRELOAD", "1") != "0":
        threading.Thread(target=_load_and_warmup, daemon=True).start()
    else:
        logger.info("启动期预载已关闭(LATENTSYNC_PRELOAD=0)，首个请求时按需加载")
    if _IDLE_UNLOAD > 0:
        logger.info(f"空闲自动卸载已启用: {_IDLE_UNLOAD:.0f}s")
        threading.Thread(target=_idle_watch, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="info")
