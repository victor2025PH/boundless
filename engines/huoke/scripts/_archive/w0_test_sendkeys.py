# -*- coding: utf-8 -*-
"""测试 d.send_keys() 在设备8DWOF6CYY5R8YHX8上是否可用"""
import sys, time, subprocess
sys.path.insert(0, '.')

DEVICE = '8DWOF6CYY5R8YHX8'
ADB = r'C:\platform-tools\adb.exe'

import uiautomator2 as u2
out = open('data/sendkeys_test.txt', 'w', encoding='utf-8')
d = u2.connect(DEVICE)

# Open FB search
subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'tap', '580', '112'],
               capture_output=True)
time.sleep(2)
xml = d.dump_hierarchy()
out.write(f'EditText exists: {"EditText" in xml}\n')

# Try d.send_keys
try:
    d.send_keys('Keiko Suzuki', clear=True)
    out.write('d.send_keys() succeeded!\n')
    time.sleep(1.5)
    xml2 = d.dump_hierarchy()
    import re
    for line in xml2.split('\n'):
        if 'EditText' in line:
            m = re.search(r'text="([^"]*)"', line)
            if m:
                out.write(f'EditText text: {repr(m.group(1))}\n')
except Exception as e:
    out.write(f'd.send_keys() FAILED: {e}\n')
    # Fallback to adb shell
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'input', 'text', 'Keiko%sSuzuki'],
                   capture_output=True)
    time.sleep(1.5)
    xml2 = d.dump_hierarchy()
    for line in xml2.split('\n'):
        if 'EditText' in line:
            m = re.search(r'text="([^"]*)"', line)
            if m:
                out.write(f'ADB text fallback result: {repr(m.group(1))}\n')

d.screenshot('data/sendkeys_result.png')
out.close()
print('Done. data/sendkeys_test.txt')
