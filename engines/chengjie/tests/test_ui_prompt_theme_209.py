# -*- coding: utf-8 -*-
"""#209（L-3 D）批量挂链弹层：主题令牌随宿主 / 天数步进器带默认值 / 实时预览 / 取消正常 / 空元素。

YD2SMM（skuio，1.0.74）：uiPrompt 弹层在工作台壳白底压深色主题（令牌链读的
--tk-bg-card / --bg-card 三宿主都没定义 → 回落 #fff）、说明文字浅灰压白底、输入框纯黑
无默认值、「取消」像禁用态、无预览、「工作链配置」标题右侧残留空元素。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_UIP = _ROOT / "shared" / "copilot" / "ui-prompt.js"
_UIP_MIRROR = _ROOT / "desktop" / "renderer" / "shared" / "copilot" / "ui-prompt.js"
_WF = _ROOT / "src" / "web" / "templates" / "workflows.html"
_WS_BASE = _ROOT / "src" / "web" / "templates" / "workspace_base.html"
_BASE = _ROOT / "src" / "web" / "templates" / "base.html"
_INBOX = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_CP_LIGHT = _ROOT / "shared" / "copilot" / "theme-light.css"
_CP_DARK = _ROOT / "shared" / "copilot" / "theme-dark.css"
_ROUTES = _ROOT / "src" / "web" / "routes" / "unified_inbox_workflow_routes.py"


def _defined_vars(text: str) -> set:
    return set(re.findall(r"(--[\w-]+)\s*:", text))


def _used_vars(js: str) -> set:
    return set(re.findall(r"var\((--[\w-]+)", js))


# ── 令牌链在三宿主都能落到真定义（不再回落 #fff 白底）────────────────────────

def test_token_chain_resolves_in_each_host():
    js = _UIP.read_text(encoding="utf-8")
    used = _used_vars(js)
    assert "--tk-bg-card" not in used and "--bg-card" not in used, "旧链里的未定义令牌必须清掉（正是白底根因）"
    hosts = {
        "workspace_base": _defined_vars(_WS_BASE.read_text(encoding="utf-8")),
        "base": _defined_vars(_BASE.read_text(encoding="utf-8")),
        "inbox": _defined_vars(_INBOX.read_text(encoding="utf-8")) | _defined_vars(_WS_BASE.read_text(encoding="utf-8")),
        "copilot_app": _defined_vars(_CP_LIGHT.read_text(encoding="utf-8")) | _defined_vars(_CP_DARK.read_text(encoding="utf-8")),
    }
    # 每个宿主：面板底 / 正文色 / 边框 / 品牌 四类至少各命中一个链上令牌
    roles = {
        "surface": ("--cp-surface", "--tk-surface", "--card", "--bg-panel"),
        "text": ("--cp-text", "--tk-text", "--t", "--text-main"),
        "border": ("--cp-border", "--tk-border", "--bd", "--border"),
        "brand": ("--p", "--tk-brand", "--cp-accent", "--accent"),
    }
    for role, chain in roles.items():
        for tok in chain:
            assert tok in used, f"令牌链缺 {tok}（{role}）"
        for host, defined in hosts.items():
            assert any(tok in defined for tok in chain), f"{host} 宿主上 {role} 令牌链全部未定义 → 会回落字面量"
    # 暗色下面板底必须跟着宿主翻：三宿主的暗段都定义了链上令牌（workspace_base [data-theme=dark] 段同文件）
    dark_ws = _WS_BASE.read_text(encoding="utf-8")
    assert dark_ws.count("--tk-surface:") >= 2, "workspace_base 暗色段缺 --tk-surface 翻转"


def test_mirror_in_sync_and_contract_strings():
    assert _UIP.read_bytes() == _UIP_MIRROR.read_bytes(), "双树镜像不一致（scripts/sync_copilot_mirror.py）"
    js = _UIP.read_text(encoding="utf-8")
    for needle in ("uip-step", "uip-hint", "uip-preview", "opts.preview", "opts.min", "opts.max",
                   "window.uiPrompt = uiPrompt", '"Escape"', "mousedown", "uip-btn.ok:disabled"):
        assert needle in js, needle
    # 取消按钮：显式正文色（源码里按令牌常量拼接），不再 color:inherit 在错误底色上像禁用
    assert 'background:transparent;color:" + TEXT' in js, "取消/普通按钮须显式取宿主正文色"
    assert 'var TEXT = "var(--cp-text,var(--tk-text,var(--t,var(--text-main,' in js


# ── node：步进器夹值 / 默认值 / 预览 / Enter / Esc ───────────────────────────

_DOM = r"""
function El(tag){ this.tag=tag; this.children=[]; this.attrs={}; this.style={}; this.listeners={}; this.parentNode=null;
  this.textContent=''; this.value=''; this.disabled=false; this.className=''; this.id=''; this.type='text';
  var self=this; this.classList={ add:function(c){ if((' '+self.className+' ').indexOf(' '+c+' ')<0) self.className=(self.className+' '+c).trim(); },
    remove:function(c){ self.className=(' '+self.className+' ').replace(' '+c+' ',' ').trim(); } }; }
El.prototype.setAttribute=function(k,v){ this.attrs[k]=String(v); if(k==='id') this.id=String(v); };
El.prototype.getAttribute=function(k){ return this.attrs.hasOwnProperty(k)?this.attrs[k]:null; };
El.prototype.appendChild=function(c){ c.parentNode=this; this.children.push(c); return c; };
El.prototype.removeChild=function(c){ this.children=this.children.filter(function(x){return x!==c;}); c.parentNode=null; };
El.prototype.addEventListener=function(t,f){ (this.listeners[t]=this.listeners[t]||[]).push(f); };
El.prototype.removeEventListener=function(){};
El.prototype.fire=function(t,ev){ (this.listeners[t]||[]).forEach(function(f){ f(ev||{target:null,preventDefault:function(){},stopPropagation:function(){}}); }); };
El.prototype.focus=function(){}; El.prototype.select=function(){};
El.prototype.all=function(){ var out=[]; (function w(n){ n.children.forEach(function(c){ out.push(c); w(c); }); })(this); return out; };
El.prototype.byClass=function(cls){ return this.all().filter(function(e){ return (' '+e.className+' ').indexOf(' '+cls+' ')>=0; }); };
var _docListeners={};
var document={ body:new El('body'), head:new El('head'), documentElement:new El('html'), activeElement:null,
  createElement:function(t){ return new El(t); },
  getElementById:function(id){ var all=document.body.all().concat(document.head.all()); for(var i=0;i<all.length;i++){ if(all[i].id===id) return all[i]; } return null; },
  addEventListener:function(t,f){ (_docListeners[t]=_docListeners[t]||[]).push(f); },
  removeEventListener:function(t,f){ _docListeners[t]=(_docListeners[t]||[]).filter(function(x){return x!==f;}); } };
document.documentElement.getAttribute=function(){ return 'zh-CN'; };
var navigator={language:'zh-CN'};
var window={};
var _timers=[]; var setTimeout=function(f){ _timers.push(f); return _timers.length; }; var clearTimeout=function(){};
function flush(){ var t=_timers.slice(); _timers=[]; t.forEach(function(f){ f(); }); }
function key(k,target){ (_docListeners.keydown||[]).slice().forEach(function(f){ f({key:k,target:target,preventDefault:function(){},stopPropagation:function(){}}); }); }
"""

_SCENARIO = r"""
var out={}; var previews=[];
var p=window.uiPrompt('title','3',{type:'number',min:1,max:90,step:1,hint:'h',unit:'d',preview:function(v){ previews.push(v); return 'match '+v; }});
var card=document.body.byClass('uip-card')[0];
var input=card.byClass('uip-input')[0];
var steps=card.byClass('uip-step');
var okb=card.byClass('ok')[0];
var cancel=card.byClass('uip-btn').filter(function(b){ return b!==okb; })[0];
out.default_value=input.value;
out.has_stepper=steps.length===2;
out.has_hint=card.byClass('uip-hint').length===1;
out.has_unit=card.byClass('uip-unit').length===1;
out.cancel_disabled=!!cancel.disabled;
out.cancel_class=cancel.className;
steps[1].fire('click'); steps[1].fire('click');   // 3 → 5
out.after_plus=input.value;
input.value='999'; input.fire('input'); input.fire('blur');   // 越界回弹 90
out.after_over=input.value;
out.plus_disabled_at_max=!!steps[1].disabled;
input.value='0'; input.fire('input'); input.fire('blur');     // 下界 1
out.after_under=input.value;
out.minus_disabled_at_min=!!steps[0].disabled;
flush();                                                     // 去抖后的预览（结果经 Promise 微任务落 DOM）
Promise.resolve().then(function(){}).then(function(){
  out.preview_text=card.byClass('uip-preview')[0].textContent;
  out.preview_calls=previews.length>=1;
  key('Enter', input);
});
p.then(function(v){ out.resolved=v;
  var p2=window.uiPrompt('t2','7',{type:'number',min:1,max:90});
  key('Escape', null);
  return p2;
}).then(function(v2){ out.resolved_esc=v2; out.cards_left=document.body.byClass('uip-card').length; console.log(JSON.stringify(out)); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑弹层行为")
def test_stepper_preview_and_exits_in_node():
    js = _DOM + "\n" + _UIP.read_text(encoding="utf-8") + "\n" + _SCENARIO
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["default_value"] == "3"
    assert out["has_stepper"] and out["has_hint"] and out["has_unit"], out
    assert out["cancel_disabled"] is False and out["cancel_class"] == "uip-btn", "取消按钮必须是可用的普通按钮"
    assert out["after_plus"] == "5"
    assert out["after_over"] == "90" and out["plus_disabled_at_max"] is True, "越上界须回弹到 90"
    assert out["after_under"] == "1" and out["minus_disabled_at_min"] is True, "越下界须回弹到 1"
    assert out["preview_calls"] is True and out["preview_text"] == "match 1", out
    assert out["resolved"] == "1"
    assert out["resolved_esc"] is None and out["cards_left"] == 0, "Esc 须 resolve(null) 且收掉弹层"


# ── workflows.html 接线 + 路由三数 ────────────────────────────────────────────

def _fn_src(html: str, name: str) -> str:
    m = re.search(r"^" + re.escape(name) + r"=function\(", html, re.M)
    assert m, f"workflows.html 丢失 {name}"
    i = html.index("{", m.end())
    depth, j = 0, i
    while j < len(html):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[m.start():j + 1]
        j += 1
    raise AssertionError("花括号不配对")


def test_bulk_start_uses_stepper_preview_and_no_native_confirm():
    html = _WF.read_text(encoding="utf-8")
    src = _fn_src(html, "WF.bulkStart")
    assert "min:1, max:90" in src and "||3;" in src, "天数须带默认值 3、范围 1–90"
    assert "preview:function(v)" in src and "dry_run:true" in src, "确认前须走 dry_run 实时预览"
    assert "wf_bulk_preview" in src and "wf_bulk_prompt_hint" in src
    assert not re.search(r"(?<![\w.$])confirm\s*\(", src), "弹层内已有预览+确认，不得再弹原生 confirm"
    assert "exec_ids" in src and "chain-executions/" in src and "wf_bulk_undo" in src, "「已挂 N 个（可撤销）」须能逐条撤回"
    assert "#seed-msg:empty{display:none;}" in html, "标题右侧空元素须在空时不占位"


def test_bulk_start_route_returns_preview_counts_and_exec_ids():
    src = _ROUTES.read_text(encoding="utf-8")
    seg = src[src.index('/bulk-start")'):]
    seg = seg[: seg.index("@app.get(")]
    for needle in ('"matched": matched', '"eligible": eligible', '"skipped": skipped',
                   '"exec_ids": exec_ids', "if len(candidates) < limit:"):
        assert needle in seg, needle


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.workflows_page import EN, ZH
    for k in ("wf_bulk_prompt_title", "wf_bulk_prompt_hint", "wf_bulk_unit_days", "wf_bulk_ok",
              "wf_bulk_cancel", "wf_bulk_preview", "wf_bulk_preview_fail", "wf_bulk_done_undo",
              "wf_bulk_undo", "wf_bulk_undo_done", "wf_bulk_undo_partial"):
        assert k in ZH and k in EN, k
    assert "{n}" in ZH["wf_bulk_preview"] and "{w}" in ZH["wf_bulk_preview"] and "{s}" in ZH["wf_bulk_preview"]


def test_hosts_bumped_ui_prompt_stamp():
    for host in (_ROOT / "src" / "web" / "templates" / "_win_unique.html",
                 _ROOT / "shared" / "copilot" / "app.html"):
        # 2026-09-11：uiPrompt 增 danger/match（用户管理删除逐字确认）→ 戳号同步抬
        assert "ui-prompt.js?v=20260911a" in host.read_text(encoding="utf-8"), host.name


def test_ui_prompt_danger_and_match_contract():
    """danger：确认键走危险色令牌链（不引入新字面量之外的品牌色）；match：输入不等于期望值时
    确认键禁用、Enter 不过闸。users.html 删除确认必须同时用上这两项。"""
    body = _UIP.read_text(encoding="utf-8")
    assert 'var DANGER = "var(--red,var(--tk-danger,var(--cp-danger,var(--danger,#dc2626))))"' in body
    assert ".uip-btn.ok.danger{background:" in body
    assert "var isDanger = !!opts.danger;" in body
    assert "function matchOk()" in body and "if (!matchOk())" in body
    users = (_ROOT / "src" / "web" / "templates" / "users.html").read_text(encoding="utf-8")
    assert re.search(r"window\.uiPrompt\([^;]*danger:true[^;]*match:uname", users)
    assert _UIP.read_bytes() == _UIP_MIRROR.read_bytes(), "镜像漂移：python -m scripts.sync_copilot_mirror"
