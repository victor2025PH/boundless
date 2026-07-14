# -*- coding: utf-8 -*-
import json, os
with open('data/w0_jp_ground_truth_v2.json', encoding='utf-8') as f:
    d = json.load(f)
total_ok = 0
total_missing = 0
for p in d['profiles']:
    seq = p['seq']
    name = p.get('display_name','')[:25]
    paths = p.get('image_paths', [])
    ok = sum(1 for x in paths if os.path.exists(x))
    miss = len(paths) - ok
    total_ok += ok
    total_missing += miss
    print(f"seq={seq:02d} {name!r:30} ok={ok}/{len(paths)}")
print(f"\nTotal images ok={total_ok} missing={total_missing}")
