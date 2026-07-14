# -*- coding: utf-8 -*-
"""VN 验证探针：角色名无裸编号 + 溯源/描述完整 + 三库计数（迁移后自检，可反复跑）。"""
import io
import json
import re
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
d = json.loads(urllib.request.urlopen("http://127.0.0.1:9000/profiles", timeout=10).read())
rows = d["profiles"]
bad = [p["name"] for p in rows if re.search(r"\d{3,}", p["name"])]
print("含编号的角色名:", bad or "无 ✔")
libs = {}
for p in sorted(rows, key=lambda x: x["name"]):
    lib = p.get("lib") or ("human" if p["has_face"] and p["has_voice"] else
                           ("photo" if p["has_face"] else ("voice" if p["has_voice"] else "draft")))
    libs[lib] = libs.get(lib, 0) + 1
    print(f"{p['name']:<10} lib={lib:<6} spk={p.get('voicepack_spk') or '-':<8} "
          f"desc={(p.get('description') or '')[:52]}")
print("三库计数:", libs, "总数:", len(rows))
sys.exit(1 if bad else 0)
