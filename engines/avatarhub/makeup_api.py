# -*- coding: utf-8 -*-
"""
妆容定妆 API —— MediaPipe FaceMesh(478点) + LAB 区域颜色迁移
端口: 8004（8003 已被 faceswap2 高清副本预留）
用法: POST /makeup_transfer {source_image: b64, style?/ref_image?/params?}

设计（2026-07-08 Look Pack 定妆包 · 深入思考后的选型结论）：
  · 为什么不用 FLUX-Makeup/Stable-Makeup 扩散方案：权重 6~24GB、20s+/张、
    需新 conda 环境；且 inswapper 直播换脸走 ArcFace 身份向量（对妆容鲁棒），
    烘进源脸的妆在换脸输出里本就存留有限——重型生成投入产出不成比。
  · 本服务定位「离线定妆」：给数字人照片链（Ditto/LivePortrait/lipsync 直接
    使用照片像素，妆容 100% 可见）与角色预览/缩略图上妆；1~3s/张、纯 CPU、
    零大权重（face_landmarker.task 3.7MB 已随 models/ 下发）。
  · 直播换脸链的妆容在 faceswap_api 输出端叠加（见 _apply_live_makeup），
    与本服务共用同一套颜色规范（spec 字段一致）。
  · 引擎可插拔：MAKEUP_BACKEND=regional(默认)。后续接 FLUX-Makeup 时新增
    backend 分支即可，API 契约不变。

关键实现点：
  · 中文路径下 mediapipe 打不开模型文件（GBK 坑）→ model_asset_buffer 读内存；
  · 唇彩按「红度加权」上色（LAB a 通道 132~142 平滑门限）：张嘴露齿时
    牙齿 a≈128 权重≈0 不被染色，无需精确内唇索引，鲁棒；
  · 眼影带 = 上眼睑弧线向眉下缘插值推移（比例 0.66），减去膨胀眼球区；
  · 腮红 = 颧骨锚点(205/425 与 50/280 中点)椭圆 + 大核羽化；
  · 磨皮 = 脸椭圆减五官区 bilateral 混合（保边缘），奶油肌妆感的地基。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import base64, time, threading
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import app_config

app = FastAPI(title="Makeup Transfer API")
import service_auth                                  # GPU 服务面统一加固：鉴权 + CORS 收敛
service_auth.secure(app, name="makeup")

MAKEUP_DIR = app_config.BASE / "makeup_styles"       # 参考妆图库（与 hair_styles/clothes 同构）
MAKEUP_DIR.mkdir(exist_ok=True)
_LM_MODEL = app_config.MODELS_DIR / "face_landmarker.task"
_LM_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
           "face_landmarker/float16/latest/face_landmarker.task")


def _log(msg: str):
    """GBK 控制台安全打印（与 bg_replace 同款防崩）。"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(str(msg).encode("gbk", "replace").decode("gbk"), flush=True)
        except Exception:
            pass


# ── FaceLandmarker（懒加载 + 线程锁：mediapipe graph 非线程安全）────────
_landmarker = None
_lm_lock = threading.Lock()

def _get_landmarker():
    global _landmarker
    if _landmarker is not None:
        return _landmarker
    try:
        if not _LM_MODEL.exists():           # 缺件自动下载（3.7MB）
            import urllib.request
            _log(f"[MakeupAPI] 下载 face_landmarker.task ...")
            _LM_MODEL.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(_LM_URL, str(_LM_MODEL))
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        buf = _LM_MODEL.read_bytes()          # 中文路径坑：必须走内存 buffer
        opts = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_buffer=buf),
            num_faces=1, output_face_blendshapes=False)
        _landmarker = vision.FaceLandmarker.create_from_options(opts)
        _log("[MakeupAPI] FaceLandmarker(478点) 加载完成")
    except Exception as e:
        import traceback; traceback.print_exc()
        _log(f"[MakeupAPI] FaceLandmarker 加载失败: {e}")
        _landmarker = None
    return _landmarker


def _detect_landmarks(img_bgr: np.ndarray):
    """返回 (478,2) 像素坐标 或 None。"""
    lm = _get_landmarker()
    if lm is None:
        return None
    import mediapipe as mp
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    with _lm_lock:
        res = lm.detect(mp_img)
    if not res.face_landmarks:
        return None
    h, w = img_bgr.shape[:2]
    pts = np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]], dtype=np.float32)
    return pts


# ── FaceMesh 区域索引（社区标准索引集）──────────────────────────────
LIPS_OUTER = [61,146,91,181,84,17,314,405,321,375,291,409,270,269,267,0,37,39,40,185]
LEFT_EYE  = [33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246]
RIGHT_EYE = [362,382,381,380,374,373,390,249,263,466,388,387,386,385,384,398]
LEFT_LID_UP  = [33,246,161,160,159,158,157,173,133]    # 上眼睑弧（太阳穴→鼻侧）
RIGHT_LID_UP = [362,398,384,385,386,387,388,466,263]   # 上眼睑弧（鼻侧→太阳穴）
LEFT_BROW_LOW  = [46,53,52,65,55]                      # 左眉下缘
RIGHT_BROW_LOW = [285,295,282,283,276]                 # 右眉下缘
FACE_OVAL = [10,338,297,332,284,251,389,356,454,323,361,288,397,365,379,378,400,
             377,152,148,176,149,150,136,172,58,132,93,234,127,162,21,54,103,67,109]

def _poly_mask(shape, pts, blur_k=0):
    m = np.zeros(shape[:2], dtype=np.float32)
    if pts is not None and len(pts) >= 3:
        cv2.fillPoly(m, [np.round(pts).astype(np.int32)], 1.0)
    if blur_k >= 3:
        k = blur_k | 1
        m = cv2.GaussianBlur(m, (k, k), 0)
    return m


def _eye_dist(pts):
    lc = pts[LEFT_EYE].mean(axis=0); rc = pts[RIGHT_EYE].mean(axis=0)
    return float(np.linalg.norm(rc - lc))


def _shadow_poly(pts, lid_idx, brow_idx, lift=0.66):
    """眼影带多边形：上眼睑弧向眉下缘按 x 插值推移 lift 比例。"""
    lid = pts[lid_idx]
    brow = pts[brow_idx]
    order = np.argsort(brow[:, 0])
    bx, by = brow[order, 0], brow[order, 1]
    top = lid.copy()
    ys = np.interp(lid[:, 0], bx, by)          # 各睑点对应的眉线 y
    top[:, 1] = lid[:, 1] + lift * (ys - lid[:, 1])
    return np.concatenate([lid, top[::-1]], axis=0)


def _region_masks(img, pts):
    """构建全部妆容区域掩码（float 0~1，已羽化）。"""
    h, w = img.shape[:2]
    ed = _eye_dist(pts)
    k_lip = max(3, int(ed * 0.10))
    k_eye = max(5, int(ed * 0.22))
    k_blush = max(15, int(ed * 0.85))
    k_skin = max(9, int(ed * 0.30))

    lips = _poly_mask(img.shape, pts[LIPS_OUTER], blur_k=k_lip)

    eye_l = _poly_mask(img.shape, pts[LEFT_EYE])
    eye_r = _poly_mask(img.shape, pts[RIGHT_EYE])
    eyes = cv2.dilate(np.maximum(eye_l, eye_r),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(ed*0.10)),)*2))

    shadow = np.maximum(
        _poly_mask(img.shape, _shadow_poly(pts, LEFT_LID_UP, LEFT_BROW_LOW)),
        _poly_mask(img.shape, _shadow_poly(pts, RIGHT_LID_UP, RIGHT_BROW_LOW)))
    shadow = np.clip(shadow - eyes, 0, 1)
    kk = k_eye | 1
    shadow = cv2.GaussianBlur(shadow, (kk, kk), 0)

    blush = np.zeros((h, w), dtype=np.float32)
    for a_idx, b_idx in ((205, 50), (425, 280)):    # 颧骨锚点中点为腮红中心
        c = (pts[a_idx] + pts[b_idx]) / 2.0
        cv2.ellipse(blush, (int(c[0]), int(c[1])),
                    (int(ed * 0.30), int(ed * 0.22)), 0, 0, 360, 1.0, -1)
    kb = k_blush | 1
    blush = cv2.GaussianBlur(blush, (kb, kb), 0)

    oval = _poly_mask(img.shape, pts[FACE_OVAL])
    feat = np.maximum(eyes, _poly_mask(img.shape, pts[LIPS_OUTER]))
    feat = cv2.dilate(feat, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                      (max(3, int(ed*0.16)),)*2))
    brow_band = np.zeros((h, w), dtype=np.float32)
    for bi in (LEFT_BROW_LOW, RIGHT_BROW_LOW):
        cv2.polylines(brow_band, [np.round(pts[bi]).astype(np.int32)], False, 1.0,
                      thickness=max(3, int(ed * 0.18)))
    skin = np.clip(oval - feat - brow_band, 0, 1)
    ks = k_skin | 1
    skin = cv2.GaussianBlur(skin, (ks, ks), 0)
    return {"lips": lips, "shadow": shadow, "blush": blush, "skin": skin, "eyes": eyes}


def _redness_weight(lab_a: np.ndarray) -> np.ndarray:
    """唇区红度权重：a<132(牙齿/阴影)→0，a>142(唇色)→1，线性过渡。"""
    return np.clip((lab_a.astype(np.float32) - 132.0) / 10.0, 0.0, 1.0)


def _shift_lab(img_lab, mask, color_bgr, strength, l_factor=0.25):
    """在 mask 加权下把 ab 通道向目标色收敛、L 通道按 l_factor 微调（保纹理）。"""
    tgt = cv2.cvtColor(np.uint8([[list(color_bgr)]]), cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    m = (mask * strength)[..., None]
    out = img_lab.copy()
    out[..., 1:] = img_lab[..., 1:] + m * (tgt[1:] - img_lab[..., 1:])
    out[..., 0:1] = img_lab[..., 0:1] + m * l_factor * (tgt[0] - img_lab[..., 0:1])
    return out


def apply_makeup(img: np.ndarray, spec: dict) -> tuple[np.ndarray, dict]:
    """核心上妆。spec 字段（全部可选）：
      lip_color/eye_color/blush_color: [B,G,R]；lip/eye/blush/skin: 0~1 强度。
    返回 (结果图, 实际生效 spec)。找不到人脸时抛 ValueError。"""
    pts = _detect_landmarks(img)
    if pts is None:
        raise ValueError("未检测到人脸")
    masks = _region_masks(img, pts)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    applied = {}

    lip_s = float(spec.get("lip", 0) or 0)
    if lip_s > 0 and spec.get("lip_color"):
        w = masks["lips"] * _redness_weight(lab[..., 1])
        lab = _shift_lab(lab, w, spec["lip_color"], min(lip_s, 1.0), l_factor=0.35)
        applied["lip"] = {"color": spec["lip_color"], "strength": lip_s}

    eye_s = float(spec.get("eye", 0) or 0)
    if eye_s > 0 and spec.get("eye_color"):
        lab = _shift_lab(lab, masks["shadow"], spec["eye_color"], min(eye_s, 1.0), l_factor=0.45)
        applied["eye"] = {"color": spec["eye_color"], "strength": eye_s}

    blush_s = float(spec.get("blush", 0) or 0)
    if blush_s > 0 and spec.get("blush_color"):
        lab = _shift_lab(lab, masks["blush"], spec["blush_color"], min(blush_s, 1.0), l_factor=0.10)
        applied["blush"] = {"color": spec["blush_color"], "strength": blush_s}

    out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

    skin_s = float(spec.get("skin", 0) or 0)
    if skin_s > 0:
        sm = cv2.bilateralFilter(out, 9, 45, 9)
        m = (masks["skin"] * min(skin_s, 1.0) * 0.85)[..., None]
        out = (out.astype(np.float32) * (1 - m) + sm.astype(np.float32) * m).astype(np.uint8)
        applied["skin"] = {"strength": skin_s}
    return out, applied


def extract_makeup(img: np.ndarray) -> dict:
    """从参考妆图提取颜色规范（区域中位色）。"""
    pts = _detect_landmarks(img)
    if pts is None:
        raise ValueError("参考图未检测到人脸")
    masks = _region_masks(img, pts)
    lab_a = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)[..., 1]

    def _median(mask, extra=None):
        m = mask > 0.5
        if extra is not None:
            m &= extra
        if m.sum() < 20:
            return None
        return [int(v) for v in np.median(img[m], axis=0)]

    lip = _median(masks["lips"], _redness_weight(lab_a) > 0.5)
    return {"lip_color": lip,
            "eye_color": _median(masks["shadow"]),
            "blush_color": _median(masks["blush"]),
            "skin_color": _median(masks["skin"])}


# ── 内置妆容预设（BGR）────────────────────────────────────────────────
PRESETS = {
    "自然裸妆": {"lip_color": [86, 68, 178],  "lip": 0.45, "eye_color": [105, 120, 150],
                 "eye": 0.22, "blush_color": [140, 150, 230], "blush": 0.22, "skin": 0.30},
    "复古红唇": {"lip_color": [48, 28, 165],  "lip": 0.70, "eye_color": [80, 85, 110],
                 "eye": 0.18, "blush_color": [130, 130, 215], "blush": 0.12, "skin": 0.30},
    "元气桃花": {"lip_color": [110, 90, 220], "lip": 0.50, "eye_color": [140, 120, 200],
                 "eye": 0.22, "blush_color": [150, 130, 240], "blush": 0.40, "skin": 0.32},
    "烟熏":     {"lip_color": [100, 110, 180],"lip": 0.35, "eye_color": [65, 60, 70],
                 "eye": 0.50, "blush_color": [140, 145, 205], "blush": 0.10, "skin": 0.28},
    "奶茶":     {"lip_color": [100, 120, 196],"lip": 0.45, "eye_color": [95, 125, 160],
                 "eye": 0.28, "blush_color": [135, 160, 225], "blush": 0.22, "skin": 0.35},
    # ── 阶段6 扩容（2026-07-08）：热门直播妆 10 款，色值按小红书/美妆热榜口碑色调制 ──
    "斩男红梨": {"lip_color": [70, 40, 200],  "lip": 0.62, "eye_color": [90, 100, 140],
                 "eye": 0.20, "blush_color": [140, 140, 235], "blush": 0.20, "skin": 0.30},
    "女团紫":   {"lip_color": [160, 60, 190], "lip": 0.55, "eye_color": [130, 95, 150],
                 "eye": 0.28, "blush_color": [170, 130, 235], "blush": 0.25, "skin": 0.32},
    "欧美深邃": {"lip_color": [90, 95, 170],  "lip": 0.45, "eye_color": [45, 55, 85],
                 "eye": 0.55, "blush_color": [110, 140, 200], "blush": 0.15, "skin": 0.25},
    "清冷白开水": {"lip_color": [120, 110, 200], "lip": 0.35, "eye_color": [140, 140, 165],
                 "eye": 0.12, "blush_color": [160, 155, 235], "blush": 0.12, "skin": 0.35},
    "蜜桃奶油": {"lip_color": [105, 130, 235], "lip": 0.50, "eye_color": [110, 130, 180],
                 "eye": 0.20, "blush_color": [125, 160, 245], "blush": 0.38, "skin": 0.34},
    "姨妈色":   {"lip_color": [70, 30, 140],  "lip": 0.72, "eye_color": [90, 70, 110],
                 "eye": 0.25, "blush_color": [130, 110, 190], "blush": 0.12, "skin": 0.28},
    "南瓜色":   {"lip_color": [65, 110, 215], "lip": 0.55, "eye_color": [80, 110, 170],
                 "eye": 0.30, "blush_color": [110, 150, 235], "blush": 0.25, "skin": 0.30},
    "玫瑰豆沙": {"lip_color": [110, 90, 190], "lip": 0.50, "eye_color": [105, 95, 150],
                 "eye": 0.22, "blush_color": [145, 130, 220], "blush": 0.22, "skin": 0.30},
    "冷茶色":   {"lip_color": [115, 110, 180],"lip": 0.48, "eye_color": [95, 100, 125],
                 "eye": 0.28, "blush_color": [140, 140, 210], "blush": 0.15, "skin": 0.30},
    "雾面脏橘": {"lip_color": [80, 115, 205], "lip": 0.52, "eye_color": [75, 105, 160],
                 "eye": 0.32, "blush_color": [115, 150, 230], "blush": 0.22, "skin": 0.30},
}

_active_style: str = "自然裸妆"


def _scan_refs() -> list:
    out = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        out += [f.stem for f in MAKEUP_DIR.glob(ext)]
    return sorted(set(out))


def _load_ref(name: str):
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = MAKEUP_DIR / f"{name}{ext}"
        if p.exists():
            arr = np.frombuffer(p.read_bytes(), np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return None


# ── 工具 ─────────────────────────────────────────────────────────────
def b64_to_img(b64: str) -> np.ndarray:
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    arr = np.frombuffer(base64.b64decode(b64), np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def img_to_b64(img: np.ndarray, quality=92) -> str:
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode()


# ── 请求模型 ─────────────────────────────────────────────────────────
class MakeupRequest(BaseModel):
    source_image: str            # 待上妆人脸（b64）
    style:        str = ""       # 预设名或已上传参考名（空=当前激活）
    ref_image:    str = ""       # 参考妆图 b64（优先于 style）
    params:       dict = {}      # 显式覆盖 spec 字段（最高优先）
    debug:        bool = False   # 附掩码可视化


class ExtractRequest(BaseModel):
    image: str


# ── 路由 ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "makeup-api", "status": "running", "backend": "regional",
            "model_loaded": _get_landmarker() is not None,
            "ui": "http://127.0.0.1:8004/ui"}


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _get_landmarker() is not None,
            "backend": "regional"}


@app.get("/makeup_styles")
def list_styles():
    """detail=预设完整色彩规范（BGR），供直播妆容层 UI「从预设取色」一键同步——
    离线定妆与直播层同一套颜色单一真相。"""
    return {"presets": list(PRESETS.keys()), "refs": _scan_refs(),
            "active": _active_style, "detail": PRESETS}


@app.post("/makeup_styles/activate")
def activate_style(data: dict):
    global _active_style
    name = (data.get("name") or "").strip()
    if name not in PRESETS and name not in _scan_refs():
        raise HTTPException(404, f"找不到妆容: {name}")
    _active_style = name
    return {"ok": True, "active": _active_style}


@app.post("/makeup_styles/upload")
def upload_style(data: dict):
    name = (data.get("name") or "").strip()
    img64 = data.get("image") or ""
    if not name or not img64:
        raise HTTPException(400, "需要 name 和 image")
    if "," in img64:
        img64 = img64.split(",", 1)[1]
    (MAKEUP_DIR / f"{name}.jpg").write_bytes(base64.b64decode(img64))
    return {"ok": True, "refs": _scan_refs()}


@app.post("/makeup_extract")
def makeup_extract(req: ExtractRequest):
    img = b64_to_img(req.image)
    if img is None:
        raise HTTPException(400, "图片解码失败")
    try:
        return {"ok": True, **extract_makeup(img)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/makeup_transfer")
def makeup_transfer(req: MakeupRequest):
    t0 = time.time()
    img = b64_to_img(req.source_image)
    if img is None:
        raise HTTPException(400, "源图解码失败")

    # spec 解析优先级：params > ref_image > style(预设/参考名) > 激活样式
    spec: dict = {}
    style_used = ""
    if req.ref_image:
        ref = b64_to_img(req.ref_image)
        if ref is None:
            raise HTTPException(400, "参考图解码失败")
        try:
            ext = extract_makeup(ref)
        except ValueError as e:
            raise HTTPException(400, f"参考图: {e}")
        spec = {k: v for k, v in ext.items() if v}
        spec.update({"lip": 0.55, "eye": 0.30, "blush": 0.28, "skin": 0.30})
        style_used = "ref_image"
    else:
        name = req.style.strip() or _active_style
        if name in PRESETS:
            spec = dict(PRESETS[name]); style_used = name
        else:
            ref = _load_ref(name)
            if ref is not None:
                try:
                    ext = extract_makeup(ref)
                except ValueError as e:
                    raise HTTPException(400, f"参考妆图「{name}」: {e}")
                spec = {k: v for k, v in ext.items() if v}
                spec.update({"lip": 0.55, "eye": 0.30, "blush": 0.28, "skin": 0.30})
                style_used = name
            elif not req.params:
                raise HTTPException(404, f"找不到妆容: {name}")
    if req.params:
        spec.update({k: v for k, v in req.params.items() if v is not None})
        style_used = style_used or "params"

    try:
        out, applied = apply_makeup(img, spec)
    except ValueError as e:
        raise HTTPException(400, str(e))

    resp = {"result_image": img_to_b64(out),
            "elapsed_ms": int((time.time() - t0) * 1000),
            "style": style_used, "applied": applied}
    if req.debug:
        pts = _detect_landmarks(img)
        masks = _region_masks(img, pts)
        vis = img.copy().astype(np.float32)
        for key, col in (("lips", (0, 0, 255)), ("shadow", (255, 0, 200)),
                         ("blush", (0, 200, 255)), ("skin", (0, 255, 0))):
            m = masks[key][..., None] * 0.45
            vis = vis * (1 - m) + np.float32(col) * m
        resp["debug_masks"] = img_to_b64(vis.astype(np.uint8))
    _log(f"[MakeupAPI] 上妆完成 {resp['elapsed_ms']}ms style={style_used} "
         f"applied={list(applied.keys())}")
    return resp


# ── 极简 UI ──────────────────────────────────────────────────────────
@app.get("/ui", response_class=HTMLResponse)
def makeup_ui():
    return """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><title>妆容定妆</title>
<style>
 body{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px;max-width:760px;margin:auto}
 h1{color:#e94560;text-align:center;font-size:22px}
 .card{background:#16213e;border-radius:12px;padding:16px;margin:12px 0}
 select,button,input{background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;padding:7px 12px;font-size:14px}
 button{cursor:pointer}button:hover{background:#e94560}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}
 img{max-width:100%;border-radius:8px}
 .cols{display:flex;gap:12px}.cols>div{flex:1;text-align:center}
 .muted{color:#889;font-size:12px}
</style></head><body>
<h1>💄 妆容定妆（离线 · 1~3s/张）</h1>
<div class="card">
 <div class="row"><input type="file" id="src" accept="image/*">
  <select id="style"></select>
  <label class="muted"><input type="checkbox" id="dbg"> 显示区域</label>
  <button onclick="run()">上妆</button></div>
 <div class="muted">或上传参考妆图（提取唇色/眼影/腮红后套用）：<input type="file" id="ref" accept="image/*"></div>
</div>
<div class="card cols"><div><div class="muted">原图</div><img id="a"></div>
 <div><div class="muted">结果</div><img id="b"></div></div>
<script>
const $=id=>document.getElementById(id);
async function loadStyles(){const d=await fetch('/makeup_styles').then(r=>r.json());
 $('style').innerHTML=[...d.presets,...d.refs].map(s=>`<option ${s===d.active?'selected':''}>${s}</option>`).join('')}
loadStyles();
function b64(f){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)})}
async function run(){
 const f=$('src').files[0]; if(!f){alert('先选人脸图');return}
 const body={source_image:await b64(f), style:$('style').value, debug:$('dbg').checked};
 const rf=$('ref').files[0]; if(rf) body.ref_image=await b64(rf);
 $('a').src=URL.createObjectURL(f);
 const d=await fetch('/makeup_transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
 if(d.result_image) $('b').src='data:image/jpeg;base64,'+(body.debug?d.debug_masks:d.result_image);
 else alert(JSON.stringify(d).slice(0,300));
}
</script></body></html>"""


if __name__ == "__main__":
    _log("[MakeupAPI] 启动 妆容定妆服务 :8004 ...")
    _get_landmarker()                      # 预热（3.7MB 模型，秒级）
    uvicorn.run(app, host="0.0.0.0", port=8004, log_level="warning")
