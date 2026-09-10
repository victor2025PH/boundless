/* WSAppearance - agent-level appearance engine (chat theme / wallpaper / bubble
 * colors / night mode / text size / bubble radius / animations).
 *
 * Single renderer, two mounts:
 *   - unified_inbox.html quick panel (palette button, bottom-left rail)
 *   - /personal-settings full page (admin backend)
 *
 * State schema mirrors src/web/appearance_prefs.py (server-side sanitizer).
 * Persistence: localStorage 'ws_appearance_v1' (instant) + /api/workspace/prefs
 * {appearance} (roaming; server ignores the key until the instance restart that
 * ships the backend half -- pushes are harmless no-ops before that).
 *
 * Visual application = CSS custom properties on <html>; every consumer style in
 * unified-inbox.css declares the current default as var() fallback, so "engine
 * absent / state default" renders exactly the shipped look (zero regression).
 * i18n: labels resolved via window.T (inbox) or window.__apI18n (settings page).
 * ASCII-only source: UI strings all live in i18n packs, never hardcoded here.
 */
(function () {
  'use strict';
  var LS_KEY = 'ws_appearance_v1';
  var THEME_KEY = 'cp_theme';
  /* ?theme=dark|light = explicit embed pin (desktop-shell webview locks its theme
   * via URL param, see unified_inbox head script). Without a pin, boot-time
   * applyTheme() stomps the param with state.night.mode ('auto' by default) ->
   * "dark shell flashes back to light" race (found 2026-08-10 while probing).
   * The pin wins inside this window ONLY -- it must never reach THEME_KEY
   * (profile-wide channel); leaking it there made a pinned window re-broadcast
   * dark on every applyTheme and silently revert the admin day/night switch in
   * a sibling window of the same partition (fixed 2026-08-15, see applyTheme).
   * The pin is a boot DEFAULT, not a lock: an explicit user pick (setNightMode)
   * hands this window's theme over to the user and releases the pin right there
   * (window.cpReleaseThemePin, published by workspace_base head; local fallback
   * below when this engine runs standalone). Not releasing it = the segmented
   * control highlight moves and cp_theme is written, yet data-cp-theme never
   * changes -- exactly the "light/dark does nothing" report (fixed 2026-08-16,
   * desktop-shell webview loads /workspace?theme=dark).
   * Always read the pin through currentPin(): caching it in a closure var means
   * the stale value survives the release, which is the same as never releasing. */
  /* Release flag lives in sessionStorage on purpose: the pin is window-scoped, so its
   * release must be too (localStorage is partition-wide -> releasing here would unpin
   * the shell's other windows as well). Survives reloads of this window, gone on restart. */
  var PIN_OFF_KEY = 'cp_theme_pin_off';
  /* Q-10 B (#268 N9JQ3M / #267 4KEJ63, 2026-09-10): the pin is only a factory default
   * for users who never picked a theme. An explicit pick persists USER_SET_KEY in
   * localStorage (profile-wide, like cp_theme itself) so that ?theme= URLs -- desktop
   * shell webview, F5, in-app navigation, top pill, shell restart -- never act as a pin
   * again; the window-scoped sessionStorage release alone died with the window, which is
   * exactly the "switch back to dark after refresh / page change" report.
   * HANDOFF_KEY marks "schedule was left by a manual light/dark pick": the schedule chip
   * / user-menu row show "taken over manually" with a one-click way back to schedule. */
  var USER_SET_KEY = 'cp_theme_user_set';
  var HANDOFF_KEY = 'cp_theme_sched_handoff';
  var _urlPin = null;
  try {
    var _utp = new URLSearchParams(location.search).get('theme');
    if (_utp === 'dark' || _utp === 'light') _urlPin = _utp;
    if (_urlPin && sessionStorage.getItem(PIN_OFF_KEY) === '1') _urlPin = null;
    if (_urlPin && localStorage.getItem(USER_SET_KEY) === '1') _urlPin = null;
  } catch (_) {}
  function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (_) {} }
  function schedHandoff() { return lsGet(HANDOFF_KEY) === '1'; }
  function currentPin() {
    var wp = window.__cpThemePin;                    /* publisher (workspace_base) is authoritative */
    if (wp === 'dark' || wp === 'light') return wp;
    if (wp === null) return null;
    return _urlPin;                                  /* standalone: no publisher on the page */
  }
  function releasePin() {
    if (typeof window.cpReleaseThemePin === 'function') { window.cpReleaseThemePin(); }
    else { window.__cpThemePin = null; }
    _urlPin = null;
    try { sessionStorage.setItem(PIN_OFF_KEY, '1'); } catch (_) {}
    lsSet(USER_SET_KEY, '1');
  }

  /* ---- catalog (ids must stay in sync with appearance_prefs.py) ---- */
  var BUNDLES = [
    { id: 'brand',    out: '#2563eb', inL: '#ffffff', inD: '#23252c', wall: 'flat',  radius: 16 },
    { id: 'aurora',   out: '#0f766e', inL: '#ecf7f1', inD: '#1e2b26', wall: 'mist',  radius: 16 },
    { id: 'starry',   out: '#6d28d9', inL: '#eef0fb', inD: '#232338', wall: 'night', radius: 20 },
    { id: 'sunset',   out: '#c2410c', inL: '#fdf3ec', inD: '#2b221c', wall: 'dusk',  radius: 16 },
    { id: 'graphite', out: '#334155', inL: '#eef2f7', inD: '#24262c', wall: 'dots',  radius: 12 },
    { id: 'sakura',   out: '#be185d', inL: '#fdf1f6', inD: '#2b2027', wall: 'flat',  radius: 20 }
  ];
  var WALLS = {
    flat:  { l: '', d: '' },
    mist:  {
      l: 'radial-gradient(120% 90% at 15% 10%, #dbddbb 0%, transparent 55%), radial-gradient(110% 90% at 85% 20%, #d5d88d 0%, transparent 55%), radial-gradient(130% 100% at 20% 90%, #88b884 0%, transparent 60%), #6ba587',
      d: 'radial-gradient(120% 90% at 15% 10%, #263430 0%, transparent 55%), radial-gradient(110% 90% at 85% 20%, #1f3a2e 0%, transparent 55%), radial-gradient(130% 100% at 20% 90%, #16342b 0%, transparent 60%), #0f231d'
    },
    night: {
      l: 'radial-gradient(120% 90% at 20% 12%, #dbe4ff 0%, transparent 55%), radial-gradient(110% 90% at 82% 18%, #e6dcff 0%, transparent 55%), radial-gradient(130% 100% at 30% 92%, #c9d8ff 0%, transparent 60%), #dfe6f5',
      d: 'radial-gradient(120% 90% at 20% 12%, #1d2440 0%, transparent 55%), radial-gradient(110% 90% at 82% 18%, #2a2046 0%, transparent 55%), radial-gradient(130% 100% at 30% 92%, #16203a 0%, transparent 60%), #12172b'
    },
    dusk:  {
      l: 'radial-gradient(120% 90% at 18% 12%, #ffe8d2 0%, transparent 55%), radial-gradient(110% 90% at 84% 16%, #ffd6c2 0%, transparent 55%), radial-gradient(130% 100% at 70% 92%, #f6c1a7 0%, transparent 60%), #f3d9c3',
      d: 'radial-gradient(120% 90% at 18% 12%, #3a2a20 0%, transparent 55%), radial-gradient(110% 90% at 84% 16%, #38221c 0%, transparent 55%), radial-gradient(130% 100% at 70% 92%, #2e1d17 0%, transparent 60%), #241812'
    },
    dots:  {
      l: 'radial-gradient(rgba(15,23,42,.08) 1.2px, transparent 1.3px) 0 0/18px 18px, #eef1f5',
      d: 'radial-gradient(rgba(255,255,255,.07) 1.2px, transparent 1.3px) 0 0/18px 18px, #15161b'
    }
  };
  var ANIM_WALLS = { mist: 1, night: 1, dusk: 1 };
  var WALL_IDS = ['flat', 'mist', 'night', 'dusk', 'dots'];
  var RADII = [8, 12, 16, 20];
  var SIZES = [13, 14, 15, 16];
  var DEF = { v: 1, theme: 'brand', out: '', in: '', wall: 'flat', radius: 16, fs: 13,
              night: { mode: 'auto', start: 1320, end: 480 }, anim: true, ts: 0 };

  /* ---- tiny utils ---- */
  function T(k) {
    try { if (typeof window.T === 'function') { var v = window.T(k); if (v && v !== k) return v; } } catch (_) {}
    return (window.__apI18n || {})[k] || k;
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function bundleOf(id) {
    for (var i = 0; i < BUNDLES.length; i++) if (BUNDLES[i].id === id) return BUNDLES[i];
    return BUNDLES[0];
  }
  function hexRgb(h) {
    var m = /^#([0-9a-fA-F]{6})$/.exec(String(h || '').trim());
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgbHex(r, g, b) {
    function p(v) { v = Math.max(0, Math.min(255, Math.round(v))); return (v < 16 ? '0' : '') + v.toString(16); }
    return '#' + p(r) + p(g) + p(b);
  }
  function relLum(h) {
    var c = hexRgb(h); if (!c) return 0;
    function f(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }
  function contrast(a, b) {
    var x = relLum(a), y = relLum(b);
    var hi = Math.max(x, y), lo = Math.min(x, y);
    return (hi + 0.05) / (lo + 0.05);
  }
  function bestInk(bg) { return contrast('#ffffff', bg) >= contrast('#111827', bg) ? '#ffffff' : '#111827'; }
  /* Contrast guard: darken a custom bubble color until body text reaches AA 4.5.
     Returns {bg, fixed} -- fixed=true means we had to adjust (UI shows a hint). */
  function guardBg(bg) {
    var c = hexRgb(bg);
    if (!c) return { bg: bg, fixed: false };
    var cur = bg, fixed = false;
    for (var i = 0; i < 9; i++) {
      if (contrast(bestInk(cur), cur) >= 4.5) return { bg: cur, fixed: fixed };
      c = [c[0] * 0.9, c[1] * 0.9, c[2] * 0.9];
      cur = rgbHex(c[0], c[1], c[2]);
      fixed = true;
    }
    return { bg: cur, fixed: fixed };
  }
  function minToHM(m) {
    m = Math.max(0, Math.min(1439, m | 0));
    var h = Math.floor(m / 60), mm = m % 60;
    return (h < 10 ? '0' : '') + h + ':' + (mm < 10 ? '0' : '') + mm;
  }
  function hmToMin(s, dflt) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(String(s || ''));
    if (!m) return dflt;
    var v = (+m[1]) * 60 + (+m[2]);
    return (v >= 0 && v < 1440) ? v : dflt;
  }
  function toast(msg) {
    try { if (typeof window._toast === 'function') { window._toast(msg); return; } } catch (_) {}
    try {
      var el = document.createElement('div');
      el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:9999;' +
        'background:rgba(15,23,42,.88);color:#fff;font-size:12.5px;padding:7px 14px;border-radius:8px;';
      el.textContent = msg;
      document.body.appendChild(el);
      setTimeout(function () { try { el.remove(); } catch (_) {} }, 1800);
    } catch (_) {}
  }

  /* ---- state ---- */
  var state = clone(DEF);
  var _lastGuardFixed = false;
  try {
    var raw = localStorage.getItem(LS_KEY);
    if (raw) {
      var got = JSON.parse(raw);
      if (got && got.v === 1) {
        state = clone(DEF);
        for (var k in got) if (Object.prototype.hasOwnProperty.call(got, k)) state[k] = got[k];
        if (!got.night || typeof got.night !== 'object') state.night = clone(DEF.night);
      }
    }
  } catch (_) {}

  function isDarkNow() {
    return (document.documentElement.getAttribute('data-cp-theme') || 'light') === 'dark';
  }
  function sysDark() {
    try { return window.matchMedia && window.matchMedia('(prefers-color-scheme:dark)').matches; } catch (_) { return false; }
  }
  function reducedMotion() {
    try { return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
  }
  function inNightWindow(n) {
    var d = new Date(), m = d.getHours() * 60 + d.getMinutes();
    return n.start <= n.end ? (m >= n.start && m < n.end) : (m >= n.start || m < n.end);
  }
  function schedEffective() { return inNightWindow(state.night) ? 'dark' : 'light'; }

  /* ---- application ---- */
  function applyVars() {
    var html = document.documentElement, st = html.style;
    var b = bundleOf(state.theme);
    var dark = isDarkNow();
    var g = guardBg(state.out && hexRgb(state.out) ? state.out : b.out);
    var outBg = g.bg;
    var inRaw = (state['in'] && hexRgb(state['in'])) ? state['in'] : (dark ? b.inD : b.inL);
    var gi = guardBg(inRaw);
    _lastGuardFixed = g.fixed || gi.fixed;
    st.setProperty('--bbl-out-bg', outBg);
    st.setProperty('--bbl-out-fg', bestInk(outBg));
    st.setProperty('--bbl-in-bg', gi.bg);
    st.setProperty('--bbl-in-fg', bestInk(gi.bg));
    st.setProperty('--bbl-radius', state.radius + 'px');
    st.setProperty('--bbl-fs', state.fs + 'px');
    var w = WALLS[state.wall] || WALLS.flat;
    var wallCss = dark ? w.d : w.l;
    if (wallCss) st.setProperty('--chat-wall', wallCss); else st.removeProperty('--chat-wall');
    html.classList.toggle('ws-wall-on', !!wallCss);
    html.classList.toggle('ws-wall-anim', !!(ANIM_WALLS[state.wall] && state.anim && !reducedMotion()));
    html.classList.toggle('ws-anim-off', !state.anim);
  }
  function applyTheme() {
    /* 写进 THEME_KEY 的必须是**治理值**（night.mode 折算），绝不含 URL 钉：
       THEME_KEY 是跨窗口共享键，把窗口级的钉写进去 → 每次 applyTheme 都会把它
       重新广播出去，同 profile 里另一个后台窗口刚点的日/夜在 150ms 内被盖回
       （2026-08-15 实锤「后台切换白天晚上点了没反应」的根因）。
       钉只在**本窗口**生效：走页面侧 cpApplyTheme（workspace_base/unified_inbox
       的 eff() 已「钉优先」），无该钩子时下面 eff 也按钉解析。 */
    var mode = (state.night && state.night.mode) || 'auto';
    var want = (mode === 'schedule') ? schedEffective() : mode;
    try { localStorage.setItem(THEME_KEY, want); } catch (_) {}
    if (typeof window.cpApplyTheme === 'function') { window.cpApplyTheme(); }
    else {
      var eff = currentPin() || ((want === 'auto') ? (sysDark() ? 'dark' : 'light') : want);
      document.documentElement.setAttribute('data-cp-theme', eff);
    }
    logThemeChange(mode);
  }
  /* Q-10 B: one console line per effective change ("theme change source=... value=... mode=...").
   * source: manual (user pick) | schedule (night window tick) | system (prefers-color-scheme)
   *       | restore (boot from localStorage) | sync (roaming pull / other-tab storage event).
   * Manual picks always log (even a no-op re-pick is a user action worth seeing); the
   * others only when the painted value or mode actually changed, so the 60s tick is silent. */
  var _themeSrc = 'restore';
  var _lastThemeLog = '';
  function logThemeChange(mode) {
    var value = '';
    try { value = document.documentElement.getAttribute('data-cp-theme') || ''; } catch (_) {}
    var line = _themeSrc + '|' + value + '|' + mode;
    if (_themeSrc !== 'manual' && line === _lastThemeLog) return;
    _lastThemeLog = line;
    try { console.info('theme change source=%s value=%s mode=%s', _themeSrc, value, mode); } catch (_) {}
  }
  function applyAll(withTransition, src) {
    _themeSrc = src || 'sync';
    var run = function () { applyTheme(); applyVars(); };
    if (withTransition && state.anim && !reducedMotion() && document.startViewTransition) {
      try { document.startViewTransition(run); return; } catch (_) {}
    }
    run();
  }

  /* ---- persistence ---- */
  var _pushT = null;
  function save(opts) {
    opts = opts || {};
    if (!opts.silent) state.ts = Date.now();
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (_) {}
    if (!opts.noPush) {
      clearTimeout(_pushT);
      _pushT = setTimeout(pushSrv, 900);
    }
  }
  function pushSrv() {
    try {
      fetch('/api/workspace/prefs', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ appearance: state })
      }).catch(function () {});
    } catch (_) {}
  }
  function pullSrv() {
    try {
      fetch('/api/workspace/prefs', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          var ap = d && d.prefs && d.prefs.appearance;
          if (ap && typeof ap === 'object' && ap.v === 1 && (ap.ts || 0) > (state.ts || 0)) {
            state = clone(DEF);
            for (var k in ap) if (Object.prototype.hasOwnProperty.call(ap, k)) state[k] = ap[k];
            if (!ap.night || typeof ap.night !== 'object') state.night = clone(DEF.night);
            save({ silent: true, noPush: true });
            applyAll(false, 'sync');
            syncAllMounts();
          }
        }).catch(function () {});
    } catch (_) {}
  }

  /* ---- panel renderer (shared by popover + full page) ---- */
  var _mounts = [];
  function chipRow(role, items) {
    var h = '<div class="wsap-chips" data-role="' + role + '">';
    for (var i = 0; i < items.length; i++) h += items[i];
    return h + '</div>';
  }
  function swatch(css) {
    return '<span class="wsap-sw" style="background:' + css + '"></span>';
  }
  function renderInto(host, opts) {
    opts = opts || {};
    var b, i, chips = [];
    for (i = 0; i < BUNDLES.length; i++) {
      b = BUNDLES[i];
      chips.push('<button type="button" class="wsap-chip" data-ap-theme="' + b.id + '">' +
        swatch(b.out) + T('ap.theme.' + b.id) + '</button>');
    }
    var wallChips = [];
    for (i = 0; i < WALL_IDS.length; i++) {
      var wid = WALL_IDS[i], wc = WALLS[wid];
      wallChips.push('<button type="button" class="wsap-chip" data-ap-wall="' + wid + '">' +
        swatch(wc.l || 'var(--bg-chat,#f3f4f6)') + T('ap.wall.' + wid) + '</button>');
    }
    var nightChips = [];
    var modes = ['auto', 'light', 'dark', 'schedule'];
    for (i = 0; i < modes.length; i++) {
      nightChips.push('<button type="button" class="wsap-chip" data-ap-night="' + modes[i] + '">' +
        T('ap.night.' + modes[i]) +
        (modes[i] === 'schedule' ? '<span data-role="handoff" style="font-size:10px;opacity:.8"></span>' : '') +
        '</button>');
    }
    var fsChips = [], rdChips = [];
    for (i = 0; i < SIZES.length; i++) fsChips.push('<button type="button" class="wsap-chip" data-ap-fs="' + SIZES[i] + '">' + SIZES[i] + 'px</button>');
    for (i = 0; i < RADII.length; i++) rdChips.push('<button type="button" class="wsap-chip" data-ap-radius="' + RADII[i] + '">' + RADII[i] + 'px</button>');

    host.innerHTML =
      (opts.header === false ? '' :
        '<div class="wsap-hd"><span>' + T('ap.pop.title') + '</span>' +
        '<button type="button" class="wsap-x" data-ap-act="close" aria-label="close">&#10005;</button></div>') +
      '<div class="wsap-roam">' + T('ap.pop.roam') + '</div>' +
      '<div class="wsap-sec">' + T('ap.sec.theme') + '</div>' + chipRow('theme', chips) +
      '<div class="wsap-sec">' + T('ap.sec.wall') + '</div>' + chipRow('wall', wallChips) +
      '<div class="wsap-sec">' + T('ap.sec.custom') + '</div>' +
      '<div class="wsap-row wsap-custom">' +
        '<label class="wsap-clr">' + T('ap.custom.out') + ' <input type="color" data-ap-color="out"></label>' +
        '<label class="wsap-clr">' + T('ap.custom.in') + ' <input type="color" data-ap-color="in"></label>' +
        '<button type="button" class="wsap-mini" data-ap-act="resetColors">' + T('ap.custom.reset') + '</button>' +
      '</div>' +
      '<div class="wsap-guard" data-role="guard" hidden>' + T('ap.guard.fixed') + '</div>' +
      '<div class="wsap-sec">' + T('ap.sec.night') + '</div>' + chipRow('night', nightChips) +
      '<div class="wsap-row wsap-sched" data-role="sched" hidden>' +
        '<label>' + T('ap.night.from') + ' <input type="time" data-ap-time="start"></label>' +
        '<label>' + T('ap.night.to') + ' <input type="time" data-ap-time="end"></label>' +
      '</div>' +
      '<div class="wsap-grid2">' +
        '<div><div class="wsap-sec">' + T('ap.sec.fs') + '</div>' + chipRow('fs', fsChips) +
          '<div class="wsap-hint">' + T('ap.fs.hint') + '</div></div>' +
        '<div><div class="wsap-sec">' + T('ap.sec.radius') + '</div>' + chipRow('radius', rdChips) + '</div>' +
      '</div>' +
      '<div class="wsap-row wsap-anim"><label class="wsap-anim-l">' +
        '<input type="checkbox" data-ap-anim> ' + T('ap.anim.label') + '</label></div>' +
      '<div class="wsap-ft">' +
        '<button type="button" class="wsap-mini" data-ap-act="resetAll">' + T('ap.reset_all') + '</button>' +
        '<span class="wsap-sp"></span>' +
        (opts.allLink === false ? '' :
          /* Named-window reuse: land in the ONE admin-backend window instead of
           * spawning a fresh tab per click (multiwin governance, 2026-08-14).
           * #appearance hash marks it as a deep link so _win_unique navigates the
           * already-open admin window instead of focus-only. __openUnique stub is
           * always defined on workspace pages (_win_unique.html); target=_blank
           * stays as the degraded-path fallback (popup blocked / desktop shell). */
          '<a class="wsap-link" href="/personal-settings#appearance" data-winname="admin" ' +
          'target="_blank" rel="noopener" ' +
          'onclick="window.WSAppearance&&WSAppearance._bcn&&WSAppearance._bcn(\'theme_all_settings_qp\');return window.__openUnique?window.__openUnique(event,this):true">' +
          T('ap.all_settings') + ' &#8594;</a>') +
      '</div>';

    if (!host.__apWired) {
      host.__apWired = 1;
      host.addEventListener('click', function (e) {
        var el = e.target.closest ? e.target.closest('[data-ap-theme],[data-ap-wall],[data-ap-night],[data-ap-fs],[data-ap-radius],[data-ap-act]') : null;
        if (!el) return;
        if (el.hasAttribute('data-ap-theme')) {
          var nb = bundleOf(el.getAttribute('data-ap-theme'));
          state.theme = nb.id; state.wall = nb.wall; state.radius = nb.radius;
          state.out = ''; state['in'] = '';
          save(); applyAll(true); syncAllMounts();
        } else if (el.hasAttribute('data-ap-wall')) {
          state.wall = el.getAttribute('data-ap-wall');
          save(); applyAll(false); syncAllMounts();
        } else if (el.hasAttribute('data-ap-night')) {
          /* Q-10 B: the quick-panel chips used to write state.night.mode directly, bypassing
           * setNightMode -- so a pinned window (desktop shell ?theme=dark) saw the chip
           * highlight move while data-cp-theme never changed, and leaving schedule gave no
           * exit toast / handoff mark. Single write path, like every other entry point. */
          setNightMode(el.getAttribute('data-ap-night'));
        } else if (el.hasAttribute('data-ap-fs')) {
          state.fs = parseInt(el.getAttribute('data-ap-fs'), 10) || 13;
          save(); applyAll(false); syncAllMounts();
        } else if (el.hasAttribute('data-ap-radius')) {
          state.radius = parseInt(el.getAttribute('data-ap-radius'), 10) || 16;
          save(); applyAll(false); syncAllMounts();
        } else {
          var act = el.getAttribute('data-ap-act');
          if (act === 'close') { closePop(); }
          else if (act === 'resetColors') {
            state.out = ''; state['in'] = '';
            save(); applyAll(false); syncAllMounts();
          } else if (act === 'resetAll') {
            state = clone(DEF);
            releasePin(); lsDel(HANDOFF_KEY);
            save(); applyAll(true, 'manual'); syncAllMounts();
            toast(T('ap.saved'));
          }
        }
      });
      host.addEventListener('input', function (e) {
        var t = e.target;
        if (t.hasAttribute('data-ap-color')) {
          state[t.getAttribute('data-ap-color') === 'out' ? 'out' : 'in'] = t.value;
          save(); applyAll(false); syncAllMounts(t);
        } else if (t.hasAttribute('data-ap-time')) {
          var key = t.getAttribute('data-ap-time');
          state.night[key] = hmToMin(t.value, DEF.night[key]);
          save(); applyAll(false);
        } else if (t.hasAttribute('data-ap-anim')) {
          state.anim = !!t.checked;
          save(); applyAll(false); syncAllMounts(t);
        }
      });
    }
    _mounts.push(host);
    syncMount(host);
  }
  function syncMount(host, skipEl) {
    if (!host || !host.isConnected) return;
    var b = bundleOf(state.theme), dark = isDarkNow();
    host.querySelectorAll('[data-ap-theme]').forEach(function (el) {
      el.classList.toggle('on', el.getAttribute('data-ap-theme') === state.theme && !state.out && !state['in']);
    });
    host.querySelectorAll('[data-ap-wall]').forEach(function (el) {
      el.classList.toggle('on', el.getAttribute('data-ap-wall') === state.wall);
    });
    var nmode = state.night.mode || 'auto';
    host.querySelectorAll('[data-ap-night]').forEach(function (el) {
      el.classList.toggle('on', el.getAttribute('data-ap-night') === nmode);
    });
    var ho = host.querySelector('[data-role="handoff"]');
    if (ho) ho.textContent = (nmode !== 'schedule' && schedHandoff()) ? T('ap.night.handoff') : '';
    host.querySelectorAll('[data-ap-fs]').forEach(function (el) {
      el.classList.toggle('on', parseInt(el.getAttribute('data-ap-fs'), 10) === state.fs);
    });
    host.querySelectorAll('[data-ap-radius]').forEach(function (el) {
      el.classList.toggle('on', parseInt(el.getAttribute('data-ap-radius'), 10) === state.radius);
    });
    var oc = host.querySelector('input[data-ap-color="out"]');
    var ic = host.querySelector('input[data-ap-color="in"]');
    if (oc && oc !== skipEl) oc.value = (state.out && hexRgb(state.out)) ? state.out : b.out;
    if (ic && ic !== skipEl) ic.value = (state['in'] && hexRgb(state['in'])) ? state['in'] : (dark ? b.inD : b.inL);
    var sched = host.querySelector('[data-role="sched"]');
    if (sched) {
      sched.hidden = (state.night.mode !== 'schedule');
      var si = sched.querySelector('input[data-ap-time="start"]');
      var ei = sched.querySelector('input[data-ap-time="end"]');
      if (si && si !== skipEl) si.value = minToHM(state.night.start);
      if (ei && ei !== skipEl) ei.value = minToHM(state.night.end);
    }
    var ac = host.querySelector('input[data-ap-anim]');
    if (ac && ac !== skipEl) ac.checked = !!state.anim;
    var gd = host.querySelector('[data-role="guard"]');
    if (gd) gd.hidden = !_lastGuardFixed;
  }
  function syncAllMounts(skipEl) {
    _mounts = _mounts.filter(function (h) { return h && h.isConnected; });
    _mounts.forEach(function (h) { syncMount(h, skipEl); });
  }

  /* ---- quick popover (inbox) ---- */
  function popEl() { return document.getElementById('wsap-pop'); }
  function closePop() { var p = popEl(); if (p) p.classList.remove('show'); }
  function openPop(ev) {
    var p = popEl();
    if (!p) return;
    if (p.classList.contains('show')) { closePop(); return; }
    if (!p.__apRendered) { renderInto(p, { header: true, allLink: true }); p.__apRendered = 1; }
    else syncMount(p);
    p.classList.add('show');
    try {
      var btn = (ev && (ev.currentTarget || ev.target)) || null;
      var r = btn && btn.getBoundingClientRect ? btn.getBoundingClientRect() : { right: 60, bottom: window.innerHeight - 60, top: window.innerHeight - 60 };
      var w = p.offsetWidth || 316, h = p.offsetHeight || 420;
      var x = Math.max(8, Math.min(r.right + 10, window.innerWidth - w - 8));
      var y = Math.max(8, Math.min(r.bottom - h, window.innerHeight - h - 8));
      p.style.left = x + 'px'; p.style.top = y + 'px';
    } catch (_) {}
  }
  document.addEventListener('click', function (e) {
    var p = popEl();
    if (!p || !p.classList.contains('show')) return;
    if (e.target.closest && (e.target.closest('#wsap-pop') || e.target.closest('#cp-theme-btn'))) return;
    closePop();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePop(); });

  /* ---- theme-change hook + night schedule tick + cross-tab sync ---- */
  function hookThemeApply() {
    var orig = window.cpApplyTheme;
    if (typeof orig === 'function' && !orig.__wsAp) {
      var wrapped = function () { orig(); try { applyVars(); } catch (_) {} };
      wrapped.__wsAp = 1;
      window.cpApplyTheme = wrapped;
    }
  }
  setInterval(function () {
    if ((state.night && state.night.mode) === 'schedule') {
      var want = schedEffective();
      var cur = localStorage.getItem(THEME_KEY);
      if (cur !== want) { applyAll(true, 'schedule'); }
    }
  }, 60000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (state.night && state.night.mode) === 'schedule') applyAll(false, 'schedule');
  });
  /* prefers-color-scheme flip while in auto: repaint + log source=system (workspace_base's own
   * listener only repaints; it does not know the engine's mode nor the log line). */
  try {
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme:dark)').addEventListener('change', function () {
        if (((state.night && state.night.mode) || 'auto') === 'auto') { applyAll(false, 'system'); syncAllMounts(); }
      });
    }
  } catch (_) {}
  window.addEventListener('storage', function (e) {
    if (e.key === LS_KEY && e.newValue) {
      try {
        var got = JSON.parse(e.newValue);
        if (got && got.v === 1 && (got.ts || 0) >= (state.ts || 0)) {
          state = clone(DEF);
          for (var k in got) if (Object.prototype.hasOwnProperty.call(got, k)) state[k] = got[k];
          applyAll(false, 'sync'); syncAllMounts();
        }
      } catch (_) {}
    }
    if (e.key === HANDOFF_KEY || e.key === USER_SET_KEY) syncAllMounts();
  });

  /* ---- night-mode single write path ----
   * Every theme entry point (user-menu segmented control, quick panel, settings
   * page, legacy cpCycleTheme delegate) MUST go through setNightMode: it is the
   * only writer that keeps state.night.mode + cp_theme + server roaming + all
   * mounted panels in sync. Raw localStorage('cp_theme') writes get stomped by
   * this engine on the next applyTheme() -- that was the original two-switcher
   * conflict this API exists to kill (2026-08-14). */
  /* 用量埋点（theme_ 前缀）：打在单一写路径上＝分段控件/快捷面板/设置页/legacy
   * cycle 全入口一网打尽；漫游回拉（pullSrv）与启动装载不经过这里，零噪声。
   * 读数：/api/admin/ui-event-trend?prefix=theme_（ops.ui_event_trend 开时落库）。 */
  /* Q-10 B: light/dark/auto while scheduled = manual takeover -> HANDOFF_KEY set (schedule
   * chip / user-menu row show "overridden manually" + one-click resume); picking schedule
   * again clears it. Also persists USER_SET_KEY via releasePin() so ?theme= URLs stop
   * re-pinning this profile after the first explicit pick. */
  function markHandoff(was, mode) {
    if (mode === 'schedule') lsDel(HANDOFF_KEY);
    else if (was === 'schedule') lsSet(HANDOFF_KEY, '1');
  }
  function bcn(action) {
    try {
      navigator.sendBeacon('/api/telemetry/ui-event', new Blob(
        [JSON.stringify({ page: (location && location.pathname) || '', action: String(action) })],
        { type: 'application/json' }));
    } catch (_) {}
  }
  function setNightMode(mode) {
    if (['auto', 'light', 'dark', 'schedule'].indexOf(mode) < 0) return false;
    /* Explicit pick takes over from the boot pin: release BEFORE the no-op shortcut --
     * "already auto + pinned dark" is exactly where the user sees nothing happen. */
    releasePin();
    var was = (state.night && state.night.mode) || 'auto';
    markHandoff(was, mode);
    if (was === mode) { applyAll(false, 'manual'); return true; }
    state.night.mode = mode;
    save(); applyAll(true, 'manual'); syncAllMounts();
    bcn('theme_set_' + mode);
    if (was === 'schedule') toast(T('ap.sched_exit'));
    return true;
  }
  function cycleNight() {
    var order = ['auto', 'light', 'dark'];
    var cur = (state.night && state.night.mode) || 'auto';
    /* schedule counts as position -1 -> next click lands on 'auto' (leaving the
     * special mode toward the default, with an explicit exit toast) */
    var next = order[(order.indexOf(cur) + 1) % order.length];
    setNightMode(next);
    return next;
  }

  /* ---- public API ---- */
  window.WSAppearance = {
    open: openPop,
    close: closePop,
    mount: function (host, opts) { renderInto(host, opts || {}); },
    get: function () { return clone(state); },
    setNightMode: setNightMode,
    cycleNight: cycleNight,
    applyAll: applyAll,
    _bcn: bcn,          /* 快捷面板内联 onclick 消费（theme_ 埋点），别删 */
    _bundles: BUNDLES,
    _walls: WALLS
  };

  function boot() {
    hookThemeApply();
    applyAll(false, 'restore');
    pullSrv();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
