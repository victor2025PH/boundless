# -*- coding: utf-8 -*-
"""P10 授权卡实拍（非门禁·人工 QA 辅助，需 playwright + Hub 在线）。

授权卡是 IIFE 里 innerHTML 拼出来的 DOM，uivr 矩阵只拍关页不拍展开态——
本工具拦截 /api/license/status 注入指定档位/状态（不动真实授权），点开徽章截卡片，
「试用旗舰按钮/输码框/档位对比表」一图验收。

用法:
  python tools/_lic_card_shot.py                      # 试用态(试用按钮现身)
  python tools/_lic_card_shot.py --state valid        # 正式授权态(试用按钮退场)
  python tools/_lic_card_shot.py --state trialing     # 试用升级中(还原按钮现身)
  python tools/_lic_card_shot.py --state grace        # 宽限期(琥珀横幅)
  python tools/_lic_card_shot.py --open-activate      # 连输码/导入面板一起展开
  python tools/_lic_card_shot.py --state trialing --days 1 --shot-banner
                                                      # P11 试用临期：48h 软着陆横幅一起截
  python tools/_lic_card_shot.py --matrix --out ui_snapshots/phases/lic_states
                                                      # P12 六态矩阵一次拍全(卡+横幅)，交付证据链
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台防 ⚡ 等字符炸 print
except Exception:
    pass

STATES = ("trial", "valid", "expired", "trialing", "grace")

# P12 矩阵档案：客户会遇到的授权 UI 全谱系（卡片+横幅），一次拍全入 ui_snapshots/phases/。
# (state, days_left 覆写或 -1, 文件名后缀)
MATRIX = [
    ("trial",    -1, ""),
    ("valid",    -1, ""),
    ("trialing",  6, ""),        # 试用中·未临期（无横幅）
    ("trialing",  1, "_d1"),     # 试用临期 48h：软着陆横幅
    ("grace",    -1, ""),        # 宽限期：琥珀横幅
    ("expired",  -1, ""),        # 已过期：红横幅（enforcing）
]


def build_state(st: str, days: int) -> dict:
    """构造 /api/license/status 注入态（与真实后端 summary() 字段同构）。"""
    is_valid = st in ("valid", "trialing")   # trialing=试签生效中（valid+trial_up.active）
    state = {
        "status": "valid" if is_valid else st,
        "status_label": ("已授权" if is_valid else
                         {"trial": "试用中", "expired": "已过期", "grace": "宽限期"}[st]),
        "edition": "trial" if st == "trial" else "pro",
        "edition_label": "试用版" if st == "trial" else "旗舰版",
        "days_left": {"valid": 300, "trialing": 6, "trial": 9, "expired": 0, "grace": 0}[st],
        # 试用也限时（真实后端=first_seen+14d）——mock 同语义，卡上显「剩 N 天」而非「永久」
        "expires_ts": int(time.time()) + {"valid": 300, "trialing": 6, "trial": 9,
                                          "expired": -3, "grace": -2}[st] * 86400,
        "expiring_soon": False, "enforcing": True,
        "in_grace": st == "grace", "grace_left": 5 if st == "grace" else 0,
        "this_machine": "AAAA-BBBB-CCCC-DDDD",
        "licensee": ("ACME" if st in ("valid", "grace", "expired") else
                     ("试用升级（限时体验）" if st == "trialing" else "")),
        "features": {"max_sessions": 8 if is_valid else 1},
        "activation_configured": True, "crl": {}, "message": "实拍注入态（非真实授权）",
        "trial_up": {"active": st == "trialing", "prev_available": st == "trialing"},
        "effective": {"enforced": True, "preset_ultra": is_valid, "preset_vocal": is_valid},
    }
    if days >= 0:
        state["days_left"] = days
        state["expires_ts"] = int(time.time()) + days * 86400
    return state


MATRIX_JS = [{"edition": e, "label": lb, "features": {
                 "preset_ultra": e != "trial", "preset_vocal": e == "pro",
                 "multi_replica": e == "pro", "max_sessions": {"trial": 1, "standard": 2, "pro": 8}[e],
                 "watermark_free": e == "pro"}}
             for e, lb in (("trial", "试用版"), ("standard", "标准版"), ("pro", "旗舰版"))]


def shoot(browser, base: str, st: str, days: int, out: Path,
          open_activate: bool = False, shot_banner: bool = False) -> str:
    """开新页注入指定态 → 截卡片（可选横幅）。返回徽章文案。"""
    state = build_state(st, days)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/api/license/status",
               lambda route: route.fulfill(content_type="application/json",
                   body=json.dumps({"ok": True, "available": True, "state": state,
                                    "matrix": MATRIX_JS})))
    page.goto(base + "/ui?uivr=1", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    page.click("#licChip")
    page.wait_for_timeout(400)
    if open_activate:
        btn = page.locator("#licActBtn")
        if btn.count():
            btn.click()
            page.wait_for_timeout(300)
    out.parent.mkdir(parents=True, exist_ok=True)
    page.locator("#licCard").screenshot(path=str(out))   # 卡片绝对定位，截 licWrap 只会得到徽章
    chip_txt = page.locator("#licChip").inner_text()     # P11 徽章文案顺带带出（试用旗舰·剩N天）
    if shot_banner:
        bn = page.locator("#licBanner")
        if bn.count() and bn.is_visible():
            bp = out.with_name(out.stem + "_banner.png")
            bn.screenshot(path=str(bp))
            print(f"OK: banner -> {bp}")
        else:
            print(f"WARN: licBanner 不可见（{st} 态无横幅）")
    page.close()
    return chip_txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    ap.add_argument("--state", default="trial", choices=list(STATES))
    ap.add_argument("--open-activate", action="store_true")
    ap.add_argument("--days", type=int, default=-1, help="覆写剩余天数（P11 试用临期横幅=trialing+days<=2）")
    ap.add_argument("--shot-banner", action="store_true", help="额外截顶部横幅（licBanner）")
    ap.add_argument("--matrix", action="store_true",
                    help="P12 六态全谱实拍（卡+横幅）到 --out 目录，交付证据链")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright 未装（pip install -r requirements/selfcheck.txt）"); return 2

    with sync_playwright() as p:
        b = p.chromium.launch()
        if args.matrix:
            outdir = Path(args.out or (Path(tempfile.gettempdir()) / "stream_states" / "lic_states"))
            n = 0
            for st, days, suffix in MATRIX:
                f = outdir / f"lic_{st}{suffix}.png"
                chip = shoot(b, args.base, st, days, f, shot_banner=True)
                print(f"OK: {st}{suffix or ''} chip=[{chip}] -> {f}")
                n += 1
            b.close()
            print(f"OK: 授权态矩阵 {n} 态 -> {outdir}")
            return 0
        out = Path(args.out or (Path(tempfile.gettempdir()) / "stream_states" / f"lic_card_{args.state}.png"))
        chip_txt = shoot(b, args.base, args.state, args.days, out,
                         open_activate=args.open_activate, shot_banner=args.shot_banner)
        b.close()
    print(f"OK: lic card ({args.state}{' +activate' if args.open_activate else ''}) chip=[{chip_txt}] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
