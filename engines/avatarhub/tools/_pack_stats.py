# -*- coding: utf-8 -*-
"""音色包统计：index.json 汇总 + 体积。"""
import glob, io, json, os

PACK = r"C:\模仿音色\voice_pack_aishell3"
rows = json.load(io.open(os.path.join(PACK, "index.json"), encoding="utf-8"))
males = [r for r in rows if r.get("gender") == "male"]
fems = [r for r in rows if r.get("gender") == "female"]
print("total:", len(rows), "| male:", len(males), "| female:", len(fems))
good = [r for r in rows if (r.get("snr") or 0) >= 50 and (r.get("bw") or 0) >= 9.5]
print("high-quality (snr>=50dB & bw>=9.5kHz):", len(good))
files = glob.glob(os.path.join(PACK, "*"))
sz = sum(os.path.getsize(f) for f in files) / 1e6
print(f"pack size: {sz:.0f} MB, files: {len(files)}")
lows = sorted(males, key=lambda r: r.get("f0_med") or 999)[:5]
print("lowest-pitch males:", [(r["spk"], r.get("f0_med")) for r in lows])
