# -*- coding: utf-8 -*-
"""用 Ditto 把「一张脸 + 克隆音」渲染成会说话的数字人视频(嘴型跟音频)。
供同传演示:同一张脸分别说中文/英文 → 直观展示"我说中文,对方听我的声音说英文"。
产物: demo_record/ditto/<tag>.mp4
"""
import os
import subprocess
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ditto")
os.makedirs(OUT, exist_ok=True)
DITTO = "http://127.0.0.1:8096/ditto/generate"
FACE = r"C:\Users\user\Desktop\明星\刘德华2.jpg"


def gen(audio_wav, tag):
    clean = os.path.join(OUT, f"_{tag}_16k.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio_wav,
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", clean], check=True)
    t = time.time()
    r = requests.post(DITTO,
                      files={"audio": ("a.wav", open(clean, "rb"), "audio/wav"),
                             "face": ("f.jpg", open(FACE, "rb"), "image/jpeg")},
                      data={"sampling_timesteps": 25, "crop_scale": 2.3}, timeout=180)
    os.remove(clean)
    if r.status_code == 200 and b"ftyp" in r.content[:120]:
        p = os.path.join(OUT, f"{tag}.mp4")
        open(p, "wb").write(r.content)
        print(f"{tag}: ok {len(r.content)//1024}KB {int((time.time()-t)*1000)}ms")
        return p
    print(f"{tag}: FAIL {r.status_code} {r.content[:120]}")
    return None


if __name__ == "__main__":
    src = os.path.join(HERE, "interp2")
    import json
    meta = json.load(open(os.path.join(src, "lines.json"), encoding="utf-8"))
    for m in meta:
        for lang in ("zh", "en"):
            gen(m[f"{lang}_wav"], f"line{m['i']}_{lang}")
    print("done ->", OUT)
