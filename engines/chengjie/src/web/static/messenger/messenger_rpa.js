/* Messenger 渠道页主脚本（P2-3 自 _channel_body_messenger.html 原样外迁，2026-08-02）。
 *
 * 纪律：
 * - 本文件以浏览器全局作用域执行，与原内联 <script> 完全同语义（同位置同步加载）；
 *   内联 onclick 依赖的函数经文件尾 Object.assign(window, …) 暴露，勿删。
 * - i18n 一律 window.T/Tf 键（禁中文字面量）；键门禁见 tests/test_i18n_coverage.py
 *   ::test_static_js_window_t_keys_resolve（静态 JS 与模板同网）。
 * - 改动本文件后必须 bump 模板引用处的 ?v= 缓存戳 + python scripts/bump_ui_build.py
 *   （浏览器缓存与陈旧标签页是两套失效机制，都要动）。
 * - 哑按钮门禁经 tests/_inline_handler_scan.local_static_globals 解析本文件，
 *   新增内联 handler 目标函数须顶层定义或挂 window。
 */
(function(){
  const state = window.state = {
    status: null,
    config: null,
    approvals: [],
    templates: null,
    runs: [],
    accounts: {},
    bindings: null,
    media: null,
    funnel: null,
    leads: [],
    leadSummary: null,
    selectedLeadKey: '',
    leadDetail: null,
    personas: null,
    strategy: null,
    personaJsonVisible: false,
    editingPersonaId: '',
    editingAccountId: '',
    mobileAuto: null,
    poolFilter: 'all',
    poolHost: 'all',
    poolSize: 'normal',
    poolAdvanced: false,
    poolSelected: new Set(),
  };
  const _refreshInflight = {};
  const _refreshLastAt = {};
  let _lightRefreshPromise = null;

  const qs = (s, root=document) => root.querySelector(s);
  const qsa = (s, root=document) => Array.from(root.querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
  const num = (v, d=0) => Number.isFinite(Number(v)) ? Number(v) : d;
  const fmtTs = (ts) => {
    if (!ts) return '--';
    if (Number.isNaN(new Date(Number(ts) * 1000).getTime())) return '--';
    return window.wsFmtDateTime(Number(ts), {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  };
  const fmtDuration = (sec) => {
    sec = Math.max(0, Math.floor(Number(sec || 0)));
    if (!sec) return '--';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return window.Tf('msg_js_p01',{d:d,h:h});
    if (h > 0) return window.Tf('msg_js_p02',{h:h,m:m});
    return window.Tf('msg_js_p03',{m:Math.max(1, m)});
  };
  const short = (s, n=90) => {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1) + '...' : s;
  };
  const splitCsv = (s) => String(s || '').split(',').map(x => x.trim()).filter(Boolean);
  const joinCsv = (v) => Array.isArray(v) ? v.join(', ') : String(v || '');
  // P11-A: 语言旗帜映射
  const _LANG_FLAGS = {
    'zh-cn':'🇨🇳','zh':'🇨🇳','en':'🇺🇸','de':'🇩🇪','ja':'🇯🇵','ko':'🇰🇷',
    'fr':'🇫🇷','es':'🇪🇸','ar':'🇸🇦','ru':'🇷🇺','hi':'🇮🇳',
    'it':'🇮🇹','pt':'🇧🇷','nl':'🇳🇱','pl':'🇵🇱','tr':'🇹🇷','cs':'🇨🇿','hu':'🇭🇺',
  };
  const _langBadge = (rl) => rl
    ? `<span class="mr-lang-badge" style="font-size:.66rem;padding:.06rem .28rem" title="${window.T('psn_reply_language')}">${_LANG_FLAGS[rl]||'🌐'} ${esc(rl)}</span>`
    : '';

  // B6-P6: toast 升级 — 4 类型 + icon + 关闭按钮 + 错误同步入历史
  const ICONS = {ok:'✓', err:'✕', warn:'⚠', info:'ⓘ'};
  function toast(msg, type='ok'){
    if (type === 'error') type = 'err';  // 兼容旧代码
    const el = qs('#mr-toast');
    if (!el) return;
    const icon = ICONS[type] || '';
    el.className = 'mr-toast show ' + type;
    el.innerHTML = `<span class="tx-close" onclick="this.parentElement.className='mr-toast'">×</span>` +
                   `<span class="tx-icon">${icon}</span>${esc(String(msg))}`;
    clearTimeout(toast._timer);
    const dur = type === 'err' ? 6000 : 3200;
    toast._timer = setTimeout(() => el.className = 'mr-toast', dur);
    if (type === 'err') errLog.push(String(msg));
  }
  window.toast = toast;

  // B6-P6: 错误历史日志（错误持久化到 #err-panel，运营可回查）
  const errLog = {
    items: [],  // {ts, msg, ctx}
    push(msg, ctx) {
      this.items.unshift({ts: Date.now(), msg: String(msg), ctx: ctx || ''});
      if (this.items.length > 50) this.items.pop();
      this.render();
    },
    clear() { this.items = []; this.render(); },
    render() {
      const bell = qs('#err-bell');
      const badge = qs('#err-bell-badge');
      const list = qs('#err-panel-list');
      if (!bell || !list) return;
      bell.classList.toggle('has-errs', this.items.length > 0);
      if (badge) badge.textContent = this.items.length;
      list.innerHTML = this.items.length ? this.items.map(e =>
        `<div class="err-item">
          <div class="ts">${window.wsFmtTime(e.ts, {hour12:false})}</div>
          <div class="msg">${esc(e.msg)}</div>
          ${e.ctx ? `<div class="ctx">${esc(e.ctx)}</div>` : ''}
        </div>`
      ).join('') : '<div class="err-item" style="color:var(--th-ink-slate5,#64748b);text-align:center">'+window.T('msg_js_001')+'</div>';
    },
  };
  window.errLog = errLog;

  // B6-P6: 通用确认对话框（promise 化 — 替代 native confirm）
  function confirm2(opts) {
    if (typeof opts === 'string') opts = {message: opts};
    const title = opts.title || window.T('msg_s401');
    const message = opts.message || '';
    const okText = opts.okText || window.T('rpa_fn_ack');
    const cancelText = opts.cancelText || window.T('ov_js_cancel');
    const danger = !!opts.danger;
    return new Promise(resolve => {
      const mask = qs('#cfm-mask');
      qs('#cfm-title').textContent = title;
      qs('#cfm-msg').innerHTML = esc(message).replace(/\n/g, '<br>');
      const okBtn = qs('#cfm-ok');
      const cancelBtn = qs('#cfm-cancel');
      okBtn.textContent = okText;
      okBtn.className = 'cfm-btn ' + (danger ? 'danger' : 'primary');
      cancelBtn.textContent = cancelText;
      mask.classList.add('show');
      const cleanup = (val) => {
        mask.classList.remove('show');
        okBtn.onclick = null; cancelBtn.onclick = null;
        document.removeEventListener('keydown', escHandler);
        resolve(val);
      };
      okBtn.onclick = () => cleanup(true);
      cancelBtn.onclick = () => cleanup(false);
      const escHandler = (e) => { if (e.key === 'Escape') cleanup(false); };
      document.addEventListener('keydown', escHandler);
    });
  }
  window.confirm2 = confirm2;

  // B6-P6: api() 升级 — 15s 超时 + 401/403/5xx/network 友好诊断 + 错误自动入历史
  async function api(path, opts={}){
    if (!navigator.onLine) {
      const e = new Error(window.T('msg_js_002')+' — '+window.T('msg_js_003'));
      errLog.push(e.message, path);
      throw e;
    }
    const init = Object.assign({credentials:'same-origin'}, opts);
    if (init.body && typeof init.body !== 'string') {
      init.headers = Object.assign({'Content-Type':'application/json'}, init.headers || {});
      init.body = JSON.stringify(init.body);
    }
    const ctrl = new AbortController();
    init.signal = ctrl.signal;
    const timeoutMs = opts.timeoutMs || 15000;
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    let r;
    try {
      r = await apiFetch(path, init);
    } catch(netErr) {
      clearTimeout(t);
      const isAbort = netErr.name === 'AbortError';
      const friendly = isAbort
        ? `${window.T('msg_js_255')} (>${timeoutMs/1000}s) — ${window.T('msg_js_256')}`
        : window.Tf('msg_js_p04',{e:netErr.message || 'fetch failed'});
      const e = new Error(friendly);
      errLog.push(friendly, path);
      throw e;
    }
    clearTimeout(t);
    if (!r.ok) {
      let detail = '';
      try { const b = await r.json(); detail = b.detail || b.error || ''; } catch(e) {}
      let friendly;
      if (r.status === 401) friendly = window.T('inbox.skel.session_expired');
      else if (r.status === 403) friendly = window.T('msg_js_004') + (detail ? `：${detail}` : '');
      else if (r.status === 404) friendly = window.Tf('msg_js_p05',{path:path});
      else if (r.status === 503) friendly = window.T('msg_js_005') + (detail ? `：${detail}` : '');
      else if (r.status >= 500) friendly = window.Tf('msg_js_p06',{s:r.status}) + (detail ? `：${detail}` : '');
      else friendly = detail || `${path} ${r.status}`;
      errLog.push(friendly, path);
      throw new Error(friendly);
    }
    return r.json();
  }
  const apiGet = (p, opts={}) => api(p, opts);
  const apiPost = (p, body={}, opts={}) => api(p, Object.assign({method:'POST', body}, opts));
  const apiPut = (p, body={}, opts={}) => api(p, Object.assign({method:'PUT', body}, opts));
  const apiDelete = (p, opts={}) => api(p, Object.assign({method:'DELETE'}, opts));
  window.apiGet = apiGet; window.apiPost = apiPost; window.apiPut = apiPut; window.apiDelete = apiDelete;

  // B6-P6: 离线检测 — 顶部红条
  window.addEventListener('online', () => { qs('#offline-banner')?.style && (qs('#offline-banner').style.display='none'); toast('✓ '+window.T('msg_js_006'), 'ok'); });
  window.addEventListener('offline', () => { qs('#offline-banner')?.style && (qs('#offline-banner').style.display='block'); toast('⚠ '+window.T('msg_js_002'), 'err'); });

  function activeTab(){
    return qs('.mr-tab.active')?.dataset.tab || 'overview';
  }

  function refreshOnce(key, fn, opts={}){
    const force = !!opts.force;
    const minMs = Number(opts.minMs || 0);
    const now = Date.now();
    if (_refreshInflight[key]) return _refreshInflight[key];
    if (!force && minMs > 0 && now - (_refreshLastAt[key] || 0) < minMs) {
      return Promise.resolve({skipped:true});
    }
    _refreshLastAt[key] = now;
    _refreshInflight[key] = Promise.resolve()
      .then(fn)
      .finally(() => { delete _refreshInflight[key]; });
    return _refreshInflight[key];
  }

  function settleVisible(tasks){
    return Promise.allSettled(tasks.filter(Boolean));
  }

  function setTab(tab){
    qsa('.mr-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    qsa('.mr-view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
    qs('.mr-page')?.classList.toggle('monitor-mode', tab === 'accounts');
    if (tab === 'accounts') {
      settleVisible([
        refreshOnce('accounts', () => refreshAccounts(), {force:true}),
        refreshOnce('mobileAutoStatus', refreshMobileAutoStatus, {force:true}),
        refreshOnce('bindings', refreshBindings, {force:true}),
      ]).catch(e => toast(e.message, 'err'));
    }
    if (tab === 'personas') {
      settleVisible([
        refreshOnce('personas', refreshPersonas, {force:true}),
        refreshOnce('strategy', refreshStrategy, {force:true}),
      ]).catch(e => toast(e.message, 'err'));
    }
    if (tab === 'settings') {
      refreshOnce('config', refreshConfig, {force:true}).catch(e => toast(e.message, 'err'));
      refreshOnce('media', refreshMedia, {force:true}).catch(e => toast(e.message, 'err'));
      refreshSettingsHealth().catch(() => {});
      clearInterval(_healthTimer);
      _healthTimer = setInterval(() => {
        if (qs('#view-settings')?.classList.contains('active'))
          refreshSettingsHealth().catch(() => {});
      }, 20000);
    } else {
      clearInterval(_healthTimer);
    }
    if (tab === 'runs') {
      // B6-P4: 进数据中心默认加载当前 sub-tab 数据
      const cur = qs('.dc-tab.active')?.dataset.dc || 'runs';
      loadDcPane(cur).catch(e => toast(e.message, 'err'));
    }
  }

  function setSettingsTab(stId){
    closePresetDrop();
    clearSearchHighlights();
    qsa('.st-tab').forEach(b => b.classList.toggle('active', b.dataset.st === stId));
    qsa('.st-pane').forEach(p => p.classList.toggle('active', p.id === 'st-' + stId));
    if (stId === 'voice') refreshMedia().catch(e => toast(e.message, 'err'));
    if (stId === 'ops') refreshConfig().catch(e => toast(e.message, 'err'));
    refreshSettingsHealth().catch(() => {});
  }
  /* 挂 window：末尾独立 <script> 的「ops tab 顺带刷发送队列」包装层要引用本函数；
     原实现跨 IIFE 直呼 setSettingsTab 必抛 ReferenceError（dead-click 守卫红条实锤），
     现经 window 间接层唯一出口，包装层替换 window.setSettingsTab 即对点击生效。 */
  window.setSettingsTab = setSettingsTab;

  qsa('.st-tab').forEach(b => b.addEventListener('click', () => window.setSettingsTab(b.dataset.st)));

  function fmtSec(s){
    s = Math.max(0, Math.floor(Number(s || 0)));
    if (!s) return '';
    if (s < 60) return s + ' '+window.T('msg_js_007');
    if (s < 3600) return Math.round(s/60) + ' '+window.T('msg_js_008');
    return Math.round(s/3600*10)/10 + ' '+window.T('msg_js_009');
  }

  function updateDur(inputId, displayId){
    const el = qs('#' + displayId);
    if (el) el.textContent = fmtSec(qs('#' + inputId)?.value);
  }

  function updateRunTargetWarning(){
    const val = (qs('#cfg-run-targets')?.value || '').trim();
    const hasTarget = val.length > 0;
    const banner = qs('#run-target-banner');
    const pill = qs('#target-danger-pill');
    const tab = qs('.st-tab[data-st="automation"]');
    if (banner) banner.classList.toggle('visible', hasTarget);
    if (pill) pill.style.display = hasTarget ? '' : 'none';
    if (tab) tab.classList.toggle('has-danger', hasTarget);
  }

  let _settingsSnapshot = null;
  function markSettingsDirty(){
    const banner = qs('#settings-unsaved');
    if (banner) banner.style.display = 'flex';
  }
  function clearSettingsDirty(){
    const banner = qs('#settings-unsaved');
    if (banner) banner.style.display = 'none';
  }
  function reloadAllSettings(){
    clearSettingsDirty();
    refreshConfig().catch(e => toast(e.message, 'err'));
    refreshMedia().catch(e => toast(e.message, 'err'));
  }

  qsa('.mr-tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  qsa('[data-goto]').forEach(b => b.addEventListener('click', () => setTab(b.dataset.goto)));

  async function refreshStatus(){
    let st;
    try {
      st = await apiGet('/api/messenger-rpa/status');
    } catch(e) {
      qs('#mr-status-text').textContent = window.T('msg_js_010')+': ' + e.message;
      qs('#mr-status-dot').className = 'rpa-status-dot err';
      return;
    }
    state.status = st;
    const dot = qs('#mr-status-dot');
    const text = qs('#mr-status-text');
    const running = !!st.running;
    const paused = st.paused_until && st.paused_until > Date.now() / 1000;
    dot.className = 'rpa-status-dot' + (paused ? ' paused' : running ? '' : ' err');
    dot.style.color = dot.style.background;
    if (!st.available) {
      /* P0 状态口径：人话主文案在前，后端 hint（可能含配置键）降级为小字技术注脚 */
      const _hint = st.hint || ('Messenger ' + window.T('msg_js_011'));
      text.innerHTML = esc(window.T('chc_svc_down_human'))
        + '<span style="display:block;font-size:.72rem;color:var(--t3);margin-top:.15rem">' + esc(_hint) + '</span>';
    } else {
      const mode = paused ? window.T('msg_js_012') : running ? window.T('ov_js_running') : window.T('ov_js_stopped');
      const listen = st.notif_running ? window.T('msg_js_013') : window.T('msg_js_014');
      text.textContent = window.Tf('msg_js_p07',{mode:mode,listen:listen,n:st.consecutive_empty || 0});
    }
    qs('#qc-empty').textContent = st.pending_empty_count == null ? 0 : st.pending_empty_count;
    const sla = st.approval_sla || {};
    const slaText = (sla.pending_count || 0) === 0
      ? window.T('msg_js_015')
      : window.Tf('msg_js_p08',{n:sla.pending_count || 0,age:sla.oldest_age_sec || 0});
    const slaEl = qs('#overview-sla');
    slaEl.textContent = slaText;
    slaEl.className = 'mr-pill ' + ((sla.overdue_count || 0) > 0 ? 'danger' : 'ok');
  }

  async function refreshConfig(){
    let cfg;
    try {
      cfg = await apiGet('/api/messenger-rpa/config');
    } catch(e) {
      const modePill = qs('#mr-mode-pill');
      if (modePill) {
        modePill.textContent = 'mode unavailable';
        modePill.className = 'mr-pill danger';
      }
      qs('#config-raw').value = window.T('msg_js_257')+': ' + e.message;
      return;
    }
    state.config = cfg;
    const ops = cfg.operations || {};
    const raw = cfg.raw || {};
    const lq = cfg.lead_qualification || {};
    const handoff = lq.handoff || {};
    const targets = ops.run_once_target_names || raw.run_once_target_names || [];
    const modePill = qs('#mr-mode-pill');
    if (modePill) modePill.textContent = `mode ${ops.reply_mode || raw.reply_mode || '--'}`;
    const targetPill = qs('#mr-target-pill');
    if (targetPill) targetPill.textContent = targets.length ? `target ${targets.join(', ')}` : 'target all';
    qs('#overview-persona').textContent = (cfg.reply_profiles || {}).default || '--';
    qs('#overview-lead-rule').textContent = lq.enabled ? `ON · ${lq.min_score_for_line || 80}+` : 'OFF';
    qs('#overview-lead-rule').className = 'mr-pill ' + (lq.enabled ? 'ok' : 'warn');
    qs('#overview-line-id').textContent = handoff.line_id ? handoff.line_id : window.T('dash.m.not_configured');
    qs('#overview-line-id').className = 'mr-pill ' + (handoff.line_id ? 'ok' : 'warn');
    fillConfigForm(cfg);
  }

  function fillConfigForm(cfg){
    const raw = cfg.raw || {};
    const ops = cfg.operations || {};
    const lq = cfg.lead_qualification || {};
    const target = lq.target || {};
    const q = lq.question_policy || {};
    const handoff = lq.handoff || {};
    const low = lq.low_priority || {};
    qs('#cfg-enabled').checked = !!raw.enabled;
    qs('#cfg-autostart').checked = !!raw.autostart;
    qs('#cfg-suppress-identity').checked = !!raw.suppress_global_ai_identity;
    qs('#cfg-disable-memory').checked = !!raw.disable_episodic_memory;
    qs('#cfg-title-vision').checked = raw.thread_title_vision_fallback !== false;
    qs('#cfg-pre-self-guard').checked = raw.pre_thread_self_xml_guard !== false;
    qs('#cfg-stale-peer-guard').checked = raw.stale_peer_after_self_guard !== false;
    const replyMode = raw.reply_mode || ops.reply_mode || 'approve';
    qs('#cfg-reply-mode').value = replyMode;
    qsa('.mode-card').forEach(c => c.classList.toggle('active', c.dataset.mode === replyMode));
    qs('#cfg-start-mode').value = raw.run_once_start_mode || ops.run_once_start_mode || 'smart_current_thread';
    qs('#cfg-language').value = raw.language_alignment || ops.language_alignment || 'auto';
    const defLang = raw.default_reply_lang || ops.default_reply_lang || '';
    const defLangEl = qs('#cfg-default-lang');
    if (defLangEl) defLangEl.value = defLang || 'zh';
    const forceLang = raw.force_reply_lang || ops.force_reply_lang || '';
    const forceLangEl = qs('#cfg-force-lang');
    if (forceLangEl) forceLangEl.value = forceLang;
    qs('#cfg-max-inbox').value = raw.max_inbox_per_run || ops.max_inbox_per_run || '';
    const cooldown = raw.companion_reply_cooldown_sec ?? ops.companion_reply_cooldown_sec ?? '';
    qs('#cfg-cooldown').value = cooldown;
    updateDur('cfg-cooldown', 'dur-cooldown');
    const ivBase = raw.interval_sec ?? ops.interval_sec ?? '';
    const ivMin  = raw.min_interval_sec ?? ops.min_interval_sec ?? '';
    const ivMax  = raw.max_interval_sec ?? ops.max_interval_sec ?? '';
    const bkOff  = raw.backoff_multiplier ?? ops.backoff_multiplier ?? '';
    qs('#cfg-interval').value = ivBase;
    qs('#cfg-min-interval').value = ivMin;
    qs('#cfg-max-interval').value = ivMax;
    qs('#cfg-backoff').value = bkOff;
    updateDur('cfg-interval', 'dur-interval');
    updateDur('cfg-max-interval', 'dur-max-interval');
    qs('#cfg-companion').value = raw.companion_mode === false ? 'false' : 'true';
    qs('#cfg-run-targets').value = joinCsv(raw.run_once_target_names || []);
    qs('#cfg-targets').value = joinCsv(raw.target_chat_names || []);
    updateRunTargetWarning();
    qs('#lead-enabled').checked = !!lq.enabled;
    qs('#lead-country').value = target.country || 'JP';
    qs('#lead-gender').value = target.gender || 'female';
    qs('#lead-language').value = target.language || 'ja';
    qs('#lead-age-min').value = target.age_min || 37;
    qs('#lead-age-max').value = target.age_max || 60;
    qs('#lead-min-score').value = lq.min_score_for_line || 80;
    qs('#lead-min-turns').value = handoff.min_turns_before_send || 6;
    qs('#lead-age-turns').value = q.min_turns_before_age || 4;
    qs('#lead-budget-turns').value = q.min_turns_before_budget || 6;
    qs('#lead-low-score').value = low.score_below || 40;
    qs('#lead-stop-turns').value = low.stop_after_low_value_turns || 3;
    qs('#lead-line-id').value = handoff.line_id || '';
    qs('#lead-template').value = handoff.template || '';
    qs('#config-raw').value = JSON.stringify(raw, null, 2);
    fillOpsForm(raw);
    clearSettingsDirty();
  }

  // B6-P1: 运维诊断 ops sub-tab — 字段填充 / 状态卡 / 保存 / 重置
  function fillOpsForm(raw) {
    const setVal = (id, v, def) => { const el = qs(id); if (el) el.value = (v === undefined || v === null || v === '') ? (def ?? '') : v; };
    const setBool = (id, v) => { const el = qs(id); if (el) el.value = v === false ? 'false' : 'true'; };
    const rg = raw.runaway_guard || {};
    setBool('#ops-runaway-enabled', rg.enabled !== false);
    setVal('#ops-runaway-window', rg.window_sec, 600);
    setVal('#ops-runaway-max-sends', rg.max_sends_per_window, 5);
    setVal('#ops-runaway-hard', rg.hard_ceiling_sends, 8);
    setVal('#ops-runaway-cooldown', rg.cooldown_sec, 600);
    setBool('#ops-runaway-seq', rg.sequence_check_enabled !== false);
    setVal('#ops-runaway-seq-max', rg.sequence_max_consecutive_self, 3);
    setVal('#ops-runaway-seq-overlap', rg.sequence_overlap_threshold, 0.8);
    const vmg = raw.vision_misroute_guard || {};
    setBool('#ops-vmg-enabled', vmg.enabled !== false);
    setVal('#ops-vmg-cooldown', vmg.cooldown_sec, 300);
    setBool('#ops-vmg-only-sticky', !!vmg.only_when_vision_target_was_sticky);
    const st = raw.sticky_thread || {};
    setBool('#ops-sticky-enabled', st.enabled !== false);
    setVal('#ops-sticky-cooldown', st.post_send_cooldown_sec, 90);
    setBool('#ops-sticky-hash', st.hash_diff_enabled !== false);
    setVal('#ops-sticky-fullcheck', st.full_check_after_n_idle, 8);
    setVal('#ops-hourly-cap', raw.per_chat_hourly_cap, 30);
    setVal('#ops-self-skip', raw.self_skip_cooldown_sec, 60);
    setVal('#ops-spam-cooldown', raw.spam_cooldown_sec, 3600);
    const ai = raw.ai || {};
    const tiers = ai.tiers || {};
    setBool('#ops-ai-tiers-enabled', !!tiers.enabled);
    setVal('#ops-ai-premium-kw', joinCsv(tiers.premium_keywords || []));
    setVal('#ops-ai-low-kw', joinCsv(tiers.low_keywords || []));
    setVal('#ops-ai-low-threshold', tiers.low_threshold, 2);
    setVal('#ops-ai-premium-threshold', tiers.premium_threshold, 2);
    const pl = raw.pace_learning || {};
    setBool('#ops-pace-enabled', !!pl.enabled);
    setVal('#ops-pace-samples', pl.min_samples, 5);
    setVal('#ops-pace-throttle', pl.throttle_multiplier, 1.5);
    setVal('#ops-pace-block', pl.block_multiplier, 3.0);
    const cp = raw.credit_policy || {};
    setBool('#ops-credit-enabled', !!cp.enabled);
    setVal('#ops-credit-blacklist', cp.blacklist_threshold, -100);
    setVal('#ops-credit-low', cp.low_threshold, -50);
    setVal('#ops-credit-fail', cp.send_fail_delta, -5);
    setVal('#ops-credit-recover', cp.recover_delta, 1);
    setVal('#ops-credit-escalate', cp.escalation_delta, -20);
  }

  function renderOpsStatusCards(st) {
    const root = qs('#ops-status-grid');
    if (!root) return;
    if (!st || !st.available) {
      root.innerHTML = '<div class="mr-empty" style="grid-column:1/-1">'+window.T('msg_js_011')+'</div>';
      return;
    }
    const now = Date.now() / 1000;
    const lastTick = st.last_tick_ts || 0;
    const tickAge = lastTick ? Math.round(now - lastTick) : null;
    const empty = st.consecutive_empty || 0;
    const unhealthy = st.consecutive_unhealthy || 0;
    const runaway = (st.runaway || {}).count || 0;
    const stickyChat = st.sticky_thread_chat || st.sticky_chat || '--';
    const sendsToday = (st.send_counters || {}).today || 0;
    const cards = [
      {lbl: window.T('msg_js_258'), val: st.running ? '⚡ '+window.T('ov_js_running') : '⏹ '+window.T('ov_ctrl_stop'), col: st.running ? '#10b981' : '#ef4444'},
      {lbl: window.T('msg_js_259'), val: tickAge != null ? (tickAge + 's '+window.T('msg_js_260')) : '--', col: tickAge != null && tickAge > 120 ? '#f59e0b' : '#cbd5e1'},
      {lbl: window.T('msg_js_261'), val: empty + ' '+window.T('ov_js_times_unit'), col: empty >= 10 ? '#f59e0b' : empty >= 30 ? '#ef4444' : '#cbd5e1'},
      {lbl: window.T('msg_js_262'), val: unhealthy + ' '+window.T('ov_js_times_unit'), col: unhealthy >= 3 ? '#ef4444' : unhealthy >= 1 ? '#f59e0b' : '#10b981'},
      {lbl: window.T('msg_js_263'), val: sendsToday + ' '+window.T('dash.items'), col: '#3b82f6'},
      {lbl: 'Runaway '+window.T('msg_js_264'), val: runaway, col: runaway > 0 ? '#ef4444' : '#10b981'},
      {lbl: window.T('msg_js_265')+' sticky chat', val: String(stickyChat).slice(0, 20), col: '#3b82f6'},
    ];
    root.innerHTML = cards.map(c => `
      <div style="border:1px solid var(--bd);border-radius:8px;padding:12px 14px;background:var(--input)">
        <div style="font-size:11px;color:var(--t3);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px">${esc(c.lbl)}</div>
        <div style="font-size:18px;font-weight:700;color:${c.col}">${esc(c.val)}</div>
      </div>`).join('');
  }
  // 暴露给 refreshSettingsHealth 使用
  window.renderOpsStatusCards = renderOpsStatusCards;

  async function refreshOpsStatus() {
    let st;
    try { st = await apiGet('/api/messenger-rpa/status'); }
    catch(e) { renderOpsStatusCards(null); return; }
    renderOpsStatusCards(st);
  }

  async function saveOpsConfig() {
    const getNum = (id, def) => { const v = qs(id)?.value; return v === '' || v === undefined ? def : Number(v); };
    const getBool = (id) => qs(id)?.value !== 'false';
    const getCsv = (id) => splitCsv(qs(id)?.value || '');
    const patch = {
      runaway_guard: {
        enabled: getBool('#ops-runaway-enabled'),
        window_sec: getNum('#ops-runaway-window', 600),
        max_sends_per_window: getNum('#ops-runaway-max-sends', 5),
        hard_ceiling_sends: getNum('#ops-runaway-hard', 8),
        cooldown_sec: getNum('#ops-runaway-cooldown', 600),
        sequence_check_enabled: getBool('#ops-runaway-seq'),
        sequence_max_consecutive_self: getNum('#ops-runaway-seq-max', 3),
        sequence_overlap_threshold: getNum('#ops-runaway-seq-overlap', 0.8),
      },
      vision_misroute_guard: {
        enabled: getBool('#ops-vmg-enabled'),
        cooldown_sec: getNum('#ops-vmg-cooldown', 300),
        only_when_vision_target_was_sticky: getBool('#ops-vmg-only-sticky'),
      },
      sticky_thread: {
        enabled: getBool('#ops-sticky-enabled'),
        post_send_cooldown_sec: getNum('#ops-sticky-cooldown', 90),
        hash_diff_enabled: getBool('#ops-sticky-hash'),
        full_check_after_n_idle: getNum('#ops-sticky-fullcheck', 8),
      },
      per_chat_hourly_cap: getNum('#ops-hourly-cap', 30),
      self_skip_cooldown_sec: getNum('#ops-self-skip', 60),
      spam_cooldown_sec: getNum('#ops-spam-cooldown', 3600),
      ai: {
        tiers: {
          enabled: getBool('#ops-ai-tiers-enabled'),
          premium_keywords: getCsv('#ops-ai-premium-kw'),
          low_keywords: getCsv('#ops-ai-low-kw'),
          low_threshold: getNum('#ops-ai-low-threshold', 2),
          premium_threshold: getNum('#ops-ai-premium-threshold', 2),
        },
      },
      pace_learning: {
        enabled: getBool('#ops-pace-enabled'),
        min_samples: getNum('#ops-pace-samples', 5),
        throttle_multiplier: getNum('#ops-pace-throttle', 1.5),
        block_multiplier: getNum('#ops-pace-block', 3.0),
      },
      credit_policy: {
        enabled: getBool('#ops-credit-enabled'),
        blacklist_threshold: getNum('#ops-credit-blacklist', -100),
        low_threshold: getNum('#ops-credit-low', -50),
        send_fail_delta: getNum('#ops-credit-fail', -5),
        recover_delta: getNum('#ops-credit-recover', 1),
        escalation_delta: getNum('#ops-credit-escalate', -20),
      },
    };
    try {
      await apiPut('/api/messenger-rpa/config', patch);
      toast('✅ '+window.T('msg_js_266'), 'ok');
      clearSettingsDirty();
    } catch(e) {
      toast(window.T('ov_js_save_fail')+': ' + e.message, 'err');
      return;
    }
    await refreshConfig();
  }

  async function resetOpsDefaults() {
    const ok = await confirm2({
      title: window.T('msg_js_267'),
      message: window.T('msg_js_268')+'"'+window.T('msg_js_269')+'"'+window.T('msg_js_270')+'？',
      okText: window.T('msg_js_271'),
    });
    if (!ok) return;
    const defaults = {
      runaway_guard: {
        enabled: true, window_sec: 600, max_sends_per_window: 5,
        hard_ceiling_sends: 8, cooldown_sec: 600,
        sequence_check_enabled: true, sequence_max_consecutive_self: 3,
        sequence_overlap_threshold: 0.8,
      },
      vision_misroute_guard: {enabled: true, cooldown_sec: 300, only_when_vision_target_was_sticky: false},
      sticky_thread: {enabled: true, post_send_cooldown_sec: 90, hash_diff_enabled: true, full_check_after_n_idle: 8},
      per_chat_hourly_cap: 30, self_skip_cooldown_sec: 60, spam_cooldown_sec: 3600,
      ai: {tiers: {enabled: false, premium_keywords: [], low_keywords: [], low_threshold: 2, premium_threshold: 2}},
      pace_learning: {enabled: false, min_samples: 5, throttle_multiplier: 1.5, block_multiplier: 3.0},
      credit_policy: {enabled: false, blacklist_threshold: -100, low_threshold: -50, send_fail_delta: -5, recover_delta: 1, escalation_delta: -20},
    };
    fillOpsForm(defaults);
    toast(window.T('msg_js_272')+' — '+window.T('msg_js_273')+'"'+window.T('msg_js_269')+'"', 'info');
  }

  async function saveConfig(){
    const patch = {
      enabled: qs('#cfg-enabled')?.checked ?? false,
      autostart: qs('#cfg-autostart')?.checked ?? false,
      suppress_global_ai_identity: qs('#cfg-suppress-identity')?.checked ?? false,
      disable_episodic_memory: qs('#cfg-disable-memory')?.checked ?? false,
      thread_title_vision_fallback: qs('#cfg-title-vision')?.checked ?? false,
      pre_thread_self_xml_guard: qs('#cfg-pre-self-guard')?.checked ?? false,
      stale_peer_after_self_guard: qs('#cfg-stale-peer-guard')?.checked ?? false,
      reply_mode: qs('#cfg-reply-mode')?.value || 'approve',
      run_once_start_mode: qs('#cfg-start-mode')?.value || 'smart_current_thread',
      language_alignment: qs('#cfg-language')?.value || 'english_fallback_only',
      default_reply_lang: qs('#cfg-default-lang')?.value || '',
      force_reply_lang: qs('#cfg-force-lang')?.value || '',
      max_inbox_per_run: num(qs('#cfg-max-inbox')?.value, 1),
      companion_reply_cooldown_sec: num(qs('#cfg-cooldown')?.value, 300),
      companion_mode: qs('#cfg-companion')?.value === 'true',
      run_once_target_names: splitCsv(qs('#cfg-run-targets')?.value),
      target_chat_names: splitCsv(qs('#cfg-targets')?.value),
    };
    const ivBase = num(qs('#cfg-interval')?.value, 0);
    const ivMin  = num(qs('#cfg-min-interval')?.value, 0);
    const ivMax  = num(qs('#cfg-max-interval')?.value, 0);
    const bkOff  = parseFloat(qs('#cfg-backoff')?.value || '0') || 0;
    if (ivBase > 0) patch.interval_sec = ivBase;
    if (ivMin > 0)  patch.min_interval_sec = ivMin;
    if (ivMax > 0)  patch.max_interval_sec = ivMax;
    if (bkOff > 0)  patch.backoff_multiplier = bkOff;
    try {
      await apiPut('/api/messenger-rpa/config', patch);
      toast('✅ '+window.T('msg_js_274'));
      clearSettingsDirty();
      updateRunTargetWarning();
    } catch(e) {
      console.error('[saveConfig]', e);
      toast(window.T('ov_js_save_fail')+': ' + e.message, 'err');
      return;
    }
    await Promise.all([refreshConfig(), refreshLeads()]);
  }

  async function saveConversionConfig(){
    const patch = {
      lead_qualification: {
        enabled: qs('#lead-enabled').checked,
        target: {
          country: qs('#lead-country').value.trim() || 'JP',
          language: qs('#lead-language').value.trim() || 'ja',
          gender: qs('#lead-gender').value.trim() || 'female',
          age_min: num(qs('#lead-age-min').value, 37),
          age_max: num(qs('#lead-age-max').value, 60),
        },
        min_score_for_line: num(qs('#lead-min-score').value, 80),
        question_policy: {
          min_turns_before_age: num(qs('#lead-age-turns').value, 4),
          min_turns_before_budget: num(qs('#lead-budget-turns').value, 6),
        },
        handoff: {
          line_id: qs('#lead-line-id').value.trim(),
          template: qs('#lead-template').value.trim(),
          min_turns_before_send: num(qs('#lead-min-turns').value, 6),
        },
        low_priority: {
          score_below: num(qs('#lead-low-score').value, 40),
          stop_after_low_value_turns: num(qs('#lead-stop-turns').value, 3),
        },
      },
    };
    try {
      await apiPut('/api/messenger-rpa/config', patch);
      toast('✅ '+window.T('msg_js_275'));
    } catch(e) {
      console.error('[saveConversionConfig]', e);
      toast(window.T('ov_js_save_fail')+': ' + e.message, 'err');
      return;
    }
    await Promise.all([refreshConfig(), refreshLeads()]);
  }

  async function refreshMedia(){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/media');
    } catch(e) {
      qs('#media-summary').textContent = window.T('msg_js_276');
      return;
    }
    state.media = r;
    const cfg = r.config || {};
    const deep = cfg.media_deep_understand || {};
    const vi = cfg.voice_input || {};
    const apCfg = vi.audio_pipeline || {};
    const vo = cfg.voice_output || {};
    const vp = vo.voice_profile || {};
    const emoji = cfg.emoji_policy || {};
    const policy = cfg.media_handling_policy || 'ai';
    qs('#media-policy').value = ['ai','ack_and_approve','ack_only'].includes(policy) ? policy : 'ai';
    qs('#media-emoji-style').value = emoji.style === 'conservative' ? 'conservative' : 'natural';
    qs('#media-include-links').checked = !!cfg.media_include_links;
    qs('#media-image-enabled').checked = deep.enabled !== false;
    qs('#media-image-timeout').value = deep.timeout_sec || 15;
    qs('#voice-input-enabled').checked = !!vi.enabled;
    qs('#voice-prefer-transcribe').checked = vi.prefer_transcribe !== false;
    qs('#voice-capture-mode').value = vi.capture_mode || 'run_as';
    qs('#voice-asr-backend').value = apCfg.backend || 'faster_whisper';
    qs('#voice-asr-model').value = apCfg.model || apCfg.model_size || 'base';
    qs('#voice-asr-language').value = apCfg.language || vi.language_hint || 'auto';
    qs('#voice-timeout').value = vi.timeout_sec || 30;
    qs('#voice-output-enabled').value = vo.enabled ? 'true' : 'false';
    qs('#voice-output-mode').value = vo.mode || 'approval_only';
    const voTrigger = vo.trigger || 'when_peer_voice';
    const voProb = Math.round((Number(vo.voice_probability || 0.35)) * 100);
    const voMaxChars = vo.max_text_chars || '';
    if (qs('#voice-trigger')) qs('#voice-trigger').value = voTrigger;
    if (qs('#voice-probability')) qs('#voice-probability').value = voProb;
    if (qs('#voice-max-chars')) qs('#voice-max-chars').value = voMaxChars;
    updateVoiceProbUI(voTrigger, voProb);
    qs('#voice-tts-backend').value = vo.backend || 'edge_tts';
    qs('#voice-tts-voice').value = vo.voice || '';
    qs('#voice-tts-max').value = vo.max_seconds || 20;
    qs('#voice-profile-enabled').checked = !!vp.enabled;
    qs('#voice-profile-consent').checked = vp.owner_consent !== false;
    qs('#voice-profile-speaker').value = vp.speaker_id || '';
    qs('#voice-profile-ref').value = vp.reference_audio_path || '';
    qs('#voice-profile-command').value = vp.command_template || '';
    qs('#voice-text-summary').checked = vo.send_text_summary !== false;
    qs('#voice-night-quiet').checked = vo.night_quiet !== false;
    const caps = r.capabilities || {};
    const ap = r.audio_pipeline || {};
    const tts = r.tts_pipeline || {};
    const vr = r.voice_runtime || {};
    qs('#media-summary').textContent = [
      window.Tf('msg_js_p09',{x:caps.image_understanding ? window.T('msg_js_p12') : window.T('rpa_fn_ack')}),
      window.Tf('msg_js_p10',{x:caps.voice_transcription ? window.T('msg_js_p13') : window.T('msg_js_p14')}),
      window.Tf('msg_js_p11',{x:caps.send_voice ? window.T('msg_js_p15') : window.T('msg_js_p14')}),
      ap.backend ? `ASR ${ap.backend}` : '',
      tts.backend ? `TTS ${tts.backend}` : '',
    ].filter(Boolean).join(' · ');
    const input = vr.input || {};
    const output = vr.output || {};
    const safety = vr.safety || {};
    qs('#media-cap-strip').innerHTML = [
      capCard(window.T('msg_js_277'), caps.image_understanding ? window.T('msg_js_278') : window.T('ov_js_disable'), caps.image_understanding ? 'ok' : 'off'),
      capCard(window.T('msg_js_279'), input.enabled ? `${input.asr_backend || 'ASR'} · ${input.capture_mode || ''}` : window.T('ov_js_disable'), input.enabled ? 'ok' : 'off'),
      capCard(window.T('msg_js_280'), output.enabled ? `${output.backend || 'TTS'} · ${output.mode || ''}` : window.T('ov_js_disable'), output.enabled ? 'warn' : 'off'),
      capCard(window.T('msg_js_281'), safety.approval_first ? window.T('msg_js_282') : window.T('msg_js_283'), safety.approval_first ? 'ok' : 'warn'),
    ].join('');
  }

  function capCard(title, value, cls){
    return `<div class="mr-cap ${cls || ''}"><b>${esc(value || '--')}</b><span>${esc(title)}</span></div>`;
  }

  async function saveMedia(){
    const body = {
      media_handling_policy: qs('#media-policy')?.value || 'ai',
      media_include_links: qs('#media-include-links')?.checked ?? false,
      media_deep_understand: {
        enabled: qs('#media-image-enabled')?.checked ?? false,
        timeout_sec: num(qs('#media-image-timeout')?.value, 15),
      },
      voice_input: {
        enabled: qs('#voice-input-enabled')?.checked ?? false,
        prefer_transcribe: qs('#voice-prefer-transcribe')?.checked ?? false,
        capture_mode: qs('#voice-capture-mode')?.value || 'run_as',
        timeout_sec: num(qs('#voice-timeout')?.value, 30),
        audio_pipeline: {
          enabled: qs('#voice-input-enabled')?.checked ?? false,
          backend: qs('#voice-asr-backend')?.value || 'faster_whisper',
          model_size: (qs('#voice-asr-model')?.value || '').trim() || 'base',
          model: (qs('#voice-asr-model')?.value || '').trim() || 'whisper-1',
          language: (qs('#voice-asr-language')?.value || '').trim() || 'auto',
        },
      },
      voice_output: {
        enabled: qs('#voice-output-enabled')?.value === 'true',
        mode: qs('#voice-output-mode')?.value || 'approval_only',
        trigger: qs('#voice-trigger')?.value || 'when_peer_voice',
        voice_probability: Math.round(num(qs('#voice-probability')?.value, 35)) / 100,
        max_text_chars: num(qs('#voice-max-chars')?.value, 0) || undefined,
        backend: qs('#voice-tts-backend')?.value || 'edge_tts',
        voice: (qs('#voice-tts-voice')?.value || '').trim(),
        max_seconds: num(qs('#voice-tts-max')?.value, 20),
        voice_profile: {
          enabled: qs('#voice-profile-enabled')?.checked ?? false,
          owner_consent: qs('#voice-profile-consent')?.checked ?? false,
          speaker_id: (qs('#voice-profile-speaker')?.value || '').trim() || 'my_voice',
          reference_audio_path: (qs('#voice-profile-ref')?.value || '').trim(),
          backend: 'voice_clone_command',
          command_template: (qs('#voice-profile-command')?.value || '').trim(),
        },
        send_text_summary: qs('#voice-text-summary')?.checked ?? false,
        night_quiet: qs('#voice-night-quiet')?.checked ?? false,
      },
      emoji_policy: {
        style: qs('#media-emoji-style')?.value || 'natural',
      },
    };
    try {
      await apiPut('/api/messenger-rpa/media', body);
      toast('✅ '+window.T('msg_js_284'));
    } catch(e) {
      console.error('[saveMedia]', e);
      toast(window.T('ov_js_save_fail')+': ' + e.message, 'err');
      return;
    }
    await Promise.all([refreshMedia(), refreshConfig()]);
  }

  /* ── P2: Health strip ───────────────────────────── */
  let _healthTimer = null;

  function timeAgo(ts){
    if (!ts) return '--';
    const sec = Math.round(Date.now()/1000 - ts);
    if (sec < 0) return window.T('rpa_just_now');
    if (sec < 5) return window.T('rpa_just_now');
    if (sec < 60) return sec + window.T('msg_js_285');
    if (sec < 3600) return Math.round(sec/60) + window.T('dash.ago.min');
    return Math.round(sec/3600*10)/10 + window.T('dash.ago.hour');
  }

  async function refreshSettingsHealth(){
    let st;
    try { st = await apiGet('/api/messenger-rpa/status'); } catch(e) {
      qs('#st-hstate').textContent = window.T('msg_js_286');
      renderOpsStatusCards(null);
      return;
    }
    const dot     = qs('#st-hdot');
    const hstate  = qs('#st-hstate');
    const hlast   = qs('#st-hlast');
    const hiv     = qs('#st-hiv');
    const hempty  = qs('#st-hempty');
    const hlang   = qs('#st-hlang');
    const htarget = qs('#st-htarget');

    if (!st.available) {
      dot.style.background = dot.style.color = 'var(--red)';
      hstate.textContent = window.T('msg_js_011');
      hstate.style.color = 'var(--red)';
      renderOpsStatusCards(st);
      return;
    }

    const now = Date.now()/1000;
    const paused  = st.paused_until && st.paused_until > now;
    const running = !!st.running;
    const color   = paused ? 'var(--amber)' : running ? 'var(--green)' : 'var(--red)';
    dot.style.background = dot.style.color = color;
    hstate.style.color = color;
    hstate.textContent = paused ? '⏸ '+window.T('msg_js_012') : running ? '⚡ '+window.T('ov_js_running') : '⏹ '+window.T('ov_js_stopped');

    const lastTick = st.last_tick_ts || 0;
    const tickAge  = lastTick ? Math.round(now - lastTick) : null;
    hlast.textContent = window.T('msg_js_259')+' ' + (lastTick ? timeAgo(lastTick) : '--');
    hlast.className  = 'st-h-item' + (tickAge != null && tickAge > 120 ? ' warn' : '');

    const perA    = st.per_account || {};
    const aids    = Object.keys(perA);
    const liveIv  = aids.length > 0
      ? Math.min(...aids.map(a => perA[a].cur_iv_sec || 0))
      : (st.config && st.config.interval_sec) || 0;
    hiv.textContent = liveIv > 0 ? (window.T('msg_js_287')+' ' + fmtSec(liveIv)) : window.T('msg_js_287')+' --';
    const baseIv    = (st.config && st.config.interval_sec) || 0;
    hiv.className   = 'st-h-item' + (liveIv > baseIv * 2.5 && liveIv > 60 ? ' warn' : '');

    const empty = st.consecutive_empty || 0;
    hempty.textContent = window.T('msg_js_288')+' ' + empty + window.T('ov_js_times_unit');
    hempty.className   = 'st-h-item' + (empty >= 10 ? ' warn' : '');

    const cfg       = st.config || {};
    const forceLang = cfg.force_reply_lang || '';
    const langNames = {ja:window.T('msg_js_289')+' 🇯🇵', zh:window.T('msg_js_290')+' 🇨🇳', en:window.T('msg_js_291')+' 🇬🇧', ko:window.T('msg_js_292')+' 🇰🇷'};
    qs('#st-hlang-sep').style.display = forceLang ? '' : 'none';
    hlang.style.display = forceLang ? '' : 'none';
    hlang.textContent   = langNames[forceLang] || (window.T('msg_js_293')+' ' + forceLang);

    const targets   = [].concat(cfg.run_once_target_names || []).filter(Boolean);
    qs('#st-htarget-sep').style.display = targets.length ? '' : 'none';
    htarget.style.display = targets.length ? '' : 'none';
    htarget.textContent   = targets.length ? ('⚠ '+window.T('msg_js_294')+': ' + targets.slice(0,2).join(', ') + (targets.length > 2 ? '…' : '')) : '';

    renderOpsStatusCards(st);
  }

  /* ── P2: Presets ─────────────────────────────────── */
  const PRESETS = {
    aggressive: {
      interval_sec: 15, min_interval_sec: 5, max_interval_sec: 90, backoff_multiplier: 1.3,
      companion_reply_cooldown_sec: 120, max_inbox_per_run: 3,
      _voice_trigger: 'random', _voice_prob: 40,
      _mode: 'auto',
    },
    balanced: {
      interval_sec: 30, min_interval_sec: 8, max_interval_sec: 180, backoff_multiplier: 1.5,
      companion_reply_cooldown_sec: 300, max_inbox_per_run: 2,
      _voice_trigger: 'when_peer_voice', _voice_prob: 35,
      _mode: 'approve',
    },
    conservative: {
      interval_sec: 60, min_interval_sec: 20, max_interval_sec: 300, backoff_multiplier: 1.8,
      companion_reply_cooldown_sec: 600, max_inbox_per_run: 1,
      _voice_trigger: 'when_peer_voice', _voice_prob: 20,
      _mode: 'approve',
    },
    debug: {
      interval_sec: 20, min_interval_sec: 10, max_interval_sec: 60, backoff_multiplier: 1.2,
      companion_reply_cooldown_sec: 60, max_inbox_per_run: 1,
      _voice_trigger: 'when_peer_voice', _voice_prob: 0,
      _mode: 'approve', _start_mode: 'force_chats',
    },
  };

  function applyPreset(name){
    const p = PRESETS[name];
    if (!p) return;
    const setV = (id, v) => { const el = qs('#'+id); if (el) el.value = v; };
    setV('cfg-interval', p.interval_sec);
    setV('cfg-min-interval', p.min_interval_sec);
    setV('cfg-max-interval', p.max_interval_sec);
    setV('cfg-backoff', p.backoff_multiplier);
    setV('cfg-cooldown', p.companion_reply_cooldown_sec);
    setV('cfg-max-inbox', p.max_inbox_per_run);
    updateDur('cfg-interval', 'dur-interval');
    updateDur('cfg-max-interval', 'dur-max-interval');
    updateDur('cfg-cooldown', 'dur-cooldown');
    if (p._mode) {
      setV('cfg-reply-mode', p._mode);
      qsa('.mode-card').forEach(c => c.classList.toggle('active', c.dataset.mode === p._mode));
    }
    if (p._start_mode) setV('cfg-start-mode', p._start_mode);
    if (p._voice_trigger) {
      setV('voice-trigger', p._voice_trigger);
      setV('voice-probability', p._voice_prob);
      updateVoiceProbUI(p._voice_trigger, p._voice_prob);
    }
    closePresetDrop();
    markSettingsDirty();
    window.setSettingsTab('automation');
    toast('🎛 '+window.T('msg_js_295'), 'ok');
  }

  function togglePresetDrop(){
    const drop = qs('#st-preset-drop');
    const btn  = qs('#btn-settings-preset');
    if (!drop) return;
    const open = drop.classList.toggle('open');
    btn && btn.classList.toggle('active', open);
  }

  function closePresetDrop(){
    qs('#st-preset-drop')?.classList.remove('open');
    qs('#btn-settings-preset')?.classList.remove('active');
  }

  /* ── P2: Quick search ────────────────────────────── */
  let _searchResults = [];
  let _searchIdx     = 0;

  function openSettingsSearch(){
    const box = qs('#st-search-box');
    const btn = qs('#btn-settings-search');
    if (!box) return;
    box.classList.add('open');
    btn && btn.classList.add('active');
    qs('#st-search-input')?.focus();
  }

  function closeSettingsSearch(){
    qs('#st-search-box')?.classList.remove('open');
    qs('#btn-settings-search')?.classList.remove('active');
    clearSearchHighlights();
    qs('#st-search-input') && (qs('#st-search-input').value = '');
    qs('#st-search-stat') && (qs('#st-search-stat').textContent = '');
    _searchResults = [];
  }

  function clearSearchHighlights(){
    qsa('.st-hl, .st-hl-anim').forEach(el => el.classList.remove('st-hl', 'st-hl-anim'));
  }

  function searchSettings(q){
    clearSearchHighlights();
    _searchResults = [];
    _searchIdx = 0;
    const stat = qs('#st-search-stat');
    if (!q || q.length < 1) { if (stat) stat.textContent = ''; return; }
    const lo = q.toLowerCase();
    const paneMap = {'st-automation':'automation','st-dialog':'dialog','st-voice':'voice','st-conversion':'conversion','st-ops':'ops'};
    qsa('#view-settings label, #view-settings .rpa-card-title, #view-settings .mr-check').forEach(el => {
      const txt = el.textContent.toLowerCase();
      if (!txt.includes(lo)) return;
      const pane = el.closest('.st-pane');
      if (!pane) return;
      const target = el.closest('.mr-field, .mr-check') || el;
      _searchResults.push({ el: target, paneId: paneMap[pane.id] });
    });
    if (stat) stat.textContent = _searchResults.length ? _searchResults.length + ' '+window.T('msg_js_296') : window.T('msg_js_297');
    if (!_searchResults.length) return;
    jumpToSearchResult(0);
  }

  function jumpToSearchResult(idx){
    if (!_searchResults.length) return;
    _searchIdx = ((idx % _searchResults.length) + _searchResults.length) % _searchResults.length;
    const item = _searchResults[_searchIdx];
    if (!item) return;
    window.setSettingsTab(item.paneId);
    setTimeout(() => {
      item.el.classList.add('st-hl', 'st-hl-anim');
      item.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 80);
    const stat = qs('#st-search-stat');
    if (stat) stat.textContent = `${_searchIdx+1}/${_searchResults.length}`;
  }

  /* ── P2: event bindings ──────────────────────────── */
  {
    qs('#btn-settings-preset')?.addEventListener('click', e => { e.stopPropagation(); togglePresetDrop(); });
    qsa('.st-preset-item').forEach(item => item.addEventListener('click', () => applyPreset(item.dataset.preset)));
    document.addEventListener('click', e => {
      if (!qs('#st-preset-drop')?.contains(e.target) && e.target !== qs('#btn-settings-preset')) closePresetDrop();
    });
    qs('#btn-settings-search')?.addEventListener('click', () => {
      const box = qs('#st-search-box');
      box?.classList.contains('open') ? closeSettingsSearch() : openSettingsSearch();
    });
    qs('#btn-search-close')?.addEventListener('click', closeSettingsSearch);
    let _searchDebounce = null;
    qs('#st-search-input')?.addEventListener('input', function(){
      clearTimeout(_searchDebounce);
      _searchDebounce = setTimeout(() => searchSettings(this.value.trim()), 160);
    });
    qs('#st-search-input')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') { jumpToSearchResult(_searchIdx + (e.shiftKey ? -1 : 1)); e.preventDefault(); }
      if (e.key === 'Escape') { closeSettingsSearch(); e.preventDefault(); }
    });
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f' && qs('#view-settings')?.classList.contains('active')) {
        e.preventDefault(); openSettingsSearch();
      }
    });
  }

  function updateVoiceProbUI(trigger, probPct){
    const field = qs('#voice-prob-field');
    if (field) field.style.display = (trigger === 'random') ? '' : 'none';
    const valEl = qs('#voice-prob-val');
    const noteEl = qs('#voice-prob-note');
    if (valEl) valEl.textContent = probPct + '%';
    if (noteEl) noteEl.textContent = window.Tf('msg_js_p16',{p:probPct,q:100-probPct});
  }

  {/* settings event bindings */
    qs('#voice-trigger')?.addEventListener('change', function(){
      const prob = num(qs('#voice-probability')?.value, 35);
      updateVoiceProbUI(this.value, prob);
    });
    qs('#voice-probability')?.addEventListener('input', function(){
      const trigger = qs('#voice-trigger')?.value || 'random';
      updateVoiceProbUI(trigger, num(this.value, 35));
    });
    ['cfg-cooldown','cfg-interval','cfg-max-interval'].forEach(id => {
      const displayMap = {'cfg-cooldown':'dur-cooldown','cfg-interval':'dur-interval','cfg-max-interval':'dur-max-interval'};
      qs('#'+id)?.addEventListener('input', () => updateDur(id, displayMap[id]));
    });
    qs('#cfg-run-targets')?.addEventListener('input', updateRunTargetWarning);
    const settingsFields = ['#cfg-enabled','#cfg-autostart','#cfg-suppress-identity','#cfg-disable-memory',
      '#cfg-title-vision','#cfg-pre-self-guard','#cfg-stale-peer-guard','#cfg-start-mode',
      '#cfg-language','#cfg-default-lang','#cfg-force-lang','#cfg-max-inbox','#cfg-cooldown',
      '#cfg-interval','#cfg-min-interval','#cfg-max-interval','#cfg-backoff','#cfg-companion',
      '#cfg-run-targets','#cfg-targets'];
    settingsFields.forEach(sel => {
      const el = qs(sel);
      if (el) el.addEventListener('change', markSettingsDirty);
      if (el && el.type !== 'checkbox' && el.tagName !== 'SELECT') el.addEventListener('input', markSettingsDirty);
    });
    qsa('.mode-card').forEach(card => {
      card.addEventListener('click', () => {
        qsa('.mode-card').forEach(c => c.classList.remove('active'));
        card.classList.add('active');
        qs('#cfg-reply-mode').value = card.dataset.mode;
        markSettingsDirty();
      });
    });
    qs('#btn-save-config')?.addEventListener('click', () => saveConfig().catch(e => toast(e.message,'err')));
    qs('#btn-save-dialog')?.addEventListener('click', () => saveConfig().catch(e => toast(e.message,'err')));
    qs('#btn-save-conversion')?.addEventListener('click', () => saveConversionConfig().catch(e => toast(e.message,'err')));
    qs('#btn-ops-save')?.addEventListener('click', () => saveOpsConfig().catch(e => toast(e.message,'err')));
    qs('#btn-ops-reset-defaults')?.addEventListener('click', () => resetOpsDefaults().catch(e => toast(e.message,'err')));
    qs('#btn-ops-refresh-status')?.addEventListener('click', () => refreshOpsStatus().catch(e => toast(e.message,'err')));
  }

  async function testAsr(){
    const path = qs('#asr-test-path').value.trim();
    if (!path) return toast(window.T('msg_js_298'), 'err');
    qs('#asr-test-result').textContent = window.T('msg_js_299')+'...';
    const r = await apiPost('/api/messenger-rpa/media/asr-test', {path, timeout_sec: num(qs('#voice-timeout').value, 30)});
    const rv = r.result || {};
    qs('#asr-test-result').innerHTML = rv.ok
      ? `<b>${window.T('ov_js_succ_label')}</b> · ${esc(rv.model || '')} · ${esc(rv.language || '')} · ${rv.latency_ms || 0}ms<br>${esc(rv.text || '')}`
      : `<b>${window.T('rpa_js_st_failed')}</b> · ${esc(rv.error || 'unknown')}`;
  }

  async function testTts(){
    const text = qs('#tts-test-text').value.trim();
    if (!text) return toast(window.T('msg_js_300'), 'err');
    qs('#tts-test-result').textContent = window.T('msg_js_301')+'...';
    const r = await apiPost('/api/messenger-rpa/media/tts-test', {text, timeout_sec: 30});
    const rv = r.result || {};
    qs('#tts-test-result').innerHTML = rv.ok
      ? `<b>${window.T('ov_js_succ_label')}</b> · ${esc(rv.provider || '')} · ${esc(rv.voice || '')} · ${rv.latency_ms || 0}ms<br>${esc(rv.audio_path || '')}`
      : `<b>${window.T('rpa_js_st_failed')}</b> · ${esc(rv.error || 'unknown')}`;
  }

  async function refreshPersonas(){
    const root = qs('#persona-preview');
    let pmData, mrpaData;
    try {
      [pmData, mrpaData] = await Promise.allSettled([
        apiGet('/api/personas/profiles'),
        apiGet('/api/messenger-rpa/personas'),
      ]).then(([a, b]) => [a.value, b.value]);
    } catch(e) {
      if (root) root.innerHTML = `<div class="mr-empty">${window.T('msg_js_496')}: ${esc(e.message)}</div>`;
      return;
    }
    // Store PM profiles for chat-binding manager dropdowns
    state.pmProfiles = Object.values((pmData || {}).profiles || {});
    state.personas = mrpaData;
    renderPersonas(pmData || {}, (mrpaData || {}).reply_profiles || {});
    if (state.bindings) renderBindings(state.bindings);
  }

  function renderPersonas(pmData, mrpaRp){
    const root = qs('#persona-preview');
    const profiles = state.pmProfiles || [];
    if (!profiles.length) {
      root.innerHTML = `<div class="mr-empty">${window.T('msg_js_691')} · <a href="/personas" style="color:var(--p)">${window.T('msg_js_692')}</a></div>`;
      return;
    }
    const mrpaProfiles = (mrpaRp.profiles || []);
    const defaultId = mrpaRp.default || '';
    // _activeBanner: show currently active Messenger default persona
    const _defId = defaultId;
    const _defPM = profiles.find(x => (x.id || x.name) === _defId);
    const _defName = (_defPM && _defPM.name) || _defId || window.T('msg_js_497');
    const _activeBanner = `<div class="mr-persona-active" style="padding:10px 14px;border:1px solid var(--th-bd-blue2d,#2d6cdf);border-radius:8px;background:var(--th-bg-blue08,rgba(45,108,223,0.08));margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-weight:600">🎭 Messenger ${window.T('psn_tab_default')}</span>
        <span class="mr-badge sent">${esc(_defId || '—')}</span>
        <span class="mr-muted">${esc(_defName)}</span>
        <a href="/personas" style="margin-left:auto;font-size:11px;color:var(--p)">${window.T('msg_js_693')} →</a>
      </div>`;
    // P2-D: Read-only PM profile cards; editing is exclusively via /personas Studio
    const LANG_LABELS = {auto:window.T('msg_js_498'), ja:window.T('dash.lang.ja'), en:window.T('dash.lang.en'), zh:window.T('dash.lang.zh'), ko:window.T('dash.lang.ko')};
    root.innerHTML = _activeBanner + profiles.map(p => {
      const pid = p.id || p.name || '?';
      const persona = p;  // PM profile IS the persona dict
      const personaName = persona.name || p.id || window.T('base.review.no_name');
      const role = persona.role || '';
      const isImported = !!persona._mrpa_source;
      const mrpaMatch = mrpaProfiles.find(m => (m.id || m.name) === pid);
      const isDefault = pid === defaultId;
      const matchNames = (mrpaMatch && mrpaMatch.match_names) || [];
      const lang = (mrpaMatch && mrpaMatch.language) || 'auto';
      const langLabel = LANG_LABELS[lang] || lang;
      return `<div style="border:1px solid ${isDefault ? 'var(--th-bd-emerald5,#10b981)' : 'var(--bd)'};border-radius:8px;padding:12px 14px;margin-bottom:8px;background:${isDefault ? 'var(--th-bg-grn05,rgba(16,185,129,0.05))' : 'var(--input)'}">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
          <div>
            <div style="font-size:14px;font-weight:700;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span>🎭 ${esc(personaName)}</span>
              ${isDefault ? '<span class="mr-badge sent" style="font-size:10px">⭐ Messenger'+window.T('psn_js_050')+'</span>' : ''}
              ${isImported ? '<span class="mr-badge" style="font-size:10px;opacity:.7">config'+window.T('import')+'</span>' : '<span class="mr-badge" style="font-size:10px;background:var(--th-bg-brand15,rgba(91,124,246,.15));color:var(--p)">PM'+window.T('edit')+'</span>'}
              <span class="mr-badge" style="font-size:10px">${esc(langLabel)}</span>
            </div>
            <div style="font-size:11px;color:var(--t3);margin-top:3px">ID: <code>${esc(pid)}</code>${role ? ' · ' + esc(role) : ''}
              ${matchNames.length ? ' · '+window.T('msg_js_499')+': ' + matchNames.slice(0,3).map(n => `<span class="mr-badge" style="font-size:10px">${esc(n)}</span>`).join('') : ''}
            </div>
          </div>
          <a href="/personas" class="btn" style="font-size:11px;text-decoration:none">✏️ ${window.T('msg_js_693')}</a>
        </div>
      </div>`;
    }).join('');
  }

  async function deletePersona(pid){
    const rp = (state.personas || {}).reply_profiles || {};
    const profiles = rp.profiles || [];
    const target = profiles.find(x => x && x.id === pid);
    if (!target) return toast(window.T('msg_js_500'), 'err');
    if (rp.default === pid) return toast(window.T('msg_js_501')+' — '+window.T('msg_js_502'), 'err');
    const personaName = (target.persona && target.persona.name) || pid;
    const ok = await confirm2({
      title: window.T('msg_js_503'),
      message: window.Tf('msg_js_p17',{name:personaName,id:pid}),
      okText: window.T('msg_js_504'),
      danger: true,
    });
    if (!ok) return;
    const newProfiles = profiles.filter(x => x && x.id !== pid);
    const r = await apiPut('/api/messenger-rpa/personas', {
      reply_profiles: {default: rp.default, profiles: newProfiles},
    });
    state.personas = {reply_profiles: r.reply_profiles || {}};
    qs('#personas-json').value = JSON.stringify(r.reply_profiles || {}, null, 2);
    renderPersonas(r.reply_profiles || {});
    toast(window.Tf('msg_js_p18',{name:personaName}), 'ok');
    refreshConfig().catch(() => {});
  }

  function openPersonaForm(pid){
    const rp = ((state.personas || {}).reply_profiles || {});
    const p = (rp.profiles || []).find(x => x && x.id === pid);
    if (!p) return toast(window.T('msg_js_505')+': ' + pid, 'err');
    const persona = p.persona || {};
    const speaking = persona.speaking || {};
    const facts = persona.facts || [];
    state.editingPersonaId = pid;
    // 改为打开 modal（之前是内联展开）。用 .show class 配合 CSS 控制
    const modal = qs('#persona-edit-modal');
    if (modal) modal.classList.add('show');
    // 保险：确保表单 visible（防止旧逻辑遗留 display:none）
    qs('#persona-form').style.display = '';
    qs('#persona-form-id').value = pid;
    qs('#persona-form-name').value = persona.name || p.name || pid;
    qs('#persona-form-language').value = p.language || 'auto';
    qs('#persona-form-customer-type').value = p.customer_type || '';
    qs('#persona-form-style').value = p.style_hint || '';
    qs('#persona-form-match-names').value = joinCsv(p.match_names || []);
    qs('#persona-form-forbidden').value = joinCsv(speaking.forbidden_phrases || []);
    qs('#persona-form-facts').value = Array.isArray(facts) ? facts.join('\n') : '';
    qs('#persona-form').scrollIntoView({block:'nearest', behavior:'smooth'});
  }

  async function savePersonaForm(){
    const pid = state.editingPersonaId;
    if (!pid) return toast(window.T('ov_js_select_persona_first'), 'err');
    const body = {
      name: qs('#persona-form-name').value.trim(),
      language: qs('#persona-form-language').value.trim() || 'auto',
      customer_type: qs('#persona-form-customer-type').value.trim(),
      style_hint: qs('#persona-form-style').value.trim(),
      match_names: splitCsv(qs('#persona-form-match-names').value),
      forbidden_phrases: splitCsv(qs('#persona-form-forbidden').value),
      background_facts: qs('#persona-form-facts').value.split('\n').map(x => x.trim()).filter(Boolean),
    };
    const r = await api(`/api/messenger-rpa/strategy/personas/${encodeURIComponent(pid)}`, {method:'PATCH', body});
    const _picked = (r.persona && (r.persona.name || pid)) || pid;
    toast(window.Tf('msg_js_p19',{p:_picked}));
    state.personas = {reply_profiles: r.reply_profiles || {}};
    qs('#personas-json').value = JSON.stringify(r.reply_profiles || {}, null, 2);
    renderPersonas(r.reply_profiles || {});
    // 关 modal（用 .show class 配合新 CSS）— 不要再隐藏 #persona-form 本身（会造成下次 open modal 内容空白）
    const modal = qs('#persona-edit-modal');
    if (modal) modal.classList.remove('show');
    state.editingPersonaId = '';
    await Promise.allSettled([refreshConfig(), refreshStrategy()]);
  }
  // 暴露到 window，让 modal 的 event delegation handler 能调
  window.savePersonaForm = savePersonaForm;

  async function createPersona(action='create'){
    const rp = ((state.personas || {}).reply_profiles || {});
    const id = prompt(action === 'copy' ? window.T('msg_js_506')+' ID' : window.T('msg_js_507')+' ID');
    if (!id) return;
    const body = {action, id: id.trim()};
    if (action === 'copy') body.source_id = rp.default || ((rp.profiles || [])[0] || {}).id || '';
    const r = await apiPost('/api/messenger-rpa/strategy/personas', body);
    toast(action === 'copy' ? window.T('msg_js_508') : window.T('msg_js_509'));
    state.personas = {reply_profiles: r.reply_profiles || {}};
    qs('#personas-json').value = JSON.stringify(r.reply_profiles || {}, null, 2);
    renderPersonas(r.reply_profiles || {});
    await refreshStrategy();
  }

  async function togglePersona(pid){
    const rp = ((state.personas || {}).reply_profiles || {});
    const p = (rp.profiles || []).find(x => x && x.id === pid);
    const action = p && p.status === 'disabled' ? 'enable' : 'disable';
    if (action === 'disable' && !confirm(window.T('msg_js_510')+'？')) return;
    const r = await apiPost(`/api/messenger-rpa/strategy/personas/${encodeURIComponent(pid)}/${action}`, {});
    toast(action === 'enable' ? window.Tf('msg_js_p20',{id:pid}) : window.Tf('msg_js_p21',{id:pid}));
    state.personas = {reply_profiles: r.reply_profiles || {}};
    qs('#personas-json').value = JSON.stringify(r.reply_profiles || {}, null, 2);
    renderPersonas(r.reply_profiles || {});
    await refreshStrategy();
  }

  async function setDefaultPersona(pid){
    const r = await apiPost(`/api/messenger-rpa/strategy/personas/${encodeURIComponent(pid)}/set_default`, {});
    toast(window.Tf('msg_js_p22',{id:pid}));
    state.personas = {reply_profiles: r.reply_profiles || {}};
    qs('#personas-json').value = JSON.stringify(r.reply_profiles || {}, null, 2);
    renderPersonas(r.reply_profiles || {});
    await Promise.allSettled([refreshConfig(), refreshStrategy()]);
  }

  async function refreshStrategy(){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/strategy/runtime?limit=160');
    } catch(e) {
      qs('#strategy-states') && (qs('#strategy-states').innerHTML = `<div class="mr-empty">${window.T('msg_js_694')}: ${esc(e.message)}</div>`);
      return;
    }
    state.strategy = r;
    renderStrategyRuntime(r);
  }

  function renderStrategyRuntime(r){
    const s = r.summary || {};
    const heroEl = qs('#strategy-hero');
    if (heroEl) heroEl.innerHTML = [
      strategyCard(s.accounts || 0, window.T('msg_s073')),
      strategyCard(`${s.pending_jobs || 0}/${s.jobs || 0}`, window.T('msg_js_511')),
      strategyCard(s.conversation_states || 0, window.T('msg_s075')),
      strategyCard(`${s.avg_health || 0}`, window.T('msg_s076')),
    ].join('');
    renderStrategyStages(s.stage_counts || {});
    renderStrategyStates(r.conversation_states || []);
  }

  function strategyCard(value, label){
    return `<div class="mr-strategy-card"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
  }

  function renderStrategyStages(counts){
    const order = [
      ['new_lead',window.T('dash.tg.new_contacts')], ['greeting',window.T('msg_js_512')], ['qualification',window.T('msg_js_513')],
      ['education',window.T('msg_js_514')], ['objection_handling',window.T('msg_js_515')], ['offer',window.T('msg_js_516')],
      ['follow_up',window.T('msg_js_517')], ['handoff',window.T('msg_js_518')], ['closed_lost',window.T('dash.stage.LOST')],
    ];
    const max = Math.max(1, ...Object.values(counts).map(x => Number(x || 0)));
    qs('#strategy-stage-funnel').innerHTML = order.map(([key,label]) => {
      const v = Number(counts[key] || 0);
      const w = Math.max(4, Math.round(v / max * 100));
      const loss = key === 'closed_lost' || key === 'handoff';
      return `<div class="mr-funnel-row">
        <div class="mr-funnel-label">${esc(label)}</div>
        <div class="mr-bar"><div class="mr-bar-fill ${loss ? 'loss' : ''}" style="width:${w}%"></div></div>
        <div class="mr-muted">${v}</div>
      </div>`;
    }).join('');
  }

  function renderStrategyStates(rows){
    const root = qs('#strategy-states');
    if (!rows.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_695')+'。</div>';
      return;
    }
    root.innerHTML = rows.slice(0, 80).map(x => {
      const topics = x.recent_topics || [];
      const facts = x.used_persona_facts || [];
      return `<div class="mr-state-card">
        <div class="mr-persona-top">
          <div class="mr-name">${esc(short(x.chat_key || x.customer_id || '--', 42))}</div>
          <span class="mr-badge approved">${esc(x.stage || 'new_lead')}</span>
        </div>
        <div class="mr-muted">${esc(x.account_id || '--')} · ${esc(x.persona_id || '--')} · ${esc(x.customer_language || '--')} / ${esc(x.customer_type || '--')}</div>
        <div class="mr-note">${esc(short(x.memory_summary || window.T('msg_js_696'), 180))}</div>
        ${topics.length ? `<div class="mr-evidence">${topics.slice(0,6).map(t => `<span>${esc(t)}</span>`).join('')}</div>` : ''}
        ${facts.length ? `<div class="mr-muted" style="margin-top:.35rem">${window.T('msg_js_867')}：${esc(facts.slice(-3).join('；'))}</div>` : ''}
        <div class="mr-actions">
          <select data-state-stage="${esc(x.customer_id)}">
            ${['new_lead','greeting','qualification','education','objection_handling','offer','follow_up','handoff','closed_lost'].map(st => `<option value="${st}" ${st === x.stage ? 'selected' : ''}>${st}</option>`).join('')}
          </select>
          <button class="btn" data-state-action="edit_summary" data-customer-id="${esc(x.customer_id)}">${window.T('rpa_js_summary')}</button>
          <button class="btn" data-state-action="clear_memory" data-customer-id="${esc(x.customer_id)}">${window.T('msg_js_868')}</button>
          <button class="btn" data-state-action="clear_used_facts" data-customer-id="${esc(x.customer_id)}">${window.T('msg_js_869')}</button>
          <button class="btn" data-state-action="handoff" data-customer-id="${esc(x.customer_id)}">${window.T('msg_js_518')}</button>
        </div>
      </div>`;
    }).join('');
    qsa('[data-state-action]', root).forEach(btn => btn.addEventListener('click', () => updateConversationState(btn.dataset.customerId, btn.dataset.stateAction)));
    qsa('[data-state-stage]', root).forEach(sel => sel.addEventListener('change', () => updateConversationState(sel.dataset.stateStage, 'stage', sel.value)));
  }

  async function updateConversationState(customerId, action, stageValue=''){
    const body = {action};
    if (action === 'stage') {
      body.action = 'update';
      body.stage = stageValue;
    }
    if (action === 'edit_summary') {
      const cur = ((state.strategy || {}).conversation_states || []).find(x => x.customer_id === customerId) || {};
      const summary = prompt(window.T('msg_js_697'), cur.memory_summary || '');
      if (summary == null) return;
      body.action = 'update';
      body.memory_summary = summary;
    }
    if (action === 'clear_memory' && !confirm(window.T('msg_js_698')+'？')) return;
    if (action === 'clear_used_facts' && !confirm(window.T('msg_js_699')+'？')) return;
    if (action === 'handoff' && !confirm(window.T('msg_js_700')+'？')) return;
    await api(`/api/messenger-rpa/strategy/conversations/${encodeURIComponent(customerId)}`, {method:'PATCH', body});
    toast(window.T('msg_js_701'));
    await refreshStrategy();
  }

  async function savePersonas(){
    let body;
    try {
      body = JSON.parse(qs('#personas-json').value || '{}');
    } catch(e) {
      toast(window.T('msg_js_702')+': ' + e.message, 'err');
      return;
    }
    const r = await apiPut('/api/messenger-rpa/personas', body);
    const _def = (r.reply_profiles && r.reply_profiles.default) || (body.default || '');
    toast(window.Tf('msg_js_p23',{d:_def || window.T('msg_js_497')}));
    renderPersonas(r.reply_profiles || body);
    await Promise.allSettled([refreshConfig(), refreshStrategy()]);
  }

  async function getTemplates(){
    if (state.templates) return state.templates;
    try {
      const r = await apiGet('/api/messenger-rpa/templates');
      state.templates = r.templates || [];
    } catch(e) {
      state.templates = [];
    }
    return state.templates;
  }

  async function refreshApprovals(){
    let pending, all, tpls;
    try {
      [pending, all, tpls] = await Promise.all([
        apiGet('/api/messenger-rpa/approvals?status=pending&limit=80'),
        apiGet('/api/messenger-rpa/approvals?status=all&limit=160'),
        getTemplates(),
      ]);
    } catch(e) {
      /* P0 状态口径同款：人话主文案在前，技术细节（含 state_store 未注入类后端 detail）
         降级为小字注脚——运营先看懂"服务没启动"，工程师仍能看到根因。 */
      qs('#approvals-list').innerHTML = `<div class="mr-empty">${window.T('chc_svc_down_human')}`
        + `<span style="display:block;font-size:.72rem;color:var(--t3);margin-top:.25rem">${window.T('msg_js_870')}: ${esc(e.message)}</span></div>`;
      return;
    }
    const pList = pending.approvals || [];
    const allList = all.approvals || [];
    state.approvals = allList;
    const approved = allList.filter(a => a.status === 'approved');
    const now = Date.now() / 1000;
    const sent = allList.filter(a => a.status === 'sent' && now - Number(a.sent_at || 0) < 86400);
    qs('#kpi-pending').textContent = pList.length;
    qs('#kpi-sent').textContent = sent.length;
    qs('#qc-pending').textContent = pList.length;
    qs('#qc-approved').textContent = approved.length;
    qs('#qc-sent').textContent = sent.length;
    // Q3: 浏览器 title 角标
    if(window.rpa && window.rpa.notify) window.rpa.notify.setBadge(pList.length, 'FB '+window.T('ov_kpi_pending'));
    renderApprovals(allList.slice(0, 50), tpls);
  }

  function renderApprovals(list, tpls){
    const root = qs('#approvals-list');
    if (!list.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_703')+'</div>';
      updateBulkButtons();
      return;
    }
    const tplButtons = (aid) => tpls.length ? `<div class="mr-actions">
      ${tpls.slice(0,6).map((t,i) => `<button class="btn btn-tpl" data-id="${aid}" data-idx="${i}">${esc(t.label || (window.T('inbox.tpl_default') + (i+1)))}</button>`).join('')}
    </div>` : '';
    root.innerHTML = list.map(a => {
      const isPending = a.status === 'pending';
      const age = Math.max(0, Math.floor(Date.now()/1000 - Number(a.created_at || Date.now()/1000)));
      const cls = esc(a.status || '');
      return `<div class="mr-appr-row ${cls}">
        <div class="mr-appr-meta">
          ${isPending ? `<input type="checkbox" class="pick-approval" data-id="${a.id}">` : ''}
          <span class="mr-badge ${cls}">${esc(a.status || '--')}</span>
          <span>#${esc(a.id)}</span>
          <span>${esc(a.chat_name || a.chat_key || '--')}</span>
          <span>${fmtTs(a.created_at)}</span>
          ${isPending ? `<span class="${age > 600 ? 'mr-badge rejected' : 'mr-muted'}">${age}s</span>` : ''}
          <span class="push">${esc(a.reply_lang || '')}</span>
        </div>
        <div class="mr-peer"><span class="mr-muted">${window.T('msg_js_873')}：</span>${esc(a.peer_text || '')}</div>
        ${isPending ? `
          <textarea class="mr-edit" data-id="${a.id}" placeholder="${window.T('msg_js_871')}">${esc(a.reply_text || '')}</textarea>
          ${tplButtons(a.id)}
          <div class="mr-actions">
            <button class="btn btn-primary btn-approve" data-id="${a.id}">${window.T('msg_js_707')}</button>
            <button class="btn btn-suggest" data-id="${a.id}">${window.T('msg_js_872')}</button>
            <button class="btn btn-reject" data-id="${a.id}">${window.T('inbox.autolog.rejected')}</button>
          </div>
        ` : `<div class="mr-reply"><span class="mr-muted">${window.T('ov_js_replies_unit')}：</span>${esc(a.reply_text || '')}</div>`}
      </div>`;
    }).join('');
    qsa('.pick-approval', root).forEach(cb => cb.addEventListener('change', updateBulkButtons));
    qsa('.btn-tpl', root).forEach(b => b.addEventListener('click', () => {
      const ta = qs(`.mr-edit[data-id="${b.dataset.id}"]`, root);
      const t = tpls[Number(b.dataset.idx)] || {};
      if (ta) ta.value = t.text || '';
    }));
    qsa('.btn-approve', root).forEach(b => b.addEventListener('click', () => approveOne(b.dataset.id)));
    qsa('.btn-suggest', root).forEach(b => b.addEventListener('click', () => suggestOne(b.dataset.id)));
    qsa('.btn-reject', root).forEach(b => b.addEventListener('click', () => rejectOne(b.dataset.id)));
    updateBulkButtons();
  }

  function updateBulkButtons(){
    const picked = qsa('.pick-approval:checked').length;
    qs('#btn-bulk-approve').disabled = picked === 0;
    qs('#btn-bulk-reject').disabled = picked === 0;
    qs('#btn-bulk-approve').textContent = window.Tf('msg_js_p24',{n:picked});
    qs('#btn-bulk-reject').textContent = window.Tf('msg_js_p25',{n:picked});
  }

  async function approveOne(id){
    if (!(await confirm2(window.Tf('msg_js_p26',{id:id})))) return;
    const ta = qs(`.mr-edit[data-id="${id}"]`);
    const body = {decided_by:'web'};
    if (ta && ta.value.trim()) body.reply_text = ta.value.trim();
    const r = await apiPost(`/api/messenger-rpa/approvals/${encodeURIComponent(id)}/approve`, body);
    const send = r.send || {};
    toast(window.Tf('msg_js_p27',{a:!!send.ok,b:send.step || ''}));
    await refreshAllLight();
  }

  async function suggestOne(id){
    const r = await apiPost(`/api/messenger-rpa/approvals/${encodeURIComponent(id)}/suggest`, {});
    const ta = qs(`.mr-edit[data-id="${id}"]`);
    if (ta && r.suggestion) ta.value = r.suggestion;
    toast(window.T('msg_js_704'));
  }

  async function rejectOne(id){
    const note = prompt(window.T('msg_js_705')) || '';
    await apiPost(`/api/messenger-rpa/approvals/${encodeURIComponent(id)}/reject`, {decided_by:'web', note});
    toast(window.T('msg_js_706'));
    await refreshApprovals();
  }

  async function bulkDecision(action){
    const ids = qsa('.pick-approval:checked').map(cb => Number(cb.dataset.id)).filter(Boolean);
    if (!ids.length) return;
    const label = action === 'approve' ? window.T('msg_js_707') : window.T('inbox.autolog.rejected');
    if (!(await confirm2(window.Tf('msg_js_p28',{label:label,n:ids.length})))) return;
    const r = await apiPost('/api/messenger-rpa/approvals/batch', {ids, action, decided_by:'web-bulk'});
    toast(window.Tf('msg_js_p29',{s:(r.succeeded_ids || []).length,f:(r.failed || []).length}));
    await refreshAllLight();
  }

  async function refreshRuns(){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/recent?limit=80');
    } catch(e) {
      qs('#overview-runs').innerHTML = `<div class="mr-empty">${window.T('msg_js_1033')}</div>`;
      qs('#runs-table').innerHTML = `<div class="mr-empty">${window.T('msg_js_1033')}: ${esc(e.message)}</div>`;
      return;
    }
    state.runs = r.runs || [];
    qs('#kpi-runs').textContent = state.runs.length;
    renderRuns();
  }

  function renderRuns(){
    const list = state.runs || [];
    const overview = qs('#overview-runs');
    overview.innerHTML = list.length ? list.slice(0,8).map(runRowCompact).join('') : '<div class="mr-empty">'+window.T('msg_js_874')+'</div>';
    const root = qs('#runs-table');
    if (!list.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_874')+'</div>';
      return;
    }
    root.innerHTML = `<div class="mr-scroll tall"><table class="mr-table">
      <thead><tr><th>${window.T('time')}</th><th>${window.T('msg_js_1034')}</th><th>${window.T('tour_step')}</th><th>${window.T('draft.q.lang_match')}</th><th>${window.T('msg_js_1035')}</th><th>${window.T('msg_js_1036')}</th></tr></thead>
      <tbody>${list.map(run => `<tr>
        <td>${fmtTs(run.ts)}</td>
        <td><b>${esc(run.chat_name || run.chat_key || '--')}</b></td>
        <td><span class="mr-badge ${run.ok ? 'sent' : 'rejected'}">${esc(run.step || '--')}</span></td>
        <td>${_langBadge(run.reply_lang || '')}</td>
        <td>${esc(short(run.peer_text || '', 120))}</td>
        <td>${esc(short(run.reply_text || run.error || '', 160))}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
  }

  function runRowCompact(run){
    return `<div class="mr-row">
      <div style="min-width:0">
        <div class="mr-name">${esc(run.chat_name || run.chat_key || '--')} ${run.ok && run.reply_lang ? _langBadge(run.reply_lang) : ''}</div>
        <div class="mr-muted">${esc(short(run.peer_text || run.error || '', 70))}</div>
      </div>
      <div style="text-align:right">
        <span class="mr-badge ${run.ok ? 'sent' : 'rejected'}">${esc(run.step || '--')}</span>
        <div class="mr-muted">${fmtTs(run.ts)}</div>
      </div>
    </div>`;
  }

  // B6-P4: 数据中心 sub-tab 切换 + 各 pane loader
  function setDcPane(name){
    qsa('.dc-tab').forEach(b => b.classList.toggle('active', b.dataset.dc === name));
    qsa('.dc-pane').forEach(p => {
      const on = p.dataset.dcPane === name;
      p.classList.toggle('active', on);
      p.style.display = on ? '' : 'none';
    });
    loadDcPane(name).catch(e => toast(e.message, 'err'));
  }
  // P9-D: 聊天历史面板（lazy init，仅首次激活时挂载工厂）
  let _mrHistoryInited = false;
  function _initMrHistoryOnce(){
    if (_mrHistoryInited) return;
    if (!(window.rpa && window.rpa.analytics)) return;
    _mrHistoryInited = true;
    rpa.analytics.initSearch({
      inputId:'mr-hist-q', resultId:'mr-hist-results',
      apiBase:'/api/messenger-rpa', days:30, limit:30,
      onPick:function(ck){
        // P10-A: 打开通用会话抽屉
        if(window.rpa && rpa.analytics) rpa.analytics.openDetail({apiBase:'/api/messenger-rpa', chatKey:ck});
      }
    });
    rpa.analytics.intentChart({bodyId:'mr-intent-body', apiBase:'/api/messenger-rpa', defaultHours:168});
  }
  async function loadDcPane(name){
    if (name === 'runs') return refreshRuns();
    if (name === 'history'){ _initMrHistoryOnce(); return; }
    if (name === 'cost') return refreshDcCost();
    if (name === 'variants') return refreshDcVariants();
    if (name === 'accounts-active') return refreshDcAccounts();
    if (name === 'health') return refreshDcHealth();
  }
  // P11.6.1 (2026-05-04)：健康指标面板 30s 自动刷新（仅当 health pane 可见时）
  let _hcRefreshTimer = null;
  function _ensureHealthAutoRefresh(){
    if (_hcRefreshTimer) return;
    _hcRefreshTimer = setInterval(() => {
      const pane = qs('.dc-pane[data-dc-pane="health"]');
      if (pane && pane.style.display !== 'none') {
        refreshDcHealth().catch(() => {});
      }
    }, 30000);
  }

  // P2++G2 (2026-05-04) sticker decision tile —— 在健康指标面板顶部展示
  // modality 类型分布 / 决策原因分布 / hit 时 cat 命中分布
  function _renderStickerDecisionTile(root, cum, win) {
    const modalityTypes = ['modality_text', 'modality_sticker', 'modality_text_with_sticker'];
    const reasonBases = ['hit', 'cooldown', 'disabled', 'prob_skip', 'prob_zero', '24h_cap', 'asset_missing', 'error', 'unknown'];
    const cats = ['love', 'happy', 'wink', 'cute', 'thinking', 'awkward', 'sad', 'angry'];

    const totalDecisions = (cum['modality'] || 0);
    const winDecisions = (win['modality'] || 0);
    if (totalDecisions === 0) {
      root.innerHTML = `<div class="dc-card" style="background:var(--th-bg-ind06,rgba(99,102,241,.06));padding:10px 14px">
        <div class="lbl">🎨 sticker ${window.T('msg_js_1037')}</div>
        <div class="sub" style="margin-top:4px">${window.T('msg_js_1038')} LLM reply）</div>
      </div>`;
      return;
    }

    // modality 类型分布
    const modRows = modalityTypes.map(k => {
      const c = cum[k] || 0; const w = win[k] || 0;
      if (c === 0 && w === 0) return '';
      const pct = totalDecisions ? ((c / totalDecisions) * 100).toFixed(0) : 0;
      const label = k.replace('modality_', '').replace(/_with_/, '+');
      return `<tr><td><b>${label}</b></td>
        <td style="text-align:right">${c} <span style="color:var(--t3)">(${pct}%)</span></td>
        <td style="text-align:right;color:var(--th-ink-blue5,#1d4ed8)">${w}</td></tr>`;
    }).filter(Boolean).join('');

    // reason 分布
    const reasonRows = reasonBases.map(b => {
      const k = `modality_reason_${b}`;
      const c = cum[k] || 0; const w = win[k] || 0;
      if (c === 0 && w === 0) return '';
      const pct = totalDecisions ? ((c / totalDecisions) * 100).toFixed(0) : 0;
      const color = b === 'hit' ? '#10b981' : (b === 'error' ? '#f97316' : 'var(--t)');
      return `<tr><td style="color:${color}"><b>${b}</b></td>
        <td style="text-align:right">${c} <span style="color:var(--t3)">(${pct}%)</span></td>
        <td style="text-align:right;color:var(--th-ink-blue5,#1d4ed8)">${w}</td></tr>`;
    }).filter(Boolean).join('');

    // cat 分布（hit 命中时的 sticker 类）
    const catRows = cats.map(c => {
      const k = `modality_cat_${c}`;
      const cv = cum[k] || 0; const wv = win[k] || 0;
      if (cv === 0 && wv === 0) return '';
      const emojiMap = {
        love: '❤️', happy: '😄', wink: '😉', cute: '🥰',
        thinking: '🤔', awkward: '😅', sad: '😢', angry: '😠'
      };
      return `<tr><td>${emojiMap[c] || ''} <b>${c}</b></td>
        <td style="text-align:right">${cv}</td>
        <td style="text-align:right;color:var(--th-ink-blue5,#1d4ed8)">${wv}</td></tr>`;
    }).filter(Boolean).join('');

    const hitRate = totalDecisions ? (((cum['modality_reason_hit'] || 0) / totalDecisions) * 100).toFixed(1) : 0;
    const winHitRate = winDecisions ? (((win['modality_reason_hit'] || 0) / winDecisions) * 100).toFixed(1) : 0;

    root.innerHTML = `
      <div class="dc-card" style="background:var(--th-bg-ind06,rgba(99,102,241,.06));padding:14px;border-radius:8px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <span style="font-size:18px">🎨</span>
          <span style="font-weight:600;color:var(--t)">sticker ${window.T('msg_js_1037')}（dry_run ${window.T('msg_js_1039')}）</span>
          <span style="margin-left:auto;font-size:.75rem;color:var(--t3)">${window.T('msg_js_1040')} ${hitRate}% · 1h ${winHitRate}%</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          <div>
            <div style="font-size:.78rem;color:var(--t3);margin-bottom:4px">modality ${window.T('msg_js_1041')}</div>
            <table class="mr-table" style="width:100%;font-size:.8rem">
              <thead><tr><th>${window.T('msg_js_1042')}</th><th style="text-align:right">${window.T('msg_js_1043')}</th><th style="text-align:right">1h</th></tr></thead>
              <tbody>${modRows || '<tr><td colspan="3" style="color:var(--t3);text-align:center">'+window.T('dash.none')+'</td></tr>'}</tbody>
            </table>
          </div>
          <div>
            <div style="font-size:.78rem;color:var(--t3);margin-bottom:4px">${window.T('msg_js_1044')}</div>
            <table class="mr-table" style="width:100%;font-size:.8rem">
              <thead><tr><th>reason</th><th style="text-align:right">${window.T('msg_js_1043')}</th><th style="text-align:right">1h</th></tr></thead>
              <tbody>${reasonRows || '<tr><td colspan="3" style="color:var(--t3);text-align:center">'+window.T('dash.none')+'</td></tr>'}</tbody>
            </table>
          </div>
          <div>
            <div style="font-size:.78rem;color:var(--t3);margin-bottom:4px">${window.T('msg_js_1045')}</div>
            <table class="mr-table" style="width:100%;font-size:.8rem">
              <thead><tr><th>cat</th><th style="text-align:right">${window.T('msg_js_1043')}</th><th style="text-align:right">1h</th></tr></thead>
              <tbody>${catRows || '<tr><td colspan="3" style="color:var(--t3);text-align:center">'+window.T('msg_js_875')+' hit</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  }

  // P11.6 (2026-05-04) 健康指标面板
  async function refreshDcHealth(){
    _ensureHealthAutoRefresh();
    const root = qs('#dc-health-metrics');
    const stickerRoot = qs('#dc-sticker-metrics');
    let r;
    try { r = await apiGet('/api/messenger-rpa/hint-metrics?window=3600'); }
    catch(e){
      root.innerHTML = `<div class="mr-empty">${window.T('msg_js_1203')}: ${esc(e.message)}</div>`;
      if (stickerRoot) stickerRoot.innerHTML = '';
      return;
    }
    const cum = r.metrics || {};
    const win = r.window_metrics || {};
    const names = Array.from(new Set([...Object.keys(cum), ...Object.keys(win)]));
    // P2++G2: 单独渲染 sticker decision tile（modality_* / 决策原因 / cat 命中分布）
    if (stickerRoot) {
      _renderStickerDecisionTile(stickerRoot, cum, win);
    }
    if (!names.length){
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1046')+'）</div>';
      return;
    }
    // 按累计降序 + 突显窗口频率（>=2 标蓝，>=5 标橙）
    names.sort((a,b)=>(cum[b]||0)-(cum[a]||0));
    const totalCum = r.total_events || 0;
    const totalWin = r.window_total || 0;
    const rows = names.map(n=>{
      const c = cum[n]||0; const w = win[n]||0;
      const wColor = w>=5 ? '#f97316' : (w>=2 ? '#3b82f6' : 'var(--t3)');
      return `<tr><td style="font-family:monospace;font-size:.78rem">${esc(n)}</td>
        <td style="text-align:right;font-weight:700">${c}</td>
        <td style="text-align:right;color:${wColor};font-weight:700">${w}</td></tr>`;
    }).join('');
    const _ts = window.wsFmtTime(null);
    root.innerHTML = `
      <div style="display:flex;gap:14px;margin-bottom:10px">
        <div class="dc-card" style="flex:1"><div class="lbl">${window.T('msg_js_1204')}</div><div class="val">${totalCum}</div><div class="sub">${names.length} ${window.T('msg_js_1205')}</div></div>
        <div class="dc-card" style="flex:1"><div class="lbl">${window.T('msg_js_1206')} 1h</div><div class="val">${totalWin}</div><div class="sub">${window.T('msg_js_1207')}</div></div>
        <div class="dc-card" style="flex:1"><div class="lbl">${window.T('msg_js_1208')}</div><div class="val" style="font-size:14px">${_ts}</div><div class="sub">${window.T('msg_js_1209')}</div></div>
      </div>
      <table class="mr-table" style="width:100%">
        <thead><tr><th>${window.T('msg_js_1210')} (hint base)</th><th style="text-align:right">${window.T('msg_js_1043')}</th><th style="text-align:right">${window.T('msg_js_1206')} 1h</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div style="margin-top:14px;padding:10px;background:var(--th-bg-ind06,rgba(99,102,241,.06));border-radius:6px;font-size:.78rem;color:var(--t3)">
        Prometheus ${window.T('msg_js_1211')}：<code style="background:rgba(0,0,0,.18);padding:2px 6px;border-radius:3px">GET /api/messenger-rpa/metrics</code>
        ${window.T('msg_js_1212')} <code>messenger_rpa_hint_total{name="..."}</code> counter，${window.T('msg_js_1213')} Grafana。
      </div>
    `;
  }
  // 成本看板
  async function refreshDcCost(){
    const root = qs('#dc-cost-table'); const summary = qs('#dc-cost-summary');
    summary.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1047')+'...</div>';
    let r;
    try { r = await apiGet('/api/messenger-rpa/llm-cost'); }
    catch(e){
      summary.innerHTML = '';
      root.innerHTML = `<div class="mr-empty">${window.T('msg_js_1214')}: ${esc(e.message)}</div>`;
      return;
    }
    // r 通常含 totals + per_provider/per_model 分桶
    const totals = r.totals || r.summary || r;
    const totIn = totals.input_tokens || totals.prompt_tokens || 0;
    const totOut = totals.output_tokens || totals.completion_tokens || 0;
    const totCost = totals.cost_usd || totals.cost || 0;
    const reqCount = totals.requests || totals.calls || 0;
    summary.innerHTML = [
      `<div class="dc-card"><div class="lbl">${window.T('msg_js_1215')}</div><div class="val">${reqCount}</div></div>`,
      `<div class="dc-card"><div class="lbl">${window.T('inbox.prompt.title')} tokens</div><div class="val">${(totIn/1000).toFixed(1)}K</div></div>`,
      `<div class="dc-card"><div class="lbl">${window.T('msg_js_1216')} tokens</div><div class="val">${(totOut/1000).toFixed(1)}K</div></div>`,
      `<div class="dc-card"><div class="lbl">${window.T('msg_js_1217')}</div><div class="val" style="color:var(--th-ink-emerald5,#047857)">$${Number(totCost).toFixed(4)}</div></div>`,
    ].join('');
    // per model 表格
    const buckets = r.per_model || r.models || r.per_provider || {};
    const rows = Object.entries(buckets);
    if (!rows.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1048')+'）</div>';
      return;
    }
    rows.sort((a, b) => (b[1].cost_usd || b[1].cost || 0) - (a[1].cost_usd || a[1].cost || 0));
    root.innerHTML = `<div class="mr-scroll tall"><table class="mr-table">
      <thead><tr><th>${window.T('msg_js_1218')} / Provider</th><th style="text-align:right">${window.T('msg_js_1219')}</th><th style="text-align:right">${window.T('inbox.prompt.title')} tokens</th><th style="text-align:right">${window.T('msg_js_1216')} tokens</th><th style="text-align:right">${window.T('msg_js_1220')}</th></tr></thead>
      <tbody>${rows.map(([k, v]) => `<tr>
        <td><b>${esc(k)}</b></td>
        <td style="text-align:right">${v.requests || v.calls || 0}</td>
        <td style="text-align:right">${(v.input_tokens || v.prompt_tokens || 0).toLocaleString()}</td>
        <td style="text-align:right">${(v.output_tokens || v.completion_tokens || 0).toLocaleString()}</td>
        <td style="text-align:right;color:var(--th-ink-emerald5,#047857)">$${Number(v.cost_usd || v.cost || 0).toFixed(4)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }
  // A/B 人设效果
  async function refreshDcVariants(){
    const root = qs('#dc-variants-table');
    let r;
    try { r = await apiGet('/api/messenger-rpa/variants/stats'); }
    catch(e){
      root.innerHTML = `<div class="mr-empty">${window.T('msg_js_1221')}: ${esc(e.message)}</div>`;
      return;
    }
    const variants = r.variants || r.per_variant || {};
    const rows = Object.entries(variants);
    const enabled = r.experiment_enabled;
    const banner = enabled
      ? '<div style="padding:8px 12px;background:var(--th-bg-grn10,rgba(16,185,129,0.1));border:1px solid var(--th-bd-emerald5,#10b981);border-radius:6px;color:var(--th-ink-emerald5,#047857);margin-bottom:12px;font-size:12px">✓ '+window.T('msg_js_1049')+' — '+window.T('msg_js_1050')+' messenger_rpa.persona_experiment</div>'
      : '<div style="padding:8px 12px;background:var(--th-bg-amber10,rgba(245,158,11,0.1));border:1px solid var(--th-bd-amber5,#f59e0b);border-radius:6px;color:var(--th-ink-amber5,#92400e);margin-bottom:12px;font-size:12px">⚠ '+window.T('msg_js_1051')+' — '+window.T('msg_js_1052')+' ⚙️ '+window.T('strategies')+' → persona_experiment.enabled '+window.T('ov_js_enable')+'</div>';
    if (!rows.length) {
      root.innerHTML = banner + '<div class="mr-empty">'+window.T('msg_js_1053')+'</div>';
      return;
    }
    const maxSent = Math.max(...rows.map(([_, v]) => v.sent || v.send_count || 0)) || 1;
    root.innerHTML = banner + `<div class="mr-scroll tall"><table class="mr-table">
      <thead><tr><th>${window.T('msg_js_1222')}</th><th style="text-align:right">${window.T('rpa_js_st_sent')}</th><th>${window.T('msg_js_1223')}</th><th style="text-align:right">${window.T('msg_js_1224')}</th><th style="text-align:right">${window.T('msg_js_1225')}</th><th style="text-align:right">${window.T('msg_js_1226')}</th></tr></thead>
      <tbody>${rows.map(([k, v]) => {
        const sent = v.sent || v.send_count || 0;
        const skipped = v.skipped || v.skip_count || 0;
        const rejected = v.rejected || v.reject_count || 0;
        const total = sent + rejected;
        const passRate = total ? (sent / total * 100).toFixed(1) : '--';
        const w = (sent / maxSent * 100);
        return `<tr>
          <td><b>${esc(k)}</b></td>
          <td style="text-align:right">${sent}</td>
          <td><span class="dc-bar-cell" style="width:${w}%"></span><span class="mr-muted">${w.toFixed(0)}%</span></td>
          <td style="text-align:right">${skipped}</td>
          <td style="text-align:right">${rejected}</td>
          <td style="text-align:right;color:${passRate >= 80 ? 'var(--th-ink-emerald5,#047857)' : passRate >= 50 ? 'var(--th-ink-amber5,#92400e)' : 'var(--th-ink-red5,#b91c1c)'}">${passRate}%</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }
  // 账号活跃
  async function refreshDcAccounts(){
    const root = qs('#dc-accounts-active');
    let st;
    try { st = await apiGet('/api/messenger-rpa/status'); }
    catch(e){
      root.innerHTML = `<div class="mr-empty">${window.T('msg_js_1227')}: ${esc(e.message)}</div>`;
      return;
    }
    const accounts = st.accounts || st.account_status || {};
    const accountsArr = Array.isArray(accounts) ? accounts : Object.entries(accounts).map(([k, v]) => ({account_id: k, ...v}));
    if (!accountsArr.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1054')+' — '+window.T('msg_js_1055')+' messenger_rpa.accounts '+window.T('msg_js_1056')+'</div>';
      return;
    }
    const maxSent = Math.max(...accountsArr.map(a => (a.sends_today || a.send_counters?.today || 0))) || 1;
    root.innerHTML = `<div class="mr-scroll tall"><table class="mr-table">
      <thead><tr><th>${window.T('ov_js_th_account')}</th><th>${window.T('ov_js_th_status')}</th><th style="text-align:right">${window.T('msg_js_263')}</th><th>${window.T('msg_js_1223')}</th><th style="text-align:right">${window.T('msg_js_1228')}</th><th style="text-align:right">${window.T('msg_js_1229')}</th></tr></thead>
      <tbody>${accountsArr.map(a => {
        const sent = a.sends_today || a.send_counters?.today || 0;
        const empty = a.consecutive_empty || 0;
        const unhealthy = a.consecutive_unhealthy || 0;
        const running = a.running !== false;
        const w = (sent / maxSent * 100);
        return `<tr>
          <td><b>${esc(a.account_id || a.id || '--')}</b></td>
          <td>${running ? '<span class="mr-badge sent">'+window.T('ov_js_stage_active')+'</span>' : '<span class="mr-badge rejected">'+window.T('ov_ctrl_stop')+'</span>'}</td>
          <td style="text-align:right">${sent}</td>
          <td><span class="dc-bar-cell" style="width:${w}%"></span><span class="mr-muted">${w.toFixed(0)}%</span></td>
          <td style="text-align:right">${empty}</td>
          <td style="text-align:right;color:${unhealthy >= 5 ? 'var(--th-ink-red5,#b91c1c)' : unhealthy >= 2 ? 'var(--th-ink-amber5,#92400e)' : 'var(--t2)'}">${unhealthy}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }

  async function refreshAccounts(deep=false){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/accounts/health' + (deep ? '?deep=true' : ''));
    } catch(e) {
      qs('#accounts-summary').textContent = window.T('msg_js_1057');
      qs('#accounts-list').innerHTML = `<div class="mr-empty">${window.T('msg_js_1230')}: ${esc(e.message)}</div>`;
      qs('#kpi-online').textContent = '0/0';
      return;
    }
    state.accounts = r.accounts || {};
    const keys = Object.keys(state.accounts);
    const safe = r.safe_count || 0;
    const total = r.total || keys.length;
    qs('#kpi-online').textContent = `${safe}/${total}`;
    qs('#accounts-summary').textContent = window.Tf('msg_js_p30',{safe:safe,total:total});
    renderAccounts(keys);
  }

  async function refreshMobileAutoStatus(){
    try {
      const r = await apiGet('/api/messenger-rpa/mobile-auto/status');
      state.mobileAuto = r;
    } catch(e) {
      state.mobileAuto = {errors:{status:e.message}, accounts:[], summary:{}};
    }
    refreshHostFilterOptions();
    renderAccounts(Object.keys(state.accounts || {}));
  }

  function mobileStatusByAccount(){
    const rows = ((state.mobileAuto || {}).accounts || []);
    return new Map(rows.map(r => [String(r.account_id || ''), r]));
  }

  function mobileDeviceRows(){
    return ((state.mobileAuto || {}).devices || []).filter(r => r && r.row_id);
  }

  function mobileDeviceByRow(){
    return new Map(mobileDeviceRows().map(r => [String(r.row_id || ''), r]));
  }

  function isDeviceOnlyId(id){
    return String(id || '').startsWith('device:');
  }

  function deviceHost(ms){
    return String((ms || {}).host_name || ((ms || {}).is_cluster ? 'Worker' : window.T('msg_js_1231')) || window.T('msg_js_1231')).trim();
  }

  function refreshHostFilterOptions(){
    const el = qs('#pool-host-filter');
    if (!el) return;
    const hosts = new Set();
    mobileDeviceRows().forEach(d => hosts.add(deviceHost(d)));
    ((state.mobileAuto || {}).accounts || []).forEach(d => hosts.add(deviceHost(d)));
    const current = state.poolHost || 'all';
    el.innerHTML = '<option value="all">'+window.T('msg_s045')+'</option>' + Array.from(hosts).filter(Boolean).sort().map(h => (
      `<option value="${esc(h)}" ${h === current ? 'selected' : ''}>${esc(h)}</option>`
    )).join('');
    if (current !== 'all' && !hosts.has(current)) {
      state.poolHost = 'all';
      el.value = 'all';
    }
  }

  function batteryChip(level){
    if (level == null || level === '') return '';
    const n = Math.max(0, Math.min(100, Number(level) || 0));
    const cls = n > 50 ? '' : n > 20 ? 'mid' : 'low';
    const chipCls = n > 20 ? 'ok' : 'bad';
    return `<span class="mr-chip ${chipCls} mr-battery"><span class="mr-battery-bar"><span class="mr-battery-fill ${cls}" style="width:${n}%"></span></span>${n}%</span>`;
  }

  function taskChip(ms){
    const status = (ms || {}).task_status || 'idle';
    if (status === 'running') return `<span class="mr-chip warn">${window.T('msg_js_1376')} ${ms.active_task_count || 0}</span>`;
    if (status === 'pending') return `<span class="mr-chip warn">${window.T('msg_js_1377')} ${ms.active_task_count || 0}</span>`;
    return '<span class="mr-chip ok">'+window.T('msg_js_1232')+'</span>';
  }

  function inferDeviceNumber(...values){
    for (const raw of values) {
      const s = String(raw == null ? '' : raw).trim();
      if (!s || s === '--') continue;
      const m = s.match(/(?:^|[^A-Za-z0-9])(\d{1,2})(?:\u53f7|$|[^A-Za-z0-9])/);
      if (m) return m[1].padStart(2, '0');
    }
    return '';
  }

  function nextDeviceSuggestion({deviceOnly, online, ms, hasIssue}){
    if (deviceOnly) return window.T('msg_js_1233');
    if (!online) return window.T('msg_js_1234');
    if (!((ms || {}).vpn_connected)) return window.T('msg_js_1235')+' VPN';
    if (Number((ms || {}).active_task_count || 0) > 0) return window.T('msg_js_1236');
    if (hasIssue) return window.T('msg_js_1237');
    return '';
  }

  function bindingByAccount(){
    const bindings = ((state.bindings || {}).bindings || []);
    return new Map(bindings.map(b => [String(b.account_id || b.id || ''), b]));
  }

  function visiblePoolIds(){
    return qsa('#accounts-list .mr-device-card[data-aid]:not(.device-only)').map(x => x.dataset.aid).filter(Boolean);
  }

  function updatePoolSelectedUI(){
    const ids = visiblePoolIds();
    const visible = new Set(ids);
    qsa('#accounts-list .mr-device-card[data-aid]').forEach(card => {
      const on = state.poolSelected.has(card.dataset.aid);
      card.classList.toggle('selected', on);
      const cb = qs('.mr-device-pick', card);
      if (cb) cb.checked = on;
    });
    state.poolSelected.forEach(id => {
      if (!visible.has(id) && !((state.accounts || {})[id])) state.poolSelected.delete(id);
    });
    const el = qs('#pool-selected-count');
    if (el) el.textContent = window.Tf('msg_js_p31',{n:state.poolSelected.size});
    const bar = qs('#pool-selection-bar');
    if (bar) {
      bar.style.display = state.poolSelected.size ? 'flex' : 'none';
      bar.classList.toggle('advanced-mode', !!state.poolAdvanced);
    }
    qs('#pool-toolbar')?.classList.toggle('advanced-mode', !!state.poolAdvanced);
    qs('#accounts-list')?.classList.toggle('advanced-mode', !!state.poolAdvanced);
    qs('#accounts-layout')?.classList.toggle('advanced-mode', !!state.poolAdvanced);
  }

  function setPoolSelected(ids, on=true){
    ids.forEach(id => {
      if (!id) return;
      if (on) state.poolSelected.add(id);
      else state.poolSelected.delete(id);
    });
    updatePoolSelectedUI();
  }

  function renderAccounts(keys){
    const root = qs('#accounts-list');
    const bindings = ((state.bindings || {}).bindings || []);
    const conflicts = ((state.bindings || {}).conflicts || []);
    const byAccount = new Map(bindings.map(b => [String(b.account_id || b.id || ''), b]));
    const byMobile = mobileStatusByAccount();
    const byDevice = mobileDeviceByRow();
    const boundSerials = new Set(bindings.map(b => String(b.adb_serial || '').trim()).filter(Boolean));
    const unboundDeviceRows = mobileDeviceRows().filter(d => {
      const serial = String(d.adb_serial || d.device_id || '').trim();
      return serial && !boundSerials.has(serial) && !String(d.account_id || '').trim();
    });
    const ids = Array.from(new Set([
      ...bindings.map(b => String(b.account_id || b.id || '')).filter(Boolean),
      ...keys,
      ...unboundDeviceRows.map(d => String(d.row_id || '')).filter(Boolean),
    ]));
    const syncEl = qs('#mobile-auto-sync');
    if (syncEl) {
      const ma = (state.bindings || {}).mobile_auto || {};
      const ds = (state.bindings || {}).device_summary || {};
      const ms = (state.mobileAuto || {}).summary || {};
      syncEl.textContent = ma.root_path
        ? window.Tf('msg_js_p32',{on:ms.devices_online ?? 0,tot:ms.devices_total || ds.total || 0,w:ms.cluster_devices || 0,v:ms.vpn_connected || 0,t:ms.tasks_active || 0,h:ms.health_avg || 0})
        : 'mobile-auto '+window.T('msg_js_1238');
    }
    if (!ids.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1239')+'</div>';
      return;
    }
    let cards = ids.map(aid => {
      const deviceOnly = isDeviceOnlyId(aid);
      const a = deviceOnly ? {} : (state.accounts[aid] || {});
      const b = deviceOnly ? {} : (byAccount.get(aid) || {});
      const ms = deviceOnly ? (byDevice.get(aid) || {}) : (byMobile.get(aid) || {});
      let cls = a.present ? 'online' : 'offline';
      let text = a.present ? 'online' : (a.adb_state || 'offline');
      if (deviceOnly) {
        cls = ms.online ? 'online' : 'offline';
        text = ms.device_status || (ms.online ? 'online' : 'offline');
      } else if (a.ui_unsafe) { cls = 'warn'; text = 'UI '+window.T('msg_js_1240'); }
      else if (a.paused) { cls = 'paused'; text = a.paused_left_sec ? window.Tf('msg_js_p33',{n:Math.ceil(a.paused_left_sec)}) : window.T('ov_ctrl_pause'); }
      const host = deviceHost(ms);
      const bound = deviceOnly ? false : !!(b.adb_serial && (b.device_number || b.mobile_device_id));
      const online = deviceOnly ? !!ms.online : (cls === 'online' || !!ms.online);
      const hasIssue = cls === 'offline' || cls === 'warn' || cls === 'paused' || (!deviceOnly && (!b.adb_serial || b.persona_exists === false));
      const suggestion = nextDeviceSuggestion({deviceOnly, online, ms, hasIssue});
      if (state.poolHost !== 'all' && host !== state.poolHost) return '';
      if (state.poolFilter === 'online' && !online) return '';
      if (state.poolFilter === 'issue' && !hasIssue) return '';
      if (state.poolFilter === 'unbound' && bound) return '';
      if (state.poolFilter === 'offline' && online) return '';
      if (state.poolFilter === 'vpn' && ms.vpn_connected) return '';
      if (state.poolFilter === 'task' && !(Number(ms.active_task_count || 0) > 0)) return '';
      const info = [
        a.screen_on == null ? '' : window.Tf('msg_js_p34',{x:a.screen_on ? window.T('msg_js_p38') : window.T('msg_js_p39')}),
        a.locked == null ? '' : window.Tf('msg_js_p35',{x:a.locked ? window.T('yes') : window.T('no')}),
        a.adbkeyboard_installed == null ? '' : window.Tf('msg_js_p36',{x:a.adbkeyboard_installed ? window.T('msg_js_p40') : window.T('msg_js_p41')}),
        a.last_run_step ? window.Tf('msg_js_p37',{x:a.last_run_step}) : '',
      ].filter(Boolean).join(' · ');
      const serial = String(ms.adb_serial || ms.device_id || b.adb_serial || a.adb_serial || '').trim();
      const baseScreenUrl = ms.screen_url || b.screen_url || '';
      const screenUrl = baseScreenUrl
        ? `${baseScreenUrl}?mode=grid&max_h=${state.poolSize === 'large' ? 720 : state.poolSize === 'compact' ? 360 : 620}&quality=${state.poolSize === 'large' ? 72 : 62}&t=${Date.now()}`
        : '';
      const alias = b.device_alias || ms.device_alias || ((b.mobile_auto_status || {}).device_alias) || a.label || serial || aid || window.T('msg_js_1241');
      const no = b.device_number || ms.device_number || ((b.mobile_auto_status || {}).device_number) || inferDeviceNumber(alias, a.label, ms.device_alias, b.device_alias) || '--';
      const persona = b.persona_id || b.reply_profile_id || window.T('psn_tab_default');
      const login = b.login_account || b.messenger_login || aid;
      const cardCls = online ? 'online' : hasIssue ? 'warn' : 'offline';
      const primaryLabel = deviceOnly ? window.T('msg_js_1242') : hasIssue ? window.T('dash.lb.handled') : window.T('msg_js_1243');
      const primaryAttrs = `data-device-detail="${esc(aid)}"`;
      const issueChip = !online
        ? '<span class="mr-chip bad">'+window.T('dash.pr.offline')+'</span>'
        : deviceOnly
          ? '<span class="mr-chip warn">'+window.T('msg_js_1244')+'</span>'
          : !ms.vpn_connected
            ? '<span class="mr-chip warn">VPN '+window.T('msg_js_1245')+'</span>'
            : Number(ms.active_task_count || 0) > 0
              ? `<span class="mr-chip warn">${window.T('msg_s043')} ${esc(ms.active_task_count)}</span>`
              : '<span class="mr-chip ok">'+window.T('msg_js_1246')+'</span>';
      const chips = [
        `<span class="mr-chip ${ms.is_cluster ? 'warn' : 'ok'}">${esc(host)}</span>`,
        issueChip,
        `<span class="mr-chip ${online ? 'ok' : 'bad'}">${esc(text)}</span>`,
        batteryChip(ms.battery_level),
        `<span class="mr-chip ${ms.vpn_connected ? 'ok' : 'warn'}">VPN ${ms.vpn_connected ? 'ON' : 'OFF'}</span>`,
        taskChip(ms),
        deviceOnly ? '' : `<span class="mr-chip ${b.persona_exists === false ? 'bad' : 'ok'}">${esc(persona)}</span>`,
        deviceOnly ? '' : `<span class="mr-chip ${b.line_id ? 'ok' : 'warn'}">LINE ${esc(b.line_id || window.T('msg_js_1247'))}</span>`,
        deviceOnly || a.locked == null ? '' : `<span class="mr-chip ${a.locked ? 'warn' : 'ok'}">${window.T('msg_js_1270')}:${a.locked ? window.T('yes') : window.T('no')}</span>`,
      ].filter(Boolean).join('');
      const picked = state.poolSelected.has(aid);
      const actions = deviceOnly
        ? `<button class="btn btn-primary" ${primaryAttrs}>${esc(primaryLabel)}</button>
              <button class="btn mr-advanced-only" data-device-action="refresh" data-device-id="${esc(serial)}" data-cluster="${ms.is_cluster ? '1' : '0'}">${window.T('refresh')}</button>
              <button class="btn mr-advanced-only" data-device-action="reconnect" data-device-id="${esc(serial)}" data-cluster="${ms.is_cluster ? '1' : '0'}">${window.T('msg_js_1254')}</button>`
        : `<button class="btn btn-primary" ${primaryAttrs}>${esc(primaryLabel)}</button>
              ${a.paused || a.ui_unsafe
                ? `<button class="btn mr-advanced-only btn-account-resume" data-aid="${esc(aid)}">${window.T('ov_ctrl_resume')}</button>`
                : `<button class="btn mr-advanced-only btn-account-pause" data-aid="${esc(aid)}">${window.T('ov_ctrl_pause')}</button>`}
              <button class="btn mr-advanced-only" data-goto-binding="${esc(aid)}">${window.T('psn_bind')}</button>`;
      return `<div class="mr-device-card ${cardCls}${deviceOnly ? ' device-only' : ''}${picked ? ' selected' : ''}" data-aid="${esc(aid)}" data-device-id="${esc(serial)}" data-cluster="${ms.is_cluster ? '1' : '0'}" data-vpn="${ms.vpn_connected ? '1' : '0'}" data-task-count="${esc(ms.active_task_count || 0)}">
        <input type="checkbox" class="mr-device-pick" data-aid="${esc(aid)}" ${picked ? 'checked' : ''} ${deviceOnly ? 'disabled' : ''} title="${deviceOnly ? window.T('msg_js_1378') : window.T('msg_s085')}">
        <div class="mr-screen ${screenUrl ? '' : 'has-placeholder'}">
          <span class="mr-device-no">${esc(no)}</span>
          <span class="mr-device-role">${deviceOnly ? esc(host) : bound ? window.T('dash.stage.BONDED') : window.T('psn_sf_unbound')}</span>
          ${screenUrl
            ? `<img src="${esc(screenUrl)}" data-screen-url="${esc(screenUrl)}" alt="${esc(alias)}" loading="lazy" onload="this.closest('.mr-screen')?.classList.add('has-image')" onerror="this.closest('.mr-screen')?.classList.add('has-placeholder');this.replaceWith(Object.assign(document.createElement('div'),{className:'mr-screen-empty',innerHTML:'<b>${window.T('msg_js_1504')}</b><span>${window.T('msg_js_1505')}</span>'}))">`
            : `<div class="mr-screen-empty"><b>${window.T('msg_js_1391')}</b><span>${window.T('msg_js_1506')}</span></div>`}
          <div class="mr-device-bottom">
            <div class="mr-device-line">
              <div class="mr-device-title">${esc(alias)}</div>
              <span class="mr-badge ${cls}">${esc(text)}</span>
            </div>
            <div class="mr-device-sub">${esc(serial || window.T('dash.m.not_configured')+' ADB')} · ${esc(deviceOnly ? host : (login || aid || '--'))}</div>
            <div class="mr-device-sub">${state.poolAdvanced ? esc(info || window.T('msg_js_1379')) : esc(bound || !deviceOnly ? window.T('msg_js_1380') : window.T('msg_js_1381'))} </div>
            ${suggestion ? `<div class="mr-device-suggestion">${esc(suggestion)}</div>` : ''}
            <div class="mr-device-chips">${chips}</div>
            <div class="mr-device-actions">${actions}</div>
          </div>
        </div>
      </div>`;
    }).filter(Boolean);
    const issueLine = conflicts.length
      ? `<div class="mr-pool-alert">${window.T('msg_js_1507')} ${conflicts.length} ${window.T('msg_js_1508')}<button class="btn" id="btn-show-conflicts">${window.T('msg_js_1509')}</button><button class="btn" id="btn-focus-bindings">${window.T('msg_js_1510')}</button></div>`
      : '';
    const summary = (state.mobileAuto || {}).summary || {};
    const statusStrip = `<div class="mr-status-strip">
      <div class="mr-status-tile"><b>${summary.devices_online ?? '-'}/${summary.devices_total ?? '-'}</b><span>mobile-auto ${window.T('dash.pr.online')}</span></div>
      <div class="mr-status-tile"><b>${summary.cluster_devices ?? '-'}</b><span>Worker ${window.T('msg_js_1486')}</span></div>
      <div class="mr-status-tile"><b>${summary.vpn_connected ?? '-'}</b><span>VPN ${window.T('ov_js_connected')}</span></div>
      <div class="mr-status-tile"><b>${summary.tasks_active ?? '-'}</b><span>${window.T('msg_js_1511')}</span></div>
      <div class="mr-status-tile"><b>${summary.health_avg ?? '-'}</b><span>${window.T('msg_js_1512')}</span></div>
    </div>`;
    root.innerHTML = statusStrip + issueLine + `<div class="mr-device-wall ${state.poolSize === 'compact' ? 'compact' : state.poolSize === 'large' ? 'large' : ''}">${cards.join('') || '<div class="mr-empty">'+window.T('msg_js_1382')+'</div>'}</div>`;
    qsa('.mr-device-pick', root).forEach(cb => cb.addEventListener('click', ev => {
      ev.stopPropagation();
      setPoolSelected([cb.dataset.aid], cb.checked);
    }));
    qsa('.mr-device-card[data-aid]', root).forEach(card => card.addEventListener('click', ev => {
      if (ev.target.closest('button,input,a,select,textarea')) return;
      openDeviceDrawer(card.dataset.aid);
    }));
    qsa('.btn-account-trigger', root).forEach(b => b.addEventListener('click', () => accountAction(b.dataset.aid, 'trigger')));
    qsa('[data-primary-account-action]', root).forEach(b => b.addEventListener('click', () => accountAction(b.dataset.aid, b.dataset.primaryAccountAction)));
    qsa('.btn-account-resume', root).forEach(b => b.addEventListener('click', () => accountAction(b.dataset.aid, 'resume')));
    qsa('.btn-account-pause', root).forEach(b => b.addEventListener('click', () => accountAction(b.dataset.aid, 'pause')));
    qsa('[data-device-action]', root).forEach(b => b.addEventListener('click', () => mobileDeviceAction(
      b.dataset.deviceId,
      b.dataset.deviceAction,
      b.dataset.cluster === '1',
    ).catch(e => toast(e.message, 'err'))));
    qsa('[data-device-detail]', root).forEach(b => b.addEventListener('click', () => openDeviceDrawer(b.dataset.deviceDetail)));
    qsa('[data-goto-binding]', root).forEach(b => b.addEventListener('click', () => {
      const aid = b.dataset.gotoBinding;
      const card = qsa('.mr-binding-card[data-account-id]').find(x => x.dataset.accountId === aid);
      if (card) card.scrollIntoView({behavior:'smooth', block:'center'});
    }));
    const showConflicts = qs('#btn-show-conflicts', root);
    if (showConflicts) showConflicts.addEventListener('click', showConflictDrawer);
    const focusBindings = qs('#btn-focus-bindings', root);
    if (focusBindings) focusBindings.addEventListener('click', () => qs('#bindings-list')?.scrollIntoView({behavior:'smooth', block:'start'}));
    updatePoolSelectedUI();
  }

  async function accountAction(aid, action){
    if (action === 'trigger') {
      const r = await apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/trigger`, {});
      const res = r.result || {};
      toast(window.Tf('msg_js_p42',{aid:aid,step:res.step || '--',ok:!!res.ok}));
    } else if (action === 'resume') {
      await apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/resume`, {});
      toast(window.Tf('msg_js_p43',{aid:aid}));
    } else if (action === 'pause') {
      await apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/pause`, {seconds:300});
      toast(window.Tf('msg_js_p44',{aid:aid}));
    }
    await refreshAccounts();
  }

  async function mobileDeviceAction(deviceId, action, isCluster=false, extra={}, options={}){
    if (!deviceId) {
      toast(window.T('msg_js_1383'), 'err');
      return;
    }
    const labels = {
      reconnect: window.T('msg_js_1254'),
      vpn_check: 'VPN '+window.T('msg_js_1055'),
      refresh: window.T('refresh'),
      key_back: window.T('back'),
      key_home: 'Home',
      key_power: window.T('msg_js_1384'),
      tap: window.T('msg_js_1385'),
      tap_ratio: window.T('msg_js_1385'),
      open_messenger: window.T('base.drill.open')+' Messenger',
    };
    const label = labels[action] || action;
    const r = await apiPost(`/api/messenger-rpa/mobile-auto/devices/${encodeURIComponent(deviceId)}/action`, {
      action,
      is_cluster: !!isCluster,
      ...extra,
    });
    const path = r.path || '';
    if (!options.silent) {
      toast(window.Tf('msg_js_p45',{label:label,x:path.includes('batch-reconnect') ? window.T('msg_js_p48') : ''}));
    }
    if (options.refresh !== false) await refreshMobileAutoStatus();
  }

  async function batchDeviceMaintenance(kind){
    const cards = qsa('#accounts-list .mr-device-card[data-device-id]').filter(card => {
      if (!card.dataset.deviceId) return false;
      if (kind === 'offline') return card.classList.contains('offline');
      if (kind === 'vpn') return card.dataset.vpn !== '1';
      return false;
    });
    if (!cards.length) {
      toast(kind === 'offline' ? window.T('msg_js_1386') : window.T('msg_js_1387'), 'err');
      return;
    }
    const action = kind === 'offline' ? 'reconnect' : 'vpn_check';
    const label = kind === 'offline' ? window.T('msg_js_1388') : 'VPN '+window.T('msg_js_1055');
    if (!(await confirm2(window.Tf('msg_js_p46',{n:cards.length,label:label})))) return;
    const rs = await Promise.allSettled(cards.map(card => mobileDeviceAction(
      card.dataset.deviceId,
      action,
      card.dataset.cluster === '1',
      {},
      {silent:true, refresh:false},
    )));
    const ok = rs.filter(r => r.status === 'fulfilled').length;
    toast(window.Tf('msg_js_p47',{label:label,ok:ok,tot:cards.length}));
    await refreshMobileAutoStatus();
  }

  function accountBinding(aid){
    return bindingByAccount().get(String(aid || '')) || {};
  }

  function screenUrlForBinding(b, mode='large'){
    if (!b || !b.screen_url) return '';
    const h = mode === 'small' ? 360 : 720;
    const q = mode === 'small' ? 45 : 65;
    return `${b.screen_url}?mode=control&max_h=${h}&quality=${q}&t=${Date.now()}`;
  }

  function accountSelectOptions(){
    const bindings = ((state.bindings || {}).bindings || []);
    const known = new Set(bindings.map(b => String(b.account_id || b.id || '')).filter(Boolean));
    Object.keys(state.accounts || {}).forEach(id => known.add(id));
    return Array.from(known).sort().map(id => {
      const b = bindings.find(x => String(x.account_id || x.id || '') === id) || {};
      const a = (state.accounts || {})[id] || {};
      const label = [b.label || a.label || id, b.adb_serial ? `ADB ${b.adb_serial}` : window.T('psn_sf_unbound')].filter(Boolean).join(' · ');
      return `<option value="${esc(id)}">${esc(label)}</option>`;
    }).join('');
  }

  function suggestedAccountForDevice(ms){
    const serial = String((ms || {}).adb_serial || (ms || {}).device_id || '').trim();
    const no = String((ms || {}).device_number || '').replace(/^0+/, '');
    const bindings = ((state.bindings || {}).bindings || []);
    const unbound = bindings.filter(b => !String(b.adb_serial || '').trim());
    const byNumber = unbound.find(b => {
      const bn = String(b.device_number || b.mobile_device_id || '').replace(/^0+/, '');
      const label = String(b.label || b.account_id || b.id || '');
      return no && (bn === no || label.includes(no));
    });
    const pick = byNumber || unbound[0] || bindings.find(b => String(b.adb_serial || '').trim() === serial);
    return String((pick || {}).account_id || (pick || {}).id || '');
  }

  async function bindDeviceToAccount(accountId, ms){
    const serial = String((ms || {}).adb_serial || (ms || {}).device_id || '').trim();
    if (!accountId || !serial) {
      toast(window.T('msg_js_1389'), 'err');
      return;
    }
    const no = (ms || {}).device_number || '';
    const alias = (ms || {}).device_alias || deviceHost(ms);
    await apiPut('/api/messenger-rpa/bindings', {accounts:[{
      account_id: accountId,
      adb_serial: serial,
      device_number: no,
      device_alias: alias,
    }]});
    toast(`${window.T('dash.stage.BONDED')} ${accountId} -> ${serial}`);
    await Promise.all([refreshBindings(), refreshMobileAutoStatus(), refreshAccounts()]);
    openDeviceDrawer(accountId);
  }

  function closeDeviceDrawer(){
    qs('#mr-drawer-mask').classList.remove('show');
    const d = qs('#mr-device-drawer');
    d.classList.remove('show');
    d.setAttribute('aria-hidden', 'true');
  }

  function imageTapRatio(img, ev){
    const rect = img.getBoundingClientRect();
    const nw = img.naturalWidth || rect.width;
    const nh = img.naturalHeight || rect.height;
    const scale = Math.max(rect.width / nw, rect.height / nh);
    const drawnW = nw * scale;
    const drawnH = nh * scale;
    const offX = (rect.width - drawnW) / 2;
    const offY = (rect.height - drawnH) / 2;
    const rx = (ev.clientX - rect.left - offX) / drawnW;
    const ry = (ev.clientY - rect.top - offY) / drawnH;
    return {
      x_ratio: Math.max(0, Math.min(1, rx)),
      y_ratio: Math.max(0, Math.min(1, ry)),
      image_width: Math.round(nw),
      image_height: Math.round(nh),
      marker_x: ev.clientX - rect.left,
      marker_y: ev.clientY - rect.top,
    };
  }

  function showTapMarker(box, x, y){
    const old = qs('.mr-tap-marker', box);
    if (old) old.remove();
    const m = document.createElement('span');
    m.className = 'mr-tap-marker';
    m.style.left = `${x}px`;
    m.style.top = `${y}px`;
    box.appendChild(m);
    setTimeout(() => m.remove(), 650);
  }

  function openDeviceDrawer(aid){
    const deviceOnly = isDeviceOnlyId(aid);
    const deviceRow = mobileDeviceByRow().get(aid) || {};
    const a = deviceOnly ? {} : ((state.accounts || {})[aid] || {});
    const b = deviceOnly ? {} : accountBinding(aid);
    const ms = deviceOnly ? deviceRow : (mobileStatusByAccount().get(aid) || {});
    const ma = (b.mobile_auto_status || {});
    const serial = ms.adb_serial || ms.device_id || b.adb_serial || a.adb_serial || '';
    const host = ms.host_name || (ms.is_cluster ? 'Worker' : window.T('msg_js_1231'));
    const alias = b.device_alias || ms.device_alias || ma.device_alias || a.label || serial || aid || window.T('msg_js_1241');
    const no = b.device_number || ms.device_number || ma.device_number || '--';
    const persona = b.persona_id || b.reply_profile_id || window.T('psn_tab_default');
    const login = b.login_account || b.messenger_login || aid;
    const online = deviceOnly ? !!ms.online : (!!a.present || !!ms.online);
    const paused = !!a.paused;
    const img = screenUrlForBinding({screen_url: ms.screen_url || b.screen_url}, 'large');
    const suggestedAccount = deviceOnly ? suggestedAccountForDevice(ms) : '';
    const drawerSuggestion = nextDeviceSuggestion({deviceOnly, online, ms, hasIssue: !online || !!a.ui_unsafe || !!a.paused});
    qs('#drawer-title').textContent = `${no} · ${alias}`;
    qs('#drawer-sub').textContent = (serial || (window.T('dash.m.not_configured')+' ADB'))+' · '+(deviceOnly ? host : (b.login_account || b.messenger_login || aid || '--'));
    const recent = (state.runs || []).filter(r => {
      const ck = String(r.chat_key || '');
      return ck.includes(aid) || String(r.account_id || '') === aid || String(r.adb_serial || '') === serial;
    }).slice(0, 8);
    const runHtml = recent.length ? `<table class="mr-mini-table">
      <thead><tr><th>${window.T('time')}</th><th>${window.T('tour_step')}</th><th>${window.T('msg_js_1625')}</th></tr></thead>
      <tbody>${recent.map(r => `<tr><td>${fmtTs(r.ts)}</td><td>${esc(r.step || '--')}</td><td>${r.ok ? '<span class="mr-badge sent">ok</span>' : `<span class="mr-badge rejected">${esc(short(r.error || 'fail', 40))}</span>`}</td></tr>`).join('')}</tbody>
    </table>` : '<div class="mr-empty" style="padding:.8rem">'+window.T('msg_js_1513')+'</div>';
    qs('#drawer-body').innerHTML = `
      <div class="mr-drawer-screen">${img ? `<img src="${esc(img)}" alt="${esc(alias)}">` : '<div class="mr-screen-empty"><b>'+window.T('msg_js_1391')+'</b><span>'+window.T('msg_js_1514')+'</span></div>'}</div>
      <div class="mr-drawer-tabs">
        <button type="button" class="active" data-drawer-tab="overview">${window.T('msg_js_1633')}</button>
        <button type="button" data-drawer-tab="binding">${window.T('psn_bind')}</button>
        <button type="button" data-drawer-tab="control">${window.T('msg_js_1634')}</button>
        <button type="button" data-drawer-tab="runs">${window.T('inbox.asb.log')}</button>
      </div>
      <div class="mr-drawer-tab active" data-drawer-panel="overview">
        ${drawerSuggestion ? `<div class="mr-next-action">${esc(drawerSuggestion)}</div>` : ''}
        <div class="mr-drawer-actions">
          ${deviceOnly ? `<button class="btn btn-primary" data-drawer-tab-jump="binding">${window.T('msg_js_1626')}</button>` : `
            ${paused || a.ui_unsafe
              ? `<button class="btn" data-drawer-action="resume" data-aid="${esc(aid)}">${window.T('ov_ctrl_resume')}</button>`
              : `<button class="btn" data-drawer-action="pause" data-aid="${esc(aid)}">${window.T('ov_ctrl_pause_title')}</button>`}
          `}
        </div>
        <div class="mr-detail-grid">
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1635')}</div><div class="mr-detail-value">${online ? window.T('dash.pr.online') : esc(a.adb_state || window.T('dash.pr.offline'))}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1636')}</div><div class="mr-detail-value">${deviceOnly ? window.T('msg_js_1515') : a.ui_unsafe ? 'UI '+window.T('msg_js_1240') : paused ? window.T('msg_js_012') : window.T('msg_js_1516')}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1637')}</div><div class="mr-detail-value">${ms.battery_level == null ? '--' : `${esc(ms.battery_level)}%`} / ${ms.battery_temp == null ? '--' : `${esc(ms.battery_temp)}℃`}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">VPN</div><div class="mr-detail-value">${ms.vpn_connected ? window.T('ov_js_connected') : window.T('msg_js_1517')} ${esc(ms.vpn_country || ms.vpn_config || '')}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1638')}</div><div class="mr-detail-value">${esc(host)}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('ov_js_th_account')}</div><div class="mr-detail-value">${esc(deviceOnly ? window.T('msg_js_1244') : (login || aid || '--'))}</div></div>
        </div>
      </div>
      <div class="mr-drawer-tab" data-drawer-panel="binding">
        ${deviceOnly ? `
          <div class="mr-next-action">${window.T('msg_js_1627')}：${esc(suggestedAccount || window.T('msg_js_1518'))}</div>
          <div class="mr-drawer-actions">
            <select data-drawer-bind-account style="min-width:210px">
              <option value="">${window.T('msg_js_1628')}</option>
              ${accountSelectOptions()}
            </select>
            <button class="btn btn-primary" data-drawer-action="bind-device" data-device-id="${esc(serial)}">${window.T('msg_js_1242')}</button>
          </div>
        ` : `
          <div class="mr-detail-grid">
            <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1629')}</div><div class="mr-detail-value">${esc(login || aid || '--')}</div></div>
            <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('inbox.voice.meta_persona')}</div><div class="mr-detail-value">${esc(persona)}</div></div>
            <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1630')} LINE</div><div class="mr-detail-value">${esc(b.line_id || window.T('dash.m.not_configured'))}</div></div>
            <div class="mr-detail-cell"><div class="mr-detail-label">ADB ${window.T('msg_js_1631')}</div><div class="mr-detail-value">${esc(serial || '--')}</div></div>
          </div>
          <div class="mr-drawer-actions"><button class="btn btn-primary" data-drawer-action="binding" data-aid="${esc(aid)}">${window.T('msg_js_1632')}</button></div>
        `}
      </div>
      <div class="mr-drawer-tab" data-drawer-panel="control">
        <div>
          <div class="mr-note">${window.T('msg_js_1639')} X/Y。</div>
          <div class="mr-drawer-actions">
            <button class="btn" data-drawer-action="key-back" data-device-id="${esc(serial)}">${window.T('back')}</button>
            <button class="btn" data-drawer-action="key-home" data-device-id="${esc(serial)}">Home</button>
            <button class="btn" data-drawer-action="key-power" data-device-id="${esc(serial)}">${window.T('msg_js_1384')}</button>
            <button class="btn" data-drawer-action="open-messenger" data-device-id="${esc(serial)}">${window.T('base.drill.open')} Messenger</button>
            <button class="btn" data-drawer-action="refresh-device" data-device-id="${esc(serial)}">${window.T('msg_js_1640')}</button>
            <button class="btn" data-drawer-action="reconnect-device" data-device-id="${esc(serial)}">${window.T('msg_js_1254')}</button>
            <button class="btn" data-drawer-action="vpn-check" data-device-id="${esc(serial)}">VPN ${window.T('msg_js_1055')}</button>
            <input data-tap-x type="number" min="0" max="9999" placeholder="X" style="width:72px">
            <input data-tap-y type="number" min="0" max="9999" placeholder="Y" style="width:72px">
            <button class="btn" data-drawer-action="tap-device" data-device-id="${esc(serial)}">${window.T('msg_js_1385')}</button>
          </div>
        </div>
        <div class="mr-detail-grid">
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1641')}</div><div class="mr-detail-value">${a.screen_on == null ? '--' : a.screen_on ? window.T('msg_js_1519') : window.T('msg_js_1520')} / ${a.locked == null ? '--' : a.locked ? window.T('msg_js_1270') : window.T('msg_js_1521')}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1642')}</div><div class="mr-detail-value">${a.adbkeyboard_installed == null ? '--' : a.adbkeyboard_installed ? window.T('msg_js_1522') : window.T('msg_js_1523')}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1643')}</div><div class="mr-detail-value">${ms.mem_usage == null ? '--' : `${esc(ms.mem_usage)}%`} / ${ms.storage_usage == null ? '--' : `${esc(ms.storage_usage)}%`}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">mobile-auto ${window.T('psn_js_120')}</div><div class="mr-detail-value">${esc(deviceOnly ? host : ((ma.sources || []).join(', ') || window.T('msg_js_1524')))}</div></div>
        </div>
      </div>
      <div class="mr-drawer-tab" data-drawer-panel="runs">
        <div class="mr-detail-grid">
          <div class="mr-detail-cell"><div class="mr-detail-label">mobile-auto ${window.T('msg_s043')}</div><div class="mr-detail-value">${esc(ms.task_status || 'idle')} · ${esc(ms.active_task_count || 0)} ${window.T('msg_js_1644')}</div></div>
          <div class="mr-detail-cell"><div class="mr-detail-label">${window.T('msg_js_1645')}</div><div class="mr-detail-value">${esc(a.last_run_step || '--')}</div></div>
        </div>
        ${runHtml}
      </div>`;
    const bindSelect = qs('[data-drawer-bind-account]');
    if (bindSelect && suggestedAccount) bindSelect.value = suggestedAccount;
    const switchDrawerTab = name => {
      qsa('[data-drawer-tab]').forEach(b => b.classList.toggle('active', b.dataset.drawerTab === name));
      qsa('[data-drawer-panel]').forEach(p => p.classList.toggle('active', p.dataset.drawerPanel === name));
    };
    qsa('[data-drawer-tab]').forEach(btn => btn.addEventListener('click', () => switchDrawerTab(btn.dataset.drawerTab)));
    qsa('[data-drawer-tab-jump]').forEach(btn => btn.addEventListener('click', () => switchDrawerTab(btn.dataset.drawerTabJump)));
    const screenImg = qs('.mr-drawer-screen img');
    if (screenImg) {
      screenImg.addEventListener('click', async ev => {
        const tap = imageTapRatio(screenImg, ev);
        showTapMarker(screenImg.closest('.mr-drawer-screen'), tap.marker_x, tap.marker_y);
        qs('[data-tap-x]').value = Math.round(tap.x_ratio * tap.image_width);
        qs('[data-tap-y]').value = Math.round(tap.y_ratio * tap.image_height);
        await mobileDeviceAction(serial, 'tap_ratio', !!ms.is_cluster, tap);
      });
    }
    qsa('[data-drawer-action]').forEach(btn => btn.addEventListener('click', async () => {
      const action = btn.dataset.drawerAction;
      const id = btn.dataset.aid;
      if (action === 'bind-device') {
        const accountId = qs('[data-drawer-bind-account]')?.value || '';
        await bindDeviceToAccount(accountId, ms);
      } else if (action === 'refresh-device') {
        await mobileDeviceAction(serial, 'refresh', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'reconnect-device') {
        await mobileDeviceAction(serial, 'reconnect', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'vpn-check') {
        await mobileDeviceAction(serial, 'vpn_check', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'key-back') {
        await mobileDeviceAction(serial, 'key_back', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'key-home') {
        await mobileDeviceAction(serial, 'key_home', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'key-power') {
        await mobileDeviceAction(serial, 'key_power', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'open-messenger') {
        await mobileDeviceAction(serial, 'open_messenger', !!ms.is_cluster);
        openDeviceDrawer(aid);
      } else if (action === 'tap-device') {
        const x = Number(qs('[data-tap-x]')?.value || 0);
        const y = Number(qs('[data-tap-y]')?.value || 0);
        await mobileDeviceAction(serial, 'tap', !!ms.is_cluster, {x, y});
        openDeviceDrawer(aid);
      }
      else if (action === 'select') setPoolSelected([id], true);
      else if (action === 'binding') {
        const card = qsa('.mr-binding-card[data-account-id]').find(x => x.dataset.accountId === id);
        if (card) card.scrollIntoView({behavior:'smooth', block:'center'});
      } else {
        await accountAction(id, action);
        openDeviceDrawer(id);
      }
    }));
    qs('#mr-drawer-mask').classList.add('show');
    const d = qs('#mr-device-drawer');
    d.classList.add('show');
    d.setAttribute('aria-hidden', 'false');
  }

  function showConflictDrawer(){
    const conflicts = ((state.bindings || {}).conflicts || []);
    qs('#drawer-title').textContent = window.T('msg_js_1646');
    qs('#drawer-sub').textContent = conflicts.length ? window.Tf('msg_js_p50',{n:conflicts.length}) : window.T('msg_js_1647');
    const rows = conflicts.map(c => {
      const text = (c.serials || []).length
        ? window.Tf('msg_js_p51',{n:c.number || '--',s:(c.serials || []).join(', ')})
        : `${c.serial || '--'} ${c.field || ''}: ${(c.existing || []).join(', ')} -> ${c.incoming || ''}`;
      return `<div class="mr-conflict-item">${esc(text)}<div class="mr-muted">${esc(c.source || '')}</div></div>`;
    }).join('');
    qs('#drawer-body').innerHTML = `
      <div class="mr-conflict-list">${rows || '<div class="mr-empty">'+window.T('msg_js_1648')+'</div>'}</div>
      <div class="mr-drawer-actions">
        <button class="btn btn-primary" id="drawer-focus-bindings">${window.T('msg_js_1746')}</button>
      </div>`;
    qs('#drawer-focus-bindings')?.addEventListener('click', () => qs('#bindings-list')?.scrollIntoView({behavior:'smooth', block:'start'}));
    qs('#mr-drawer-mask').classList.add('show');
    const d = qs('#mr-device-drawer');
    d.classList.add('show');
    d.setAttribute('aria-hidden', 'false');
  }

  async function accountActionNoRefresh(aid, action){
    if (action === 'trigger') return apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/trigger`, {});
    if (action === 'resume') return apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/resume`, {});
    if (action === 'pause') return apiPost(`/api/messenger-rpa/accounts/${encodeURIComponent(aid)}/pause`, {seconds:300});
    throw new Error('unknown action: ' + action);
  }

  async function batchPoolAction(action){
    const ids = [...state.poolSelected].filter(Boolean);
    if (!ids.length) {
      toast(window.T('msg_js_1649'), 'err');
      return;
    }
    const label = action === 'trigger' ? window.T('msg_js_1650') : action === 'pause' ? window.T('ov_ctrl_pause_title') : window.T('ov_ctrl_resume');
    if (!(await confirm2(window.Tf('msg_js_p52',{n:ids.length,label:label})))) return;
    const rs = await Promise.allSettled(ids.map(id => accountActionNoRefresh(id, action)));
    const ok = rs.filter(r => r.status === 'fulfilled').length;
    toast(window.Tf('msg_js_p53',{label:label,ok:ok,tot:ids.length}));
    await Promise.allSettled([refreshAccounts(), refreshMobileAutoStatus(), refreshStatus(), refreshRuns()]);
  }

  function profileOptions(selected){
    const rp = ((state.personas || {}).reply_profiles || {});
    const profiles = Array.isArray(rp.profiles) ? rp.profiles : [];
    const ids = profiles.map(p => String(p.id || p.name || '').trim()).filter(Boolean);
    const opts = [`<option value="">${window.T('psn_tab_default')}</option>`];
    profiles.forEach(p => {
      const id = String(p.id || p.name || '').trim();
      if (!id) return;
      const label = [id, p.language ? `(${p.language})` : ''].filter(Boolean).join(' ');
      opts.push(`<option value="${esc(id)}" ${id === selected ? 'selected' : ''}>${esc(label)}</option>`);
    });
    if (selected && !ids.includes(selected)) {
      opts.push(`<option value="${esc(selected)}" selected>${esc(selected)} (${window.T('msg_js_1747')})</option>`);
    }
    return opts.join('');
  }

  async function refreshBindings(){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/bindings');
    } catch(e) {
      qs('#bindings-summary').textContent = window.T('msg_js_1651');
      qs('#bindings-list').innerHTML = `<div class="mr-empty">${window.T('msg_js_1748')}: ${esc(e.message)}</div>`;
      return;
    }
    state.bindings = r;
    renderBindings(r);
    renderAccounts(Object.keys(state.accounts || {}));
  }

  function renderBindings(data){
    const root = qs('#bindings-list');
    const rows = (data || {}).bindings || [];
    const devices = (data || {}).devices || [];
    const summary = (data || {}).binding_summary || {};
    const conflicts = (data || {}).conflicts || [];
    const total = summary.total_accounts || rows.length;
    const mapped = summary.mapped_devices || 0;
    qs('#bindings-summary').textContent = window.Tf('msg_js_p54',{m:mapped,t:total,c:conflicts.length});
    if (!rows.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1652')+' messenger_rpa.accounts '+window.T('msg_js_1653')+'</div>';
      return;
    }
    const serialOptions = devices.map(d => {
      const serial = d.current_serial || d.serial || d.adb_serial || '';
      if (!serial) return '';
      const label = [d.number ? window.Tf('msg_js_p55',{n:d.number}) : '', d.alias || d.display_name || d.model || ''].filter(Boolean).join(' ');
      return `<option value="${esc(serial)}" label="${esc(label)}"></option>`;
    }).join('');
    const conflictText = conflicts.slice(0,4).map(c => (
      (c.serials || []).length
        ? window.Tf('msg_js_p51',{n:c.number || '--',s:(c.serials || []).join(', ')})
        : `${c.serial || '--'} ${c.field || ''}: ${(c.existing || []).join(', ')} -> ${c.incoming || ''}`
    )).join('；');
    const conflictHtml = conflicts.length ? `<div class="mr-binding-card">
      <div class="mr-name">mobile-auto ${window.T('msg_js_1646')}</div>
      <div class="mr-note">${esc(conflictText)}</div>
    </div>` : '';
    root.innerHTML = `<datalist id="binding-serials">${serialOptions}</datalist>${conflictHtml}` + rows.map(b => {
      const aid = b.account_id || b.id || '';
      const pid = b.reply_profile_id || b.persona_id || '';
      const mappedOk = !!(b.device_number || b.mobile_device_id);
      let badgeCls = mappedOk ? 'sent' : 'warn';
      let badgeText = mappedOk ? window.T('msg_js_1654') : window.T('msg_js_1655');
      if (pid && b.persona_exists === false) {
        badgeCls = 'warn';
        badgeText = window.T('msg_js_1656');
      }
      if (!b.adb_serial) {
        badgeCls = 'rejected';
        badgeText = window.T('msg_js_1657');
      }
      const meta = [
        b.adb_serial ? `ADB ${b.adb_serial}` : window.T('dash.m.not_configured')+' ADB',
        b.mobile_device_id ? window.Tf('msg_js_p56',{id:b.mobile_device_id}) : '',
        b.line_id ? `LINE ${b.line_id}` : '',
      ].filter(Boolean).join(' · ');
      return `<div class="mr-binding-card" data-account-id="${esc(aid)}">
        <div class="mr-binding-top">
          <div style="min-width:0">
            <div class="mr-name">${esc(b.label || aid || '--')}</div>
            <div class="mr-muted">${esc(aid || '--')} · ${esc(meta || window.T('msg_js_1658'))}</div>
          </div>
          <span class="mr-badge ${badgeCls}">${esc(badgeText)}</span>
        </div>
        <div class="mr-binding-grid">
          <div class="mr-field">
            <label>ADB ${window.T('msg_js_1631')}</label>
            <input data-bind-field="adb_serial" list="binding-serials" value="${esc(b.adb_serial || '')}">
          </div>
          <div class="mr-field">
            <label>${window.T('msg_js_1749')}</label>
            <input data-bind-field="device_number" value="${esc(b.device_number || '')}" placeholder="05">
          </div>
          <div class="mr-field">
            <label>${window.T('msg_js_1750')}</label>
            <input data-bind-field="device_alias" value="${esc(b.device_alias || b.mobile_device_alias || '')}" placeholder="05${window.T('msg_js_1712')}">
          </div>
          <div class="mr-field">
            <label>${window.T('msg_js_1715')}</label>
            <input data-bind-field="login_account" value="${esc(b.login_account || b.messenger_login || '')}" placeholder="Messenger ${window.T('msg_js_1751')}">
          </div>
          <div class="mr-field">
            <label>${window.T('msg_js_1752')}</label>
            <select data-bind-field="reply_profile_id">${profileOptions(String(pid || '').trim())}</select>
          </div>
          <div class="mr-field">
            <label>${window.T('msg_js_1630')} LINE</label>
            <input data-bind-field="line_id" value="${esc(b.line_id || '')}" placeholder="@line_id">
          </div>
        </div>
      </div>`;
    }).join('');
  }

  async function saveBindings(){
    const root = qs('#bindings-list');
    const accounts = qsa('.mr-binding-card[data-account-id]', root).map(card => {
      const val = field => {
        const el = qs(`[data-bind-field="${field}"]`, card);
        return el ? String(el.value || '').trim() : '';
      };
      return {
        account_id: card.dataset.accountId,
        adb_serial: val('adb_serial'),
        device_number: val('device_number'),
        device_alias: val('device_alias'),
        login_account: val('login_account'),
        reply_profile_id: val('reply_profile_id'),
        line_id: val('line_id'),
      };
    });
    if (!accounts.length) {
      toast(window.T('msg_js_1659'), 'err');
      return;
    }
    await apiPut('/api/messenger-rpa/bindings', {accounts});
    toast(window.Tf('msg_js_p57',{n:accounts.length}));
    await Promise.all([refreshBindings(), refreshAccounts(), refreshConfig()]);
  }

  async function refreshCoordinator(){
    const root = qs('#coord-list');
    try {
      const c = await apiGet('/api/messenger-rpa/coordinator');
      if (!c.enabled) {
        qs('#coord-summary').textContent = window.T('ov_js_not_enabled');
        root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1660')+'</div>';
        return;
      }
      const active = c.active_chats || {};
      const portraits = c.portrait_cache || {};
      const aKeys = Object.keys(active);
      const pKeys = Object.keys(portraits);
      qs('#coord-summary').textContent = window.Tf('msg_js_p58',{a:aKeys.length,p:pKeys.length});
      const html = [
        ...aKeys.map(k => `<div class="mr-row"><span class="mr-name">${esc(k)}</span><span class="mr-badge paused">${esc(active[k])}</span></div>`),
        ...pKeys.slice(0,20).map(k => {
          const p = portraits[k] || {};
          return `<div class="mr-row"><span class="mr-name">${esc(k)}</span><span class="mr-muted">${esc(p.account_id || '')} · ${esc(p.age_sec || 0)}s</span></div>`;
        }),
      ].join('');
      root.innerHTML = html || '<div class="mr-empty">'+window.T('msg_js_1753')+'</div>';
    } catch(e) {
      qs('#coord-summary').textContent = window.T('ov_js_unavailable');
      root.innerHTML = `<div class="mr-empty">${window.T('rpa_load_failed')}: ${esc(e.message)}</div>`;
    }
  }

  async function refreshFunnel(){
    try {
      const d = await apiGet('/api/messenger-rpa/funnel');
      state.funnel = d;
      renderFunnel(d);
    } catch(e) {
      qs('#funnel-bars').innerHTML = '<div class="mr-empty">'+window.T('msg_js_1754')+'</div>';
    }
  }

  function renderFunnel(d){
    const stages = [
      ['INITIAL',window.T('rpa_fn_stage_initial'),''],
      ['ENGAGED',window.T('rpa_fn_stage_engaged'),''],
      ['HANDOFF_READY',window.T('rpa_fn_stage_handoff_ready'),''],
      ['HANDOFF_SENT',window.T('rpa_fn_stage_handoff_sent'),''],
      ['LINE_ADDED',window.T('rpa_fn_stage_line_added'),''],
      ['LINE_ACCEPTED',window.T('rpa_fn_stage_line_accepted'),''],
      ['LINE_ENGAGED','LINE '+window.T('msg_js_1755'),''],
      ['BONDED',window.T('rpa_fn_stage_bonded'),''],
      ['LOST_HANDOFF',window.T('rpa_fn_stage_lost_handoff'),'loss'],
      ['LOST_LINE_SILENT',window.T('dash.stage.LOST')+'-LINE','loss'],
    ];
    const f = d.funnel || {};
    const maxVal = Math.max(1, ...stages.map(s => f[s[0]] || 0));
    qs('#funnel-total').textContent = window.Tf('msg_js_p59',{n:(d.conversions || {}).total_journeys || 0});
    qs('#funnel-bars').innerHTML = stages
      .filter(s => (f[s[0]] || 0) > 0 || ['INITIAL','ENGAGED','HANDOFF_SENT','LINE_ENGAGED'].includes(s[0]))
      .map(s => {
        const v = f[s[0]] || 0;
        const pct = Math.max(2, Math.round(v / maxVal * 100));
        return `<div class="mr-funnel-row">
          <div class="mr-funnel-label">${esc(s[1])}</div>
          <div class="mr-bar"><div class="mr-bar-fill ${s[2]}" style="width:${pct}%"></div></div>
          <div class="mr-muted">${v}</div>
        </div>`;
      }).join('');
    const cv = d.conversions || {};
    const rates = [
      [window.T('rpa_fn_rate_engaged'), cv.engaged_rate],
      [window.T('rpa_fn_rate_handoff'), cv.handoff_rate],
      [window.T('rpa_fn_rate_add'), cv.line_add_rate],
      [window.T('msg_js_1756'), cv.overall_rate],
    ].filter(x => x[1] != null);
    qs('#funnel-rates').innerHTML = rates.map(x => `<span class="mr-badge approved">${esc(x[0])} ${esc(x[1])}%</span>`).join('');
    const hd = d.handoff || {};
    qs('#kpi-line').textContent = hd.inject_rate == null ? '--' : `${hd.inject_rate}%`;
  }

  async function refreshLeads(){
    let r;
    try {
      r = await apiGet('/api/messenger-rpa/leads?limit=200');
    } catch(e) {
      qs('#leads-summary').textContent = window.T('msg_js_1757');
      qs('#leads-list').innerHTML = `<div class="mr-empty">${window.T('msg_js_1836')}: ${esc(e.message)}</div>`;
      qs('#overview-leads').innerHTML = `<div class="mr-empty">${window.T('msg_js_1836')}</div>`;
      qs('#kpi-high').textContent = '0';
      return;
    }
    state.leads = r.items || [];
    const s = r.summary || {};
    state.leadSummary = s;
    qs('#kpi-high').textContent = s.high || 0;
    const actions = s.actions || {};
    qs('#leads-summary').textContent = window.Tf('msg_js_p60',{t:s.total || 0,u:actions.unassigned || 0,f:actions.active_followup || 0,h:actions.ready_for_handoff || 0});
    renderLeadSummary(s);
    // B3: 同时刷新 chat_persona_bindings 表
    await refreshChatPersonaBindings();
    renderLeads();
    bindLeadPersonaSelectHandlers();
  }

  // B3: per-chat persona binding 加载 + 切换 + 批量
  async function refreshChatPersonaBindings(){
    try {
      const r = await apiGet('/api/messenger-rpa/chat-persona-bindings');
      state.chatPersonaBindings = {};
      (r.bindings || []).forEach(b => {
        const key = `${b.chat_name}|${b.account_id || ''}`;
        state.chatPersonaBindings[key] = b.reply_profile_id;
      });
    } catch(e) {
      console.warn('chat-persona-bindings '+window.T('rpa_load_failed'), e);
      state.chatPersonaBindings = {};
    }
  }

  function bindLeadPersonaSelectHandlers(){
    qsa('.mr-persona-binding-select').forEach(sel => {
      sel.addEventListener('change', async (e) => {
        e.stopPropagation();
        const chatName = sel.dataset.chatName;
        const accountId = sel.dataset.accountId || '';
        const newProfile = sel.value;
        try {
          if (!newProfile) {
            // 空 → 移除绑定
            const url = `/api/messenger-rpa/chat-persona-bindings/${encodeURIComponent(chatName)}?account_id=${encodeURIComponent(accountId)}`;
            await api(url, {method:'DELETE'});
            toast(window.Tf('msg_js_p61',{c:chatName}));
          } else {
            await api(`/api/messenger-rpa/chat-persona-bindings/${encodeURIComponent(chatName)}`, {
              method:'PUT', body: {reply_profile_id: newProfile, account_id: accountId},
            });
            toast(window.Tf('msg_js_p62',{c:chatName,p:newProfile}));
          }
          await refreshChatPersonaBindings();
          renderLeads();
        } catch(err) {
          toast(window.T('psn_js_030')+': ' + err.message, 'err');
        }
      });
    });
  }

  async function batchBindPersona() {
    const checks = qsa('.mr-lead-bulk-check:checked');
    if (!checks.length) {
      return toast(window.T('msg_js_1758')+' chat', 'err');
    }
    const profiles = (((state.personas || {}).reply_profiles || {}).profiles) || [];
    if (!profiles.length) return toast('reply_profiles '+window.T('msg_js_1759'), 'err');
    const profileId = prompt(
      `${window.T('msg_js_1837')} ${checks.length} ${window.T('msg_js_1838')}。\n\n${window.T('msg_s416')} reply_profile_id:\n${profiles.map(p => '  - ' + p.id).join('\n')}\n\n${window.T('inbox.prompt.title')} reply_profile_id（${window.T('msg_js_1839')} = ${window.T('msg_js_1840')}）:`,
      profiles[0].id,
    );
    if (profileId === null) return;
    const bindings = checks.map(c => ({
      chat_name: c.dataset.chatName,
      account_id: c.dataset.accountId || '',
      reply_profile_id: (profileId || '').trim(),
    }));
    try {
      if (!profileId.trim()) {
        // 批量移除
        let removed = 0;
        for (const b of bindings) {
          const url = `/api/messenger-rpa/chat-persona-bindings/${encodeURIComponent(b.chat_name)}?account_id=${encodeURIComponent(b.account_id)}`;
          await api(url, {method:'DELETE'});
          removed++;
        }
        toast(window.Tf('msg_js_p63',{n:removed}));
      } else {
        const r = await apiPost('/api/messenger-rpa/chat-persona-bindings/batch', {bindings});
        toast(window.Tf('msg_js_p64',{a:r.applied,t:r.total_in_request}));
      }
      await refreshChatPersonaBindings();
      renderLeads();
    } catch(err) {
      toast(window.T('msg_js_1760')+': ' + err.message, 'err');
    }
  }

  function leadLevel(score){
    score = Number(score || 0);
    if (score >= 80) return 'high';
    if (score >= 40) return 'mid';
    return 'low';
  }

  function leadOps(x){
    const op = (x && x.operator_handoff) || {};
    return {
      status: op.status || 'new',
      lineStatus: op.line_status || 'not_sent',
      owner: op.owner || '',
      priority: op.priority || '',
    };
  }

  function leadIsActive(x){
    return ['assigned','in_progress','line_sent','line_added'].includes(leadOps(x).status);
  }

  function leadIsUnassigned(x){
    const op = leadOps(x);
    return !op.owner && ['new','assigned'].includes(op.status);
  }

  function leadReadyForHandoff(x){
    const handoff = x.handoff || {};
    return !!handoff.line_ready || (Number(x.score || 0) >= 80 && !!x.line_id);
  }

  function leadPendingApproval(x){
    const stats = x.stats || {};
    return Number(stats.pending_approval_count || 0) > 0;
  }

  function leadNeedsLine(x){
    return !String(x.line_id || '').trim();
  }

  function leadFollowupDue(x){
    const ts = Number(((x.operator_handoff || {}).next_followup_at) || 0);
    return ts > 0 && ts <= Date.now() / 1000;
  }

  function leadFollowupToday(x){
    const ts = Number(((x.operator_handoff || {}).next_followup_at) || 0);
    const now = Date.now() / 1000;
    return ts > now && ts <= now + 86400;
  }

  function leadPriorityScore(x){
    const op = leadOps(x);
    let score = Number(x.score || 0) * 10 + Number(x.updated_at || 0) / 100000;
    if (leadFollowupDue(x)) score += 1100;
    if (leadPendingApproval(x)) score += 900;
    if (leadReadyForHandoff(x)) score += 700;
    if (leadIsUnassigned(x)) score += 420;
    if (leadIsActive(x)) score += 260;
    if (op.status === 'converted') score -= 2000;
    if (op.status === 'lost' || op.status === 'paused') score -= 1200;
    return score;
  }

  function leadRiskFlags(x){
    const flags = [];
    if (leadNeedsLine(x)) flags.push('LINE'+window.T('dash.m.not_configured'));
    if (leadPendingApproval(x)) flags.push(window.T('msg_s108'));
    if ((x.missing_fields || []).length) flags.push(window.T('msg_js_1841'));
    if (leadIsUnassigned(x)) flags.push(window.T('msg_s104'));
    if (leadFollowupDue(x)) flags.push(window.T('msg_s105'));
    else if (leadFollowupToday(x)) flags.push(window.T('msg_js_1842'));
    if (Number(x.credit || 100) < 60) flags.push(window.T('msg_js_1843'));
    return flags.slice(0, 4);
  }

  function leadNextActionText(x){
    const op = leadOps(x);
    if (op.status === 'converted') return window.T('msg_js_1844')+'。';
    if (op.status === 'lost') return window.T('msg_js_1845')+'。';
    if (leadPendingApproval(x)) return window.T('msg_js_1846')+'。';
    if (leadReadyForHandoff(x)) return window.T('msg_js_1847')+' LINE。';
    if (leadNeedsLine(x)) return window.T('msg_js_1848')+' LINE。';
    return (x.handoff || {}).next_action || window.T('msg_js_1849')+'。';
  }

  function renderLeadSummary(s){
    const actions = (s || {}).actions || {};
    const handoff = (s || {}).handoff_statuses || {};
    const line = (s || {}).line_statuses || {};
    const lineSent = Number(line.sent || 0) + Number(line.accepted || 0) + Number(line.engaged || 0);
    const lineAdded = Number(line.added || 0) + Number(line.accepted || 0) + Number(line.engaged || 0) + Number(line.converted || 0);
    const rows = [
      [window.T('msg_s104'), actions.unassigned || 0, 'warn'],
      [window.T('msg_s105'), actions.followup_due || 0, actions.followup_due ? 'warn' : ''],
      [window.T('msg_s107'), actions.ready_for_handoff || 0, 'hot'],
      [window.T('ov_js_dob_sent')+' LINE', lineSent, ''],
      [window.T('msg_js_1850')+' LINE', lineAdded, 'hot'],
      [window.T('msg_s123'), actions.needs_line_config || 0, 'warn'],
    ];
    qs('#lead-crm-summary').innerHTML = rows.map(r => `<div class="mr-lead-kpi ${r[2]}"><b>${esc(r[1])}</b><span>${esc(r[0])}</span></div>`).join('');
  }

  function renderLeads(){
    const filter = qs('#lead-filter').value;
    const statusFilter = qs('#lead-status-filter')?.value || 'all';
    const sortMode = qs('#lead-sort')?.value || 'priority';
    const kw = String(qs('#lead-search')?.value || '').trim().toLowerCase();
    let list = state.leads || [];
    if (filter !== 'all') {
      list = list.filter(x => {
        if (filter === 'high' || filter === 'mid' || filter === 'low') return leadLevel(x.score) === filter;
        if (filter === 'unassigned') return leadIsUnassigned(x);
        if (filter === 'due') return leadFollowupDue(x);
        if (filter === 'active') return leadIsActive(x);
        if (filter === 'ready') return leadReadyForHandoff(x);
        if (filter === 'pending') return leadPendingApproval(x);
        if (filter === 'missing_line') return leadNeedsLine(x);
        return true;
      });
    }
    if (statusFilter !== 'all') {
      list = list.filter(x => leadOps(x).status === statusFilter);
    }
    if (kw) {
      list = list.filter(x => [
        x.chat_name, x.chat_key, x.account_id, x.account_label,
        x.summary_short, x.last_peer_text, x.last_reply, x.persona_id,
      ].some(v => String(v || '').toLowerCase().includes(kw)));
    }
    list = [...list].sort((a,b) => {
      if (sortMode === 'recent') return Number(b.updated_at || 0) - Number(a.updated_at || 0);
      if (sortMode === 'score') return Number(b.score || 0) - Number(a.score || 0);
      if (sortMode === 'duration') return Number(b.talk_duration_sec || 0) - Number(a.talk_duration_sec || 0);
      return leadPriorityScore(b) - leadPriorityScore(a);
    });
    const top = [...(state.leads || [])].sort((a,b) => leadPriorityScore(b) - leadPriorityScore(a)).slice(0,4);
    qs('#overview-leads').innerHTML = top.length ? top.map(leadCompact).join('') : '<div class="mr-empty">'+window.T('msg_js_1851')+'</div>';
    const root = qs('#leads-list');
    if (!list.length) {
      root.innerHTML = '<div class="mr-empty">'+window.T('msg_js_1852')+'</div>';
      return;
    }
    root.innerHTML = list.map(leadCard).join('');
    qsa('.btn-lead-detail', root).forEach(b => b.addEventListener('click', () => refreshLeadDetail(b.dataset.chatKey)));
    qsa('.mr-lead-card', root).forEach(card => {
      card.addEventListener('dblclick', () => refreshLeadDetail(card.dataset.chatKey));
    });
  }

  function leadCompact(x){
    const lvl = leadLevel(x.score);
    const op = leadOps(x);
    return `<div class="mr-row">
      <div style="min-width:0">
        <div class="mr-name">${esc(x.chat_name || x.chat_key)}</div>
        <div class="mr-muted">${esc(handoffLabel(HANDOFF_STATUS_OPTIONS, op.status))} · ${esc(short(leadNextActionText(x), 58))}</div>
      </div>
      <span class="mr-badge ${lvl}">${Number(x.score || 0)}</span>
    </div>`;
  }

  function leadCard(x){
    const lvl = leadLevel(x.score);
    const missing = x.missing_fields || [];
    const evidence = x.evidence || [];
    const stats = x.stats || {};
    const op = leadOps(x);
    const acct = [x.account_id || '', x.device_number ? window.Tf('msg_js_p55',{n:x.device_number}) : '', x.login_account || ''].filter(Boolean).join(' · ');
    const activeCls = state.selectedLeadKey === x.chat_key ? ' active' : '';
    const risks = leadRiskFlags(x);
    const sideBadge = leadReadyForHandoff(x) ? window.T('msg_s107') : lvl === 'high' ? window.T('msg_s110') : lvl === 'mid' ? window.T('msg_js_1853') : window.T('msg_s112');
    const sideCls = leadReadyForHandoff(x) ? 'sent' : lvl;
    return `<div class="mr-lead-card priority-${lvl}${activeCls}" data-chat-key="${esc(x.chat_key || '')}">
      <div>
        <div class="mr-score ${lvl}">${Number(x.score || 0)}</div>
        <div class="mr-muted" style="margin-top:.35rem">${fmtTs(x.updated_at)}</div>
        <div class="mr-muted">${esc(fmtDuration(x.talk_duration_sec))}</div>
      </div>
      <div style="min-width:0">
        <div class="mr-lead-top">
          <span class="mr-lead-title">${esc(x.chat_name || x.chat_key || '--')}</span>
          <span class="mr-badge ${sideCls}">${esc(sideBadge)}</span>
          <span class="mr-badge approved">${esc(handoffLabel(HANDOFF_STATUS_OPTIONS, op.status))}</span>
        </div>
        <div class="mr-lead-meta">
          <span>${esc(acct || window.T('msg_js_1854'))}</span>
          <span>persona ${esc(x.persona_id || '--')}</span>
          ${x.forced_lang
            ? `<span class="mr-lang-badge locked" title="${window.T('msg_js_1916')}" onclick="msgrLangUnlock(event,'${esc(x.chat_key)}')">🔒 ${esc(x.forced_lang)}</span>`
            : `<span class="mr-lang-badge" title="${window.T('msg_js_1917')}" onclick="msgrLangLockPrompt(event,'${esc(x.chat_key)}','${esc(x.reply_lang||'')}')">🌐 ${esc(x.reply_lang||'--')}</span>`
          }
        </div>
        <div class="mr-lead-line"><b>${window.T('rpa_js_summary')}：</b>${esc(short(x.summary_short || '', 170))}</div>
        <div class="mr-lead-line"><b>${window.T('msg_js_1206')}：</b>${esc(short(x.last_peer_text || '', 135))}</div>
        <div class="mr-lead-line"><b>${window.T('tour_next')}：</b>${esc(short(leadNextActionText(x), 145))}</div>
        ${x.lead && x.lead.derived_from_history ? '<div class="mr-muted" style="margin-top:.25rem">'+window.T('msg_js_1855')+'</div>' : ''}
        <div class="mr-evidence">${evidence.slice(-8).map(v => `<span>${esc(v)}</span>`).join('')}</div>
      </div>
      <div class="mr-lead-side">
        <span class="mr-badge ${op.owner ? 'sent' : 'warn'}">${window.T('msg_js_1918')} ${esc(op.owner || window.T('msg_js_1856'))}</span>
        <span class="mr-badge ${op.lineStatus === 'not_sent' ? 'paused' : 'approved'}">LINE ${esc(handoffLabel(LINE_STATUS_OPTIONS, op.lineStatus))}</span>
        <div class="mr-muted" style="margin-top:.45rem">${window.T('msg_js_1919')} ${esc(stats.turn_count || 0)} · ${window.T('ov_kpi_pending')} ${esc(stats.pending_approval_count || 0)}</div>
        <div class="mr-muted">LINE ${x.line_id ? esc(x.line_id) : window.T('dash.m.not_configured')}</div>
        <div class="mr-risk-list">${risks.length ? risks.map(v => `<span>${esc(v)}</span>`).join('') : '<span>'+window.T('msg_js_1857')+'</span>'}</div>
        <div class="mr-evidence">${missing.slice(0,4).map(v => `<span>${esc(v)}</span>`).join('')}</div>
        <div class="mr-actions"><button class="btn btn-primary btn-lead-detail" data-chat-key="${esc(x.chat_key || '')}">${window.T('msg_js_1920')}</button></div>
      </div>
      ${renderPersonaBindingRow(x)}
    </div>`;
  }

  // B3：per-chat persona binding 行渲染（在 leadCard 末尾追加）
  function renderPersonaBindingRow(x) {
    const chatName = x.chat_name || x.chat_key || '';
    const accountId = x.account_id || '';
    const key = `${chatName}|${accountId}`;
    const boundProfile = (state.chatPersonaBindings || {})[key] || '';
    const profiles = (((state.personas || {}).reply_profiles || {}).profiles) || [];
    if (!profiles.length) return '';
    const opts = ['<option value="">['+window.T('msg_js_1858')+']</option>']
      .concat(profiles.map(p => `<option value="${esc(p.id)}" ${boundProfile === p.id ? 'selected' : ''}>${esc(p.id)}</option>`))
      .join('');
    return `<div style="grid-column:1 / -1;border-top:1px solid #2a3142;padding:6px 10px;display:flex;align-items:center;gap:8px;background:rgba(45,108,223,0.05)">
      <input type="checkbox" class="mr-lead-bulk-check" data-chat-name="${esc(chatName)}" data-account-id="${esc(accountId)}" style="width:14px;height:14px" title="${window.T('msg_js_1922')}">
      <span style="font-size:12px;color:var(--th-ink-slate4,#5b6b85)">🎭 ${window.T('inbox.voice.meta_persona')}:</span>
      <select class="mr-persona-binding-select" data-chat-name="${esc(chatName)}" data-account-id="${esc(accountId)}" style="font-size:12px;padding:2px 6px;background:var(--th-bg-inkpanel,#1a1f2e);color:var(--th-ink-slate3,#cbd5e1);border:1px solid var(--th-bd-slate7,#334155);border-radius:4px;flex:0 1 220px">${opts}</select>
      ${boundProfile ? `<span class="mr-badge sent" style="font-size:10px">${window.T('dash.stage.BONDED')}</span>` : `<span class="mr-muted" style="font-size:11px">${window.T('msg_js_1921')}</span>`}
      <span style="margin-left:auto;font-size:11px;color:var(--th-ink-slate5,#64748b)">${window.T('msg_js_1923')}</span>
    </div>`;
  }

  async function refreshLeadDetail(chatKey){
    if (!chatKey) return;
    state.selectedLeadKey = chatKey;
    renderLeads();
    qs('#lead-detail-summary').textContent = window.T('msg_js_1924')+'...';
    qs('#lead-detail').innerHTML = '<div class="mr-empty">'+window.T('msg_js_1047')+'...</div>';
    try {
      const d = await apiGet(`/api/messenger-rpa/leads/${encodeURIComponent(chatKey)}?history_limit=80`);
      state.leadDetail = d;
      renderLeadDetail(d);
    } catch(e) {
      qs('#lead-detail-summary').textContent = window.T('msg_js_1925');
      qs('#lead-detail').innerHTML = `<div class="mr-empty">${window.T('msg_js_1981')}: ${esc(e.message)}</div>`;
    }
  }

  function detailCell(label, value){
    return `<div class="mr-detail-cell"><div class="mr-detail-label">${esc(label)}</div><div class="mr-detail-value">${esc(value || '--')}</div></div>`;
  }

  const HANDOFF_STATUS_OPTIONS = [
    ['new', window.T('msg_s113')],
    ['assigned', window.T('msg_s114')],
    ['in_progress', window.T('msg_s106')],
    ['line_sent', window.T('ov_js_dob_sent')+' LINE'],
    ['line_added', window.T('msg_js_1850')+' LINE'],
    ['converted', window.T('inbox.stat.done')],
    ['lost', window.T('msg_s117')],
    ['paused', window.T('ov_ctrl_pause')],
  ];
  const LINE_STATUS_OPTIONS = [
    ['not_sent', window.T('msg_js_1926')],
    ['sent', window.T('rpa_js_st_sent')],
    ['added', window.T('msg_js_1927')],
    ['accepted', window.T('msg_js_1928')],
    ['engaged', 'LINE'+window.T('msg_js_1755')],
    ['converted', window.T('inbox.stat.done')],
    ['lost', window.T('dash.stage.LOST')],
  ];
  const PRIORITY_OPTIONS = [
    ['', window.T('msg_js_1929')],
    ['urgent', window.T('msg_js_1930')],
    ['high', window.T('msg_js_1931')],
    ['mid', window.T('inbox.xls.zh')],
    ['low', window.T('msg_js_1932')],
  ];
  function optionHtml(options, selected){
    return options.map(([v, label]) => `<option value="${esc(v)}"${String(selected || '') === v ? ' selected' : ''}>${esc(label)}</option>`).join('');
  }
  function handoffLabel(options, value){
    const row = options.find(x => x[0] === String(value || ''));
    return row ? row[1] : (value || '--');
  }
  function localInputTs(ts){
    if (!ts) return '';
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return '';
    const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }
  function parseLocalInputTs(value){
    if (!value) return 0;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? 0 : Math.floor(d.getTime() / 1000);
  }

  function missingFieldAdvice(field){
    const f = String(field || '');
    const map = {
      country: window.T('msg_js_1933'),
      gender: window.T('msg_js_1934'),
      age_range: window.T('msg_js_1935'),
      occupation: window.T('msg_js_1936'),
      income_signal: window.T('msg_js_1937'),
      income_band: window.T('msg_js_1938'),
      need_tags: window.T('msg_js_1939'),
    };
    return map[f] || f;
  }

  async function copyText(text){
    const value = String(text || '').trim();
    if (!value) {
      toast(window.T('msg_js_1940'), 'err');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const ta = document.createElement('textarea');
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast(window.T('msg_js_1941'));
  }

  async function quickLeadHandoff(action){
    const presets = {
      take: {status:'in_progress'},
      line_sent: {status:'line_sent', line_status:'sent'},
      line_added: {status:'line_added', line_status:'added'},
      converted: {status:'converted', line_status:'converted'},
      lost: {status:'lost', line_status:'lost'},
    };
    const p = presets[action] || {};
    if (p.status && qs('#lead-handoff-status')) qs('#lead-handoff-status').value = p.status;
    if (p.line_status && qs('#lead-line-status')) qs('#lead-line-status').value = p.line_status;
    await saveLeadHandoff();
  }

  function renderLeadDetail(d){
    const x = d.lead || {};
    const account = d.account || {};
    const profile = d.customer_profile || {};
    const brief = d.handoff_brief || {};
    const handoff = brief.handoff_advice || x.handoff || {};
    const op = d.operator_handoff || x.operator_handoff || {};
    const timeline = d.timeline || [];
    const history = d.history_turns || [];
    const stats = x.stats || {};
    const lvl = leadLevel(x.score);
    const opStatus = op.status || 'new';
    const lineStatus = op.line_status || 'not_sent';
    const priority = op.priority || '';
    const opUpdated = op.updated_at ? window.Tf('msg_js_p65',{t:fmtTs(op.updated_at),by:op.updated_by || 'web'}) : window.T('msg_js_1942');
    const risks = leadRiskFlags(x);
    const readyText = leadReadyForHandoff(x) ? window.T('msg_s107') : lvl === 'mid' ? window.T('msg_js_1853') : lvl === 'high' ? window.T('msg_s110') : window.T('msg_s112');
    qs('#lead-detail-summary').textContent = `${x.chat_name || x.chat_key || '--'} · ${account.account_id || '--'} · ${fmtDuration(x.talk_duration_sec)}`;
    const profileRows = [
      [window.T('msg_js_1943'), [profile.country, profile.reply_lang].filter(Boolean).join(' / ')],
      [window.T('msg_js_1944'), [profile.gender, profile.age_range].filter(Boolean).join(' / ')],
      [window.T('msg_js_1945'), profile.occupation || profile.occupation_tier],
      [window.T('msg_js_1946'), [profile.income_band, profile.income_confidence ? window.Tf('msg_js_p66',{c:profile.income_confidence}) : ''].filter(Boolean).join(' / ')],
      [window.T('msg_js_1947'), Array.isArray(profile.need_tags) ? profile.need_tags.join(', ') : profile.need_tags],
      [window.T('msg_js_1948'), profile.relationship_stage || x.stage],
    ];
    const timelineRows = timeline.slice(0,40).map(r => `<tr>
      <td>${fmtTs(r.ts)}</td>
      <td>${esc(r.source || '')}<br><span class="mr-muted">${esc(r.status || r.step || '')}</span></td>
      <td>${esc(r.message_type || '')}</td>
      <td>${esc(short(r.peer_text || '', 130))}</td>
      <td>${esc(short(r.reply_text || r.error || '', 160))}</td>
    </tr>`).join('');
    const historyRows = history.slice(-16).map(m => {
      const role = (m && m.role) || '';
      const content = (m && m.content) || '';
      return `<div class="mr-history-row">
        <span class="mr-badge ${role === 'user' ? 'approved' : 'sent'}">${esc(role || '--')}</span>
        <div class="mr-note" style="margin-top:.35rem">${esc(content)}</div>
      </div>`;
    }).join('');
    const gapRows = (x.missing_fields || []).slice(0,8).map(f => `<span>${esc(missingFieldAdvice(f))}</span>`).join('');
    qs('#lead-detail').innerHTML = `
      <div class="mr-stack">
        <div class="mr-detail-hero">
          <div class="mr-detail-hero-main">
            <div style="min-width:0">
              <h3>${esc(x.chat_name || x.chat_key || '--')}</h3>
              <div class="mr-muted">${esc(x.chat_key || '')}</div>
              <div class="mr-lead-meta">
                <span>${esc(account.account_id || '--')}</span>
                <span>${esc(account.device_number ? account.device_number + window.T('msg_js_1712') : account.device_alias || '--')}</span>
                <span>${esc(x.persona_id || '--')}</span>
              </div>
            </div>
            <div class="mr-lead-side" style="align-items:flex-end">
              <span class="mr-badge ${lvl}">${esc(Number(x.score || 0))} ${window.T('msg_js_1982')}</span>
              <span class="mr-badge ${leadReadyForHandoff(x) ? 'sent' : 'approved'}">${esc(readyText)}</span>
            </div>
          </div>
          <div class="mr-next-action"><b>${window.T('tour_next')}：</b>${esc(leadNextActionText(x))}</div>
          <div class="mr-risk-list">${risks.length ? risks.map(v => `<span>${esc(v)}</span>`).join('') : '<span>'+window.T('msg_js_1949')+'</span>'}</div>
          <div class="mr-quick-actions">
            <button class="btn btn-primary" data-handoff-quick="take">${window.T('msg_js_1983')}</button>
            <button class="btn" data-handoff-quick="line_sent">${window.T('ov_js_dob_sent')} LINE</button>
            <button class="btn" data-handoff-quick="line_added">${window.T('msg_js_1850')} LINE</button>
            <button class="btn" data-copy-opening>${window.T('msg_js_1984')}</button>
            <button class="btn" data-handoff-quick="converted">${window.T('rpa_fn_stage_bonded')}</button>
            <button class="btn" data-handoff-quick="lost">${window.T('dash.stage.LOST')}</button>
          </div>
        </div>
        <div class="mr-detail-grid">
          ${detailCell(window.T('ov_js_th_account'), `${account.account_id || '--'} ${account.account_label ? '· ' + account.account_label : ''}`)}
          ${detailCell(window.T('msg_js_1950'), [account.device_number ? account.device_number + window.T('msg_js_1712') : '', account.device_alias, account.adb_serial].filter(Boolean).join(' · '))}
          ${detailCell(window.T('msg_js_1715'), account.login_account || '--')}
          ${detailCell(window.T('msg_js_1951')+' LINE', account.line_id || window.T('dash.m.not_configured'))}
          ${detailCell(window.T('msg_js_1952'), fmtDuration(x.talk_duration_sec))}
          ${detailCell(window.T('msg_js_1953'), window.Tf('msg_js_p67',{r:stats.turn_count || 0,c:stats.customer_message_count || 0,b:stats.bot_reply_count || 0}))}
          ${detailCell(window.T('msg_js_1954'), fmtTs(x.first_contact_at))}
          ${detailCell(window.T('msg_js_1955'), fmtTs(x.last_activity_at))}
        </div>
        <div class="mr-handoff-box">
          <div class="mr-row" style="padding-top:0">
            <div>
              <div class="mr-name">${window.T('msg_js_2010')}</div>
              <div class="mr-muted">${esc(opUpdated)}</div>
            </div>
            <span class="mr-badge approved">${esc(handoffLabel(HANDOFF_STATUS_OPTIONS, opStatus))}</span>
          </div>
          <div class="mr-field-grid" style="margin-top:.65rem">
            <div class="mr-field">
              <label>${window.T('msg_js_1918')}</label>
              <input id="lead-handoff-owner" value="${esc(op.owner || '')}" placeholder="${window.T('msg_js_2011')}">
            </div>
            <div class="mr-field">
              <label>${window.T('msg_js_2012')}</label>
              <select id="lead-handoff-status">${optionHtml(HANDOFF_STATUS_OPTIONS, opStatus)}</select>
            </div>
            <div class="mr-field">
              <label>LINE ${window.T('msg_js_2013')}</label>
              <select id="lead-line-status">${optionHtml(LINE_STATUS_OPTIONS, lineStatus)}</select>
            </div>
            <div class="mr-field">
              <label>${window.T('msg_js_2014')}</label>
              <select id="lead-handoff-priority">${optionHtml(PRIORITY_OPTIONS, priority)}</select>
            </div>
            <div class="mr-field">
              <label>${window.T('msg_js_1625')}</label>
              <input id="lead-handoff-outcome" value="${esc(op.outcome || '')}" placeholder="${window.T('msg_js_2015')}">
            </div>
            <div class="mr-field">
              <label>${window.T('msg_js_2016')}</label>
              <input id="lead-next-followup" type="datetime-local" value="${esc(localInputTs(op.next_followup_at))}">
            </div>
            <div class="mr-field" style="grid-column:1/-1">
              <label>${window.T('msg_js_2017')}</label>
              <textarea id="lead-handoff-notes" class="mr-edit" placeholder="${window.T('msg_js_2018')}">${esc(op.notes || '')}</textarea>
            </div>
          </div>
          <div class="mr-actions">
            <button class="btn btn-primary" id="btn-save-lead-handoff">${window.T('msg_js_2019')}</button>
            <span class="mr-muted">${window.T('msg_js_2020')}</span>
          </div>
        </div>
        <div class="mr-handoff-box">
          <div class="mr-name">${window.T('msg_js_2021')}</div>
          <div class="mr-note" style="margin-top:.35rem">${esc(handoff.next_action || '')}</div>
          <div class="mr-reply">${esc(handoff.recommended_opening || '')}</div>
          <div class="mr-evidence">${(handoff.cautions || []).map(v => `<span>${esc(v)}</span>`).join('')}</div>
        </div>
        <div>
          <div class="mr-name">${window.T('msg_js_2022')}</div>
          <table class="mr-mini-table"><tbody>
            ${profileRows.map(r => `<tr><th>${esc(r[0])}</th><td>${esc(r[1] || '--')}</td></tr>`).join('')}
          </tbody></table>
        </div>
        <div class="mr-handoff-box">
          <div class="mr-name">${window.T('msg_js_1841')}</div>
          <div class="mr-evidence">${gapRows || '<span>'+window.T('msg_js_1985')+'</span>'}</div>
        </div>
        <div>
          <div class="mr-name">${window.T('msg_js_2023')}</div>
          <div class="mr-note" style="margin-top:.35rem">${esc(brief.detailed_summary || brief.one_line || window.T('msg_js_696'))}</div>
          <div class="mr-evidence">${(x.evidence || []).slice(-12).map(v => `<span>${esc(v)}</span>`).join('')}</div>
        </div>
        <div>
          <div class="mr-name">${window.T('msg_js_2024')}</div>
          <table class="mr-mini-table">
            <thead><tr><th>${window.T('time')}</th><th>${window.T('msg_js_2025')}</th><th>${window.T('msg_js_1042')}</th><th>${window.T('msg_js_2026')}</th><th>${window.T('msg_js_2027')}</th></tr></thead>
            <tbody>${timelineRows || '<tr><td colspan="5">'+window.T('msg_js_1986')+'</td></tr>'}</tbody>
          </table>
        </div>
        <div>
          <div class="mr-name">${window.T('msg_js_2028')}</div>
          ${historyRows || '<div class="mr-empty">'+window.T('msg_js_1987')+'</div>'}
        </div>
      </div>`;
    qs('#btn-save-lead-handoff')?.addEventListener('click', () => saveLeadHandoff().catch(e => toast(e.message, 'err')));
    qsa('[data-handoff-quick]', qs('#lead-detail')).forEach(btn => {
      btn.addEventListener('click', () => quickLeadHandoff(btn.dataset.handoffQuick).catch(e => toast(e.message, 'err')));
    });
    qs('[data-copy-opening]', qs('#lead-detail'))?.addEventListener('click', () => copyText(handoff.recommended_opening).catch(e => toast(e.message, 'err')));
  }

  async function saveLeadHandoff(){
    const d = state.leadDetail || {};
    const x = d.lead || {};
    const chatKey = x.chat_key || state.selectedLeadKey;
    if (!chatKey) throw new Error(window.T('msg_js_1988'));
    const body = {
      owner: qs('#lead-handoff-owner')?.value || '',
      status: qs('#lead-handoff-status')?.value || 'new',
      line_status: qs('#lead-line-status')?.value || 'not_sent',
      priority: qs('#lead-handoff-priority')?.value || '',
      outcome: qs('#lead-handoff-outcome')?.value || '',
      notes: qs('#lead-handoff-notes')?.value || '',
      next_followup_at: parseLocalInputTs(qs('#lead-next-followup')?.value || ''),
      updated_by: 'web',
    };
    await apiPut(`/api/messenger-rpa/leads/${encodeURIComponent(chatKey)}/handoff`, body);
    toast(window.T('msg_js_1989'));
    await refreshLeadDetail(chatKey);
    await refreshLeads();
  }

  async function triggerRun(btn){
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = window.T('ov_js_running')+'...';
    try {
      const r = await apiPost('/api/messenger-rpa/trigger', {});
      toast(`run_once: step=${r.step || '--'} ok=${!!r.ok}`);
      await refreshAllLight();
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function pauseAll(){
    /* P1-3 控制条统一：暂停时长跟随 select（缺省 5 分钟），与 LINE/WA 同交互 */
    const sec = Number(document.getElementById('mr-pause-select')?.value) || 300;
    await apiPost('/api/messenger-rpa/pause', {seconds: sec});
    toast(window.T('msg_js_1990'));
    await refreshStatus();
  }

  async function resumeAll(){
    await apiPost('/api/messenger-rpa/resume', {});
    toast(window.T('msg_js_1991'));
    await refreshStatus();
  }

  async function calibrate(){
    const r = await apiPost('/api/messenger-rpa/calibrate', {});
    toast(window.Tf('msg_js_p68',{ok:!!r.ok}));
  }

  async function installKeyboard(){
    if (!(await confirm2(window.T('msg_js_1992')+' adb_serial '+window.T('msg_js_1486')+'？'))) return;
    const r = await apiPost('/api/messenger-rpa/install-adbkeyboard', {});
    toast(`ADBKeyboard: ok=${!!r.ok}`);
    await refreshAccounts(true);
  }

  async function refreshAllLight(opts={}){
    if (_lightRefreshPromise) return _lightRefreshPromise;
    const force = !!opts.force;
    const tab = activeTab();
    const tasks = [
      refreshOnce('status', refreshStatus, {minMs:5000, force}),
      refreshOnce('approvals', refreshApprovals, {minMs:12000, force}),
      refreshOnce('runs', refreshRuns, {minMs:12000, force}),
      refreshOnce('funnel', refreshFunnel, {minMs:45000, force}),
      refreshOnce('leads', refreshLeads, {minMs:45000, force}),
      refreshOnce('coordinator', refreshCoordinator, {minMs:45000, force}),
    ];
    if (!state.config || tab === 'settings') {
      tasks.push(refreshOnce('config', refreshConfig, {minMs:60000, force}));
    }
    if (tab === 'accounts') {
      tasks.push(
        refreshOnce('accounts', () => refreshAccounts(), {minMs:15000, force}),
        refreshOnce('mobileAutoStatus', refreshMobileAutoStatus, {minMs:20000, force}),
        refreshOnce('bindings', refreshBindings, {minMs:60000, force}),
      );
    }
    if (tab === 'personas') {
      tasks.push(
        refreshOnce('personas', refreshPersonas, {minMs:60000, force}),
        refreshOnce('strategy', refreshStrategy, {minMs:20000, force}),
      );
    }
    if (tab === 'settings') {
      tasks.push(refreshOnce('media', refreshMedia, {minMs:60000, force}));
    }
    _lightRefreshPromise = settleVisible(tasks)
      .finally(() => { _lightRefreshPromise = null; });
    return _lightRefreshPromise;
  }

  async function initialLoad(){
    await settleVisible([
      refreshOnce('status', refreshStatus, {force:true}),
      refreshOnce('config', refreshConfig, {force:true}),
      refreshOnce('approvals', refreshApprovals, {force:true}),
      refreshOnce('runs', refreshRuns, {force:true}),
    ]);
    await settleVisible([
      refreshOnce('funnel', refreshFunnel, {force:true}),
      refreshOnce('leads', refreshLeads, {force:true}),
    ]);
    settleVisible([
      refreshOnce('accounts', () => refreshAccounts(), {force:true}),
      refreshOnce('coordinator', refreshCoordinator, {force:true}),
    ]);
  }

  qs('#btn-refresh')?.addEventListener('click', () => refreshAllLight({force:true}).then(() => toast(window.T('msg_js_1993'))));
  qs('#btn-refresh-leads')?.addEventListener('click', () => refreshLeads().then(() => toast(window.T('msg_js_1994'))));
  // B3: per-chat persona binding 批量按钮
  qs('#btn-bulk-bind-persona')?.addEventListener('click', () => batchBindPersona());
  qs('#btn-bulk-bind-all-account')?.addEventListener('click', () => batchBindAccountLevel());
  qs('#btn-clear-bulk-checks')?.addEventListener('click', () => {
    qsa('.mr-lead-bulk-check').forEach(c => c.checked = false);
    updateBulkChecksCount();
  });
  document.addEventListener('change', (e) => {
    if (e.target && e.target.classList && e.target.classList.contains('mr-lead-bulk-check')) {
      updateBulkChecksCount();
    }
  });
  function updateBulkChecksCount() {
    const n = qsa('.mr-lead-bulk-check:checked').length;
    const el = qs('#bulk-checks-count');
    if (el) el.textContent = window.Tf('msg_js_p69',{n:n});
  }
  async function batchBindAccountLevel() {
    const leads = state.leads || [];
    if (!leads.length) return toast(window.T('msg_js_2029'), 'err');
    const accounts = [...new Set(leads.map(l => l.account_id || '').filter(Boolean))];
    if (!accounts.length) return toast('leads '+window.T('msg_js_2030')+' account_id，'+window.T('msg_js_2031'), 'err');
    const profiles = (state.pmProfiles || (((state.personas || {}).reply_profiles || {}).profiles) || []);
    if (!profiles.length) return toast(window.T('msg_js_2032'), 'err');
    const accountId = prompt(
      `${window.T('msg_js_2044')}。\n\n${window.T('msg_s416')} account_id:\n${accounts.map(a => '  - ' + a).join('\n')}\n\n${window.T('inbox.prompt.title')} account_id:`,
      accounts[0],
    );
    if (!accountId) return;
    const profileId = prompt(
      `${window.T('msg_js_2045')} "${accountId}" ${window.T('msg_js_2046')}。\n\n${window.T('msg_s416')} reply_profile_id:\n${profiles.map(p => '  - ' + (p.id||p.name)).join('\n')}\n\n${window.T('inbox.prompt.title')} reply_profile_id:`,
      (profiles[0] && (profiles[0].id||profiles[0].name)) || '',
    );
    if (!profileId) return;
    const targetChats = leads.filter(l => (l.account_id || '') === accountId);
    if (!(await confirm2(window.Tf('msg_js_p70',{a:accountId,n:targetChats.length,p:profileId})))) return;
    const bindings = targetChats.map(l => ({
      chat_name: l.chat_name || l.chat_key || '',
      account_id: accountId,
      reply_profile_id: profileId,
    })).filter(b => b.chat_name);
    try {
      const r = await apiPost('/api/messenger-rpa/chat-persona-bindings/batch', {bindings});
      toast(window.Tf('msg_js_p71',{a:accountId,x:r.applied,t:r.total_in_request,p:profileId}));
      await refreshChatPersonaBindings();
      renderLeads();
    } catch(err) {
      toast(window.T('msg_js_2033')+': ' + err.message, 'err');
    }
  }
  qs('#btn-refresh-runs')?.addEventListener('click', () => {
    // B6-P4: 全部刷新 = 刷新当前 sub-tab
    const cur = qs('.dc-tab.active')?.dataset.dc || 'runs';
    loadDcPane(cur).then(() => toast(window.T('msg_js_2034'))).catch(e => toast(e.message, 'err'));
  });
  // B6-P4: sub-tab 切换
  qsa('.dc-tab').forEach(b => b.addEventListener('click', () => setDcPane(b.dataset.dc)));
  qs('#btn-trigger')?.addEventListener('click', ev => triggerRun(ev.currentTarget));
  qs('#btn-trigger-2')?.addEventListener('click', ev => triggerRun(ev.currentTarget));
  qs('#btn-pause')?.addEventListener('click', pauseAll);
  qs('#btn-resume')?.addEventListener('click', resumeAll);
  qs('#btn-calibrate')?.addEventListener('click', calibrate);
  qs('#btn-install-kbd')?.addEventListener('click', installKeyboard);
  qs('#btn-health-deep')?.addEventListener('click', ev => {
    ev.currentTarget.disabled = true;
    refreshAccounts(true).then(() => toast(window.T('msg_js_2035'))).finally(() => ev.currentTarget.disabled = false);
  });
  /* btn-save-config handled in settings event bindings block */
  qs('#btn-save-personas')?.addEventListener('click', () => savePersonas().catch(e => toast(e.message, 'err')));
  qs('#btn-refresh-strategy')?.addEventListener('click', () => refreshStrategy().then(() => toast(window.T('msg_js_2036'))).catch(e => toast(e.message, 'err')));
  // persona modal close/save/cancel 改用 event delegation 处理（见 modal HTML 后的 <script>）
  // 这里保留 new/copy 按钮：
  qs('#btn-new-persona')?.addEventListener('click', () => createPersona('create').catch(e => toast(e.message, 'err')));
  qs('#btn-copy-persona')?.addEventListener('click', () => createPersona('copy').catch(e => toast(e.message, 'err')));
  qs('#btn-toggle-persona-json')?.addEventListener('click', ev => {
    state.personaJsonVisible = !state.personaJsonVisible;
    qs('#personas-json').style.display = state.personaJsonVisible ? 'block' : 'none';
    ev.currentTarget.textContent = state.personaJsonVisible ? window.T('msg_js_2037')+' JSON' : window.T('msg_js_2038')+' JSON';
  });
  qs('#btn-save-bindings')?.addEventListener('click', () => saveBindings().catch(e => toast(e.message, 'err')));
  qs('#btn-save-media')?.addEventListener('click', () => saveMedia().catch(e => toast(e.message, 'err')));
  qs('#btn-test-asr')?.addEventListener('click', () => testAsr().catch(e => {
    qs('#asr-test-result').textContent = e.message;
    toast(e.message, 'err');
  }));
  qs('#btn-test-tts')?.addEventListener('click', () => testTts().catch(e => {
    qs('#tts-test-result').textContent = e.message;
    toast(e.message, 'err');
  }));
  qs('#lead-filter')?.addEventListener('change', renderLeads);
  qs('#lead-status-filter')?.addEventListener('change', renderLeads);
  qs('#lead-sort')?.addEventListener('change', renderLeads);
  qs('#lead-search')?.addEventListener('input', renderLeads);
  qs('#btn-close-drawer')?.addEventListener('click', closeDeviceDrawer);
  qs('#mr-drawer-mask')?.addEventListener('click', closeDeviceDrawer);
  qs('#btn-pool-select-visible')?.addEventListener('click', () => setPoolSelected(visiblePoolIds(), true));
  qs('#btn-pool-clear')?.addEventListener('click', () => {
    state.poolSelected.clear();
    updatePoolSelectedUI();
  });
  qs('#btn-pool-repair-offline')?.addEventListener('click', () => batchDeviceMaintenance('offline').catch(e => toast(e.message, 'err')));
  qs('#btn-pool-check-vpn')?.addEventListener('click', () => batchDeviceMaintenance('vpn').catch(e => toast(e.message, 'err')));
  qs('#btn-pool-run')?.addEventListener('click', () => batchPoolAction('trigger').catch(e => toast(e.message, 'err')));
  qs('#btn-pool-pause')?.addEventListener('click', () => batchPoolAction('pause').catch(e => toast(e.message, 'err')));
  qs('#btn-pool-resume')?.addEventListener('click', () => batchPoolAction('resume').catch(e => toast(e.message, 'err')));
  qsa('[data-pool-filter]').forEach(b => b.addEventListener('click', () => {
    state.poolFilter = b.dataset.poolFilter || 'all';
    qsa('[data-pool-filter]').forEach(x => x.classList.toggle('active', x === b));
    renderAccounts(Object.keys(state.accounts || {}));
  }));
  qs('#pool-host-filter')?.addEventListener('change', ev => {
    state.poolHost = ev.currentTarget.value || 'all';
    renderAccounts(Object.keys(state.accounts || {}));
  });
  qs('#pool-advanced-mode')?.addEventListener('change', ev => {
    state.poolAdvanced = !!ev.currentTarget.checked;
    if (!state.poolAdvanced && ['offline','vpn','task'].includes(state.poolFilter)) {
      state.poolFilter = 'all';
      qsa('[data-pool-filter]').forEach(x => x.classList.toggle('active', x.dataset.poolFilter === 'all'));
    }
    renderAccounts(Object.keys(state.accounts || {}));
  });
  qsa('[data-pool-size]').forEach(b => b.addEventListener('click', () => {
    state.poolSize = b.dataset.poolSize || 'normal';
    qsa('[data-pool-size]').forEach(x => x.classList.toggle('active', x === b));
    renderAccounts(Object.keys(state.accounts || {}));
  }));
  qs('#bulk-all')?.addEventListener('change', ev => {
    qsa('.pick-approval').forEach(cb => { cb.checked = ev.currentTarget.checked; });
    updateBulkButtons();
  });
  qs('#btn-bulk-approve')?.addEventListener('click', () => bulkDecision('approve').catch(e => toast(e.message, 'err')));
  qs('#btn-bulk-reject')?.addEventListener('click', () => bulkDecision('reject').catch(e => toast(e.message, 'err')));

  document.addEventListener('click', ev => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    if (btn.matches('.btn-approve,.btn-suggest,.btn-reject,.btn-account-trigger,.btn-account-resume,.btn-account-pause,#btn-pool-run,#btn-pool-pause,#btn-pool-resume')) {
      btn.disabled = true;
      setTimeout(() => { if (btn.isConnected) btn.disabled = false; }, 4000);
    }
  });

  // ── P1-E3: 紧急停发应急工具 ──
  // 三个 API：
  //   POST   /api/messenger-rpa/accounts/{aid}/chats/emergency_stop  停发
  //   DELETE /api/messenger-rpa/accounts/{aid}/chats/emergency_stop  解除
  //   GET    /api/messenger-rpa/accounts/{aid}/chats/skipped         列表
  async function emergencyStop(){
    const acc = (qs('#es-account-id')?.value || '').trim();
    const name = (qs('#es-chat-name')?.value || '').trim();
    const reason = (qs('#es-reason')?.value || '').trim() || 'manual_emergency';
    let skipSec = Number(qs('#es-self-skip-sec')?.value || 1800);
    if (!Number.isFinite(skipSec) || skipSec < 0) skipSec = 1800;
    if (!acc || !name){
      toast(window.T('msg_js_2039'), 'err');
      return;
    }
    const ok = window.confirm(
      window.Tf('msg_js_p72',{acc:acc,name:name,reason:reason}) +
      window.Tf('msg_js_p73',{s:skipSec}) +
      window.T('msg_js_2068'),
    );
    if (!ok) return;
    try{
      const r = await apiPost(
        `/api/messenger-rpa/accounts/${encodeURIComponent(acc)}/chats/emergency_stop`,
        { chat_name: name, reason, self_skip_sec: skipSec },
      );
      toast(window.Tf('msg_js_p74',{c:r.chat_name || name,k:r.chat_key}), 'ok');
      // 清空 chat_name + reason 输入框，方便接连停发
      const cn = qs('#es-chat-name'); if (cn) cn.value = '';
      const rs = qs('#es-reason');    if (rs) rs.value = '';
      refreshEmergencyStopList();
    } catch(e){
      toast(window.Tf('msg_js_p75',{e:e.message}), 'err');
    }
  }

  async function emergencyRelease(chatName){
    const acc = (qs('#es-account-id')?.value || '').trim();
    if (!acc){
      toast(window.T('msg_js_2040')+' ID', 'err');
      return;
    }
    if (!window.confirm(window.Tf('msg_js_p76',{c:chatName}))) return;
    try{
      await api(
        `/api/messenger-rpa/accounts/${encodeURIComponent(acc)}/chats/emergency_stop` +
        `?chat_name=${encodeURIComponent(chatName)}`,
        { method: 'DELETE' },
      );
      toast(window.Tf('msg_js_p77',{c:chatName}), 'ok');
      refreshEmergencyStopList();
    } catch(e){
      toast(window.Tf('msg_js_p78',{e:e.message}), 'err');
    }
  }

  async function refreshEmergencyStopList(){
    const acc = (qs('#es-account-id')?.value || '').trim();
    const list = qs('#emergency-stop-list');
    if (!list) return;
    if (!acc){
      list.innerHTML = '<div class="mr-empty" style="padding:.4rem">'+window.T('msg_js_2041')+'&ldquo;'+window.T('refresh')+'&rdquo;'+window.T('msg_js_2042')+'</div>';
      return;
    }
    list.innerHTML = '<div class="mr-empty" style="padding:.4rem">'+window.T('msg_s040')+'…</div>';
    try{
      const r = await apiGet(
        `/api/messenger-rpa/accounts/${encodeURIComponent(acc)}/chats/skipped?limit=200`,
      );
      const chats = Array.isArray(r.chats) ? r.chats : [];
      if (!chats.length){
        list.innerHTML = '<div class="mr-empty" style="padding:.4rem">'+window.T('msg_js_2043')+'</div>';
        return;
      }
      list.innerHTML = chats.map(c => {
        const ts = Number(c.created_at || 0) * 1000;
        const when = ts ? window.wsFmtDateTime(ts) : '-';
        return `
          <div style="display:flex;align-items:center;gap:.5rem;padding:.4rem 0;border-bottom:1px solid var(--bd)">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                ${esc(c.chat_name || '(unnamed)')}
              </div>
              <div style="font-size:.7rem;color:var(--t3,#5b6b85);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                ${esc(c.reason || '-')} · ${esc(when)}
              </div>
            </div>
            <button class="btn" data-emerg-release="${esc(c.chat_name || '')}"
                    style="font-size:.7rem;padding:.2rem .5rem;flex:0 0 auto">
              ${window.T('msg_js_2047')}
            </button>
          </div>
        `;
      }).join('');
      list.querySelectorAll('[data-emerg-release]').forEach(btn => {
        btn.addEventListener('click', () => emergencyRelease(btn.dataset.emergRelease));
      });
    } catch(e){
      list.innerHTML = `<div class="mr-empty" style="padding:.4rem;color:var(--th-ink-red4,#b91c1c)">${window.T('rpa_load_failed')}：${esc(e.message)}</div>`;
    }
  }
  qs('#btn-emergency-stop')?.addEventListener('click', () => emergencyStop());
  qs('#btn-emergency-refresh')?.addEventListener('click', () => refreshEmergencyStopList());
  qs('#es-account-id')?.addEventListener('change', () => refreshEmergencyStopList());

  initialLoad();
  setInterval(() => refreshAllLight().catch(() => {}), 25000);
  setInterval(() => {
    const cb = qs('#pool-auto-refresh');
    if (!cb || !cb.checked) return;
    qsa('#accounts-list img[data-screen-url]').forEach(img => {
      const base = String(img.dataset.screenUrl || '').replace(/([?&])t=\d+/, '$1t=' + Date.now());
      img.src = base.includes('t=') ? base : base + (base.includes('?') ? '&' : '?') + 't=' + Date.now();
    });
  }, 5000);

  /* 全局暴露：本块整体在 IIFE 内，内联 on*="fn()" 在全局作用域求值，未暴露即抛 ReferenceError（哑按钮）。
     saveConfig/reloadAllSettings 是 IIFE 内裸 function，却被「保存配置/放弃修改」按钮内联 onclick 调用。
     常驻门禁见 tests/test_rpa_inline_handlers_exposed.py。 */
  Object.assign(window, { saveConfig, reloadAllSettings });
})();
