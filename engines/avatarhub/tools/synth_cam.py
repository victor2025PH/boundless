# -*- coding: utf-8 -*-
"""synth_cam.py — 合成运动 MJPEG 摄像头源（SWAP_AUTO_QUALITY 无人标定用）

把一张人像照片合成为"平移/缩放/轻旋转+亮度起伏"的连续运动画面，经 MJPEG HTTP 吐出，
`realtime_stream.py --source http://127.0.0.1:8087/stream` 即可把它当摄像头用——
换脸链路(检测/贴合/增强)吃到的帧内容与真人坐镜头前等价（有脸、在动、光照微变），
从而把「用户在摄像头前 15 分钟」的标定环节替换为全自动。

用法:
  python tools/synth_cam.py                     # 默认 _snap_raw.jpg，8087 端口，720p@15fps
  python tools/synth_cam.py --photo _ldh720.jpg --port 8087 --fps 15
端点:
  GET /stream   multipart MJPEG
  GET /health   {"ok":true,...}
"""
import argparse
import io
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_latest = b""
_meta = {"frames": 0}


def _load_photo(path: str, w: int, h: int) -> np.ndarray:
    # cv2.imread 不认 Windows 中文路径 → 字节流 + imdecode
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        raise SystemExit(f"读不到照片: {path}")
    # 人像等比缩到画面高的 95% 居中，1.25x 画布四周边缘复制——竖版照片 cover-crop 会把脸推出画
    scale = (h * 0.95) / img.shape[0]
    iw, ih = max(1, int(img.shape[1] * scale)), int(img.shape[0] * scale)
    img = cv2.resize(img, (iw, ih))
    cw, ch = int(w * 1.25), int(h * 1.25)
    left = max(0, (cw - iw) // 2)
    top = max(0, (ch - ih) // 2)
    return cv2.copyMakeBorder(img, top, max(0, ch - ih - top),
                              left, max(0, cw - iw - left), cv2.BORDER_REPLICATE)


def synth_loop(photo: np.ndarray, w: int, h: int, fps: int):
    """正弦驱动的平移/缩放/旋转/亮度 → 模拟说话时的头部小幅运动。"""
    global _latest
    ph, pw = photo.shape[:2]
    cx, cy = pw / 2, ph / 2
    t0 = time.time()
    period = 1.0 / max(fps, 1)
    while True:
        t = time.time() - t0
        # 幅度经验值：±3% 平移、±4% 缩放、±2° 旋转 —— 贴近坐姿说话的自然晃动
        dx = 0.03 * pw * np.sin(2 * np.pi * t / 6.1)
        dy = 0.02 * ph * np.sin(2 * np.pi * t / 4.3 + 1.0)
        zoom = 1.0 + 0.04 * np.sin(2 * np.pi * t / 8.7 + 2.0)
        ang = 2.0 * np.sin(2 * np.pi * t / 5.6 + 0.5)
        M = cv2.getRotationMatrix2D((cx + dx, cy + dy), ang, zoom)
        frame = cv2.warpAffine(photo, M, (pw, ph), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
        # 中心裁切到目标分辨率
        x0 = int((pw - w) / 2 + dx / 2)
        y0 = int((ph - h) / 2 + dy / 2)
        x0 = max(0, min(pw - w, x0)); y0 = max(0, min(ph - h, y0))
        frame = frame[y0:y0 + h, x0:x0 + w]
        # 亮度微起伏（室内光照/屏幕反光）
        gain = 1.0 + 0.05 * np.sin(2 * np.pi * t / 7.9)
        frame = cv2.convertScaleAbs(frame, alpha=gain, beta=0)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with _lock:
                _latest = jpg.tobytes()
                _meta["frames"] += 1
        time.sleep(period)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            body = ('{"ok":true,"frames":%d}' % _meta["frames"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/stream"):
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                with _lock:
                    jpg = _latest
                if jpg:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                time.sleep(1 / 15)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", default=str(BASE / "_ldh720.jpg"))
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    photo_path = args.photo if Path(args.photo).exists() else str(BASE / "_ldh.jpg")
    photo = _load_photo(photo_path, args.width, args.height)
    threading.Thread(target=synth_loop, args=(photo, args.width, args.height, args.fps),
                     daemon=True).start()
    print(f"[SynthCam] {photo_path} -> http://127.0.0.1:{args.port}/stream "
          f"({args.width}x{args.height}@{args.fps})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
