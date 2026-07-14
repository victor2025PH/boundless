# -*- coding: utf-8 -*-
"""P5d SBV2 情感 style 向量生成。

将 LinXiaoling_JP 训练集按文件名前缀(情绪)分组，生成：
  1) 每条 wav 的 per-utterance style .npy（供训练用）
  2) 多情绪 style_vectors.npy + config.json style2id（供推理选风格）

用法（在 C:\\SBV2 venv 下）：
  python C:\\模仿音色\\tools\\sbv2_emotion_styles.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

SBV2_ROOT = Path(r"C:\SBV2")
DATA = SBV2_ROOT / "Data" / "LinXiaoling_JP"
WAVS = DATA / "wavs"
CONFIG = DATA / "config.json"

EMO_MAP = {
    "neutral": "Neutral",
    "happy": "Happy",
    "sad": "Sad",
    "angry": "Angry",
    "surprised": "Surprised",
}


def _ensure_sbv2_path():
    root = str(SBV2_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


_INFER = None


def _get_inference(device: str = "cpu"):
    global _INFER
    if _INFER is not None:
        return _INFER
    _ensure_sbv2_path()
    import functools
    import torch
    # torch>=2.6 默认 weights_only=True；pyannote 权重(本地 HF 缓存,可信来源)用旧式
    # pickle 存档 → 统一强制 weights_only=False(allowlist 逐个放行不可枚举,不值得)。
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
    inf.to(torch.device(device))
    _INFER = inf
    return _INFER


def _extract_style(wav_path: str, device: str = "cpu") -> np.ndarray:
    return np.asarray(_get_inference(device)(wav_path), dtype=np.float32)  # type: ignore


def main():
    if not WAVS.is_dir():
        raise SystemExit(f"wavs 目录不存在: {WAVS}")

    wavs = sorted(WAVS.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"无 wav 文件: {WAVS}")

    by_emo: dict[str, list[np.ndarray]] = defaultdict(list)
    all_vecs: list[np.ndarray] = []
    ok = skip = 0

    for wav in wavs:
        prefix = wav.stem.split("_")[0].lower()
        if prefix not in EMO_MAP:
            print(f"[skip] 未知情绪前缀: {wav.name}")
            skip += 1
            continue
        npy = wav.with_suffix(wav.suffix + ".npy")
        if npy.exists():
            vec = np.load(npy).astype(np.float32)
        else:
            try:
                vec = _extract_style(str(wav))
                np.save(npy, vec)
            except Exception as e:
                print(f"[err] {wav.name}: {e}")
                skip += 1
                continue
        by_emo[prefix].append(vec)
        all_vecs.append(vec)
        ok += 1
        if ok % 20 == 0:
            print(f"[style] {ok} done")

    if not all_vecs:
        raise SystemExit("无有效 style 向量")

    # Neutral = 全量均值；各情绪 = 该组均值
    global_mean = np.mean(np.stack(all_vecs), axis=0)
    style_vectors = [global_mean]
    style2id = {"Neutral": 0}
    for emo_key, style_name in EMO_MAP.items():
        if emo_key == "neutral":
            continue
        vecs = by_emo.get(emo_key, [])
        if not vecs:
            print(f"[warn] 情绪 {emo_key} 无样本，跳过")
            continue
        mean = np.mean(np.stack(vecs), axis=0)
        style2id[style_name] = len(style_vectors)
        style_vectors.append(mean)

    out_npy = DATA / "style_vectors.npy"
    np.save(out_npy, np.stack(style_vectors, axis=0))
    print(f"[save] {out_npy}  shapes={np.load(out_npy).shape}  styles={list(style2id)}")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg.setdefault("data", {})
    cfg["data"]["num_styles"] = len(style2id)
    cfg["data"]["style2id"] = style2id
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[save] {CONFIG}  style2id={style2id}")

    counts = {k: len(v) for k, v in by_emo.items()}
    print(f"DONE ok={ok} skip={skip}  per_emo={counts}")


if __name__ == "__main__":
    main()
