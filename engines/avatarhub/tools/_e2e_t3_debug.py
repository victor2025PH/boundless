# -*- coding: utf-8 -*-
"""单调 T3 crit 横幅：mock snapshot 后 dump degradeBar 的类名/display/尺寸，定位 is_visible=False 原因。"""
import sys, json, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page()
    pg.goto(HUB + "/phone", wait_until="domcontentloaded", timeout=20000)
    pg.wait_for_timeout(2500)

    def _route_crit(route):
        body = {"ok": True, "pressure": "yellow", "gpu_util": 50, "ram_percent": 40,
                "services": {"fish_tts": False, "emotion_tts": False, "stt": True,
                             "lipsync": True, "vcam": True},
                "services_busy": [], "latency_ms": {},
                "supervisor": {"fish_tts": {"alive": False, "offloaded": False, "tripped": False, "restarts": 2}},
                "tts_channel": {"stats": {}}, "metrics": {}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    pg.route("**/api/ops/snapshot", _route_crit)
    pg.evaluate("() => pollHealthDegrade()")
    pg.wait_for_timeout(800)
    info = pg.evaluate("""() => {
        const el = document.getElementById('degradeBar');
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const par = [];
        let n = el;
        while (n && n.id !== undefined && n.tagName !== 'BODY') {
            const c = getComputedStyle(n);
            par.push({id: n.id || n.className, display: c.display, vis: c.visibility, h: n.getBoundingClientRect().height});
            n = n.parentElement;
        }
        return {cls: el.className, styleDisplay: el.style.display, csDisplay: cs.display,
                vis: cs.visibility, rect: {w: r.width, h: r.height}, chain: par.slice(0, 6),
                html: el.innerHTML.slice(0, 80)};
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    b.close()
