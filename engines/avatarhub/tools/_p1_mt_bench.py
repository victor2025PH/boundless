# -*- coding: utf-8 -*-
"""P1-3 翻译换代 A/B 基准：qwen2.5:14b(现役) vs Hy-MT2(候选)。
复用 live_interpreter._llm_req_body —— 测的就是生产请求形状(含 Hy-MT 官方指令模板)。
指标：每句时延 / 解码 tok/s / VRAM 驻留 / Z1Q-Z2Q 占位符存活 / 译文(人工复核)。
用法: python _p1_mt_bench.py qwen2.5:14b kaelri/hy-mt2:7b-q4_K_M ...
结果: stdout + 追加 logs/optimize_20260707/mt_ab_results.jsonl
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("INTERP_STREAM_ASR", "0")
import live_interpreter as li

OLLAMA = "http://127.0.0.1:11434"
OUT = Path(__file__).resolve().parent.parent / "logs" / "optimize_20260707" / "mt_ab_results.jsonl"

# 直播带货 + 会议 + 占位符压力 + 品牌词 + 长句(与 07-05 基准同源,可横向比)
SENTS = [
    ("zh", "en", "你好，欢迎大家来到我的直播间，今天给大家带来几款新品。"),
    ("zh", "en", "这款产品现在下单立减五十，库存不多，喜欢的朋友抓紧时间。"),
    ("zh", "en", "请把 Z1Q 的最新功能给大家演示一下，注意保持 Z2Q 完全不变。"),
    ("zh", "en", "我们这套系统支持声音克隆加数字人加换脸，延迟能做到一秒出头，而且全部在本地运行，数据不出门。"),
    ("en", "zh", "Can you hear me clearly? Let's get started with today's meeting."),
    ("en", "zh", "The shipment will arrive in three business days, and you can track it online at any time."),
    ("en", "zh", "Our Z1Q platform integrates face swap and voice cloning with Z2Q latency under one second."),
    ("zh", "en", "那个订单的尾款麻烦今天之内结一下，不然赶不上这批船期了。"),
]


def call(model, src, dest, text, timeout=600):
    t0 = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json=li._llm_req_body(model, text, src, dest) | {"keep_alive": "5m"},
                      timeout=timeout)
    r.raise_for_status()
    ms = (time.time() - t0) * 1000
    j = r.json()
    out = (j.get("message") or {}).get("content") or ""
    out = li._THINK_RE.sub("", out)
    out = " ".join(ln.strip() for ln in out.splitlines() if ln.strip()).strip().strip('"').strip()
    ev, ed = j.get("eval_count"), j.get("eval_duration")
    tps = (ev / (ed / 1e9)) if ev and ed else None
    return ms, out, tps


def ollama_ps():
    try:
        j = requests.get(f"{OLLAMA}/api/ps", timeout=5).json()
        return {m["name"]: {"vram_gb": round(m.get("size_vram", 0) / 1e9, 1),
                            "total_gb": round(m.get("size", 0) / 1e9, 1)}
                for m in j.get("models", [])}
    except Exception:
        return {}


def bench(model):
    print(f"\n===== {model} =====")
    t0 = time.time()
    try:
        call(model, "zh", "en", "你好")           # 预热/加载
    except Exception as e:
        print(f"  加载失败: {e}")
        return None
    load_s = time.time() - t0
    ps = ollama_ps().get(model) or {}
    print(f"  加载 {load_s:.1f}s · VRAM {ps.get('vram_gb')}G / 总 {ps.get('total_gb')}G")
    rows, lat, tpss, surv_ok, surv_all = [], [], [], 0, 0
    for src, dest, text in SENTS:
        try:
            ms, out, tps = call(model, src, dest, text)
        except Exception as e:
            print(f"  [{src}->{dest}] 失败: {e}")
            continue
        lat.append(ms)
        if tps:
            tpss.append(tps)
        ph = [p for p in ("Z1Q", "Z2Q") if p in text]
        if ph:
            surv_all += len(ph)
            surv_ok += sum(1 for p in ph if p in out)
        rows.append({"src": src, "dest": dest, "text": text, "out": out,
                     "ms": round(ms), "tps": round(tps, 1) if tps else None})
        print(f"  [{src}->{dest}] {round(ms)}ms  {out}")
    res = {"model": model, "load_s": round(load_s, 1),
           "vram_gb": ps.get("vram_gb"), "total_gb": ps.get("total_gb"),
           "lat_med_ms": round(statistics.median(lat)) if lat else None,
           "lat_max_ms": round(max(lat)) if lat else None,
           "tps_med": round(statistics.median(tpss), 1) if tpss else None,
           "placeholder_survival": f"{surv_ok}/{surv_all}",
           "rows": rows, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"  中位 {res['lat_med_ms']}ms · 最大 {res['lat_max_ms']}ms · {res['tps_med']} tok/s"
          f" · 占位符存活 {res['placeholder_survival']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")
    return res


if __name__ == "__main__":
    models = sys.argv[1:] or ["qwen2.5:14b"]
    for m in models:
        bench(m)
        try:  # 逐个卸载,避免两模型同时驻留干扰 VRAM 读数
            requests.post(f"{OLLAMA}/api/chat",
                          json={"model": m, "messages": [], "keep_alive": 0}, timeout=10)
        except Exception:
            pass
        time.sleep(2)
    print(f"\n结果已追加 {OUT}")
