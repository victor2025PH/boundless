# -*- coding: utf-8 -*-
"""打印 W0-3 分类报告"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('data/w0_classify_report.json', encoding='utf-8') as f:
    d = json.load(f)

stats = d['stats']
print("=== W0-3 分类统计 ===")
print(f"总计: {stats['total']}")
print(f"L1通过: {stats['l1_pass']} ({stats['l1_pass']/max(1,stats['total'])*100:.0f}%)")
print(f"L1失败: {stats['l1_fail']}")
print(f"L2运行: {stats['l2_run']}")
print(f"L2命中(精准客户): {stats['l2_match']}")
print()

matched = [r for r in d['results'] if r['match']]
print(f"[精准客户 {len(matched)} 人]")
for r in matched:
    name = r['display_name'][:28]
    print(f"  [{r['seq']:02d}] {name:<30} gender={r['gender']:<8} age={r['age_band']:<6} conf={r['overall_confidence']:.2f}  L1={r['l1_score']:.0f}  cache={r['from_cache']}")

print()
not_matched = [r for r in d['results'] if not r['match']]
print(f"[未命中 {len(not_matched)} 人]")
for r in not_matched:
    name = r['display_name'][:28]
    reasons = ','.join(r.get('l1_reasons', [])[:2])
    print(f"  [{r['seq']:02d}] {name:<30} L1={r['l1_score']:.0f}  stage={r['stage_reached']}  {reasons[:50]}")
