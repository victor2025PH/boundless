# -*- coding: utf-8 -*-
"""垫播素材与状态机轻测：从 monitor_relay.py 抽取 _bed_material(不起服务、不开声卡)。"""
import os, sys, re, types, logging, threading, time
import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor_relay.py")
with open(SRC, "r", encoding="utf-8") as f:
    src = f.read()

m = re.search(r"_bed_mat_cache: dict.*?(?=\ndef _bed_player)", src, re.S)
assert m, "_bed_material 未找到"
G = {"os": os, "np": np, "time": time, "threading": threading,
     "logger": logging.getLogger("t"), "_BED_WAV": ""}
exec(m.group(0), G)
_bed_material = G["_bed_material"]

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

sr = 48000
buf = _bed_material(sr)
rms = float(np.sqrt(np.mean(buf * buf)))
dbfs = 20 * np.log10(rms + 1e-12)
check(f"素材时长 12s (got {buf.size/sr:.1f}s)", abs(buf.size / sr - 12.0) < 0.01)
check(f"响度 -26dBFS±1 (got {dbfs:.1f})", -27.0 <= dbfs <= -25.0)
check(f"高于静音判定阈值 0.008 (rms={rms:.4f})", rms > 0.008)
check(f"无削波 (peak={np.abs(buf).max():.3f})", np.abs(buf).max() <= 0.9)
# 循环无缝：尾→头拼接处的样本跳变应与素材内部相邻样本差在同量级(无咔哒)
internal_step = float(np.abs(np.diff(buf)).max())
seam_step = abs(float(buf[0]) - float(buf[-1]))
check(f"循环接缝平滑 (seam={seam_step:.5f} <= 内部最大步进 {internal_step:.5f}×1.5)",
      seam_step <= internal_step * 1.5)
# 呼吸包络：素材应有明显强弱起伏(非恒定响度的嗡嗡声)
w = buf.reshape(-1, sr // 4)
seg_rms = np.sqrt((w * w).mean(axis=1))
check(f"有呼吸起伏 (max/min={seg_rms.max()/max(seg_rms.min(),1e-9):.2f}x)",
      seg_rms.max() / max(seg_rms.min(), 1e-9) >= 1.4)
check("缓存复用同一对象", _bed_material(sr) is buf)

print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
