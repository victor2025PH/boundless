# -*- coding: utf-8 -*-
"""
W0 从Facebook主页/通知/群组XML提取profile ID，然后直接导航到个人主页截图
"""
import sys, time, subprocess, re
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
out = open('data/profile_ids.txt', 'w', encoding='utf-8')

import uiautomator2 as u2
d = u2.connect(DEVICE)

# 1. 获取主页XML，分析所有元素的属性
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-n',
                'com.facebook.katana/.LoginActivity'], capture_output=True)
time.sleep(2)
xml = d.dump_hierarchy()

out.write('=== Searching for profile/uid patterns in XML ===\n')
# Look for numeric IDs in resource-id attributes
uid_pattern = re.compile(r'resource-id="([^"]*)"')
uids = set()
for m in uid_pattern.finditer(xml):
    rid = m.group(1)
    if rid and rid not in ['', 'com.facebook.katana:id/(name removed)']:
        # Extract any numeric sequences that could be UIDs
        nums = re.findall(r'\d{6,}', rid)
        for n in nums:
            uids.add(n)
        out.write(f'rid: {rid}\n')

out.write(f'\nFound numeric IDs: {sorted(uids)}\n')

# 2. Navigate to notifications page and capture more
subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                'android.intent.action.VIEW', '-d', 'fb://notifications'],
               capture_output=True)
time.sleep(3)

xml_notif = d.dump_hierarchy()
open('data/notifications_full.xml', 'w', encoding='utf-8').write(xml_notif)
out.write(f'\nNotifications XML len: {len(xml_notif)}\n')

# Look for view-source links, hrefs, data attributes that might contain UIDs
for m in uid_pattern.finditer(xml_notif):
    rid = m.group(1)
    if rid and rid not in ['', 'com.facebook.katana:id/(name removed)']:
        nums = re.findall(r'\d{6,}', rid)
        for n in nums:
            uids.add(n)

out.write(f'All UIDs found so far: {sorted(uids)}\n')

# 3. Navigate to profile deep links if we found any UIDs
for uid in list(uids)[:5]:
    out.write(f'\n=== Testing profile UID: {uid} ===\n')
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                    'android.intent.action.VIEW', '-d', f'fb://profile/{uid}'],
                   capture_output=True)
    time.sleep(3)
    app = d.app_current()
    out.write(f'app: {app}\n')
    xml_p = d.dump_hierarchy()
    out.write(f'xml len: {len(xml_p)}\n')
    # Check if it looks like a profile page
    if len(xml_p) > 60000 or 'timeline' in xml_p.lower():
        out.write('  -> Looks like a profile page!\n')
        d.screenshot(f'data/profile_{uid}.png')
        open(f'data/profile_{uid}.xml', 'w', encoding='utf-8').write(xml_p)

# 4. Try navigation to group members list
# The notification showed "中高年/熟年/シニア" group - need group ID
# Also try by name-based deep links
test_profiles = [
    ('michelle_yumiko_nomura', 'fb://profile/michelle.yumiko.nomura'),
    ('tomi_nozaki', 'fb://profile/tomi.nozaki'),
    # Try general search by navigating to "好友" tab
]

for name, url in test_profiles:
    out.write(f'\n=== {name}: {url} ===\n')
    result = subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                             'android.intent.action.VIEW', '-d', url],
                            capture_output=True, text=True)
    out.write(f'stdout: {result.stdout.strip()}\n')
    time.sleep(3)
    xml_p = d.dump_hierarchy()
    out.write(f'xml len: {len(xml_p)}\n')

out.close()
print('Done. data/profile_ids.txt')
