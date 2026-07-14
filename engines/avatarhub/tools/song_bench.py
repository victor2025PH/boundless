# -*- coding: utf-8 -*-
"""
AI 翻唱端到端基准：整曲 → song_studio(7853) 翻唱 → 耗时/RTF/贴合度报告。

用法（任意装了 requests 的 python）：
  python tools/song_bench.py --song path\to\song.mp3 --ref path\to\voice.wav
  python tools/song_bench.py --song a.mp3 --ref v.wav --steps 50 --pitch -2 --dry

输出：bench_out/song_bench/<时间戳>/cover.wav + vocals.wav + report.json
报告字段：separate_ms / convert_ms / mix_ms / total_ms / rtf / pitch_shift / cosine
（cosine 用 hub 同一把尺 clone_scorer——须在 hub 同机跑，本地 import。）
"""
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent.parent
SRV = os.environ.get("SONG_STUDIO_URL", "http://127.0.0.1:7853")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", required=True)
    ap.add_argument("--ref", required=True, help="目标音色 wav（3~30s）")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--pitch", type=int, default=None, help="半音；缺省=自动")
    ap.add_argument("--dry", action="store_true", help="song 已是干声，跳过分离")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    h = requests.get(f"{SRV}/health", timeout=5).json()
    print("health:", json.dumps(h.get("capabilities"), ensure_ascii=False))
    if not h.get("capabilities", {}).get("svc" if args.dry else "cover"):
        print("引擎能力未就绪，先跑 tools/setup_song_studio.py --all")
        return 2

    song_b = Path(args.song).read_bytes()
    ref_b = Path(args.ref).read_bytes()
    t0 = time.time()
    r = requests.post(f"{SRV}/v1/cover", json={
        "song_b64": base64.b64encode(song_b).decode(),
        "reference_b64": base64.b64encode(ref_b).decode(),
        "song_name": Path(args.song).name,
        "pitch_shift": args.pitch,
        "diffusion_steps": args.steps,
        "skip_separation": bool(args.dry),
    }, timeout=300)
    r.raise_for_status()
    tid = r.json()["task_id"]
    print("task:", tid)

    last = ""
    while True:
        st = requests.get(f"{SRV}/v1/task/{tid}", timeout=30).json()
        line = f"{st['status']} {st.get('progress', 0)}% {st.get('detail', '')}"
        if line != last:
            print(f"[{int(time.time()-t0):>4}s] {line}", flush=True)
            last = line
        if st["status"] in ("done", "error", "cancelled"):
            break
        if time.time() - t0 > args.timeout:
            print("TIMEOUT")
            return 3
        time.sleep(3)

    if st["status"] != "done":
        print("FAILED:", st.get("detail"))
        return 1

    out_dir = BASE / "bench_out" / "song_bench" / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem, fname in (("result", "cover.wav"), ("vocals", "vocals.wav")):
        a = requests.get(f"{SRV}/v1/task/{tid}/audio", params={"stem": stem}, timeout=300)
        if a.status_code == 200:
            (out_dir / fname).write_bytes(a.content)

    report = dict(st.get("result") or {})
    report["task_id"] = tid
    report["song"] = args.song
    report["steps"] = args.steps

    # 贴合度（hub 同机时用同一把尺）
    try:
        sys.path.insert(0, str(BASE))
        import clone_scorer
        sc = clone_scorer.score_similarity(
            base64.b64encode(ref_b).decode(),
            base64.b64encode((out_dir / "vocals.wav").read_bytes()).decode())
        if sc.get("ok"):
            report["cosine"] = sc["cosine"]
            report["similarity_label"] = sc["label"]
    except Exception as e:
        report["cosine_error"] = str(e)

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("outputs:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
