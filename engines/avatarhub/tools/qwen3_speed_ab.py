# -*- coding: utf-8 -*-
"""qwen3_speed_ab.py — Qwen3-TTS 提速方案 A/B 实测（2026-07-05）

对比三种合成模式的 RTF 与音色相似度（同一参考音、同一文本集）：
  A) 单条 · non_streaming=False   —— qwen_tts 默认（模拟流式喂文本），上一轮基线
  B) 单条 · non_streaming=True    —— 全文预填（服务端新默认，验证速度/质量不劣化）
  C) 批量 · /v1/tts/clone/batch   —— 批推理吞吐档（离线配音主路径，批内共享循环开销）

相似度用 clone_scorer(campplus 余弦)，参考音取 hub 刘德华 profile（与上轮 A/B 同源可比）。
产物: logs/qwen3_ab_batch_YYYYMMDD.json + 控制台摘要。

用法:
  python tools/qwen3_speed_ab.py                       # 测 .117 线上
  python tools/qwen3_speed_ab.py --url http://127.0.0.1:7859   # 测本机 5090 副本
  python tools/qwen3_speed_ab.py --skip-single        # 只测批量（快速回归）
"""
import argparse
import base64
import io
import json
import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

import requests

HUB = "http://127.0.0.1:9000"

# 8 句中文测试集：长短句混合，贴近离线配音的真实分句分布
TEXTS = [
    "大家好，欢迎来到今天的直播间。",
    "这个产品我自己用了三个月，效果确实不错。",
    "点击下方链接就可以直接下单了。",
    "今天的优惠力度是全年最大的，错过要再等一年。",
    "有什么问题都可以在评论区留言。",
    "我们的售后服务是七天无理由退换。",
    "感谢大家的支持，我们明天同一时间再见。",
    "记得关注我，不迷路。",
]


def _svc_headers() -> dict:
    tok = ""
    try:
        tok = (Path("secrets/service_token.txt").read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return {"X-AH-Svc": tok} if tok else {}


def _wav_seconds(b64: str) -> float:
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        return w.getnframes() / max(w.getframerate(), 1)


def donor_ref() -> tuple[str, str]:
    p = requests.get(f"{HUB}/profiles/刘德华", params={"include_face": "true"}, timeout=15).json()
    b64 = p.get("voice_b64", "")
    ref_text = (p.get("fish_tts_params") or {}).get("reference_text", "")
    assert b64, "刘德华 profile 无参考音"
    return b64, ref_text


def score(ref_b64: str, syn_b64: str) -> float | None:
    try:
        import clone_scorer
        r = clone_scorer.score_similarity(ref_b64, syn_b64)
        return r.get("cosine") if r.get("ok") else None
    except Exception:
        return None


def run_single(url, hdrs, ref_b64, ref_text, texts, non_streaming: bool):
    times, secs, coss = [], [], []
    for t in texts:
        t0 = time.time()
        r = requests.post(f"{url}/v1/tts/clone", headers=hdrs, timeout=300, json={
            "text": t, "reference_audio_b64": ref_b64, "reference_text": ref_text,
            "language": "zh", "temperature": 0.7, "top_p": 0.7,
            "repetition_penalty": 1.2, "seed": 123, "non_streaming": non_streaming})
        r.raise_for_status()
        wall = time.time() - t0
        a = r.json()["audio_base64"]
        times.append(wall)
        secs.append(_wav_seconds(a))
        c = score(ref_b64, a)
        if c is not None:
            coss.append(c)
        print(f"    「{t[:12]}…」 wall={wall:.1f}s audio={secs[-1]:.1f}s "
              f"rtf={wall / max(secs[-1], 0.01):.2f} cos={c}")
    return {"wall_s": round(sum(times), 2), "audio_s": round(sum(secs), 2),
            "rtf": round(sum(times) / max(sum(secs), 0.01), 3),
            "cos_mean": round(sum(coss) / len(coss), 4) if coss else None,
            "n": len(texts)}


def run_batch(url, hdrs, ref_b64, ref_text, texts, non_streaming: bool):
    t0 = time.time()
    r = requests.post(f"{url}/v1/tts/clone/batch", headers=hdrs, timeout=600, json={
        "texts": texts, "reference_audio_b64": ref_b64, "reference_text": ref_text,
        "language": "zh", "temperature": 0.7, "top_p": 0.7,
        "repetition_penalty": 1.2, "seed": 123, "non_streaming": non_streaming})
    r.raise_for_status()
    wall = time.time() - t0
    d = r.json()
    coss = []
    for it in d["results"]:
        c = score(ref_b64, it["audio_base64"])
        if c is not None:
            coss.append(c)
    audio_s = d.get("audio_seconds") or sum(i["seconds"] for i in d["results"])
    return {"wall_s": round(wall, 2), "audio_s": round(audio_s, 2),
            "rtf": round(wall / max(audio_s, 0.01), 3),
            "cos_mean": round(sum(coss) / len(coss), 4) if coss else None,
            "cos_each": [round(c, 3) for c in coss], "n": len(texts)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://192.168.0.117:7858")
    ap.add_argument("--skip-single", action="store_true")
    ap.add_argument("--n-single", type=int, default=4, help="单条模式测前 N 句(省时)")
    ap.add_argument("--batch-nonstream", type=int, default=1, choices=(0, 1),
                    help="批量档 non_streaming 取值(默认1；0=qwen_tts 原始喂法)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    hdrs = _svc_headers()
    ref_b64, ref_text = donor_ref()
    print(f"目标: {args.url} | 参考音: 刘德华({len(ref_b64)}b64)")

    # 预热：参考 prompt 进缓存 + CUDA 编译暖身（不计入任何一组）
    requests.post(f"{args.url}/v1/refs/prewarm", headers=hdrs, timeout=120,
                  json={"references": [{"audio_b64": ref_b64, "text": ref_text}]})
    requests.post(f"{args.url}/v1/tts/clone", headers=hdrs, timeout=300, json={
        "text": "预热句子。", "reference_audio_b64": ref_b64, "reference_text": ref_text,
        "language": "zh", "seed": 123})
    print("预热完成\n")

    out = {"ts": datetime.now().isoformat(timespec="seconds"), "url": args.url,
           "texts": TEXTS, "modes": {}}

    if not args.skip_single:
        sub = TEXTS[:args.n_single]
        print(f"[A] 单条 non_streaming=False (基线) × {len(sub)}")
        out["modes"]["single_stream_sim"] = run_single(args.url, hdrs, ref_b64, ref_text, sub, False)
        print(f"\n[B] 单条 non_streaming=True (全文预填) × {len(sub)}")
        out["modes"]["single_nonstream"] = run_single(args.url, hdrs, ref_b64, ref_text, sub, True)

    bns = bool(args.batch_nonstream)
    print(f"\n[C] 批量 batch={len(TEXTS)} non_streaming={bns}")
    out["modes"][f"batch_ns{int(bns)}"] = run_batch(args.url, hdrs, ref_b64, ref_text, TEXTS, bns)

    print("\n===== 汇总 =====")
    for k, v in out["modes"].items():
        print(f"{k:22s} rtf={v['rtf']:<6} wall={v['wall_s']:<7} audio={v['audio_s']:<7} "
              f"cos={v['cos_mean']}")

    day = datetime.now().strftime("%Y%m%d")
    p = Path("logs") / f"qwen3_ab_batch_{day}.json"
    if p.exists():                       # 同日多次运行 → 合并 modes，别覆盖前一组
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            merged = dict(old.get("modes") or {})
            merged.update(out["modes"])
            out["modes"] = merged
        except Exception:
            pass
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写 {p}")


if __name__ == "__main__":
    main()
