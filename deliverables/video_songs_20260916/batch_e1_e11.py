#!/usr/bin/env python3
"""E1–E11 一键：缺说唱则作曲，再拼装成片。

  python batch_e1_e11.py              # 全做
  python batch_e1_e11.py --only E5,E6
  python batch_e1_e11.py --assemble-only   # 只拼（须已有 take json）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
# E1/E2 已有可用 take；其余现场抽
KNOWN = {"E1": 426, "E2": 428}
EPS = [f"E{i}" for i in range(1, 12)]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd: list[str]) -> int:
    log("$ " + " ".join(cmd)[:180])
    p = subprocess.run(cmd, cwd=str(ROOT))
    return p.returncode


def latest_take(ep: str) -> int | None:
    if ep in KNOWN:
        # 仍以磁盘上匹配 history_id 的 json 为准；没有则用 KNOWN
        for p in sorted(OUT.glob(f"{ep}_*_s*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                hid = json.loads(p.read_text(encoding="utf-8")).get("history_id")
                if hid == KNOWN[ep]:
                    return int(hid)
            except Exception:
                continue
        return KNOWN[ep]
    best = None
    best_m = -1.0
    for p in OUT.glob(f"{ep}_*_s*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            hid = d.get("history_id")
            wav = p.with_suffix(".wav")
            if hid and wav.exists() and wav.stat().st_size > 10_000:
                m = wav.stat().st_mtime
                if m > best_m:
                    best_m, best = m, int(hid)
        except Exception:
            continue
    return best


def ensure_song(ep: str) -> int | None:
    hid = latest_take(ep)
    if hid is not None and ep not in ("E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10", "E11"):
        # E1/E2 reuse
        log(f"{ep}: reuse take {hid}")
        return hid
    if ep in KNOWN:
        log(f"{ep}: reuse take {KNOWN[ep]}")
        return KNOWN[ep]
    # 已有新抽过的也复用（避免重复烧 GPU）
    if hid is not None:
        log(f"{ep}: reuse existing take {hid}")
        return hid
    rc = run([sys.executable, "make_song.py", ep, "--cut", "full", "--tries", "1"])
    if rc != 0:
        log(f"[FAIL] make_song {ep} exit={rc}")
        return None
    hid = latest_take(ep)
    if hid is None:
        # make_song 刚写的：取最新 json
        js = sorted(OUT.glob(f"{ep}_*_s*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if js:
            hid = int(json.loads(js[0].read_text(encoding="utf-8"))["history_id"])
    log(f"{ep}: new take {hid}")
    return hid


def assemble(ep: str, take: int) -> bool:
    rc = run([sys.executable, "make_episode.py", ep, "--take", str(take)])
    if rc != 0:
        log(f"[FAIL] make_episode {ep} take={take} exit={rc}")
        return False
    mp4s = list((OUT / ep).glob(f"{ep}_h{take}_episode.mp4"))
    if not mp4s:
        log(f"[FAIL] {ep} 无成片 mp4")
        return False
    log(f"[OK] {mp4s[0].name} {mp4s[0].stat().st_size // 1024}KB")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--assemble-only", action="store_true")
    a = ap.parse_args()
    eps = [x.strip() for x in a.only.split(",") if x.strip()] or EPS
    fails = []
    takes: dict[str, int] = {}
    for ep in eps:
        log(f"======== {ep} ========")
        if a.assemble_only:
            hid = latest_take(ep)
        else:
            hid = ensure_song(ep)
        if hid is None:
            fails.append(ep + ":song")
            continue
        takes[ep] = hid
        if not assemble(ep, hid):
            fails.append(ep + ":assemble")
    # 回写 registry picked_take
    reg_path = ROOT / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    items = reg.get("items") or reg
    for it in items:
        ep = it.get("id")
        if ep in takes:
            it["picked_take"] = takes[ep]
            it["status"] = "assembled"
    if isinstance(reg, dict) and "items" in reg:
        reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        reg_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"DONE takes={takes}")
    if fails:
        log(f"FAILS: {fails}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
