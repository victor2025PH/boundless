# -*- coding: utf-8 -*-
"""工作台 chrome（顶栏/左栏/右栏 iframe）主题一致性浏览器门禁（2026-08-17 P0 收口配套）。

**守什么**：老板实录「选亮色的时候，左侧APP状态栏和右侧工具箱栏还是暗色模式」。
根因是三个互相独立的机制，静态门禁一个都证不了：
  ① 顶栏 .ws-top / 左栏 .nav-rail 曾是「恒深色 chrome」硬编码——现改为
     --chrome-ink* / --rail-* 令牌随主题（暗档=历史字面量，零回归承诺）；
     回归形态＝有人按旧注释把某条规则改回裸深色值，亮色下该面又变黑。
  ② App(iframe) 右栏主题只在加载时读一次 ?theme=，靠 cp-cmd set-theme 桥热同步
     （cpApplyTheme 推送 + cp-ready 握手对齐）；回归形态＝桥断了，切亮色右栏纹丝不动。
  ③ 主题写路径必须走 cpSetTheme→WSAppearance（裸写 cp_theme 会被引擎按 night.mode
     盖回）；本门禁本身就走这条真实用户路径，写路径断了这里先红。

与 verify_theme_contrast_ui 同族（同实例 + token 登录 + Playwright），**只读**：
只切主题读计算样式；走查前记下管理员当前档位、走查后原样恢复（主题偏好经服务器
漫游，不恢复=门禁每跑一次就替坐席改一次主题）。

用法::

    python tools/verify_ws_chrome_theme.py                 # 默认打 /workspace
    python tools/verify_ws_chrome_theme.py --shots out/    # 顺带存两主题截图

断言（两主题各一轮）：
  1. /workspace 可加载（.ws-top 出现）。
  2. cpSetTheme 后宿主 data-cp-theme 真的切档（写路径闭环）。
  3. light：顶栏为浅实底 + 深 ink；dark：保持深色渐变 + 白 ink（暗档零回归）。
  4. 左栏 .nav-rail 底随主题同向翻转（在收件箱页才断言）。
  5. App(iframe) 启用时，iframe 内 data-cp-theme 与宿主一致（set-theme 桥活着）。

缺 playwright / 实例不可达 / 无 token → SKIP exit 0（不污染回归信号）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verify_theme_contrast_ui import read_token  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

_PROBE_JS = """() => {
  const top = document.querySelector('.ws-top');
  const cs = top ? getComputedStyle(top) : null;
  const rail = document.querySelector('.nav-rail');
  const rcs = rail ? getComputedStyle(rail) : null;
  const f = document.getElementById('ws-cp-appframe');
  let ifrTheme = null;
  try {
    if (f && f.contentDocument && f.contentDocument.documentElement)
      ifrTheme = f.contentDocument.documentElement.getAttribute('data-cp-theme');
  } catch (e) { ifrTheme = 'ERR'; }
  return {
    host: document.documentElement.getAttribute('data-cp-theme'),
    topBg: cs ? (cs.backgroundImage + '|' + cs.backgroundColor) : null,
    topInk: cs ? cs.color : null,
    railBg: rcs ? (rcs.backgroundImage + '|' + rcs.backgroundColor) : null,
    ifrTheme,
  };
}"""


class Checker:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            self.fails.append(name)


def run(base: str, token: str, shots: str | None, headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        for theme in ("light", "dark"):
            print(f"== theme={theme} ==")
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            ctx.add_init_script("localStorage.setItem('ws_goal_tour_done','1');")
            ctx.request.post(base + "/login", form={"auth_token": token})
            page = ctx.new_page()
            page.goto(base + "/workspace", wait_until="domcontentloaded")
            try:
                page.wait_for_selector(".ws-top", timeout=15000)
            except Exception:
                ck.check(f"[{theme}] /workspace 加载", False, "ws-top 未出现（登录失败/模板异常）")
                ctx.close()
                continue
            ck.check(f"[{theme}] /workspace 加载", True)
            page.wait_for_timeout(1500)  # 异步渲染 + App(iframe) 加载余量
            # 主题必须走治理写路径 cpSetTheme（裸写 cp_theme 会被外观引擎按 night.mode
            # 盖回，且偏好经服务器漫游）；先记旧档，走查完恢复。
            prior = page.evaluate(
                "() => { try { return (window.WSAppearance && WSAppearance.get().night"
                " || {}).mode || 'auto'; } catch (e) { return 'auto'; } }"
            )
            page.evaluate(f"() => window.cpSetTheme && window.cpSetTheme('{theme}')")
            page.wait_for_timeout(600)  # 引擎应用 + iframe set-theme 桥余量

            row = page.evaluate(_PROBE_JS)
            ck.check(f"[{theme}] 宿主 data-cp-theme（cpSetTheme 写路径）",
                     row["host"] == theme, str(row["host"]))
            top_bg = row["topBg"] or ""
            top_ink = row["topInk"] or ""
            if theme == "light":
                ok_bg = "gradient" not in top_bg
                ok_ink = "15, 27, 45" in top_ink          # --chrome-ink 亮档 #0f1b2d
            else:
                ok_bg = "linear-gradient" in top_bg        # 暗档=历史深色渐变（零回归）
                ok_ink = "255, 255, 255" in top_ink
            ck.check(f"[{theme}] 顶栏底色随主题", ok_bg, top_bg[:110])
            ck.check(f"[{theme}] 顶栏 ink 随主题", ok_ink, top_ink)
            if row["railBg"] is None:
                print(f"  [SKIP] [{theme}] nav-rail 不在此页")
            else:
                rail_bg = row["railBg"]
                if theme == "light":
                    ck.check(f"[{theme}] 左栏底随主题", "gradient" not in rail_bg, rail_bg[:110])
                else:
                    ck.check(f"[{theme}] 左栏保持深色渐变", "linear-gradient" in rail_bg, rail_bg[:110])
            if row["ifrTheme"] and row["ifrTheme"] != "ERR":
                ck.check(f"[{theme}] App(iframe) 主题跟随宿主（set-theme 桥）",
                         row["ifrTheme"] == theme, str(row["ifrTheme"]))
            else:
                print(f"  [SKIP] [{theme}] App(iframe) 未启用/未加载（{row['ifrTheme']}）")

            if shots:
                out = Path(shots)
                out.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out / f"workspace_{theme}.png"), full_page=False)
                print(f"  [SHOT] {out / f'workspace_{theme}.png'}")
            # 恢复走查前档位（漫游偏好不留门禁痕迹），等 debounce POST 落地
            page.evaluate(f"() => window.cpSetTheme && window.cpSetTheme('{prior}')")
            page.wait_for_timeout(1200)
            ctx.close()
        browser.close()

    n = len(ck.fails)
    print(f"\n== 工作台 chrome 主题一致性: {ck.total - n}/{ck.total} PASS"
          + (f"  FAILED: {ck.fails}" if n else " =="))
    return 1 if n else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", default=None, help="截图输出目录（可选）")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SKIP: playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("SKIP: 读不到 web_admin.auth_token")
        return 0
    try:
        import urllib.request
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception as exc:
        print(f"SKIP: 实例不可达 {args.base} ({exc})")
        return 0
    return run(args.base, token, args.shots, args.headed)


if __name__ == "__main__":
    sys.exit(main())
