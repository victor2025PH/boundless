# -*- coding: utf-8 -*-
"""Messenger 应用内登录（表单中继）driveTick 状态机 真浏览器门禁（Playwright）。

**为什么需要它**（B64 二期，2026-08-23 「点登录没反应」事故沉淀）：connect_relay.js
是接入弹窗里唯一有交互时序的组件——提交回执、verifying 超时升级、密码显隐眼睛、
Enter 提交、检查点/锁定截图视图。这些是**点击时序 + DOM 事件接线**，node 纯函数测试
只能证「视图规格算对了」，证不了「点了眼睛真的切了 type」「空字段真的没打网络」
「提交真的走了一次 relay-submit」。模板热更新直上生产 + 本组件是接入链唯一入口，
只有真浏览器能压住回归。

**夹具模式**（与 verify_cp_voice_ui.py / verify_goal_form_ui.py 同族）：file:// 自包含
页面 + 真组件（connect_relay.js）+ **stub fetchJson**——relay-step/relay-submit 全部
走注入的假响应，零边车、零实例、零网络。t() 用 key-echo（断言键名，与 i18n 词典解耦）。

覆盖不变量（编号对应 run()）：
  R1  credentials 步：head + prep 前置卡 + email/password 输入 + 密码眼睛 + 提交钮
  R2  密码眼睛：点击 password 框 type 在 password/text 间切换、换词
  R3  空字段本地即拦：只填 email 点提交 → 就地 missing 红字、**零** relay-submit 网络调用
  R4  正常提交：填全 → submitting（按钮禁用）→ 一次 relay-submit → verifying 舞台
  R5  Enter 键提交：输入框回车 = 点提交（同一次网络调用）
  R6  checkpoint：step=checkpoint → 截图视图 + 可放大图 + 出路清单
  R6b 检查点「继续」钮 → 一次 step=checkpoint 的 relay-submit
  R6c B64三期 继续回执 clicked=true → 忙态钮在整块重渲间存活 + 「已替点·跟进中」提示
  R6d B64三期 继续回执 clicked=false → miss 指路 notice + 按钮可再点
  R6e B64三期 device_confirm 子味 → 手机主视觉 + 三步清单 + 截图折叠且开合跨重渲存活
  R6f B64三期 等待生命感 → 已等待 + 会话剩余 <5min 的 meta 行
  R7  account_locked：code=account_locked → 锁定专属标题（非泛检查点文案）
  R8  success：status=authorized → 成功画勾 SVG
  R9  step 前进作废本地相位：verifying(credentials) + 服务端到 twofactor → 渲 code 表单
  R10 B64三期 解卡正反馈：continued 相位 + 服务端前进 → preparing 顶部绿色确认条

用法::
    python tools/verify_connect_relay_ui.py             # 门禁模式
    python tools/verify_connect_relay_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>connect_relay state probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;max-width:360px;">
<div id="relay" class="connect-relay-view"></div>
<script src="@@RELAY_JS@@"></script>
<script>
// stub relay-step / relay-submit：测试用例改 window.__stepResp / __submitResp 控制服务端态。
window.__stepResp = { status: 'pending', step: 'credentials', fields: ['email', 'password'] };
window.__submitResp = { ok: true };
window.__submitCalls = [];
window.__qr = '';
window.__fetchJson = function (url, init) {
  var u = String(url || '');
  if (u.indexOf('relay-submit') >= 0) {
    try { window.__submitCalls.push(JSON.parse((init && init.body) || '{}')); } catch (e) { window.__submitCalls.push({}); }
    return Promise.resolve(window.__submitResp);
  }
  return Promise.resolve(window.__stepResp);
};
window.__c = document.getElementById('relay');
// 一次 driveTick「轮询 tick」；t=key-echo（断言键名）。async 便于用例 await 到位。
window.__expiresIn = 0;
window.tick = function () {
  return window.ConnectRelay.driveTick({
    container: window.__c,
    stepUrl: '/api/x/login/id/relay-step',
    submitUrl: '/api/x/login/id/relay-submit',
    platform: 'messenger',
    qrImage: window.__qr,
    expiresIn: window.__expiresIn || 0,
    fetchJson: window.__fetchJson,
    t: function (k) { return k; },
  });
};
window.q = function (sel) { return window.__c.querySelector(sel); };
window.snap = function () {
  var mode = (window.__c.querySelector('[data-relay-mode]') || {}).getAttribute
    ? window.__c.querySelector('[data-relay-mode]').getAttribute('data-relay-mode') : '';
  var pw = window.q('[data-relay-field="password"]');
  var err = window.q('.connect-relay-error');
  var submitBtn = window.q('[data-relay-submit]');
  return {
    mode: mode,
    hasEmail: !!window.q('[data-relay-field="email"]'),
    hasPassword: !!pw,
    pwType: pw ? pw.type : '',
    hasEye: !!window.q('[data-relay-eye]'),
    hasHead: !!window.q('.connect-relay-head'),
    hasPrep: !!window.q('.connect-relay-next.prep'),
    hasSubmit: !!submitBtn,
    submitDisabled: submitBtn ? !!submitBtn.disabled : null,
    hasZoom: !!window.q('[data-relay-zoom]'),
    hasWays: !!window.q('.connect-relay-next'),
    hasCheck: !!window.q('.connect-relay-check'),
    errText: err ? err.textContent : '',
    headText: (window.q('.connect-relay-head') || {}).textContent || '',
    submitCalls: window.__submitCalls.length,
    html: window.__c.innerHTML.length,
  };
};
window.setField = function (name, val) {
  var el = window.q('[data-relay-field="' + name + '"]');
  if (el) el.value = val;
};
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    html = _HTML.replace(
        "@@RELAY_JS@@",
        (ENGINE / "src/web/static/workspace/connect_relay.js").as_uri())
    fp = tmp / "probe_relay.html"
    fp.write_text(html, encoding="utf-8")
    return fp


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
        print(f"\n== connect_relay 状态机验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def run(page, ck: Checker) -> None:
    ev = page.evaluate

    # R1 credentials 步：完整表单要素
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    s = ev("snap()")
    ck.check("R1 credentials 渲染 email+password", s["mode"] == "form" and s["hasEmail"] and s["hasPassword"])
    ck.check("R1 步骤标题 + 前置清单卡在场", s["hasHead"] and s["hasPrep"])
    ck.check("R1 密码眼睛 + 提交钮在场", s["hasEye"] and s["hasSubmit"])
    ck.check("R1 密码初始为遮蔽态", s["pwType"] == "password", s["pwType"])

    # R2 密码眼睛切换
    ev("() => { q('[data-relay-eye]').click(); }")
    s = ev("snap()")
    ck.check("R2 点眼睛 → 明文", s["pwType"] == "text", s["pwType"])
    ev("() => { q('[data-relay-eye]').click(); }")
    s = ev("snap()")
    ck.check("R2 再点 → 复原遮蔽", s["pwType"] == "password", s["pwType"])

    # R3 空字段本地即拦（只填 email）→ missing 红字、零网络提交
    ev("() => { setField('email','a@b.com'); q('[data-relay-submit]').click(); }")
    page.wait_for_timeout(60)
    s = ev("snap()")
    ck.check("R3 空密码提交被本地拦截（零 relay-submit 网络）", s["submitCalls"] == 0, f"calls={s['submitCalls']}")
    ck.check("R3 就地 missing 红字", "relay_err_missing" in s["errText"], s["errText"])

    # R4 正常提交：填全 → 一次 relay-submit → verifying
    ev("() => { setField('email','a@b.com'); setField('password','pw123456'); }")
    ev("() => { q('[data-relay-submit]').click(); }")
    page.wait_for_timeout(120)
    s = ev("snap()")
    ck.check("R4 提交走且仅走一次 relay-submit", s["submitCalls"] == 1, f"calls={s['submitCalls']}")
    ck.check("R4 提交后进 verifying 舞台", s["mode"] == "verifying", s["mode"])

    # R5 Enter 键提交（回到 credentials：新一轮 tick 复位相位）
    ev("() => { window.__c.__relayLocal = null; window.__c.__relayResp = null; window.__submitCalls = []; }")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    ev("""() => {
      setField('email','a@b.com'); setField('password','pw123456');
      const inp = q('[data-relay-field="password"]');
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    }""")
    page.wait_for_timeout(120)
    s = ev("snap()")
    ck.check("R5 Enter 键 = 提交（一次 relay-submit）", s["submitCalls"] == 1, f"calls={s['submitCalls']}")

    # R6 checkpoint：截图视图 + 可放大 + 出路
    ev("""() => {
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__submitCalls = [];
      window.__stepResp = { status:'pending', step:'checkpoint', escalate:true, qr_image:'data:image/png;base64,AAAA' };
      window.__qr = 'data:image/png;base64,AAAA';
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    s = ev("snap()")
    ck.check("R6 checkpoint → 截图视图", s["mode"] == "screenshot")
    ck.check("R6 截图可放大 + 出路清单", s["hasZoom"] and s["hasWays"])

    # R6b 检查点「继续」钮 → 一次 step=checkpoint 的 relay-submit（替点离屏登录页）
    hasCont = ev("() => !!q('[data-relay-continue]')")
    ck.check("R6b 检查点出「继续」推进钮", hasCont)
    ev("() => { q('[data-relay-continue]').click(); }")
    page.wait_for_timeout(120)
    contCall = ev("() => (window.__submitCalls[0]||{}).step")
    ck.check("R6b 点继续 → step=checkpoint 的 relay-submit", contCall == "checkpoint", str(contCall))

    # R6c 继续回执 clicked=true：忙态钮必须在整块重渲后仍存活 + 提示切「已替点·跟进中」
    ev("""() => {
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__submitCalls = [];
      window.__submitResp = { ok:true, submitted:true, clicked:true };
      window.__stepResp = { status:'pending', step:'checkpoint', escalate:true, qr_image:'data:image/png;base64,AAAA' };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    ev("() => { q('[data-relay-continue]').click(); }")
    page.wait_for_timeout(150)
    ev("() => window.tick()")   # 再 tick 一轮：非表单态每轮整块重渲，忙态由相位携带才存活
    page.wait_for_timeout(60)
    cont_busy = ev("() => { var b = q('[data-relay-continue]'); return b ? b.disabled : null; }")
    follow_hint = ev("() => window.__c.innerHTML.indexOf('relay_continued_hint') >= 0")
    ck.check("R6c 继续(clicked=true) → 忙态跨重渲存活 + 跟进提示", cont_busy is True and follow_hint)

    # R6d 继续回执 clicked=false：miss 指路 notice + 按钮可立即再点
    ev("""() => {
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__submitResp = { ok:true, submitted:false, clicked:false };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    ev("() => { q('[data-relay-continue]').click(); }")
    page.wait_for_timeout(150)
    miss_notice = ev("() => window.__c.innerHTML.indexOf('relay_continue_miss') >= 0")
    cont_enabled = ev("() => { var b = q('[data-relay-continue]'); return b ? !b.disabled : null; }")
    ck.check("R6d 继续(clicked=false) → miss 指路 + 按钮可再点", miss_notice and cont_enabled is True)

    # R6e device_confirm 子味：手机主视觉 + 三步清单 + 截图折叠（开合跨重渲存活）
    ev("""() => {
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__stepResp = { status:'pending', step:'checkpoint', escalate:true, code:'device_confirm', qr_image:'data:image/png;base64,AAAA' };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    dc = ev("""() => ({
      flavor: !!q('[data-relay-flavor="device_confirm"]'),
      hero: !!q('.connect-relay-hero'),
      steps: !!q('.connect-relay-steps'),
      boxOpen: (q('[data-relay-shotbox]')||{}).open === true,
    })""")
    ck.check("R6e device_confirm → 手机主视觉 + 三步清单 + 截图默认折叠",
             dc["flavor"] and dc["hero"] and dc["steps"] and not dc["boxOpen"])
    ev("() => { q('[data-relay-shotbox] summary').click(); }")
    page.wait_for_timeout(60)
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    box_open = ev("() => (q('[data-relay-shotbox]')||{}).open === true")
    ck.check("R6e 折叠区展开后跨重渲存活", box_open)

    # R6f 等待生命感：已等待（modeSince 拨旧）+ 会话剩余 <5min → meta 行两段齐出
    ev("""() => {
      window.__expiresIn = 240;
      if (window.__c.__relayLocal) window.__c.__relayLocal.modeSince = Date.now() - 70000;
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    meta = ev("() => (q('.connect-relay-meta')||{}).textContent || ''")
    ck.check("R6f meta 行：已等待 + 环境保留倒计时",
             ("relay_waited" in meta) and ("relay_ttl_left" in meta), meta)

    # R7 account_locked：锁定专属标题
    ev("""() => {
      window.__expiresIn = 0;
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__stepResp = { status:'pending', step:'checkpoint', escalate:true, code:'account_locked', qr_image:'data:x' };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    s = ev("snap()")
    ck.check("R7 锁定专属标题（非泛检查点）", "relay_locked_title" in s["headText"], s["headText"])

    # R8 success：成功画勾
    ev("""() => {
      window.__c.__relayLocal = null; window.__c.__relayResp = null;
      window.__stepResp = { status:'authorized' };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    s = ev("snap()")
    ck.check("R8 授权成功 → 画勾 SVG", s["mode"] == "success" and s["hasCheck"])

    # R9 step 前进作废本地相位：verifying(credentials) 后服务端到 twofactor → code 表单
    ev("""() => {
      window.__c.__relayLocal = { phase:'verifying', ts: Date.now(), step:'credentials', prefill:null };
      window.__c.__relayResp = null;
      window.__stepResp = { status:'pending', step:'twofactor', fields:['code'] };
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    s = ev("snap()")
    hasCode = ev("() => !!q('[data-relay-field=\"code\"]')")
    ck.check("R9 服务端前进到 2FA → 渲 code 表单（旧 verifying 相位失效）",
             s["mode"] == "form" and hasCode)

    # R10 解卡正反馈：checkpoint 上的 continued 相位 + 服务端前进（wait 过渡页）
    # → preparing 顶部闪现绿色「验证已通过」条（driveTick 侦测步进清相位并记 unstuckTs）
    ev("""() => {
      window.__c.__relayLocal = { phase:'continued', ts: Date.now(), step:'checkpoint', prefill:null };
      window.__c.__relayResp = null;
      window.__stepResp = { status:'pending', step:'wait', qr_image:'' };
      window.__qr = '';
    }""")
    ev("() => window.tick()")
    page.wait_for_timeout(60)
    unstuck = ev("() => !!q('.connect-relay-unstuck')")
    ck.check("R10 继续起效（step 前进）→ 解卡正反馈绿条", unstuck)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with tempfile.TemporaryDirectory() as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 420, "height": 820})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(120)
            run(page, ck)
            ck.check("R0 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
