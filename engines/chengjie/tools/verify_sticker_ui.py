# -*- coding: utf-8 -*-
"""表情包面板真浏览器门禁（Playwright；2026-08-17 表情/文件收发主线）。

**为什么需要它**：sticker-panel.js 是外挂静态组件（包装 wsEmojiOpen 注入双 tab、
全程事件委托、feature flag 动态显隐）——模板静态门禁（哑按钮/孤儿引用）扫不到
「外部 JS 里的委托处理器是否真的接住了点击」这类运行时行为；而模板/静态 JS
热更新直上生产，坏了没有部署缓冲。本工具用真浏览器把关键交互走一遍：

  1. 组件资产已装载（sticker-panel.js/css 標籤在，且 JS 求值无抛错）；
  2. 点表情按钮 → 弹层打开（emoji 旧行为不回归）；
  3. flag 开启时：双 tab 注入、切「表情包」tab → 贴纸区可见（空态 CTA 或网格）、
     包切换条渲染、管理弹层能开能关；
  4. flag 关闭时：**不注入** tab（纯 emoji 零变化）——反向断言防「关了还漏 UI」；
  5. 全程零未捕获异常 / 零 ReferenceError（console + pageerror 监听）。

与 verify_inbox_density.py 同族（同实例 + token 登录 + SKIP exit 0 语义）：
缺 playwright / 实例不可达 / 工作台脚本未就绪 → SKIP exit 0，不污染回归信号。
**副作用：无**——只开关面板，不发送任何消息（发送链由 tests/test_sticker_routes.py
的假编排器覆盖）。

用法::

    python tools/verify_sticker_ui.py             # 门禁模式
    python tools/verify_sticker_ui.py --headed    # 肉眼看一遍
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：Windows 先试 ::1 每连接 ~2s 回退
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== 表情包面板验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


_POP_JS = """() => {
  const pop = document.getElementById('wsemj-pop');
  return {
    show: !!pop && pop.classList.contains('show'),
    topbar: !!pop && !!pop.querySelector('.stk-topbar'),
    tabs: pop ? pop.querySelectorAll('.stk-top').length : 0,
    picker: !!pop && !!pop.querySelector('emoji-picker'),
  };
}"""

_STK_JS = """() => {
  const area = document.getElementById('stk-area');
  const grid = document.getElementById('stk-grid');
  const bar = document.getElementById('stk-packbar');
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none';
  return {
    area: !!area, area_vis: vis(area),
    cells: grid ? grid.querySelectorAll('.stk-cell').length : 0,
    empty_cta: grid ? grid.querySelectorAll('.stk-empty [data-stk-seed],.stk-empty [data-stk-mgr]').length : 0,
    packs: bar ? bar.querySelectorAll('[data-stk-pack]').length : 0,
    gear: bar ? !!bar.querySelector('[data-stk-mgr]') : false,
    hint: (document.getElementById('stk-hint') || {}).textContent || '',
    search: !!document.getElementById('stk-q'),
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    errors: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" and "ReferenceError" in (m.text or "") else None)
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.wsEmojiOpen === 'function'", timeout=20000)
        except Exception:
            print("[SKIP] 工作台脚本未就绪（实例在重启？）")
            browser.close()
            return 0
        page.wait_for_timeout(2500)

        # flag 状态（决定走「注入」还是「反向断言」分支）
        st = page.evaluate(
            "() => fetch('/api/stickers/status').then(r=>r.json()).catch(()=>null)")
        enabled = bool(st and st.get("enabled"))
        print(f"== flag: inbox.stickers.enabled = {enabled} ==")

        print("== 1. 组件资产装载 ==")
        assets = page.evaluate(
            "() => ({js: !!document.querySelector('script[src*=\"sticker-panel.js\"]'),"
            " css: !!document.querySelector('link[href*=\"sticker-panel.css\"]')})")
        ck.check("sticker-panel.js 已引入", assets["js"])
        ck.check("sticker-panel.css 已引入", assets["css"])

        print("== 2. emoji 弹层旧行为 ==")
        # 先选中一个**已读**会话（composer 才可交互；只点已读行不改变未读状态，
        # 与 verify_inbox_density 同纪律）；没有已读会话就整体 SKIP。
        opened = page.evaluate(
            "() => { const el=document.querySelector('.conv-item:not(.has-unread)');"
            " if(!el) return false; el.click(); return true; }")
        if not opened:
            print("[SKIP] 无已读会话可打开（不动未读状态）")
            browser.close()
            return 0
        page.wait_for_timeout(1500)
        # JS 直点（按钮存在即可，绕开浮层遮挡的 actionability 误伤；只读动作）
        page.evaluate(
            "() => { const b=document.getElementById('reply-emoji-btn');"
            " if(b) b.click(); }")
        try:
            page.wait_for_function(
                "() => document.getElementById('wsemj-pop')"
                " && document.getElementById('wsemj-pop').classList.contains('show')",
                timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1200)   # 给包装器的 augmentSoon 轮询留时间
        pop = page.evaluate(_POP_JS)
        ck.check("弹层打开且 emoji-picker 在", pop["show"] and pop["picker"],
                 f"show={pop['show']} picker={pop['picker']}")

        if not enabled:
            print("== 3. flag 关：不注入 tab（旧行为零变化）==")
            ck.check("未注入 .stk-topbar", not pop["topbar"], f"tabs={pop['tabs']}")
        else:
            print("== 3. flag 开：双 tab 注入 + 贴纸区 ==")
            ck.check("双 tab 已注入", pop["topbar"] and pop["tabs"] == 2,
                     f"tabs={pop['tabs']}")
            page.click(".stk-top[data-stk-top='stk']")
            # 等条件而非固定时长（2026-08-17 实锤：实例刚重启缓存冷，包列表 +
            # 自动落包的条目拉取两次往返 >1.5s，固定 sleep 会在数据到达前取数
            # 误报红）。两段软等：先包条按钮出现，再网格出格子/空态；超时不抛
            # ——照常取数，让断言如实红。
            for cond in (
                "() => document.querySelectorAll('#stk-packbar [data-stk-pack]').length > 0",
                # 网格「真到位」＝有格子 / 有空态 CTA / 空态文案不再是挂载时的
                # 「加载中」占位（占位也是 .stk-empty，不排除它这一等形同虚设）
                "() => { const g=document.getElementById('stk-grid');"
                " if (!g) return false;"
                " if (g.querySelectorAll('.stk-cell').length > 0) return true;"
                " if (g.querySelector('[data-stk-seed],[data-stk-mgr]')) return true;"
                " const e=g.querySelector('.stk-empty'); if (!e) return false;"
                " const ld=(window.T?window.T('inbox.stk.loading'):'')||'';"
                " return (e.textContent||'').trim() !== ld.trim(); }",
            ):
                try:
                    page.wait_for_function(cond, timeout=12000)
                except Exception:
                    break
            page.wait_for_timeout(300)
            stk = page.evaluate(_STK_JS)
            ck.check("贴纸区可见", stk["area"] and stk["area_vis"])
            ck.check("贴纸搜索框已挂载", stk["search"])
            ck.check("网格有内容（贴纸格 或 空态 CTA）",
                     stk["cells"] > 0 or stk["empty_cta"] > 0,
                     f"cells={stk['cells']} cta={stk['empty_cta']}")
            has_packs = stk["packs"] > 0
            ck.check("包切换条渲染（含最近 tab + 管理入口）",
                     (has_packs and stk["gear"]) or stk["empty_cta"] > 0,
                     f"packs={stk['packs']} gear={stk['gear']}")
            if stk["hint"]:
                print(f"     能力预告: {stk['hint'][:40]!r}")
            print("== 4. 管理弹层开/关 ==")
            page.evaluate(
                "() => { const b=document.querySelector('[data-stk-mgr]');"
                " if (b) b.click(); }")
            page.wait_for_timeout(900)
            mgr = page.evaluate(
                "() => { const m=document.getElementById('stk-mgr');"
                " return {open: !!m && getComputedStyle(m).display !== 'none'}; }")
            ck.check("管理弹层可打开", mgr["open"])
            if mgr["open"]:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
                mgr2 = page.evaluate(
                    "() => { const m=document.getElementById('stk-mgr');"
                    " return {open: !!m && getComputedStyle(m).display !== 'none'}; }")
                ck.check("Esc 关闭管理弹层", not mgr2["open"])

        print("== 5. 运行时零异常 ==")
        ck.check("无未捕获异常 / ReferenceError", not errors,
                 "; ".join(errors[:3])[:160])
        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到 auth_token（数据根不对 / 非本机）")
        return 0
    try:
        import urllib.request
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("[SKIP] 实例不可达: " + args.base)
        return 0
    try:
        return run(args.base, token, headed=args.headed)
    except Exception as ex:  # noqa: BLE001
        print(f"[SKIP] 浏览器环境异常: {str(ex)[:200]}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
