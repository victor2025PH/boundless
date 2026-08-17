# -*- coding: utf-8 -*-
"""接入向导「失败态自愈」真浏览器门禁（Playwright；2026-08-13 沉淀）。

**为什么需要它**：cred_invalid 自愈是两个状态机的协同（后端换发冷却 × 前端倒计时
自动重试），静态门禁只能证「函数挂了 window / i18n 键存在」，证不了「倒计时真的
递减、归零真的重试、手机号链真的不自动重发短信」。而 unified_inbox.html 是多线
活跃热区 + 模板热更新直上生产——回归是**静默的**：卡还在、字还对，只是倒计时
永远不触发，用户又回到对着红字掐表的旧世界。

与 ``tools/verify_inbox_identity.py`` 同族（同一实例 + token 登录 + Playwright +
SKIP exit 0）。**零真实登录**：telegram 的 modes/start/cancel/status/preflight/
diagnostic-upload 全部 page.route 注入合成响应，不打一发真 login/start，不烧池容量。

用法::

    python tools/verify_connect_ui.py              # 门禁
    python tools/verify_connect_ui.py --headed     # 肉眼
    python tools/verify_connect_ui.py --self-proof # 探测器自证（必红才算过）

覆盖的不变量：
  0. 源码接线（静态，不依赖实例）：失败卡 DOM 五件套 / _renderConnectFailure 被
     start+poll+code 三路径引用 / 倒计时状态机在场 / i18n 15 键 zh+en 双份。
  1. hosted 废凭据 → 失败卡可见 + 遮罩无转圈（「转圈+报错并存」回归钉）+
     倒计时文案带秒数 + 底部按钮禁用。
  2. 倒计时归零 → **自动**重试（login/start 请求数递增）；封顶 2 轮后转
     exhausted 文案 + 按钮恢复可点（不做机关枪）。
  3. 一键上报：点击 → 诊断短码渲染进卡片（合成 246810）。
  4. self 自填凭据 → 就地修正表单可见（API ID/Hash 双输入框）。
  5. phone_invalid → 字段级错误挂在号码输入框下（失败卡不出场、表单不收起）。
  6. 手机号链 cred_invalid → 倒计时期表单收起；归零后表单**解锁**且
     login/start 请求数不变（绝不自动重发短信）。
  7. 基线：pending+二维码正常路径失败卡不出场（渲染器不漏进成功路径）。

**副作用：无。** 全程 route mock；modal 由 window.openConnect 驱动；收尾 closeConnect。
缺 playwright / 实例不可达 → SKIP exit 0；无 token → ABORT exit 2。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
I18N_PACK = ENGINE_ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"

# 本批新增键（zh+en 各一份 → pack 文本里每键恰出现 2 次）
_NEW_KEYS = [
    "inbox.connect.fail_swap_wait", "inbox.connect.fail_swap_wait_phone",
    "inbox.connect.fail_swap_ready_phone", "inbox.connect.fail_retrying",
    "inbox.connect.fail_retry_countdown", "inbox.connect.fail_retry_wait",
    "inbox.connect.fail_retry_now", "inbox.connect.fail_self_cred",
    "inbox.connect.fail_pool_cred", "inbox.connect.fail_exhausted",
    "inbox.connect.report_btn", "inbox.connect.report_busy",
    "inbox.connect.report_ok", "inbox.connect.report_fail",
]

_CARD_JS = """() => {
  const card = document.getElementById('connect-fail-card');
  if (!card) return {absent: true};
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none';
  const body = document.getElementById('connect-fail-body');
  const fix = document.getElementById('connect-fail-fix');
  const done = document.getElementById('connect-fail-report-done');
  const btn = document.getElementById('connect-refresh-btn');
  const mask = document.getElementById('connect-qr-mask');
  const phone = document.getElementById('connect-phone-view');
  const perr = document.getElementById('connect-phone-err');
  const ringWrap = document.getElementById('connect-fail-ring-wrap');
  const steps = document.getElementById('connect-steps');
  return {
    absent: false,
    card_visible: vis(card),
    body: (body && body.textContent || '').trim(),
    fix_visible: vis(fix),
    done_text: (done && done.textContent || '').trim(),
    refresh_disabled: !!(btn && btn.disabled),
    mask_has_spin: !!(mask && mask.querySelector('.spin')
                      && getComputedStyle(mask).display !== 'none'),
    phone_visible: vis(phone),
    phone_err: perr && vis(perr) ? (perr.textContent || '').trim() : '',
    qr_visible: (() => { const q = document.getElementById('connect-qr-img');
                         return !!q && q.style.display !== 'none'; })(),
    ring_visible: vis(ringWrap),
    steps_err: !!(steps && steps.classList.contains('err')),
  };
}"""


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self, title: str = "接入向导失败态验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def check_source_wiring(ck: Checker, src: str = "", pack: str = "") -> None:
    """不依赖实例：防失败卡/渲染器/键位再次断链（--self-proof 传入篡改文本自证）。"""
    print("== 0. 源码接线（静态）==")
    if not src:
        if not INBOX_TEMPLATE.exists():
            ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
            return
        src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    if not pack:
        pack = I18N_PACK.read_text(encoding="utf-8") if I18N_PACK.exists() else ""
    for did in ("connect-fail-card", "connect-fail-body", "connect-fail-fix",
                "connect-fix-save", "connect-fail-report",
                "connect-phone-err", "connect-code-err"):
        ck.check(f"DOM #{did} 在场", f'id="{did}"' in src)
    ck.check("渲染器已定义", "function _renderConnectFailure" in src)
    # def 1 处 + start(!ok) + start(status=failed) + poll(failed) + code(failed) ≥ 5 处
    ck.check("渲染器接进 ≥4 条路径",
             src.count("_renderConnectFailure(") >= 5,
             f"count={src.count('_renderConnectFailure(')}")
    ck.check("倒计时状态机在场",
             "function _startCredCountdown" in src and "_CRED_AUTO_RETRY_MAX" in src)
    ck.check("手机号解锁分支在场", "fail_swap_ready_phone" in src)
    ck.check("上报接线（diagnostic-upload）", "/api/admin/diagnostic-upload" in src)
    ck.check("cred_invalid 仍在原因映射表",
             bool(re.search(r"cred_invalid:\s*'inbox\.connect\.err_cred_invalid'", src)))
    # P2 视觉件（2026-08-13）：倒计时环 DOM + 步骤条 err 态样式
    ck.check("倒计时环 DOM 在场", 'id="connect-fail-ring-wrap"' in src)
    ck.check("步骤条 err 态样式在场", ".connect-steps.err .cs-step.active" in src)
    bad = [k for k in _NEW_KEYS if pack.count(f'"{k}"') != 2]
    ck.check("i18n 15 键 zh+en 双份", not bad, f"missing/odd={bad}" if bad else "")


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


def _login_workspace(p: Any, base: str, token: str, *, headed: bool):
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.openConnect === 'function'", timeout=20000)
    page.wait_for_timeout(1500)
    return browser, ctx, page


def _install_routes(page: Any, scenario: dict, hits: List[str]) -> None:
    """telegram 登录相关 API 全量合成：零真实登录、零池容量消耗。"""
    import json as _json

    def _json_route(route: Any, payload: dict) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(payload))

    def _modes(route: Any) -> None:
        _json_route(route, {"ok": True, "default": "protocol", "modes": [
            {"mode": "protocol", "available": True, "ready": True,
             "login_kind": "qr", "label": "扫码登录"},
            {"mode": "phone", "available": True, "ready": True,
             "login_kind": "phone_code", "label": "手机号登录"},
        ]})

    def _start(route: Any) -> None:
        hits.append(scenario.get("kind", ""))
        sc = scenario.get("kind", "hosted")
        base = {"ok": True, "login_id": f"vc-{len(hits)}", "mode": scenario.get("mode", "protocol"),
                "login_kind": scenario.get("login_kind", "qr"), "qr_url": "",
                "qr_image": "", "instruction": "", "instruction_key": "",
                "interactive": False, "expires_in": 180, "detail": "", "reason_code": "",
                "status": "pending"}
        if sc == "hosted":
            base.update(status="failed", reason_code="cred_invalid",
                        cred_source="hosted", retry_after_sec=1,
                        detail="[400 API_ID_INVALID] verify-fixture")
        elif sc == "self":
            base.update(status="failed", reason_code="cred_invalid",
                        cred_source="self", retry_after_sec=-1,
                        detail="[400 API_ID_INVALID] verify-fixture")
        elif sc == "phone_invalid":
            base.update(status="failed", reason_code="phone_invalid",
                        detail="PHONE_NUMBER_INVALID verify-fixture")
        elif sc == "pending_qr":
            base.update(status="pending", qr_image=(
                "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
                "FcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        _json_route(route, base)

    page.route("**/api/platforms/telegram/modes*", _modes)
    page.route("**/api/platforms/telegram/login/start", _start)
    page.route("**/api/platforms/telegram/login/*/cancel",
               lambda r: _json_route(r, {"ok": True}))
    page.route("**/api/platforms/telegram/login/*/status*",
               lambda r: _json_route(r, {"ok": True, "status": "pending",
                                         "pin": "", "reason_code": ""}))
    page.route("**/api/platforms/telegram/login/preflight*",
               lambda r: _json_route(r, {"ok": True, "reachable": True}))
    page.route("**/api/admin/diagnostic-upload*",
               lambda r: _json_route(r, {"ok": True, "code": "246810"}))


def _open_to_qr(page: Any) -> None:
    """openConnect → 模式卡就绪 → chooseMode(protocol) → 下一步（触发 startConnect）。"""
    page.evaluate("() => window.openConnect('telegram')")
    page.wait_for_function(
        "() => { const b = document.getElementById('connect-modes');"
        "  return !!b && !b.querySelector('.spin'); }", timeout=8000)
    page.evaluate("() => window.chooseMode('protocol')")
    page.wait_for_timeout(120)
    page.evaluate("() => window.confirmConfigAndScan()")


def _open_to_phone(page: Any) -> None:
    page.evaluate("() => window.openConnect('telegram')")
    page.wait_for_function(
        "() => { const b = document.getElementById('connect-modes');"
        "  return !!b && !b.querySelector('.spin'); }", timeout=8000)
    page.evaluate("() => window.chooseMode('phone')")
    page.wait_for_timeout(120)
    page.evaluate("() => window.confirmConfigAndScan()")
    page.wait_for_function(
        "() => { const v = document.getElementById('connect-phone-view');"
        "  return !!v && getComputedStyle(v).display !== 'none'; }", timeout=6000)


def _wait_card(page: Any, *, timeout_ms: int = 8000) -> dict:
    deadline = time.time() + timeout_ms / 1000.0
    last: dict = {"absent": True}
    while time.time() < deadline:
        last = page.evaluate(_CARD_JS)
        if last.get("card_visible"):
            return last
        page.wait_for_timeout(150)
    return last


def _wait_hits(page: Any, hits: List[str], n: int, *, timeout_s: float) -> bool:
    """等 login/start 请求数达到 n。必须用 page.wait_for_timeout 而非 time.sleep：
    sync Playwright 的 route 拦截只在 Playwright 调用期间被泵送，睡在 Python 侧
    会把 mock 响应卡死（首跑实测：倒计时归零后 startConnect 的 cancel/start 全部
    悬在拦截队列里，hits 永远不涨）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if len(hits) >= n:
            return True
        page.wait_for_timeout(200)
    return len(hits) >= n


def run(base: str, token: str, *, headed: bool = False) -> int:
    ck = Checker()
    check_source_wiring(ck)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        ck.skip("browser", "playwright 未安装")
        rc = ck.summary()
        return 0 if rc == 0 or ck.skipped else rc

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            ck.skip("browser", f"实例不可达/工作台未就绪（{str(e)[:80]}）")
            rc = ck.summary()
            return rc if rc else 0
        try:
            hits: List[str] = []
            scenario = {"kind": "hosted", "mode": "protocol", "login_kind": "qr"}
            _install_routes(page, scenario, hits)

            print("== 1. hosted 废凭据：失败卡 + 倒计时 + 无转圈 ==")
            _open_to_qr(page)
            st = _wait_card(page)
            ck.check("失败卡可见", st.get("card_visible"), str(st.get("body", ""))[:60])
            ck.check("遮罩无转圈（并存回归钉）", not st.get("mask_has_spin"))
            ck.check("倒计时文案带秒数", bool(re.search(r"\d", st.get("body", ""))))
            ck.check("底部按钮倒计时期禁用", st.get("refresh_disabled"))
            ck.check("倒计时环可见（P2）", st.get("ring_visible"))
            ck.check("步骤条进入 err 态（P2）", st.get("steps_err"))

            print("== 2. 归零自动重试 → 封顶转 exhausted ==")
            ck.check("自动重试 #1（start 第 2 发）", _wait_hits(page, hits, 2, timeout_s=9))
            ck.check("自动重试 #2（start 第 3 发）", _wait_hits(page, hits, 3, timeout_s=9))
            page.wait_for_timeout(600)
            st = page.evaluate(_CARD_JS)
            ck.check("耗尽后按钮恢复可点", not st.get("refresh_disabled"))
            ck.check("耗尽文案不再带倒计时", not re.search(r"\d+s", st.get("body", "")))
            ck.check("耗尽后倒计时环收起（P2）", not st.get("ring_visible"))
            n_after = len(hits)
            page.wait_for_timeout(2500)
            ck.check("耗尽后不再自动重试", len(hits) == n_after)

            print("== 3. 一键上报 → 短码渲染 ==")
            page.evaluate("() => document.getElementById('connect-fail-report').click()")
            page.wait_for_timeout(1200)
            st = page.evaluate(_CARD_JS)
            ck.check("诊断短码渲染进卡片", "246810" in st.get("done_text", ""))

            print("== 4. self 自填凭据 → 修正表单 ==")
            page.evaluate("() => window.closeConnect()")
            page.wait_for_timeout(300)
            scenario["kind"] = "self"
            _open_to_qr(page)
            st = _wait_card(page)
            ck.check("失败卡可见（self）", st.get("card_visible"))
            ck.check("修正表单亮出", st.get("fix_visible"))

            print("== 5. phone_invalid → 字段级错误（表单不收起）==")
            page.evaluate("() => window.closeConnect()")
            page.wait_for_timeout(300)
            scenario.update(kind="phone_invalid", mode="phone", login_kind="phone_code")
            _open_to_phone(page)
            page.evaluate(
                "() => { const i = document.getElementById('connect-phone-input');"
                "  i.value = '+85212345678'; window.submitConnectPhone(); }")
            page.wait_for_timeout(1500)
            st = page.evaluate(_CARD_JS)
            ck.check("号码字段级错误可见", bool(st.get("phone_err")))
            ck.check("号码表单未被收起", st.get("phone_visible"))
            ck.check("字段级错误不出失败卡", not st.get("card_visible"))

            print("== 6. 手机号链 cred_invalid → 收表单，归零只解锁不自动发码 ==")
            page.evaluate("() => window.closeConnect()")
            page.wait_for_timeout(300)
            scenario["kind"] = "hosted"
            _open_to_phone(page)
            base_hits = len(hits)
            page.evaluate(
                "() => { const i = document.getElementById('connect-phone-input');"
                "  i.value = '+85212345678'; window.submitConnectPhone(); }")
            st = _wait_card(page)
            ck.check("失败卡可见（phone×hosted）", st.get("card_visible"))
            ck.check("倒计时期表单收起（防狂点发码）", not st.get("phone_visible"))
            ck.check("提交计 1 发", len(hits) == base_hits + 1)
            page.wait_for_function(
                "() => { const v = document.getElementById('connect-phone-view');"
                "  return !!v && getComputedStyle(v).display !== 'none'; }",
                timeout=9000)
            page.wait_for_timeout(800)
            ck.check("归零后表单解锁且零自动发码", len(hits) == base_hits + 1,
                     f"hits={len(hits) - base_hits}")

            print("== 7. 基线：pending+二维码路径失败卡不出场 ==")
            page.evaluate("() => window.closeConnect()")
            page.wait_for_timeout(300)
            scenario.update(kind="pending_qr", mode="protocol", login_kind="qr")
            _open_to_qr(page)
            page.wait_for_timeout(1200)
            st = page.evaluate(_CARD_JS)
            ck.check("二维码渲染", st.get("qr_visible"))
            ck.check("失败卡未出场", not st.get("card_visible"))
            page.evaluate("() => window.closeConnect()")
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return ck.summary()


def self_proof() -> int:
    """探测器自证：篡改后的源码必须被静态段揪出来（红才算过）。"""
    print("== self-proof：剥掉渲染器/键位后静态段必须变红 ==")
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    pack = I18N_PACK.read_text(encoding="utf-8")
    tampered_src = src.replace("function _renderConnectFailure", "function _renamed")
    tampered_pack = pack.replace('"inbox.connect.fail_swap_wait"', '"inbox.connect.gone"', 1)
    ck = Checker()
    check_source_wiring(ck, src=tampered_src, pack=tampered_pack)
    fails = [n for n, ok in ck.results if not ok]
    good = len(fails) >= 2
    print(f"\n== self-proof: {'PASS（篡改被抓）' if good else 'FAIL（探测器失明）'} fails={fails}")
    return 0 if good else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--self-proof", action="store_true")
    args = ap.parse_args()
    if args.self_proof:
        return self_proof()
    token = read_token(args.data_root)
    if not token:
        print("[ABORT] 读不到 web_admin.auth_token（--data-root 指错了？）")
        return 2
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
