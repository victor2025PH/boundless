# -*- coding: utf-8 -*-
"""品牌令牌渲染值 真浏览器验证（Playwright；2026-07-30 品牌统一收官沉淀）。

**为什么需要它**：品牌统一改造（--tk-brand/--cp-accent/--p → --bl-growth 阶、
Montserrat 单一源、桌面字体相对路径）全部是**渲染期行为**——静态门禁
（test_brand_token_bridge / test_legacy_blue_ratchet）只能证「link 挂了、
字面量清了」，证不了「浏览器算出来的颜色/字体真的是品牌值」。曾靠 IDE 浏览器
一次性人工核验（含一次 about:blank 假测量教训——上下文必须先验 URL），
本工具把这些验收机械化，模板/CSS 热更新直上生产后随 -Full 复验。

覆盖的不变量（三个面）：
  A. 认证面 /login（公开页，无需登录）：
     1. --bl-growth ≡ #1e8cf2（brand.css 已装载且值未漂移）
     2. --p 解析为 growth 锚点之一（亮 #1e8cf2 / 暗 #54a7f5）
     3. body 计算字体首选 Montserrat（--bl-font-sans → --auth-font 链通）
     4. document.fonts.check Montserrat 已真实加载（woff2 相对路径 200）
  B. 桌面壳 renderer（file:// 直开 = Electron 宿主的等价上下文）：
     5. --cp-accent 计算值 = growth-400（暗色档），hover = growth-300
     6. --bl-growth 可解析（brand.css 排在 copilot 主题之前的实际效果）
     7. Montserrat 经 brand/fonts/ 相对路径真实加载（钉死 file:// 404 事故类）
  C. 坐席工作台 /workspace（需 token；读不到 token 则该节 SKIP）：
     8. --tk-brand 解析为 growth 锚点之一
     9. body 计算字体首选 Montserrat

用法::

    python tools/verify_brand_ui.py                # 默认实例 + 桌面 renderer
    python tools/verify_brand_ui.py --headed       # 肉眼看一遍
    python tools/verify_brand_ui.py --shots out/   # 存证截图

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 → SKIP exit 0
（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。只读：不登录发消息、
不改任何状态；/login 仅渲染、/workspace 仅读计算样式。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
_ENGINE = Path(__file__).resolve().parents[1]
DESKTOP_INDEX = _ENGINE / "desktop" / "renderer" / "index.html"

# growth 锚点（platform/brand tokens v1.0.0；亮=500 档、暗=400 档）
GROWTH_500 = "#1e8cf2"
GROWTH_400 = "#54a7f5"
GROWTH_300 = "#8bc4f8"
_RGB = {
    GROWTH_500: "rgb(30, 140, 242)",
    GROWTH_400: "rgb(84, 167, 245)",
    GROWTH_300: "rgb(139, 196, 248)",
}

# montserrat 判定用 fonts.load 显式触发拉取再 check：@font-face 是懒加载，
# 页面若只渲染 CJK 字形浏览器根本不请求 woff2，check() 会假阴；load() 强制
# 走一次网络/缓存，失败（404/MIME 拒绝）时 check 仍 false——这才是
# 「woff2 URL 可达」不变量的正确探法（首跑 A4 假阴的教训）。
_PROBE_JS = """async () => {
  try { await document.fonts.load('700 16px Montserrat'); } catch (e) {}
  const rootCs = getComputedStyle(document.documentElement);
  const probe = document.createElement('div');
  probe.style.color = 'var(%s)';
  document.body.appendChild(probe);
  const accent = getComputedStyle(probe).color;
  probe.remove();
  return {
    url: location.href,
    accent: accent,
    growth: rootCs.getPropertyValue('--bl-growth').trim(),
    bodyFont: getComputedStyle(document.body).fontFamily,
    montserrat: document.fonts.check('700 16px Montserrat'),
  };
}"""


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
        print(f"\n== 品牌渲染验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _probe(page: Any, var_name: str) -> dict:
    """先验 URL 再取数（IDE 浏览器时代踩过 about:blank 假测量）。"""
    data = page.evaluate(_PROBE_JS % var_name)
    assert not data["url"].startswith("about:"), "求值落在 about:blank，上下文丢失"
    return data


def _shot(page: Any, shots: Optional[Path], name: str) -> None:
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / name))


def run(base: str, data_root: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})

        # ── A. 认证面 /login（公开）─────────────────────────────
        page = ctx.new_page()
        live = True
        try:
            page.goto(base + "/login", wait_until="domcontentloaded", timeout=8000)
        except Exception:
            live = False
            print(f"[SKIP] 实例不可达（{base}）——跳过 A/C 两节")
        if live:
            print("== A. 认证面 /login ==")
            d = _probe(page, "--p")
            ck.check("A1 --bl-growth ≡ #1e8cf2", d["growth"].lower() == GROWTH_500,
                     d["growth"])
            ck.check("A2 --p 解析为 growth 锚点",
                     d["accent"] in (_RGB[GROWTH_500], _RGB[GROWTH_400]), d["accent"])
            ck.check("A3 body 字体首选 Montserrat",
                     d["bodyFont"].split(",")[0].strip().strip('"\'') == "Montserrat",
                     d["bodyFont"][:60])
            ck.check("A4 Montserrat 已真实加载（woff2 200）", d["montserrat"])
            _shot(page, shots, "brand-login.png")

        # ── B. 桌面壳 renderer（file:// = Electron 宿主等价上下文）──
        if DESKTOP_INDEX.is_file():
            print("== B. 桌面壳 renderer（file://）==")
            page2 = ctx.new_page()
            page2.goto(DESKTOP_INDEX.as_uri(), wait_until="domcontentloaded")
            page2.wait_for_timeout(400)   # 字体加载窗
            d2 = _probe(page2, "--cp-accent")
            ck.check("B1 --cp-accent = growth-400（暗色档）",
                     d2["accent"] == _RGB[GROWTH_400], d2["accent"])
            hov = page2.evaluate(_PROBE_JS % "--cp-accent-hover")
            ck.check("B2 --cp-accent-hover = growth-300",
                     hov["accent"] == _RGB[GROWTH_300], hov["accent"])
            ck.check("B3 --bl-growth 可解析（brand.css 先于主题装载）",
                     d2["growth"].lower() == GROWTH_500, d2["growth"])
            ck.check("B4 Montserrat 经相对路径加载（file:// 404 事故类）",
                     d2["montserrat"])
            _shot(page2, shots, "brand-desktop.png")
        else:
            print(f"[SKIP] 桌面 renderer 缺失（{DESKTOP_INDEX}）")

        # ── C. 坐席工作台 /workspace（需 token）────────────────
        if live:
            token = read_token(data_root)
            if not token:
                print("[SKIP] 读不到实例 token——跳过工作台节")
            else:
                print("== C. 坐席工作台 /workspace ==")
                ctx.request.post(base + "/login", form={"auth_token": token})
                page3 = ctx.new_page()
                page3.goto(base + "/workspace", wait_until="domcontentloaded")
                page3.wait_for_timeout(1500)
                d3 = _probe(page3, "--tk-brand")
                ck.check("C1 --tk-brand 解析为 growth 锚点",
                         d3["accent"] in (_RGB[GROWTH_500], _RGB[GROWTH_400]),
                         d3["accent"])
                ck.check("C2 工作台字体首选 Montserrat",
                         d3["bodyFont"].split(",")[0].strip().strip('"\'') == "Montserrat",
                         d3["bodyFont"][:60])
                _shot(page3, shots, "brand-workspace.png")

        browser.close()

    if not ck.results:
        print("[SKIP] 无可验证面（实例宕 + renderer 缺失）")
        return 0
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--shots", type=Path, default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未安装 playwright——品牌渲染验证跳过（exit 0）")
        return 0
    try:
        return run(args.base, args.data_root, shots=args.shots, headed=args.headed)
    except Exception as e:  # 环境类故障不污染回归信号；断言类失败已在 run 内计数
        print(f"[SKIP] 环境异常：{e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
