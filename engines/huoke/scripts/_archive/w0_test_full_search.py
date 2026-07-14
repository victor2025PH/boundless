# -*- coding: utf-8 -*-
"""
测试完整搜索流程和人员卡片提取是否正常
"""
import sys, time, subprocess
sys.path.insert(0, '.')

DEVICE = '8DWOF6CYY5R8YHX8'
ADB = r'C:\platform-tools\adb.exe'

out = open('data/full_search_test.txt', 'w', encoding='utf-8')

from src.app_automation.facebook import FacebookAutomation
import uiautomator2 as u2

fb = FacebookAutomation()
d = u2.connect(DEVICE)

# Make sure FB home is showing
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-n',
                'com.facebook.katana/.LoginActivity'], capture_output=True)
time.sleep(2)

# Test search_people
out.write('=== Test search_people("Keiko Suzuki") ===\n')
results = fb.search_people('Keiko Suzuki', device_id=DEVICE, max_results=5)
out.write(f'Results count: {len(results)}\n')
for r in results:
    out.write(f'  {r}\n')

# Take screenshot of current state
d.screenshot('data/full_search_s1.png')

# Test _first_search_result_element
out.write('\n=== Test _first_search_result_element ===\n')
first = fb._first_search_result_element(d)
out.write(f'First element: {first}\n')
if first:
    out.write(f'Bounds: {first.info}\n')

# Test navigate_to_profile
out.write('\n=== Test navigate_to_profile("Yumi Tanaka") ===\n')
# First go back to home
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-n',
                'com.facebook.katana/.LoginActivity'], capture_output=True)
time.sleep(2)

nav = fb.navigate_to_profile('Yumi Tanaka', device_id=DEVICE, post_open_dwell_sec=(2, 4))
out.write(f'Navigate result: {nav}\n')
d.screenshot('data/full_search_profile.png')
app = d.app_current()
out.write(f'Current app: {app}\n')

out.close()
print('Done. data/full_search_test.txt')
