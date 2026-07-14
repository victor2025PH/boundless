# -*- coding: utf-8 -*-
"""P1 精选 12 候选：按声学特征（音高/起伏/语速/干净度）从 218 人中挑有辨识度的。"""
import json, io

rows = json.load(io.open(r"C:\模仿音色\voice_pack_aishell3\index.json", encoding="utf-8"))
males = [r for r in rows if r["gender"] == "male" and r["f0_med"]]
fems = [r for r in rows if r["gender"] == "female" and r["f0_med"]]


def show(r, tag):
    print(f"{tag} {r['spk']} f0={r['f0_med']:6.1f} iqr={r['f0_iqr']:5.1f} "
          f"rate={r['rate']:.2f} snr={r['snr']:6.1f} bw={r['bw']:5.1f} "
          f"age={r['age']} acc={r['accent']} dur={r['dur']}")


print("=== male ===")
for r in sorted(males, key=lambda x: x["f0_med"])[:6]:
    show(r, "LOW   ")
for r in sorted(males, key=lambda x: -x["snr"])[:6]:
    show(r, "CLEAN ")
for r in sorted(males, key=lambda x: -x["f0_iqr"])[:4]:
    show(r, "LIVELY")
for r in sorted(males, key=lambda x: -x["rate"])[:4]:
    show(r, "FAST  ")
for r in sorted(males, key=lambda x: x["f0_iqr"])[:4]:
    show(r, "STEADY")
print("=== female ===")
for r in sorted(fems, key=lambda x: x["f0_med"])[:6]:
    show(r, "LOW   ")
for r in sorted(fems, key=lambda x: -x["snr"])[:6]:
    show(r, "CLEAN ")
for r in sorted(fems, key=lambda x: -x["f0_iqr"])[:4]:
    show(r, "LIVELY")
for r in sorted(fems, key=lambda x: -x["rate"])[:4]:
    show(r, "FAST  ")
for r in sorted(fems, key=lambda x: x["f0_iqr"])[:4]:
    show(r, "STEADY")
for r in sorted(fems, key=lambda x: -x["f0_med"])[:4]:
    show(r, "BRIGHT")
