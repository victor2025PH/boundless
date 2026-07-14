# -*- coding: utf-8 -*-
"""追加 A/B：更低底噪 + 更高采样步数对嘴部虚假动作的影响。"""
import sys, time
from pathlib import Path

import numpy as np
import requests
import cv2

sys.path.insert(0, str(Path(__file__).parent))
from _ditto_idle_ab import wav_bytes, mouth_motion, FACE, DITTO, BASE


def gen(audio: bytes, tag: str, steps: int) -> Path:
    r = requests.post(f"{DITTO}/ditto/generate",
                      files={"audio": (f"{tag}.wav", audio, "audio/wav"),
                             "face": ("face.jpg", FACE.read_bytes(), "image/jpeg")},
                      data={"sampling_timesteps": steps, "crop_scale": 2.8}, timeout=240)
    r.raise_for_status()
    out = BASE / "logs" / f"_idleab_{tag}.mp4"
    out.write_bytes(r.content)
    return out


def main():
    rng = np.random.default_rng(7)
    n = int(2.5 * 16000)
    cases = [
        ("noise80_s25", (rng.standard_normal(n) * 1e-4).astype(np.float32), 25),
        ("noise60_s50", (rng.standard_normal(n) * 1e-3).astype(np.float32), 50),
        ("zeros_s50",   np.zeros(n, dtype=np.float32), 50),
    ]
    for tag, s, steps in cases:
        t0 = time.time()
        mp4 = gen(wav_bytes(s), tag, steps)
        st = mouth_motion(mp4)
        print(f"[{tag:14s}] {st} ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
