# -*- coding: utf-8 -*-
"""voice_variant.py — 借音角色差异化：参考音变调变体（opt-in，默认只试听不落库）

背景：古天乐/张一健/葛优/阿龙2 共用刘德华参考音（用户指定），四角色音色完全相同。
原路线图设想"qwen3 instruct 差异化配音"——查证 qwen_tts 0.1.1 源码后否决：克隆链路
没有 instruct 入口，0.6B 连 CustomVoice 的 instruct 都强制置 None（见 qwen3_tts_server.py
/v1/tts/instruct 的 501 说明）。

替代方案（本工具）：对参考音做**半音级变调**生成变体——参考音变了，克隆出来的音色就变，
且对 fish/qwen3/cosyvoice 所有引擎生效（引擎无关），实时/离线链路都吃到差异化。
幅度建议 ±1~2 半音：保留"刘德华味"又能区分角色；超过 ±3 会明显失真。

默认 dry-run：只写试听文件 logs/voice_variants/<角色>_<±N>st.wav，用户听过满意再 --apply
（--apply 自动备份原音到 backups/voice_variant_<日期>/，可随时还原）。
不自动批量应用——用户明确指定过"都用刘德华的声音"，是否差异化由用户拍板。

用法（fishspeech 环境，需 librosa）:
  %CONDA_ROOT%\\envs\\fishspeech\\python.exe tools\\voice_variant.py --profile 葛优 --semitones -2
  ...\\python.exe tools\\voice_variant.py --profile 葛优 --semitones -2 --apply     # 听过满意后落库
  ...\\python.exe tools\\voice_variant.py --restore backups\\voice_variant_20260705\\葛优_voice.wav --profile 葛优

参考幅度（男声借刘德华音）: 葛优 -2 | 古天乐 -1 | 张一健 +1 | 阿龙2 +2
"""
import argparse
import base64
import io
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests

HUB = "http://127.0.0.1:9000"


def _wav_b64_to_np(b64: str):
    import soundfile as sf
    data, sr = sf.read(io.BytesIO(base64.b64decode(b64)), dtype="float32", always_2d=False)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    return data, sr


def _np_to_wav_b64(y, sr: int) -> str:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser(description="参考音变调变体（借音角色差异化，opt-in）")
    ap.add_argument("--profile", required=True, help="hub 角色名")
    ap.add_argument("--semitones", type=float, help="变调半音数(建议 ±1~2)")
    ap.add_argument("--apply", action="store_true", help="写回角色库(默认只出试听文件)")
    ap.add_argument("--restore", help="从备份 WAV 还原该角色参考音")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.restore:
        raw = Path(args.restore).read_bytes()
        r = requests.patch(f"{HUB}/profiles/{args.profile}", timeout=60,
                           json={"voice_b64": base64.b64encode(raw).decode()})
        print(f"还原 {args.profile}: HTTP {r.status_code} {r.text[:120]}")
        return

    if args.semitones is None:
        raise SystemExit("需要 --semitones（或 --restore）")

    p = requests.get(f"{HUB}/profiles/{args.profile}",
                     params={"include_face": "true"}, timeout=15).json()
    b64 = p.get("voice_b64", "")
    if not b64:
        raise SystemExit(f"角色「{args.profile}」无参考音")

    import librosa
    y, sr = _wav_b64_to_np(b64)
    shifted = librosa.effects.pitch_shift(y=y, sr=sr, n_steps=float(args.semitones))
    out_b64 = _np_to_wav_b64(shifted, sr)

    tag = f"{args.semitones:+.0f}st"
    preview = BASE / "logs" / "voice_variants" / f"{args.profile}_{tag}.wav"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(base64.b64decode(out_b64))
    print(f"试听: {preview}")

    if not args.apply:
        print("dry-run 结束（满意后加 --apply 落库；reference_text 不变，变调不改词）")
        return

    bak = BASE / "backups" / f"voice_variant_{datetime.now():%Y%m%d}"
    bak.mkdir(parents=True, exist_ok=True)
    (bak / f"{args.profile}_voice.wav").write_bytes(base64.b64decode(b64))
    r = requests.patch(f"{HUB}/profiles/{args.profile}", timeout=60, json={"voice_b64": out_b64})
    ok = r.status_code == 200 and r.json().get("ok")
    print(f"落库 {args.profile} ({tag}): {'OK' if ok else 'FAIL ' + r.text[:120]} | 原音备份: {bak}")


if __name__ == "__main__":
    main()
