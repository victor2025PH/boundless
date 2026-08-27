/* 小智「教学模式」——点哪教哪（实施58 P0，2026-08-23）。
 *
 * 独立于 assistant-ball.js 的姊妹模块（形象线并行升级球本体，本文件刻意
 * 零依赖其内部实现，只做只读 DOM 协作）：
 *   - 入口：向 .asb-panel 注入一行「🎓 教学模式」按钮（MutationObserver 兜底
 *     重注入，球面板被形象线重渲染也能自愈）；
 *   - 教学态：window 捕获相位拦截点击（先于 _win_unique/页面委托），点任意
 *     元素出讲解气泡——绝不真正触发目标动作（安全探索是卖点不是缺陷）；
 *   - 讲解底料：admin 壳 TERM_DICT（help_terms.py 全站词典）标题精确命中出
  *     desc/usage；未命中→「让小智详细讲讲」退出教学态后经
 *     window.AssistantBall.ask / asb-ask 开面板投问
 *     （球开合走 pointerup，勿对 .asb-ball 调 click()）；
 *   - 融合契约（与球形象线）：视觉只用 --xz-* 局部变量，init 按壳映射到
 *     --tk-* 与 --p 同源 token——形象线改球的主题，本模块自动跟色；流星
 *     执行引擎（P2 assistant-agent.js）同此原则：取样球当前视觉，零协调成本。
 *   - 埋点：navigator.sendBeacon → POST /api/telemetry/ui-event
 *     （asb_teach_on/off/click/ask/term_hit，ops「UI 交互」卡可见）。
 *
 * 挂载：两壳模板 assistant loader 在球 init 后追加加载本文件并调
 *   XZTeach.init({shell:'admin'|'workspace', lang}); 文件缺席=静默无痕。
 * 门禁：tests/test_assistant_teach_wireup.py（版本戳同步/无内联 handler/
 *   词典 zh-en 齐平）。
 */
(function () {
  'use strict';
  if (window.XZTeach) { return; }

  var VER = '20260827a';

  var I18N = {
    zh: {
      row_btn: '🎓 教学模式 · 点哪学哪',
      row_btn_on: '🎓 教学中 · 点此退出',
      row_hint: '进入后点击页面任意位置看讲解，不会真的执行',
      banner: '教学模式：点击页面上任何元素看讲解（不会真的执行）',
      banner_exit: '退出教学（Esc）',
      banner_page: '📖 教我这个页',
      ask_page1: '教学模式提问：页面「',
      ask_page2: '」最重要的三件事是什么？分别在哪里点、怎么用？',
      tour_btn: '🧭 带我走一遍',
      tour_next: '下一个 →',
      tour_prev: '← 上一个',
      tour_done: '✅ 逛完了',
      tour_none: '这页还没备好导览词条，先让小智讲讲整页吧',
      bub_ask: '🤖 让小智详细讲讲',
      bub_close: '知道了',
      bub_safe: '教学模式下点击不会真的执行，放心探索',
      bub_zone: '这个区域',
      bub_generic: '想深入了解它的用法？让小智结合帮助库给你讲讲。',
      ask_q1: '教学模式提问：页面「',
      ask_q2: '」里的「',
      ask_q3: '」是干什么用的？怎么正确使用？',
      ask_anchor: '（元素标识 ',
      ask_anchor2: '）',
      usage_t: '怎么用：',
    },
    en: {
      row_btn: '🎓 Teach mode · click to learn',
      row_btn_on: '🎓 Teaching · click to exit',
      row_hint: 'Click anything on the page for an explanation; nothing is executed',
      banner: 'Teach mode: click any element for an explanation (nothing is executed)',
      banner_exit: 'Exit (Esc)',
      banner_page: '📖 Teach this page',
      ask_page1: 'Teach-mode question: on page "',
      ask_page2: '", what are the three most important things and how do I use each?',
      tour_btn: '🧭 Walk me through',
      tour_next: 'Next →',
      tour_prev: '← Back',
      tour_done: '✅ Done',
      tour_none: 'No tour entries for this page yet — ask the assistant instead',
      bub_ask: '🤖 Ask the assistant',
      bub_close: 'Got it',
      bub_safe: 'Clicks are intercepted in teach mode — explore safely',
      bub_zone: 'This area',
      bub_generic: 'Want the full story? Let the assistant explain it from the help corpus.',
      ask_q1: 'Teach-mode question: on page "',
      ask_q2: '", what does "',
      ask_q3: '" do and how do I use it correctly?',
      ask_anchor: ' (element id ',
      ask_anchor2: ')',
      usage_t: 'How to use: ',
    },
  };

  var S = {
    shell: 'admin', lang: 'zh', on: false,
    banner: null, hover: null, bubble: null, row: null, mo: null,
    termIdx: null, dict: null, dictLoading: false,
  };

  function t(k) {
    var d = I18N[S.lang] || I18N.zh;
    return d[k] || I18N.zh[k] || k;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
               "'": '&#39;' }[c];
    });
  }
  function beacon(action) {
    try {
      navigator.sendBeacon('/api/telemetry/ui-event', new Blob(
        [JSON.stringify({ page: (location && location.pathname) || '',
                          action: String(action) })],
        { type: 'application/json' }));
    } catch (e) { /* best-effort */ }
  }

  /* ── 样式（--xz-* 局部令牌；init 按壳映射同源 token＝与球自动同色） ── */
  function injectCss() {
    if (document.getElementById('xzt-style')) { return; }
    var css = '' +
'.xzt-banner{position:fixed;top:10px;left:50%;transform:translateX(-50%);' +
'z-index:10005;display:flex;align-items:center;gap:.6rem;max-width:92vw;' +
'padding:.45rem .9rem;border-radius:999px;font-size:.78rem;color:#fff;' +
'background:linear-gradient(135deg,var(--xz-accent,#4f6ef7),#8b5cf6);' +
'box-shadow:0 6px 24px rgba(79,110,247,.45);animation:xztIn .22s ease}' +
'@keyframes xztIn{from{opacity:0;transform:translate(-50%,-6px)}' +
'to{opacity:1;transform:translate(-50%,0)}}' +
'.xzt-banner button{border:1px solid rgba(255,255,255,.55);background:rgba(255,255,255,.14);' +
'color:#fff;border-radius:999px;cursor:pointer;font-family:inherit;font-size:.72rem;' +
'padding:.16rem .6rem;flex-shrink:0}' +
'.xzt-banner button:hover{background:rgba(255,255,255,.26)}' +
'.xzt-hover{position:fixed;z-index:10003;pointer-events:none;' +
'border:2px dashed var(--xz-accent,#4f6ef7);border-radius:8px;' +
'background:rgba(79,110,247,.08);transition:all .06s linear;display:none}' +
'.xzt-bubble{position:fixed;z-index:10006;width:290px;max-width:calc(100vw - 20px);' +
'background:var(--xz-bg,#fff);color:var(--xz-txt,#111);border:1px solid var(--xz-bd,#ddd);' +
'border-radius:12px;box-shadow:0 12px 36px rgba(0,0,0,.26);padding:.6rem .7rem;' +
'font-size:.78rem;line-height:1.5;animation:xztIn2 .16s ease}' +
'@keyframes xztIn2{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}' +
'.xzt-bub-t{display:flex;align-items:center;gap:.4rem;font-weight:700;' +
'font-size:.8rem;margin-bottom:.3rem;word-break:break-all}' +
'.xzt-bub-b{color:var(--xz-txt,#333);word-break:break-word;max-height:150px;overflow-y:auto}' +
'.xzt-bub-u{margin-top:.3rem;color:var(--xz-muted,#777);font-size:.72rem}' +
'.xzt-bub-row{display:flex;gap:.4rem;margin-top:.55rem}' +
'.xzt-bub-row button{border:1px solid var(--xz-bd,#ddd);border-radius:8px;cursor:pointer;' +
'font-family:inherit;font-size:.74rem;padding:.3rem .6rem;background:var(--xz-input,#f5f6f8);' +
'color:var(--xz-txt,#333)}' +
'.xzt-bub-row button.pri{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7);' +
'color:#fff;font-weight:600}' +
'.xzt-bub-safe{margin-top:.45rem;font-size:.66rem;color:var(--xz-muted,#999)}' +
/* P0-3 2026-08-27：① 虚线边框在设计系统里是「占位/拖放区」语义，用在主打
   功能上＝看起来没做完、没人敢点 → 改实线 + --xz-input 柔和底（跟随主题，
   不写死色值）；② hint 原为 nowrap+ellipsis，把「不会真的执行任何操作」这句
   打消顾虑的承诺截断成「不会真…」——恰恰是最该看全的那句。改整行独占
   （flex 100%）自然换行，行高两行是刻意的代价。 */
'.xzt-row{display:flex;align-items:center;gap:.45rem;padding:.4rem .8rem;' +
'border-top:1px solid var(--xz-bd,#ddd);flex-shrink:0;flex-wrap:wrap}' +
'.xzt-row button{border:1px solid var(--xz-accent,#4f6ef7);' +
'background:var(--xz-input,#f3f4f6);' +
'color:var(--xz-accent,#4f6ef7);border-radius:999px;cursor:pointer;font-family:inherit;' +
'font-size:.74rem;font-weight:600;padding:.26rem .7rem;white-space:nowrap}' +
'.xzt-row button:hover{background:rgba(79,110,247,.1)}' +
'.xzt-row .hint{font-size:.64rem;color:var(--xz-muted,#999);' +
'flex:1 0 100%;line-height:1.45;word-break:break-word}' +
'@media(prefers-reduced-motion:reduce){.xzt-banner,.xzt-bubble{animation:none}}';
    var st = document.createElement('style');
    st.id = 'xzt-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  /* 画布令牌 --xz-*：与 assistant-ball.js / assistant-agent.js 的同名函数
     逐字等值（P0-4 三套前缀合一）。别再改回私有前缀。 */
  function themeVars() {
    return S.shell === 'workspace'
      ? { '--xz-bg': 'var(--tk-surface,#fff)',
          '--xz-bd': 'var(--tk-border,#e2e8f0)',
          '--xz-txt': 'var(--tk-text,#0f172a)',
          '--xz-muted': 'var(--tk-text-muted,#64748b)',
          '--xz-accent': 'var(--tk-brand,#4f6ef7)',
          '--xz-input': 'var(--tk-bg,#f1f5f9)' }
      : { '--xz-bg': 'var(--card,#fff)',
          '--xz-bd': 'var(--bd,#e5e7eb)',
          '--xz-txt': 'var(--t,#111827)',
          '--xz-muted': 'var(--t3,#6b7280)',
          '--xz-accent': 'var(--p,#4f6ef7)',
          '--xz-input': 'var(--input,#f3f4f6)' };
  }
  function applyVars(el) {
    var vars = themeVars();
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) {
        el.style.setProperty(k, vars[k]);
      }
    }
  }

  /* ── 面板入口行（对球 DOM 只做追加；被重渲染吃掉则观察者自愈） ── */
  function ensureRow() {
    var panel = document.querySelector('.asb-panel');
    if (!panel || panel.querySelector('.xzt-row')) { return; }
    var row = document.createElement('div');
    row.className = 'xzt-row';
    applyVars(row);
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = t(S.on ? 'row_btn_on' : 'row_btn');
    btn.addEventListener('click', function () {
      if (S.on) { stop(); } else { start(); }
    });
    var hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = t('row_hint');
    row.appendChild(btn);
    row.appendChild(hint);
    var tools = panel.querySelector('.asb-tools');
    if (tools) { panel.insertBefore(row, tools); }
    else { panel.appendChild(row); }
    S.row = row;
  }
  function syncRowBtn() {
    var b = S.row && S.row.querySelector('button');
    if (b) { b.textContent = t(S.on ? 'row_btn_on' : 'row_btn'); }
  }
  function watchPanel() {
    if (S.mo) { return; }
    var panel = document.querySelector('.asb-panel');
    if (!panel || !window.MutationObserver) { return; }
    var pending = false;
    S.mo = new MutationObserver(function () {
      if (pending) { return; }
      pending = true;
      setTimeout(function () { pending = false; ensureRow(); }, 250);
    });
    S.mo.observe(panel, { childList: true });
  }

  /* ── 讲解底料（2026-08-23 深化）：admin 壳直用模板注入的 TERM_DICT；
     workspace 壳经 GET /api/assistant/terms 下发同一份词典（此前 workspace
     教学模式点什么都是空泛兜底＝等于没词典，这就是「不能用」的根因）。
     匹配三级降级：data-anchor 直配 → 归一化标题精确 → 互含（≥4 字）。 ── */
  function ensureDict() {
    if (S.dict) { return; }
    /* eslint-disable no-undef */
    if (typeof TERM_DICT !== 'undefined' && TERM_DICT) {
      S.dict = TERM_DICT;
      S.termIdx = null;
      return;
    }
    /* eslint-enable no-undef */
    if (S.dictLoading) { return; }
    S.dictLoading = true;
    try {
      var raw = localStorage.getItem('xzt_terms_v1');
      if (raw) {
        var ent = JSON.parse(raw);
        if (ent && ent.ver === VER && ent.terms &&
            Date.now() - (ent.ts || 0) < 21600000) {
          S.dict = ent.terms;
          S.termIdx = null;
          return;
        }
      }
    } catch (e) { /* ignore */ }
    fetch('/api/assistant/terms').then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (j && j.ok && j.terms) {
        S.dict = j.terms;
        S.termIdx = null;
        try {
          localStorage.setItem('xzt_terms_v1', JSON.stringify(
            { ver: VER, ts: Date.now(), terms: j.terms }));
        } catch (e) { /* quota：不缓存也能用 */ }
      }
    }).catch(function () { /* 后端未装载：保持空泛兜底，绝不报错 */ });
  }
  function normLabel(s) {
    var x = String(s == null ? '' : s).toLowerCase();
    /* 去 emoji/装饰符（代理对+符号区+变体选择符）与尾部计数「(3)/（3）」 */
    x = x.replace(/[\uD800-\uDFFF\u2190-\u2BFF\u2600-\u27BF\uFE0E\uFE0F\u200D]/g, '');
    x = x.replace(/[（(]\s*\d+\s*[)）]\s*$/, '');
    x = x.replace(/\s+/g, ' ').trim();
    return x;
  }
  function buildIdx() {
    S.termIdx = {};
    var d = S.dict || {};
    for (var k in d) {
      if (!Object.prototype.hasOwnProperty.call(d, k)) { continue; }
      var e2 = d[k] || {};
      S.termIdx[String(k).toLowerCase()] = k;
      if (e2.zh) { S.termIdx[normLabel(e2.zh)] = k; }
      if (e2.en) { S.termIdx[normLabel(e2.en)] = k; }
    }
  }
  function termLookup(label, anchor) {
    ensureDict();
    if (!S.dict) { return null; }
    if (!S.termIdx) {
      try { buildIdx(); } catch (e) { S.termIdx = {}; }
    }
    var key = '';
    var a = String(anchor || '').trim().toLowerCase();
    if (a && S.termIdx[a]) { key = S.termIdx[a]; }
    var lb = normLabel(label);
    if (!key && lb && S.termIdx[lb]) { key = S.termIdx[lb]; }
    if (!key && lb && lb.length >= 4) {
      var best = '', bestLen = 0;
      for (var nk in S.termIdx) {
        if (!Object.prototype.hasOwnProperty.call(S.termIdx, nk)) { continue; }
        if (nk.length < 4) { continue; }
        if (nk.indexOf(lb) >= 0 || lb.indexOf(nk) >= 0) {
          if (nk.length > bestLen) { best = S.termIdx[nk]; bestLen = nk.length; }
        }
      }
      key = best;
    }
    if (!key) { return null; }
    var e3 = S.dict[key] || {};
    return {
      title: S.lang === 'en' ? (e3.en || e3.zh) : (e3.zh || e3.en),
      desc: S.lang === 'en' ? (e3.desc_en || e3.desc) : (e3.desc || e3.desc_en),
      usage: S.lang === 'en' ? (e3.usage_en || '') : (e3.usage || ''),
    };
  }

  /* ── 目标解析：往上找最近的可交互祖先，标签取 aria/文本/占位 ── */
  var PICK_SEL = 'button,a,[role="button"],input,select,textarea,label,' +
    'summary,[data-anchor],[data-ob-id],th,[data-act],[data-tab]';
  function pickTarget(el) {
    if (!el || el.nodeType !== 1) { return el; }
    var hit = null;
    try { hit = el.closest(PICK_SEL); } catch (e) { hit = null; }
    return hit || el;
  }
  function labelOf(el) {
    var s = '';
    try {
      s = el.getAttribute('aria-label') || el.getAttribute('title') ||
        el.getAttribute('placeholder') || '';
      if (!s) { s = String(el.innerText || el.value || '').trim(); }
    } catch (e) { s = ''; }
    s = s.replace(/\s+/g, ' ').trim();
    if (s.length > 48) { s = s.slice(0, 48) + '…'; }
    return s || t('bub_zone');
  }
  function anchorOf(el) {
    try {
      return el.getAttribute('data-anchor') || el.getAttribute('data-ob-id') ||
        el.id || '';
    } catch (e) { return ''; }
  }

  /* ── 悬停高亮 ── */
  function ensureHover() {
    if (S.hover) { return S.hover; }
    var h = document.createElement('div');
    h.className = 'xzt-hover';
    applyVars(h);
    document.body.appendChild(h);
    S.hover = h;
    return h;
  }
  function moveHover(el) {
    var h = ensureHover();
    if (!el) { h.style.display = 'none'; return; }
    var r;
    try { r = el.getBoundingClientRect(); } catch (e) { return; }
    if (!r || (!r.width && !r.height)) { h.style.display = 'none'; return; }
    h.style.display = 'block';
    h.style.left = (r.left - 3) + 'px';
    h.style.top = (r.top - 3) + 'px';
    h.style.width = (r.width + 6) + 'px';
    h.style.height = (r.height + 6) + 'px';
  }

  function isOwnUi(el) {
    /* 点到按钮里的文本节点时 target.nodeType!==1，必须向上走到元素，
       否则捕获拦截器会把「详细讲讲」当成页面点击吞掉。 */
    if (!el) { return false; }
    if (el.nodeType !== 1) { return isOwnUi(el.parentElement || el.parentNode); }
    var hit = null;
    try {
      hit = el.closest('.xzt-banner,.xzt-bubble,.asb-wrap,.asb-panel,.xzt-row,.asb-an');
    } catch (e) { hit = null; }
    return !!hit;
  }

  /* ── 讲解气泡 ── */
  function closeBubble() {
    if (S.bubble) { try { S.bubble.remove(); } catch (e) { /* */ } }
    S.bubble = null;
  }
  function showBubble(el) {
    closeBubble();
    var label = labelOf(el);
    var anchor = anchorOf(el);
    var term = termLookup(label, anchor);
    if (term) { beacon('asb_teach_term_hit'); }
    else { beacon('asb_teach_term_miss'); }
    var bub = document.createElement('div');
    bub.className = 'xzt-bubble';
    applyVars(bub);
    var body = term
      ? esc(term.desc || '') +
        (term.usage
          ? '<div class="xzt-bub-u">' + esc(t('usage_t')) + esc(term.usage) +
            '</div>'
          : '')
      : esc(t('bub_generic'));
    bub.innerHTML = '' +
      '<div class="xzt-bub-t">🎓 <span>' +
      esc(term ? term.title : label) + '</span></div>' +
      '<div class="xzt-bub-b">' + body + '</div>' +
      '<div class="xzt-bub-row">' +
      '<button type="button" class="pri" data-xzt="ask">' +
      esc(t('bub_ask')) + '</button>' +
      '<button type="button" data-xzt="close">' + esc(t('bub_close')) +
      '</button></div>' +
      '<div class="xzt-bub-safe">' + esc(t('bub_safe')) + '</div>';
    document.body.appendChild(bub);
    S.bubble = bub;
    /* 定位：优先元素下方，越界翻上方，水平钳制视口 */
    var r;
    try { r = el.getBoundingClientRect(); } catch (e) { r = null; }
    var bw = bub.offsetWidth || 290;
    var bh = bub.offsetHeight || 140;
    var left = r ? Math.min(Math.max(r.left, 10),
      window.innerWidth - bw - 10) : 12;
    var top = r ? (r.bottom + 8) : 60;
    if (r && top + bh > window.innerHeight - 10) {
      top = Math.max(10, r.top - bh - 8);
    }
    bub.style.left = left + 'px';
    bub.style.top = top + 'px';
    function onBubClick(ev) {
      var node = ev.target;
      if (node && node.nodeType !== 1) { node = node.parentElement; }
      var b = node && node.closest ? node.closest('[data-xzt]') : null;
      if (!b) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      if (b.getAttribute('data-xzt') === 'ask') {
        askAssistant(label, anchor);
      } else {
        closeBubble();
      }
    }
    bub.addEventListener('click', onBubClick);
    /* 按钮上再挂一份：委托在捕获拦截/换节点时可能丢，直接监听更稳 */
    var askBtn = bub.querySelector('[data-xzt="ask"]');
    if (askBtn) {
      askBtn.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        askAssistant(label, anchor);
      });
    }
  }

  /* ── 「让小智详细讲讲」：结构化问题投进球的问答链（DOM 协作，全防御） ── */
  function askAssistant(label, anchor) {
    beacon('asb_teach_ask');
    var q = t('ask_q1') + (location.pathname || '/') + t('ask_q2') + label +
      t('ask_q3') +
      (anchor ? t('ask_anchor') + anchor + t('ask_anchor2') : '');
    /* 先退教学态：捕获拦截器还在的话，后续开面板会被当成「再点一次页面」。
       球开合走 pointerup 不是 click——HTMLElement.click() 打不开面板，
       这就是「详细讲讲」点了气泡关掉、对话却不出现的根因。 */
    stop();
    var api = window.AssistantBall;
    if (api && typeof api.ask === 'function') {
      try { if (api.ask(q)) { return; } } catch (e) { /* fallback */ }
    }
    try {
      document.dispatchEvent(new CustomEvent('asb-ask', {
        bubbles: true, detail: { q: q }
      }));
      return;
    } catch (e2) { /* last resort */ }
    var panel = document.querySelector('.asb-panel');
    if (!panel) { return; }
    var chatTab = panel.querySelector('[data-tab="chat"]');
    if (chatTab && !chatTab.classList.contains('cur')) {
      try { chatTab.click(); } catch (e3) { /* ignore */ }
    }
    var inp = panel.querySelector('.asb-in');
    var send = panel.querySelector('.asb-send');
    if (inp) { inp.value = q; }
    if (send) { try { send.click(); } catch (e4) { /* ignore */ } }
  }

  /* ── 「教我这个页」（P1 v1）：页面级结构化提问走同一条 RAG 问答链——
     零新后端、答案出自帮助语料、答不上自动进补语料清单（自进化）。
     锚点铺设+聚光灯串讲属下一批（收件箱是多线热区，本批刻意不碰）。 ── */
  function askPage(e) {
    if (e && e.preventDefault) { e.preventDefault(); e.stopPropagation(); }
    beacon('asb_teach_page');
    var q = t('ask_page1') + (location.pathname || '/') + t('ask_page2');
    stop();
    var api = window.AssistantBall;
    if (api && typeof api.ask === 'function') {
      try { if (api.ask(q)) { return; } } catch (e1) { /* fallback */ }
    }
    try {
      document.dispatchEvent(new CustomEvent('asb-ask', {
        bubbles: true, detail: { q: q }
      }));
    } catch (e2) { /* ignore */ }
  }

  /* ── 自动导览（P2「带我走一遍」）：扫当前页可见交互控件 × 词典命中 →
     串成有序讲解（上一处/下一处 + 进度）。零模板改动零后端——词条即导览，
     词典每长一条导览自动多一站；哪页词条备货不足自动如实降级为整页提问。 ── */
  var TOUR = { on: false, items: [], idx: 0 };

  function collectTourItems() {
    ensureDict();
    if (!S.dict) { return []; }
    var nodes;
    try { nodes = document.querySelectorAll(PICK_SEL); } catch (e) { return []; }
    var items = [];
    var seen = {};
    for (var i = 0; i < nodes.length && items.length < 8; i++) {
      var el = nodes[i];
      if (isOwnUi(el)) { continue; }
      var r;
      try { r = el.getBoundingClientRect(); } catch (e2) { continue; }
      if (!r || r.width < 8 || r.height < 8) { continue; }
      var term = termLookup(labelOf(el), anchorOf(el));
      if (!term || !term.desc) { continue; }
      if (seen[term.title]) { continue; }
      seen[term.title] = 1;
      items.push({ el: el, term: term });
    }
    return items;
  }
  function startTour() {
    beacon('asb_teach_tour_start');
    TOUR.items = collectTourItems();
    TOUR.idx = 0;
    if (!TOUR.items.length) {
      TOUR.on = false;
      /* 诚实降级：没备货就借气泡说明 + 落到整页提问 */
      closeBubble();
      var bub = document.createElement('div');
      bub.className = 'xzt-bubble';
      applyVars(bub);
      bub.innerHTML = '<div class="xzt-bub-b">' + esc(t('tour_none')) +
        '</div><div class="xzt-bub-row">' +
        '<button type="button" class="pri" data-xzt="ask">' +
        esc(t('bub_ask')) + '</button>' +
        '<button type="button" data-xzt="close">' + esc(t('bub_close')) +
        '</button></div>';
      document.body.appendChild(bub);
      S.bubble = bub;
      bub.style.left = '50%';
      bub.style.top = '64px';
      bub.style.transform = 'translateX(-50%)';
      bub.addEventListener('click', function (ev) {
        var b = ev.target && ev.target.nodeType === 1
          ? ev.target.closest('[data-xzt]') : null;
        if (!b) { return; }
        ev.preventDefault();
        ev.stopPropagation();
        if (b.getAttribute('data-xzt') === 'ask') { askPage(); }
        else { closeBubble(); }
      });
      return;
    }
    TOUR.on = true;
    showTourStep();
  }
  function endTour(done) {
    if (TOUR.on && done) { beacon('asb_teach_tour_done'); }
    TOUR.on = false;
    TOUR.items = [];
    closeBubble();
    moveHover(null);
  }
  function showTourStep() {
    if (!TOUR.on || !TOUR.items.length) { return; }
    if (TOUR.idx < 0) { TOUR.idx = 0; }
    if (TOUR.idx >= TOUR.items.length) { endTour(true); return; }
    var item = TOUR.items[TOUR.idx];
    var el = item.el;
    if (!el || !el.isConnected) {  /* 页面局部重渲染吃掉了这一站：跳过 */
      TOUR.items.splice(TOUR.idx, 1);
      showTourStep();
      return;
    }
    try {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } catch (e) { /* ignore */ }
    moveHover(el);
    closeBubble();
    var term = item.term;
    var last = TOUR.idx >= TOUR.items.length - 1;
    var bub = document.createElement('div');
    bub.className = 'xzt-bubble';
    applyVars(bub);
    bub.innerHTML = '' +
      '<div class="xzt-bub-t">🧭 <span>' + esc(term.title) + '</span>' +
      '<span style="margin-left:auto;font-weight:400;font-size:.66rem;' +
      'color:var(--xz-muted,#999)">' + (TOUR.idx + 1) + ' / ' +
      TOUR.items.length + '</span></div>' +
      '<div class="xzt-bub-b">' + esc(term.desc || '') +
      (term.usage
        ? '<div class="xzt-bub-u">' + esc(t('usage_t')) + esc(term.usage) +
          '</div>'
        : '') + '</div>' +
      '<div class="xzt-bub-row">' +
      (TOUR.idx > 0
        ? '<button type="button" data-xzt="tprev">' + esc(t('tour_prev')) +
          '</button>'
        : '') +
      '<button type="button" class="pri" data-xzt="tnext">' +
      esc(t(last ? 'tour_done' : 'tour_next')) + '</button>' +
      '<button type="button" data-xzt="tend">✕</button></div>';
    document.body.appendChild(bub);
    S.bubble = bub;
    /* 定位与讲解气泡同策略：目标下方，越界翻上方 */
    var r;
    try { r = el.getBoundingClientRect(); } catch (e3) { r = null; }
    var bw = bub.offsetWidth || 290;
    var bh = bub.offsetHeight || 140;
    var left = r ? Math.min(Math.max(r.left, 10),
      window.innerWidth - bw - 10) : 12;
    var top = r ? (r.bottom + 8) : 60;
    if (r && top + bh > window.innerHeight - 10) {
      top = Math.max(10, r.top - bh - 8);
    }
    bub.style.left = left + 'px';
    bub.style.top = top + 'px';
    bub.addEventListener('click', function (ev) {
      var node = ev.target;
      if (node && node.nodeType !== 1) { node = node.parentElement; }
      var b = node && node.closest ? node.closest('[data-xzt]') : null;
      if (!b) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      var a = b.getAttribute('data-xzt');
      if (a === 'tnext') {
        beacon('asb_teach_tour_step');
        TOUR.idx += 1;
        showTourStep();
      } else if (a === 'tprev') {
        TOUR.idx -= 1;
        showTourStep();
      } else {
        endTour(false);
      }
    });
  }

  /* ── 教学态开关与全局拦截（window 捕获相位＝先于页面一切委托/拦截器） ── */
  function onClick(e) {
    if (!S.on) { return; }
    if (isOwnUi(e.target)) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    e.stopPropagation();
    if (TOUR.on) { endTour(false); }  /* 导览中点了别处＝改为自由探索 */
    beacon('asb_teach_click');
    showBubble(pickTarget(e.target));
  }
  function onMove(e) {
    if (!S.on) { return; }
    if (isOwnUi(e.target)) { moveHover(null); return; }
    moveHover(pickTarget(e.target));
  }
  function onKey(e) {
    if (!S.on || e.key !== 'Escape') { return; }
    if (TOUR.on) {
      endTour(false);
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    if (S.bubble) {
      closeBubble();
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    var panel = document.querySelector('.asb-panel');
    if (panel && panel.classList.contains('open')) { return; } /* 球先关面板 */
    stop();
    e.preventDefault();
    e.stopImmediatePropagation();
  }
  function onSubmit(e) {
    if (!S.on) { return; }
    if (isOwnUi(e.target)) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  function start() {
    if (S.on) { return; }
    S.on = true;
    injectCss();
    ensureDict();  /* 词典异步就位；就位前命中层自动退化为引导问 */
    /* 看页面：教学从收起面板开始（面板挡住页面就没得点了） */
    var panel = document.querySelector('.asb-panel');
    if (panel && panel.classList.contains('open')) {
      var x = panel.querySelector('.asb-x');
      if (x) { try { x.click(); } catch (e) { /* ignore */ } }
    }
    var banner = document.createElement('div');
    banner.className = 'xzt-banner';
    applyVars(banner);
    banner.innerHTML = '<span>' + esc(t('banner')) + '</span>' +
      '<button type="button" data-xzt="btour">' + esc(t('tour_btn')) +
      '</button>' +
      '<button type="button" data-xzt="bpage">' + esc(t('banner_page')) +
      '</button>' +
      '<button type="button" data-xzt="bexit">' + esc(t('banner_exit')) +
      '</button>';
    banner.querySelector('[data-xzt="bexit"]')
      .addEventListener('click', stop);
    banner.querySelector('[data-xzt="bpage"]')
      .addEventListener('click', askPage);
    banner.querySelector('[data-xzt="btour"]')
      .addEventListener('click', startTour);
    document.body.appendChild(banner);
    S.banner = banner;
    ensureHover();
    window.addEventListener('click', onClick, true);
    window.addEventListener('auxclick', onClick, true);
    window.addEventListener('mousemove', onMove, true);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('submit', onSubmit, true);
    syncRowBtn();
    beacon('asb_teach_on');
  }
  function stop() {
    if (!S.on) { return; }
    endTour(false);
    S.on = false;
    window.removeEventListener('click', onClick, true);
    window.removeEventListener('auxclick', onClick, true);
    window.removeEventListener('mousemove', onMove, true);
    window.removeEventListener('keydown', onKey, true);
    window.removeEventListener('submit', onSubmit, true);
    if (S.banner) { try { S.banner.remove(); } catch (e) { /* */ } }
    if (S.hover) { try { S.hover.remove(); } catch (e) { /* */ } }
    S.banner = null;
    S.hover = null;
    closeBubble();
    syncRowBtn();
    beacon('asb_teach_off');
  }

  function init(opts) {
    opts = opts || {};
    S.shell = opts.shell === 'workspace' ? 'workspace' : 'admin';
    S.lang = String(opts.lang || '').toLowerCase().indexOf('en') === 0
      ? 'en' : 'zh';
    injectCss();
    /* 球可能尚未 buildDom 完成：短轮询挂入口行（球缺席=静默零痕迹） */
    var tries = 0;
    (function poll() {
      ensureRow();
      watchPanel();
      if (!S.row && ++tries < 20) { setTimeout(poll, 500); }
    })();
  }

  window.XZTeach = { init: init, start: start, stop: stop, _ver: VER };
})();
