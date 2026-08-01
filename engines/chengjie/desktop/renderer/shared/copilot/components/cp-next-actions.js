"use strict";
/* 两端共享组件 · 下一步最佳行动 NBA(<cp-next-actions>)
   对等网页端 _loadNextActions:展示推荐动作,template→回填话术(emit cp-fill),
   task/tag/escalate/note/chain→client.executeAction。完成后 emit cp-action-done 并刷新。
   用法:
     const el = document.createElement('cp-next-actions');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getNextActions / executeAction */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-next-actions: CpPanelBase 未加载"); return; }

  const ACCENT = {
    escalate: { bg: "rgba(220,38,38,.10)", border: "var(--cp-danger,#dc2626)" },
    template: { bg: "rgba(13,148,136,.10)", border: "#0d9488" },
  };

  class CpNextActions extends Base {
    emptyText() { return this.t("cp.nba.empty"); }
    emptyDataText() { return this.t("cp.nba.no_data"); }
    styles() {
      return `
      .card.escalate { border-left-color:var(--cp-danger,#dc2626); }
      .card.template { border-left-color:#0d9488; }
      .moodline { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                  padding:3px 2px 5px; }
      .moodline .mv { color:var(--cp-ok,#0f9d75); font-weight:600; }
      .moodline .mnone { color:var(--cp-text-tiny,#94a3b8); }
      .acts button.on { background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; }`;
    }
    async fetchData(ctx) {
      return this._client.getNextActions({ conversationId: ctx.conversationId });
    }
    renderData(d) {
      const acts = Array.isArray(d.actions) ? d.actions : [];
      if (!acts.length) return `<div class="empty">${this.esc(this.emptyDataText())}</div>`;
      const esc = (s) => this.esc(s);
      /* P1-198 情绪标记状态化：顶部常驻「当前情绪」行（服务端 conv_tags∩情绪词表的
         单一事实源），当前生效的情绪 chip 高亮——点完不再「毫无反应」。 */
      const mood = String(d.current_mood || "");
      const moodLine =
        `<div class="moodline">${esc(this.t("cp.nba.mood_current"))}: ` +
        (mood ? `<span class="mv">🏷 ${esc(mood)}</span>`
              : `<span class="mnone">${esc(this.t("cp.nba.mood_none"))}</span>`) +
        `</div>`;
      return moodLine + acts.map((a, i) => {
        const t = a.action_type;
        const cls = t === "escalate" ? "card escalate" : t === "template" ? "card template" : "card";
        const btns = [];
        if (t === "template" && a.config && a.config.template_text) {
          btns.push(`<button data-act="fill" data-idx="${i}">${esc(this.t("cp.nba.use_template"))}</button>`);
        }
        if (t === "task") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="task">${esc(this.t("cp.nba.create_task"))}</button>`);
        if (t === "tag" && a.config && (a.config.tag || a.config.tag_options)) {
          const opts = (a.config.tag_options || [a.config.tag]).filter(Boolean);
          opts.forEach((tag) => {
            const on = mood && tag === mood ? " class=\"on\"" : "";
            btns.push(`<button${on} data-act="exec" data-idx="${i}" data-kind="tag" data-tag="${esc(tag)}">🏷 ${esc(tag)}</button>`);
          });
        }
        if (t === "escalate") btns.push(`<button class="danger" data-act="exec" data-idx="${i}" data-kind="escalate">${esc(this.t("cp.nba.escalate"))}</button>`);
        if (t === "note") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="note">${esc(this.t("cp.nba.add_note"))}</button>`);
        if (t === "chain") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="chain">${esc(this.t("cp.nba.start_chain"))}</button>`);
        return `<div class="${cls}">` +
          `<div class="title">${esc(a.icon || "💡")} ${esc(a.name || "")}</div>` +
          (a.reason ? `<div class="reason">${esc(a.reason)}</div>` : "") +
          (btns.length ? `<div class="acts">${btns.join("")}</div>` : "") +
          `</div>`;
      }).join("");
    }
    onAction(act, el) {
      const idx = parseInt(el.getAttribute("data-idx"), 10);
      const a = (this._d && this._d.actions || [])[idx];
      if (!a) return;
      if (act === "fill") {
        const text = (a.config && a.config.template_text) || "";
        if (text) this.emit("cp-fill", { text, source: "nba" });
        return;
      }
      if (act === "exec") {
        const kind = el.getAttribute("data-kind") || a.action_type;
        let config = Object.assign({}, a.config || {});
        if (kind === "tag") config = { tag: el.getAttribute("data-tag") || config.tag };
        if (kind === "note") {
          const body = (typeof prompt === "function") ? prompt(this.t("cp.nba.note_prompt")) : "";
          if (!body || !body.trim()) return;
          config = { note_body: body.trim() };
        }
        this._exec(a.action_id || "", kind, config);
      }
    }
    async _exec(action_id, action_type, config) {
      const cid = this._ctx && this._ctx.conversationId;
      if (!cid) return;
      this.shadowRoot.querySelectorAll("[data-act]").forEach((b) => (b.disabled = true));
      let ok = false;
      let err = "";
      try {
        const r = await this._client.executeAction({ conversationId: cid, action_id, action_type, config });
        ok = !!(r && r.ok);
        // P1-198 诚实化：服务端如实报「没建成」的原因（如会话未关联客户档案），
        // 透传给宿主 toast——「点了没反应」变成「点了知道为什么不行」。
        if (!ok) err = String((r && (r.error || r.detail)) || "");
      } catch (e) { ok = false; }
      this.emit("cp-action-done", { action_type, ok, error: err, conversationId: cid });
      this.refresh();
    }
  }

  if (!customElements.get("cp-next-actions")) customElements.define("cp-next-actions", CpNextActions);
})(typeof window !== "undefined" ? window : this);
