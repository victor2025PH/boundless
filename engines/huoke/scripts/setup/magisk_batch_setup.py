#!/usr/bin/env python3
"""
Batch Magisk setup for multiple phones.
Automates: Fix environment → Direct Install → Reboot → Install module → Reboot → Verify root
"""
import subprocess
import time
import re
import sys
import xml.etree.ElementTree as ET

def adb(serial, *args, timeout=30):
    cmd = ["adb", "-s", serial] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() + r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"

def tap_text(serial, xml_str, text):
    pattern = f'text="{re.escape(text)}"[^>]*bounds="\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]"'
    m = re.search(pattern, xml_str)
    if m:
        cx = (int(m.group(1)) + int(m.group(3))) // 2
        cy = (int(m.group(2)) + int(m.group(4))) // 2
        adb(serial, "shell", f"input tap {cx} {cy}")
        return True
    return False

def tap_resource(serial, xml_str, res_id):
    pattern = f'resource-id="{re.escape(res_id)}"[^>]*bounds="\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]"'
    m = re.search(pattern, xml_str)
    if m:
        cx = (int(m.group(1)) + int(m.group(3))) // 2
        cy = (int(m.group(2)) + int(m.group(4))) // 2
        adb(serial, "shell", f"input tap {cx} {cy}")
        return True
    return False

def dump_ui(serial):
    adb(serial, "shell", "uiautomator dump --compressed /sdcard/_ui.xml 2>/dev/null")
    time.sleep(1)
    return adb(serial, "shell", "cat /sdcard/_ui.xml")

def get_texts(xml_str):
    return [m.group(1) for m in re.finditer(r'text="([^"]+)"', xml_str)]

def unlock_screen(serial):
    adb(serial, "shell", "input keyevent KEYCODE_WAKEUP")
    time.sleep(1)
    adb(serial, "shell", "input swipe 540 2000 540 800 300")
    time.sleep(2)

def wait_for_device(serial, max_wait=120):
    for i in range(max_wait // 5):
        time.sleep(5)
        r = adb(serial, "devices", timeout=5)
        if f"{serial}\tdevice" in subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout:
            return True
    return False

def setup_phone(serial):
    print(f"\n{'='*60}")
    print(f"  SETTING UP: {serial}")
    print(f"{'='*60}")

    # Step 1: Unlock and open Magisk
    print("[1/8] Unlocking screen...")
    unlock_screen(serial)
    adb(serial, "shell", "am start -S -n com.topjohnwu.magisk/com.topjohnwu.magisk.ui.MainActivity")
    time.sleep(8)

    # Step 2: Handle "Fix environment" dialog
    print("[2/8] Checking for fix environment dialog...")
    xml = dump_ui(serial)
    texts = get_texts(xml)
    
    if "需要修复运行环境" in texts or "确定" in texts:
        print("  Found fix dialog, clicking OK...")
        tap_text(serial, xml, "确定")
        time.sleep(3)
        
        # Should now show install options
        xml = dump_ui(serial)
        texts = get_texts(xml)
        
        if "直接安装（推荐）" in texts:
            print("[3/8] Selecting Direct Install...")
            tap_text(serial, xml, "直接安装（推荐）")
            time.sleep(1)
            tap_text(serial, xml, "开始")
            time.sleep(15)
            
            xml = dump_ui(serial)
            if "完成！" in get_texts(xml) or "All done" in str(xml):
                print("[4/8] Direct Install complete! Rebooting...")
                tap_text(serial, xml, "重启")
            else:
                print(f"  Unexpected state: {get_texts(xml)[:5]}")
                adb(serial, "reboot")
        else:
            print(f"  Unexpected state after OK: {texts[:5]}")
            # Try direct install anyway
            adb(serial, "reboot")
    elif "Magisk" in texts and "安装" in texts:
        print("  No fix dialog, Magisk home screen detected")
        print("  Proceeding to module installation...")
    else:
        print(f"  Unknown state: {texts[:5]}")
    
    # Step 5: Wait for reboot
    print("[5/8] Waiting for reboot...")
    time.sleep(35)
    if not wait_for_device(serial):
        print(f"  ERROR: Device {serial} didn't come back!")
        return False
    time.sleep(15)

    # Step 6: Install module
    print("[6/8] Installing shell_su_fix module...")
    unlock_screen(serial)
    adb(serial, "shell", "am start -S -n com.topjohnwu.magisk/com.topjohnwu.magisk.ui.MainActivity")
    time.sleep(5)
    
    xml = dump_ui(serial)
    # Dismiss any dialog first
    if "确定" in get_texts(xml) and "需要修复" in str(xml):
        print("  Fix dialog appeared again, clicking OK...")
        tap_text(serial, xml, "确定")
        time.sleep(3)
        xml = dump_ui(serial)
        if "直接安装" in str(xml):
            tap_text(serial, xml, "直接安装（推荐）")
            time.sleep(1)
            tap_text(serial, xml, "开始")
            time.sleep(15)
            xml = dump_ui(serial)
            tap_text(serial, xml, "重启")
            time.sleep(35)
            if not wait_for_device(serial):
                print(f"  ERROR: Device didn't come back after 2nd direct install!")
                return False
            time.sleep(15)
            unlock_screen(serial)
            adb(serial, "shell", "am start -S -n com.topjohnwu.magisk/com.topjohnwu.magisk.ui.MainActivity")
            time.sleep(5)
            xml = dump_ui(serial)
    
    # Dismiss warning if present
    if "不再显示" in get_texts(xml):
        tap_text(serial, xml, "不再显示")
        time.sleep(1)
        xml = dump_ui(serial)
    
    # Navigate to Modules tab
    tap_resource(serial, xml, "com.topjohnwu.magisk:id/modulesFragment")
    time.sleep(3)
    xml = dump_ui(serial)
    
    if "从本地安装" in get_texts(xml):
        tap_text(serial, xml, "从本地安装")
        time.sleep(3)
        
        # Open nav drawer for Downloads
        adb(serial, "shell", "input tap 50 100")
        time.sleep(2)
        xml = dump_ui(serial)
        tap_text(serial, xml, "下载")
        time.sleep(3)
        
        xml = dump_ui(serial)
        tap_text(serial, xml, "shell_su_fix.zip")
        time.sleep(3)
        
        xml = dump_ui(serial)
        if "确定" in get_texts(xml):
            tap_text(serial, xml, "确定")
            time.sleep(10)
            
            xml = dump_ui(serial)
            if "完成！" in get_texts(xml):
                print("  Module installed!")
            else:
                print(f"  Module install result: {get_texts(xml)[:5]}")
    else:
        print(f"  Modules screen unexpected: {get_texts(xml)[:5]}")

    # Step 7: Reboot for module activation
    print("[7/8] Rebooting for module activation...")
    adb(serial, "reboot")
    time.sleep(35)
    if not wait_for_device(serial):
        print(f"  ERROR: Device didn't come back after module reboot!")
        return False
    time.sleep(15)

    # Step 8: Verify root
    print("[8/8] Verifying root...")
    result = adb(serial, "shell", "/debug_ramdisk/su -c id", timeout=15)
    if "uid=0(root)" in result:
        print(f"  ROOT VERIFIED: {result}")
        return True
    else:
        print(f"  ROOT FAILED: {result}")
        # Try with PATH
        result2 = adb(serial, "shell", "PATH=/debug_ramdisk:$PATH su -c id", timeout=15)
        if "uid=0(root)" in result2:
            print(f"  ROOT OK (via PATH): {result2}")
            return True
        return False

def main():
    devices = sys.argv[1:] if len(sys.argv) > 1 else [
        "7HKB6HRSHYDMIJ4X", "BACIKBQ8CYCYDAHU", "EY6X856DAAORVOPB",
        "HIXOOB7DEIQ4RCDI", "J7Z5TGTCDA9H7DMF", "QWSSW86HJNZTD6EI", "SW6DB68DUCYDXWUG"
    ]
    
    results = {}
    for d in devices:
        try:
            ok = setup_phone(d)
            results[d] = "ROOT OK" if ok else "FAILED"
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            results[d] = f"ERROR: {e}"
    
    print(f"\n{'='*60}")
    print("  BATCH RESULTS")
    print(f"{'='*60}")
    for d, r in results.items():
        status = "✓" if "OK" in r else "✗"
        print(f"  {status} {d}: {r}")
    
    ok_count = sum(1 for r in results.values() if "OK" in r)
    print(f"\n  {ok_count}/{len(devices)} devices rooted successfully")

if __name__ == "__main__":
    main()
