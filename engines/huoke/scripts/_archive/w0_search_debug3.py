# -*- coding: utf-8 -*-
"""
W0 搜索调试v3 - 使用deep link打开搜索页，然后用element.set_text()输入文字
"""
import sys, time, uiautomator2 as u2, subprocess
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
out = open('data/search_debug3.txt', 'w', encoding='utf-8')

d = u2.connect(DEVICE)

# Step 1: Open FB search via deep link
out.write('=== Step 1: Launch deep link search ===\n')
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
                '-d', 'fb://search/?search_query=Yumi+Tanaka', 'com.facebook.katana'],
               capture_output=True)
time.sleep(2.5)

d.screenshot('data/sd3_s0.png')
xml0 = d.dump_hierarchy()
edit_exists = d(className='android.widget.EditText').exists
out.write(f'EditText exists: {edit_exists}\n')

if not edit_exists:
    out.write('ERROR: No EditText after deep link\n')
    out.close()
    sys.exit(1)

el = d(className='android.widget.EditText')
info = el.info
out.write(f'EditText info: focused={info.get("focused")}, text={repr(info.get("text",""))}\n')
out.write(f'EditText bounds: {info.get("bounds")}\n')

# Step 2: Try set_text on the element
out.write('\n=== Step 2: Try el.set_text("Yumi Tanaka") ===\n')
try:
    el.set_text('Yumi Tanaka')
    out.write('set_text succeeded!\n')
    time.sleep(1.5)
    d.screenshot('data/sd3_s1_set_text.png')
    xml1 = d.dump_hierarchy()
    el2 = d(className='android.widget.EditText')
    info2 = el2.info
    out.write(f'After set_text - text: {repr(info2.get("text",""))}\n')
except Exception as e:
    out.write(f'set_text FAILED: {e}\n')

# Step 3: Try deep link with query directly for People search
out.write('\n=== Step 3: Try people-specific search URL ===\n')
# Different URL formats to try
urls = [
    'fb://search/people/?q=Yumi+Tanaka',
    'fb://search/?q=Yumi+Tanaka&filters=people',
    'https://www.facebook.com/search/people/?q=Yumi+Tanaka',
]
for url in urls:
    out.write(f'Trying: {url}\n')
    result = subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
                            '-d', url, 'com.facebook.katana'],
                           capture_output=True, text=True)
    out.write(f'Result: {result.stdout} {result.stderr}\n')
    time.sleep(3)
    d.screenshot(f'data/sd3_url_{urls.index(url)}.png')
    xml = d.dump_hierarchy()
    # Check for People results
    has_people = 'People' in xml or '用户' in xml or '人物' in xml
    has_results = len(xml) > 80000
    out.write(f'Has people filter: {has_people}, xml_len: {len(xml)}\n')
    # Save XML
    open(f'data/sd3_url_{urls.index(url)}.xml', 'w', encoding='utf-8').write(xml)
    if has_results:
        out.write('  -> This URL seems to work (large XML)!\n')
        break

out.close()
print('Done. See data/search_debug3.txt')
