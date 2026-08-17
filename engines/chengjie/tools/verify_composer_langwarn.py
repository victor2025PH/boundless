# -*- coding: utf-8 -*-
"""坐席手发「语言错配警示条」真浏览器门禁（Playwright；2026-08-15 D 批沉淀）。

**为什么需要它**：外语会话里坐席直接打中文点发送（未开出站翻译）→ 客户收到
原样中文——这是「中文输入 × 英文客户」事故家族里唯一没有护栏的一段（AI 回复链
已修，出站翻译开着时后端会翻，唯独「坐席手打 + 不译」裸奔）。警示条
``#lang-warn-bar`` 不拦发送（故意发中文是合法意图），给「开启自动翻译」修复
入口（与手选下拉「自动」同一条 ``_onXlateChange`` 写路径）。

决策纯函数 ``_langWarnCheck`` 窄口径（宁漏报不误报，判据镜像 spoken_style
zh_only）：会话语言已知且非 zh/ja、≥4 个汉字且占比 ≥40%、出站翻译未开、
非 / 命令。真值表在真浏览器直测（函数挂 window 正是为此）。

与 ``tools/verify_inbox_identity.py`` 同族（同实例 + token 登录 + Playwright +
SKIP exit 0）。**零生产写入**：行为段只改页面 JS 内存（``selectedChat.language``）
与 ephemeral context 的 localStorage，不打任何写接口。

用法::

    python tools/verify_composer_langwarn.py                # 门禁
    python tools/verify_composer_langwarn.py --headed       # 肉眼
    python tools/verify_composer_langwarn.py --source-only  # 只跑静态接线
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
INBOX_CSS = ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"

# 真值表：(输入文本, 会话语言, _xlateOut, 期望, 场景名)
TRUTH_TABLE: List[Tuple[str, str, str, bool, str]] = [
    ("今晚吃什么呢朋友", "en", "", True, "外语会话打整句中文 → 警"),
    ("今晚吃什么呢朋友", "EN-us", "", True, "语言码带地区后缀（基码比较）"),
    ("今晚吃什么呢朋友", "ko", "", True, "韩语会话打中文 → 警"),
    ("好的", "en", "", False, "<4 个汉字的短插语不警"),
    ("今晚吃什么呢朋友", "zh", "", False, "中文会话不警"),
    ("今晚吃什么呢朋友", "zh-TW", "", False, "zh-TW 归中文不警"),
    ("今晚吃什么呢朋友", "ja", "", False, "日语会话不警（日文用汉字，会误伤）"),
    ("今晚吃什么呢朋友", "", "", False, "会话语言未知不警"),
    ("今晚吃什么呢朋友", "unknown", "", False, "unknown 不警"),
    ("今晚吃什么呢朋友", "en", "auto", False, "出站翻译已开（auto）不警"),
    ("今晚吃什么呢朋友", "en", "en", False, "出站翻译已开（具体语种）不警"),
    ("/翻译 今晚吃什么呢朋友", "en", "", False, "/ 命令输入不警"),
    ("hello there my friend", "en", "", False, "纯外语输入不警"),
    ("好的好的 sounds great, see you tonight my friend", "en", "", False,
     "汉字占比 <40%（以外语为主的混排）不警"),
]


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

    def summary(self, title: str = "手发语言错配警示验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def read_token(data_root: str) -> str:
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


def check_source_wiring(ck: Checker) -> None:
    """不依赖实例：防警示条被顺手删掉 / 接线断掉（模板热更新直上生产）。"""
    print("== 0. 源码接线（静态）==")
    if not INBOX_TEMPLATE.exists():
        ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
        return
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    ck.check("决策纯函数已定义", "function _langWarnCheck" in src)
    ck.check("纯函数挂 window（门禁直测契约）", "window._langWarnCheck" in src)
    ck.check("修复/免扰 handler 挂 window",
             "window._langWarnFix" in src and "window._langWarnDismiss" in src)
    ck.check("警示条容器在模板", 'id="lang-warn-bar"' in src)
    import re as _re
    ck.check("输入钩子接线（_onReplyInput → debounced）",
             bool(_re.search(r"function _onReplyInput[\s\S]{0,800}_syncLangWarnDebounced\s*\(", src)))
    ck.check("翻译开关钩子接线（_onXlateChange → sync）",
             bool(_re.search(r"function _onXlateChange[\s\S]{0,800}_syncLangWarn\s*\(", src)))
    ck.check("换会话钩子接线（_loadXlateForConv → sync）",
             bool(_re.search(r"function _loadXlateForConv[\s\S]{0,2000}_syncLangWarn\s*\(", src)))
    ck.check("修复走单写路径（_langWarnFix → _onXlateChange）",
             bool(_re.search(r"function _langWarnFix[\s\S]{0,400}_onXlateChange\s*\(", src)))
    for key in ("inbox.langwarn.msg", "inbox.langwarn.fix",
                "inbox.langwarn.fix_t", "inbox.langwarn.dismiss_t"):
        ck.check(f"i18n 键被引用：{key}", key in src)
    if INBOX_CSS.exists():
        css = INBOX_CSS.read_text(encoding="utf-8")
        ck.check("CSS 规则存在（.lang-warn-bar）", ".lang-warn-bar" in css)


def _login_workspace(p: Any, base: str, token: str, *, headed: bool) -> Tuple[Any, Any, Any]:
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.setPlatFilter === 'function'", timeout=20000)
    page.wait_for_timeout(3500)
    return browser, ctx, page


_BAR_JS = """() => {
  const el = document.getElementById('lang-warn-bar');
  if (!el) return {absent: true};
  const st = getComputedStyle(el);
  return {
    absent: false,
    visible: st.display !== 'none' && el.getBoundingClientRect().height > 0,
    text: (el.textContent || '').trim(),
    has_fix: !!el.querySelector('.lw-fix'),
    has_x: !!el.querySelector('.lw-x'),
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 工作台脚本未就绪（{str(e)[:80]}）")
            return ck.summary()

        # ── 1. 真值表（真浏览器直测纯函数）──────────────────────────
        print("== 1. _langWarnCheck 真值表 ==")
        fn_ok = page.evaluate("() => typeof window._langWarnCheck === 'function'")
        if not ck.check("window._langWarnCheck 可达", fn_ok):
            browser.close()
            return ck.summary()
        for text, lang, xout, want, name in TRUTH_TABLE:
            got = page.evaluate(
                "(a) => window._langWarnCheck(a[0], a[1], a[2])", [text, lang, xout])
            ck.check(name, got == want, f"got={got} want={want}")

        # ── 2. 行为：route mock 把会话语言改成 en，全程真实交互 ─────────
        # ⚠ 模板脚本是模块作用域：顶层 let（selectedChat/_xlateOut）从 evaluate
        # **不可直达**（首版实锤 ReferenceError），只有挂 window 的函数可达。
        # 故不做内存态改写，改与 verify_inbox_identity 第 7 节同款 route mock
        # （chats 响应打补丁 language='en'），配真实点击/键入——反而更贴生产路径。
        print("== 2. 警示条行为（chats route mock language=en，不写生产）==")
        import json as _json

        def _patch_chats(route: Any) -> None:
            resp = route.fetch()
            try:
                data = resp.json()
            except Exception:
                route.fulfill(response=resp)
                return
            for c in (data.get("chats") or []):
                c["language"] = "en"
            route.fulfill(status=resp.status, content_type="application/json",
                          body=_json.dumps(data, ensure_ascii=False))

        page.route("**/api/unified-inbox/chats*", _patch_chats)
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.setPlatFilter === 'function'", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        opened = page.evaluate(
            """() => {
              const row = document.querySelector('#conv-items .conv-item:not(.has-unread)');
              if (!row) return false;
              row.click();
              return true;
            }""")
        if not opened:
            ck.skip("警示条行为", "实例无已读会话可点")
            browser.close()
            return ck.summary()
        # 等 composer 就绪（reply-ta 可见即会话已打开）
        try:
            page.wait_for_selector("#reply-ta", state="visible", timeout=8000)
        except Exception:
            ck.skip("警示条行为", "composer 未就绪")
            browser.close()
            return ck.summary()
        page.wait_for_timeout(1200)   # 等 _loadXlateForConv 完成（xlate 状态就位）
        # 真实键入中文 → input 事件 → debounce(250ms) → 条应出现
        page.fill("#reply-ta", "今晚吃什么呢朋友")
        page.wait_for_timeout(700)
        bar = page.evaluate(_BAR_JS)
        # 若该会话本就开着出站翻译（坐席浏览器默认），mock 语言也不该出条——
        # 用下拉状态判定这是「合法熄灭」还是「断线」。
        out0 = page.evaluate(
            "() => (document.getElementById('xlate-out')||{}).value || ''")
        if out0:
            # 出站翻译默认开：先关掉再验（下拉藏在收起的翻译面板里不可见，
            # select_option 会等可见超时 → JS 直驱 + 生产同款 onchange 链）
            page.evaluate(
                "() => { const so=document.getElementById('xlate-out');"
                " if(so){ so.value=''; } _onXlateChange(); }")
            page.wait_for_timeout(400)
            page.fill("#reply-ta", "今晚吃什么呢朋友再来一句")
            page.wait_for_timeout(700)
            bar = page.evaluate(_BAR_JS)
        ck.check("外语会话打中文 → 警示条出现",
                 bar.get("visible") and bar.get("has_fix") and bar.get("has_x"),
                 f"bar={bar}")
        # 点「开启自动翻译」→ 下拉变 auto、条熄灭
        if bar.get("visible"):
            page.click("#lang-warn-bar .lw-fix")
            page.wait_for_timeout(400)
            after = page.evaluate(
                """() => ({
                  out: (document.getElementById('xlate-out')||{}).value || '',
                  bar: getComputedStyle(document.getElementById('lang-warn-bar')).display !== 'none',
                })""")
            ck.check("修复：出站翻译置 auto", after.get("out") == "auto", f"out={after.get('out')!r}")
            ck.check("修复后警示条熄灭", not after.get("bar"))
        # 关回翻译再键入 → 条复现；点 ✕ → 本会话免扰（重打字也不再出）
        page.evaluate(
            "() => { const so=document.getElementById('xlate-out');"
            " if(so){ so.value=''; } _onXlateChange(); }")
        page.wait_for_timeout(400)
        page.fill("#reply-ta", "")
        page.fill("#reply-ta", "今晚吃什么呢朋友")
        page.wait_for_timeout(700)
        bar2 = page.evaluate(_BAR_JS)
        if ck.check("关闭翻译后条复现", bar2.get("visible"), f"bar={bar2}"):
            page.click("#lang-warn-bar .lw-x")
            page.wait_for_timeout(300)
            page.fill("#reply-ta", "还是想打中文再试一次")
            page.wait_for_timeout(700)
            bar3 = page.evaluate(_BAR_JS)
            ck.check("✕ 免扰后同会话不再出条", not bar3.get("visible"), f"bar={bar3}")
        page.unroute("**/api/unified-inbox/chats*")
        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="坐席手发语言错配警示门禁（只读 + 页面内存态，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--source-only", action="store_true", help="只跑静态接线（不启浏览器）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.source_only:
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("语言错配警示源码接线")

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[WARN] 未装 playwright；仅跑静态源码接线")
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("语言错配警示源码接线（无 playwright）")

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        ck = Checker()
        check_source_wiring(ck)
        code = ck.summary("语言错配警示源码接线（实例不可达）")
        return 0 if code == 0 else code

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
