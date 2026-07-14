# -*- coding: utf-8 -*-
"""视频换妆:对真实人物视频逐帧上妆(makeup_api 8004,CPU),做「左原图/右上妆」分屏,
强度加大到肉眼可见。修复"静图看不出区别"——用真人动态视频 + 明显妆效。
用法: facefusion python demo_record/gen_video_makeup.py --in clip.mp4 --out out.mp4 --style 复古红唇
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
MK = "http://127.0.0.1:8004/makeup_transfer"

# 加强妆(比预设更浓,肉眼可见):唇/眼/腮/肤 强度显著提高
STRONG = {
    "复古红唇": {"lip_color": [48, 28, 175], "lip": 0.85, "eye_color": [70, 70, 100],
              "eye": 0.42, "blush_color": [120, 120, 220], "blush": 0.40, "skin": 0.40},
    "元气桃花": {"lip_color": [120, 90, 230], "lip": 0.80, "eye_color": [140, 120, 210],
              "eye": 0.40, "blush_color": [150, 120, 245], "blush": 0.55, "skin": 0.42},
    "烟熏冷艳": {"lip_color": [90, 95, 165], "lip": 0.70, "eye_color": [55, 50, 60],
              "eye": 0.72, "blush_color": [130, 130, 200], "blush": 0.30, "skin": 0.38},
}


def makeup_frame(bgr, params):
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    b64 = base64.b64encode(buf.tobytes()).decode()
    body = {"source_image": b64, "params": params}
    req = urllib.request.Request(MK, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=30))
        rb = d.get("result_image", "")
        if rb:
            arr = np.frombuffer(base64.b64decode(rb), np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        pass
    return bgr   # 失败该帧退原图(无脸帧也走这条,不闪)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--style", default="复古红唇")
    ap.add_argument("--max-sec", type=float, default=8.0)
    args = ap.parse_args()
    params = STRONG.get(args.style, STRONG["复古红唇"])

    ff = "ffmpeg"
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,r_frame_rate",
                            "-of", "default=noprint_wrappers=1:nokey=1", args.src],
                           capture_output=True, text=True).stdout.split()
    w, h = int(probe[0]), int(probe[1])
    num, den = probe[2].split("/"); fps = float(num) / float(den)
    maxf = int(args.max_sec * fps)

    dec = subprocess.Popen([ff, "-v", "error", "-t", str(args.max_sec), "-i", args.src,
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE, bufsize=10 ** 8)
    noaudio = os.path.abspath(args.dst) + ".na.mp4"
    enc = subprocess.Popen([ff, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{w*2}x{h}", "-r", f"{fps}", "-i", "-",
                            "-c:v", "libx264", "-crf", "19", "-preset", "medium",
                            "-pix_fmt", "yuv420p", noaudio], stdin=subprocess.PIPE)
    fb = w * h * 3
    n = 0
    while n < maxf:
        raw = dec.stdout.read(fb)
        if len(raw) < fb:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
        made = makeup_frame(frame, params)
        # 左原/右妆分屏 + 中缝亮线
        combo = np.hstack([frame, made])
        cv2.line(combo, (w, 0), (w, h), (238, 211, 34), 3)
        enc.stdin.write(np.ascontiguousarray(combo).tobytes())
        n += 1
        if n % 30 == 0:
            print(f"  …{n}/{maxf} 帧")
    enc.stdin.close(); dec.wait(); enc.wait()
    # 配原声(可选)——分屏是视觉演示,保留原声让画面不死
    out = os.path.abspath(args.dst)
    subprocess.run([ff, "-y", "-v", "error", "-i", noaudio, "-i", args.src,
                    "-map", "0:v", "-map", "1:a:0?", "-t", str(args.max_sec),
                    "-c:v", "copy", "-c:a", "aac", "-shortest", out], check=False)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        os.replace(noaudio, out)
    else:
        os.remove(noaudio)
    print("成片:", out, f"({n} 帧)")


if __name__ == "__main__":
    main()
