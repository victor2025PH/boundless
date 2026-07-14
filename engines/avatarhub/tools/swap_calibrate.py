# -*- coding: utf-8 -*-
"""swap_calibrate.py — SWAP_AUTO_QUALITY 阈值标定（无人值守版，2026-07-05）

目的：把「用户坐镜头前 15 分钟」的标定改为全自动——
对四个画质档(eco/natural/beauty/hd)，用与 realtime_stream.swap_worker **逐字节同形**的请求
(同 PROC_W 缩放、同 JPEG_Q 编码、同 enhance/smooth 参数、同 /faceswap 端点)打一批合成运动帧，
测出每档的每帧时延分布(p50/p95/max)，据此给出 SWAP_AUTO_DOWN_MS / SWAP_AUTO_UP_MS 建议：

  自适应信号是 EWMA(0.7旧+0.3新)——统计上贴近**均值**而非 p95(GFPGAN 并发排队的长尾
  会被平滑掉)，所以阈值从 mean 推：
  DOWN_MS: 取「最重档(hd) 并发 mean × 1.5」再夹到 ≥ 帧预算(1000/SWAP_FPS[hd])——
           正常运行 EWMA 永不越线，只有真过载(抢卡/掉CPU,秒级时延)才降档。
  UP_MS:   升档条件=「当前档 EWMA < UP_MS」。要让梯子能一路爬回目标档(hd)，UP 必须高于
           所有"低于目标的档"的正常 mean(否则爬升在半路卡死)，又要与 DOWN 留滞回间隙：
           UP = min( 1.15 × max(mean of 档<hd), 0.75 × DOWN )。
           (v1 用 p95+相邻放大比：噪声长尾直接把阈值顶爆、还会把 beauty→hd 爬升卡死，
            实测数据暴露后两处都改由 mean 推。)

用法(需 faceswap 引擎在线；hub 在线则走生产同款代理路径):
  python tools/swap_calibrate.py                        # hub 代理(生产路径,自动注入活跃角色脸)
  python tools/swap_calibrate.py --direct               # 直连 8000(无 hub 时)
  python tools/swap_calibrate.py --url http://192.168.0.104:8000/faceswap --direct --tag dot104
  python tools/swap_calibrate.py --n 40 --photo _snap_raw.jpg
产物: logs/swap_calibration_<日期><tag>.json + 控制台建议值
"""
import argparse
import base64
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import cv2
import numpy as np
import requests

# 与 realtime_stream._SWAP_PRESETS 保持一致（标定的就是这份表的行为）
PRESETS = {
    "eco":     {"proc_w": 288, "enhance": "none",   "smooth": 0.9, "jpeg_q": 45, "fps": 10},
    "natural": {"proc_w": 384, "enhance": "none",   "smooth": 1.0, "jpeg_q": 55, "fps": 15},
    "beauty":  {"proc_w": 448, "enhance": "gfpgan", "smooth": 1.2, "jpeg_q": 60, "fps": 14},
    "hd":      {"proc_w": 512, "enhance": "gfpgan", "smooth": 1.0, "jpeg_q": 72, "fps": 12},
}
ORDER = ["eco", "natural", "beauty", "hd"]


def _imread_any(path: str):
    """cv2.imread 不认 Windows 中文路径 → 字节流 + imdecode。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _stage_canvas(img, w: int, h: int):
    """人像等比缩到画面高的 95%，居中放进 1.25x 运动画布，四周边缘复制当背景——
    竖版照片直接 cover-crop 会把人脸推出画(标定得到的全是'未检测到人脸')。"""
    scale = (h * 0.95) / img.shape[0]
    iw, ih = max(1, int(img.shape[1] * scale)), int(img.shape[0] * scale)
    img = cv2.resize(img, (iw, ih))
    cw, ch = int(w * 1.25), int(h * 1.25)
    left = max(0, (cw - iw) // 2)
    top = max(0, (ch - ih) // 2)
    return cv2.copyMakeBorder(img, top, max(0, ch - ih - top),
                              left, max(0, cw - iw - left), cv2.BORDER_REPLICATE)


def synth_frames(photo_path: str, n: int, w=1280, h=720):
    """合成 n 帧“说话晃动”运动帧(与 tools/synth_cam.py 同一运动模型，离线批量版)。"""
    img = _imread_any(photo_path)
    if img is None:
        raise SystemExit(f"读不到照片: {photo_path}")
    img = _stage_canvas(img, w, h)
    ph, pw = img.shape[:2]
    frames = []
    for i in range(n):
        t = i / 15.0
        dx = 0.03 * pw * np.sin(2 * np.pi * t / 6.1)
        dy = 0.02 * ph * np.sin(2 * np.pi * t / 4.3 + 1.0)
        zoom = 1.0 + 0.04 * np.sin(2 * np.pi * t / 8.7 + 2.0)
        ang = 2.0 * np.sin(2 * np.pi * t / 5.6 + 0.5)
        M = cv2.getRotationMatrix2D((pw / 2 + dx, ph / 2 + dy), ang, zoom)
        f = cv2.warpAffine(img, M, (pw, ph), borderMode=cv2.BORDER_REPLICATE)
        x0 = max(0, min(pw - w, int((pw - w) / 2 + dx / 2)))
        y0 = max(0, min(ph - h, int((ph - h) / 2 + dy / 2)))
        frames.append(f[y0:y0 + h, x0:x0 + w])
    return frames


def encode_like_stream(frame, proc_w: int, jpeg_q: int) -> str:
    """swap_worker 同款：限宽缩放 + JPEG 编码 → b64。"""
    h, w = frame.shape[:2]
    pw = min(w, proc_w)
    phh = int(h * pw / w)
    small = cv2.resize(frame, (pw, phh))
    _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])
    return base64.b64encode(buf).decode()


def _one(url: str, frame, ps: dict):
    payload = {"target_image": encode_like_stream(frame, ps["proc_w"], ps["jpeg_q"]),
               "smooth_alpha": ps["smooth"], "enhance": ps["enhance"]}
    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=60)
        wall_ms = (time.time() - t0) * 1000.0
        d = r.json() if r.status_code == 200 else {}
        if not d.get("result_image"):
            return None
        # 与 realtime_stream 一致：优先用服务端 elapsed_ms(纯推理)，否则整程往返。
        # 并发>1 时必须用墙钟(elapsed_ms 不含排队/串行化等待，而 EWMA 信号吃的是往返)。
        return wall_ms
    except Exception:
        return None


def measure(url: str, frames, ps: dict, workers: int = 1, n_warm: int = 3):
    """workers>1 = 生产同形：保持 N 路在飞(realtime_stream SWAP_WORKERS 行为)，
    测的是含 GPU 串行化排队的**每请求墙钟**——这才是 _swap_latency_avg 真正看到的信号。
    (v1 串行单发把 hd 实际时延低估了 ~40%,标定出的 DOWN 阈值在基线期就误触发。)"""
    import concurrent.futures as cf
    lats, fails, done = [], 0, 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        pend = set()
        it = iter(range(len(frames) + n_warm))
        def _submit():
            try:
                i = next(it)
            except StopIteration:
                return False
            pend.add(ex.submit(_one, url, frames[i % len(frames)], ps))
            return True
        for _ in range(workers):
            if not _submit():
                break
        while pend:
            fut = next(cf.as_completed(pend))
            pend.discard(fut)
            lat = fut.result()
            done += 1
            if lat is None:
                fails += 1
            elif done > n_warm * workers:    # 暖机(含检测/分辨率缓存冷启)不计
                lats.append(lat)
            _submit()
    if not lats:
        return {"fail": fails, "n": 0}
    lats.sort()
    return {"n": len(lats), "fail": fails,
            "p50": round(statistics.median(lats), 1),
            "p95": round(lats[int(0.95 * (len(lats) - 1))], 1),
            "mean": round(statistics.mean(lats), 1),
            "max": round(max(lats), 1)}


def recommend(res: dict) -> dict:
    """由分布导出阈值建议(逻辑见文件头)。"""
    hd = res.get("hd") or {}
    if not hd.get("n"):
        return {}
    budget_hd = 1000.0 / PRESETS["hd"]["fps"]
    down = max(hd["mean"] * 1.5, budget_hd)
    # UP 须高于目标以下各档的正常 mean(否则爬升半路卡死)，且与 DOWN 留滞回间隙
    below_mean = [(res.get(k) or {}).get("mean") or 0 for k in ORDER[:-1]]
    up = min(1.15 * max(below_mean), 0.75 * down) if any(below_mean) else down * 0.5
    return {"down_ms": int(round(down)), "up_ms": int(round(up)),
            "hd_frame_budget_ms": round(budget_hd, 1),
            "hd_mean_ms": hd["mean"], "below_target_mean_ms": below_mean}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--direct", action="store_true", help="直连 faceswap:8000(默认走 hub 9000 代理)")
    ap.add_argument("--photo", default=str(BASE / "_ldh720.jpg"))
    ap.add_argument("--n", type=int, default=30, help="每档计入分布的请求数(另加暖机)")
    ap.add_argument("--workers", type=int, default=3, help="并发在飞数(=realtime SWAP_WORKERS,生产同形)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    url = args.url or ("http://127.0.0.1:8000/faceswap" if args.direct
                       else "http://127.0.0.1:9000/faceswap")
    photo = args.photo if Path(args.photo).exists() else str(BASE / "_ldh.jpg")
    frames = synth_frames(photo, args.n)
    print(f"标定目标: {url} | 照片: {photo} | 每档 {args.n} 请求 × {args.workers} 并发在飞(生产同形)")

    res = {}
    for name in ORDER:
        ps = PRESETS[name]
        r = measure(url, frames, ps, workers=args.workers)
        res[name] = r
        print(f"  [{name:7s}] proc_w={ps['proc_w']:3d} enhance={ps['enhance']:6s} "
              f"-> p50={r.get('p50')}ms p95={r.get('p95')}ms max={r.get('max')}ms "
              f"(n={r.get('n')}, fail={r.get('fail')})")

    rec = recommend(res)
    out = {"ts": datetime.now().isoformat(timespec="seconds"), "url": url,
           "photo": photo, "presets": PRESETS, "results": res, "recommend": rec}
    day = datetime.now().strftime("%Y%m%d")
    p = BASE / "logs" / f"swap_calibration_{day}{('_' + args.tag) if args.tag else ''}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n建议阈值: SWAP_AUTO_DOWN_MS={rec.get('down_ms')}  SWAP_AUTO_UP_MS={rec.get('up_ms')}"
          f"  (hd 并发 mean {rec.get('hd_mean_ms')}ms, 目标以下各档 mean {rec.get('below_target_mean_ms')})")
    print(f"已写 {p}")


if __name__ == "__main__":
    main()
