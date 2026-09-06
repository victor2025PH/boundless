/*!
 * notify-bus.js — 统一通知总线 v1（实施75 P0/P1-core，2026-08-27）。
 *
 * 单一入口，三个渲染面：
 *   AITRNotify.notify(opts)        → 右下 toast（委托 CRMW.toast；CRMW 缺席自带最小渲染）
 *                                     + 消息中心留痕（__wsNotif.add，type=sys_status）
 *   AITRNotify.ongoing.set/clear   → 右下「系统状态胶囊」——顶部横幅的「常驻性」由它
 *                                     承接：小 chip、不通栏、不推挤内容，点击开通知中心；
 *                                     带 action 时右侧常驻一个动作按钮（见下方 ongoing.set）。
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
 *   domKey     同键的存活卡片只保留一张：原地更新文案 + **计数聚合**（#53 四件套④，
 *              2026-08-30）——存活期内同键再报显示「×N」徽标而不是叠罗汉/纯覆盖，
 *              「工作链失败 ×12」一眼读出规模；卡片关闭即计数清零。
 * 另有 AITRNotify.dismiss(domKey)：程序化撤下某张存活卡片（如状态恢复时）。
 *
 * ongoing.set(id, o) 新增 o.dismissMs（#53 四件套①，2026-08-30）：>0 时胶囊带 ✕，
 * 点 ✕ 本 id 静默 dismissMs 毫秒（localStorage 跨标签页），期内 set 直接忽略——
 * 「告警通道未接通」这类对当前用户语义有限的常驻提示从此可以请走（0830 原图 811
 * 实锤：胶囊盖住工具箱/语音面板且无任何关闭手段）。不传 = 旧行为（不可关）。
 *
 * ongoing.set(id, o) 另支持 o.onDismiss / o.dismissTitle（工单 #143，2026-09-02）：
 * onDismiss=函数时胶囊同样带 ✕，但静默语义交还调用方（如 chandown 按「异常集合
 * 签名」记 sessionStorage——同事件本会话不再弹、新事故照常弹），总线不落 LS；
 * 与 dismissMs 可并存（都传则两种记账都执行）。dismissTitle = ✕ 的 tooltip 文案。
 *
 * 告警归拢 + 分级（#196，L-3 C，2026-09-06）：
 *   - 同 domKey 再报 → **原地更新**（换文案、×N 计数、重置停留），不再拆掉重建
 *     ——「只更新持续时长不重弹」；
 *   - 右下角**同时最多 1 张告警卡**（severity warn/error 且进了通知中心的卡）：
 *     第二项不同告警到来时两张一起折成一张汇总卡「有 N 项需要注意 ›」，点开进
 *     通知中心；操作结果（center:false，如 chandown-op）与 info/success 不参与折叠；
 *   - opts.tier 'customer'|'channel'|'infra'|'quality'：后两级默认只进通知中心不弹
 *     （opts.forceToast=true 可覆写）。客户在等 > 通道断线 > 基础设施 > 质量黄灯。
 *   - 汇总文案由宿主经 AITRNotify.setSummaryText(fn(n)) 注入（本文件不做 i18n），
 *     缺省英文。
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

  /* ── #196 归拢状态 ── */
  var SUMMARY_KEY = '__summary';
  var _folded = 0;                 /* 汇总卡代表的告警项数（关闭汇总卡即清零） */
  var _summaryText = function(n){ return n + ' item' + (n === 1 ? '' : 's') + ' need attention \u203a'; };
  var LOW_TIERS = { infra:1, quality:1 };

  function _isAlertCard(o){
    /* 参与归拢的判据：告警级 + 进了通知中心（用户折起后仍能在中心找到全文） */
    return (o.severity === 'warn' || o.severity === 'error') && o.center !== false
      && o.domKey !== SUMMARY_KEY;
  }
  function _liveAlertCards(wrap){
    var out = [], all = wrap.querySelectorAll('[data-ntf-alert="1"]');
    for(var i = 0; i < all.length; i++){
      if(all[i].getAttribute('data-ntf-domkey') !== SUMMARY_KEY) out.push(all[i]);
    }
    return out;
  }
  function _showSummary(wrap, n){
    _folded = n;
    var live = wrap.querySelector('[data-ntf-domkey="' + SUMMARY_KEY + '"]');
    var text = _summaryText(n);
    if(live && live.__ntfUpdate){ live.__ntfUpdate(text, n); return live; }
    return _cardToast({ severity: 'warn', text: text, domKey: SUMMARY_KEY, center: false,
                        ttlMs: 30000, _summary: true, onClick: _openCenter,
                        onClose: function(){ _folded = 0; } });
  }

  /* 卡片渲染器：带按钮/置顶/同键去重的 toast（也是 CRMW 缺席时纯文本的兜底）。
     视觉对齐 .tk-toast 家族：深底、圆角、左缘语义色条。 */
  function _cardToast(o){
    var wrap = _cardWrap();
    var aggCount = 1;
    var isAlert = _isAlertCard(o);
    if(o.domKey){
      var live = wrap.querySelector('[data-ntf-domkey="' + o.domKey + '"]');
      if(live){
        /* #53-④ 同类聚合 → #196 原地更新：同键再报换文案 + ×N，**不重弹**（旧实现
           拆掉重建＝视觉上每小时「又弹一次」，50+ 次即由此来） */
        aggCount = (parseInt(live.getAttribute('data-ntf-count') || '1', 10) || 1) + 1;
        if(live.__ntfUpdate){ live.__ntfUpdate(String(o.text || ''), aggCount); return live; }
        try{ live.remove(); }catch(_e){}
      }
    }
    if(isAlert){
      /* 同屏最多 1 张告警卡：已有别的告警在场 → 一起折成汇总卡（全文都在通知中心） */
      var others = _liveAlertCards(wrap);
      var hasSummary = !!wrap.querySelector('[data-ntf-domkey="' + SUMMARY_KEY + '"]');
      if(others.length || hasSummary){
        for(var k = 0; k < others.length; k++){ try{ others[k].remove(); }catch(_e2){} }
        return _showSummary(wrap, (hasSummary ? _folded : others.length) + 1);
      }
    }
    var t = document.createElement('div');
    t.setAttribute('role', o.severity === 'error' ? 'alert' : 'status');
    if(o.domKey){
      t.setAttribute('data-ntf-domkey', String(o.domKey));
      t.setAttribute('data-ntf-count', String(aggCount));
    }
    if(isAlert) t.setAttribute('data-ntf-alert', '1');
    t.style.cssText = 'max-width:360px;background:#232429;color:#fff;padding:10px 14px;'
      + 'border-radius:10px;font-size:13px;line-height:1.5;pointer-events:auto;'
      + 'box-shadow:0 6px 18px rgba(0,0,0,.28);border-left:3px solid '
      + (SEV_COLOR[o.severity] || 'var(--tk-brand,#1e8cf2)') + ';';
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:flex-start;gap:10px;';
    var msg = document.createElement('span');
    msg.style.cssText = 'flex:1 1 auto;';
    msg.textContent = String(o.text || '');
    if(typeof o.onClick === 'function'){
      /* 整卡可点（汇总卡「有 N 项需要注意 ›」→ 通知中心） */
      msg.style.cursor = 'pointer';
      msg.setAttribute('role', 'button');
      msg.tabIndex = 0;
      msg.addEventListener('click', function(){ _close(); try{ o.onClick(); }catch(_e){} });
      msg.addEventListener('keydown', function(e){
        if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); _close(); try{ o.onClick(); }catch(_e){} }
      });
    }
    row.appendChild(msg);
    var cnt = document.createElement('span');
    cnt.setAttribute('data-ntf-agg', '1');
    cnt.style.cssText = 'flex:0 0 auto;background:rgba(255,255,255,.18);'
      + 'border-radius:9px;padding:0 7px;font-size:11.5px;font-weight:700;line-height:1.6;';
    function _paintCount(n){
      cnt.textContent = '\u00d7' + n;
      cnt.style.display = (n > 1 && !o._summary) ? '' : 'none';
      t.setAttribute('data-ntf-count', String(n));
    }
    _paintCount(aggCount);
    row.appendChild(cnt);
    var x = document.createElement('button');
    x.type = 'button';
    x.textContent = '\u2715';
    x.setAttribute('aria-label', 'close');
    x.style.cssText = 'flex:0 0 auto;border:none;background:transparent;color:#fff;'
      + 'opacity:.65;cursor:pointer;font-size:12px;padding:0 2px;line-height:1.5;';
    x.addEventListener('click', function(){ _close(); });
    row.appendChild(x);
    t.appendChild(row);
    var acts = o.actions || [];
    var timer = null;
    function _close(){
      if(timer){ clearTimeout(timer); timer = null; }
      try{ t.remove(); }catch(_e){}
      try{ if(typeof o.onClose === 'function') o.onClose(); }catch(_e2){}
    }
    function _arm(){
      if(timer){ clearTimeout(timer); timer = null; }
      if(o.sticky) return;
      var ttl = (o.ttlMs && o.ttlMs > 0) ? o.ttlMs
        : (acts.length ? 15000 : (o.severity === 'error' ? 8000 : 6000));
      timer = setTimeout(_close, ttl);
    }
    /* #196 原地更新：同键再报只换文案 + 计数 + 重置停留（卡片 DOM 不动＝不重弹） */
    t.__ntfUpdate = function(text, n){
      msg.textContent = String(text || '');
      _paintCount(n);
      _arm();
    };
    t.__ntfClose = _close;
    if(acts.length){
      var bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;';
      for(var i = 0; i < acts.length; i++){
        (function(a){
          if(!a || !a.label) return;
          var b = document.createElement('button');
          b.type = 'button';
          b.textContent = String(a.label);
          if(a.title) b.title = String(a.title);
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
    _arm();
    return t;
  }

  function _toast(opts){
    if(!opts.text) return;
    var hasActs = !!(opts.actions && opts.actions.length);
    /* #196：告警级（warn/error 且进中心）一律走卡片路径——只有卡片路径能做同键原地
       更新与「同屏最多一张」折叠；info/success 短提示仍交 CRMW 轻 toast。 */
    if(!hasActs && !opts.sticky && !opts.domKey && !_isAlertCard(opts)
       && window.CRMW && window.CRMW.toast){
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
    /* #196 分级：基础设施（GPU/Embedding/官网连通）/ 质量黄灯 默认只进通知中心不弹；
       客户在等 / 通道断线 照常弹。forceToast=true 可覆写（调用方明确要打断时）。 */
    var lowTier = !!(opts.tier && LOW_TIERS[String(opts.tier)]) && !opts.forceToast;
    if(opts.ttlMs !== 0 && !lowTier){
      _toast({ text: opts.text, severity: opts.severity || 'info',
               actions: opts.actions, sticky: !!opts.sticky,
               domKey: opts.domKey, ttlMs: opts.ttlMs, center: opts.center });
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

  /* #196：宿主注入汇总卡文案（fn(n) → string；本总线不做 i18n） */
  function setSummaryText(fn){
    if(typeof fn === 'function') _summaryText = fn;
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

  /* ── 系统状态胶囊（横幅「常驻性」的新家）─────────────────────────────
     2026-08-28：胶囊此前只有文本 + 「点开通知中心」，**持续状态却没有持续动作**——
     配套卡片 20s 就自动消失，之后坐席只剩一句被截断的话（桌面壳还没有浏览器刷新
     按钮可用）。三处同批修：① 可选 action ＝ 右侧常驻按钮；② 文本 2 行 clamp 不再
     硬截断成半句；③ 结构 button 化（键盘可达 + 全文进 title/aria-label）。
     配色随主题令牌，fallback 保住经典后台壳（该壳无 --tk-*，维持中性深底）。 */
  var _ongoing = {};   /* id -> {text, tone, title, action:{label,onClick}} */
  var _box = null;
  var TONE_COLOR = { info:'var(--tk-brand,#1e8cf2)', warn:'#f59e0b', error:'#ef4444', success:'#22c55e' };

  function _ensureBox(){
    if(_box && _box.isConnected) return _box;
    _box = document.getElementById('ws-status-capsule');
    if(_box) return _box;
    var st = document.createElement('style');
    st.textContent = '#ws-status-capsule{position:fixed;right:16px;bottom:16px;z-index:9998;'
      + 'display:flex;flex-direction:column;gap:6px;align-items:flex-end;}'
      + '#ws-status-capsule .wsntf-chip{display:flex;align-items:center;gap:6px;max-width:360px;'
      + 'background:var(--tk-surface,rgba(15,23,42,.92));color:var(--tk-text,#e2e8f0);'
      + 'border:1px solid var(--tk-border,rgba(148,163,184,.25));'
      + 'border-radius:14px;padding:5px 8px 5px 12px;font-size:12px;line-height:1.4;'
      + 'box-shadow:0 4px 14px rgba(0,0,0,.25);user-select:none;}'
      + '#ws-status-capsule .wsntf-main{display:flex;align-items:center;gap:7px;flex:1 1 auto;'
      + 'min-width:0;border:none;background:transparent;color:inherit;font:inherit;'
      + 'text-align:left;cursor:pointer;padding:0;}'
      + '#ws-status-capsule .wsntf-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;}'
      /* 2 行封顶：短文案一行，长文案折行——绝不再把行动指引截成半句 */
      + '#ws-status-capsule .wsntf-txt{min-width:0;display:-webkit-box;-webkit-line-clamp:2;'
      + '-webkit-box-orient:vertical;overflow:hidden;}'
      /* .wsntf-act 刻意**不**限定在 #ws-status-capsule 内：消息中心「进行中」分组
         镜像同一个动作按钮，共用这一份规格（别再各写一套内联样式——那既是第二套
         尺寸，也会顶穿模板的内联颜色 ratchet）。 */
      + '.wsntf-act{flex:0 0 auto;border:1px solid var(--tk-brand,#1e8cf2);'
      + 'background:var(--tk-brand,#1e8cf2);color:#fff;border-radius:8px;padding:3px 10px;'
      + 'font:inherit;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;}'
      + '#ws-status-capsule button:focus-visible{outline:2px solid var(--tk-brand,#1e8cf2);'
      + 'outline-offset:2px;}'
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

  var LS_ONG_SNOOZE = 'aitr.ntf.ong.snooze.';

  function _ongSnoozed(id){
    try{
      return (parseInt(_lsGet(LS_ONG_SNOOZE + id) || '0', 10) || 0) > Date.now();
    }catch(_e){ return false; }
  }

  function _chip(id, o){
    var chip = document.createElement('div');
    chip.className = 'wsntf-chip';
    chip.setAttribute('data-ntf-id', id);
    /* 全文进 title + aria-label：胶囊窄，长文案会被 clamp，悬停与读屏都必须拿到整句 */
    var full = String(o.text || '') + (o.title ? (' \u2014 ' + o.title) : '');
    var main = document.createElement('button');
    main.type = 'button';
    main.className = 'wsntf-main';
    main.title = full;
    main.setAttribute('aria-label', full);
    var dot = document.createElement('span');
    dot.className = 'wsntf-dot';
    dot.style.background = TONE_COLOR[o.tone] || TONE_COLOR.info;
    var txt = document.createElement('span');
    txt.className = 'wsntf-txt';
    txt.textContent = o.text;
    main.appendChild(dot);
    main.appendChild(txt);
    main.addEventListener('click', _openCenter);
    chip.appendChild(main);
    var a = o.action;
    if(a && a.label){
      var act = document.createElement('button');
      act.type = 'button';
      act.className = 'wsntf-act';
      act.textContent = String(a.label);
      if(a.title) act.title = String(a.title);
      act.addEventListener('click', function(ev){
        try{ ev.stopPropagation(); }catch(_e){}
        try{ if(typeof a.onClick === 'function') a.onClick(); }catch(_e2){}
      });
      chip.appendChild(act);
    }
    /* #53-①：dismissMs > 0 → 胶囊带 ✕，点了本 id 静默该时长（localStorage 跨
       标签页）。信息不丢：消息中心留痕仍在，静默到期自动恢复。
       #143：onDismiss=函数 → 同样带 ✕，静默记账交调用方（总线不落 LS）。 */
    if(o.dismissMs > 0 || o.onDismiss){
      var x = document.createElement('button');
      x.type = 'button';
      x.textContent = '\u2715';
      x.setAttribute('aria-label', o.dismissTitle || 'dismiss');
      if(o.dismissTitle) x.title = o.dismissTitle;
      x.style.cssText = 'flex:0 0 auto;border:none;background:transparent;'
        + 'color:inherit;opacity:.55;cursor:pointer;font-size:11px;padding:0 2px;';
      x.addEventListener('click', function(ev){
        try{ ev.stopPropagation(); }catch(_e){}
        if(o.dismissMs > 0) _lsSet(LS_ONG_SNOOZE + id, String(Date.now() + o.dismissMs));
        try{ if(typeof o.onDismiss === 'function') o.onDismiss(); }catch(_e2){}
        delete _ongoing[String(id)];
        _render();
      });
      chip.appendChild(x);
    }
    return chip;
  }

  function _render(){
    var box = _ensureBox();
    var ids = Object.keys(_ongoing);
    while(box.firstChild) box.removeChild(box.firstChild);
    for(var i = 0; i < ids.length; i++){
      box.appendChild(_chip(ids[i], _ongoing[ids[i]]));
    }
    box.style.display = ids.length ? 'flex' : 'none';
    _syncToastOffset();
    /* 消息中心「进行中」分组等消费方按此事件重绘 */
    try{ document.dispatchEvent(new CustomEvent('aitr:ntf-ongoing')); }catch(_e){}
  }

  window.AITRNotify = {
    notify: notify,
    dismiss: dismiss,
    setSummaryText: setSummaryText,
    alert: uxAlert,
    confirm: uxConfirm,
    ongoing: {
      /* o.action = {label, onClick, title}：胶囊右侧常驻按钮。持续状态必须持续可
         执行——只给文本会让坐席在卡片过期后无路可走（2026-08-28 「刷新页面」事故）。
         o.dismissMs（#53）：>0 = 可关闭（✕ 静默该毫秒数）；静默期内 set 直接忽略。 */
      set: function(id, o){
        if(!id) return;
        o = o || {};
        if(o.dismissMs > 0 && _ongSnoozed(String(id))) return;
        var a = o.action;
        _ongoing[String(id)] = {
          text: String(o.text || ''),
          tone: String(o.tone || 'info'),
          title: String(o.title || ''),
          dismissMs: Math.max(0, Number(o.dismissMs) || 0),
          dismissTitle: String(o.dismissTitle || ''),
          onDismiss: (typeof o.onDismiss === 'function') ? o.onDismiss : null,
          action: (a && a.label && typeof a.onClick === 'function')
            ? { label: String(a.label), onClick: a.onClick, title: String(a.title || '') }
            : null
        };
        _render();
      },
      clear: function(id){
        if(_ongoing[String(id)]){ delete _ongoing[String(id)]; _render(); }
      },
      active: function(){ return Object.keys(_ongoing); },
      /* 消息中心「进行中」分组用：只出可安全进 HTML 的字段，动作经 invoke(id) 触发
         （中心那侧是 innerHTML 拼接，拿不到函数引用）。 */
      snapshot: function(){
        var out = [];
        for(var id in _ongoing){
          var o = _ongoing[id];
          out.push({ id: id, text: o.text, tone: o.tone, title: o.title,
                     actionLabel: (o.action ? o.action.label : '') });
        }
        return out;
      },
      invoke: function(id){
        var o = _ongoing[String(id)];
        if(!o || !o.action) return false;
        try{ o.action.onClick(); return true; }catch(_e){ return false; }
      }
    }
  };
})();
