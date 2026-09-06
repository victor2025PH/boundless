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

  var VER = '20260905b';

  var I18N = {
    zh: {
      mode_t: '点哪学哪',
      mode_d: '开着它在页面上点任何按钮，小智就地讲这是什么、怎么用。',
      row_btn: '开始点哪学哪',
      row_btn_on: '教学中 · 点此退出',
      row_hint: '进入后点击页面任意位置看讲解，不会真的执行',
      banner: '教学模式：点击页面上任何元素看讲解（不会真的执行）',
      banner_exit: '退出教学（Esc）',
      banner_page: '教我这个页',
      ask_page1: '教学模式提问：页面「',
      ask_page2: '」最重要的三件事是什么？分别在哪里点、怎么用？',
      tour_btn: '带我走一遍',
      tour_next: '下一个 →',
      tour_prev: '← 上一个',
      tour_done: '逛完了',
      tour_none: '这页还没备好导览词条，先让小智讲讲整页吧',
      stat_wait: '正在准备讲解词典…',
      stat_n1: '本页 ',
      stat_n2: ' 处可讲解',
      stat_zero: '本页暂时没有可讲解的控件——用「教我这个页」让小智整体讲一遍',
      stat_off: '讲解词典这会儿取不到，点哪学哪仍可用（讲通用说明）',
      bub_ask: '让小智详细讲讲',
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
      mode_t: 'Click to learn',
      mode_d: 'Turn it on, click any control on the page, and the assistant '
        + 'explains what it is and how to use it.',
      row_btn: 'Start click-to-learn',
      row_btn_on: 'Teaching · click to exit',
      row_hint: 'Click anything on the page for an explanation; nothing is executed',
      banner: 'Teach mode: click any element for an explanation (nothing is executed)',
      banner_exit: 'Exit (Esc)',
      banner_page: 'Teach this page',
      ask_page1: 'Teach-mode question: on page "',
      ask_page2: '", what are the three most important things and how do I use each?',
      tour_btn: 'Walk me through',
      tour_next: 'Next →',
      tour_prev: '← Back',
      tour_done: 'Done',
      tour_none: 'No tour entries for this page yet — ask the assistant instead',
      stat_wait: 'Loading the explanation dictionary…',
      stat_n1: '',
      stat_n2: ' things I can explain on this page',
      stat_zero: 'Nothing on this page matches the dictionary yet — use '
        + '"Teach this page" for a whole-page walkthrough',
      stat_off: 'The dictionary is unreachable right now; click-to-learn still '
        + 'works with generic explanations',
      bub_ask: 'Ask the assistant',
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
    banner: null, hover: null, bubble: null, api: null, claimed: false,
    termIdx: null, dict: null, dictLoading: false, dictFailed: false,
    modeEl: null, covSent: {},
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

  /* ── 单色线性图标子集（v2 2026-09-05，与 assistant-ball.js 同规格：24 网格、
     1.8 描边、currentColor）——替掉 🎓📖🧭✅🤖 等 emoji；spark 是填充的 Logo 星。 */
  var ICONS = {
    cap: '<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08' +
      'a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/>' +
      '<path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
    book: '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 ' +
      '4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    compass: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 ' +
      '1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6' +
      'a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5' +
      'a1 1 0 0 1 1 1z"/>',
    spark: '<path fill="currentColor" stroke="none" d="M12 3l1.7 4.6L18 9.3l-4.3 1.7L12 15.6' +
      'l-1.7-4.6L6 9.3l4.3-1.7L12 3z"/><path fill="currentColor" stroke="none" opacity=".85" ' +
      'd="M18.5 14l.9 2.3 2.1.8-2.1.8-.9 2.3-.9-2.3-2.1-.8 2.1-.8.9-2.3z"/>',
  };
  function ic(name, cls) {
    return '<svg class="asb-i' + (cls ? ' ' + cls : '') +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"' +
      ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || '') + '</svg>';
  }

  /* ── 样式（--xz-* 局部令牌；init 按壳映射同源 token＝与球自动同色） ── */
  function injectCss() {
    if (document.getElementById('xzt-style')) { return; }
    var css = '' +
/* 图标度量与球同一份（球缺席时本模块自带，重复定义无害） */
'.asb-i{width:1em;height:1em;flex-shrink:0;display:inline-block;vertical-align:-.16em}' +
'button>.asb-i+span{margin-left:.3em}' +
/* v2：横幅走光谱（蓝→紫）与带色投影；讲解气泡改玻璃壳 + 顶部高光 + 分层阴影，
   字号地板 ≥ .75rem（.66/.72 的说明文字对 8 小时坐席是真实疲劳源）。 */
'.xzt-banner{position:fixed;top:10px;left:50%;transform:translateX(-50%);' +
'z-index:10005;display:flex;align-items:center;gap:.6rem;max-width:92vw;' +
'padding:.5rem 1rem;border-radius:999px;font-size:.8rem;color:#fff;' +
'background:linear-gradient(135deg,var(--xz-rb-2,var(--xz-accent,#4f6ef7)),var(--xz-rb-3,#8b5cf6));' +
'box-shadow:0 10px 28px -8px var(--xz-tint,rgba(79,110,247,.45)),inset 0 1px 0 rgba(255,255,255,.28);' +
'animation:xztIn .22s ease}' +
'@keyframes xztIn{from{opacity:0;transform:translate(-50%,-6px)}' +
'to{opacity:1;transform:translate(-50%,0)}}' +
'.xzt-banner button{border:1px solid rgba(255,255,255,.55);background:rgba(255,255,255,.14);' +
'color:#fff;border-radius:999px;cursor:pointer;font-family:inherit;font-size:.76rem;' +
'padding:.22rem .7rem;flex-shrink:0;display:inline-flex;align-items:center;gap:.3em;' +
'transition:background .15s ease}' +
'.xzt-banner button:hover{background:rgba(255,255,255,.26)}' +
'.xzt-hover{position:fixed;z-index:10003;pointer-events:none;' +
'border:2px dashed var(--xz-accent,#4f6ef7);border-radius:8px;' +
'background:rgba(79,110,247,.08);transition:all .06s linear;display:none}' +
'.xzt-bubble{position:fixed;z-index:10006;width:300px;max-width:calc(100vw - 20px);' +
'background:var(--xz-glass,var(--xz-bg,#fff));color:var(--xz-txt,#111);' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:16px;box-shadow:var(--xz-sh-key,0 12px 36px rgba(0,0,0,.26)),' +
'var(--xz-sh-amb,0 2px 8px rgba(15,27,45,.08)),inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6));' +
'-webkit-backdrop-filter:blur(14px) saturate(1.3);backdrop-filter:blur(14px) saturate(1.3);' +
'padding:.7rem .8rem;font-size:.82rem;line-height:1.55;animation:xztIn2 .16s ease}' +
'@keyframes xztIn2{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}' +
'.xzt-bub-t{display:flex;align-items:center;gap:.4rem;font-weight:700;' +
'font-size:.86rem;margin-bottom:.35rem;word-break:break-all}' +
'.xzt-bub-t .asb-i{color:var(--xz-accent,#4f6ef7);font-size:1.05rem;flex-shrink:0}' +
'.xzt-bub-n{margin-left:auto;font-weight:500;font-size:.75rem;color:var(--xz-muted,#999);' +
'font-variant-numeric:tabular-nums}' +
'.xzt-bub-b{color:var(--xz-txt,#333);word-break:break-word;max-height:150px;overflow-y:auto}' +
'.xzt-bub-u{margin-top:.35rem;color:var(--xz-muted,#777);font-size:.76rem}' +
'.xzt-bub-row{display:flex;gap:.4rem;margin-top:.6rem;align-items:center}' +
'.xzt-bub-row button{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 90%,transparent);' +
'border-radius:10px;cursor:pointer;font-family:inherit;font-size:.78rem;padding:.38rem .7rem;' +
'background:var(--xz-input,#f5f6f8);color:var(--xz-txt,#333);display:inline-flex;' +
'align-items:center;gap:.35em;transition:border-color .15s ease,filter .15s ease}' +
'.xzt-bub-row button:hover{border-color:var(--xz-accent,#4f6ef7)}' +
'.xzt-bub-row button.pri{background:var(--xz-accent,#4f6ef7);' +
'background-image:linear-gradient(145deg,var(--xz-rb-2,#4f6ef7),var(--xz-rb-3,#8b5cf6));' +
'border-color:transparent;color:#fff;font-weight:600;' +
'box-shadow:0 6px 14px -8px var(--xz-tint,rgba(79,110,247,.5))}' +
'.xzt-bub-row button.pri:hover{filter:brightness(1.06)}' +
'.xzt-bub-row button.ic{width:30px;height:30px;padding:0;border-radius:50%;' +
'justify-content:center;margin-left:auto;background:none}' +
'.xzt-bub-safe{margin-top:.5rem;font-size:.75rem;color:var(--xz-muted,#999);' +
'display:flex;align-items:flex-start;gap:.35em}' +
'.xzt-bub-safe .asb-i{color:#059669;flex-shrink:0;margin-top:.15em}' +
/* P0-3 2026-08-27：① 虚线边框在设计系统里是「占位/拖放区」语义，用在主打
   功能上＝看起来没做完、没人敢点 → 改实线 + --xz-input 柔和底（跟随主题，
   不写死色值）；② hint 原为 nowrap+ellipsis，把「不会真的执行任何操作」这句
   打消顾虑的承诺截断成「不会真…」——恰恰是最该看全的那句。改整行独占
   （flex 100%）自然换行，行高两行是刻意的代价。 */
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

  /* ── 面板「教学」模式（实施73 P1-1，2026-08-28）──
     此前是往面板底部插一行虚线胶囊（`.xzt-row` + MutationObserver 自愈）：
     入口挤在最不显眼的位置、且报障/我的页签下也跟着显示。现在升格为模式条
     里的一格，由球提供容器、本模块只负责内容——协作面从「插一行」变成
     「认领一个模式」，仍不触碰球的内部符号（走公共 API registerMode）。 */
  function mountMode(el, api) {
    S.api = api;
    S.modeEl = el;
    el.innerHTML =
      '<div class="asb-md-hero">' +
      '<div class="asb-md-t">' + ic('cap') + '<span>' + esc(t('mode_t')) + '</span></div>' +
      '<div class="asb-md-d">' + esc(t('mode_d')) + '</div>' +
      /* P2-1 状态行：由 syncStat 填内容（词典异步下发，这里先留空壳） */
      '<div class="asb-md-stat" data-n="-1"></div>' +
      '<button type="button" class="asb-md-go' + (S.on ? ' off' : '') +
      '" data-xzt="mode-go">' + ic(S.on ? 'x' : 'cap') + '<span>' +
      esc(t(S.on ? 'row_btn_on' : 'row_btn')) + '</span></button>' +
      '<div class="asb-md-row">' +
      '<button type="button" class="asb-md-b" data-xzt="mode-tour">' + ic('compass') +
      '<span>' + esc(t('tour_btn')) + '</span></button>' +
      '<button type="button" class="asb-md-b" data-xzt="mode-page">' + ic('book') +
      '<span>' + esc(t('banner_page')) + '</span></button></div>' +
      /* 安全承诺全文常驻（不再是被省略号吃掉半句的一行 hint）——
         「会不会真的点下去」正是没人敢开教学模式的第一顾虑。 */
      '<div class="asb-md-safe">' + ic('shield') + '<span>' + esc(t('row_hint')) +
      '</span></div>' +
      '</div>';
    applyVars(el);
    el.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-xzt]');
      if (!b) { return; }
      var a = b.getAttribute('data-xzt');
      if (a === 'mode-go') {
        if (S.on) { stop(); } else { start(); }
        return;
      }
      /* 导览/整页讲解都要先进教学态（它们复用同一套气泡与拦截器） */
      if (a === 'mode-tour') { if (!S.on) { start(); } startTour(); return; }
      if (a === 'mode-page') { if (!S.on) { start(); } askPage(); }
    });
    syncStat();
  }

  /* 教学态开关会改按钮文案/配色——开或退之后让球重挂本模式内容 */
  function syncRowBtn() {
    if (S.api && typeof S.api.refresh === 'function') {
      try { S.api.refresh(); } catch (e) { /* ignore */ }
    }
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
      } else {
        S.dictFailed = true;
      }
      syncStat();   /* 词典迟到 → 回填状态行，否则永远停在「正在准备…」 */
    }).catch(function () {
      /* 后端未装载：保持空泛兜底，绝不报错——但状态行要如实说取不到 */
      S.dictFailed = true;
      syncStat();
    });
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
      hit = el.closest('.xzt-banner,.xzt-bubble,.asb-wrap,.asb-panel,.asb-an');
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
      '<div class="xzt-bub-t">' + ic('cap') + '<span>' +
      esc(term ? term.title : label) + '</span></div>' +
      '<div class="xzt-bub-b">' + body + '</div>' +
      '<div class="xzt-bub-row">' +
      '<button type="button" class="pri" data-xzt="ask">' + ic('spark') + '<span>' +
      esc(t('bub_ask')) + '</span></button>' +
      '<button type="button" data-xzt="close">' + esc(t('bub_close')) +
      '</button></div>' +
      '<div class="xzt-bub-safe">' + ic('shield') + '<span>' + esc(t('bub_safe')) +
      '</span></div>';
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
  var TOUR_MAX = 8;   /* 导览站点上限：再多就不是「走一遍」而是上课 */
  var SCAN_MAX = 60;  /* 计数上限：给「本页 N 处」一个不失真又不卡页的天花板 */

  /* ── 唯一扫描器（实施73 P2-1）：可见交互控件 × 词典命中，按讲解标题去重。
     ⚠ 计数（本页 N 处可讲解）与导览（带我走一遍）必须共用它。两套扫描一旦
     分叉，卡片说 12、导览只走 5，用户只会认为导览坏了——门禁钉住这一点。
     返回 null＝词典未就绪（与「扫出 0 处」是两件事，别合并）。 ── */
  function scanTeachable(limit) {
    ensureDict();
    if (!S.dict) { return null; }
    var nodes;
    try { nodes = document.querySelectorAll(PICK_SEL); } catch (e) { return null; }
    var items = [];
    var seen = {};
    var cap = limit > 0 ? limit : SCAN_MAX;
    for (var i = 0; i < nodes.length && items.length < cap; i++) {
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
  function collectTourItems() {
    return scanTeachable(TOUR_MAX) || [];
  }

  /* ── 状态卡「本页 N 处可讲解」（实施73 P2-1）──
     教学模式此前只说「点哪教哪」，没有任何进入理由；N 是最便宜的那个理由。
     顺带它是 P2-2「哪页该补词条/锚点」的直接读数（见 beaconCoverage）。
     -1＝词典还在路上，-2＝取词典失败（三种状态各说各话，不装作 0 处）。 */
  function teachStat() {
    if (S.dictFailed && !S.dict) { return -2; }
    var items = scanTeachable(0);
    return items === null ? -1 : items.length;
  }
  function statHtml(n) {
    if (n === -2) { return esc(t('stat_off')); }
    if (n < 0) { return esc(t('stat_wait')); }
    if (n === 0) { return esc(t('stat_zero')); }
    return ic('compass') + ' ' + esc(t('stat_n1')) + '<b>' + n + '</b>' + esc(t('stat_n2'));
  }
  /* beacon 只能带 action 带不了数字 → 分三桶落。ops 里按页看
     asb_teach_cov_0 的分布，就是「该给哪页补词条」的采购清单。
     每页每桶一次，避免开合面板把分母灌水。 */
  function beaconCoverage(n) {
    var b = n === 0 ? 'asb_teach_cov_0'
      : (n < 5 ? 'asb_teach_cov_lo' : 'asb_teach_cov_ok');
    var key = (location.pathname || '/') + '|' + b;
    if (S.covSent[key]) { return; }
    S.covSent[key] = 1;
    beacon(b);
  }
  /* 词典是异步下发的（workspace 壳走 /api/assistant/terms），所以状态行
     必须能被回填——否则首开面板永远停在「正在准备讲解词典…」。 */
  function syncStat() {
    var host = S.modeEl;
    if (!host || !host.isConnected) { S.modeEl = null; return; }
    var box = host.querySelector('.asb-md-stat');
    if (!box) { return; }
    var n = teachStat();
    box.innerHTML = statHtml(n);
    box.setAttribute('data-n', String(n));
    if (n >= 0) { beaconCoverage(n); }
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
        '<button type="button" class="pri" data-xzt="ask">' + ic('spark') + '<span>' +
        esc(t('bub_ask')) + '</span></button>' +
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
      '<div class="xzt-bub-t">' + ic('compass') + '<span>' + esc(term.title) + '</span>' +
      '<span class="xzt-bub-n">' + (TOUR.idx + 1) + ' / ' +
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
      '<button type="button" class="pri" data-xzt="tnext">' + (last ? ic('check') : '') +
      '<span>' + esc(t(last ? 'tour_done' : 'tour_next')) + '</span></button>' +
      '<button type="button" class="ic" data-xzt="tend" aria-label="' +
      esc(t('banner_exit')) + '">' + ic('x') + '</button></div>';
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
      '<button type="button" data-xzt="btour">' + ic('compass') + '<span>' +
      esc(t('tour_btn')) + '</span></button>' +
      '<button type="button" data-xzt="bpage">' + ic('book') + '<span>' +
      esc(t('banner_page')) + '</span></button>' +
      '<button type="button" data-xzt="bexit">' + ic('x') + '<span>' +
      esc(t('banner_exit')) + '</span></button>';
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
    /* 球可能尚未 buildDom 完成：短轮询认领模式（球缺席=静默零痕迹，
       面板退化为单模式的纯问答，不会留下点不动的空格子）。 */
    var tries = 0;
    (function poll() {
      if (window.AssistantBall &&
          typeof window.AssistantBall.registerMode === 'function') {
        S.claimed = window.AssistantBall.registerMode('teach', {
          /* iconName＝球内置线性图标名（v2.1 起 registerMode 优先按它解析）；
             icon 仍传 emoji 给不识别 iconName 的旧球缓存当回退 */
          order: 20, icon: '🎓', iconName: 'cap', labelKey: 'mode_teach',
          mount: mountMode,
        });
      }
      if (!S.claimed && ++tries < 20) { setTimeout(poll, 500); }
    })();
  }

  window.XZTeach = { init: init, start: start, stop: stop, _ver: VER };
})();
