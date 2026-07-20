"""
人脸增强服务 —— 给口型/换脸输出做 GFPGAN 复原，提升 MuseTalk 256 实时口型的清晰度。

端口: 8092  (运行环境: facefusion，已装 gfpgan/basicsr/torch cu128 + insightface buffalo_l)
设计:
- 用已缓存的 insightface buffalo_l 只做 5 点关键点检测（CPU，零额外下载），
  按 FFHQ 512 模板对齐 → 裸 GFPGANv1Clean(GPU,fp16) 复原 → 反仿射 + 羽化贴回原帧。
- 避开 facexlib（其检测/解析权重未下载），不需要任何新模型。
- 数字人头像为静帧循环 → 人脸位置逐帧固定，默认只检测一次复用仿射矩阵（detect_once），大幅提速。

API:
  GET  /health
  POST /enhance_video  (multipart video=<silent mp4>; form blend, detect_once) → 增强后的无声 mp4
"""
import os, sys, io, time, tempfile, logging, threading, gc
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import numpy as np
import cv2
import torch
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Enhance] %(message)s")
logger = logging.getLogger("enhance")

import app_config
GFPGAN_MODEL = str(app_config.BASE / "GFPGANv1.4.pth")
PORT = int(os.environ.get("ENHANCE_PORT") or app_config.port("enhance") or 8092)
_device = "cuda" if torch.cuda.is_available() else "cpu"
# 注意：GFPGAN 的 StyleGAN2 解码器在 fp16 下数值溢出→输出恒定色块，必须用 fp32。
_dtype = torch.float32

# FFHQ 512 对齐模板（facexlib 标准 5 点，顺序: 左眼/右眼/鼻/左嘴角/右嘴角，与 insightface kps 一致）
_FACE_TEMPLATE = np.array([
    [192.98138, 239.94708], [318.90277, 240.1936],
    [256.63416, 314.01935], [201.26117, 371.41043], [313.08905, 371.15118],
], dtype=np.float32)

app = FastAPI(title="Face Enhance (GFPGAN)", version="1.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="enhance")

_lock = threading.Lock()
_model = None
_det = None
_load_error = ""


def _load():
    global _model, _det, _load_error
    with _lock:
        if _model is not None and _det is not None:
            return True
        try:
            from gfpgan.archs.gfpganv1_clean_arch import GFPGANv1Clean
            m = GFPGANv1Clean(out_size=512, num_style_feat=512, channel_multiplier=2,
                              decoder_load_path=None, fix_decoder=False, num_mlp=8,
                              input_is_latent=True, different_w=True, narrow=1, sft_half=True)
            ckpt = torch.load(GFPGAN_MODEL, map_location="cpu")
            sd = ckpt.get("params_ema", ckpt.get("params", ckpt))
            m.load_state_dict(sd, strict=False)
            m.eval().to(_device)
            if _dtype != torch.float32:
                m.to(dtype=_dtype)   # bf16：范围安全、~1.65× 提速
            _model = m

            from insightface.app import FaceAnalysis
            det = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                               providers=["CPUExecutionProvider"])
            det.prepare(ctx_id=-1, det_size=(640, 640))
            _det = det
            logger.info(f"GFPGAN + insightface(det) 就绪 on {_device}/{_dtype}")
            return True
        except Exception as e:
            _load_error = str(e)
            logger.exception("加载失败")
            return False


# ── 空闲自动卸载（默认关闭；设环境变量 ENHANCE_IDLE_UNLOAD=秒 开启）──
_IDLE_UNLOAD = float(os.environ.get("ENHANCE_IDLE_UNLOAD", "0"))
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
    global _model, _det, _sr_model
    with _lock:
        if _model is None and _det is None and _sr_model is None:
            return
        _model = None
        _det = None
    with _sr_lock:
        _sr_model = None
    try:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    logger.info("空闲卸载: GFPGAN / 超分 已释放，下次请求将自动重载")


def _idle_watch():
    while True:
        time.sleep(30)
        try:
            if _IDLE_UNLOAD <= 0:
                continue
            with _inflight_lock:
                busy = _inflight > 0
            if _model is not None and not busy and (time.time() - _last_used) > _IDLE_UNLOAD:
                _unload_models()
        except Exception:
            pass


@torch.no_grad()
def _restore_512(aligned_bgr: np.ndarray) -> np.ndarray:
    """512x512 对齐人脸 → GFPGAN 复原 → 512x512 BGR。"""
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
    t = (t - 0.5) / 0.5
    t = t.to(_device, dtype=_dtype)
    out = _model(t, return_rgb=False)[0]
    out = out.clamp(-1, 1).float()
    out = (out + 1) / 2.0
    out = out.squeeze(0).permute(1, 2, 0).cpu().numpy()
    out = (out * 255.0).round().astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_RGB2BGR)


def _feather_mask(h, w, inv_affine, blur=21):
    """512 全白方块经反仿射映回原图 → 腐蚀 + 高斯羽化的软掩码。"""
    m = np.ones((512, 512), np.float32)
    inv = cv2.warpAffine(m, inv_affine, (w, h))
    k = max(3, int(min(h, w) * 0.01)) | 1
    inv = cv2.erode(inv, np.ones((k, k), np.uint8))
    inv = cv2.GaussianBlur(inv, (blur | 1, blur | 1), 0)
    return inv[..., None]


def _enhance_frame(frame_bgr, affine, blend, inv_affine=None, mask=None):
    # detect_once 下 affine 恒定 → inv_affine 与羽化 mask 每帧相同,由调用方预算一次传入,免重复 warp/erode/blur。
    h, w = frame_bgr.shape[:2]
    aligned = cv2.warpAffine(frame_bgr, affine, (512, 512), flags=cv2.INTER_LINEAR)
    restored = _restore_512(aligned)
    if inv_affine is None:
        inv_affine = cv2.invertAffineTransform(affine)
    inv_restored = cv2.warpAffine(restored, inv_affine, (w, h), flags=cv2.INTER_LINEAR)
    if mask is None:
        mask = _feather_mask(h, w, inv_affine)
    if blend < 1.0:
        inv_restored = (blend * inv_restored.astype(np.float32)
                        + (1 - blend) * frame_bgr.astype(np.float32))
    out = mask * inv_restored.astype(np.float32) + (1 - mask) * frame_bgr.astype(np.float32)
    return out.round().astype(np.uint8)


def _affine_for(frame_bgr):
    """检测最大人脸 → 5 点 → 对齐仿射矩阵；无脸返回 None。"""
    faces = _det.get(frame_bgr)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    kps = np.asarray(f.kps, dtype=np.float32)
    affine, _ = cv2.estimateAffinePartial2D(kps, _FACE_TEMPLATE, method=cv2.LMEDS)
    return affine


# ════════════════════════════════════════════════════════════════════════
#  Real-ESRGAN 全帧超分（Phase 8-4）—— GFPGAN 只复原人脸，这里升整帧分辨率(背景/身体/服装)
#  自含推理：仅依赖 basicsr 的 RRDBNet/SRVGGNetCompact（facefusion env 已装），不引入 realesrgan 包；
#  若用户日后装了 realesrgan，则优先用其 RealESRGANer（带成熟分块/dni 去噪）。权重按需下载，缺失→优雅 503。
#
#  选型（深思后取舍）：SwinIR 画质最高但 transformer 逐帧太慢、不适合视频；x4plus(RRDB) 更锐但更重；
#  realesr-general-x4v3(SRVGGNetCompact) 轻量快 + 自带去噪强度(dni) → 视频后处理默认选它。
#  SUPERRES_MODEL=<.pth 绝对路径> 覆盖（按文件名自动判别架构）。
# ════════════════════════════════════════════════════════════════════════
_SR_MODELS_DIR = str(app_config.BASE / "models" / "realesrgan")
_SR_DEFAULT_NAME = os.environ.get("SUPERRES_MODEL_NAME", "realesr-general-x4v3.pth")
_SR_MODEL_PATH = os.environ.get("SUPERRES_MODEL", "").strip() or os.path.join(_SR_MODELS_DIR, _SR_DEFAULT_NAME)
_SR_URLS = {
    "realesr-general-x4v3.pth":
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
    "RealESRGAN_x4plus.pth":
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
    "RealESRGAN_x4plus_anime_6B.pth":
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
}
# 超分用 fp16（RRDB/SRVGG 卷积网在 fp16 数值安全，官方默认半精；GFPGAN 的 StyleGAN 解码器才需 fp32）
_SR_FP32 = os.environ.get("SUPERRES_FP32", "0") == "1"
_SR_TILE = int(os.environ.get("SUPERRES_TILE", "0"))        # >0 启用分块（低显存/超大图）；0=整图
_SR_TILE_PAD = int(os.environ.get("SUPERRES_TILE_PAD", "10"))
_sr_lock = threading.Lock()
_sr_model = None
_sr_native_scale = 4
_sr_dtype = torch.float32
_sr_name = ""
_sr_load_error = ""


def _sr_build_arch(model_name: str):
    """按权重文件名选 basicsr 架构 + 原生放大倍率。"""
    from basicsr.archs.rrdbnet_arch import RRDBNet
    n = model_name.lower()
    if "anime" in n and "6b" in n:
        return RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=4), 4
    if "general" in n and "x4v3" in n:
        from basicsr.archs.srvgg_arch import SRVGGNetCompact
        return SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=32,
                               upscale=4, act_type="prelu"), 4
    if "x2" in n:
        return RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=2), 2
    # 默认 RealESRGAN_x4plus
    return RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4), 4


def _sr_ensure_weights() -> bool:
    """权重不存在则按需下载（官方 release）；下载失败/无网 → False（端点优雅 503）。"""
    if os.path.isfile(_SR_MODEL_PATH):
        return True
    name = os.path.basename(_SR_MODEL_PATH)
    url = _SR_URLS.get(name)
    if not url:
        return False
    try:
        import urllib.request
        os.makedirs(os.path.dirname(_SR_MODEL_PATH), exist_ok=True)
        logger.info(f"下载超分权重 {name} …")
        tmp = _SR_MODEL_PATH + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, _SR_MODEL_PATH)
        logger.info(f"超分权重就绪: {_SR_MODEL_PATH}")
        return True
    except Exception as e:
        logger.warning(f"超分权重下载失败: {e}")
        return False


def _load_superres() -> bool:
    global _sr_model, _sr_native_scale, _sr_dtype, _sr_name, _sr_load_error
    with _sr_lock:
        if _sr_model is not None:
            return True
        if not _sr_ensure_weights():
            _sr_load_error = f"超分权重缺失且无法下载: {_SR_MODEL_PATH}"
            return False
        try:
            name = os.path.basename(_SR_MODEL_PATH)
            net, scale = _sr_build_arch(name)
            ckpt = torch.load(_SR_MODEL_PATH, map_location="cpu")
            sd = ckpt.get("params_ema", ckpt.get("params", ckpt))
            net.load_state_dict(sd, strict=True)
            net.eval().to(_device)
            _sr_dtype = torch.float32 if (_SR_FP32 or _device == "cpu") else torch.float16
            if _sr_dtype == torch.float16:
                net.half()
            _sr_model = net
            _sr_native_scale = scale
            _sr_name = name
            logger.info(f"Real-ESRGAN 就绪: {name} (x{scale}, {_device}/{_sr_dtype})")
            return True
        except Exception as e:
            _sr_load_error = f"{type(e).__name__}: {e}"
            logger.exception("超分模型加载失败")
            return False


@torch.no_grad()
def _sr_net_forward(t: "torch.Tensor") -> "torch.Tensor":
    """对已在设备上的 NCHW float 张量跑超分网络，支持可选分块（省显存）。"""
    if _SR_TILE <= 0:
        return _sr_model(t)
    # 分块推理：重叠 pad 防接缝，逐块前向后拼回
    b, c, h, w = t.shape
    s = _sr_native_scale
    out = t.new_zeros(b, c, h * s, w * s)
    tile, pad = _SR_TILE, _SR_TILE_PAD
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            ey, ex = min(y + tile, h), min(x + tile, w)
            iy0, ix0 = max(y - pad, 0), max(x - pad, 0)
            iy1, ix1 = min(ey + pad, h), min(ex + pad, w)
            part = _sr_model(t[:, :, iy0:iy1, ix0:ix1])
            # 裁掉 pad 区域后写回输出
            ty0, tx0 = (y - iy0) * s, (x - ix0) * s
            out[:, :, y * s:ey * s, x * s:ex * s] = part[:, :, ty0:ty0 + (ey - y) * s,
                                                         tx0:tx0 + (ex - x) * s]
    return out


@torch.no_grad()
def _superres_image(img_bgr: np.ndarray, outscale: float = 2.0,
                    denoise: float | None = None) -> np.ndarray:
    """整帧超分：BGR uint8 → 放大 outscale 倍的 BGR uint8。
    denoise 仅 general-x4v3 有意义（暂用后处理双边滤波近似 dni 去噪强度，0=不额外去噪）。"""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    t = torch.from_numpy(np.transpose(rgb, (2, 0, 1))).unsqueeze(0).to(_device)
    if _sr_dtype == torch.float16:
        t = t.half()
    out = _sr_net_forward(t)
    out = out.clamp(0, 1).squeeze(0).float().cpu().numpy()
    out = np.transpose(out, (1, 2, 0))
    out = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
    out = (out * 255.0).round().astype(np.uint8)
    # 网络原生 x{_sr_native_scale}，按需缩放到目标 outscale
    h0, w0 = img_bgr.shape[:2]
    tw, th = int(round(w0 * outscale)), int(round(h0 * outscale))
    if (out.shape[1], out.shape[0]) != (tw, th):
        interp = cv2.INTER_AREA if outscale < _sr_native_scale else cv2.INTER_LANCZOS4
        out = cv2.resize(out, (tw, th), interpolation=interp)
    if denoise and denoise > 0:
        out = cv2.bilateralFilter(out, d=5, sigmaColor=int(40 * denoise), sigmaSpace=5)
    return out


@app.get("/health")
def health():
    return {"ok": True, "service": "enhance", "loaded": _model is not None,
            "device": _device, "error": _load_error,
            "superres_loaded": _sr_model is not None,
            "superres_model": _sr_name or os.path.basename(_SR_MODEL_PATH),
            "superres_available": _sr_model is not None or os.path.isfile(_SR_MODEL_PATH)
                                   or os.path.basename(_SR_MODEL_PATH) in _SR_URLS,
            "superres_error": _sr_load_error}


@app.get("/meminfo")
def meminfo():
    info = {"service": "enhance"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
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
        if torch.cuda.is_available():
            before = torch.cuda.memory_reserved()
    except Exception:
        before = None
    n = gc.collect()
    freed_mb = None
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - torch.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}


@app.post("/enhance_video")
async def enhance_video(video: UploadFile = File(...),
                        blend: float = Form(1.0),
                        detect_once: int = Form(1)):
    if _model is None and not _load():
        raise HTTPException(503, f"模型未就绪: {_load_error}")
    _touch()
    raw = await video.read()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw); tin = f.name
    tout = tin.replace(".mp4", "_enh.mp4")
    _infer_enter()
    try:
        cap = cv2.VideoCapture(tin)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frames = []
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            frames.append(fr)
        cap.release()
        if not frames:
            raise HTTPException(400, "视频无帧")

        t0 = time.time()
        affine = None
        inv_affine = None; mask = None   # detect_once 下预算一次复用
        n_enh = 0
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(tout, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for idx, fr in enumerate(frames):
            if affine is None or not detect_once:
                a = _affine_for(fr)
                if a is not None:
                    affine = a
                    if detect_once:   # 仿射恒定 → 反仿射与羽化 mask 只算一次
                        inv_affine = cv2.invertAffineTransform(affine)
                        mask = _feather_mask(h, w, inv_affine)
            if affine is not None:
                try:
                    fr = _enhance_frame(fr, affine, blend, inv_affine, mask); n_enh += 1
                except Exception as e:
                    logger.warning(f"帧{idx}增强失败,原样输出: {e}")
            writer.write(fr)
        writer.release()
        dt = time.time() - t0
        logger.info(f"enhance {n_enh}/{len(frames)} 帧 in {dt:.1f}s ({dt/max(1,len(frames))*1000:.0f}ms/帧)")
        with open(tout, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="video/mp4",
                        headers={"X-Enhanced-Frames": str(n_enh),
                                 "X-Processing-Time": f"{dt:.2f}s"})
    finally:
        for p in (tin, tout):
            try: os.unlink(p)
            except Exception: pass
        # 整段视频帧解码进内存(frames)，用完释放并清显存缓存
        try:
            frames = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        _infer_leave()


@app.post("/superres_image")
async def superres_image(image: UploadFile = File(...),
                         scale: float = Form(2.0),
                         denoise: float = Form(0.0)):
    """整帧超分单图（Real-ESRGAN）。multipart image → 放大 scale 倍的 PNG。缺权重→503。"""
    if _sr_model is None and not _load_superres():
        raise HTTPException(503, f"超分模型未就绪: {_sr_load_error}")
    raw = await image.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "无法解码图片")
    _infer_enter()
    try:
        t0 = time.time()
        out = _superres_image(img, outscale=float(scale), denoise=float(denoise))
        ok, buf = cv2.imencode(".png", out)
        if not ok:
            raise HTTPException(500, "编码失败")
        return Response(content=buf.tobytes(), media_type="image/png",
                        headers={"X-Processing-Time": f"{time.time()-t0:.2f}s",
                                 "X-Output-Size": f"{out.shape[1]}x{out.shape[0]}"})
    finally:
        try:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        _infer_leave()


@app.post("/superres_video")
async def superres_video(video: UploadFile = File(...),
                         scale: float = Form(2.0),
                         denoise: float = Form(0.0),
                         fps_override: float = Form(0.0)):
    """整帧超分视频（Real-ESRGAN）—— 离线 HD 后处理：升整帧分辨率(背景/身体/服装一并变清晰)。
    非实时；逐帧推理。缺权重→503。返回放大后的无声 mp4。"""
    if _sr_model is None and not _load_superres():
        raise HTTPException(503, f"超分模型未就绪: {_sr_load_error}")
    raw = await video.read()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw); tin = f.name
    tout = tin.replace(".mp4", "_sr.mp4")
    _infer_enter()
    frames = None
    try:
        cap = cv2.VideoCapture(tin)
        fps = float(fps_override) if fps_override and fps_override > 0 else (cap.get(cv2.CAP_PROP_FPS) or 25)
        frames = []
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            frames.append(fr)
        cap.release()
        if not frames:
            raise HTTPException(400, "视频无帧")
        t0 = time.time()
        out0 = _superres_image(frames[0], outscale=float(scale), denoise=float(denoise))
        oh, ow = out0.shape[:2]
        writer = cv2.VideoWriter(tout, cv2.VideoWriter_fourcc(*"mp4v"), fps, (ow, oh))
        writer.write(out0)
        for fr in frames[1:]:
            writer.write(_superres_image(fr, outscale=float(scale), denoise=float(denoise)))
        writer.release()
        dt = time.time() - t0
        logger.info(f"superres {len(frames)} 帧 → {ow}x{oh} in {dt:.1f}s "
                    f"({dt/max(1,len(frames))*1000:.0f}ms/帧)")
        with open(tout, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="video/mp4",
                        headers={"X-SR-Frames": str(len(frames)),
                                 "X-Output-Size": f"{ow}x{oh}",
                                 "X-Processing-Time": f"{dt:.2f}s"})
    finally:
        for p in (tin, tout):
            try: os.unlink(p)
            except Exception: pass
        try:
            frames = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        _infer_leave()


@app.on_event("startup")
async def _startup():
    threading.Thread(target=_load, daemon=True).start()
    if _IDLE_UNLOAD > 0:
        logger.info(f"空闲自动卸载已启用: {_IDLE_UNLOAD:.0f}s")
        threading.Thread(target=_idle_watch, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
