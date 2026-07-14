#!/usr/bin/env python3
"""
Minimal fastboot implementation over libusb (pyusb).
Bypasses WinUSB driver requirement on Windows.
Works with libusb-win32/libusbK drivers installed by Zadig.
"""

import sys
import struct
import time
import usb.core
import usb.util

MTK_VID = 0x0E8D
FASTBOOT_PID = 0x201C
TIMEOUT_MS = 30000
CHUNK_SIZE = 256 * 1024  # 256KB per transfer


class FastbootDevice:
    def __init__(self, dev):
        self.dev = dev
        self._claim()

    def _claim(self):
        self.dev.set_configuration()
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        )
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        )
        if not self.ep_out or not self.ep_in:
            raise RuntimeError("Cannot find bulk endpoints")

    def _send(self, data: bytes):
        self.ep_out.write(data, timeout=TIMEOUT_MS)

    def _recv(self, length=512) -> str:
        raw = self.ep_in.read(length, timeout=TIMEOUT_MS)
        return bytes(raw).decode("utf-8", errors="replace")

    def command(self, cmd: str) -> str:
        self._send(cmd.encode("utf-8"))
        responses = []
        while True:
            resp = self._recv()
            if resp.startswith("OKAY"):
                responses.append(resp)
                return "\n".join(responses)
            elif resp.startswith("FAIL"):
                raise RuntimeError(f"Fastboot FAIL: {resp[4:]}")
            elif resp.startswith("INFO"):
                responses.append(resp[4:])
                print(f"  [INFO] {resp[4:]}")
            elif resp.startswith("DATA"):
                responses.append(resp)
                return "\n".join(responses)
            else:
                responses.append(resp)
                return "\n".join(responses)

    def getvar(self, var: str) -> str:
        self._send(f"getvar:{var}".encode("utf-8"))
        resp = self._recv()
        if resp.startswith("OKAY"):
            return resp[4:].strip()
        elif resp.startswith("FAIL"):
            raise RuntimeError(f"getvar {var} failed: {resp[4:]}")
        return resp.strip()

    def flash(self, partition: str, data: bytes):
        size = len(data)
        print(f"  Sending download command ({size} bytes = {size/1024/1024:.1f} MB)...")
        self._send(f"download:{size:08x}".encode("utf-8"))
        resp = self._recv()
        if not resp.startswith("DATA"):
            raise RuntimeError(f"Expected DATA response, got: {resp}")

        sent = 0
        while sent < size:
            chunk = data[sent:sent + CHUNK_SIZE]
            self.ep_out.write(chunk, timeout=TIMEOUT_MS)
            sent += len(chunk)
            pct = sent * 100 // size
            print(f"\r  Uploading: {pct}% ({sent//1024//1024}MB/{size//1024//1024}MB)", end="", flush=True)
        print()

        resp = self._recv()
        if not resp.startswith("OKAY"):
            raise RuntimeError(f"Download failed: {resp}")
        print("  Download complete, flashing...")

        self._send(f"flash:{partition}".encode("utf-8"))
        while True:
            resp = self._recv()
            if resp.startswith("OKAY"):
                print(f"  Flash {partition} OK!")
                return
            elif resp.startswith("INFO"):
                print(f"  [INFO] {resp[4:]}")
            elif resp.startswith("FAIL"):
                raise RuntimeError(f"Flash failed: {resp[4:]}")

    def reboot(self):
        print("  Sending reboot command...")
        self._send(b"reboot")
        try:
            resp = self._recv()
            print(f"  Reboot response: {resp}")
        except Exception:
            pass


def find_fastboot_devices():
    devs = list(usb.core.find(find_all=True, idVendor=MTK_VID, idProduct=FASTBOOT_PID))
    return devs


def flash_boot(image_path: str):
    devs = find_fastboot_devices()
    if not devs:
        print("ERROR: No MediaTek fastboot device found")
        return False

    print(f"Found {len(devs)} fastboot device(s)")

    with open(image_path, "rb") as f:
        img_data = f.read()
    print(f"Boot image: {len(img_data)} bytes ({len(img_data)/1024/1024:.1f} MB)")

    for i, raw_dev in enumerate(devs):
        print(f"\n--- Device {i+1} (bus={raw_dev.bus} addr={raw_dev.address}) ---")
        try:
            fb = FastbootDevice(raw_dev)
            try:
                sn = fb.getvar("serialno")
                print(f"  Serial: {sn}")
            except Exception:
                print("  (could not read serial)")

            print(f"  Flashing boot_a...")
            fb.flash("boot_a", img_data)
            print(f"  Rebooting...")
            fb.reboot()
            print(f"  Device {i+1} DONE!")
        except Exception as e:
            print(f"  ERROR on device {i+1}: {e}")
            continue

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fastboot_libusb.py <patched_boot.img>")
        sys.exit(1)

    ok = flash_boot(sys.argv[1])
    sys.exit(0 if ok else 1)
