# -*- coding: utf-8 -*-
"""参考音裁剪 CLI — 按审计建议把过长/带静音头的参考音裁到最有表现力的一段。

流程（--apply 时）：
  ① 选段：pick_best_segment 滑窗按「音高+能量动态」评分取最优窗口（与审计
     判「平淡」同刻度），窗沿吸附静音帧防切字；
  ② 备份：原 wav/txt → *.bak_trim_<date>（可回滚）；
  ③ 新逐字稿：裁剪段送 AvatarHub STT(140:7854) 产 verbatim 文本——**逐字稿必须
     跟音频逐字对应**（zero_shot 的 prompt_text 语义），沿用旧稿=错稿必劣化；
     STT 不可用/失败 → **中止不动原件**（宁可不裁，不能错稿）；
  ④ 原位替换 wav + txt：预渲染指纹生命周期（Phase5 _ref.json content sha1）自动
     检测漂移 → 命中层拒陈旧库存回落现场合成（零错声窗口），夜间任务自动重渲。

不带 --apply＝干跑：只报告选段与前后指标，不动任何文件。
用法：python -m scripts.trim_reference_audio --ref config/voice_refs/lin_xiaoyu.wav
      [--target-sec 8] [--apply]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="参考音裁剪（选段+备份+STT 新逐字稿）")
    ap.add_argument("--ref", required=True, help="参考音 WAV 路径")
    ap.add_argument("--target-sec", type=float, default=8.0)
    ap.add_argument("--out", default="",
                    help="输出到新文件（不动原件；切情绪分库片段用，如 "
                         "lin_xiaoyu_happy.wav），sidecar 写 out 旁")
    ap.add_argument("--start", type=float, default=-1.0,
                    help="手动指定选段起点秒（与 --out 配合切指定情绪片段；"
                         "缺省=自动按韵律评分选段）")
    ap.add_argument("--apply", action="store_true",
                    help="真裁剪（默认干跑只报告选段）")
    args = ap.parse_args()

    from src.ai.reference_audio_audit import (
        _decode_wav,
        analyze_wav_bytes,
        pick_best_segment,
        write_wav_mono,
    )

    ref = Path(args.ref)
    if not ref.is_file():
        print(f"[!] 参考音不存在: {ref}")
        return 1
    raw = ref.read_bytes()
    before = analyze_wav_bytes(raw)
    if not before.get("ok"):
        print(f"[!] 无法分析: {before.get('detail')}")
        return 1
    a, sr, _nch, _sw = _decode_wav(raw)
    if args.start >= 0:
        s0 = float(args.start)
        s1 = min(len(a) / sr, s0 + float(args.target_sec))
    else:
        s0, s1 = pick_best_segment(a, sr, target_sec=args.target_sec)
    if s1 - s0 <= 0.5:
        print(f"[!] 选段过短（{s0}~{s1}s），放弃")
        return 1
    seg = a[int(s0 * sr): int(s1 * sr)]

    import io
    import wave as _wave

    import numpy as _np
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes((_np.clip(seg, -1, 1) * 32767).astype("<i2").tobytes())
    seg_bytes = buf.getvalue()
    after = analyze_wav_bytes(seg_bytes)

    print(f"[*] {ref.name}: {before.get('duration_sec')}s → 选段 {s0}~{s1}s "
          f"({round(s1 - s0, 2)}s)")
    print(f"    裁前: 能量std={before.get('energy_db_std')}dB "
          f"音高std={before.get('f0_semi_std')}semi 头静音={before.get('lead_silence_sec')}s")
    print(f"    裁后: 能量std={after.get('energy_db_std')}dB "
          f"音高std={after.get('f0_semi_std')}semi 头静音={after.get('lead_silence_sec')}s")

    if not args.apply:
        print("[*] 干跑结束（--apply 才真裁剪）")
        return 0

    # ── 新逐字稿（必须先成功，才动原件）──
    from scripts.voice_similarity_probe import _load_config
    from src.ai.avatar_voice import AvatarVoiceClient
    client = AvatarVoiceClient.from_config(_load_config())
    text = client.stt(seg_bytes, language="")     # 空串=服务端自动检测
    if not (text or "").strip():
        print("[!] STT 未产出逐字稿 → 中止（逐字稿错配比不裁更伤，原件未动）")
        return 1
    text = text.strip()
    print(f"[*] 新逐字稿: {text}")

    if args.out:
        # 切到新文件（情绪分库片段等）：原件与其 sidecar 不动
        out_path = Path(args.out)
        write_wav_mono(seg, sr, str(out_path))
        out_path.with_suffix(".txt").write_text(text, encoding="utf-8")
        print(f"[*] 已输出 {out_path}（含 sidecar 逐字稿；原件未动）")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak_wav = ref.with_name(ref.name + f".bak_trim_{stamp}")
    shutil.copy2(ref, bak_wav)
    sidecar = ref.with_suffix(".txt")
    if sidecar.is_file():
        shutil.copy2(sidecar, sidecar.with_name(sidecar.name + f".bak_trim_{stamp}"))
    write_wav_mono(seg, sr, str(ref))
    sidecar.write_text(text, encoding="utf-8")
    print(f"[*] 已替换 {ref.name}（备份 {bak_wav.name}）+ 逐字稿已更新")
    print("[*] 预渲染指纹将自动检测漂移：旧库存拒命中回落现场合成，夜间任务自动重渲")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
