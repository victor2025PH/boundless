# -*- coding: utf-8 -*-
"""Messenger 应用内登录（表单中继）前端渲染器浏览器门禁（夹具模式）。

**夹具模式**（与 tools/verify_goal_form_ui.py 同族）：file:// 自包含页面内联
``src/web/static/workspace/connect_relay.js``，用 playwright 真浏览器驱动 ConnectRelay
的渲染器走一遍「账密 → 输入 → 同步骤不清屏 → 2FA → 提交收值 → 成功」全链，验证 node 单测
覆盖不到的**薄 DOM 壳**（render 的重渲去重 + data-relay-field 收值 + 提交回调）。

零实例依赖、零生产写入：不连后端、不登录任何真账号，纯前端组件在空白页里跑。缺 playwright
→ SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。

关键不变量（回归网）：
  · credentials 渲出 email+password；2FA 渲出 code 并替换掉账密字段；
  · **同步骤重渲不清屏**——用户已输入的账密在下一轮 poll（同 spec）后仍在（cp-goal 草稿
    幸存事故同类，登录表单被 2.5s 轮询清空会更致命）；
  · 提交按 data-relay-field 收值 → onSubmit(step, values) 拿到所填内容；
  · 成功态渲终态。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

REPO = Path(__file__).resolve().parents[1]
RELAY_JS = REPO / "src" / "web" / "static" / "workspace" / "connect_relay.js"


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        okk = bool(cond)
        self.results.append((name, okk))
        print(f"  [{'PASS' if okk else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return okk

    def summary(self) -> int:
        fails = [n for n, okk in self.results if not okk]
        total = len(self.results)
        print(f"\n== 表单中继渲染器验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def build_fixture_page(tmp: Path) -> Path:
    js = RELAY_JS.read_text(encoding="utf-8")
    html = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>relay fixture</title>"
            "</head><body><div id=\"host\"></div><script>\n" + js + "\n</script></body></html>")
    fp = tmp / "relay_fixture.html"
    fp.write_text(html, encoding="utf-8")
    return fp


# 单链场景：一个 host 贯穿账密→输入→同步骤重渲→2FA→提交→成功，模拟坐席真实节奏 + 轮询重渲。
_SCEN_JS = r"""
async () => {
  const CR = window.ConnectRelay;
  if (!CR) return { error: 'ConnectRelay 未加载' };
  const host = document.getElementById('host');
  const submits = [];
  const handlers = { t: (k) => k, onSubmit: (step, vals) => submits.push({ step: step, vals: vals }) };

  // 1) 账密渲染
  CR.render(host, CR.relayFormView({ step: 'credentials', fields: ['email', 'password'] }), handlers);
  const hasEmail = !!host.querySelector('[data-relay-field="email"]');
  const hasPw = !!host.querySelector('[data-relay-field="password"]');
  const hasSubmit = !!host.querySelector('[data-relay-submit]');

  // 2) 用户输入
  const emailEl = host.querySelector('[data-relay-field="email"]');
  const pwEl = host.querySelector('[data-relay-field="password"]');
  emailEl.value = 'a@b.com';
  pwEl.value = 'secret-pass';

  // 3) 同步骤重渲（模拟下一轮 poll 返回同 spec）——绝不能清掉正在输入的内容
  CR.render(host, CR.relayFormView({ step: 'credentials', fields: ['email', 'password'] }), handlers);
  const preservedEmail = host.querySelector('[data-relay-field="email"]').value === 'a@b.com';
  const preservedPw = host.querySelector('[data-relay-field="password"]').value === 'secret-pass';

  // 4) 提交收值
  host.querySelector('[data-relay-submit]').click();
  const submitOk = submits.length === 1 && submits[0].step === 'credentials'
    && submits[0].vals.email === 'a@b.com' && submits[0].vals.password === 'secret-pass';

  // 5) 转 2FA：code 字段替换掉账密字段（步骤变→重渲）
  CR.render(host, CR.relayFormView({ step: 'twofactor', fields: ['code'] }), handlers);
  const hasCode = !!host.querySelector('[data-relay-field="code"]');
  const noEmailNow = !host.querySelector('[data-relay-field="email"]');

  // 6) 密码错：回账密步 + 错误横幅（步骤同但 error 变→重渲）
  CR.render(host, CR.relayFormView({ step: 'credentials', fields: ['email', 'password'], error: true }), handlers);
  const errBanner = !!host.querySelector('.connect-relay-error[role="alert"]');

  // 7) 成功终态
  CR.render(host, CR.relayFormView({ status: 'authorized' }), handlers);
  const successMode = !!host.querySelector('[data-relay-mode="success"]');

  // 8) driveTick 编排：注入 stub fetchJson，跑「拉 step→渲染→提交→POST 收到」
  const host2 = document.createElement('div'); host2.id = 'host2'; document.body.appendChild(host2);
  const posts = [];
  let stepResp = { status: 'pending', step: 'credentials', fields: ['email', 'password'] };
  const fetchJson = (url, init) => {
    if (init && init.method === 'POST') { posts.push({ url: url, body: JSON.parse(init.body) }); return Promise.resolve({ ok: true, submitted: true }); }
    return Promise.resolve(stepResp);
  };
  await CR.driveTick({ container: host2, stepUrl: '/step', submitUrl: '/submit', fetchJson: fetchJson, t: (k) => k });
  const tickRendered = !!host2.querySelector('[data-relay-field="email"]');
  host2.querySelector('[data-relay-field="email"]').value = 'x@y.com';
  host2.querySelector('[data-relay-field="password"]').value = 'pw';
  host2.querySelector('[data-relay-submit]').click();
  const tickPosted = posts.length === 1 && posts[0].url === '/submit'
    && posts[0].body.step === 'credentials' && posts[0].body.values.email === 'x@y.com';

  return { hasEmail, hasPw, hasSubmit, preservedEmail, preservedPw, submitOk,
           hasCode, noEmailNow, errBanner, successMode, tickRendered, tickPosted };
}
"""


def run(headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with tempfile.TemporaryDirectory(prefix="relay_ui_probe_") as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            page.goto(page_fp.as_uri())
            ck.check("ConnectRelay 组件已加载", page.evaluate("!!window.ConnectRelay"))
            r = page.evaluate(_SCEN_JS)
            if not isinstance(r, dict) or r.get("error"):
                ck.check("场景链执行", False, str(r))
                browser.close()
                return ck.summary()
            ck.check("[1] 账密态渲出 email+password+提交钮",
                     r.get("hasEmail") and r.get("hasPw") and r.get("hasSubmit"))
            ck.check("[3] 同步骤重渲不清屏（账密输入幸存 2.5s 轮询）",
                     r.get("preservedEmail") and r.get("preservedPw"))
            ck.check("[4] 提交按 data-relay-field 收值 → onSubmit(step,values)",
                     r.get("submitOk"))
            ck.check("[5] 转 2FA：code 字段替换账密字段",
                     r.get("hasCode") and r.get("noEmailNow"))
            ck.check("[6] 密码错回账密步 + 错误横幅（role=alert）", r.get("errBanner"))
            ck.check("[7] 授权→成功终态", r.get("successMode"))
            ck.check("[8] driveTick：拉 step→渲染原生表单", r.get("tickRendered"))
            ck.check("[8] driveTick：提交经注入 fetchJson POST relay-submit + submitPayload",
                     r.get("tickPosted"))
            browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    return run(args.headed)


if __name__ == "__main__":
    sys.exit(main())
