# -*- coding: utf-8 -*-
"""
测试 scrcpy inject_text 是否真正工作
- 打开搜索页（EditText已聚焦）
- 直接inject_text，不做额外操作
- 多次检查EditText内容和截图
"""
import sys, time, subprocess
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
out = open('data/inject_text_test.txt', 'w', encoding='utf-8')

from src.host.scrcpy_manager import ScrcpySession
import uiautomator2 as u2
import re

d = u2.connect(DEVICE)

# Start scrcpy
sess = ScrcpySession(DEVICE, port=27302, max_size=480, bitrate=500_000,
                     max_fps=10, enable_control=True, quality='minimal')
ok = sess.start()
out.write(f'scrcpy: {ok} control:{sess.has_control} size:{sess.screen_width}x{sess.screen_height}\n')

# Open FB home first
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-n',
                'com.facebook.katana/.LoginActivity'], capture_output=True)
time.sleep(2)

# Open search via deep link  
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                'android.intent.action.VIEW', '-d',
                'fb://search/?search_query=test', 'com.facebook.katana'],
               capture_output=True)
time.sleep(3)  # Wait longer for InputConnection to establish

# Screenshot and check state
d.screenshot('data/inject_s0.png')
xml = d.dump_hierarchy()
el = d(className='android.widget.EditText')
out.write(f'EditText exists: {el.exists}, focused: {el.info.get("focused") if el.exists else "N/A"}\n')

# Test 1: inject_text immediately
out.write('\n--- Test 1: inject_text "YumiT" ---\n')
ok = sess.inject_text('YumiT')
out.write(f'inject_text result: {ok}\n')
time.sleep(2)
d.screenshot('data/inject_s1.png')
xml1 = d.dump_hierarchy()
for line in xml1.split('\n'):
    if 'EditText' in line:
        m = re.search(r'text="([^"]*)"', line)
        if m:
            out.write(f'EditText text after inject: {repr(m.group(1))}\n')

# Test 2: try typing char by char using KEYCODE
out.write('\n--- Test 2: inject_keycode chars one by one ---\n')
# Android keycodes: A=29, B=30, ... Z=54, space=62
# For uppercase: META_SHIFT_ON=0x41
keymap = {
    'a': 29, 'b': 30, 'c': 31, 'd': 32, 'e': 33, 'f': 34, 'g': 35,
    'h': 36, 'i': 37, 'j': 38, 'k': 39, 'l': 40, 'm': 41, 'n': 42,
    'o': 43, 'p': 44, 'q': 45, 'r': 46, 's': 47, 't': 48, 'u': 49,
    'v': 50, 'w': 51, 'x': 52, 'y': 53, 'z': 54, ' ': 62
}
META_SHIFT = 0x41

test_text = 'abc'
for ch in test_text:
    kc = keymap.get(ch.lower(), 0)
    if kc == 0:
        continue
    meta = META_SHIFT if ch.isupper() else 0
    ok1 = sess.inject_keycode(0, kc, meta=meta)  # DOWN
    time.sleep(0.05)
    ok2 = sess.inject_keycode(1, kc, meta=meta)  # UP
    out.write(f'  char {repr(ch)} keycode={kc} down={ok1} up={ok2}\n')
    time.sleep(0.1)

time.sleep(1)
d.screenshot('data/inject_s2_keycode.png')
xml2 = d.dump_hierarchy()
for line in xml2.split('\n'):
    if 'EditText' in line:
        m = re.search(r'text="([^"]*)"', line)
        if m:
            out.write(f'EditText after keycode: {repr(m.group(1))}\n')

# Test 3: Try inject_text followed by checking screenshot
out.write('\n--- Test 3: inject_text "Hello" then screenshot ---\n')
ok = sess.inject_text('Hello')
out.write(f'inject_text Hello: {ok}\n')
time.sleep(0.5)
d.screenshot('data/inject_s3.png')
xml3 = d.dump_hierarchy()
for line in xml3.split('\n'):
    if 'EditText' in line:
        m = re.search(r'text="([^"]*)"', line)
        if m:
            out.write(f'EditText after Hello: {repr(m.group(1))}\n')

sess.stop()
out.close()
print('Done. data/inject_text_test.txt')
