# -*- coding: utf-8 -*-
"""白标换肤压力测试（2026-08-23 P6）：验证「改品牌 token 就能全站换肤」是否属实。

原理
====
不动任何生产文件：页面加载后**运行时覆写**品牌主色家族（``--bl-growth`` 全阶 +
ring 渐变，等价于贴牌客户改 tokens.json 的效果），再对全 DOM 做**计算样式扫描**——
凡是仍然渲染为旧品牌蓝（#1e8cf2 家族）的元素，就是绕过 token 链的硬编码泄漏，
换肤时会以「新品牌界面里嵌着旧蓝色块」的形态出货。

判定
====
- 覆写后计算样式（color/background/border/outline/fill/stroke）仍 ≈ 旧品牌蓝
  RGB（500/400/600/700 四阶，±4/通道容差）＝泄漏；
- ``_ALLOWED_FIXED``（frontend-theme 规则明文允许恒定的：品牌 logo 渐变、平台
  品牌色、出站气泡）按选择器豁免，登记必附原因；
- 计算样式返回的是已解析 rgb()，Telegram 蓝 (36,161,222)/(0,136,204) 等平台色
  在容差外，天然不误报。

用法
====
    python tools/verify_brand_reskin.py            # 扫描并报告（有泄漏退出 1）
    python tools/verify_brand_reskin.py --shots out # 附换肤后截图（肉眼复核）

家族约定：缺 playwright / 实例不可达 → SKIP exit 0（可挂 sweep 不污染信号）。
**刻意暂不进 gate_sweep**：首轮必有存量泄漏，清零或登记完毕后再挂（防长期红）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# 换肤试色：玫红家族（避开语义色 红/黄/绿 与三系光环色 蓝/紫/橙 的色相）
_TEST = {
    "--bl-growth": "#e91e63",
    "--bl-growth-50": "#fdeef4", "--bl-growth-100": "#fbd5e3",
    "--bl-growth-200": "#f8abc8", "--bl-growth-300": "#f47ba8",
    "--bl-growth-400": "#f06292", "--bl-growth-500": "#e91e63",
    "--bl-growth-600": "#c2185b", "--bl-growth-700": "#a3134c",
    "--bl-growth-800": "#850f3e", "--bl-growth-900": "#6b0c32",
    "--bl-growth-ring": "linear-gradient(135deg,#c2185b 0%,#f06292 100%)",
}

# 旧品牌蓝（覆写后仍出现＝泄漏）：growth 500/400/600/700
_LEAK_RGB = [(30, 140, 242), (84, 167, 245), (13, 118, 217), (11, 100, 183)]
_TOL = 4

# 规则明文允许恒定的区域（frontend-theme.mdc：出站气泡/平台品牌色/品牌 logo 渐变）。
# 登记必附原因；只豁免这些选择器的**自身与后代**。
_ALLOWED_FIXED = {
    ".brand-lockup": "品牌 logo 渐变按规则保留固定色",
    "#nav-rail": "平台品牌图标区（TG/LINE 等官方色按规则恒定）",
    ".mob-plat-bar": "移动底栏平台图标区（同上）",
}

_PAGES = [
    ("login", "/login", 2500),
    ("workspace", "/workspace", 7000),
    ("ops", "/admin/ops", 8000),
    ("personas", "/personas", 5000),
    ("knowledge", "/knowledge", 4500),
    ("reply_settings", "/reply-settings", 5000),
    ("membership", "/membership", 4500),
    ("cases", "/cases", 4500),
    ("care", "/care-schedule", 4500),
]

_SCAN_JS = """
(args) => {
  const leaks = [];
  const tol = args.tol, targets = args.targets;
  const allowSel = args.allow;
  const near = (r,g,b) => targets.some(t =>
    Math.abs(r-t[0])<=tol && Math.abs(g-t[1])<=tol && Math.abs(b-t[2])<=tol);
  const parse = (s) => {
    const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/.exec(s||'');
    if(!m) return null;
    const a = m[4]===undefined?1:parseFloat(m[4]);
    if(a < 0.02) return null;
    return [ +m[1], +m[2], +m[3] ];
  };
  const path = (el) => {
    const bits = [];
    let n = el;
    for(let i=0;i<4 && n && n.nodeType===1;i++){
      let b = n.tagName.toLowerCase();
      if(n.id) b += '#'+n.id;
      else if(n.classList && n.classList.length) b += '.'+Array.from(n.classList).slice(0,2).join('.');
      bits.unshift(b); n = n.parentElement;
    }
    return bits.join('>');
  };
  const allowed = (el) => allowSel.some(sel => { try{ return el.closest(sel); }catch(_){ return false; } });
  const PROPS = ['color','backgroundColor','borderTopColor','borderBottomColor',
                 'borderLeftColor','borderRightColor','outlineColor','fill','stroke'];
  document.querySelectorAll('*').forEach(el => {
    const r = el.getBoundingClientRect();
    if(r.width < 2 || r.height < 2) return;
    const cs = getComputedStyle(el);
    if(cs.display === 'none' || cs.visibility === 'hidden') return;
    for(const p of PROPS){
      const rgb = parse(cs[p]);
      if(rgb && near(rgb[0], rgb[1], rgb[2])){
        if(allowed(el)) continue;
        leaks.push({ prop:p, val:cs[p], path:path(el) });
        break;
      }
    }
  });
  // 聚合：prop+val+路径尾段 分组计数
  const groups = {};
  for(const L of leaks){
    const key = L.prop + '|' + L.val + '|' + L.path.split('>').pop();
    (groups[key] = groups[key] || {prop:L.prop, val:L.val, n:0, sample:L.path}).n++;
  }
  return Object.values(groups).sort((a,b)=>b.n-a.n);
}
"""


def read_token(data_root: str) -> str:
    import yaml
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(data_root) / "config" / name
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


def main() -> int:
    ap = argparse.ArgumentParser(description="白标换肤压力测试（运行时覆写品牌 token + 泄漏扫描，只读）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--shots", default="", help="截图目录（留空不截）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] playwright 不可用")
        return 0
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:
        print(f"[SKIP] 实例不可达：{str(e)[:60]}")
        return 0
    tok = read_token(args.data_root)
    if not tok:
        print("[SKIP] 读不到 auth_token")
        return 0

    override_css = ":root{" + ";".join(
        f"{k}:{v} !important" for k, v in _TEST.items()) + ";}"
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme="light")
        ctx.request.post(args.base + "/login", form={"auth_token": tok})
        pg = ctx.new_page()
        pg.add_init_script(
            "try{localStorage.setItem('_onboard_shown_simple','1');"
            "localStorage.setItem('tour_done','1')}catch(e){}")
        for name, path_, wait in _PAGES:
            try:
                pg.goto(args.base + path_, wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(wait)
                pg.add_style_tag(content=override_css)
                pg.wait_for_timeout(400)
                groups = pg.evaluate(_SCAN_JS, {
                    "tol": _TOL,
                    "targets": [list(t) for t in _LEAK_RGB],
                    "allow": list(_ALLOWED_FIXED),
                })
                n = sum(g["n"] for g in groups)
                total += n
                flag = "LEAK" if n else "ok"
                print(f"[{flag:>4}] {name:<14} {n} 处")
                for g in groups[:8]:
                    print(f"        {g['n']:>3}x {g['prop']:<16} {g['val']:<22} e.g. {g['sample']}")
                if shots:
                    pg.screenshot(path=str(shots / f"reskin_{name}.png"))
            except Exception as e:
                print(f"[ERR ] {name}: {str(e)[:100]}")
        ctx.close()
        browser.close()
    print(f"== 换肤泄漏总计 {total} 处（豁免区外仍渲染旧品牌蓝）==")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
