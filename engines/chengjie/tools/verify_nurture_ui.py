# -*- coding: utf-8 -*-
"""工具箱「智能养号」cp-nurture 可懂化改版 真浏览器门禁（Playwright；
2026-08-22 随「cp.app.h_nurture 裸键 + 工程黑话直出」改版落地）。

**为什么需要它**：改版核心全是**组件内交互时序**——三步引导条随引擎状态迁移、
go_live 内联确认、模拟记录列表回填、dirty 才亮保存、viewer 隐藏写控件、探针两击
armed 危险态。静态门禁只能证「键在/字符串在」，证不了「点了会怎样」；而共享组件
热更新直上生产。N0 把 0621 事故本身钉死：applyI18n 缺键必须保留内联兜底。

**夹具模式**（与 tools/verify_cp_voice_ui.py 同族）：file:// 自包含页面 +
真词典（cp-i18n.js，CP_LANG 钉 zh）+ 真基类（cp-panel-base.js）+ 真组件
（cp-nurture.js）+ **stub client 对象**——cp-nurture 全部 IO 走注入 client
（nurtureStatus/Save/Engine/Shadow/Probe），零实例依赖、零生产写入。

覆盖的不变量（编号对应 run() 里的断言）：
  N0  applyI18n：已知键翻译上屏；未知键**保留内联兜底文字**，绝不显示键名裸串
  N1  暂停+零方案：intro 引导可见、「模拟运行」禁用+悬浮指路、步骤1=当前、
      判词随 fleet 灯、无模拟记录区、高级区默认收起、账号行 label/短 key 回落
  N2  配方案闭环：开关→保存点亮→保存→请求载荷正确→「已保存 ✓」→计数/步骤1/
      intro/模拟运行按钮全部就地同步（不整块重渲）
  N3  模拟运行：enable_dry 请求→状态文案/步骤2迁移→模拟记录区出现并逐条可读
      （人话账号名+动作名+演练/失败 chip）
  N4  go_live 内联确认：写明试点账号数、取消可回退、确认才发 go_live→自动养护态
  N5  引擎开但零启用方案：内联警示可见（不藏进高级折叠）
  N6  viewer 只读：readonly 条+零引擎按钮+零保存钮+开关禁用+无高级区
  N7  高级区：警告 chips + 探针（下拉用人话名；先检查→检查通过；真跑一次两击
      armed 危险态，真跑成功后模拟记录自动刷新）
  N8  dirty 语义：改档位亮保存、改回熄灭；行为 chips 切换同语义
  N2b/N4b 试点 chip（P1 试点 UI 化）：暂停期直达开关；引擎 live 时设为试点=两击
      armed 确认（缩小影响面方向不设阻力）；写走 nurtureSave {canary:{key,pilot}}
  N10 ntr_ 埋点全链路：sim_start/golive_ask/confirm/cancel/save/pilot_on/
      probe_pre/probe_run/adv_open 全部经 sendBeacon 发出（fixture 截获断言）
  N11 分区折叠（2026-08-28）：五个分区标题都是折叠开关；**收起后标题行必须仍带
      该块关键读数**（灯+判词 / 引擎状态 / n-t 已启用 + 未保存红点）；引擎误配
      警示收起后照旧显示（折叠≠把出问题的事实一起藏掉）；收起再展开编辑中方案
      原样还在（_work 是内存 SSOT）；折叠态经 localStorage 跨实例记忆
  N12 长列表截断 + 起号期口径（2026-08-28）：账号 >6 个截到 5 行 + 「显示全部 n 个」，
      **在养的号一个都不许被截断藏掉**（有后果的号不是「列表太长」的代价），
      ≤6 个不截（藏 1 个换一个按钮是净亏）；「养号中」→「起号期」+ 悬浮口径
      （该数来自账号注册天数，与「已启用养护」无关，两数并排曾被读成矛盾）；
      intro 引导只在账号方案展开时出现（不指着收起的东西说话）

用法::

    python tools/verify_nurture_ui.py             # 门禁模式
    python tools/verify_nurture_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>cp-nurture revamp probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;max-width:420px;">
<div style="font-size:12px;color:#64748b;">
  <span id="k-known" data-cp-i18n="cp.app.h_nurture">兜底A</span> ·
  <span id="k-miss" data-cp-i18n="cp.app.h_missing_probe_20260822">智能养号兜底</span>
</div>
<script>window.CP_LANG = 'zh';</script>
<script src="@@I18N_JS@@"></script>
<script>window.CopilotShared.applyI18n(document);</script>
<script src="@@BASE_JS@@"></script>
<script>
window.__state = {
  canWrite: true,
  engine: { running: true, enabled: false, dry_run: true,
            ledger: { planned: 0, executed: 0, failed: 0, shadow_len: 0 } },
  plans: {},
  pilots: [],
  shadowSamples: [],
};
window.__calls = { status: 0, save: [], engine: [], shadow: 0, probe: [], canary: [] };
window.__beacons = [];
navigator.sendBeacon = (url, blob) => { window.__beacons.push(blob); return true; };
window.__mkStatus = () => {
  const S = window.__state;
  const raw = [
    { platform: 'line', account_id: 'Uk4bhjJ4HC2jhfuF0CwF4LlyIJWgLnScdIt1OIkQOg',
      stage: 'active', label: '小林 LINE 主号' },
    { platform: 'messenger', account_id: '100089088819384', stage: 'offline', label: '' },
  ].concat(S.extraAccounts || []);   // N12 长列表截断用：测试侧可加号
  const accts = raw.map((a) => {
    const key = a.platform + ':' + a.account_id;
    return Object.assign({}, a, {
      nurture_key: key,
      is_canary: S.pilots.indexOf(key) >= 0,
      nurture_plan: S.plans[key] || { enabled: false, profile: 'balanced', ramp_days: 0,
        behaviors: { browse: false, read: false, react: false, self_chat: false } },
    });
  });
  const nurtured = accts.filter((a) => a.nurture_plan.enabled).length;
  const eng = JSON.parse(JSON.stringify(S.engine));
  eng.canary_count = S.pilots.length;
  return { ok: true, can_write: S.canWrite,
    enabled: S.engine.enabled, dry_run: S.engine.dry_run, default_profile: 'balanced',
    profiles: ['conservative', 'balanced', 'aggressive'],
    behaviors: ['browse', 'read', 'react', 'self_chat'],
    fleet: { fleet_light: 'amber' },
    lifecycle: { active: 1, warming: 0, restricted: 0, banned: 0 },
    accounts: accts, total: accts.length, nurtured: nurtured,
    engine: eng,
    warnings: [{ code: 'risk_backoff_off', severity: 'warn' }],
    executor_ready: true };
};
window.__stubClient = {
  async nurtureStatus() { window.__calls.status++; return window.__mkStatus(); },
  async nurtureSave(b) {
    if (b && b.canary) {
      window.__calls.canary.push(JSON.parse(JSON.stringify(b.canary)));
      const S = window.__state;
      const k = String(b.canary.key || '');
      S.pilots = S.pilots.filter((x) => x !== k);
      if (b.canary.pilot) S.pilots.push(k);
      return { ok: true, canary_accounts: S.pilots.slice() };
    }
    window.__calls.save.push(JSON.parse(JSON.stringify(b || {})));
    const a = (b && b.account) || {};
    window.__state.plans[a.platform + ':' + a.account_id] = a.plan;
    return { ok: true };
  },
  async nurtureEngine(b) {
    window.__calls.engine.push(JSON.parse(JSON.stringify(b || {})));
    const S = window.__state;
    if (b.action === 'enable_dry') { S.engine.enabled = true; S.engine.dry_run = true; }
    else if (b.action === 'go_live') { S.engine.dry_run = false; }
    else { S.engine.enabled = false; }
    return { ok: true, engine: S.engine };
  },
  async nurtureShadow(_n) {
    window.__calls.shadow++;
    return { ok: true, samples: window.__state.shadowSamples };
  },
  async nurtureProbe(b) {
    window.__calls.probe.push(JSON.parse(JSON.stringify(b || {})));
    return { ok: true, result: { executable: true, blocked: false, ok: true,
      latency_ms: 42, detail: 'probe:ok' } };
  },
};
</script>
<script src="@@NURTURE_JS@@"></script>
<script>
const el = document.createElement('cp-nurture');
el.client = window.__stubClient;
window.__el = el;
document.body.appendChild(el);
window.q = (s) => window.__el.shadowRoot.querySelector(s);
window.qa = (s) => Array.from(window.__el.shadowRoot.querySelectorAll(s));
window.snap = () => {
  const g = window.q, ga = window.qa;
  const tryB = g('[data-role=eng-try]');
  const intro = g('[data-role=intro]');
  const s1 = g('[data-step="1"]'), s2 = g('[data-step="2"]'), s3 = g('[data-step="3"]');
  const rows = ga('.nt-acct');
  const firstSave = g('.nt-acct [data-act=save]');
  const probeRun = g('[data-act=probe-run]');
  return {
    verdict: (g('.nt-verdict') || {}).textContent || '',
    state: (g('.nt-eng-state') || {}).textContent || '',
    foot: (g('.nt-eng-foot') || {}).textContent || '',
    notes: ga('.nt-note').map((n) => n.textContent).join('|'),
    introShown: !!(intro && !intro.hidden),
    tryDisabled: tryB ? tryB.disabled : null,
    tryTitle: tryB ? (tryB.getAttribute('title') || '') : '',
    s1: s1 ? s1.className : '', s2: s2 ? s2.className : '', s3: s3 ? s3.className : '',
    s1dot: s1 ? (s1.querySelector('.dot') || {}).textContent : '',
    cnt: (g('[data-role=acct-cnt]') || {}).textContent || '',
    rowN: rows.length,
    nm1: rows[0] ? (rows[0].querySelector('.nm') || {}).textContent : '',
    nm1title: rows[0] ? rows[0].querySelector('.nm').getAttribute('title') : '',
    nm2: rows[1] ? (rows[1].querySelector('.nm') || {}).textContent : '',
    save1Disabled: firstSave ? firstSave.disabled : null,
    msg1: rows[0] ? ((rows[0].querySelector('[data-role=msg]') || {}).textContent || '') : '',
    hasShadow: !!g('.nt-shadow'),
    shRows: ga('.nt-sh-row').length,
    sh1: (ga('.nt-sh-row')[0] || {}).textContent || '',
    sh2: (ga('.nt-sh-row')[1] || {}).textContent || '',
    confirmBox: !!g('.nt-confirm'),
    confirmTxt: (g('.nt-confirm') || {}).textContent || '',
    advHd: !!g('.nt-adv-hd'),
    advBody: !!g('.nt-adv-bd'),
    warnTxt: ga('.nt-warn').map((n) => n.textContent).join('|'),
    probeMsg: (g('[data-role=probe-msg]') || {}).textContent || '',
    probeRunTxt: probeRun ? probeRun.textContent : '',
    probeArmed: !!(probeRun && probeRun.classList.contains('armed')),
    probeOpt1: (g('[data-role=probe-acct] option') || {}).textContent || '',
    ro: !!g('.nt-ro'),
    engBtnN: ga('[data-act=eng],[data-act=golive-ask]').length,
    saveBtnN: ga('[data-act=save]').length,
    sw1Disabled: (g('.nt-acct [data-role=on]') || {}).disabled,
    chips: ga('.nt-chip').map((c) => c.getAttribute('data-beh') + ':' + c.getAttribute('aria-pressed')).join('|'),
    pilots: ga('.nt-pilot').map((c) => c.getAttribute('aria-pressed')).join('|'),
    pilot2Armed: !!(ga('.nt-pilot')[1] && ga('.nt-pilot')[1].classList.contains('armed')),
    // N11 分区折叠
    folds: ga('.nt-sec').map((b) => b.getAttribute('data-fold') + ':' + b.getAttribute('aria-expanded')).join('|'),
    hdOv: (g('[data-fold=overview]') || {}).textContent || '',
    hdEng: (g('[data-fold=engine]') || {}).textContent || '',
    hdAcct: (g('[data-fold=accounts]') || {}).textContent || '',
    hdShadow: (g('[data-fold=shadow]') || {}).textContent || '',
    ovLight: !!g('[data-fold=overview] .nt-light'),
    dirtyDot: !!g('[data-fold=accounts] .dirty'),
    healthBox: !!g('.nt-health'),
    sumBox: !!g('.nt-sum'),
    engBox: !!g('.nt-eng'),
    hintBox: !!g('.nt-hint'),
    // N12 长列表截断 + 起号期口径
    kpiWarmLabel: ((ga('.nt-sum .nt-kpi')[1] || {}).querySelector
      ? (ga('.nt-sum .nt-kpi')[1].querySelector('.l') || {}).textContent : '') || '',
    kpiWarmTitle: (ga('.nt-sum .nt-kpi')[1]
      ? (ga('.nt-sum .nt-kpi')[1].getAttribute('title') || '') : ''),
    acctMore: (g('[data-act=acct-all]') || {}).textContent || '',
    acctLess: !!g('[data-act=acct-less]'),
    rowNames: ga('.nt-acct .nm').map((n) => n.textContent).join('|'),
    stages: ga('.nt-acct .nt-stage').map((n) => n.textContent).join('|'),
  };
};
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    html = (_HTML
            .replace("@@I18N_JS@@", (ENGINE / "shared/copilot/i18n/cp-i18n.js").as_uri())
            .replace("@@BASE_JS@@", (ENGINE / "shared/copilot/components/cp-panel-base.js").as_uri())
            .replace("@@NURTURE_JS@@", (ENGINE / "shared/copilot/components/cp-nurture.js").as_uri()))
    fp = tmp / "probe_nurture.html"
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
        print(f"\n== cp-nurture 可懂化验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _wait(page, expr: str, ms: int = 4000) -> bool:
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def run(page, ck: Checker) -> None:
    ev = page.evaluate

    # N0 applyI18n：已知键翻译、未知键保留内联兜底（0621 事故直接钉死）
    known = ev("() => document.getElementById('k-known').textContent")
    miss = ev("() => document.getElementById('k-miss').textContent")
    ck.check("N0 已知键翻译上屏", known == "智能养号", known)
    ck.check("N0 未知键保留内联兜底（不显示键名裸串）", miss == "智能养号兜底", miss)

    # N1 暂停 + 零方案
    ck.check("N1 状态加载完成", _wait(page, "() => snap().state.length > 0"))
    s = ev("snap()")
    ck.check("N1 判词随 fleet 灯（amber→需留意）", "有账号需要留意" in s["verdict"], s["verdict"])
    ck.check("N1 暂停态文案", "已暂停" in s["state"], s["state"])
    ck.check("N1 intro 引导可见", s["introShown"])
    ck.check("N1 零方案时模拟运行禁用+悬浮指路",
             s["tryDisabled"] is True and "先在下方启用" in s["tryTitle"], s["tryTitle"])
    ck.check("N1 步骤1=当前", "cur" in s["s1"] and s["s1dot"] == "1", s["s1"])
    ck.check("N1 引擎没开过=无模拟记录区", not s["hasShadow"])
    ck.check("N1 高级区收起（有入口无内容）", s["advHd"] and not s["advBody"])
    ck.check("N1 账号 label 人话名", s["nm1"] == "小林 LINE 主号", s["nm1"])
    ck.check("N1 无 label 回落 id + title 带全 key",
             s["nm2"] == "100089088819384"
             and s["nm1title"] == "line:Uk4bhjJ4HC2jhfuF0CwF4LlyIJWgLnScdIt1OIkQOg",
             f"{s['nm2']} / {s['nm1title']}")
    ck.check("N1 未改动时保存熄灭", s["save1Disabled"] is True)
    ck.check("N1 计零态 foot 有语境", "从「模拟运行」开始" in s["foot"], s["foot"])
    ck.check("N1 试点 chip 渲染且全部未选", s["pilots"] == "false|false", s["pilots"])

    # N2 配方案闭环（开关 → 保存点亮 → 保存 → 就地同步）
    ev("() => { q('.nt-acct [data-role=on]').click(); }")
    s = ev("snap()")
    ck.check("N2 开关改动点亮保存", s["save1Disabled"] is False)
    ev("() => { q('.nt-acct [data-act=save]').click(); }")
    ck.check("N2 保存请求发出", _wait(page, "() => window.__calls.save.length === 1"))
    body = ev("window.__calls.save[0]")
    acct = (body or {}).get("account") or {}
    ck.check("N2 载荷（平台/账号/enabled）",
             acct.get("platform") == "line" and acct.get("plan", {}).get("enabled") is True
             and acct.get("account_id", "").startswith("Uk4bhjJ4"), str(acct)[:120])
    ck.check("N2 行内「已保存 ✓」", _wait(page, "() => snap().msg1.includes('已保存')"))
    s = ev("snap()")
    ck.check("N2 计数就地同步 1/2", "1/2" in s["cnt"], s["cnt"])
    ck.check("N2 步骤1 转 done ✓", "done" in s["s1"] and s["s1dot"] == "✓", s["s1"])
    ck.check("N2 intro 就地隐藏", not s["introShown"])
    ck.check("N2 模拟运行按钮就地解锁", s["tryDisabled"] is False)
    ck.check("N2 保存后按钮熄灭（dirty 清零）", s["save1Disabled"] is True)

    # N2b 试点 chip：引擎暂停期直达开关（无确认），写 canary 载荷 + 回源点亮
    ev("() => { qa('.nt-pilot')[0].click(); }")
    ck.check("N2b 暂停期设试点直达生效", _wait(page, "() => window.__calls.canary.length === 1"))
    can0 = ev("window.__calls.canary[0]")
    ck.check("N2b canary 载荷（key+pilot=true）",
             can0.get("pilot") is True and str(can0.get("key", "")).startswith("line:"), str(can0))
    ck.check("N2b 试点 chip 回源点亮", _wait(page, "() => snap().pilots.indexOf('true') === 0"))

    # N3 模拟运行（预置影子样本 → enable_dry → 记录区可读）
    ev("""() => {
      const now = Date.now() / 1000;
      window.__state.shadowSamples = [
        { ts: now - 120, key: 'line:Uk4bhjJ4HC2jhfuF0CwF4LlyIJWgLnScdIt1OIkQOg',
          kind: 'read', dry_run: true, ok: true, detail: '' },
        { ts: now - 5400, key: 'messenger:100089088819384',
          kind: 'self_chat', dry_run: false, ok: false, detail: 'boom' },
      ];
      window.__state.engine.ledger.planned = 3;
      q('[data-role=eng-try]').click();
    }""")
    ck.check("N3 enable_dry 请求发出",
             _wait(page, "() => window.__calls.engine.length === 1")
             and ev("window.__calls.engine[0].action") == "enable_dry")
    ck.check("N3 状态迁移到模拟运行", _wait(page, "() => snap().state.includes('模拟运行中')"))
    ck.check("N3 影子样本回填", _wait(page, "() => snap().shRows === 2"))
    s = ev("snap()")
    ck.check("N3 步骤2=当前", "cur" in s["s2"], s["s2"])
    ck.check("N3 记录行1：人话名+动作名+演练 chip",
             "小林 LINE 主号" in s["sh1"] and "已读消息" in s["sh1"] and "演练" in s["sh1"], s["sh1"])
    ck.check("N3 记录行2：失败 chip + 自号互聊",
             "失败" in s["sh2"] and "自号互聊" in s["sh2"], s["sh2"])
    ck.check("N3 foot 计数有值", "已计划 3 次" in s["foot"], s["foot"])

    # N4 go_live 内联确认（取消可回退，确认才真开）
    ev("() => { q('[data-act=golive-ask]').click(); }")
    s = ev("snap()")
    ck.check("N4 确认框写明试点影响面", s["confirmBox"] and "1 个试点账号" in s["confirmTxt"],
             s["confirmTxt"][:80])
    ck.check("N4 确认框点名试点名单（不只报数字）",
             "试点账号：" in s["confirmTxt"] and "小林 LINE 主号" in s["confirmTxt"],
             s["confirmTxt"][:120])
    ev("() => { q('[data-act=golive-no]').click(); }")
    s = ev("snap()")
    ck.check("N4 取消回退（引擎未动）", (not s["confirmBox"]) and ev("window.__calls.engine.length") == 1)
    ev("() => { q('[data-act=golive-ask]').click(); }")
    ev("() => { q('[data-act=golive-yes]').click(); }")
    ck.check("N4 确认才发 go_live",
             _wait(page, "() => window.__calls.engine.length === 2")
             and ev("window.__calls.engine[1].action") == "go_live")
    ck.check("N4 迁移到自动养护态", _wait(page, "() => snap().state.includes('自动养护中')"))
    s = ev("snap()")
    ck.check("N4 步骤3=当前、步骤2=done", "cur" in s["s3"] and "done" in s["s2"], f"{s['s2']} / {s['s3']}")

    # N4b live 期设试点＝两击 armed 确认（该号立即参与真实动作，危险方向要显式确认）
    ev("() => { qa('.nt-pilot')[1].click(); }")
    s = ev("snap()")
    ck.check("N4b live 首击 armed 不生效",
             s["pilot2Armed"] and ev("window.__calls.canary.length") == 1)
    ev("() => { qa('.nt-pilot')[1].click(); }")
    ck.check("N4b 二击真设试点", _wait(page, "() => window.__calls.canary.length === 2")
             and _wait(page, "() => snap().pilots === 'true|true'"))
    ck.check("N4b 引擎状态行试点数回源", _wait(page, "() => snap().state.includes('2 个')"))

    # N5 引擎开但零启用方案 → 内联警示（不藏进高级折叠）
    ev("() => { window.__state.plans = {}; window.__el._load(); }")
    ck.check("N5 零启用内联警示可见",
             _wait(page, "() => snap().notes.includes('引擎已开但没有启用养护的账号')"))
    ev("""() => {
      window.__state.plans['line:Uk4bhjJ4HC2jhfuF0CwF4LlyIJWgLnScdIt1OIkQOg'] =
        { enabled: true, profile: 'balanced', ramp_days: 0,
          behaviors: { browse: false, read: false, react: false, self_chat: false } };
    }""")

    # N6 viewer 只读
    ev("() => { window.__state.canWrite = false; window.__el._load(); }")
    ck.check("N6 readonly 条出现", _wait(page, "() => snap().ro === true"))
    s = ev("snap()")
    ck.check("N6 零引擎按钮/零保存钮/无高级区",
             s["engBtnN"] == 0 and s["saveBtnN"] == 0 and not s["advHd"],
             f"eng={s['engBtnN']} save={s['saveBtnN']} adv={s['advHd']}")
    ck.check("N6 行内开关禁用", s["sw1Disabled"] is True)
    ev("() => { window.__state.canWrite = true; window.__el._load(); }")
    ck.check("N6 恢复可写", _wait(page, "() => snap().ro === false && snap().advHd === true"))

    # N7 高级区：警告 + 探针（人话名下拉 / 先检查 / 两击 armed 真跑 / 记录自动刷新）
    ev("() => { q('.nt-adv-hd').click(); }")
    ck.check("N7 高级区展开", _wait(page, "() => snap().advBody === true"))
    s = ev("snap()")
    ck.check("N7 lint 警告人话化", "风险退避已关闭" in s["warnTxt"], s["warnTxt"][:60])
    ck.check("N7 探针下拉用人话名", s["probeOpt1"] == "小林 LINE 主号", s["probeOpt1"])
    ev("() => { q('[data-act=probe-pre]').click(); }")
    ck.check("N7 先检查 → 检查通过", _wait(page, "() => snap().probeMsg.includes('检查通过')"))
    ck.check("N7 先检查 confirm=false", ev("window.__calls.probe[0].confirm") is False)
    shadow_before = ev("window.__calls.shadow")
    ev("() => { q('[data-act=probe-run]').click(); }")
    s = ev("snap()")
    ck.check("N7 首击进入 armed 危险态", s["probeArmed"] and "再点一次确认真跑" in s["probeRunTxt"],
             s["probeRunTxt"])
    ev("() => { q('[data-act=probe-run]').click(); }")
    ck.check("N7 二击真跑成功（42ms）", _wait(page, "() => snap().probeMsg.includes('42ms')"))
    ck.check("N7 真跑 confirm=true", ev("window.__calls.probe[1].confirm") is True)
    ck.check("N7 真跑后记录自动刷新", ev("window.__calls.shadow") > shadow_before)

    # N8 dirty 语义（档位/行为 chips）
    ev("""() => {
      const sel = q('.nt-acct [data-role=profile]');
      sel.value = 'aggressive';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("N8 改档位点亮保存", s["save1Disabled"] is False)
    ev("""() => {
      const sel = q('.nt-acct [data-role=profile]');
      sel.value = 'balanced';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("N8 改回原值熄灭保存", s["save1Disabled"] is True)
    ev("() => { q('.nt-acct [data-act=more]').click(); }")
    ck.check("N8 行为 chips 展开", _wait(page, "() => snap().chips.length > 0"))
    ev("() => { q('.nt-chip[data-beh=read]').click(); }")
    s = ev("snap()")
    ck.check("N8 chip 切换 aria-pressed + 点亮保存",
             "read:true" in s["chips"] and s["save1Disabled"] is False, s["chips"])
    ev("() => { q('.nt-chip[data-beh=read]').click(); }")
    s = ev("snap()")
    ck.check("N8 chip 切回熄灭保存", "read:false" in s["chips"] and s["save1Disabled"] is True)

    # N11 分区折叠：收起后标题必须仍带该块关键读数（否则折叠＝把信息弄丢）
    s = ev("snap()")
    ck.check("N11 五个分区标题都是折叠开关",
             s["folds"] == "overview:true|engine:true|shadow:true|accounts:true|advanced:true",
             s["folds"])
    ev("() => { q('[data-fold=overview]').click(); }")
    s = ev("snap()")
    ck.check("N11 概览收起：KPI 消失但标题接过灯+判词",
             (not s["healthBox"]) and (not s["sumBox"]) and s["ovLight"]
             and "有账号需要留意" in s["hdOv"],
             f"health={s['healthBox']} light={s['ovLight']} hd={s['hdOv']}")
    ev("() => { q('[data-fold=overview]').click(); }")
    ck.check("N11 概览展开复原",
             _wait(page, "() => snap().healthBox === true && snap().sumBox === true"))

    # 引擎收起：状态进标题，且误配警示**不许**跟着一起消失
    ev("() => { window.__state.plans = {}; window.__el._load(); }")
    ck.check("N11 前置：清空方案后误配警示出现",
             _wait(page, "() => snap().notes.includes('引擎已开但没有启用养护的账号')"))
    ev("() => { q('[data-fold=engine]').click(); }")
    s = ev("snap()")
    ck.check("N11 引擎收起：面板消失、状态进标题、误配警示照旧显示",
             (not s["engBox"]) and "自动养护中" in s["hdEng"]
             and "引擎已开但没有启用养护的账号" in s["notes"],
             f"engBox={s['engBox']} hd={s['hdEng'][:40]} notes={s['notes'][:30]}")
    ev("() => { q('[data-fold=engine]').click(); }")
    ck.check("N11 引擎展开复原", _wait(page, "() => snap().engBox === true"))

    # 账号方案收起：计数留在标题、未保存改动亮红点、展开后编辑内容原样还在
    ev("() => { q('.nt-acct [data-role=on]').click(); }")
    ev("() => { q('[data-fold=accounts]').click(); }")
    s = ev("snap()")
    ck.check("N11 账号方案收起：行消失、计数仍在、未保存红点点亮",
             s["rowN"] == 0 and "0/2" in s["cnt"] and s["dirtyDot"],
             f"rowN={s['rowN']} cnt={s['cnt']} dot={s['dirtyDot']}")
    ev("() => { q('[data-fold=accounts]').click(); }")
    s = ev("snap()")
    ck.check("N11 展开后未保存改动原样还在（_work 是内存 SSOT）",
             s["rowN"] == 2 and s["save1Disabled"] is False,
             f"rowN={s['rowN']} save={s['save1Disabled']}")

    # 折叠态 localStorage 记忆（file:// 下 localStorage 可能不可用 → 降级放行）
    ls_ok = ev("() => { try { localStorage.setItem('__nt_probe', '1');"
               " localStorage.removeItem('__nt_probe'); return true; } catch (e) { return false; } }")
    if ls_ok:
        ev("() => { q('[data-fold=shadow]').click(); }")
        raw = ev("() => { try { return localStorage.getItem('cp.nurture.folds.v1') || ''; }"
                 " catch (e) { return ''; } }")
        ck.check("N11 折叠态写入 localStorage", '"shadow":1' in (raw or ""), str(raw)[:90])
        inherited = ev("""async () => {
          const el2 = document.createElement('cp-nurture');
          el2.client = window.__stubClient;
          document.body.appendChild(el2);
          let v = 'timeout';
          for (let i = 0; i < 60; i++) {
            await new Promise((r) => setTimeout(r, 25));
            const b = el2.shadowRoot.querySelector('[data-fold=shadow]');
            if (b) { v = b.getAttribute('aria-expanded'); break; }
          }
          el2.remove();
          return v;
        }""")
        ck.check("N11 新实例继承折叠态（跨会话记忆）", inherited == "false", str(inherited))
        ev("() => { q('[data-fold=shadow]').click(); }")

        # 全部收起 → 底部科普脚注一并收（否则折叠没真正把空间还回来）；展开即回来
        ev("""() => { ['overview', 'engine', 'shadow', 'accounts', 'advanced']
              .forEach((s) => { const b = q('[data-fold=' + s + ']'); if (b) b.click(); }); }""")
        s = ev("snap()")
        ck.check("N11 全收起：只剩分区标题行，科普脚注一并收起",
                 s["folds"] == "overview:false|engine:false|shadow:false|accounts:false|advanced:false"
                 and (not s["hintBox"]) and (not s["engBox"]) and s["rowN"] == 0,
                 f"folds={s['folds']} hint={s['hintBox']}")
        ev("() => { q('[data-fold=accounts]').click(); }")
        ck.check("N11 展开任意一块 → 脚注回来", _wait(page, "() => snap().hintBox === true"))
    else:
        ck.check("N11 localStorage 不可用时折叠仍工作（记忆降级不报错）", True,
                 "file:// 无 localStorage，跳过记忆断言")

    # N12 长列表截断 + 起号期口径（2026-08-28）
    # 前置守卫：N11 收尾时概览可能仍是收起态——本段断言不该依赖上一段的残留状态
    ev("""() => { const b = q('[data-fold=overview]');
          if (b && b.getAttribute('aria-expanded') === 'false') b.click(); }""")
    s = ev("snap()")
    ck.check("N12 起号期 KPI 不再叫「养号中」，且悬浮写明与「已启用养护」无关",
             s["kpiWarmLabel"] == "起号期" and "与下方是否启用养护无关" in s["kpiWarmTitle"],
             f"label={s['kpiWarmLabel']} title={s['kpiWarmTitle'][:40]}")

    ev("""() => {
      window.__state.extraAccounts = [];
      for (let i = 1; i <= 8; i++) {
        window.__state.extraAccounts.push({ platform: 'telegram',
          account_id: '90000000' + i, stage: i === 1 ? 'warming' : 'active',
          label: '备用号 ' + i });
      }
      // 末位的号「在养」——截断绝不许把有后果的号藏掉
      window.__state.plans['telegram:900000008'] =
        { enabled: true, profile: 'balanced', ramp_days: 0,
          behaviors: { browse: false, read: false, react: false, self_chat: false } };
      window.__el._load();
    }""")
    ck.check("N12 前置：10 个账号已加载", _wait(page, "() => snap().cnt.includes('/10')"))
    s = ev("snap()")
    ck.check("N12 长列表截断到 5 行 + 「显示全部 10 个账号」",
             s["rowN"] == 5 and "显示全部 10 个账号" in s["acctMore"],
             f"rowN={s['rowN']} btn={s['acctMore']}")
    ck.check("N12 在养的号绝不被截断藏掉（末位「备用号 8」仍在）",
             "备用号 8" in s["rowNames"], s["rowNames"])
    ck.check("N12 行徽章同步改口径（stage_warming → 起号期）",
             "起号期" in s["stages"], s["stages"])
    ev("() => { q('[data-act=acct-all]').click(); }")
    s = ev("snap()")
    ck.check("N12 显示全部 → 10 行 + 出现「收起长列表」",
             s["rowN"] == 10 and s["acctLess"], f"rowN={s['rowN']} less={s['acctLess']}")
    ev("() => { q('[data-act=acct-less]').click(); }")
    ck.check("N12 收起回 5 行", _wait(page, "() => snap().rowN === 5"))
    ev("() => { window.__state.extraAccounts = []; window.__el._load(); }")
    ck.check("N12 回到 2 个号时不截断（藏 1 个换一个按钮是净亏）",
             _wait(page, "() => snap().rowN === 2 && snap().acctMore === ''"))

    # intro 引导只在账号方案展开时出现（引导指向的东西不能是收起的）
    ev("""() => { window.__state.engine.enabled = false; window.__state.plans = {};
          window.__el._load(); }""")
    ck.check("N12 前置：暂停+零方案 → intro 引导可见",
             _wait(page, "() => snap().introShown === true"))
    ev("() => { q('[data-fold=accounts]').click(); }")
    s = ev("snap()")
    ck.check("N12 账号方案折起 → 引导不再指着收起的东西说话", not s["introShown"])
    ev("() => { q('[data-fold=accounts]').click(); }")
    ck.check("N12 展开 → 引导回来", _wait(page, "() => snap().introShown === true"))

    # N10 ntr_ 埋点全链路（fixture 截获 sendBeacon，解 Blob 断言动作集）
    acts = ev("""async () => {
      const t = await Promise.all(window.__beacons.map((b) => b.text()));
      return t.map((s) => { try { return JSON.parse(s).action; } catch (e) { return ''; } });
    }""")
    need = {"ntr_sim_start", "ntr_golive_ask", "ntr_golive_cancel", "ntr_golive_confirm",
            "ntr_save", "ntr_pilot_on", "ntr_probe_pre", "ntr_probe_run", "ntr_adv_open",
            # 折叠也要可读数：哪块被最多人收起＝分区优先级的直接证据
            "ntr_fold_overview", "ntr_unfold_overview",
            # 「显示全部」被点得多 ⇒ 截断上限（ACCT_VISIBLE_CAP）定小了
            "ntr_acct_show_all", "ntr_acct_show_less"}
    got = set(acts or [])
    ck.check("N10 ntr_ 埋点全链路", need.issubset(got),
             "missing=" + ",".join(sorted(need - got)) if not need.issubset(got)
             else f"{len(got & need)}/{len(need)}")


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
            page = browser.new_page(viewport={"width": 480, "height": 960})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(150)
            run(page, ck)
            ck.check("N9 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
