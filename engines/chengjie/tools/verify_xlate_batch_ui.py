"""收件箱「翻译提速批次」真浏览器门禁（2026-08-09，只读）。

验证五件事（模板/CSS 热更新直上生产，静态门禁证不了「浏览器里真的能用」）：
  1. 翻译弹层出现「引擎 →」下拉（5 选项：自动/HY-MT/AI/DeepL/Google）；
  2. `_onXlateEngineSel` 挂上 window（inline onchange 可达，非哑控件）；
  3. unified-inbox.css 以新缓存戳（?v=20260809a+）被引用且含 xl-skel 骨架规则；
  4. 浏览器会话（cookie 鉴权）内 POST /api/unified-inbox/translate-batch 真可用；
  5. 页面加载零 ReferenceError（dead-click 红条兜底不应触发）。

缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号，与其余 verify_* 工具同约定）。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:18799"
DATA_ROOT = Path(r"D:\chengjie-instances\zhiliao\data")


def _token() -> str:
    import yaml

    for name in ("config.local.yaml", "config.yaml"):
        p = DATA_ROOT / "config" / name
        if not p.exists():
            continue
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright 不可用")
        return 0
    try:
        urllib.request.urlopen(DEFAULT_BASE + "/login", timeout=4)
    except Exception:
        print("SKIP: 实例不可达")
        return 0
    tok = _token()
    if not tok:
        print("SKIP: 未读到 auth_token")
        return 0

    fails: list = []
    ref_errors: list = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.request.post(DEFAULT_BASE + "/login", form={"auth_token": tok})
        page = ctx.new_page()
        page.on("pageerror", lambda e: ref_errors.append(str(e)))
        page.goto(DEFAULT_BASE + "/workspace", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # 1. 引擎下拉存在且 5 选项
        n_opts = page.evaluate(
            "()=>{const s=document.getElementById('xlate-engine-sel');"
            "return s?s.options.length:-1}")
        if n_opts != 5:
            fails.append(f"engine dropdown options={n_opts} (expect 5)")

        # 2. onchange 处理器 window 可达
        fn_ok = page.evaluate("()=>typeof window._onXlateEngineSel==='function'")
        if not fn_ok:
            fails.append("_onXlateEngineSel not on window (dead dropdown)")

        # 3. CSS 新戳 + 骨架规则送达
        css_href = page.evaluate(
            "()=>{const l=[...document.querySelectorAll('link[rel=stylesheet]')]"
            ".map(x=>x.href).find(h=>h.includes('unified-inbox.css'));return l||''}")
        if "v=20260809" not in css_href:
            fails.append(f"css stamp stale: {css_href}")
        css_text = ctx.request.get(css_href).text() if css_href else ""
        if "xl-skel-bar" not in css_text:
            fails.append("css missing xl-skel rules")

        # 4. 浏览器会话内批量端点可用（cookie 鉴权）
        rsp = page.evaluate(
            "async()=>{const r=await fetch('/api/unified-inbox/translate-batch',"
            "{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({target_lang:'zh',items:["
            "{id:'v1',text:'verify tool ping one',source_lang:'en'},"
            "{id:'v2',text:'verify tool ping two',source_lang:'en'}]})});"
            "return {status:r.status,body:await r.json()}}")
        body = (rsp or {}).get("body") or {}
        oks = [bool((it.get("translation") or {}).get("ok"))
               for it in body.get("items") or []]
        if not ((rsp or {}).get("status") == 200 and body.get("ok") and all(oks)
                and len(oks) == 2):
            fails.append(f"batch endpoint via browser session: {json.dumps(rsp)[:200]}")

        # 5. 零 ReferenceError
        refs = [e for e in ref_errors if "is not defined" in e]
        if refs:
            fails.append(f"ReferenceError on load: {refs[:2]}")

        browser.close()

    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("PASS: dropdown(5 opts) + window handler + css stamp/skel + batch via "
          "browser session + zero ReferenceError")
    return 0


if __name__ == "__main__":
    sys.exit(main())
