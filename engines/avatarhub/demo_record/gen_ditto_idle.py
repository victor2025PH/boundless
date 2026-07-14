# -*- coding: utf-8 -*-
"""用 Ditto 静默驱动把静图变"活"(呼吸/眨眼级微动)——发型/试衣静图 → 动态视频。
掺 -80dB 底噪的静音 wav 驱动(纯零会让 Ditto 幻觉张嘴,实测经验)。
用法: python gen_ditto_idle.py 图片 输出.mp4 [秒数]
"""
import io
import os
import sys
import wave

import numpy as np
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DITTO = "http://127.0.0.1:8096/ditto/generate"


def silent_wav(secs, rate=16000):
    n = int(rate * secs)
    noise = (np.random.default_rng(7).standard_normal(n) * 1e-4 * 32767).astype(np.int16)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(noise.tobytes()); w.close()
    return buf.getvalue()


def animate(img_path, out_path, secs=4.0, crop_scale=2.3):
    r = requests.post(DITTO,
                      files={"audio": ("idle.wav", silent_wav(secs), "audio/wav"),
                             "face": ("f.jpg", open(img_path, "rb"), "image/jpeg")},
                      data={"sampling_timesteps": 25, "crop_scale": crop_scale}, timeout=180)
    if r.status_code == 200 and b"ftyp" in r.content[:120]:
        open(out_path, "wb").write(r.content)
        return True
    print("FAIL", r.status_code, r.content[:120])
    return False


if __name__ == "__main__":
    img, out = sys.argv[1], sys.argv[2]
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    ok = animate(img, out, secs)
    print(("ok " if ok else "fail ") + out, os.path.getsize(out) // 1024 if ok else 0, "KB")
