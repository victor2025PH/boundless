/* ============================================================================
 * 坐席新手任务卡（WP-7 前端半件，2026-08-18）——独立悬浮组件，零页面耦合。
 *
 * 设计（对齐 docs/实施41 交接契约，落点刻意偏离规格的「侧栏内」）：
 *  - 侧栏正被 sidebar-dock 线连续重构（连续两日 ACTIVE），把卡长进那里＝
 *    持续互踩。改为 fixed 悬浮卡（本文件自包含）+ workspace_base 一行
 *    include——sibling 怎么重构侧栏都不受影响，且全工作台页可见（不只收件箱）。
 *  - 完成判定＝fetch 旁路监听 5 个**权威 API**（比 hook 页面函数名稳：DOM/函数
 *    随时被重构，API 路径是跨线契约）。不 clone 响应、不读响应体，原 promise
 *    原样返回；卡片不激活（flag 关 404 / 老坐席 / 已收起 / 全✓）＝不装补丁，
 *    全✓当场卸载补丁——常态零开销。
 *  - i18n 走 window.T（键 agt_*，pack=i18n_packs/agent_tasks_card.py，无中文兜底）；
 *    埋点走既有 ui-event beacon（action 前缀 agt_，进 ui-event-trend 复盘完成率）。
 *  - 主题用 --tk-* 工作台壳 token（admin-theme 纪律：文字不用 opacity 调灰，
 *    说明字号 ≥.75rem）。
 * ========================================================================== */
(function () {
  'use strict';
  if (window.__agtCardBooted) return;
  window.__agtCardBooted = true;

  var API = '/api/agent-tasks';
  var LS_COLLAPSE = 'agt.card.collapsed';
  var TASKS = ['view_conversation', 'approve_draft', 'edit_send',
               'send_voice', 'create_goal'];
  var _state = null;          // 最近一次服务端状态
  var _origFetch = null;      // 补丁卸载用
  var _pendingDone = {};      // 去抖：同任务 complete 在途防重

  function T(k) { return (window.T ? window.T(k) : k); }

  function beacon(action) {
    try {
      if (!navigator.sendBeacon) return;
      var body = JSON.stringify({ page: location.pathname,
                                  action: 'agt_' + String(action || '') });
      navigator.sendBeacon('/api/telemetry/ui-event',
                           new Blob([body], { type: 'application/json' }));
    } catch (_e) {}
  }

  function api(path, init) {
    var f = window.apiFetch || window.fetch;
    return f(path, init, { timeoutMs: 10000 });
  }

  /* ── 完成判定：请求特征 → 任务 id（纯函数，便于人查表）──
     GET  /api/unified-inbox/thread?…          → view_conversation
     POST /api/drafts/{id}/resolve action=approve   → approve_draft
     POST /api/drafts/{id}/resolve action=edit_send → edit_send
     POST /api/unified-inbox/send-voice        → send_voice（精确段，防 send-voice-status 误中）
     POST /api/goals                           → create_goal（精确，防 /api/goals/… 子路径误中） */
  function matchTask(url, init) {
    try {
      var u = String(url || '');
      if (u.indexOf('://') >= 0) {
        if (u.indexOf(location.origin + '/') !== 0) return '';
        u = u.slice(location.origin.length);
      }
      u = u.split('#')[0];
      var path = u.split('?')[0];
      var method = String((init && init.method) || 'GET').toUpperCase();
      if (method === 'GET' && path === '/api/unified-inbox/thread') {
        return 'view_conversation';
      }
      if (method !== 'POST') return '';
      if (path === '/api/unified-inbox/send-voice') return 'send_voice';
      if (path === '/api/goals') return 'create_goal';
      if (/^\/api\/drafts\/[^/]+\/resolve$/.test(path)) {
        var action = '';
        try {
          var body = init && init.body;
          if (typeof body === 'string') action = String((JSON.parse(body) || {}).action || '');
        } catch (_e) { action = ''; }
        if (action === 'approve') return 'approve_draft';
        if (action === 'edit_send') return 'edit_send';
      }
      return '';
    } catch (_e) { return ''; }
  }
  window.__agtMatchTask = matchTask;   // 供门禁/控制台自检

  function needAny() {
    if (!_state || !_state.tasks) return false;
    for (var i = 0; i < _state.tasks.length; i++) {
      if (!_state.tasks[i].done_ts) return true;
    }
    return false;
  }

  function installPatch() {
    if (_origFetch || typeof window.fetch !== 'function') return;
    _origFetch = window.fetch;
    window.fetch = function (input, init) {
      var p = _origFetch.apply(this, arguments);
      try {
        var tid = matchTask(
          (input && input.url) ? input.url : input, init);
        if (tid && _state && !_pendingDone[tid]) {
          var row = null;
          for (var i = 0; i < (_state.tasks || []).length; i++) {
            if (_state.tasks[i].id === tid) { row = _state.tasks[i]; break; }
          }
          if (row && !row.done_ts) {
            // 旁路链：绝不影响原请求消费方（不 clone、不读 body）
            p.then(function (res) {
              if (res && res.ok) complete(tid);
            }).catch(function () {});
          }
        }
      } catch (_e) {}
      return p;
    };
  }

  function uninstallPatch() {
    if (_origFetch) { window.fetch = _origFetch; _origFetch = null; }
  }

  function complete(tid) {
    if (_pendingDone[tid]) return;
    _pendingDone[tid] = true;
    api(API + '/complete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: tid })
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        delete _pendingDone[tid];
        if (!d) return;
        _state = d;
        beacon('done_' + tid);
        render();
        if (d.all_done) {
          beacon('all_done');
          uninstallPatch();
          var el = document.getElementById('agt-card');
          if (el) setTimeout(function () { el.remove(); }, 2600);
        }
      })
      .catch(function () { delete _pendingDone[tid]; });
  }

  function dismiss() {
    beacon('dismissed');
    api(API + '/dismiss', { method: 'POST' }).catch(function () {});
    uninstallPatch();
    var el = document.getElementById('agt-card');
    if (el) el.remove();
  }

  function setCollapsed(v) {
    try { localStorage.setItem(LS_COLLAPSE, v ? '1' : ''); } catch (_e) {}
    render();
  }
  function isCollapsed() {
    try { return localStorage.getItem(LS_COLLAPSE) === '1'; } catch (_e) { return false; }
  }

  function ensureHost() {
    var el = document.getElementById('agt-card');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'agt-card';
    el.style.cssText =
      'position:fixed;right:16px;bottom:16px;z-index:8800;max-width:270px;' +
      'font-size:.8rem;color:var(--tk-text,#e6e8ee);' +
      'background:var(--tk-surface,#171a22);border:1px solid var(--tk-border,#2a2f3a);' +
      'border-radius:var(--tk-radius-lg,14px);box-shadow:var(--tk-shadow-md,0 8px 24px rgba(0,0,0,.35));';
    (document.body || document.documentElement).appendChild(el);
    return el;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function render() {
    if (!_state) return;
    var el = ensureHost();
    var done = _state.done_count || 0;
    if (_state.all_done) {
      el.innerHTML = '<div style="padding:.7rem .85rem;display:flex;gap:.45rem;align-items:center">'
        + '<span style="font-size:1rem">🎉</span>'
        + '<span>' + esc(T('agt_all_done')) + '</span></div>';
      return;
    }
    if (isCollapsed()) {
      el.innerHTML = '<button type="button" id="agt-pill" style="all:unset;cursor:pointer;'
        + 'display:flex;gap:.4rem;align-items:center;padding:.5rem .8rem;font-size:.78rem;'
        + 'color:var(--tk-text,#e6e8ee)">🎓 <b>' + done + '/' + TASKS.length + '</b> '
        + esc(T('agt_pill')) + '</button>';
      var pill = el.querySelector('#agt-pill');
      if (pill) pill.addEventListener('click', function () { setCollapsed(false); beacon('expand'); });
      return;
    }
    var rows = '';
    for (var i = 0; i < _state.tasks.length; i++) {
      var t = _state.tasks[i];
      var ok = !!t.done_ts;
      rows += '<div style="display:flex;gap:.45rem;align-items:center;padding:.24rem 0">'
        + '<span style="width:1rem;text-align:center;flex:none;'
        + (ok ? 'color:var(--tk-ok-ink,#34d399);font-weight:700">✓' : 'color:var(--tk-text-muted,#98a1b3)">○')
        + '</span><span style="' + (ok ? 'color:var(--tk-text-muted,#98a1b3)' : '') + '">'
        + esc(T('agt_task_' + t.id)) + '</span></div>';
    }
    el.innerHTML =
      '<div style="padding:.65rem .85rem .7rem">'
      + '<div style="display:flex;align-items:center;gap:.45rem;margin-bottom:.35rem">'
      + '<span>🎓</span><b style="flex:1;font-size:.8rem">' + esc(T('agt_title')) + '</b>'
      + '<span style="color:var(--tk-text-muted,#98a1b3);font-size:.75rem">' + done + '/' + TASKS.length + '</span>'
      + '<button type="button" id="agt-min" title="' + esc(T('agt_collapse')) + '" style="all:unset;cursor:pointer;'
      + 'padding:.1rem .3rem;color:var(--tk-text-muted,#98a1b3)">—</button>'
      + '</div>'
      + '<div style="height:5px;border-radius:4px;background:var(--tk-border,#2a2f3a);overflow:hidden;margin-bottom:.4rem">'
      + '<div style="height:100%;width:' + Math.round(done * 100 / TASKS.length) + '%;'
      + 'background:var(--tk-brand,#5b7cf6);border-radius:4px"></div></div>'
      + rows
      + '<div style="display:flex;justify-content:flex-end;margin-top:.4rem">'
      + '<button type="button" id="agt-dismiss" style="all:unset;cursor:pointer;font-size:.75rem;'
      + 'color:var(--tk-text-muted,#98a1b3);padding:.15rem .3rem">' + esc(T('agt_dismiss')) + '</button>'
      + '</div></div>';
    var mb = el.querySelector('#agt-min');
    if (mb) mb.addEventListener('click', function () { setCollapsed(true); beacon('collapse'); });
    var db = el.querySelector('#agt-dismiss');
    if (db) db.addEventListener('click', dismiss);
  }

  function boot() {
    api(API).then(function (r) {
      if (!r.ok) return null;               // 404=flag 关：整组件静默不渲染
      return r.json();
    }).then(function (d) {
      if (!d || !d.show) return;            // 老坐席/已收起/全✓：不渲染不打补丁
      _state = d;
      render();
      beacon('shown');
      if (needAny()) installPatch();
    }).catch(function () {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
