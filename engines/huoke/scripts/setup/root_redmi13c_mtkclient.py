#!/usr/bin/env python3
"""
MTKClient Root Script for Xiaomi Redmi 13C (23106RN0DA / Gale)
Target: HEPVC685NRSOIVWG
SoC: MediaTek Helio G85 (MT6769Z)
Firmware: V14.0.6.0.TGPMIXN
Partition: A/B, active slot = _a

Usage:
    python scripts/root_redmi13c_mtkclient.py <step>

Steps:
    1. brom_backup   - Enter BROM mode → backup boot_a + vbmeta_a + seccfg
    2. brom_unlock   - Enter BROM mode → unlock bootloader (seccfg) + disable vbmeta
    3. post_unlock   - After factory reset: verify unlock, install Magisk APK
    4. patch_boot    - Push stock boot.img to phone → Magisk patches it → pull back
    5. flash_boot    - Reboot to fastboot → flash patched boot → reboot
    6. verify_root   - Verify root + Magisk working
"""

import os
import sys
import subprocess
import time
import json
from pathlib import Path
from datetime import datetime

DEVICE_ID = "HEPVC685NRSOIVWG"
DEVICE_MODEL = "23106RN0DA"
DEVICE_CODENAME = "gale"
ACTIVE_SLOT = "_a"
FIRMWARE = "V14.0.6.0.TGPMIXN"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MTKCLIENT_DIR = Path(r"c:\openclaw\data\mtkclient")
BACKUP_DIR = PROJECT_ROOT / "data" / "root_backup" / DEVICE_ID
LIBUSB_DLL = Path(r"C:\Users\zan\AppData\Local\Programs\Python\Python313\Lib\site-packages\libusb\_platform\windows\x86_64")

os.environ["PATH"] = str(LIBUSB_DLL) + ";" + os.environ.get("PATH", "")

PARTITIONS_TO_BACKUP = ["boot_a", "boot_b", "vbmeta_a", "vbmeta_b", "seccfg", "lk_a", "preloader_a"]


def run(cmd, cwd=None, timeout=300, check=True):
    print(f"\n{'='*60}")
    print(f"[CMD] {cmd}")
    print(f"{'='*60}")
    result = subprocess.run(
        cmd, shell=True, cwd=cwd,
        capture_output=True, text=True, timeout=timeout
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(f"[STDERR] {result.stderr}")
    if check and result.returncode != 0:
        print(f"[ERROR] Command failed with exit code {result.returncode}")
        return False
    return True


def adb(cmd, timeout=30):
    return run(f"adb -s {DEVICE_ID} {cmd}", timeout=timeout, check=False)


def adb_output(cmd, timeout=30):
    result = subprocess.run(
        f"adb -s {DEVICE_ID} {cmd}",
        shell=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip()


def mtk(args, timeout=300):
    return run(
        f"python mtk.py {args}",
        cwd=str(MTKCLIENT_DIR),
        timeout=timeout,
        check=False
    )


def wait_for_brom():
    """Guide user to enter BROM mode and wait for MTK USB device."""
    print("\n" + "=" * 60)
    print("  BROM MODE - REQUIRES YOUR ACTION ON THE PHONE")
    print("=" * 60)
    print("""
    Follow these steps EXACTLY:

    1. DISCONNECT the USB cable from the phone
    2. POWER OFF the phone completely (long press power → Power Off → wait 10 sec)
    3. On the computer, the script will start waiting for BROM connection...
    4. Hold BOTH Volume Up (+) AND Volume Down (-) buttons simultaneously
    5. While holding BOTH buttons, PLUG IN the USB cable
    6. Keep holding for 5-10 seconds until the script detects the device
    7. You can release the buttons when you see "BROM mode detected!"

    NOTE: If it doesn't work on first try:
    - Make sure the phone is COMPLETELY off (not just screen off)
    - Try a USB 2.0 port (not USB 3.0)
    - Hold the volume buttons BEFORE plugging in the cable
    - If you see "HANDSHAKE FAILED", try with Zadig driver replacement

    Press ENTER when the phone is OFF and you're ready to proceed...
    """)
    input()
    print("Waiting for BROM connection... (plug in phone while holding Vol+/Vol-)")
    print("(Timeout: 120 seconds)")


def step_brom_backup():
    """Step 1: Enter BROM mode and backup critical partitions."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    wait_for_brom()

    print("\n[BACKUP] Reading critical partitions via MTKClient...")
    for part in PARTITIONS_TO_BACKUP:
        outfile = BACKUP_DIR / f"{part}.img"
        print(f"\n--- Reading {part} → {outfile} ---")
        mtk(f"r {part} {outfile}", timeout=120)

    backup_files = list(BACKUP_DIR.glob("*.img"))
    print(f"\n[RESULT] Backed up {len(backup_files)} partitions to {BACKUP_DIR}")
    for f in backup_files:
        size = f.stat().st_size
        print(f"  {f.name}: {size:,} bytes ({size/1024/1024:.1f} MB)")

    if (BACKUP_DIR / "boot_a.img").exists():
        print("\n[OK] boot_a.img backup successful!")
        return True
    else:
        print("\n[FAIL] boot_a.img backup FAILED")
        return False


def step_brom_unlock():
    """Step 2: Enter BROM mode and unlock bootloader + disable vbmeta."""
    wait_for_brom()

    print("\n[UNLOCK] Unlocking bootloader via seccfg...")
    print("[WARNING] This WILL factory reset the phone!")
    result = mtk("da seccfg unlock", timeout=120)

    print("\n[VBMETA] Disabling AVB verification...")
    mtk("da vbmeta", timeout=120)

    print("\n[DONE] Bootloader unlock command sent.")
    print("The phone should now reboot and factory reset.")
    print("After reset, you'll need to:")
    print("  1. Complete initial setup (skip Google account)")
    print("  2. Enable Developer Options (tap Build Number 7x)")
    print("  3. Enable USB Debugging")
    print("  4. Run: python scripts/root_redmi13c_mtkclient.py post_unlock")
    return result


def step_post_unlock():
    """Step 3: After factory reset - verify unlock and install Magisk APK."""
    print("[CHECK] Verifying device connection...")
    adb("devices")

    print("\n[CHECK] Verifying bootloader unlock status...")
    locked = adb_output("shell getprop ro.secureboot.lockstate")
    flash_locked = adb_output("shell getprop ro.boot.flash.locked")
    vb_state = adb_output("shell getprop ro.boot.vbmeta.device_state")
    verified = adb_output("shell getprop ro.boot.verifiedbootstate")

    print(f"  secureboot.lockstate: {locked}")
    print(f"  boot.flash.locked: {flash_locked}")
    print(f"  vbmeta.device_state: {vb_state}")
    print(f"  verifiedbootstate: {verified}")

    if locked == "unlocked" or flash_locked == "0" or vb_state == "unlocked" or verified == "orange":
        print("\n[OK] Bootloader is UNLOCKED!")
    else:
        print("\n[WARN] Bootloader might still be locked. Check values above.")
        print("If still locked, you may need to re-run the BROM unlock step.")

    print("\n[MAGISK] Checking for Magisk APK...")
    magisk_apk = None
    search_dirs = [
        PROJECT_ROOT / "apk_repo",
        PROJECT_ROOT / "data",
        Path(r"C:\Users\zan\Downloads"),
    ]
    for d in search_dirs:
        if d.exists():
            for f in d.glob("Magisk*.apk"):
                magisk_apk = f
                break
            for f in d.glob("magisk*.apk"):
                magisk_apk = f
                break

    if magisk_apk:
        print(f"[MAGISK] Found: {magisk_apk}")
        print("[MAGISK] Installing Magisk APK...")
        adb(f"install -r {magisk_apk}")
    else:
        print("[MAGISK] Magisk APK not found!")
        print("Please download Magisk APK from: https://github.com/topjohnwu/Magisk/releases")
        print(f"Place it in: {PROJECT_ROOT / 'apk_repo'}")
        print("Then re-run this step, OR install it manually on the phone.")

    return True


def step_patch_boot():
    """Step 4: Push stock boot.img to phone, patch with Magisk, pull back."""
    stock_boot = BACKUP_DIR / "boot_a.img"
    if not stock_boot.exists():
        print(f"[ERROR] Stock boot image not found at {stock_boot}")
        print("You need to run step 1 (brom_backup) first, or")
        print("manually place boot_a.img in the backup directory.")
        return False

    print(f"[PATCH] Stock boot.img: {stock_boot} ({stock_boot.stat().st_size:,} bytes)")

    print("\n[PATCH] Pushing boot.img to phone for Magisk patching...")
    adb(f'push "{stock_boot}" /sdcard/Download/boot.img')

    print("""
    ============================================================
     MANUAL STEP REQUIRED - Patch boot.img with Magisk
    ============================================================

    On the phone:
    1. Open Magisk app
    2. Tap "Install" next to "Magisk" section
    3. Choose "Select and Patch a File"
    4. Navigate to Downloads → select "boot.img"
    5. Wait for patching to complete
    6. You should see "magisk_patched-XXXXX.img" in Downloads

    Press ENTER when patching is complete...
    """)
    input()

    print("[PATCH] Pulling patched boot.img from phone...")
    adb("shell ls -la /sdcard/Download/magisk_patched*.img")

    patched_name = adb_output("shell ls /sdcard/Download/magisk_patched*.img")
    if not patched_name:
        print("[ERROR] No patched boot image found on phone!")
        return False

    patched_local = BACKUP_DIR / "boot_a_magisk_patched.img"
    adb(f'pull "{patched_name}" "{patched_local}"')

    if patched_local.exists():
        print(f"\n[OK] Patched boot.img saved: {patched_local} ({patched_local.stat().st_size:,} bytes)")
        return True
    else:
        print("[ERROR] Failed to pull patched boot image!")
        return False


def step_flash_boot():
    """Step 5: Reboot to fastboot and flash patched boot.img."""
    patched_boot = BACKUP_DIR / "boot_a_magisk_patched.img"
    if not patched_boot.exists():
        print(f"[ERROR] Patched boot image not found at {patched_boot}")
        return False

    print(f"[FLASH] Patched boot.img: {patched_boot} ({patched_boot.stat().st_size:,} bytes)")

    print("\n[FLASH] Rebooting to fastboot mode...")
    adb("reboot bootloader")
    print("Waiting 15 seconds for fastboot...")
    time.sleep(15)

    print("[FLASH] Checking fastboot connection...")
    run("fastboot devices")

    print("\n[FLASH] Flashing patched boot to boot_a...")
    run(f'fastboot flash boot_a "{patched_boot}"', timeout=60)

    print("\n[FLASH] Disabling AVB verification on vbmeta...")
    run("fastboot --disable-verity --disable-verification flash vbmeta_a vbmeta_a.img 2>nul", check=False)

    stock_vbmeta = BACKUP_DIR / "vbmeta_a.img"
    if stock_vbmeta.exists():
        print("[FLASH] Flashing vbmeta with verification disabled...")
        run(f'fastboot --disable-verity --disable-verification flash vbmeta_a "{stock_vbmeta}"', timeout=60, check=False)

    print("\n[FLASH] Rebooting...")
    run("fastboot reboot")
    print("Waiting 60 seconds for boot...")
    time.sleep(60)

    print("[CHECK] Checking ADB connection...")
    run(f"adb -s {DEVICE_ID} wait-for-device", timeout=120)
    time.sleep(10)

    return True


def step_verify_root():
    """Step 6: Verify root access and Magisk installation."""
    print("[VERIFY] Checking device connection...")
    adb("devices")

    print("\n[VERIFY] Testing root access...")
    root_id = adb_output("shell su -c id")
    print(f"  su -c id: {root_id}")

    print("\n[VERIFY] Checking Magisk version...")
    magisk_v = adb_output("shell su -c 'magisk -v'")
    magisk_vc = adb_output("shell su -c 'magisk -V'")
    print(f"  Magisk version: {magisk_v}")
    print(f"  Magisk versionCode: {magisk_vc}")

    print("\n[VERIFY] Checking Zygisk status...")
    zygisk = adb_output("shell su -c 'magisk --denylist ls 2>/dev/null && echo zygisk_ok'")
    print(f"  Zygisk: {zygisk}")

    print("\n[VERIFY] Device properties...")
    props = {
        "lockstate": adb_output("shell getprop ro.secureboot.lockstate"),
        "flash.locked": adb_output("shell getprop ro.boot.flash.locked"),
        "vbmeta": adb_output("shell getprop ro.boot.vbmeta.device_state"),
        "verified": adb_output("shell getprop ro.boot.verifiedbootstate"),
    }
    for k, v in props.items():
        print(f"  {k}: {v}")

    success = "uid=0(root)" in root_id
    if success:
        print("\n" + "=" * 60)
        print("  ROOT VERIFIED SUCCESSFULLY!")
        print(f"  Device: {DEVICE_ID}")
        print(f"  Magisk: {magisk_v} ({magisk_vc})")
        print("=" * 60)

        report = {
            "device_id": DEVICE_ID,
            "model": DEVICE_MODEL,
            "codename": DEVICE_CODENAME,
            "firmware": FIRMWARE,
            "root_status": "rooted",
            "magisk_version": magisk_v,
            "magisk_version_code": magisk_vc,
            "bootloader": "unlocked",
            "verified_boot": props["verified"],
            "rooted_at": datetime.now().isoformat(),
            "method": "mtkclient_brom",
        }
        report_file = BACKUP_DIR / "root_report.json"
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n[SAVED] Root report: {report_file}")
    else:
        print("\n[FAIL] Root verification FAILED!")
        print("Possible issues:")
        print("  - Magisk not installed properly")
        print("  - Need to grant su permission in Magisk app")
        print("  - Boot partition not flashed correctly")

    return success


STEPS = {
    "brom_backup": step_brom_backup,
    "brom_unlock": step_brom_unlock,
    "post_unlock": step_post_unlock,
    "patch_boot": step_patch_boot,
    "flash_boot": step_flash_boot,
    "verify_root": step_verify_root,
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nAvailable steps:")
        for name, func in STEPS.items():
            print(f"  {name:15s} - {func.__doc__.strip().split(chr(10))[0]}")
        sys.exit(1)

    step = sys.argv[1]
    if step not in STEPS:
        print(f"Unknown step: {step}")
        print(f"Available: {', '.join(STEPS.keys())}")
        sys.exit(1)

    print(f"\n{'#'*60}")
    print(f"  MTKClient Root - Redmi 13C ({DEVICE_ID})")
    print(f"  Step: {step}")
    print(f"  Time: {datetime.now()}")
    print(f"{'#'*60}")

    success = STEPS[step]()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
