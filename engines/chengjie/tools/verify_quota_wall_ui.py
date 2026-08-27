# -*- coding: utf-8 -*-
"""额度墙 v2（quotawall）真浏览器门禁（Playwright；2026-08-21 随 P2 落地）。

**为什么需要它**：额度墙/预警条全部是 workspace_base.html 内联 JS，模板热更新
直上生产；静态门禁只能证「字符串在」，证不了「调度器真的去抖/互斥/自动到账
恢复」。这些全是运行时行为：每会话一次的被动弹、动作点 10 分钟去抖、与
#ws-aitrial-upsell 的同屏互斥、凭证兑换成功即关墙、点充值后 watch 盯到账、
预警条按日贪睡——任何一条回归都是「坐席被弹窗轰炸」或「付了钱不恢复」级事故。

**夹具模式**（与 tools/verify_goal_form_ui.py 同族）：file:// 自包含页 +
**从真模板热提取**额度墙脚本段（start=var quotaPill / end=refreshQuota 轮询行，
Jinja isMaster 表达式替换为 window.__FX_MASTER 开关）+ 假 apiFetch/sendBeacon/
window.open + wsBanner 薄壳——零实例依赖、零遥测污染、单一事实源（模板改了
门禁自动测新代码，绝不测夹具的旧拷贝）。

覆盖的不变量：
  S1  被动触发：maybePassive(chars) → 墙出现、标题=zh 词条、「稍后再说」关墙
  S2  每会话每变体一次：同变体二次被动 → 不再弹
  S3  动作点触发：aitr:quota-blocked(license_chars) → 无视会话键立即弹；Esc 关
  S4  动作点 10 分钟去抖：立刻再触发 → 不弹；清 debounce 键 → 再弹；背板点击关
  S5  互斥：#ws-aitrial-upsell 可见时被动弹让路
  S6  凭证兑换：弹层内粘贴→兑换成功 → toast「+N 已到账」+ 墙自动关 + 埋点
  S7  充值 watch：点「立即充值」→ window.open(shop) + watching 提示；额度改善
      → toast + 墙自动关 + qw_credited 埋点（__qwWatchMs 快进钩子）
  S8  agent 变体：agent_chars → 标题/正文带 used/quota、无凭证输入框
  S9  trial 变体：state.wall=trial → 标题=体验档 + 「注册领取」主 CTA（#mb-claim）
  S10 预警条：forecast.days_left<=3 → #ws-quotalow 出条（文案带剩余量）+
      master 出充值钮；「今天不再提醒」→ 日键贪睡，同日刷新不再出
  S11 坐席角色页：正文=agent_hint、只有「查看额度详情」、无凭证框；预警条走
      text_agent 口径且无充值钮

用法::

    python tools/verify_quota_wall_ui.py            # 门禁模式
    python tools/verify_quota_wall_ui.py --headed   # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_SEG_START = "var quotaPill=document.getElementById('ws-quota');"
_SEG_END = "refreshQuota(); setInterval(refreshQuota, 300000);"


def _wall_segment() -> str:
    """从真模板热提取额度墙脚本段（含调度器/预警条/refreshQuota）。"""
    src = (ENGINE / "src/web/templates/workspace_base.html").read_text(encoding="utf-8")
    i = src.index(_SEG_START)
    j = src.index(_SEG_END, i) + len(_SEG_END)
    seg = src[i:j]
    # isMaster 的 Jinja 表达式 → 夹具开关（默认 master；页面可在段前置 false）
    seg = re.sub(
        r"\{\{\s*'true' if user_role == 'master' else 'false'\s*\}\}",
        "(window.__FX_MASTER!==false)", seg)
    # 其余 Jinja（当前段内没有，防御未来加入）→ 占位
    seg = re.sub(r"\{%-?.*?-?%\}", "", seg, flags=re.S)
    seg = re.sub(r"\{\{.*?\}\}", "true", seg, flags=re.S)
    return seg


def _zh_map() -> dict:
    from src.web.i18n_packs.membership import ZH as MB_ZH
    from src.web.i18n_packs.workspace_quota import ZH as WQ_ZH

    out = dict(WQ_ZH)
    for k in ("mb_redeem_ph", "mb_redeem_btn"):
        out[k] = MB_ZH[k]
    return out


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>quota wall probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;">
<a id="ws-quota" style="display:none;"></a>
<div id="ws-aitrial-upsell" style="display:none;position:fixed;inset:0;"></div>
<div id="ws-quotalow" style="display:none;align-items:center;gap:10px;">
  <span id="ws-quotalow-text"></span>
  <button type="button" id="ws-quotalow-buy" style="display:none;"></button>
  <a id="ws-quotalow-view" href="/membership" style="display:none;"></a>
  <span id="ws-quotalow-x" role="button"></span>
</div>
<script>
window.__FX_MASTER = @@MASTER@@;
window.__ZH__ = @@ZH@@;
function __fmt(s, vars) {
  s = String(s == null ? '' : s);
  if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
  return s;
}
window.T = (k) => (window.__ZH__[k] != null ? window.__ZH__[k] : k);
window.Tf = (k, vars) => __fmt(window.T(k), vars);
window.__beacons = [];
try {
  Object.defineProperty(navigator, 'sendBeacon', { value: (url, blob) => {
    try { blob.text().then((t) => {
      try { window.__beacons.push(JSON.parse(t).action || ''); } catch (_e) {}
    }); } catch (_e) {}
    return true;
  }});
} catch (_e) {}
window.__opens = [];
window.open = (u) => { window.__opens.push(String(u || '')); return null; };
window.wsBanner = {
  register: () => {},
  set: (id, want) => {
    const el = document.getElementById(id);
    if (el) el.style.display = want ? 'flex' : 'none';
  },
  shown: () => null,
};
window.__FX_QUOTA = {
  ok: true, visible: true, source: 'license',
  included: 100000, used: 20000, remaining: 80000,
  exceeded: false, hours_left: null, expired: false, level: 'ok',
  state: { feat: 'qs1', verdict: 'ok', wall: '', level: 'ok', shop_url: '',
           tok: {}, hosted: {}, agent: {}, forecast: null },
};
window.__FX_VOUCHER = { ok: false, detail: 'not set' };
window.__posts = [];
const __respond = (d, st) => new Response(JSON.stringify(d),
  { status: st || 200, headers: { 'Content-Type': 'application/json' } });
window.apiFetch = async (url, init) => {
  url = String(url);
  const method = String((init && init.method) || 'GET').toUpperCase();
  if (url.indexOf('/api/workspace/quota') >= 0) return __respond(window.__FX_QUOTA);
  if (method === 'POST' && url.indexOf('/api/admin/license/topup-voucher') >= 0) {
    try { window.__posts.push(JSON.parse(String((init && init.body) || '{}'))); }
    catch (_e) { window.__posts.push({}); }
    return __respond(window.__FX_VOUCHER);
  }
  return __respond({}, 404);
};
window.__qwWatchMs = 60;
</script>
<script>
(function(){
  var _setPill = function(el, html){ el.innerHTML = html; el.style.display = 'inline-flex'; };
  var _pillIc = function(){ return ''; };
@@SEGMENT@@
  window.__fxRefreshQuota = refreshQuota;
})();
</script>
</body></html>
"""


def build_fixture_page(tmp: Path, *, master: bool) -> Path:
    html = (_HTML
            .replace("@@MASTER@@", "true" if master else "false")
            .replace("@@ZH@@", json.dumps(_zh_map(), ensure_ascii=False))
            .replace("@@SEGMENT@@", _wall_segment()))
    fp = tmp / ("probe_wall_master.html" if master else "probe_wall_agent.html")
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
        print(f"\n== 额度墙 v2 验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_SCEN_JS = r"""
async (zh) => {
  const out = {};
  const wall = () => document.getElementById('ws-quota-wall');
  const wait = async (fn, n) => {
    for (let i = 0; i < (n || 60); i++) {
      const v = fn();
      if (v) return v;
      await new Promise((r) => setTimeout(r, 40));
    }
    return null;
  };
  const tick = (ms) => new Promise((r) => setTimeout(r, ms || 80));
  const wallTitle = () => {
    const w = wall();
    if (!w || !w.firstElementChild) return '';
    const box = w.firstElementChild;               // ov > box > [title, body, ...]
    return box.firstElementChild
      ? String(box.firstElementChild.textContent || '') : '';
  };
  const btnByText = (txt) => {
    const w = wall();
    if (!w) return null;
    const els = w.querySelectorAll('a,button');
    for (const el of els) if (el.textContent === txt) return el;
    return null;
  };
  const dispatchBlocked = (src) => document.dispatchEvent(
    new CustomEvent('aitr:quota-blocked', { detail: { source: src } }));
  const setState = (patch) => {
    Object.assign(window.__FX_QUOTA.state, patch || {});
    window.__wsQuotaState = Object.assign({}, window.__wsQuotaState || {},
                                          window.__FX_QUOTA.state);
  };

  // ── S1 被动触发 + 稍后再说 ──
  window.__wsQuotaWall.maybePassive({ wall: 'chars' });
  out.s1_shown = !!wall();
  out.s1_title = wallTitle() === zh['ws.quotawall.title'];
  const later = btnByText(zh['ws.quotawall.later']);
  out.s1_hasLater = !!later;
  if (later) later.click();
  await tick();
  out.s1_closed = !wall();

  // ── S2 每会话每变体一次 ──
  window.__wsQuotaWall.maybePassive({ wall: 'chars' });
  await tick();
  out.s2_suppressed = !wall();

  // ── S3 动作点触发（无视会话键）+ Esc 关 ──
  dispatchBlocked('license_chars');
  out.s3_shown = !!wall();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await tick();
  out.s3_escClosed = !wall();

  // ── S4 动作点 10 分钟去抖；清键即恢复；背板点击关 ──
  dispatchBlocked('license_chars');
  await tick();
  out.s4_debounced = !wall();
  try { localStorage.removeItem('qw2.act.chars'); } catch (_e) {}
  dispatchBlocked('license_chars');
  out.s4_reshow = !!wall();
  if (wall()) wall().click();          // target=ov 本体 → 背板关闭
  await tick();
  out.s4_backdropClosed = !wall();

  // ── S5 与每日推销窗互斥 ──
  const up = document.getElementById('ws-aitrial-upsell');
  up.style.display = 'flex';
  try { sessionStorage.removeItem('qw2.sess.tok'); } catch (_e) {}
  window.__wsQuotaWall.maybePassive({ wall: 'tok' });
  await tick();
  out.s5_mutex = !wall();
  up.style.display = 'none';

  // ── S6 凭证兑换：成功 → toast + 关墙 + 埋点 ──
  try { localStorage.removeItem('qw2.act.chars'); } catch (_e) {}
  dispatchBlocked('license_chars');
  const vin = wall() ? wall().querySelector('input') : null;
  out.s6_hasVoucherInput = !!vin;
  window.__FX_VOUCHER = { ok: true, chars: 1234 };
  if (vin) {
    vin.value = 'VOUCH-TEST';
    const vbtn = btnByText(zh['mb_redeem_btn']);
    if (vbtn) vbtn.click();
  }
  await wait(() => !wall(), 50);
  out.s6_closed = !wall();
  out.s6_posted = !!(window.__posts.length
    && window.__posts[0].voucher === 'VOUCH-TEST');
  const toastTxt = () => {
    const els = document.querySelectorAll('body > div');
    for (const el of els) {
      if (el.id) continue;
      if (el.style && el.style.position === 'fixed'
          && el.style.display === 'block') return el.textContent || '';
    }
    return '';
  };
  out.s6_toast = (toastTxt().indexOf('1.2k') >= 0);
  await wait(() => window.__beacons.indexOf('qw_cta_voucher_ok') >= 0, 30);
  out.s6_beacon = window.__beacons.indexOf('qw_cta_voucher_ok') >= 0;

  // ── S7 充值 watch：open(shop) + watching → 额度改善 → toast + 关墙 ──
  setState({ shop_url: 'https://shop.example/order?sku=recharge-100' });
  try { localStorage.removeItem('qw2.act.chars'); } catch (_e) {}
  try { sessionStorage.removeItem('qw2.sess.chars'); } catch (_e) {}
  window.__FX_QUOTA.level = 'out';
  window.__FX_QUOTA.exceeded = true;
  window.__FX_QUOTA.remaining = 0;
  setState({ wall: 'chars', level: 'out', verdict: 'chars_out' });
  window.__wsQuotaWall.maybePassive({ wall: 'chars' });
  const buyBtn = btnByText(zh['ws.quotawall.recharge']);
  out.s7_hasBuy = !!buyBtn;
  if (buyBtn) buyBtn.click();
  out.s7_opened = window.__opens.length > 0
    && window.__opens[0].indexOf('shop.example') >= 0;
  const note = wall() ? Array.from(wall().querySelectorAll('div')).find(
    (el) => (el.textContent || '') === zh['ws.quotawall.watching']) : null;
  out.s7_watching = !!note;
  // 到账：额度改善 + verdict 清墙
  window.__FX_QUOTA.level = 'ok';
  window.__FX_QUOTA.exceeded = false;
  window.__FX_QUOTA.remaining = 150000;
  setState({ wall: '', level: 'ok', verdict: 'ok' });
  await wait(() => !wall(), 80);
  out.s7_closed = !wall();
  await wait(() => window.__beacons.indexOf('qw_credited') >= 0, 40);
  out.s7_credited = window.__beacons.indexOf('qw_credited') >= 0;

  // ── S8 agent 变体：带 used/quota、无凭证框 ──
  setState({ agent: { enabled: true, blocked: true, used: 500, quota: 500 } });
  try { localStorage.removeItem('qw2.act.agent'); } catch (_e) {}
  dispatchBlocked('agent_chars');
  out.s8_shown = !!wall();
  out.s8_title = wallTitle() === zh['ws.quotawall.title_agent'];
  out.s8_body = !!wall() && (wall().textContent || '').indexOf('500') >= 0;
  out.s8_noVoucher = !!wall() && !wall().querySelector('input');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await tick();

  // ── S9 trial 变体：注册领取主 CTA ──
  setState({ wall: 'trial' });
  try { localStorage.removeItem('qw2.act.trial'); } catch (_e) {}
  dispatchBlocked('license_chars');
  out.s9_title = wallTitle() === zh['ws.quotawall.title_trial'];
  const claim = btnByText(zh['ws.quotawall.claim']);
  out.s9_claim = !!claim && String(claim.getAttribute('href') || '')
    .indexOf('#mb-claim') >= 0;
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await tick();

  // ── S10 预警条：days_left<=3 出条 + 贪睡 ──
  window.__FX_QUOTA.level = 'low';
  window.__FX_QUOTA.remaining = 3000;
  setState({ wall: '', level: 'low', verdict: 'low',
             forecast: { days_left: 2.1, burn7: 1400 } });
  window.__fxRefreshQuota();
  const bar = document.getElementById('ws-quotalow');
  await wait(() => bar.style.display !== 'none', 40);
  out.s10_shown = bar.style.display !== 'none';
  const barTxt = document.getElementById('ws-quotalow-text').textContent || '';
  out.s10_text = barTxt.indexOf('3k') >= 0;
  out.s10_buy = document.getElementById('ws-quotalow-buy').style.display !== 'none';
  document.getElementById('ws-quotalow-x').click();
  await tick();
  out.s10_snoozed = bar.style.display === 'none';
  window.__fxRefreshQuota();
  await tick(200);
  out.s10_staysHidden = bar.style.display === 'none';
  await wait(() => window.__beacons.indexOf('qw_low_shown') >= 0, 30);
  out.s10_beacon = window.__beacons.indexOf('qw_low_shown') >= 0;
  return out;
}
"""

_AGENT_JS = r"""
async (zh) => {
  const out = {};
  const wall = () => document.getElementById('ws-quota-wall');
  const tick = (ms) => new Promise((r) => setTimeout(r, ms || 80));
  window.__wsQuotaWall.maybePassive({ wall: 'chars' });
  out.shown = !!wall();
  out.body_hint = !!wall()
    && (wall().textContent || '').indexOf(zh['ws.quotawall.agent_hint']) >= 0;
  out.hasView = !!wall() && !!Array.from(wall().querySelectorAll('a')).find(
    (a) => a.textContent === zh['ws.quotawall.view']);
  out.noVoucher = !!wall() && !wall().querySelector('input');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await tick();
  // 预警条走坐席口径：text_agent + 无充值钮
  Object.assign(window.__FX_QUOTA, { level: 'low', remaining: 3000 });
  Object.assign(window.__FX_QUOTA.state,
    { level: 'low', forecast: { days_left: 1.5, burn7: 2000 } });
  window.__fxRefreshQuota();
  const bar = document.getElementById('ws-quotalow');
  for (let i = 0; i < 40 && bar.style.display === 'none'; i++) await tick(40);
  out.bar_shown = bar.style.display !== 'none';
  const t = document.getElementById('ws-quotalow-text').textContent || '';
  out.bar_agentCopy = t.length > 0
    && t === (window.Tf('ws.quotalow.text_agent',
      { remaining: '3k', days: '2', date: t.match(/\d+-\d+/) ? t.match(/\d+-\d+/)[0] : '' }));
  out.bar_noBuy = document.getElementById('ws-quotalow-buy').style.display === 'none';
  out.bar_viewShown = document.getElementById('ws-quotalow-view').style.display !== 'none';
  return out;
}
"""


def run(headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    zh = _zh_map()
    with tempfile.TemporaryDirectory(prefix="qw_wall_probe_") as td:
        page_master = build_fixture_page(Path(td), master=True)
        page_agent = build_fixture_page(Path(td), master=False)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(page_master.as_uri())
            ck.check("调度器已挂 window.__wsQuotaWall",
                     page.evaluate("!!(window.__wsQuotaWall && window.__wsQuotaWall.show)"))
            r = page.evaluate(_SCEN_JS, zh)
            if not isinstance(r, dict):
                ck.check("场景链执行", False, str(r))
                browser.close()
                return ck.summary()
            ck.check("[S1] 被动触发弹 chars 墙（zh 标题）+ 稍后再说关墙",
                     r.get("s1_shown") and r.get("s1_title")
                     and r.get("s1_hasLater") and r.get("s1_closed"))
            ck.check("[S2] 每会话每变体只被动弹一次", r.get("s2_suppressed"))
            ck.check("[S3] 动作点触发无视会话键 + Esc 关闭",
                     r.get("s3_shown") and r.get("s3_escClosed"))
            ck.check("[S4] 动作点 10 分钟去抖 / 清键恢复 / 背板点击关",
                     r.get("s4_debounced") and r.get("s4_reshow")
                     and r.get("s4_backdropClosed"))
            ck.check("[S5] 与 #ws-aitrial-upsell 同屏互斥", r.get("s5_mutex"))
            ck.check("[S6] 凭证兑换成功 → 关墙 + POST 契约 + toast + 埋点",
                     r.get("s6_hasVoucherInput") and r.get("s6_closed")
                     and r.get("s6_posted") and r.get("s6_toast")
                     and r.get("s6_beacon"))
            ck.check("[S7] 点充值 → open(shop)+watching；到账 → 自动关墙 + qw_credited",
                     r.get("s7_hasBuy") and r.get("s7_opened")
                     and r.get("s7_watching") and r.get("s7_closed")
                     and r.get("s7_credited"))
            ck.check("[S8] agent 变体：标题/used/quota 数字/无凭证框",
                     r.get("s8_shown") and r.get("s8_title")
                     and r.get("s8_body") and r.get("s8_noVoucher"))
            ck.check("[S9] trial 变体：体验档标题 + 注册领取主 CTA（#mb-claim）",
                     r.get("s9_title") and r.get("s9_claim"))
            ck.check("[S10] 预警条：出条/文案带剩余量/充值钮/按日贪睡/埋点",
                     r.get("s10_shown") and r.get("s10_text") and r.get("s10_buy")
                     and r.get("s10_snoozed") and r.get("s10_staysHidden")
                     and r.get("s10_beacon"))
            # ── 坐席角色页 ──
            page2 = browser.new_page(viewport={"width": 1280, "height": 900})
            page2.goto(page_agent.as_uri())
            r2 = page2.evaluate(_AGENT_JS, zh)
            if not isinstance(r2, dict):
                ck.check("坐席场景链执行", False, str(r2))
            else:
                ck.check("[S11] 坐席墙：agent_hint 正文 + 仅查看链接 + 无凭证框",
                         r2.get("shown") and r2.get("body_hint")
                         and r2.get("hasView") and r2.get("noVoucher"))
                ck.check("[S11b] 坐席预警条：text_agent 口径 + 无充值钮 + 查看链接",
                         r2.get("bar_shown") and r2.get("bar_agentCopy")
                         and r2.get("bar_noBuy") and r2.get("bar_viewShown"))
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
