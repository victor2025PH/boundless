"use strict";
/* 两端共享组件 · 协作上下文(<cp-collab>)— 继承 CpPanelBase
   对等网页端 _loadCollabContext:阶段/积分/运行中工作链 chips
   + 跨会话同事注解数。只读。
   用法:
     const el = document.createElement('cp-collab');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getCollabContext */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-collab: CpPanelBase 未加载"); return; }

  class CpCollab extends Base {
    emptyText() { return this.t("cp.collab.empty"); }
    emptyDataText() { return this.t("cp.collab.no_data"); }
    errText() { return this.t("cp.collab.err"); }
    styles() {
      return `
      .chips { display:flex; gap:var(--cp-gap-xs,4px); flex-wrap:wrap; }
      .chip { display:inline-block; padding:2px 8px; border-radius:99px;
              font-size:var(--cp-fs-tiny,11px); background:var(--cp-surface-2,#f1f5f9);
              color:var(--cp-text-dim,#475569); }
      .chip.warn { background:rgba(217,119,6,.14); color:var(--cp-warn,#92400e); }
      /* 关系阶段四档分级配色（初识=灰 / 升温=琥珀 / 暧昧=紫 / 稳定=绿）：
         半透明底 + currentColor 圆点，日夜两套主题下均可读。 */
      .chip.stage { display:inline-flex; align-items:center; gap:5px; font-weight:600; }
      .chip.stage::before { content:""; width:7px; height:7px; border-radius:50%;
              background:currentColor; opacity:.85; flex-shrink:0; }
      .chip.stage-initial { background:rgba(100,116,139,.14); color:var(--cp-text-dim,#475569); }
      .chip.stage-warming { background:rgba(217,119,6,.14); color:var(--cp-warn,#b45309); }
      .chip.stage-intimate { background:rgba(124,58,237,.13); color:var(--cp-violet,#7c3aed); }
      .chip.stage-steady { background:rgba(5,150,105,.13); color:var(--cp-ok,#059669); }
      .notes { margin-top:4px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }`;
    }

    async fetchData(ctx) {
      return this._client.getCollabContext({ conversationId: ctx.conversationId });
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      const rel = d.relationship || {};
      const stageLbl = d.contact_stage_label || rel.display_stage_label || rel.stage_label || "";
      const stageId = String(d.contact_stage || rel.display_stage || rel.stage || "").toLowerCase();
      const stageCls = /^(initial|warming|intimate|steady)$/.test(stageId) ? " stage-" + stageId : "";
      // 双信号悬浮提示：阶段判定的依据（互动轮次 + 亲密度）就地可见
      const ex = rel.exchange_count;
      const sc = rel.intimacy_score;
      let stageTitle = "";
      if (ex != null && sc != null) stageTitle = this.t("cp.collab.stage_sig", { ex: ex, sc: Math.round(sc) });
      else if (ex != null) stageTitle = this.t("cp.collab.stage_sig_ex", { ex: ex });
      const chains = (d.active_chains || []).length;
      const eng = d.engagement || {};
      const pts = eng.points != null ? this.t("cp.collab.points", { pts: esc(eng.points), level: esc(eng.level_name || "") }) : "";
      const notes = (d.recent_notes || []).length;

      const chips =
        (stageLbl ? `<span class="chip stage${stageCls}"${stageTitle ? ` title="${esc(stageTitle)}"` : ""}>${esc(this.t("cp.collab.stage", { stage: stageLbl }))}</span>` : "") +
        (d.stage_conflict ? `<span class="chip warn">${esc(this.t("cp.collab.stage_conflict"))}</span>` : "") +
        (pts ? `<span class="chip">${pts}</span>` : "") +
        (chains ? `<span class="chip">${esc(this.t("cp.collab.chains", { n: chains }))}</span>` : "");

      const notesHtml = notes ? `<div class="notes">${esc(this.t("cp.collab.notes", { n: notes }))}</div>` : "";
      return `<div class="chips">${chips || '<span class="chip">—</span>'}</div>` + notesHtml;
    }
  }

  if (!customElements.get("cp-collab")) customElements.define("cp-collab", CpCollab);
})(typeof window !== "undefined" ? window : this);
