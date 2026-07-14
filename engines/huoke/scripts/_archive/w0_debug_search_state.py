# -*- coding: utf-8 -*-
"""调试：对设备执行搜索后，dump XML找出人物卡片和People filter的坐标"""
import sys, time, subprocess
sys.path.insert(0, '.')

DEVICE = '8DWOF6CYY5R8YHX8'
ADB = r'C:\platform-tools\adb.exe'

def adb(*args):
    r = subprocess.run([ADB, '-s', DEVICE] + list(args), capture_output=True, text=True, timeout=15)
    return (r.stdout or '') + (r.stderr or '')

# 返回FB首页
adb('shell', 'am', 'start', '-n', 'com.facebook.katana/.LoginActivity')
time.sleep(2)

# 截图首页
subprocess.run([ADB, '-s', DEVICE, 'shell', 'screencap', '-p', '/sdcard/s0_home.png'])
subprocess.run([ADB, '-s', DEVICE, 'pull', '/sdcard/s0_home.png', 'data/debug_s0_home.png'])

# 点搜索栏 (580, 112)
subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'tap', '580', '112'])
time.sleep(2)

# 截图搜索页
subprocess.run([ADB, '-s', DEVICE, 'shell', 'screencap', '-p', '/sdcard/s1_search.png'])
subprocess.run([ADB, '-s', DEVICE, 'pull', '/sdcard/s1_search.png', 'data/debug_s1_search.png'])

# 输入搜索词
subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'text', 'Keiko%sSuzuki'])
time.sleep(1.5)

# 截图有文字的搜索页
subprocess.run([ADB, '-s', DEVICE, 'shell', 'screencap', '-p', '/sdcard/s2_typed.png'])
subprocess.run([ADB, '-s', DEVICE, 'pull', '/sdcard/s2_typed.png', 'data/debug_s2_typed.png'])

# 按Enter
subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'keyevent', '66'])
time.sleep(4)

# 截图搜索结果页（All results）
subprocess.run([ADB, '-s', DEVICE, 'shell', 'screencap', '-p', '/sdcard/s3_results.png'])
subprocess.run([ADB, '-s', DEVICE, 'pull', '/sdcard/s3_results.png', 'data/debug_s3_results.png'])

# dump XML - ALL results
import uiautomator2 as u2
d = u2.connect(DEVICE)
xml_all = d.dump_hierarchy()
with open('data/debug_s3_all_results.xml', 'w', encoding='utf-8') as f:
    f.write(xml_all)

print(f'XML length: {len(xml_all)}')
print('=== Clickable elements (ALL results) ===')
from src.vision.screen_parser import XMLParser
elements = XMLParser.parse(xml_all)
for el in elements:
    if el.clickable:
        cd = (getattr(el, 'content_desc', '') or '').strip()
        t = (el.text or '').strip()
        b = el.bounds
        w = b[2] - b[0] if b else 0
        h = b[3] - b[1] if b else 0
        cls = (el.class_name or '')
        print(f'  bounds={b} w={w} h={h} cls={cls[:30]} cd={repr(cd[:50])} text={repr(t[:30])}')

# 找filter chips区域（y < 280）
print('\n=== Elements at y < 280 (filter chips area) ===')
for el in elements:
    b = el.bounds
    if not b:
        continue
    if b[1] < 280:  # top < 280
        cd = (getattr(el, 'content_desc', '') or '').strip()
        t = (el.text or '').strip()
        print(f'  bounds={b} cls={el.class_name[:30] if el.class_name else ""} cd={repr(cd[:50])} text={repr(t[:30])} click={el.clickable}')

print('\nDebug done. Check data/debug_*.png and data/debug_s3_all_results.xml')
