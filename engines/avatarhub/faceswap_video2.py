# -*- coding: utf-8 -*-
"""faceswap_video2.py — 高质量真实视频换脸(+可选换声)，修 v1 三大问题：

  1) 抖动：对检测关键点做时序 EMA 平滑(说话头基本静止,平滑后贴脸稳定不抖)。
  2) 口型/画质：用 HyperSwap-256(FaceFusion 高清核 + 模型脸形掩码贴回 + LAB 校色)
     取代 inswapper_128,保留目标表情、口型细节更清晰、无灰边。
  3) 换声：--rvc 把原声过 RVC 变声(同词同时长 → 口型仍对得上),实现"换脸又换声"。

用法(facefusion 环境)：
  python faceswap_video2.py --input in.mp4 --output out.mp4 --main-face faces/刘德华.jpg
  # 加换声(音色由 rvc .pth 决定,口型不变)：
  python faceswap_video2.py --input in.mp4 --output out.mp4 --main-face 刘德华.jpg \
      --rvc "D:/projects/模仿音色/rvc/assets/weights/weights/CN_Chinese_Narrator.pth" --rvc-pitch 0
  --no-enhance 关 GFPGAN(更快)；--smooth 0.6 关键点平滑系数(0=不平滑,越大越稳但越钝)。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
HYPERSWAP = BASE / "models" / "hyperswap_1a_256.onnx"
INSWAPPER = BASE / "Deep-Live-Cam" / "models" / "inswapper_128.onnx"
GFPGAN_MODEL = BASE / "GFPGANv1.4.pth"
RVC_API = "http://127.0.0.1:6242"

_ARCFACE_128_TEMPLATE = np.array([
    [0.36167656, 0.40387734], [0.63696719, 0.40235469], [0.50019687, 0.56044219],
    [0.38710391, 0.72160547], [0.61507734, 0.72034453]], dtype=np.float32)


def find_ffmpeg() -> str:
    import shutil
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    pkgs = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkgs.exists() else []
    if hits:
        return str(hits[-1])
    raise SystemExit("[错误] 找不到 ffmpeg")


def imread_unicode(path: str) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"[错误] 读不出图：{path}")
    return img


def probe(ff: str, path: str):
    fp = ff.replace("ffmpeg", "ffprobe") if ff != "ffmpeg" else "ffprobe"
    out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,r_frame_rate",
                          "-of", "default=noprint_wrappers=1:nokey=1", path],
                         capture_output=True, text=True)
    w, h, rate = [l.strip() for l in out.stdout.splitlines() if l.strip()][:3]
    num, den = rate.split("/")
    return int(w), int(h), (float(num) / float(den) if float(den) else 30.0)


def _box_feather_mask(size):
    h, w = size
    blur = int(w * 0.5 * 0.3)
    ba = max(blur // 2, 1)
    m = np.ones((h, w), np.float32)
    m[:ba, :] = 0; m[-ba:, :] = 0; m[:, :ba] = 0; m[:, -ba:] = 0
    if blur > 0:
        m = cv2.GaussianBlur(m, (0, 0), blur * 0.25)
    return m


def _lab_color_transfer(src, ref, mask):
    m = mask > 0.5
    if m.sum() < 50:
        return src
    s = cv2.cvtColor(src, cv2.COLOR_BGR2LAB).astype(np.float32)
    r = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(np.float32)
    out = s.copy()
    for c in range(3):
        ss = s[:, :, c][m].std() + 1e-6; sm = s[:, :, c][m].mean()
        rs = r[:, :, c][m].std() + 1e-6; rm = r[:, :, c][m].mean()
        out[:, :, c] = (s[:, :, c] - sm) / ss * rs + rm
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def _paste_crop_back(frame, crop, mask, M):
    h, w = frame.shape[:2]
    ch, cw = crop.shape[:2]
    IM = cv2.invertAffineTransform(M)
    pts = np.array([[0, 0], [cw, 0], [cw, ch], [0, ch]], np.float32)
    proj = np.hstack([pts, np.ones((4, 1), np.float32)]) @ IM.T
    x1, y1 = np.clip(np.floor(proj.min(0)).astype(int), 0, [w, h])
    x2, y2 = np.clip(np.ceil(proj.max(0)).astype(int), 0, [w, h])
    if x2 <= x1 or y2 <= y1:
        return frame
    PM = IM.copy(); PM[0, 2] -= x1; PM[1, 2] -= y1
    pw, ph = int(x2 - x1), int(y2 - y1)
    inv_mask = cv2.warpAffine(mask, PM, (pw, ph)).clip(0, 1)[..., None]
    inv_crop = cv2.warpAffine(crop, PM, (pw, ph), borderMode=cv2.BORDER_REPLICATE)
    region = frame[y1:y2, x1:x2].astype(np.float32)
    frame[y1:y2, x1:x2] = (region * (1 - inv_mask) + inv_crop.astype(np.float32) * inv_mask).astype(frame.dtype)
    return frame


class HyperSwap:
    """HyperSwap-256 换脸(移植自 faceswap_api：模型脸形掩码贴回 + LAB 校色,消灰边)。"""
    def __init__(self, model_file, providers):
        import onnxruntime as ort
        self.s = ort.InferenceSession(str(model_file), providers=providers)
        self.inames = [i.name for i in self.s.get_inputs()]
        self.onames = [o.name for o in self.s.get_outputs()]
        tgt = next(i.shape for i in self.s.get_inputs() if i.name == "target")
        self.S = int(tgt[2])
        self.has_mask = len(self.onames) > 1

    @staticmethod
    def _stretch(mm):
        mm = mm.clip(0, 1).astype(np.float32)
        return (cv2.GaussianBlur(mm, (0, 0), 5).clip(0.5, 1) - 0.5) * 2

    def get(self, img, kps, src_emb):
        S = self.S
        tpl = _ARCFACE_128_TEMPLATE * S
        M = cv2.estimateAffinePartial2D(np.asarray(kps, np.float32), tpl,
                                        method=cv2.RANSAC, ransacReprojThreshold=100)[0]
        aimg = cv2.warpAffine(img, M, (S, S), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA)
        blob = ((aimg[:, :, ::-1].astype(np.float32) / 255.0 - 0.5) / 0.5)
        blob = np.expand_dims(blob.transpose(2, 0, 1), 0).astype(np.float32)
        latent = np.asarray(src_emb, np.float32).reshape(1, -1)
        feed = {self.inames[0]: latent, self.inames[1]: blob}
        if self.has_mask:
            preds = self.s.run(None, feed)
            pred, mm = preds[0][0], preds[1][0]
            mm = mm[0] if mm.ndim == 3 else mm
        else:
            pred, mm = self.s.run([self.onames[0]], feed)[0][0], None
        out = pred.transpose(1, 2, 0)
        out = (out * 0.5 + 0.5).clip(0, 1)
        fake = (out[:, :, ::-1] * 255).astype(np.uint8)
        mask = np.minimum(_box_feather_mask((S, S)),
                          self._stretch(mm) if mm is not None else _box_feather_mask((S, S)))
        try:
            fake = _lab_color_transfer(fake, aimg, mask)
        except Exception:
            pass
        return _paste_crop_back(img.copy(), fake, mask, M)


def rvc_convert(ff: str, in_wav_bytes: bytes, pth: str, pitch: int) -> bytes:
    """把 WAV 字节过 RVC 变声,返回 WAV 字节。失败抛异常。"""
    a = base64.b64encode(in_wav_bytes).decode()
    body = {"audio_base64": a, "pth_path": pth, "pitch": pitch,
            "index_rate": 0.5, "f0method": "rmvpe", "protect": 0.33}
    req = urllib.request.Request(RVC_API + "/convert", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=120))
    return base64.b64decode(d["audio_base64"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--main-face", required=True)
    ap.add_argument("--no-enhance", action="store_true")
    ap.add_argument("--det-size", type=int, default=1024)
    ap.add_argument("--smooth", type=float, default=0.6,
                    help="关键点时序 EMA 平滑系数(0=不平滑;0.6=稳且跟手)")
    ap.add_argument("--engine", choices=["hyperswap", "inswapper"], default="hyperswap")
    ap.add_argument("--rvc", default="", help="RVC .pth 路径;给了则把原声变声")
    ap.add_argument("--rvc-pitch", type=int, default=0)
    args = ap.parse_args()

    ff = find_ffmpeg()
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
    print("[换脸] 加载检测 buffalo_l …")
    app = FaceAnalysis(name="buffalo_l", providers=providers)
    app.prepare(ctx_id=0, det_size=(640, 640))
    src_faces = app.get(imread_unicode(args.main_face))
    if not src_faces:
        raise SystemExit(f"[错误] 源脸图无脸：{args.main_face}")
    src = max(src_faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    src_emb = src.normed_embedding

    if args.engine == "hyperswap" and HYPERSWAP.exists():
        print(f"[换脸] 换脸网 HyperSwap-256")
        swapper = HyperSwap(HYPERSWAP, providers)
        use_hyper = True
    else:
        print(f"[换脸] 换脸网 inswapper_128(回退)")
        swapper = insightface.model_zoo.get_model(str(INSWAPPER), providers=providers)
        use_hyper = False

    enhancer = None
    if not args.no_enhance and GFPGAN_MODEL.exists():
        try:
            from gfpgan import GFPGANer
            enhancer = GFPGANer(model_path=str(GFPGAN_MODEL), upscale=1, arch="clean",
                                channel_multiplier=2, bg_upsampler=None)
            print("[换脸] GFPGAN 增强已启用")
        except Exception as e:
            print(f"[换脸] GFPGAN 不可用：{e}")

    app.prepare(ctx_id=0, det_size=(args.det_size, args.det_size))
    w, h, fps = probe(ff, args.input)
    print(f"[换脸] {w}x{h}@{fps:.2f} smooth={args.smooth} 开始逐帧 …")

    dec = subprocess.Popen([ff, "-v", "error", "-i", args.input, "-f", "rawvideo",
                            "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE, bufsize=10 ** 8)
    enc = subprocess.Popen([ff, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-",
                            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                            "-pix_fmt", "yuv420p", os.path.abspath(args.output) + ".noaudio.mp4"],
                           stdin=subprocess.PIPE)

    fb = w * h * 3
    n = swapped = 0
    ema_kps = None       # 关键点 EMA(消抖核心)
    ema_bbox = None
    a = float(args.smooth)
    while True:
        raw = dec.stdout.read(fb)
        if len(raw) < fb:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3).copy()
        faces = app.get(frame)
        if faces:
            f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
            kps = np.asarray(f.kps, np.float32)
            bbox = np.asarray(f.bbox, np.float32)
            # 时序 EMA：位置突变(切镜/大幅移动)时重置,否则平滑
            if ema_kps is not None and np.linalg.norm(kps - ema_kps) < 0.12 * (bbox[2] - bbox[0]) * 5:
                ema_kps = a * ema_kps + (1 - a) * kps
                ema_bbox = a * ema_bbox + (1 - a) * bbox
            else:
                ema_kps, ema_bbox = kps, bbox
            skps = ema_kps
            if use_hyper:
                frame = swapper.get(frame, skps, src_emb)
            else:
                f.kps = skps
                frame = swapper.get(frame, f, src, paste_back=True)
            if enhancer is not None:
                try:
                    _, _, frame = enhancer.enhance(frame, has_aligned=False,
                                                   only_center_face=True, paste_back=True)
                except Exception:
                    pass
            swapped += 1
        else:
            ema_kps = None
        enc.stdin.write(frame.tobytes())
        n += 1
        if n % 30 == 0:
            print(f"  …{n} 帧(换 {swapped})")
    enc.stdin.close(); dec.wait(); enc.wait()
    noaudio = os.path.abspath(args.output) + ".noaudio.mp4"

    # ── 音轨：--rvc 则原声变声后合入,否则保留原声 ──
    out = os.path.abspath(args.output)
    if args.rvc:
        print(f"[换声] 提取原声 → RVC({Path(args.rvc).name}) …")
        wav = subprocess.run([ff, "-v", "error", "-i", args.input, "-vn", "-ar", "44100",
                              "-ac", "1", "-f", "wav", "-"], capture_output=True).stdout
        try:
            conv = rvc_convert(ff, wav, args.rvc, args.rvc_pitch)
            tmp_wav = out + ".rvc.wav"
            open(tmp_wav, "wb").write(conv)
            subprocess.run([ff, "-y", "-v", "error", "-i", noaudio, "-i", tmp_wav,
                            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                            "-shortest", out], check=True)
            os.remove(tmp_wav)
            print("[换声] 完成(变声音轨)")
        except Exception as e:
            print(f"[换声] RVC 失败,保留原声：{e}")
            subprocess.run([ff, "-y", "-v", "error", "-i", noaudio, "-i", args.input,
                            "-map", "0:v", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
                            "-shortest", out], check=True)
    else:
        subprocess.run([ff, "-y", "-v", "error", "-i", noaudio, "-i", args.input,
                        "-map", "0:v", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
                        "-shortest", out], check=True)
    os.remove(noaudio)
    print(f"[换脸] 完成：{n} 帧,换 {swapped} → {out}")


if __name__ == "__main__":
    main()
