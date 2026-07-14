#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB search 链路逐步 trace 工具 (2026-04-24).

**定位**: 与 smoke_fb_name_hunter_realdevice.py 的区别 —
  smoke 走 worker pool → task executor → AdbFallbackDevice 包装 (可能)
  trace 直接用 u2.connect() 驱动设备, 每步截图 + dump_hierarchy 落盘,
  复用生产 `fb._extract_search_results` 验证 extractor 逻辑。

**用途**:
  1. smoke 失败时先跑 trace — 同一链路 trace 成功 = 业务逻辑对, 问题在 wrapper 层
  2. 验证新加的 selector / 时序 / 自检逻辑能否稳定 work (不用重启 server)
  3. 两种 device mode (原生 u2 vs wrapper 模拟) 对比, 定位 wrapper 行为差异

用法::

    # 默认: 原生 u2 模式 (知道 wrapper 有 bug 时用来验证业务代码对)
    python scripts/debug_fb_search_trace.py --device 8DWOF6CYY5R8YHX8 --query 佐藤花子

    # wrapper 模式: 走生产 `FacebookAutomation._u2()` 路径, 看 wrapper 下是否 work
    python scripts/debug_fb_search_trace.py --device <id> --query <name> --mode wrapper

    # 仅验 extract (设备已在结果页时): 跳过启动 + 搜索, 直接 extract
    python scripts/debug_fb_search_trace.py --device <id> --query <name> --extract-only

每步产物:
  debug/trace_<ts>/step{NN}_<tag>.png    截图
  debug/trace_<ts>/step{NN}_<tag>.xml    hierarchy dump
  debug/trace_<ts>/summary.md            每步结果表
"""
from __future__ import annotations

import argparse
import io
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

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class Trace:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.step = 0
        self.rows = []

    def snap(self, d, tag: str):
        self.step += 1
        import re
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", tag)[:36]
        name = f"step{self.step:02d}_{safe}"
        png = self.out / f"{name}.png"
        xml_f = self.out / f"{name}.xml"
        try:
            d.screenshot(str(png))
        except Exception as e:
            print(f"  [!] screenshot 失败: {e}")
        xml = ""
        try:
            xml = d.dump_hierarchy() or ""
            xml_f.write_text(xml, encoding="utf-8")
        except Exception as e:
            print(f"  [!] dump_hierarchy 失败: {e}")
        try:
            pkg = d.info.get("currentPackageName", "?")
        except Exception:
            pkg = "?"
        print(f"[step {self.step:02d}] {tag}  pkg={pkg}  xml_len={len(xml)}  → {png.name}")
        return xml, pkg

    def mark(self, ok: bool, msg: str):
        icon = "✓" if ok else "✗"
        print(f"         {icon} {msg}")
        self.rows.append((self.step, ok, msg))

    def save_summary(self, meta: dict):
        lines = [
            "# FB Search Trace",
            "",
            f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
            f"- 设备: {meta.get('device', '?')}",
            f"- 模式: {meta.get('mode', 'native')}",
            f"- 查询: `{meta.get('query', '')}`",
            f"- 结果数: {meta.get('results_count', 0)}",
            "",
            "## 每步",
            "",
            "| # | OK | 备注 |",
            "|---|----|------|",
        ]
        for step, ok, msg in self.rows:
            icon = "✓" if ok else "✗"
            safe = msg.replace("|", "\\|")
            lines.append(f"| {step} | {icon} | {safe} |")
        if meta.get("top_results"):
            lines += ["", "## 候选", ""]
            for r in meta["top_results"][:10]:
                lines.append(f"- {r.get('name', '?')}")
        (self.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"\n→ summary: {self.out / 'summary.md'}")


def _dismiss_popups(d, keys=("Not Now", "Skip", "OK", "Continue",
                              "Allow", "Close", "Got it", "Maybe Later")):
    for t in keys:
        try:
            el = d(text=t)
            if el.exists(timeout=0.3):
                el.click()
                time.sleep(0.4)
        except Exception:
            pass


def _run_trace(d, query: str, tr: Trace, is_wrapper: bool) -> tuple:
    """执行完整 search 链路 trace, 返回 (results, meta)."""
    # ── 1. 重启 FB 确保新鲜状态 ──────────────────────────────
    print("\n─── 1. app_stop + app_start ───")
    try:
        d.app_stop("com.facebook.katana")
    except Exception as e:
        print(f"  [!] app_stop: {e}")
    time.sleep(1.5)
    try:
        d.app_start("com.facebook.katana")
    except Exception as e:
        print(f"  [!] app_start: {e}")
    time.sleep(7)
    _dismiss_popups(d)

    xml1, pkg1 = tr.snap(d, "after_restart")
    tr.mark(pkg1 == "com.facebook.katana",
             f"FB 前台 (pkg={pkg1})")

    # ── 2. 找 Button desc=Search 并 click ─────────────────────
    print("\n─── 2. 点 Button desc='Search' ───")
    btn = d(className="android.widget.Button", description="Search")
    btn_exists = btn.exists(timeout=2.5)
    tr.mark(btn_exists, f"Button desc='Search' exists={btn_exists}")
    if not btn_exists:
        tr.snap(d, "no_search_btn")
        return [], {"phase": "no_search_btn"}
    btn.click()
    time.sleep(1.8)
    xml2, _ = tr.snap(d, "after_click_search")
    tr.mark("android.widget.EditText" in xml2,
             "搜索页: 含 EditText")

    # ── 3. 找 EditText + set_text ─────────────────────────────
    print(f"\n─── 3. EditText.set_text({query!r}) ───")
    ed = d(className="android.widget.EditText")
    ed_exists = ed.exists(timeout=2.0)
    tr.mark(ed_exists, f"EditText exists={ed_exists}")
    if not ed_exists:
        tr.snap(d, "no_edittext")
        return [], {"phase": "no_edittext"}
    try:
        ed.click()
        time.sleep(0.5)
    except Exception as e:
        print(f"  [!] ed.click: {e}")
    try:
        ed.clear_text()
        time.sleep(0.3)
    except Exception as e:
        print(f"  [!] ed.clear_text: {e}")
    try:
        ed.set_text(query)
    except Exception as e:
        print(f"  [!] ed.set_text: {e}")
    time.sleep(2.0)

    # 关键验证: set_text 后 EditText 真的含 query 吗?
    try:
        ed2 = d(className="android.widget.EditText")
        if ed2.exists(timeout=0.8):
            got = ed2.get_text() or ""
            tr.mark(query[:2] in got,
                     f"EditText.get_text()={got!r} (期望含 {query[:2]!r})")
    except Exception as e:
        print(f"  [!] get_text 验证: {e}")
    tr.snap(d, "after_set_text")

    # ── 4. press enter 提交搜索 ──────────────────────────────
    print("\n─── 4. press enter 提交 ───")
    d.press("enter")
    time.sleep(3.5)
    xml4, _ = tr.snap(d, "after_enter")
    tr.mark(len(xml4) > 60000,
             f"结果页渲染 (xml_len={len(xml4)}, 大于 60k 通常是结果列表)")

    # ── 5. 用生产 _extract_search_results 解析 ────────────────
    print("\n─── 5. 生产 _extract_search_results ───")
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation()
    results = []
    for attempt in range(3):
        try:
            results = fb._extract_search_results(d, 10, query_hint=query)
        except Exception as e:
            print(f"  [!] extract 异常: {e}")
            results = []
        print(f"  attempt {attempt + 1}: {len(results)} 条")
        if results:
            break
        time.sleep(1.2)
    tr.snap(d, "after_extract")
    tr.mark(len(results) > 0,
             f"extract 返回 {len(results)} 条候选")
    if results:
        for r in results[:8]:
            print(f"    • {r.get('name')!r}")
    return results, {"top_results": results}


def main():
    ap = argparse.ArgumentParser(description="FB search 链路 trace + extract 验证")
    ap.add_argument("--device", required=True, help="设备 ID")
    ap.add_argument("--query", required=True, help="搜索关键字")
    ap.add_argument("--mode", choices=("native", "wrapper"), default="native",
                     help="native: 直接 u2.connect; wrapper: 走生产 FacebookAutomation._u2()")
    ap.add_argument("--extract-only", action="store_true",
                     help="跳过启动+搜索, 仅在当前页跑 extract (要求已在结果页)")
    args = ap.parse_args()

    out = Path("debug") / (f"trace_{args.mode}_" +
                             datetime.now().strftime("%Y%m%d_%H%M%S"))
    tr = Trace(out)
    print(f"[init] out = {out}")
    print(f"[init] mode = {args.mode}")

    # 获取 device 对象
    if args.mode == "native":
        import uiautomator2 as u2
        d = u2.connect(args.device)
        print(f"[init] 原生 u2.Device: {type(d).__module__}.{type(d).__name__}")
    else:
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        fb._current_device = args.device
        d = fb._u2(args.device)
        print(f"[init] 生产 _u2(): {type(d).__module__}.{type(d).__name__}")

    meta = {"device": args.device, "mode": args.mode, "query": args.query}

    if args.extract_only:
        print("\n─── extract-only 模式 ───")
        tr.snap(d, "current_page")
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        results = fb._extract_search_results(d, 10, query_hint=args.query)
        print(f"  → {len(results)} 条候选:")
        for r in results[:10]:
            print(f"    • {r.get('name')!r}")
        meta["results_count"] = len(results)
        meta["top_results"] = results
        tr.mark(len(results) > 0, f"extract-only: {len(results)} 条")
        tr.save_summary(meta)
        return

    results, extra = _run_trace(d, args.query, tr, args.mode == "wrapper")
    meta["results_count"] = len(results)
    meta.update(extra)
    tr.save_summary(meta)


if __name__ == "__main__":
    main()
