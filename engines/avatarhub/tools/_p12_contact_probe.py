# -*- coding: utf-8 -*-
"""P12 联系厂商动线一次性实拍验证（非门禁）：
备份 brand 配置 → 写入 contact → 实拍过期横幅+授权卡（应出现「📞 复制联系方式」按钮与联系行）
→ 点按钮验证剪贴板 → 还原 brand 配置。需 Hub 在线 + playwright。"""
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:9000"
OUT = Path(__file__).resolve().parent.parent / "ui_snapshots" / "phases" / "lic_states"


def api(path, payload=None):
    req = urllib.request.Request(BASE + path)
    if payload is not None:
        req.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    orig = api("/api/brand").get("config") or {}
    print("原配置:", json.dumps(orig, ensure_ascii=False))
    test_cfg = dict(orig)
    test_cfg["contact"] = "微信 boundless-sales · 138-0000-0000"
    api("/api/brand", {"config": test_cfg})
    try:
        from playwright.sync_api import sync_playwright
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _lic_card_shot import build_state, MATRIX_JS
        state = build_state("expired", -1)
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1440, "height": 900},
                                permissions=["clipboard-read", "clipboard-write"])
            page = ctx.new_page()
            page.route("**/api/license/status",
                       lambda route: route.fulfill(content_type="application/json",
                           body=json.dumps({"ok": True, "available": True, "state": state,
                                            "matrix": MATRIX_JS})))
            page.goto(BASE + "/ui?uivr=1", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            bn = page.locator("#licBnContact")
            assert bn.count() == 1, "横幅未出现联系厂商按钮"
            print("横幅按钮文案:", bn.inner_text())
            OUT.mkdir(parents=True, exist_ok=True)
            page.locator("#licBanner").screenshot(path=str(OUT / "lic_expired_contact_banner.png"))
            bn.click()
            page.wait_for_timeout(300)
            clip = page.evaluate("navigator.clipboard.readText()")
            assert "boundless-sales" in clip, f"剪贴板不对: {clip!r}"
            print("剪贴板验证 OK:", clip)
            print("点击后按钮文案:", bn.inner_text())
            page.click("#licChip")
            page.wait_for_timeout(400)
            card_txt = page.locator("#licCard").inner_text()
            assert "boundless-sales" in card_txt, "授权卡未出现联系行"
            page.locator("#licCard").screenshot(path=str(OUT / "lic_expired_contact_card.png"))
            print("授权卡联系行 OK")
            b.close()
    finally:
        api("/api/brand", {"config": orig})
        print("已还原品牌配置:", json.dumps(api("/api/brand").get("config") or {}, ensure_ascii=False))
    print("OK: 联系厂商动线端到端验证通过 ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
