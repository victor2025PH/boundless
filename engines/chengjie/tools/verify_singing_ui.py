# -*- coding: utf-8 -*-
"""歌房页真浏览器只读验证（Playwright；实施58 P3-2）。

**为什么需要它**：singing.html 的点唱台卡片流/备货矩阵/迷你播放器/feat 探测
（新端点未装载时按钮隐藏）全是纯前端行为且模板热更新直上生产；静态门禁证不了
「失败单显示的是人话不是 JSON」「旧后端下不摆死按钮」。

与 verify_care_ui 同族（同实例 + token 登录 + Playwright），**只读**：不点
放行/打回/开关/补货（动状态或真发消息），只验渲染与探测层。

覆盖的不变量：
  1. 页面加载 + hero 渲染（总开关 + 健康灯 ≥3 枚）。
  2. 点唱台：分状态计数 chips 齐 5 枚；订单卡或空态提示二选一可见。
  3. **失败单绝无原始 JSON**（06:10 实锤修复项）：页面可见文本不含
     "lyrics_rejected:{" / "'attempts'"。
  4. 备货矩阵 ≥10 行人设 + ≥1 试听键 + 贴合度圆点在场。
  5. 曲库表 3 行曲目 + 启用开关。
  6. 声库卡 ≥3 张。
  7. feat 一致性：补货/重试类按钮的出现 ↔ /api/singing/supply-status 可用
     （旧后端=按钮隐藏，绝不摆死按钮）。
  8. i18n 无裸键（可见文本不含 "sg_"）；迷你播放器默认隐藏。
  9. 零 pageerror（ReferenceError 级前端崩溃）。

缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号）。
用法：python tools/verify_singing_ui.py [--headed] [--base URL] [--data-root D]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"


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


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 歌房页验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => {
  const q = (sel) => Array.from(document.querySelectorAll(sel));
  const vis = (el) => !!el && el.offsetParent !== null;
  return {
    heroSwitch: vis(document.getElementById('sg-cfg-enabled')?.parentElement),
    healthPills: q('#sg-health .sg-pill').length,
    ordCounts: q('#sg-ord-counts .sg-stat').length,
    ordCards: q('#sg-orders .sg-ordcard').length,
    ordEmptyVisible: vis(document.getElementById('sg-ord-empty')),
    matrixRows: q('#sg-matrix tr').length,
    playBtns: q('.sg-play').length,
    vmDots: q('.sg-vm').length,
    tplRows: q('#sg-templates tr').length,
    tplToggles: q('#sg-templates input[type=checkbox]').length,
    voiceCards: q('#sg-voices .sg-vcard').length,
    supplyBtns: q('#sg-templates .sg-btn').length,
    retryBtns: q('#sg-orders .sg-ordacts .sg-btn').length,
    playerHidden: !document.getElementById('sg-player')?.classList.contains('show'),
    bodyText: document.body.innerText || '',
  };
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] playwright 未安装（exit 0）")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到 web_admin.auth_token（exit 0）")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=8)
    except Exception:
        print("[SKIP] 实例不可达（exit 0）")
        return 0

    c = Checker()
    page_errors: List[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        # 会话 cookie 走 context 请求（verify_care_ui 同款），不碰登录表单 UI
        ctx.request.post(args.base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(args.base + "/singing", timeout=30000,
                  wait_until="domcontentloaded")
        # 基座模板常驻轮询 → networkidle 永不触发；等真实渲染信号（矩阵出行）
        page.wait_for_function(
            "() => document.querySelectorAll('#sg-matrix tr').length > 1",
            timeout=20000)
        page.wait_for_timeout(900)
        st = page.evaluate(_STATE_JS)

        # 后端 feat 实况（同会话 cookie 复用）
        feat_v2 = page.evaluate(
            "() => fetch('/api/singing/supply-status',{credentials:'same-origin'})"
            ".then(r => r.ok).catch(() => false)")

        c.check("hero 总开关渲染", st["heroSwitch"])
        c.check("健康灯 ≥3", st["healthPills"] >= 3, f"got {st['healthPills']}")
        c.check("点唱台计数 chips=5", st["ordCounts"] == 5,
                f"got {st['ordCounts']}")
        c.check("订单卡或空态二选一",
                st["ordCards"] > 0 or st["ordEmptyVisible"])
        c.check("失败单无原始 JSON",
                "lyrics_rejected:{" not in st["bodyText"]
                and "'attempts'" not in st["bodyText"])
        c.check("备货矩阵 ≥10 行", st["matrixRows"] >= 11,
                f"got {st['matrixRows']}")
        c.check("试听键在场", st["playBtns"] >= 1, f"got {st['playBtns']}")
        c.check("贴合度圆点在场", st["vmDots"] >= 1, f"got {st['vmDots']}")
        c.check("曲库 3 行", st["tplRows"] >= 4, f"got {st['tplRows']}")
        c.check("曲目启用开关在场", st["tplToggles"] >= 3)
        c.check("声库卡 ≥3", st["voiceCards"] >= 3, f"got {st['voiceCards']}")
        c.check("feat 一致性：新端点可用 ↔ 补货键出现",
                (st["supplyBtns"] > 0) == bool(feat_v2),
                f"feat_v2={feat_v2} btns={st['supplyBtns']}")
        c.check("迷你播放器默认隐藏", st["playerHidden"])
        c.check("i18n 无裸键", "sg_" not in st["bodyText"])
        c.check("零 pageerror", not page_errors, "; ".join(page_errors[:3]))
        browser.close()
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(main())
