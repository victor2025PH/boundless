# -*- coding: utf-8 -*-
"""双栏融合（桌面壳 × 收件箱）网页半区 真浏览器验证（Playwright；2026-08-11 沉淀）。

**为什么需要它**：2026-08-11 双栏融合把桌面壳左侧账号 rail 收编为顶部标签条，
「打开官方网页版」入口下沉到收件箱账号抽屉，未读徽标/标签状态经 inbox-preload.js
桥双向流动。页面侧全部是**纯前端行为**（桥探测渲染 / 点击驱动桥 / 状态回推消费 /
无桥降级），静态门禁只能证「函数挂了 window / i18n 键存在」，证不了「点了真的驱动
壳、状态真的改写入口语义、纯浏览器真的零变化」，而模板热更新直上生产。
壳侧半区（标签条布局/桥接线/preload 契约）由 desktop/test/renderer-boot-invariants
钉住；本工具钉页面半区，两半合起来才是整条融合链。

与 ``tools/verify_account_rail_ui.py`` 同族（同一实例 + token 登录 + Playwright），
只读：不发消息、不改配置；桥用 stub 冒充（真壳不在场也可验）。

用法::

    python tools/verify_shell_fusion_ui.py            # 打默认实例
    python tools/verify_shell_fusion_ui.py --headed   # 肉眼看一遍
    python tools/verify_shell_fusion_ui.py --shots out/

覆盖的不变量：
  1. 有桥：首轮会话聚合后未读总数经 pushBadge 回推（口径=页面 rail 同源）。
  2. 有桥：账号抽屉溢出菜单渲染「网页版」入口，且仅限可内嵌平台（无 line/web）。
  3. P2 状态：getState() 快照在开抽屉时被拉取——状态里已打开的平台，入口语义
     升级（label 与未打开平台不同 + tooltip 带注入健康）。
  4. P2 状态：postMessage 推送新状态（移除该平台）→ 开着的抽屉就地重渲，
     入口语义回落（label 与其它平台一致）。
  5. P3 诚实隐藏：壳说 enabled=false（Option C 关内嵌）→ 入口整个不渲染
     （不给用户一个点了只弹「未启用」的死入口）。
  6. 有桥：点击入口 → 桥收到 openEmbedded(platform) + 确认 toast +
     shell_open_web/shell_switch_web 用量埋点（本工具桩接 sendBeacon，零漏斗污染）。
  7. 无桥（纯浏览器）：入口一个都不渲染（桌面/网页共用模板零分叉）。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 → SKIP exit 0
（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# stub 状态：enabled=true，telegram 已打开（health=on），其余平台未打开。
# sendBeacon 必须打桩：openAcctWebTab 会发 shell_open_web/shell_switch_web ui-event，
# 门禁跑一次真发一次＝污染用量漏斗（与 verify_setup_wizard_ui 同教训）。
_STUB_INIT = """
  window.__cx_calls = [];
  window.__cx_beacons = [];
  try {
    navigator.sendBeacon = (u, b) => { window.__cx_beacons.push(String(u)); return true; };
  } catch (_) {}
  window.__cx_state = { enabled: true, embedded: [
    { id: 'stub-tg', platform: 'telegram', label: 'TG stub', health: 'on' }
  ]};
  window.__chatxShell = {
    desktop: true,
    openEmbedded: (p, o) => window.__cx_calls.push(['open', String(p)]),
    pushBadge: (x) => window.__cx_calls.push(['badge', Number((x||{}).unread)]),
    getState: () => window.__cx_state,
  };
"""

_ENTRY_JS = """() => {
  const out = {};
  Array.from(document.querySelectorAll('.acct-menu .acct-menu-item'))
    .filter(b => (b.getAttribute('onclick')||'').indexOf('openAcctWebTab') >= 0)
    .forEach(b => {
      const m = /openAcctWebTab\\('([^']+)'\\)/.exec(b.getAttribute('onclick')||'');
      const pl = m ? m[1] : '?';
      if (!out[pl]) out[pl] = {
        label: (b.textContent||'').trim(),
        title: b.getAttribute('title')||'',
      };
    });
  return out;
}"""

_EMBEDDABLE = {"telegram", "whatsapp", "instagram", "messenger", "x", "zalo"}


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

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 双栏融合页面半区验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})

        # ── 场景 A：有桥（stub 须在页面脚本前就位 → add_init_script）────────
        page = ctx.new_page()
        page.add_init_script(_STUB_INIT)
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.openAcctWebTab === 'function'", timeout=20000)
        except Exception:
            print("  [ABORT] 工作台 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        page.wait_for_timeout(2500)   # 等首轮 loadChats（badge 推送点）

        print("== 1. 未读徽标回推（页面聚合口径 → 壳收件箱标签）==")
        badge = [c for c in page.evaluate("() => window.__cx_calls") if c[0] == "badge"]
        ck.check("首轮 loadChats 后 pushBadge 已回推", len(badge) >= 1,
                 f"calls={badge[:3]}")

        print("== 2. 抽屉入口渲染（仅可内嵌平台）==")
        page.evaluate("window.openDrawer()")
        page.wait_for_timeout(1500)
        entries = page.evaluate(_ENTRY_JS)
        plats = sorted(entries.keys())
        ck.check("入口已渲染（≥1 平台）", len(plats) >= 1, f"plats={plats}")
        ck.check("入口仅限可内嵌平台（无 line/web）",
                 all(x in _EMBEDDABLE for x in plats), f"plats={plats}")
        if shots:
            page.screenshot(path=str(shots / "fusion_drawer.png"))

        print("== 3. P2 状态快照：已打开平台入口语义升级 ==")
        tg = entries.get("telegram") or {}
        others = [v for k, v in entries.items() if k != "telegram"]
        if not tg or not others:
            print("  [SKIP] 抽屉缺 telegram 或对照平台（数据不足，语义分叉无从对比）")
        else:
            ck.check("telegram（状态=已打开）label 与未打开平台不同",
                     tg.get("label") and others[0].get("label")
                     and tg["label"] != others[0]["label"],
                     f"tg={tg.get('label')!r} other={others[0].get('label')!r}")
            ck.check("telegram tooltip 带注入健康（{s} 已实插）",
                     bool(tg.get("title")) and "{s}" not in tg["title"]
                     and tg["title"] != others[0].get("title"),
                     f"title={tg.get('title')!r}")

        print("== 4. P2 状态推送：移除平台 → 开着的抽屉就地回落 ==")
        page.evaluate("""() => {
          window.__cx_state = { embedded: [] };
          window.postMessage({ type: 'chatx-shell-state', state: window.__cx_state }, '*');
        }""")
        page.wait_for_timeout(800)
        entries2 = page.evaluate(_ENTRY_JS)
        tg2 = entries2.get("telegram") or {}
        others2 = [v for k, v in entries2.items() if k != "telegram"]
        if not tg2 or not others2:
            print("  [SKIP] 回落对比缺数据")
        else:
            ck.check("状态清空后 telegram label 回落（与其它平台一致）",
                     tg2.get("label") == others2[0].get("label"),
                     f"tg={tg2.get('label')!r} other={others2[0].get('label')!r}")

        print("== 5. Option-C 诚实隐藏：壳说 enabled=false → 入口整个不渲染 ==")
        page.evaluate("""() => {
          window.__cx_state = { enabled: false, embedded: [] };
          window.postMessage({ type: 'chatx-shell-state', state: window.__cx_state }, '*');
        }""")
        page.wait_for_timeout(800)
        n_off = page.evaluate("""() =>
          Array.from(document.querySelectorAll('.acct-menu .acct-menu-item'))
            .filter(b => (b.getAttribute('onclick')||'').indexOf('openAcctWebTab') >= 0).length
        """)
        ck.check("enabled=false 时入口零渲染（死入口不出现）", n_off == 0, f"n={n_off}")
        page.evaluate("""() => {
          window.__cx_state = { enabled: true, embedded: [] };
          window.postMessage({ type: 'chatx-shell-state', state: window.__cx_state }, '*');
        }""")
        page.wait_for_timeout(800)

        print("== 6. 点击入口 → 桥收到 openEmbedded + toast + 用量埋点 ==")
        clicked = page.evaluate("""() => {
          const btn = Array.from(document.querySelectorAll('.acct-menu .acct-menu-item'))
            .find(b => (b.getAttribute('onclick')||'').indexOf('openAcctWebTab') >= 0);
          if (!btn) return false;
          btn.click();
          return true;
        }""")
        page.wait_for_timeout(400)
        opens = [c for c in page.evaluate("() => window.__cx_calls") if c[0] == "open"]
        ck.check("桥收到 openEmbedded(platform)", clicked and len(opens) >= 1,
                 f"opens={opens}")
        toast_ok = page.evaluate(
            "() => /(顶部标签|tab strip)/i.test((document.body.innerText||''))"
            " || !!document.querySelector('.tk-toast')")
        ck.check("出确认 toast", toast_ok)
        beacons = page.evaluate("() => window.__cx_beacons")
        ck.check("shell_open_web 用量埋点已发（且被本工具桩接住，零漏斗污染）",
                 any("ui-event" in b for b in beacons), f"beacons={beacons[:3]}")

        # ── 场景 B：无桥（纯浏览器降级）────────────────────────────────────
        print("== 7. 无桥降级：入口零渲染 ==")
        page2 = ctx.new_page()
        page2.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page2.wait_for_function(
                "() => typeof window.openAcctWebTab === 'function'", timeout=20000)
        except Exception:
            ck.check("无桥页工作台就绪", False)
            browser.close()
            return ck.summary()
        page2.wait_for_timeout(2000)
        page2.evaluate("window.openDrawer()")
        page2.wait_for_timeout(1500)
        n2 = page2.evaluate("""() =>
          Array.from(document.querySelectorAll('.acct-menu .acct-menu-item'))
            .filter(b => (b.getAttribute('onclick')||'').indexOf('openAcctWebTab') >= 0).length
        """)
        ck.check("无桥时入口不渲染（纯浏览器零变化）", n2 == 0, f"n={n2}")

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="双栏融合页面半区真浏览器验证（只读）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未装 playwright（pip install playwright && playwright install chromium）")
        return 0

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass          # 任何 HTTP 响应都代表实例活着
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2
    shots = None
    if args.shots:
        shots = Path(args.shots)
        shots.mkdir(parents=True, exist_ok=True)
    print(f"== 0. 目标 {args.base} ==")
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
