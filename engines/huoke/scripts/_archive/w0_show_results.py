# -*- coding: utf-8 -*-
"""查看 W0 抓取结果"""
import json, sys
sys.path.insert(0, '.')

with open('data/w0_jp_ground_truth_v2.json', encoding='utf-8') as f:
    d = json.load(f)

print(f'Total: {d["actual_count"]}')
print(f'Stats: {d["stats"]}')
print()
for p in d['profiles']:
    name = p.get('display_name', '')
    shots = len(p.get('image_paths', []))
    bio = p.get('bio', '')[:80]
    print(f'  {p["seq"]:02d}. display_name={name!r} shots={shots}')
    print(f'      bio={bio!r}')
    print()
