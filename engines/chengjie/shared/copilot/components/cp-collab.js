"use strict";
/* 两端共享组件 · 协作上下文(<cp-collab>)— 继承 CpPanelBase
   对等网页端 _loadCollabContext:阶段/积分/运行中工作链 chips
   + 跨会话同事注解数。
   P1-4 第二刀（2026-08-12）：opt-in 内部注解子分区——宿主带 `notes` 属性才渲染
   （统一 App 挂 <cp-collab notes>；网页原生右栏保持宿主内联注解实现，不带属性=
   零重复零回归）。数据零新读端点：collab-context 响应自带 recent_notes（最近 5 条
   完整对象）；写入走 addConvNote；@提及通知选人/编辑/删除留在网页端（卡内小字指路）。
   用法:
     const el = document.createElement('cp-collab');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getCollabContext（notes 模式另需 addConvNote） */
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
      .notes { margin-top:4px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      /* ── notes 子分区（opt-in）── */
      .nsec { margin-top:8px; padding-top:8px; border-top:1px dashed var(--cp-border,#e2e8f0); }
      .nsec-ttl { font-size:var(--cp-fs-tiny,11px); font-weight:600; color:var(--cp-text-dim,#64748b);
                  margin-bottom:5px; display:flex; align-items:center; gap:5px; }
      .nsec-ttl .cnt { font-weight:400; color:var(--cp-text-tiny,#94a3b8); }
      .nitem { display:flex; gap:6px; align-items:flex-start; padding:4px 0;
               border-bottom:1px solid var(--cp-border,#eef2f7); }
      .nitem:last-child { border-bottom:0; }
      .nav-av { flex-shrink:0; width:20px; height:20px; border-radius:50%; color:#fff;
                display:flex; align-items:center; justify-content:center;
                font-size:10px; font-weight:700; user-select:none; }
      .nbody { flex:1; min-width:0; }
      .nmeta { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .nmeta .who { font-weight:600; color:var(--cp-text-dim,#64748b); }
      .ntext { font-size:var(--cp-fs-sm,12px); line-height:1.45; word-break:break-word; white-space:pre-wrap; }
      .ntext .mention { color:var(--cp-accent,#4f46e5); font-weight:600; }
      .nempty { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); line-height:1.5; padding:2px 0 4px; }
      .note-ta { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
                 border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
                 background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b);
                 padding:5px 7px; margin-top:6px; resize:vertical; min-height:40px; }
      .nfoot { display:flex; align-items:center; gap:6px; margin-top:5px; }
      .nhint { flex:1; font-size:10px; color:var(--cp-text-tiny,#94a3b8); line-height:1.4; }`;
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

      const notesHtml = notes && !this.hasAttribute("notes")
        ? `<div class="notes">${esc(this.t("cp.collab.notes", { n: notes }))}</div>` : "";
      return `<div class="chips">${chips || '<span class="chip">—</span>'}</div>` + notesHtml +
        (this.hasAttribute("notes") ? this._notesHtml(d) : "");
    }

    /* ── notes 子分区（opt-in）：列表（recent_notes 最近 5 条）+ 添加输入 ── */
    _noteHue(s) {
      s = String(s || "?"); let h = 0;
      for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
      return h % 360;
    }
    _noteAgo(tsSec) {
      if (!tsSec) return "";
      const diff = Date.now() - tsSec * 1000;
      if (diff < 60000) return this.t("cp.collab.note_just");
      if (diff < 3600000) return this.t("cp.collab.note_ago_min", { n: Math.floor(diff / 60000) });
      if (diff < 86400000) return this.t("cp.collab.note_ago_hr", { n: Math.floor(diff / 3600000) });
      try { return new Date(tsSec * 1000).toLocaleDateString(); } catch (_e) { return ""; }
    }
    _notesHtml(d) {
      const esc = (s) => this.esc(s);
      const items = (d.recent_notes || []).slice(0, 5).map((n) => {
        const who = n.agent_name || n.agent_id || "?";
        const init = String(who).charAt(0).toUpperCase();
        const body = esc(n.body).replace(/@(\S+)/g, '<span class="mention">@$1</span>');
        return `<div class="nitem">` +
          `<span class="nav-av" style="background:hsl(${this._noteHue(who)},45%,45%)">${esc(init)}</span>` +
          `<div class="nbody"><div class="nmeta"><span class="who">${esc(who)}</span> · ${esc(this._noteAgo(n.ts))}</div>` +
          `<div class="ntext">${body}</div></div></div>`;
      }).join("");
      return `<div class="nsec">` +
        `<div class="nsec-ttl">${esc(this.t("cp.collab.notes_title"))}</div>` +
        (items || `<div class="nempty">${esc(this.t("cp.collab.notes_empty"))}</div>`) +
        `<textarea class="note-ta" rows="2" placeholder="${esc(this.t("cp.collab.note_ph"))}"></textarea>` +
        `<div class="nfoot"><span class="nhint">${esc(this.t("cp.collab.notes_hint"))}</span>` +
        `<button class="primary" data-act="note-add">${esc(this.t("cp.collab.note_add"))}</button></div>` +
        `</div>`;
    }
    _render(html) {
      super._render(html);
      // Ctrl+Enter 快捷发送（与网页端注解输入同习惯）；每次重渲后旧节点连监听一起废弃，无泄漏
      const ta = this.shadowRoot.querySelector(".note-ta");
      if (ta) ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); this._submitNote(); }
      });
    }
    async _submitNote() {
      const ctx = this._ctx;
      const ta = this.shadowRoot.querySelector(".note-ta");
      const btn = this.shadowRoot.querySelector('[data-act="note-add"]');
      if (!ta || !ctx || !this._client || !this._client.addConvNote) return;
      const body = String(ta.value || "").trim();
      if (!body) return;
      if (btn) btn.disabled = true;
      try {
        const r = await this._client.addConvNote({ conversationId: ctx.conversationId, body: body });
        if (!r || r.ok === false) throw new Error("add note failed");
        ta.value = "";
        this.emit("cp-note-added", { conversationId: ctx.conversationId });
        this.refresh();   // 重取 collab-context（recent_notes 已含新注解）
      } catch (_e) {
        if (btn) {
          const t0 = btn.textContent;
          btn.textContent = this.t("cp.collab.note_fail");
          setTimeout(() => { if (btn.isConnected) { btn.textContent = t0; btn.disabled = false; } }, 2200);
          return;
        }
      }
      if (btn) btn.disabled = false;
    }
    onAction(act, _el) {
      if (act === "note-add") this._submitNote();
    }
  }

  if (!customElements.get("cp-collab")) customElements.define("cp-collab", CpCollab);
})(typeof window !== "undefined" ? window : this);
