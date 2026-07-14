# -*- coding: utf-8 -*-
"""
W0 搜索流程直接调试脚本 - 修复版，无PowerShell转义问题
"""
import sys, time, uiautomator2 as u2
sys.path.insert(0, '.')
from src.behavior.human_behavior import HumanBehavior

out = open('data/search_debug2.txt', 'w', encoding='utf-8')

d = u2.connect('LVHIOZSWDAYLCELN')
hb = HumanBehavior()

d.screenshot('data/sd_s0.png')
out.write('s0: current screen\n')

# Check current app
info = d.app_current()
out.write(f'current app: {info}\n')

# Try clicking search by content description
el = d(description='搜索')
out.write(f'search el exists (description=搜索): {el.exists}\n')

if el.exists:
    info2 = el.info
    out.write(f'search el bounds: {info2.get("bounds")}\n')
    # Use hb.tap which falls back to adb shell input tap for MIUI INJECT_EVENTS restriction
    hb.tap(d, 580, 112)
    out.write('clicked via hb.tap at 580,112 (shell fallback)\n')
else:
    hb.tap(d, 580, 112)
    out.write('element not found, clicked via hb.tap 580,112\n')

time.sleep(2.5)
d.screenshot('data/sd_s1.png')
xml1 = d.dump_hierarchy()
open('data/sd_s1.xml', 'w', encoding='utf-8').write(xml1)
out.write(f's1 xml len: {len(xml1)}\n')

# Search for key indicators
for term in ['EditText', 'focused="true"', 'Search Facebook', 'search_box', 'Search', 'People']:
    if term in xml1:
        out.write(f's1 CONTAINS: {term}\n')

# Print EditText lines
for line in xml1.split('\n'):
    if 'EditText' in line or 'focused="true"' in line or 'search' in line.lower():
        out.write(f'LINE: {line.strip()[:300]}\n')

# Now type
out.write('\nTyping Yumi Tanaka...\n')
# Try typing directly
d.send_keys('Yumi Tanaka', clear=True)
time.sleep(2)
d.screenshot('data/sd_s2.png')
out.write('s2: after typing\n')

# Get hierarchy again
xml2 = d.dump_hierarchy()
open('data/sd_s2.xml', 'w', encoding='utf-8').write(xml2)
out.write(f's2 xml len: {len(xml2)}\n')
for term in ['Yumi', 'Tanaka', 'EditText', 'focused="true"']:
    if term in xml2:
        out.write(f's2 CONTAINS: {term}\n')

d.press('enter')
time.sleep(4)
d.screenshot('data/sd_s3.png')
out.write('s3: after enter (search results)\n')

xml3 = d.dump_hierarchy()
open('data/sd_s3.xml', 'w', encoding='utf-8').write(xml3)
out.write(f's3 xml len: {len(xml3)}\n')
for term in ['People', 'Users', 'Results', 'EditText']:
    if term in xml3:
        out.write(f's3 CONTAINS: {term}\n')

# Print all text from s3
for line in xml3.split('\n'):
    line = line.strip()
    if 'text=' in line and len(line) < 300:
        out.write(f'S3: {line[:250]}\n')

out.close()
print('Done. See data/search_debug2.txt')
