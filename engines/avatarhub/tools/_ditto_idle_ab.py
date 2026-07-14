# -*- coding: utf-8 -*-
"""A/B：纯零静音 vs 低幅底噪 → Ditto 嘴部虚假动作对比。
嘴区动作分数 = 嘴部裁剪区逐帧 absdiff 均值（越低越像闭嘴待机）。"""
import sys, io, time, wave, struct
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests
import numpy as np
import cv2

BASE = Path(r"c:\模仿音色")
DITTO = "http://127.0.0.1:8096"
FACE = BASE / "faces" / "刘德华.jpg"
SECS = 2.5


def wav_bytes(samples: np.ndarray, rate=16000) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes((samples * 32767).astype(np.int16).tobytes())
    w.close()
    return buf.getvalue()


def gen(audio: bytes, tag: str) -> Path:
    r = requests.post(f"{DITTO}/ditto/generate",
                      files={"audio": (f"{tag}.wav", audio, "audio/wav"),
                             "face": ("face.jpg", FACE.read_bytes(), "image/jpeg")},
                      data={"sampling_timesteps": 25, "crop_scale": 2.8}, timeout=240)
    r.raise_for_status()
    out = BASE / "logs" / f"_idleab_{tag}.mp4"
    out.write_bytes(r.content)
    return out


def mouth_motion(mp4: Path) -> dict:
    cap = cv2.VideoCapture(str(mp4))
    prev_m, prev_g, md, gd = None, None, [], []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        h, w = f.shape[:2]
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        m = g[int(h*0.32):int(h*0.52), int(w*0.38):int(w*0.58)]  # 嘴部区
        if prev_m is not None:
            md.append(float(np.abs(m - prev_m).mean()))
            gd.append(float(np.abs(g - prev_g).mean()))
        prev_m, prev_g = m, g
    cap.release()
    return {"mouth_mean": round(float(np.mean(md)), 3), "mouth_max": round(float(np.max(md)), 3),
            "global_mean": round(float(np.mean(gd)), 3)}


def main():
    n = int(SECS * 16000)
    rng = np.random.default_rng(7)
    cases = {
        "zeros": np.zeros(n, dtype=np.float32),
        "noise_60db": (rng.standard_normal(n) * 1e-3).astype(np.float32),
        "noise_45db": (rng.standard_normal(n) * 5.6e-3).astype(np.float32),
    }
    for tag, s in cases.items():
        t0 = time.time()
        mp4 = gen(wav_bytes(s), tag)
        st = mouth_motion(mp4)
        print(f"[{tag:12s}] {st} ({time.time()-t0:.1f}s) -> {mp4.name}")


if __name__ == "__main__":
    main()
