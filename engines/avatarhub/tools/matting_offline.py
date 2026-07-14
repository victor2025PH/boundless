# -*- coding: utf-8 -*-
"""录播增强：MatAnyone 2 影视级离线抠像（ADR-12-03，方案第③阶段）。

用法（facefusion 环境）：
  python tools/matting_offline.py -i 录像.mp4 --bg bg_images/游艇.webp
  python tools/matting_offline.py -i 录像.mp4 --bg green          # 绿幕底(后期再键控)
  python tools/matting_offline.py -i 录像.mp4 --bg none           # 只出 alpha 视频

产物（默认 logs/matting_offline/<名字>_*.mp4）：
  <名字>_com.mp4    合成结果（含原音轨）
  <名字>_pha.mp4    alpha 通道（灰度，供剪辑软件二次合成）
  <名字>_rgba.mov   --export prores 时：ProRes 4444 前景+alpha（剪辑软件直接用）

与官方 inference 的差异（为生产录播设计）：
  · 流式处理：官方一次性把全片读进内存(5min@720p ≈ 100GB 张量,直接爆)；
    这里逐帧读→逐帧写，内存占用与时长无关；
  · 首帧掩码自动化：官方要手工提供 PNG；这里默认用本机 RVM(TorchScript)自举——
    RVM 出首帧 α → 二值化 → 喂 MatAnyone 2 记忆库，全程零人工；
  · 背景合成 + 音轨回贴一条龙（官方只出绿底 fgr + pha,无音频）；
  · 显存护栏：开跑前查空闲显存，直播中(<3G 空闲)默认拒跑,--force 才放行。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import cv2

BASE = Path(r"C:\模仿音色")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "vendor" / "MatAnyone2"))

RVM_MODEL = BASE / "models" / "rvm_mobilenetv3_fp16.torchscript"
MA2_MODEL = BASE / "models" / "matanyone2.pth"


class Progress:
    """机读进度落盘（原子替换）：hub 轮询此文件驱动前端进度条。path 为空则静默。"""

    def __init__(self, path: str):
        self.path = path
        self.data = {"state": "loading", "n": 0, "total": 0, "ms": 0.0,
                     "eta_s": 0, "error": "", "outputs": []}
        self._last = 0.0

    def update(self, throttle=1.0, **kw):
        if not self.path:
            return
        self.data.update(kw)
        now = time.time()
        if throttle and now - self._last < throttle and self.data["state"] == "running":
            return
        self._last = now
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass


def gpu_free_mb() -> int:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return 1 << 30


def imread_any(path: str):
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


def cover_resize(img, w, h):
    ih, iw = img.shape[:2]
    scale = max(w / iw, h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x0, y0 = (nw - w) // 2, (nh - h) // 2
    return np.ascontiguousarray(img[y0:y0 + h, x0:x0 + w])


def auto_first_mask(frame_bgr, torch):
    """RVM 自举首帧掩码：α>0.5 二值化（等价官方 GUI 里人工/SAM 给的 binary mask）。"""
    import io
    with open(RVM_MODEL, "rb") as f:
        net = torch.jit.load(io.BytesIO(f.read()), map_location="cuda").eval()
    with torch.inference_mode():
        t = torch.from_numpy(np.ascontiguousarray(frame_bgr)).cuda()
        src = t.permute(2, 0, 1)[None].flip(1).half().div_(255.0)
        ds = torch.tensor([0.375 if frame_bgr.shape[0] <= 720 else 0.25], device="cuda")
        _, pha, *_ = net(src, None, None, None, None, ds)
        m = (pha[0, 0].float().cpu().numpy() > 0.5).astype(np.uint8) * 255
    del net
    torch.cuda.empty_cache()
    if m.sum() == 0:
        raise RuntimeError("首帧未检测到人像(RVM α 全零)——请确认视频首帧有人,或用 --mask 手工给掩码")
    return m


def main():
    ap = argparse.ArgumentParser(description="MatAnyone 2 离线抠像(录播增强)")
    ap.add_argument("-i", "--input", required=True, help="输入视频(mp4/mov/avi)")
    ap.add_argument("-o", "--output", default=str(BASE / "logs" / "matting_offline"), help="输出目录")
    ap.add_argument("--bg", default="green", help="背景：图片/视频路径 | green | none(只出alpha)")
    ap.add_argument("--mask", default="auto", help="首帧人像掩码 PNG；auto=RVM 自举")
    ap.add_argument("--max-size", type=int, default=-1, help="短边超此值则降采样(如 1080)；-1 不限")
    ap.add_argument("--internal-size", type=int, default=720,
                    help="抠像内部处理短边(帧先降采样推理,alpha 再还原,合成/产物仍原生分辨率)。"
                         "1080p 原生算爆显存(溢出共享内存,5.5s/帧)→720 内部处理回到 720p 量级。"
                         "-1=原生分辨率(仅小视频/追求极限细节用)")
    ap.add_argument("--warmup", type=int, default=10, help="首帧预热迭代(官方默认 10)")
    ap.add_argument("--erode", type=int, default=10, help="首帧掩码收缩核")
    ap.add_argument("--dilate", type=int, default=10, help="首帧掩码膨胀核")
    ap.add_argument("--force", action="store_true", help="空闲显存<3G 也强行跑(可能影响直播,慎用)")
    ap.add_argument("--suffix", default="", help="输出文件名后缀")
    ap.add_argument("--export", default="mp4", choices=("mp4", "prores"),
                    help="prores=额外产出 ProRes 4444 前景+alpha 的 .mov(剪辑软件直接用)")
    ap.add_argument("--progress-file", default="", help="机读进度 JSON 落盘路径(hub 轮询用)")
    args = ap.parse_args()

    prog = Progress(args.progress_file)
    prog.update(throttle=0)
    try:
        _run(args, prog)
    except SystemExit as e:
        prog.update(throttle=0, state="error", error=str(e)[:200])
        raise
    except Exception as e:
        prog.update(throttle=0, state="error", error=f"{type(e).__name__}: {str(e)[:180]}")
        raise


def _run(args, prog):
    src_path = str(Path(args.input).resolve())
    if not os.path.exists(src_path):
        sys.exit(f"输入不存在: {src_path}")
    free = gpu_free_mb()
    if free < 3000 and not args.force:
        sys.exit(f"空闲显存仅 {free}MB(<3G)，疑似直播中——录播增强请停播后跑，或加 --force")

    cap = cv2.VideoCapture(src_path)
    if not cap.isOpened():
        sys.exit("视频打不开(编码不支持?)")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if not (1 <= fps <= 120):
        fps = 25.0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ok, first = cap.read()
    if not ok:
        sys.exit("读不到首帧")
    h, w = first.shape[:2]
    scale_out = None
    if args.max_size > 0 and min(h, w) > args.max_size:
        s = args.max_size / min(h, w)
        w, h = int(w * s) // 2 * 2, int(h * s) // 2 * 2
        scale_out = (w, h)
        first = cv2.resize(first, scale_out, interpolation=cv2.INTER_AREA)
    print(f"[in ] {src_path}  {w}x{h}@{fps:.1f}fps  ~{n_total}帧  空闲显存={free}MB")
    prog.update(throttle=0, total=n_total)

    import torch
    from matanyone2.utils.get_default_model import get_matanyone2_model
    from matanyone2.inference.inference_core import InferenceCore
    from matanyone2.utils.inference_utils import gen_dilate, gen_erosion

    # ── 首帧掩码 ────────────────────────────────────────────
    if args.mask == "auto":
        mask = auto_first_mask(first, torch)
        print(f"[mask] RVM 自举，人像占比 {mask.mean() / 255 * 100:.1f}%")
    else:
        g = cv2.imdecode(np.fromfile(args.mask, np.uint8), cv2.IMREAD_GRAYSCALE)
        if g is None:
            sys.exit(f"掩码读不出: {args.mask}")
        mask = cv2.resize(g, (w, h), interpolation=cv2.INTER_NEAREST)
    if args.dilate > 0:
        mask = gen_dilate(mask, args.dilate, args.dilate)
    if args.erode > 0:
        mask = gen_erosion(mask, args.erode, args.erode)

    # ── 背景源 ──────────────────────────────────────────────
    bg_mode, bg_img, bg_cap = args.bg, None, None
    if bg_mode not in ("green", "none"):
        p = str(Path(bg_mode).resolve())
        if p.lower().endswith((".mp4", ".webm", ".mov", ".avi", ".m4v", ".gif")):
            bg_cap = cv2.VideoCapture(p)
            if not bg_cap.isOpened():
                sys.exit(f"背景视频打不开: {p}")
        else:
            bg_img = imread_any(p)
            if bg_img is None:
                sys.exit(f"背景图读不出: {p}")
            bg_img = cover_resize(bg_img, w, h)

    def bg_frame():
        if bg_img is not None:
            return bg_img
        if bg_cap is not None:
            ok2, fr = bg_cap.read()
            if not ok2:
                bg_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok2, fr = bg_cap.read()
            return cover_resize(fr, w, h) if ok2 else np.full((h, w, 3), (0, 255, 0), np.uint8)
        return np.full((h, w, 3), (0, 255, 0), np.uint8)      # green

    # ── 高分辨率内部降采样：帧降到 internal-size 推理,alpha 还原到原尺寸 ──
    # (vendor 的 max_internal_size 在软掩码路径有维度 bug,故工具侧自做,不碰 vendor)
    inter = int(args.internal_size)
    proc_scale = None
    if 0 < inter < min(h, w):
        s = inter / min(h, w)
        proc_scale = (int(w * s) // 2 * 2, int(h * s) // 2 * 2)
        mask = cv2.resize(mask, proc_scale, interpolation=cv2.INTER_NEAREST)
        print(f"[model] 内部处理 {proc_scale[0]}x{proc_scale[1]}(产物仍 {w}x{h})")

    # ── 模型加载（141MB fp32 权重 + 记忆库）────────────────
    t0 = time.time()
    ma2 = get_matanyone2_model(str(MA2_MODEL), "cuda")
    proc = InferenceCore(ma2, cfg=ma2.cfg)
    print(f"[model] MatAnyone2 加载 {time.time() - t0:.1f}s")

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem + (f"_{args.suffix}" if args.suffix else "")
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    # ── 产物编码：裸帧管道直供 ffmpeg NVENC(显卡专用编码块,不占 CUDA 算力) ──
    # 旧链路 cv2 mp4v 临时文件 + libx264 二次转码,4K 下两个 writer 各吃几十 ms/帧还画质损两道；
    # 现一步到位(com 顺带直接混音轨),NVENC 不可用时回退 CPU x264(仍免临时文件)。
    def _nvenc_ok():
        try:
            r = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.2",
                                "-c:v", "h264_nvenc", "-f", "null", "-"],
                               capture_output=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False

    if _nvenc_ok():
        _venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    else:
        _venc = ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
        print("[warn] NVENC 不可用，产物编码回退 CPU x264", flush=True)

    final_com = "" if bg_mode == "none" else str(outdir / f"{stem}_com.mp4")
    pha_path = str(outdir / f"{stem}_pha.mp4")
    com_proc = None
    if final_com:
        com_proc = subprocess.Popen(
            [ff, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
             "-r", f"{fps:.3f}", "-i", "pipe:0", "-i", src_path,
             "-map", "0:v:0", "-map", "1:a:0?", *_venc, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", final_com],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pha_proc = subprocess.Popen(
        [ff, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}",
         "-r", f"{fps:.3f}", "-i", "pipe:0", *_venc, "-pix_fmt", "yuv420p", pha_path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _pipe_die(which: str):
        for p in (com_proc, pha_proc, ff_proc):
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        prog.update(throttle=0, state="error", error=f"{which} 编码管道中断(磁盘满或显卡编码器异常)")
        sys.exit(f"[err ] {which} 编码管道中断")

    # ProRes 4444 前景+alpha：BGRA 裸帧经管道喂 ffmpeg(prores_ks, yuva444p10le)。
    # cv2.VideoWriter 不支持 alpha 通道,故走子进程；straight alpha,剪辑软件通用。
    rgba_path, ff_proc = "", None
    if args.export == "prores":
        rgba_path = str(outdir / f"{stem}_rgba.mov")
        ff_proc = subprocess.Popen(
            [ff, "-y", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
             "-r", f"{fps:.3f}", "-i", "pipe:0", "-i", src_path,
             "-map", "0:v:0", "-map", "1:a:0?",
             "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
             "-c:a", "aac", "-shortest", rgba_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    peak = {"mb": 0}
    stop_watch = threading.Event()

    def _watch():
        while not stop_watch.is_set():
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=10)
                peak["mb"] = max(peak["mb"], int(r.stdout.strip().splitlines()[0]))
            except Exception:
                pass
            time.sleep(2)

    threading.Thread(target=_watch, daemon=True).start()

    mask_t = torch.from_numpy(mask).float().cuda()
    n_out, t_start = 0, time.time()
    a_prev, flick = None, []

    import torch.nn.functional as _F

    # 背景常量(图片/绿幕)只上传 GPU 一次；视频背景逐帧上传。合成在 GPU 混合：
    # 4K 下 CPU numpy 混合 ~2500 万像素浮点是主要瓶颈之一,GPU 化对所有分辨率零语义变化。
    _bg_gpu = {}

    def bg_tensor():
        if bg_img is not None:
            if "c" not in _bg_gpu:
                _bg_gpu["c"] = torch.from_numpy(bg_img).cuda().float()
            return _bg_gpu["c"]
        if bg_cap is not None:
            return torch.from_numpy(np.ascontiguousarray(bg_frame())).cuda().float()
        if "c" not in _bg_gpu:
            _bg_gpu["c"] = torch.from_numpy(np.full((h, w, 3), (0, 255, 0), np.uint8)).cuda().float()
        return _bg_gpu["c"]

    with torch.inference_mode(), torch.amp.autocast("cuda"):
        frame, ti = first, 0
        while True:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if proc_scale:
                rgb = cv2.resize(rgb, proc_scale, interpolation=cv2.INTER_AREA)
            img_t = torch.from_numpy(rgb).cuda().permute(2, 0, 1).float().div_(255.0)
            if ti == 0:
                proc.step(img_t, mask_t, objects=[1])                 # 写入首帧记忆
                out_prob = proc.step(img_t, first_frame_pred=True)
                for _ in range(max(0, args.warmup - 1)):              # 首帧复读预热(官方套路)
                    out_prob = proc.step(img_t, first_frame_pred=True)
            else:
                out_prob = proc.step(img_t)
            alpha_t = proc.output_prob_to_mask(out_prob)
            if proc_scale:      # GPU 上双线性还原 alpha 到原生分辨率
                alpha_t = _F.interpolate(alpha_t[None, None].float(), size=(h, w),
                                         mode="bilinear", align_corners=False)[0, 0]
            alpha_t = alpha_t.float()
            alpha = alpha_t.cpu().numpy()

            a8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
            try:
                pha_proc.stdin.write(a8.tobytes())
            except OSError:
                _pipe_die("pha")
            if com_proc is not None:
                fg_g = torch.from_numpy(frame).cuda().float()
                a3_g = alpha_t[..., None]
                com = (fg_g * a3_g + bg_tensor() * (1.0 - a3_g)).to(torch.uint8).cpu().numpy()
                try:
                    com_proc.stdin.write(com.tobytes())
                except OSError:
                    _pipe_die("com")
            if ff_proc is not None:
                try:
                    ff_proc.stdin.write(np.dstack([frame, a8]).tobytes())
                except OSError:
                    print("[warn] ProRes 管道中断，放弃 rgba 导出", flush=True)
                    ff_proc, rgba_path = None, ""
            if a_prev is not None:
                band = (alpha > 0.02) & (alpha < 0.98)
                if band.any():
                    flick.append(float(np.abs(alpha - a_prev)[band].mean()))
            a_prev = alpha
            n_out += 1
            dt = (time.time() - t_start) / n_out
            prog.update(state="running", n=n_out, ms=round(dt * 1000),
                        eta_s=round(dt * max(0, n_total - n_out)))
            if n_out % 25 == 0:
                print(f"  {n_out}/{n_total}  {dt * 1000:.0f}ms/帧  剩余~{dt * max(0, n_total - n_out):.0f}s",
                      flush=True)
            ok, frame = cap.read()
            if not ok:
                break
            if scale_out:
                frame = cv2.resize(frame, scale_out, interpolation=cv2.INTER_AREA)
            ti += 1

    cap.release()
    if bg_cap is not None:
        bg_cap.release()
    ms = (time.time() - t_start) / max(1, n_out) * 1000
    stop_watch.set()
    prog.update(throttle=0, state="running", n=n_out, ms=round(ms))       # 封装期心跳
    for p, tag in ((pha_proc, "pha"), (com_proc, "com"), (ff_proc, "rgba")):
        if p is None:
            continue
        try:
            p.stdin.close()
            p.wait(timeout=600)
            bad = p.returncode != 0
        except Exception:
            bad = True
        if bad:
            if tag == "pha":
                pha_path = ""
            elif tag == "com":
                final_com = ""
            else:
                rgba_path = ""
            print(f"[warn] {tag} 编码收尾失败，产物缺失", flush=True)
    if rgba_path and not os.path.exists(rgba_path):
        rgba_path = ""

    fl = float(np.mean(flick)) if flick else 0.0
    outputs = [os.path.basename(p) for p in (final_com, pha_path, rgba_path) if p]
    prog.update(throttle=0, state="done", n=n_out, total=max(n_total, n_out),
                ms=round(ms), eta_s=0, outputs=outputs)
    print(f"[done] {n_out}帧  {ms:.0f}ms/帧({1000 / ms:.1f}fps)  峰值显存={peak['mb']}MB  "
          f"边带时域抖动={fl:.4f}")
    for p in (final_com, pha_path, rgba_path):
        if p:
            print(f"[out ] {p}")


def _warm_only():
    """Hub 启动预热：ResNet 骨干下载进 torch hub 缓存 + MatAnyone2 假帧推理。"""
    import torch
    from matanyone2.utils.get_default_model import get_matanyone2_model
    from matanyone2.inference.inference_core import InferenceCore
    t0 = time.time()
    ma2 = get_matanyone2_model(str(MA2_MODEL), "cuda")
    proc = InferenceCore(ma2, cfg=ma2.cfg)
    dummy = torch.zeros(3, 720, 1280, device="cuda")
    mask = torch.zeros(720, 1280, device="cuda")
    with torch.inference_mode():
        proc.step(dummy, mask, objects=[1])
        proc.step(dummy, first_frame_pred=True)
    torch.cuda.synchronize()
    print(f"[warm] MatAnyone2 就绪 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--warm-only":
        _warm_only()
    else:
        main()
