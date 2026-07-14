# -*- coding: utf-8 -*-
"""qwen3_dub.py — Qwen3-TTS 离线批量配音 CLI（2026-07-05）

定位：把 Qwen3-TTS 的「商用许可 + 最高音色相似度 + 批推理吞吐」用起来的落地入口。
实时对话走 fish（RTF 0.34），**成品配音走这里**：长文案分句 → /v1/tts/clone/batch
批合成（.117 3060 实测 RTF 0.649，比逐句快 4.2×）→ 按句间停顿拼接成整段 WAV。

用法:
  python tools/qwen3_dub.py --profile 刘德华 --text "第一句。第二句！第三句？" --out dub.wav
  python tools/qwen3_dub.py --profile 葛优 --text-file script.txt --out dub.wav --gap-ms 260
  python tools/qwen3_dub.py --profile 皮特 --text-file en.txt --language en --out dub.wav
  （--url 可指到本机 5090 副本；--score 输出与参考音的 campplus 相似度）

音色来源 = hub 角色库（与直播同源），所以「直播像谁、配音就像谁」。
"""
import argparse
import base64
import io
import re
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import requests

HUB = "http://127.0.0.1:9000"
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…])\s*|\n+")


def _svc_headers() -> dict:
    try:
        tok = (Path(__file__).resolve().parent.parent / "secrets" / "service_token.txt"
               ).read_text(encoding="utf-8").strip()
        return {"X-AH-Svc": tok} if tok else {}
    except Exception:
        return {}


def split_sentences(text: str, max_chars: int = 120) -> list[str]:
    """按句末标点/换行分句；超长句再按逗号折半，防 max_new_tokens 截断。"""
    outs = []
    for seg in _SENT_SPLIT.split(text):
        seg = seg.strip()
        if not seg:
            continue
        while len(seg) > max_chars:
            cut = seg.rfind("，", 0, max_chars)
            cut = cut if cut > 20 else max_chars
            outs.append(seg[:cut].strip("，, "))
            seg = seg[cut:].strip("，, ")
        if seg:
            outs.append(seg)
    return outs


def profile_voice(name: str) -> tuple[str, str]:
    p = requests.get(f"{HUB}/profiles/{name}", params={"include_face": "true"}, timeout=15).json()
    b64 = p.get("voice_b64", "")
    if not b64:
        raise SystemExit(f"角色「{name}」无参考音（先在角色库配置克隆音）")
    return b64, (p.get("fish_tts_params") or {}).get("reference_text", "")


def _decode_wav_b64(b64: str) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sr


def main():
    ap = argparse.ArgumentParser(description="Qwen3-TTS 离线批量配音")
    ap.add_argument("--profile", required=True, help="hub 角色名（音色来源）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="配音文案（直接传字符串）")
    g.add_argument("--text-file", help="配音文案文件（UTF-8）")
    ap.add_argument("--out", required=True, help="输出 WAV 路径")
    ap.add_argument("--url", default="http://192.168.0.117:7858", help="qwen3 服务地址")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--gap-ms", type=int, default=200, help="句间停顿(默认 200ms)")
    ap.add_argument("--seed", type=int, default=123)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--score", action="store_true", help="输出与参考音的 campplus 相似度")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    text = args.text if args.text else Path(args.text_file).read_text(encoding="utf-8")
    sents = split_sentences(text)
    if not sents:
        raise SystemExit("文案为空")
    ref_b64, ref_text = profile_voice(args.profile)
    print(f"角色={args.profile} 分句={len(sents)} 目标={args.url}")

    hdrs = _svc_headers()
    t0 = time.time()
    r = requests.post(f"{args.url}/v1/tts/clone/batch", headers=hdrs, timeout=1800, json={
        "texts": sents, "reference_audio_b64": ref_b64, "reference_text": ref_text,
        "language": args.language, "temperature": args.temperature,
        "top_p": 0.7, "repetition_penalty": 1.2, "seed": args.seed})
    r.raise_for_status()
    d = r.json()
    wall = time.time() - t0

    sr = int(d["sample_rate"])
    gap = np.zeros(int(sr * args.gap_ms / 1000), dtype=np.float32)
    pieces = []
    for i, it in enumerate(d["results"]):
        pcm, isr = _decode_wav_b64(it["audio_base64"])
        assert isr == sr, f"采样率不一致: {isr} vs {sr}"
        pieces.append(pcm)
        if i != len(d["results"]) - 1:
            pieces.append(gap)
    full = np.concatenate(pieces)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((full * 32767).clip(-32768, 32767).astype(np.int16).tobytes())

    dur = len(full) / sr
    print(f"完成: {out} | 时长 {dur:.1f}s | 合成墙钟 {wall:.1f}s | RTF {wall / max(dur, 0.01):.2f} "
          f"(服务端批 RTF {d.get('rtf')})")

    if args.score:
        try:
            import clone_scorer
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes((full * 32767).clip(-32768, 32767).astype(np.int16).tobytes())
            res = clone_scorer.score_similarity(ref_b64, base64.b64encode(buf.getvalue()).decode())
            print(f"音色相似度: cosine={res.get('cosine')} ({res.get('label', res.get('detail'))})")
        except Exception as e:
            print(f"评分跳过: {e}")


if __name__ == "__main__":
    main()
