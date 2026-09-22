#!/usr/bin/env python3
"""按 ASR 逐句命中均值给某集说唱 take 打分，打印排序；可选 --apply 写回 registry picked_take。

  python pick_best_take.py E12
  python pick_best_take.py E12 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
REG = ROOT / "registry.json"


def score(js: Path) -> tuple[float, float, dict]:
    m = json.loads(js.read_text(encoding="utf-8"))
    hits = m.get("line_hits") or []
    mean = sum(hits) / len(hits) if hits else 0.0
    low = sum(1 for h in hits if h < 0.5)
    return mean, float(low), m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    cands = sorted(OUT.glob(f"{a.episode}_full_s*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # 也接受非 full 命名
    if not cands:
        cands = [p for p in OUT.glob(f"{a.episode}_*_s*.json") if "_intro" not in p.name]
    if not cands:
        print(f"[FAIL] 无 {a.episode} take json")
        return 1
    ranked = []
    for js in cands:
        mean, low, m = score(js)
        hid = m.get("history_id")
        wav = js.with_suffix(".wav")
        ok = wav.exists() and wav.read_bytes()[:4] == b"RIFF"
        ranked.append((mean, -low, hid, js, ok, m))
    ranked.sort(reverse=True)
    print(f"== pick_best_take {a.episode} ({len(ranked)} candidates) ==")
    for i, (mean, neg_low, hid, js, ok, m) in enumerate(ranked):
        mark = " ← best" if i == 0 else ""
        print(f"  #{i+1} hist={hid} mean={mean:.3f} low<{0.5}={-neg_low} "
              f"wav={'RIFF' if ok else 'BAD'} {js.name}{mark}")
    best = ranked[0]
    if not best[4]:
        print("[FAIL] best take wav 非 RIFF")
        return 1
    if a.apply:
        reg = json.loads(REG.read_text(encoding="utf-8"))
        hit = False
        for it in reg.get("items") or []:
            if it.get("id") == a.episode:
                it["picked_take"] = int(best[2])
                it["status"] = it.get("status") or "song_ok"
                hit = True
                break
        if not hit:
            print(f"[FAIL] registry 无 {a.episode}")
            return 1
        REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  applied picked_take={best[2]}")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
