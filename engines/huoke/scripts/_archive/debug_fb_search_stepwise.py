#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB 搜索→加好友链路分步调试 (2026-04-23 v2).

直接调用生产 FacebookAutomation.search_people / _tap_search_bar_preferred /
_extract_search_results, 每个关键点截图 + dump XML, 分析:
  1. 搜索入口是否真的被点开 (xml chars 变化 + 出现 Recent searches)
  2. 输入后是否真的 submit (结果 feed 刷新)
  3. People tab 是否切成功
  4. 提取到的候选人名列表
  5. `_search_result_name_plausible` 把谁 pass/谁 reject
  6. 点第一个匹配后是否进 profile 页 + 有 Add Friend 按钮

**不真点 Add Friend** — 只检测按钮存在性.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 把项目根加到 sys.path 以 import src
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _init_logging():
    """让 src.app_automation.facebook 的 log.info 打到 console."""
    import logging
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S"))
        root.addHandler(h)


class StepRecorder:
    def __init__(self, out_dir: Path):
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.step_no = 0
        self.findings = []

    def dump(self, d, title: str, raw_xml: str = ""):
        self.step_no += 1
        tag = re.sub(r'[^a-zA-Z0-9_]+', '_', title)[:40]
        name = f"step{self.step_no:02d}_{tag}"
        png = self.out / f"{name}.png"
        xml_f = self.out / f"{name}.xml"
        try:
            d.screenshot(str(png))
        except Exception as e:
            print(f"  [WARN] 截图失败: {e}")
        xml = raw_xml
        if not xml:
            try:
                xml = d.dump_hierarchy()
            except Exception as e:
                print(f"  [WARN] dump_hierarchy 失败: {e}")
                xml = ""
        xml_f.write_text(xml, encoding="utf-8")
        try:
            info = d.info
            pkg = info.get("currentPackageName", "")
        except Exception:
            pkg = "?"
        print(f"\n━━━ step {self.step_no}: {title} ━━━")
        print(f"  [dump] pkg={pkg} xml_chars={len(xml)} → {png.name}")
        return name, xml

    def note(self, ok: bool, msg: str):
        icon = "✓" if ok else "✗"
        print(f"  {icon} {msg}")
        self.findings.append((self.step_no, ok, msg))


# ─── 诊断工具 ───────────────────────────────────────────────────────

def scan_topbar_clickables(xml: str, y_max: int = 280) -> list:
    """顶栏 (y<280) 内所有 clickable 元素 — 帮找搜索入口。"""
    results = []
    for m in re.finditer(
        r'<node[^>]*\bclass="([^"]+)"[^>]*\btext="([^"]*)"[^>]*\bresource-id="([^"]*)"[^>]*\bcontent-desc="([^"]*)"[^>]*\bclickable="(true|false)"[^>]*\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml, re.S
    ):
        if m.group(5) != "true":
            continue
        y1 = int(m.group(7))
        if y1 >= y_max:
            continue
        results.append({
            "class": m.group(1).split(".")[-1],
            "text": m.group(2)[:30],
            "rid": m.group(3).split(":id/")[-1] if ":id/" in m.group(3) else m.group(3),
            "desc": m.group(4)[:40],
            "bounds": f"({m.group(6)},{m.group(7)})-({m.group(8)},{m.group(9)})",
        })
    return results


def extract_people_candidates(xml: str, max_items: int = 15) -> list:
    FILTER_TABS = {
        "All", "Posts", "People", "Groups", "Pages", "Events", "Reels",
        "Photos", "Marketplace", "Videos", "Places", "News", "Home",
        "Search Facebook", "Search", "What's on your mind?", "Clear",
        "See all", "See more", "Recent", "Recent searches", "Filters",
        "全部", "贴文", "用户", "小组", "公共主页", "活动", "影片",
    }
    items = []
    for m in re.finditer(
        r'<node[^>]*\b(?:text|content-desc)="([^"]+)"[^>]*\bclass="([^"]+)"[^>]*\bclickable="(true|false)"[^>]*\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml, re.S
    ):
        txt = m.group(1).strip()
        cls = m.group(2)
        clickable = m.group(3) == "true"
        y1 = int(m.group(5))
        if not txt or len(txt) < 2 or len(txt) > 40:
            continue
        if txt in FILTER_TABS:
            continue
        if y1 < 260:
            continue
        if "TextView" not in cls and "Button" not in cls and "View" not in cls:
            continue
        items.append({
            "text": txt, "clickable": clickable, "class": cls.split(".")[-1],
            "y1": y1,
            "bounds": (int(m.group(4)), y1, int(m.group(6)), int(m.group(7))),
        })
    seen, dedup = set(), []
    for it in items:
        if it["text"] in seen:
            continue
        seen.add(it["text"])
        dedup.append(it)
        if len(dedup) >= max_items:
            break
    return dedup


def has_addfriend_button(xml: str) -> bool:
    lo = xml.lower()
    if "addfriend" in lo.replace("_", "").replace("-", ""):
        return True
    if "add friend" in lo:
        return True
    if "友達を追加" in xml or "友達になる" in xml:
        return True
    return False


def is_profile_page(xml: str) -> bool:
    lo = xml.lower()
    if "profile_actionbar" in lo or "profile_header" in lo:
        return True
    if "com.facebook.katana:id/profile_" in lo:
        return True
    hits = sum(1 for s in ("Add Friend", "Message", "Follow",
                             "友達を追加", "メッセージ", "フォロー") if s in xml)
    return hits >= 1


# ─── 主流程 ─────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--launch-wait", type=float, default=7.0,
                     help="启动 FB 后等顶栏渲染的秒数 (默认 7)")
    args = ap.parse_args()

    _init_logging()

    import uiautomator2 as u2
    from src.app_automation.facebook import FacebookAutomation

    out = Path("debug") / ("fb_search_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    rec = StepRecorder(out)
    print(f"[init] out = {out}")

    print(f"[init] 连接设备 {args.device} …")
    d = u2.connect(args.device)
    wh = (d.info["displayWidth"], d.info["displayHeight"])
    print(f"  screen={wh[0]}x{wh[1]}")

    # 初始化生产 facebook automation
    fb = FacebookAutomation()
    fb._current_device = args.device

    # ── step 1: 启动 FB 前 ───────────────────────────────────────
    rec.dump(d, "before_launch")

    print(f"[act] 强制 stop + 启动 com.facebook.katana (确保到 Home) …")
    d.app_stop("com.facebook.katana")
    time.sleep(1.5)
    d.app_start("com.facebook.katana", use_monkey=True)
    time.sleep(args.launch_wait)
    # 额外保险: 连按 back 回 Home (最多 3 次), 再按屏幕底部第一个 tab
    for _ in range(3):
        try:
            pkg = d.info.get("currentPackageName", "")
            if pkg != "com.facebook.katana":
                break
            # 如果在 profile/settings/messenger 等子页, back
            xml_chk = d.dump_hierarchy() or ""
            if "What's on your mind?" in xml_chk or "Stories" in xml_chk:
                break
            d.press("back")
            time.sleep(1.0)
        except Exception:
            break

    # 主动扫弹窗
    for t in ("Not Now", "Skip", "Maybe Later", "OK", "Got it", "Continue",
                "Close", "Dismiss", "Allow", "While using the app", "Later"):
        try:
            el = d(text=t)
            if el.exists(timeout=0.3):
                el.click()
                print(f"  [dialog] 跳 '{t}'")
                time.sleep(0.5)
        except Exception:
            pass

    _, xml2 = rec.dump(d, "after_launch_ready")
    pkg2 = d.info.get("currentPackageName", "")
    rec.note(pkg2 == "com.facebook.katana", f"FB 前台 (pkg={pkg2})")

    topbar = scan_topbar_clickables(xml2, y_max=280)
    print(f"  [analyze] 顶栏 (y<280) clickable 元素 {len(topbar)} 个:")
    for i, e in enumerate(topbar[:12]):
        print(f"    {i+1:2d}. {e['class']:14s} rid={e['rid'][:25]:25s} "
              f"text={e['text'][:12]!r:14s} desc={e['desc'][:28]!r:30s} "
              f"@ {e['bounds']}")

    # 找可能的搜索入口: desc/text 含 Search, 或 rid 含 search
    search_hints = [e for e in topbar
                     if "search" in (e["desc"] + e["text"] + e["rid"]).lower()]
    if search_hints:
        print(f"  [analyze] 顶栏发现 {len(search_hints)} 个含 'search' 的 clickable:")
        for e in search_hints:
            print(f"    → {e}")

    # ── 完整走生产 search_people — 这才是 smoke 真实路径 ──────
    print(f"\n[act] 走生产 search_people({args.query!r}) …")
    try:
        sp_results = fb.search_people(args.query, args.device, max_results=5)
    except Exception as e:
        print(f"  [EXC] search_people: {e}")
        sp_results = []
    _, xml_sp = rec.dump(d, "after_search_people")
    print(f"  → search_people 返回 {len(sp_results)} 个:")
    for r in sp_results[:5]:
        print(f"     • {r.get('name')!r}")
    # 把输入框是否有 query 记下来
    input_has_q = False
    try:
        el = d(className="android.widget.EditText")
        if el.exists(timeout=1.0):
            txt = el.get_text()
            input_has_q = bool(txt and args.query[:2] in txt)
            print(f"  [analyze] 当前页 EditText 内容={txt!r} (期望含 {args.query[:2]!r} → {input_has_q})")
    except Exception as e:
        print(f"  [analyze] 读 EditText 异常: {e}")
    rec.note(len(sp_results) > 0, f"生产 search_people 返回 {len(sp_results)} 条")
    rec.note(input_has_q, f"search 框实际输入内容符合预期={input_has_q}")

    # 为了避免下面原有 step 3 逻辑重复, 提前结束
    _write_summary(rec, out, args,
                    early_exit_reason=None if sp_results else "search_people 返回空",
                    extra={
                        "production_search_people_count": len(sp_results),
                        "input_has_query": input_has_q,
                    })
    return

    # ── step 3: 直接调用生产 _tap_search_bar_preferred ─────────
    print(f"\n[act] 调用生产 _tap_search_bar_preferred …")
    ok_search = False
    try:
        ok_search = fb._tap_search_bar_preferred(d, args.device)
    except Exception as e:
        print(f"  [EXC] _tap_search_bar_preferred 抛异常: {e}")
    time.sleep(2.5)
    _, xml3 = rec.dump(d, "after_tap_search_bar")
    rec.note(ok_search, f"_tap_search_bar_preferred 返回 {ok_search}")

    # 判断是否真进搜索页
    has_recent = "Recent searches" in xml3 or "Recent" in xml3
    has_search_edit = "Search Facebook" in xml3
    xml_changed = len(xml3) != len(xml2)
    rec.note(has_recent or has_search_edit,
              f"搜索页特征: Recent_searches={has_recent}  Search_Facebook={has_search_edit}  "
              f"xml_changed={xml_changed}")

    if not (has_recent or has_search_edit):
        print(f"  [warn] 看上去没真进搜索页 — 看 step03 截图+xml 分析")
        # 不继续后续步骤, 因为没意义
        _write_summary(rec, out, args, "未能打开搜索页", {
            "search_tap_ok": ok_search,
            "has_recent_searches": has_recent,
            "has_search_facebook": has_search_edit,
            "topbar_clickables": len(topbar),
            "topbar_search_hints": [e["desc"] or e["text"] or e["rid"]
                                      for e in search_hints],
        })
        return

    # ── step 4: 输入 query ───────────────────────────────────────
    print(f"\n[act] 输入 {args.query!r} …")
    typed = False
    for sel in (
        {"resourceId": "com.facebook.katana:id/search_query_text_view"},
        {"description": "Search Facebook", "className": "android.widget.EditText"},
        {"className": "android.widget.EditText"},
    ):
        try:
            el = d(**sel)
            if el.exists(timeout=1.5):
                try:
                    el.clear_text()
                except Exception:
                    pass
                el.set_text(args.query)
                print(f"  [input] via {sel}")
                typed = True
                break
        except Exception:
            pass
    if not typed:
        d.shell(f'input text "{args.query}"')
        print(f"  [input] via adb shell input text")
    time.sleep(2)
    rec.dump(d, "typed")
    d.press("enter")
    time.sleep(3.5)
    _, xml5 = rec.dump(d, "after_enter")

    # ── step 6: 切 People tab via 生产 _people_tab_fallback_adb ─
    print(f"\n[act] 切 People tab …")
    tapped = False
    for sel in (
        {"descriptionContains": "People search results"},
        {"text": "People"},
        {"text": "用户"},
        {"text": "人物"},
    ):
        try:
            el = d(**sel)
            if el.exists(timeout=1.5):
                el.click()
                tapped = True
                print(f"  [sel] People tab via {sel}")
                break
        except Exception:
            pass
    if not tapped:
        try:
            fb._people_tab_fallback_adb(d, args.device)
            print(f"  [fallback] 走生产 _people_tab_fallback_adb 按坐标 tap")
        except Exception as e:
            print(f"  [EXC] fallback: {e}")
    time.sleep(2.5)
    _, xml6 = rec.dump(d, "people_tab")
    rec.note(tapped, f"People tab 切换 (by selector={tapped})")

    # ── step 7: 调生产 _extract_search_results + 分析 plausible ─
    print(f"\n[analyze] 生产 _extract_search_results(query_hint={args.query!r}) …")
    try:
        results = fb._extract_search_results(d, 10, query_hint=args.query)
    except Exception as e:
        print(f"  [EXC] _extract_search_results: {e}")
        results = []
    print(f"  → 生产返回 {len(results)} 个命名候选:")
    for r in results[:10]:
        print(f"     • {r.get('name')!r}")

    # 对比: 启发式从 XML 提取, 不过滤 plausible
    raw_cands = extract_people_candidates(xml6, max_items=20)
    print(f"  → 原始顶层 textview candidate ({len(raw_cands)}, 无 plausible 过滤):")
    for c in raw_cands[:10]:
        match = any(
            t.lower() in c["text"].lower()
            for t in (args.query.split() or [args.query])
        )
        flag = "★" if match else " "
        print(f"    {flag} y={c['y1']:4d} {c['text']!r}")

    rec.note(len(results) > 0,
              f"_extract_search_results 结果数 {len(results)}")
    rec.note(len(raw_cands) > 0,
              f"原始 XML 可见人名候选 {len(raw_cands)}")

    # ── step 8: 点第一个 result (如果有) ─────────────────────────
    if results:
        target = results[0]["name"]
        print(f"\n[act] 点第一个结果 {target!r} …")
        clicked = False
        # FB 搜索结果 element text 通常是 "山田花子\xa0· Add\xa0friend",
        # 不能精确 text 匹配 — 用 textContains / descContains 多重兜底
        for sel in (
            {"textContains": target},
            {"descriptionContains": target},
            {"text": target},
        ):
            try:
                el = d(**sel)
                if el.exists(timeout=1.8):
                    el.click()
                    clicked = True
                    print(f"  [click] via {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            # 用生产的 _first_search_result_element + _el_center 点
            try:
                first_el = fb._first_search_result_element(d)
                if first_el:
                    cx, cy = fb._el_center(first_el)
                    d.click(cx, cy)
                    clicked = True
                    print(f"  [click] via _first_search_result_element @ ({cx},{cy})")
            except Exception as e:
                print(f"  [EXC] _first_search_result_element: {e}")

        if clicked:
            time.sleep(5.5)
            _, xml8 = rec.dump(d, "after_click_result")
            is_prof = is_profile_page(xml8)
            has_af = has_addfriend_button(xml8)
            rec.note(is_prof, "进 profile 页" if is_prof else
                      "未检测到 profile 特征")
            rec.note(has_af, "有 Add Friend 按钮" if has_af else
                      "无 Add Friend 按钮")
        else:
            rec.note(False, f"点不到任何候选 target={target!r}")
    else:
        print(f"\n[skip] 生产返回 0 个结果 — 不点")

    _write_summary(rec, out, args, None, {
        "search_tap_ok": ok_search,
        "production_results_count": len(results),
        "raw_candidates_count": len(raw_cands),
    })


def _write_summary(rec, out, args, early_exit_reason, extra):
    lines = [
        "# FB 搜索分步调试报告",
        f"",
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 设备: {args.device}",
        f"- 搜索词: `{args.query}`",
        f"- 启动等待: {args.launch_wait}s",
        f"",
    ]
    if early_exit_reason:
        lines += [f"## 提前退出: {early_exit_reason}", ""]
    lines += ["## 每步检查", "", "| Step | OK | 说明 |", "|---|---|---|"]
    for step, ok, msg in rec.findings:
        icon = "✓" if ok else "✗"
        safe = msg.replace("|", "\\|")
        lines.append(f"| {step} | {icon} | {safe} |")
    lines += ["", "## 量化发现", ""]
    for k, v in extra.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] → {out / 'summary.md'}")


if __name__ == "__main__":
    main()
