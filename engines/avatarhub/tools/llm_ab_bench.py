# -*- coding: utf-8 -*-
"""_llm_ab_bench.py — 同传翻译 LLM A/B 基准（临时脚本，用后归档）。
复刻 live_interpreter._translate_llm 的完整请求形状（system prompt / think:False /
temperature / num_ctx=2048），对候选模型实测：加载耗时、每句时延、解码 tok/s、
GPU 驻留字节（ollama /api/ps 的 size_vram/size）、译文质量（人工复核）与占位符保持。
用法: python _llm_ab_bench.py qwen2.5:32b   （单模型；模型间的 stop 由外层控制）
结果: 打印到 stdout 并追加 JSON 到 logs/optimize_20260705/llm_ab_results.jsonl
"""
import json, re, statistics, subprocess, sys, time

import requests

OLLAMA = "http://127.0.0.1:11434"
OUT = "logs/optimize_20260705/llm_ab_results.jsonl"
LANG = {"zh": "Chinese", "en": "English"}

# 语料：直播带货 + 会议 + 术语占位压力（Z1Q/Z2Q 即生产占位符格式）+ 长句
SENTS = [
    ("zh", "en", "你好，欢迎大家来到我的直播间，今天给大家带来几款新品。"),
    ("zh", "en", "这款产品现在下单立减五十，库存不多，喜欢的朋友抓紧时间。"),
    ("zh", "en", "请把 Z1Q 的最新功能给大家演示一下，注意保持 Z2Q 完全不变。"),
    ("zh", "en", "我们这套系统支持声音克隆加数字人加换脸，延迟能做到一秒出头，而且全部在本地运行，数据不出门。"),
    ("en", "zh", "Can you hear me clearly? Let's get started with today's meeting."),
    ("en", "zh", "The shipment will arrive in three business days, and you can track it online at any time."),
]


def sys_prompt(sl, dl):
    return (
        f"You are a professional real-time interpreter. Translate the user's {sl} text into {dl}. "
        f"Output ONLY the {dl} translation on a single line: no quotes, no pinyin, no explanations, no notes. "
        f"Preserve the meaning, tone and named entities, and produce natural spoken-style {dl}. "
        f"Keep any placeholder tokens shaped like Z1Q or Z2Q exactly unchanged."
    )


def call(model, src, dest, text, timeout=600):
    t0 = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": model,
        "messages": [{"role": "system", "content": sys_prompt(LANG[src], LANG[dest])},
                     {"role": "user", "content": text}],
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.2, "top_p": 0.8, "num_predict": 512, "num_ctx": 2048},
    }, timeout=timeout)
    r.raise_for_status()
    ms = (time.time() - t0) * 1000
    j = r.json()
    out = (j.get("message") or {}).get("content") or ""
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
    out = " ".join(ln.strip() for ln in out.splitlines() if ln.strip()).strip().strip('"').strip()
    ev, ed = j.get("eval_count"), j.get("eval_duration")
    tps = (ev / (ed / 1e9)) if ev and ed else None
    return ms, out, tps


def ollama_ps():
    try:
        models = requests.get(f"{OLLAMA}/api/ps", timeout=5).json().get("models", [])
        return [{"name": m.get("name"), "size": m.get("size"),
                 "size_vram": m.get("size_vram"),
                 "gpu_pct": round(100.0 * (m.get("size_vram") or 0) / m.get("size"), 1) if m.get("size") else None}
                for m in models]
    except Exception as e:
        return [{"error": str(e)}]


def nvsmi():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader"],
            text=True).strip()
        return out
    except Exception as e:
        return str(e)


def main():
    model = sys.argv[1]
    print(f"\n===== BENCH {model} =====", flush=True)
    t0 = time.time()
    call(model, "zh", "en", "你好")  # 加载+首句（不计入统计）
    load_s = time.time() - t0
    ps = ollama_ps()
    print(f"load+first_sentence: {load_s:.1f}s ; ps={json.dumps(ps, ensure_ascii=False)} ; vram={nvsmi()}", flush=True)
    lats, rows = [], []
    for src, dest, text in SENTS:
        ms, out, tps = call(model, src, dest, text)
        lats.append(ms)
        rows.append({"dir": f"{src}->{dest}", "ms": round(ms), "tps": round(tps or 0, 1), "src": text, "out": out})
        print(f"[{src}->{dest}] {ms:7.0f}ms  tps={round(tps or 0, 1):>6}  :: {out}", flush=True)
    med = statistics.median(lats)
    mx = max(lats)
    res = {"model": model, "load_first_s": round(load_s, 1), "median_ms": round(med),
           "max_ms": round(mx), "rows": rows, "ps": ps, "vram": nvsmi(),
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"== {model}: median {med:.0f}ms · max {mx:.0f}ms · gpu_resident {ps[0].get('gpu_pct') if ps else '?'}%", flush=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
