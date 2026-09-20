"use strict";
/* 两端共享 · 面板基类 CpPanelBase
   抽出 client/context/过期 token/空错态/点击委托/事件派发等共性,
   子类只需实现:
     emptyText()         空态文案(无会话时)
     styles()            子类追加 css(可选)
     async fetchData(ctx)  取数(返回后端 json)
     renderData(d)       渲染(返回 html 字符串)
     onAction(act, el)   处理 [data-act] 点击(可选)
   契约:不在组件内直接操作 composer,需要回填输入框时 emit('cp-fill',{text})
        交由宿主(web→reply-ta / 桌面→inject)落地。
   挂在 window.CopilotShared.CpPanelBase,子类 extends 它。 */
(function (root) {
  const BASE_CSS = `
    :host { display:block; font-size:var(--cp-fs,13px); color:var(--cp-text,#1e293b); }
    /* 统一细滚动条：文档级滚动条样式穿不进 shadow DOM（2026-08-17 人设列表
       「Windows 默认宽白滚动条压深色卡」实锤），基类单点注入 → 全部 cp-* 组件
       的内滚区一次生效。走标准属性(scrollbar-width/color, Chromium121+/FF)，
       thumb 色随主题（--cp-sb-thumb 由 theme-*.css 提供），track 恒透明。 */
    * { scrollbar-width:thin; scrollbar-color:var(--cp-sb-thumb,rgba(100,116,139,.45)) transparent; }
    .wrap { background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
            border-radius:var(--cp-radius,10px); padding:var(--cp-gap,10px); }
    .empty,.err { color:var(--cp-text-tiny,#94a3b8); font-size:var(--cp-fs-sm,12px); }
    .err { color:var(--cp-danger,#dc2626); }
    .card { border-left:3px solid var(--cp-border,#e5e7eb); border-radius:0 var(--cp-radius-sm,6px) var(--cp-radius-sm,6px) 0;
            padding:5px 8px; margin-bottom:var(--cp-gap-xs,4px); background:var(--cp-surface-2,#f8fafc); }
    .card .title { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); color:var(--cp-text,#1e293b); }
    .card .reason,.card .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin:2px 0; }
    .card .body { font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151); line-height:1.45; margin:3px 0; }
    .acts { display:flex; gap:var(--cp-gap-xs,4px); flex-wrap:wrap; margin-top:3px; }
    button { font:inherit; font-size:var(--cp-fs-tiny,11px); cursor:pointer;
             border:1px solid var(--cp-border,#e2e8f0); background:var(--cp-surface,#fff);
             color:var(--cp-text,#1e293b); border-radius:var(--cp-radius-sm,6px); padding:3px 9px; }
    button.primary { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; }
    button.danger { color:var(--cp-danger,#dc2626); border-color:var(--cp-danger,#dc2626); }
    button:disabled { opacity:.5; cursor:default; }
    .stage-badge { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:var(--cp-gap-xs,4px); }`;

  class CpPanelBase extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML =
        `<style>${BASE_CSS}${this.styles() || ""}</style>` +
        `<div class="wrap"><div class="empty">${this._escStatic(this.emptyText())}</div></div>`;
      this._client = null;
      this._ctx = null;
      this._d = null;
      this._reqToken = 0;
      this._renderedCid = "";   // 最近一次真正渲染过的会话（同会话刷新免清屏判据）
      this.shadowRoot.addEventListener("click", (e) => {
        const b = e.target.closest("[data-act]");
        if (b && !b.disabled) this.onAction(b.getAttribute("data-act"), b);
      });
    }

    // —— i18n（共享词典，回退 key）——
    t(key, vars) {
      const f = root.CopilotShared && root.CopilotShared.t;
      return f ? f(key, vars) : key;
    }

    // —— 子类可覆写 ——
    styles() { return ""; }
    emptyText() { return this.t("cp.base.empty"); }
    emptyDataText() { return this.t("cp.base.no_data"); }
    errText() { return this.t("cp.base.err"); }
    async fetchData(_ctx) { throw new Error("fetchData not implemented"); }
    renderData(_d) { return ""; }
    onAction(_act, _el) {}

    // —— 公共属性 ——
    set client(c) { this._client = c; }
    get client() { return this._client; }
    set context(ctx) { this._ctx = ctx; this.refresh(); }
    get context() { return this._ctx; }
    setContext(ctx) { this.context = ctx; }
    get data() { return this._d; }

    // —— 工具 ——
    _wrap() { return this.shadowRoot.querySelector(".wrap"); }
    _render(html) { this._wrap().innerHTML = html; }
    _escStatic(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    }
    esc(s) { return this._escStatic(s); }
    /* 线性 SVG 图标（P2B 统一图标语言）：委托 sidebar-chrome.uiIcon（其内部先委托全站
       /static/ui_icons.js 注册表、缺库回落子集表）；全链缺失返回空串——图标只是增强，
       绝不阻塞渲染（按钮仍有 title/文字兜底）。 */
    ic(name, size) {
      try {
        const sc = root.CopilotShared && root.CopilotShared.sidebarChrome;
        if (sc && typeof sc.uiIcon === "function") return sc.uiIcon(name, size || 13) || "";
      } catch (_e) { /* 静默 */ }
      return "";
    }
    emit(name, detail) {
      this.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail: detail || {} }));
    }

    /* 宿主侧卡头 pill / 懒观测：refresh 各终态统一派发 cp-data-loaded（bubbles+composed）。 */
    _notifyLoaded(state) {
      this.emit("cp-data-loaded", Object.assign({
        panelId: this.id || "",
        data: this._d,
        ok: false,
        empty: false,
        error: false,
      }, state || {}));
    }

    async refresh() {
      const ctx = this._ctx;
      if (!this._client || !ctx || !ctx.conversationId) {
        this._d = null;
        this._renderedCid = "";
        this._render(`<div class="empty">${this._escStatic(this.emptyText())}</div>`);
        this._notifyLoaded({ ok: false, empty: true });
        return;
      }
      // P3(2026-08-05) 同会话刷新不清屏：旧内容原地保留到新数据到位——重试/组件内部
      // 刷新（建目标成功回源、拍反馈失败回源等）不再闪一帧「加载中」把面板打空。
      // **换会话仍先清屏**：旧会话内容顶着新会话在读是串数据，比闪烁更糟；
      // 无旧数据（错误行/空态）时同样给 loading 反馈，别让重试点了像没反应。
      const switching = this._renderedCid !== String(ctx.conversationId);
      if (switching || !this._d) {
        this._render(`<div class="empty">${this._escStatic(this.t("cp.common.loading"))}</div>`);
      }
      this._renderedCid = String(ctx.conversationId);
      const token = ++this._reqToken;
      let d;
      try {
        d = await this.fetchData(ctx);
      } catch (e) {
        if (token === this._reqToken) {
          this._d = null;
          this._render(`<div class="err">${this._escStatic(this.errText())}</div>`);
          this._notifyLoaded({ ok: false, error: true });
        }
        return;
      }
      if (token !== this._reqToken) return; // 已切换会话,丢弃过期响应
      if (!d || d.ok === false) {
        this._d = d || null;
        this._render(`<div class="empty">${this._escStatic(this.emptyDataText())}</div>`);
        this._notifyLoaded({ ok: false, empty: true, data: d || null });
        return;
      }
      this._d = d;
      const html = this.renderData(d);
      this._render(html || `<div class="empty">${this._escStatic(this.emptyDataText())}</div>`);
      this._notifyLoaded({ ok: true, empty: !html });
    }
  }

  root.CopilotShared = Object.assign(root.CopilotShared || {}, { CpPanelBase });
})(typeof window !== "undefined" ? window : this);
