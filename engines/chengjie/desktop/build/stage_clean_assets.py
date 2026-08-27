#!/usr/bin/env python3
"""干净包（clean 档）暂存：确保**不带任何数据种子**去打包。

背景：内测包（stage_internal_assets.py）把生产机的人设/语音/相册/KB 打成
build/seed-data 随包分发；对外发行的「全新安装包」一概不带这些数据——
用户装完知识库/人设库/相册/语音全空，由产品内向导与后台自行配置。

本脚本只做一件事并把它做死：删掉 build/seed-data（上一次内测打包的残留），
并验证删干净。electron-builder 对缺失的 extraResources 源目录只 warn 不拷
（app-builder-lib fileMatcher "file source doesn't exist"），于是产物里自然
没有 resources/seed-data，后端首启 _ensure_seeded_extras 整条链 no-op，
落回 config.desktop.min.yaml 标准形态。产物侧另有 after-pack.js 反向断言
（CHATX_ALLOW_STANDARD=1 时包里不得出现 seed-data），双保险。

用法（在 desktop/ 下）：
    python build/stage_clean_assets.py    # = npm run stage:clean

退出码：0 成功（已无种子）；1 删不掉（文件被占用等）。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent            # desktop/build
SEED = HERE / "seed-data"


def main() -> int:
    if not SEED.exists():
        print("✓ clean 档暂存：build/seed-data 本就不存在，包里不会有任何数据种子")
        return 0
    n_files = sum(1 for p in SEED.rglob("*") if p.is_file())
    for attempt in range(3):
        shutil.rmtree(SEED, ignore_errors=True)
        if not SEED.exists():
            break
        time.sleep(1.0 * (attempt + 1))
    if SEED.exists():
        print(f"✗ 删不掉 {SEED}（文件被占用？）——clean 档打包前必须清掉内测种子残留",
              file=sys.stderr)
        return 1
    print(f"✓ clean 档暂存：已清除内测种子残留 build/seed-data（{n_files} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
