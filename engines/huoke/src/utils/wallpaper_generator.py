# -*- coding: utf-8 -*-
"""
Wallpaper Generator & Auto-Manager.

Generates numbered wallpaper images and deploys them silently via ADB.
Provides WallpaperAutoManager for fully automatic numbering on startup
and when new devices come online.

Usage:
    from src.utils.wallpaper_generator import generate_wallpaper, deploy_wallpaper
    from src.utils.wallpaper_generator import WallpaperAutoManager

    # Generate + push + set silently
    deploy_wallpaper(device_manager, device_id, number=1, display_name="Phone-1")

    # Auto-manage all devices
    mgr = WallpaperAutoManager(config_root)
    mgr.ensure_all_numbered(device_manager)
    mgr.on_device_online(device_manager, "SERIAL123")
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from src.host.device_registry import PROJECT_ROOT, tools_dir

log = logging.getLogger(__name__)

_OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "openclaw_wallpapers")


def _lookup_device_location(serial: str) -> str:
    """Query location label (e.g. "主控-手机05") from shared openclaw.db devices table."""
    try:
        db_path = PROJECT_ROOT / "data" / "openclaw.db"
        if not db_path.exists():
            return ""
        import sqlite3
        with sqlite3.connect(str(db_path), timeout=3) as conn:
            row = conn.execute(
                "SELECT location FROM devices WHERE serial=?", (serial,)
            ).fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def _get_cjk_font(size: int):
    """Return a font that supports CJK characters (Microsoft YaHei preferred)."""
    from PIL import ImageFont
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════
#  Wallpaper image generation
# ═══════════════════════════════════════════════════════════════════════════

def _number_to_hsl(number: int):
    """黄金角分布: 每个编号获得独特色相。"""
    hue = (number * 137.508) % 360
    return hue


def _hsl_to_rgb(h: float, s: float, l: float):
    """HSL → RGB (h:0-360, s:0-1, l:0-1)。"""
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return int(r * 255), int(g * 255), int(b * 255)


def generate_wallpaper(number: int,
                       display_name: str = "",
                       width: int = 720,
                       height: int = 1600,
                       output_dir: str = "",
                       location: str = "") -> str:
    """Generate a numbered wallpaper PNG with unique color per device."""
    from PIL import Image, ImageDraw

    out_dir = output_dir or _OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    # 基于编号的独特色相
    hue = _number_to_hsl(number)

    # 渐变背景: 深色顶部 → 更深色底部, 色调随编号变化
    for y in range(height):
        ratio = y / height
        r, g, b = _hsl_to_rgb(hue, 0.45, 0.08 + ratio * 0.07)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # 装饰光晕 — 与编号同色系
    accent_r, accent_g, accent_b = _hsl_to_rgb(hue, 0.7, 0.5)
    cx, cy = width // 2, int(height * 0.36)
    glow_r = int(width * 0.35)
    for offset in range(glow_r, 0, -2):
        alpha_ratio = (offset / glow_r)
        a = int(15 * alpha_ratio)
        r = int(accent_r * (1 - alpha_ratio * 0.7))
        g = int(accent_g * (1 - alpha_ratio * 0.7))
        b = int(accent_b * (1 - alpha_ratio * 0.7))
        draw.ellipse(
            [cx - offset, cy - offset, cx + offset, cy + offset],
            fill=(r, g, b),
        )

    # 编号圆环
    ring_r = int(width * 0.24)
    for dr in range(4):
        draw.ellipse(
            [cx - ring_r - dr, cy - ring_r - dr,
             cx + ring_r + dr, cy + ring_r + dr],
            outline=(accent_r, accent_g, accent_b),
        )

    num_str = f"{number:02d}"

    font_number = _get_font(int(width * 0.50))
    font_subtitle = _get_font(int(width * 0.055))
    font_brand = _get_font(int(width * 0.04))
    font_label = _get_font(int(width * 0.035))

    # 大号编号
    _draw_text_centered(draw, num_str, font_number, width, int(height * 0.28),
                        fill=(255, 255, 255), shadow=True)

    # 号字标签
    _draw_text_centered(draw, "号 机", font_label, width, int(height * 0.48),
                        fill=(accent_r, accent_g, accent_b))

    # 设备名称
    if display_name:
        _draw_text_centered(draw, display_name, font_subtitle, width,
                            int(height * 0.54), fill=(148, 163, 184))

    # 位置标签（如"主控-手机05"，来自共享 devices 表）
    if location:
        font_location = _get_cjk_font(int(width * 0.042))
        _draw_text_centered(draw, location, font_location, width,
                            int(height * 0.61), fill=(100, 200, 255))

    # 底部分隔线 + 品牌
    line_y = int(height * 0.85)
    line_margin = int(width * 0.25)
    draw.line([(line_margin, line_y), (width - line_margin, line_y)],
              fill=(accent_r // 3, accent_g // 3, accent_b // 3), width=1)

    _draw_text_centered(draw, "OpenClaw", font_brand, width,
                        int(height * 0.87), fill=(100, 116, 139))

    out_path = os.path.join(out_dir, f"wallpaper_{num_str}.png")
    img.save(out_path, format="PNG", optimize=True)
    log.info("[壁纸] 生成编号壁纸 #%s (hue=%.0f) → %s", num_str, hue, out_path)
    return out_path


def _get_font(size: int):
    from PIL import ImageFont
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_text_centered(draw, text: str, font, canvas_width: int, y: int,
                        fill=(255, 255, 255), shadow: bool = False):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (canvas_width - tw) // 2
    if shadow:
        for dx, dy in [(2, 2), (-1, -1), (3, 3)]:
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


# ═══════════════════════════════════════════════════════════════════════════
#  Silent wallpaper deployment via ADB
# ═══════════════════════════════════════════════════════════════════════════

_REMOTE_PATH = "/sdcard/Download/openclaw_wallpaper.png"
_HELPER_PKG = "com.openclaw.wallpaperhelper"
_HELPER_APK_LOCAL = str(tools_dir() / "wallpaper_helper" / "openclaw_wp_helper.apk")
_HELPER_JAR_LOCAL = str(tools_dir() / "wallpaper_helper" / "openclaw_wp.jar")
_REMOTE_JAR = "/data/local/tmp/openclaw_wp.jar"


def _try_app_process_wallpaper(manager, device_id: str) -> bool:
    """通过 app_process + dex jar 直调 WallpaperManager hidden API 设壁纸。

    完全不安装任何 APK，秒级，不触发 com.miui.securitycenter。
    需要 _REMOTE_PATH 已被 push 到设备（deploy_wallpaper 主流程已完成）。

    实测验证（slot 2，2026-04-25）：焦点零变化、最近 Activity 无 securitycenter 痕迹。
    """
    if not os.path.exists(_HELPER_JAR_LOCAL):
        return False

    ok, out = manager._run_adb(
        ['push', _HELPER_JAR_LOCAL, _REMOTE_JAR], device_id, timeout=15)
    if not ok:
        log.warning("[壁纸] jar push 失败 %s: %s", device_id[:8], (out or '')[:120])
        return False

    cmd = (f"CLASSPATH={_REMOTE_JAR} "
           f"app_process /system/bin com.openclaw.WallpaperSetter {_REMOTE_PATH}")
    ok, out = manager._run_adb(['shell', cmd], device_id, timeout=20)
    if ok and 'WP_SET_OK' in (out or ''):
        log.info("[壁纸] app_process 直调成功 %s", device_id[:8])
        return True
    log.warning("[壁纸] app_process 失败 %s: %s", device_id[:8], (out or '')[:200])
    return False


def _try_root_method(manager, device_id: str) -> bool:
    """Try setting wallpaper via root cp to system path."""
    script = (
        f'cp {_REMOTE_PATH} /data/system/users/0/wallpaper '
        f'&& chmod 600 /data/system/users/0/wallpaper '
        f'&& chown system:system /data/system/users/0/wallpaper '
        f'&& am broadcast -a android.intent.action.WALLPAPER_CHANGED '
        f'&& echo ROOT_WP_OK'
    )
    ok, out = manager._run_adb(['shell', 'su', '-c', script], device_id)
    if ok and 'ROOT_WP_OK' in out:
        return True
    ok, out = manager._run_adb(['shell', script], device_id)
    return ok and 'ROOT_WP_OK' in out


def _ensure_helper_installed(manager, device_id: str) -> bool:
    """确保壁纸 Helper APK 已安装。未安装则自动安装+授权+禁用轮播。

    走 src/utils/safe_apk_install.safe_install_apk（push + pm install），
    绕开 MIUI 14+ 的 securitycenter/AdbInstallActivity 拦截。
    主流程已优先走 _try_app_process_wallpaper（零安装），此函数仅作 fallback。
    """
    ok, out = manager._run_adb(
        ['shell', 'pm', 'list', 'packages', _HELPER_PKG], device_id)
    if ok and _HELPER_PKG in out:
        return True

    if not os.path.exists(_HELPER_APK_LOCAL):
        log.warning("[壁纸] Helper APK 不存在: %s", _HELPER_APK_LOCAL)
        return False

    from src.utils.safe_apk_install import safe_install_apk
    adb = getattr(manager, 'adb_path', 'adb')
    success, msg = safe_install_apk(
        adb, device_id, _HELPER_APK_LOCAL, replace=True, test=True, timeout=30)
    if not success:
        log.warning("[壁纸] Helper APK 安装失败 %s: %s", device_id[:8], msg)
        ok2, out2 = manager._run_adb(
            ['shell', 'pm', 'list', 'packages', _HELPER_PKG], device_id)
        if not (ok2 and _HELPER_PKG in out2):
            return False
    log.info("[壁纸] Helper APK 安装成功: %s", device_id[:8])

    # 授权 + 禁用壁纸轮播
    manager._run_adb(['shell',
        'pm grant com.openclaw.wallpaperhelper android.permission.READ_MEDIA_IMAGES 2>/dev/null;'
        'pm grant com.openclaw.wallpaperhelper android.permission.READ_EXTERNAL_STORAGE 2>/dev/null;'
        'settings put secure lock_screen_magazine_status 0;'
        'settings put secure miui_wallpaper_content_type 0;'
        'pm disable-user --user 0 com.miui.android.fashiongallery 2>/dev/null'
    ], device_id)

    return True


def _try_helper_apk_wallpaper(manager, device_id: str) -> bool:
    """通过 Helper APK 的 BroadcastReceiver 设壁纸（秒级，无UI交互）。

    说明：默认 `am broadcast` 会等待 Receiver 跑完再返回。WallpaperManager.setBitmap
    在部分 MIUI/Android 13 上可能阻塞数十秒，导致主机侧 ADB 超时（旧版仅用 10s 超时易失败）。
    使用 `--async` 只派发广播、不等待完成，避免误报失败；极老系统无该参数时再回退同步并加长超时。
    """
    if not _ensure_helper_installed(manager, device_id):
        return False

    base = [
        '-a', 'com.openclaw.SET_WALLPAPER',
        '--es', 'path', _REMOTE_PATH,
        '-n', f'{_HELPER_PKG}/.WallpaperReceiver',
    ]
    ok, out = manager._run_adb(['shell', 'am', 'broadcast', '--async'] + base, device_id, timeout=30)
    if ok:
        log.info("[壁纸] Helper APK 异步派发 %s: %s", device_id[:8], (out or '')[:100])
        return True
    if out and ('Unknown option' in out or 'unknown option' in out.lower()):
        ok2, out2 = manager._run_adb(['shell', 'am', 'broadcast'] + base, device_id, timeout=120)
        if ok2 and 'result=0' in (out2 or ''):
            log.info("[壁纸] Helper APK 同步成功 %s", device_id[:8])
            return True
        log.warning("[壁纸] Helper APK 同步失败 %s: %s", device_id[:8], out2)
        return False
    log.warning("[壁纸] Helper APK 异步派发失败 %s: %s", device_id[:8], out)
    return False


def _try_gallery_wallpaper(manager, device_id: str) -> bool:
    """在 MIUI 相册中打开已推送的壁纸文件，供用户手动「设为壁纸」。"""
    ok, out = manager._run_adb(
        ['shell', 'am', 'start', '-n',
         'com.miui.gallery/.activity.ExternalPhotoPageActivity',
         '-d', f'file://{_REMOTE_PATH}',
         '-t', 'image/png'],
        device_id,
    )
    if ok and 'Starting' in (out or ''):
        log.info("[壁纸] 设备 %s MIUI Gallery 已打开壁纸图片", device_id[:8])
        return True
    return False


def _try_miui_auto_wallpaper(manager, device_id: str) -> bool:
    """通过 MIUI Gallery + input tap 自动设桌面壁纸（Redmi 13C 720x1600 等）。

    当仓库无 Helper APK 或 Helper 广播失败时使用。需开启「USB 调试（安全设置）」
    否则 input tap 无效（与 JFIJYPR 等机型的已知限制一致）。
    """
    import time

    manager._run_adb(['shell', 'am', 'force-stop', 'com.miui.gallery'], device_id)
    time.sleep(0.5)

    ok, out = manager._run_adb(
        ['shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
         '-d', f'file://{_REMOTE_PATH}',
         '-t', 'image/png',
         '--grant-read-uri-permission'],
        device_id,
    )
    if not ok or 'Error' in (out or ''):
        ok, out = manager._run_adb(
            ['shell', 'am', 'start', '-n',
             'com.miui.gallery/.activity.ExternalPhotoPageActivity',
             '-d', f'file://{_REMOTE_PATH}',
             '-t', 'image/png'],
            device_id,
        )
        if not ok:
            return False

    time.sleep(2.5)

    manager._run_adb(['shell', 'input', 'tap', '360', '800'], device_id)
    time.sleep(0.8)

    manager._run_adb(['shell', 'input', 'tap', '688', '75'], device_id)
    time.sleep(1.0)

    _tap_wallpaper_option(manager, device_id)
    time.sleep(1.5)

    _tap_apply_button(manager, device_id)
    time.sleep(1.0)

    _tap_home_screen_option(manager, device_id)
    time.sleep(1.0)

    manager._run_adb(['shell', 'input', 'keyevent', 'KEYCODE_HOME'], device_id)

    log.info("[壁纸] 设备 %s MIUI 自动壁纸流程已执行", device_id[:8])
    return True


def _tap_wallpaper_option(manager, device_id: str):
    """尝试点击「设为壁纸」菜单项（适配多种 MIUI 布局）。"""
    import time
    try:
        manager._run_adb(
            ['shell', 'uiautomator', 'dump', '/sdcard/ui_dump.xml'],
            device_id, timeout=5,
        )
        ok, xml = manager._run_adb(
            ['shell', 'cat', '/sdcard/ui_dump.xml'],
            device_id, timeout=3,
        )
        if ok and xml:
            import re
            for pattern in [
                r'text="设为壁纸"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="设置壁纸"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="Set as wallpaper"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                m = re.search(pattern, xml)
                if m:
                    x = (int(m.group(1)) + int(m.group(3))) // 2
                    y = (int(m.group(2)) + int(m.group(4))) // 2
                    manager._run_adb(
                        ['shell', 'input', 'tap', str(x), str(y)], device_id)
                    log.debug("[壁纸] uiautomator 找到壁纸按钮: (%d,%d)", x, y)
                    return
    except Exception:
        pass

    for y in [435, 483, 531, 387]:
        manager._run_adb(['shell', 'input', 'tap', '360', str(y)], device_id)
        time.sleep(0.3)
        ok, out = manager._run_adb(
            ['shell', 'dumpsys', 'activity', 'top'],
            device_id, timeout=3,
        )
        if ok and ('wallpaper' in (out or '').lower()
                   or 'crop' in (out or '').lower()):
            return


def _tap_apply_button(manager, device_id: str):
    """点击「应用」等确认按钮。"""
    import time
    try:
        manager._run_adb(
            ['shell', 'uiautomator', 'dump', '/sdcard/ui_dump.xml'],
            device_id, timeout=5,
        )
        ok, xml = manager._run_adb(
            ['shell', 'cat', '/sdcard/ui_dump.xml'],
            device_id, timeout=3,
        )
        if ok and xml:
            import re
            for pattern in [
                r'text="应用"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="Apply"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="确定"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="设为桌面"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                m = re.search(pattern, xml)
                if m:
                    x = (int(m.group(1)) + int(m.group(3))) // 2
                    y = (int(m.group(2)) + int(m.group(4))) // 2
                    manager._run_adb(
                        ['shell', 'input', 'tap', str(x), str(y)], device_id)
                    log.debug("[壁纸] 找到应用按钮: (%d,%d)", x, y)
                    return
    except Exception:
        pass

    manager._run_adb(['shell', 'input', 'tap', '360', '1520'], device_id)


def _tap_home_screen_option(manager, device_id: str):
    """若弹出桌面/锁屏选择，点「桌面」。"""
    try:
        manager._run_adb(
            ['shell', 'uiautomator', 'dump', '/sdcard/ui_dump.xml'],
            device_id, timeout=5,
        )
        ok, xml = manager._run_adb(
            ['shell', 'cat', '/sdcard/ui_dump.xml'],
            device_id, timeout=3,
        )
        if ok and xml:
            import re
            for pattern in [
                r'text="桌面"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="主屏幕"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="Home screen"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="设为桌面"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                m = re.search(pattern, xml)
                if m:
                    x = (int(m.group(1)) + int(m.group(3))) // 2
                    y = (int(m.group(2)) + int(m.group(4))) // 2
                    manager._run_adb(
                        ['shell', 'input', 'tap', str(x), str(y)], device_id)
                    return
    except Exception:
        pass

    manager._run_adb(['shell', 'input', 'tap', '360', '780'], device_id)


def _set_device_label(manager, device_id: str, number: int) -> bool:
    """Set device name so the number is visible in system settings.

    部分机型上 settings put 偶发慢，使用独立超时，避免继承过短的 connection.timeout_seconds。
    """
    name = f"OpenClaw-{number:02d}"
    ok1, e1 = manager._run_adb(
        ['shell', 'settings', 'put', 'global', 'device_name', name],
        device_id, timeout=20,
    )
    ok2, e2 = manager._run_adb(
        ['shell', 'settings', 'put', 'system', 'device_name', name],
        device_id, timeout=20,
    )
    if ok1 or ok2:
        log.info("[壁纸] 设备 %s 名称已设为 %s (global=%s system=%s)", device_id[:8], name, ok1, ok2)
        return True
    log.warning("[壁纸] 设备名称写入失败 %s: global=%s system=%s", device_id[:8], e1, e2)
    return False


def deploy_wallpaper(manager, device_id: str, number: int,
                     display_name: str = "",
                     width: int = 720, height: int = 1600,
                     location: str = "") -> bool:
    """
    Generate a numbered wallpaper and deploy it to a device.

    Tries methods in order:
      1. su root cp（秒级，需 root）
      2. Helper APK 广播（★主路径；用 safe_install_apk 安装不再触发手机管家；
         真正调用 WallpaperManager 高级 API，能触发 launcher 刷新）
      3. app_process + jar 直调 hidden API（fallback；零安装但有 commit bug：
         setWallpaper(...)/PFD 写入成功、dumpsys id 递增，但 launcher 不刷新；
         仅在某些已经设过同图的场景下"看似生效"，不可靠）
      4. MIUI 相册 + input 自动化（无 APK/jar 时的回退，Redmi/MIUI）
      5. 仅打开相册 / 已推送文件（保底）
    """
    try:
        if not location:
            location = _lookup_device_location(device_id)
        local_path = generate_wallpaper(number, display_name, width, height,
                                        location=location)

        ok, out = manager._run_adb(
            ['push', local_path, _REMOTE_PATH], device_id,
        )
        if not ok:
            log.error("[壁纸] Push 失败 %s: %s", device_id[:8], out)
            return False

        _set_device_label(manager, device_id, number)

        # Method 1: Root (最快, 秒级)
        if _try_root_method(manager, device_id):
            log.info("[壁纸] 设备 %s root壁纸设置成功 (#%02d)",
                     device_id[:8], number)
            return True

        # Method 2: Helper APK broadcast ★主路径（safe_install_apk 不弹手机管家）
        helper_ok = _try_helper_apk_wallpaper(manager, device_id)
        if helper_ok:
            log.info("[壁纸] 设备 %s APK 壁纸设置成功 (#%02d)",
                     device_id[:8], number)
            return True

        # Method 4: MIUI Gallery 自动化（无需 APK，与旧版 bundle 行为一致）
        if _try_miui_auto_wallpaper(manager, device_id):
            log.info("[壁纸] 设备 %s MIUI 自动壁纸设置成功 (#%02d)",
                     device_id[:8], number)
            return True

        # Method 4: 至少打开相册，便于手动点「设为壁纸」
        if _try_gallery_wallpaper(manager, device_id):
            log.warning(
                "[壁纸] 设备 %s 已打开相册，若桌面未变请手动设为壁纸或检查 USB 调试(安全设置)",
                device_id[:8],
            )
        else:
            log.info(
                "[壁纸] 设备 %s 壁纸文件已推送 (#%02d)，自动流程均未成功，请手动设置",
                device_id[:8], number,
            )
        return True

    except Exception as e:
        log.error("[壁纸] 部署失败 %s: %s", device_id[:8], e)
        return False


def deploy_all_wallpapers(manager, device_configs: dict) -> dict:
    """Deploy numbered wallpapers to all configured devices.

    如果 device_configs 为空（config/devices.yaml 没列设备但实际有 ADB 在线），
    回退到 manager.get_all_devices() 用 device_aliases.json 的 number 字段。
    避免 dashboard 点'部署壁纸'时 total=0 的迷之静默。
    """
    results = {}

    if not device_configs and manager:
        from pathlib import Path
        try:
            from src.host.device_registry import PROJECT_ROOT
            aliases_path = PROJECT_ROOT / "config" / "device_aliases.json"
            aliases = {}
            if aliases_path.exists():
                aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
        except Exception:
            aliases = {}
        for dev in manager.get_all_devices():
            if not getattr(dev, "is_online", False):
                continue
            sid = dev.device_id
            entry = aliases.get(sid, {})
            num = entry.get("number") or entry.get("wallpaper_number")
            if not num:
                continue
            name = entry.get("display_name") or entry.get("alias") or sid[:8]
            ok = deploy_wallpaper(manager, sid, int(num), name)
            results[sid] = ok
        return results

    sorted_devices = sorted(device_configs.items(),
                            key=lambda kv: kv[1].get("display_name", ""))
    for i, (serial, cfg) in enumerate(sorted_devices, start=1):
        name = cfg.get("display_name", f"Phone-{i}")
        res = cfg.get("resolution", {})
        w = res.get("width", 720)
        h = res.get("height", 1600)
        ok = deploy_wallpaper(manager, serial, i, name, w, h)
        results[serial] = ok
    return results


def deploy_wallpapers_parallel(manager, devices_with_numbers: list,
                                max_workers: int = 4) -> dict:
    """并行部署壁纸到多台设备。

    Args:
        manager: DeviceManager instance
        devices_with_numbers: [(device_id, number, display_name), ...]
        max_workers: 并行线程数（默认 4；含 MIUI 自动化时并发过高易抢焦点）

    Returns:
        dict: {device_id: True/False}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    if not devices_with_numbers:
        return {}

    # Step 1: 先批量生成所有壁纸图片（快速，无需并行限制）
    log.info("[壁纸] 开始并行部署 %d 台设备, %d 线程",
             len(devices_with_numbers), max_workers)
    t0 = time.time()

    # 预生成所有壁纸
    for _, num, dname in devices_with_numbers:
        try:
            generate_wallpaper(num, dname)
        except Exception as e:
            log.warning("[壁纸] 生成 #%02d 失败: %s", num, e)

    # Step 2: 并行推送+设置
    results = {}

    def _deploy_one(item):
        did, num, dname = item
        try:
            ok = deploy_wallpaper(manager, did, num, display_name=dname)
            return did, ok
        except Exception as e:
            log.error("[壁纸] 部署 #%02d (%s) 异常: %s", num, did[:8], e)
            return did, False

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_deploy_one, item): item
                   for item in devices_with_numbers}
        for future in as_completed(futures):
            did, ok = future.result()
            results[did] = ok

    elapsed = time.time() - t0
    success = sum(1 for v in results.values() if v)
    log.info("[壁纸] 并行部署完成: %d/%d 成功, 耗时 %.1fs",
             success, len(results), elapsed)
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  WallpaperAutoManager — fully automatic numbering & deployment
# ═══════════════════════════════════════════════════════════════════════════

class WallpaperAutoManager:
    """
    Manages automatic device numbering and wallpaper deployment.

    Uses DeviceRegistry (fingerprint-based) as the source of truth for
    numbering. device_aliases.json is kept in sync as a secondary store
    (keyed by current ADB serial for frontend compatibility).
    """

    def __init__(self, config_root: str | Path):
        self._config_root = Path(config_root)
        self._aliases_path = self._config_root / "config" / "device_aliases.json"
        self._lock = threading.Lock()
        self._deployed: set[str] = set()
        self._startup_running = False

    # ── Persistence ──────────────────────────────────────────────────────

    def _load_aliases(self) -> dict:
        if self._aliases_path.exists():
            try:
                with open(self._aliases_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_aliases(self, data: dict):
        self._aliases_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._aliases_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_registry(self):
        from src.device_control.device_registry import get_device_registry
        return get_device_registry(self._config_root)

    # ── Public API ───────────────────────────────────────────────────────

    def ensure_all_numbered(self, manager) -> dict:
        """
        Fingerprint-based numbering for all known devices.
        1. Collect fingerprints for online devices
        2. Look up registry for existing numbers
        3. Assign new numbers for unknown devices
        4. Sync aliases.json and registry
        5. Deploy wallpapers
        """
        self._startup_running = True
        try:
            registry = self._get_registry()
            from src.device_control.device_manager import DeviceStatus

            # Bootstrap: import existing aliases into registry on first run
            registry.bootstrap_from_aliases(self._aliases_path, manager)

            all_devices = manager.get_all_devices()
            from src.host.device_alias_labels import (
                load_local_cluster_identity,
                apply_slot_and_labels,
                used_slots_resolved,
            )
            chid, chname = load_local_cluster_identity()
            local_ids = {d.device_id for d in all_devices}

            with self._lock:
                aliases = self._load_aliases()
                changed = False

                for dev in sorted(all_devices,
                                  key=lambda d: d.display_name or d.device_id):
                    did = dev.device_id
                    fp = dev.fingerprint

                    if fp:
                        reg_entry = registry.lookup(fp)
                        placeholder_key = f"serial:{did}"
                        placeholder_entry = None
                        if not reg_entry:
                            all_reg = registry.get_all()
                            placeholder_entry = all_reg.get(placeholder_key)

                        if reg_entry and reg_entry.get("number"):
                            num = reg_entry["number"]
                            old_serial = reg_entry.get("current_serial", "")
                            if old_serial != did:
                                registry.update_serial(fp, did)
                                log.info("[壁纸自动] 指纹匹配 %s: "
                                         "旧串号 %s → 新串号 %s, 编号 #%02d",
                                         fp[:12], old_serial[:8], did[:8], num)
                        elif placeholder_entry and placeholder_entry.get("number"):
                            num = placeholder_entry["number"]
                            _lbl = apply_slot_and_labels({}, num, chid, chname or "")
                            registry.register(
                                fp, did, num, _lbl["alias"],
                                imei=dev.imei, hw_serial=dev.hw_serial,
                                android_id=dev.android_id, model=dev.model,
                            )
                            with registry._lock:
                                reg_data = registry._load()
                                reg_data.pop(placeholder_key, None)
                                registry._save(reg_data)
                            log.info("[壁纸自动] 升级指纹 %s: serial:%s → fp=%s, 编号 #%02d",
                                     did[:8], did[:8], fp[:12], num)
                        else:
                            used_now = used_slots_resolved(
                                aliases, chid, local_device_ids=local_ids)
                            num = 1
                            while num in used_now:
                                num += 1
                            _lbl = apply_slot_and_labels({}, num, chid, chname or "")
                            registry.register(
                                fp, did, num, _lbl["alias"],
                                imei=dev.imei, hw_serial=dev.hw_serial,
                                android_id=dev.android_id, model=dev.model,
                            )
                            log.info("[壁纸自动] 新设备 %s (fp=%s) → 槽位 #%02d (%s)",
                                     did[:8], fp[:12], num, _lbl.get("display_label", ""))
                    else:
                        entry = aliases.get(did)
                        if entry and entry.get("number"):
                            num = entry["number"]
                        else:
                            used_now = used_slots_resolved(
                                aliases, chid, local_device_ids=local_ids)
                            num = 1
                            while num in used_now:
                                num += 1
                            log.info("[壁纸自动] 无指纹设备 %s → 编号 #%02d",
                                     did[:8], num)

                    aliases[did] = apply_slot_and_labels(
                        {
                            "display_name": dev.display_name or f"Phone-{num}",
                        },
                        num,
                        chid,
                        chname or "",
                    )
                    changed = True

                if changed:
                    self._save_aliases(aliases)

            # Deploy wallpapers (outside lock)
            results = {}
            for dev in all_devices:
                did = dev.device_id
                num = aliases.get(did, {}).get("number", 0)
                is_online = dev.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)
                deployed = False
                if is_online and num > 0:
                    deployed = deploy_wallpaper(
                        manager, did, num,
                        display_name=dev.display_name or f"Phone-{num}",
                    )
                    if deployed:
                        self._deployed.add(did)
                results[did] = {"number": num, "deployed": deployed}

            total = len(results)
            deployed_count = sum(1 for v in results.values() if v["deployed"])
            log.info("[壁纸自动] 编号完成: %d 台设备, %d 台已部署壁纸",
                     total, deployed_count)
            return results
        finally:
            self._startup_running = False

    def on_device_online(self, manager, device_id: str):
        """
        Called when a device comes online.
        Uses fingerprint to look up existing number; assigns new if needed.
        """
        if self._startup_running:
            log.debug("[壁纸自动] 跳过 %s — 启动编号进行中", device_id[:8])
            return

        if device_id in self._deployed:
            return

        info = manager.get_device_info(device_id)
        if not info:
            return
        display_name = info.display_name or ""
        fp = info.fingerprint

        registry = self._get_registry()
        from src.host.device_alias_labels import (
            load_local_cluster_identity,
            apply_slot_and_labels,
            used_slots_resolved,
        )
        chid, chname = load_local_cluster_identity()

        with self._lock:
            aliases = self._load_aliases()
            try:
                from src.device_control.device_manager import get_device_manager
                _mgr = get_device_manager(str(self._config_root / "config" / "devices.yaml"))
                local_ids = {d.device_id for d in _mgr.get_all_devices()}
            except Exception:
                local_ids = {device_id}

            if fp:
                reg_entry = registry.lookup(fp)
                if reg_entry and reg_entry.get("number"):
                    num = reg_entry["number"]
                    old_serial = reg_entry.get("current_serial", "")
                    if old_serial != device_id:
                        registry.update_serial(fp, device_id)
                        registry.migrate_serial(
                            old_serial, device_id, self._config_root)
                        aliases = self._load_aliases()
                        log.info("[壁纸自动] 设备指纹匹配 %s → #%02d (旧=%s)",
                                 device_id[:8], num, old_serial[:8])
                else:
                    used_now = used_slots_resolved(
                        aliases, chid, local_device_ids=local_ids)
                    num = 1
                    while num in used_now:
                        num += 1
                    _lbl = apply_slot_and_labels({}, num, chid, chname or "")
                    registry.register(
                        fp, device_id, num, _lbl["alias"],
                        imei=info.imei, hw_serial=info.hw_serial,
                        android_id=info.android_id, model=info.model,
                    )
                    log.info("[壁纸自动] 新设备上线 %s (fp=%s) → 槽位 #%02d",
                             device_id[:8], fp[:12], num)
            else:
                entry = aliases.get(device_id)
                if entry and entry.get("number"):
                    num = entry["number"]
                else:
                    used_now = used_slots_resolved(
                        aliases, chid, local_device_ids=local_ids)
                    num = 1
                    while num in used_now:
                        num += 1
                    log.info("[壁纸自动] 新设备上线(无指纹) %s → 编号 #%02d",
                             device_id[:8], num)

            aliases[device_id] = apply_slot_and_labels(
                {"display_name": display_name or f"Phone-{num}"},
                num,
                chid,
                chname or "",
            )
            self._save_aliases(aliases)

        deployed = deploy_wallpaper(
            manager, device_id, num,
            display_name=display_name or f"Phone-{num}",
        )
        if deployed:
            self._deployed.add(device_id)
            log.info("[壁纸自动] 设备 %s 壁纸已自动部署 (#%02d)",
                     device_id[:8], num)
        else:
            log.warning("[壁纸自动] 设备 %s 壁纸部署失败", device_id[:8])


# ── Singleton ────────────────────────────────────────────────────────────

_auto_manager: Optional[WallpaperAutoManager] = None
_auto_manager_lock = threading.Lock()


def get_wallpaper_auto_manager(config_root: str | Path = "") -> WallpaperAutoManager:
    """Get or create the singleton WallpaperAutoManager."""
    global _auto_manager
    if _auto_manager is None:
        with _auto_manager_lock:
            if _auto_manager is None:
                if not config_root:
                    config_root = PROJECT_ROOT
                _auto_manager = WallpaperAutoManager(config_root)
    return _auto_manager
