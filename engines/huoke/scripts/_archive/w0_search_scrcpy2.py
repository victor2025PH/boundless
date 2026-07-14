# -*- coding: utf-8 -*-
"""
W0 搜索测试v2 - 修复scrcpy坐标缩放问题，正确使用设备坐标vs视频坐标
设备真实分辨率: 720x1600
scrcpy视频分辨率: ~216x480 (max_size=480)
"""
import sys, time, subprocess, logging
sys.path.insert(0, '.')

DEVICE = 'LVHIOZSWDAYLCELN'
ADB = r'C:\platform-tools\adb.exe'
DEVICE_W, DEVICE_H = 720, 1600

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

out = open('data/search_scrcpy2.txt', 'w', encoding='utf-8')

try:
    from src.host.scrcpy_manager import ScrcpySession
    import uiautomator2 as u2
    from src.vision.screen_parser import XMLParser

    d = u2.connect(DEVICE)

    log.info('Starting scrcpy session...')
    sess = ScrcpySession(DEVICE, port=27301, max_size=480, bitrate=500_000,
                         max_fps=10, enable_control=True, quality='minimal')
    ok = sess.start()
    out.write(f'scrcpy start: {ok}, control: {sess.has_control}\n')
    out.write(f'video size: {sess.screen_width}x{sess.screen_height}\n')

    if not sess.has_control:
        raise RuntimeError('No scrcpy control socket')

    # Helper: tap using device coordinates, scrcpy will scale internally
    # scrcpy protocol: send (x,y) in video space with (screen_w, screen_h) = video dims
    # So we SCALE from device coords to video coords
    vid_w, vid_h = sess.screen_width, sess.screen_height
    out.write(f'scale: {vid_w}/{DEVICE_W}={vid_w/DEVICE_W:.3f} x {vid_h}/{DEVICE_H}={vid_h/DEVICE_H:.3f}\n')

    def device_tap(dx, dy):
        """Tap at device coordinates (720x1600 space)"""
        vx = int(dx * vid_w / DEVICE_W)
        vy = int(dy * vid_h / DEVICE_H)
        out.write(f'  tap device({dx},{dy}) -> video({vx},{vy})\n')
        return sess.tap(vx, vy)

    # Step 1: Make sure FB home screen is showing
    time.sleep(0.5)
    app = d.app_current()
    out.write(f'current app: {app}\n')

    # Go to FB home if not already there
    subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-n',
                    'com.facebook.katana/.LoginActivity'],
                   capture_output=True)
    time.sleep(2)
    d.screenshot('data/sc2_s0.png')

    # Step 2: Tap search icon (580, 112) in device coords
    out.write('\nStep 2: Tap search icon\n')
    device_tap(580, 112)
    time.sleep(2.5)

    d.screenshot('data/sc2_s1.png')
    xml1 = d.dump_hierarchy()
    has_edit = 'EditText' in xml1
    out.write(f's1: xml_len={len(xml1)}, has EditText: {has_edit}\n')

    if not has_edit:
        # Search icon tap didn't work - use deep link
        out.write('Tap did not open search, using deep link fallback\n')
        subprocess.run([ADB, '-s', DEVICE, 'shell', 'am', 'start', '-a',
                        'android.intent.action.VIEW', '-d',
                        'fb://search/?search_query=test', 'com.facebook.katana'],
                       capture_output=True)
        time.sleep(2.5)
        d.screenshot('data/sc2_s1b.png')
        xml1 = d.dump_hierarchy()
        has_edit = 'EditText' in xml1
        out.write(f's1b deep link: xml_len={len(xml1)}, has EditText: {has_edit}\n')

    # Step 3: Tap the EditText to ensure focus (404, 112) device coords
    out.write('\nStep 3: Tap EditText for focus\n')
    device_tap(404, 112)
    time.sleep(1)

    # Step 4: inject_text
    out.write('\nStep 4: inject_text\n')
    for i in range(3):  # Try multiple times
        ok = sess.inject_text('Yumi Tanaka')
        out.write(f'  attempt {i+1}: inject_text={ok}\n')
        time.sleep(0.5)
        if ok:
            break

    time.sleep(1.5)
    d.screenshot('data/sc2_s2.png')
    xml2 = d.dump_hierarchy()
    open('data/sc2_s2.xml', 'w', encoding='utf-8').write(xml2)
    out.write(f's2: xml_len={len(xml2)}\n')

    # Check EditText content
    for line in xml2.split('\n'):
        if 'EditText' in line:
            import re
            text_m = re.search(r'text="([^"]*)"', line)
            if text_m:
                out.write(f'EditText text: {repr(text_m.group(1))}\n')

    # Step 5: Tap the on-screen "搜索" keyboard button (662, 1454 in device coords)
    out.write('\nStep 5: Tap keyboard search button (662,1454)\n')
    device_tap(662, 1454)
    time.sleep(4)

    d.screenshot('data/sc2_s3.png')
    xml3 = d.dump_hierarchy()
    open('data/sc2_s3.xml', 'w', encoding='utf-8').write(xml3)
    out.write(f's3: xml_len={len(xml3)}\n')

    # Also try press ENTER keycode
    out.write('\nStep 6: inject ENTER keycode\n')
    sess.inject_keycode(0, 66)  # KEYCODE_ENTER DOWN
    time.sleep(0.1)
    sess.inject_keycode(1, 66)  # UP
    time.sleep(3)

    d.screenshot('data/sc2_s4.png')
    xml4 = d.dump_hierarchy()
    open('data/sc2_s4.xml', 'w', encoding='utf-8').write(xml4)
    out.write(f's4: xml_len={len(xml4)}\n')

    # Parse final results
    def parse_results(label, xml):
        els = XMLParser.parse(xml)
        out.write(f'\n=== {label} ({len(els)} elements) ===\n')
        for e in els:
            cd = getattr(e,'content_desc','') or ''
            t = e.text or ''
            if (cd or t) and e.bounds:
                out.write(f'  {repr(t)[:30]:30s} | {repr(cd)[:50]:50s}\n')

    parse_results('s3', xml3)
    parse_results('s4', xml4)

    sess.stop()
    out.close()
    print('Done! See data/search_scrcpy2.txt')

except Exception as e:
    import traceback
    msg = traceback.format_exc()
    out.write(f'ERROR: {e}\n{msg}\n')
    out.close()
    print(f'ERROR: {e}')
    raise
