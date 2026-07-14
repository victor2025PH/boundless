# -*- coding: utf-8 -*-
"""
W0-2/W0-3: 直接ADB方式抓取30个日本女性FB profile截图+分类

绕过 AutoSelector 缓存，直接用 adb shell input tap/text/keyevent 操作，
避免 smart_tap 坐标缓存问题，保证 People 过滤器正确点击。

用法:
  python scripts/w0_capture_direct.py --device 8DWOF6CYY5R8YHX8 --target 30
"""

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ADB = r"C:\platform-tools\adb.exe"

# 日本女性名字列表（罗马字）
JP_FEMALE_NAMES = [
    "Yumi Tanaka", "Keiko Suzuki", "Hanako Yamada", "Noriko Sato",
    "Michiko Nakamura", "Yoko Ito", "Kazuko Kobayashi", "Fumiko Kato",
    "Hiroko Yoshida", "Reiko Yamamoto", "Sachiko Watanabe", "Tomoko Abe",
    "Kimiko Ikeda", "Ayako Hayashi", "Yoshiko Shimizu", "Masako Yamashita",
    "Chieko Matsumoto", "Hisako Ogawa", "Nobuko Inoue", "Teruko Kimura",
    "Ryoko Fujii", "Chizuko Hayashi", "Setsuko Taniguchi", "Naoko Ueda",
    "Mieko Ishikawa", "Kyoko Nishimura", "Sumiko Goto", "Haruko Mori",
    "Etsuko Saito", "Mineko Yamamoto",
]


def adb(serial: str, *args, timeout: int = 10) -> str:
    cmd = [ADB, "-s", serial] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return str(e)


def adb_tap(serial: str, x: int, y: int):
    adb(serial, "shell", "input", "tap", str(x), str(y))


def adb_swipe(serial: str, x1: int, y1: int, x2: int, y2: int, dur_ms: int = 400):
    adb(serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur_ms))


def adb_text(serial: str, text: str):
    """输入文字（仅ASCII，用%s替代空格）"""
    encoded = text.replace(" ", "%s")
    adb(serial, "shell", "input", "text", encoded)


def adb_keyevent(serial: str, keycode: int):
    adb(serial, "shell", "input", "keyevent", str(keycode))


def adb_back(serial: str):
    adb_keyevent(serial, 4)  # KEYCODE_BACK


def adb_screenshot(serial: str, save_path: str) -> bool:
    tmp = f"/sdcard/w0_tmp_cap_{int(time.time())}.png"
    out_dir = os.path.dirname(save_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    # 先截图到设备
    r1 = subprocess.run(
        [ADB, "-s", serial, "shell", "screencap", "-p", tmp],
        capture_output=True, timeout=15
    )
    if r1.returncode != 0:
        return False
    # 拉取到电脑
    r2 = subprocess.run(
        [ADB, "-s", serial, "pull", tmp, save_path],
        capture_output=True, timeout=30
    )
    # 清理临时文件
    subprocess.run([ADB, "-s", serial, "shell", "rm", "-f", tmp],
                   capture_output=True, timeout=5)
    return r2.returncode == 0 and os.path.exists(save_path) and os.path.getsize(save_path) > 1000


def get_xml(serial: str) -> str:
    """获取当前界面XML层次结构"""
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)
        return d.dump_hierarchy()
    except Exception:
        return ""


def is_on_report_dialog(xml: str) -> bool:
    """检测是否在 Report（举报）对话框页面"""
    lower = xml.lower()
    return any(kw in lower for kw in [
        "what do you want to report?",
        "if someone is in immediate danger",
        "report this profile",
        "why are you reporting this?",
    ])


def is_on_profile_page(xml: str) -> bool:
    """检测是否在用户主页（有 Add Friend / Message / Follow 按钮）"""
    lower = xml.lower()
    # Must have action buttons but NOT be on a report dialog
    if is_on_report_dialog(xml):
        return False
    profile_signals = ["add friend", "message", "follow"]
    return any(sig in lower for sig in profile_signals)


def search_and_navigate(serial: str, name: str, search_coord=(580, 112),
                        people_tab_coord=(332, 204)) -> dict:
    """
    直接用 ADB 搜索姓名并点击第一个搜索结果进入 profile。
    返回 {"ok": bool, "display_name": str} 字典。
    """
    import uiautomator2 as u2

    # 打开 FB 首页
    adb(serial, "shell", "am", "start", "-n",
        "com.facebook.katana/.LoginActivity")
    time.sleep(2)

    # 点击搜索栏
    adb_tap(serial, search_coord[0], search_coord[1])
    time.sleep(2)

    # 输入搜索词
    adb_text(serial, name)
    time.sleep(1.5)

    # 按回车搜索
    adb_keyevent(serial, 66)
    time.sleep(3)

    # 截图确认搜索结果页
    xml_after_search = get_xml(serial)

    # 检查是否已经直接进入了 profile 页
    # Facebook 搜索某人名后按 Enter，有时直接打开第一匹配者的 profile
    try:
        d = u2.connect(serial)
        xml_after_search = d.dump_hierarchy()
    except Exception:
        xml_after_search = ""

    if xml_after_search and is_on_profile_page(xml_after_search):
        log.info("    搜索后直接进入 profile 页 ✓")
        return {"ok": True, "display_name": name}

    if xml_after_search and is_on_report_dialog(xml_after_search):
        log.warning("    搜索后出现 Report 对话框，返回")
        adb_back(serial)
        time.sleep(1)
        return {"ok": False, "display_name": ""}

    log.info("    在搜索结果页，尝试点击 People 过滤器 + 第一条结果")

    # 先尝试通过 u2 selector 找 People filter tab
    people_tapped = False
    try:
        people_selectors = [
            {"descriptionContains": "People search results"},
            {"text": "People"},
            {"descriptionContains": "People"},
        ]
        for sel in people_selectors:
            el = d(**sel)
            if el.exists(timeout=2):
                el.click()
                people_tapped = True
                log.info("    [People filter] selector %s 点击成功", sel)
                break
    except Exception as e:
        log.debug("    u2 People filter selector 失败: %s", e)

    if not people_tapped:
        log.info("    [People filter] 使用坐标 %s", people_tab_coord)
        adb_tap(serial, people_tab_coord[0], people_tab_coord[1])

    time.sleep(2)

    # 重新获取 XML
    try:
        xml = d.dump_hierarchy()
    except Exception:
        xml = ""

    # 检查是否已在 profile 页（过滤器有时直接跳转）
    if xml and is_on_profile_page(xml):
        log.info("    点击过滤器后直接进入 profile 页 ✓")
        return {"ok": True, "display_name": name}

    if not xml:
        log.warning("    XML 为空")
        return {"ok": False, "display_name": ""}

    # 在搜索结果中找第一个人物卡片
    from src.vision.screen_parser import XMLParser
    elements = XMLParser.parse(xml)

    _excluded = {
        "add friend", "add\xa0friend", "see all", "back",
        "filter all", "clear text", "more options",
        "all search results", "reels search results",
        "people search results", "groups search results",
        "events search results",
    }

    first_card = None
    card_display_name = ""
    for el in elements:
        if not el.clickable:
            continue
        cd = (getattr(el, "content_desc", "") or "").strip()
        t = (el.text or "").strip()
        display = cd or t
        if len(display) < 2:
            continue
        if display.lower() in _excluded:
            continue
        if "more options" in display.lower():
            continue
        b = el.bounds
        if not b:
            continue
        w = b[2] - b[0]
        h = b[3] - b[1]
        if h < 80 or w < 400:
            continue
        cls = (el.class_name or "")
        if "Button" in cls or "ViewGroup" in cls or "FrameLayout" in cls:
            first_card = el
            # 从 display 提取纯名字（去掉后面的 "," 或 "(" 附加信息）
            card_display_name = display.split(",")[0].split("(")[0].split("·")[0].strip()
            log.info("    找到第一个人物卡片: %r name=%r (cls=%s bounds=%s)",
                     display[:40], card_display_name, cls, b)
            break

    if first_card is None:
        log.warning("    未找到人物卡片元素，XML 元素数: %d", len(elements))
        return {"ok": False, "display_name": ""}

    # 点击人物卡片中心
    b = first_card.bounds
    cx = (b[0] + b[2]) // 2
    cy = (b[1] + b[3]) // 2
    adb_tap(serial, cx, cy)
    time.sleep(3)

    # 验证是否进入了 profile 页
    xml_after = get_xml(serial)
    if is_on_report_dialog(xml_after):
        log.warning("    进入了 Report 对话框，返回")
        adb_back(serial)
        time.sleep(1)
        return {"ok": False, "display_name": ""}

    resolved_name = card_display_name or name
    if is_on_profile_page(xml_after):
        log.info("    点击人物卡片后进入 profile 页 ✓  name=%r", resolved_name)
        return {"ok": True, "display_name": resolved_name}

    log.warning("    未能确认进入 profile 页，继续尝试")
    return {"ok": True, "display_name": resolved_name}


def capture_screenshots(serial: str, save_dir: str, count: int = 3,
                        tag: str = "profile") -> list:
    """对当前页面连续截图，返回文件路径列表"""
    os.makedirs(save_dir, exist_ok=True)
    paths = []
    for i in range(count):
        p = os.path.join(save_dir, f"{tag}_{i+1}.png")
        if adb_screenshot(serial, p):
            paths.append(p)
            log.debug("    截图保存: %s", p)
        else:
            log.warning("    截图失败: %s", p)
        # 在截图间轻微滚动（不是最后一张）
        if i < count - 1:
            # 向上滚动（即向下看内容）
            jitter_x = random.randint(300, 420)
            adb_swipe(serial, jitter_x, 1100, jitter_x, 600, 400)
            time.sleep(1.5)
    return paths


def extract_profile_info_from_xml(serial: str) -> dict:
    """从当前 XML 提取 display_name 和 bio 文本（在滚动前调用）"""
    xml = get_xml(serial)
    if not xml or is_on_report_dialog(xml):
        return {"display_name": "", "bio": ""}

    try:
        from src.vision.screen_parser import XMLParser
        import re
        elements = XMLParser.parse(xml)

        clock_pat = re.compile(r'^[\u300c\u300d]?\d{1,2}:\d{2}[\u300c\u300d]?$')
        date_pat = re.compile(r'^\d{1,2}\s+\w+\s+\d{4}')  # "29 May 2022..."
        skip = {"Home", "Friends", "Marketplace", "Search", "Like",
                "Comment", "Share", "Follow", "Message", "Add Friend",
                "Followers", "Following", "About", "Photos", "More",
                "See All", "See more", "Report", "100", "All", "Reels",
                "Add friend", "Message", "Following"}

        name = ""
        bio_parts = []

        # 优先从 profile header 区域（y > 68 and y < 800）找名字
        # profile 名字通常是独立的 text 或 content_desc，不含"friends/posts"分隔符
        header_elements = [el for el in elements
                           if el.bounds and el.bounds[1] >= 68 and el.bounds[3] < 800]

        for el in header_elements:
            t = (el.text or "").strip()
            cd = (getattr(el, "content_desc", "") or "").strip()
            # 优先用 text（FB profile 名字通常在 text 里，content_desc 可能含通知等）
            for val in [t, cd]:
                if not val or len(val) < 2 or len(val) > 60:
                    continue
                if clock_pat.match(val):
                    continue
                if date_pat.match(val):
                    continue
                if val.isdigit():
                    continue
                val_lower = val.lower()
                if val_lower in {s.lower() for s in skip}:
                    continue
                # 排除通知类文字
                if "notification:" in val_lower or "notif" in val_lower:
                    continue
                # 排除 profile picture, photo, cover 等
                if any(kw in val_lower for kw in ["profile picture", "cover photo",
                                                   "phone signal", "battery",
                                                   "android system"]):
                    continue
                # 排除包含 "·" 或 "|" 的（通常是组合描述，不是纯名字）
                # 但保留包含日文字符的（日文名字可能没有ASCII分隔符）
                has_ascii_only = all(ord(c) < 128 for c in val)
                if has_ascii_only and ("·" in val or " | " in val or "," in val):
                    continue
                # 检查是否像人名（首字母大写 + 空格 + 首字母大写，或者包含日文）
                parts = val.split()
                # 排除只有一个词的 ASCII 字符串（通常不是人名，除非是单名日文）
                if has_ascii_only and len(parts) < 2:
                    continue
                looks_like_name = (
                    len(parts) >= 2 and all(p[0].isupper() for p in parts if p)
                ) or any(ord(c) > 0xFF for c in val)  # 含非ASCII（日文等）
                if looks_like_name and not name:
                    name = val
                    break
            if name:
                break

        # 收集 bio 文本（头部区域 y < 600，排除状态栏和时钟）
        for el in elements:
            t = (el.text or "").strip()
            if not t or clock_pat.match(t) or t.isdigit():
                continue
            b = el.bounds
            if b and b[1] < 68:  # 状态栏
                continue
            bio_parts.append(t)

        bio = " | ".join(bio_parts)[:300]
        return {"display_name": name, "bio": bio}
    except Exception as e:
        log.debug("extract_profile_info_from_xml 失败: %s", e)
        return {"display_name": "", "bio": ""}


def run_vlm_classify(image_paths: list, display_name: str, bio: str,
                     persona_key: str = "jp_female_midlife",
                     target_key: str = "", task_id: str = "",
                     device_id: str = "") -> dict:
    """运行 L1+L2 分类，返回分类结果"""
    try:
        from src.host.fb_profile_classifier import classify
        from src.host.fb_target_personas import get_persona
        from src.host.ollama_vlm import warmup

        # 确保 VLM 已预热
        try:
            warmup(force=False)
        except Exception:
            pass

        result = classify(
            device_id=device_id,
            task_id=task_id,
            persona_key=persona_key,
            target_key=target_key,
            display_name=display_name,
            bio=bio,
            username="",
            locale="ja",
            image_paths=image_paths,
            l2_image_paths=image_paths,
            do_l2=True,
            dry_run=False,
        )
        return result
    except Exception as e:
        log.warning("VLM 分类失败: %s", e)
        return {"match": False, "score": 0.0, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="W0 直接ADB Profile 抓取")
    parser.add_argument("--device", default="8DWOF6CYY5R8YHX8")
    parser.add_argument("--target", type=int, default=30)
    parser.add_argument("--persona", default="jp_female_midlife")
    parser.add_argument("--skip-l2", action="store_true", help="跳过 L2 VLM 分类")
    parser.add_argument("--out", default="data/w0_jp_ground_truth_v2.json")
    parser.add_argument("--shot-dir", default="data/fb_profile_shots_v2")
    args = parser.parse_args()

    serial = args.device
    log.info("=== W0 直接ADB Profile 抓取 ===")
    log.info("设备: %s  目标: %d  Persona: %s", serial, args.target, args.persona)

    # 确认设备在线
    devices_out = adb(serial, "devices")
    if serial not in devices_out:
        log.error("设备 %s 不在线！退出", serial)
        sys.exit(1)

    os.makedirs(args.shot_dir, exist_ok=True)

    captured = []
    stats = {
        "total_searched": 0,
        "nav_ok": 0,
        "nav_fail": 0,
        "report_dialog": 0,
        "l1_pass": 0,
        "l1_fail": 0,
        "l2_run": 0,
        "l2_match": 0,
    }

    names_to_try = list(JP_FEMALE_NAMES)
    random.shuffle(names_to_try)

    for name in names_to_try:
        if len(captured) >= args.target:
            break

        stats["total_searched"] += 1
        log.info("\n=== 搜索: 「%s」 [%d/%d] ===",
                 name, len(captured), args.target)

        # 随机等待
        time.sleep(random.uniform(5, 10))

        # 搜索并导航到 profile
        nav_result = search_and_navigate(serial, name)
        if not nav_result.get("ok"):
            stats["nav_fail"] += 1
            log.warning("  导航失败，跳过")
            # 返回首页
            adb(serial, "shell", "am", "start", "-n",
                "com.facebook.katana/.LoginActivity")
            time.sleep(2)
            continue

        stats["nav_ok"] += 1
        # 使用从搜索结果卡片提取的名字，比 XML 解析更可靠
        resolved_name = nav_result.get("display_name") or name
        log.info("  导航成功  resolved_name=%r", resolved_name)

        # 提取 profile 文本（在滚动之前！）
        info = extract_profile_info_from_xml(serial)
        # 如果 XML 提取到了更好的名字，优先用它；否则用 nav 返回的名字
        xml_name = info.get("display_name", "")
        display_name = xml_name if xml_name and xml_name != name else resolved_name
        bio = info.get("bio", "")

        # 截图
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_dir = os.path.join(args.shot_dir, f"{ts_str}_{name[:10].replace(' ', '_')}")
        image_paths = capture_screenshots(serial, shot_dir, count=3)

        if not image_paths:
            log.warning("  截图全部失败，跳过")
            adb_back(serial)
            time.sleep(1)
            continue

        log.info("  截图 %d 张  display_name: 「%s」", len(image_paths), display_name)
        log.info("  bio[:80]: 「%s」", bio[:80])

        # 分类
        target_key = f"search:{name}"
        clf_result = {}
        if not args.skip_l2:
            log.info("  运行 L1+L2 分类...")
            clf_result = run_vlm_classify(
                image_paths=image_paths,
                display_name=display_name,
                bio=bio,
                persona_key=args.persona,
                target_key=target_key,
                task_id=f"w0v2_{len(captured)+1:03d}",
                device_id=serial,
            )

        l1_pass = (clf_result.get("l1") or {}).get("pass", False)
        match = bool(clf_result.get("match"))
        score = float(clf_result.get("score") or 0)

        if l1_pass:
            stats["l1_pass"] += 1
        else:
            stats["l1_fail"] += 1

        if clf_result.get("stage_reached") == "L2":
            stats["l2_run"] += 1
            if match:
                stats["l2_match"] += 1

        result_icon = "✅" if match else "❌"
        log.info("  %s L1=%s L2_match=%s score=%.1f",
                 result_icon, l1_pass, match, score)

        # 保存结果
        profile_record = {
            "seq": len(captured) + 1,
            "search_name": name,
            "display_name": display_name,
            "bio": bio[:200],
            "target_key": target_key,
            "image_paths": image_paths,
            "shot_dir": shot_dir,
            "l1_pass": l1_pass,
            "match": match,
            "score": score,
            "stage": clf_result.get("stage_reached", ""),
            "captured_at": datetime.now().isoformat(),
        }
        captured.append(profile_record)

        # 保存进度
        progress = {
            "w0_version": "2.0",
            "device": serial,
            "persona_key": args.persona,
            "target_count": args.target,
            "actual_count": len(captured),
            "stats": stats,
            "profiles": captured,
            "saved_at": datetime.now().isoformat(),
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        log.info("  [%d/%d] 已记录，进度保存至 %s", len(captured), args.target, args.out)

        # 返回首页准备下一个搜索
        adb(serial, "shell", "am", "start", "-n",
            "com.facebook.katana/.LoginActivity")
        time.sleep(random.uniform(8, 15))

    log.info("\n=== W0 完成 ===")
    log.info("抓取 %d/%d 个 profile", len(captured), args.target)
    log.info("统计: %s", stats)
    log.info("结果保存至: %s", args.out)


if __name__ == "__main__":
    main()
