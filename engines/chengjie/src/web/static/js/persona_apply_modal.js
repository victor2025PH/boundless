/* ═══════════════════════════════════════════════════════════════════════════
 * persona_apply_modal.js — 人设「应用到…」统一入口弹窗（personas.html 拆分模块）
 *
 * 用途：
 *   给单个人设提供统一的"应用出口"弹窗，三个分区：
 *     1) 账号默认 —— 把人设指定为 TG / Messenger / WhatsApp 账号的默认人设
 *        （端点与 personas.html 里 PLAT_ENDPOINT + _ppPick 完全一致）
 *     2) 绑定会话 —— 查看 / 解绑已绑定 chat，或按 chat_id 新增绑定
 *        （与 bindProfileToChatB / unbindChat 相同端点与 body）
 *     3) 全局默认 —— 把人设设为全局兜底人设（参照 saveDefaultPersona）
 *
 * 实现约定：
 *   - IIFE 直挂 window，无 ES module、无构建；ES2017 以内写法（无可选链）。
 *   - 复用页面已有模态骨架类 .pp-modal-ov/.pp-modal/.pp-modal-hd/.pp-modal-ttl/
 *     .pp-modal-sub/.pp-modal-x/.pp-modal-list/.pp-modal-foot；
 *     增量样式注入 <style id="ps-apply-style">，类名前缀 .psa- 避免冲突。
 *   - 模态 DOM 动态创建 append 到 body，关闭即移除，再次 open 重建。
 *   - 页面全局（T/_toast/_allProfiles/_faceRefs/_profileColor/loadProfileList/
 *     refreshStatus/_reloadBindingsList）一律防御式引用，缺失时优雅降级。
 *
 * 对外 API：
 *   window.PSApply.open(pid)   —— 打开指定人设的「应用到…」弹窗
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── 端点（照抄 personas.html 的 PLAT_ENDPOINT，body 同 _ppPick）────────── */
  var ENDPOINT = {
    tg:   function (aid) { return '/api/personas/tg-account/'   + encodeURIComponent(aid) + '/assign-profile'; },
    mrpa: function (aid) { return '/api/personas/mrpa-account/' + encodeURIComponent(aid) + '/assign-profile'; },
    wa:   function (aid) { return '/api/personas/wa-account/'   + encodeURIComponent(aid) + '/assign-profile'; }
  };
  var PLAT_LABEL = { tg: 'TG', mrpa: 'Messenger', wa: 'WhatsApp' };
  var ACC_KEY = { tg: 'tg_accounts', mrpa: 'mrpa_accounts', wa: 'wa_accounts' };

  /* ── 模块状态 ──────────────────────────────────────────────────────────── */
  var _pid = '';        // 当前弹窗对应的人设 id
  var _gen = 0;         // 代际号：open/close 时 +1，用于丢弃过期的异步响应
  var _accCount = -1;   // 服务概况：正在使用该人设的账号数（-1 = 加载中）
  var _bindTotal = -1;  // 服务概况：该人设已绑定的会话数（-1 = 加载中）
  var _escBound = false;

  /* ── 小工具 ───────────────────────────────────────────────────────────── */
  function _t(key, fb) {
    if (typeof window.T === 'function') return window.T(key, fb);
    return fb;
  }

  // 占位符插值：_fmt('{a} 个', {a:3}) → '3 个'（split/join，不依赖页面的 Tf）
  function _fmt(s, vars) {
    s = String(s == null ? '' : s);
    if (!vars) return s;
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) {
        s = s.split('{' + k + '}').join(String(vars[k]));
      }
    }
    return s;
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _toastSafe(msg, type) {
    if (typeof window._toast === 'function') window._toast(msg, type);
  }

  function _fail(err) {
    _toastSafe(_fmt(_t('psn_apply_fail', '操作失败：{err}'), { err: err }), 'err');
  }

  // 操作成功后同步刷新页面主视图（存在才调）
  function _refreshPage() {
    if (typeof window.loadProfileList === 'function') window.loadProfileList();
    if (typeof window.refreshStatus === 'function') window.refreshStatus();
    if (typeof window._reloadBindingsList === 'function') window._reloadBindingsList();
  }

  function _findProfile(pid) {
    var list = window._allProfiles || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === pid) return list[i];
    }
    return null;
  }

  async function _fetchPersona(pid) {
    var r = await fetch('/api/personas/profiles/' + encodeURIComponent(pid));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    return d.persona || {};
  }

  /* ── 增量样式（一次注入，.psa- 前缀）──────────────────────────────────── */
  var CSS = ''
    + '.psa-hd-main{display:flex;align-items:center;gap:.7rem;min-width:0;flex:1}'
    + '.psa-av{position:relative;width:42px;height:42px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:1rem;font-weight:700;color:#fff;overflow:hidden;background:#6366f1}'
    + '.psa-av img{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover}'
    + '.psa-hd-txt{min-width:0}'
    + '.psa-role{font-size:.72rem;font-weight:400;color:var(--t2);margin-left:.3rem}'
    + '.psa-tabs{display:flex;gap:.35rem;padding:.1rem 1.1rem .6rem}'
    + '.psa-tab{flex:1;padding:.42rem .3rem;font-size:.76rem;font-weight:600;text-align:center;background:transparent;border:1px solid var(--bd);border-radius:8px;color:var(--t2);cursor:pointer;font-family:inherit}'
    + '.psa-tab:hover{color:var(--t1);background:rgba(91,124,246,.06)}'
    + '.psa-tab.active{color:var(--accent,#5b7cf6);border-color:var(--accent,#5b7cf6);background:rgba(91,124,246,.1)}'
    + '.psa-pane{padding:.2rem .35rem .5rem}'
    + '.psa-sec-title{font-size:.72rem;font-weight:700;color:var(--t2);margin:.5rem .15rem .3rem}'
    + '.psa-row{display:flex;align-items:center;gap:.55rem;padding:.45rem .3rem;border-bottom:1px dashed var(--bd)}'
    + '.psa-row:last-child{border-bottom:none}'
    + '.psa-plat{flex-shrink:0;font-size:.62rem;font-weight:700;padding:.12rem .42rem;border-radius:5px;background:rgba(91,124,246,.12);color:var(--accent,#5b7cf6);border:1px solid rgba(91,124,246,.2)}'
    + '.psa-row-main{flex:1;min-width:0}'
    + '.psa-row-name{font-size:.8rem;font-weight:600;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.psa-row-sub{font-size:.68rem;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.psa-cur{flex-shrink:0;font-size:.66rem;font-weight:700;color:#10b981}'
    + '.psa-empty{padding:1.3rem 0;text-align:center;color:var(--t2);font-size:.8rem}'
    + '.psa-loading{padding:1.1rem 0;text-align:center;color:var(--t2);font-size:.78rem}'
    + '.psa-chips{display:flex;flex-wrap:wrap;gap:.3rem;margin:.15rem .15rem .45rem}'
    + '.psa-chip{font-size:.66rem;padding:.12rem .45rem;border-radius:5px;background:rgba(91,124,246,.08);border:1px solid var(--bd);color:var(--t2)}'
    + '.psa-more{padding:.3rem 0;text-align:center;color:var(--t2);font-size:.72rem}'
    + '.psa-bind-bar{display:flex;gap:.4rem;margin:.65rem .15rem .35rem}'
    + '.psa-bind-bar input{flex:1;min-width:0;padding:.45rem .6rem;border:1px solid var(--bd);border-radius:8px;background:var(--bg2,var(--bg));color:var(--t1);font-size:.8rem;font-family:inherit}'
    + '.psa-desc{font-size:.78rem;color:var(--t2);line-height:1.6;margin:.55rem .15rem .85rem}';

  function _injectStyle() {
    if (document.getElementById('ps-apply-style')) return;
    var st = document.createElement('style');
    st.id = 'ps-apply-style';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ── 头像：优先锁脸基准照，回落首字母色块（img 失败自移除露出首字母）──── */
  function _avatarHTML(pid, p) {
    var color = '#6366f1';
    if (typeof window._profileColor === 'function') {
      try { color = window._profileColor(pid) || color; } catch (e) {}
    }
    var initial = String((p && p.name) || pid || '?').charAt(0).toUpperCase();
    var refs = window._faceRefs || {};
    var fr = refs[pid];
    var img = (fr && fr.url)
      ? '<img src="' + _esc(fr.url) + '" alt="" loading="lazy" onerror="this.remove()">'
      : '';
    return '<div class="psa-av" style="background:' + _esc(color) + '">' + _esc(initial) + img + '</div>';
  }

  /* ── 服务概况副标题：{a} 个账号 · {c} 个会话 ─────────────────────────── */
  function _updateServing() {
    var el = document.getElementById('psa-serving');
    if (!el) return;
    if (_accCount < 0 || _bindTotal < 0) {
      el.textContent = _t('psn_apply_loading', '加载中…');
      return;
    }
    el.textContent = _fmt(_t('psn_apply_serving', '当前服务：{a} 个账号 · {c} 个会话'),
      { a: _accCount, c: _bindTotal });
  }

  /* ═══ 分区 1：账号默认 ═══════════════════════════════════════════════ */
  async function _loadAccounts() {
    var gen = _gen;
    var box = document.getElementById('psa-pane-account');
    if (!box) return;
    box.innerHTML = '<div class="psa-loading">' + _esc(_t('psn_apply_loading', '加载中…')) + '</div>';
    try {
      var r = await fetch('/api/personas/status');
      if (gen !== _gen) return;   // 弹窗已关闭/重开，丢弃过期响应
      if (!r.ok) {
        _accCount = 0; _updateServing();
        box.innerHTML = '<div class="psa-empty">' + _esc(_fmt(_t('psn_apply_fail', '操作失败：{err}'), { err: 'HTTP ' + r.status })) + '</div>';
        return;
      }
      var d = await r.json();
      if (gen !== _gen) return;
      _renderAccountPane(d || {});
    } catch (e) {
      if (gen !== _gen) return;
      _accCount = 0; _updateServing();
      box.innerHTML = '<div class="psa-empty">' + _esc(_fmt(_t('psn_apply_fail', '操作失败：{err}'), { err: e })) + '</div>';
    }
  }

  function _renderAccountPane(d) {
    var box = document.getElementById('psa-pane-account');
    if (!box) return;
    var plats = ['tg', 'mrpa', 'wa'];
    var total = 0, mine = 0, html = '';
    for (var i = 0; i < plats.length; i++) {
      var plat = plats[i];
      var accs = d[ACC_KEY[plat]] || [];
      for (var j = 0; j < accs.length; j++) {
        var a = accs[j] || {};
        total++;
        var aid = a.account_id != null ? String(a.account_id) : '';
        var name = a.label || aid || '—';
        var curName = a.active_profile ? (a.active_profile.name || a.active_profile.id || '') : '';
        var isMine = (a.persona_ids || []).indexOf(_pid) !== -1;
        if (isMine) mine++;
        html += '<div class="psa-row">'
          + '<span class="psa-plat">' + PLAT_LABEL[plat] + '</span>'
          + '<div class="psa-row-main">'
          +   '<div class="psa-row-name" title="' + _esc(aid) + '">' + _esc(name) + '</div>'
          +   '<div class="psa-row-sub">' + (curName ? _esc(curName) : '—') + '</div>'
          + '</div>'
          + (isMine
              ? '<span class="psa-cur">' + _esc(_t('psn_apply_current', '✓ 当前人设')) + '</span>'
                + '<button class="btn btn-sm btn-danger" data-act="clear" data-plat="' + plat + '" data-aid="' + _esc(aid) + '">'
                + _esc(_t('psn_apply_clear', '清除')) + '</button>'
              : '<button class="btn btn-sm btn-primary" data-act="assign" data-plat="' + plat + '" data-aid="' + _esc(aid) + '">'
                + _esc(_t('psn_apply_assign', '指定')) + '</button>')
          + '</div>';
      }
    }
    _accCount = mine;
    _updateServing();
    box.innerHTML = total ? html
      : '<div class="psa-empty">' + _esc(_t('psn_apply_no_accounts', '暂无已接入账号')) + '</div>';
  }

  // 指定 / 清除账号默认人设：POST assign-profile，body {profile_id}（清除传 ''，同 _ppPick('')）
  async function _assign(plat, aid, profileId, btn) {
    var ep = ENDPOINT[plat];
    if (!ep || !aid || (btn && btn.disabled)) return;
    if (btn) btn.disabled = true;
    try {
      var r = await fetch(ep(aid), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: profileId })
      });
      var d = null;
      try { d = await r.json(); } catch (e2) { d = null; }
      if (r.ok && d && d.ok) {
        _toastSafe(profileId ? _t('psn_apply_ok_assign', '已指定到账号') : _t('psn_apply_ok_clear', '已清除账号指定'), 'ok');
        _loadAccounts();
        _refreshPage();
      } else {
        _fail((d && d.detail) || ('HTTP ' + r.status));
        if (btn) btn.disabled = false;
      }
    } catch (e) {
      _fail(e);
      if (btn) btn.disabled = false;
    }
  }

  /* ═══ 分区 2：绑定会话 ═══════════════════════════════════════════════ */
  async function _loadBindings() {
    var gen = _gen;
    var pid = _pid;
    var box = document.getElementById('psa-pane-chat');
    if (!box) return;
    box.innerHTML = '<div class="psa-loading">' + _esc(_t('psn_apply_loading', '加载中…')) + '</div>';
    try {
      var r = await fetch('/api/personas/profiles/' + encodeURIComponent(pid) + '/bindings');
      if (gen !== _gen) return;
      if (!r.ok) {
        _bindTotal = 0; _updateServing();
        box.innerHTML = '<div class="psa-empty">' + _esc(_fmt(_t('psn_apply_fail', '操作失败：{err}'), { err: 'HTTP ' + r.status })) + '</div>';
        return;
      }
      var d = await r.json();
      if (gen !== _gen) return;
      _renderChatPane(d || {});
    } catch (e) {
      if (gen !== _gen) return;
      _bindTotal = 0; _updateServing();
      box.innerHTML = '<div class="psa-empty">' + _esc(_fmt(_t('psn_apply_fail', '操作失败：{err}'), { err: e })) + '</div>';
    }
  }

  function _renderChatPane(d) {
    var box = document.getElementById('psa-pane-chat');
    if (!box) return;
    var binds = d.bindings || [];
    var total = (typeof d.total === 'number') ? d.total : binds.length;
    _bindTotal = total;
    _updateServing();

    var html = '<div class="psa-sec-title">' + _esc(_t('psn_apply_bound_list', '已绑定会话')) + ' (' + total + ')</div>';
    if (!total) {
      html += '<div class="psa-empty">' + _esc(_t('psn_apply_no_bindings', '还没有绑定任何会话')) + '</div>';
    } else {
      // 按平台计数 chips
      var perPlat = {};
      for (var i = 0; i < binds.length; i++) {
        var pl = (binds[i] && binds[i].platform) || '?';
        perPlat[pl] = (perPlat[pl] || 0) + 1;
      }
      html += '<div class="psa-chips">';
      for (var k in perPlat) {
        if (Object.prototype.hasOwnProperty.call(perPlat, k)) {
          html += '<span class="psa-chip">' + _esc(k) + ' × ' + perPlat[k] + '</span>';
        }
      }
      html += '</div>';
      // 最多 20 行 chat_id
      var rows = binds.slice(0, 20);
      for (var m = 0; m < rows.length; m++) {
        var b = rows[m] || {};
        var cid = String(b.chat_id || '');
        var sub = String(b.platform || '') + (b.binding_type === 'inline' ? ' [inline]' : '');
        html += '<div class="psa-row">'
          + '<div class="psa-row-main">'
          +   '<div class="psa-row-name" title="' + _esc(cid) + '">' + _esc(cid) + '</div>'
          +   '<div class="psa-row-sub">' + _esc(sub) + '</div>'
          + '</div>'
          + '<button class="btn btn-sm btn-danger" data-act="unbind" data-cid="' + _esc(cid) + '">'
          + _esc(_t('psn_apply_unbind', '解绑')) + '</button>'
          + '</div>';
      }
      if (binds.length > 20) html += '<div class="psa-more">… +' + (binds.length - 20) + '</div>';
    }
    // 新增绑定输入区
    html += '<div class="psa-bind-bar">'
      + '<input id="psa-chat-input" type="text" placeholder="' + _esc(_t('psn_apply_chat_ph', '输入会话 ID（chat_id）')) + '" autocomplete="off">'
      + '<button class="btn btn-sm btn-primary" data-act="bind">' + _esc(_t('psn_apply_bind', '绑定')) + '</button>'
      + '</div>';
    box.innerHTML = html;

    var inp = document.getElementById('psa-chat-input');
    if (inp) {
      inp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          var b = box.querySelector('[data-act="bind"]');
          if (b && !b.disabled) _bind(b);
        }
      });
    }
  }

  // 绑定：先 GET 完整 persona，再 POST /api/persona/bind {chat_id, persona}（同 bindProfileToChatB）
  async function _bind(btn) {
    var inp = document.getElementById('psa-chat-input');
    var cid = inp ? String(inp.value || '').trim() : '';
    if (!cid) { if (inp) inp.focus(); return; }
    if (btn && btn.disabled) return;
    if (btn) btn.disabled = true;
    try {
      var persona = await _fetchPersona(_pid);
      var r = await fetch('/api/persona/bind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: cid, persona: persona })
      });
      var d = null;
      try { d = await r.json(); } catch (e2) { d = null; }
      if (d && d.ok) {
        _toastSafe(_t('psn_apply_ok_bind', '已绑定会话'), 'ok');
        _loadBindings();
        _refreshPage();
      } else {
        _fail((d && d.detail) || ('HTTP ' + r.status));
        if (btn) btn.disabled = false;
      }
    } catch (e) {
      _fail(e && e.message ? e.message : e);
      if (btn) btn.disabled = false;
    }
  }

  // 解绑：POST /api/persona/unbind {chat_id}（同 unbindChat / _unbindFromPreview）
  async function _unbind(cid, btn) {
    if (!cid || (btn && btn.disabled)) return;
    if (btn) btn.disabled = true;
    try {
      var r = await fetch('/api/persona/unbind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: cid })
      });
      if (!r.ok) {
        _fail('HTTP ' + r.status);
        if (btn) btn.disabled = false;
        return;
      }
      _toastSafe(_t('psn_apply_ok_unbind', '已解绑'), 'ok');
      _loadBindings();
      _refreshPage();
    } catch (e) {
      _fail(e);
      if (btn) btn.disabled = false;
    }
  }

  /* ═══ 分区 3：全局默认 ═══════════════════════════════════════════════ */
  function _renderDefaultPane() {
    var box = document.getElementById('psa-pane-default');
    if (!box) return;
    box.innerHTML = '<div class="psa-desc">'
      + _esc(_t('psn_apply_default_desc', '所有未绑定人设、未命中路由规则的会话，将统一用该人设兜底回复。'))
      + '</div>'
      + '<button class="btn btn-sm btn-primary" data-act="setdefault">'
      + _esc(_t('psn_apply_set_default', '设为全局默认')) + '</button>';
  }

  // 设为全局默认：GET 完整 persona → POST /api/persona/update-default {persona}（参照 saveDefaultPersona）
  async function _setDefault(btn) {
    if (btn && btn.disabled) return;
    var p = _findProfile(_pid) || {};
    var nm = p.name || _pid;
    var msg = _fmt(_t('psn_apply_default_confirm', '确认把「{name}」设为全局默认人设？'), { name: nm });
    if (!window.confirm(msg)) return;
    if (btn) btn.disabled = true;
    try {
      var persona = await _fetchPersona(_pid);
      var r = await fetch('/api/persona/update-default', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ persona: persona })
      });
      var d = null;
      try { d = await r.json(); } catch (e2) { d = null; }
      if (d && d.ok) {
        _toastSafe(_t('psn_apply_ok_default', '已设为全局默认'), 'ok');
        _refreshPage();
      } else {
        _fail((d && d.detail) || ('HTTP ' + r.status));
      }
    } catch (e) {
      _fail(e && e.message ? e.message : e);
    }
    if (btn) btn.disabled = false;
  }

  /* ── tab 切换 ─────────────────────────────────────────────────────────── */
  var PANE_ID = { account: 'psa-pane-account', chat: 'psa-pane-chat', 'default': 'psa-pane-default' };

  function _switchTab(name) {
    if (!PANE_ID[name]) return;
    var ov = document.getElementById('psa-ov');
    if (!ov) return;
    var tabs = ov.querySelectorAll('.psa-tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].className = 'psa-tab' + (tabs[i].getAttribute('data-tab') === name ? ' active' : '');
    }
    for (var k in PANE_ID) {
      if (Object.prototype.hasOwnProperty.call(PANE_ID, k)) {
        var el = document.getElementById(PANE_ID[k]);
        if (el) el.style.display = (k === name) ? '' : 'none';
      }
    }
  }

  /* ── 打开 / 关闭 ─────────────────────────────────────────────────────── */
  function _onKey(e) {
    if (e.key === 'Escape' || e.keyCode === 27) close();
  }

  function close() {
    _gen++;
    var ov = document.getElementById('psa-ov');
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
    if (_escBound) {
      document.removeEventListener('keydown', _onKey);
      _escBound = false;
    }
  }

  function _onRootClick(e) {
    var ov = document.getElementById('psa-ov');
    if (e.target === ov) { close(); return; }   // 点遮罩关闭
    var btn = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    if (act === 'close') close();
    else if (act === 'tab') _switchTab(btn.getAttribute('data-tab'));
    else if (act === 'assign') _assign(btn.getAttribute('data-plat'), btn.getAttribute('data-aid'), _pid, btn);
    else if (act === 'clear') _assign(btn.getAttribute('data-plat'), btn.getAttribute('data-aid'), '', btn);
    else if (act === 'unbind') _unbind(btn.getAttribute('data-cid'), btn);
    else if (act === 'bind') _bind(btn);
    else if (act === 'setdefault') _setDefault(btn);
  }

  function open(pid) {
    if (!pid) return;
    close();   // 幂等：先移除旧实例再重建
    _gen++;
    _pid = String(pid);
    _accCount = -1;
    _bindTotal = -1;
    _injectStyle();

    var p = _findProfile(_pid) || {};
    var roleHtml = p.role ? '<span class="psa-role">' + _esc(p.role) + '</span>' : '';

    var ov = document.createElement('div');
    ov.id = 'psa-ov';
    ov.className = 'pp-modal-ov open';
    ov.innerHTML = ''
      + '<div class="pp-modal" role="dialog" aria-modal="true">'
      +   '<div class="pp-modal-hd">'
      +     '<div class="psa-hd-main">'
      +       _avatarHTML(_pid, p)
      +       '<div class="psa-hd-txt">'
      +         '<div class="pp-modal-ttl">' + _esc(_t('psn_apply_title', '应用到…')) + ' · ' + _esc(p.name || _pid) + roleHtml + '</div>'
      +         '<div class="pp-modal-sub">' + _esc(_t('psn_apply_sub', '把这个人设用到账号 / 会话 / 全局兜底')) + '</div>'
      +         '<div class="pp-modal-sub" id="psa-serving">' + _esc(_t('psn_apply_loading', '加载中…')) + '</div>'
      +       '</div>'
      +     '</div>'
      +     '<button class="pp-modal-x" data-act="close">✕</button>'
      +   '</div>'
      +   '<div class="psa-tabs">'
      +     '<button class="psa-tab active" data-act="tab" data-tab="account">' + _esc(_t('psn_apply_tab_account', '账号默认')) + '</button>'
      +     '<button class="psa-tab" data-act="tab" data-tab="chat">' + _esc(_t('psn_apply_tab_chat', '绑定会话')) + '</button>'
      +     '<button class="psa-tab" data-act="tab" data-tab="default">' + _esc(_t('psn_apply_tab_default', '全局默认')) + '</button>'
      +   '</div>'
      +   '<div class="pp-modal-list">'
      +     '<div class="psa-pane" id="psa-pane-account"></div>'
      +     '<div class="psa-pane" id="psa-pane-chat" style="display:none"></div>'
      +     '<div class="psa-pane" id="psa-pane-default" style="display:none"></div>'
      +   '</div>'
      +   '<div class="pp-modal-foot">'
      +     '<span></span>'
      +     '<button class="btn btn-sm" data-act="close">' + _esc(_t('psn_close', '关闭')) + '</button>'
      +   '</div>'
      + '</div>';

    ov.addEventListener('click', _onRootClick);
    document.body.appendChild(ov);

    if (!_escBound) {
      document.addEventListener('keydown', _onKey);
      _escBound = true;
    }

    _renderDefaultPane();
    // 账号 / 绑定两路并行拉取
    _loadAccounts();
    _loadBindings();
  }

  /* ── 对外 API ─────────────────────────────────────────────────────────── */
  window.PSApply = { open: open };
})();
