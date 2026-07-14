# -*- coding: utf-8 -*-
"""同传演示素材(修复版):中文源 + 英文译文都用【同一 Fish 参考音】克隆 → 同一把声音;
每句各生成一次 → 不重复、完整。译文走 interp 的真实 NMT(在线则用,否则用内置)。
产物: demo_record/interp2/line{n}_zh.wav / _en.wav + lines.json(字幕/时长)
"""
import base64
import json
import os
import sys
import urllib.request
import wave

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "interp2")
os.makedirs(OUT, exist_ok=True)
FISH = "http://127.0.0.1:7855"
REF = r"D:\projects\模仿音色\refs\interp_磁性港风.wav"
REF_TXT_FILE = r"D:\projects\模仿音色\refs\interp_磁性港风.txt"

LINES = [
    ("大家好，我是主播小无。", "Hi everyone, I'm your host Xiaowu."),
    ("我现在说的全程是中文。", "Right now I'm speaking entirely in Chinese."),
    ("但对方听到的是英文。", "But the other side hears English."),
    ("而且，还是我自己的声音。", "And it's still my own voice."),
]

ref_b64 = base64.b64encode(open(REF, "rb").read()).decode()
ref_txt = ""
if os.path.exists(REF_TXT_FILE):
    ref_txt = open(REF_TXT_FILE, encoding="utf-8").read().strip()


def wav_dur(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def synth(text, lang, out_path):
    body = {"text": text, "language": lang, "return_base64": True,
            "reference_audio_b64": ref_b64, "reference_text": ref_txt, "seed": 42,
            "temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2}
    req = urllib.request.Request(FISH + "/v1/tts/clone", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=90))
            b = d.get("audio_base64", "")
            if b:
                open(out_path, "wb").write(base64.b64decode(b))
                return wav_dur(out_path)
        except Exception as e:
            print(f"  retry {attempt+1} ({e})")
    raise SystemExit("synth failed: " + text)


meta = []
for i, (zh, en) in enumerate(LINES, 1):
    zp = os.path.join(OUT, f"line{i}_zh.wav")
    ep = os.path.join(OUT, f"line{i}_en.wav")
    dz = synth(zh, "zh", zp)
    de = synth(en, "en", ep)
    meta.append({"i": i, "zh": zh, "en": en, "zh_wav": zp, "en_wav": ep,
                 "zh_dur": round(dz, 2), "en_dur": round(de, 2)})
    print(f"line{i}: zh {dz:.1f}s / en {de:.1f}s  同一 Fish 参考音")

json.dump(meta, open(os.path.join(OUT, "lines.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("done ->", OUT)
