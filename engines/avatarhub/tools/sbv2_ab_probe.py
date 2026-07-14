# -*- coding: utf-8 -*-
"""P6 A/B 对比探针：SBV2(7861) vs CosyVoice3(7852) 日语情感配音。
逐句测「整句合成延迟」并把两边 wav 落盘到 logs\\sbv2_ab\\ 供人工听感对比。

用法(任意有 requests 的环境)：
  python tools\\sbv2_ab_probe.py            # 全 8 句
  python tools\\sbv2_ab_probe.py --quick    # 只测 3 句(快速冒烟)
"""
import argparse
import base64
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "logs" / "sbv2_ab"
OUT.mkdir(parents=True, exist_ok=True)

SBV2 = "http://127.0.0.1:7861"
COSY = "http://127.0.0.1:7852"

CASES = [
    ("neutral",   "", "明日の会議は午後三時からです。"),
    ("happy",     "用开心愉快的语气说", "わあ、今日は最高の一日でした！"),
    ("excited",   "用兴奋激动的语气说", "やった、ついにできたよ！"),
    ("sad",       "用悲伤难过的语气说", "はぁ…今日はもう何もする気になれないよ…。"),
    ("angry",     "用愤怒生气的语气说", "もう、何回同じこと言わせるの！"),
    ("surprised", "用惊讶的语气说",     "えっ、本当に今日だったの！？"),
    ("neutral",   "", "この資料、目を通しておいてもらえると助かります。"),
    ("happy",     "用开心愉快的语气说", "ハハハ、その話、何回聞いても笑っちゃうよ！"),
]


def _ref_b64() -> str:
    p = BASE / "refs" / "interp_林小玲.wav"
    return base64.b64encode(p.read_bytes()).decode() if p.is_file() else ""


def probe_sbv2(text: str, emo: str) -> tuple[float, bytes]:
    t0 = time.time()
    r = requests.post(f"{SBV2}/v1/tts",
                      json={"text": text, "language": "ja", "emotion": emo,
                            "return_base64": True}, timeout=120)
    r.raise_for_status()
    return time.time() - t0, base64.b64decode(r.json()["audio_base64"])


def probe_cosy(text: str, instruct: str, ref: str) -> tuple[float, bytes]:
    ep = "/v1/tts/instruct" if instruct else "/v1/tts/clone"
    payload = {"text": text, "reference_audio_b64": ref, "return_base64": True}
    if instruct:
        payload["instruct"] = instruct
    t0 = time.time()
    r = requests.post(f"{COSY}{ep}", json=payload, timeout=180)
    r.raise_for_status()
    return time.time() - t0, base64.b64decode(r.json()["audio_base64"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-cosy", action="store_true")
    args = ap.parse_args()
    cases = CASES[:3] if args.quick else CASES
    ref = _ref_b64()

    rows = []
    for i, (emo, instruct, text) in enumerate(cases):
        row = {"i": i, "emo": emo, "text": text}
        try:
            dt, wav = probe_sbv2(text, emo)
            (OUT / f"{i:02d}_{emo}_sbv2.wav").write_bytes(wav)
            row["sbv2_s"] = round(dt, 2)
        except Exception as e:
            row["sbv2_s"] = f"ERR {str(e)[:40]}"
        if not args.skip_cosy:
            try:
                dt, wav = probe_cosy(text, instruct, ref)
                (OUT / f"{i:02d}_{emo}_cosy.wav").write_bytes(wav)
                row["cosy_s"] = round(dt, 2)
            except Exception as e:
                row["cosy_s"] = f"ERR {str(e)[:40]}"
        print(row)
        rows.append(row)

    (OUT / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = [r for r in rows if isinstance(r.get("sbv2_s"), float)]
    if ok:
        avg = sum(r["sbv2_s"] for r in ok) / len(ok)
        print(f"\nSBV2 avg {avg:.2f}s over {len(ok)} sentences  → wavs in {OUT}")


if __name__ == "__main__":
    main()
