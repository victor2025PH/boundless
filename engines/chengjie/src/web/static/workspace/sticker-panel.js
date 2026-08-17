/* 表情包（贴纸）面板 —— 挂载在收件箱既有 emoji 弹层 #wsemj-pop 上（2026-08-17 主线）。
 *
 * 为什么是独立静态文件：unified_inbox.html 是多线并发热区，本组件把全部逻辑收在
 * 这里，模板只需一行 <script src> 引入；贴纸气泡上的「收藏」按钮也走 data 属性
 * 事件委托（data-stk-collect），零内联 onclick——不触碰模板哑按钮门禁的扫描面。
 *
 * 行为：
 * - 包装 window.wsEmojiOpen：弹层打开后注入「表情 | 表情包」双 tab（feature flag
 *   关闭时不注入，纯 emoji 旧行为零变化）；
 * - 表情包区＝搜索过滤 + 最近使用 + 包切换条 + 网格 + 悬停放大预览，点贴纸即发
 *   （send-sticker，带幂等键）；响应 sent_as=image 时提示「已按图片发出」；
 *   LINE 官方贴纸在非 LINE 会话置灰；
 * - 管理弹层＝建包 / 拖拽或逐张上传（规范化在服务端，带 i/n 进度）/ 重命名 /
 *   删包 / 删贴纸 / 导入官方包；
 * - 入站贴纸收藏：委托监听 [data-stk-collect] → POST /api/stickers/collect；
 *   面板确认 flag 开启后置 documentElement[data-stk-on=1]，CSS 据此显隐收藏按钮。
 *
 * 依赖模板全局（均为顶层声明，有存在性守卫）：apiFetch/_toast/selectedChat/
 * loadThread/loadChats/_newClientMsgId/_uiBeacon/window.T/window.Tf/uiIcon。
 */
(function () {
  'use strict';

  var S = {
    enabled: null,          // null=未探测；false=flag 关（不注入 tab）
    packs: [], recent: [], limits: {},
    loadedAt: 0, active: 'recent', sending: false,
    mounted: false, mgrOpen: false,
    q: '',                  // 当前包内搜索（只滤已加载条目，不打服务端）
    gridItems: [], gridEmptyKey: 'inbox.stk.pack_empty',
    uploading: false,
    userPicked: false,      // 用户显式点过包 tab（自动落包只在未显式选择时生效）
  };

  function T(k) { try { var v = window.T ? window.T(k) : ''; return v || k; } catch (_) { return k; } }
  function Tf(k, p) { try { return window.Tf ? window.Tf(k, p) : k; } catch (_) { return k; } }
  function toast(msg, color) { try { if (typeof window._toast === 'function') window._toast(msg, color); } catch (_) { } }
  function beacon(k) { try { if (typeof window._uiBeacon === 'function') window._uiBeacon(k); } catch (_) { } }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function ic(name, size) {
    try { if (typeof window.uiIcon === 'function') return window.uiIcon(name, size || 14); } catch (_) { }
    return '';
  }
  function af(url, init, opt) {
    if (typeof window.apiFetch === 'function') return window.apiFetch(url, init, opt);
    return fetch(url, Object.assign({ credentials: 'same-origin' }, init || {}));
  }
  function jpost(url, body) {
    return af(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; }); });
  }
  function detailMsg(d, fallback) {
    if (d && d.detail) {
      if (typeof d.detail === 'string') return d.detail;
      if (typeof d.detail === 'object' && d.detail.message) return String(d.detail.message);
    }
    return fallback || '';
  }
  function curChat() { return window.selectedChat || null; }
  function newCmid() {
    try { if (typeof window._newClientMsgId === 'function') return window._newClientMsgId(); } catch (_) { }
    return 'stk-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }
  function failToast(d, status) {
    toast(detailMsg(d) || ('HTTP ' + status), '#dc2626');
  }

  /* ── 数据 ─────────────────────────────────────────────── */

  function fetchStatus() {
    return af('/api/stickers/status').then(function (r) {
      if (!r.ok) { S.enabled = null; return false; }  // 路由未装载（重启前灰度期）→ 保持未知，下次再探
      return r.json().then(function (d) {
        S.enabled = !!(d && d.enabled);
        if (S.enabled) { try { document.documentElement.setAttribute('data-stk-on', '1'); } catch (_) { } }
        return S.enabled;
      });
    }).catch(function () { S.enabled = null; return false; }); // 网络异常下次再探
  }

  function loadPacks(force) {
    var now = Date.now();
    if (!force && S.loadedAt && now - S.loadedAt < 30000) return Promise.resolve(true);
    return af('/api/stickers/packs').then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.enabled === false) { S.enabled = false; return false; }
        S.packs = d.packs || []; S.recent = d.recent || [];
        S.limits = d.limits || {}; S.loadedAt = now;
        // 首开体验：「最近」空（刚播种/新坐席从没发过）且用户没显式选过 tab
        // → 自动落到第一个包，别让人对着空网格发呆。
        if (!S.userPicked && S.active === 'recent' && !S.recent.length && S.packs.length) {
          S.active = String(S.packs[0].id || 'recent');
        }
        return true;
      }).catch(function () { return false; });
  }

  /* ── 弹层挂载 ─────────────────────────────────────────── */

  function pop() { return document.getElementById('wsemj-pop'); }

  function place() {
    var p = pop(); if (!p) return;
    try {
      var btn = document.getElementById('reply-emoji-btn');
      var r = btn ? btn.getBoundingClientRect() : { left: 60, top: window.innerHeight - 80 };
      var w = p.offsetWidth || 342, h = p.offsetHeight || 384;
      p.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
      p.style.top = Math.max(8, r.top - h - 8) + 'px';
    } catch (_) { }
  }

  function ensureMount() {
    var p = pop(); if (!p || S.mounted) return;
    var strip = document.createElement('div');
    strip.className = 'stk-topbar';
    strip.innerHTML =
      '<button type="button" class="stk-top on" data-stk-top="emoji">' + esc(T('inbox.stk.tab_emoji')) + '</button>' +
      '<button type="button" class="stk-top" data-stk-top="stk">' + esc(T('inbox.stk.tab_sticker')) + '</button>';
    p.insertBefore(strip, p.firstChild);
    var area = document.createElement('div');
    area.id = 'stk-area';
    area.style.display = 'none';
    area.innerHTML =
      '<div class="stk-search">' + ic('search', 13) +
      '<input type="search" id="stk-q" maxlength="40" placeholder="' + esc(T('inbox.stk.search_ph')) +
      '" autocomplete="off"></div>' +
      '<div class="stk-grid" id="stk-grid"><div class="stk-empty">' + esc(T('inbox.stk.loading')) + '</div></div>' +
      '<div class="stk-hint" id="stk-hint"></div>' +
      '<div class="stk-packbar" id="stk-packbar"></div>';
    p.appendChild(area);
    S.mounted = true;
  }

  function showTab(which) {
    var p = pop(); if (!p) return;
    var pk = p.querySelector('emoji-picker');
    var area = document.getElementById('stk-area');
    var isStk = which === 'stk';
    if (pk) pk.style.display = isStk ? 'none' : '';
    if (area) area.style.display = isStk ? 'flex' : 'none';
    var tops = p.querySelectorAll('.stk-top');
    for (var i = 0; i < tops.length; i++) {
      tops[i].classList.toggle('on', tops[i].getAttribute('data-stk-top') === (isStk ? 'stk' : 'emoji'));
    }
    if (isStk) {
      beacon('stk_open');
      loadPacks(false).then(function () { renderPackbar(); renderGrid(); place(); });
    } else {
      hidePreview();
    }
    place();
  }

  /* ── 渲染 ─────────────────────────────────────────────── */

  function capHint() {
    var c = curChat() || {}; var plat = String(c.platform || '');
    if (plat === 'telegram' || plat === 'whatsapp') return T('inbox.stk.cap_native');
    if (plat === 'line') return T('inbox.stk.cap_line');
    return T('inbox.stk.cap_image');
  }

  function renderPackbar() {
    var bar = document.getElementById('stk-packbar'); if (!bar) return;
    var h = '<button type="button" class="stk-pk' + (S.active === 'recent' ? ' on' : '') +
      '" data-stk-pack="recent" title="' + esc(T('inbox.stk.tab_recent')) + '">' +
      ic('clock', 15) + '</button>';
    for (var i = 0; i < S.packs.length; i++) {
      var pk = S.packs[i];
      var cover = pk.cover_url
        ? '<img src="' + esc(pk.cover_url) + '" alt="" loading="lazy" draggable="false">'
        : '<span class="stk-pk-txt">' + esc(String(pk.title || '?').slice(0, 2)) + '</span>';
      h += '<button type="button" class="stk-pk' + (S.active === pk.id ? ' on' : '') +
        '" data-stk-pack="' + esc(pk.id) + '" title="' + esc(pk.title || '') + '">' + cover + '</button>';
    }
    h += '<button type="button" class="stk-pk stk-pk-mgr" data-stk-mgr="1" title="' +
      esc(T('inbox.stk.manage')) + '">' + ic('gear', 15) + '</button>';
    bar.innerHTML = h;
    var hint = document.getElementById('stk-hint');
    if (hint) hint.textContent = capHint();
  }

  function itemsForActive() {
    if (S.active === 'recent') return { items: S.recent, emptyKey: 'inbox.stk.recent_empty' };
    for (var i = 0; i < S.packs.length; i++) {
      if (S.packs[i].id === S.active) return null; // 需拉取
    }
    return { items: S.recent, emptyKey: 'inbox.stk.recent_empty' };
  }

  var _itemsCache = {};   // pack_id → items（30s 随 loadedAt 一起失效）

  function renderGrid() {
    var grid = document.getElementById('stk-grid'); if (!grid) return;
    if (!S.packs.length && !S.recent.length) {
      hidePreview();
      grid.innerHTML =
        '<div class="stk-empty">' + esc(T('inbox.stk.empty')) +
        '<div class="stk-empty-cta">' +
        '<button type="button" class="stk-btn" data-stk-seed="1">' + esc(T('inbox.stk.empty_cta_official')) + '</button>' +
        '<button type="button" class="stk-btn" data-stk-mgr="1">' + esc(T('inbox.stk.empty_cta_create')) + '</button>' +
        '</div></div>';
      return;
    }
    var pre = itemsForActive();
    if (pre) { paintGrid(pre.items, pre.emptyKey); return; }
    var pid = S.active;
    if (_itemsCache[pid] && _itemsCache[pid].at > Date.now() - 30000) {
      paintGrid(_itemsCache[pid].items, 'inbox.stk.pack_empty'); return;
    }
    grid.innerHTML = '<div class="stk-empty">' + esc(T('inbox.stk.loading')) + '</div>';
    af('/api/stickers/packs/' + encodeURIComponent(pid) + '/items')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var items = (d && d.items) || [];
        _itemsCache[pid] = { items: items, at: Date.now() };
        if (S.active === pid) paintGrid(items, 'inbox.stk.pack_empty');
      }).catch(function () { paintGrid([], 'inbox.stk.pack_empty'); });
  }

  function matchItem(it, q) {
    if (!q) return true;
    var hay = String(it.emoji_tag || '') + ' ' + String(it.id || '');
    var kws = it.keywords;
    if (kws && kws.length) hay += ' ' + kws.join(' ');
    return hay.toLowerCase().indexOf(q) >= 0;
  }

  function paintGrid(items, emptyKey) {
    var grid = document.getElementById('stk-grid'); if (!grid) return;
    S.gridItems = items || [];
    S.gridEmptyKey = emptyKey || 'inbox.stk.pack_empty';
    var q = String(S.q || '').trim().toLowerCase();
    var shown = q ? S.gridItems.filter(function (it) { return matchItem(it, q); }) : S.gridItems;
    if (!S.gridItems.length) {
      hidePreview();
      grid.innerHTML = '<div class="stk-empty">' + esc(T(S.gridEmptyKey)) + '</div>';
      return;
    }
    if (!shown.length) {
      hidePreview();
      grid.innerHTML = '<div class="stk-empty">' + esc(T('inbox.stk.search_empty')) + '</div>';
      return;
    }
    var plat = String((curChat() || {}).platform || '');
    var h = '';
    for (var i = 0; i < shown.length; i++) {
      var it = shown[i];
      var lineOnly = !!it.line_only;
      var dis = lineOnly && plat !== 'line';
      h += '<button type="button" class="stk-cell' + (dis ? ' dis' : '') +
        '" data-stk-id="' + esc(it.id) + '"' + (dis ? ' data-stk-dis="1"' : '') +
        ' title="' + esc(dis ? T('inbox.stk.line_only_hint') : (it.emoji_tag || '')) + '">' +
        '<img src="' + esc(it.url) + '" alt="" loading="lazy" draggable="false"></button>';
    }
    grid.innerHTML = h;
  }

  /* ── 悬停放大预览 ─────────────────────────────────────── */

  function previewEl() { return document.getElementById('stk-preview'); }

  function hidePreview() {
    var el = previewEl();
    if (el) el.classList.remove('on');
  }

  function showPreview(cell) {
    if (!cell || cell.getAttribute('data-stk-dis')) { hidePreview(); return; }
    var img = cell.querySelector('img');
    if (!img || !img.src) { hidePreview(); return; }
    var el = previewEl();
    if (!el) {
      el = document.createElement('div');
      el.id = 'stk-preview';
      el.setAttribute('aria-hidden', 'true');
      document.body.appendChild(el);
    }
    el.innerHTML = '<img src="' + esc(img.src) + '" alt="">';
    var r = cell.getBoundingClientRect();
    var left = r.right + 10;
    if (left + 180 > window.innerWidth) left = r.left - 188;
    if (left < 8) left = 8;
    var top = Math.max(8, Math.min(r.top - 24, window.innerHeight - 184));
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    el.classList.add('on');
  }

  /* ── 发送 ─────────────────────────────────────────────── */

  function sendSticker(sid, cell) {
    var c = curChat();
    if (!c) { toast(T('inbox.toast.select_first'), '#b45309'); return; }
    if (c.read_only || c.can_send === false) { toast(T('inbox.acct.removed_readonly'), '#dc2626'); return; }
    if (S.sending) return;
    S.sending = true;
    hidePreview();
    if (cell) cell.classList.add('busy');
    jpost('/api/unified-inbox/send-sticker', {
      platform: c.platform, account_id: c.account_id || 'default',
      chat_key: c.chat_key, sticker_id: sid, client_msg_id: newCmid(),
    }).then(function (res) {
      var r = res.r, d = res.d;
      if (r.ok && d && d.ok) {
        beacon('stk_send');
        if (d.sent_as === 'image') toast(T('inbox.stk.sent_as_image'), '#b45309');
        var p = pop(); if (p) p.classList.remove('show');
        try { if (typeof window.loadThread === 'function') window.loadThread(curChat(), true); } catch (_) { }
        try { if (typeof window.loadChats === 'function') window.loadChats(); } catch (_) { }
        S.loadedAt = 0; // recent 变了，下次打开重拉
      } else {
        failToast(d, r.status);
      }
    }).catch(function () {
      toast(T('inbox.media.send_fail_net'), '#dc2626');
    }).then(function () {
      S.sending = false;
      if (cell) cell.classList.remove('busy');
    });
  }

  /* ── 收藏入站贴纸（气泡按钮 data-stk-collect 委托）───────── */

  function collect(ref, btn) {
    if (!ref) return;
    if (btn) btn.classList.add('busy');
    jpost('/api/stickers/collect', { media_ref: ref }).then(function (res) {
      var r = res.r, d = res.d;
      if (r.ok && d && d.ok) {
        beacon('stk_collect');
        toast(T(d.deduped ? 'inbox.stk.collect_dup' : 'inbox.stk.collected'), d.deduped ? '#64748b' : '#0f9d75');
        S.loadedAt = 0;
      } else if (r.status === 403) {
        toast(T('inbox.stk.disabled_hint'), '#b45309');
      } else {
        failToast(d, r.status);
      }
    }).catch(function () { toast(T('inbox.media.send_fail_net'), '#dc2626'); })
      .then(function () { if (btn) btn.classList.remove('busy'); });
  }

  /* ── 管理弹层 ─────────────────────────────────────────── */

  function mgrEl() { return document.getElementById('stk-mgr'); }

  function bindMgrDrop(el) {
    if (!el || el.__stkDrop) return;
    el.__stkDrop = true;
    el.addEventListener('dragover', function (e) {
      if (!e.dataTransfer) return;
      var types = e.dataTransfer.types;
      var has = false;
      for (var i = 0; i < (types || []).length; i++) if (types[i] === 'Files') has = true;
      if (!has) return;
      e.preventDefault();
      el.classList.add('stk-drop-on');
    });
    el.addEventListener('dragleave', function (e) {
      if (e.target === el) el.classList.remove('stk-drop-on');
    });
    el.addEventListener('drop', function (e) {
      e.preventDefault();
      el.classList.remove('stk-drop-on');
      var raw = (e.dataTransfer && e.dataTransfer.files) ? e.dataTransfer.files : [];
      var files = [];
      for (var i = 0; i < raw.length; i++) {
        var f = raw[i];
        if (/^image\//.test(f.type) || /\.(png|jpe?g|webp|gif)$/i.test(f.name || '')) files.push(f);
      }
      if (!files.length) return;
      if (!_mgrExpanded) { toast(T('inbox.stk.drop_need_pack'), '#b45309'); return; }
      doUpload(_mgrExpanded, files);
    });
  }

  function openMgr() {
    var el = mgrEl();
    if (!el) {
      el = document.createElement('div');
      el.id = 'stk-mgr';
      el.innerHTML =
        '<div class="stk-mgr-card" role="dialog" aria-modal="true">' +
        '<div class="stk-mgr-hd"><span>' + esc(T('inbox.stk.manage')) + '</span>' +
        '<button type="button" class="stk-mgr-x" data-stk-mgr-close="1" title="' + esc(T('inbox.stk.close')) + '">' +
        ic('x', 14) + '</button></div>' +
        '<div class="stk-mgr-new">' +
        '<input type="text" id="stk-new-name" maxlength="40" placeholder="' + esc(T('inbox.stk.new_pack_ph')) + '">' +
        '<button type="button" class="stk-btn" data-stk-newpack="1">' + esc(T('inbox.stk.new_pack')) + '</button>' +
        '<button type="button" class="stk-btn ghost" data-stk-seed="1">' + esc(T('inbox.stk.seed_official')) + '</button>' +
        '</div>' +
        '<div class="stk-mgr-hint">' + esc(T('inbox.stk.upload_hint')) + ' · ' +
        esc(T('inbox.stk.drop_hint')) + '</div>' +
        '<div class="stk-upbar" id="stk-upbar"><div class="stk-up-meta" id="stk-up-meta"></div>' +
        '<div class="stk-up-track"><div class="stk-up-fill" id="stk-up-fill"></div></div></div>' +
        '<div class="stk-mgr-list" id="stk-mgr-list"></div>' +
        '</div>';
      document.body.appendChild(el);
      el.addEventListener('click', function (e) { if (e.target === el) closeMgr(); });
      bindMgrDrop(el);
    }
    el.style.display = 'flex';
    S.mgrOpen = true;
    renderMgr();
  }

  function closeMgr() {
    var el = mgrEl(); if (el) el.style.display = 'none';
    S.mgrOpen = false;
  }

  var _mgrExpanded = '';   // 展开条目网格的 pack id

  function renderMgr() {
    var list = document.getElementById('stk-mgr-list'); if (!list) return;
    loadPacks(true).then(function () {
      if (!S.packs.length) {
        list.innerHTML = '<div class="stk-empty">' + esc(T('inbox.stk.empty')) + '</div>';
        return;
      }
      var h = '';
      for (var i = 0; i < S.packs.length; i++) {
        var pk = S.packs[i];
        var official = pk.kind === 'official';
        h += '<div class="stk-mgr-row" data-stk-row="' + esc(pk.id) + '">' +
          '<button type="button" class="stk-mgr-title" data-stk-expand="' + esc(pk.id) + '">' +
          (pk.cover_url ? '<img src="' + esc(pk.cover_url) + '" alt="" draggable="false">' : '') +
          '<span>' + esc(pk.title || pk.id) + '</span><em>' + Number(pk.count || 0) + '</em>' +
          (official ? '<i class="stk-official">official</i>' : '') + '</button>' +
          '<span class="stk-mgr-acts">' +
          '<button type="button" class="stk-btn ghost" data-stk-rename="' + esc(pk.id) + '">' + esc(T('inbox.stk.rename')) + '</button>' +
          '<button type="button" class="stk-btn" data-stk-upload="' + esc(pk.id) + '">' + esc(T('inbox.stk.upload')) + '</button>' +
          '<button type="button" class="stk-btn danger" data-stk-delpack="' + esc(pk.id) + '">' + esc(T('inbox.stk.delete_pack')) + '</button>' +
          '</span></div>' +
          (_mgrExpanded === pk.id ? '<div class="stk-mgr-items" data-stk-items="' + esc(pk.id) + '"><div class="stk-empty">' + esc(T('inbox.stk.loading')) + '</div></div>' : '');
      }
      list.innerHTML = h;
      if (_mgrExpanded) fillMgrItems(_mgrExpanded);
    });
  }

  function fillMgrItems(pid) {
    var box = document.querySelector('[data-stk-items="' + pid + '"]'); if (!box) return;
    af('/api/stickers/packs/' + encodeURIComponent(pid) + '/items')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var items = (d && d.items) || [];
        if (!items.length) { box.innerHTML = '<div class="stk-empty">' + esc(T('inbox.stk.pack_empty')) + '</div>'; return; }
        var h = '';
        for (var i = 0; i < items.length; i++) {
          var it = items[i];
          h += '<span class="stk-mgr-it"><img src="' + esc(it.url) + '" alt="" loading="lazy" draggable="false">' +
            '<button type="button" class="stk-it-x" data-stk-delitem="' + esc(it.id) + '" data-stk-pid="' + esc(pid) +
            '" title="' + esc(T('inbox.stk.delete_item')) + '">' + ic('x', 10) + '</button></span>';
        }
        box.innerHTML = h;
      }).catch(function () { });
  }

  var _upInput = null;

  function pickUpload(pid) {
    if (!_upInput) {
      _upInput = document.createElement('input');
      _upInput.type = 'file';
      _upInput.accept = '.png,.jpg,.jpeg,.webp,.gif,image/*';
      _upInput.multiple = true;
      _upInput.style.display = 'none';
      document.body.appendChild(_upInput);
      _upInput.addEventListener('change', function () {
        var files = Array.prototype.slice.call(_upInput.files || []);
        var pid2 = _upInput.getAttribute('data-pid') || '';
        _upInput.value = '';
        if (files.length && pid2) doUpload(pid2, files);
      });
    }
    _upInput.setAttribute('data-pid', pid);
    _upInput.click();
  }

  function paintUpProg(info) {
    var bar = document.getElementById('stk-upbar');
    var meta = document.getElementById('stk-up-meta');
    var fill = document.getElementById('stk-up-fill');
    if (!bar) return;
    if (!info) { bar.classList.remove('on'); if (fill) fill.style.width = '0'; return; }
    bar.classList.add('on');
    if (meta) meta.textContent = Tf('inbox.stk.uploading_n', {
      i: info.i, n: info.n, name: info.name || '',
    });
    if (fill) fill.style.width = Math.round((info.i / Math.max(1, info.n)) * 100) + '%';
  }

  function doUpload(pid, files) {
    if (S.uploading || !files || !files.length) return;
    S.uploading = true;
    var i = 0, added = 0, deduped = 0, failed = 0;
    function finish() {
      S.uploading = false;
      paintUpProg(null);
      beacon('stk_add');
      var bits = [Tf('inbox.stk.added_n', { n: added })];
      if (deduped) bits.push(Tf('inbox.stk.dedup_n', { n: deduped }));
      if (failed) bits.push(Tf('inbox.stk.failed_n', { n: failed }));
      toast(bits.join(' / '), failed ? '#b45309' : '#0f9d75');
      S.loadedAt = 0; delete _itemsCache[pid];
      _mgrExpanded = pid;
      renderMgr();
      if (S.active === pid || S.active === 'recent') renderGrid();
    }
    function next() {
      if (i >= files.length) { finish(); return; }
      var f = files[i];
      paintUpProg({ i: i + 1, n: files.length, name: f.name || '' });
      var fd = new FormData();
      fd.append('file', f);
      af('/api/stickers/packs/' + encodeURIComponent(pid) + '/items',
        { method: 'POST', body: fd }, { timeoutMs: 120000 })
        .then(function (r) { return r.json().then(function (d) { return { r: r, d: d }; }); })
        .then(function (res) {
          var d = res.d || {};
          if (res.r.ok && d.ok) {
            added += (d.added || []).length;
            deduped += Number(d.deduped || 0);
            failed += (d.failed || []).length;
          } else {
            failed += 1;
          }
        })
        .catch(function () { failed += 1; })
        .then(function () { i += 1; next(); });
    }
    next();
  }

  function startRename(pid, btn) {
    var row = btn && btn.closest ? btn.closest('.stk-mgr-row') : null;
    var span = row && row.querySelector('.stk-mgr-title span');
    if (!span) return;
    var old = String(span.textContent || '').trim();
    var inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'stk-rename-inp';
    inp.value = old;
    inp.maxLength = 40;
    span.replaceWith(inp);
    inp.focus();
    inp.select();
    var done = false;
    function commit() {
      if (done) return;
      done = true;
      var title = String(inp.value || '').trim();
      if (!title || title === old) { renderMgr(); return; }
      af('/api/stickers/packs/' + encodeURIComponent(pid), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title }),
      }).then(function (r) { return r.json().then(function (d) { return { r: r, d: d }; }); })
        .then(function (res) {
          if (!(res.r.ok && res.d && res.d.ok)) failToast(res.d, res.r.status);
          S.loadedAt = 0;
          renderMgr();
          renderPackbar();
        })
        .catch(function () { toast(T('inbox.media.send_fail_net'), '#dc2626'); renderMgr(); });
    }
    inp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); commit(); }
      if (ev.key === 'Escape') { ev.preventDefault(); done = true; renderMgr(); }
    });
    inp.addEventListener('blur', commit);
  }

  function seedOfficial(btn) {
    if (btn) btn.disabled = true;
    jpost('/api/stickers/seed-official', {}).then(function (res) {
      var r = res.r, d = res.d;
      if (r.ok && d && d.ok) {
        beacon('stk_seed');
        toast(T('inbox.stk.seeded'), '#0f9d75');
        S.loadedAt = 0; _itemsCache = {};
        if (S.mgrOpen) renderMgr();
        loadPacks(true).then(function () { renderPackbar(); renderGrid(); });
      } else {
        failToast(d, r.status);
      }
    }).catch(function () { toast(T('inbox.media.send_fail_net'), '#dc2626'); })
      .then(function () { if (btn) btn.disabled = false; });
  }

  /* ── 事件委托（单监听器，覆盖弹层 + 管理弹层 + 气泡收藏）── */

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    if (!t) return;
    var el;
    if ((el = t.closest('[data-stk-top]'))) {
      e.stopPropagation();
      showTab(el.getAttribute('data-stk-top') === 'stk' ? 'stk' : 'emoji');
      return;
    }
    if ((el = t.closest('[data-stk-pack]'))) {
      e.stopPropagation();
      S.active = el.getAttribute('data-stk-pack') || 'recent';
      S.userPicked = true;
      renderPackbar(); renderGrid();
      return;
    }
    if ((el = t.closest('[data-stk-id]'))) {
      e.stopPropagation();
      if (el.getAttribute('data-stk-dis')) { toast(T('inbox.stk.line_only_hint'), '#b45309'); return; }
      sendSticker(el.getAttribute('data-stk-id'), el);
      return;
    }
    if ((el = t.closest('[data-stk-collect]'))) {
      e.stopPropagation(); e.preventDefault();
      collect(el.getAttribute('data-stk-collect'), el);
      return;
    }
    if ((el = t.closest('[data-stk-mgr]'))) { e.stopPropagation(); openMgr(); return; }
    if ((el = t.closest('[data-stk-mgr-close]'))) { e.stopPropagation(); closeMgr(); return; }
    if ((el = t.closest('[data-stk-seed]'))) { e.stopPropagation(); seedOfficial(el); return; }
    if ((el = t.closest('[data-stk-newpack]'))) {
      e.stopPropagation();
      var inp = document.getElementById('stk-new-name');
      var title = inp ? String(inp.value || '').trim() : '';
      if (!title) { if (inp) inp.focus(); return; }
      jpost('/api/stickers/packs', { title: title }).then(function (res) {
        if (res.r.ok && res.d && res.d.ok) {
          if (inp) inp.value = '';
          S.loadedAt = 0; renderMgr();
        } else {
          failToast(res.d, res.r.status);
        }
      });
      return;
    }
    if ((el = t.closest('[data-stk-rename]'))) {
      e.stopPropagation();
      startRename(el.getAttribute('data-stk-rename'), el);
      return;
    }
    if ((el = t.closest('[data-stk-upload]'))) {
      e.stopPropagation(); pickUpload(el.getAttribute('data-stk-upload')); return;
    }
    if ((el = t.closest('[data-stk-delpack]'))) {
      e.stopPropagation();
      var pid = el.getAttribute('data-stk-delpack');
      if (!window.confirm(T('inbox.stk.delete_pack_confirm'))) return;
      af('/api/stickers/packs/' + encodeURIComponent(pid), { method: 'DELETE' })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function () {
          S.loadedAt = 0; delete _itemsCache[pid];
          if (S.active === pid) S.active = 'recent';
          if (_mgrExpanded === pid) _mgrExpanded = '';
          renderMgr(); renderPackbar(); renderGrid();
        });
      return;
    }
    if ((el = t.closest('[data-stk-delitem]'))) {
      e.stopPropagation();
      if (!window.confirm(T('inbox.stk.delete_item'))) return;
      var sid = el.getAttribute('data-stk-delitem');
      var pid3 = el.getAttribute('data-stk-pid');
      af('/api/stickers/packs/' + encodeURIComponent(pid3) + '/items/' + encodeURIComponent(sid),
        { method: 'DELETE' })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function () {
          S.loadedAt = 0; delete _itemsCache[pid3];
          fillMgrItems(pid3); renderMgr();
        });
      return;
    }
    if ((el = t.closest('[data-stk-expand]'))) {
      e.stopPropagation();
      var pid4 = el.getAttribute('data-stk-expand');
      _mgrExpanded = (_mgrExpanded === pid4) ? '' : pid4;
      renderMgr();
      return;
    }
  }, true);

  document.addEventListener('input', function (e) {
    if (!e.target || e.target.id !== 'stk-q') return;
    S.q = String(e.target.value || '');
    paintGrid(S.gridItems, S.gridEmptyKey);
  });

  document.addEventListener('mouseover', function (e) {
    var cell = e.target && e.target.closest ? e.target.closest('.stk-cell') : null;
    if (cell && cell.closest('#stk-grid')) showPreview(cell);
  });
  document.addEventListener('mouseout', function (e) {
    var cell = e.target && e.target.closest ? e.target.closest('.stk-cell') : null;
    if (!cell) return;
    var to = e.relatedTarget;
    if (to && cell.contains(to)) return;
    hidePreview();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && S.mgrOpen) closeMgr();
  });

  /* ── wsEmojiOpen 包装：弹层出现后注入 tab（flag 关＝零改动）── */

  function augmentSoon(tries) {
    var p = pop();
    if (p && p.classList.contains('show')) {
      if (S.enabled === null) {
        fetchStatus().then(function (ok) { if (ok) { ensureMount(); place(); } });
      } else if (S.enabled) {
        ensureMount();
        place();
      }
      return;
    }
    if ((tries || 0) < 30) setTimeout(function () { augmentSoon((tries || 0) + 1); }, 100);
  }

  function wrapOpen() {
    var orig = window.wsEmojiOpen;
    if (typeof orig !== 'function' || orig.__stkWrapped) return false;
    var wrapped = function (ev) {
      var out = orig.apply(this, arguments);
      try { augmentSoon(0); } catch (_) { }
      return out;
    };
    wrapped.__stkWrapped = true;
    window.wsEmojiOpen = wrapped;
    return true;
  }

  if (!wrapOpen()) {
    document.addEventListener('DOMContentLoaded', wrapOpen);
    setTimeout(wrapOpen, 1500);
  }
  // 收藏按钮显隐门（CSS [data-stk-on] 选择器）：页面装载即探一次 flag，
  // 失败静默——收藏按钮不显示，点表情按钮时会再探。
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(fetchStatus, 800);
  } else {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(fetchStatus, 800); });
  }
})();
