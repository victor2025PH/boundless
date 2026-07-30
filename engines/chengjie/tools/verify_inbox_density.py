# -*- coding: utf-8 -*-
"""坐席工作台「会话面板密度」真浏览器门禁（Playwright；2026-07-30 沉淀）。

**为什么需要它**：坐席每天盯的是 300px 宽的会话列表，而列表**上方**的 chrome
（保存视图栏 / 列表头 / 标签 strip）每多一行就少露一条会话。这类退化的特点是
**没人会发现**——不报错、不变红、截图看着也「正常」，只是可视会话少了一条。
2026-07-30 两次降噪都是靠**临时脚本手工量**才找出来的：

  * `#saved-views-bar` 在 0 个已保存视图时仍渲染按钮+空态提示，占 32px，
    且它在 `.list-header` 之外**不参与滚动折叠**；
  * `#filter-summary` 在「只有一个主 tab 激活」时纯复述上方 tab 行，占 25px。

同一段「登录 + 量高度」样板当天被手写了四遍。本工具把它固化成**有回归守护的
指标**：以后谁往列表头加一行，门禁直接点名，而不是等下一个人再手写脚本量一遍。

与 ``tools/verify_account_rail_ui.py`` / ``verify_multiwin_ui.py`` 同族
（同一实例 + token 登录 + Playwright + SKIP exit 0 语义）。

**副作用：无。** 只操作筛选层 UI 与滚动；量 composer 需要打开一个会话，故
**刻意只点 `.conv-item:not(.has-unread)`（已读会话）**——打开已读会话不改变任何
已读状态；全列表都是未读时直接跳过 composer 检查，绝不为了测量把客户标成已读。

用法::

    python tools/verify_inbox_density.py              # 断言（门禁模式）
    python tools/verify_inbox_density.py --baseline   # 只打印实测值，不断言（重校准天花板用）
    python tools/verify_inbox_density.py --headed     # 肉眼看一遍

覆盖的不变量：
  1. `#saved-views-bar`：0 个已保存视图 → `display:none`（不占位）。
  2. `#filter-summary`：单一主 tab（超时/未读/我的）激活 → 不渲染（tab 行已高亮，
     「全部」tab 即清除入口，再显示一行等于复述）。
  3. `#filter-summary`：主 tab + 非默认排序（2 维）→ **必须**渲染且含清空入口
     （tab 行清不掉排序 —— 这条防「降噪降过头把能力也砍了」）。
  4. `#filter-summary`：更多菜单档位（待回复）→ **必须**渲染
     （它折叠在菜单里，chip 是唯一的在屏指示）。
  5. 滚动折叠：滚动列表 → `.list-header.compact`，且 `#saved-views-bar` 同步带
     `compact`（它是兄弟节点，不会被列表头的 class 带动，曾漏同步）。
  6. 高度预算 ratchet：列表头（无筛选态）与 composer 各有天花板，只许降不许升。
  7. 「可见但空」扫描：列表面板与 composer 内不得出现「占 ≥10px 却没有任何内容」
     的元素 —— 上面两条真实缺陷都是这个形态。

⚠️ 扫描器的 shadow DOM 盲区（2026-07-30 踩过，别再踩）：`textContent` /
`innerHTML` **穿不透 shadow root**，于是 `<cp-next-actions>` 这类 Web Component
会被误判成「185px 的空块」（它的 2504 字符内容都在 shadow root 里）。故检测器
遇到带 `shadowRoot` 的元素**直接放行**——组件自己管空态（实测 cp-voice /
cp-goal 无数据时确实塌成 0px），这个启发式无权替它判断。

天花板维护：实测值低于天花板较多时把数字改小（对齐仓内
`_INLINE_COLOR_CEILINGS` 的 ratchet 文化）；`--baseline` 直接给当前实测值。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 → SKIP exit 0
（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://localhost:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# 量测视口固定，否则高度不可比（换宽度会改变筛选 tab 的换行行数）
VIEWPORT = {"width": 1440, "height": 900}

# ── 高度预算天花板（只许降不许升）─────────────────────────────────────
# 2026-07-30 实测 @1440x900：列表头（无筛选）180px、composer 139px。
# 留 ~15px 余量吸收字体/平台差异，但小于「新增一行」的代价（约 25px）——
# 于是「有人加了一行」必然破线，而「字体差 1px」不会误报。
LIST_HEADER_MAX = 196
COMPOSER_MAX = 152

# 「可见但空」扫描的良性命中登记：装饰件 / 分隔线 / 拖拽把手本就无内容。
# 新增豁免必须写清原因（对齐仓内 _ACCEPTED_DUP_IDS 的登记文化）。
WASTE_ALLOWLIST = {
    "rt-divider": "工具条分隔线，纯视觉 1px 竖线",
    "conv-acct-accent": "会话行左侧账号色条，纯视觉 2px",
    "ava-img": "头像图片容器，背景图渲染（无子节点）",
    "ws-sidebar-resize": "侧栏拖拽把手，靠 cursor/hover 提供 affordance",
    "cp-card-bd": "副驾卡体，内容由 Web Component 在 shadow root 内渲染",
}

# 共享检测器：可见、占 >=10px、却没有任何内容的元素。
# 见模块 docstring 的 shadow DOM 盲区说明——带 shadowRoot 一律放行。
_WASTE_JS = """(a) => {
  // 注意：Playwright 的 evaluate 只传**一个** arg —— 首版写成两个形参导致
  // allow=undefined，`allow.some` 直接 TypeError。统一走单对象解构。
  const root = document.querySelector(a.sel);
  const allow = a.allow || [];
  if (!root) return {absent: true, items: []};
  const items = [];
  const seen = new Set();
  root.querySelectorAll('*').forEach(el => {
    // 只看 HTML 元素：SVG 子元素（path/polyline/circle…）没有 offsetHeight
    // （得到 undefined，而 `undefined < 10` 为 false 会穿过早退），且它们的
    // className 是 SVGAnimatedString 对象、toString 出 "[object SVGAnimatedString]"。
    // 首版漏了这条 → 图标内部的 <path> 被当成「占位却无内容」误报。
    if (!(el instanceof HTMLElement)) return;
    const h = el.offsetHeight;
    if (!(h >= 10)) return;              // 同时挡住 undefined / NaN
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if ((el.textContent || '').trim()) return;
    // 自身即控件（input/textarea 无 textContent 但显然有内容）
    if (/^(input|textarea|select|img|svg|canvas|video|button)$/i.test(el.tagName)) return;
    if (el.querySelector('img,svg,input,textarea,select,canvas,video,button')) return;
    // Web Component：内容在 shadow root，本启发式看不见 → 放行（组件自管空态）
    if (el.shadowRoot) return;
    if (el.children.length > 2) return;          // 容器骨架，非叶子浪费
    const cls = (el.className || '').toString();
    if (allow.some(a => cls.indexOf(a) >= 0 || el.id === a)) return;
    const key = (el.id || '') + '|' + cls.slice(0, 40);
    if (seen.has(key)) return;
    seen.add(key);
    items.push({id: el.id || '', cls: cls.slice(0, 44), tag: el.tagName.toLowerCase(),
                h: h, w: el.offsetWidth});
  });
  return {absent: false, items: items};
}"""

_SNAP_JS = """() => {
  const q = (id) => document.getElementById(id);
  const fs = q('filter-summary'), hdr = q('list-header'), sv = q('saved-views-bar');
  return {
    fs_h: fs ? fs.offsetHeight : null,
    fs_txt: fs ? (fs.textContent || '').replace(/\\s+/g, ' ').trim() : '',
    // 「清空筛选」入口按元素判定，不靠文案字符串（文案会 i18n 变化，且 ✕ 在 GBK 控制台不可打印）
    fs_clear: !!q('fs-clear-all'),
    fs_chips: fs ? fs.querySelectorAll('.fs-chip').length : 0,
    hdr_h: hdr ? hdr.offsetHeight : null,
    hdr_compact: hdr ? hdr.classList.contains('compact') : null,
    sv_h: sv ? sv.offsetHeight : null,
    sv_empty: sv ? sv.classList.contains('is-empty') : null,
    sv_compact: sv ? sv.classList.contains('compact') : null,
  };
}"""

_COMPOSER_JS = """() => {
  const wrap = document.querySelector('.reply-bar-wrap');
  if (!wrap) return {absent: true};
  return {
    absent: false,
    total: wrap.offsetHeight,
    rows: [...wrap.children].map(el => ({
      id: el.id || '', h: el.offsetHeight,
      disp: getComputedStyle(el).display,
    })),
  };
}"""


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。

    与同族 verify_*_ui.py 各自持有一份（工具保持自包含、可单独拷走运行）。
    """
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
        print(f"\n== 收件箱密度验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def _set_filter(page: Any, f: str, more: bool = False) -> None:
    sel = ".ftab-more-item[data-f='%s']" % f if more else ".ftab[data-f='%s']" % f
    page.evaluate("(s) => setFilter(s.f, document.querySelector(s.sel))",
                  {"f": f, "sel": sel})
    page.wait_for_timeout(700)


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False, baseline: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    allow = sorted(WASTE_ALLOWLIST)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.setPlatFilter === 'function'",
                                   timeout=20000)
        except Exception:
            print("[SKIP] 工作台脚本未就绪（实例在重启？）")
            browser.close()
            return 0
        page.wait_for_timeout(3500)

        # ── 1. 无筛选基线：列表头预算 + 保存视图栏不占位 ──────────────
        print("== 1. 无筛选基线（列表头预算 / 保存视图栏）==")
        _set_filter(page, "all")
        base_snap = page.evaluate(_SNAP_JS)
        if baseline:
            print(f"     [baseline] list_header={base_snap['hdr_h']}px  "
                  f"saved_views={base_snap['sv_h']}px")
        else:
            ck.check("列表头（无筛选）未超预算",
                     base_snap["hdr_h"] is not None and base_snap["hdr_h"] <= LIST_HEADER_MAX,
                     f"{base_snap['hdr_h']}px <= {LIST_HEADER_MAX}px")
        ck.check("保存视图栏：0 个视图时不占位",
                 base_snap["sv_h"] == 0,
                 f"h={base_snap['sv_h']} is-empty={base_snap['sv_empty']}")

        # ── 2. filter-summary：该隐的隐 ────────────────────────────────
        print("== 2. filter-summary：单一主 tab 不复述 ==")
        has_sla = page.evaluate("() => !!document.querySelector(\".ftab[data-f='sla']\")")
        if not has_sla:
            ck.skip("单一主 tab 不渲染", "该实例无「超时」tab")
        else:
            _set_filter(page, "sla")
            s = page.evaluate(_SNAP_JS)
            ck.check("单一主 tab（超时）→ filter-summary 不渲染",
                     s["fs_h"] == 0, f"fs_h={s['fs_h']} hdr_h={s['hdr_h']}")

            # ── 3. 该留的留（防降噪降过头）────────────────────────────
            print("== 3. filter-summary：多维度仍保留（能力不丢）==")
            page.evaluate("() => setSortMode('urgent')")
            page.wait_for_timeout(700)
            s2 = page.evaluate(_SNAP_JS)
            ck.check("主 tab + 排序（2 维）→ 仍渲染",
                     (s2["fs_h"] or 0) > 0,
                     f"fs_h={s2['fs_h']} chips={s2['fs_chips']}")
            # 结构化判定：两枚 chip（tab 维 + 排序维）+ 清空入口都在 —— 这才是
            # 「比 tab 行多给的能力」（tab 行清不掉排序），文案无关、编码无关。
            ck.check("多维度时逐维度 chip 与「清空筛选」入口都在",
                     s2["fs_chips"] >= 2 and s2["fs_clear"] is True,
                     f"chips={s2['fs_chips']} clear_btn={s2['fs_clear']}")
            page.evaluate("() => setSortMode('recent')")
            page.wait_for_timeout(500)

        print("== 4. filter-summary：更多菜单档位仍保留 ==")
        has_waiting = page.evaluate(
            "() => !!document.querySelector(\".ftab-more-item[data-f='waiting']\")")
        if not has_waiting:
            ck.skip("更多档位仍渲染", "该实例无「待回复」档位")
        else:
            _set_filter(page, "waiting", more=True)
            s3 = page.evaluate(_SNAP_JS)
            ck.check("更多档位（待回复）→ 仍渲染（唯一在屏指示）",
                     (s3["fs_h"] or 0) > 0, f"fs_h={s3['fs_h']} txt={s3['fs_txt'][:30]!r}")
            _set_filter(page, "all")

        # ── 5. 滚动折叠 + 兄弟节点同步 ────────────────────────────────
        print("== 5. 滚动折叠：列表头收起且保存视图栏同步 ==")
        scrollable = page.evaluate(
            "() => { const el=document.getElementById('conv-items');"
            " return !!el && el.scrollHeight > el.clientHeight + 80; }")
        if not scrollable:
            ck.skip("滚动折叠", "会话不足，列表不可滚动")
        else:
            page.evaluate("() => { document.getElementById('conv-items').scrollTop = 240; }")
            page.wait_for_timeout(800)
            sc = page.evaluate(_SNAP_JS)
            ck.check("滚动后列表头进入 compact", sc["hdr_compact"] is True,
                     f"hdr_compact={sc['hdr_compact']}")
            ck.check("保存视图栏同步 compact（兄弟节点需显式同步）",
                     sc["sv_compact"] is True, f"sv_compact={sc['sv_compact']}")
            page.evaluate("() => _expandListHeader()")
            page.wait_for_timeout(600)
            sr = page.evaluate(_SNAP_JS)
            ck.check("回顶后两者都恢复展开",
                     sr["hdr_compact"] is False and sr["sv_compact"] is False,
                     f"hdr={sr['hdr_compact']} sv={sr['sv_compact']}")

        # ── 6. 列表面板「可见但空」扫描 ───────────────────────────────
        print("== 6. 「可见但空」扫描：会话列表面板 ==")
        w1 = page.evaluate(_WASTE_JS, {"sel": ".conv-list-panel", "allow": allow})
        if w1.get("absent"):
            ck.skip("列表面板扫描", "未找到 .conv-list-panel")
        else:
            items = w1["items"]
            ck.check("列表面板无「占位却无内容」的元素", not items,
                     _fmt_waste(items))

        # ── 7. composer：预算 + 扫描（只开已读会话，零副作用）─────────
        print("== 7. composer：预算 + 扫描（只打开已读会话）==")
        opened = page.evaluate("""() => {
          const row = document.querySelector('#conv-items .conv-item:not(.has-unread)');
          if (!row) return false;
          row.click(); return true;
        }""")
        if not opened:
            ck.skip("composer 预算/扫描",
                    "无「已读」会话可打开——不为测量把未读标成已读")
        else:
            page.wait_for_timeout(2500)
            comp = page.evaluate(_COMPOSER_JS)
            if comp.get("absent"):
                ck.skip("composer 预算/扫描", "会话未打开或 .reply-bar-wrap 缺失")
            else:
                if baseline:
                    print(f"     [baseline] composer_total={comp['total']}px")
                    for r in comp["rows"]:
                        if r["h"]:
                            print(f"        h={r['h']:<5} {r['disp']:<8} #{r['id']}")
                else:
                    ck.check("composer 未超预算", comp["total"] <= COMPOSER_MAX,
                             f"{comp['total']}px <= {COMPOSER_MAX}px")
                w2 = page.evaluate(_WASTE_JS, {"sel": ".reply-bar-wrap", "allow": allow})
                ck.check("composer 内无「占位却无内容」的元素",
                         not w2.get("items"), _fmt_waste(w2.get("items") or []))
            if shots:
                page.screenshot(path=str(shots / "inbox_density.png"))

        browser.close()
    if baseline:
        print("\n== baseline 模式：仅打印实测值，未断言高度预算 ==")
    return ck.summary()


def _fmt_waste(items: List[dict]) -> str:
    if not items:
        return ""
    return "; ".join(
        f"{('#' + i['id']) if i['id'] else i['tag']}.{i['cls']} h={i['h']}"
        for i in items[:6])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="坐席工作台会话面板密度门禁（只读，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--baseline", action="store_true",
                    help="只打印实测值、不断言高度预算（重校准天花板用）")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK，会话文案里的 ✕ / emoji 直接 UnicodeEncodeError 崩掉
    # （首次运行就踩到了；同族 .ps1 脚本的「ASCII-only」注释说的是同一个坑）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  老 Python / 被重定向的流
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
    print(f"== 0. 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, shots=shots, headed=args.headed, baseline=args.baseline)


if __name__ == "__main__":
    sys.exit(main())
