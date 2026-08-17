"use strict";

const ICONS = { telegram: "✈️", whatsapp: "🟢", line: "💬", messenger: "💠", instagram: "📷", x: "𝕏", zalo: "💙", signal: "🔵" };

// 标签条自有图标（收件箱/新增）：lucide 单色描边，与工作台 ui_icons 同风格、吃 currentColor
// 主题色——28px 小标签里 emoji 渲染粗糙且不随激活态变色（platform-icons.js 只管平台图标）。
const INBOX_TAB_SVG =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-6l-2 3h-4l-2-3H2"/>' +
  '<path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>';
const PLUS_TAB_SVG =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';

// 平台图标：优先真实品牌原生图标(platform-icons.js)，缺库时回落 emoji。
// inlineDefs 使渐变自包含，跨 shadow / 独立场景均可渲染。
function platIconHtml(p, size) {
  return window.platformIconSVG
    ? window.platformIconSVG(p, { size: size || 20, inlineDefs: true })
    : (ICONS[p] || "💬");
}

// 统一收件箱标签页的固定 id（区别于 config.accounts[] 的真实账号）
const INBOX_ID = "__inbox__";

// 可内嵌官方网页并注入的平台（须与 inject/profiles.js 的 BUILTIN_PROFILES[*].supported 对齐）。
// telegram/whatsapp：完整定制档；instagram/messenger/x/zalo：通用工厂档（翻译/智能回复/状态可用，
// 同步回流默认关闭待选择器现场校准，可经 /api/desktop/selector-profiles 热更新打开）。
// 2026-08-13：后四者在 platform-caps 标 assistOnly——标签「人工」+ 诚实条 + 禁止 HostBridge/
// conv-ops 伪全自动；全自动产能只认统一收件箱（Messenger=messenger-web 服务器登录）。
// LINE 无可用完整网页版聊天（line.me 为营销页）→ 仍不内嵌，统一引导到收件箱。
const EMBEDDABLE = {
  telegram: true, whatsapp: true, instagram: true, messenger: true, x: true, zalo: true,
};
function isEmbeddable(platform) { return !!EMBEDDABLE[platform]; }

// 注入诊断：保存各账号最近一次 inject-status 上报，按激活 Tab 渲染顶部状态条。
// deriveInjectState 由 inject-status.js 提供（浏览器全局；亦可 node 单测）。
const InjectStatus = { byId: {}, el: null, activeId: null };

// 统一收件箱运行态（供 rail 切换时联动遮罩显隐）：wv=后台 webview，overlay=连接/错误遮罩，
// applyVisibility(active)=切到本标签时按当前阶段决定遮罩显隐。
const Inbox = { wv: null, overlay: null, phase: "init", applyVisibility: null };

// 后台 /workspace 走 session cookie 鉴权；webview 落到 /login 时在页面内 POST 凭据自动登录后回跳。
// cred 为 {auth_token} 或 {username,password}；成功/失败都 location.replace 回目标页——
// 失败会被后端再 303 回 /login，由调用方据「再次到 /login」切到下一组凭据或转人工。
function backendLoginJS(cred, path) {
  const repl = "location.replace(" + JSON.stringify(path) + ");";
  return (
    "(function(){try{" +
    "var c=" + JSON.stringify(cred || {}) + ";" +
    "var b=Object.keys(c).map(function(k){return k+'='+encodeURIComponent(c[k]);}).join('&');" +
    "fetch('/login',{method:'POST'," +
    "headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b," +
    "credentials:'same-origin'})" +
    ".then(function(){" + repl + "})" +
    ".catch(function(){" + repl + "});" +
    "}catch(e){}})();"
  );
}

// 多账号:把 config.accounts[] 解析成 rail 渲染单元。每个账号引用 platforms[] 的平台模板拿 url/inject。
// 向后兼容:accounts 为空时,从启用的 platforms 合成单账号(account_id = platform.account_id || `${id}-desktop`)。
let ACCOUNTS = []; // 解析后的账号列表（rail 渲染源）
const ACCOUNT_BY_ID = {}; // account_id → 解析后账号
let TEMPLATES = {}; // platform → config.platforms[*] 模板（运行时新增内嵌账号取 url/inject）
const RENDERED = new Set(); // 已渲染 rail Tab 的 id，运行时新增/重建去重
const ACTIVE_CHAT_BY_ACCOUNT = {}; // D4b：account_id → 当前打开会话 chat_key（受控出站分发用）

// 运行时新增的内嵌账号持久化到 localStorage，重启后自动重建（partition=persist:id 故会话也续上）。
const RUNTIME_ACCOUNTS_KEY = "desktop_runtime_accounts";
function loadRuntimeAccounts() {
  try {
    const raw = localStorage.getItem(RUNTIME_ACCOUNTS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (e) {
    return [];
  }
}
function persistRuntimeAccounts() {
  try {
    localStorage.setItem(RUNTIME_ACCOUNTS_KEY, JSON.stringify(serializeRuntimeAccounts(ACCOUNTS)));
  } catch (e) {
    /* localStorage 不可用时忽略，仅失去重启持久化 */
  }
}

function resolveAccounts(cfg) {
  const templates = {};
  (cfg.platforms || []).forEach((p) => {
    templates[p.id] = p;
  });
  TEMPLATES = templates;
  let list = (cfg.accounts || []).filter((a) => a && a.platform && a.enabled !== false);
  if (!list.length) {
    list = (cfg.platforms || [])
      .filter((p) => p.enabled)
      .map((p) => ({ id: p.account_id || `${p.id}-desktop`, platform: p.id, label: p.name }));
  }
  return list
    .map((a) => {
      const t = templates[a.platform] || {};
      return {
        id: a.id,
        platform: a.platform,
        label: a.label || t.name || a.platform,
        url: a.url || t.url || "",
        inject: a.inject || t.inject || "",
        persona_id: a.persona_id || t.persona_id || "",
        proxy: a.proxy || "",
      };
    })
    .filter((a) => a.id && a.url);
}

(async function () {
  const cfg = await window.shell.getConfig();
  // Option C：默认只保留统一收件箱（与网页同源同款），关掉左侧账号栏 rail + 内嵌官方网页 Tab。
  // 置 config.embedded_official_pages.enabled=true 可整套恢复（免真机扫码 + 注入栈）。
  let EMBEDDED_ON = ((cfg.embedded_official_pages || {}).enabled === true);
  // ── ui_visibility.manual_console（2026-08-14）：服务端「内部功能显隐」开关统一压制
  // 内嵌官方网页版整套（rail 平台标签 + ➕新增 + 页内标签条 + openEmbedded 深链）——
  // 本地配置开着也要过服务端闸（开发者页 /developer 才能放行）。仅在本地已启用时才探
  // （省一次启动请求）；端点缺席/后端未起＝维持本地配置（fail-open，旧后端零回归）。
  // ⚠ 必须经主进程 IPC（desktop:ui-flags → backendGet）取数：壳页面是 file:// 源、
  // 后端默认不回 CORS 头，renderer 直接 fetch 会被 CORS 拦下永远走 fail-open
  //（2026-08-15 实锤：1.0.33 首包压制全程失效的根因）。
  // ⚠ 首探失败必须重试（2026-08-15 173 实锤）：坐席机 spawn.enabled=true 时壳与
  // backend sidecar 同秒启动，sidecar 冷启动 10s+，一次性 2.5s 探针必失败 →
  // fail-open 把该藏的 rail 留下。故拿不到确定答案时每 5s 补探（最多 2 分钟），
  // 拿到 manual_console=false 再补挂 no-embedded 类（CSS 整条藏 rail，事后加同样生效）。
  if (EMBEDDED_ON && window.shell && window.shell.uiFlags) {
    const applyUiFlags = (d) => {
      if (!(d && d.ok && d.flags)) return false;          // 未起/失败＝无定论，继续探
      if (d.flags.manual_console === false) {
        EMBEDDED_ON = false;
        const appEl = document.getElementById("app");
        if (appEl) appEl.classList.add("no-embedded");
        try { pushShellState(true); } catch (_) {}        // 通知页面收「打开官方网页版」入口
      }
      return true;                                        // 有定论（无论显/藏）即停
    };
    let settled = false;
    try { settled = applyUiFlags(await window.shell.uiFlags()); } catch (_) {}
    if (!settled) {
      let tries = 0;
      const t = setInterval(async () => {
        if (!EMBEDDED_ON || ++tries > 24) { clearInterval(t); return; }
        let d = null;
        try { d = await window.shell.uiFlags(); } catch (_) {}
        if (applyUiFlags(d)) clearInterval(t);
      }, 5000);
    }
  }
  if (!EMBEDDED_ON) {
    const appEl = document.getElementById("app");
    if (appEl) appEl.classList.add("no-embedded");
  }
  let WHATSAPP_UA = cfg.whatsapp_user_agent || "";
  function whatsappUserAgent() {
    return WHATSAPP_UA || (typeof chromeLikeUserAgent === "function" ? chromeLikeUserAgent() : "");
  }
  // D2：whatsapp/instagram/messenger/x/zalo 等拒载 Electron UA 的平台统一伪装 Chrome；
  // telegram 用默认 UA（零回归）。needsChromeUa 由 webview-ua.js 提供（全局 / 单测共用）。
  function uaNeedsSpoof(platform) {
    return typeof needsChromeUa === "function" && needsChromeUa(platform);
  }
  function applyWhatsappWebviewAttrs(wv, platform) {
    if (!wv || !uaNeedsSpoof(platform)) return;
    const ua = whatsappUserAgent();
    if (ua) wv.setAttribute("useragent", ua);
  }
  async function ensureWhatsappSessionUa(acc) {
    if (!acc || !uaNeedsSpoof(acc.platform)) return;
    if (!window.shell.applyWhatsappUa) return;
    try { await window.shell.applyWhatsappUa({ id: acc.id, platform: acc.platform }); } catch (_) {}
  }
  const rail = document.getElementById("rail");
  const stage = document.getElementById("webviews");
  // 竖栏纵向滚动是原生手势，无需旧横条时代的 wheel→scrollLeft 换轴处理。
  // Ctrl+1..9 快捷切标签（批次三）：按竖栏可见顺序取第 N 个标签（不含 ➕），复用各标签
  // 自己的 click 逻辑（via-inbox 项照常跳工作台）。已知边界：焦点落在 webview 页面内部时
  // 按键被页面吞掉，本快捷键只覆盖壳 UI 持焦的场景（全局劫持需 main 层 before-input-event，
  // 会连官方网页输入框一起吃 Ctrl+数字，刻意不做）。
  document.addEventListener("keydown", (e) => {
    if (!e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    if (e.key < "1" || e.key > "9") return;
    const target = visibleRailItems()[Number(e.key) - 1];
    if (target) { e.preventDefault(); target.click(); }
  });
  function visibleRailItems() {
    return Array.from(rail.querySelectorAll(".rail-item:not(.rail-add)"))
      .filter((el) => el.offsetParent !== null);
  }
  // 快捷键可发现性：tooltip 里标注 Ctrl+N 序号。不用 MutationObserver 盯 DOM——tooltip
  // 只在 hover 时显示，鼠标进栏那一刻惰性重算一次即可（序号跟 keydown 同一枚举口径，
  // 永不漂移）。title 被别处改写时（cur≠上次合成值）以新值为基，不吞外部更新。
  function refreshRailShortcutHints() {
    visibleRailItems().forEach((el, i) => {
      const cur = el.getAttribute("title") || "";
      if (cur !== el.dataset.tipComposed) el.dataset.tipBase = cur;
      const base = el.dataset.tipBase || "";
      const composed = i < 9 ? (base ? base + "\n" : "") + "快捷键 Ctrl+" + (i + 1) : base;
      if (composed !== cur) { el.title = composed; el.dataset.tipComposed = composed; }
    });
  }
  rail.addEventListener("mouseenter", refreshRailShortcutHints);
  ACCOUNTS = mergeRuntimeAccounts(resolveAccounts(cfg), loadRuntimeAccounts());
  ACCOUNTS.forEach((a) => {
    ACCOUNT_BY_ID[a.id] = a;
  });

  // ── 页内标签条接管（P2 2026-08-14）：/workspace 页在自己头部（ws-top）之下渲染
  // cx-shell-strip（数据=本壳 pushShellState），渲染成功经桥回报 stripReady →
  // 工作台标签激活期间壳竖栏整条让位（视觉上「状态栏移到工作台状态栏下面」）。
  // 页面每次导航 stripReady 归零（旧页面/加载失败/落在 /login 都不让位——竖栏兜底，
  // 绝不出现「rail 藏了、页里又没标签条」的死路）；切到内嵌官方页标签时竖栏回归。
  let _activeTabId = INBOX_ID;
  let _pageStripLive = false;
  function syncPageStripTakeover() {
    const appEl = document.getElementById("app");
    if (!appEl) return;
    appEl.classList.toggle("rail-page-strip", _pageStripLive && _activeTabId === INBOX_ID);
  }

  function activate(id) {
    const isInbox = id === INBOX_ID;
    _activeTabId = id;
    syncPageStripTakeover();
    document.querySelectorAll(".rail-item").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
    document.querySelectorAll("#webviews webview").forEach((wv) => wv.classList.toggle("active", wv.dataset.id === id));
    // 收件箱(/workspace)页面**永远自带**完整业务助手右栏 → 激活收件箱时一律隐藏桌面原生 #copilot,
    // 避免「两个业务助手」(其中桌面侧因无会话上下文而空、功能不可用)。
    // 内嵌平台 Tab(Telegram/WhatsApp 原生聊天)才保留桌面 #copilot(原生/ iframe 副驾数据源)。
    const _cp = document.getElementById("copilot");
    if (_cp) {
      // 双保险：!important 内联压制任何样式表规则 + .cp-hidden 类，杜绝「两个业务助手」复发
      _cp.classList.toggle("cp-hidden", isInbox);
      if (isInbox) _cp.style.setProperty("display", "none", "important");
      else _cp.style.removeProperty("display");
    }
    // 连接/错误遮罩只属于收件箱：切到本标签按当前阶段决定显隐，切走则一律藏起
    if (Inbox.applyVisibility) Inbox.applyVisibility(isInbox);
    // 注入状态条只属于内嵌平台 Tab：收件箱激活时隐藏
    InjectStatus.activeId = isInbox ? null : id;
    renderInjectStatus();
    // Path2 assist-only 诚实条：Messenger 等官方网页可聊但不接全自动
    if (typeof syncAssistOnlyBanner === "function") syncAssistOnlyBanner(isInbox ? null : id);
  }
  Inbox.activate = activate; // 暴露给模块级「在收件箱打开会话」深链使用

  // 顶部注入状态条：盖在内嵌平台 webview 右上角
  function buildInjectStatusPill() {
    const el = document.createElement("div");
    el.id = "inject-status";
    el.hidden = true;
    el.innerHTML = '<span class="is-dot"></span><span class="is-text"></span>';
    stage.appendChild(el);
    InjectStatus.el = el;
  }
  function renderInjectStatus() {
    const el = InjectStatus.el;
    if (!el) return;
    const id = InjectStatus.activeId;
    if (!id) { el.hidden = true; return; }
    const st = deriveInjectState(InjectStatus.byId[id]);
    el.className = "is-" + st.cls;
    el.querySelector(".is-text").textContent = st.text;
    el.title = st.detail;
    el.hidden = false;
  }
  function onInjectStatus(payload, wv) {
    if (!payload || !wv) return;
    InjectStatus.byId[wv.dataset.id] = payload;
    updateRailHealth(wv.dataset.id);   // 每个号刷自己的栏点(不只当前聚焦),这才是「一眼看全」
    if (InjectStatus.activeId === wv.dataset.id) renderInjectStatus();
  }

  // 账号栏健康三态点：把该账号最近一次 inject-status 经 webmulti.railBadge 滚成一枚角标点
  // （dot=on/warn/off/idle）。当前 renderer 只有 inject 这一维可靠（session 在线 / translate
  // 可达尚未逐账号接线）→ 直接用 inject 的 cls 驱动,诚实反映「已知的」；将来接了另两维,
  // 换成 accountHealthState(三维取最差) 即可,railBadge 出参形状不变、这里零改动。
  function updateRailHealth(id) {
    if (typeof railBadge !== "function") return; // webmulti 未加载：静默降级,不影响主链
    const item = document.querySelector('.rail-item[data-id="' + id + '"]');
    if (!item) return;
    const dot = item.querySelector(".rail-dot");
    if (!dot) return;
    const st = deriveInjectState(InjectStatus.byId[id]);
    const badge = railBadge({ level: st.cls, text: st.text });
    dot.className = "rail-dot " + badge.dot;
    dot.title = badge.text || "";
    pushShellState();   // 健康变化同步回推页面抽屉（内容级去重，常态零流量）
  }
  buildInjectStatusPill();

  // 统一收件箱：内嵌后台 /workspace（与网页后台同源同款，聚合 Telegram / WhatsApp / Messenger / LINE / Web）
  // 默认激活为首屏，直接对齐后台多平台聊天能力。带连接/登录/错误三态遮罩：遮住登录闪屏、后端未起时给重试。
  function buildInboxTab() {
    const ui = (cfg && cfg.unified_inbox) || {};
    if (ui.enabled === false) return false;
    const backend = (cfg && cfg.backend) || {};
    const base = (backend.base_url || "http://127.0.0.1:18799").replace(/\/+$/, "");
    // navPath=用于路由判定的纯路径；navTarget=实际加载/登录回跳的相对地址（含 ?lang= 语言对齐）
    const navPath = ui.path || "/workspace";
    const lang = ui.lang || "";
    let navTarget = navPath + (lang ? (navPath.includes("?") ? "&" : "?") + "lang=" + encodeURIComponent(lang) : "");
    // 桌面壳是深色专属 → 给嵌入的 /workspace 强制 ?theme=dark，避免独立 webview 分区 auto→跟随系统出现「深色壳里白聊天」。
    navTarget += (navTarget.includes("?") ? "&" : "?") + "theme=dark";
    const fullUrl = base + navTarget;
    // 凭据链：优先 token，回退用户名/密码（token 为空或失效时自动接力）
    const creds = [];
    if (backend.token) creds.push({ auth_token: backend.token });
    if (backend.user && backend.pass) creds.push({ username: backend.user, password: backend.pass });

    const item = document.createElement("div");
    item.className = "rail-item active";
    item.dataset.id = INBOX_ID;
    item.title = "人工操作台（统一收件箱 · AI+人工协作）";
    item.setAttribute("aria-label", "人工操作台");
    // rail-badge＝全局未读徽标：数据由 /workspace 页面经 inbox-preload.js 桥回推
    // （cmd=badge），壳自己不另拉后端——未读口径单源在页面聚合逻辑里。
    // rail-chip「AI+人工」＝模式徽章：与官方平台组的「人工」chip 构成两类入口的色彩语义。
    item.innerHTML = `<span class="ic">${INBOX_TAB_SVG}</span><span>${ui.label || "人工操作台"}</span>` +
      '<span class="rail-chip" title="AI 拟稿/自动回复 + 人工审核协作">AI+人工</span>' +
      '<span class="rail-badge" id="rail-inbox-badge" hidden></span>';
    item.addEventListener("click", () => activate(INBOX_ID));
    rail.appendChild(item);

    const wv = document.createElement("webview");
    wv.dataset.id = INBOX_ID;
    wv.dataset.kind = "backend";
    wv.className = "active";
    // 启动闸门：先挂空白页，等后端探活通过再 loadURL（见 bootLoad）。
    // 直接把 fullUrl 写进 src 会与后端冷启动赛跑——输了这一跑，工作台的
    // Service Worker 会用离线壳接管这次导航（HTTP 200 且 URL 不变），
    // 壳误判为加载成功 → 遮罩消失 + 自动重连停摆 → 用户只能手点「重新连接」。
    wv.setAttribute("src", "about:blank");
    wv.setAttribute("partition", "persist:backend-workspace");
    wv.setAttribute("allowpopups", "true");
    // 反向桥 preload：向 /workspace 页面暴露 window.__chatxShell（打开官方网页版标签 /
    // 未读徽标回推）。preload 属性须在挂载(appendChild)前设置；跨导航（登录回跳）持续生效。
    // 纯浏览器打开同一页面时没有这份 preload → 页面按「桥不存在」降级，模板零分叉。
    try { wv.setAttribute("preload", new URL("inbox-preload.js", window.location.href).toString()); } catch (_) {}
    wv.addEventListener("ipc-message", (e) => {
      if (e.channel === "chatx-bridge") onShellBridge(e.args[0]);
    });
    // 页面每次导航（登录回跳/手动刷新）preload 内的状态快照清零 → 强制重推当前标签状态；
    // 页内标签条同步归零（新页面须重新 stripReady 才让位竖栏，防「藏了 rail 页里又没条」死路）
    wv.addEventListener("dom-ready", () => {
      _pageStripLive = false;
      syncPageStripTakeover();
      pushShellState(true);
    });
    wv._loginIdx = 0;       // 凭据链游标
    wv._loginPending = false; // 登录尝试进行中标记
    stage.appendChild(wv);

    // ── 连接遮罩（loading / error）：盖在收件箱 webview 上 ──────────────
    const overlay = document.createElement("div");
    overlay.className = "inbox-overlay";
    overlay.innerHTML =
      '<div class="inbox-overlay-card">' +
      '<div class="inbox-spinner"></div>' +
      '<div class="inbox-msg">正在连接后台…</div>' +
      '<button class="inbox-retry" hidden>重试连接</button>' +
      "</div>";
    stage.appendChild(overlay);
    const msgEl = overlay.querySelector(".inbox-msg");
    const spinEl = overlay.querySelector(".inbox-spinner");
    const retryBtn = overlay.querySelector(".inbox-retry");

    Inbox.wv = wv;
    Inbox.overlay = overlay;
    Inbox.phase = "loading";

    function setPhase(phase, msg) {
      Inbox.phase = phase;
      if (msg) msgEl.textContent = msg;
      const isErr = phase === "error";
      spinEl.hidden = isErr;
      retryBtn.hidden = !isErr;
      overlay.classList.toggle("err", isErr);
      // 仅在本标签激活时显示遮罩；ready 态彻底隐藏
      const active = item.classList.contains("active");
      overlay.style.display = (phase !== "ready" && active) ? "flex" : "none";
    }
    // 供 rail 切换联动：切到收件箱时按阶段恢复遮罩，切走时隐藏
    Inbox.applyVisibility = function (active) {
      overlay.style.display = (active && Inbox.phase !== "ready") ? "flex" : "none";
    };

    // 后端未起→自动重连：错误态下轮询健康探针，一旦可达自动重载（用户先开桌面后开后端也能自愈）
    function stopReconnectPoll() {
      if (Inbox._reconnectTimer) { clearTimeout(Inbox._reconnectTimer); Inbox._reconnectTimer = null; }
    }
    function startReconnectPoll() {
      if (Inbox._reconnectTimer) return;
      const tick = async () => {
        Inbox._reconnectTimer = null;
        if (Inbox.phase !== "error") return;
        let ok = false;
        try { const h = await window.shell.backendHealth(); ok = !!(h && h.ok); } catch (e) {}
        if (ok) { reload(); return; }
        // 桌面自拉起后端时，把笼统的「等待重连」细化为启动阶段，降低首启焦虑。
        try {
          const st = window.shell.backendSpawnStatus ? await window.shell.backendSpawnStatus() : null;
          const phase = st && st.status;
          if (phase === "starting" || phase === "probing")
            setPhase("error", "正在启动后台服务，请稍候…（首次启动较慢）");
          else if (phase === "failed")
            setPhase("error", "后台启动失败：" + (st.lastError || "未知错误") + "\n详见 用户数据/logs/backend.log；将持续重试…");
        } catch (e) {}
        Inbox._reconnectTimer = setTimeout(tick, 2000);
      };
      Inbox._reconnectTimer = setTimeout(tick, 2000);
    }

    function reload() {
      Inbox._bootAborted = true; // 手动重试接管启动闸门，避免闸门稍后再导航一次
      stopReconnectPoll();
      wv._loginIdx = 0;
      wv._loginPending = false;
      wv._phase = undefined;
      setPhase("loading", "正在连接后台…");
      try { wv.loadURL(fullUrl); } catch (e) { try { wv.reload(); } catch (e2) {} }
    }
    retryBtn.addEventListener("click", reload);

    // 单一导航处理：凭据链自动登录 + 三态遮罩。
    // _loginIdx 指向下一组待试凭据；_loginPending 防同一次 /login 加载被 dom-ready+did-navigate 重复触发。
    function onNav(url) {
      if (!url || url.indexOf("about:") === 0) return; // 启动闸门期的空白页，不参与状态判定
      let p = "";
      try { p = new URL(url).pathname; } catch (e) { return; }
      if (p === navPath) {
        // 路径对 ≠ 真的进了工作台：PWA 离线壳被 SW 回落时 URL 仍是 navPath 且 200。
        // 用页面自带的 data-ws-offline 标记区分，识破后回到 error 态继续自动重连。
        wv.executeJavaScript(
          "!!(document.body && document.body.dataset && document.body.dataset.wsOffline)"
        ).then((isOfflineShell) => {
          if (!isOfflineShell) { wv._phase = "done"; wv._loginIdx = 0; setPhase("ready"); return; }
          wv._phase = "error";
          setPhase("error", "后台还没起来（当前是离线页）。\n正在等待后台启动并自动重连…\n后端地址：" + base);
          startReconnectPoll();
        }).catch(() => {
          // 旧后端不带该标记 / 取值失败 → 保持原行为，不因探测失败卡住用户
          wv._phase = "done"; wv._loginIdx = 0; setPhase("ready");
        });
        return;
      }
      if (p === "/login") {
        if (wv._loginPending) return; // 本次登录尝试进行中，等其 location.replace
        const idx = wv._loginIdx || 0;
        if (idx < creds.length) {
          wv._loginIdx = idx + 1;
          wv._loginPending = true;
          setPhase("loading", idx === 0 ? "正在登录后台…" : "首选凭据失败，尝试备用凭据…");
          wv.executeJavaScript(backendLoginJS(creds[idx], navTarget)).catch(() => {});
        } else {
          // 凭据用尽（或未配置）：露出登录页让人工处理
          wv._phase = "failed";
          setPhase("ready");
          if (creds.length) flash("自动登录失败，请在页面手动登录");
        }
        return;
      }
      // 其它后台子路径（/setup 等）：直接展示
      setPhase("ready");
    }

    wv.addEventListener("did-start-loading", () => {
      wv._loginPending = false; // 新一次加载开始（含 replace 跳转），解除登录进行中标记
      if (wv._phase !== "done") setPhase("loading", Inbox.phase === "loading" ? msgEl.textContent : "正在连接后台…");
    });
    wv.addEventListener("dom-ready", () => { wv._navAlive = true; try { onNav(wv.getURL()); } catch (e) {} });
    wv.addEventListener("did-navigate", (e) => { wv._navAlive = true; onNav(e.url); });
    wv.addEventListener("page-title-updated", (e) => {
      const t = String(e.title || "").trim();
      if (!t || !window.shell || !window.shell.setWindowTitle) return;
      // 嵌入页 title 已由后端按生效品牌渲染（白标可改），直接透传到 OS 窗口标题——
      // 不再硬编码「智聊」，否则白标客户会被强改回默认品牌。
      window.shell.setWindowTitle(t);
    });
    wv.addEventListener("did-fail-load", (e) => {
      if (!e.isMainFrame) return;
      wv._navAlive = true; // 失败也是「导航链活着」——看门狗只抓完全静默的吞导航
      if (e.errorCode === -3) return; // ERR_ABORTED：重定向/replace 的正常中断，忽略
      wv._phase = "error";
      setPhase("error", "无法连接后台服务（" + (e.errorDescription || ("错误 " + e.errorCode)) + "）。\n正在等待后台启动并自动重连…\n后端地址：" + base);
      startReconnectPoll();
    });

    // ── 启动闸门：后端可达后才真正加载工作台 ─────────────────────────────
    // 安装版的 backend.exe 冷启动常需 15-30s（PyInstaller 解压 + 初始化）。
    // 先探活再导航，把「和后端赛跑」变成「等后端就位」，顺带让首屏不再闪 /login。
    const BOOT_POLL_MS = 700;
    const BOOT_ESCAPE_MS = 20000; // 超过它就露出可点的错误态（但后台继续自愈，不放弃）
    async function bootLoad() {
      // 探活 API 缺失（老壳/异常打包）→ 退回「直接加载」旧行为，绝不把用户卡在遮罩后面
      if (!window.shell || typeof window.shell.backendHealth !== "function") {
        try { wv.loadURL(fullUrl); } catch (e) { wv.setAttribute("src", fullUrl); }
        return;
      }
      const t0 = Date.now();
      Inbox._bootAborted = false;
      for (;;) {
        if (Inbox._bootAborted) return; // 用户点了「重试连接」，导航交给 reload()
        let ok = false, conflict = null;
        try {
          const h = await window.shell.backendHealth();
          ok = !!(h && h.ok);
          if (h && h.conflict) conflict = String(h.error || "");
        } catch (e) {}
        // 端口冲突：继续等只会一直等（对方一直在应答），必须立刻告诉用户怎么办。
        if (conflict !== null) {
          setPhase("error", "后端端口被占用：" + conflict
            + "\n请停掉占用该端口的程序，或改 config.json 的 backend.base_url 端口后重启本应用。"
            + "\n后端地址：" + base);
          return;
        }
        if (ok) break;

        const secs = Math.round((Date.now() - t0) / 1000);
        let spawnPhase = "", spawnErr = "";
        try {
          const st = window.shell.backendSpawnStatus ? await window.shell.backendSpawnStatus() : null;
          spawnPhase = (st && st.status) || "";
          spawnErr = (st && st.lastError) || "";
        } catch (e) {}

        // 20s 内静默等待（正常冷启动），之后升级为 error 态：重试按钮可见、
        // 后台轮询不停。既不让用户面对无出口的转圈，也不放弃自动恢复。
        if (Date.now() - t0 > BOOT_ESCAPE_MS || spawnPhase === "failed") {
          const why = spawnPhase === "failed"
            ? "后台启动失败：" + (spawnErr || "未知错误") + "\n详见 用户数据/logs/backend.log。"
            : "后台还没起来（已等待 " + secs + "s，首次启动较慢）。";
          setPhase("error", why + "\n正在自动重试…\n后端地址：" + base);
        } else if (secs >= 3) {
          setPhase("loading", "正在启动后台服务…（首次启动较慢，已 " + secs + "s）");
        }
        await new Promise((r) => setTimeout(r, BOOT_POLL_MS));
      }
      if (Inbox._bootAborted) return;
      setPhase("loading", "正在载入工作台…");
      wv._navAlive = false;
      try { wv.loadURL(fullUrl); } catch (e) { wv.setAttribute("src", fullUrl); }
      // 装载看门狗（2026-08-15 117 实锤）：分区里 v5 之前的旧 Service Worker 会把
      // /workspace 导航整个吞掉——dom-ready / did-navigate / did-fail-load 全静默，
      // 遮罩钉死「正在载入工作台…」；且 SW 只在成功导航时才自更新，被吞就永远自锁。
      // 后端探活刚通过（bootLoad 前提）而导航 25s 无任何动静＝按 SW 挂死处理：
      // 经主进程清该分区 serviceworkers+cachestorage 后重载一次（仅一次防循环；
      // 老壳无 clearWorkspaceSw 则只重载，不劣于旧行为）。
      setTimeout(async () => {
        if (wv._navAlive || Inbox._bootAborted || wv._swPurged) return;
        wv._swPurged = true;
        try {
          if (window.shell && window.shell.clearWorkspaceSw) await window.shell.clearWorkspaceSw();
        } catch (e) {}
        reload();
      }, 25000);
    }

    // 主动先点亮 loading 遮罩，遮住首屏可能的 /login 闪屏（不依赖 did-start-loading 时序）
    setPhase("loading", "正在连接后台…");
    bootLoad();
    return true;
  }

  const inboxOn = buildInboxTab();

  if (!ACCOUNTS.length && !inboxOn) {
    stage.innerHTML = '<div class="placeholder">config.json 里没有启用任何账号</div>';
    return;
  }

  // 收件箱关闭时的兜底首屏：第一个「可内嵌」的账号（跳过 Messenger/LINE）
  const firstEmbeddableIdx = ACCOUNTS.findIndex((a) => isEmbeddable(a.platform));

  let railAddBtn = null;
  let addMenuEl = null;

  // 竖栏 64px 只放得下短名：取「账号N」尾缀或首 4 字，全名恒在 tooltip（title）里
  function shortTabLabel(label) {
    const s = String(label || "").trim();
    const m = s.match(/(账号\s*\d+)$/);
    if (m) return m[1].replace(/\s+/g, "");
    return s.length > 5 ? s.slice(0, 4) + "…" : s;
  }

  // ── 单个内嵌平台账号 → rail Tab + webview。初始渲染与运行时新增共用，按 id 幂等。──────
  function addAccountTab(a, opts) {
    opts = opts || {};
    if (!a || !a.id || RENDERED.has(a.id)) return false;
    if (!isEmbeddable(a.platform) || !a.url) return false;
    RENDERED.add(a.id);
    ACCOUNT_BY_ID[a.id] = a;
    const active = !!opts.active;
    if (active) InjectStatus.activeId = a.id;

    const item = document.createElement("div");
    item.className = "rail-item" + (active ? " active" : "");
    item.dataset.id = a.id;
    const _pc = window.PlatformCaps;
    const _assistTag = (_pc && _pc.assistOnlyTabTag(a.platform)) || "";
    item.title = _assistTag
      ? `${a.label}（官方网页·仅人工；全自动请用「人工操作台」）`
      : `${a.label}（${a.platform}:${a.id}）`;
    item.setAttribute("aria-label", a.label);
    // 运行时新增的账号(_auto)带「✕」可就地移除；config.json 定义的账号不可在此删（交配置管理）
    const rmHtml = a._auto ? '<span class="rm" title="移除该内嵌标签">✕</span>' : "";
    const tagHtml = _assistTag
      ? `<span class="via-tag assist" title="仅人工聊天/翻译；全自动在「人工操作台」">${_assistTag}</span>`
      : "";
    // rail-dot＝账号健康三态点；rail-meta 把 chip/点/✕ 收成一行，竖栏标签不无限长高。
    item.innerHTML = `<span class="ic">${platIconHtml(a.platform)}</span><span>${shortTabLabel(a.label)}</span>` +
      `<span class="rail-meta">${tagHtml}<span class="rail-dot idle" title="等待注入…"></span>${rmHtml}</span>`;
    item.addEventListener("click", (e) => {
      if (e.target && e.target.classList && e.target.classList.contains("rm")) {
        e.stopPropagation();
        removeEmbeddedAccount(a.id);
        return;
      }
      activate(a.id);
    });
    // 浏览器习惯：鼠标中键关标签（仅运行时新增的 _auto 账号，与 ✕ 同权限规则）
    item.addEventListener("auxclick", (e) => {
      if (e.button !== 1 || !a._auto) return;
      e.preventDefault();
      removeEmbeddedAccount(a.id);
    });
    railInsert(item);

    const wv = document.createElement("webview");
    wv.dataset.id = a.id;
    wv.dataset.platform = a.platform;
    wv.dataset.account = a.id;
    wv.className = active ? "active" : "";
    // 每个账号独立 session 分区（按 account_id）→ 同平台多号并存、互不串号；与主进程代理分区一致
    wv.setAttribute("partition", `persist:${a.id}`);
    wv.setAttribute("allowpopups", "true");
    // WhatsApp Web 拒载 Electron UA；须在 setAttribute("src") 之前设 useragent
    applyWhatsappWebviewAttrs(wv, a.platform);
    ensureWhatsappSessionUa(a);
    wv.setAttribute("src", a.url);
    if (a.inject) wv.setAttribute("preload", window.shell.injectUrl(a.inject));
    // 注入脚本经 sendToHost 上报当前会话；webview 归属账号在此注入给 inject（同平台多号 hostname 相同，inject 自己分不清）
    wv.addEventListener("ipc-message", (e) => {
      if (e.channel === "active-chat") onActiveChat(e.args[0], wv);
      else if (e.channel === "inject-status") onInjectStatus(e.args[0], wv);
      else if (e.channel === "fill-result") onFillResult(e.args[0]);
    });
    wv.addEventListener("dom-ready", () => {
      try {
        wv.send("set-account", { platform: a.platform, account_id: a.id });
      } catch (err) {
        /* webview 尚未就绪，忽略 */
      }
    });
    stage.appendChild(wv);
    syncRailVisibility();
    return true;
  }

  // 非内嵌平台（Messenger/LINE）：不开内嵌死页，渲染为「↪收件箱」入口
  function addViaInboxItem(a) {
    if (!inboxOn) return; // 收件箱未开时无处可去，直接不渲染，避免死页
    const ri = document.createElement("div");
    ri.className = "rail-item via-inbox";
    ri.dataset.id = "viainbox:" + a.id;
    ri.title = `${a.label}（${a.platform}）无官方网页版聊天，请在「人工操作台」中使用`;
    ri.setAttribute("aria-label", a.label);
    ri.innerHTML = `<span class="ic">${platIconHtml(a.platform)}</span><span>${shortTabLabel(a.label)}</span><span class="via-tag">↪工作台</span>`;
    ri.addEventListener("click", () => {
      activate(INBOX_ID);
      flash(`${a.label} 无官方网页版，已切到人工操作台`);
    });
    railInsert(ri);
    syncRailVisibility();
  }

  // 新 Tab 一律插在「➕新增」按钮之前，保持 ➕ 常驻队尾
  function railInsert(node) {
    if (railAddBtn && railAddBtn.parentNode === rail) rail.insertBefore(node, railAddBtn);
    else rail.appendChild(node);
  }

  // 标签条按需出现（rail-solo）：只剩收件箱一个标签（无任何内嵌/↪收件箱 Tab）时整条隐藏，
  // 常态桌面与网页版零差异；从抽屉「打开官方网页版」开出第一个标签才浮现（浏览器心智），
  // 关掉最后一个自动消失——入口由页面账号抽屉兜底，不会死路。
  // 收件箱未启用（纯内嵌形态）时标签条是唯一导航，恒显示；Option C（EMBEDDED_ON=false）
  // 仍由 no-embedded 类整条隐藏，不归这里管。
  function syncRailVisibility() {
    const appEl = document.getElementById("app");
    if (!appEl || !EMBEDDED_ON) return;
    const hasTabs = !!rail.querySelector('.rail-item:not(.rail-add):not([data-id="' + INBOX_ID + '"])');
    appEl.classList.toggle("rail-solo", inboxOn && !hasTabs);
    pushShellState();   // 标签增删与状态回推同触发点（内部按内容去重，不会刷噪音）
  }

  // ── 壳 → 页 状态回推（P2）：已打开的内嵌标签清单 + 注入健康 ─────────────────
  // 抽屉「打开官方网页版」入口据此升级为「切换到网页版标签 + 健康提示」——
  // 「这个号的网页版开没开、注入健不健康」终于在账号抽屉一处看全。
  // 传输走 wv.send("chatx-bridge-state")（preload 转 postMessage 进页面世界）；
  // health 沿用 railBadge 的 dot 词表（on/warn/off/idle），与标签条圆点同源同义。
  let _lastShellStateJson = "";
  function collectShellState() {
    const embedded = [];
    const _pcState = window.PlatformCaps;
    ACCOUNTS.forEach((a) => {
      if (!RENDERED.has(a.id)) return;
      const st = deriveInjectState(InjectStatus.byId[a.id]);
      const dot = (typeof railBadge === "function")
        ? railBadge({ level: st.cls, text: st.text }).dot : "idle";
      embedded.push({
        id: a.id, platform: a.platform, label: a.label, health: dot,
        // assist＝assist-only 平台（Messenger/IG/X/Zalo 官方网页仅人工），页内标签条据此挂「人工」角标
        assist: !!(_pcState && _pcState.isAssistOnlyEmbed(a.platform)),
      });
    });
    // enabled＝内嵌能力总闸（Option C）。页面据此决定「打开官方网页版」入口显隐——
    // 壳明说关了还渲染入口，点了只会弹「未启用」＝死入口。Option C 下唯一的推送点
    // 是收件箱 dom-ready 强推（syncRailVisibility/updateRailHealth 都不会跑），
    // 每次页面加载必达，页面不需要超时兜底。
    // strip＝本壳支持「页内标签条接管」（stripReady 握手 + openEmbedded 认 shell_tab_id）。
    // 旧壳无此键 → 页面不渲染 cx-shell-strip，双向前向兼容（老包热更页面零变化）。
    return { enabled: EMBEDDED_ON, strip: true, embedded };
  }
  function pushShellState(force) {
    if (!Inbox.wv) return;
    const state = collectShellState();
    const j = JSON.stringify(state);
    if (!force && j === _lastShellStateJson) return;   // 健康点常态不变，重复推送只是噪音
    _lastShellStateJson = j;
    try { Inbox.wv.send("chatx-bridge-state", state); } catch (_) { /* 收件箱未就绪，下次触发再推 */ }
  }

  // ── 运行时新增内嵌账号（无需改 config.json / 重启）：➕ → 选平台 → 起 webview 内扫码 ──────
  function buildRailAddButton() {
    const btn = document.createElement("div");
    btn.className = "rail-item rail-add";
    btn.title = "新增内嵌账号标签（Telegram / WhatsApp 网页版，在标签内扫码登录）";
    btn.innerHTML = `<span class="ic">${PLUS_TAB_SVG}</span><span>新增</span>`;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleAddMenu(btn);
    });
    rail.appendChild(btn);
    railAddBtn = btn;
  }

  function toggleAddMenu(anchor) {
    if (addMenuEl) return closeAddMenu();
    const menu = document.createElement("div");
    menu.className = "rail-add-menu";
    const embeddables = Object.keys(EMBEDDABLE).filter((p) => EMBEDDABLE[p]);
    const _pcMenu = window.PlatformCaps;
    menu.innerHTML = embeddables
      .map((p) => {
        const name = (TEMPLATES[p] && TEMPLATES[p].name) || p;
        const hint = (_pcMenu && _pcMenu.assistOnlyMenuHint(p)) || "";
        return `<button data-plat="${p}"><span class="ic">${platIconHtml(p, 16)}</span>${name}${hint}</button>`;
      })
      .join("");
    menu.addEventListener("click", (e) => {
      const b = e.target.closest("[data-plat]");
      if (!b) return;
      addEmbeddedAccount(b.getAttribute("data-plat"));
      closeAddMenu();
    });
    document.body.appendChild(menu);
    // 标签栏在左侧竖排 → 菜单贴锚点右侧弹出，底部不够时向上收
    const r = anchor.getBoundingClientRect();
    menu.style.left = Math.round(r.right + 6) + "px";
    const mh = menu.offsetHeight || 0;
    const top = Math.min(Math.round(r.top), Math.max(8, window.innerHeight - mh - 8));
    menu.style.top = top + "px";
    addMenuEl = menu;
    // 下一拍起监听全局点击关菜单（避免本次 ➕ 点击立即触发；关闭时显式移除，防监听堆积）
    setTimeout(() => document.addEventListener("click", closeAddMenu), 0);
  }
  function closeAddMenu() {
    if (!addMenuEl) return;
    addMenuEl.remove();
    addMenuEl = null;
    document.removeEventListener("click", closeAddMenu);
  }

  function nextLabel(platform) {
    const base = (TEMPLATES[platform] && TEMPLATES[platform].name) || platform;
    const n = ACCOUNTS.filter((a) => a.platform === platform).length + 1;
    return `${base} 账号${n}`;
  }

  async function addEmbeddedAccount(platform) {
    if (!isEmbeddable(platform)) return flash("该平台无可内嵌网页版");
    const id = `auto-${platform}-${Date.now().toString(36)}`;
    const acc = buildRuntimeAccount({ id, platform, label: nextLabel(platform), template: TEMPLATES[platform] });
    if (!acc) return flash("无法新增：缺少该平台网页地址");
    if (isWhatsappPlatform(platform)) await ensureWhatsappSessionUa(acc);
    ACCOUNTS.push(acc);
    if (!addAccountTab(acc, { active: true })) return flash("新增失败");
    persistRuntimeAccounts();
    activate(acc.id);
    flash(`已新增内嵌标签：${acc.label}（请在标签内扫码登录）`);
    return acc; // 双面板融合 P0：调用方（openEmbedded 深链）需要新标签的账号对象做会话定位
  }

  function removeEmbeddedAccount(id) {
    const idx = ACCOUNTS.findIndex((a) => a.id === id);
    if (idx < 0) return;
    const itemEl = document.querySelector('.rail-item[data-id="' + id + '"]');
    const wasActive = itemEl && itemEl.classList.contains("active");
    ACCOUNTS.splice(idx, 1);
    delete ACCOUNT_BY_ID[id];
    RENDERED.delete(id);
    delete InjectStatus.byId[id];
    if (itemEl) itemEl.remove();
    const wv = document.querySelector('#webviews webview[data-id="' + id + '"]');
    if (wv) wv.remove();
    persistRuntimeAccounts();
    if (wasActive) {
      const fb = inboxOn ? INBOX_ID : ((ACCOUNTS.find((a) => isEmbeddable(a.platform)) || {}).id || INBOX_ID);
      activate(fb);
    }
    syncRailVisibility();
    flash("已移除内嵌标签");
  }

  // ── 收件箱页 → 壳 反向桥（inbox-preload.js 经 sendToHost 抵达）──────────────
  // 双栏融合后「打开官方网页版」入口下沉到页面账号抽屉（网页版是账号的一种打开方式，
  // 不再是与收件箱并列的一级导航）；页面按钮点击经此桥驱动壳侧标签。
  //   cmd=openEmbedded：已有该平台标签→切过去；没有→按 ➕新增 同一条路径现建
  //                     （UA 伪装 / persist 分区 / 注入 preload 全复用）。
  //   cmd=badge：收件箱未读总数 → 收件箱标签徽标（口径单源=页面聚合逻辑）。
  function onShellBridge(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.cmd === "badge") { updateInboxBadge(msg.unread); return; }
    // stripReady＝页面已渲染 cx-shell-strip（P2 握手）：工作台激活期间壳竖栏让位。
    // 只在收到本报文后才藏 rail——页面加载失败/旧模板/落在 /login 都不会发，竖栏兜底。
    if (msg.cmd === "stripReady") {
      _pageStripLive = true;
      syncPageStripTakeover();
      return;
    }
    if (msg.cmd === "openEmbedded") {
      const plat = String(msg.platform || "").toLowerCase();
      // 双面板融合 P0（2026-08-13）：opts 从「前向兼容占位」升级为深链载荷
      // {thread, account_id}——工作台会话头「原生页打开」带会话定位。旧页面
      // 不带 opts / 旧壳收到 opts 都按「仅切标签」降级，桥契约双向前向兼容。
      const opts = (msg.opts && typeof msg.opts === "object") ? msg.opts : {};
      if (!EMBEDDED_ON) {
        flash("内嵌官方网页版未启用（config.embedded_official_pages）");
        return;
      }
      // shell_tab_id＝页内标签条精确点名某个壳标签（chatx-bridge-state.embedded[].id 原样回传），
      // 优先于按平台猜首个——同平台多号时点谁切谁。查无此 id（标签刚被移除）回落平台匹配。
      const tabId = String(opts.shell_tab_id || "");
      if (tabId && RENDERED.has(tabId) && ACCOUNT_BY_ID[tabId]) {
        activate(tabId);
        locateEmbeddedThread(tabId, ACCOUNT_BY_ID[tabId].platform, opts);
        return;
      }
      if (!isEmbeddable(plat)) { flash("该平台无可内嵌的官方网页版"); return; }
      // 账号选择＝该平台首个已渲染标签（桌面标签与后端账号无强映射，P0 语义；
      // 多标签同平台时若需精确映射，后续在账号配置补 backend_account_id 再升级）。
      const existing = ACCOUNTS.find((a) => a.platform === plat && RENDERED.has(a.id));
      if (existing) {
        activate(existing.id);
        locateEmbeddedThread(existing.id, plat, opts);
        flash(`已切到网页版标签：${existing.label}`);
        return;
      }
      addEmbeddedAccount(plat).then((acc) => {
        if (acc && acc.id) locateEmbeddedThread(acc.id, plat, opts);
      }).catch(() => {});
    }
  }

  // 深链定位：切到/建出标签后，把 webview 导航到该平台的会话直达 URL
  // （thread-url.js 纯函数；messenger=messenger.com/t/<id>）。不支持 URL 路由的
  // 平台 threadUrl 返回 "" → 仅切标签（旧行为）。webview 的 src 赋值即触发导航；
  // 新建标签的 webview 由 addAccountTab 同步建出，改 src ＝以会话页为首个导航。
  // 定位失败绝不影响「切标签」本身（try/catch 吞掉，深链是增强不是前提）。
  function locateEmbeddedThread(accountId, platform, opts) {
    try {
      const th = String((opts && opts.thread) || "").trim();
      if (!th || typeof threadUrl !== "function") return;
      const url = threadUrl(platform, th);
      if (!url) return;
      const wv = document.querySelector('#webviews webview[data-id="' + accountId + '"]');
      if (wv) wv.src = url;
    } catch (_) { /* 深链失败不影响切标签 */ }
  }
  function updateInboxBadge(n) {
    const b = document.getElementById("rail-inbox-badge");
    if (!b) return;
    const v = Number(n) || 0;
    b.hidden = v <= 0;
    b.textContent = v > 99 ? "99+" : String(v);
  }

  // Option C：内嵌官方网页关闭时，桌面只剩统一收件箱（rail 由 #app.no-embedded 隐藏），
  // 不渲染任何内嵌平台 Tab / webview / ➕新增，与网页 /workspace 完全一致。
  if (EMBEDDED_ON) {
    // 分组标题「官方平台 · 人工」：把常驻首标签（人工操作台）与账号标签在竖栏内分区。
    // 只剩收件箱时整条 rail 由 rail-solo 隐藏，故无需单独管理本标题显隐。
    const grp = document.createElement("div");
    grp.className = "rail-group-label";
    grp.textContent = "官方平台 · 人工";
    grp.title = "官方网页版账号：人工聊天/翻译辅助；全自动请用「人工操作台」";
    rail.appendChild(grp);
    buildRailAddButton();
    ACCOUNTS.forEach((a, idx) => {
      if (!isEmbeddable(a.platform)) return addViaInboxItem(a);
      // 统一收件箱开启时它占首屏，内嵌平台一律非激活；未开启时回退老行为（首个可内嵌账号激活）
      addAccountTab(a, { active: !inboxOn && idx === firstEmbeddableIdx });
    });
    syncRailVisibility();   // 零内嵌账号的初始态：标签条按需隐藏（与网页版零差异）
  }

  // 启动首屏对齐：收件箱是首屏时必须走一遍 activate()——buildInboxTab 只置 active class，
  // 而「隐藏桌面 #copilot」的逻辑只活在 activate() 里。漏掉这步 = 开机即「两个业务助手」
  // （页面自带右栏 + 桌面空壳侧栏并排；Option C 默认无 rail 可点，activate 永远不会被触发，
  // 双栏成为常态——2026-08-03 安装版实锤）。
  if (inboxOn) activate(INBOX_ID);

  initCopilot();
  if (inboxOn) {
    const ob = document.getElementById("cp-open-inbox");
    if (ob) ob.hidden = false; // 内嵌平台 Tab 右栏：一键在统一收件箱打开同会话
  }
  if (EMBEDDED_ON) startOutboundPoll(); // D4b：受控出站轮询（仅内嵌模式）
})();

// ── 业务右栏逻辑 ─────────────────────────────────────────────────────────────
// （2026-08-15）「全自动托管」HostBridge autopilot 已整体下线：与收件箱「🚀 全自动」档
// （auto_ai + 受控出站队列 D4b）功能重复，且绕过服务端 send-gate/kill-switch/预算闸门，
// 两开同会话必双发。全自动唯一入口＝统一收件箱的会话自动化档位。
const Copilot = {
  ctx: null, // {platform, chat_key, name, messages, webview}
  tplLoaded: false,
  activeTab: "reply", // reply | customer | tools
  chatActive: false,
};

// 与网页 unified_inbox 共用 sidebar-chrome（shared/copilot/sidebar-chrome.js）
const _sc = () => (window.CopilotShared && window.CopilotShared.sidebarChrome) || {};
let _desktopTabBadges = null;

// 可折叠卡片：与网页 unified_inbox 同源（sidebar-chrome.createCardController）。
// 默认折叠集（draft/profile 默认展开）；组件 id → 所属卡（懒取数按卡展开态决定是否喂 context）。
const _CP_CARD_DEF_COLLAPSED = { voice: 1, kb: 1, tpl: 1, relstage: 1, collab: 1, chain: 1, analysis: 1 };
const _CP_CARD_OF = { "cp-relstage": "relstage", "cp-collab": "collab", "cp-chain": "chain", "cp-voice": "voice" };
let _cpCardCtrl = null;
function _cpCardCollapsed(name) {
  return _cpCardCtrl ? _cpCardCtrl.isCollapsed(name) : !!_CP_CARD_DEF_COLLAPSED[name];
}
// 展开某卡：把折叠期暂存的会话上下文补喂给该卡组件（触发取数），与网页 _cpOnCardExpand 同口径
function _cpOnCardExpand(cardName) {
  Object.keys(_CP_CARD_OF).forEach((id) => {
    if (_CP_CARD_OF[id] !== cardName) return;
    const el = $(id);
    if (el && el._pendingCtx) { const c = el._pendingCtx; el._pendingCtx = null; el.context = c; }
  });
}
// 卡组件 context：折叠则暂存 _pendingCtx（懒加载），展开则立即喂。
// 无控制器（共享模块缺失）时回落为直接喂 context，保持旧行为不阻断。
function _feedCardComponent(id, ctx) {
  const el = $(id);
  if (!el) return;
  const cardName = _CP_CARD_OF[id];
  if (_cpCardCtrl && cardName && _cpCardCtrl.isCollapsed(cardName)) el._pendingCtx = ctx;
  else el.context = ctx;
}
// 卡头状态 pill：组件 cp-data-loaded → 折叠态也能一眼读懂面板内容（复用共享 pillMetaFromCpLoaded）
const _CP_PILL_BY_PANEL = { "cp-draft": "cpp-draft", "cp-relstage": "cpp-relstage", "cp-collab": "cpp-collab", "cp-chain": "cpp-chain" };
const _CP_PILL_TONES = ["pill-accent", "pill-ok", "pill-warn", "pill-danger"];
function _deskPillT(key, vars) {
  const m = {
    "inbox.pill.guard_high": "高风险", "inbox.pill.guard_medium": "中风险", "inbox.pill.draft_ready": "已生成",
    "inbox.pill.chain_failed": "{n} 失败", "inbox.pill.chain_running": "{n} 运行中",
  };
  let s = m[key] || key;
  if (vars) Object.keys(vars).forEach((p) => { s = s.split("{" + p + "}").join(String(vars[p])); });
  return s;
}
function _setCardPill(pillId, text, tone) {
  const el = $(pillId);
  if (!el) return;
  el.textContent = text || "";
  _CP_PILL_TONES.forEach((c) => el.classList.remove(c));
  if (text && tone) el.classList.add("pill-" + tone);
}
function _updateCardPill(det) {
  const pillId = _CP_PILL_BY_PANEL[det && det.panelId];
  if (!pillId) return;
  const sc = _sc();
  if (!sc.pillMetaFromCpLoaded) return;
  const m = sc.pillMetaFromCpLoaded(det, { t: _deskPillT, tf: _deskPillT });
  _setCardPill(pillId, m.text, m.tone);
}

// 右栏分区按 tab 显隐（仅在有会话时）。把"一长条 5 段"改为分组,避免滚到底。
function renderSections() {
  document.querySelectorAll(".cp-sec[data-tab]").forEach((sec) => {
    sec.hidden = !Copilot.chatActive || sec.dataset.tab !== Copilot.activeTab;
  });
}

function setTab(tab) {
  const sc = _sc();
  Copilot.activeTab = sc.normalizeTab ? sc.normalizeTab(tab) : tab;
  document.querySelectorAll(".cp-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === Copilot.activeTab));
  renderSections();
  if (sc.storeTab) sc.storeTab(Copilot.activeTab);
}

function $(id) {
  return document.getElementById(id);
}

// 副驾空状态渲染成「能力橱窗」：把竞品没有的能力（AI 人设拟稿/受控出站人审/知识库/
// 关系阶段/语音克隆）在第一屏显性化。数据来自 shared/copilot/cp-capabilities.js（单源，浏览器全局纯函数）；
// 内容全是我们自己的静态文案（非用户输入）→ innerHTML 安全。函数/数据缺失静默保留原有兜底文案。
function renderCopilotShowcase() {
  try {
    const el = $("cp-empty");
    if (!el || typeof capabilityShowcase !== "function") return;
    const items = capabilityShowcase() || [];
    if (!items.length) return;
    const head = typeof showcaseHeadline === "function" ? showcaseHeadline() : "";
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const rows = items.map((it) =>
      '<li><span class="cp-sc-dot"></span><div>' +
      '<b>' + esc(it.title) + '</b><span>' + esc(it.desc) + '</span></div></li>'
    ).join("");
    el.innerHTML =
      '<div class="cp-showcase">' +
      (head ? '<div class="cp-sc-hd">' + esc(head) + '</div>' : '') +
      '<ul class="cp-sc-list">' + rows + '</ul>' +
      '<div class="cp-sc-ft">打开一个会话即可开始</div></div>';
  } catch (e) { /* 橱窗只是空状态美化,任何异常都不该影响副驾主链 */ }
}

function initCopilot() {
  $("cp-toggle").addEventListener("click", () => {
    const el = $("copilot");
    el.classList.toggle("collapsed");
    $("cp-toggle").textContent = el.classList.contains("collapsed") ? "⟨" : "⟩";
  });
  document.querySelectorAll(".cp-tab").forEach((b) => {
    b.addEventListener("click", () => setTab(b.dataset.tab));
  });
  const sc = _sc();
  if (sc.createResizeController) {
    sc.createResizeController({
      panel: "copilot",
      handle: "cp-sidebar-resize",
      defaultWidth: 320,
      hideWhenCollapsed: () => $("copilot") && $("copilot").classList.contains("collapsed"),
    }).init();
  }
  if (sc.readStoredTab) Copilot.activeTab = sc.readStoredTab(Copilot.activeTab);
  renderSections();
  document.querySelectorAll(".cp-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === Copilot.activeTab);
  });
  if (sc.bindTabHotkeys) {
    sc.bindTabHotkeys({
      setTab,
      isEnabled: () => Copilot.chatActive,
    });
  }
  // 可折叠卡片：状态记忆 + 展开懒取数 + 卡头图标/pill（与网页 unified_inbox 同源）
  if (sc.createCardController) {
    _cpCardCtrl = sc.createCardController({
      defaultCollapsed: _CP_CARD_DEF_COLLAPSED,
      onExpand: _cpOnCardExpand,
    });
    _cpCardCtrl.bindClicks("copilot");
    _cpCardCtrl.apply();
  }
  renderCopilotShowcase();   // 空状态＝能力橱窗（对标翻译型竞品,显性化我们独有的能力）
  if (sc.decorateCardIcons) sc.decorateCardIcons(document, 14);
  document.addEventListener("cp-data-loaded", (e) => _updateCardPill((e && e.detail) || {}));
  if (sc.initDesktopTabBadges) {
    _desktopTabBadges = sc.initDesktopTabBadges({
      badgeReply: "cp-tab-badge-reply",
      badgeCustomer: "cp-tab-badge-customer",
      badgeTools: "cp-tab-badge-tools",
      snoozedLabel: "搁置中",
      translate: (key, vars) => {
        const m = {
          "inbox.pill.guard_high": "高风险",
          "inbox.pill.guard_medium": "中风险",
          "inbox.pill.chain_failed": "{n} 失败",
          "inbox.pill.chain_running": "{n} 运行中",
        };
        let s = m[key] || key;
        if (vars) Object.keys(vars).forEach((p) => { s = s.split("{" + p + "}").join(String(vars[p])); });
        return s;
      },
    });
  }
  // 关系阶段动作(确认进阶/降级/回暖/对齐)完成后:刷新档案,联动后续可在此扩展
  const relEl = $("cp-relstage");
  if (relEl) {
    relEl.addEventListener("cp-rel-changed", () => {
      loadProfile();
    });
  }
  // 共享组件 cp-fill:回填输入框(桥到 webview composer);cp-action-done:刷新关系阶段
  document.addEventListener("cp-fill", (e) => {
    const text = e && e.detail && e.detail.text;
    if (text) fillComposer(text, false);
  });
  // 共享组件 cp-send:填入并发送(过发送风控闸门)
  document.addEventListener("cp-send", (e) => {
    const text = e && e.detail && e.detail.text;
    if (text) fillComposer(text, true);
  });
  document.addEventListener("cp-action-done", (e) => {
    // 对齐网页宿主 P1-198：执行类动作（任务/标签/升级/注解）必须有可见结果反馈——
    // 此前桌面零反馈，服务端如实报的失败原因（如「会话未关联客户档案」）被静默吞掉，
    // 坐席眼里就是「点了没反应」（2026-08-13 198 实录）。ok→轻提示；fail→显示原因。
    const d = (e && e.detail) || {};
    if (d.ok) flash("已执行 ✓");
    else flash(d.error ? String(d.error) : "执行失败，请稍后重试");
    shellBeacon(d.ok ? "cpshell_exec_ok" : "cpshell_exec_fail");
    const rel = $("cp-relstage");
    if (rel) rel.refresh();
    const chain = $("cp-chain");
    if (chain) chain.refresh();
  });
  $("cp-analyze-btn").addEventListener("click", runAnalyze);
  $("cp-kb-btn").addEventListener("click", runKbSearch);
  $("cp-kb-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runKbSearch();
  });
  // <cp-draft> 钉绑人设 / 改回复语言后,同步给 webview 浮钮(保持原生「智能回复」一致)
  document.addEventListener("cp-persona-pinned", () => pushPersonaToWebview());
  document.addEventListener("cp-lang-changed", () => pushReplyLangToWebview());
  setupAccountsPanel();
  setupIframeMode();
  const openInboxBtn = $("cp-open-inbox");
  if (openInboxBtn) openInboxBtn.addEventListener("click", () => openInInbox());
  const assistInboxBtn = document.getElementById("assist-only-inbox-btn");
  if (assistInboxBtn) {
    assistInboxBtn.addEventListener("click", () => openInInbox({ fromAssistBanner: true }));
  }
  const cpVoice = $("cp-voice");
  if (cpVoice && window.CopilotShared) cpVoice.client = window.CopilotShared.createCopilotClient();
  const cpDraft = $("cp-draft");
  if (cpDraft && window.CopilotShared) {
    cpDraft.client = window.CopilotShared.createCopilotClient();
    // 桌面会话是 webview 实时消息(未必落后端 inbox),注入实时上下文供 <cp-draft> 生成
    cpDraft._messagesProvider = async () => { await ensureFullThread(); return contextMessages(); };
  }
}

// ── 会话深链：内嵌平台 Tab 当前会话 → 切到统一收件箱并打开同一会话 ──────────
function openInInbox(opts) {
  opts = opts || {};
  const c = Copilot.ctx;
  if (!Inbox.wv || !Inbox.activate) { flash("人工操作台（统一收件箱）未启用"); return; }
  // 诚实条 CTA：无会话也可切到收件箱（Messenger 全自动入口）
  if (!c || !c.chat_key) {
    Inbox.activate(INBOX_ID);
    flash(opts.fromAssistBanner
      ? "已切到人工操作台 — Messenger 全自动需在此完成服务器登录"
      : "已切到人工操作台");
    return;
  }
  Inbox.activate(INBOX_ID);
  const payload = {
    platform: c.platform,
    account_id: currentAccountId(c),
    chat_key: c.chat_key,
    name: c.name || "",
  };
  const js =
    "window.__desktopOpenConversation && window.__desktopOpenConversation(" +
    JSON.stringify(payload) + ");";
  deliverToInbox(js);
  flash(opts.fromAssistBanner
    ? "已在人工操作台打开 — 全自动请确认账号已服务器登录"
    : "已在人工操作台打开 📥");
}

// 收件箱可能仍在加载/登录：轮询到 ready（约 12s）再投递 executeJavaScript
function deliverToInbox(js, tries) {
  tries = tries == null ? 48 : tries;
  if (Inbox.phase === "ready" && Inbox.wv) {
    Inbox.wv.executeJavaScript(js).catch(() => {});
    return;
  }
  if (tries <= 0) { flash("收件箱尚未就绪，请稍后重试"); return; }
  setTimeout(() => deliverToInbox(js, tries - 1), 250);
}

// 账号管理面板（全局，不依赖会话）：👥 切换显隐，首次打开挂 client 触发加载
function setupAccountsPanel() {
  const toggle = $("cp-accounts-toggle");
  const panel = $("cp-accounts-panel");
  const el = $("cp-accounts");
  if (!toggle || !panel || !el) return;
  toggle.addEventListener("click", () => {
    const show = panel.hidden;
    panel.hidden = !show;
    if (show) {
      if (!el.client) el.client = copilotClient();
      else el.reload();
    }
  });
}

// ── 灰度:统一前端 App(iframe 加载 /copilot/app.html,与网页同源同款) ──────────
function setupIframeMode() {
  const btn = $("cp-mode-toggle");
  // 统一前端 App 默认开启（与网页同源同款）；显式存 "0" 才回退到原生 aside（保留兜底/对照）。
  // 一次性迁移到新基线：旧会话可能持久化过 cp_use_iframe=0，这里以「默认开」覆盖一次，之后仍尊重用户手动 🧪 切换。
  try {
    if (localStorage.getItem("cp_iframe_default_v2") !== "1") {
      localStorage.setItem("cp_iframe_default_v2", "1");
      localStorage.setItem("cp_use_iframe", "1");
    }
    Copilot.useIframe = localStorage.getItem("cp_use_iframe") !== "0";
  } catch (e) { Copilot.useIframe = true; }
  if (btn) {
    btn.addEventListener("click", toggleIframeMode);
    btn.classList.toggle("active", Copilot.useIframe);
  }
  // 接住 iframe 内统一 App 的回吐:就绪/回填/发送
  window.addEventListener("message", onFrameMessage);
  if (Copilot.useIframe) enableIframe();
}

// 壳层 UI 埋点（fire-and-forget，失败静默）：cpshell_* 前缀供「原生 aside 退役」决策读数
// （周审 /api/admin/ui-event-trend?prefix=cpshell_：手动切回≈0 且回退仅现于重启窗 → 可删原生）
function shellBeacon(action) {
  try {
    if (window.shell && typeof window.shell.uiEvent === "function") {
      window.shell.uiEvent({ page: "desktop-shell", action: String(action || "") });
    }
  } catch (e) { /* 埋点绝不影响壳 */ }
}

function toggleIframeMode() {
  Copilot._autoFellBack = false;   // 用户手动切换＝明示意愿，看门狗自动恢复不再介入
  Copilot.useIframe = !Copilot.useIframe;
  shellBeacon(Copilot.useIframe ? "cpshell_manual_iframe" : "cpshell_manual_native");
  try { localStorage.setItem("cp_use_iframe", Copilot.useIframe ? "1" : "0"); } catch (e) {}
  const btn = $("cp-mode-toggle");
  if (btn) btn.classList.toggle("active", Copilot.useIframe);
  // 手动切回原生＝明示意愿：停掉后台就绪轮询（否则轮询会替用户切回 iframe）
  if (Copilot.useIframe) enableIframe(); else { clearFrameRetry(); disableIframe(); }
}

async function frameBackend() {
  if (!Copilot._backend) {
    const cfg = await window.shell.getConfig();
    Copilot._backend = (cfg && cfg.backend) || {};
  }
  return Copilot._backend;
}

// 后端源(用于 postMessage targetOrigin 与来源校验);base_url 形如 http://127.0.0.1:8000
function frameOrigin(baseUrl) {
  try { return new URL(baseUrl).origin; } catch (e) { return ""; }
}

async function enableIframe() {
  document.getElementById("copilot").classList.add("iframe-mode");
  const frame = $("cp-appframe");
  const b = await frameBackend();
  const base = b.base_url || "http://127.0.0.1:18799";
  Copilot._frameOrigin = frameOrigin(base);
  // token 放 hash(不进 server access log);主题镜像宿主壳 data-cp-theme(桌面为深色专属，
  // 将来若壳可切换，iframe 自动跟随)，使统一 App 副驾与深色壳一致，消除 iframe 模式「一黑一白」
  const hostTheme = document.documentElement.getAttribute("data-cp-theme") === "light" ? "light" : "dark";
  const src = `${base}/copilot/app.html?theme=${hostTheme}#token=${encodeURIComponent(b.token || "")}`;
  Copilot._frameSrc = src;
  console.log("[iframe] enable origin=" + Copilot._frameOrigin + " src=" + src);
  frame.onload = function () { console.log("[iframe] onload fired"); };
  // 已就绪且地址未变（手动切换回来 / lateReady 自愈恢复）→ 沿用现成 iframe，只补喂上下文
  if (Copilot._frameReady && frame.getAttribute("src") === src) {
    feedActiveChat();
    return;
  }
  // 三壳合一 P2（2026-08-13 坐席机实锤）：坐席机后端由壳自拉起，冷启动几十秒；
  // iframe 撞 ERR_CONNECTION_REFUSED 错误页后【永不自愈】——同值 src 不触发重载、
  // 错误页也永远不会 postMessage cp-ready → 旧实现里坐席每次开机都被钉死在原生
  // aside，与网页端长期呈现「两套工具箱」。对齐收件箱 webview 的启动闸门语义：
  // 后端就绪才导航；冷启动窗不再回退原生（旧面板此时同样取不到数据）——面板位
  // 直接给启动叙事占位（spawn 三态文案），后端就绪由轮询接管加载。
  let healthy = false;
  try { const h = await window.shell.backendHealth(); healthy = !!(h && h.ok); } catch (e) {}
  if (healthy) {
    navigateFrame(frame, src);
    // 看门狗只在真正发起过导航后武装：管「后端健康但没有 /copilot/app.html」（旧
    // 后端）这类死面板——12s 没等到 cp-ready → 回退原生 aside。刻意不写
    // localStorage —— 下次启动仍先试统一 App（后端升级后自动恢复），手动 🧪 不受影响。
    if (!Copilot._frameReady) startFrameWatchdog();
  } else {
    setFrameBootOverlay(true, await bootPhaseText());
    scheduleFrameRetry();
    // 冷启动窗读数（经主进程排队通道，后端就绪后补发落库）：
    // 「这台坐席机经历了几次启动闸门等待」= 1.0.24 起舰队遥测的健康主信号
    shellBeacon("cpshell_boot_gate_wait");
  }
  // 已有会话则补喂(未 ready 时缓存,cp-ready 后 flush)
  feedActiveChat();
}

// 冷启动占位：iframe 区盖一层启动叙事（元素在 index.html #cp-appframe-wrap 内，
// 非 iframe-mode 时随 wrap 一起隐藏，无需额外互斥）。
function setFrameBootOverlay(show, phaseText) {
  const ov = $("cp-boot-overlay");
  if (!ov) return;
  ov.hidden = !show;
  if (show && phaseText) {
    const p = $("cp-boot-phase");
    if (p) p.textContent = String(phaseText);
  }
}

// 启动阶段文案：复用收件箱同款 backendSpawnStatus 三态，如实告知在等什么。
async function bootPhaseText() {
  try {
    const st = window.shell.backendSpawnStatus ? await window.shell.backendSpawnStatus() : null;
    const phase = st && st.status;
    if (phase === "failed") {
      return "后台服务启动失败：请重启应用；反复出现请联系运维（日志：logs/backend.log）";
    }
    if (phase === "port-conflict") {
      return "端口被其他程序占用，后台服务无法启动——请联系运维处理";
    }
    if (phase === "starting" || phase === "probing") {
      return "首次启动较慢（约 1 分钟），就绪后自动进入业务面板";
    }
  } catch (e) { /* 拿不到阶段就用缺省文案 */ }
  return "正在连接后台服务…（就绪后自动进入业务面板）";
}

// 强制导航：iframe 对「同值 src」不触发重载——加载失败的错误页会永久驻留，
// 必须先归零到 about:blank 再设真实地址，保证必然发生一次新导航。
function navigateFrame(frame, src) {
  Copilot._frameReady = false;
  try { frame.setAttribute("src", "about:blank"); } catch (e) { /* ignore */ }
  setTimeout(() => { try { frame.setAttribute("src", src); } catch (e) { /* ignore */ } }, 0);
}

// 后端就绪轮询（单实例）：冷启动/后端重启窗内每 2s 探活，就绪即真正加载统一 App；
// cp-ready 到达后由 onFrameMessage 的 lateReady 分支自动从原生回退档切回 iframe 档。
function scheduleFrameRetry() {
  if (Copilot._frameRetryTimer) return;
  Copilot._frameRetryTimer = setInterval(async () => {
    if (Copilot._frameReady) { clearFrameRetry(); setFrameBootOverlay(false); return; }
    let ok = false;
    try { const h = await window.shell.backendHealth(); ok = !!(h && h.ok); } catch (e) {}
    if (!ok) {
      // 冷启动进行中：把 spawn 阶段（启动中/失败/端口冲突）如实刷进占位文案
      if (Copilot.useIframe) setFrameBootOverlay(true, await bootPhaseText());
      return;
    }
    clearFrameRetry();
    const frame = $("cp-appframe");
    if (frame && Copilot._frameSrc) {
      console.log("[iframe] 后端已就绪 → 加载统一业务面板");
      if (Copilot.useIframe) setFrameBootOverlay(true, "正在加载业务面板…");
      navigateFrame(frame, Copilot._frameSrc);
      startFrameWatchdog();
      // 与 boot_gate_wait 配对：等待后成功进入加载＝闸门闭环（差值=卡死量）
      shellBeacon("cpshell_boot_gate_load");
    }
  }, 2000);
}

function clearFrameRetry() {
  if (Copilot._frameRetryTimer) { clearInterval(Copilot._frameRetryTimer); Copilot._frameRetryTimer = null; }
}

function startFrameWatchdog() {
  clearFrameWatchdog();
  Copilot._frameWatchdog = setTimeout(() => {
    Copilot._frameWatchdog = null;
    if (Copilot._frameReady || !Copilot.useIframe) return;
    console.warn("[iframe] cp-ready 超时，自动回退原生副驾（iframe 保留在后台，就绪后自动恢复）");
    shellBeacon("cpshell_iframe_fallback");
    Copilot._autoFellBack = true;   // 标记：是看门狗回退的，不是用户选择——迟到的 cp-ready 可自动恢复
    Copilot.useIframe = false;
    const btn = $("cp-mode-toggle");
    if (btn) btn.classList.remove("active");
    disableIframe();
    flash("统一业务面板加载超时，已回退经典面板（后台就绪后自动恢复）");
    // 后端未就绪（冷启动/重启窗）→ 保持后台轮询，就绪后重载 iframe 走 lateReady 自愈；
    // 后端本就健康却超时（如旧后端没有 /copilot/app.html）→ 维持单次尝试语义，不无限重载。
    (async () => {
      let ok = false;
      try { const h = await window.shell.backendHealth(); ok = !!(h && h.ok); } catch (e) {}
      if (!ok) scheduleFrameRetry();
    })();
  }, 12000);
}

function clearFrameWatchdog() {
  if (Copilot._frameWatchdog) { clearTimeout(Copilot._frameWatchdog); Copilot._frameWatchdog = null; }
}

function disableIframe() {
  clearFrameWatchdog();
  setFrameBootOverlay(false);
  document.getElementById("copilot").classList.remove("iframe-mode");
  // 回到原生模式:按当前会话刷新原生面板
  if (Copilot.ctx && Copilot.chatActive) {
    renderSections();
    loadProfile();
    loadFullThread();
    loadRelStage();
  }
}

function onFrameMessage(e) {
  const msg = e && e.data;
  // 看门狗回退期间仍放行迟到的 cp-ready（iframe 留在后台自愈重载，就绪即自动恢复）
  const lateReady = !!(msg && msg.type === "cp-ready" && Copilot._autoFellBack);
  if (!Copilot.useIframe && !lateReady) return;
  console.log("[iframe] msg origin=" + e.origin + " type=" + (msg && msg.type));
  if (Copilot._frameOrigin && e.origin !== Copilot._frameOrigin) return; // 仅信任后端源
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "cp-ready") {
    Copilot._frameReady = true;
    clearFrameWatchdog();
    setFrameBootOverlay(false);
    if (lateReady && !Copilot.useIframe) {
      // 后端重启窗/冷启动解包期看门狗回退过 → 统一 App 现已真就绪，自动恢复 iframe 档
      shellBeacon("cpshell_iframe_recover");
      Copilot._autoFellBack = false;
      Copilot.useIframe = true;
      const btn = $("cp-mode-toggle");
      if (btn) btn.classList.add("active");
      enableIframe();
      flash("统一业务面板已恢复 ✓");
      return;
    }
    if (Copilot._pendingFrameCtx) postFrameContext(Copilot._pendingFrameCtx);
    return;
  }
  if (msg.type === "cp-fill" && msg.text) { fillComposer(msg.text, false); return; }
  if (msg.type === "cp-send" && msg.text) { sendComposer(msg.text); return; }
  // （cp-autopilot 消息已下线：HostBridge 托管开关与收件箱 auto_ai 档重复，见 Copilot 定义处注释。
  //   旧版 app.html 若仍上吐该消息，此处静默忽略即可。）
  // Path2 assist-only 诚实卡 CTA → 切到统一收件箱（可带当前会话深链）
  if (msg.type === "cp-open-inbox") {
    if (typeof openInInbox === "function") openInInbox({ fromAssistBanner: true });
    return;
  }
}

// 统一 App 已在组件侧过了护栏,这里直接发送(不重复弹确认)
function sendComposer(text) {
  const t = String(text || "").trim();
  if (!t || !Copilot.ctx || !Copilot.ctx.webview) return;
  Copilot.ctx.webview.send("fill-composer", { text: t, send: true });
  flash("已填入并发送 ✓");
}

async function feedActiveChat() {
  const c = Copilot.ctx;
  if (!Copilot.useIframe || !c || !c.chat_key || !window.CopilotShared) return;
  try {
    const acc = currentAccountId(c);
    const cid = window.CopilotShared.conversationId(c.platform, acc, c.chat_key);
    const assist = currentIsAssistOnly();
    const preferInbox = !!(window.PlatformCaps && window.PlatformCaps.prefersInboxAuto(c.platform));
    const ctx = {
      type: "cp-context",
      conversationId: cid,
      chatKey: c.chat_key,
      platform: c.platform,
      accountId: acc,
      // assistOnly：官方网页不接 ingest → conv-ops 诚实态
      caps: { assistOnly: assist, preferInboxAuto: preferInbox },
      customer: {
        platform: c.platform,
        account_id: acc,
        account_label: c.account_label || "",
        chat_key: c.chat_key,
        summary: c.name ? ("会话对象：" + c.name) : "",
      },
    };
    if (Copilot._frameReady) postFrameContext(ctx); else Copilot._pendingFrameCtx = ctx;
  } catch (e) { /* ignore */ }
}

// 壳浮钮深链 → 统一 App 指令（iframe 模式）：focus-draft / focus-voice / set-tab
function postFrameCmd(cmd, extra) {
  if (!Copilot.useIframe) return false;
  postFrameContext(Object.assign({ type: "cp-cmd", cmd: cmd }, extra || {}));
  return true;
}

function postFrameContext(ctx) {
  const frame = $("cp-appframe");
  if (frame && frame.contentWindow && Copilot._frameOrigin) {
    frame.contentWindow.postMessage(ctx, Copilot._frameOrigin);
  }
}

// 把当前生效 persona_id 下发给 webview 注入脚本（让浮钮「智能回复」一致）
// 人设来源已迁移：权威=后台会话绑定（由 <cp-draft> 钉绑），回落账号默认
async function pushPersonaToWebview() {
  const c = Copilot.ctx;
  if (!c || !c.webview) return;
  try {
    c.webview.send("set-persona", { persona_id: await selectedPersonaId(c) });
  } catch (e) {}
}

// ── 会话级「回复语言」：把 AI 草稿译成客户语言（现由 <cp-draft> 承载,按会话记忆）──
function selectedReplyLang() {
  // 回复语言现由 <cp-draft> 承载,按会话记忆于 cp_replylang:<conversationId>
  try {
    const c = Copilot.ctx;
    if (!c || !window.CopilotShared) return "";
    const cid = window.CopilotShared.conversationId(c.platform, currentAccountId(c), c.chat_key);
    return (localStorage.getItem("cp_replylang:" + cid) || "").trim();
  } catch (e) { return ""; }
}

async function pushReplyLangToWebview() {
  const c = Copilot.ctx;
  if (!c || !c.webview) return;
  try {
    c.webview.send("set-reply-lang", { target_lang: selectedReplyLang() });
  } catch (e) {}
}

// 对比语言已迁移至 <cp-draft contrast>（按会话记忆于 cp_contrastlang:<conversationId>）

// ── Path2 assist-only（官方网页不接全自动）────────────────────────────────
function currentIsAssistOnly() {
  const c = Copilot.ctx;
  return !!(c && window.PlatformCaps && window.PlatformCaps.isAssistOnlyEmbed(c.platform));
}

function syncAssistOnlyBanner(accountId) {
  const el = document.getElementById("assist-only-banner");
  if (!el) return;
  if (!accountId) { el.hidden = true; return; }
  const a = ACCOUNT_BY_ID[accountId];
  const pc = window.PlatformCaps;
  if (!pc || !a || !pc.isAssistOnlyEmbed(a.platform)) { el.hidden = true; return; }
  const txt = el.querySelector(".aob-text");
  if (txt) txt.textContent = pc.assistOnlyBannerText(a.platform);
  el.hidden = false;
}

// （2026-08-15）「全自动托管」函数族（setupAutopilot / maybeAutopilot / runAutopilotReply
//   / setAutopilotStatus / syncAssistOnlyAutopilotUi）已整体下线，理由见 Copilot 定义处注释。
//   历史 localStorage 键 desktop_autopilot 不再被读取，残值无害。

// 草拟用的 persona_id：下拉选中优先；为空时回落 config 账号绑定
async function selectedPersonaId(c) {
  // 人设现由 <cp-draft> 承载并钉到后台;权威来源=后台会话绑定,回落账号默认
  try {
    const res = await copilotClient().getPersonaBindings();
    const ck = c ? c.chat_key : "";
    const b = res && res.bindings && ck ? res.bindings[ck] : null;
    const pid = b && (b.id || "");
    if (pid) return pid;
  } catch (e) {}
  return personaIdForAccount(c);
}

// 人设选择/来源提示/后台钉绑（applyBackendBinding/pinPersonaToBackend）已迁移至
// 共享组件 <cp-draft persona pin>；钉绑后经 cp-persona-pinned 事件回推 webview 浮钮。

async function fillComposer(text, send) {
  const t = String(text || "").trim();
  if (!t || !Copilot.ctx || !Copilot.ctx.webview) return;
  // 一键「填入并发送」前过发送风控闸门；纯「填入」由人工复核，不拦
  if (send && !(await guardConfirm(t))) return;
  Copilot.ctx.webview.send("fill-composer", { text: t, send: !!send });
  flash(send ? "已填入并发送 ✓" : "已填入 ✓");
}

// ── D4b：受控桌面出站轮询 ────────────────────────────────────────────────────
// 从后端「受控出站队列」取走发给各内嵌账号的全自动回复（已在服务端过 send-gate/
// kill-switch 闸门），分发到对应 webview 的官方页 DOM 发送，再回执。
// 安全：只对**当前已打开对应会话**的账号拉取（chat_key 过滤）——注入 fill-composer 只填
// 当前打开的 composer、不会按 chat_key 导航，故未打开的会话命令留队列等打开，绝不发错聊天。
let _outboundTimer = null;

// fill-composer 回执等待表：token → resolve。注入侧发完/填完经 sendToHost("fill-result")
// 回来，这里把它变成一个可 await 的结果。等不到＝注入没装载/页面在跳转（不是「发失败」，
// 见 FILL_ACK_TIMEOUT_MS 处的说明）。
const _fillWaiters = new Map();
let _fillSeq = 0;
const FILL_ACK_TIMEOUT_MS = 8000; // > 注入侧 150ms 起手 + 1500ms 清空回读，留足余量
function onFillResult(payload) {
  const token = payload && payload.token;
  const w = token ? _fillWaiters.get(token) : null;
  if (!w) return; // 迟到的回执（已超时）：丢弃，本轮已按未确认处理
  _fillWaiters.delete(token);
  clearTimeout(w.timer);
  w.resolve({ received: true, ok: payload.ok === true, reason: String(payload.reason || "") });
}
function sendAndAwaitFill(wv, text) {
  const token = `f${Date.now().toString(36)}${++_fillSeq}`;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      _fillWaiters.delete(token);
      // 超时**不等于失败**：多半是注入层压根没装载（如 1.016/1.017 漏包）或页面正在跳转。
      // 交主进程判成「不 ack」→ 服务端 180s 后回收重取，注入恢复即自愈。
      resolve({ received: false, ok: false, reason: "no_inject_ack", timeout: true });
    }, FILL_ACK_TIMEOUT_MS);
    _fillWaiters.set(token, { resolve, timer });
    try {
      wv.send("fill-composer", { text, send: true, token });
    } catch (e) {
      _fillWaiters.delete(token);
      clearTimeout(timer);
      resolve({ received: false, ok: false, reason: "webview_send_failed", timeout: true });
    }
  });
}

// 注入侧压根不可能发出去的账号（无选择器档案）→ 不拉命令。
// 拉了就是「认领 → 等不到回执 → 不 ack → 180s 回收 → 再认领」的空转，attempts 白涨到
// 上限被判死；不拉则命令留 pending 等注入恢复/换平台，语义与「会话没打开」一致。
// 只拦 unsupported（确定性事实）；warn/bad 仍尝试——失配未必发不出去，宁可试。
function injectSendable(accountId) {
  try {
    const st = deriveInjectState(InjectStatus.byId[accountId]);
    return !st || st.cls !== "bad" || st.code !== "unsupported";
  } catch (e) { return true; } // 判不出来就照旧尝试，绝不因诊断层出问题停掉回复
}

async function pollOutboundOnce() {
  if (!window.shell || !window.shell.outboundPull) return;
  const wvs = document.querySelectorAll('#webviews webview[data-account]');
  for (const wv of wvs) {
    const account_id = wv.dataset.account;
    const platform = wv.dataset.platform;
    if (!account_id || !platform) continue;
    const chat_key = ACTIVE_CHAT_BY_ACCOUNT[account_id];
    if (!chat_key) continue; // 该账号未打开任何会话 → 不拉（命令留队列等打开）
    if (!injectSendable(account_id)) continue;
    let res;
    try {
      res = await window.shell.outboundPull({ platform, account_id, chat_key, limit: 10 });
    } catch (e) { continue; }
    const items = (res && res.items) || [];
    for (const it of items) {
      // ① 拟人节奏：策略在主进程（outbound-pace.js）。不可用则退化成旧的固定间隔，
      //    绝不因为节奏层出问题就不回复客户。
      let plan = { typingMs: 0, waitMs: 600, throttled: false };
      try {
        if (window.shell.pacePlan) plan = await window.shell.pacePlan({ account_id, text: it.text });
      } catch (e) { /* 用兜底 plan */ }
      if (plan && plan.throttled) break; // 本账号已撞每分钟安全阀：剩下的留在队列（不 ack）
      // 与上一条的自然间隔 + 打字耗时。填入会派发 input 事件 → 对端看到「正在输入…」，
      // 故 typing 段先等再填**不**等价于空耗：等的是「像人一样想一下」。
      if (plan && plan.waitMs > 0) await sleep(plan.waitMs);
      if (plan && plan.typingMs > 0) await sleep(plan.typingMs);
      // 等待期间坐席可能切走会话/关掉账号 → 复核，避免把回复填进已切换的聊天
      if (ACTIVE_CHAT_BY_ACCOUNT[account_id] !== chat_key) break;

      // ② 诚实回执：等注入告诉我们「填上了吗/发出去了吗」，由主进程按三档语义决定
      //    ack 成功 / ack 失败 / 不 ack（旧实现无条件 ack 成功，注入一死就集体谎报送达）
      const result = await sendAndAwaitFill(wv, it.text);
      try {
        if (window.shell.outboundReport) {
          await window.shell.outboundReport({ id: it.id, account_id, result });
        } else {
          await window.shell.outboundAck({ id: it.id, ok: result.ok, error: result.reason });
        }
      } catch (e) { /* 回执发不出：服务端回收机制兜底 */ }
      if (!result.ok) break; // 这个账号的发送链当前不通，别把整队命令喂进黑洞
    }
  }
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
// 拟人节奏把一轮派发从「N×600ms」拉长到「N×(间隔+打字)」＝可达数十秒，远超 5s 轮询间隔
// → 必须防重入。不防的话多个循环并行给同一账号发，节奏被有效对折（频控虽仍兜着上限，
// 但「像人」这个目标就没了），且回执 token 表也会无谓膨胀。
let _pollBusy = false;
function startOutboundPoll() {
  if (_outboundTimer) return;
  _outboundTimer = setInterval(() => {
    if (_pollBusy) return;
    _pollBusy = true;
    pollOutboundOnce().catch(() => {}).finally(() => { _pollBusy = false; });
  }, 5000);
}

// 发送前风控：命中支付/密码=high 需确认；优惠/投诉=medium 提醒；AI 口吻提醒。护栏不可用不阻断。
async function guardConfirm(text) {
  let v;
  try {
    v = await window.shell.guardCheck({ text });
  } catch (e) {
    return true;
  }
  if (!v || v.ok === false) return true;
  const terms = (v.hits || []).map((h) => h.term).filter(Boolean).join("、");
  const robo = (v.robotic || []).join("、");
  if (v.risk === "high") {
    return window.confirm(`⚠ 高风险内容，命中敏感词：${terms}\n（支付/密码/账号安全类）\n\n确认仍要直接发送给客户吗？`);
  }
  if (v.risk === "medium") {
    return window.confirm(`提醒：命中需谨慎词：${terms}\n（优惠/投诉/法律类）\n\n确认发送？`);
  }
  if (robo) {
    return window.confirm(`提醒：回复像 AI 口吻（含「${robo}」），可能露馅。\n\n仍要发送吗？`);
  }
  return true;
}

async function copyText(text) {
  const t = String(text || "");
  try {
    if (window.shell.copy) await window.shell.copy(t);
    else await navigator.clipboard.writeText(t);
    flash("已复制 ✓");
  } catch (e) {
    flash("复制失败");
  }
}

let _toastTimer = null;
function flash(msg) {
  let t = $("cp-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "cp-toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), 1400);
}

// 统一操作行：填入 / 填入并发送 / 复制（text 可为字符串或取值函数，便于读编辑框最新值）
function actionRow(text) {
  const get = typeof text === "function" ? text : () => text;
  const row = document.createElement("div");
  row.className = "cp-actions";
  const mk = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = "cp-act " + cls;
    b.textContent = label;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      fn();
    });
    return b;
  };
  row.appendChild(mk("填入", "primary", () => fillComposer(get(), false)));
  row.appendChild(mk("填入并发送", "send", () => fillComposer(get(), true)));
  row.appendChild(mk("复制", "ghost", () => copyText(get())));
  return row;
}

function onActiveChat(payload, webview) {
  if (!payload || !payload.chat_key) return;
  console.log(`[panel] received active-chat: ${payload.chat_key} msgs=${(payload.messages || []).length}`);
  const switched = payload.switched || !Copilot.ctx || Copilot.ctx.chat_key !== payload.chat_key;
  // 账号归属:优先 inject 上报；回落该 webview 的 dataset（renderer 才是权威来源）
  const accountId = payload.account_id || (webview && webview.dataset && webview.dataset.account) || "";
  Copilot.ctx = { ...payload, account_id: accountId, webview };
  // D4b：记录每个内嵌账号「当前打开的会话」——受控出站轮询据此只把命令分发到
  // 已打开对应会话的 webview（注入 fill-composer 不导航，防止发错聊天）。
  if (accountId) ACTIVE_CHAT_BY_ACCOUNT[accountId] = payload.chat_key;

  const _accId = currentAccountId(Copilot.ctx);
  const _assist = !!(window.PlatformCaps && window.PlatformCaps.isAssistOnlyEmbed(payload.platform));
  const _cpCtx = window.CopilotShared ? {
    platform: payload.platform,
    accountId: _accId,
    chatKey: payload.chat_key,
    conversationId: window.CopilotShared.conversationId(payload.platform, _accId, payload.chat_key),
    caps: { assistOnly: _assist },
  } : null;
  const cpVoice = $("cp-voice");
  if (cpVoice && _cpCtx) _feedCardComponent("cp-voice", _cpCtx); // 语音卡默认折叠 → 懒取数
  // 草稿/人设/回复语言/对比语言统一由 <cp-draft persona contrast pin> 承载（两端同源；草稿卡默认展开）
  const cpDraft = $("cp-draft");
  if (cpDraft && _cpCtx && "context" in cpDraft) cpDraft.context = _cpCtx;

  $("cp-empty").hidden = true;
  $("cp-tabs").hidden = false;
  Copilot.chatActive = true;
  renderSections();
  $("cp-title").textContent = payload.name || "业务助手";

  if (switched) {
    $("cp-analyze").hidden = true;
    $("cp-analyze-replies").innerHTML = "";
    $("cp-kb").innerHTML = "";
    $("cp-kb-input").value = "";
    Copilot.fullMessages = null;
    // persona/reply-lang 现由 <cp-draft> 承载（context 已设触发自取）；把当前生效值（后台绑定 + 记忆语言）推给 webview 浮钮
    pushPersonaToWebview();
    pushReplyLangToWebview();
    // iframe 模式下,业务面板数据由统一 App 自取,跳过原生面板加载,避免重复后端调用
    if (!Copilot.useIframe) {
      loadProfile();
      loadFullThread();
      loadRelStage();
    }
  }
  feedActiveChat();
  loadTemplatesOnce();
}

// 拉取该会话在后台 store 的完整历史（P1 已同步），供「分析所有对话」与人设回复使用
async function loadFullThread() {
  const c = Copilot.ctx;
  if (!c || !c.chat_key) return;
  try {
    const res = await window.shell.thread({
      platform: c.platform,
      account_id: currentAccountId(c),
      chat_key: c.chat_key,
    });
    const ms = (res && res.messages) || [];
    if (ms.length) {
      Copilot.fullMessages = ms.map((m) => ({ direction: m.direction || "in", text: m.text || "" }));
    }
  } catch (e) {
    /* 取不到就回落 DOM 消息 */
  }
}

// 确保已拿到 store 完整历史（草拟/分析前调用，避免只用 DOM 的零散几条）
async function ensureFullThread() {
  if (Copilot.fullMessages && Copilot.fullMessages.length) return;
  await loadFullThread();
}

// 兜底清洗单条文本：处理修复前同步进库的历史脏数据
//  - 尾部重复时间戳：「你好哦17:1017:10」「12323:3123:31」
//  - 文件气泡噪声：「txt / / / 新建文本文档(3).txt / 254 B」→「[文件] 新建文本文档(3).txt」
const FILE_EXT = /\.(txt|xlsx?|docx?|pdf|zip|rar|csv|png|jpe?g|gif|mp4|mp3|wav)$/i;
function cleanText(s) {
  if (!s) return "";
  let t = String(s).replace(/\r/g, "");
  // ① 去 webk 图标字体私用区字形（已读勾/状态 tgico，码点 E000–F8FF）——它们夹在正文与
  //    时间戳之间，会让时间戳清洗失效、文件名变乱码
  t = t.replace(/[\uE000-\uF8FF]/g, "");
  // ② 文件气泡噪声：按 / 或换行切段，取含扩展名那段作文件名
  const segs = t.split(/[/\n]/).map((x) => x.replace(/\s+/g, " ").trim());
  if (segs.length > 3) {
    const f = segs.find((x) => FILE_EXT.test(x));
    if (f) return "[文件] " + f;
  }
  // ③ 去重复时间戳（webk 把时间渲染两份：「10:5610:56」）
  t = t.replace(/(\d{1,2}:\d{2})\1+/g, "");
  return t.replace(/[ \t]{2,}/g, " ").replace(/\n{2,}/g, "\n").trim();
}

// 纯数字/纯标点的测试垃圾消息（如 0001 / 22222 / 11 / 123 / 232323），喂给 AI 只会
// 干扰它判断真实意图，应从上下文剔除（不动 store，只在喂 AI 前过滤）
function isJunkText(text) {
  const t = String(text || "").replace(/\s/g, "");
  if (!t) return true;
  if (/^\d+$/.test(t)) return true; // 纯数字
  if (/^[!-/:-@[-`{-~]+$/.test(t)) return true; // 纯 ASCII 标点
  return false;
}

// 清洗整段历史：清噪声 + 丢空行/垃圾 + 折叠连续完全重复
function sanitizeMessages(msgs) {
  const out = [];
  let prev = null;
  for (const m of msgs || []) {
    const text = cleanText(m && m.text);
    if (!text || isJunkText(text)) continue;
    if (prev && prev.text === text && prev.direction === (m && m.direction)) continue;
    const item = Object.assign({}, m, { text });
    out.push(item);
    prev = item;
  }
  return out;
}

// 后台完整历史 + 屏幕最新消息合并去重（store 尾部常滞后于实时 DOM，
// 不合并会让草稿只回到旧消息而非用户刚发的那句），统一过清洗兜底
function contextMessages() {
  const c = Copilot.ctx || {};
  const store = Copilot.fullMessages && Copilot.fullMessages.length ? Copilot.fullMessages.slice() : [];
  const dom = c.messages || [];
  const merged = store.slice();
  const seen = new Set(merged.map((m) => (m.direction || "") + "|" + cleanText(m.text)));
  for (const m of dom) {
    const k = (m.direction || "") + "|" + cleanText(m.text);
    if (!seen.has(k)) {
      merged.push(m);
      seen.add(k);
    }
  }
  return sanitizeMessages(merged.length ? merged : dom);
}

async function runAnalyze() {
  const c = Copilot.ctx;
  if (!c) return;
  const btn = $("cp-analyze-btn");
  const box = $("cp-analyze");
  const reps = $("cp-analyze-replies");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "分析中…";
  reps.innerHTML = "";
  try {
    await ensureFullThread();
    const res = await window.shell.analyze({
      messages: contextMessages(),
      chat: { platform: c.platform, chat_key: c.chat_key, name: c.name },
    });
    const a = (res && res.analysis) || {};
    if (!res || !res.ok) {
      box.hidden = false;
      box.textContent = "分析失败";
      return;
    }
    const chips = [];
    if (a.intent) chips.push(`<span class="tag">意图：${esc(a.intent)}</span>`);
    if (a.sentiment) chips.push(`<span class="tag">情绪：${esc(a.sentiment)}</span>`);
    if (a.detected_lang) chips.push(`<span class="tag">语种：${esc(a.detected_lang)}</span>`);
    (a.risk_signals || []).forEach((r) => {
      const label = typeof r === "string" ? r : r.type || r.label || "";
      if (label) chips.push(`<span class="tag danger">⚠ ${esc(label)}</span>`);
    });
    box.hidden = false;
    box.innerHTML =
      (a.context_summary ? `<div class="cp-summary">${esc(a.context_summary)}</div>` : "") +
      (chips.length ? `<div class="cp-chips">${chips.join("")}</div>` : "");
    // 阶梯式建议话术
    const replies = a.suggested_replies && a.suggested_replies.length
      ? a.suggested_replies
      : (a.suggested_reply ? [{ text: a.suggested_reply }] : []);
    replies.forEach((r) => {
      const txt = typeof r === "string" ? r : r.text || "";
      if (!txt) return;
      const item = document.createElement("div");
      item.className = "cp-item";
      const meta = typeof r === "object" && (r.risk_level || r.rationale)
        ? `<div class="it-title">${esc(r.risk_level || r.rationale || "")}</div>` : "";
      item.innerHTML = `${meta}<div>${esc(txt)}</div>`;
      item.appendChild(actionRow(txt));
      reps.appendChild(item);
    });
  } catch (e) {
    box.hidden = false;
    box.textContent = "分析失败";
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function loadProfile() {
  const c = Copilot.ctx;
  const box = $("cp-profile");
  box.textContent = "加载中…";
  try {
    const res = await window.shell.profile({
      platform: c.platform,
      account_id: currentAccountId(c),
      chat_key: c.chat_key,
    });
    if (!res || !res.ok || !res.profile) {
      box.textContent = "暂无档案（会话刚同步，稍后重试）";
      return;
    }
    const p = res.profile;
    const rel = p.relationship || {};
    const act = p.activity || {};
    const tags = [];
    if (rel.stage) tags.push(`阶段：${rel.stage}`);
    if (p.language) tags.push(`语言：${p.language}`);
    if (rel.intimacy_score != null) tags.push(`亲密度：${rel.intimacy_score}`);
    if (act.message_count != null) tags.push(`消息：${act.message_count}`);
    box.innerHTML =
      `<div style="font-weight:600;margin-bottom:6px">${esc(p.display_name || c.name || c.chat_key)}</div>` +
      tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("");
  } catch (e) {
    box.textContent = "档案加载失败";
  }
}

// 草稿生成/对比语言/send-pick 已迁移至共享组件 <cp-draft persona contrast pin>
// （见 shared/copilot/components/cp-draft.js）。两端同源,经 cp-send/cp-fill 事件落地。

async function runKbSearch() {
  const c = Copilot.ctx;
  const q = $("cp-kb-input").value.trim();
  const list = $("cp-kb");
  if (!q) {
    list.innerHTML = '<div class="cp-hint">输入关键词搜索知识库</div>';
    return;
  }
  list.innerHTML = '<div class="cp-hint">搜索中…</div>';
  try {
    const res = await window.shell.kbSearch({ q, platform: c ? c.platform : "", intent: "" });
    const entries = (res && res.entries) || [];
    if (!entries.length) {
      list.innerHTML = '<div class="cp-hint">无匹配条目</div>';
      return;
    }
    list.innerHTML = "";
    entries.forEach((en) => {
      const ans = en.answer || "";
      const item = document.createElement("div");
      item.className = "cp-item";
      item.innerHTML =
        `<div class="it-title">${esc(en.title || en.category || "条目")}</div>` +
        `<div>${esc(ans.slice(0, 160))}${ans.length > 160 ? "…" : ""}</div>`;
      item.appendChild(actionRow(ans));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<div class="cp-hint">搜索失败</div>';
  }
}

async function loadTemplatesOnce() {
  if (Copilot.tplLoaded) return;
  Copilot.tplLoaded = true;
  const list = $("cp-tpl");
  try {
    const res = await window.shell.templates();
    const tpls = (res && res.templates) || [];
    if (!tpls.length) {
      list.innerHTML = '<div class="cp-hint">暂无快捷回复模板</div>';
      return;
    }
    list.innerHTML = "";
    tpls.slice(0, 30).forEach((t) => {
      const title = (typeof t === "string" ? "" : t.title || t.name || t.label) || "";
      const body = typeof t === "string" ? t : t.text || t.content || t.body || t.template || "";
      if (!body) return;
      const item = document.createElement("div");
      item.className = "cp-item";
      item.innerHTML =
        (title ? `<div class="it-title">${esc(title)}</div>` : "") +
        `<div>${esc(body.slice(0, 120))}${body.length > 120 ? "…" : ""}</div>`;
      item.appendChild(actionRow(body));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<div class="cp-hint">模板加载失败</div>';
  }
}

// account_id：优先用会话上报的归属账号（多账号），回落平台兜底
function currentAccountId(c) {
  if (c && c.account_id) return c.account_id;
  return c && c.platform ? `${c.platform}-desktop` : "";
}
// P1 共享组件:数据适配层单例(桌面 → window.shell IPC)
function copilotClient() {
  if (!Copilot._client && window.CopilotShared) {
    Copilot._client = window.CopilotShared.createCopilotClient();
  }
  return Copilot._client;
}

// 共享会话面板:本地拼 conversation_id(platform:account_id:chat_key)喂给各共享组件
async function loadRelStage() {
  const c = Copilot.ctx;
  if (!c || !c.chat_key || !window.CopilotShared) return;
  try {
    const cid = window.CopilotShared.conversationId(
      c.platform, currentAccountId(c), c.chat_key
    );
    const client = copilotClient();
    ["cp-relstage", "cp-collab", "cp-chain"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.client = client;
      // 懒取数：折叠卡暂存 _pendingCtx，展开时（_cpOnCardExpand）再喂 context 触发取数
      _feedCardComponent(id, { conversationId: cid });
    });
  } catch (e) {
    console.log(`[panel] 会话面板 失败: ${e}`);
  }
}

// 桌面账号绑定的后台人设 id（按当前会话归属账号；留空=用 domain 默认人设「线上陪伴」）
function personaIdForAccount(c) {
  const a = c && c.account_id ? ACCOUNT_BY_ID[c.account_id] : null;
  return (a && a.persona_id) || "";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// ── 壳级通知条（更新就绪 / 官方公告，P0 2026-08-14）──────────────────────────
// 数据与决策全在主进程（update-notify.pickNotice）：这里只消费「当前该显示的一条」。
// 主按钮语义随 kind 变：更新=「立即重启更新」；公告带链接=「查看详情」（读后即销）。
// ✕ 语义：更新=稍后（4h 后再提醒，不打断当下工作）；公告=已读（不再出现）。
(function initShellNotice() {
  const bar = document.getElementById("shell-notice");
  const sh = window.shell;
  if (!bar || !sh || typeof sh.shellNotice !== "function") return;
  const badge = document.getElementById("sn-badge");
  const text = document.getElementById("sn-text");
  const primary = document.getElementById("sn-primary");
  const dismiss = document.getElementById("sn-dismiss");
  let cur = null;

  function render(n) {
    cur = n && n.kind ? n : null;
    if (!cur) { bar.hidden = true; return; }
    bar.className = "sn-" + (cur.tone || "info");
    badge.textContent = cur.badge || "";
    text.textContent = cur.text || "";
    text.title = cur.text || ""; // 超长省略时 hover 看全文
    primary.disabled = false;
    if (cur.kind === "update" && cur.action === "restart") {
      primary.textContent = "立即重启更新";
      primary.hidden = false;
    } else if (cur.kind === "announcement" && cur.action === "open") {
      primary.textContent = "查看详情";
      primary.hidden = false;
    } else {
      primary.hidden = true;
    }
    // 强制升级（forced=版本已低于官方支持线）：横幅不可关闭——「稍后」对必须完成的
    // 事是假选项；主按钮（就绪后的「立即重启更新」）是唯一出口，应用其余功能不锁。
    dismiss.hidden = !!cur.forced;
    bar.hidden = false;
  }

  primary.addEventListener("click", async () => {
    if (!cur) return;
    try {
      if (cur.kind === "update") {
        primary.disabled = true;
        primary.textContent = "正在重启…";
        const r = await sh.updateRestart();
        if (!r || !r.ok) render(await sh.shellNotice()); // 没就绪等罕见态：回读真状态复位按钮
      } else if (cur.kind === "announcement") {
        sh.noticeOpen(cur.id);
        render(await sh.noticeAck({ action: "read", id: cur.id }));
      }
    } catch (e) { console.log(`[notice] 主按钮失败: ${e}`); }
  });
  dismiss.addEventListener("click", async () => {
    if (!cur) return;
    try {
      const args = cur.kind === "update" ? { action: "snooze" } : { action: "read", id: cur.id };
      render(await sh.noticeAck(args));
    } catch (e) { console.log(`[notice] 关闭失败: ${e}`); }
  });

  if (typeof sh.onShellNotice === "function") sh.onShellNotice(render);
  sh.shellNotice().then(render).catch(() => {});
})();
