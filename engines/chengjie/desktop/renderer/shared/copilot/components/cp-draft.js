"use strict";
/* 两端共享组件 · AI 回复草稿(<cp-draft>)— 继承 CpPanelBase(重写 refresh,不自动取数)
   自取持久化线程(getHistory by conversation_id)→ 走 /api/desktop/smart-reply 产线
   (SkillManager→意图→策略→KB→人设),返回人设化草稿 + 可选译文。
   关键:不传 persona_id —— 服务端按 chat_key 解析后台人设绑定(cp-persona 钉的),
   两组件经后端单一事实源联动;返回 persona/persona_tier 让徽标说真话。
   "使用"→emit cp-fill(回填由宿主落地)。回复语言按会话本地记忆。
   用法:
     const el = document.createElement('cp-draft');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey, platform };
   client 需实现:getHistory / smartReply */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-draft: CpPanelBase 未加载"); return; }

  // 语种与后台翻译栏保持同一套（顺序/数量一致）；标签经 i18n 词典按 UI 语言显示
  const LANGS = [
    ["", "cp.lang.follow"], ["zh", "cp.lang.zh"], ["en", "cp.lang.en"], ["th", "cp.lang.th"],
    ["vi", "cp.lang.vi"], ["id", "cp.lang.id"], ["ja", "cp.lang.ja"],
    ["ko", "cp.lang.ko"], ["ru", "cp.lang.ru"], ["es", "cp.lang.es"], ["pt", "cp.lang.pt"],
  ];
  const TIER = { conv_override: "cp.draft.tier_conv_override", chat_binding: "cp.draft.tier_chat_binding", account_profile: "cp.draft.tier_account_profile", domain: "cp.draft.tier_domain", default: "cp.draft.tier_default" };

  class CpDraft extends Base {
    constructor() {
      super();
      this._genToken = 0;
      this._pickSeq = 0;
      /* P1-198：生成模式——reply=承接客户最后一条(旧行为)；opener=主动开启新话题
         （无入站消息也可生成，走 /api/desktop/smart-reply {mode:"opener"} 开场产线） */
      this._mode = "reply";
      /* P22：坐席显式指令（目标今日拍 / 画像缺口追问）。进 smart-reply.instruction，
         不写 composer——防误发意图原文。{text, label?, pushLevel?, goalId?} */
      this._directive = null;
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="lang"]');
        if (s) { this._saveLang(s.value); this.emit("cp-lang-changed", { lang: s.value }); }
        const c = e.target.closest('select[data-role="contrast"]');
        if (c) this._saveContrast(c.value);
        const p = e.target.closest('select[data-role="persona"]');
        if (p) { this._pinState = null; this._updatePinState(); }
      });
    }
    emptyText() { return this.t("cp.draft.empty"); }
    styles() {
      return `
      .ctl { display:flex; flex-direction:column; gap:var(--cp-gap-sm,6px); align-items:stretch; }
      .mrow { display:flex; gap:4px; }
      button.mode { flex:1; padding:4px 6px; font-size:var(--cp-fs-tiny,11px); opacity:.78; }
      button.mode.on { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; opacity:1; font-weight:600; }
      select { font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px; width:100%;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      button.gen { width:100%; white-space:nowrap; padding:6px 10px;
                   background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; font-weight:600; }
      button.gen:hover { filter:brightness(1.05); }
      button.primary { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; padding:6px 10px; white-space:nowrap; }
      .slot { margin-top:var(--cp-gap-sm,6px); }
      .draft { background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
               border-radius:var(--cp-radius-sm,6px); padding:7px 9px; }
      .badges { display:flex; gap:var(--cp-gap-xs,4px); flex-wrap:wrap; margin-bottom:4px; }
      .bdg { font-size:var(--cp-fs-tiny,11px); padding:1px 7px; border-radius:99px;
             background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .bdg.intent { background:var(--cp-surface,#eef2ff); color:var(--cp-text-dim,#64748b); }
      .bdg.kb { background:var(--cp-surface-2,#f8fafc); color:var(--cp-text-dim,#64748b); border:1px solid var(--cp-border,#e2e8f0); cursor:help; }
      .bdg.goal { cursor:help; }
      .bdg.goal.on { background:rgba(15,157,117,.14); color:var(--cp-ok,#0f9d75); }
      .bdg.goal.off { background:rgba(217,119,6,.14); color:var(--cp-warn,#92400e); }
      .reply { font-size:var(--cp-fs,13px); color:var(--cp-text,#1e293b); line-height:1.5; white-space:pre-wrap; }
      .tr { margin-top:5px; padding-top:5px; border-top:1px dashed var(--cp-border,#e2e8f0);
            font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#475569); white-space:pre-wrap; }
      .tr .tl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .slot .empty,.slot .err { font-size:var(--cp-fs-sm,12px); }
      .guardbox { margin-top:5px; }
      .guard { font-size:var(--cp-fs-tiny,11px); padding:4px 8px; border-radius:var(--cp-radius-sm,6px);
               display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
      .guard.high { background:rgba(220,38,38,.12); color:var(--cp-danger,#dc2626); }
      .guard.medium { background:rgba(217,119,6,.14); color:var(--cp-warn,#92400e); }
      .guard.low { background:rgba(15,157,117,.12); color:var(--cp-ok,#0f9d75); }
      button.send { background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; }
      button.force { background:var(--cp-danger,#dc2626); color:#fff; border-color:transparent; }
      .prow { display:flex; align-items:center; gap:var(--cp-gap-sm,6px); margin-bottom:var(--cp-gap-sm,6px); }
      .prow label { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); flex:0 0 auto; }
      .prow select { flex:1; min-width:0; }
      button.pin { flex:0 0 auto; padding:5px 8px; }
      button.pin.on { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; }
      .psrc { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:-2px 0 6px; }
      .dirchip { display:flex; align-items:flex-start; gap:6px; margin:0 0 6px;
                 padding:6px 8px; border-radius:var(--cp-radius-sm,6px);
                 background:rgba(79,70,229,.08); border:1px solid rgba(79,70,229,.22);
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-accent,#4f46e5); line-height:1.4; }
      .dirchip .dirbody { flex:1; min-width:0; }
      .dirchip .dirlab { font-weight:600; margin-right:4px; }
      .dirchip .dirtxt { color:var(--cp-text,#1e293b); display:block; margin-top:2px;
                         white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
                         max-width:100%; }
      .dirchip button.dirx { flex:0 0 auto; padding:0 6px; font-size:14px; line-height:1.2;
                             background:transparent; border:0; color:var(--cp-text-dim,#64748b); cursor:pointer; }
      .lblock { border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px); padding:6px 8px; margin-top:6px; }
      .lblock.active { border-color:var(--cp-accent,#4f46e5); box-shadow:0 0 0 1px var(--cp-accent,#4f46e5) inset; }
      .lblock .lhead { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:4px; }
      .lblock textarea { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px); padding:5px 7px;
               resize:vertical; color:var(--cp-text,#1e293b); background:var(--cp-surface,#fff); }`;
    }

    // 重写:不自动取数(避免每次切会话就烧 LLM),只渲染控件 + 空 slot
    async refresh() {
      if (!this._client || !this._ctx || !this._ctx.conversationId) {
        this._render(`<div class="empty">${this.esc(this.emptyText())}</div>`);
        return;
      }
      this._draft = null;
      // 切会话清指令，避免上一条会话的今日拍串到下一条
      this._directive = null;
      const lang = this._loadLang();
      const opts = LANGS.map(([v, k]) =>
        `<option value="${v}"${v === lang ? " selected" : ""}>${this.esc(this.t(k))}</option>`).join("");
      const wantPersona = this.hasAttribute("persona");
      const wantContrast = this.hasAttribute("contrast");
      let personaRow = "";
      if (wantPersona) {
        personaRow =
          `<div class="prow"><label>${this.esc(this.t("cp.draft.persona_label"))}</label>` +
          `<select data-role="persona"><option value="">${this.esc(this.t("cp.draft.persona_default"))}</option></select>` +
          `<button class="pin" data-act="pin" title="${this.esc(this.t("cp.draft.pin_title"))}">📌</button></div>` +
          `<div class="psrc" data-role="psrc"></div>`;
      }
      let contrastRow = "";
      if (wantContrast) {
        const cl = this._loadContrast();
        const copts = LANGS.filter(([v]) => v !== "")
          .map(([v, k]) => `<option value="${v}"${v === cl ? " selected" : ""}>${this.esc(this.t(k))}</option>`).join("");
        contrastRow = `<select data-role="contrast"><option value="">${this.esc(this.t("cp.draft.no_contrast"))}</option>${copts}</select>`;
      }
      const modeRow =
        `<div class="mrow">` +
        `<button class="mode${this._mode !== "opener" ? " on" : ""}" data-act="mode-reply">${this.esc(this.t("cp.draft.mode_reply"))}</button>` +
        `<button class="mode${this._mode === "opener" ? " on" : ""}" data-act="mode-opener" title="${this.esc(this.t("cp.draft.mode_opener_t"))}">${this.esc(this.t("cp.draft.mode_opener"))}</button>` +
        `</div>`;
      this._render(
        personaRow +
        `<div data-role="dirhost"></div>` +
        `<div class="ctl">` + modeRow + `<select data-role="lang">${opts}</select>` +
        contrastRow +
        `<button class="gen" data-act="gen">${this.esc(this.t("cp.draft.gen_btn"))}</button></div>` +
        `<div class="slot"></div>`
      );
      this._paintDirective();
      if (wantPersona) this._loadPersonas();
      // P4-C：无会话级记忆时，回落服务端「默认回复语言」（账号>平台>全局）。不写本地（仅默认，非用户选择）。
      if (!lang) this._applyServerReplyDefault(this._ctx && this._ctx.conversationId);
    }

    // 取服务端默认回复语言并回填选择器（仅当当前仍是同一会话、且用户未手动改/无本地记忆）。best-effort。
    async _applyServerReplyDefault(cid) {
      if (!cid || !this._client || typeof this._client.defaultReplyLang !== "function") return;
      const parts = String(cid).split(":");
      const platform = (this._ctx && this._ctx.platform) || parts[0] || "";
      const accountId = parts.length >= 2 ? parts[1] : "default";
      let r;
      try { r = await this._client.defaultReplyLang({ platform, account_id: accountId }); }
      catch (e) { return; }
      const resolved = (r && r.ok && r.resolved) ? String(r.resolved) : "";
      if (!resolved) return;
      // 会话可能在 await 期间被切走；且若期间已落本地记忆，则尊重之，不覆盖。
      if (!this._ctx || this._ctx.conversationId !== cid || this._loadLang()) return;
      const sel = this.shadowRoot.querySelector('select[data-role="lang"]');
      if (sel && sel.value === "" && LANGS.some(([v]) => v === resolved)) sel.value = resolved;
    }

    _contrastKey() { return "cp_contrastlang:" + (this._ctx && this._ctx.conversationId || ""); }
    _loadContrast() { try { return localStorage.getItem(this._contrastKey()) || ""; } catch (e) { return ""; } }
    _saveContrast(v) { try { if (v) localStorage.setItem(this._contrastKey(), v); else localStorage.removeItem(this._contrastKey()); } catch (e) {} }
    _langLabel(code) { const f = LANGS.find(([v]) => v === code); return f ? this.t(f[1]) : code; }

    async _loadPersonas() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      if (!sel || !this._client) return;
      this._pinState = null;
      let summary = [];
      this._profiles = {};
      try {
        const d = await this._client.listPersonas();
        summary = (d && d.summary) || [];
        this._profiles = (d && d.profiles) || {};
      } catch (e) {}
      let bound = "";
      try {
        const b = await this._client.getPersonaBindings();
        const ck = (this._ctx && this._ctx.chatKey) || "";
        const bd = (b && b.bindings && ck) ? b.bindings[ck] : null;
        bound = bd ? (bd.id || "") : "";
      } catch (e) {}
      /* 会话覆写层优先回显（cp-persona 同源 effective API）：之前只读 legacy 绑定，
         会话覆写钉过的人设换回会话后下拉框显示成「默认」，坐席误以为掉绑再钉一次。 */
      try {
        const cid = String((this._ctx && this._ctx.conversationId) || "");
        if (cid.split(":").length >= 3 && typeof this._client.personaEffective === "function") {
          const eff = await this._client.personaEffective({ conversationId: cid });
          if (eff && eff.ok && eff.conv && eff.conv.id) {
            bound = eff.conv.id;
            this._pinState = { scope: "conversation", suppressed: false };
          }
        }
      } catch (e) {}
      let h = `<option value="">${this.esc(this.t("cp.draft.persona_default"))}</option>`;
      summary.forEach((p) => {
        const id = (p && p.id) || "";
        const nm = p && p.role ? `${p.name} (${p.role})` : ((p && (p.name || p.id)) || id);
        h += `<option value="${this.esc(id)}"${id === bound ? " selected" : ""}>${this.esc(nm)}</option>`;
      });
      sel.innerHTML = h;
      this._updatePinState();
    }

    _updatePinState() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const pin = this.shadowRoot.querySelector("button.pin");
      const src = this.shadowRoot.querySelector('[data-role="psrc"]');
      if (pin && sel) pin.classList.toggle("on", !!sel.value);
      if (!src || !sel) return;
      if (!sel.value) { src.textContent = this.t("cp.draft.unpinned"); return; }
      const st = this._pinState || {};
      if (st.suppressed) src.textContent = this.t("cp.draft.pin_suppressed");
      else if (st.scope === "conversation") src.textContent = this.t("cp.draft.pinned_conv");
      else src.textContent = this.t("cp.draft.pinned");
    }

    async _pinPersona() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const pid = sel ? sel.value : "";
      const ctx = this._ctx || {};
      const chatKey = ctx.chatKey || "";
      const cid = String(ctx.conversationId || "");
      if (!this._client) return;
      const persona = pid ? (this._profiles || {})[pid] : null;
      if (pid && !persona) return;
      /* 会话覆写优先（出站链 resolve_effective_persona 的最高层，压得住账号默认人设）；
         开关关（服务端 400）/ 壳未暴露 IPC → 回落 legacy 旧语义。legacy 绑定在
         「账号配了默认人设」的部署里必被压制（conv_override > account_profile > legacy），
         正是「钉了林若曦、出站还是林小雨」的根因——所以能走覆写就绝不写 legacy。 */
      const canConv = cid.split(":").length >= 3 &&
        typeof this._client.bindConvPersona === "function";
      if (!canConv && !chatKey) return;
      let ok = false, scope = "";
      if (canConv) {
        try {
          const r = pid
            ? await this._client.bindConvPersona({ conversationId: cid, profileId: pid })
            : await this._client.unbindConvPersona({ conversationId: cid });
          if (r && r.ok) { ok = true; scope = "conversation"; }
        } catch (e) { /* 覆写不可用 → 下面回落 legacy */ }
      }
      if (!ok && chatKey) {
        try {
          const r = pid
            ? await this._client.bindPersona({ chatKey, persona })
            : await this._client.unbindPersona({ chatKey });
          if (r && r.ok) { ok = true; scope = "legacy"; }
        } catch (e) { ok = false; }
      }
      /* legacy 档钉完读一次生效真相：被更高层压制就如实警示，别再宣称「全端生效」 */
      let suppressed = false;
      if (ok && scope === "legacy" && pid && cid.split(":").length >= 3 &&
          typeof this._client.personaEffective === "function") {
        try {
          const eff = await this._client.personaEffective({ conversationId: cid });
          const effId = eff && eff.ok && eff.effective && eff.effective.id;
          suppressed = !!(effId && effId !== pid);
        } catch (e) {}
      }
      this._pinState = ok ? { scope, suppressed } : null;
      this._updatePinState();
      this.emit("cp-persona-pinned", {
        personaId: pid,
        personaName: persona ? (persona.name || pid) : "",
        chatKey, conversationId: cid, scope, suppressed, ok,
      });
    }

    onAction(act, el) {
      if (act === "gen") { this._generate(); return; }
      if (act === "dir-clear") { this.clearDirective(); return; }
      if (act === "mode-reply" || act === "mode-opener") {
        this._setMode(act === "mode-opener" ? "opener" : "reply");
        return;
      }
      if (act === "pin") { this._pinPersona(); return; }
      if (act === "fill-pick") {
        const t = this._pickedText();
        if (t) this.emit("cp-fill", { text: t, source: "draft" });
        return;
      }
      if (act === "send-pick") {
        const t = this._pickedText();
        if (t) this._guardThenSend(t, "reply");
        return;
      }
      const d = this._draft || {};
      const textOf = (which) => (which === "translated" ? (d.translated || "") : (d.reply || ""));
      if (act === "fill") {
        const text = textOf(el.getAttribute("data-which"));
        if (text) this.emit("cp-fill", { text, source: "draft" });
        return;
      }
      if (act === "send") {
        const which = el.getAttribute("data-which") || "reply";
        const text = textOf(which);
        if (text) this._guardThenSend(text, which);
        return;
      }
      if (act === "send-force") {
        const which = el.getAttribute("data-which") || "reply";
        const text = textOf(which);
        if (text) this._emitSend(text, which, "high");
      }
    }

    async _guardThenSend(text, which) {
      const box = this.shadowRoot.querySelector(".guardbox");
      let g;
      try { g = await this._client.guardCheck({ text }); } catch (e) { g = null; }
      if (!g || !g.ok) { this._emitSend(text, which, "low"); return; } // 护栏不可用不阻断发送
      if (box) box.innerHTML = this._guardBanner(g, which);
      if (g.block) return; // 高风险:等待二次确认(force)
      this._emitSend(text, which, g.risk || "low");
    }

    _guardBanner(g, which) {
      const esc = (s) => this.esc(s);
      const sep = this.t("cp.common.list_sep");
      const cls = g.risk === "high" ? "high" : g.risk === "medium" ? "medium" : "low";
      const msg = g.risk === "high" ? this.t("cp.draft.guard_high")
        : g.risk === "medium" ? this.t("cp.draft.guard_medium") : this.t("cp.draft.guard_low");
      const hits = (g.hits || []).map((h) => esc(h.term)).join(sep); // 词条已转义
      const hitPfx = hits ? this.t("cp.draft.hit_pfx", { hits }) : "";
      const rob = (g.robotic || []).length
        ? this.t("cp.draft.robotic_pfx", { list: (g.robotic || []).map(esc).join(sep) }) : "";
      const force = g.block
        ? `<button class="force" data-act="send-force" data-which="${esc(which)}">${this.esc(this.t("cp.draft.send_force"))}</button>` : "";
      return `<div class="guard ${cls}">${msg}${hitPfx}${rob}${force}</div>`;
    }

    _emitSend(text, which, risk) {
      const box = this.shadowRoot.querySelector(".guardbox");
      if (box) box.innerHTML = "";
      this.emit("cp-send", { text, which, risk, conversationId: this._ctx && this._ctx.conversationId });
    }

    _pickedText() {
      const root = this.shadowRoot;
      const picked = root.querySelector("input[data-pick]:checked");
      const which = picked ? picked.getAttribute("data-pick") : "reply";
      const ta = root.querySelector(which === "contrast" ? '[data-role="contrast-ta"]' : '[data-role="reply-ta"]');
      return ta ? ta.value : "";
    }

    async _translateInto(ta, text, lang) {
      const s = (text || "").trim();
      if (!ta) return;
      if (!s) { ta.value = ""; ta.disabled = false; return; }
      ta.disabled = true; ta.value = this.t("cp.draft.translating");
      let r;
      try { r = await this._client.translate({ text: s, target_lang: lang }); } catch (e) { r = null; }
      ta.value = (r && r.ok && r.text) ? r.text : this.t("cp.draft.translate_fail");
      ta.disabled = false;
    }

    async _wireContrast(replyText, contrastLang) {
      const root = this.shadowRoot;
      const replyTa = root.querySelector('[data-role="reply-ta"]');
      const contrastTa = root.querySelector('[data-role="contrast-ta"]');
      const blocks = root.querySelectorAll(".lblock");
      root.querySelectorAll("input[data-pick]").forEach((rb) => {
        rb.addEventListener("change", () => {
          blocks.forEach((b) => b.classList.toggle("active", b.getAttribute("data-block") === rb.getAttribute("data-pick") && rb.checked));
        });
      });
      if (replyTa) replyTa.addEventListener("focus", () => { const r = root.querySelector('input[data-pick="reply"]'); if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); } });
      if (contrastTa) contrastTa.addEventListener("focus", () => { const r = root.querySelector('input[data-pick="contrast"]'); if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); } });
      await this._translateInto(contrastTa, replyText, contrastLang);
      let userEdited = false;
      if (contrastTa) contrastTa.addEventListener("input", () => { userEdited = true; });
      let t;
      if (replyTa) replyTa.addEventListener("input", () => {
        if (userEdited) return;
        clearTimeout(t);
        t = setTimeout(() => this._translateInto(contrastTa, replyTa.value, contrastLang), 600);
      });
    }

    _slot() { return this.shadowRoot.querySelector(".slot"); }
    _langKey() { return "cp_replylang:" + (this._ctx && this._ctx.conversationId || ""); }
    _loadLang() { try { return localStorage.getItem(this._langKey()) || ""; } catch (e) { return ""; } }
    _saveLang(v) { try { if (v) localStorage.setItem(this._langKey(), v); else localStorage.removeItem(this._langKey()); } catch (e) {} }

    async _generate() {
      const ctx = this._ctx;
      if (!this._client || !ctx || !ctx.conversationId) return;
      const cid = ctx.conversationId;
      const parts = String(cid).split(":");
      const platform = ctx.platform || parts[0] || "telegram";
      const chatKey = ctx.chatKey || (parts.length >= 3 ? parts.slice(2).join(":") : "");
      const sel = this.shadowRoot.querySelector('select[data-role="lang"]');
      const lang = sel ? sel.value : "";
      const slot = this._slot();
      if (slot) slot.innerHTML = `<div class="empty">${this.esc(this.t("cp.draft.generating"))}</div>`;
      const token = ++this._genToken;

      // 上下文来源:宿主可注入 messagesProvider(桌面=webview 实时消息,未必落后端 inbox);
      // 不设则自取持久化线程(后台=getHistory by conversation_id)。
      let raw = [];
      try {
        if (typeof this._messagesProvider === "function") {
          raw = (await this._messagesProvider({ conversationId: cid, platform, chatKey })) || [];
        } else {
          const h = await this._client.getHistory({ conversationId: cid, limit: 30 });
          raw = h && Array.isArray(h.messages) ? h.messages : [];
        }
      } catch (e) { raw = []; }
      const messages = (Array.isArray(raw) ? raw : [])
        .map((m) => ({ direction: m.direction, text: m.text }))
        .filter((m) => m.text);
      if (token !== this._genToken) return;
      const opener = this._mode === "opener";
      // P1-198：开新话题模式不依赖入站消息（没人说话也能主动开场）；续聊模式保持旧检查
      if (!messages.length && !opener) {
        if (slot) slot.innerHTML = `<div class="err">${this.esc(this.t("cp.draft.no_context"))}</div>`;
        return;
      }

      const personaSel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const personaId = personaSel ? personaSel.value : "";
      // conversation_id 必带：服务端由它反解 account → 会话覆写/账号人设才解析得准
      // （只给 platform+chat_key 时多账号下会落到 config 默认人设 → 口径与出站链分裂）。
      const payload = { messages, platform, chat_key: chatKey, target_lang: lang, conversation_id: cid };
      if (opener) payload.mode = "opener";
      // P2-198 直出模式：带上坐席 UI 语言——正文按客户语言直出时，服务端附一份
      // 该语言的对照译文（gloss，只读），中文坐席不必再切「中文」生成 + 发送再翻。
      payload.gloss_lang = (root.CopilotShared && root.CopilotShared.lang) || "zh";
      if (personaId) payload.persona_id = personaId;
      // P22：坐席指令进 prompt【坐席指令】——绝不写进 messages / composer
      const dirText = this._directive && String(this._directive.text || "").trim();
      if (dirText) {
        payload.instruction = dirText.slice(0, 400);
        // P23：目标/来源随行（服务端落 drive_draft 耐久事件 + 抽检样本归属）
        if (this._directive.goalId) payload.goal_id = this._directive.goalId;
        if (this._directive.source) payload.instruction_source = this._directive.source;
      }
      let r;
      try {
        r = await this._client.smartReply(payload);
      } catch (e) { r = null; }
      if (token !== this._genToken) return;
      if (!r || !r.ok || !r.reply) {
        if (slot) slot.innerHTML = `<div class="err">${this.esc((r && r.detail) || this.t("cp.draft.gen_fail"))}</div>`;
        return;
      }
      this._draft = r;
      this._draftLang = lang || "zh";
      // P4-C：记录本次实际请求的回复语言（可能源自服务端默认，未落本地记忆）；
      //       供 _paintDraft 判定是否取译文文本，替代仅看本地记忆的 _loadLang()。
      this._reqLang = lang;
      this._paintDraft(r);
    }

    /* P22：宿主（目标卡「采纳并拟稿」）注入坐席指令。
       opts: {text, summary?, label?, pushLevel?, goalId?, source?, autoGen?}
       text=送进 smart-reply.instruction 的完整指令；summary=chip 短展示（默认取首行）。
       autoGen 默认 true。
       P28：opener 产线已吃 instruction + 注入目标（P25），**不再**强制切回 reply——
       否则「开新话题」态点采纳会丢掉 opener 语义（坐席实录：目标设了开场仍跑题）。 */
    setDirective(opts) {
      const o = opts || {};
      const text = String(o.text || o.intent || "").trim().slice(0, 400);
      if (!text) return;
      const summary = String(o.summary || o.intent || "").trim()
        || text.split(/\n/)[0].slice(0, 80);
      this._directive = {
        text,
        summary,
        label: String(o.label || "").trim(),
        pushLevel: String(o.pushLevel || "").trim(),
        goalId: String(o.goalId || "").trim(),
        source: String(o.source || "").trim(),
      };
      this._paintDirective();
      if (o.autoGen !== false) this._generate();
    }
    clearDirective() {
      this._directive = null;
      this._paintDirective();
    }
    _paintDirective() {
      const host = this.shadowRoot && this.shadowRoot.querySelector('[data-role="dirhost"]');
      if (!host) return;
      const d = this._directive;
      if (!d || !d.text) { host.innerHTML = ""; return; }
      const lab = d.label || this.t("cp.draft.directive_label");
      const tipParts = [this.t("cp.draft.directive_tip")];
      if (d.pushLevel === "none") tipParts.push(this.t("cp.draft.directive_companion_tip"));
      if (d.text) tipParts.push(d.text);
      const tip = tipParts.filter(Boolean).join("\n");
      const shown = d.summary || d.text.split(/\n/)[0];
      host.innerHTML =
        `<div class="dirchip" title="${this.esc(tip)}">` +
        `<div class="dirbody"><span class="dirlab">${this.esc(lab)}</span>` +
        (d.pushLevel === "none"
          ? `<span class="dirlab" style="font-weight:400;opacity:.85">${this.esc(this.t("cp.draft.directive_companion_tip"))}</span>`
          : "") +
        `<span class="dirtxt">${this.esc(shown)}</span></div>` +
        `<button type="button" class="dirx" data-act="dir-clear" aria-label="${this.esc(this.t("cp.draft.directive_clear"))}">\u00d7</button></div>`;
    }

    /* 宿主联动（cp-persona-changed 后调用）：人设换绑使已展示的草稿口吻过期 ——
       仅当面板里已有草稿时才重生成（闲置面板不烧 LLM；下次手动生成天然用新人设）。 */
    regenerate() {
      if (this._draft) this._generate();
    }

    /* P1-198：切换生成模式（只改按钮态，不清已生成草稿、不烧 LLM）。 */
    _setMode(m) {
      this._mode = m === "opener" ? "opener" : "reply";
      this.shadowRoot.querySelectorAll("button.mode").forEach((b) => {
        const isOpener = b.getAttribute("data-act") === "mode-opener";
        b.classList.toggle("on", isOpener === (this._mode === "opener"));
      });
    }

    /* P28：goal_applied 徽章——「设了目标为什么没切入」从黑箱变可读。
       成功=绿；有 goal_id / 驱动指令却跳过=琥珀（带 reason）；无目标静默不刷。 */
    _goalBadgeHtml(r) {
      const ga = r && r.goal_applied;
      if (!ga || typeof ga !== "object") return "";
      const esc = (s) => this.esc(s);
      if (ga.injected) {
        const pl = String(ga.push_level || "").trim();
        const tip = [ga.title, ga.intent, ga.profile_gap].filter(Boolean).join("\n");
        return `<span class="bdg goal on" title="${esc(tip)}">🎯 ${esc(this.t("cp.draft.goal_on"))}` +
          (pl ? ` · ${esc(pl)}` : "") + `</span>`;
      }
      const reason = String(ga.reason || "").trim();
      if (!reason || reason === "no_goal" || reason === "unknown") return "";
      if (!ga.goal_id && !(this._directive && this._directive.goalId)) return "";
      const key = "cp.draft.goal_" + reason;
      let lab = this.t(key);
      if (!lab || lab === key || String(lab).indexOf("cp.draft.") === 0) {
        lab = this.t("cp.draft.goal_skipped");
      }
      const tip = [ga.title, reason, ga.hold_reason].filter(Boolean).join(" · ");
      return `<span class="bdg goal off" title="${esc(tip)}">🎯 ${esc(lab)}</span>`;
    }

    _paintDraft(r) {
      const esc = (s) => this.esc(s);
      const slot = this._slot();
      if (!slot) return;
      const tierKey = TIER[r.persona_tier];
      const tierLbl = tierKey ? this.t(tierKey) : (r.persona_tier || "");
      const badges =
        (r.persona ? `<span class="bdg">🎭 ${esc(r.persona)}${tierLbl ? " · " + esc(tierLbl) : ""}</span>` : "") +
        (r.intent ? `<span class="bdg intent">${esc(this.t("cp.draft.intent"))} ${esc(r.intent)}</span>` : "") +
        this._goalBadgeHtml(r);
      // P2 证据链：草稿引用的 KB 条目（display-only chips，悬停看片段——引用注入不再黑盒）
      const kbRefs = Array.isArray(r.kb_refs) ? r.kb_refs.slice(0, 3) : [];
      const kbChips = kbRefs.length
        ? `<div class="badges">` + kbRefs.map((k) =>
            `<span class="bdg kb" title="${esc(String(k.snippet || "").slice(0, 200))}">📚 ${esc(String(k.title || k.category || "").slice(0, 24))}</span>`
          ).join("") + `</div>`
        : "";
      // —— 对比语言路径(桌面：reply/contrast 双块可编辑 + send-pick) ——
      const wantContrast = this.hasAttribute("contrast");
      const contrastSel = this.shadowRoot.querySelector('select[data-role="contrast"]');
      const contrastLang = contrastSel ? contrastSel.value : "";
      const replyLang = this._draftLang || "zh";
      // 指定回复语言时后端已用该语言生成(translated≈reply)，取可读文本
      const replyText = (this._reqLang && r.translated) ? r.translated : r.reply;
      // P2-198 对照译文（只读）：正文按客户语言直出时给坐席看的母语对照。
      // 刻意**没有**「填入」按钮——把对照填进输入框发出去=把中文发给外语客户
      // （正是 P0 修掉的泄漏路径），对照只服务于「读得懂」。
      const gloss = r.gloss
        ? `<div class="tr gloss"><div class="tl">${esc(this.t("cp.draft.gloss"))}</div>${esc(r.gloss)}</div>` : "";
      if (wantContrast && contrastLang && contrastLang !== replyLang) {
        this._pickSeq += 1;
        const nm = "cppick" + this._pickSeq;
        slot.innerHTML =
          `<div class="draft">` +
          (badges ? `<div class="badges">${badges}</div>` : "") + kbChips +
          `<div class="lblock active" data-block="reply">` +
            `<div class="lhead"><input type="radio" name="${nm}" data-pick="reply" checked><span>${esc(this._langLabel(replyLang))}</span></div>` +
            `<textarea data-role="reply-ta" rows="4">${esc(replyText)}</textarea></div>` +
          `<div class="lblock" data-block="contrast">` +
            `<div class="lhead"><input type="radio" name="${nm}" data-pick="contrast"><span>${esc(this._langLabel(contrastLang))}</span></div>` +
            `<textarea data-role="contrast-ta" rows="4">${esc(this.t("cp.draft.translating"))}</textarea></div>` +
          gloss +
          `<div class="acts">` +
            `<button class="primary" data-act="fill-pick">${esc(this.t("cp.draft.fill"))}</button>` +
            `<button class="send" data-act="send-pick">${esc(this.t("cp.draft.fill_send"))}</button>` +
          `</div><div class="guardbox"></div></div>`;
        this._wireContrast(replyText, contrastLang);
        this._preflightGuardForPill(r);
        return;
      }
      // —— 默认路径(后台/无对比)：保持与原行为一致 ——
      const translated = r.translated
        ? `<div class="tr"><div class="tl">${esc(this.t("cp.draft.translation"))}</div>${esc(r.translated)}` +
          `<div class="acts"><button data-act="fill" data-which="translated">${esc(this.t("cp.draft.fill_translated"))}</button></div></div>` : "";
      // 发送默认用客户可读文本:有译文发译文,否则发原文
      const sendWhich = r.translated ? "translated" : "reply";
      slot.innerHTML =
        `<div class="draft">` +
        (badges ? `<div class="badges">${badges}</div>` : "") + kbChips +
        `<div class="reply">${esc(r.reply)}</div>` +
        `<div class="acts">` +
        `<button class="primary" data-act="fill" data-which="reply">${esc(this.t("cp.draft.fill"))}</button>` +
        `<button class="send" data-act="send" data-which="${sendWhich}">${esc(this.t("cp.draft.fill_send"))}</button>` +
        `</div>` +
        translated + gloss +
        `<div class="guardbox"></div>` +
        `</div>`;
      this._preflightGuardForPill(r);
    }

    async _preflightGuardForPill(r) {
      this._emitDraftLoaded(r, null);
      const text = (this._reqLang && r && r.translated) ? r.translated : (r && r.reply) || "";
      if (!text) return;
      let g = null;
      try { g = await this._client.guardCheck({ text }); } catch (e) { g = null; }
      if (g && g.ok) {
        const box = this.shadowRoot.querySelector(".guardbox");
        const which = (this._reqLang && r && r.translated) ? "translated" : "reply";
        if (box) box.innerHTML = this._guardBanner(g, which);
      }
      if (g && g.risk) this._emitDraftLoaded(r, g);
    }

    _emitDraftLoaded(r, guard) {
      const data = {
        generated: true,
        persona: (r && r.persona) || "",
        intent: (r && r.intent) || "",
      };
      if (guard && guard.risk) data.guardRisk = guard.risk;
      if (guard && guard.block) data.guardBlock = !!guard.block;
      this.emit("cp-data-loaded", {
        panelId: this.id || "",
        ok: true,
        data,
      });
    }
  }

  if (!customElements.get("cp-draft")) customElements.define("cp-draft", CpDraft);
})(typeof window !== "undefined" ? window : this);
