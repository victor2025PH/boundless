# -*- coding: utf-8 -*-
"""分析搜索结果XML找出可点击元素"""
import sys
sys.path.insert(0, '.')
from src.vision.screen_parser import XMLParser

out = open('data/search_clickable.txt', 'w', encoding='utf-8')
xml = open('data/dev2_s3.xml', 'r', encoding='utf-8').read()
els = XMLParser.parse(xml)

out.write('=== Clickable TextViews ===\n')
clickable_tvs = []
for e in els:
    if e.clickable and e.text and len(e.text) >= 2 and e.class_name and 'TextView' in e.class_name:
        clickable_tvs.append(e)
        out.write(f'  text={repr(e.text)[:50]:50s}  bounds={e.bounds}\n')

out.write(f'\nTotal clickable TVs: {len(clickable_tvs)}\n')

out.write('\n=== All clickable elements ===\n')
for e in els:
    if e.clickable:
        cd = getattr(e,'content_desc','') or ''
        t = e.text or ''
        if cd or t:
            out.write(f'  text={repr(t)[:35]:35s}  cd={repr(cd)[:40]:40s}  cls={e.class_name[-20:]:20s}  bounds={e.bounds}\n')

out.close()
print('Done')
