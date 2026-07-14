# -*- coding: utf-8 -*-
"""faceswap_video.py — 在一段真实视频上做「双人换脸」，保留原声。

场景：直播换脸演示片——大画面主播换成一张超级美女的脸，右上角"真人小窗"换成一张
      男人的脸。因为用的是真实拍摄的视频，两个窗口的动作/口型天生完美同步，
      只把脸替换掉，效果远胜 AI 生成（AI 做不到两窗逐帧对齐）。

引擎：直接复用本项目的换脸栈——InsightFace(buffalo_l 检测) + inswapper_128 换脸网
      + 可选 GFPGAN 人脸增强；CUDA/TensorRT 加速（facefusion conda 环境）。

用法（在 facefusion 环境跑）：
  python faceswap_video.py --input in.mp4 --output out.mp4 \
      --main-face faces/默认.jpg --corner-face faces/刘德华.jpg

  --main-face    大画面主脸要换成的人脸图（必填）
  --corner-face  右上角小窗要换成的人脸图（不填=只换主脸，其余脸不动）
  --corner       小窗所在角落：tr(右上,默认) tl tr br bl
  --no-enhance   关闭 GFPGAN 增强（更快，画质略糊，先验证用）
  --det-size     检测分辨率，默认 1280（小窗脸小，调大更易检出）

Windows 中文路径安全：视频读写全走 ffmpeg 管道（不经 cv2.VideoCapture，避免非 ASCII 路径失败）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
INSWAPPER = BASE / "Deep-Live-Cam" / "models" / "inswapper_128.onnx"
GFPGAN_MODEL = BASE / "GFPGANv1.4.pth"


def find_ffmpeg() -> str:
    import shutil
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    pkgs = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkgs.exists() else []
    if hits:
        return str(hits[-1])
    # facefusion 自带的 ffmpeg 兜底
    for c in (BASE / "facefusion", BASE):
        for h in c.glob("**/ffmpeg.exe"):
            return str(h)
    raise SystemExit("[错误] 找不到 ffmpeg")


def find_ffprobe(ff: str) -> str:
    if ff == "ffmpeg":
        return "ffprobe"
    cand = Path(ff).with_name("ffprobe.exe")   # 同目录取 ffprobe，别用 replace（会误伤目录名里的 ffmpeg）
    return str(cand) if cand.exists() else "ffprobe"


def imread_unicode(path: str) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"[错误] 读不出人脸图：{path}")
    return img


def probe(ffprobe: str, path: str) -> tuple[int, int, float]:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    w, h = int(lines[0]), int(lines[1])
    num, den = lines[2].split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return w, h, fps


def area(f) -> float:
    b = f.bbox
    return float((b[2] - b[0]) * (b[3] - b[1]))


def center(f) -> tuple[float, float]:
    b = f.bbox
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def in_corner(f, w: int, h: int, corner: str) -> bool:
    cx, cy = center(f)
    right, top = cx > w * 0.5, cy < h * 0.5
    return {
        "tr": right and top, "tl": (not right) and top,
        "br": right and (not top), "bl": (not right) and (not top),
    }[corner]


def main():
    ap = argparse.ArgumentParser(description="真实视频双人换脸（保留原声）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--main-face", required=True)
    ap.add_argument("--corner-face", default="")
    ap.add_argument("--corner", default="tr", choices=["tr", "tl", "br", "bl"])
    ap.add_argument("--no-enhance", action="store_true")
    ap.add_argument("--det-size", type=int, default=1280)
    ap.add_argument("--delogo", default="",
                    help="去水印矩形，格式 x:y:w:h；多个用分号隔开，如 6:1055:210:145;600:20:100:40")
    args = ap.parse_args()

    if not INSWAPPER.exists():
        raise SystemExit(f"[错误] 缺换脸模型：{INSWAPPER}")
    ff = find_ffmpeg()
    ffprobe = find_ffprobe(ff)

    # 让 onnxruntime CUDA EP 找到 torch 自带的 CUDA 运行库（与 faceswap_api 同款处理）
    try:
        import torch
        tlib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(tlib):
            os.add_dll_directory(tlib)
            os.environ["PATH"] = tlib + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

    import insightface
    from insightface.app import FaceAnalysis

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    print("[换脸] 加载检测模型 buffalo_l …")
    app = FaceAnalysis(name="buffalo_l", providers=providers)
    print("[换脸] 加载换脸网 inswapper_128 …")
    swapper = insightface.model_zoo.get_model(str(INSWAPPER), providers=providers)

    enhancer = None
    if not args.no_enhance and GFPGAN_MODEL.exists():
        try:
            from gfpgan import GFPGANer
            enhancer = GFPGANer(model_path=str(GFPGAN_MODEL), upscale=1, arch="clean",
                                channel_multiplier=2, bg_upsampler=None)
            print("[换脸] GFPGAN 人脸增强已启用")
        except Exception as e:
            print(f"[换脸] GFPGAN 不可用，跳过增强：{e}")

    def source_face(path: str):
        # 源脸图是人像裁剪，脸占比大 → 固定 640 检测（det_size 过大反而检不到小图上的大脸）
        app.prepare(ctx_id=0, det_size=(640, 640))
        faces = app.get(imread_unicode(path))
        if not faces:
            raise SystemExit(f"[错误] 人脸图里没检测到脸：{path}")
        return max(faces, key=area)

    src_main = source_face(args.main_face)
    src_corner = source_face(args.corner_face) if args.corner_face else None
    print(f"[换脸] 主脸←{Path(args.main_face).name}"
          + (f"，角落({args.corner})←{Path(args.corner_face).name}" if src_corner else "，仅换主脸"))

    # 视频帧里小窗人脸很小 → 用较大的 det_size 提升检出率
    app.prepare(ctx_id=0, det_size=(args.det_size, args.det_size))
    w, h, fps = probe(ffprobe, args.input)
    print(f"[换脸] 视频 {w}x{h} @ {fps:.2f}fps，开始逐帧处理 …")

    # 去水印：把每个 x:y:w:h 编成一条 delogo 滤镜，链在编码前（作用于换脸后的成片）
    enc_vf = []
    for box in (b.strip() for b in args.delogo.split(";") if b.strip()):
        try:
            x, y, bw, bh = (int(v) for v in box.split(":"))
            enc_vf.append(f"delogo=x={x}:y={y}:w={bw}:h={bh}")
        except Exception:
            raise SystemExit(f"[错误] --delogo 格式应为 x:y:w:h，收到：{box}")
    if enc_vf:
        print(f"[换脸] 去水印区：{args.delogo}")

    dec = subprocess.Popen(
        [ff, "-v", "error", "-i", args.input, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=10 ** 8)
    enc_cmd = [ff, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-", "-i", args.input,
               "-map", "0:v:0", "-map", "1:a:0?"]
    if enc_vf:
        enc_cmd += ["-vf", ",".join(enc_vf)]
    enc_cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", args.output]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    frame_bytes = w * h * 3
    n = swapped_main = swapped_corner = 0
    while True:
        raw = dec.stdout.read(frame_bytes)
        if len(raw) < frame_bytes:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3).copy()
        faces = app.get(frame)
        if faces:
            faces.sort(key=area, reverse=True)
            main_t = faces[0]
            frame = swapper.get(frame, main_t, src_main, paste_back=True)
            swapped_main += 1
            if src_corner is not None:
                cands = [f for f in faces[1:] if in_corner(f, w, h, args.corner)]
                if cands:
                    frame = swapper.get(frame, cands[0], src_corner, paste_back=True)
                    swapped_corner += 1
            if enhancer is not None:
                try:
                    _, _, frame = enhancer.enhance(frame, has_aligned=False,
                                                   only_center_face=False, paste_back=True)
                except Exception:
                    pass
        enc.stdin.write(frame.tobytes())
        n += 1
        if n % 30 == 0:
            print(f"  …{n} 帧（主脸换 {swapped_main}，角落换 {swapped_corner}）")

    enc.stdin.close()
    dec.wait()
    enc.wait()
    print(f"[换脸] 完成：共 {n} 帧，主脸 {swapped_main}，角落 {swapped_corner} → {args.output}")


if __name__ == "__main__":
    main()
