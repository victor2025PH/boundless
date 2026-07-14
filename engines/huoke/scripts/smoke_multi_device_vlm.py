# -*- coding: utf-8 -*-
"""多机并发 VLM smoke：验证 _VLM_LOCK 在真机链路下正确串行化。

场景:
    两台（或多台）真机同时发起 classify_images()，应该被 _VLM_LOCK
    串行化，peak queue_wait_ms >= 一次 VLM 推理耗时。

用法::
    python scripts/smoke_multi_device_vlm.py --devices JZBIGUKZS4NBAYDI 192.168.0.160:5555
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _b(s): return f"\033[36m{s}\033[0m"


ADB = r"C:\platform-tools\adb.exe"
FAILS: List[str] = []


def chk(name, ok, detail=""):
    tag = _g("PASS") if ok else _r("FAIL")
    print(f"[{tag}] {name}  {detail}")
    if not ok:
        FAILS.append(name)


def screencap_one(device: str, save_path: str) -> bool:
    remote = "/sdcard/_multi_vlm_probe.png"
    subprocess.run([ADB, "-s", device, "shell", "screencap", "-p", remote],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="ignore", timeout=10, check=False)
    r = subprocess.run([ADB, "-s", device, "pull", remote, save_path],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", timeout=15, check=False)
    return r.returncode == 0 and os.path.exists(save_path) and os.path.getsize(save_path) > 10240


def run_device_classify(device: str, img_path: str, results: Dict[str, Any]):
    """在一个线程里跑一次 classify_images，结果写入共享 dict。"""
    from src.host import ollama_vlm
    t0 = time.time()
    try:
        r, meta = ollama_vlm.classify_images(
            prompt=(
                "Look at the Facebook profile screenshot and return JSON: "
                '{"is_japanese": bool, "age_band": str, "gender": str, "topics": [str]}'
            ),
            image_paths=[img_path],
            scene="fb_profile_l2_multi",
            task_id=f"multi_{device[:8]}",
            device_id=device,
        )
        results[device] = {
            "ok": bool(meta.get("ok")),
            "total_ms": int(meta.get("total_ms") or 0),
            "latency_ms": int(meta.get("latency_ms") or 0),
            "queue_wait_ms": int(meta.get("queue_wait_ms") or 0),
            "wall_ms": int((time.time() - t0) * 1000),
            "result": r if isinstance(r, dict) else {},
            "error": meta.get("error", ""),
        }
    except Exception as e:
        results[device] = {"ok": False, "error": f"EXC:{e}",
                           "wall_ms": int((time.time() - t0) * 1000)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", nargs="+", required=True,
                    help="ADB device serials / ip:port")
    args = ap.parse_args()
    devices: List[str] = args.devices

    print("=" * 60)
    print(f"  多机并发 VLM smoke  devices={devices}")
    print("=" * 60)

    # 先 warmup，确保比较的是 lock 本身
    print(_b("\n─── 1) VLM warmup ───"))
    from src.host import ollama_vlm
    st0 = ollama_vlm.get_warmup_state()
    if not st0.get("fresh"):
        r = ollama_vlm.warmup(force=False)
        chk("warmup ok", r.get("ok"), f"latency={r.get('latency_ms')}ms")
    else:
        chk("warmup 已 fresh，跳过", True, f"age={st0.get('age_sec')}s")

    # 每台各自截一张
    print(_b("\n─── 2) 各设备 screencap 取样 ───"))
    img_map: Dict[str, str] = {}
    shot_dir = ROOT / "data" / f"multi_shots_{int(time.time())}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    for dev in devices:
        p = str(shot_dir / (dev.replace(":", "_") + ".png"))
        ok = screencap_one(dev, p)
        chk(f"{dev} screencap", ok,
            f"size={os.path.getsize(p) if ok else 'N/A'}B")
        if ok:
            img_map[dev] = p
    if len(img_map) < 2:
        print(_r("至少需要两台能截图的设备才能跑并发 smoke"))
        sys.exit(1)

    # 记录并发开始前的 concurrency stats
    stats_before = ollama_vlm.get_concurrency_stats()
    print(_b("\n─── 3) 并发 classify_images（预期被 _VLM_LOCK 串行化） ───"))
    results: Dict[str, Any] = {}
    threads = []
    wall0 = time.time()
    for dev, img in img_map.items():
        t = threading.Thread(target=run_device_classify,
                             args=(dev, img, results),
                             name=f"vlm-{dev[:8]}")
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=180)
    wall_total = int((time.time() - wall0) * 1000)

    # 结果汇总
    print()
    for dev, r in results.items():
        print(f"  [{dev}] ok={r.get('ok')} wall={r.get('wall_ms')}ms "
              f"lat={r.get('latency_ms')}ms queue={r.get('queue_wait_ms')}ms "
              f"err={r.get('error','')}")

    oks = sum(1 for r in results.values() if r.get("ok"))
    chk(f"所有设备 classify ok ({oks}/{len(img_map)})",
        oks == len(img_map))

    stats_after = ollama_vlm.get_concurrency_stats()
    calls_delta = stats_after["total_calls"] - stats_before["total_calls"]
    chk("concurrency.total_calls +=N", calls_delta == len(img_map),
        f"before={stats_before['total_calls']} after={stats_after['total_calls']}")

    # 至少有一台 queue_wait_ms > 0（证明被 _VLM_LOCK 等待过）
    queue_waits = [r.get("queue_wait_ms", 0) for r in results.values()]
    chk("至少一台 queue_wait_ms > 0 (被串行化)",
        max(queue_waits) > 0,
        f"waits={queue_waits} peak_wait_ms={stats_after.get('peak_wait_ms')}")

    # 串行化 vs 并行：wall time >= 两次 latency 之和 * 0.7（允许 30% 误差）
    lat_sum = sum(r.get("latency_ms", 0) for r in results.values())
    chk(f"wall_total ({wall_total}ms) 接近串行 lat_sum ({lat_sum}ms)",
        wall_total >= int(lat_sum * 0.7),
        f"ratio={wall_total/max(lat_sum,1):.2f}")

    print(_b("\n─── 总结 ───"))
    if FAILS:
        print(_r(f"{len(FAILS)} 个检查失败: {FAILS}"))
        sys.exit(1)
    print(_g(f"ALL GREEN — 多机 VLM 串行化正确，并发 {len(img_map)} 台 wall={wall_total}ms"))


if __name__ == "__main__":
    main()
