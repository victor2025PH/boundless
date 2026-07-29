# -*- coding: utf-8 -*-
"""多窗口协调器（__wsMultiWin）真浏览器验证（Playwright；2026-07-29 沉淀）。

**为什么需要它**：坐席工作台的多开治理是**纯前端逻辑**（localStorage 主控位 +
storage 事件 + 遮罩），既有 pytest 门禁只能扫「函数有没有挂 window」，扫不出
「两个标签页之间的互斥到底成不成立」。而模板改动是热更新——保存即上生产，
没有「未部署」缓冲。故用真浏览器双标签页把这条不变量钉住。

与 ``tools/smoke_acceptance.py`` 同族（同一 dev/prod 实例 + token 登录 + Playwright），
只读：不发消息、不改配置，只开两个 /workspace 标签页点遮罩按钮。

用法::

    python tools/verify_multiwin_ui.py                       # 打默认实例
    python tools/verify_multiwin_ui.py --base http://localhost:18901
    python tools/verify_multiwin_ui.py --shots out/          # 存证截图
    python tools/verify_multiwin_ui.py --headed              # 肉眼看一遍

覆盖的不变量：
  1. 单窗口 → 成为主控、无遮罩、持提示音领导权。
  2. 第二窗口 → 出接管选择遮罩（在此使用 / 保持待机），且**不自动抢**主控。
  3. 点「在此窗口使用」→ 新窗口接管；旧窗口经 storage 事件**自动退待机**
     （遮罩 + ``__wsStandby`` 旗标 + 让出提示音权 = 轮询/SSE/响铃全停）。
  4. 待机窗口点「在此窗口继续」→ 抢回主控，对侧退待机（互斥恒成立、可来回切）。

注意：``127.0.0.1`` 在部分 Chromium 环境不可达，默认用 ``localhost``。
token 从实例数据根读取，**绝不打印**。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://localhost:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# 协调器状态读取脚本：主控位键与 workspace_base.html::__wsMultiWin 保持一致
_STATE_JS = """() => {
  const mw = window.__wsMultiWin || null;
  let primary = null;
  try {
    primary = JSON.parse(localStorage.getItem('aitr.mw.primary::/workspace') || 'null');
  } catch (e) {}
  const mask = document.getElementById('mw-mask');
  const visible = !!(mask && mask.style.display === 'flex');
  const txt = (id) => {
    const el = document.getElementById(id);
    return el ? String(el.textContent || '') : '';
  };
  return {
    hasCoordinator: !!mw,
    winId: mw ? mw.winId : null,
    isStandby: mw ? mw.isStandby() : null,
    globalStandby: !!window.__wsStandby,
    canSound: mw ? mw.canSound() : null,
    primaryId: primary ? primary.id : null,
    maskVisible: visible,
    maskTitle: visible ? txt('mw-title') : '',
    maskBtn: visible ? txt('mw-use') : '',
  };
}"""


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
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
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 多窗口协调器验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _wait_standby(page: Any, want: bool, tries: int = 12) -> Dict[str, Any]:
    """等对侧标签页处理完 storage 事件（跨页事件需对方跑一轮事件循环）。"""
    st: Dict[str, Any] = {}
    for _ in range(tries):
        page.wait_for_timeout(400)
        st = page.evaluate(_STATE_JS)
        if bool(st.get("isStandby")) is want:
            break
    return st


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        # 同一 context = 同一 profile → 共享 localStorage + storage 事件（正是多开场景）
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.request.post(base + "/login", form={"auth_token": token})

        print("== 1. 第一个窗口：成为主控、无遮罩 ==")
        t1 = ctx.new_page()
        t1.goto(base + "/workspace", wait_until="domcontentloaded")
        t1.wait_for_timeout(2500)
        s1 = t1.evaluate(_STATE_JS)
        if not ck.check("协调器已加载（__wsMultiWin 存在）", s1["hasCoordinator"],
                        f"win={s1['winId']}"):
            print("  [ABORT] 页面没有协调器——检查是否登录成功 / 模板是否含 P1 代码")
            browser.close()
            return ck.summary() or 1
        ck.check("窗口1 非待机", s1["isStandby"] is False)
        ck.check("窗口1 持有主控位", s1["primaryId"] == s1["winId"])
        ck.check("窗口1 无遮罩", s1["maskVisible"] is False)
        ck.check("窗口1 持提示音领导权", s1["canSound"] is True)

        print("== 2. 第二个窗口：出接管选择遮罩，且不自动抢主控 ==")
        t2 = ctx.new_page()
        t2.goto(base + "/workspace", wait_until="domcontentloaded")
        t2.wait_for_timeout(2500)
        s2 = t2.evaluate(_STATE_JS)
        ck.check("窗口2 进待机（未接管前）", s2["isStandby"] is True)
        ck.check("窗口2 出遮罩", s2["maskVisible"] is True,
                 f"title={s2['maskTitle']!r}")
        ck.check("遮罩标题=已在另一个窗口打开", "另一个窗口" in (s2["maskTitle"] or ""))
        ck.check("遮罩主按钮=在此窗口使用", "在此" in (s2["maskBtn"] or ""),
                 f"btn={s2['maskBtn']!r}")
        ck.check("窗口2 待机时无提示音权（不重复响铃）", s2["canSound"] is False)
        ck.check("窗口1 仍是主控（新窗口不自动抢）",
                 t1.evaluate(_STATE_JS)["isStandby"] is False)
        if shots:
            t2.screenshot(path=str(shots / "mw_tab2_takeover.png"))

        print("== 3. 点「在此窗口使用」→ 接管；对侧经 storage 事件自动退待机 ==")
        t2.click("#mw-use")
        t2.wait_for_timeout(1200)
        s2c = t2.evaluate(_STATE_JS)
        ck.check("窗口2 转为主控",
                 s2c["isStandby"] is False and s2c["primaryId"] == s2c["winId"])
        ck.check("窗口2 遮罩消失", s2c["maskVisible"] is False)
        ck.check("窗口2 取得提示音领导权", s2c["canSound"] is True)
        s1c = _wait_standby(t1, want=True)
        ck.check("窗口1 经 storage 事件自动退待机", s1c["isStandby"] is True)
        ck.check("窗口1 出待机遮罩", s1c["maskVisible"] is True,
                 f"title={s1c['maskTitle']!r}")
        ck.check("待机标题=本窗口已待机", "待机" in (s1c["maskTitle"] or ""))
        ck.check("窗口1 让出提示音权", s1c["canSound"] is False)
        ck.check("窗口1 全局待机旗标置起（轮询/SSE 停摆开关）",
                 s1c["globalStandby"] is True)
        if shots:
            t1.screenshot(path=str(shots / "mw_tab1_standby.png"))

        print("== 4. 待机窗口点「在此窗口继续」→ 抢回主控，互斥恒成立 ==")
        t1.click("#mw-use")
        t1.wait_for_timeout(1200)
        s1d = t1.evaluate(_STATE_JS)
        ck.check("窗口1 抢回主控",
                 s1d["isStandby"] is False and s1d["primaryId"] == s1d["winId"])
        ck.check("窗口2 退回待机", _wait_standby(t2, want=True)["isStandby"] is True)

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多窗口协调器真浏览器验证（只读）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}；127.0.0.1 部分环境不可达）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未装 playwright（pip install playwright && playwright install chromium）")
        return 0

    # 实例不可达 → 跳过而非失败（与 regression.ps1 的 ui-regress 同哲学：
    # 环境缺失不该把「代码有没有回归」的信号污染成红）。挂进 gate_sweep -Full 的前提。
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
    print(f"== 0. 目标 {args.base} ==")
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
