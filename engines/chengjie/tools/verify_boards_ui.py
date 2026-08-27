# -*- coding: utf-8 -*-
"""管理面六看板「侧栏 + 亮暗双主题可读性」真浏览器门禁（Playwright；2026-08-18 沉淀）。

**为什么需要它**：2026-08-18 管理面统一批把六个工作台壳页（用量与额度 / 概览 /
运营队列 / 坐席绩效 / AI 质量 / ROI）挂上 `_ws_sidebar.html` 左侧导航，并做了
整页 `--tk-*` 令牌归队（此前用量/绩效页是「硬编码浅卡底 × 会随暗色翻转的
--th-ink-* 文字」——暗色主题下近白字压白卡整页不可读，静态门禁全程绿灯）。
这两类回归**只有真浏览器能抓**：

  * 静态门禁能证「模板里写了 {% set ws_sidebar = true %}」，证不了侧栏真的
    渲染出来、当前页真的高亮（上下文变量断供时 Jinja 的 `or []` 守卫会让
    侧栏**静默塌成空**，不报错）；
  * 令牌写对了 ≠ 渲染对了：页面级硬编码底色 + 翻转文字令牌的组合，每个值
    单看都「合法」，只有把两个主题各渲染一遍、算实际对比度才现形；
  * 队列看板 2026-08-18 由「恒暗大屏」改为随主题翻转（老板拍板），退回
    恒暗（或半暗怪胎）没有任何静态信号——这里用「亮/暗两主题页面底色必须
    不同」钉死。

与 ``tools/verify_inbox_density.py`` / ``verify_care_ui.py`` 同族（同一实例 +
token 登录 + Playwright + 缺环境 SKIP exit 0 语义）。

**副作用：无。** 六页全部是只读看板（GET 渲染 + 只读 API），本工具不点击任何
写操作按钮；主题切换只发生在本工具的浏览器 context 的 localStorage 里，
不碰服务端与坐席的真实偏好。

用法::

    python tools/verify_boards_ui.py                 # 断言（门禁模式）
    python tools/verify_boards_ui.py --shots out/    # 顺带产出亮暗双主题截图
    python tools/verify_boards_ui.py --headed        # 肉眼看一遍

覆盖的不变量：
  1. 六页在 1440×900 下侧栏 `#ws-side` 渲染且宽度 ≥180px（塌成空/被挤没都算红）；
  2. 五个有导航项的页面（usage/queue/agent-perf/ai-quality/roi）侧栏当前页高亮
     指向本页 path（概览是顶栏 tab 领地、侧栏无对应项，刻意只验侧栏存在）；
  3. 亮/暗两主题下，每页采样标签/正文/表格类文字的实际对比度 ≥ 4.5:1
     （WCAG AA 小字号线；渐变描字 -webkit-text-fill-color:transparent 跳过，
     品牌强调链接/按钮不在采样清单——那是 3:1 大字号语义，采样清单只收
     「必须可读」的信息文字）；
  4. 每页页面根容器底色在亮/暗两主题下必须**不同**（防「恒暗/恒亮」回归——
     队列看板旧病，也是用量页「硬编码浅底」旧病的直接读数）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：Windows 先试 ::1 每连接吃 ~2s 回退
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

VIEWPORT = {"width": 1440, "height": 900}

# 对比度地板：WCAG AA 小字号线。仓库暗色令牌地板（muted@卡面 ≥8.0）更严，但那
# 管的是**令牌定义**；本工具量的是**页面实际组合**（含页面级样式覆盖），4.5 是
# 「信息文字必须可读」的兜底线——低于它一定是真缺陷，不会误伤设计余量。
CONTRAST_FLOOR = 4.5

# (名字, path, 根容器选择器, 侧栏应有当前页高亮)
PAGES: List[Tuple[str, str, str, bool]] = [
    ("usage", "/workspace/usage", ".ug-wrap", True),
    ("dash", "/workspace/dash", ".db-wrap", False),  # 概览=顶栏 tab 领地，侧栏无对应项
    ("queue", "/workspace/queue", ".qm-wrap", True),
    ("agent-perf", "/workspace/agent-perf", ".ap-wrap", True),
    ("ai-quality", "/workspace/ai-quality", ".aq-wrap", True),
    ("roi", "/workspace/roi", ".roi-wrap", True),
]

# 对比度采样清单：只收「信息文字」（标签/数值/表格/侧栏普通项）。刻意不收
# 品牌强调元素（返回链/active 项/主按钮/db-fold-btn）——品牌蓝对浅底 ≈3.3:1
# 是大字号/非正文语义的刻意设计，收进来全是假阳性。
_SAMPLE_SELECTORS = (
    "h2, h3, "
    ".db-lbl, .db-grp-h, .db-exec-sub, "
    ".ug-card .l, .ug-card .v, .uq-muted, .uq-kv, .uq-big, .uq-tbl th, "
    ".ap-kpi .lbl, .ap-kpi .val, .ap-table th, .ap-table td, "
    ".qm-kpi .lbl, .qm-kpi .val, .qm-ts, .qm-stat .sk, .qm-stat .sv, .qm-table th, "
    ".aq-card .l, .aq-card .v, .roi-card .l, .roi-card .v, "
    ".wsb-sec-lbl, .wsb-nav a:not(.active) span"
)

_CONTRAST_JS = """(a) => {
  function parse(c){ const m=String(c||'').match(/rgba?\\(([^)]+)\\)/); if(!m) return null;
    const p=m[1].split(',').map(parseFloat); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; }
  function blend(fg,bg){ const a=fg.a==null?1:fg.a; if(a>=1) return fg;
    return {r:fg.r*a+bg.r*(1-a), g:fg.g*a+bg.g*(1-a), b:fg.b*a+bg.b*(1-a), a:1}; }
  function lum(c){ const f=(v)=>{ v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); };
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); }
  function ratio(f,b){ const l1=lum(f), l2=lum(b); const hi=Math.max(l1,l2), lo=Math.min(l1,l2);
    return (hi+0.05)/(lo+0.05); }
  function effBg(el){ let n=el;
    while(n && n!==document.documentElement){
      const bg=parse(getComputedStyle(n).backgroundColor);
      if(bg && bg.a>0.9) return bg;
      n=n.parentElement;
    }
    const b=parse(getComputedStyle(document.body).backgroundColor);
    return (b && b.a>0) ? b : {r:255,g:255,b:255,a:1};
  }
  const out=[]; let n=0;
  document.querySelectorAll(a.sels).forEach((el)=>{
    if(n>=200) return;                        // 采样上限，防超大表拖慢门禁
    if(!(el instanceof HTMLElement)) return;
    if(el.offsetParent===null) return;        // 不可见（display:none 链）
    const txt=(el.textContent||'').trim();
    if(!txt) return;
    const cs=getComputedStyle(el);
    if(cs.webkitTextFillColor==='rgba(0, 0, 0, 0)'||cs.webkitTextFillColor==='transparent') return; // 渐变描字
    const fgRaw=parse(cs.color); if(!fgRaw) return;
    const bg=effBg(el);
    const fg=blend(fgRaw,bg);
    n++;
    out.push({r:Math.round(ratio(fg,bg)*100)/100, tag:el.tagName.toLowerCase(),
              cls:String(el.className||'').slice(0,40), txt:txt.slice(0,16)});
  });
  out.sort((x,y)=>x.r-y.r);
  return {sampled:n, worst:out.slice(0,4)};
}"""

_SIDEBAR_JS = """(path) => {
  const side=document.getElementById('ws-side');
  const active=document.querySelector('.wsb-nav a.active');
  return {
    present: !!side,
    width: side ? side.offsetWidth : 0,
    items: side ? side.querySelectorAll('.wsb-nav a').length : 0,
    active_href: active ? active.getAttribute('href') : '',
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
        except Exception:  # noqa: BLE001
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
        print(f"\n== 六看板侧栏/主题验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def _fmt_worst(worst: List[Dict[str, Any]]) -> str:
    return "; ".join(f"{w['tag']}.{w['cls']}[{w['txt']}]={w['r']}" for w in worst[:3])


def run(base: str, token: str, *, shots: Optional[Path] = None, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    wrap_bg: Dict[str, Dict[str, str]] = {}  # page -> {theme: bgColor}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport=VIEWPORT)
            # 主题控制走**窗口级 URL 主题钉** ?theme=（workspace_base 头部解析为
            # window.__cpThemePin）而非写 localStorage cp_theme——首跑实锤：实例侧
            # 夜间治理值会盖过 localStorage（两个 context 全渲染成暗色），而 URL 钉
            # 只作用于本窗口、不落盘、不碰坐席真实偏好，正是给自动化用的档。
            # ui_mode=full cookie：四看板按设计只在完整模式菜单有项（简洁模式深链
            # 进来侧栏无该项=无高亮属预期），高亮断言以管理员完整菜单视角为准。
            ctx.add_cookies([{"name": "ui_mode", "value": "full", "url": base}])
            ctx.request.post(base + "/login", form={"auth_token": token})
            page = ctx.new_page()
            # 未捕获 JS 异常收集：热更新模板的 ReferenceError 类缺陷（哑按钮门禁的
            # 运行时盲区——innerHTML 动态拼出的调用链）只有真浏览器执行才现形。
            js_errors: List[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)[:120]))
            for name, path, wrap_sel, has_nav in PAGES:
                label = f"[{theme}] {name}"
                js_errors.clear()
                try:
                    page.goto(base + path + "?theme=" + theme,
                              wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(900)
                except Exception as e:  # noqa: BLE001
                    ck.check(f"{label} 页面可达", False, str(e)[:70])
                    continue
                ck.check(f"{label} 零未捕获 JS 异常", not js_errors,
                         ("; ".join(js_errors[:2]) if js_errors else ""))
                snap = page.evaluate(_SIDEBAR_JS, path)
                ck.check(f"{label} 侧栏渲染且未塌缩",
                         snap["present"] and snap["width"] >= 180 and snap["items"] >= 5,
                         f"width={snap['width']}px items={snap['items']}")
                if has_nav:
                    ck.check(f"{label} 侧栏当前页高亮",
                             snap["active_href"] == path,
                             f"active={snap['active_href'] or '(none)'}")
                bg = page.evaluate(
                    "(sel)=>{const el=document.querySelector(sel);"
                    "return el?getComputedStyle(el).backgroundColor:'';}", wrap_sel)
                wrap_bg.setdefault(name, {})[theme] = bg or ""
                cres = page.evaluate(_CONTRAST_JS, {"sels": _SAMPLE_SELECTORS})
                if not cres["sampled"]:
                    ck.skip(f"{label} 文字对比度", "无可采样文字（空数据态）")
                else:
                    worst = cres["worst"][0]["r"] if cres["worst"] else 99
                    ck.check(f"{label} 文字对比度 ≥{CONTRAST_FLOOR}",
                             worst >= CONTRAST_FLOOR,
                             f"n={cres['sampled']} worst: {_fmt_worst(cres['worst'])}")
                if shots:
                    page.screenshot(path=str(shots / f"{name}_{theme}.png"), full_page=False)
            ctx.close()

        # ── 窄屏档（390×844，暗色）：≤768px 侧栏应整体让位（_ws_sidebar 媒体查询
        # display:none），页面脚本零异常，且无横向溢出。溢出断言原计划「只记录不设卡」
        # （怕数据表在 390px 天然溢出=假阳性），2026-08-18 首跑实测六页 scrollWidth
        # 全部恰好 390（卡片 overflow 把宽表收住了）→ 升级为硬断言，防未来回归。
        nctx = browser.new_context(viewport={"width": 390, "height": 844})
        nctx.add_cookies([{"name": "ui_mode", "value": "full", "url": base}])
        nctx.request.post(base + "/login", form={"auth_token": token})
        npg = nctx.new_page()
        n_errors: List[str] = []
        npg.on("pageerror", lambda e: n_errors.append(str(e)[:120]))
        for name, path, wrap_sel, _nav in PAGES:
            label = f"[narrow] {name}"
            n_errors.clear()
            try:
                npg.goto(base + path + "?theme=dark",
                         wait_until="domcontentloaded", timeout=30000)
                npg.wait_for_timeout(700)
            except Exception as e:  # noqa: BLE001
                ck.check(f"{label} 页面可达", False, str(e)[:70])
                continue
            snap = npg.evaluate(
                "(sel)=>{const side=document.getElementById('ws-side');"
                "const wrap=document.querySelector(sel);"
                "return {side_vis: !!(side && side.offsetWidth>0), wrap: !!wrap,"
                " overflow: Math.max(document.documentElement.scrollWidth,"
                "                    document.body.scrollWidth)};}", wrap_sel)
            ck.check(f"{label} 侧栏让位（≤768px 隐藏）",
                     snap["wrap"] and not snap["side_vis"])
            ck.check(f"{label} 无横向溢出",
                     snap["overflow"] <= 392,
                     f"scrollWidth={snap['overflow']}")
            ck.check(f"{label} 零未捕获 JS 异常", not n_errors,
                     ("; ".join(n_errors[:2]) if n_errors else ""))
        nctx.close()
        browser.close()

    # 跨主题：页面根容器底色必须随主题变化（恒暗/恒亮回归的直接读数）
    for name, _path, _sel, _nav in PAGES:
        pair = wrap_bg.get(name, {})
        lite, dark = pair.get("light", ""), pair.get("dark", "")
        if not lite or not dark:
            ck.skip(f"{name} 主题跟随", "任一主题下根容器缺失")
            continue
        ck.check(f"{name} 页面底色随主题翻转", lite != dark, f"light={lite} dark={dark}")
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="管理面六看板侧栏/亮暗主题门禁（只读，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK：文案里的 ≥/emoji 会 UnicodeEncodeError（同族工具同坑）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

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
        pass  # 任何 HTTP 响应都代表实例活着
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 0
    shots = None
    if args.shots:
        shots = Path(args.shots)
        shots.mkdir(parents=True, exist_ok=True)
    print(f"== 0. 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}，亮/暗双主题）==")
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
