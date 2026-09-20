"use strict";
/* 两端共享 · 单次翻译工具 cp-xlate-tools（工具箱重组 P1，2026-08-17）
   把收件箱「对话翻译」弹层底部的四个单次工具（图片 OCR 翻译 / 语音转写翻译 /
   多线路对照 / 文档整篇翻译）组件化进业务助手工具箱——两端（网页右栏 / 统一 App
   iframe）同一份实现，直连收件箱同一批后端端点（零后端改动）：
     POST /api/unified-inbox/translate-image    {image_b64, target_lang}
     POST /api/unified-inbox/translate-voice    {audio_b64, target_lang}
     POST /api/unified-inbox/translate-compare  {text, target_lang, style, fuse, 会话三元组?}
     POST /api/unified-inbox/translate-document {text, target_lang, style, 会话三元组?}
     POST /api/unified-inbox/translate-document-file {file_b64, filename, target_lang, style,
                                                      stream?, 会话三元组?} (+SSE 进度)
   设计不变量：
   - 弱会话依赖：无会话也可用（目标语「跟随会话」仅在有会话时把 auto 交服务端解析，
     否则回落坐席界面语言）；context 重喂**绝不整块重渲**（防打断进行中的上传/结果
     查看——goal 表单「编辑被外部刷新打断」同款教训），refresh 只同步目标语可用性。
   - 填入输入框走 emit('cp-fill')：网页宿主已监听（填 reply-ta + toast + 采纳埋点），
     App 壳已桥 postMessage → 桌面注入 composer，零新宿主接线。
   - 桌面 token 模式（无 session cookie）：EventSource 带不了 Bearer → 文档上传自动
     退化 stream:false 同步档（无进度条但结果可达）；译后文件下载改 fetch(blob)
     （authHeaders 随宿主补丁/显式头带上），不用裸 <a href> 导航（会 401）。
   - 埋点出口 emit('cp-tool-used',{tool})——网页宿主转 _uiBeacon('cptool_*')；
     App 端暂无 beacon 通道（登记在案的观测债，见 panel-manifest xlate 注）。
   client 需实现 xlateImage/xlateVoice/xlateCompare/xlateDocument/xlateDocumentFile
   （缺失时按钮如实报「客户端未就绪」，不静默）。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  // 与弹层四按钮同一套线性 SVG（自包含，不依赖宿主图标注册表）
  const IC = {
    img: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="9" r="1.6"/><path d="M21 15l-5-5L5 21"/>',
    voice: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="8" y1="22" x2="16" y2="22"/>',
    compare: '<polyline points="17 2 21 6 17 10"/><path d="M3 12V10a4 4 0 0 1 4-4h14"/><polyline points="7 22 3 18 7 14"/><path d="M21 12v2a4 4 0 0 1-4 4H3"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><polyline points="14 3 14 8 19 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/>',
    back: '<polyline points="15 18 9 12 15 6"/>',
  };
  function svg(name, size) {
    const s = size || 14;
    return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true" style="flex-shrink:0;">' + (IC[name] || "") + "</svg>";
  }

  /* Q-21 A（#290，2026-09-12）：目标语只读语言目录单点 GET /api/lang-catalog（34 码含粤语 yue /
     繁体 zh-tw，显示名按 UI 语言，能力位 translate）。下面 15 语短表**只是端点不可达时的兜底**
     ——三单复验「翻译工具没有粤语」的根因就是这份短表与顶栏 34 语目录不同源。 */
  const LANGS_FALLBACK = ["zh", "en", "ja", "ko", "th", "vi", "id", "ms", "es", "pt", "ru", "ar", "fr", "de", "hi"];
  const LANG_NAMES_FALLBACK = {
    zh: { zh: "中文", en: "英语", ja: "日语", ko: "韩语", th: "泰语", vi: "越南语", id: "印尼语",
          ms: "马来语", es: "西班牙语", pt: "葡萄牙语", ru: "俄语", ar: "阿拉伯语", fr: "法语",
          de: "德语", hi: "印地语" },
    en: { zh: "Chinese", en: "English", ja: "Japanese", ko: "Korean", th: "Thai", vi: "Vietnamese",
          id: "Indonesian", ms: "Malay", es: "Spanish", pt: "Portuguese", ru: "Russian",
          ar: "Arabic", fr: "French", de: "German", hi: "Hindi" },
  };
  let LANGS = LANGS_FALLBACK.slice();
  let LANG_NAMES = { zh: Object.assign({}, LANG_NAMES_FALLBACK.zh), en: Object.assign({}, LANG_NAMES_FALLBACK.en) };
  let _catalogLoaded = "";
  let _catalogPromise = null;
  async function _loadLangCatalog(client, uiLang) {
    if (_catalogLoaded === uiLang) return true;
    if (_catalogPromise) return _catalogPromise;
    _catalogPromise = (async () => {
      try {
        let d = null;
        if (client && typeof client.langCatalog === "function") d = await client.langCatalog({ ui_lang: uiLang });
        if (!(d && d.ok && Array.isArray(d.langs) && d.langs.length)) {
          const r = await fetch("/api/lang-catalog?ui_lang=" + encodeURIComponent(uiLang || ""), { credentials: "same-origin" });
          d = r.ok ? await r.json() : null;
        }
        if (d && d.ok && Array.isArray(d.langs) && d.langs.length) {
          const rows = d.langs.filter((l) => l && l.code && !(l.caps && l.caps.translate === false));
          LANGS = rows.map((l) => String(l.code));
          const names = {};
          rows.forEach((l) => { names[l.code] = String(l.name || l.code); });
          LANG_NAMES = { zh: names, en: names };   // name 已按 ui_lang 解析
          _catalogLoaded = uiLang;
        }
      } catch (_e) { /* 兜底短表 */ }
      _catalogPromise = null;
      return true;
    })();
    return _catalogPromise;
  }

  class CpXlateTools extends Base {
    constructor() {
      super();
      this._view = "menu";     // menu | img | voice | compare | doc
      this._target = "auto";  // 目标语选择（auto=跟随会话/界面语言，见 _effTarget）
      this._busy = false;
      this._tok = 0;           // 请求代际：切视图/新请求后旧响应一律丢弃
      this._booted = false;
      this._texts = {};        // 结果文本按 ref 存放（动作按钮不内联长文本）
      this._cands = [];
    }

    styles() {
      return `
      .xt-top { display:flex; align-items:center; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
      .xt-top .lbl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); flex-shrink:0; }
      .xt-top select { font:inherit; font-size:var(--cp-fs-tiny,11px); max-width:150px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:3px 5px; }
      .xt-back { display:inline-flex; align-items:center; gap:3px; }
      .xt-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
      .xt-tile { display:flex; flex-direction:column; align-items:flex-start; gap:3px;
        padding:9px 10px; min-height:56px; text-align:left; cursor:pointer; font:inherit;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:8px;
        background:var(--cp-surface-2,#f8fafc); color:var(--cp-text,#1e293b);
        transition:border-color .12s, background .12s; }
      .xt-tile:hover { border-color:var(--cp-accent,#6366f1);
        background:color-mix(in srgb, var(--cp-accent,#6366f1) 6%, var(--cp-surface-2,#f8fafc)); }
      .xt-tile:active { transform:translateY(1px); }
      .xt-tile b { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
        display:inline-flex; align-items:center; gap:5px; color:inherit; }
      .xt-tile i { font-style:normal; font-size:var(--cp-fs-tiny,11px);
        color:var(--cp-text-dim,#64748b); line-height:1.45; }
      .xt-hint { margin-top:7px; font-size:10px; color:var(--cp-text-tiny,#94a3b8); line-height:1.5; }
      .xt-pick { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
      .xt-status { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:6px 0 0; }
      .xt-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:6px; line-height:1.5; }
      .xt-seg { margin-top:8px; }
      .xt-seg .lbl { display:block; font-size:10px; color:var(--cp-text-tiny,#94a3b8); margin-bottom:2px; }
      .xt-seg .val { font-size:var(--cp-fs-sm,12px); line-height:1.55; color:var(--cp-text,#1e293b);
        background:var(--cp-bg-soft,#f1f5f9); border-radius:var(--cp-radius-sm,6px);
        padding:6px 8px; max-height:180px; overflow:auto; white-space:pre-wrap; word-break:break-word; }
      .xt-seg .val.tr { border-left:3px solid var(--cp-accent,#6366f1); }
      .xt-acts { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
      textarea.xt-src { width:100%; box-sizing:border-box; min-height:64px; resize:vertical;
        font:inherit; font-size:var(--cp-fs-sm,12px); line-height:1.5; padding:6px 8px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .xt-cand { display:block; width:100%; box-sizing:border-box; text-align:left; cursor:pointer;
        font:inherit; margin-top:6px; padding:7px 9px; border:1px solid var(--cp-border,#e2e8f0);
        border-radius:8px; background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .xt-cand:hover { border-color:var(--cp-accent,#6366f1); }
      .xt-cand.fuse { border:1.5px solid var(--cp-accent,#6366f1); }
      .xt-cand .eng { font-size:10px; color:var(--cp-text-dim,#64748b); margin-bottom:2px;
        display:flex; align-items:center; gap:5px; }
      .xt-cand .eng b { color:var(--cp-accent,#6366f1); }
      .xt-cand .txt { font-size:var(--cp-fs-sm,12px); line-height:1.5; white-space:pre-wrap; word-break:break-word; }
      .xt-cand .picked { color:var(--cp-ok,#0f9d75); font-size:10px; font-weight:600; }
      .xt-prog-track { height:6px; margin-top:6px; background:var(--cp-bg-soft,#f1f5f9);
        border-radius:3px; overflow:hidden; }
      .xt-prog-bar { height:100%; width:0%; background:var(--cp-accent,#6366f1); transition:width .2s; }
      input[type=file] { display:none; }`;
    }

    /* ── 生命周期：不走基类「无会话=空态」流——工具卡无会话也可用 ── */
    connectedCallback() {
      if (this._booted) return;
      this._booted = true;
      this._renderView();
    }
    async refresh() {
      // context 重喂只同步「跟随会话」注释是否可解析，绝不重渲打断使用中的面板
      this._notifyLoaded({ ok: true });
      const sel = this.shadowRoot.querySelector('[data-role="target"]');
      if (sel) this._syncAutoLabel(sel);
    }

    _cl() {
      if (!this._client && root.CopilotShared && root.CopilotShared.createCopilotClient) {
        try { this._client = root.CopilotShared.createCopilotClient(); } catch (_e) { /* 保持 null */ }
      }
      return this._client;
    }
    _lang() { return (root.CopilotShared && root.CopilotShared.lang) === "en" ? "en" : "zh"; }
    _hasCtx() { return !!(this._ctx && this._ctx.conversationId); }
    /* 目标语解析：auto + 有会话 + 端点支持会话解析 → 交服务端（'auto'+三元组）；
       其余回落界面语言（图片/语音端点不吃会话三元组，auto 必须在客户端落地）。 */
    _effTarget(convCapable) {
      const v = this._target || "auto";
      if (v !== "auto") return v;
      if (convCapable && this._hasCtx()) return "auto";
      return this._lang() === "en" ? "en" : "zh";
    }
    _convBody(body) {
      const c = this._ctx || {};
      if (body.target_lang === "auto" && c.conversationId) {
        body.platform = c.platform || "";
        body.account_id = c.accountId || "default";
        body.chat_key = c.chatKey || "";
      }
      return body;
    }
    _used(tool) { try { this.emit("cp-tool-used", { tool: tool }); } catch (_e) { /* 埋点绝不阻断 */ } }

    /* ── 渲染 ── */
    _renderView() {
      const v = this._view;
      let html = this._topHtml(v);
      if (v === "menu") html += this._menuHtml();
      else if (v === "img" || v === "voice") html += this._pickHtml(v);
      else if (v === "compare") html += this._compareHtml();
      else if (v === "doc") html += this._docHtml();
      html += '<div class="xt-status" data-role="status"></div><div data-role="out"></div>';
      this._render(html);
      this._wire();
      this._ensureCatalog();
    }
    /* Q-21 A：目录未按当前 UI 语言加载过 → 拉一次，回来后就地重填目标语 select（保留已选）。 */
    _ensureCatalog() {
      const ul = this._lang();
      if (_catalogLoaded === ul) return;
      _loadLangCatalog(this._cl(), ul).then(() => {
        const sel = this.shadowRoot && this.shadowRoot.querySelector('[data-role="target"]');
        if (sel && _catalogLoaded === ul) sel.innerHTML = this._langOptions();
      }).catch(() => {});
    }
    _topHtml(v) {
      const esc = (s) => this.esc(s);
      let h = '<div class="xt-top">';
      if (v !== "menu") {
        h += '<button type="button" class="xt-back" data-act="back">' + svg("back", 12) +
          esc(this.t("cp.xlate.back")) + "</button>";
      }
      h += '<span class="lbl">' + esc(this.t("cp.xlate.target")) + "</span>" +
        '<select data-role="target">' + this._langOptions() + "</select></div>";
      return h;
    }
    _langOptions() {
      const esc = (s) => this.esc(s);
      const names = LANG_NAMES[this._lang()] || LANG_NAMES.zh;
      let h = '<option value="auto"' + (this._target === "auto" ? " selected" : "") + ">" +
        esc(this.t(this._hasCtx() ? "cp.xlate.auto_conv" : "cp.xlate.auto_ui")) + "</option>";
      LANGS.forEach((c) => {
        h += '<option value="' + c + '"' + (this._target === c ? " selected" : "") + ">" +
          esc(names[c] || c) + " · " + c + "</option>";
      });
      return h;
    }
    _syncAutoLabel(sel) {
      const opt = sel && sel.querySelector('option[value="auto"]');
      if (opt) opt.textContent = this.t(this._hasCtx() ? "cp.xlate.auto_conv" : "cp.xlate.auto_ui");
    }
    _menuHtml() {
      const esc = (s) => this.esc(s);
      const tile = (act, ic, k, dk) =>
        '<button type="button" class="xt-tile" data-act="' + act + '"><b>' + svg(ic) +
        esc(this.t(k)) + "</b><i>" + esc(this.t(dk)) + "</i></button>";
      return '<div class="xt-grid">' +
        tile("open-img", "img", "cp.xlate.img", "cp.xlate.img_d") +
        tile("open-voice", "voice", "cp.xlate.voice", "cp.xlate.voice_d") +
        tile("open-compare", "compare", "cp.xlate.compare", "cp.xlate.compare_d") +
        tile("open-doc", "doc", "cp.xlate.doc", "cp.xlate.doc_d") +
        "</div>" +
        '<div class="xt-hint">' + esc(this.t("cp.xlate.hint")) + "</div>";
    }
    _pickHtml(v) {
      const esc = (s) => this.esc(s);
      const img = v === "img";
      return '<div class="xt-pick">' +
        '<button type="button" class="primary" data-act="pick">' + svg(img ? "img" : "voice", 13) + " " +
        esc(this.t(img ? "cp.xlate.pick_img" : "cp.xlate.pick_audio")) + "</button>" +
        '<span class="lbl" style="font-size:10px;color:var(--cp-text-tiny,#94a3b8);">' +
        esc(this.tf("cp.xlate.max_mb", { mb: img ? 8 : 25 })) + "</span></div>" +
        '<input type="file" data-role="file" accept="' + (img ? "image/*" : "audio/*") + '" />';
    }
    _compareHtml() {
      const esc = (s) => this.esc(s);
      return '<textarea class="xt-src" data-role="src" rows="3" placeholder="' +
        esc(this.t("cp.xlate.cmp_ph")) + '"></textarea>' +
        '<div class="xt-acts"><button type="button" class="primary" data-act="run-compare">' +
        esc(this.t("cp.xlate.cmp_run")) + "</button></div>";
    }
    _docHtml() {
      const esc = (s) => this.esc(s);
      return '<textarea class="xt-src" data-role="src" rows="4" placeholder="' +
        esc(this.t("cp.xlate.doc_ph")) + '"></textarea>' +
        '<div class="xt-acts">' +
        '<button type="button" class="primary" data-act="run-doc">' + esc(this.t("cp.xlate.doc_run")) + "</button>" +
        '<button type="button" data-act="pick-txt">' + esc(this.t("cp.xlate.doc_txt")) + "</button>" +
        '<button type="button" data-act="pick-doc">' + esc(this.t("cp.xlate.doc_file")) + "</button>" +
        "</div>" +
        '<input type="file" data-role="txtfile" accept=".txt,text/plain" />' +
        '<input type="file" data-role="docfile" accept=".docx,.xlsx,.pdf" />';
    }
    _wire() {
      const sr = this.shadowRoot;
      const sel = sr.querySelector('[data-role="target"]');
      if (sel) sel.addEventListener("change", () => { this._target = sel.value || "auto"; });
      const fi = sr.querySelector('[data-role="file"]');
      if (fi) fi.addEventListener("change", () => this._onMediaFile(fi));
      const tf = sr.querySelector('[data-role="txtfile"]');
      if (tf) tf.addEventListener("change", () => this._onTxtFile(tf));
      const df = sr.querySelector('[data-role="docfile"]');
      if (df) df.addEventListener("change", () => this._onDocFile(df));
      // 对照：宿主可注入 getSourceText 钩子（网页=读 composer），有内容才预填
      if (this._view === "compare") {
        const ta = sr.querySelector('[data-role="src"]');
        if (ta && !ta.value && typeof this.getSourceText === "function") {
          try { ta.value = String(this.getSourceText() || ""); } catch (_e) { /* 预填是增强 */ }
        }
      }
    }
    _status(msg) {
      const el = this.shadowRoot.querySelector('[data-role="status"]');
      if (el) el.textContent = msg || "";
    }
    _out(html) {
      const el = this.shadowRoot.querySelector('[data-role="out"]');
      if (el) el.innerHTML = html || "";
      return el;
    }
    _err(msg) {
      this._status("");
      this._out('<div class="xt-err">' + this.esc(msg) + "</div>");
    }
    tf(key, vars) { return this.t(key, vars); }

    /* ── 动作分发 ── */
    onAction(act, el) {
      if (act === "back") { this._tok++; this._busy = false; this._view = "menu"; this._renderView(); return; }
      if (act === "open-img" || act === "open-voice" || act === "open-compare" || act === "open-doc") {
        this._tok++; this._busy = false;
        this._view = act.slice(5);
        this._renderView();
        return;
      }
      if (act === "pick") { const fi = this.shadowRoot.querySelector('[data-role="file"]'); if (fi) fi.click(); return; }
      if (act === "pick-txt") { const t = this.shadowRoot.querySelector('[data-role="txtfile"]'); if (t) t.click(); return; }
      if (act === "pick-doc") { const d = this.shadowRoot.querySelector('[data-role="docfile"]'); if (d) d.click(); return; }
      if (act === "run-compare") { this._runCompare(); return; }
      if (act === "run-doc") { this._runDocText(); return; }
      if (act === "fill") {
        const txt = this._actText(el);
        if (txt) { this.emit("cp-fill", { text: txt, source: "xlate" }); this._mark(el, this.t("cp.xlate.filled")); }
        return;
      }
      if (act === "copy") {
        const txt = this._actText(el);
        if (txt) {
          try { navigator.clipboard.writeText(txt); this._mark(el, this.t("cp.xlate.copied")); } catch (_e) { /* 剪贴板不可用静默 */ }
        }
        return;
      }
      if (act === "use-cand") {
        const i = parseInt(el.getAttribute("data-idx") || "-1", 10);
        const c = (this._cands || [])[i];
        if (c && c.text) {
          this.emit("cp-fill", { text: c.text, source: "xlate_cmp" });
          const p = el.querySelector(".picked");
          if (p) p.textContent = "✓ " + this.t("cp.xlate.filled");
        }
        return;
      }
      if (act === "dl") {
        const url = el.getAttribute("data-url") || "";
        const name = el.getAttribute("data-name") || "translated";
        if (url) this._saveUrl(url, name, el);
        return;
      }
    }
    _actText(el) {
      // 动作按钮不内联长文本（转义/体积双坑）：文本存组件字段，按 data-ref 取
      const ref = el.getAttribute("data-ref") || "";
      return String((this._texts || {})[ref] || "");
    }
    _mark(el, label) {
      const old = el.textContent;
      el.textContent = label;
      el.disabled = true;
      setTimeout(() => { el.textContent = old; el.disabled = false; }, 1400);
    }
    _resultActs(ref) {
      const esc = (s) => this.esc(s);
      return '<div class="xt-acts">' +
        '<button type="button" class="primary" data-act="fill" data-ref="' + ref + '">' +
        esc(this.t("cp.xlate.fill")) + "</button>" +
        '<button type="button" data-act="copy" data-ref="' + ref + '">' +
        esc(this.t("cp.xlate.copy")) + "</button></div>";
    }

    /* ── 图片 / 语音 ── */
    async _onMediaFile(input) {
      const f = input && input.files && input.files[0];
      input.value = "";
      if (!f || this._busy) return;
      const img = this._view === "img";
      const maxMb = img ? 8 : 25;
      if (f.size > maxMb * 1024 * 1024) { this._err(this.tf("cp.xlate.too_large", { mb: maxMb })); return; }
      const client = this._cl();
      if (!client || (img ? !client.xlateImage : !client.xlateVoice)) { this._err(this.t("cp.xlate.no_client")); return; }
      const tok = ++this._tok;
      this._busy = true;
      this._out("");
      this._status(this.t(img ? "cp.xlate.recognizing" : "cp.xlate.transcribing"));
      this._used(img ? "img" : "voice");
      try {
        const b64 = await this._readDataUrl(f);
        const target = this._effTarget(false);
        const d = img
          ? await client.xlateImage({ imageB64: b64, targetLang: target })
          : await client.xlateVoice({ audioB64: b64, targetLang: target });
        if (tok !== this._tok) return;
        this._busy = false;
        // M-5 D（#225）：语音上传后回显「已收到 x 秒音频」——落盘核查过了才有这行；
        // 失败也带（转录超时 ≠ 文件没收到，两件事分开说）
        const recv = (!img && d && d.received) ? this._receivedLine(d.received) : "";
        if (!d || !d.ok) {
          this._err(this._failMsg(d));
          if (recv) this._status(recv);
          return;
        }
        const tr = d.translation || {};
        const trText = tr.translated_text || tr.translated || tr.text || "";
        const src = img ? (d.ocr_text || "") : (d.transcript || "");
        const srcTag = !img && d.asr_language ? " · " + d.asr_language : "";
        this._texts = { r1: trText };
        this._status(recv);
        let html = '<div class="xt-seg"><span class="lbl">' +
          this.esc(this.t(img ? "cp.xlate.src_ocr" : "cp.xlate.src_transcript") + srcTag) +
          '</span><div class="val">' + this.esc(src) + "</div></div>";
        if (trText) {
          html += '<div class="xt-seg"><span class="lbl">' +
            this.esc(this.tf("cp.xlate.result_to", { tgt: target })) +
            '</span><div class="val tr">' + this.esc(trText) + "</div>" + this._resultActs("r1") + "</div>";
        }
        this._out(html);
      } catch (_e) {
        if (tok !== this._tok) return;
        this._busy = false;
        this._err(this.t("cp.xlate.net_err"));
      }
    }
    _readDataUrl(f) {
      return new Promise((res, rej) => {
        const rd = new FileReader();
        rd.onload = () => res(String(rd.result || ""));
        rd.onerror = rej;
        rd.readAsDataURL(f);
      });
    }
    _failMsg(d) {
      const r = (d && (d.message || d.error || d.reason)) || "";
      return r ? this.tf("cp.xlate.fail", { r: String(r) }) : this.t("cp.xlate.net_err");
    }
    /* 后端 received={bytes,duration_sec}：算得出秒数说秒，算不出说 KB（都不是就不说） */
    _receivedLine(recv) {
      const sec = Number(recv && recv.duration_sec);
      if (isFinite(sec) && sec > 0) return this.tf("cp.xlate.received_audio", { s: sec.toFixed(1) });
      const kb = Math.round((Number(recv && recv.bytes) || 0) / 1024);
      return kb > 0 ? this.tf("cp.xlate.received_audio_kb", { kb: String(kb) }) : "";
    }

    /* ── 多线路对照 ── */
    async _runCompare() {
      if (this._busy) return;
      const ta = this.shadowRoot.querySelector('[data-role="src"]');
      const text = String((ta && ta.value) || "").trim();
      if (!text) { this._err(this.t("cp.xlate.need_text")); return; }
      const client = this._cl();
      if (!client || !client.xlateCompare) { this._err(this.t("cp.xlate.no_client")); return; }
      const tok = ++this._tok;
      this._busy = true;
      this._out("");
      this._status(this.t("cp.xlate.translating"));
      this._used("compare");
      try {
        const target = this._effTarget(true);
        const d = await client.xlateCompare(this._convBody({ text: text, target_lang: target, style: "chat", fuse: true }));
        if (tok !== this._tok) return;
        this._busy = false;
        this._status("");
        if (!d || !d.ok) { this._err(this._failMsg((d && d.compare && { message: d.compare.error }) || d)); return; }
        const cands = ((d.compare && d.compare.candidates) || []).filter((c) => c.ok && c.translated_text);
        const fu = (d.compare && d.compare.fusion) || null;
        this._cands = [];
        let html = "";
        if (fu && fu.ok && fu.text) {
          this._cands.push({ text: fu.text });
          html += this._candHtml(this._cands.length - 1, this.t("cp.xlate.cmp_fuse"), fu.text, true,
            this.tf("cp.xlate.fuse_from", { n: (fu.engines_used || []).length }));
        }
        cands.forEach((c) => {
          this._cands.push({ text: c.translated_text });
          html += this._candHtml(this._cands.length - 1, String(c.engine || "?"), c.translated_text, false, "");
        });
        if (!this._cands.length) { this._err(this.t("cp.xlate.cmp_none")); return; }
        this._out('<div class="xt-seg"><span class="lbl">' +
          this.esc(this.tf("cp.xlate.result_to", { tgt: d.resolved_target || target })) + "</span></div>" + html);
      } catch (_e) {
        if (tok !== this._tok) return;
        this._busy = false;
        this._err(this.t("cp.xlate.net_err"));
      }
    }
    _candHtml(idx, engine, text, isFuse, sub) {
      const esc = (s) => this.esc(s);
      return '<button type="button" class="xt-cand' + (isFuse ? " fuse" : "") + '" data-act="use-cand" data-idx="' + idx + '">' +
        '<div class="eng">' + (isFuse ? "<b>" + esc(engine) + "</b>" : esc(engine)) +
        (sub ? '<span style="opacity:.7;">' + esc(sub) + "</span>" : "") +
        '<span class="picked" style="margin-left:auto;">' + esc(this.t("cp.xlate.cmp_pick")) + "</span></div>" +
        '<div class="txt">' + esc(text) + "</div></button>";
    }

    /* ── 文档：粘贴文本档 ── */
    async _runDocText() {
      if (this._busy) return;
      const ta = this.shadowRoot.querySelector('[data-role="src"]');
      const text = String((ta && ta.value) || "").trim();
      if (!text) { this._err(this.t("cp.xlate.need_text")); return; }
      const client = this._cl();
      if (!client || !client.xlateDocument) { this._err(this.t("cp.xlate.no_client")); return; }
      const tok = ++this._tok;
      this._busy = true;
      this._out("");
      this._status(this.t("cp.xlate.doc_running"));
      this._used("doc");
      try {
        const target = this._effTarget(true);
        const d = await client.xlateDocument(this._convBody({ text: text, target_lang: target, style: "chat" }));
        if (tok !== this._tok) return;
        this._busy = false;
        if (!d || !d.ok) { this._err(this._failMsg(d)); return; }
        const s = d.stats || {};
        this._status(this.tf("cp.xlate.doc_done", { tr: s.translated || 0, tot: s.total || 0 }));
        const trText = d.translated_text || "";
        this._texts = { r1: trText };
        this._out('<div class="xt-seg"><span class="lbl">' +
          this.esc(this.tf("cp.xlate.result_to", { tgt: d.target_lang || target })) +
          '</span><div class="val tr">' + this.esc(trText) + "</div>" + this._resultActs("r1") + "</div>");
      } catch (_e) {
        if (tok !== this._tok) return;
        this._busy = false;
        this._err(this.t("cp.xlate.net_err"));
      }
    }
    _onTxtFile(input) {
      const f = input && input.files && input.files[0];
      input.value = "";
      if (!f) return;
      if (f.size > 2 * 1024 * 1024) { this._err(this.tf("cp.xlate.too_large", { mb: 2 })); return; }
      const rd = new FileReader();
      rd.onload = () => {
        const ta = this.shadowRoot.querySelector('[data-role="src"]');
        if (ta) ta.value = String(rd.result || "");
      };
      rd.readAsText(f);
    }

    /* ── 文档：docx/xlsx/pdf 上传档（cookie 态走 SSE 进度；token 态退化同步） ── */
    _tokenMode() {
      try {
        const ah = root.CopilotShared && root.CopilotShared.authHeaders && root.CopilotShared.authHeaders();
        return !!(ah && ah.Authorization);
      } catch (_e) { return false; }
    }
    async _onDocFile(input) {
      const f = input && input.files && input.files[0];
      input.value = "";
      if (!f || this._busy) return;
      if (f.size > 10 * 1024 * 1024) { this._err(this.tf("cp.xlate.too_large", { mb: 10 })); return; }
      const client = this._cl();
      if (!client || !client.xlateDocumentFile) { this._err(this.t("cp.xlate.no_client")); return; }
      const tok = ++this._tok;
      this._busy = true;
      this._out("");
      this._status(this.t("cp.xlate.doc_running"));
      this._used("doc_file");
      const stream = typeof EventSource !== "undefined" && !this._tokenMode();
      try {
        const b64 = await this._readDataUrl(f);
        const target = this._effTarget(true);
        const d = await client.xlateDocumentFile(this._convBody({
          file_b64: b64, filename: f.name, target_lang: target, style: "chat", stream: stream,
        }));
        if (tok !== this._tok) return;
        if (!d || !d.ok) { this._busy = false; this._err(this._failMsg(d)); return; }
        if (d.job_id && d.progress_url) { this._docStream(d.progress_url, tok); return; }
        this._busy = false;
        this._docApply(d);
      } catch (_e) {
        if (tok !== this._tok) return;
        this._busy = false;
        this._err(this.t("cp.xlate.net_err"));
      }
    }
    _docStream(url, tok) {
      const out = this._out('<div class="xt-prog-track"><div class="xt-prog-bar" data-role="bar"></div></div>');
      const bar = out ? out.querySelector('[data-role="bar"]') : null;
      let es;
      try { es = new EventSource(url); } catch (_e) { this._busy = false; this._err(this.t("cp.xlate.net_err")); return; }
      es.onmessage = (ev) => {
        if (tok !== this._tok) { try { es.close(); } catch (_e) { /* 已切走 */ } return; }
        let d;
        try { d = JSON.parse(ev.data); } catch (_e) { return; }
        if (d.status === "running") {
          const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
          if (bar) bar.style.width = pct + "%";
          this._status(this.tf("cp.xlate.prog", { done: d.done || 0, tot: d.total || 0 }));
        } else if (d.status === "done") {
          es.close();
          this._busy = false;
          this._docApply(d);
        } else if (d.status === "error") {
          es.close();
          this._busy = false;
          this._err(this._failMsg(d));
        }
      };
      es.onerror = () => {
        try { es.close(); } catch (_e) { /* 双保险 */ }
        if (tok !== this._tok) return;
        this._busy = false;
        this._err(this.t("cp.xlate.prog_interrupted"));
      };
    }
    _docApply(d) {
      const s = d.stats || {};
      this._status(this.tf("cp.xlate.doc_done", { tr: s.translated || 0, tot: s.total || 0 }));
      if (d.kind === "file" && d.download_url) {
        this._out('<div class="xt-seg"><span class="lbl">' + this.esc(d.filename || "") + "</span>" +
          '<div class="xt-acts"><button type="button" class="primary" data-act="dl" data-url="' +
          this.esc(d.download_url) + '" data-name="' + this.esc(d.filename || "translated") + '">' +
          this.esc(this.t("cp.xlate.download")) + "</button></div>" +
          '<div class="xt-hint">' + this.esc(this.t("cp.xlate.dl_once")) + "</div></div>");
        return;
      }
      const txt = d.text || d.translated_text || "";
      this._texts = { r1: txt };
      this._out('<div class="xt-seg"><div class="val tr">' + this.esc(txt) + "</div>" + this._resultActs("r1") + "</div>");
    }
    /* 译后文件取回：fetch(blob) 而非 <a href> 导航——token 宿主（桌面 iframe）无
       session cookie，裸导航必 401；fetch 吃 app.html 的 Authorization 补丁 +
       此处显式再带一次（网页 cookie 态 authHeaders 返回空对象=零变化）。
       注意 token 一次性（取回即删）：失败后不能重试同链接，如实提示重新翻译。 */
    async _saveUrl(url, name, btn) {
      if (btn) btn.disabled = true;
      try {
        const headers = (root.CopilotShared && root.CopilotShared.authHeaders)
          ? root.CopilotShared.authHeaders() : {};
        const r = await fetch(url, { headers: headers, credentials: "same-origin" });
        if (!r.ok) throw new Error("http " + r.status);
        const b = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(b);
        a.download = name || "translated";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { try { URL.revokeObjectURL(a.href); a.remove(); } catch (_e) { /* 清理尽力 */ } }, 0);
        // 一次性令牌（取回即删）：成功后永久禁用本按钮，不走 _mark 的定时恢复
        if (btn) { btn.textContent = this.t("cp.xlate.dl_started"); btn.disabled = true; }
      } catch (_e) {
        if (btn) btn.disabled = false;
        this._err(this.t("cp.xlate.dl_fail"));
      }
    }
  }

  if (!customElements.get("cp-xlate-tools")) customElements.define("cp-xlate-tools", CpXlateTools);
})(typeof window !== "undefined" ? window : this);
