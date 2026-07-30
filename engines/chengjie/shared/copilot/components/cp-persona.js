"use strict";
/* 两端共享组件 · 会话人设(<cp-persona>)— 继承 CpPanelBase
   2026-07-26 方案 A 重写:三层真相 UI ——
   ① 生效横幅:这条会话此刻实际以谁的身份说话(与出站链同一 resolver 的读侧,
      /api/persona/effective),tier 徽章标明来源(会话覆写/账号人设/legacy/域默认);
   ② 会话级覆写选择器(开关 inbox.persona_conv_override.enabled 开启时):卡片列表 +
      搜索,点选即换绑当前会话(有既往绑定/出站历史时先弹确认——用户拍板的弹窗口径);
   ③ 优雅降级:effective API 不可用(RPA 2 段键会话/桌面壳未暴露 IPC)或开关关 →
      回落 legacy peer 级绑定 UI(原 select,行为与旧版完全一致)。
   组件只写后端(bindings_runtime.yaml 三段键),变更后派发 cp-persona-changed 供宿主联动。
   用法:
     const el = document.createElement('cp-persona');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey, platform, accountId };
   client 需实现:listPersonas / getPersonaBindings / bindPersona / unbindPersona
     + personaEffective / bindConvPersona / unbindConvPersona(缺失时自动降级 legacy)*/
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-persona: CpPanelBase 未加载"); return; }

  const TIER_KEYS = {
    conv_override: "cp.persona.tier.conv_override",
    account_profile: "cp.persona.tier.account_profile",
    chat_binding: "cp.persona.tier.chat_binding",
    domain: "cp.persona.tier.domain",
    default: "cp.persona.tier.default",
  };

  class CpPersona extends Base {
    constructor() {
      super();
      this._pending = null;   // 待确认的换绑 {pid, toName}
      this._busy = false;
      // select 变更非点击,基类只委托 click,这里补 change 委托(legacy 模式用)
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="persona"]');
        if (s && !s.disabled) this._onSelect(s.value);
      });
      // 搜索:input 事件就地过滤卡片(不重渲染 → 不丢焦点)
      this.shadowRoot.addEventListener("input", (e) => {
        const inp = e.target.closest('input[data-role="psearch"]');
        if (inp) this._filterCards(inp.value);
      });
    }
    emptyText() { return this.t("cp.persona.empty"); }
    emptyDataText() { return this.t("cp.persona.no_data"); }
    errText() { return this.t("cp.persona.err"); }
    styles() {
      return `
      .lbl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:var(--cp-gap-xs,4px); }
      select { width:100%; font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .src { font-size:var(--cp-fs-tiny,11px); margin-top:var(--cp-gap-xs,4px); }
      .src.bound { color:var(--cp-ok,#0f9d75); }
      .src.unbound { color:var(--cp-text-tiny,#94a3b8); }
      /* —— 生效横幅 —— */
      .eff { display:flex; align-items:center; gap:6px; padding:6px 8px; margin-bottom:6px;
             border-radius:var(--cp-radius-sm,6px); background:var(--cp-surface-2,#f8fafc);
             border:1px solid var(--cp-border,#e2e8f0); }
      .eff .nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); color:var(--cp-text,#1e293b);
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .eff .tier { flex:none; font-size:10px; padding:1px 6px; border-radius:999px;
                   background:var(--cp-accent-bg,#eef2ff); color:var(--cp-accent,#4f46e5); }
      .eff.t-conv .tier { background:#ecfdf5; color:#0f9d75; }
      .eff.t-legacy .tier { background:#fffbeb; color:#b45309; }
      .warn { font-size:var(--cp-fs-tiny,11px); color:#b45309; background:#fffbeb;
              border:1px solid #fde68a; border-radius:var(--cp-radius-sm,6px);
              padding:4px 6px; margin-bottom:6px; }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:4px 0 6px; }
      /* —— 卡片选择器 —— */
      .psearch { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
                 padding:4px 8px; margin-bottom:4px; border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); background:var(--cp-surface,#fff);
                 color:var(--cp-text,#1e293b); }
      .plist { max-height:196px; overflow-y:auto; display:flex; flex-direction:column; gap:3px; }
      .pcard { display:flex; align-items:center; gap:6px; padding:5px 8px; cursor:pointer;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); }
      .pcard:hover { border-color:var(--cp-accent,#4f46e5); }
      .pcard.on { border-color:var(--cp-ok,#0f9d75); background:#ecfdf5; }
      .pcard .pnm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pcard .prole { flex:1; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pcard .pmark { flex:none; font-size:10px; color:var(--cp-ok,#0f9d75); }
      .pcard .ptag { flex:none; font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .pcard .pacct { flex:none; font-size:10px; padding:1px 6px; border-radius:999px;
                      border:1px solid var(--cp-border,#e2e8f0); color:var(--cp-text-dim,#64748b);
                      background:var(--cp-surface,#fff); cursor:pointer; visibility:hidden; }
      .pcard:hover .pacct { visibility:visible; }
      .pcard .pacct:hover { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); }
      .pcard.hide { display:none; }
      .nomatch { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); padding:4px 2px; }
      .clr { margin-top:6px; }
      .busy .plist,.busy .clr { pointer-events:none; opacity:.55; }
      /* —— 确认弹窗 —— */
      .cfm-mask { position:fixed; inset:0; background:rgba(15,23,42,.45); z-index:9998; }
      .cfm { position:fixed; left:50%; top:38%; transform:translate(-50%,-50%); z-index:9999;
             width:min(320px,86vw); background:var(--cp-surface,#fff);
             border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius,10px);
             padding:12px; box-shadow:0 12px 32px rgba(15,23,42,.22); }
      .cfm .ct { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); margin-bottom:6px; }
      .cfm .cb { font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151); line-height:1.5; }
      .cfm .cw { font-size:var(--cp-fs-tiny,11px); color:#b45309; background:#fffbeb;
                 border:1px solid #fde68a; border-radius:var(--cp-radius-sm,6px);
                 padding:4px 6px; margin-top:6px; }
      .cfm .cacts { display:flex; gap:6px; justify-content:flex-end; margin-top:10px; }
      .failtip { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:4px; }`;
    }

    async fetchData(ctx) {
      // effective(3 段键才可用) 与 人设清单/legacy 绑定 并行取
      const cid = String(ctx.conversationId || "");
      const canEff = cid.split(":").length >= 3 && this._client.personaEffective;
      const [list, binds, eff] = await Promise.all([
        this._client.listPersonas(),
        this._client.getPersonaBindings().catch(() => null),
        canEff
          ? this._client.personaEffective({ conversationId: cid }).catch(() => null)
          : Promise.resolve(null),
      ]);
      if (list && list.ok === false) return list;
      const summary = (list && list.summary) || [];
      const profiles = (list && list.profiles) || {};
      const bindings = (binds && binds.bindings) || {};
      const bound = (ctx.chatKey && bindings[ctx.chatKey]) || null;
      return {
        ok: true, summary, profiles,
        eff: (eff && eff.ok) ? eff : null,
        boundId: bound ? (bound.id || "") : "",
        boundName: bound ? (bound.name || "") : "",
      };
    }

    /* —— 渲染 —— */

    renderData(d) {
      if (d.eff) return this._renderEffective(d);
      return this._renderLegacy(d);
    }

    _tierChip(tier) {
      const k = TIER_KEYS[tier] || TIER_KEYS.default;
      return this.t(k);
    }

    _renderEffective(d) {
      const esc = (s) => this.esc(s);
      const eff = d.eff;
      const e = eff.effective || {};
      const tier = String(e.tier || "default");
      const tone = tier === "conv_override" ? "t-conv"
        : (tier === "chat_binding" ? "t-legacy" : "");
      const voice = e.has_voice ? " 🎙" : "";
      let html =
        `<div class="lbl">${esc(this.t("cp.persona.eff_label"))}</div>` +
        `<div class="eff ${tone}">` +
        `<span class="nm">${esc(this.t("cp.persona.eff_speaking", { name: (e.name || e.id || "—") + voice }))}</span>` +
        `<span class="tier">${esc(this._tierChip(tier))}</span>` +
        `</div>`;
      if (eff.legacy_suppressed && eff.legacy && eff.legacy.name) {
        html += `<div class="warn">${esc(this.t("cp.persona.legacy_suppressed", { name: eff.legacy.name }))}</div>`;
      }
      if (!eff.enabled) {
        // 开关关:横幅照说真话,下面回落 legacy 绑定 UI(旧语义)
        html += `<div class="hint">${esc(this.t("cp.persona.disabled_hint"))}</div>`;
        html += this._legacyBody(d);
        return html;
      }
      // 覆写选择器:搜索 + 卡片
      const summary = Array.isArray(d.summary) ? d.summary : [];
      const convId = (eff.conv && eff.conv.id) || "";
      const acctId = (eff.account && eff.account.id) || "";
      html += `<div class="lbl">${esc(this.t("cp.persona.conv_pick"))}</div>`;
      if (summary.length > 5) {
        html += `<input class="psearch" data-role="psearch" type="text" placeholder="${esc(this.t("cp.persona.search_ph"))}">`;
      }
      // 「整号」mini 入口（hover 显现）：把整个账号切到该人设——运营拍板不限 master，
      // 但必须能定位 platform+account（RPA 2 段键会话定位不了 → 不渲染入口）。
      const canAcct = !!this._acctRef();
      const cards = summary.map((p) => {
        const on = p.id === convId;
        const isAcct = p.id === acctId;
        const q = `${p.name || ""} ${p.role || ""} ${p.id || ""}`.toLowerCase();
        return `<div class="pcard${on ? " on" : ""}" data-act="pick" data-pid="${esc(p.id)}" data-q="${esc(q)}">` +
          `<span class="pnm">${esc(p.name || p.id)}${p.has_voice ? " 🎙" : ""}</span>` +
          `<span class="prole">${esc(p.role || "")}</span>` +
          (on ? `<span class="pmark">✓</span>` : "") +
          (isAcct && !on ? `<span class="ptag">${esc(this._tierChip("account_profile"))}</span>` : "") +
          (canAcct && !isAcct
            ? `<span class="pacct" data-act="pick-acct" data-pid="${esc(p.id)}" title="${esc(this.t("cp.persona.acct_btn_title"))}">${esc(this.t("cp.persona.acct_btn"))}</span>`
            : "") +
          `</div>`;
      });
      html += `<div class="plist">${cards.join("")}` +
        `<div class="nomatch hide" data-role="nomatch">${esc(this.t("cp.persona.no_match"))}</div></div>`;
      if (convId) {
        html += `<div class="clr"><button data-act="clear">${esc(this.t("cp.persona.clear_override"))}</button></div>`;
      }
      html += `<div class="failtip hide" data-role="failtip">${esc(this.t("cp.persona.switch_fail"))}</div>`;
      html += `<div data-role="cfm-slot"></div>`;
      return html;
    }

    _legacyBody(d) {
      const esc = (s) => this.esc(s);
      const summary = Array.isArray(d.summary) ? d.summary : [];
      const boundId = d.boundId || "";
      const opts = [`<option value="">${esc(this.t("cp.persona.unbound_opt"))}</option>`]
        .concat(summary.map((p) => {
          const label = p.role ? `${p.name} (${p.role})` : (p.name || p.id);
          const sel = p.id === boundId ? " selected" : "";
          return `<option value="${esc(p.id)}"${sel}>${esc(label)}</option>`;
        }));
      const src = boundId
        ? `<div class="src bound">${esc(this.t("cp.persona.bound", { name: d.boundName || boundId }))}</div>`
        : `<div class="src unbound">${esc(this.t("cp.persona.unbound"))}</div>`;
      return `<select data-role="persona">${opts.join("")}</select>` + src;
    }

    _renderLegacy(d) {
      return `<div class="lbl">${this.esc(this.t("cp.persona.label"))}</div>` + this._legacyBody(d);
    }

    /* —— 搜索过滤(就地,不重渲染) —— */
    _filterCards(q) {
      const needle = String(q || "").trim().toLowerCase();
      let vis = 0;
      this.shadowRoot.querySelectorAll(".pcard").forEach((el) => {
        const hit = !needle || (el.getAttribute("data-q") || "").indexOf(needle) >= 0;
        el.classList.toggle("hide", !hit);
        if (hit) vis += 1;
      });
      const nm = this.shadowRoot.querySelector('[data-role="nomatch"]');
      if (nm) nm.classList.toggle("hide", vis > 0);
    }

    /* —— 点击处理 —— */
    onAction(act, el) {
      if (this._busy) return;
      if (act === "pick") this._onPickConv(el.getAttribute("data-pid") || "");
      else if (act === "pick-acct") this._onPickAccount(el.getAttribute("data-pid") || "");
      else if (act === "clear") this._onPickConv("");
      else if (act === "cfm-ok") { const p = this._pending; this._pending = null; this._closeConfirm(); if (p) this._doBindConv(p.pid); }
      else if (act === "cfm-acct") { const p = this._pending; this._pending = null; this._closeConfirm(); if (p) this._doBindAccount(p.pid); }
      else if (act === "cfm-cancel") { this._pending = null; this._closeConfirm(); }
    }

    /* 账号定位（整号切换用）:优先 ctx 显式字段，缺则拆 3 段 conversationId。*/
    _acctRef() {
      const ctx = this._ctx || {};
      let plat = String(ctx.platform || "");
      let acct = String(ctx.accountId || "");
      if (!plat || !acct) {
        const parts = String(ctx.conversationId || "").split(":");
        if (parts.length >= 3) { plat = plat || parts[0]; acct = acct || parts[1]; }
      }
      return (plat && acct) ? { platform: plat, accountId: acct } : null;
    }

    /* 语音能力提示行(换绑弹窗):目标人设没配语音 / 不在自动语音灰度名单 → 预警。
       解除覆写(pid="")回到账号人设=现状,不提示。*/
    _voiceHints(pid) {
      if (!pid) return [];
      const eff = this._d && this._d.eff;
      const summary = (this._d && this._d.summary) || [];
      const p = summary.find((s) => s && s.id === pid)
        || ((this._d && this._d.profiles) || {})[pid] || {};
      const name = p.name || pid;
      const hasVoice = !!(p.has_voice
        || ((p.voice_profile || {}).enabled || (p.voice_profile || {}).backend));
      if (!hasVoice) return [this.t("cp.persona.voice_hint_novoice", { name })];
      const va = (eff && eff.voice_autosend) || {};
      const allow = Array.isArray(va.allowlist) ? va.allowlist : [];
      if (va.enabled && allow.length && allow.indexOf(pid) < 0) {
        return [this.t("cp.persona.voice_hint_gated", { name })];
      }
      return [];
    }

    _onPickConv(pid) {
      const eff = this._d && this._d.eff;
      if (!eff) return;
      const curConv = (eff.conv && eff.conv.id) || "";
      if (pid && pid === curConv) return;          // 点了当前覆写 → 无操作
      if (!pid && !curConv) return;                // 无覆写时点解除 → 无操作
      // 弹窗口径(用户拍板):有既往绑定(覆写/legacy) 或 有出站历史才弹;否则静默直绑
      const hadBinding = !!(eff.conv || eff.legacy);
      if (hadBinding || eff.has_outbound) {
        const fromName = (eff.effective && (eff.effective.name || eff.effective.id)) || "—";
        let toName;
        if (pid) {
          const p = (this._d.profiles || {})[pid] || {};
          toName = p.name || pid;
        } else {
          // 解除覆写 → 回到账号人设/默认
          toName = (eff.account && eff.account.name) || this._tierChip("default");
        }
        this._pending = { pid };
        this._openConfirm({
          pid, fromName, toName,
          hasOutbound: !!eff.has_outbound,
          scopes: (pid && this._acctRef()) ? ["conv", "account"] : ["conv"],
        });
        return;
      }
      this._doBindConv(pid);
    }

    /* 卡片「整号」入口:恒弹确认(影响账号全部会话,无静默路径)。*/
    _onPickAccount(pid) {
      if (!pid || !this._acctRef()) return;
      const eff = this._d && this._d.eff;
      const fromName = (eff && eff.account && (eff.account.name || eff.account.id))
        || (eff && eff.effective && (eff.effective.name || eff.effective.id)) || "—";
      const p = ((this._d && this._d.profiles) || {})[pid] || {};
      this._pending = { pid };
      this._openConfirm({
        pid, fromName, toName: p.name || pid,
        hasOutbound: false, scopes: ["account"],
      });
    }

    _openConfirm(opt) {
      const esc = (s) => this.esc(s);
      const slot = this.shadowRoot.querySelector('[data-role="cfm-slot"]');
      if (!slot) return;
      const scopes = opt.scopes || ["conv"];
      const convScope = scopes.indexOf("conv") >= 0;
      const acctScope = scopes.indexOf("account") >= 0;
      const acct = this._acctRef() || {};
      let body;
      if (!convScope && acctScope) {
        body = this.t("cp.persona.confirm_acct_body",
          { acct: acct.accountId || "?", to: opt.toName });
      } else {
        body = opt.pid
          ? this.t("cp.persona.confirm_switch", { from: opt.fromName, to: opt.toName })
          : this.t("cp.persona.confirm_clear", { to: opt.toName });
      }
      let warn = "";
      if (opt.hasOutbound) {
        warn += `<div class="cw">${esc(this.t("cp.persona.confirm_outbound", { from: opt.fromName }))}</div>`;
      }
      if (acctScope) {
        warn += `<div class="cw">${esc(this.t("cp.persona.confirm_acct_warn"))}</div>`;
      }
      this._voiceHints(opt.pid).forEach((h) => {
        warn += `<div class="cw">${esc(h)}</div>`;
      });
      const btns = [`<button data-act="cfm-cancel">${esc(this.t("cp.persona.confirm_cancel"))}</button>`];
      if (acctScope) {
        btns.push(`<button class="${convScope ? "" : "primary"}" data-act="cfm-acct">${esc(this.t("cp.persona.confirm_acct_btn"))}</button>`);
      }
      if (convScope) {
        btns.push(`<button class="primary" data-act="cfm-ok">${esc(this.t(acctScope ? "cp.persona.confirm_conv_btn" : "cp.persona.confirm_ok"))}</button>`);
      }
      slot.innerHTML =
        `<div class="cfm-mask" data-act="cfm-cancel"></div>` +
        `<div class="cfm">` +
        `<div class="ct">${esc(this.t("cp.persona.confirm_title"))}</div>` +
        `<div class="cb">${esc(body)}</div>` + warn +
        `<div class="cacts">${btns.join("")}</div></div>`;
    }

    _closeConfirm() {
      const slot = this.shadowRoot.querySelector('[data-role="cfm-slot"]');
      if (slot) slot.innerHTML = "";
    }

    async _doBindConv(pid) {
      const ctx = this._ctx || {};
      const cid = String(ctx.conversationId || "");
      if (!cid) return;
      this._busy = true;
      const wrap = this._wrap();
      if (wrap) wrap.classList.add("busy");
      let ok = false;
      try {
        const r = pid
          ? await this._client.bindConvPersona({ conversationId: cid, profileId: pid })
          : await this._client.unbindConvPersona({ conversationId: cid });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this._busy = false;
      if (wrap) wrap.classList.remove("busy");
      // personaName:换绑目标显示名;解除覆写时=回落的账号人设名(宿主 toast 用)
      let pname = "";
      if (pid) {
        const p = (this._d && this._d.profiles || {})[pid] || {};
        pname = p.name || pid;
      } else {
        const eff = this._d && this._d.eff;
        pname = (eff && eff.account && eff.account.name) || "";
      }
      this.emit("cp-persona-changed", {
        scope: "conversation", personaId: pid, personaName: pname, ok,
        conversationId: cid, chatKey: ctx.chatKey || "",
      });
      if (!ok) {
        const tip = this.shadowRoot.querySelector('[data-role="failtip"]');
        if (tip) tip.classList.remove("hide");
        return;
      }
      this.refresh();
    }

    /* —— 账号级整号切换(写 registry meta,影响该账号全部会话;会话覆写不受动) —— */
    async _doBindAccount(pid) {
      const ref = this._acctRef();
      if (!pid || !ref || !this._client.setAccountPersona) return;
      this._busy = true;
      const wrap = this._wrap();
      if (wrap) wrap.classList.add("busy");
      let ok = false;
      try {
        const r = await this._client.setAccountPersona({
          platform: ref.platform, accountId: ref.accountId, profileId: pid,
        });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this._busy = false;
      if (wrap) wrap.classList.remove("busy");
      const p = ((this._d && this._d.profiles) || {})[pid] || {};
      this.emit("cp-persona-changed", {
        scope: "account", personaId: pid, personaName: p.name || pid, ok,
        platform: ref.platform, accountId: ref.accountId,
        conversationId: (this._ctx && this._ctx.conversationId) || "",
        chatKey: (this._ctx && this._ctx.chatKey) || "",
      });
      if (!ok) {
        const tip = this.shadowRoot.querySelector('[data-role="failtip"]');
        if (tip) tip.classList.remove("hide");
        return;
      }
      this.refresh();
    }

    /* —— legacy select(开关关/RPA 2 段键会话,原语义不变) —— */
    async _onSelect(pid) {
      const chatKey = this._ctx && this._ctx.chatKey;
      if (!chatKey) return;
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      if (sel) sel.disabled = true;
      let ok = false;
      try {
        if (pid) {
          const persona = (this._d && this._d.profiles || {})[pid];
          if (!persona) { if (sel) sel.disabled = false; return; }
          const r = await this._client.bindPersona({ chatKey, persona });
          ok = !!(r && r.ok);
        } else {
          const r = await this._client.unbindPersona({ chatKey });
          ok = !!(r && r.ok);
        }
      } catch (e) { ok = false; }
      this.emit("cp-persona-changed", { personaId: pid, chatKey, ok });
      this.refresh();
    }
  }

  if (!customElements.get("cp-persona")) customElements.define("cp-persona", CpPersona);
})(typeof window !== "undefined" ? window : this);
