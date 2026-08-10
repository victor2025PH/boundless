# -*- coding: utf-8 -*-
"""修 SBV2 「styles(5) != style_vectors(6)」装载失败（P8 Fearful 融合遗留）。

背景：JVNV 融合（P8）给 style_vectors.npy 追加了第 6 行（Fearful），但
Data/<Model>/config.json 的 data.style2id / num_styles 仍是 5 → TTSModel.load()
校验失败，模型 ready 但 loaded=false（176:7861 /health 的 load_error 实锤）。

用法（在跑 sbv2_tts_server 的那台机器上，如 176）：
    python fix_sbv2_styles.py --model-dir C:\\SBV2\\Data\\LinXiaolingJVNV
    # 然后热装载（无需重启服务）：
    curl -X POST http://127.0.0.1:7861/v1/reload

行为：
  - 向量行数 == config 样式数：已一致，不动。
  - 向量行数 == config 样式数 + 1：按 P8 语义把缺的 "Fearful" 补进 style2id
    （索引=最后一行）并同步 num_styles；config.json 先备份 .bak-styles。
  - 其他不一致：只报告不动（避免瞎猜），人工核对 sbv2_emotion_styles.py 产物。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

MISSING_STYLE_NAME = "Fearful"  # P8 追加的第 6 情绪（见 tools/sbv2_jvnv_fuse.py）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=r"C:\SBV2\Data\LinXiaolingJVNV")
    ap.add_argument("--style-name", default=MISSING_STYLE_NAME)
    args = ap.parse_args()

    d = Path(args.model_dir)
    cfg_p = d / "config.json"
    vec_p = d / "style_vectors.npy"
    if not cfg_p.is_file() or not vec_p.is_file():
        print(f"!! 缺文件: {cfg_p} / {vec_p}")
        return 1

    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    data = cfg.setdefault("data", {})
    s2id = dict(data.get("style2id") or {})
    vec = np.load(vec_p)
    n_cfg, n_vec = len(s2id), int(vec.shape[0])
    print(f"config styles={n_cfg} {sorted(s2id, key=s2id.get)}")
    print(f"style_vectors rows={n_vec} shape={vec.shape}")

    if n_cfg == n_vec:
        print("OK 已一致，无需修复")
        return 0
    if n_vec != n_cfg + 1:
        print("!! 差异不是 +1，不敢自动修（人工核对 JVNV 融合产物）")
        return 2
    if args.style_name in s2id:
        print(f"!! {args.style_name} 已在 style2id 里但计数仍不一致，人工核对")
        return 2

    bak = cfg_p.with_suffix(".json.bak-styles")
    if not bak.exists():
        shutil.copy2(cfg_p, bak)
        print(f"备份 -> {bak.name}")
    s2id[args.style_name] = n_vec - 1
    data["style2id"] = s2id
    data["num_styles"] = n_vec
    cfg_p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    print(f"OK 已补 {args.style_name}={n_vec - 1}，num_styles={n_vec}")
    print("下一步: curl -X POST http://127.0.0.1:7861/v1/reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
