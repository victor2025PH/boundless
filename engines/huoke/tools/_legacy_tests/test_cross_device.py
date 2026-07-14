#!/usr/bin/env python3
"""
跨设备 + 跨账号 Telegram 互聊测试 v5。
修复: 侧边栏已展开检测 + Phone2 直接搜索框
"""
import builtins
import time
import uiautomator2 as u2
from lxml import etree
import re

_p = builtins.print
def print(*a, **k):
    k["flush"] = True
    _p(*a, **k)

DEV1 = "89NZVGKFD6BYUO5P"
DEV2 = "R8CIFUBIOVCIUW5H"
bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')
results = []

P1_ACCOUNTS = [
    {"name": "Carlin",      "username": "@wardzmick"},
    {"name": "Vivian",      "username": "@dthb3"},
    {"name": "Chaya Chaya", "username": "@fordbrend"},
]

def report(step, desc, ok):
    results.append((step, desc, ok))
    print(f"  [{'✓' if ok else '✗'}] {step}: {desc} → {'PASS' if ok else 'FAIL'}")

def bc(bs):
    m = bounds_re.match(bs)
    return ((int(m.group(1))+int(m.group(3)))//2, (int(m.group(2))+int(m.group(4)))//2) if m else (None, None)

def fresh_start(d):
    d.app_stop("org.telegram.messenger")
    time.sleep(1)
    d.app_start("org.telegram.messenger")
    time.sleep(5)
    for t in ["Cancel", "取消", "OK", "NOT NOW"]:
        if d(text=t).exists(timeout=2):
            d(text=t).click()
            time.sleep(1)
            break
    for _ in range(5):
        xml = d.dump_hierarchy()
        if "Open navigation menu" in xml or "Search" in xml or "搜索" in xml:
            return
        d.press("back")
        time.sleep(1)

def find_in_xml(xml, text_match, y_min=0, y_max=9999):
    root = etree.fromstring(xml.encode("utf-8"))
    hits = []
    for el in root.iter():
        if el.get("text", "") == text_match:
            cx, cy = bc(el.get("bounds", ""))
            if cx and y_min < cy < y_max:
                hits.append((cx, cy))
    return hits

def switch_account_phone1(d, target_name):
    """侧边栏切换: 处理已展开和未展开两种状态"""
    fresh_start(d)

    # 打开侧边栏
    for desc in ["Open navigation menu", "打开导航菜单"]:
        btn = d(description=desc)
        if btn.exists(timeout=3):
            btn.click()
            time.sleep(2)
            break
    else:
        d.swipe(20, 400, 500, 400, duration=0.3)
        time.sleep(2)

    xml = d.dump_hierarchy()

    # 检查是否已经展开 (Hide accounts 存在) 或需要展开 (Show accounts 存在)
    already_expanded = "Hide accounts" in xml
    needs_expand = "Show accounts" in xml

    if not already_expanded and needs_expand:
        root = etree.fromstring(xml.encode("utf-8"))
        for el in root.iter():
            if el.get("content-desc", "") == "Show accounts":
                cx, cy = bc(el.get("bounds", ""))
                if cx:
                    d.click(cx, cy)
                    time.sleep(2)
                    xml = d.dump_hierarchy()
                    already_expanded = True
                    break

    if not already_expanded:
        # 最后尝试: 直接点击电话号码旁边的展开区域
        hits = find_in_xml(xml, "+", y_min=250, y_max=400)
        if not hits:
            # 找任何 + 开头的电话号码
            root = etree.fromstring(xml.encode("utf-8"))
            for el in root.iter():
                text = el.get("text", "")
                if text.startswith("+63"):
                    cx, cy = bc(el.get("bounds", ""))
                    if cx and 250 < cy < 400:
                        # 点箭头区域（电话号码右侧）
                        d.click(400, cy)
                        time.sleep(2)
                        xml = d.dump_hierarchy()
                        already_expanded = "Hide accounts" in xml
                        break

    if not already_expanded:
        print(f"    无法展开账号列表")
        d.press("back")
        return False

    # 查找目标账号 (y > 370 区域)
    hits = find_in_xml(xml, target_name, y_min=370, y_max=700)
    if hits:
        cx, cy = hits[0]
        print(f"    点击 {target_name} at ({cx}, {cy})")
        d.click(cx, cy)
        time.sleep(5)
        return True

    # 检查是否已经是目标账号（顶部 y<370）
    hits_top = find_in_xml(xml, target_name, y_min=200, y_max=370)
    if hits_top:
        print(f"    已经是 {target_name}")
        d.press("back")
        time.sleep(1)
        return True

    # 调试: 列出展开后的所有文本
    root = etree.fromstring(xml.encode("utf-8"))
    print(f"    未找到 {target_name}，展开区域内容:")
    for el in root.iter():
        text = el.get("text", "")
        cx, cy = bc(el.get("bounds", ""))
        if text and cx and 350 < cy < 700 and len(text) < 40:
            rid = el.get("resource-id", "")
            if "systemui" not in rid:
                print(f"      y={cy}: \"{text}\"")

    d.press("back")
    return False

def send_message_any(d, target_username, message):
    """搜索用户 → 发消息。兼容 Phone1(搜索按钮) 和 Phone2(搜索框)"""
    fresh_start(d)

    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode("utf-8"))

    # 方案 A: 有搜索按钮 (Phone1 style)
    search_clicked = False
    for el in root.iter():
        desc = el.get("content-desc", "")
        cls = el.get("class", "")
        if desc in ("Search", "搜索"):
            cx, cy = bc(el.get("bounds", ""))
            if cx:
                d.click(cx, cy)
                search_clicked = True
                time.sleep(1.5)
                break

    # 方案 B: 有搜索框 (Phone2 style — "Search Chats" EditText)
    if not search_clicked:
        for el in root.iter():
            text = el.get("text", "")
            cls = el.get("class", "")
            if ("Search" in text or "搜索" in text) and "EditText" in cls:
                cx, cy = bc(el.get("bounds", ""))
                if cx:
                    d.click(cx, cy)
                    search_clicked = True
                    time.sleep(1.5)
                    break

    if not search_clicked:
        print(f"    未找到搜索入口")
        return False

    # 输入搜索文本
    edit = d(className="android.widget.EditText", packageName="org.telegram.messenger")
    if not edit.exists(timeout=5):
        print(f"    搜索框未出现")
        d.press("back")
        return False
    edit.set_text(target_username)
    time.sleep(3)

    # 找搜索结果
    clean = target_username.lstrip("@").lower()
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode("utf-8"))
    clicked = False

    for node in root.iter():
        if "telegram" not in node.get("package", ""):
            continue
        text = node.get("text", "")
        cls = node.get("class", "")
        if "ViewGroup" in cls and text and f"@{clean}" in text.lower():
            cx, cy = bc(node.get("bounds", ""))
            if cx and cy > 150:
                d.click(cx, cy)
                clicked = True
                break

    if not clicked:
        for node in root.iter():
            if "telegram" not in node.get("package", ""):
                continue
            text = node.get("text", "")
            cls = node.get("class", "")
            if "ViewGroup" in cls and text and len(text) > 3 and "Global" not in text:
                if clean in text.lower().replace(" ", ""):
                    cx, cy = bc(node.get("bounds", ""))
                    if cx and cy > 200:
                        d.click(cx, cy)
                        clicked = True
                        break

    if not clicked:
        print(f"    搜索 '{target_username}' 无结果")
        d.press("back")
        return False

    time.sleep(3)

    # 输入消息
    msg_input = d(className="android.widget.EditText", packageName="org.telegram.messenger")
    if not msg_input.exists(timeout=5):
        print(f"    未找到消息输入框")
        d.press("back")
        return False
    msg_input.set_text(message)
    time.sleep(1)

    # 发送
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode("utf-8"))
    for el in root.iter():
        desc = el.get("content-desc", "")
        if desc in ("Send", "发送"):
            m = bounds_re.match(el.get("bounds", ""))
            if m:
                x = int(m.group(3)) - 40
                y = (int(m.group(2)) + int(m.group(4))) // 2
                d.click(x, y)
                time.sleep(2)
                return True

    print(f"    未找到发送按钮")
    d.press("back")
    return False


# ═══════════════════════════════════════════
print("连接设备...")
d1 = u2.connect(DEV1)
d2 = u2.connect(DEV2)
print(f"  Phone 1: {DEV1[:8]}")
print(f"  Phone 2: {DEV2[:8]}")

print(f"\n{'='*60}")
print("[Step 0] 初始化")
print(f"{'='*60}")
fresh_start(d1)
fresh_start(d2)
print("  OK")

for i, account in enumerate(P1_ACCOUNTS):
    step = i + 1
    acct_name = account["name"]
    acct_user = account["username"]

    print(f"\n{'='*60}")
    print(f"[Step {step}] Phone1({acct_name}/{acct_user}) ↔ Phone2(Vyanka/@vyanks)")
    print(f"{'='*60}")

    print(f"\n  [切换] Phone1 → {acct_name}")
    ok = switch_account_phone1(d1, acct_name)
    report(f"{step}a", f"切换到 {acct_name}", ok)
    if not ok:
        print("  跳过")
        continue

    msg1 = f"From {acct_name} #{step} {time.strftime('%H:%M:%S')}"
    print(f"\n  Phone1({acct_name}) → @vyanks: \"{msg1}\"")
    ok1 = send_message_any(d1, "@vyanks", msg1)
    report(f"{step}b", f"{acct_name}→Vyanka", ok1)

    msg2 = f"Reply to {acct_name} #{step} {time.strftime('%H:%M:%S')}"
    print(f"  Phone2(Vyanka) → {acct_user}: \"{msg2}\"")
    ok2 = send_message_any(d2, acct_user, msg2)
    report(f"{step}c", f"Vyanka→{acct_name}", ok2)

    time.sleep(2)

print(f"\n{'='*60}")
print("[Step 4] 切回 Carlin")
print(f"{'='*60}")
ok = switch_account_phone1(d1, "Carlin")
report("4", "切回 Carlin", ok)

print(f"\n{'='*60}")
print("测试结果汇总")
print(f"{'='*60}")
total = len(results)
passed = sum(1 for _, _, ok in results if ok)
for step, desc, ok in results:
    print(f"  [{'✓' if ok else '✗'}] {step}: {desc}")
print(f"\n  总计: {total} | 通过: {passed} | 失败: {total-passed}")
if total:
    print(f"  通过率: {passed/total*100:.0f}%")
print("=" * 60)
