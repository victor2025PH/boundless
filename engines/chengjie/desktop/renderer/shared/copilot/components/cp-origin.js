"use strict";
/* 两端共享组件 · 跨平台档案(<cp-origin>)— 继承 CpPanelBase（2026-08-18 P0）
   「客户从哪个平台来 / 在那边叫什么 / 聊过哪些话题域」——展示来源轨迹 + 卡内两步
   表单补录；保存后服务端渲染 _origin_block 注入 AI（所填即所注，卡内可预览）。
   刻意用**卡内分步表单**而非弹层：右栏窄宽度下弹层收益低，且从根上避开
   「背板误触关闭丢内容」整类事故（cp-goal 曾为此补三层护栏）。
   草稿幸存：逐键快照 sessionStorage（键=会话 id，24h TTL）；编辑期外部 context
   重喂一律挂起（_ctxDeferred），关表单补刷——与 cp-goal 同契约。
   用法:
     const el = document.createElement('cp-origin');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey, platform, accountId };
   client 需实现: getOrigin / saveOrigin */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-origin: CpPanelBase 未加载"); return; }

  const DRAFT_PREFIX = "cpOrigin.draft::";
  const DRAFT_TTL_MS = 24 * 3600 * 1000;
  const CHANNELS = ["telegram", "whatsapp", "line", "messenger", "wechat", "web", "mobile", "other"];
  const KNOWN = ["", "recent", "months", "halfyear", "years"];
  const TOPIC_PRESETS = ["daily", "work", "family", "hobby", "feelings", "trade"];

  class CpOrigin extends Base {
    constructor() {
      super();
      this._formOpen = false;
      this._formStep = 1;
      this._ctxDeferred = null;
      this._status = null; // {text, tone:'ok'|'err'|'warn'}
      /* P1 导入向导态：file 对象只活在内存（无法进 sessionStorage，关向导即弃） */
      this._impOpen = false;
      this._impBusy = false;
      this._impErr = "";
      this._impFile = null;
      this._impData = null;   // parse 响应
      this._impSel = null;    // {sourceChannel, sourceLabel, customer, topicsOff, note, manualFacts}
    }
    emptyText() { return this.t("cp.origin.empty"); }
    emptyDataText() {
      // contacts 子系统整体关闭时 GET 返回 {ok:false, error:"contacts_disabled"}
      // （走基类空态而非 renderData）——按「未开通」提示而非「暂无数据」误导。
      const d = this._d;
      if (d && d.error === "contacts_disabled") return this.t("cp.origin.unavailable");
      return this.t("cp.origin.no_data");
    }
    errText() { return this.t("cp.origin.err"); }

    /* 编辑期防打断：表单/向导打开时外部重喂只暂存，关闭后补刷（cp-goal 同契约）。 */
    set context(ctx) {
      if (this._formOpen || this._impOpen) { this._ctxDeferred = ctx; return; }
      super.context = ctx;
    }
    get context() { return super.context; }

    styles() {
      return `
      .trail { display:flex; align-items:center; gap:4px; flex-wrap:wrap; margin-bottom:var(--cp-gap-xs,4px); }
      .tchip { display:inline-block; font-size:var(--cp-fs-tiny,11px); line-height:1.6; padding:0 8px;
               border-radius:99px; border:1px solid var(--cp-border,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .tchip.cur { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); font-weight:600; }
      .tarrow { color:var(--cp-text-tiny,#94a3b8); font-size:var(--cp-fs-tiny,11px); }
      .line { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); margin:2px 0; line-height:1.5; }
      .line b { color:var(--cp-text,#1e293b); font-weight:600; }
      .topics { display:flex; gap:4px; flex-wrap:wrap; margin:3px 0; }
      .tp { font-size:var(--cp-fs-tiny,11px); padding:1px 7px; border-radius:99px;
            background:var(--cp-accent-weak,rgba(79,70,229,.08)); color:var(--cp-accent,#4f46e5); }
      .warnrow { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn,#d97706); margin:4px 0;
                 padding:4px 8px; border:1px dashed color-mix(in srgb,var(--cp-warn,#d97706) 45%,transparent);
                 border-radius:8px; }
      .status { font-size:var(--cp-fs-tiny,11px); margin-top:4px; }
      .status.ok { color:var(--cp-ok,#0f9d75); } .status.err { color:var(--cp-danger,#dc2626); }
      .status.warn { color:var(--cp-warn,#d97706); }
      details.prev { margin-top:var(--cp-gap-xs,4px); }
      details.prev summary { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); cursor:pointer; }
      .prevbox { font-size:var(--cp-fs-tiny,11px); white-space:pre-wrap; line-height:1.55;
                 background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:8px; padding:6px 8px; margin-top:3px; color:var(--cp-text-dim,#64748b); }
      .guide { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); line-height:1.55; margin-bottom:6px; }
      /* —— 表单 —— */
      .f-hd { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
      .f-ttl { font-size:var(--cp-fs-sm,12px); font-weight:700; color:var(--cp-text,#1e293b); flex:1; }
      .f-x { border:none; background:transparent; padding:2px 6px; font-size:13px; color:var(--cp-text-tiny,#94a3b8); }
      .f-row { margin-bottom:6px; }
      .f-lbl { display:block; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:2px; }
      select, input[type=text], textarea {
        width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        padding:4px 7px; background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      textarea { resize:vertical; min-height:52px; }
      .tpsel { display:flex; gap:4px; flex-wrap:wrap; }
      .tpsel .tp { cursor:pointer; user-select:none; border:1px solid transparent;
                   background:var(--cp-surface-2,#f8fafc); color:var(--cp-text-dim,#64748b); }
      .tpsel .tp.on { background:var(--cp-accent-weak,rgba(79,70,229,.1));
                      color:var(--cp-accent,#4f46e5); border-color:var(--cp-accent,#4f46e5); }
      .chk { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px);
             color:var(--cp-text-dim,#64748b); }
      .chk input { width:auto; }
      .steps { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      /* —— 导入向导 —— */
      .imp-stats { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                   background:var(--cp-surface-2,#f8fafc); border-radius:8px; padding:5px 8px; margin-bottom:6px; }
      .imp-fact { display:flex; gap:6px; align-items:flex-start; padding:4px 6px; border-radius:7px;
                  border:1px solid var(--cp-border,#e2e8f0); margin-bottom:4px; }
      .imp-fact input { width:auto; margin-top:2px; flex:0 0 auto; }
      .imp-fact .ft { flex:1; min-width:0; font-size:var(--cp-fs-sm,12px); line-height:1.45; color:var(--cp-text,#1e293b); }
      .imp-fact .fq { display:block; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                      margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .imp-fact .sus { display:inline-block; font-size:10px; padding:0 5px; border-radius:99px; margin-left:4px;
                       background:color-mix(in srgb,var(--cp-warn,#d97706) 12%,transparent); color:var(--cp-warn,#d97706); }
      .imp-batch { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px);
                   color:var(--cp-text-dim,#64748b); padding:3px 0; }
      .imp-batch .bmeta { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .imp-batch .revoked { text-decoration:line-through; opacity:.6; }`;
    }

    async fetchData(ctx) {
      return this._client.getOrigin({
        conversationId: ctx.conversationId, platform: ctx.platform,
        accountId: ctx.accountId, chatKey: ctx.chatKey,
      });
    }

    /* ── 草稿幸存 ── */
    _draftKey() {
      const cid = (this._ctx && this._ctx.conversationId) || "";
      return cid ? DRAFT_PREFIX + cid : "";
    }
    _draftLoad() {
      const k = this._draftKey(); if (!k) return null;
      try {
        const raw = sessionStorage.getItem(k); if (!raw) return null;
        const o = JSON.parse(raw);
        if (!o || !o.ts || (Date.now() - o.ts) > DRAFT_TTL_MS) { sessionStorage.removeItem(k); return null; }
        return o.v || null;
      } catch (_e) { return null; }
    }
    _draftSave() {
      const k = this._draftKey(); if (!k) return;
      try { sessionStorage.setItem(k, JSON.stringify({ ts: Date.now(), v: this._collectForm() })); } catch (_e) {}
    }
    _draftClear() {
      const k = this._draftKey(); if (!k) return;
      try { sessionStorage.removeItem(k); } catch (_e) {}
    }

    /* ── 视图 ── */
    renderData(d) {
      if (this._formOpen) return this._renderForm();
      if (this._impOpen) return this._renderImport();
      const esc = (s) => this.esc(s);
      const prof = d.profile || null;
      const trail = Array.isArray(d.trail) ? d.trail : [];
      const imports = Array.isArray(d.imports) ? d.imports : [];
      const hasData = !!(prof || trail.length >= 2 || imports.length);
      const parts = [];
      // 未开通（origin_profile 热闸关 / contacts 子系统关）：只给友好提示，
      // 不给「填写/导入」入口——邀请用户填一张必然保存失败的表比隐藏更糟
      // （2026-08-20 内测实录：v1.0.43 客户包填完档案保存失败）。
      if (d.enabled === false) {
        parts.push(`<div class="warnrow">${esc(this.t("cp.origin.unavailable"))}</div>`);
        if (this._status) {
          parts.push(`<div class="status ${this._status.tone}">${esc(this._status.text)}</div>`);
          this._status = null;
        }
        return parts.join("");
      }
      if (!hasData) {
        parts.push(`<div class="guide">${esc(this.t("cp.origin.guide"))}</div>`);
        parts.push(`<div class="acts"><button class="primary" data-act="form">${esc(this.t("cp.origin.fill_btn"))}</button>` +
          `<button data-act="imp_open">${esc(this.t("cp.origin.imp_btn"))}</button></div>`);
      } else {
        if (trail.length) {
          const cur = this._curChannelKey();
          const chips = trail.map((r) => {
            const isCur = this._normCh(r.channel) === cur;
            // i18n P0：优先按渠道**码**查本地化标签（服务端 r.label 恒中文，
            // 英文 UI 直显会漏「其他平台/网页」这类中文）；未知码回落服务端 label。
            const lbl = this._chLabel(r.channel) || r.label || r.channel;
            return `<span class="tchip${isCur ? " cur" : ""}">${esc(lbl)}</span>`;
          });
          parts.push(`<div class="trail">${chips.join(`<span class="tarrow">→</span>`)}</div>`);
        }
        const lines = [];
        if (prof && prof.origin_channel) {
          const bits = [this._chLabel(prof.origin_channel)];
          if (prof.origin_label) bits.push(prof.origin_label);
          const ks = this._knownLabel(prof.known_since);
          if (ks) bits.push(ks);
          lines.push(`<div class="line">${esc(this.t("cp.origin.from"))}：<b>${esc(bits.join(" · "))}</b></div>`);
        }
        const names = [];
        const prior = (prof && prof.prior_names) || {};
        Object.keys(prior).forEach((ch) => {
          if (prior[ch]) names.push(`${this._chLabel(ch)}「${prior[ch]}」`);
        });
        if (prof && prof.preferred_name) names.push(`${this.t("cp.origin.preferred")}「${prof.preferred_name}」`);
        if (names.length) lines.push(`<div class="line">${esc(this.t("cp.origin.names"))}：${esc(names.join("；"))}</div>`);
        parts.push(lines.join(""));
        const topics = (prof && prof.topics) || [];
        if (topics.length) {
          parts.push(`<div class="topics">${topics.slice(0, 8).map((t) => `<span class="tp">${esc(t)}</span>`).join("")}</div>`);
        }
        if (d.enabled === false) {
          parts.push(`<div class="warnrow">${esc(this.t("cp.origin.disabled_hint"))}</div>`);
        }
        if (imports.length) {
          const rows = imports.slice(0, 5).map((b) => {
            const revoked = b.status !== "confirmed";
            const meta = [
              this._chLabel(b.source_channel || "") || b.source_label || "",
              (b.msg_count ? b.msg_count + this.t("cp.origin.imp_msgs_suffix") : ""),
              (b.date_from ? `${b.date_from}~${b.date_to || "?"}` : ""),
            ].filter(Boolean).join(" · ");
            const btn = revoked
              ? `<span class="sus">${esc(this.t("cp.origin.imp_revoked"))}</span>`
              : `<button data-act="imp_revoke" data-bid="${esc(b.batch_id || "")}">${esc(this.t("cp.origin.imp_revoke"))}</button>`;
            return `<div class="imp-batch"><span class="bmeta${revoked ? " revoked" : ""}">${esc(meta)}</span>${btn}</div>`;
          }).join("");
          parts.push(
            `<details class="prev"><summary>${esc(this.t("cp.origin.imp_batches", { n: imports.length }))}</summary>${rows}</details>`);
        }
        if (d.block_preview) {
          parts.push(
            `<details class="prev"><summary>${esc(this.t("cp.origin.preview_ttl"))}</summary>` +
            `<div class="prevbox">${esc(d.block_preview)}</div></details>`);
        }
        const acts = [`<button class="primary" data-act="form">${esc(this.t(prof ? "cp.origin.edit_btn" : "cp.origin.fill_btn"))}</button>`];
        acts.push(`<button data-act="imp_open">${esc(this.t("cp.origin.imp_btn"))}</button>`);
        if (d.contact_id) {
          acts.push(`<button data-act="open360" data-cid="${esc(d.contact_id)}">${esc(this.t("cp.origin.manage"))}</button>`);
        }
        parts.push(`<div class="acts">${acts.join("")}</div>`);
      }
      if (this._status) {
        parts.push(`<div class="status ${this._status.tone}">${esc(this._status.text)}</div>`);
        this._status = null;
      }
      return parts.join("");
    }

    /* ── 表单 ── */
    _renderForm() {
      const esc = (s) => this.esc(s);
      const f = this._form || {};
      const step = this._formStep;
      const hd =
        `<div class="f-hd"><span class="f-ttl">${esc(this.t(step === 1 ? "cp.origin.step1_ttl" : "cp.origin.step2_ttl"))}</span>` +
        `<span class="steps">${step}/2</span>` +
        `<button class="f-x" data-act="close" title="✕">✕</button></div>`;
      if (step === 1) {
        const chOpts = CHANNELS.concat(
          f.origin_channel && CHANNELS.indexOf(f.origin_channel) < 0 ? [f.origin_channel] : []
        ).map((c) =>
          `<option value="${esc(c)}"${f.origin_channel === c ? " selected" : ""}>${esc(this._chLabel(c))}</option>`);
        const ksOpts = KNOWN.map((k) =>
          `<option value="${esc(k)}"${(f.known_since || "") === k ? " selected" : ""}>${esc(k ? this.t("cp.origin.known." + k) : this.t("cp.origin.known.unknown"))}</option>`);
        return hd +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_channel"))}</label>` +
          `<select data-f="origin_channel"><option value=""></option>${chOpts.join("")}</select></div>` +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_prior_name"))}</label>` +
          `<input type="text" data-f="prior_name" maxlength="40" value="${esc(f.prior_name || "")}"></div>` +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_known"))}</label>` +
          `<select data-f="known_since">${ksOpts.join("")}</select></div>` +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_label"))}</label>` +
          `<input type="text" data-f="origin_label" maxlength="80" value="${esc(f.origin_label || "")}"></div>` +
          `<div class="acts"><button class="primary" data-act="next">${esc(this.t("cp.origin.next"))}</button></div>`;
      }
      const tsel = TOPIC_PRESETS.map((k) => {
        const label = this.t("cp.origin.tp." + k);
        const on = (f.topics || []).indexOf(label) >= 0;
        return `<span class="tp${on ? " on" : ""}" data-act="tp" data-tp="${esc(label)}">${esc(label)}</span>`;
      }).join("");
      const customTopics = (f.topics || []).filter((t) =>
        TOPIC_PRESETS.every((k) => this.t("cp.origin.tp." + k) !== t)).join("、");
      return hd +
        `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_topics"))}</label>` +
        `<div class="tpsel">${tsel}</div>` +
        `<input type="text" data-f="topics_custom" maxlength="120" placeholder="${esc(this.t("cp.origin.f_topics_add"))}" value="${esc(customTopics)}" style="margin-top:4px;"></div>` +
        `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_facts"))}</label>` +
        `<textarea data-f="facts" rows="3" placeholder="${esc(this.t("cp.origin.f_facts_ph"))}">${esc(f.facts || "")}</textarea></div>` +
        `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_preferred"))}</label>` +
        `<input type="text" data-f="preferred_name" maxlength="40" value="${esc(f.preferred_name || "")}"></div>` +
        `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_note"))}</label>` +
        `<textarea data-f="background_note" rows="2" maxlength="400">${esc(f.background_note || "")}</textarea></div>` +
        `<div class="f-row chk"><input type="checkbox" data-f="ai_visible"${f.ai_visible === false ? "" : " checked"} id="cpo-aiv">` +
        `<label for="cpo-aiv">${esc(this.t("cp.origin.f_ai_visible"))}</label></div>` +
        `<div class="acts"><button data-act="back">${esc(this.t("cp.origin.back"))}</button>` +
        `<button class="primary" data-act="save">${esc(this.t("cp.origin.save"))}</button></div>`;
    }

    /* 从 shadow DOM 收集表单值合并进 this._form（渲染前必调，防重渲丢输入）。 */
    _collectForm() {
      const f = this._form || {};
      this.shadowRoot.querySelectorAll("[data-f]").forEach((el) => {
        const k = el.getAttribute("data-f");
        if (el.type === "checkbox") f[k] = !!el.checked;
        else f[k] = el.value;
      });
      this._form = f;
      return f;
    }
    _bindFormInputs() {
      this.shadowRoot.querySelectorAll("[data-f]").forEach((el) => {
        el.addEventListener("input", () => { this._collectForm(); this._draftSave(); });
        el.addEventListener("change", () => { this._collectForm(); this._draftSave(); });
      });
    }
    _paintForm() {
      this._render(this._renderForm());
      this._bindFormInputs();
    }

    _openForm() {
      const d = this._d || {};
      const prof = d.profile || {};
      const draft = this._draftLoad();
      if (draft) {
        this._form = draft;
        this._status = { text: this.t("cp.origin.draft_restored"), tone: "ok" };
      } else {
        const prior = prof.prior_names || {};
        const firstPrior = Object.keys(prior)[0] || "";
        this._form = {
          origin_channel: prof.origin_channel || "",
          origin_label: prof.origin_label || "",
          known_since: prof.known_since || "",
          prior_name: firstPrior ? String(prior[firstPrior] || "") : "",
          prior_channel: firstPrior,
          topics: (prof.topics || []).slice(),
          facts: "",
          preferred_name: prof.preferred_name || "",
          background_note: prof.background_note || "",
          ai_visible: prof.ai_visible !== false,
        };
      }
      this._formOpen = true;
      this._formStep = (draft && draft.__step === 2) ? 2 : 1;
      this._paintForm();
    }
    _closeForm(keepDraft) {
      this._formOpen = false;
      this._formStep = 1;
      if (!keepDraft) this._draftClear();
      else this._status = { text: this.t("cp.origin.dirty_hint"), tone: "warn" };
      const deferred = this._ctxDeferred;
      this._ctxDeferred = null;
      if (deferred) { super.context = deferred; return; }
      this.refresh();
    }

    /* ── P1 导入向导 ── */
    _openImport() {
      this._impOpen = true;
      this._impBusy = false;
      this._impFile = null;
      this._impData = null;
      this._impErr = "";
      this._impSel = { sourceChannel: "wechat", sourceLabel: "", customer: "",
                       topicsOff: {}, note: null, manualFacts: "" };
      this._impPaint();
    }
    _closeImport() {
      this._impOpen = false;
      this._impBusy = false;
      this._impFile = null;
      this._impData = null;
      const deferred = this._ctxDeferred;
      this._ctxDeferred = null;
      if (deferred) { super.context = deferred; return; }
      this.refresh();
    }
    _impPaint() {
      this._render(this._renderImport());
      const fi = this.shadowRoot.querySelector('input[type="file"]');
      if (fi) {
        fi.addEventListener("change", () => {
          this._impFile = (fi.files && fi.files[0]) || null;
          const nm = this.shadowRoot.querySelector("[data-imp-fname]");
          if (nm) nm.textContent = this._impFile ? this._impFile.name : "";
        });
      }
      this.shadowRoot.querySelectorAll("[data-imp]").forEach((el) => {
        el.addEventListener("change", () => {
          const k = el.getAttribute("data-imp");
          this._impSel[k] = el.value;
          if (k === "customer" && this._impData) this._impParse();  // 换客户方 → 重解析
        });
        el.addEventListener("input", () => {
          this._impSel[el.getAttribute("data-imp")] = el.value;
        });
      });
    }
    _renderImport() {
      const esc = (s) => this.esc(s);
      const sel = this._impSel || {};
      const d = this._impData;
      const hd =
        `<div class="f-hd"><span class="f-ttl">${esc(this.t("cp.origin.imp_ttl"))}</span>` +
        `<span class="steps">${d ? "2/2" : "1/2"}</span>` +
        `<button class="f-x" data-act="imp_close" title="✕">✕</button></div>`;
      if (!d) {
        const chOpts = CHANNELS.map((c) =>
          `<option value="${esc(c)}"${sel.sourceChannel === c ? " selected" : ""}>${esc(this._chLabel(c))}</option>`);
        return hd +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_src"))}</label>` +
          `<select data-imp="sourceChannel">${chOpts.join("")}</select></div>` +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_label"))}</label>` +
          `<input type="text" data-imp="sourceLabel" maxlength="80" value="${esc(sel.sourceLabel || "")}"></div>` +
          `<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_file"))}</label>` +
          `<input type="file" accept=".txt,.json,.csv,text/plain,application/json,text/csv">` +
          `<span class="steps" data-imp-fname>${esc(this._impFile ? this._impFile.name : "")}</span></div>` +
          (this._impErr ? `<div class="status err">${esc(this._impErr)}</div>` : "") +
          `<div class="acts"><button class="primary" data-act="imp_parse"${this._impBusy ? " disabled" : ""}>` +
          `${esc(this.t(this._impBusy ? "cp.origin.imp_parsing" : "cp.origin.imp_parse"))}</button></div>`;
      }
      // ── 审阅屏 ──
      const sm = d.summary || {};
      const parts = [];
      const rng = (d.date_from ? `${d.date_from}~${d.date_to || "?"} · ` : "");
      parts.push(`<div class="imp-stats">${esc(rng)}${d.msg_count}${esc(this.t("cp.origin.imp_msgs_suffix"))}` +
        `${d.truncated ? " · " + esc(this.t("cp.origin.imp_truncated")) : ""}</div>`);
      if (d.duplicate) parts.push(`<div class="warnrow">${esc(this.t("cp.origin.imp_dup_warn"))}</div>`);
      if (sm.error) parts.push(`<div class="warnrow">${esc(this.t("cp.origin.imp_llm_err"))}</div>`);
      const sOpts = (d.senders || []).map((s) =>
        `<option value="${esc(s.name)}"${(sel.customer || d.customer_sender) === s.name ? " selected" : ""}>` +
        `${esc(s.name)} (${s.count})</option>`);
      parts.push(`<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_customer"))}</label>` +
        `<select data-imp="customer">${sOpts.join("")}</select></div>`);
      const topics = (sm.topics || []);
      if (topics.length) {
        const chips = topics.map((t) => {
          const off = !!(sel.topicsOff || {})[t];
          return `<span class="tp${off ? "" : " on"}" data-act="imp_tp" data-tp="${esc(t)}">${esc(t)}</span>`;
        }).join("");
        parts.push(`<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_topics"))}</label>` +
          `<div class="tpsel">${chips}</div></div>`);
      }
      parts.push(`<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_note"))}</label>` +
        `<textarea data-imp="note" rows="2" maxlength="400">${esc(sel.note != null ? sel.note : (sm.note || ""))}</textarea></div>`);
      const facts = (sm.facts || []);
      if (facts.length) {
        const rows = facts.map((f, i) => {
          const sus = f.grounded === false;
          return `<label class="imp-fact"><input type="checkbox" data-fi="${i}"${sus ? "" : " checked"}>` +
            `<span class="ft">${esc(f.text)}${sus ? `<span class="sus">${esc(this.t("cp.origin.imp_suspect"))}</span>` : ""}` +
            (f.quote ? `<span class="fq">“${esc(f.quote)}”</span>` : "") + `</span></label>`;
        }).join("");
        parts.push(`<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.imp_facts"))}</label>${rows}</div>`);
      }
      parts.push(`<div class="f-row"><label class="f-lbl">${esc(this.t("cp.origin.f_facts"))}</label>` +
        `<textarea data-imp="manualFacts" rows="2" placeholder="${esc(this.t("cp.origin.f_facts_ph"))}">${esc(sel.manualFacts || "")}</textarea></div>`);
      if (this._impErr) parts.push(`<div class="status err">${esc(this._impErr)}</div>`);
      parts.push(`<div class="acts"><button data-act="imp_back"${this._impBusy ? " disabled" : ""}>${esc(this.t("cp.origin.back"))}</button>` +
        `<button class="primary" data-act="imp_confirm"${(this._impBusy || d.duplicate) ? " disabled" : ""}>` +
        `${esc(this.t(this._impBusy ? "cp.origin.imp_confirming" : "cp.origin.imp_confirm"))}</button></div>`);
      return hd + parts.join("");
    }
    async _impParse() {
      if (!this._impFile) { this._impErr = this.t("cp.origin.imp_need_file"); this._impPaint(); return; }
      const ctx = this._ctx || {};
      this._impBusy = true; this._impErr = ""; this._impPaint();
      let res = null;
      try {
        res = await this._client.importOriginParse({
          conversationId: ctx.conversationId, platform: ctx.platform,
          accountId: ctx.accountId, chatKey: ctx.chatKey,
          file: this._impFile,
          sourceChannel: (this._impSel || {}).sourceChannel || "",
          sourceLabel: (this._impSel || {}).sourceLabel || "",
          customerSender: (this._impSel || {}).customer || "",
        });
      } catch (_e) { res = null; }
      this._impBusy = false;
      if (!this._impOpen) return;   // 等待期间被关闭 → 丢弃
      if (!res || res.ok === false) {
        const code = (res && res.error) || "";
        const key = "cp.origin.imp_err_" + code;
        const msg = this.t(key);
        this._impErr = (msg !== key) ? msg : ((res && res.error) || this.t("cp.origin.imp_err"));
        this._impData = null;
        this._impPaint();
        return;
      }
      this._impErr = "";
      this._impData = res;
      this._impSel.customer = res.customer_sender || "";
      this._impPaint();
    }
    async _impConfirm(btn) {
      const d = this._impData; if (!d) return;
      const sel = this._impSel || {};
      const ctx = this._ctx || {};
      const topics = (d.summary && d.summary.topics || []).filter((t) => !(sel.topicsOff || {})[t]);
      const facts = [];
      this.shadowRoot.querySelectorAll("input[data-fi]").forEach((cb) => {
        if (!cb.checked) return;
        const f = (d.summary && d.summary.facts || [])[Number(cb.getAttribute("data-fi"))];
        if (f && f.text) facts.push(f.text);
      });
      String(sel.manualFacts || "").split(/\n+/).forEach((ln) => {
        const s = ln.trim();
        if (s.length >= 2 && facts.indexOf(s) < 0) facts.push(s);
      });
      this._impBusy = true; this._impErr = ""; this._impPaint();
      let res = null;
      try {
        res = await this._client.importOriginConfirm({
          conversationId: ctx.conversationId, platform: ctx.platform,
          accountId: ctx.accountId, chatKey: ctx.chatKey,
          sourceChannel: sel.sourceChannel || "",
          sourceLabel: sel.sourceLabel || "",
          fileName: d.file_name || "", fileSha256: d.file_sha256 || "",
          msgCount: d.msg_count || 0, dateFrom: d.date_from || "", dateTo: d.date_to || "",
          topics: topics,
          note: (sel.note != null ? sel.note : (d.summary && d.summary.note) || ""),
          facts: facts.slice(0, 8),
        });
      } catch (_e) { res = null; }
      this._impBusy = false;
      if (!this._impOpen) return;
      if (!res || res.ok === false) {
        const code = (res && res.error) || "";
        const key = "cp.origin.imp_err_" + code;
        const msg = this.t(key);
        this._impErr = (msg !== key) ? msg : ((res && res.error) || this.t("cp.origin.save_err"));
        this._impPaint();
        return;
      }
      const nWritten = Number(res.facts_written || 0);
      let doneText = this.t("cp.origin.imp_done", { n: nWritten });
      // 轻量「关系起点」提示（P2）：导入了成规模的历史 → 建议去关系进展卡确认
      // 更成熟的起点（走既有人工确认机制，绝不自动改阶段）。
      if (nWritten >= 3 || Number(res.msg_count || d.msg_count || 0) >= 50) {
        doneText += " " + this.t("cp.origin.imp_stage_hint");
      }
      this._status = { text: doneText, tone: "ok" };
      this.emit("cp-origin-saved", {
        conversationId: ctx.conversationId,
        factsWritten: Number(res.facts_written || 0), imported: true,
      });
      this._closeImport();
    }

    async onAction(act, el) {
      if (act === "form") { this._openForm(); return; }
      if (act === "open360") {
        const cid = el.getAttribute("data-cid") || "";
        if (cid) { try { root.open("/workspace/contact/" + encodeURIComponent(cid), "_blank", "noopener"); } catch (_e) {} }
        return;
      }
      /* —— 导入向导 —— */
      if (act === "imp_open") { this._openImport(); return; }
      if (act === "imp_close") { this._closeImport(); return; }
      if (act === "imp_parse") { await this._impParse(); return; }
      if (act === "imp_back") {
        this._impData = null; this._impErr = "";
        this._impPaint(); return;
      }
      if (act === "imp_tp") {
        const t = el.getAttribute("data-tp") || "";
        const off = (this._impSel.topicsOff = this._impSel.topicsOff || {});
        off[t] = !off[t];
        this._impPaint(); return;
      }
      if (act === "imp_confirm") { await this._impConfirm(el); return; }
      if (act === "imp_revoke") {
        const bid = el.getAttribute("data-bid") || "";
        if (!bid) return;
        const ok = (typeof confirm === "function")
          ? confirm(this.t("cp.origin.imp_revoke_confirm")) : true;
        if (!ok) return;
        el.disabled = true;
        let res = null;
        try { res = await this._client.importOriginRevoke({ batchId: bid }); } catch (_e) { res = null; }
        if (res && res.ok !== false) {
          this._status = { text: this.t("cp.origin.imp_revoked"), tone: "ok" };
          this.refresh();
        } else {
          el.disabled = false;
        }
        return;
      }
      if (!this._formOpen) return;
      if (act === "tp") {
        this._collectForm();
        const f = this._form;
        const label = el.getAttribute("data-tp") || "";
        f.topics = f.topics || [];
        const i = f.topics.indexOf(label);
        if (i >= 0) f.topics.splice(i, 1); else f.topics.push(label);
        this._draftSave();
        this._paintForm();
        return;
      }
      if (act === "next") {
        this._collectForm(); this._form.__step = 2; this._formStep = 2;
        this._draftSave(); this._paintForm(); return;
      }
      if (act === "back") {
        this._collectForm(); this._form.__step = 1; this._formStep = 1;
        this._draftSave(); this._paintForm(); return;
      }
      if (act === "close") {
        this._collectForm();
        const f = this._form || {};
        const dirty = !!(f.origin_channel || f.origin_label || f.prior_name ||
          (f.topics || []).length || (f.facts || "").trim() ||
          f.preferred_name || f.background_note);
        this._closeForm(dirty);
        return;
      }
      if (act === "save") { await this._save(el); return; }
    }

    async _save(btn) {
      this._collectForm();
      const ctx = this._ctx || {};
      const f = this._form || {};
      const topics = (f.topics || []).slice();
      String(f.topics_custom || "").split(/[、,，;；]/).forEach((t) => {
        const s = t.trim();
        if (s && topics.indexOf(s) < 0) topics.push(s);
      });
      const facts = String(f.facts || "").split(/\n+/)
        .map((s) => s.trim()).filter((s) => s.length >= 2).slice(0, 5);
      const priorNames = {};
      if ((f.prior_name || "").trim()) {
        priorNames[f.prior_channel || f.origin_channel || "other"] = f.prior_name.trim();
      }
      const payload = {
        conversationId: ctx.conversationId, platform: ctx.platform,
        accountId: ctx.accountId, chatKey: ctx.chatKey,
        profile: {
          origin_channel: f.origin_channel || "",
          origin_label: f.origin_label || "",
          known_since: f.known_since || "",
          preferred_name: f.preferred_name || "",
          background_note: f.background_note || "",
          prior_names: priorNames,
          topics: topics,
          ai_visible: f.ai_visible !== false,
        },
        facts: facts,
      };
      if (btn) { btn.disabled = true; btn.textContent = this.t("cp.origin.saving"); }
      let res = null;
      try { res = await this._client.saveOrigin(payload); } catch (_e) { res = null; }
      if (!res || res.ok === false) {
        if (btn) { btn.disabled = false; btn.textContent = this.t("cp.origin.save"); }
        // 错误码 → 人话（裸 "contacts_disabled" 曾原样显给内测用户）
        const ecode = (res && res.error) || "";
        const etext = ecode === "contacts_disabled"
          ? this.t("cp.origin.unavailable")
          : (ecode || this.t("cp.origin.save_err"));
        this._status = { text: etext, tone: "err" };
        this._paintForm();
        const st = this.shadowRoot.querySelector(".status");
        if (!st) {
          const wrap = this._wrap();
          const div = document.createElement("div");
          div.className = "status err";
          div.textContent = this._status.text;
          wrap.appendChild(div);
        }
        this._status = null;
        return;
      }
      this._draftClear();
      this._formOpen = false;
      this._formStep = 1;
      const n = Number(res.facts_written || 0);
      this._status = {
        text: n > 0 ? this.t("cp.origin.saved_facts", { n: n }) : this.t("cp.origin.saved"),
        tone: "ok",
      };
      this.emit("cp-origin-saved", { conversationId: ctx.conversationId, factsWritten: n });
      const deferred = this._ctxDeferred;
      this._ctxDeferred = null;
      if (deferred) { super.context = deferred; return; }
      this.refresh();
    }

    /* ── 小工具 ── */
    _normCh(c) {
      let s = String(c || "").trim().toLowerCase();
      if (s.slice(-4) === "_rpa") s = s.slice(0, -4);
      return s;
    }
    _curChannelKey() {
      return this._normCh((this._ctx && this._ctx.platform) || "");
    }
    _chLabel(c) {
      const k = this._normCh(c);
      const fixed = { telegram: "Telegram", line: "LINE", messenger: "Messenger", whatsapp: "WhatsApp" };
      if (fixed[k]) return fixed[k];
      const key = "cp.origin.ch." + k;
      const v = this.t(key);
      return v === key ? (c || "") : v;
    }
    _knownLabel(k) {
      const s = String(k || "").trim();
      if (!s) return "";
      const key = "cp.origin.known." + s;
      const v = this.t(key);
      return v === key ? s : v;
    }
  }

  if (!customElements.get("cp-origin")) customElements.define("cp-origin", CpOrigin);
})(typeof window !== "undefined" ? window : this);
