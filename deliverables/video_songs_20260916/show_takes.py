#!/usr/bin/env python3
"""列出 out/ 里某选题全部 take 的 ASR 分段与逐句命中，给人耳裁决当索引。用法：python show_takes.py V1"""
import glob
import json
import sys

pat = sys.argv[1] if len(sys.argv) > 1 else "*"
for f in sorted(glob.glob(f"out/{pat}_*_s*.json")):
    m = json.load(open(f, encoding="utf-8"))
    print(f"== {f}  hist={m.get('history_id')} seed={m.get('seed')} last_sung={m.get('last_sung_s')}s")
    print(f"   hits={m.get('line_hits')}")
    for s in m.get("asr", []) or []:
        print(f"   {s['start']:5.1f}-{s['end']:5.1f} {s['text']}")
