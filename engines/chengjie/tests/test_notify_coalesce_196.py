# -*- coding: utf-8 -*-
"""#196（L-3 C）断线弹窗出口 + 告警归拢 / 分级。

5BKTRB / 7WPEVQ（skuio，1.0.73→1.0.74）：右下角「whatsapp 通道 (Alixia) 已断线 3 天 9 小时」
只有「1 小时内别再提醒」一个出口，每小时重弹，两天 50+ 次，用户对所有告警麻木。

本批：
- 出路：去重连 / 标为已停用 / 登出 / 1 小时 / 24 小时 / 不再提醒此账号；胶囊动作改
  「处理 ›」重开出路卡；按账号静默本机镜像 + 服务端注册表 meta（换机不丢），快照直接不返回；
  「标为已停用」= offline + operator:disabled（expected_online=False → 横幅不亮/看门狗不催）；
- notify-bus：同 domKey 再报原地更新不重弹；同屏最多 1 张告警卡，第二项折成汇总卡；
  tier=infra/quality 默认只进通知中心。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_BUS = _ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "notify-bus.js"
_TPL = _ENGINE_ROOT / "src" / "web" / "templates" / "workspace_base.html"


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", True, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


# ── 服务端：按账号静默 + 标停用 → 快照不再返回 ───────────────────────────────

def test_mute_and_disable_drop_account_from_snapshot():
    from src.integrations.account_registry import get_account_registry
    from src.integrations.platform_session_health import (
        channel_alert_muted, get_platform_session_health, mark_account_disabled,
        session_expected_online, set_channel_alert_mute,
    )
    from src.web.routes.unified_inbox_setup_routes import _channel_health_snapshot
    reg = get_account_registry()
    tag = str(int(time.time() * 1000))[-7:]
    a, b, c = f"alx{tag}", f"bob{tag}", f"cid{tag}"
    try:
        for acct in (a, b, c):
            reg.upsert("whatsapp", acct, status="online")
        s = get_platform_session_health()
        for acct in (a, b, c):
            s.record("whatsapp", acct, "needs_login")
        shown = {it["account_id"] for it in _channel_health_snapshot()["unhealthy"]}
        assert {a, b, c} <= shown

        # 不再提醒此账号（永久）→ 快照剔除；其它账号照亮
        assert set_channel_alert_mute("whatsapp", a, hours=None) == -1.0
        assert channel_alert_muted(f"whatsapp:{a}")
        # 24 小时 → 到期前剔除、到期后恢复
        t0 = time.time()
        until = set_channel_alert_mute("whatsapp", b, hours=24, now=t0)
        assert until == t0 + 24 * 3600
        assert channel_alert_muted(f"whatsapp:{b}")
        assert not channel_alert_muted(f"whatsapp:{b}", now=until + 1)
        shown = {it["account_id"] for it in _channel_health_snapshot()["unhealthy"]}
        assert a not in shown and b not in shown and c in shown

        # 标为已停用 → offline + operator:disabled → 不再期望在线 → 快照剔除
        assert mark_account_disabled("whatsapp", c, actor="boss")
        row = reg.get("whatsapp", c) or {}
        assert row.get("status") == "offline"
        assert (row.get("meta") or {}).get("offline_reason") == "operator:disabled"
        assert not session_expected_online(f"whatsapp:{c}")
        shown = {it["account_id"] for it in _channel_health_snapshot()["unhealthy"]}
        assert c not in shown
        # 取消静默
        assert set_channel_alert_mute("whatsapp", a, hours=0) == 0.0
        assert not channel_alert_muted(f"whatsapp:{a}")
    finally:
        for acct in (a, b, c):
            try:
                reg.remove("whatsapp", acct)
            except Exception:
                pass


def test_mute_unknown_account_is_noop():
    from src.integrations.platform_session_health import (
        channel_alert_muted, mark_account_disabled, set_channel_alert_mute,
    )
    assert set_channel_alert_mute("whatsapp", "nobody-xyz", hours=None) == 0.0
    assert not channel_alert_muted("whatsapp:nobody-xyz")
    assert not mark_account_disabled("whatsapp", "nobody-xyz")


def test_routes_registered_and_disable_requires_supervisor():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None, config_manager=None)
    paths = {r.path for r in app.routes}
    assert "/api/workspace/channel-alert/mute" in paths
    assert "/api/workspace/channel-alert/disable" in paths
    cli = TestClient(app)
    r = cli.post("/api/workspace/channel-alert/mute", json={"platform": "whatsapp"})
    assert r.status_code == 200 and r.json()["ok"] is False
    # 不在注册表的账号：静默落不了服务端 → stored=false（前端回落本机）
    r = cli.post("/api/workspace/channel-alert/mute",
                 json={"platform": "whatsapp", "account_id": "ghost-x", "hours": "forever"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["stored"] is False


# ── 前端契约（模板热更新直上生产，静态钉住） ─────────────────────────────────

def test_chandown_exits_wired():
    tpl = _TPL.read_text(encoding="utf-8")
    for needle in ("ws.chandown.reconnect_btn", "ws.chandown.disable_btn", "ws.chandown.snooze1h",
                   "ws.chandown.snooze24h", "ws.chandown.mute_acct", "ws.chandown.options",
                   "_chanMuteAcct(first, null)", "_chanDisable(first)", "_chanReconnect(first)",
                   "/api/workspace/channel-alert/mute", "/api/workspace/channel-alert/disable",
                   "ws_chandown_mute", "_chanMutedLocal(s)", "_chanShowCard(_chanCur, true)",
                   "tier: 'channel'"):
        assert needle in tpl, f"断线弹窗出口接线丢失：{needle}"
    # 基础设施类默认只进通知中心
    assert "tier: 'infra'" in tpl
    assert "AITRNotify.setSummaryText(" in tpl


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    from src.web.i18n_packs.notify_center import EN as NEN, ZH as NZH
    for k in ("ws.chandown.options", "ws.chandown.snooze24h", "ws.chandown.mute_acct",
              "ws.chandown.reconnect_btn", "ws.chandown.disable_btn", "ws.chandown.disable_confirm",
              "ws.chandown.disable_ok", "ws.chandown.mute_forever_ok", "ws.chandown.cancel"):
        assert k in ZH and k in EN, k
    assert "ntf.summary_n" in NZH and "ntf.summary_n" in NEN


# ── notify-bus 行为（node 跑总线源码 + 最小 DOM 桩）────────────────────────────

_DOM_STUB = r"""
function El(tag){ this.tag=tag; this.children=[]; this.attrs={}; this.style={}; this.listeners={};
  this.parent=null; this.textContent=''; this.isConnected=true; this.id=''; this.className=''; }
El.prototype.setAttribute=function(k,v){ this.attrs[k]=String(v); if(k==='id') this.id=String(v); };
El.prototype.getAttribute=function(k){ return Object.prototype.hasOwnProperty.call(this.attrs,k)?this.attrs[k]:null; };
El.prototype.appendChild=function(c){ c.parent=this; this.children.push(c); return c; };
El.prototype.removeChild=function(c){ this.children=this.children.filter(function(x){ return x!==c; }); c.parent=null; };
El.prototype.remove=function(){ if(this.parent) this.parent.removeChild(this); };
El.prototype.addEventListener=function(t,f){ (this.listeners[t]=this.listeners[t]||[]).push(f); };
El.prototype.click=function(){ (this.listeners.click||[]).forEach(function(f){ f({}); }); };
El.prototype.getBoundingClientRect=function(){ return {top:0,left:0,width:0,height:0}; };
Object.defineProperty(El.prototype,'firstChild',{get:function(){ return this.children[0]||null; }});
Object.defineProperty(El.prototype,'childElementCount',{get:function(){ return this.children.length; }});
function _match(el, sel){
  var m, rx=/\[([\w-]+)="([^"]*)"\]/g, ok=true, any=false;
  while((m=rx.exec(sel))){ any=true; if(el.getAttribute(m[1])!==m[2]) ok=false; }
  if(!any) return false;
  return ok;
}
El.prototype._all=function(){ var out=[]; (function walk(n){ n.children.forEach(function(c){ out.push(c); walk(c); }); })(this); return out; };
El.prototype.querySelectorAll=function(sel){ return this._all().filter(function(e){ return _match(e, sel); }); };
El.prototype.querySelector=function(sel){ return this.querySelectorAll(sel)[0]||null; };
var document={ body:new El('body'), head:new El('head'),
  createElement:function(t){ return new El(t); },
  getElementById:function(id){ var all=document.body._all().concat(document.head._all()); for(var i=0;i<all.length;i++){ if(all[i].id===id) return all[i]; } return null; },
  querySelector:function(){ return null; },
  dispatchEvent:function(){}, addEventListener:function(){}, removeEventListener:function(){} };
var _ls={}; var localStorage={ getItem:function(k){ return Object.prototype.hasOwnProperty.call(_ls,k)?_ls[k]:null; },
  setItem:function(k,v){ _ls[k]=String(v); }, removeItem:function(k){ delete _ls[k]; },
  key:function(i){ return Object.keys(_ls)[i]||null; } };
Object.defineProperty(localStorage,'length',{get:function(){ return Object.keys(_ls).length; }});
var window={ innerHeight:800 }; function CustomEvent(){}
var setTimeout=function(){ return 1; }, clearTimeout=function(){};
"""

_SCENARIO = r"""
var AITRNotify = window.AITRNotify;
function cards(){ var w=document.getElementById('aitr-ntf-fallback'); return w?w.children.slice():[]; }
function keys(){ return cards().map(function(c){ return c.getAttribute('data-ntf-domkey'); }); }
var out={};
// ① 同 key 3 次告警 → 只 1 张卡，计数 3，且是同一个 DOM 节点（不重弹）
AITRNotify.notify({severity:'error', text:'wa down 1h', domKey:'chandown', centerId:'chandown'});
var first=cards()[0];
AITRNotify.notify({severity:'error', text:'wa down 2h', domKey:'chandown', centerId:'chandown'});
AITRNotify.notify({severity:'error', text:'wa down 3h', domKey:'chandown', centerId:'chandown'});
out.same_key_cards=cards().length;
out.same_key_count=cards()[0].getAttribute('data-ntf-count');
out.same_node=(cards()[0]===first);
out.same_key_text=cards()[0].children[0].children[0].textContent;
// ② 第二项不同告警 → 折成 1 张汇总卡
AITRNotify.notify({severity:'warn', text:'buried 8', domKey:'buried', centerId:'buried'});
out.after_second_cards=cards().length;
out.after_second_keys=keys();
out.summary_text=cards()[0].children[0].children[0].textContent;
// ③ 第三项 → 汇总卡计数 3，仍只 1 张
AITRNotify.notify({severity:'error', text:'wf failed', domKey:'wf_failed'});
out.after_third_cards=cards().length;
out.summary_text3=cards()[0].children[0].children[0].textContent;
// ④ 操作结果（center:false）不参与折叠 → 与汇总卡并存
AITRNotify.notify({severity:'error', text:'relogin failed', domKey:'chandown-op', center:false});
out.op_keys=keys();
// ⑤ 低两级只进通知中心不弹（汇总计数不动）；forceToast 覆写 → 并入汇总（3→4），仍不多一张卡
var before=cards().length;
AITRNotify.notify({severity:'warn', text:'gpu unreachable', tier:'infra', centerId:'gpu'});
AITRNotify.notify({severity:'warn', text:'draft quality yellow', tier:'quality', centerId:'dq'});
out.low_tier_added=cards().length-before;
out.low_tier_summary=cards()[0].children[0].children[0].textContent;
AITRNotify.notify({severity:'warn', text:'forced', tier:'infra', forceToast:true, centerId:'gpu2'});
out.force_added=cards().length-before;
out.force_summary=cards()[0].children[0].children[0].textContent;
// ⑥ 关闭汇总卡后新告警重新单卡出现
cards().forEach(function(c){ c.__ntfClose(); });
AITRNotify.notify({severity:'error', text:'fresh', domKey:'fresh', centerId:'fresh'});
out.fresh_keys=keys();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑 notify-bus 行为")
def test_notify_bus_coalesce_and_tiers_in_node():
    js = _DOM_STUB + "\n" + _BUS.read_text(encoding="utf-8") + "\n" + _SCENARIO
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["same_key_cards"] == 1, out
    assert out["same_key_count"] == "3", out
    assert out["same_node"] is True, "同键再报必须原地更新，不得拆掉重建（那就是「每小时重弹」）"
    assert out["same_key_text"] == "wa down 3h", "同键再报须换成最新文案（持续时长跟着走）"
    assert out["after_second_cards"] == 1 and out["after_second_keys"] == ["__summary"], out
    assert "2" in out["summary_text"], out
    assert out["after_third_cards"] == 1 and "3" in out["summary_text3"], out
    assert sorted(out["op_keys"]) == ["__summary", "chandown-op"], out
    assert out["low_tier_added"] == 0, "infra/quality 级默认不得弹卡"
    assert "3" in out["low_tier_summary"], "低级别不弹也不该改动汇总计数"
    assert out["force_added"] == 0 and "4" in out["force_summary"], (
        "forceToast 须能覆写分级——但屏上已有汇总卡时并入汇总（3→4），不多一张卡")
    assert out["fresh_keys"] == ["fresh"], out


def test_notify_bus_contract_strings():
    bus = _BUS.read_text(encoding="utf-8")
    assert "__ntfUpdate" in bus and "SUMMARY_KEY" in bus and "LOW_TIERS" in bus
    assert "setSummaryText" in bus
    assert "onDismiss" in bus, "#143 胶囊 ✕ 支撑不得丢"
