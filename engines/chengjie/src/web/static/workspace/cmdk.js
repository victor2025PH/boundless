/* 坐席工作台 ⌘K 命令面板（三期 + 四期全局搜索）
 *
 * 全 workspace 壳层可用（workspace_base.html 引入）。四类条目：
 *   1. 页面导航 —— 从顶栏主导航 + 「更多」菜单收割可见链接（角色过滤天然正确）
 *   2. 会话跳转 —— 收件箱页经 window.WS_STATE 只读桥搜索本地会话，
 *      经 window.__wsFocusConv 打开；其它工作台页写 sessionStorage 后跳 /workspace
 *   3. 动作     —— 仅当对应 window.* 函数存在时出现（收件箱页最全）
 *   4. 全局搜索 —— q≥2 时异步打 /api/workspace/search（消息/联系人/注解），
 *      generation token 防乱序；不阻塞本地 filter 同步渲染
 *
 * 设计约束：
 *   - 纯自包含 ES 模块雏形（无 import；样式自注入；只依赖 window.T/Tf 与既有导出）
 *   - i18n：所有文案走 window.T('ws.cmdk.*')，无中文字面量（过 CJK 门禁）
 *   - Ctrl/Cmd+K 打开（收件箱知识库已改绑 Ctrl+Shift+K）；Esc 关闭并归还焦点
 */
(function () {
  'use strict';
  if (window.__wsCmdk) return;
  window.__wsCmdk = true;

  var T = function (k) { return (window.T ? window.T(k) : k); };

  var CSS = [
    '.cmdk-overlay{position:fixed;inset:0;z-index:4000;background:rgba(0,0,0,.45);display:none;align-items:flex-start;justify-content:center;padding-top:12vh;-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);}',
    '.cmdk-overlay.open{display:flex;}',
    '.cmdk-box{width:min(620px,92vw);background:var(--tk-surface,#fff);border:1px solid var(--tk-border,#e5e6ea);border-radius:14px;box-shadow:var(--tk-shadow-md,0 10px 32px rgba(0,0,0,.3));overflow:hidden;}',
    '.cmdk-input{width:100%;border:0;outline:none;background:transparent;color:var(--tk-text,#111);font-size:15px;padding:14px 16px;border-bottom:1px solid var(--tk-border,#e5e6ea);font-family:inherit;}',
    '.cmdk-list{max-height:min(46vh,420px);overflow-y:auto;padding:6px;}',
    '.cmdk-grp{font-size:11px;font-weight:700;color:var(--tk-text-muted,#888);letter-spacing:.4px;padding:8px 10px 3px;}',
    '.cmdk-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;cursor:pointer;color:var(--tk-text,#111);font-size:13px;}',
    '.cmdk-item .ck-ic{width:18px;text-align:center;flex-shrink:0;opacity:.75;}',
    '.cmdk-item .ck-lbl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.cmdk-item .ck-sub{font-size:11px;color:var(--tk-text-muted,#888);flex-shrink:0;max-width:38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.cmdk-item .ck-sub em,.cmdk-item .ck-lbl em{background:color-mix(in srgb,var(--tk-warn,#f59e0b) 28%,transparent);color:inherit;font-style:normal;border-radius:2px;padding:0 1px;}',
    '.cmdk-item.active{background:color-mix(in srgb,var(--tk-brand,#2563eb) 12%,transparent);}',
    '.cmdk-item.active .ck-lbl{color:var(--tk-brand,#2563eb);font-weight:600;}',
    '.cmdk-empty{padding:18px 12px;text-align:center;color:var(--tk-text-muted,#888);font-size:13px;}',
    '.cmdk-hint{padding:6px 10px;font-size:11px;color:var(--tk-text-muted,#888);}',
    '.cmdk-foot{display:flex;gap:12px;padding:7px 12px;border-top:1px solid var(--tk-border,#e5e6ea);color:var(--tk-text-muted,#888);font-size:11px;}',
    '.cmdk-foot kbd{background:color-mix(in srgb,var(--tk-text,#000) 9%,transparent);border-radius:4px;padding:0 5px;font-family:inherit;}'
  ].join('\n');

  function injectStyle() {
    var s = document.createElement('style');
    s.id = 'cmdk-style';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function buildDom() {
    var ov = document.createElement('div');
    ov.className = 'cmdk-overlay';
    ov.id = 'cmdk-overlay';
    var box = document.createElement('div');
    box.className = 'cmdk-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', T('ws.cmdk.placeholder'));
    var input = document.createElement('input');
    input.className = 'cmdk-input';
    input.id = 'cmdk-input';
    input.type = 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = T('ws.cmdk.placeholder');
    var list = document.createElement('div');
    list.className = 'cmdk-list';
    list.id = 'cmdk-list';
    list.setAttribute('role', 'listbox');
    var foot = document.createElement('div');
    foot.className = 'cmdk-foot';
    foot.innerHTML = '<span><kbd>\u2191\u2193</kbd> ' + esc(T('ws.cmdk.hint_nav')) + '</span>'
      + '<span><kbd>Enter</kbd> ' + esc(T('ws.cmdk.hint_run')) + '</span>'
      + '<span><kbd>Esc</kbd> ' + esc(T('ws.cmdk.hint_close')) + '</span>';
    box.appendChild(input); box.appendChild(list); box.appendChild(foot);
    ov.appendChild(box);
    document.body.appendChild(ov);
    return { ov: ov, input: input, list: list };
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  /* 先 esc 再高亮，避免 XSS；q 为空则原样返回已转义串 */
  function hl(escaped, q) {
    var raw = String(q == null ? '' : q).trim();
    if (!raw || !escaped) return escaped;
    var safe = raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return escaped.replace(new RegExp('(' + safe + ')', 'gi'), '<em>$1</em>');
    } catch (_) {
      return escaped;
    }
  }

  /* ── 数据源 ─────────────────────────────────────────── */

  function harvestPages() {
    var out = [], seen = {};
    var links = document.querySelectorAll('.ws-top a[href], #ws-more-menu a[href]');
    links.forEach(function (a) {
      var url = a.getAttribute('href') || '';
      if (!url || url.charAt(0) === '#') return;
      // 跳过被角色隐藏的项（主管菜单坐席态 display:none）
      var el = a, hidden = false;
      while (el && el !== document.body) {
        if (el.style && el.style.display === 'none') { hidden = true; break; }
        el = el.parentElement;
      }
      if (hidden) return;
      var label = (a.textContent || '').replace(/\s+/g, ' ').trim();
      if (!label || seen[url]) return;
      seen[url] = 1;
      out.push({ kind: 'page', label: label, url: url, ic: '\u2192' });
    });
    return out;
  }

  var ACTION_DEFS = [
    { fn: 'openOldestWaiting', key: 'ws.cmdk.act_oldest', ic: '\u23f1' },
    { fn: 'toggleArchive', key: 'ws.cmdk.act_archive', ic: '\ud83d\udce6', needConv: true },
    { fn: 'snoozeConv', key: 'ws.cmdk.act_snooze', ic: '\ud83c\udf19', needConv: true },
    { fn: 'toggleKbMode', key: 'ws.cmdk.act_kb', ic: '\ud83d\udcda' },
    { fn: 'openDrawer', key: 'ws.cmdk.act_connect', ic: '\ud83d\udd17' },
    { fn: 'toggleBatchMode', key: 'ws.cmdk.act_batch', ic: '\u2611' },
    { fn: 'toggleContactsPanel', key: 'ws.cmdk.act_contacts', ic: '\ud83d\udc65' },
    { fn: 'setFilter', arg: 'waiting', key: 'ws.cmdk.act_filter_waiting', ic: '\u23f3' },
    { fn: 'setFilter', arg: 'sla', key: 'ws.cmdk.act_filter_sla', ic: '\ud83d\udea8' },
    { fn: 'setFilter', arg: 'unread', key: 'ws.cmdk.act_filter_unread', ic: '\u25cf' },
    { fn: 'resetAllFilters', key: 'ws.cmdk.act_filter_all', ic: '\u27f2' }
  ];

  function harvestActions() {
    var out = [];
    var hasConv = !!(window.WS_STATE && window.WS_STATE.getSelectedKey && window.WS_STATE.getSelectedKey());
    ACTION_DEFS.forEach(function (d) {
      if (typeof window[d.fn] !== 'function') return;
      if (d.needConv && !hasConv) return;
      out.push({ kind: 'action', label: T(d.key), ic: d.ic, run: function () {
        try { d.arg !== undefined ? window[d.fn](d.arg) : window[d.fn](); } catch (_) {}
      } });
    });
    return out;
  }

  function _convItem(c) {
    return {
      kind: 'conv',
      label: String(c.name || c.chat_key || ''),
      ic: '\ud83d\udcac',
      sub: String(c.last_text || '').slice(0, 40),
      cid: c.conversation_id || (window.WS_STATE.convKey ? window.WS_STATE.convKey(c) : '')
    };
  }

  function harvestConvs(q) {
    if (!window.WS_STATE || !window.WS_STATE.getChats) return [];
    var chats = window.WS_STATE.getChats();
    if (!q) return [];
    var ql = q.toLowerCase(), out = [];
    for (var i = 0; i < chats.length && out.length < 6; i++) {
      var nm = String(chats[i].name || chats[i].chat_key || '');
      if (nm.toLowerCase().indexOf(ql) < 0) continue;
      out.push(_convItem(chats[i]));
    }
    return out;
  }

  /* 七期：空查询时给「最近会话」快速跳转（列表本身即按最近排序） */
  function harvestRecent() {
    if (!window.WS_STATE || !window.WS_STATE.getChats) return [];
    var chats = window.WS_STATE.getChats();
    var out = [];
    for (var i = 0; i < chats.length && out.length < 5; i++) {
      out.push(_convItem(chats[i]));
    }
    return out;
  }

  /* ── 九期：带参命令 ──────────────────────────────────
   * `@xxx` 转给坐席（当前会话，POST batch/assign）；`#xxx` 按标签筛选（setTagFilter）。
   * 坐席名单取 /api/workspace/presence，30s 缓存；均仅在收件箱能力可用时出现。 */
  var agentsCache = { list: null, ts: 0, loading: false };

  function toast(msg, kind) {
    try {
      if (window.CRMW && window.CRMW.toast) { window.CRMW.toast(msg, kind || 'info'); return; }
      if (typeof window._toast === 'function') { window._toast(msg); return; }
    } catch (_) {}
  }

  function loadAgents() {
    if (agentsCache.loading) return;
    if (agentsCache.list && (Date.now() - agentsCache.ts) < 30000) return;
    agentsCache.loading = true;
    fetch('/api/workspace/presence', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        agentsCache.list = (d && d.ok && Array.isArray(d.agents)) ? d.agents : [];
        agentsCache.ts = Date.now();
        agentsCache.loading = false;
        if (dom && state.open && dom.input.value.charAt(0) === '@') render(dom.input.value, true);
      })
      .catch(function () { agentsCache.loading = false; agentsCache.list = agentsCache.list || []; });
  }

  function harvestAgents(q) {
    var cid = window.WS_STATE && window.WS_STATE.getSelectedCid && window.WS_STATE.getSelectedCid();
    if (!cid) return [{ kind: 'hint', label: T('ws.cmdk.need_conv'), ic: '\u26a0', disabled: true }];
    if (!agentsCache.list) {
      loadAgents();
      return [{ kind: 'hint', label: T('ws.cmdk.searching'), ic: '\u2026', disabled: true }];
    }
    var ql = q.toLowerCase(), out = [];
    agentsCache.list.forEach(function (a) {
      var name = String(a.display_name || a.agent_id || '');
      if (!name) return;
      if (ql && name.toLowerCase().indexOf(ql) < 0
          && String(a.agent_id || '').toLowerCase().indexOf(ql) < 0) return;
      out.push({
        kind: 'assign', label: name, ic: '\ud83e\udd1d',
        sub: String(a.status || ''), agentId: String(a.agent_id || name), cid: cid
      });
    });
    if (!out.length) out.push({ kind: 'hint', label: T('ws.cmdk.no_agents'), ic: '\u2014', disabled: true });
    return out.slice(0, 8);
  }

  function harvestTags(q) {
    if (!window.WS_STATE || !window.WS_STATE.getTagLibrary) return [];
    var tags = window.WS_STATE.getTagLibrary();
    var ql = q.toLowerCase(), out = [];
    tags.forEach(function (t) {
      if (ql && String(t).toLowerCase().indexOf(ql) < 0) return;
      out.push({ kind: 'tagf', label: String(t), ic: '\ud83c\udff7', tag: String(t) });
    });
    if (!out.length) out.push({ kind: 'hint', label: T('ws.cmdk.no_match'), ic: '\u2014', disabled: true });
    return out.slice(0, 10);
  }

  function runAssign(it) {
    fetch('/api/workspace/batch/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ conversation_ids: [it.cid], agent_id: it.agentId })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          var m = T('ws.cmdk.assign_ok');
          toast(m.split('{name}').join(it.label), 'ok');
          // 十期闭环：即时刷新列表（assignment/处理人徽章），不等下一轮轮询
          try { if (typeof window.loadChats === 'function') window.loadChats(); } catch (_) {}
        } else {
          toast(T('ws.cmdk.assign_fail'), 'err');
        }
      })
      .catch(function () { toast(T('ws.cmdk.assign_fail'), 'err'); });
  }

  /* ── 过滤 + 渲染 ────────────────────────────────────── */

  var state = {
    items: [], idx: 0, open: false, lastFocus: null,
    remote: [], remoteQ: '', remoteLoading: false, searchGen: 0, searchTimer: null
  };
  var dom = null;

  function filterItems(q) {
    var raw = q.trim();
    // 九期：带参命令模式（@坐席 / #标签），独占面板不混入其它组
    if (raw.charAt(0) === '@') {
      return [{ title: T('ws.cmdk.assign_mode'), items: harvestAgents(raw.slice(1).trim()) }];
    }
    // 十期：> 页面直达（纯本地，只看导航项）
    if (raw.charAt(0) === '>') {
      var pq = raw.slice(1).trim().toLowerCase();
      var pgOnly = harvestPages().filter(function (it) {
        return !pq || it.label.toLowerCase().indexOf(pq) >= 0;
      }).slice(0, 10);
      if (!pgOnly.length) {
        pgOnly = [{ kind: 'hint', label: T('ws.cmdk.no_match'), ic: '\u2014', disabled: true }];
      }
      return [{ title: T('ws.cmdk.pages'), items: pgOnly }];
    }
    if (raw.charAt(0) === '#') {
      if (typeof window.setTagFilter !== 'function') {
        return [{ title: T('ws.cmdk.tag_mode'), items: [{
          kind: 'hint', label: T('ws.cmdk.need_inbox'), ic: '\u26a0', disabled: true
        }] }];
      }
      return [{ title: T('ws.cmdk.tag_mode'), items: harvestTags(raw.slice(1).trim()) }];
    }
    var ql = raw.toLowerCase();
    var pages = harvestPages(), actions = harvestActions();
    var match = function (it) { return !ql || it.label.toLowerCase().indexOf(ql) >= 0; };
    var groups = [];
    if (!ql) {
      // 七期：空查询 = 快速跳转最近会话（比先打字再选更快一步）
      var recent = harvestRecent();
      if (recent.length) groups.push({ title: T('ws.cmdk.recent'), items: recent });
    } else {
      var convs = harvestConvs(ql);
      if (convs.length) groups.push({ title: T('ws.cmdk.convs'), items: convs });
    }

    // 四/七期：全局搜索（q≥2 恒占位防「搜了没反馈」）；消息命中优先，联系人独立组
    if (ql.length >= 2) {
      if (state.remoteLoading || state.remoteQ !== ql) {
        groups.push({ title: T('ws.cmdk.search'), items: [{
          kind: 'hint', label: T('ws.cmdk.searching'), ic: '\u2026', disabled: true
        }] });
      } else if (state.remote.length) {
        var msgs = [], contacts = [];
        state.remote.forEach(function (it) {
          (it.kind === 'contact' ? contacts : msgs).push(it);
        });
        if (msgs.length) groups.push({ title: T('ws.cmdk.search'), items: msgs.slice(0, 6) });
        if (contacts.length) groups.push({ title: T('ws.cmdk.contacts'), items: contacts.slice(0, 4) });
      } else {
        groups.push({ title: T('ws.cmdk.search'), items: [{
          kind: 'hint', label: T('ws.cmdk.search_empty'), ic: '\u2014', disabled: true
        }] });
      }
    }

    var acts = actions.filter(match).slice(0, 7);
    // 九期：带参命令入口（选中即切换到 @/# 模式，面板保持打开）
    var hasConv = !!(window.WS_STATE && window.WS_STATE.getSelectedCid && window.WS_STATE.getSelectedCid());
    if (hasConv) {
      var aLbl = T('ws.cmdk.act_assign');
      if (match({ label: aLbl })) acts.push({ kind: 'prefix', prefix: '@', label: aLbl, ic: '\ud83e\udd1d' });
    }
    if (typeof window.setTagFilter === 'function') {
      var tLbl = T('ws.cmdk.act_tagfilter');
      if (match({ label: tLbl })) acts.push({ kind: 'prefix', prefix: '#', label: tLbl, ic: '\ud83c\udff7' });
    }
    var pLbl = T('ws.cmdk.act_pages');
    if (match({ label: pLbl })) acts.push({ kind: 'prefix', prefix: '>', label: pLbl, ic: '\u2192' });
    if (acts.length) groups.push({ title: T('ws.cmdk.actions'), items: acts });
    var pgs = pages.filter(match).slice(0, 7);
    if (pgs.length) groups.push({ title: T('ws.cmdk.pages'), items: pgs });
    return groups;
  }

  function mapRemoteHit(r) {
    var typ = String(r.type || '');
    var ic = typ === 'contact' ? '\ud83d\udc64' : (typ === 'note' ? '\ud83d\udcdd' : '\ud83d\udcac');
    var kind = typ === 'contact' ? 'contact' : 'conv';
    var item = {
      kind: kind,
      label: String(r.title || ''),
      ic: ic,
      sub: String(r.preview || '').slice(0, 48)
    };
    if (kind === 'contact') {
      item.url = r.url || ('/workspace/contact/' + encodeURIComponent(r.contact_id || ''));
    } else {
      item.cid = r.conversation_id || '';
      item.mid = r.message_id || '';
      if (!item.cid && r.url) item.url = r.url;
      else if (item.cid && item.mid) {
        item.url = '/workspace?conv=' + encodeURIComponent(item.cid)
          + '&mid=' + encodeURIComponent(item.mid);
      }
    }
    return item;
  }

  function scheduleRemoteSearch(q) {
    var ql = q.trim().toLowerCase();
    if (state.searchTimer) { clearTimeout(state.searchTimer); state.searchTimer = null; }
    // 带参命令模式（@/#/>）不打全局搜索
    if (ql.charAt(0) === '@' || ql.charAt(0) === '#' || ql.charAt(0) === '>') {
      state.remote = []; state.remoteQ = ''; state.remoteLoading = false; state.searchGen += 1;
      return;
    }
    if (ql.length < 2) {
      state.remote = [];
      state.remoteQ = '';
      state.remoteLoading = false;
      state.searchGen += 1;
      return;
    }
    // 同 query 已有结果且不在加载 → 跳过
    if (state.remoteQ === ql && (state.remote.length || !state.remoteLoading)) return;

    state.remoteLoading = true;
    state.remoteQ = ql;
    var gen = ++state.searchGen;
    state.searchTimer = setTimeout(function () {
      state.searchTimer = null;
      fetch('/api/workspace/search?' + new URLSearchParams({
        q: ql, types: 'messages,contacts,notes', limit: '12'
      }), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (gen !== state.searchGen || !state.open) return;
          var rows = (d && d.ok && Array.isArray(d.results)) ? d.results : [];
          state.remote = rows.map(mapRemoteHit).filter(function (it) {
            return it.label || it.sub;
          });
          state.remoteLoading = false;
          state.remoteQ = ql;
          // 仅当输入框仍是同一 query 时重渲
          if (dom && dom.input && dom.input.value.trim().toLowerCase() === ql) {
            render(dom.input.value, true);
          }
        })
        .catch(function () {
          if (gen !== state.searchGen) return;
          state.remoteLoading = false;
          state.remote = [];
          if (dom && state.open) render(dom.input.value, true);
        });
    }, 220);
  }

  function render(q, keepIdx) {
    var groups = filterItems(q);
    var flat = [];
    var html = '';
    var ql = String(q || '').trim();
    groups.forEach(function (g) {
      html += '<div class="cmdk-grp">' + esc(g.title) + '</div>';
      g.items.forEach(function (it) {
        if (it.disabled) {
          html += '<div class="cmdk-hint">' + esc(it.label) + '</div>';
          return;
        }
        var i = flat.length;
        flat.push(it);
        html += '<div class="cmdk-item' + (i === state.idx ? ' active' : '') + '" role="option" data-i="' + i + '">'
          + '<span class="ck-ic">' + it.ic + '</span>'
          + '<span class="ck-lbl">' + hl(esc(it.label), ql) + '</span>'
          + (it.sub ? '<span class="ck-sub">' + hl(esc(it.sub), ql) + '</span>' : '')
          + '</div>';
      });
    });
    // 仅提示项（搜索中/无结果）也算有内容；勿用 empty 盖掉全局搜索分组
    if (!html) html = '<div class="cmdk-empty">' + esc(T('ws.cmdk.no_match')) + '</div>';
    state.items = flat;
    if (!keepIdx) state.idx = 0;
    if (state.idx >= flat.length) state.idx = Math.max(0, flat.length - 1);
    dom.list.innerHTML = html;
    var act = dom.list.querySelector('.cmdk-item.active');
    if (act) act.scrollIntoView({ block: 'nearest' });
  }

  function execIdx(i) {
    var it = state.items[i];
    if (!it || it.disabled) return;
    // 带参命令入口：不关面板，切到 @/# 模式继续输入
    if (it.kind === 'prefix') {
      dom.input.value = it.prefix;
      state.idx = 0;
      if (it.prefix === '@') loadAgents();
      render(dom.input.value);
      dom.input.focus();
      return;
    }
    close();
    if (it.kind === 'assign') { runAssign(it); return; }
    if (it.kind === 'tagf') {
      try { window.setTagFilter(it.tag); } catch (_) {}
      return;
    }
    if (it.kind === 'page' || it.kind === 'contact') {
      if (it.url) location.href = it.url;
      return;
    }
    if (it.kind === 'action') { it.run(); return; }
    if (it.kind === 'conv') {
      if (it.cid && typeof window.__wsFocusConv === 'function') {
        window.__wsFocusConv(it.cid, it.mid || undefined); return;
      }
      if (it.cid) {
        try {
          sessionStorage.setItem('ws_focus_conv', it.cid);
          if (it.mid) sessionStorage.setItem('ws_focus_mid', it.mid);
        } catch (_) {}
        location.href = it.url || ('/workspace?conv=' + encodeURIComponent(it.cid)
          + (it.mid ? ('&mid=' + encodeURIComponent(it.mid)) : ''));
        return;
      }
      if (it.url) location.href = it.url;
    }
  }

  function openPalette() {
    if (!dom) { injectStyle(); dom = buildDom(); wireDom(); }
    state.lastFocus = document.activeElement;
    state.idx = 0;
    state.open = true;
    state.remote = [];
    state.remoteQ = '';
    state.remoteLoading = false;
    state.searchGen += 1;
    if (state.searchTimer) { clearTimeout(state.searchTimer); state.searchTimer = null; }
    dom.ov.classList.add('open');
    dom.input.value = '';
    render('');
    dom.input.focus();
  }

  function close() {
    if (!dom) return;
    state.open = false;
    state.searchGen += 1;
    if (state.searchTimer) { clearTimeout(state.searchTimer); state.searchTimer = null; }
    dom.ov.classList.remove('open');
    // a11y：焦点归还给打开前的元素
    if (state.lastFocus && state.lastFocus.focus) { try { state.lastFocus.focus(); } catch (_) {} }
  }

  function wireDom() {
    dom.input.addEventListener('input', function () {
      state.idx = 0;
      var q = dom.input.value;
      scheduleRemoteSearch(q);
      render(q);
    });
    dom.input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); state.idx = Math.min(state.idx + 1, state.items.length - 1); render(dom.input.value, true); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); state.idx = Math.max(state.idx - 1, 0); render(dom.input.value, true); }
      else if (e.key === 'Enter') { e.preventDefault(); execIdx(state.idx); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    dom.list.addEventListener('click', function (e) {
      var it = e.target.closest ? e.target.closest('.cmdk-item') : null;
      if (it) execIdx(parseInt(it.dataset.i, 10));
    });
    dom.ov.addEventListener('mousedown', function (e) { if (e.target === dom.ov) close(); });
    // 面板打开时把焦点圈在输入框（简易 focus trap：Tab 不外逃）
    dom.ov.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') { e.preventDefault(); dom.input.focus(); }
    });
  }

  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      state.open ? close() : openPalette();
    } else if (e.key === 'Escape' && state.open) {
      close();
    }
  });

  window.wsOpenCmdk = openPalette;
})();
