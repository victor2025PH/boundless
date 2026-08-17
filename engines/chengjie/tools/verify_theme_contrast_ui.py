# -*- coding: utf-8 -*-
"""白天/暗色主题渲染级对比度验证（Playwright；2026-08-04 白天模式可读性收口配套）。

**为什么静态门禁不够**：tests/test_theme_contrast_gate.py 钉的是"调色板值"层面的
契约；但坐席真正看到的是**渲染结果**——级联覆盖、组件自带 opacity、页面漏挂
token CSS、某条更特异的规则把文字色打回硬编码……这些只有真浏览器的
getComputedStyle 能证。且模板/CSS 热更新直上生产，改错色当场生效。

与 tools/verify_care_ui.py 同族（同实例 + token 登录 + Playwright），**只读**：
只加载页面读计算样式，不点任何会写状态/烧 LLM 的按钮。

用法::

    python tools/verify_theme_contrast_ui.py                 # 默认打 /knowledge
    python tools/verify_theme_contrast_ui.py --shots out/    # 顺带存两主题截图
    python tools/verify_theme_contrast_ui.py --headed        # 肉眼看一遍

覆盖的不变量（两主题各跑一轮）：
  1. 知识库页可加载（登录 → 卡片渲染或空态，JS 就绪）。
  2. 高频文字件的**渲染实效对比度** ≥ 4.5（WCAG AA 正文）：
     非激活 tab / 触发词 chip / 分类徽章 / 卡片标题 / 场景说明 / meta 行 /
     次按钮 / 统计条。背景=沿祖先链合成的实效底色，含元素级 opacity。
  3. 存在停用条目时，卡片必须带「已停用」显式徽章（opacity 淡化不算状态标识）。
  4. i18n 无裸键：页面可见文本不含 "kb2_"。

缺 playwright / 实例不可达 / 无 token → SKIP exit 0（不污染回归信号）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

_TEXT_AA = 4.5

# 每页抽样：(path, 就绪 selector, [(selector, 说明, 阈值)...], 是否跑知识库专属检查)
# selector 找不到/不可见 → SKIP 不 FAIL（简洁/完整模式、空库等形态差异合法）。
_PAGES: List[dict] = [
    dict(
        path="/knowledge",
        ready="#entry-grid .entry-card, #entry-grid .empty-state",
        kb_checks=True,
        samples=[
            # 刻意排除停用卡（.65 opacity 属状态淡化，另有徽章契约）
            (".kb-tab:not(.active)", "tab 非激活", _TEXT_AA),
            (".kb-tab.active", "tab 激活", _TEXT_AA),
            (".entry-card:not(.disabled) .tag-chip", "触发词 chip", _TEXT_AA),
            (".entry-card:not(.disabled) .entry-cat-badge", "分类徽章", _TEXT_AA),
            (".entry-card:not(.disabled) .entry-title", "卡片标题", _TEXT_AA),
            (".entry-card:not(.disabled) .entry-scenario", "场景说明", _TEXT_AA),
            (".entry-card:not(.disabled) .entry-meta", "meta 行", _TEXT_AA),
            (".entry-card:not(.disabled) .btn-s.btn-xs", "次按钮", _TEXT_AA),
            ("#kb-stat-bar .kb-stat", "统计条", _TEXT_AA),
        ],
    ),
    dict(
        path="/rpa-overview",
        ready="body",
        kb_checks=False,
        samples=[
            # 2026-08-04 P1.5 修的现存缺陷：靛 300 浅字配 12-18% 浅靛晕（亮色 1.9:1）
            ("span[style*='th-ink-indigo5']", "靛晕 chips（排队偏好/意图域）", _TEXT_AA),
            ("span[style*='th-ink-red6'], strong[style*='th-ink-ghred']", "红色状态字", _TEXT_AA),
            (".badge", "通用徽章", _TEXT_AA),
        ],
    ),
    dict(
        path="/care-schedule",
        ready="#cs-steward, #cs-hero",
        kb_checks=False,
        samples=[
            ("#cs-st-chip-tx", "管家状态 chip", _TEXT_AA),
            ("#cs-st-brief", "一句话方案", _TEXT_AA),
            (".btn-s", "次按钮", _TEXT_AA),
        ],
    ),
]


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


# ── 颜色数学（与 tests/test_theme_contrast_gate.py 同口径；工具独立可跑故内联） ──

def _parse_rgb(s: str) -> Tuple[float, float, float, float]:
    s = s.strip()
    # Chromium 把 `color-mix(in srgb, X N%, transparent)`（本仓半透明底的既定写法，
    # 见 rpa_overview 头部约定）算成 `color(srgb r g b / a)`，分量是 0..1 而非 0..255。
    # 缺这一支会把「用了正确写法的元素」判成 unparsable 红，属探针缺口非产品缺陷。
    m = re.match(r"color\(\s*srgb\s+([^)]+)\)", s)
    if m:
        body = m.group(1).replace("/", " ")
        nums = [float(x) for x in body.split()]
        r, g, b = (nums[i] * 255.0 for i in range(3))
        a = nums[3] if len(nums) > 3 else 1.0
        return r, g, b, a
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if not m:
        raise ValueError(f"unparsable color: {s!r}")
    parts = [p.strip() for p in m.group(1).split(",")]
    r, g, b = (float(parts[i]) for i in range(3))
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a


def _lum(rgb: Tuple[float, float, float]) -> float:
    def f(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])


def _effective_contrast(color: str, bg_chain: List[str], opacity: float) -> float:
    """bg_chain=祖先底色 从近到远；末端必须近不透明（body 有实底）。"""
    base: Tuple[float, float, float] | None = None
    for spec in reversed(bg_chain):
        r, g, b, a = _parse_rgb(spec)
        if base is None:
            base = (r, g, b)  # 最远端当实底（body computed 恒 rgb()）
            continue
        base = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), base))  # type: ignore
    if base is None:
        base = (255.0, 255.0, 255.0)
    r, g, b, a = _parse_rgb(color)
    a *= max(0.0, min(1.0, opacity))
    fg = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), base))  # type: ignore
    l1, l2 = _lum(fg), _lum(base)  # type: ignore[arg-type]
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


_SAMPLE_JS = """(selectors) => selectors.map(sel => {
  // 取第一个**可见**匹配：简洁模式大量 pro-only 隐藏件，首个匹配常是 display:none
  const el = [...document.querySelectorAll(sel)].find(e => e.offsetParent !== null);
  if (!el) return { sel, found: false };
  const cs = getComputedStyle(el);
  const bgs = []; let op = parseFloat(cs.opacity) || 1; let n = el.parentElement;
  let cur = el;
  while (cur) {
    const s = cur === el ? cs : getComputedStyle(cur);
    if (cur !== el) { const o = parseFloat(s.opacity); if (!isNaN(o)) op *= o; }
    const bg = s.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') bgs.push(bg);
    cur = cur.parentElement;
  }
  return { sel, found: true, color: cs.color, bgs, opacity: op };
})"""


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
        print(f"\n== 主题对比度渲染验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _run_theme(ck: Checker, p, base: str, token: str, theme: str,
               headed: bool, shots: str | None) -> None:
    print(f"== theme={theme} ==")
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(f"localStorage.setItem('theme','{theme}');"
                        "localStorage.setItem('_guide_kb','1');")
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    for cfg in _PAGES:
        path = cfg["path"]
        tag = f"{theme}:{path}"
        page.goto(base + path, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(cfg["ready"], timeout=15000)
        except Exception:
            ck.check(f"[{tag}] 页面加载", False, "就绪元素未出现（登录失败/模板异常）")
            continue
        ck.check(f"[{tag}] 页面加载", True)
        page.wait_for_timeout(700)  # 首屏异步渲染余量（chips/看板行等）
        # 新会话首访引导弹窗会遮挡截图/命中，移除（与 verify_care_ui 同处理）
        page.evaluate("() => { const m = document.getElementById('onboard-modal');"
                      " if (m) m.remove(); }")

        samples = cfg["samples"]
        rows = page.evaluate(_SAMPLE_JS, [s for s, _d, _t in samples])
        for (sel, desc, need), row in zip(samples, rows):
            if not row.get("found"):
                print(f"  [SKIP] [{tag}] {desc}（页面无此元素：{sel}）")
                continue
            try:
                got = _effective_contrast(row["color"], row["bgs"], row["opacity"])
            except ValueError as exc:
                ck.check(f"[{tag}] {desc} 可解析", False, str(exc))
                continue
            ck.check(f"[{tag}] {desc} ≥ {need}", got >= need,
                     f"{got:.2f}  ({row['color']} op={row['opacity']:.2f})")

        if cfg.get("kb_checks"):
            # 停用条目必须有显式徽章（存在停用卡才断言）
            n_disabled = page.evaluate(
                "() => document.querySelectorAll('.entry-card.disabled').length")
            if n_disabled:
                n_badged = page.evaluate(
                    "() => [...document.querySelectorAll('.entry-card.disabled')]"
                    ".filter(c => c.querySelector('.badge.b-n')).length")
                ck.check(f"[{tag}] 停用卡带「已停用」徽章", n_badged == n_disabled,
                         f"{n_badged}/{n_disabled}")
            raw = page.evaluate("() => document.body.innerText.includes('kb2_')")
            ck.check(f"[{tag}] 无 kb2_ 裸键", not raw)

        if shots:
            out = Path(shots)
            out.mkdir(parents=True, exist_ok=True)
            name = path.strip("/").replace("/", "_") or "home"
            page.screenshot(path=str(out / f"{name}_{theme}.png"), full_page=False)
            print(f"  [SHOT] {out / f'{name}_{theme}.png'}")
    browser.close()


def run(base: str, token: str, *, headed: bool = False, shots: str | None = None) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        for theme in ("light", "dark"):
            _run_theme(ck, p, base, token, theme, headed, shots)
    return ck.summary()


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
    return run(args.base, token, headed=args.headed, shots=args.shots)


if __name__ == "__main__":
    sys.exit(main())
