# -*- coding: utf-8 -*-
"""多窗口协调器（__wsMultiWin v2）+ 窗口唯一性入口（_win_unique）真浏览器验证。

**为什么需要它**：坐席工作台的多开治理是**纯前端逻辑**（localStorage 主控位 +
storage 事件 + 待机细条 + 命名窗口复用），既有 pytest 门禁只能扫「函数有没有挂
window」，扫不出「两个标签页之间的互斥/自动交接到底成不成立」。而模板改动是
热更新——保存即上生产，没有「未部署」缓冲。故用真浏览器把这条不变量钉住。

与 ``tools/smoke_acceptance.py`` 同族（同一 dev/prod 实例 + token 登录 + Playwright），
只读：不发消息、不改配置，只开标签页观察协调器状态与命名窗口复用。

用法::

    python tools/verify_multiwin_ui.py                       # 打默认实例
    python tools/verify_multiwin_ui.py --base http://localhost:18901
    python tools/verify_multiwin_ui.py --shots out/          # 存证截图
    python tools/verify_multiwin_ui.py --headed              # 肉眼看一遍

覆盖的不变量（2026-08-03 v2 起，弹窗版断言见 git 历史）：
  1. 单窗口 → 成为主控、无待机细条、持提示音领导权、自报命名窗口名
     （aitr_workspace::<host>）、入口 helper 已挂 window。
  2. 第二窗口（前台可见打开）→ **自动接管**主控（v2 不再弹「在此使用/保持待机」
     选择遮罩，v1 的 #mw-mask 必须不存在）；旧窗口经 storage 事件自动退待机
     （待机细条 #mw-pill + ``__wsStandby`` 旗标 + 让出提示音权 = 轮询/SSE/响铃全停）。
  3. 待机窗口内任意点击（pointerdown）→ **自动收回**主控，对侧退待机
     （「焦点即主控」，互斥恒成立、可来回切，全程零弹窗）。
  4. 管理后台页 → ``__openUniqueUrl('/workspace')`` 首开命名窗口；再次调用
     **复用同一窗口**（总页数不增），不重复开标签页。
  5. 工作台子页（/workspace/dash 等）走独立辅助窗（aitr_wsub）——子页深链共用
     一个标签页轮换，且**不挤占**坐席收件箱专窗（聊天现场是全站最贵的状态）。
  6. 会话深链免刷新（P1-②）：收件箱在位时 ``?conv=`` 深链走 postMessage 站内交接，
     收件箱**不整页重载**（JS 哨兵变量交接后仍存活）。探针用不存在的 cid
     （只读救援查询 + 一次「不在列表」toast/计数，不碰真实会话的已读状态）。
  7. PWA 应用窗（display-mode: standalone，任务栏固定的「坐席工作台」）：workspace/
     admin 主入口**本窗原地互切**、绝不逃逸成普通浏览器标签页（app 窗自成浏览上下文组，
     window.open 命名寻址够不到组外 → Chrome 落成标签页＝「点坐席工作台变网页版」，
     2026-08-03 实测）；wsub 子页深链维持默认放行。CDP 不支持 display-mode 仿真，
     用 init script 垫 matchMedia 激活 _win_unique 的 APP_WIN 分支。

注意：``127.0.0.1`` 在部分 Chromium 环境不可达，默认用 ``localhost``。
token 从实例数据根读取，**绝不打印**。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# 协调器状态读取脚本：键名与 workspace_base.html::__wsMultiWin / _win_unique.html 保持一致
_STATE_JS = """() => {
  const mw = window.__wsMultiWin || null;
  let primary = null;
  try {
    primary = JSON.parse(localStorage.getItem('aitr.mw.primary::/workspace') || 'null');
  } catch (e) {}
  const pill = document.getElementById('mw-pill');
  const pillVisible = !!(pill && pill.style.display === 'flex');
  const txt = (id) => {
    const el = document.getElementById(id);
    return el ? String(el.textContent || '') : '';
  };
  let winName = '';
  try { winName = String(window.name || ''); } catch (e) {}
  return {
    hasCoordinator: !!mw,
    winId: mw ? mw.winId : null,
    isStandby: mw ? mw.isStandby() : null,
    globalStandby: !!window.__wsStandby,
    canSound: mw ? mw.canSound() : null,
    primaryId: primary ? primary.id : null,
    pillVisible: pillVisible,
    pillText: pillVisible ? txt('mw-pill-text') : '',
    maskPresent: !!document.getElementById('mw-mask'),
    helperReady: (typeof window.__openUnique === 'function'
                  && typeof window.__openUniqueUrl === 'function'),
    winName: winName,
  };
}"""


# display-mode 仿真：页面脚本运行前垫 matchMedia（跨导航持续），让 _win_unique
# 判定自己跑在 PWA 应用窗里（APP_WIN 分支）。其余查询透传原生实现。
_STANDALONE_SHIM_JS = """
(() => {
  const orig = window.matchMedia ? window.matchMedia.bind(window) : null;
  const stub = (q, m) => ({ matches: m, media: String(q),
    addListener(){}, removeListener(){}, addEventListener(){},
    removeEventListener(){}, dispatchEvent(){ return false; } });
  window.matchMedia = (q) => {
    if (String(q).indexOf('display-mode: standalone') >= 0) return stub(q, true);
    return orig ? orig(q) : stub(q, false);
  };
})();
"""


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

    host = base.split("//", 1)[-1].rstrip("/")
    ws_name = f"aitr_workspace::{host}"

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        # 同一 context = 同一 profile → 共享 localStorage + storage 事件（正是多开场景）
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.request.post(base + "/login", form={"auth_token": token})

        print("== 1. 第一个窗口：成为主控、无待机细条、helper 就位 ==")
        t1 = ctx.new_page()
        t1.goto(base + "/workspace", wait_until="domcontentloaded")
        t1.wait_for_timeout(2500)
        s1 = t1.evaluate(_STATE_JS)
        if not ck.check("协调器已加载（__wsMultiWin 存在）", s1["hasCoordinator"],
                        f"win={s1['winId']}"):
            print("  [ABORT] 页面没有协调器——检查是否登录成功 / 模板是否含 v2 代码")
            browser.close()
            return ck.summary() or 1
        ck.check("窗口1 非待机", s1["isStandby"] is False)
        ck.check("窗口1 持有主控位", s1["primaryId"] == s1["winId"])
        ck.check("窗口1 无待机细条", s1["pillVisible"] is False)
        ck.check("窗口1 持提示音领导权", s1["canSound"] is True)
        ck.check("窗口1 已自报命名窗口名", s1["winName"] == ws_name,
                 f"name={s1['winName']!r}")
        ck.check("入口 helper 已挂 window", s1["helperReady"] is True)

        print("== 2. 第二个窗口（前台）：自动接管，零弹窗；旧窗自动退待机 ==")
        t2 = ctx.new_page()
        t2.goto(base + "/workspace", wait_until="domcontentloaded")
        t2.wait_for_timeout(2500)
        s2 = t2.evaluate(_STATE_JS)
        ck.check("窗口2 自动接管为主控（v2 不再问人）",
                 s2["isStandby"] is False and s2["primaryId"] == s2["winId"])
        ck.check("窗口2 无 v1 选择遮罩（#mw-mask 不存在）", s2["maskPresent"] is False)
        ck.check("窗口2 无待机细条", s2["pillVisible"] is False)
        ck.check("窗口2 取得提示音领导权", s2["canSound"] is True)
        s1c = _wait_standby(t1, want=True)
        ck.check("窗口1 经 storage 事件自动退待机", s1c["isStandby"] is True)
        ck.check("窗口1 出待机细条（非阻断）", s1c["pillVisible"] is True,
                 f"text={s1c['pillText']!r}")
        ck.check("待机细条文案=另一窗口在用", "另一窗口" in (s1c["pillText"] or "")
                 or "another window" in (s1c["pillText"] or ""))
        ck.check("窗口1 让出提示音权（不重复响铃）", s1c["canSound"] is False)
        ck.check("窗口1 全局待机旗标置起（轮询/SSE 停摆开关）",
                 s1c["globalStandby"] is True)
        if shots:
            t1.screenshot(path=str(shots / "mw_tab1_standby_pill.png"))

        print("== 3. 待机窗口内任意点击 → 自动收回主控（焦点即主控，零弹窗） ==")
        t1.bring_to_front()
        t1.mouse.click(640, 500)   # 远离细条按钮的空白区：验证「任意点击即收回」
        t1.wait_for_timeout(1200)
        s1d = t1.evaluate(_STATE_JS)
        ck.check("窗口1 自动收回主控",
                 s1d["isStandby"] is False and s1d["primaryId"] == s1d["winId"])
        ck.check("窗口1 待机细条消失", s1d["pillVisible"] is False)
        ck.check("窗口2 退回待机", _wait_standby(t2, want=True)["isStandby"] is True)
        ck.check("窗口2 出待机细条", t2.evaluate(_STATE_JS)["pillVisible"] is True)

        print("== 4. 管理后台入口：命名窗口复用（再点不再新开标签页） ==")
        ta = ctx.new_page()
        ta.goto(base + "/", wait_until="domcontentloaded")
        ta.wait_for_timeout(1200)
        sa = ta.evaluate(_STATE_JS)
        ck.check("后台页 helper 已挂 window", sa["helperReady"] is True)
        ck.check("后台页已自报命名窗口名", sa["winName"] == f"aitr_admin::{host}",
                 f"name={sa['winName']!r}")
        n_before = len(ctx.pages)
        with ctx.expect_page() as pinfo:
            ta.evaluate("window.__openUniqueUrl('/workspace','workspace')")
        wpop = pinfo.value
        wpop.wait_for_url("**/workspace**", timeout=8000)
        wpop.wait_for_timeout(1500)
        ck.check("首开：命名窗口落在 /workspace", "/workspace" in wpop.url)
        n_open = len(ctx.pages)
        ck.check("首开：新增恰好一个窗口", n_open == n_before + 1,
                 f"{n_before} -> {n_open}")
        ta.evaluate("window.__openUniqueUrl('/workspace','workspace')")
        ta.wait_for_timeout(1500)
        ck.check("再点：总窗口数不增（复用而非新开）", len(ctx.pages) == n_open,
                 f"pages={len(ctx.pages)}")
        ck.check("再点：命名窗口仍在 /workspace", "/workspace" in wpop.url)
        if shots:
            wpop.screenshot(path=str(shots / "mw_named_window_reuse.png"))

        print("== 5. 工作台子页：独立辅助窗轮换，不挤占坐席收件箱专窗 ==")
        n_seat = len(ctx.pages)
        with ctx.expect_page() as pinfo2:
            ta.evaluate("window.__openUniqueUrl('/workspace/dash','workspace')")
        wsub = pinfo2.value
        wsub.wait_for_url("**/workspace/dash**", timeout=8000)
        wsub.wait_for_timeout(1200)
        ck.check("子页落在辅助窗（新增恰好一个）", len(ctx.pages) == n_seat + 1,
                 f"{n_seat} -> {len(ctx.pages)}")
        # 只比路径：收件箱自己会往 URL 写视角 hash（#plat=…）/查询参数，不算被挤占
        _seat_path = wpop.url.split("#")[0].split("?")[0].rstrip("/")
        ck.check("坐席收件箱专窗未被挤占（仍在 /workspace）",
                 _seat_path.endswith("/workspace"), f"url={wpop.url}")
        n_sub = len(ctx.pages)
        ta.evaluate("window.__openUniqueUrl('/workspace/tasks','workspace')")
        try:   # 忙机上导航提交可能慢于固定等待——轮询到位后再断言，防时序毛刺
            wsub.wait_for_url("**/workspace/tasks**", timeout=8000)
        except Exception:
            pass
        ck.check("再开另一子页：辅助窗内轮换（总窗口数不增）", len(ctx.pages) == n_sub,
                 f"pages={len(ctx.pages)}")
        ck.check("辅助窗已切到新子页", "/workspace/tasks" in wsub.url, f"url={wsub.url}")

        print("== 6. 会话深链免刷新：postMessage 站内交接，收件箱不整页重载 ==")
        wpop.evaluate("window.__mwProbe = 12345")
        n_dl = len(ctx.pages)
        # 探针 cid 刻意不存在（tg:__probe__:none）：救援查询是只读 GET，绝不动真实会话已读态
        ta.evaluate("window.__openUniqueUrl('/workspace?conv=tg%3A__probe__%3Anone','workspace')")
        ta.wait_for_timeout(2500)   # 覆盖 350ms ack 窗 + 站内开会话尝试
        ck.check("深链交接：总窗口数不增", len(ctx.pages) == n_dl,
                 f"pages={len(ctx.pages)}")
        probe = wpop.evaluate("window.__mwProbe || 0")
        ck.check("收件箱未整页重载（ack 生效，哨兵存活）", probe == 12345,
                 f"probe={probe}")
        _seat_path2 = wpop.url.split("#")[0].split("?")[0].rstrip("/")
        ck.check("收件箱专窗仍在 /workspace", _seat_path2.endswith("/workspace"),
                 f"url={wpop.url}")

        print("== 7. PWA 应用窗（standalone 模拟）：主入口本窗互切，不逃逸成网页版标签页 ==")
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx2.add_init_script(_STANDALONE_SHIM_JS)
        ctx2.request.post(base + "/login", form={"auth_token": token})
        pw = ctx2.new_page()
        pw.goto(base + "/workspace", wait_until="domcontentloaded")
        pw.wait_for_timeout(1500)
        ck.check("standalone 模拟生效",
                 pw.evaluate("matchMedia('(display-mode: standalone)').matches") is True)
        n0 = len(ctx2.pages)
        ret = pw.evaluate("window.__openUniqueUrl('/','admin')")
        pw.wait_for_timeout(1800)
        ck.check("app 窗：管理后台入口本窗原地导航（不新开）",
                 ret is True and "/workspace" not in pw.url and len(ctx2.pages) == n0,
                 f"url={pw.url} pages={len(ctx2.pages)}")
        # 新 profile 首进后台会弹 onboarding 遮罩挡点击（verify_care_ui 实测同款）→ 摘除
        pw.evaluate("(() => { const m = document.getElementById('onboard-modal'); if (m) m.remove(); })()")
        try:
            pw.locator('a[data-winname="workspace"][href="/workspace"]').first.click(timeout=8000)
            pw.wait_for_timeout(2000)
        except Exception:
            pass   # 点击失败让下面的路径断言如实变红
        _pw_path = pw.url.split("#")[0].split("?")[0].rstrip("/")
        ck.check("app 窗：侧栏「坐席工作台」本窗返回（不被踢成网页版）",
                 _pw_path.endswith("/workspace") and len(ctx2.pages) == n0,
                 f"url={pw.url} pages={len(ctx2.pages)}")
        ck.check("app 窗：wsub 子页深链维持默认放行（false）",
                 pw.evaluate("window.__openUniqueUrl('/workspace/tasks','workspace')") is False)
        ctx2.close()

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多窗口协调器 v2 + 窗口唯一性真浏览器验证（只读）")
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
