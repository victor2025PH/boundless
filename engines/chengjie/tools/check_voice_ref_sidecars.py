# -*- coding: utf-8 -*-
"""底样逐字稿一致性体检（2026-08-03，乱码事故防再犯）。

事故：mizuki / lin_jiaxin 的 ``config/voice_refs/<pid>.wav`` 被换过录音但
``.txt`` 逐字稿没同步 → 错误 reference_text 污染 7852 混合保真路径的合成
prompt，模型「续写参考音」而非念目标文本——产物是**同音色乱码**（campplus
声纹分 0.94+ 照样过，只有转写回验能抓）。逐字稿契约本要求「逐字一致，错一个
字都影响相似度」，但此前**没有任何体检在验**这条契约。

本工具：STT（140:7854 Whisper）转写每个底样 → 与 sidecar 归一化相似度：
  ≥0.80 过 / 0.60~0.80 警（可能只是转写误差，建议人工听）/ <0.60 坏 ⚠
  （多半是「换了录音没换稿」，用 ``D:\\tmp`` 里的流程或人工重写 .txt）。

用法：python tools/check_voice_ref_sidecars.py [--data-root PATH] [--min 0.80]
退出码：0=无坏项；2=有坏项（清单见输出）。适合并入周批/换底后必跑。
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import List, Optional

_ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE))


def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="底样逐字稿一致性体检（只读）")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--min", type=float, default=0.80, dest="min_ok")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    from src.ai.avatar_voice import AvatarVoiceClient
    from tools.qc_emotion_refs import text_match_ratio

    root = resolve_data_roots(args.data_root)[0]
    refs_dir = root / "config" / "voice_refs"
    if not refs_dir.is_dir():
        print(f"SKIP: 无 {refs_dir}")
        return 0
    client = AvatarVoiceClient({"enabled": True})
    bad: List[str] = []
    warn: List[str] = []
    wavs = sorted(p for p in refs_dir.glob("*.wav") if ".bak" not in p.name)
    print(f"=== 底样逐字稿体检（{refs_dir}，{len(wavs)} 个）===")
    for wav in wavs:
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            print(f"  [无稿] {wav.name}（保真路径退化为纯 zero_shot，可接受但建议补）")
            continue
        expect = txt.read_text(encoding="utf-8").strip()
        stt = client.stt(wav.read_bytes(), language="") or ""
        if not stt:
            print(f"  [跳过] {wav.name}: STT 不可用/静音")
            continue
        ratio = text_match_ratio(expect, stt)
        if ratio < 0.60:
            mark = "坏 ⚠"
            bad.append(wav.stem)
        elif ratio < args.min_ok:
            mark = "警"
            warn.append(wav.stem)
        else:
            mark = "过"
        print(f"  [{mark}] {ratio:.2f} {wav.name}")
        if ratio < args.min_ok:
            print(f"        稿: {expect[:46]}")
            print(f"        音: {stt[:46]}")
    print(f"\n汇总：坏 {len(bad)}（{', '.join(bad) or '-'}），"
          f"警 {len(warn)}（{', '.join(warn) or '-'}）。")
    if bad:
        print("坏=换了录音没换稿：备份旧 .txt 后用 STT 全文重写，"
              "再重跑 bootstrap_emotion_refs（勿带 --reuse）。")
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
