# -*- coding: utf-8 -*-
"""W1 测试：验证 L1 规则改进效果（使用 W0 采集的 29 个 profile 重新评分）"""
import json, sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

from src.host.fb_profile_classifier import score_l1
from src.host.fb_target_personas import get_persona

persona = get_persona("jp_female_midlife")
threshold = (persona.get("l1") or {}).get("pass_threshold", 30)
print(f"L1 pass_threshold = {threshold}")
print()

with open('data/w0_jp_ground_truth_v2.json', encoding='utf-8') as f:
    data = json.load(f)

pass_count = 0
fail_count = 0

for p in data['profiles']:
    seq = p['seq']
    name = p.get('display_name', '')
    bio = p.get('bio', '')
    # 清理 display_name（取逗号前）
    if ',' in name:
        candidate = name.split(',')[0].strip()
        words = candidate.split()
        if len(words) >= 2 and all(w[0].isupper() for w in words if w):
            name = candidate
    ctx = {"display_name": name, "bio": bio, "username": "", "locale": "ja"}
    score, reasons = score_l1(persona, ctx)
    passed = score >= threshold
    if passed:
        pass_count += 1
    else:
        fail_count += 1
    flag = "PASS" if passed else "fail"
    reasons_short = ", ".join(r[:35] for r in reasons[:2])
    print(f"[{flag}] [{seq:02d}] {name[:28]:<30} score={score:.0f}  {reasons_short}")

print()
print(f"L1通过: {pass_count}/29  ({pass_count/29*100:.0f}%)")
print(f"L1失败: {fail_count}/29")
