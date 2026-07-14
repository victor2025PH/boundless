#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB 群成员打招呼链路 trace (2026-04-24, Phase 9-extract).

与 Phase 7 的 debug_fb_search_trace 同思路 — 真机 dump + 每步截图, 定位
`_tap_group_members_tab` 失败原因.

**前提**: 运营把设备手工带到某个 FB 群的详情页 (已进群, 能看到 Members 入口).
脚本不启动 FB 也不进群, 从当前页直接 dump + 分析.

用法::

    # 1. 运营在设备上打开 FB, 进某个 jp 群
    # 2. 运行:
    python scripts/debug_extract_members_trace.py --device 8DWOF6CYY5R8YHX8

输出:
    debug/extract_members_<ts>/
      step01_current.png      当前页截图
      step01_current.xml      hierarchy dump
      members_candidates.txt  扫到的所有候选 "Members tab" 元素
      summary.md              诊断结论 + 推荐 selector
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _node_attrs(node_str: str) -> dict:
    out = {}
    for attr in ("class", "text", "content-desc", "resource-id",
                   "bounds", "clickable", "focusable", "package"):
        m = re.search(rf'\b{attr}="([^"]*)"', node_str)
        if m:
            out[attr] = m.group(1)
    return out


def _parse_bounds(s: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", s or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def main():
    ap = argparse.ArgumentParser(description="FB 群成员打招呼 UI 诊断")
    ap.add_argument("--device", required=True)
    args = ap.parse_args()

    out = Path("debug") / f"extract_members_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"[init] out = {out}")

    import uiautomator2 as u2
    d = u2.connect(args.device)
    info = d.info
    pkg = info.get("currentPackageName", "?")
    print(f"[init] device ok, screen={info['displayWidth']}x{info['displayHeight']}, "
          f"current pkg={pkg}")

    if pkg != "com.facebook.katana":
        print(f"[WARN] 当前不在 FB (pkg={pkg}). 请先把设备打开 FB 并进某个群页面")

    # ── step 1: 截图 + dump ─────────────────────────────────────
    d.screenshot(str(out / "step01_current.png"))
    xml = d.dump_hierarchy() or ""
    (out / "step01_current.xml").write_text(xml, encoding="utf-8")
    print(f"[step1] dump 完成 xml_len={len(xml)}")

    # ── step 2: 扫所有含 "Members" / "メンバー" / "成员" 的 node ─────
    keywords = ("Members", "メンバー", "成员", "成員", "Membri",
                  "Membres", "Mitglieder", "Miembros")
    hits = []
    for nm in re.finditer(r'<node\s[^>]+/>', xml):
        node_str = nm.group(0)
        attrs = _node_attrs(node_str)
        label = attrs.get("text") or attrs.get("content-desc") or ""
        if not any(k in label for k in keywords):
            continue
        hits.append(attrs)

    # ── step 3: 归类 ────────────────────────────────────────────
    print(f"\n[step3] 含关键词 {keywords} 的 node 共 {len(hits)} 个:\n")
    suspect_tab = []   # 最可能是 Members Tab 的
    noise = []          # 看起来是噪音/推荐群等
    for i, a in enumerate(hits):
        label = a.get("text") or a.get("content-desc") or ""
        cls = a.get("class", "").split(".")[-1]
        clickable = a.get("clickable", "false")
        rid = (a.get("resource-id") or "").split(":id/")[-1]
        bounds = a.get("bounds", "")
        b = _parse_bounds(bounds)
        area = 0
        if b:
            area = (b[2] - b[0]) * (b[3] - b[1])

        # 判是否 "Members" 短 label + 在屏幕上部 + clickable
        short_label = len(label) <= 20 and label.strip() in \
            ("Members", "MEMBERS", "メンバー", "成员", "Membri")
        # 推荐群卡片特征: label 含长文本 "Suggested" / 群名带描述
        looks_suggested = any(w in label.lower() for w in
                                ("suggested", "you may", "recommended",
                                  "可能认识", "推荐"))
        bucket = suspect_tab if (short_label and clickable == "true"
                                   and not looks_suggested) else noise
        bucket.append((i, a, bounds, cls, clickable, label[:60], area))
        print(f"  [{i:2d}] {cls:14s} clickable={clickable} "
              f"label={label[:40]!r:45s} area={area:6d} rid={rid[:25]:25s} {bounds}")

    # ── step 4: 写 candidates.txt + summary.md ─────────────────
    cand_lines = [f"总命中 {len(hits)} 个, 疑似 Tab {len(suspect_tab)} 个, "
                   f"噪音 {len(noise)} 个", ""]
    cand_lines.append("## 疑似 Members Tab (短 label + clickable + 非推荐卡片)")
    for idx, a, bounds, cls, clk, label, area in suspect_tab:
        cand_lines.append(f"[{idx}] class={cls} clickable={clk} label={label!r} "
                           f"bounds={bounds} area={area}")
        cand_lines.append(f"    rid={a.get('resource-id', '')}")
    cand_lines.append("")
    cand_lines.append("## 噪音候选 (供排查)")
    for idx, a, bounds, cls, clk, label, area in noise[:10]:
        cand_lines.append(f"[{idx}] class={cls} clickable={clk} label={label!r} "
                           f"bounds={bounds}")
    (out / "members_candidates.txt").write_text("\n".join(cand_lines),
                                                   encoding="utf-8")

    # ── step 5: 推荐 selector ──────────────────────────────────
    recs = []
    if suspect_tab:
        top = suspect_tab[0]
        idx, a, bounds, cls, clk, label, area = top
        label_str = (a.get("text") or a.get("content-desc") or "").strip()
        if a.get("text"):
            recs.append(f'{{"text": "{label_str}", "clickable": true}}')
        if a.get("content-desc"):
            recs.append(f'{{"description": "{label_str}", "clickable": true}}')
        if a.get("resource-id") and "(name removed)" not in a.get("resource-id"):
            recs.append(f'{{"resourceId": "{a.get("resource-id")}"}}')
        # 坐标 fallback (trace 给出的 bounds 中心)
        b = _parse_bounds(bounds)
        if b:
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            recs.append(f"# 坐标 fallback: d.click({cx}, {cy})")

    summary_lines = [
        "# FB Extract Members 诊断报告",
        "",
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 设备: {args.device}",
        f"- 当前 pkg: {pkg}",
        f"- xml_len: {len(xml)}",
        f"- 关键词命中: {len(hits)} 个 node",
        f"- 疑似 Members Tab: {len(suspect_tab)} 个",
        f"- 噪音: {len(noise)} 个",
        "",
        "## 推荐 selector (从最可能的候选拎出)",
        "",
    ]
    if recs:
        for r in recs:
            summary_lines.append(f"- `{r}`")
    else:
        summary_lines.append("(无明显候选 — 运营可能不在群详情页; "
                              "或 FB 新版 UI Members 入口 label 变了)")
        summary_lines.append("")
        summary_lines.append("## 下一步建议")
        summary_lines.append("1. 确认设备当前确实在某个群详情页 (能看到 'Members' 入口 UI)")
        summary_lines.append("2. 如 label 在 content-desc 而非 text — 扩展 selector")
        summary_lines.append("3. 如入口移到 `Group info → Members` 2 级页 — 改流程")
    (out / "summary.md").write_text("\n".join(summary_lines),
                                       encoding="utf-8")
    print(f"\n[done] → {out / 'summary.md'}")
    print(f"       → {out / 'members_candidates.txt'}")


if __name__ == "__main__":
    main()
