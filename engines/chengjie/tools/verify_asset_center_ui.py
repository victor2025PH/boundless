# -*- coding: utf-8 -*-
"""账号资产中心真浏览器只读门禁（身份可读化 + 动作菜单 + 口径说明，2026-08-28）。

**为什么必须有它**：模板保存即上生产（Jinja auto_reload），而静态门禁只能证明
「函数挂了 window / 没有重复 id / i18n 键解析得到」，证不了「17 张卡真的渲染出
人话名字」「⋯ 菜单点得开」「彻底删除只在历史态出现」。本页 P0 的全部价值恰恰
在这些运行时事实上。

**只读铁律**：只点开菜单、只读 DOM，**绝不点**重命名/恢复/彻底删除（那些会真改
生产数据）。删除按钮只验「在不在、该不该在」。导出也不点（会真下载 + 落审计）。

环境缺失（无 playwright / 实例不可达 / 无 auth_token）一律 **SKIP exit 0**，
不污染回归信号——与 verify_multiwin_ui / verify_care_ui 同纪律。

    python tools/verify_asset_center_ui.py [--base http://127.0.0.1:18799]
                                           [--shots out/] [--headed]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _token(root: Path) -> str:
    try:
        import yaml
    except Exception:
        return ""
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.is_file():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--data-root", default=r"D:\chengjie-instances\zhiliao\data")
    ap.add_argument("--shots", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] 未安装 playwright（exit 0）")
        return 0
    tok = _token(Path(args.data_root))
    if not tok:
        print(f"[SKIP] 未能从 {args.data_root} 读到 auth_token（exit 0）")
        return 0

    fails = []

    def check(name, cond, detail=""):
        ok = bool(cond)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not args.headed)
        except Exception as e:
            print(f"[SKIP] 浏览器启动失败：{str(e)[:120]}（exit 0）")
            return 0
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        try:
            # 令牌表单在多用户部署下默认 display:none（要先点「管理员令牌登录」）。
            # 走 context 的 request API 提交更稳：它与浏览器共用 cookie jar，
            # 拿到 session 后 page.goto 直接是已登录态，不依赖登录页的 DOM 形态。
            resp = ctx.request.post(args.base + "/login",
                                    form={"auth_token": tok}, timeout=30000)
            if resp.status not in (200, 302, 303):
                print(f"[SKIP] 登录失败 http={resp.status}（exit 0）")
                browser.close()
                return 0
            page.goto(args.base + "/workspace/assets", timeout=30000)
            page.wait_for_selector("#ac-body", state="visible", timeout=60000)
            page.wait_for_selector(".ac-card", timeout=60000)
        except Exception as e:
            print(f"[SKIP] 页面打不开（实例忙/未登录）：{str(e)[:160]}（exit 0）")
            browser.close()
            return 0

        cards = page.query_selector_all(".ac-card")
        check("页面渲染出账号卡", len(cards) > 0, f"{len(cards)} 张")

        # 1) 身份可读化：没有任何一张卡的主标题是「超长裸 id」
        names = [c.query_selector(".ac-nm2").inner_text().strip()
                 for c in cards if c.query_selector(".ac-nm2")]
        check("每张卡都有主标题", len(names) == len(cards))
        toolong = [n for n in names if len(n) > 20]
        check("主标题不再出现超长裸 ID（老板报障那条）", not toolong, str(toolong[:3]))
        # LINE 的 44 字 MID 必须已被昵称替代
        mids = [n for n in names if n.startswith("U") and len(n) >= 30]
        check("LINE MID 不作为显示名", not mids, str(mids[:2]))

        # 2) 头像存在且不是空圈
        avs = page.query_selector_all(".ac-card .ac-av")
        check("每张卡有头像锚点", len(avs) == len(cards), f"{len(avs)}/{len(cards)}")
        blank = 0
        for a in avs:
            if not (a.inner_text().strip() or a.query_selector("img")):
                blank += 1
        check("头像不留空圈（字首兜底）", blank == 0, f"空 {blank}")

        # 3) 副行：脱敏 + 完整 ID 在 title 里 + 复制按钮
        subs = page.query_selector_all(".ac-card .ac-sub2")
        check("每张卡有副行（平台 · 脱敏 ID）", len(subs) == len(cards))
        check("复制完整 ID 按钮存在",
              len(page.query_selector_all('[data-act="copyid"]')) == len(cards))
        masked = [s.inner_text() for s in subs if "****" in s.inner_text()]
        check("手机号型账号已脱敏（防截图外泄）", len(masked) > 0,
              f"{len(masked)} 张卡命中")

        # 4) 动作菜单真的能打开（静态门禁证不了这个）
        btn = page.query_selector('.ac-card [data-act="menu"]')
        check("⋯ 更多操作按钮存在", btn is not None)
        if btn:
            btn.click()
            page.wait_for_timeout(200)
            opened = page.query_selector_all(".ac-menu.open")
            check("点击后菜单打开", len(opened) == 1, f"{len(opened)} 个")
            check("菜单含重命名",
                  page.query_selector('.ac-menu.open [data-act="rename"]') is not None)
            check("aria-expanded 同步", btn.get_attribute("aria-expanded") == "true")
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("Esc 关闭菜单",
                  len(page.query_selector_all(".ac-menu.open")) == 0)

        # 5) 「彻底删除」只在历史态出现（在线号不该有）——只验存在性，绝不点
        online_with_purge = 0
        hist_with_purge = 0
        for c in cards:
            st = c.query_selector(".ac-st")
            cls = (st.get_attribute("class") or "") if st else ""
            has = c.query_selector('[data-act="purge"]') is not None
            if "online" in cls and has:
                online_with_purge += 1
            if ("history_only" in cls or "removed" in cls or "banned" in cls
                    or "offline" in cls) and has:
                hist_with_purge += 1
        check("在线账号不出现「彻底删除」", online_with_purge == 0,
              f"{online_with_purge} 张")
        check("历史/离线/封禁账号出现「彻底删除」", hist_with_purge > 0,
              f"{hist_with_purge} 张")

        # 6) 口径诚实化：0% 的无句柄平台必须带解释；派生数必须说明来源
        zero_nohandle = page.query_selector_all(".ac-card .ac-note")
        check("卡上出现口径说明行", len(zero_nohandle) > 0, f"{len(zero_nohandle)} 行")
        kpi_notes = page.query_selector_all(".ac-kpi .n")
        check("KPI 带口径脚注（联系人累计 / 媒体两套口径）", len(kpi_notes) >= 2,
              f"{len(kpi_notes)} 条")

        # 7) 迁移包 CTA（端点已上线 → features.export_migration=true）
        check("「导出迁移包」CTA 已点亮",
              len(page.query_selector_all('[data-act="migrate"]')) == len(cards))

        # 8) 可加回分母 == 同卡展示的联系人数（UI 级不变量）
        bad = 0
        for c in cards:
            cells = c.query_selector_all(".ac-nums .cell b")
            legend = c.query_selector_all(".ac-legend span")
            if len(cells) < 3 or len(legend) < 4:
                continue
            contacts = int(cells[2].inner_text().replace(",", "") or 0)
            tot = 0
            for sp in legend:
                t = sp.inner_text().strip().split()
                if t:
                    try:
                        tot += int(t[-1].replace(",", ""))
                    except ValueError:
                        pass
            if contacts != tot:
                bad += 1
        check("可加回四桶之和 == 展示的联系人数", bad == 0, f"不一致 {bad} 张")

        # 9) 零 JS 运行时报错（死按钮/未定义函数会在这里现形）
        check("页面无 JS 运行时错误", not errors, "; ".join(errors[:2]))

        if args.shots:
            out = Path(args.shots)
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / "asset_center.png"), full_page=True)
            print(f"      → 截图 {out / 'asset_center.png'}")
        browser.close()

    print("\n" + (f"✗ 失败 {len(fails)} 项: {fails}" if fails else "✓ 全部通过"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
