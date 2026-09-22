#!/usr/bin/env python3
"""重做 E1–E11：每集独立开场唱 +（可选）清种对齐内容 + 大红圈重录 + 拼装。

  python rebuild_e1_e11_v2.py
  python rebuild_e1_e11_v2.py --skip-record   # 只作曲 intro + 拼装
  python rebuild_e1_e11_v2.py --only E3
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
# 说唱 take（已有）；intro 现场抽
RAP_TAKE = {
    "E1": 426, "E2": 428, "E3": 429, "E4": 430, "E5": 431,
    "E6": 432, "E7": 433, "E8": 434, "E9": 435, "E10": 436, "E11": 437,
}
EPS = [f"E{i}" for i in range(1, 12)]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd: list[str]) -> int:
    log("$ " + " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def latest_intro(ep: str) -> Path | None:
    # make_song 落盘名：E3_intro_full_s123.wav
    cands = sorted(OUT.glob(f"{ep}_intro*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for w in cands:
        if w.stat().st_size > 8000:
            return w
    return None


def ensure_intro(ep: str) -> bool:
    if latest_intro(ep):
        log(f"{ep}: reuse intro {latest_intro(ep).name}")
        return True
    rc = run([sys.executable, "make_song.py", f"{ep}_intro", "--cut", "full", "--tries", "1"])
    ok = rc == 0 and latest_intro(ep) is not None
    log(f"{ep}: intro {'OK' if ok else 'FAIL'}")
    return ok


def seed_ep(ep: str) -> None:
    # 仅互译集需要英文 BOUNDLESS 干净上下文；其它集用真实多会话，禁止再清成「全站同一聊天」
    if ep == "E3":
        run([sys.executable, "seed_demo_chats.py", "--episode", "E3", "--thread", "BOUNDLESS", "--clear", "--pin"])
    elif ep == "E1":
        # 可选：不强清，避免毁掉多平台真实列表观感
        pass



def record_ep(ep: str) -> bool:
    return run([sys.executable, "record_chatx.py", ep, "--allow-send"]) == 0


def assemble_ep(ep: str) -> bool:
    take = RAP_TAKE[ep]
    return run([sys.executable, "make_episode.py", ep, "--take", str(take)]) == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-record", action="store_true")
    ap.add_argument("--skip-seed", action="store_true")
    a = ap.parse_args()
    eps = [x.strip() for x in a.only.split(",") if x.strip()] or EPS
    fails = []
    for ep in eps:
        log(f"======== {ep} ========")
        if not ensure_intro(ep):
            fails.append(ep + ":intro")
            continue
        if not a.skip_record:
            if not a.skip_seed:
                seed_ep(ep)
            if not record_ep(ep):
                fails.append(ep + ":record")
                continue
        if not assemble_ep(ep):
            fails.append(ep + ":assemble")
            continue
        log(f"[OK] {ep}")
    log(f"DONE fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
