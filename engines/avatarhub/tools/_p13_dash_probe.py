# -*- coding: utf-8 -*-
"""P13/P14 看板扫码聚焦联动探针：聚焦 → 重排 → 第二跳（角色扫码时序）→ 还原。

验证链路（真浏览器 + 真数据，不 mock）：
  1. /dashboard 传播卡出现「扫码进站」行（无扫码数据则跳过，探针不造数）；
  2. 点击 → 行高亮「已聚焦」、角色 Top 标注「按扫码量排」、趋势图 legend 出现「扫码聚焦」；
  3. P14-4 第二跳：聚焦态点第一个角色 → 出现「扫码时序」下钻块（有归因画图/无归因给空态说明，
     两者都算 UI 契约成立）；再点同角色收起；
  4. 再点扫码行 → 全部还原；全程零 pageerror。
用法：python tools/_p13_dash_probe.py [--base http://127.0.0.1:9000]
退出码：0=通过 1=失败 2=跳过（无扫码数据/页面无传播卡）。
"""
import argparse
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

OUT = Path(tempfile.gettempdir()) / "stream_states"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    args = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1600})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(args.base + "/dashboard", wait_until="domcontentloaded")
        try:
            pg.locator("#shareStats div.bar").first.wait_for(state="visible", timeout=20000)
        except Exception:
            print("SKIP: 传播卡未渲染（无传播数据）"); b.close(); return 2

        row = pg.locator("#shareStats div.bar", has_text="扫码进站")
        if row.count() != 1:
            print("SKIP: 无扫码进站数据（land_recap/land_poster 全零）"); b.close(); return 2

        row.click(); pg.wait_for_timeout(600)
        st = pg.locator("#shareStats").inner_text()
        tr = pg.locator("#shareTrend").inner_text()
        assert "已聚焦" in st, f"聚焦态未生效: {st[:200]}"
        assert "按扫码量排" in st, "角色榜未按扫码重排"
        assert "扫码聚焦" in tr, f"趋势图未进聚焦态: {tr[:120]}"
        pg.locator("#shareStats").screenshot(path=str(OUT / "dash_scanfocus.png"))
        pg.locator("#shareTrend").screenshot(path=str(OUT / "dash_scanfocus_trend.png"))

        # P14-4 第二跳：聚焦态点第一个角色 → 「扫码时序」下钻块（有图或空态说明均为契约成立）
        drill2 = ""
        roles = pg.locator("#shareStats div.bar").filter(has_text="扫码 ")
        role_rows = [i for i in range(roles.count())
                     if "扫码进站" not in roles.nth(i).inner_text()]
        if role_rows:
            roles.nth(role_rows[0]).click(); pg.wait_for_timeout(900)
            drill = pg.locator("#shareStats").inner_text()
            assert "扫码时序" in drill, f"第二跳未出现角色扫码时序: {drill[:200]}"
            pg.locator("#shareStats").screenshot(path=str(OUT / "dash_scandrill.png"))
            pg.locator("#shareStats div.bar").filter(has_text="◉").last.click()
            pg.wait_for_timeout(600)
            assert "扫码时序" not in pg.locator("#shareStats").inner_text(), "再点角色未收起第二跳"
            drill2 = "（含第二跳角色时序）"

        pg.locator("#shareStats div.bar", has_text="扫码进站").click()
        pg.wait_for_timeout(600)
        st2 = pg.locator("#shareStats").inner_text()
        tr2 = pg.locator("#shareTrend").inner_text()
        assert "已聚焦" not in st2 and "按扫码量排" not in st2, "再点未还原角色榜"
        assert "扫码聚焦" not in tr2, "再点未还原趋势图"
        assert not errs, f"JS 错误: {errs}"
        print(f"OK: 聚焦→重排→第二跳→还原 全链路通过{drill2}，无 JS 错误")
        print(f"截图: {OUT / 'dash_scanfocus.png'} · {OUT / 'dash_scandrill.png'}")
        b.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
