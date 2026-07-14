# -*- coding: utf-8 -*-
"""直播妆容层(_apply_live_makeup)离线验证：
从 faceswap_api.py 源码原样抽取函数（不 import 模块=不加载 GPU 模型），
用 mediapipe landmark 合成 insightface 风格 face 对象（kps/bbox/lm106 mouth 块），
真实人脸图上跑一遍，落盘可视对比 + 耗时。"""
import base64, json, re, sys, time, types, urllib.request, urllib.parse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import cv2
import numpy as np

SRC = Path(r"c:\模仿音色\faceswap_api.py").read_text(encoding="utf-8")
m = re.search(r"(_MAKEUP_WORK_SIDE.*?)\ndef poisson_blend_face", SRC, re.S)
assert m, "faceswap_api.py 中未找到 _apply_live_makeup"
ns = {"np": np, "cv2": cv2}
exec(m.group(1), ns)
_apply_live_makeup = ns["_apply_live_makeup"]
print("[harness] 已从 faceswap_api.py 抽取函数（与生产同源）")

# ── mediapipe 检测 → 合成 insightface 风格 face ────────────────────
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

buf = Path(r"c:\模仿音色\models\face_landmarker.task").read_bytes()
lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
    base_options=mp_python.BaseOptions(model_asset_buffer=buf), num_faces=1))

HUB = "http://127.0.0.1:9000"
d = json.load(urllib.request.urlopen(
    f"{HUB}/profiles/{urllib.parse.quote('Inside')}?include_face=true", timeout=10))
img = cv2.imdecode(np.frombuffer(base64.b64decode(d["face_b64"]), np.uint8), cv2.IMREAD_COLOR)
h, w = img.shape[:2]

res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                         data=np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))))
assert res.face_landmarks, "未检出人脸"
pts = np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]], np.float32)

LEFT_EYE = [33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246]
RIGHT_EYE = [362,382,381,380,374,373,390,249,263,466,388,387,386,385,384,398]
LIPS = [61,146,91,181,84,17,314,405,321,375,291,409,270,269,267,0,37,39,40,185,
        78,95,88,178,87,14,317,402,318,324,308,415,310,311,312,13,82,81,80,191]
kps = np.array([pts[LEFT_EYE].mean(0), pts[RIGHT_EYE].mean(0), pts[1],
                pts[61], pts[291]], np.float32)          # 眼/眼/鼻尖/嘴角/嘴角
face = types.SimpleNamespace(
    kps=kps,
    bbox=np.array([pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()]),
    # lm106 合约：函数只取 [52:72] 做凸包 → 前面填 52 个占位，后面放 20 个唇点
    landmark_2d_106=np.concatenate([np.tile(pts[1], (52, 1)), pts[LIPS[:20]]], 0))

OUT = Path(r"c:\模仿音色\logs\look_pack_impl_20260708")
spec = {"lip_color": [48, 28, 165], "lip": 0.55,
        "blush_color": [150, 130, 240], "blush": 0.30,
        "eye_color": [80, 70, 90], "eye": 0.30}

# 预热后计时（生产逐帧路径关心的是稳态耗时）
_ = _apply_live_makeup(img.copy(), [face], spec)
t0 = time.time()
N = 20
for _i in range(N):
    out = _apply_live_makeup(img.copy(), [face], spec)
ms = (time.time() - t0) * 1000 / N
def _save(p, im):        # cv2.imwrite 对中文路径静默失败 → imencode+write_bytes
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    Path(p).write_bytes(buf.tobytes())

_save(OUT / "03_直播妆容层_结果.jpg", out)
_save(OUT / "03_直播妆容层_对比.jpg", np.concatenate([img, out], axis=1))
print(f"[harness] 单帧上妆均耗时 {ms:.1f}ms（{img.shape[1]}x{img.shape[0]}, 唇+腮红+眼影）")
print(f"[harness] 产物: 03_直播妆容层_对比.jpg")
lm.close()
