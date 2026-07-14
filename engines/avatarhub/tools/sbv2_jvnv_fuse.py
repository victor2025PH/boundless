# -*- coding: utf-8 -*-
"""P8 JVNV 情感融合(style 向量层)：把 JVNV F2 真人强情绪的「表达方式」迁移进林小玲音色。

原理：SBV2 的 style 向量空间(wespeaker 说话人嵌入)里，「情绪」表现为相对说话人均值的
偏移方向。对 JVNV F2 每个情绪求 delta = F2_情绪均值 − F2_全体均值，再把 delta 加到
林小玲的均值/现有情绪向量上——**方向来自真人演绎(强)，落点仍在林小玲的音色邻域(稳)**。

用法（sbv2 env）：
  python tools\\sbv2_jvnv_fuse.py                       # 融合到 Data\\LinXiaoling_JP
  python tools\\sbv2_jvnv_fuse.py --model-dir C:\\SBV2\\Data\\LinXiaolingJVNV --beta 0.8
"""
from __future__ import annotations

import argparse
import functools
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

JVNV_DIR_DEFAULT = r"C:\SBV2\Data\_jvnv_src\F2"
# JVNV 6 情绪 → 本系统 style；disgust 弃用(通话场景低频,省一行)
EMO_MAP = {"anger": "Angry", "happy": "Happy", "sad": "Sad",
           "surprise": "Surprised", "fear": "Fearful"}

_INFER = None


def _get_inference(device: str):
    global _INFER
    if _INFER is not None:
        return _INFER
    import torch
    if not getattr(torch.load, "_wo_patched", False):
        _orig = torch.load

        @functools.wraps(_orig)
        def _load(*a, **k):
            k["weights_only"] = False
            return _orig(*a, **k)

        _load._wo_patched = True
        torch.load = _load
    from pyannote.audio import Inference, Model
    model = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM")
    inf = Inference(model, window="whole")
    try:
        inf.to(torch.device(device))
    except Exception:
        inf.to(torch.device("cpu"))
    _INFER = inf
    return _INFER


def _embed(wav: Path, device: str) -> np.ndarray:
    npy = wav.with_suffix(wav.suffix + ".styv.npy")   # 侧车缓存(与训练 .npy 区分)
    if npy.exists():
        return np.load(npy).astype(np.float32)
    v = np.asarray(_get_inference(device)(str(wav)), dtype=np.float32)
    np.save(npy, v)
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jvnv-dir", default=JVNV_DIR_DEFAULT)
    ap.add_argument("--model-dir", default=r"C:\SBV2\Data\LinXiaoling_JP")
    ap.add_argument("--beta", type=float, default=0.6,
                    help="既有情绪向量上叠加 JVNV delta 的强度")
    ap.add_argument("--fear-beta", type=float, default=0.8,
                    help="新建 Fearful 向量的 delta 强度(无旧向量,直接从均值出发)")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    jdir = Path(args.jvnv_dir)
    mdir = Path(args.model_dir)
    sv_path = mdir / "style_vectors.npy"
    cfg_path = mdir / "config.json"
    if not (sv_path.is_file() and cfg_path.is_file()):
        raise SystemExit(f"model-dir 不完整: {mdir}")

    wavs = sorted(jdir.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"JVNV wav 不存在: {jdir}")

    by_emo: dict[str, list[np.ndarray]] = defaultdict(list)
    allv: list[np.ndarray] = []
    t0 = time.time()
    for i, w in enumerate(wavs):
        emo = w.stem.split("_")[1]        # F2_anger_free_01 → anger
        try:
            v = _embed(w, args.device)
        except Exception as e:
            print(f"[err] {w.name}: {str(e)[:70]}")
            continue
        allv.append(v)
        if emo in EMO_MAP:
            by_emo[emo].append(v)
        if (i + 1) % 80 == 0:
            print(f"[emb] {i+1}/{len(wavs)} ({time.time()-t0:.0f}s)")

    if len(allv) < 50:
        raise SystemExit("有效嵌入过少,中止")
    f2_mean = np.mean(np.stack(allv), axis=0)
    deltas = {EMO_MAP[e]: np.mean(np.stack(vs), axis=0) - f2_mean
              for e, vs in by_emo.items() if vs}
    for st, d in deltas.items():
        print(f"[delta] {st}: |d|={float(np.linalg.norm(d)):.3f} n={len(by_emo.get({v:k for k,v in EMO_MAP.items()}[st], []))}")

    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    style2id: dict = cfg["data"]["style2id"]
    sv = np.load(sv_path).astype(np.float32)
    lx_mean = sv[style2id.get("Neutral", 0)]

    backup = sv_path.with_suffix(".npy.prefuse")
    if not backup.exists():
        shutil.copy2(sv_path, backup)
        print(f"[backup] {backup.name}")

    rows = list(sv)
    changed = []
    for st, d in deltas.items():
        if st in style2id:
            i = style2id[st]
            rows[i] = rows[i] + args.beta * d          # 旧向量方向保留,叠真人 delta
            changed.append(f"{st}(+{args.beta}d)")
        else:
            style2id[st] = len(rows)
            rows.append(lx_mean + args.fear_beta * d)  # 新 style(如 Fearful)
            changed.append(f"{st}(new,{args.fear_beta}d)")

    np.save(sv_path, np.stack(rows).astype(np.float32))
    cfg["data"]["style2id"] = style2id
    cfg["data"]["num_styles"] = len(rows)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[save] {sv_path}  styles={list(style2id)}  changed={changed}")
    print("DONE fuse")


if __name__ == "__main__":
    main()
