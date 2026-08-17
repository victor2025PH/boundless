# -*- coding: utf-8 -*-
"""desktop_agent —— 隔空拉屏·每机只读截屏推流（P0 · 2026-08-09）。

出处《隔空拉屏与远端手势操控_五视角方案_20260809.md》§六 P0。
定位：把本机桌面按需镜像到中枢 HUD 的「拉屏」源。**只读**——本文件**刻意不含任何
输入注入代码**（无 SendInput/pynput/pyautogui/keyboard），P0 的「默认无路可动」是
文件级铁证；受控输入注入是 P1/P2 的事，届时另起独立注入器并经 ops_gateway 判决。

安全/礼仪纪律：
  · 按需捕获——只在中枢正被拉屏（/api/desktop/wanted 回 true）时才截屏推流；
    没人看时只 1s 轮询问一句，零截屏零带宽，不打扰生产机（GPU 卡不做无谓抓帧）。
  · 帧只在内存流转——本机不落盘、推给中枢也只驻内存（hud_server DESKTOP 内存表）。
  · 纯 ASCII（.py 无中文常量入此文件外；本文件注释中文可，代码 ASCII）。

用法：python desktop_agent.py [--machine <id>] [--fps 8] [--hub http://127.0.0.1:7913]
  哨兵/计划任务常驻拉起（同 telemetry_agent 先例，v5 哨兵可兼看门狗）。
  依赖：mss + Pillow（刻意不用 cv2/numpy——Pillow 几乎每个 ML env 自带，只需补极轻的 mss，
  跨异构集群 venv 滚动摩擦最小；2026-08-10 P0 滚动时的可移植优化）。
"""
from __future__ import annotations

import argparse
import io
import socket
import sys
import time
import urllib.request

import mss
from PIL import Image

# 机名映射（hostname_hint -> id，与 deploy/machines.json 一致；取不到用 --machine）
HOST2ID = {
    "DESKTOP-SH6IM7V": "zhongshu", "DESKTOP-07NVNHG": "yunsheng",
    "MS-DEFYSOMWQYBZ": "shengbei", "XTZJ-2026WERVDN": "lianbei",
    "XTZJ-2026YXDYXU": "tingxie", "XTW-20250807FDX": "kouxing",
}
MAX_W = 1280            # 下采样上限（拉屏是「看」，非像素级还原；省带宽/编码）
JPEG_Q = 70


def guess_machine() -> str:
    return HOST2ID.get(socket.gethostname().upper(), socket.gethostname().lower()[:24])


def wanted(hub: str, mid: str) -> bool:
    try:
        with urllib.request.urlopen(f"{hub}/api/desktop/wanted?machine={mid}", timeout=3) as r:
            import json
            return bool(json.loads(r.read() or b"{}").get("wanted"))
    except Exception:
        return False


def grab_jpeg(sct, mon) -> bytes | None:
    try:
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.rgb)   # mss 直给 RGB 字节，免 numpy
        if img.width > MAX_W:
            img = img.resize((MAX_W, int(img.height * MAX_W / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=JPEG_Q)
        return buf.getvalue()
    except Exception:
        return None


def push(hub: str, mid: str, jpg: bytes) -> bool:
    try:
        req = urllib.request.Request(f"{hub}/api/desktop/push?machine={mid}", data=jpg,
                                     method="POST",
                                     headers={"Content-Type": "image/jpeg"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="desktop_agent 只读拉屏源")
    ap.add_argument("--machine", default="")
    ap.add_argument("--fps", type=float, default=8.0)
    ap.add_argument("--hub", default="http://127.0.0.1:7913")
    a = ap.parse_args()
    mid = (a.machine or guess_machine()).strip().lower()
    hub = a.hub.rstrip("/")
    interval = 1.0 / max(1.0, a.fps)
    print(f"desktop_agent machine={mid} hub={hub} fps={a.fps} (read-only, no input injection)")
    streaming = False
    with mss.mss() as sct:
        mon = sct.monitors[1]               # 主显示器（1 号；0 是全屏拼合）
        while True:
            if not wanted(hub, mid):
                if streaming:
                    print("no longer wanted -> idle")
                    streaming = False
                time.sleep(1.0)             # 没人看：只轻量轮询，零截屏
                continue
            if not streaming:
                print("wanted -> streaming")
                streaming = True
            t0 = time.time()
            jpg = grab_jpeg(sct, mon)
            if jpg:
                push(hub, mid, jpg)
            dt = time.time() - t0
            time.sleep(max(0.0, interval - dt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
