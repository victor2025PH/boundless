# -*- coding: utf-8 -*-
"""P5 实验室复测：最新 llama.cpp(b9902) 跑 Hy-MT2 7B Q4_K_M(官方 GGUF,复用 ollama blob)。
背景：2026-07-07 同一 GGUF 在 ollama 0.24.0 下坏(空输出+hy token 泄漏,详见 deploy.env.bat)；
llama.cpp 五月已合入混元系列修复。本探针不动生产,直连 llama-server /v1/chat/completions。
验收：① 占位符 14/14 存活 ② 数字忠实(立减五十≠50% off) ③ 长句非空 ④ 无控制 token 泄漏。
结果落 logs/optimize_20260707/mt2_lab_results.jsonl。用后归档。"""
import json
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:8180"
OUT = Path(__file__).resolve().parent.parent / "logs" / "optimize_20260707" / "mt2_lab_results.jsonl"
LANG = {"zh": "Chinese", "en": "English"}

# 与 _p1_mt_placeholder_probe.py 同套占位符句(可比),外加数字/长句/控制 token 专项
PH_SENTS = [
    ("zh", "en", "请把 Z1Q 的最新功能给大家演示一下，注意保持 Z2Q 完全不变。"),
    ("zh", "en", "今天 Z1Q 直播间下单的朋友，都送 Z2Q 的周边礼包。"),
    ("zh", "en", "Z1Q 这个型号比 Z2Q 贵两百块，但是续航翻倍。"),
    ("zh", "en", "麻烦把 Z1Q 的物流单号发到群里。"),
    ("en", "zh", "Our Z1Q platform integrates face swap and voice cloning with Z2Q latency under one second."),
    ("en", "zh", "The Z1Q firmware update fixes the Z2Q pairing issue reported last week."),
    ("en", "zh", "Please ship the Z1Q samples to our Shenzhen office by Friday."),
    ("en", "zh", "Z1Q outperforms Z2Q in every benchmark we ran."),
]
NUM_SENTS = [
    ("zh", "en", "这款产品现在下单立减五十。"),
    ("zh", "en", "先试用七天，满意再付尾款三千二。"),
]
LONG_SENT = ("zh", "en",
             "我们这套系统把实时语音识别、大模型翻译、声音克隆和数字人口型驱动整合在一条链路里，"
             "端到端延迟能压到一秒出头，直播和跨国会议都能直接用，今天下单的朋友还送一年的技术支持服务。")


def mt(src: str, dst: str, text: str) -> tuple[str, float]:
    """官方推荐提示词与采样参数(模型卡: temperature 0.7 / top_p 0.6 / top_k 20)。"""
    t0 = time.time()
    r = requests.post(f"{BASE}/v1/chat/completions", json={
        "model": "hy-mt2-7b",
        "messages": [{"role": "user",
                      "content": f"Translate the following segment into {LANG[dst]}, "
                                 f"without additional explanation：{text}"}],
        "temperature": 0.7, "top_p": 0.6, "top_k": 20, "max_tokens": 512,
    }, timeout=600)
    r.raise_for_status()
    out = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return " ".join(ln.strip() for ln in out.splitlines() if ln.strip()).strip(), time.time() - t0


def main():
    rows = []
    ok = tot = 0
    fails = []
    lat = []
    print("== Hy-MT2 7B Q4_K_M @ llama.cpp b9902 (CPU lab) ==")
    for src, dst, text in PH_SENTS:
        out, dt = mt(src, dst, text)
        lat.append(dt)
        rows.append({"kind": "placeholder", "src": text, "out": out, "s": round(dt, 1)})
        for ph in ("Z1Q", "Z2Q"):
            if ph in text:
                tot += 1
                if ph in out:
                    ok += 1
                else:
                    fails.append((ph, text[:24], out[:60]))
        print(f"  [{dt:4.1f}s] {text[:30]}\n         -> {out[:80]}")
    print(f"\n占位符存活 {ok}/{tot}")
    for ph, t, o in fails:
        print(f"   丢 {ph}: {t}… -> {o}…")

    print("\n-- 数字忠实 --")
    num_bad = []
    for src, dst, text in NUM_SENTS:
        out, dt = mt(src, dst, text)
        lat.append(dt)
        rows.append({"kind": "number", "src": text, "out": out, "s": round(dt, 1)})
        print(f"  [{dt:4.1f}s] {text} -> {out[:90]}")
        if "五十" in text and not any(k in out for k in ("50", "fifty")):
            num_bad.append(text)
        if "三千二" in text and not any(k in out for k in ("3,200", "3200", "3.2")):
            num_bad.append(text)

    print("\n-- 长句非空 --")
    src, dst, text = LONG_SENT
    out, dt = mt(src, dst, text)
    lat.append(dt)
    rows.append({"kind": "long", "src": text, "out": out, "s": round(dt, 1)})
    print(f"  [{dt:4.1f}s] {len(out)} chars -> {out[:110]}")
    long_ok = len(out) >= 40

    leak = [r for r in rows if any(tk in r["out"] for tk in
                                   ("hy_begin", "hy_end", "<|", "|>", "hy_place"))]
    verdict = {"placeholder": f"{ok}/{tot}", "number_bad": num_bad, "long_ok": long_ok,
               "token_leak": len(leak), "lat_med_s": round(sorted(lat)[len(lat) // 2], 1)}
    print(f"\n== 结论 == {json.dumps(verdict, ensure_ascii=False)}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "engine": "llama.cpp-b9902-cpu", "model": "Hy-MT2-7B-Q4_K_M",
                            "verdict": verdict, "rows": rows}, ensure_ascii=False) + "\n")
    print(f"结果已追加 {OUT}")


if __name__ == "__main__":
    main()
