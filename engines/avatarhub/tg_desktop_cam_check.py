# -*- coding: utf-8 -*-
"""Telegram 桌面版摄像头预检：列出 Media Foundation 能看到的设备（与 Telegram Desktop 一致）。"""
import sys
import os
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def main():
    print("=" * 54)
    print(" Telegram 桌面版 · 摄像头预检 (Media Foundation)")
    print("=" * 54)
    names = []
    try:
        import asyncio
        from winrt.windows.devices.enumeration import DeviceInformation, DeviceClass
        async def _run():
            col = await DeviceInformation.find_all_async(DeviceClass.VIDEO_CAPTURE)
            return [d.name for d in col]
        names = asyncio.run(_run())
    except Exception:
        import subprocess
        ps1 = os.path.join(os.path.dirname(__file__), "_mfcam_enum.ps1")
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
                text=True, encoding="utf-8", errors="replace", timeout=15)
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception as e:
            print(f"  摄像头枚举失败: {e}")
            return 1

    if not names:
        print("  (未检测到任何 MF 摄像头)")
    else:
        for n in names:
            mark = ""
            if "OBS" in n.upper():
                mark = "  ← 桌面版可直接选 OBS"
            elif "SPLITCAM" in n.upper():
                mark = "  ← 推荐(免费无水印,通用):任意软件选它"
            elif "WECAM" in n.upper():
                mark = "  ← WeCam(试用带水印)"
            elif "IVCAM" in n.upper():
                mark = "  (iVCam 需手机连接，不能接 OBS 换脸画面)"
            print(f"  - {n}{mark}")

    has_obs = any("OBS" in n.upper() for n in names)
    has_split = any("SPLITCAM" in n.upper() for n in names)
    has_wecam = any("WECAM" in n.upper() for n in names)
    print("=" * 54)
    if has_split:
        print(" 结论: SplitCam 已在列表 ✓（免费无水印 · 微信/Telegram/Messenger/LINE 通用）")
        print(" 1) SplitCam 里 Media Layers + → 选 OBS Virtual Camera 作为源")
        print(" 2) 各软件摄像头 → 选 SplitCam Video Driver；麦克风 → CABLE Output")
        return 0
    if has_obs:
        print(" 结论: OBS 已在桌面版列表 ✓（少数机器可直接用）")
        print(" Telegram: 设置 → 高级 → 通话设置 → 输入设备 → OBS Virtual Camera")
        return 0
    if has_wecam:
        print(" 结论: 检测到 WeCam（试用带水印，建议改用 SplitCam）")
        return 0
    print(" 结论: 桌面版看不到 OBS（DirectShow 设备，MF 程序如 Telegram 无法读取）")
    print(" 说明: Windows 无 DShow→MF 桥，重启也不会让 OBS 出现在 Telegram 里。")
    print(" 推荐(免费无水印, 全平台通用): 装 SplitCam → https://splitcam.com/")
    print("   链路: 换脸→OBS Virtual Camera → SplitCam 抓取 → 各软件选 SplitCam Video Driver")
    return 1

if __name__ == "__main__":
    sys.exit(main())
