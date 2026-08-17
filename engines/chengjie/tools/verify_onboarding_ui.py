# -*- coding: utf-8 -*-
"""/welcome 首启向导 真浏览器门禁（WP-2 2026-08；Playwright fixture 模式）。

**为什么需要它**：向导是「五步状态机 + 三个外部端点 + persona_create_wizard 模态
寄生」的纯前端编排——静态门禁只能证「函数挂了 window / i18n 键存在」，证不了
「点下一步真的推进、断点续走真的落对步、模板模态在 welcome 宿主里真的能建出
人设并回调宿主钩子」。而模板热更新直上生产，这些行为必须有真浏览器兜底。

**夹具模式**（对齐 tools/verify_goal_form_ui.py，P21 决策）：welcome.html 的
content 块经 Jinja 离线渲染（i18n=onboarding pack 真 zh 词条）→ file:// 自包含
页面 + **真 persona_create_wizard.js** + 假 fetch/假 window.open——零实例依赖
（实例宕/重启冷却照跑）、零遥测零配置写。

覆盖的不变量（编号对应 run() 里的断言）：
  S1  boot → 读 status → 落第 1 步（授权）；试用态渲染「体验中」chip + 官网 CTA
  S2  空授权码点激活 → 就地报错；粘贴后激活 → POST /activate 契约 + 成功提示
  S3  下一步 → 授权步记 done（POST state）→ 渠道步；stepper 打勾
  S4  渠道列表按 /api/setup/channels 渲染（就绪/待接入 chip）；「去接入」走
      window.open 深链 /workspace/setup?channel=<id>（事件委托，非内联 handler）
  S5  人设步：0 人设 → warn 计数 chip；PSWizard 模态在 welcome 宿主打开（pp-modal
      壳样式自带）→ 选模板 → 填名 → 创建 → PUT /api/personas/profiles/* 契约 →
      宿主钩子 loadProfileList 刷新计数 + 自动标记本步 done
  S6  自动化步：选「AI 拟稿人审」→ 应用 → POST automation-tier {tier:review} +
      成功提示 + 本步 done
  S7  测试消息步：账号下拉来自 status.accounts；发送 → POST /api/unified-inbox/send
      载荷钉死 chat_key='me' + client_msg_id 前缀 onb-
  S8  完成向导 → POST state {completed:true} → 完成页可见（含进入工作台链接）
  S9  断点续走：license+channel 已 done 的状态 → boot 直落人设步
  S10 已完成状态 → boot 直落完成页（「已完成过」提示），不再从头走

用法::

    python tools/verify_onboarding_ui.py            # 门禁模式
    python tools/verify_onboarding_ui.py --headed   # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_SHELL = """<!doctype html>
<html><head><meta charset="utf-8"><title>welcome wizard probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;">
<script>
window.__ZH__ = @@ZH@@;
window.T = function (k, fb) {
  var v = window.__ZH__[k];
  return (v === undefined || v === null || v === '') ? (fb !== undefined ? fb : k) : v;
};
window.Tf = function (k, vars) {
  var s = window.T(k);
  if (vars) for (var p in vars) { if (Object.prototype.hasOwnProperty.call(vars, p)) s = s.split('{' + p + '}').join(String(vars[p])); }
  return s;
};
window.__opens = [];
window.open = function (url) { window.__opens.push(String(url)); return null; };
window.__posts = [];      // {url, body}
window.__profiles = @@PROFILES@@;
window.__stateSteps = @@STATE_STEPS@@;
window.__completed = @@COMPLETED@@;
var __respond = function (d, st) {
  return new Response(JSON.stringify(d), { status: st || 200, headers: { 'Content-Type': 'application/json' } });
};
window.fetch = async function (url, opts) {
  url = String(url);
  var method = String((opts && opts.method) || 'GET').toUpperCase();
  var body = null;
  try { body = opts && opts.body ? JSON.parse(String(opts.body)) : null; } catch (e) { body = null; }
  if (method !== 'GET') window.__posts.push({ url: url, method: method, body: body });
  if (url.indexOf('/api/onboarding/status') >= 0) {
    return __respond({ ok: true, enabled: true,
      state: { steps: window.__stateSteps, completed: window.__completed, updated_at: 0 },
      steps: ['license', 'channel', 'persona', 'automation', 'test_message'],
      automation: { mode: 'auto_ai', worker_enabled: false, deliver_enabled: false },
      accounts: [{ platform: 'telegram', account_id: '8438080491', label: 'DemoAcct', online: true }],
      links: { shop_url: 'https://shop.example/x', trial_url: 'https://shop.example/trial',
               setup: '/workspace/setup', personas: '/personas', workspace: '/workspace' } });
  }
  if (url.indexOf('/api/onboarding/state') >= 0) {
    if (body && body.step) {
      window.__stateSteps[body.step] = { done: !!body.done, skipped: !!body.skipped };
    }
    if (body && Object.prototype.hasOwnProperty.call(body, 'completed')) window.__completed = !!body.completed;
    return __respond({ ok: true, state: { steps: window.__stateSteps, completed: window.__completed } });
  }
  if (url.indexOf('/api/onboarding/automation-tier') >= 0) {
    return __respond({ ok: true, tier: body && body.tier,
                       applied: ['inbox.auto_draft.automation_mode'], blocked: [] });
  }
  if (url.indexOf('/api/admin/license/activate') >= 0) {
    return __respond({ ok: true, state: 'active', plan: 'pro' });
  }
  if (url.indexOf('/api/admin/license') >= 0) {
    return __respond({ ok: true, licensed: false, trial: false, plan: 'community',
      quota: { source: 'local_trial', trial_hours_left: 71.5, trial_expired: false } });
  }
  if (url.indexOf('/api/setup/channels') >= 0) {
    return __respond({ ok: true, ready_count: 1, total: 2, channels: [
      { id: 'telegram', name: 'Telegram', ready: true },
      { id: 'line', name: 'LINE', ready: false }] });
  }
  if (url.indexOf('/api/personas/profiles') >= 0 && method === 'PUT') {
    window.__profiles = [{ id: 'support_x', name: '小美' }];
    return __respond({ ok: true, profile_id: 'support_x', retired_conflicts: [] });
  }
  if (url.indexOf('/api/personas/profiles') >= 0) {
    return __respond({ ok: true, profiles: window.__profiles });
  }
  if (url.indexOf('/api/unified-inbox/send') >= 0) {
    return __respond({ ok: true, sent: true });
  }
  return __respond({}, 404);
};
</script>
@@CONTENT@@
</body></html>
"""


def _render_content() -> str:
    """welcome.html 的 content 块 → 离线 Jinja 渲染（i18n=onboarding pack 真词条）。"""
    import jinja2

    from src.web.i18n_packs.onboarding import ZH

    src = (ENGINE / "src" / "web" / "templates" / "welcome.html").read_text(
        encoding="utf-8")
    marker = "{% block content %}"
    start = src.index(marker) + len(marker)
    end = src.rindex("{% endblock %}")
    content = src[start:end]
    # 真组件文件从 file:// 加载（../src/web/static/js/persona_create_wizard.js）
    wizard_js = (ENGINE / "src" / "web" / "static" / "js"
                 / "persona_create_wizard.js").resolve()
    content = re.sub(r'<script src="/static/js/persona_create_wizard\.js[^"]*">',
                     '<script src="%s">' % wizard_js.as_uri(), content)
    return jinja2.Template(content).render(i18n=dict(ZH))


def build_page(tmp: Path, *, state_steps=None, completed=False,
               profiles=None) -> Path:
    from src.web.i18n_packs.onboarding import ZH

    html = (_SHELL
            .replace("@@ZH@@", json.dumps(dict(ZH), ensure_ascii=False))
            .replace("@@PROFILES@@", json.dumps(profiles or []))
            .replace("@@STATE_STEPS@@", json.dumps(state_steps or {}))
            .replace("@@COMPLETED@@", "true" if completed else "false")
            .replace("@@CONTENT@@", _render_content()))
    name = "welcome_%s_%s.html" % (
        "done" if completed else "fresh", len(state_steps or {}))
    out = tmp / name
    out.write_text(html, encoding="utf-8")
    return out


class Check:
    def __init__(self):
        self.fails: list = []
        self.n = 0

    def ok(self, cond, label):
        self.n += 1
        mark = "PASS" if cond else "FAIL"
        print("  [%s] %s" % (mark, label))
        if not cond:
            self.fails.append(label)


def run(headed: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] playwright not installed; skipping browser gate")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="onb_ui_"))
    page_fresh = build_page(tmp)
    page_resume = build_page(
        tmp, state_steps={"license": {"done": True}, "channel": {"skipped": True}})
    page_done = build_page(tmp, completed=True,
                           profiles=[{"id": "p1", "name": "在册"}])
    c = Check()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pg = browser.new_page(viewport={"width": 1080, "height": 860})
        pg.goto(page_fresh.as_uri())
        pg.wait_for_timeout(250)

        # S1 boot → 第 1 步；试用 chip + CTA
        c.ok(pg.is_visible("#onb-p-license"), "S1 boot lands on license panel")
        c.ok("体验中" in pg.inner_text("#onb-lic-state"),
             "S1 trial chip rendered from /api/admin/license")
        c.ok(pg.locator("#onb-lic-cta a").count() == 1
             and "trial" in (pg.locator("#onb-lic-cta a").get_attribute("href") or ""),
             "S1 trial CTA deep link present")
        c.ok("cur" in (pg.get_attribute("#onb-steps li[data-step=license]", "class") or ""),
             "S1 stepper marks license current")

        # S2 激活：空输入报错 → 粘贴激活成功
        pg.click("#onb-lic-activate")
        pg.wait_for_timeout(120)
        c.ok("err" in (pg.get_attribute("#onb-lic-note", "class") or ""),
             "S2 empty token -> inline error")
        pg.fill("#onb-lic-token", "CHATX-XXXX")
        pg.click("#onb-lic-activate")
        pg.wait_for_timeout(200)
        acts = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/license/activate')>=0)")
        c.ok(len(acts) == 1 and acts[0]["body"]["token"] == "CHATX-XXXX",
             "S2 activate POST contract {token}")
        c.ok("ok" in (pg.get_attribute("#onb-lic-note", "class") or ""),
             "S2 activate success note")

        # S3 下一步 → done 落状态 + 渠道步
        pg.click("#onb-p-license .onb-foot .onb-btn.pri")
        pg.wait_for_timeout(250)
        c.ok(pg.is_visible("#onb-p-channel"), "S3 next -> channel panel")
        sts = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/api/onboarding/state')>=0)")
        c.ok(any(p["body"].get("step") == "license" and p["body"].get("done")
                 for p in sts), "S3 license step persisted done=true")
        c.ok("ok" in (pg.get_attribute("#onb-steps li[data-step=license]", "class") or ""),
             "S3 stepper shows license done")

        # S4 渠道列表 + 深链
        c.ok(pg.locator("#onb-ch-list .onb-row").count() == 2,
             "S4 channel rows rendered")
        c.ok("已就绪" in pg.inner_text("#onb-ch-list"),
             "S4 ready chip rendered")
        pg.click('#onb-ch-list [data-onb-ch="line"]')
        opens = pg.evaluate("window.__opens")
        c.ok(opens and opens[-1] == "/workspace/setup?channel=line",
             "S4 connect deep-links /workspace/setup?channel=line")

        # S5 人设步：模态建人设 → 宿主钩子刷新 + 自动 done
        pg.click("#onb-p-channel .onb-foot .onb-btn.pri")
        pg.wait_for_timeout(250)
        c.ok(pg.is_visible("#onb-p-persona"), "S5 persona panel visible")
        c.ok("0" in pg.inner_text("#onb-ps-state"), "S5 zero personas counted")
        pg.click("#onb-ps-open")
        pg.wait_for_timeout(200)
        c.ok(pg.is_visible("#psw-ov.open"), "S5 PSWizard modal opens on welcome host")
        pg.click('.psw-card[data-key="support"]')
        pg.wait_for_timeout(150)
        pg.fill("#psw-name", "小美")
        pg.click("#psw-create")
        pg.wait_for_timeout(300)
        puts = pg.evaluate(
            "window.__posts.filter(p=>p.method==='PUT' && p.url.indexOf('/api/personas/profiles/')>=0)")
        c.ok(len(puts) == 1 and puts[0]["body"]["persona"]["name"] == "小美",
             "S5 create goes through Studio PUT with persona body")
        c.ok(not pg.is_visible("#psw-ov.open"), "S5 modal closed after create")
        c.ok("1" in pg.inner_text("#onb-ps-state"),
             "S5 host hook refreshed persona count")
        sts = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/api/onboarding/state')>=0)")
        c.ok(any(p["body"].get("step") == "persona" and p["body"].get("done")
                 for p in sts), "S5 persona step auto-marked done")

        # S6 自动化档位
        pg.click("#onb-p-persona .onb-foot .onb-btn.pri")
        pg.wait_for_timeout(200)
        c.ok(pg.is_visible("#onb-p-automation"), "S6 automation panel visible")
        pg.click('.onb-card[data-tier="review"]')
        c.ok("sel" in (pg.get_attribute('.onb-card[data-tier="review"]', "class") or ""),
             "S6 tier card selectable")
        pg.click("#onb-au-apply")
        pg.wait_for_timeout(250)
        tiers = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/automation-tier')>=0)")
        c.ok(len(tiers) == 1 and tiers[0]["body"]["tier"] == "review",
             "S6 tier POST contract {tier:review}")
        c.ok("ok" in (pg.get_attribute("#onb-au-note", "class") or ""),
             "S6 applied note ok")

        # S7 测试消息：账号下拉 + send 载荷契约
        pg.click("#onb-p-automation .onb-foot .onb-btn.pri")
        pg.wait_for_timeout(200)
        c.ok(pg.is_visible("#onb-p-test_message"), "S7 test panel visible")
        c.ok("DemoAcct" in pg.inner_text("#onb-ts-account"),
             "S7 account select filled from status.accounts")
        pg.click("#onb-ts-send")
        pg.wait_for_timeout(250)
        sends = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/api/unified-inbox/send')>=0)")
        c.ok(len(sends) == 1, "S7 send POST fired once")
        if sends:
            b = sends[0]["body"]
            c.ok(b["chat_key"] == "me" and b["platform"] == "telegram"
                 and b["account_id"] == "8438080491",
                 "S7 payload pinned to own Saved Messages (chat_key=me)")
            c.ok(str(b.get("client_msg_id", "")).startswith("onb-"),
                 "S7 idempotency key onb- prefix")
        c.ok("ok" in (pg.get_attribute("#onb-ts-note", "class") or ""),
             "S7 sent note ok")

        # S8 完成
        pg.click("#onb-p-test_message .onb-foot .onb-btn.pri")
        pg.wait_for_timeout(250)
        c.ok(pg.is_visible("#onb-p-finish"), "S8 finish panel visible")
        c.ok(pg.locator('#onb-p-finish a[href="/workspace"]').count() == 1,
             "S8 workspace CTA present")
        comp = pg.evaluate(
            "window.__posts.filter(p=>p.url.indexOf('/api/onboarding/state')>=0"
            " && p.body && p.body.completed===true)")
        c.ok(len(comp) == 1, "S8 completed persisted once")

        # S9 断点续走：license done + channel skipped → 直落 persona
        pg.goto(page_resume.as_uri())
        pg.wait_for_timeout(250)
        c.ok(pg.is_visible("#onb-p-persona"), "S9 resume lands on persona step")
        c.ok("ok" in (pg.get_attribute("#onb-steps li[data-step=license]", "class") or ""),
             "S9 stepper restores done mark")
        c.ok("skip" in (pg.get_attribute("#onb-steps li[data-step=channel]", "class") or ""),
             "S9 stepper restores skip mark")

        # S10 已完成 → 直落完成页
        pg.goto(page_done.as_uri())
        pg.wait_for_timeout(250)
        c.ok(pg.is_visible("#onb-p-finish"), "S10 completed boots to finish panel")
        c.ok((pg.inner_text("#onb-fin-note") or "").strip() != "",
             "S10 completed note shown")

        browser.close()

    print()
    if c.fails:
        print("FAIL %d/%d: %s" % (len(c.fails), c.n, "; ".join(c.fails)))
        return 1
    print("PASS %d/%d checks" % (c.n, c.n))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    sys.exit(run(headed=args.headed))
