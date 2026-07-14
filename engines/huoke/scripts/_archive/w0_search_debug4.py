# -*- coding: utf-8 -*-
"""
W0 搜索调试v4 - 测试多种URL格式直接打开Facebook搜索结果
"""
import sys, time, uiautomator2 as u2, subprocess
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
out = open('data/search_debug4.txt', 'w', encoding='utf-8')

d = u2.connect(DEVICE)


def try_url(label, url, wait=4):
    out.write(f'\n=== {label} ===\n')
    out.write(f'URL: {url}\n')
    result = subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                             'android.intent.action.VIEW', '-d', url],
                            capture_output=True, text=True)
    out.write(f'stdout: {result.stdout.strip()}\n')
    out.write(f'stderr: {result.stderr.strip()[:200]}\n')
    time.sleep(wait)
    d.screenshot(f'data/sd4_{label}.png')
    xml = d.dump_hierarchy()
    xml_len = len(xml)
    # Count meaningful indicators
    has_people = any(t in xml for t in ['People', '用户', '人物', 'Personnes'])
    has_names = any(t in xml for t in ['Tanaka', 'Yumi', 'Yamamoto'])
    has_results_header = any(t in xml for t in ['搜索结果', 'Search results', '筛选'])
    out.write(f'xml_len: {xml_len}, people: {has_people}, names: {has_names}, results_header: {has_results_header}\n')
    if xml_len > 90000 or has_people or has_names or has_results_header:
        open(f'data/sd4_{label}.xml', 'w', encoding='utf-8').write(xml)
        out.write('  -> SAVED XML (promising!)\n')
        # Extract notable text/cd elements
        import sys as _sys
        from src.vision.screen_parser import XMLParser
        els = XMLParser.parse(xml)
        notable = []
        for e in els:
            cd = getattr(e,'content_desc','') or ''
            t = e.text or ''
            if cd or t:
                notable.append(f'    {repr(t)[:30]:30s} | {repr(cd)[:50]:50s}')
        out.write('\n'.join(notable[:30]) + '\n')
    return xml_len


# Test different URL schemes
try_url('A_fb_people_noslash', 'fb://search/people?q=Yumi+Tanaka')
try_url('B_intent_https', 'intent://www.facebook.com/search/people/%3Fq%3DYumi%2BTanaka#Intent;scheme=https;package=com.facebook.katana;end;')
try_url('C_https_fb', 'https://www.facebook.com/search/people/?q=Yumi+Tanaka', wait=5)
try_url('D_fb_search_q', 'fb://search?q=Yumi+Tanaka&type=user', wait=3)
try_url('E_fb_search_top', 'fb://search/top?q=Yumi+Tanaka', wait=3)

# Also try the intent:// format with explicit component
out.write('\n=== F: am start with explicit package and search intent ===\n')
result = subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start',
                         '-a', 'com.facebook.katana.action.SEARCH',
                         '-e', 'query', 'Yumi Tanaka',
                         'com.facebook.katana'],
                        capture_output=True, text=True)
out.write(f'Result: {result.stdout}\n')
time.sleep(3)
d.screenshot('data/sd4_F.png')
xml = d.dump_hierarchy()
out.write(f'xml_len: {len(xml)}\n')

out.close()
print('Done. See data/search_debug4.txt')
