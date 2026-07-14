# -*- coding: utf-8 -*-
"""P11 一次性 JS 探针：改动页逐个真加载，收集 pageerror/console.error（用完可留作快检）。"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9000"
PAGES = ["/ui?uivr=1", "/static/phone.html", "/dashboard", "/s?profile=x&from=recap"]

bad = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for path in PAGES:
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
        pg.on("console", lambda m, errs=errs: errs.append("console: " + m.text)
              if m.type == "error" else None)
        try:
            pg.goto(BASE + path, wait_until="domcontentloaded", timeout=15000)
            pg.wait_for_timeout(2200)
        except Exception as e:
            errs.append("goto: %s" % e)
        # 画布函数烟测：canvas_brand 新基元在真页面环境可调用
        if path.endswith("phone.html"):
            r = pg.evaluate("""() => {
                const c=document.createElement('canvas'); const x=c.getContext('2d');
                const BD=window.BD_CANVAS;
                BD.wrapText(x,'烟测换行文本'.repeat(9),10,10,60,14,2);
                return [typeof BD.ellipsize==='function' ? BD.ellipsize(x,'超长名字'.repeat(30),80) : 'MISSING',
                        typeof _wrapText];
            }""")
            if "MISSING" in str(r) or "…" not in str(r[0]) or r[1] != "function":
                errs.append("BD_CANVAS 文本基元异常: %s" % r)
        ignore = ("favicon", "net::ERR", "Failed to load resource")
        errs = [e for e in errs if not any(k in e for k in ignore)]
        print(("OK  " if not errs else "NG  ") + path + ("" if not errs else "  |  " + " ;; ".join(errs[:4])))
        bad += errs
        pg.close()
    b.close()
sys.exit(1 if bad else 0)
