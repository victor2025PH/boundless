# -*- coding: utf-8 -*-
"""VLM 预热 CLI：把 qwen2.5vl:7b 装进 GPU 显存。

用途：
    - 开机/换模型后，跑一次本脚本，消掉第一个真实任务 ~56s 的冷启动。
    - 供 Windows Task Scheduler / systemd timer 做定时保活。

用法::

    python scripts/warmup_vlm.py              # 幂等，10 分钟内已 warmup 过则跳过
    python scripts/warmup_vlm.py --force      # 强制重新跑一次
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="忽略 10min TTL，强制重新 warmup")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    from src.host import ollama_vlm

    print("=" * 50)
    print(f"  VLM warmup  force={args.force}")
    print("=" * 50)
    state_before = ollama_vlm.get_warmup_state()
    print(f"  before: {json.dumps(state_before, ensure_ascii=False)}")

    r = ollama_vlm.warmup(force=args.force, timeout=args.timeout)
    print(f"  result: {json.dumps(r, ensure_ascii=False)}")

    state_after = ollama_vlm.get_warmup_state()
    print(f"  after:  {json.dumps(state_after, ensure_ascii=False)}")

    sys.exit(0 if r.get("ok") or r.get("skipped") else 1)


if __name__ == "__main__":
    main()
