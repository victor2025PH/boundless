# -*- coding: utf-8 -*-
"""
虚拟试衣 API - 基于 IDM-VTON (HuggingFace diffusers)
端口: 8002
用法: POST /tryon  {person_image: base64, cloth_image: base64}
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import base64, os, time, cv2, numpy as np
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import uvicorn
import torch

import app_config
CLOTH_DIR = app_config.BASE / "clothes"
CLOTH_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Virtual Try-On API")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="tryon")               # 替代原 CORS:* 无鉴权

# ── 全局模型 ────────────────────────────────────────────────────
pipe = None
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_backend = "none"          # fitdit / idm-vton / inpaint_fallback / none（/health 汇报真实后端）

# FitDiT 后端（2026-07-08 试衣质量专项落地）：SD3-DiT 双塔，VITON-HD 质量第一梯队，
# 预处理全 onnx（DWPose+humanparsing，随权重仓自带）——无 detectron2，Windows 可装。
# 权重 8.1GB @ C:\models\FitDiT；宿主 env=fitdit(克隆自 musethepeak, diffusers 0.38)。
# FITDIT_OFFLOAD=1(默认) 峰值显存 <6G；=0 全驻留 ~14G 更快。失败回退 IDM-VTON→SD-inpaint。
TRYON_BACKEND = os.environ.get("TRYON_BACKEND", "auto").lower()
FITDIT_DIR = Path(os.environ.get("FITDIT_DIR", r"C:\models\FitDiT"))

def load_fitdit_model() -> bool:
    global pipe, _backend
    if TRYON_BACKEND not in ("auto", "fitdit"):
        return False
    if not (FITDIT_DIR.exists() and any(FITDIT_DIR.glob("**/*.safetensors"))):
        if TRYON_BACKEND == "fitdit":
            print(f"[TryOnAPI] TRYON_BACKEND=fitdit 但权重缺失: {FITDIT_DIR}")
        else:
            print(f"[TryOnAPI] FitDiT 权重未就位({FITDIT_DIR})，沿用 IDM-VTON")
        return False
    try:
        from fitdit_pipeline import FitDiTWrapper
        pipe = FitDiTWrapper(str(FITDIT_DIR), device=DEVICE)
        pipe.__class__._mode = "fitdit"
        _backend = "fitdit"
        print(f"[TryOnAPI] FitDiT 加载完成 offload={pipe.offload}"
              f"（约 5~9s/张 @768x1024，质量对标 2025 SOTA）")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[TryOnAPI] FitDiT 加载失败，回退 IDM-VTON: {e}")
        return False

def load_tryon_model():
    global pipe, _backend
    if load_fitdit_model():
        return
    try:
        from diffusers import AutoPipelineForInpainting
        from huggingface_hub import snapshot_download
        print(f"[TryOnAPI] 加载 IDM-VTON 模型 ({DEVICE})...")
        # 下载 IDM-VTON 模型（约 6GB，首次需要时间）
        model_path = snapshot_download(
            "yisol/IDM-VTON",
            local_dir=r"C:\models\IDM-VTON",
            ignore_patterns=["*.msgpack", "*.h5"])
        from idm_vton_pipeline import IDMVTONPipeline
        pipe = IDMVTONPipeline.from_pretrained(model_path, torch_dtype=torch.float16)
        pipe = pipe.to(DEVICE)
        _backend = "idm-vton"
        print("[TryOnAPI] IDM-VTON 加载完成")
    except Exception as e:
        print(f"[TryOnAPI] IDM-VTON 加载失败，尝试备用方案: {e}")
        load_fallback_model()

def load_fallback_model():
    """备用方案：使用 SD Inpainting + 服装分割"""
    global pipe, _backend
    try:
        from diffusers import StableDiffusionInpaintPipeline
        print("[TryOnAPI] 加载 SD Inpaint 备用模型...")
        pipe = StableDiffusionInpaintPipeline.from_pretrained(
            "runwayml/stable-diffusion-inpainting",
            torch_dtype=torch.float16,
            safety_checker=None,
        ).to(DEVICE)
        pipe.__class__._mode = "inpaint_fallback"
        _backend = "inpaint_fallback"
        print("[TryOnAPI] SD Inpaint 加载完成")
    except Exception as e2:
        print(f"[TryOnAPI] 备用模型也失败: {e2}")
        pipe = None

print("[TryOnAPI] 正在加载模型...")
load_tryon_model()

# ── 服装库管理 ───────────────────────────────────────────────────
_clothes: dict = {}
_active_cloth: str = ""

def scan_clothes():
    result = {}
    for ext in ('*.jpg','*.jpeg','*.png','*.webp'):
        for f in CLOTH_DIR.glob(ext):
            with open(f,'rb') as fp:
                result[f.stem] = base64.b64encode(fp.read()).decode()
    return result

def reload_clothes():
    global _clothes, _active_cloth
    _clothes = scan_clothes()
    if _clothes and _active_cloth not in _clothes:
        _active_cloth = list(_clothes.keys())[0]
    print(f"[TryOnAPI] 服装库: {list(_clothes.keys())}")

reload_clothes()

# ── 工具函数 ─────────────────────────────────────────────────────
def b64_to_img(b64: str) -> np.ndarray:
    if "," in b64: b64 = b64.split(",",1)[1]
    arr = np.frombuffer(base64.b64decode(b64), np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def img_to_b64(img: np.ndarray, quality=90) -> str:
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode()

def get_clothing_mask(img: np.ndarray) -> np.ndarray:
    """用颜色和位置启发式估计上身服装区域"""
    h, w = img.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    # 上身区域：纵向20%~65%，横向15%~85%
    y1, y2 = int(h * 0.18), int(h * 0.68)
    x1, x2 = int(w * 0.12), int(w * 0.88)
    mask[y1:y2, x1:x2] = 255
    # 排除脸部区域（上方20%）
    mask[:int(h*0.18), :] = 0
    return mask

def np_to_pil(img):
    from PIL import Image
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

def pil_to_np(pil_img):
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

# ── 请求模型 ──────────────────────────────────────────────────────
class TryOnRequest(BaseModel):
    person_image: str        # 人物图 base64
    cloth_image:  str = ""   # 服装图 base64（空=用当前激活）
    cloth_type:   str = "upper"   # upper/lower/full
    resolution:   str = ""   # FitDiT 档位: 768x1024(默认)/1152x1536/1536x2048；旧后端忽略

class TryOnResponse(BaseModel):
    result_image: str
    elapsed_ms:   int

# ── 路由 ──────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "tryon-api", "model_loaded": pipe is not None,
            "ui": "http://127.0.0.1:8002/ui"}

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": pipe is not None, "device": DEVICE,
            "backend": _backend}

@app.get("/clothes")
def list_clothes():
    reload_clothes()
    return {"clothes": list(_clothes.keys()), "active": _active_cloth}

@app.post("/clothes/switch")
def switch_cloth(data: dict):
    global _active_cloth
    name = data.get("name","")
    reload_clothes()
    if name not in _clothes:
        raise HTTPException(status_code=404, detail=f"找不到: {name}")
    _active_cloth = name
    return {"ok": True, "active": _active_cloth}

@app.post("/clothes/upload")
async def upload_cloth(data: dict):
    name  = data.get("name","").strip()
    img64 = data.get("image","")
    if not name or not img64:
        raise HTTPException(status_code=400, detail="需要 name 和 image")
    if "," in img64: img64 = img64.split(",",1)[1]
    save_path = CLOTH_DIR / f"{name}.jpg"
    with open(save_path,'wb') as f:
        f.write(base64.b64decode(img64))
    reload_clothes()
    return {"ok": True, "clothes": list(_clothes.keys())}

@app.post("/clothes/delete")
def delete_cloth(data: dict):
    """删服装（阶段6：库到百件量级后的管理必需品）。删激活项时自动切到剩余首件。"""
    global _active_cloth
    name = (data.get("name") or "").strip()
    reload_clothes()
    if name not in _clothes:
        raise HTTPException(status_code=404, detail=f"找不到: {name}")
    removed = False
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = CLOTH_DIR / f"{name}{ext}"
        if p.exists():
            p.unlink()
            removed = True
    if not removed:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")
    reload_clothes()
    if _active_cloth == name:
        _active_cloth = next(iter(_clothes), "")
    return {"ok": True, "clothes": list(_clothes.keys()), "active": _active_cloth}

# ── 截图抠衣（2026-07-08 阶段5）：穿着照/商品截图 → 服装白底图入库 ─────────
# 路径A 人体解析：FitDiT 自带 humanparsing onnx 提取服装类（ATR: 4上衣/5裙/
#   6裤/7连衣裙/17围巾）——零新依赖，offload 模式走 CPU。实测无人平铺商品图
#   它也能直接分割服装（泛化好），背景差分只是更深层兜底。
# 路径B 背景差分：四角中位色距离阈值 + 最大连通域（背景较素的截图）。
# A 覆盖率 <3% 自动回落 B；抠出后 bbox 加 6% 边距贴白底，羽化 7px 防硬边。
# part 限定部位：穿着照常把上衣+裤子一起抠出，试上装时传 part=upper 排除下装。
_PART_CLASSES = {"auto": [4, 5, 6, 7, 17], "upper": [4, 7, 17],
                 "lower": [5, 6], "dress": [7]}


def _extract_by_parsing(img: np.ndarray, part: str = "auto"):
    """人体解析路径。返回 (mask u8 原图尺寸, 'parsing') 或 (None, '')。"""
    if getattr(pipe.__class__, "_mode", "") != "fitdit":
        return None, ""
    from PIL import Image as PILImage
    pil = np_to_pil(img)
    w, h = pil.size
    scale = 768 / min(w, h)                      # 官方 gradio 同款检测尺寸
    det = pil.resize((int(round(w * scale)), int(round(h * scale))), PILImage.LANCZOS)
    parse, _ = pipe.parsing_model(det)
    arr = np.array(parse)
    classes = _PART_CLASSES.get(part, _PART_CLASSES["auto"])
    mask = (np.isin(arr, classes).astype(np.uint8)) * 255
    mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
    return mask, "parsing"


def _extract_by_bgdiff(img: np.ndarray):
    """背景差分路径（平铺商品图）。返回 (mask, 'bgdiff') 或 (None, '')。"""
    h, w = img.shape[:2]
    k = max(10, min(h, w) // 30)
    corners = np.concatenate([img[:k, :k].reshape(-1, 3), img[:k, -k:].reshape(-1, 3),
                              img[-k:, :k].reshape(-1, 3), img[-k:, -k:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    dist = np.linalg.norm(img.astype(np.float32) - bg, axis=2)
    mask = (dist > 30).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None, ""
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == idx).astype(np.uint8) * 255, "bgdiff"


def _compose_white(img: np.ndarray, mask: np.ndarray):
    """掩码区 bbox 裁剪 + 白底合成（羽化边缘）。掩码太小返回 None。"""
    ys, xs = np.where(mask > 0)
    if len(xs) < 400:
        return None
    x1, x2, y1, y2 = xs.min(), xs.max(), ys.min(), ys.max()
    pad = int(max(y2 - y1, x2 - x1) * 0.06) + 8
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = min(img.shape[1], x2 + pad), min(img.shape[0], y2 + pad)
    crop = img[y1:y2, x1:x2].astype(np.float32)
    m = (cv2.GaussianBlur(mask[y1:y2, x1:x2], (7, 7), 0).astype(np.float32) / 255.0)[..., None]
    return (crop * m + 255.0 * (1.0 - m)).astype(np.uint8)


@app.post("/clothes/extract")
def clothes_extract(data: dict):
    """截图抠衣：{image: b64, save_name?, part?=auto|upper|lower|dress}
    → 服装白底图（save_name 给了就入库）。
    先人体解析（穿着照/平铺图都行），覆盖率<3% 回落背景差分。"""
    img64 = (data.get("image") or "")
    if "," in img64:
        img64 = img64.split(",", 1)[1]
    if not img64:
        raise HTTPException(status_code=400, detail="需要 image")
    img = b64_to_img(img64)
    if img is None:
        raise HTTPException(status_code=400, detail="图片解码失败")
    part = (data.get("part") or "auto").lower()
    t0 = time.time()

    mask, method = None, ""
    try:
        mask, method = _extract_by_parsing(img, part)
    except Exception as e:
        print(f"[TryOnAPI] 解析路径失败(回落背景差分): {e}")
    if mask is None or (mask > 0).mean() < 0.03:
        m2, meth2 = _extract_by_bgdiff(img)
        if m2 is not None and (mask is None or (m2 > 0).mean() > (mask > 0).mean()):
            mask, method = m2, meth2
    out = _compose_white(img, mask) if mask is not None else None
    if out is None:
        raise HTTPException(status_code=422,
                            detail="未能从图中提取服装（人too小/背景太花）——试试裁剪后重传")

    result64 = img_to_b64(out, quality=95)
    saved = ""
    name = (data.get("save_name") or "").strip()
    if name:
        (CLOTH_DIR / f"{name}.jpg").write_bytes(base64.b64decode(result64))
        reload_clothes()
        saved = name
    print(f"[TryOnAPI] 抠衣完成 {int((time.time()-t0)*1000)}ms method={method} saved={saved or '-'}")
    return {"ok": True, "garment_image": result64, "method": method,
            "coverage": round(float((mask > 0).mean()), 3), "saved": saved,
            "clothes": list(_clothes.keys()) if saved else None}


@app.get("/cloth_thumb")
def cloth_thumb(name: str):
    path = CLOTH_DIR / f"{name}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    with open(str(path),'rb') as f: raw = f.read()
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (80,120))
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(content=buf.tobytes(), media_type="image/jpeg")

# FitDiT 单请求 ~5-6G 显存(offload)；FastAPI 同步端点默认 40 线程并发 ——
# 两单同时进来就是双份峰值+共卡直播被挤。串行锁：同一时刻只跑一单，其余排队。
import threading
_infer_lock = threading.Lock()

# 显存准入闸（2026-07-08 实测教训）：共卡直播把空闲显存压到 <6G 时，Windows 驱动
# 不报 OOM 而是回落到「共享系统内存」——9s 的单变成 15min+ 的 98% 假忙碌，
# 还拖累直播链（实锤：free≈4G 时 768 档 600s 未出图，杀进程收场）。
# 宁可立刻 503 让用户稍后再试，也不无声磨盘。阈值按档位阶梯，可环境变量覆盖。
# 闸体已抽公共模块 vram_gate.py（阶段5），hair 等离线 GPU 服务同款接入。
import vram_gate as _vgate
_MIN_FREE_GB = float(os.environ.get("TRYON_MIN_FREE_GB", "7"))
_RES_FREE_GB = {"768x1024": 7.0, "1152x1536": 9.0, "1536x2048": 11.0}


def _vram_gate(resolution: str = ""):
    if _backend != "fitdit":
        return
    need = max(_MIN_FREE_GB, _RES_FREE_GB.get(resolution or "768x1024", 7.0))
    _vgate.gate(need, service="tryon")


@app.post("/tryon", response_model=TryOnResponse)
def tryon(req: TryOnRequest):
    if pipe is None:
        raise HTTPException(status_code=503, detail="试衣模型未加载")
    _vram_gate(req.resolution)
    t0 = time.time()

    cloth_b64 = req.cloth_image if req.cloth_image else _clothes.get(_active_cloth,"")
    if not cloth_b64:
        raise HTTPException(status_code=400, detail="没有服装图，请先上传")

    person_img = b64_to_img(req.person_image)
    cloth_img  = b64_to_img(cloth_b64)
    if person_img is None or cloth_img is None:
        raise HTTPException(status_code=400, detail="图片解码失败")

    try:
        person_pil = np_to_pil(person_img)
        cloth_pil  = np_to_pil(cloth_img)
        h, w = person_img.shape[:2]

        mode = getattr(pipe.__class__, '_mode', 'normal')

        if mode == "fitdit":
            # FitDiT 自带 pad/resize 与 mask 流程，喂原始比例即可
            try:
                with _infer_lock:
                    result_pil = pipe.tryon(person_pil, cloth_pil, cloth_type=req.cloth_type,
                                            resolution=req.resolution)
            finally:
                # offload 已把权重挪回 CPU，这里再还缓存池——离线服务不长占直播卡显存
                torch.cuda.empty_cache()
            result = pil_to_np(result_pil)
            if result.shape[:2] != (h, w):
                result = cv2.resize(result, (w, h))
            elapsed = int((time.time() - t0) * 1000)
            print(f"[TryOnAPI] FitDiT 试衣完成 {elapsed}ms")
            return TryOnResponse(result_image=img_to_b64(result), elapsed_ms=elapsed)

        # 统一尺寸（旧后端）
        target_size = (768, 1024)
        person_pil = person_pil.resize(target_size)
        cloth_pil  = cloth_pil.resize((target_size[0], target_size[0]))

        if mode == "inpaint_fallback":
            # 备用：SD Inpainting
            from PIL import Image
            mask_np = get_clothing_mask(
                cv2.resize(person_img, target_size))
            from PIL import Image as PILImage
            mask_pil = PILImage.fromarray(mask_np).resize(target_size)
            prompt = "person wearing the clothing, photorealistic, high quality"
            result_pil = pipe(
                prompt=prompt,
                image=person_pil,
                mask_image=mask_pil,
                num_inference_steps=25,
                guidance_scale=7.5,
            ).images[0]
        else:
            # IDM-VTON 正式推理
            result_pil = pipe(
                person_image=person_pil,
                cloth_image=cloth_pil,
                cloth_type=req.cloth_type,
                num_inference_steps=20,
            ).images[0]

        result = pil_to_np(result_pil)
        result = cv2.resize(result, (w, h))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试衣失败: {e}")

    elapsed = int((time.time() - t0) * 1000)
    print(f"[TryOnAPI] 试衣完成 {elapsed}ms")
    return TryOnResponse(result_image=img_to_b64(result), elapsed_ms=elapsed)

# response_class=None → openapi 生成 AssertionError → /openapi.json 500（阶段11 根因同 faceswap）
from fastapi.responses import HTMLResponse as _HTMLResp


@app.get("/ui", response_class=_HTMLResp)
def tryon_ui():
    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><title>虚拟试衣</title>
<style>
  body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;padding:20px;max-width:750px;margin:auto}
  h1{color:#e94560;text-align:center}
  .card{background:#16213e;border-radius:12px;padding:18px;margin:12px 0}
  h3{background:#e94560;color:#fff;padding:7px 14px;border-radius:6px;margin:0 0 14px 0;font-size:14px}
  .grid{display:flex;flex-wrap:wrap;gap:10px}
  .item{background:#0f3460;border-radius:8px;padding:8px;text-align:center;cursor:pointer;
        border:2px solid transparent;transition:.2s;width:100px}
  .item:hover{border-color:#e94560}.item.active{border-color:#4caf50;background:#0d3b0d}
  .item img{width:80px;height:100px;object-fit:cover;border-radius:6px;display:block;margin:0 auto 5px}
  .item .lbl{font-size:12px}.item .badge{color:#4caf50;font-size:11px;font-weight:bold}
  input[type=text]{background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;
                   padding:7px 10px;width:100%;font-size:14px;margin-top:6px}
  .upload-area{border:2px dashed #e94560;border-radius:8px;padding:18px;text-align:center;
               cursor:pointer;margin-top:8px}
  .upload-area:hover{background:#0f3460}
  .btn{background:#e94560;color:#fff;border:none;padding:11px 20px;border-radius:8px;
       cursor:pointer;font-size:14px;font-weight:bold;width:100%;margin-top:8px}
  .btn.green{background:#2d7a2d}
  .apply-btn{background:#0f3460;border:2px solid #e94560;color:#e94560;font-size:15px;
             font-weight:bold;padding:14px;border-radius:8px;cursor:pointer;width:100%;margin-top:10px}
  .apply-btn:hover{background:#e94560;color:#fff}
  .s{text-align:center;padding:8px;border-radius:6px;margin-top:8px;font-size:13px;display:none}
  .ok{background:#0d3b0d;color:#4caf50}.err{background:#3b0d0d;color:#f44336}
  .preview-box{display:flex;gap:10px;margin-top:12px}
  .preview-box>div{flex:1;text-align:center;font-size:12px;color:#aaa}
  .preview-box img{width:100%;border-radius:8px;border:2px solid #0f3460;min-height:150px;background:#0f3460}
  select{background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;padding:7px;width:100%;margin-top:6px}
</style></head>
<body>
<h1>👗 虚拟试衣面板</h1>

<div class="card">
  <h3>服装库（点击选择）</h3>
  <div class="grid" id="clothGrid">加载中...</div>
</div>

<div class="card">
  <h3>上传新服装图片</h3>
  <p style="color:#aaa;font-size:12px">建议上传白底或纯色背景的服装平铺图，效果最好</p>
  <input type="text" id="cname" placeholder="服装名称（如：红色连衣裙）">
  <div class="upload-area" onclick="document.getElementById('cfile').click()">
    📁 点击选择服装图片
    <input type="file" id="cfile" accept="image/*" style="display:none" onchange="previewCloth(this)">
  </div>
  <div id="cprev" style="display:none;text-align:center;margin-top:8px">
    <img id="cprevImg" style="height:100px;border-radius:6px">
  </div>
  <button class="btn green" onclick="uploadCloth()">✅ 上传服装</button>
  <div id="ust" class="s"></div>
</div>

<div class="card">
  <h3>试穿预览（约10~30秒）</h3>
  <div class="upload-area" onclick="document.getElementById('pfile').click()">
    📷 上传您的全身照片（建议正面站立）
    <input type="file" id="pfile" accept="image/*" style="display:none" onchange="setPersonImg(this)">
  </div>
  <select id="clothType">
    <option value="upper">上衣</option>
    <option value="lower">下装</option>
    <option value="full">连体/全身</option>
  </select>
  <button class="apply-btn" onclick="doTryOn()">✨ 开始试穿（AI渲染）</button>
  <div class="preview-box">
    <div><img id="origImg" src=""><br>原图</div>
    <div><img id="resultImg" src=""><br>试穿效果</div>
  </div>
  <div id="pst" class="s"></div>
</div>

<script>
let clothFile=null, personB64="";

async function loadClothes(){
  const r=await fetch('/clothes'); const d=await r.json();
  const g=document.getElementById('clothGrid');
  if(!d.clothes.length){g.innerHTML='<span style="color:#aaa">服装库为空，请上传服装图片</span>';return;}
  g.innerHTML=d.clothes.map(n=>`
    <div class="item ${n===d.active?'active':''}" onclick="switchCloth('${n}')">
      <img src="/cloth_thumb?name=${encodeURIComponent(n)}" onerror="this.src=''">
      <div class="lbl">${n}</div>
      ${n===d.active?'<div class="badge">✅</div>':''}
    </div>`).join('');
}
async function switchCloth(name){
  await fetch('/clothes/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  loadClothes();
}
function previewCloth(input){
  clothFile=input.files[0]; if(!clothFile) return;
  const r=new FileReader(); r.onload=e=>{
    document.getElementById('cprevImg').src=e.target.result;
    document.getElementById('cprev').style.display='block';
  }; r.readAsDataURL(clothFile);
}
async function uploadCloth(){
  const name=document.getElementById('cname').value.trim();
  const s=document.getElementById('ust');
  if(!name||!clothFile){s.className='s err';s.textContent='请填写名称并选择图片';s.style.display='block';return;}
  const r=new FileReader(); r.onload=async e=>{
    const res=await fetch('/clothes/upload',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,image:e.target.result})});
    s.className='s ok';s.textContent='✅ 上传成功';s.style.display='block';
    clothFile=null; loadClothes(); setTimeout(()=>s.style.display='none',3000);
  }; r.readAsDataURL(clothFile);
}
function setPersonImg(input){
  const file=input.files[0]; if(!file) return;
  const r=new FileReader(); r.onload=e=>{
    personB64=e.target.result;
    document.getElementById('origImg').src=personB64;
  }; r.readAsDataURL(file);
}
async function doTryOn(){
  if(!personB64){alert('请先上传您的照片');return;}
  const s=document.getElementById('pst');
  const clothType=document.getElementById('clothType').value;
  s.className='s ok';s.textContent='AI渲染中，请耐心等待（10~30秒）...';s.style.display='block';
  const r=await fetch('/tryon',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({person_image:personB64,cloth_type:clothType})});
  if(r.ok){
    const d=await r.json();
    document.getElementById('resultImg').src='data:image/jpeg;base64,'+d.result_image;
    s.textContent='✅ 完成！耗时 '+(d.elapsed_ms/1000).toFixed(1)+'秒';
  } else {
    const e=await r.json(); s.className='s err';s.textContent='❌ '+e.detail;
  }
  setTimeout(()=>s.style.display='none',8000);
}
loadClothes();
</script>
</body></html>"""
    return HTMLResponse(content=html)

if __name__ == "__main__":
    print("=" * 50)
    print(" Virtual Try-On API  http://0.0.0.0:8002")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="warning")
