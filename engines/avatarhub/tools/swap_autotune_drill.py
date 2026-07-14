# -*- coding: utf-8 -*-
"""swap_autotune_drill.py — SWAP_AUTO_QUALITY 升降档实弹演习（无人值守，2026-07-05）

标定(swap_calibrate.py)给出阈值后，本脚本验证**状态机在真实链路上的动作**：
  1) 拉起合成运动摄像头(tools/synth_cam.py, 8087) —— 替代真人坐镜头前;
  2) 拉起 realtime_stream(8080)：SWAP_AUTO_QUALITY=1 + 标定阈值 + 目标档 hd,
     源=合成摄像头, 链路=生产同款(hub 9000 → .104 faceswap);
  3) 三阶段: A 基线 60s(应稳在 hd) → B 注压 75s(并发打满换脸引擎,应降档保帧)
     → C 撤压 120s(应逐级爬回 hd);
  4) 全程 2s 采样 /swap/status 的 auto{effective,reason}+latency, 出断言+时间线 JSON。

断言: B 内 effective 降到 ≤natural; C 结束时 effective 回到 hd。
用法:  python tools/swap_autotune_drill.py [--down 416 --up 289 --dwell 2]
产物:  logs/swap_autotune_drill_<日期>.json
注: realtime_stream 若抢不到 OBS 虚拟摄像头会自动进降级模式(仅统计/MJPEG)——
    演习只看 /swap/status, 不需要 OBS 输出, 与 vcam_server 并存无冲突。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests

PY = sys.executable
HUB_SWAP = "http://127.0.0.1:9000/faceswap"
RT = "http://127.0.0.1:8080"
CAM = "http://127.0.0.1:8087"


def wait_http(url: str, timeout_s: float, desc: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                print(f"  {desc} 就绪 ({time.time() - t0:.0f}s)")
                return True
        except Exception:
            pass
        time.sleep(2)
    print(f"  !! {desc} {timeout_s}s 未就绪")
    return False


class Hammer:
    """注压器：N 线程持续用 hd 档参数打换脸引擎（经 hub，同生产路由），模拟抢卡过载。"""

    def __init__(self, n_threads: int, payload: dict):
        self.n = n_threads
        self.payload = payload
        self.stop = threading.Event()
        self.threads = []
        self.sent = 0

    def _loop(self):
        while not self.stop.is_set():
            try:
                requests.post(HUB_SWAP, json=self.payload, timeout=30)
                self.sent += 1
            except Exception:
                time.sleep(0.3)

    def start(self):
        for _ in range(self.n):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self.threads.append(t)

    def halt(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)


def sample(timeline: list, phase: str):
    try:
        d = requests.get(f"{RT}/swap/status", timeout=3).json()
        auto = d.get("auto") or {}
        rec = {"t": round(time.time(), 1), "phase": phase,
               "effective": d.get("preset"), "target": auto.get("target"),
               "lat_ms": (d.get("stats") or {}).get("latency_ms"),
               "reason": auto.get("reason") or ""}
        timeline.append(rec)
        return rec
    except Exception as e:
        timeline.append({"t": round(time.time(), 1), "phase": phase, "err": str(e)[:60]})
        return None


def observe(timeline: list, phase: str, seconds: float):
    t_end = time.time() + seconds
    last_eff = None
    while time.time() < t_end:
        rec = sample(timeline, phase)
        if rec and rec.get("effective") != last_eff:
            last_eff = rec.get("effective")
            print(f"  [{phase}] {time.strftime('%H:%M:%S')} 档={last_eff} "
                  f"lat={rec.get('lat_ms')}ms {rec.get('reason')}")
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--down", type=int, default=650, help="标定 DOWN_MS(默认取 prod104 fp16 并发 mean 标定值)")
    ap.add_argument("--up", type=int, default=475)
    ap.add_argument("--dwell", type=int, default=2, help="演习用短驻留,加快转档(生产默认 3)")
    ap.add_argument("--hammer", type=int, default=6, help="注压并发线程数")
    ap.add_argument("--baseline-s", type=int, default=60)
    ap.add_argument("--overload-s", type=int, default=75)
    ap.add_argument("--recover-s", type=int, default=120)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    procs = []
    timeline: list = []
    verdict = {}
    try:
        print("[1/5] 拉起合成摄像头(8087)…")
        procs.append(subprocess.Popen([PY, str(BASE / "tools" / "synth_cam.py")],
                                      cwd=str(BASE), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
        if not wait_http(f"{CAM}/health", 20, "synth_cam"):
            raise SystemExit(1)

        print("[2/5] 拉起 realtime_stream(8080, 自适应开, 目标档 hd, 生产路由 hub→faceswap)…")
        env = dict(os.environ)
        env.update({"SWAP_AUTO_QUALITY": "1",
                    "SWAP_AUTO_DOWN_MS": str(args.down),
                    "SWAP_AUTO_UP_MS": str(args.up),
                    "SWAP_AUTO_DWELL": str(args.dwell),
                    "SWAP_PRESET": "hd",
                    "SWAP_STATS": "0",              # 演习不污染趋势库 logs/swap_stats.json
                    "PYTHONIOENCODING": "utf-8"})
        procs.append(subprocess.Popen(
            [PY, str(BASE / "realtime_stream.py"),
             "--source", f"{CAM}/stream", "--width", "1280", "--height", "720",
             "--no-preview"],
            cwd=str(BASE), env=env,
            stdout=open(BASE / "logs" / "rt_drill.log", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT))
        if not wait_http(f"{RT}/swap/status", 45, "realtime_stream"):
            raise SystemExit(1)

        print(f"[3/5] 阶段A·基线 {args.baseline_s}s（应稳在 hd）…")
        observe(timeline, "A-baseline", args.baseline_s)

        print(f"[4/5] 阶段B·注压 {args.overload_s}s（{args.hammer} 并发 hd 请求抢卡,应降档）…")
        from tools.swap_calibrate import synth_frames, encode_like_stream, PRESETS
        f = synth_frames(str(BASE / "_ldh720.jpg"), 1)[0]
        hd = PRESETS["hd"]
        hammer = Hammer(args.hammer, {
            "target_image": encode_like_stream(f, hd["proc_w"], hd["jpeg_q"]),
            "smooth_alpha": hd["smooth"], "enhance": hd["enhance"]})
        hammer.start()
        observe(timeline, "B-overload", args.overload_s)
        hammer.halt()
        print(f"  注压结束(共发 {hammer.sent} 发)")

        print(f"[5/5] 阶段C·撤压恢复 {args.recover_s}s（应逐级爬回 hd）…")
        observe(timeline, "C-recover", args.recover_s)

        effs_b = [r.get("effective") for r in timeline if r.get("phase") == "B-overload"]
        effs_c = [r.get("effective") for r in timeline if r.get("phase") == "C-recover"]
        verdict = {
            "baseline_held_hd": all(r.get("effective") == "hd" for r in timeline
                                    if r.get("phase") == "A-baseline" and r.get("effective")),
            "downshifted_under_load": any(e in ("eco", "natural") for e in effs_b),
            "recovered_to_hd": bool(effs_c) and effs_c[-1] == "hd",
            "min_preset_seen": min((e for e in effs_b if e), key=lambda x: ["eco", "natural", "beauty", "hd"].index(x), default=None),
        }
        verdict["pass"] = bool(verdict["downshifted_under_load"] and verdict["recovered_to_hd"])
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(2)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "thresholds": {"down_ms": args.down, "up_ms": args.up, "dwell": args.dwell},
           "hammer_threads": args.hammer, "verdict": verdict, "timeline": timeline}
    day = datetime.now().strftime("%Y%m%d")
    p = BASE / "logs" / f"swap_autotune_drill_{day}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论: {verdict} \n已写 {p}")
    sys.exit(0 if verdict.get("pass") else 1)


if __name__ == "__main__":
    main()
