"""同进程人脸增强(GFPGAN bf16) —— 供 lipsync_server 流式逐帧调用。

与跨进程 enhance_server(8092) 等价的复原逻辑,但在 lipsync 同一 Python/CUDA 进程内运行:
- 免 mp4 编解码×4、免 HTTP 往返、**免两进程争用单卡的 GPU 上下文切换**(这是实时 HD 的真瓶颈);
- 在 MuseTalk GPU 工作线程内逐帧串行: 生成 20ms + 增强(bf16) ~9ms GPU → ~32ms/帧 < 40ms,实时 25fps 可达。

依赖: 仅 vendored gfpgan_clean(纯 PyTorch) + insightface(musethepeak 已装) + GFPGANv1.4 权重(在盘)。
不引入 gfpgan/basicsr 包,零环境风险。
"""
import os
import numpy as np
import cv2
import torch
import torch.nn.functional as F

import app_config
GFPGAN_MODEL = os.environ.get("GFPGAN_MODEL", str(app_config.BASE / "GFPGANv1.4.pth"))
_DTYPE = torch.float32 if os.environ.get("ENHANCE_DTYPE", "bf16") == "fp32" else torch.bfloat16
_device = "cuda" if torch.cuda.is_available() else "cpu"
# GPU 端 warp/blend(grid_sample)：把对齐/反贴/羽化混合全留在 GPU,免 CPU warpAffine/blend(~18ms)。
_GPU_WARP = os.environ.get("FACE_ENH_GPU", "1") == "1" and _device == "cuda"
# 固定增强 batch：GFPGAN/grid_sample 对【每个新输入形状】都会触发 cuDNN autotune(首次几百~2000ms)。
# 流式末批是变长余数(1~7帧)→每次新帧数都重调=HD「假卡顿」真因。把每块 pad 到固定 _ENH_BATCH 再切回，
# 使形状恒定→只 autotune 一次→HD 稳定 ~1.3×实时(实测 30ms/帧)。设值须 ≥ 常见 UNet batch_size(默认8)。
_ENH_BATCH = int(os.environ.get("FACE_ENH_BATCH", "8"))

# FFHQ 512 标准 5 点(左眼/右眼/鼻/左嘴角/右嘴角),与 insightface kps 一致
_FACE_TEMPLATE = np.array([
    [192.98138, 239.94708], [318.90277, 240.1936],
    [256.63416, 314.01935], [201.26117, 371.41043], [313.08905, 371.15118],
], dtype=np.float32)

_model = None
_det = None


def ensure_loaded():
    global _model, _det
    if _model is not None and _det is not None:
        return True
    # 关 cuDNN autotune：MuseTalk 的 face_alignment/face_detection 会把 benchmark 置 True，
    # 导致 GFPGAN 每遇「新输入形状/新帧数」首次前向触发算法搜索(实测首次 100 帧块停顿 ~20s)。
    # 固定 batch 已让形状恒定,这里再关 autotune 双保险→冷启不再卡(稳态 ~30ms/帧=1.3×实时不受影响)。
    try:
        torch.backends.cudnn.benchmark = False
    except Exception:
        pass
    if _model is None:
        from gfpgan_clean import GFPGANv1Clean
        m = GFPGANv1Clean(out_size=512, num_style_feat=512, channel_multiplier=2,
                          decoder_load_path=None, fix_decoder=False, num_mlp=8,
                          input_is_latent=True, different_w=True, narrow=1, sft_half=True)
        ckpt = torch.load(GFPGAN_MODEL, map_location="cpu")
        sd = ckpt.get("params_ema", ckpt.get("params", ckpt))
        m.load_state_dict(sd, strict=False)
        m.eval().to(_device)
        if _DTYPE != torch.float32:
            m.to(dtype=_DTYPE)
        _model = m
        # 加载即预热固定 batch 形状的 GFPGAN forward(主开销),把 cuDNN autotune 在启动时一次性付清，
        # 避免首句现场 autotune 巨延迟。grid_sample 依赖人脸尺寸,首句再 autotune 一次(便宜)。
        try:
            with torch.no_grad():
                _dummy = torch.zeros(_ENH_BATCH, 3, 512, 512, device=_device, dtype=_DTYPE)
                _model(_dummy, return_rgb=False)
            if _device == "cuda":
                torch.cuda.synchronize()
        except Exception:
            pass
    if _det is None:
        from insightface.app import FaceAnalysis
        det = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                           providers=["CPUExecutionProvider"])
        det.prepare(ctx_id=-1, det_size=(640, 640))
        _det = det
    return True


@torch.no_grad()
def _restore_512(aligned_bgr):
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
    t = (t - 0.5) / 0.5
    t = t.to(_device, dtype=_DTYPE)
    out = _model(t, return_rgb=False)[0]
    out = out.clamp(-1, 1).float()
    out = (out + 1) / 2.0
    out = out.squeeze(0).permute(1, 2, 0).cpu().numpy()
    out = (out * 255.0).round().astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_RGB2BGR)


def _feather_mask(h, w, inv_affine, blur=21):
    m = np.ones((512, 512), np.float32)
    inv = cv2.warpAffine(m, inv_affine, (w, h))
    k = max(3, int(min(h, w) * 0.01)) | 1
    inv = cv2.erode(inv, np.ones((k, k), np.uint8))
    inv = cv2.GaussianBlur(inv, (blur | 1, blur | 1), 0)
    return inv[..., None]


def _cv_affine_to_theta(M, in_h, in_w, out_h, out_w):
    """把 cv2 仿射(in→out 像素)转成 grid_sample 的 theta(out_norm→in_norm),
    使 grid_sample(in, affine_grid(theta,out)) ≈ cv2.warpAffine(in, M, (out_w,out_h))(align_corners=False)。"""
    M_inv = cv2.invertAffineTransform(M)                  # out_pix → in_pix
    A = np.vstack([M_inv, [0, 0, 1]]).astype(np.float64)  # 3x3
    # out_norm → out_pix
    N_out_inv = np.array([[out_w/2.0, 0, (out_w-1)/2.0],
                          [0, out_h/2.0, (out_h-1)/2.0],
                          [0, 0, 1]], dtype=np.float64)
    # in_pix → in_norm
    N_in = np.array([[2.0/in_w, 0, 1.0/in_w - 1],
                     [0, 2.0/in_h, 1.0/in_h - 1],
                     [0, 0, 1]], dtype=np.float64)
    theta = (N_in @ A @ N_out_inv)[:2, :]
    return torch.from_numpy(theta).float().unsqueeze(0)   # (1,2,3)


def prepare(frame_bgr):
    """检测人脸 → 仿射 + 反仿射 + 羽化 mask(逐帧位置固定时只需算一次)。无脸返回 None。"""
    ensure_loaded()
    faces = _det.get(frame_bgr)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    kps = np.asarray(f.kps, dtype=np.float32)
    affine, _ = cv2.estimateAffinePartial2D(kps, _FACE_TEMPLATE, method=cv2.LMEDS)
    if affine is None:
        return None
    h, w = frame_bgr.shape[:2]
    inv_affine = cv2.invertAffineTransform(affine)
    mask = _feather_mask(h, w, inv_affine)
    prep = {"affine": affine, "inv_affine": inv_affine, "mask": mask}
    if _GPU_WARP:
        try:
            prep["theta_fwd"] = _cv_affine_to_theta(affine, h, w, 512, 512).to(_device)
            prep["theta_inv"] = _cv_affine_to_theta(inv_affine, 512, 512, h, w).to(_device)
            prep["mask_gpu"] = torch.from_numpy(mask).permute(2, 0, 1).unsqueeze(0).to(_device)  # (1,1,h,w)
            prep["hw"] = (h, w)
        except Exception:
            pass
    return prep


@torch.no_grad()
def _enhance_gpu_block(frames_bgr, prep, blend):
    """对【≤_ENH_BATCH 帧】一次 GPU 复原。内部把帧数 pad 到固定 _ENH_BATCH 再切回真实帧，
    使 GFPGAN/grid_sample 输入形状恒定 → cuDNN 只 autotune 一次(消除变长末批反复重调=HD 假卡顿真因)。"""
    h, w = prep["hw"]
    n = len(frames_bgr)
    B = _ENH_BATCH
    block = frames_bgr if n == B else (frames_bgr + [frames_bgr[-1]] * (B - n))  # 尾块用末帧补齐到 B
    arr = np.stack(block, 0)                                                 # (B,h,w,3) uint8 BGR
    t = torch.from_numpy(np.ascontiguousarray(arr)).to(_device)
    src = t.flip(3).permute(0, 3, 1, 2).float() / 255.0                      # (B,3,h,w) RGB [0,1]
    theta_f = prep["theta_fwd"].expand(B, -1, -1)
    grid_f = F.affine_grid(theta_f, (B, 3, 512, 512), align_corners=False)
    aligned = F.grid_sample(src, grid_f, mode="bilinear", padding_mode="reflection", align_corners=False)
    inp = ((aligned - 0.5) / 0.5).to(dtype=_DTYPE)
    out = _model(inp, return_rgb=False)[0]                                   # (B,3,512,512)
    restored = (out.clamp(-1, 1).float() + 1) / 2.0
    theta_i = prep["theta_inv"].expand(B, -1, -1)
    grid_i = F.affine_grid(theta_i, (B, 3, h, w), align_corners=False)
    inv_restored = F.grid_sample(restored, grid_i, mode="bilinear", padding_mode="zeros", align_corners=False)
    m = prep["mask_gpu"]                                                     # (1,1,h,w) 广播到 B
    res = m * (blend * inv_restored + (1 - blend) * src) + (1 - m) * src
    res = (res.clamp(0, 1) * 255.0).round().byte().permute(0, 2, 3, 1).flip(3).contiguous().cpu().numpy()
    return [res[i] for i in range(n)]                                        # 只取真实帧,丢弃 pad


@torch.no_grad()
def _enhance_gpu_batch(frames_bgr, prep, blend):
    """整批帧 GPU 复原(共享 detect-once 仿射/mask)。按固定 _ENH_BATCH 分块(形状恒定)，
    把逐帧 .cpu() 同步 + kernel 启动开销摊薄到整块 → 显著降每帧开销。返回 list[np.uint8 BGR]。"""
    B = _ENH_BATCH
    if len(frames_bgr) <= B:
        return _enhance_gpu_block(frames_bgr, prep, blend)
    out = []
    for i in range(0, len(frames_bgr), B):
        out.extend(_enhance_gpu_block(frames_bgr[i:i + B], prep, blend))
    return out


def _enhance_gpu(frame_bgr, prep, blend):
    return _enhance_gpu_batch([frame_bgr], prep, blend)[0]


def enhance_batch(frames_bgr, prep, blend=0.85):
    """整批增强。GPU 路径优先(共享仿射广播),失败逐帧回退。"""
    if not frames_bgr:
        return frames_bgr
    if _device == "cuda" and torch.backends.cudnn.benchmark:   # 防 DWPose/face_align 把它翻回 True→autotune 复发
        torch.backends.cudnn.benchmark = False
    if _GPU_WARP and "theta_fwd" in prep:
        try:
            return _enhance_gpu_batch(frames_bgr, prep, blend)
        except Exception:
            pass
    return [enhance(f, prep, blend) for f in frames_bgr]


def enhance(frame_bgr, prep, blend=0.85):
    """用预备好的仿射/mask 对单帧做 GFPGAN 复原 + 羽化贴回。GPU 路径优先,失败回退 CPU。"""
    if _GPU_WARP and "theta_fwd" in prep:
        try:
            return _enhance_gpu(frame_bgr, prep, blend)
        except Exception:
            pass
    affine = prep["affine"]; inv_affine = prep["inv_affine"]; mask = prep["mask"]
    h, w = frame_bgr.shape[:2]
    aligned = cv2.warpAffine(frame_bgr, affine, (512, 512), flags=cv2.INTER_LINEAR)
    restored = _restore_512(aligned)
    inv_restored = cv2.warpAffine(restored, inv_affine, (w, h), flags=cv2.INTER_LINEAR)
    if blend < 1.0:
        inv_restored = (blend * inv_restored.astype(np.float32)
                        + (1 - blend) * frame_bgr.astype(np.float32))
    out = mask * inv_restored.astype(np.float32) + (1 - mask) * frame_bgr.astype(np.float32)
    return out.round().astype(np.uint8)
