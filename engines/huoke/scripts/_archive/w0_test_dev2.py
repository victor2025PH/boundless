# -*- coding: utf-8 -*-
"""测试第二台设备的搜索功能"""
import sys, time, subprocess, re
sys.path.insert(0, '.')

DEVICE = '8DWOF6CYY5R8YHX8'
ADB = r'C:\platform-tools\adb.exe'

import uiautomator2 as u2

out = open('data/dev2_test.txt', 'w', encoding='utf-8')
d = u2.connect(DEVICE)

# Check current state
app = d.app_current()
out.write(f'app: {app}\n')
d.screenshot('data/dev2_s0.png')

# Check if we need to open FB home
if 'facebook' not in app.get('package', '').lower():
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'monkey', '-p',
                    'com.facebook.katana', '-c', 'android.intent.category.LAUNCHER', '1'],
                   capture_output=True)
    time.sleep(2)

# Tap search icon
result = subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'tap', '580', '112'],
                       capture_output=True, text=True)
out.write(f'tap result: rc={result.returncode} err={result.stderr}\n')
time.sleep(2)

d.screenshot('data/dev2_s1_search.png')
xml1 = d.dump_hierarchy()
has_edit = 'EditText' in xml1
out.write(f's1: xml_len={len(xml1)}, has_edit={has_edit}\n')

if has_edit:
    # Type search query
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'text', 'Yumi%sTanaka'],
                   capture_output=True)
    time.sleep(1.5)

    d.screenshot('data/dev2_s2_typed.png')
    xml2 = d.dump_hierarchy()
    out.write(f's2: xml_len={len(xml2)}\n')
    # Check EditText text
    for line in xml2.split('\n'):
        if 'EditText' in line:
            m = re.search(r'text="([^"]*)"', line)
            if m:
                out.write(f'EditText text: {repr(m.group(1))}\n')

    # Press enter to search
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'keyevent', '66'],
                   capture_output=True)
    time.sleep(4)

    d.screenshot('data/dev2_s3_results.png')
    xml3 = d.dump_hierarchy()
    open('data/dev2_s3.xml', 'w', encoding='utf-8').write(xml3)
    out.write(f's3: xml_len={len(xml3)}\n')

    # Parse elements
    sys.path.insert(0, '.')
    from src.vision.screen_parser import XMLParser
    els = XMLParser.parse(xml3)
    out.write(f's3 elements: {len(els)}\n')
    for e in els:
        cd = getattr(e, 'content_desc', '') or ''
        t = e.text or ''
        if (cd or t) and e.bounds:
            out.write(f'  {repr(t)[:30]:30s} | {repr(cd)[:55]:55s}\n')

out.close()
print('Done. data/dev2_test.txt')
