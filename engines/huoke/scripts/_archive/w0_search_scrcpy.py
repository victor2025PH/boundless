# -*- coding: utf-8 -*-
"""
W0 搜索测试 - 使用scrcpy控制通道绕过MIUI INJECT_EVENTS限制
流程: 打开FB搜索页 → scrcpy tap打开搜索框 → inject_text写入查询 → 解析结果
"""
import sys, time, subprocess, logging
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

out = open('data/search_scrcpy.txt', 'w', encoding='utf-8')

try:
    from src.host.scrcpy_manager import ScrcpySession
    import uiautomator2 as u2
    from src.vision.screen_parser import XMLParser

    d = u2.connect(DEVICE)

    # Step 1: Start scrcpy session (control only, minimal quality)
    log.info('Starting scrcpy session...')
    sess = ScrcpySession(DEVICE, port=27300, max_size=480, bitrate=500_000,
                         max_fps=10, enable_control=True, quality='minimal')
    ok = sess.start()
    out.write(f'scrcpy start: {ok}, has_control: {sess.has_control}\n')
    out.write(f'screen size: {sess.screen_width}x{sess.screen_height}\n')
    log.info('scrcpy started: %s control=%s size=%dx%d', ok, sess.has_control,
             sess.screen_width, sess.screen_height)

    if not sess.has_control:
        out.write('ERROR: No control socket!\n')
        out.close()
        sess.stop()
        sys.exit(1)

    # Step 2: Make sure we're on FB home screen
    time.sleep(1)
    app = d.app_current()
    out.write(f'current app: {app}\n')
    if 'facebook' not in app.get('package', '').lower():
        subprocess.run([ADB, '-s', DEVICE, 'shell', 'monkey', '-p',
                        'com.facebook.katana', '-c', 'android.intent.category.LAUNCHER', '1'],
                       capture_output=True)
        time.sleep(2)

    d.screenshot('data/sc_s0.png')
    out.write('s0: before search tap\n')

    # Step 3: Tap search icon at (580, 112) using scrcpy
    log.info('Tapping search icon via scrcpy...')
    sess.tap(580, 112)
    time.sleep(2)

    d.screenshot('data/sc_s1.png')
    xml1 = d.dump_hierarchy()
    out.write(f's1 after tap: xml_len={len(xml1)}\n')
    has_edit = 'EditText' in xml1
    out.write(f's1 has EditText: {has_edit}\n')

    if not has_edit:
        out.write('Search page not opened by scrcpy tap, trying deep link...\n')
        subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                        'android.intent.action.VIEW', '-d',
                        'fb://search/?search_query=Yumi+Tanaka', 'com.facebook.katana'],
                       capture_output=True)
        time.sleep(2)
        xml1 = d.dump_hierarchy()
        out.write(f's1b deep link: xml_len={len(xml1)}, EditText={("EditText" in xml1)}\n')

    # Step 4: Tap on the search EditText to focus it (scrcpy)
    # EditText bounds: (120, 68, 688, 156), center: (404, 112)
    log.info('Tapping EditText center...')
    sess.tap(404, 112)
    time.sleep(0.8)

    # Step 5: Clear any existing text and type search query
    # Press Ctrl+A then Delete to clear
    KEYCODE_CTRL_A = 29  # Android KEYCODE_A
    KEYCODE_DEL = 67
    KEYCODE_ENTER = 66
    META_CTRL = 0x12000  # CTRL modifier

    log.info('Injecting text: Yumi Tanaka')
    ok = sess.inject_text('Yumi Tanaka')
    out.write(f'inject_text result: {ok}\n')
    time.sleep(1.5)

    d.screenshot('data/sc_s2.png')
    xml2 = d.dump_hierarchy()
    open('data/sc_s2.xml', 'w', encoding='utf-8').write(xml2)
    out.write(f's2 after type: xml_len={len(xml2)}\n')

    # Check if text was entered
    els2 = XMLParser.parse(xml2)
    for e in els2:
        if e.class_name and 'EditText' in e.class_name:
            out.write(f'EditText text: {repr(e.text)}\n')
            break

    # Step 6: Press Enter/Search
    log.info('Pressing Enter key...')
    sess.inject_keycode(0, KEYCODE_ENTER)  # DOWN
    time.sleep(0.1)
    sess.inject_keycode(1, KEYCODE_ENTER)  # UP
    time.sleep(4)  # Wait for search results

    d.screenshot('data/sc_s3.png')
    xml3 = d.dump_hierarchy()
    open('data/sc_s3.xml', 'w', encoding='utf-8').write(xml3)
    out.write(f's3 search results: xml_len={len(xml3)}\n')

    # Step 7: Parse results
    els3 = XMLParser.parse(xml3)
    out.write(f's3 element count: {len(els3)}\n')
    out.write('\n=== All text/cd elements in results ===\n')
    for e in els3:
        cd = getattr(e, 'content_desc', '') or ''
        t = e.text or ''
        if cd or t:
            b = e.bounds
            out.write(f'  text={repr(t)[:35]:35s}  cd={repr(cd)[:50]:50s}  bounds={b}\n')

    # Look for People filter
    people_els = [e for e in els3 if 'People' in (e.text or '') + (getattr(e,'content_desc','') or '')]
    out.write(f'\nPeople filter elements: {len(people_els)}\n')
    if people_els:
        for pe in people_els:
            out.write(f'  {pe}\n')

    sess.stop()
    out.close()
    print('Done! See data/search_scrcpy.txt')

except Exception as e:
    import traceback
    msg = traceback.format_exc()
    out.write(f'FATAL ERROR: {e}\n{msg}\n')
    out.close()
    print(f'ERROR: {e}')
    raise
