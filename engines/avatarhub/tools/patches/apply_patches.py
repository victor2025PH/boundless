#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新部署/重 clone 引擎后，一键回打本地补丁。

嵌套引擎仓库（ACE-Step / vendor/BestCam）不随主仓库推送，
其上的本地修复统一以 patch 文件存放在本目录，换机后执行：

    python tools/patches/apply_patches.py

已收录补丁：
- ace_step_local.patch:
  ①pipeline_ace_step.py  LoRA 离线加载适配（diffusers 0.33 offline 模式
    只认「目录+weight_name」，传文件全路径会在 _best_guess_weight_name 直接 raise）
  ②music_dcae_pipeline.py  torchaudio.load→soundfile.read
    （ymsvc 环境无 torchcodec，torchaudio 2.9 载音频强依赖它）
- bestcam_local.patch: CMakeLists 本机工具链适配。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))

PATCHES = [
    ("ACE-Step", "ace_step_local.patch"),
    (os.path.join("vendor", "BestCam"), "bestcam_local.patch"),
]


def main() -> int:
    rc = 0
    for repo, patch in PATCHES:
        repo_dir = os.path.join(BASE, repo)
        patch_path = os.path.join(HERE, patch)
        if not os.path.isdir(repo_dir):
            print(f"[SKIP] {repo}: 目录不存在（该引擎未部署）")
            continue
        if not os.path.exists(patch_path):
            print(f"[SKIP] {patch}: 补丁文件缺失")
            continue
        # 已打过（反向能干净应用）→ 跳过，保持幂等
        rev = subprocess.run(["git", "apply", "--check", "--reverse", patch_path],
                             cwd=repo_dir, capture_output=True)
        if rev.returncode == 0:
            print(f"[OK]   {repo}: 补丁已在（跳过）")
            continue
        chk = subprocess.run(["git", "apply", "--check", patch_path],
                             cwd=repo_dir, capture_output=True)
        if chk.returncode != 0:
            print(f"[FAIL] {repo}: 补丁不可应用（上游代码变了？需人工三方合并）\n"
                  f"       {chk.stderr.decode(errors='replace').strip()[:300]}")
            rc = 1
            continue
        ap = subprocess.run(["git", "apply", patch_path], cwd=repo_dir,
                            capture_output=True)
        if ap.returncode == 0:
            print(f"[OK]   {repo}: 补丁已应用")
        else:
            print(f"[FAIL] {repo}: 应用失败 {ap.stderr.decode(errors='replace')[:300]}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
