/*!
 * notify-bus.js — 统一通知总线 v1（实施75 P0/P1-core，2026-08-27）。
 *
 * 单一入口，三个渲染面：
 *   AITRNotify.notify(opts)        → 右下 toast（委托 CRMW.toast；CRMW 缺席自带最小渲染）
 *                                     + 消息中心留痕（__wsNotif.add，type=sys_status）
 *   AITRNotify.ongoing.set/clear   → 右下「系统状态胶囊」——顶部横幅的「常驻性」由它
 *                                     承接：一行小 chip、不通栏、不推挤内容，点击开通知中心。
 *
 * 纪律（实施75 目标形态）：
 *   - 本文件不做 i18n（调用方传成品文案）、不做轮询（调用方喂数据）；
 *   - 不承载功能信号（quiet_poll / __wsRestartCool 等照旧走原链路，视觉与信号解耦）；
 *   - 顶部区域只放导航与身份，新状态一律走本总线，禁止再往 wsBanner 加横幅。
 *
 * notify(opts)：
 *   severity   'info'|'success'|'warn'|'error'（缺省 info）
 *   text       toast 文案；空 = 不弹 toast
 *   ttlMs      0 = 不弹 toast 只留痕；>0 = 自定停留时长（仅卡片渲染路径生效）
 *   dedupKey   同键在 dedupTtlMs（默认 6h）内只提示一次（localStorage，跨标签页生效）
 *   centerId   消息中心合并 id（sys_status 按 id 合并，同 id 只保留最新一条）
 *   centerText 消息中心文案（缺省用 text）
 *   center     false = 不写消息中心
 *   actions    [{label, onClick|href}]——带按钮的卡片 toast（自带渲染器，CRMW 无按钮能力）
 *   sticky     true = 不自动消失（必须点 ✕ 或任一按钮；用于「会话已过期」级须知）
 *   domKey     同键的存活卡片只保留一张（原地更新文案，不叠罗汉）
 * 另有 AITRNotify.dismiss(domKey)：程序化撤下某张存活卡片（如状态恢复时）。
 */
(function(){
  'use strict';
  if(window.AITRNotify) return;

  var LS_SEEN = 'aitr.ntf.seen.';

  function _lsGet(k){ try{ return localStorage.getItem(k); }catch(_e){ return null; } }
  function _lsSet(k, v){ try{ localStorage.setItem(k, v); }catch(_e){} }

  /* 开机顺手回收 7 天前的 seen 键，防 localStorage 长灰 */
  (function _gc(){
    try{
      var now = Date.now(), kill = [];
      for(var i = 0; i < localStorage.length; i++){
        var k = localStorage.key(i);
        if(!k || k.indexOf(LS_SEEN) !== 0) continue;
        var v = parseInt(localStorage.getItem(k) || '0', 10) || 0;
        if(!v || now - v > 7 * 86400000) kill.push(k);
      }
      for(var j = 0; j < kill.length; j++) localStorage.removeItem(kill[j]);
    }catch(_e){}
  })();

  /* severity → CRMW.toast 色值（crm-widgets 会映射到 .tk-toast.ok/.err/.warn 语义类） */
  var SEV_COLOR = { success:'#16a34a', error:'#dc2626', warn:'#d97706', info:'' };

  function _cardWrap(){
    var wrap = document.getElementById('aitr-ntf-fallback');
    if(!wrap){
      wrap = document.createElement('div');
      wrap.id = 'aitr-ntf-fallback';
      wrap.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;'
        + 'display:flex;flex-direction:column;gap:8px;align-items:flex-end;';
      document.body.appendChild(wrap);
    }
    return wrap;
  }

  /* 卡片渲染器：带按钮/置顶/同键去重的 toast（也是 CRMW 缺席时纯文本的兜底）。
     视觉对齐 .tk-toast 家族：深底、圆角、左缘语义色条。 */
  function _cardToast(o){
    var wrap = _cardWrap();
    if(o.domKey){
      var live = wrap.querySelector('[data-ntf-domkey="' + o.domKey + '"]');
      if(live){ try{ live.remove(); }catch(_e){} }
    }
    var t = document.createElement('div');
    t.setAttribute('role', o.severity === 'error' ? 'alert' : 'status');
    if(o.domKey) t.setAttribute('data-ntf-domkey', String(o.domKey));
    t.style.cssText = 'max-width:360px;background:#232429;color:#fff;padding:10px 14px;'
      + 'border-radius:10px;font-size:13px;line-height:1.5;pointer-events:auto;'
      + 'box-shadow:0 6px 18px rgba(0,0,0,.28);border-left:3px solid '
      + (SEV_COLOR[o.severity] || 'var(--tk-brand,#1e8cf2)') + ';';
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:flex-start;gap:10px;';
    var msg = document.createElement('span');
    msg.style.cssText = 'flex:1 1 auto;';
    msg.textContent = String(o.text || '');
    row.appendChild(msg);
    var x = document.createElement('button');
    x.type = 'button';
    x.textContent = '\u2715';
    x.setAttribute('aria-label', 'close');
    x.style.cssText = 'flex:0 0 auto;border:none;background:transparent;color:#fff;'
      + 'opacity:.65;cursor:pointer;font-size:12px;padding:0 2px;line-height:1.5;';
    x.addEventListener('click', function(){ _close(); });
    row.appendChild(x);
    t.appendChild(row);
    var timer = null;
    function _close(){
      if(timer){ clearTimeout(timer); timer = null; }
      try{ t.remove(); }catch(_e){}
    }
    var acts = o.actions || [];
    if(acts.length){
      var bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;';
      for(var i = 0; i < acts.length; i++){
        (function(a){
          if(!a || !a.label) return;
          var b = document.createElement('button');
          b.type = 'button';
          b.textContent = String(a.label);
          b.style.cssText = 'border:1px solid rgba(255,255,255,.45);background:rgba(255,255,255,.12);'
            + 'color:#fff;border-radius:7px;padding:3px 12px;font-size:12px;font-weight:600;cursor:pointer;';
          b.addEventListener('click', function(){
            _close();
            try{
              if(typeof a.onClick === 'function') a.onClick();
              else if(a.href) location.href = String(a.href);
            }catch(_e){}
          });
          bar.appendChild(b);
        })(acts[i]);
      }
      t.appendChild(bar);
    }
    wrap.appendChild(t);
    if(!o.sticky){
      var ttl = (o.ttlMs && o.ttlMs > 0) ? o.ttlMs
        : (acts.length ? 15000 : (o.severity === 'error' ? 8000 : 6000));
      timer = setTimeout(_close, ttl);
    }
    return t;
  }

  function _toast(opts){
    if(!opts.text) return;
    var hasActs = !!(opts.actions && opts.actions.length);
    if(!hasActs && !opts.sticky && !opts.domKey && window.CRMW && window.CRMW.toast){
      try{
        window.CRMW.toast(String(opts.text), SEV_COLOR[opts.severity] || '');
        _syncToastOffset();
        return;
      }catch(_e){}
    }
    _cardToast(opts);
  }

  function _center(id, text){
    if(!text) return;
    var sid = String(id || ('sys:' + Date.now()));
    if(window.__wsNotif && window.__wsNotif.add){
      try{
        window.__wsNotif.add({ type: 'sys_status', data: { id: sid, text: String(text) } });
      }catch(_e){}
    }
    /* 实施75 batch4：服务端留痕（进程级通知队列，按 id 合并）——页面刷新 /
       SSE 断线重连经 notifications 历史接口回放，消息中心不再「刷新即忘」。
       旧后端无此路由（404）＝静默跳过，绝不影响本地提示。 */
    try{
      if(window.apiFetch){
        window.apiFetch('/api/workspace/notifications/sys-status', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: sid, text: String(text).slice(0, 300)})
        }, {timeoutMs: 8000}).catch(function(){});
      }
    }catch(_e){}
  }

  function notify(opts){
    opts = opts || {};
    if(opts.dedupKey){
      var k = LS_SEEN + opts.dedupKey;
      var prev = parseInt(_lsGet(k) || '0', 10) || 0;
      var ttl = opts.dedupTtlMs || 6 * 3600000;
      if(prev && (Date.now() - prev) < ttl) return false;
      _lsSet(k, String(Date.now()));
    }
    if(opts.ttlMs !== 0){
      _toast({ text: opts.text, severity: opts.severity || 'info',
               actions: opts.actions, sticky: !!opts.sticky,
               domKey: opts.domKey, ttlMs: opts.ttlMs });
    }
    if(opts.center !== false) _center(opts.centerId || opts.dedupKey, opts.centerText || opts.text);
    return true;
  }

  function dismiss(domKey){
    if(!domKey) return;
    var wrap = document.getElementById('aitr-ntf-fallback');
    if(!wrap) return;
    var live = wrap.querySelector('[data-ntf-domkey="' + domKey + '"]');
    if(live){ try{ live.remove(); }catch(_e){} }
  }

  var _SEV_ALIAS = { ok:'success', err:'error', error:'error', success:'success',
                     warn:'warn', info:'info' };

  /* 原生 alert 的统一替身（实施75 batch4）：右下 toast，不阻塞。
     缺省按 error（存量 alert 八成是失败反馈）；成功/告警显式传 'ok'/'warn'。
     经典后台壳优先走 showToast——报障 CTA 的 MutationObserver 挂在 .toast.err
     上（实施51），走自带卡片会丢报障入口。全渲染面缺席时回落原生，绝不吞反馈。 */
  function uxAlert(text, severity){
    var sev = _SEV_ALIAS[String(severity || 'error')] || 'error';
    var msg = String(text == null ? '' : text);
    if(!msg) return;
    try{
      if(typeof window.showToast === 'function'){
        window.showToast(msg, sev === 'success' ? 'ok' : (sev === 'error' ? 'err' : sev));
        return;
      }
      notify({severity: sev, text: msg, center: false});
    }catch(_e){
      try{ window.alert(msg); }catch(__e){}
    }
  }

  /* 原生 confirm 的统一替身（Promise<boolean>，中央卡片，Esc/外点=取消，
     Enter=确认）。调用方须传本地化按钮文案（本总线不做 i18n）：
     AITRNotify.confirm(text, {okText, cancelText, danger}) 。渲染失败回落原生。 */
  function uxConfirm(text, opts){
    opts = opts || {};
    return new Promise(function(resolve){
      var mask, onKey;
      function done(v){
        try{ document.removeEventListener('keydown', onKey, true); }catch(_e){}
        try{ if(mask) mask.remove(); }catch(_e){}
        resolve(!!v);
      }
      try{
        mask = document.createElement('div');
        mask.setAttribute('data-ntf-confirm', '1');
        mask.style.cssText = 'position:fixed;inset:0;z-index:10060;background:rgba(15,23,42,.5);'
          + 'display:flex;align-items:center;justify-content:center;padding:16px;';
        var card = document.createElement('div');
        card.setAttribute('role', 'alertdialog');
        card.style.cssText = 'background:var(--tk-surface,#fff);color:var(--tk-text,#0f172a);'
          + 'border:1px solid var(--tk-border,#e2e8f0);border-radius:14px;max-width:420px;width:100%;'
          + 'padding:18px 20px;box-shadow:0 20px 50px rgba(2,6,23,.35);font-size:14px;line-height:1.6;';
        var msg = document.createElement('div');
        msg.style.cssText = 'white-space:pre-wrap;word-break:break-word;';
        msg.textContent = String(text == null ? '' : text);
        card.appendChild(msg);
        var bar = document.createElement('div');
        bar.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;margin-top:14px;';
        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.textContent = String(opts.cancelText || 'Cancel');
        cancel.style.cssText = 'border:1px solid var(--tk-border,#e2e8f0);background:transparent;'
          + 'color:var(--tk-text-muted,#64748b);border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;';
        cancel.addEventListener('click', function(){ done(false); });
        var okb = document.createElement('button');
        okb.type = 'button';
        okb.textContent = String(opts.okText || 'OK');
        okb.style.cssText = 'border:none;border-radius:8px;padding:6px 16px;font-size:13px;'
          + 'font-weight:600;cursor:pointer;color:#fff;background:'
          + (opts.danger ? '#dc2626' : 'var(--tk-brand,#1e8cf2)') + ';';
        okb.addEventListener('click', function(){ done(true); });
        bar.appendChild(cancel);
        bar.appendChild(okb);
        card.appendChild(bar);
        mask.appendChild(card);
        mask.addEventListener('click', function(e){ if(e.target === mask) done(false); });
        onKey = function(e){
          if(e.key === 'Escape'){ e.preventDefault(); done(false); }
          else if(e.key === 'Enter'){ e.preventDefault(); done(true); }
        };
        document.addEventListener('keydown', onKey, true);
        document.body.appendChild(mask);
        try{ okb.focus(); }catch(_e){}
      }catch(_e){
        var v = false;
        try{ v = window.confirm(String(text == null ? '' : text)); }catch(__e){}
        resolve(v);
      }
    });
  }

  /* ── 系统状态胶囊（横幅「常驻性」的新家）───────────────────────────── */
  var _ongoing = {};   /* id -> {text, tone, title} */
  var _box = null;
  var TONE_COLOR = { info:'var(--tk-brand,#1e8cf2)', warn:'#f59e0b', error:'#ef4444', success:'#22c55e' };

  function _ensureBox(){
    if(_box && _box.isConnected) return _box;
    _box = document.getElementById('ws-status-capsule');
    if(_box) return _box;
    var st = document.createElement('style');
    st.textContent = '#ws-status-capsule{position:fixed;right:16px;bottom:16px;z-index:9998;'
      + 'display:flex;flex-direction:column;gap:6px;align-items:flex-end;}'
      + '#ws-status-capsule .wsntf-chip{display:flex;align-items:center;gap:7px;max-width:320px;'
      + 'background:rgba(15,23,42,.92);color:#e2e8f0;border:1px solid rgba(148,163,184,.25);'
      + 'border-radius:999px;padding:5px 12px;font-size:12px;line-height:1.4;cursor:pointer;'
      + 'box-shadow:0 4px 14px rgba(0,0,0,.25);user-select:none;}'
      + '#ws-status-capsule .wsntf-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;}'
      + '#ws-status-capsule .wsntf-txt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}'
      /* 窄屏让开收件箱 56px 移动平台条 + 间隙（与筛选抽屉同决策） */
      + '@media (max-width:860px){#ws-status-capsule{bottom:76px;right:10px;}}';
    document.head.appendChild(st);
    _box = document.createElement('div');
    _box.id = 'ws-status-capsule';
    _box.setAttribute('role', 'status');
    _box.setAttribute('aria-live', 'polite');
    document.body.appendChild(_box);
    return _box;
  }

  function _openCenter(){
    var b = document.getElementById('ws-notif-btn');
    if(b){ try{ b.click(); }catch(_e){} }
  }

  function _syncToastOffset(){
    /* 胶囊在场时把 toast 栈抬到胶囊上方，避免右下角互相叠压 */
    try{
      var wrap = document.getElementById('ws-toast-box') || document.querySelector('.tk-toast-wrap');
      if(!wrap) return;
      if(_box && _box.isConnected && _box.childElementCount > 0){
        var r = _box.getBoundingClientRect();
        wrap.style.bottom = Math.max(16, Math.round(window.innerHeight - r.top + 8)) + 'px';
      }else{
        wrap.style.bottom = '';
      }
    }catch(_e){}
  }

  function _render(){
    var box = _ensureBox();
    var ids = Object.keys(_ongoing);
    while(box.firstChild) box.removeChild(box.firstChild);
    for(var i = 0; i < ids.length; i++){
      var o = _ongoing[ids[i]];
      var chip = document.createElement('div');
      chip.className = 'wsntf-chip';
      chip.setAttribute('data-ntf-id', ids[i]);
      if(o.title) chip.title = o.title;
      var dot = document.createElement('span');
      dot.className = 'wsntf-dot';
      dot.style.background = TONE_COLOR[o.tone] || TONE_COLOR.info;
      var txt = document.createElement('span');
      txt.className = 'wsntf-txt';
      txt.textContent = o.text;
      chip.appendChild(dot);
      chip.appendChild(txt);
      chip.addEventListener('click', _openCenter);
      box.appendChild(chip);
    }
    box.style.display = ids.length ? 'flex' : 'none';
    _syncToastOffset();
    /* 消息中心「进行中」分组等消费方按此事件重绘 */
    try{ document.dispatchEvent(new CustomEvent('aitr:ntf-ongoing')); }catch(_e){}
  }

  window.AITRNotify = {
    notify: notify,
    dismiss: dismiss,
    alert: uxAlert,
    confirm: uxConfirm,
    ongoing: {
      set: function(id, o){
        if(!id) return;
        o = o || {};
        _ongoing[String(id)] = {
          text: String(o.text || ''),
          tone: String(o.tone || 'info'),
          title: String(o.title || '')
        };
        _render();
      },
      clear: function(id){
        if(_ongoing[String(id)]){ delete _ongoing[String(id)]; _render(); }
      },
      active: function(){ return Object.keys(_ongoing); },
      snapshot: function(){
        var out = [];
        for(var id in _ongoing){
          var o = _ongoing[id];
          out.push({ id: id, text: o.text, tone: o.tone, title: o.title });
        }
        return out;
      }
    }
  };
})();
