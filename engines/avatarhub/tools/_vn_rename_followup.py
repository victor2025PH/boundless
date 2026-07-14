# -*- coding: utf-8 -*-
"""VN2 改名善后（一次性）：Hub 的 PATCH 改名不迁移两类旁路文件，按 rename_map 补齐——
  1) 黄金出厂包 golden_packages\\{旧名}.zip/.meta.json → 新名（meta.profile 同步改；
     注意 zip 内 manifest 仍是旧名，恢复会按旧名重建——报告里已建议音质满意后重存黄金包）；
  2) 语音预览缓存 voice_previews\\{旧名}_{lang}.wav → 直接删（纯缓存，新名首次试听自动重建）。
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
mp = json.loads((ROOT / "logs" / "rename_map_20260709.json").read_text(encoding="utf-8"))

gp = ROOT / "golden_packages"
pv = ROOT / "voice_previews"
for it in mp["renamed"]:
    old, new = it["old"], it["new"]
    for suf in (".zip", ".meta.json"):
        src = gp / f"{old}{suf}"
        if src.exists():
            dst = gp / f"{new}{suf}"
            src.rename(dst)
            print(f"黄金包改名: {src.name} → {dst.name}")
            if suf == ".meta.json":
                meta = json.loads(dst.read_text(encoding="utf-8"))
                meta["profile"] = new
                meta["note"] = f"VN2 改名迁移自「{old}」；包内快照仍为旧名，建议重存黄金包"
                dst.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    n = 0
    for f in pv.glob(f"{old}_*.wav"):
        f.unlink()
        n += 1
    if n:
        print(f"清理旧名试听缓存: {old} × {n}")
print("done")
