# -*- coding: utf-8 -*-
"""
发型迁移 API - 基于 HairFastGAN
端口: 8001
用法: POST /hair_transfer  {source_image: base64, hair_image: base64}
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import base64, os, time, cv2, numpy as np
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import app_config
HAIRFASTGAN_DIR = app_config.BASE / "HairFastGAN"
MODELS_DIR      = HAIRFASTGAN_DIR / "pretrained_models"

# 将 HairFastGAN 加入路径
if str(HAIRFASTGAN_DIR) not in sys.path:
    sys.path.insert(0, str(HAIRFASTGAN_DIR))

app = FastAPI(title="Hair Transfer API")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="hair")                # 替代原 CORS:* 无鉴权

# ── 全局发型模型 ────────────────────────────────────────────────
hair_fast = None
_load_tried = False

def load_hair_model():
    """加载 HairFastGAN（常驻 ~6G 显存）。
    2026-07-08 阶段7 改为**懒加载**：直播中 8001 常开会白占 6G 把共卡直播链
    挤穿（实测加载后 free 0G、连发型闸自己都过不了）。启动秒起、只在第一次
    /hair_transfer 时加载——加载前还有显存闸挡门，直播中拒单、空闲时才驻留。"""
    global hair_fast, _load_tried
    if hair_fast is not None or _load_tried:
        return
    _load_tried = True     # 失败也只试一次，避免每请求重复长等
    try:
        import os
        os.chdir(str(HAIRFASTGAN_DIR))
        if str(HAIRFASTGAN_DIR) not in sys.path:
            sys.path.insert(0, str(HAIRFASTGAN_DIR))
        print("[HairAPI] 首次请求，正在加载 HairFastGAN（~1.5 分钟）...")
        from hair_swap import HairFast, get_parser
        parser = get_parser()
        opts = parser.parse_args([
            '--device', 'cuda',
            '--ckpt',                str(MODELS_DIR / 'StyleGAN' / 'ffhq.pt'),
            '--rotate_checkpoint',   str(MODELS_DIR / 'Rotate'   / 'rotate_best.pth'),
            '--blending_checkpoint', str(MODELS_DIR / 'Blending' / 'checkpoint.pth'),
            '--pp_checkpoint',       str(MODELS_DIR / 'PostProcess' / 'pp_model.pth'),
        ])
        hair_fast = HairFast(opts)
        print("[HairAPI] HairFastGAN 模型加载完成")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[HairAPI] 模型加载失败: {e}")
        hair_fast = None

# ── 发型资料库 ───────────────────────────────────────────────
HAIR_DIR = app_config.BASE / "hair_styles"
HAIR_DIR.mkdir(exist_ok=True)

def scan_hair_styles():
    styles = {}
    for ext in ('*.jpg','*.jpeg','*.png','*.webp'):
        for f in HAIR_DIR.glob(ext):
            with open(f, 'rb') as fp:
                styles[f.stem] = base64.b64encode(fp.read()).decode()
    return styles

_hair_styles: dict = {}
_active_hair: str  = ""

def reload_hair():
    global _hair_styles, _active_hair
    _hair_styles = scan_hair_styles()
    if _hair_styles and _active_hair not in _hair_styles:
        _active_hair = list(_hair_styles.keys())[0]
    print(f"[HairAPI] 发型库: {list(_hair_styles.keys())}")

reload_hair()

# ── 工具函数 ───────────────────────────────────────────────────
def b64_to_img(b64: str) -> np.ndarray:
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    arr = np.frombuffer(base64.b64decode(b64), np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def img_to_b64(img: np.ndarray, quality=90) -> str:
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode()

def np_to_pil(img: np.ndarray):
    from PIL import Image
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

def pil_to_np(pil_img) -> np.ndarray:
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

# ── 发型回贴（paste_back，2026-07-10）───────────────────────────────────────────
#   痛点：HairFastGAN 输出是 FFHQ 对齐后的 1024 头肩裁剪(经 StyleGAN 重建)，背景/身体
#   与原图不同、且丢失原始分辨率与场景。用作「换脸+发型」出片时，希望**只把新发型的
#   头发像素**贴回原始换脸图，其余(脸/身体/背景)保持换脸真实像素不动。
#   做法(基于关键点仿射，避开 FFHQ quad/pad 反算，鲁棒且自包含)：
#     1) BiSeNet 在对齐空间取头发∪帽子 mask（新发型结果 ∪ 原图发型 → 短发也能盖住长发）；
#     2) dlib-68 关键点在「对齐脸」与「原始脸」上各测一次 → 估相似仿射(对齐空间→原始空间)；
#     3) 把结果图与 mask 仿射回原始尺寸，羽化后仅在头发区域合成。
#   任一步失败 → 返回 None，调用方回退到对齐裁剪结果（软降级，绝不反噬主链）。
_dlib_detector = None
_dlib_predictor = None


def _ensure_dlib():
    """dlib 68 关键点预测器（懒加载）。中文绝对路径经 UTF-8→ANSI(CreateFileA) 会 mojibake
    打不开（实测 RuntimeError），故沿用 HairFastGAN align_face 的做法：chdir 到 HFG 后用**相对
    ASCII 路径**打开（相对路径与 SetCurrentDirectoryW 的宽字符 CWD 组合 → 正确解析）。"""
    global _dlib_detector, _dlib_predictor
    if _dlib_predictor is None:
        import dlib
        rel = os.path.join("pretrained_models", "ShapeAdaptor",
                           "shape_predictor_68_face_landmarks.dat")
        cwd = os.getcwd()
        try:
            os.chdir(str(HAIRFASTGAN_DIR))
            _dlib_detector = dlib.get_frontal_face_detector()
            _dlib_predictor = dlib.shape_predictor(rel)
        finally:
            os.chdir(cwd)
    return _dlib_detector, _dlib_predictor


def _lm68(rgb_uint8: np.ndarray):
    """在 RGB uint8 图上测最大人脸的 68 关键点；无脸→None。
    全身照（试衣底片当目标图）里脸偏小，HOG 一次上采样可能漏检 → 图不大时再升采样重试一次
    （upsample=2 面积 ×4，仅限 ≤1600px 图，避免大图秒级卡顿；离线出片路径可接受）。"""
    det, pred = _ensure_dlib()
    dets = det(rgb_uint8, 1)
    if not dets and max(rgb_uint8.shape[:2]) <= 1600:
        dets = det(rgb_uint8, 2)
    if not dets:
        return None
    d = max(dets, key=lambda r: r.width() * r.height())
    sh = pred(rgb_uint8, d)
    return np.array([[p.x, p.y] for p in sh.parts()], dtype=np.float32)


def _hair_mask_1024(t_1024) -> np.ndarray:
    """[1或3,1024,1024] float[0,1] cuda 张量 → 1024² 头发∪帽子 mask（numpy float[0,1]）。"""
    import torch
    import torch.nn.functional as _F
    import torchvision.transforms as _T
    from models.Net import get_segmentation
    if t_1024.dim() == 3:
        t_1024 = t_1024.unsqueeze(0)
    norm = _T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    im512 = _F.interpolate(t_1024, size=(512, 512), mode="bilinear", align_corners=False)
    seg = get_segmentation(norm(im512.squeeze(0)).unsqueeze(0))     # [1,1,256,256] long, hair=13/hat=14
    m = ((seg == 13) | (seg == 14)).float()
    m = _F.interpolate(m, size=(1024, 1024), mode="bilinear", align_corners=False)
    return m.squeeze().detach().cpu().numpy()


def _paste_hair_back(src_bgr: np.ndarray, result_t, face_aligned_t):
    """把 result_t(1024 对齐·含新发型) 的头发区域仿射贴回 src_bgr(原始换脸图)。
    src_bgr: 原始分辨率 BGR；result_t/face_aligned_t: [3,1024,1024] 张量。失败→None。"""
    try:
        import torch
        H, W = src_bgr.shape[:2]
        dev = result_t.device
        fa = face_aligned_t.to(dev)
        # 1) 头发区域：分别取新发型 & 原图发型（对齐空间 1024）
        m_new = _hair_mask_1024(result_t)
        m_old = _hair_mask_1024(fa)
        if float(m_new.sum()) + float(m_old.sum()) < 100:
            return None
        # 2) 对齐脸 & 原始脸 各测 68 点 → 相似仿射（对齐空间 → 原始空间）
        fa_rgb = (fa.clamp(0, 1) * 255).byte().permute(1, 2, 0).cpu().numpy()
        lm_al = _lm68(np.ascontiguousarray(fa_rgb))
        lm_or = _lm68(cv2.cvtColor(src_bgr, cv2.COLOR_BGR2RGB))
        if lm_al is None or lm_or is None:
            return None
        M, _inl = cv2.estimateAffinePartial2D(lm_al, lm_or, method=cv2.RANSAC)
        if M is None:
            return None
        # 3) 结果图与两张发型 mask 一起仿射回原始尺寸
        res_rgb = (result_t.clamp(0, 1) * 255).byte().permute(1, 2, 0).cpu().numpy()
        res_bgr = cv2.cvtColor(res_rgb, cv2.COLOR_RGB2BGR)
        warp_res = cv2.warpAffine(res_bgr, M, (W, H), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REFLECT)
        warp_new = np.clip(cv2.warpAffine(m_new, M, (W, H), flags=cv2.INTER_LINEAR), 0, 1)
        warp_old = np.clip(cv2.warpAffine(m_old, M, (W, H), flags=cv2.INTER_LINEAR), 0, 1)
        k = max(3, (int(0.012 * max(H, W)) // 2) * 2 + 1)      # 边缘按图幅羽化
        out = src_bgr.astype(np.float32)
        # A) 换短发场景：原发型比新发型大的"残留头发"区 → 用**真实邻域** inpaint 抹掉
        #    （比贴 GAN 重建背景更贴合任意真实场景；失败则回退并集，让 GAN 结果盖住残留）。
        leftover = np.clip(warp_old - warp_new, 0, 1)
        try:
            lb = (leftover > 0.5).astype(np.uint8)
            if int(lb.sum()) > 50:
                dk = max(3, k // 2)
                lb_d = cv2.dilate(lb, np.ones((dk, dk), np.uint8))
                inpainted = cv2.inpaint(src_bgr, lb_d, 3, cv2.INPAINT_TELEA)
                al = cv2.GaussianBlur(leftover.astype(np.float32), (k, k), 0)[..., None]
                out = out * (1 - al) + inpainted.astype(np.float32) * al
        except Exception:
            warp_new = np.clip(warp_new + leftover, 0, 1)      # 回退到旧的并集行为
        # B) 新发型区：GAN 结果羽化贴上（脸/身体/背景保持原始换脸像素不动）
        alpha = cv2.GaussianBlur(np.clip(warp_new, 0, 1).astype(np.float32), (k, k), 0)[..., None]
        out = out * (1 - alpha) + warp_res.astype(np.float32) * alpha
        return np.clip(out, 0, 255).astype(np.uint8)
    except Exception:
        import traceback
        traceback.print_exc()
        return None

# ── 请求模型 ───────────────────────────────────────────────────
class HairRequest(BaseModel):
    source_image: str          # 待换发的人物图（base64）
    hair_image:   str = ""     # 发型参考图（为空则用当前激活发型 / hair_from_source=True 时用源图自身）
    hair_from_source: bool = False   # True: 发型参考=源图自身（源照片的脸+发一起带上，无需另传发型图）
    paste_back:  bool = False   # True: 只把头发像素贴回原始换脸图（保脸/保身/保背景），返回原分辨率合成图

class HairResponse(BaseModel):
    result_image: str
    elapsed_ms:   int
    pasted_back:  bool = False   # 是否成功走了整图回贴（否=返回对齐裁剪结果）

# ── API 路由 ───────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "hair-api", "status": "running",
            "model_loaded": hair_fast is not None,
            "ui": "http://127.0.0.1:8001/ui"}

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": hair_fast is not None}

@app.get("/meminfo")
def meminfo():
    info = {"service": "hair"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        import torch as _t
        if _t.cuda.is_available():
            info["gpu_alloc_mb"] = round(_t.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(_t.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info

def _do_unload() -> dict:
    """卸载 HairFastGAN 释放 ~6G 显存；重置懒加载标记（下次请求重新过闸加载）。"""
    global hair_fast, _load_tried
    was = hair_fast is not None
    hair_fast = None
    _load_tried = False
    out = {"ok": True, "was_loaded": was}
    try:
        import gc as _gc, torch as _t
        _gc.collect()
        if _t.cuda.is_available():
            _t.cuda.empty_cache(); _t.cuda.ipc_collect()
            out["free_gb"] = round(_t.cuda.mem_get_info()[0] / 1024**3, 1)
    except Exception:
        pass
    return out


@app.post("/unload")
def unload_endpoint():
    """手动卸载入口（阶段7）。阶段8 起还有空闲自动归还线程，通常无需手动。"""
    return _do_unload()


# ── 阶段8：显存自动归还 ────────────────────────────────────────────────
# 策略：模型驻留 且 空闲超 HAIR_IDLE_UNLOAD_MIN(默认15min) 且 显卡空闲 <
# HAIR_KEEP_FREE_GB(默认10G) → 自动卸载。空闲机显存充裕就一直驻留（重载要
# ~25s，白卸载徒增等待）；直播共卡挤压时才"用完即还"。设 0 停用。
_last_used = time.time()


def _should_unload(loaded: bool, idle_minutes: float, free_gb: float,
                   idle_min: float, keep_free: float) -> bool:
    """自动归还判定（纯函数，阶段9 抽出便于无 GPU 单测）：
    驻留中 且 空闲够久 且 显卡被挤压（空闲机显存充裕就一直驻留，重载 ~25s 不白付）。"""
    return loaded and idle_min > 0 and idle_minutes >= idle_min and free_gb < keep_free


def _idle_unload_loop():
    idle_min = float(os.environ.get("HAIR_IDLE_UNLOAD_MIN", "15"))
    keep_free = float(os.environ.get("HAIR_KEEP_FREE_GB", "10"))
    if idle_min <= 0:
        print("[HairAPI] 空闲自动归还已停用(HAIR_IDLE_UNLOAD_MIN=0)")
        return
    import vram_gate
    while True:
        time.sleep(60)
        try:
            idle = (time.time() - _last_used) / 60
            free = vram_gate.free_gb()
            if _should_unload(hair_fast is not None, idle, free, idle_min, keep_free):
                r = _do_unload()
                print(f"[HairAPI] 空闲 {idle:.0f}min 且 free {free:.1f}G<{keep_free}G "
                      f"→ 自动归还显存 (free→{r.get('free_gb', '?')}G)")
        except Exception as e:
            print(f"[HairAPI] 自动归还线程异常(继续): {e}")


import threading as _threading
_threading.Thread(target=_idle_unload_loop, daemon=True).start()


@app.post("/gc")
def gc_endpoint():
    """非侵入式回收：gc + 释放显存缓存，不卸载模型。供看门狗优先调用以避免重启打断业务。"""
    import gc as _gc
    before = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            before = _t.cuda.memory_reserved()
    except Exception:
        before = None
    n = _gc.collect()
    freed_mb = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            _t.cuda.empty_cache()
            _t.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - _t.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}

@app.get("/hair_styles")
def list_styles():
    reload_hair()
    return {"styles": list(_hair_styles.keys()), "active": _active_hair}

@app.post("/hair_styles/switch")
def switch_style(data: dict):
    global _active_hair
    name = data.get("name", "")
    reload_hair()
    if name not in _hair_styles:
        raise HTTPException(status_code=404, detail=f"找不到发型: {name}")
    _active_hair = name
    return {"ok": True, "active": _active_hair}


@app.post("/hair_styles/activate")
def activate_style(data: dict):
    """Hub 角色激活用的别名端点（与 /hair_styles/switch 同义）。"""
    return switch_style(data)

@app.post("/hair_styles/upload")
async def upload_style(data: dict):
    name  = data.get("name","").strip()
    img64 = data.get("image","")
    if not name or not img64:
        raise HTTPException(status_code=400, detail="需要 name 和 image")
    if "," in img64:
        img64 = img64.split(",",1)[1]
    save_path = HAIR_DIR / f"{name}.jpg"
    with open(save_path, 'wb') as f:
        f.write(base64.b64decode(img64))
    reload_hair()
    return {"ok": True, "styles": list(_hair_styles.keys())}

@app.get("/hair_thumb")
def hair_thumb(name: str):
    from fastapi.responses import Response
    path = HAIR_DIR / f"{name}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    with open(str(path), 'rb') as f:
        raw = f.read()
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (80, 80))
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(content=buf.tobytes(), media_type="image/jpeg")

@app.post("/hair_transfer", response_model=HairResponse)
def hair_transfer(req: HairRequest):
    # 显存准入闸在加载前挡门：模型常驻 ~6G + 推理峰值 ~3G，懒加载场景首载
    # 需要 ~10G 空闲；已驻留后单次推理 4G 即可。低于阈值时 WDDM 会静默回落
    # 共享内存假忙碌（同 tryon 2026-07-08 事故），快失败优于磨盘。
    import vram_gate
    need = float(os.environ.get("HAIR_MIN_FREE_GB", "4" if hair_fast is not None else "10"))
    vram_gate.gate(need, service="hair")
    load_hair_model()          # 懒加载：首次请求才占显存（直播中被上面闸挡住）
    if hair_fast is None:
        raise HTTPException(status_code=503, detail="HairFastGAN 模型未加载/加载失败")
    global _last_used
    _last_used = time.time()   # 空闲自动归还的计时锚点
    t0 = time.time()

    # 确定发型参考图：hair_from_source=用源图自身(源照片的发型跟随) > 显式 hair_image > 当前激活发型
    if req.hair_from_source:
        hair_b64 = req.source_image
    else:
        hair_b64 = req.hair_image if req.hair_image else _hair_styles.get(_active_hair, "")
    if not hair_b64:
        raise HTTPException(status_code=400, detail="没有发型参考图，请先上传")

    src_img  = b64_to_img(req.source_image)
    hair_img = b64_to_img(hair_b64)
    if src_img is None or hair_img is None:
        raise HTTPException(status_code=400, detail="图片解码失败")

    pasted_back = False
    try:
        src_pil  = np_to_pil(src_img)
        hair_pil = np_to_pil(hair_img)
        # align=True: 自动裁剪对齐人脸，返回 (result, aligned_face, aligned_shape, aligned_color)
        out = hair_fast.swap(src_pil, hair_pil, hair_pil, align=True)
        result_tensor = out[0] if isinstance(out, tuple) else out
        face_aligned  = out[1] if (isinstance(out, tuple) and len(out) > 1) else None
        # 转为 PIL 再转 numpy（对齐 1024 裁剪结果——默认返回；paste_back 成功则改用整图合成）
        from torchvision.transforms.functional import to_pil_image
        result_pil = to_pil_image(result_tensor.clamp(0, 1).cpu())
        result = pil_to_np(result_pil)
        if req.paste_back and face_aligned is not None:
            pasted = _paste_hair_back(src_img, result_tensor, face_aligned)
            if pasted is not None:
                result = pasted
                pasted_back = True
            else:
                print("[HairAPI] paste_back 回贴失败/无脸 → 回退对齐裁剪结果")
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"发型迁移失败: {e}")
    finally:
        # 每请求释放本轮累积的 CPU/GPU 内存，抑制长跑稳态增长
        try:
            import gc as _gc, torch as _t
            _gc.collect()
            if _t.cuda.is_available():
                _t.cuda.empty_cache(); _t.cuda.ipc_collect()
        except Exception:
            pass

    elapsed = int((time.time() - t0) * 1000)
    print(f"[HairAPI] 发型迁移完成 {elapsed}ms{' [整图回贴]' if pasted_back else ''}")
    return HairResponse(result_image=img_to_b64(result), elapsed_ms=elapsed,
                        pasted_back=pasted_back)

# response_class=None → openapi 生成 AssertionError → /openapi.json 500（阶段11 根因同 faceswap）
from fastapi.responses import HTMLResponse as _HTMLResp


@app.get("/ui", response_class=_HTMLResp)
def hair_ui():
    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><title>发型切换</title>
<style>
  body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;padding:20px;max-width:700px;margin:auto}
  h1{color:#e94560;text-align:center}
  .card{background:#16213e;border-radius:12px;padding:18px;margin:12px 0}
  h3{background:#e94560;color:#fff;padding:7px 14px;border-radius:6px;margin:0 0 14px 0;font-size:14px}
  .grid{display:flex;flex-wrap:wrap;gap:10px}
  .item{background:#0f3460;border-radius:8px;padding:8px;text-align:center;cursor:pointer;
        border:2px solid transparent;transition:.2s;width:100px}
  .item:hover{border-color:#e94560}.item.active{border-color:#4caf50;background:#0d3b0d}
  .item img{width:80px;height:80px;object-fit:cover;border-radius:6px;display:block;margin:0 auto 5px}
  .item .lbl{font-size:12px;word-break:break-all}
  .item .badge{color:#4caf50;font-size:11px;font-weight:bold}
  input[type=text]{background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;
                   padding:7px 10px;width:100%;font-size:14px;margin-top:6px}
  .upload-area{border:2px dashed #e94560;border-radius:8px;padding:20px;text-align:center;
               cursor:pointer;margin-top:10px}
  .upload-area:hover{background:#0f3460}
  .btn{background:#e94560;color:#fff;border:none;padding:11px 20px;border-radius:8px;
       cursor:pointer;font-size:14px;font-weight:bold;width:100%;margin-top:8px}
  .btn.green{background:#2d7a2d}
  .s{text-align:center;padding:8px;border-radius:6px;margin-top:8px;font-size:13px;display:none}
  .ok{background:#0d3b0d;color:#4caf50}.err{background:#3b0d0d;color:#f44336}

  /* 实时预览区 */
  .preview-box{display:flex;gap:10px;margin-top:10px}
  .preview-box>div{flex:1;text-align:center;font-size:12px;color:#aaa}
  .preview-box img{width:100%;border-radius:8px;border:2px solid #0f3460;background:#0f3460;min-height:120px}
  .apply-btn{background:#0f3460;border:2px solid #e94560;color:#e94560;font-size:14px;font-weight:bold;
             padding:12px;border-radius:8px;cursor:pointer;width:100%;margin-top:8px;transition:.2s}
  .apply-btn:hover{background:#e94560;color:#fff}
</style></head>
<body>
<h1>💇 发型切换面板</h1>

<div class="card">
  <h3>选择发型（点击激活）</h3>
  <div class="grid" id="styleGrid">加载中...</div>
</div>

<div class="card">
  <h3>上传参考发型照片</h3>
  <input type="text" id="sname" placeholder="发型名称（如：短发、长直发、卷发）">
  <div class="upload-area" onclick="document.getElementById('sfile').click()">
    📁 点击选择参考照片（只需包含发型，可以是明星照）
    <input type="file" id="sfile" accept="image/*" style="display:none" onchange="previewFile(this)">
  </div>
  <div id="sprev" style="display:none;text-align:center;margin-top:8px">
    <img id="sprevImg" style="height:80px;border-radius:6px">
  </div>
  <button class="btn green" onclick="uploadStyle()">✅ 上传发型</button>
  <div id="ust" class="s"></div>
</div>

<div class="card">
  <h3>实时发型预览</h3>
  <p style="color:#aaa;font-size:13px">上传一张你自己的正脸照片，点击"生成预览"查看换发效果</p>
  <div class="upload-area" onclick="document.getElementById('myface').click()">
    📷 上传我的照片
    <input type="file" id="myface" accept="image/*" style="display:none" onchange="setMyFace(this)">
  </div>
  <button class="apply-btn" onclick="doHairTransfer()">✨ 生成发型预览</button>
  <div class="preview-box">
    <div><img id="origImg" src=""><br>原图</div>
    <div><img id="resultImg" src=""><br>换发后</div>
  </div>
  <div id="pst" class="s"></div>
</div>

<script>
let uploadFile=null, myFaceB64="";

async function loadStyles(){
  const r=await fetch('/hair_styles'); const d=await r.json();
  const g=document.getElementById('styleGrid');
  if(!d.styles.length){g.innerHTML='<span style="color:#aaa">暂无发型，请上传参考照片</span>';return;}
  g.innerHTML=d.styles.map(n=>`
    <div class="item ${n===d.active?'active':''}" onclick="switchStyle('${n}')">
      <img src="/hair_thumb?name=${encodeURIComponent(n)}" onerror="this.src=''">
      <div class="lbl">${n}</div>
      ${n===d.active?'<div class="badge">✅当前</div>':''}
    </div>`).join('');
}

async function switchStyle(name){
  await fetch('/hair_styles/switch',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  loadStyles();
}

function previewFile(input){
  uploadFile=input.files[0]; if(!uploadFile) return;
  const r=new FileReader(); r.onload=e=>{
    document.getElementById('sprevImg').src=e.target.result;
    document.getElementById('sprev').style.display='block';
  }; r.readAsDataURL(uploadFile);
}

async function uploadStyle(){
  const name=document.getElementById('sname').value.trim();
  const s=document.getElementById('ust');
  if(!name||!uploadFile){s.className='s err';s.textContent='请填写名称并选择文件';s.style.display='block';return;}
  const r=new FileReader(); r.onload=async e=>{
    const res=await fetch('/hair_styles/upload',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,image:e.target.result})});
    const d=await res.json();
    s.className='s ok';s.textContent='✅ 上传成功';s.style.display='block';
    uploadFile=null; loadStyles(); setTimeout(()=>s.style.display='none',3000);
  }; r.readAsDataURL(uploadFile);
}

function setMyFace(input){
  const file=input.files[0]; if(!file) return;
  const r=new FileReader(); r.onload=e=>{
    myFaceB64=e.target.result;
    document.getElementById('origImg').src=myFaceB64;
  }; r.readAsDataURL(file);
}

async function doHairTransfer(){
  if(!myFaceB64){alert('请先上传您的照片');return;}
  const s=document.getElementById('pst');
  s.className='s ok';s.textContent='处理中，请稍候...';s.style.display='block';
  const r=await fetch('/hair_transfer',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({source_image:myFaceB64})});
  if(r.ok){
    const d=await r.json();
    document.getElementById('resultImg').src='data:image/jpeg;base64,'+d.result_image;
    s.textContent='✅ 完成 '+d.elapsed_ms+'ms';
  } else {
    const e=await r.json(); s.className='s err';s.textContent='❌ '+e.detail;
  }
  setTimeout(()=>s.style.display='none',5000);
}

loadStyles();
</script>
</body></html>"""
    return HTMLResponse(content=html)

if __name__ == "__main__":
    print("=" * 50)
    print(" Hair Transfer API  http://0.0.0.0:8001")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
