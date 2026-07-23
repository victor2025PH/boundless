"use strict";
/* 两端共享组件 · 关系阶段(<cp-rel-stage>)— 继承 CpPanelBase
   与网页端 _loadRelationshipStage 功能对等:全字段展示 + 确认进阶/确认回暖/
   手动降级/客户级对齐。动作完成后刷新并派发 cp-rel-changed 事件供宿主联动。
   用法:
     const el = document.createElement('cp-rel-stage');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getRelStage / confirmStage / downgradeStage / reunionStage / syncContactStage */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-rel-stage: CpPanelBase 未加载"); return; }

  class CpRelStage extends Base {
    emptyText() { return this.t("cp.rel.empty"); }
    emptyDataText() { return this.t("cp.rel.no_data"); }
    errText() { return this.t("cp.rel.err"); }
    styles() {
      return `
      .cta { border-radius:9px; padding:8px 10px; margin-bottom:var(--cp-gap-sm,6px);
             font-size:var(--cp-fs-sm,12px); line-height:1.5; display:flex;
             flex-direction:column; gap:var(--cp-gap-sm,6px); }
      .cta.warn { background:color-mix(in srgb,var(--cp-warn,#d97706) 10%,transparent);
                  border:1px solid color-mix(in srgb,var(--cp-warn,#d97706) 35%,transparent);
                  color:var(--cp-warn,#d97706); }
      .cta.danger { background:color-mix(in srgb,var(--cp-danger,#dc2626) 8%,transparent);
                    border:1px solid color-mix(in srgb,var(--cp-danger,#dc2626) 35%,transparent);
                    color:var(--cp-danger,#dc2626); }
      .cta .acts { margin-top:0; }
      .hdr { display:flex; align-items:baseline; gap:6px; flex-wrap:wrap; }
      .cur { font-weight:var(--cp-fw-bold,700); font-size:var(--cp-fs-lg,15px); color:var(--cp-accent,#4f46e5); }
      .nxt { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-tiny,#94a3b8); }
      .stp { display:flex; align-items:center; margin:var(--cp-gap-sm,6px) 0 2px; padding:3px 1px; }
      .nd { width:9px; height:9px; border-radius:50%; background:var(--cp-track,#e2e8f0); flex:0 0 auto; }
      .nd.done { background:var(--cp-ok,#0f9d75); }
      .nd.cur { background:var(--cp-accent,#4f46e5);
                box-shadow:0 0 0 3px color-mix(in srgb,var(--cp-accent,#4f46e5) 22%,transparent); }
      .nd.pend { background:var(--cp-warn,#d97706); }
      .ln { flex:1; height:2px; background:var(--cp-track,#e2e8f0); margin:0 2px; }
      .ln.done { background:var(--cp-ok,#0f9d75); }
      .track { height:6px; border-radius:99px; background:var(--cp-track,#e2e8f0);
               margin:var(--cp-gap-sm,6px) 0; overflow:hidden; }
      .bar { height:100%; border-radius:99px; background:var(--cp-accent,#4f46e5);
             width:0; transition:width var(--cp-dur,.2s) ease; }
      .meta { display:flex; gap:4px 10px; flex-wrap:wrap; font-size:var(--cp-fs-sm,12px);
              color:var(--cp-text-dim,#64748b); margin-top:var(--cp-gap-xs,4px); }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-top:var(--cp-gap-xs,4px); }
      .hint.contact { color:var(--cp-accent,#4f46e5); }
      .rbadge { display:inline-block; margin-top:var(--cp-gap-xs,4px); padding:2px 8px;
                border-radius:99px; font-size:var(--cp-fs-tiny,11px);
                background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .foot { display:flex; justify-content:flex-end; margin-top:var(--cp-gap-sm,6px); }
      button.warn { color:var(--cp-warn,#d97706); }`;
    }

    async fetchData(ctx) {
      return this._client.getRelStage({ conversationId: ctx.conversationId });
    }

    /* 2026-07 重设计：同一事实只说一次——
       ① 顶部 CTA 横幅（待确认进阶/口径冲突/回暖 收成一条可行动横幅，不再 hint+badge+按钮三处重复）
       ② 当前阶段 + 下一步 一行；③ 单一 stepper（有阶段列表用节点线，否则退化进度条）
       ④ 进度/轮次/亲密度 单行 meta；⑤ 降级收进右下角小按钮 */
    renderData(d) {
      const esc = (s) => this.esc(s);
      const pct = Math.max(0, Math.min(100, Math.round(d.progress_pct || 0)));

      const lines = [];
      const acts = [];
      let tone = "warn";
      if (d.stage_conflict) {
        const detail = d.stage_conflict_detail || {};
        const reasons = (detail.reasons || []).join(this.t("cp.common.list_sep")) || this.t("cp.rel.multi_conflict");
        lines.push(`⚠ ${esc(reasons)}`);
        const contactId = (d.context && d.context.contact_id) || "";
        if (contactId) {
          if (detail.show_to_contact !== false && detail.contact_stage) {
            acts.push(`<button data-act="sync_contact">${esc(this.t("cp.rel.sync_contact"))}</button>`);
          }
          if (detail.show_to_highest) {
            const hLbl = detail.highest_stage_label || detail.highest_stage || this.t("cp.rel.highest");
            acts.push(`<button class="primary" data-act="sync_highest">${esc(this.t("cp.rel.sync_highest", { label: hLbl }))}</button>`);
          }
          if (!detail.show_to_highest && !(detail.show_to_contact !== false && detail.contact_stage)) {
            acts.push(`<button data-act="sync_contact">${esc(this.t("cp.rel.sync_one"))}</button>`);
          }
        }
      }
      if (d.needs_confirmation) {
        if (d.computed_stage_label) lines.push(esc(this.t("cp.rel.algo_suggest", { label: d.computed_stage_label })));
        else lines.push(esc(this.t("cp.rel.pending_adv", { label: d.pending_stage_label || "" })));
        acts.push(`<button class="primary" data-act="confirm">${esc(this.t("cp.rel.confirm"))}</button>`);
      } else if (d.pending_advancement) {
        lines.push(esc(this.t("cp.rel.pending_adv", { label: d.pending_stage_label || "" })));
      }
      if (d.reunion) {
        if (!lines.length) tone = "danger";
        lines.push(esc(this.t("cp.rel.reunion_badge")));
        acts.push(`<button data-act="reunion">${esc(this.t("cp.rel.reunion"))}</button>`);
      }
      const banner = lines.length
        ? `<div class="cta ${tone}"><div>${lines.join("<br>")}</div>` +
          (acts.length ? `<div class="acts">${acts.join("")}</div>` : "") + `</div>`
        : "";

      const stages = Array.isArray(d.stages) ? d.stages : [];
      let stepper = "";
      if (stages.length) {
        const parts = [];
        stages.forEach((s, i) => {
          if (i) parts.push(`<div class="ln${stages[i - 1].done ? " done" : ""}"></div>`);
          const cls = s.pending ? "nd pend" : s.done ? "nd done" : s.active ? "nd cur" : "nd";
          parts.push(`<div class="${cls}" title="${esc(s.label)}${s.pending ? esc(this.t("cp.rel.pending_suffix")) : ""}"></div>`);
        });
        stepper = `<div class="stp">${parts.join("")}</div>`;
      } else {
        stepper = `<div class="track"><div class="bar" style="width:${pct}%"></div></div>`;
      }

      const intim = d.intimacy_score != null
        ? this.t("cp.rel.intimacy", { n: Math.round(d.intimacy_score) }) : this.t("cp.rel.intimacy_na");
      const contactHint = d.contact_stage_label
        ? `<div class="hint contact">${esc(this.t("cp.rel.contact_level", { label: d.contact_stage_label }))}` +
          (d.contact_updated_by ? esc(this.t("cp.rel.updated_by", { by: d.contact_updated_by })) : "") + `</div>`
        : "";
      const foot = (d.confirmed_stage && d.confirmed_stage !== "initial")
        ? `<div class="foot"><button class="warn" data-act="downgrade">${esc(this.t("cp.rel.downgrade"))}</button></div>` : "";

      return (
        banner +
        `<div class="hdr"><span class="cur">${esc(d.display_stage_label || d.stage_label || "—")}</span>` +
        (d.next_stage_label ? `<span class="nxt">→ ${esc(d.next_stage_label)}</span>` : "") + `</div>` +
        stepper +
        `<div class="meta"><span>${esc(this.t("cp.rel.progress", { pct }))}</span>` +
        `<span>${esc(this.t("cp.rel.rounds", { n: d.exchange_count || 0 }))}</span>` +
        `<span>${esc(intim)}</span></div>` +
        contactHint +
        (!banner && d.advancement_ready ? `<div class="rbadge">${esc(this.t("cp.rel.adv_ready"))}</div>` : "") +
        foot
      );
    }

    async onAction(action, _el) {
      const ctx = this._ctx, d = this._d || {};
      if (!this._client || !ctx || !ctx.conversationId) return;
      const cid = ctx.conversationId;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      try {
        if (action === "confirm") {
          await this._client.confirmStage({ conversationId: cid });
        } else if (action === "reunion") {
          await this._client.reunionStage({ conversationId: cid });
        } else if (action === "downgrade") {
          const reason = (typeof prompt === "function") ? prompt(this.t("cp.rel.downgrade_prompt")) : "";
          if (!reason || !reason.trim()) {
            this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = false));
            return;
          }
          await this._client.downgradeStage({ conversationId: cid, reason: reason.trim() });
        } else if (action === "sync_contact" || action === "sync_highest") {
          const contactId = (d.context && d.context.contact_id) || "";
          if (!contactId) return;
          await this._client.syncContactStage({
            contactId, mode: action === "sync_highest" ? "to_highest" : "to_contact",
          });
        }
        this.emit("cp-rel-changed", { action, conversationId: cid });
        this.refresh();
      } catch (e) {
        this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = false));
      }
    }
  }

  if (!customElements.get("cp-rel-stage")) customElements.define("cp-rel-stage", CpRelStage);
})(typeof window !== "undefined" ? window : this);
