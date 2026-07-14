# -*- coding: utf-8 -*-
"""刘德华角色的声音参考元数据：refs 数量/时长/来源 + 最近改动痕迹。"""
import json
import urllib.request
import urllib.parse

HUB = "http://127.0.0.1:9000"


def get(path):
    with urllib.request.urlopen(HUB + path, timeout=15) as r:
        return json.loads(r.read().decode())


d = get("/profiles/" + urllib.parse.quote("刘德华"))
p = d.get("profile") if isinstance(d, dict) and "profile" in d else d
info = {}
for k, v in (p or {}).items():
    if isinstance(v, str) and len(v) > 200:
        info[k] = f"<str {len(v)}B>"
    elif isinstance(v, list) and v and isinstance(v[0], str) and len(v[0]) > 200:
        info[k] = [f"<str {len(x)}B>" for x in v]
    else:
        info[k] = v
print(json.dumps(info, ensure_ascii=False, indent=1)[:2600])
