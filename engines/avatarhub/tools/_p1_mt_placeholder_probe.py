# -*- coding: utf-8 -*-
"""P1-3 补测：HY-MT1.5 vs qwen2.5:14b 的占位符存活率专项(术语锁定依赖 Z1Q/Z2Q 原样穿越 MT)。
每模型 8 句 x 共 14 个占位符，输出存活率。用后归档。"""
import json
import os
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
SENTS = [
    ("zh", "en", "请把 Z1Q 的最新功能给大家演示一下，注意保持 Z2Q 完全不变。"),
    ("zh", "en", "今天 Z1Q 直播间下单的朋友，都送 Z2Q 的周边礼包。"),
    ("zh", "en", "Z1Q 这个型号比 Z2Q 贵两百块，但是续航翻倍。"),
    ("zh", "en", "麻烦把 Z1Q 的物流单号发到群里。"),
    ("en", "zh", "Our Z1Q platform integrates face swap and voice cloning with Z2Q latency under one second."),
    ("en", "zh", "The Z1Q firmware update fixes the Z2Q pairing issue reported last week."),
    ("en", "zh", "Please ship the Z1Q samples to our Shenzhen office by Friday."),
    ("en", "zh", "Z1Q outperforms Z2Q in every benchmark we ran."),
]


def call(model, src, dest, text):
    r = requests.post(f"{OLLAMA}/api/chat",
                      json=li._llm_req_body(model, text, src, dest) | {"keep_alive": "5m"},
                      timeout=120)
    r.raise_for_status()
    out = ((r.json().get("message") or {}).get("content") or "")
    out = li._THINK_RE.sub("", out)
    return " ".join(ln.strip() for ln in out.splitlines() if ln.strip()).strip()


def main():
    for model in sys.argv[1:] or ["demonbyron/HY-MT1.5-7B:Q4_K_M", "qwen2.5:14b"]:
        ok = tot = 0
        t0 = time.time()
        fails = []
        for src, dest, text in SENTS:
            try:
                out = call(model, src, dest, text)
            except Exception as e:
                print(f"  [{model}] 调用失败: {e}")
                continue
            for ph in ("Z1Q", "Z2Q"):
                if ph in text:
                    tot += 1
                    if ph in out:
                        ok += 1
                    else:
                        fails.append((ph, text[:24], out[:60]))
        print(f"{model}: 占位符存活 {ok}/{tot} ({time.time()-t0:.0f}s)")
        for ph, t, o in fails:
            print(f"   丢 {ph}: {t}… -> {o}…")
        try:
            requests.post(f"{OLLAMA}/api/chat",
                          json={"model": model, "messages": [], "keep_alive": 0}, timeout=10)
        except Exception:
            pass
        time.sleep(2)


if __name__ == "__main__":
    main()
