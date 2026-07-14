#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Song-P3/F3 前置：拉取 ACE-Step v1-3.5B 权重（Apache-2.0，全曲级文生音乐基座）。
→ models/ace_step/ACE-Step-v1-3.5B/
复用 setup_song_studio 的 hf-mirror 16 线程断点续传管线（实测 ~25MB/s）。

用法: python tools/setup_ace_step.py [--verify]
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from setup_song_studio import _parallel_fetch, _probe_size, HOSTS  # noqa: E402

REPO = "ACE-Step/ACE-Step-v1-3.5B"
DEST = os.path.join(BASE, "models", "ace_step", "ACE-Step-v1-3.5B")
FILES = [
    "config.json",
    "ace_step_transformer/config.json",
    "ace_step_transformer/diffusion_pytorch_model.safetensors",
    "music_dcae_f8c8/config.json",
    "music_dcae_f8c8/diffusion_pytorch_model.safetensors",
    "music_vocoder/config.json",
    "music_vocoder/diffusion_pytorch_model.safetensors",
    "umt5-base/config.json",
    "umt5-base/model.safetensors",
    "umt5-base/special_tokens_map.json",
    "umt5-base/tokenizer.json",
    "umt5-base/tokenizer_config.json",
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
