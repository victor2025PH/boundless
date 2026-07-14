# -*- coding: utf-8 -*-
"""
W0 Profile Browse - 不需要输入法，通过好友推荐/通知/群组浏览日本女性profile
策略：
1. 打开FB "可能认识的人" / "好友推荐" 页面
2. 打开群组成员列表（日本中高年群组）
3. 直接导航已知profile URL
"""
import sys, time, subprocess
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
out = open('data/browse_debug.txt', 'w', encoding='utf-8')

import uiautomator2 as u2
from src.vision.screen_parser import XMLParser

d = u2.connect(DEVICE)


def try_deeplink(label, url, wait=3):
    out.write(f'\n=== {label}: {url} ===\n')
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start',
                    '-a', 'android.intent.action.VIEW', '-d', url],
                   capture_output=True)
    time.sleep(wait)
    d.screenshot(f'data/browse_{label}.png')
    xml = d.dump_hierarchy()
    xml_len = len(xml)
    els = XMLParser.parse(xml)
    out.write(f'xml_len={xml_len}, elements={len(els)}\n')
    # Show text/cd
    for e in els:
        cd = getattr(e,'content_desc','') or ''
        t = e.text or ''
        if (cd or t) and e.bounds:
            out.write(f'  {repr(t)[:30]:30s} | {repr(cd)[:60]:60s}\n')
    return xml_len, els


# 测试不需要输入的FB导航
# 1. 可能认识的人
try_deeplink('A_people', 'fb://people', wait=4)
# 2. 好友请求
try_deeplink('B_friendrequests', 'fb://friendrequests', wait=3)
# 3. 通知
try_deeplink('C_notifications', 'fb://notifications', wait=3)
# 4. 好友建议
try_deeplink('D_friend_suggestions', 'fb://friend_suggestions', wait=3)
# 5. 添加好友
try_deeplink('E_addfriend', 'fb://addfriend', wait=3)
# 6. 发现 (find people)
try_deeplink('F_find_friends', 'fb://find_friends', wait=3)
# 7. 用户相关页面
try_deeplink('G_friends', 'fb://friends', wait=3)

out.close()
print('Done. See data/browse_debug.txt')
