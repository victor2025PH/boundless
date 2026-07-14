# -*- coding: utf-8 -*-
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
with open('data/w0_greeting_library.json', encoding='utf-8') as f:
    d = json.load(f)
greetings = d if isinstance(d, list) else d.get('greetings', [])
print(f'共 {len(greetings)} 条打招呼话术')
tags = {}
for g in greetings:
    t = g.get('style_tag','?') if isinstance(g, dict) else '?'
    tags[t] = tags.get(t, 0) + 1
print('风格分布:', tags)
print()
print('示例 (前5条):')
for g in greetings[:5]:
    text = g.get('text_ja','') if isinstance(g, dict) else str(g)
    style = g.get('style_tag','?') if isinstance(g, dict) else '?'
    print(f'  [{style}] {text[:80]}')
