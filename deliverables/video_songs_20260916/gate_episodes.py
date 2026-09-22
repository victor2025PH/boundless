#!/usr/bin/env python3
"""成片闸：E1–E11 mp4 双流 + 时长 + 本集 footage clip 齐全 + 抽中段帧。

  python gate_episodes.py
  python gate_episodes.py --only E3,E4,E5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
RAP_TAKE = {
    "E1": 426, "E2": 428, "E3": 429, "E4": 430, "E5": 431,
    "E6": 432, "E7": 433, "E8": 434, "E9": 435, "E10": 436, "E11": 437,
    "E12": 452,
    "R1": 474, "R2": 477, "R3": 487, "R4": 497,
    "C1": 472,
}


def resolve_takes() -> dict[str, int]:
    """registry.picked_take 优先于默认表。"""
    out = dict(RAP_TAKE)
    try:
        reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
        for it in reg.get("items") or []:
            eid = it.get("id")
            if eid in out and it.get("picked_take"):
                out[eid] = int(it["picked_take"])
    except Exception:
        pass
    return out


def ffprobe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return json.loads(r.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    takes = resolve_takes()
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    eps = [e for e in cur["episodes"] if e["id"] in takes]
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        eps = [e for e in eps if e["id"] in want]

    fails: list[str] = []
    for ep in eps:
        eid = ep["id"]
        take = takes[eid]
        mp4 = OUT / eid / f"{eid}_h{take}_episode.mp4"
        print(f"-- {eid}")
        if not mp4.exists():
            fails.append(f"{eid}: 缺成片 {mp4.name}")
            print("  FAIL missing mp4")
            continue
        try:
            info = ffprobe(mp4)
        except Exception as e:
            fails.append(f"{eid}: ffprobe {e}")
            continue
        kinds = {s["codec_type"] for s in info["streams"]}
        dur = float(info["format"]["duration"])
        if kinds != {"video", "audio"}:
            fails.append(f"{eid}: streams={kinds}")
        if dur < 20:
            fails.append(f"{eid}: dur too short {dur:.1f}s")
        if mp4.read_bytes()[4:8] != b"ftyp":
            fails.append(f"{eid}: not ftyp")
        # intro 独立
        intros = list(OUT.glob(f"{eid}_intro*.wav"))
        if not intros:
            fails.append(f"{eid}: 缺独立开场唱 wav")
        # footage clips
        for clip in ep.get("footage_actions") or []:
            webm = OUT / eid / f"footage_{clip['clip']}.webm"
            if not webm.exists() or webm.stat().st_size < 50_000:
                fails.append(f"{eid}: 缺/过小 footage_{clip['clip']}.webm")
        # 中段抽帧（目检素材）
        mid = max(10.0, dur * 0.45)
        shot = OUT / eid / f"gate_mid_t{int(mid)}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.2f}", "-i", str(mp4), "-frames:v", "1", str(shot)],
            capture_output=True,
        )
        ok = shot.exists() and shot.stat().st_size > 20_000
        print(f"  mp4 {dur:.1f}s streams={kinds} intro={bool(intros)} mid_frame={'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f"{eid}: mid frame fail")

    print("== gate_episodes ==")
    for f in fails:
        print("FAIL", f)
    print("RESULT", "FAIL" if fails else "PASS", f"({len(fails)} fails)" if fails else "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
