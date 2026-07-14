#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Song-P6 前置：拉取 ACE-Step 中文说唱 LoRA（RapMachine，Apache-2.0）。
→ models/ace_step/loras/ACE-Step-v1-chinese-rap-LoRA/
复用 setup_song_studio 的 hf-mirror 16 线程断点续传管线。

用法: python tools/setup_ace_lora.py [--verify]
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from setup_song_studio import _parallel_fetch, _probe_size, HOSTS  # noqa: E402

REPO = "ACE-Step/ACE-Step-v1-chinese-rap-LoRA"
DEST = os.path.join(BASE, "models", "ace_step", "loras",
                    "ACE-Step-v1-chinese-rap-LoRA")
FILES = [
    "config.json",
    "pytorch_lora_weights.safetensors",   # 524MB
]


def do_download() -> bool:
    all_ok = True
    for f in FILES:
        dest = os.path.join(DEST, f.replace("/", os.sep))
        print(f"[fetch] {REPO}/{f}")
        done = False
        for host in HOSTS:
            url = f"{host}/{REPO}/resolve/main/{f}"
            try:
                total = _probe_size(url)
            except Exception as e:
                print(f"  [probe fail] {host}: {e}")
                continue
            if _parallel_fetch(url, dest, total):
                done = True
                break
            print(f"  [host fail] {host}")
        if not done:
            print(f"  [FAIL] {f}")
            all_ok = False
    return all_ok


def do_verify() -> bool:
    ok = True
    for f in FILES:
        p = os.path.join(DEST, f.replace("/", os.sep))
        if not os.path.exists(p):
            print(f"[MISS] {f}")
            ok = False
        else:
            print(f"[OK]   {f}  {os.path.getsize(p)/1e6:.1f} MB")
    return ok


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(0 if do_verify() else 1)
    ok = do_download()
    print("RESULT:", "OK" if do_verify() and ok else "FAIL")
    sys.exit(0 if ok else 1)
