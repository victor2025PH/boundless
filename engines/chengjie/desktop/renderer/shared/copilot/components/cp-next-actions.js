"use strict";
/* 两端共享组件 · 下一步最佳行动 NBA(<cp-next-actions>)
   对等网页端 _loadNextActions:展示推荐动作,template→回填话术(emit cp-fill),
   task/tag/escalate/note/chain→client.executeAction。完成后 emit cp-action-done 并刷新。
   P1-198 续(2026-08-02) 情绪标注转向：
   - 「标记客户情绪状态」卡按服务端 tag_groups 分两组渲染（客户情绪/跟进状态，
     组内互斥、跨组共存），chip 显示服务端按请求语言出的本地化 label、提交仍用
     canonical value；
   - 顶部常驻「当前情绪」行 + 转向徽标（AI 引导中/已过时效——与服务端 effective_mood
     仲裁同源，绝不前端另算一套）+「AI 检测」行（机器 last_emotion，让坐席看见
     人工标注覆写的是什么）；
   - 旧后端（无 tag_groups/mood_manual/ai_emotion 字段）自动退化为旧版扁平 chips。
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
      .moodline .aiv { color:var(--cp-text,#334155); font-weight:600; }
      .moodline .steer { font-size:10px; padding:1px 6px; border-radius:8px; margin-left:4px;
                         background:rgba(15,157,117,.12); color:var(--cp-ok,#0f9d75); }
      .moodline .steer.off { background:rgba(148,163,184,.15); color:var(--cp-text-tiny,#94a3b8); }
      .acts { flex-wrap:wrap; }
      .acts .grp { display:inline-flex; align-items:center; gap:4px; flex-wrap:wrap;
                   margin:2px 8px 2px 0; }
      .acts .grplab { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .acts button.on { background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; }
      .notebox { margin-top:6px; }
      .notebox textarea { width:100%; box-sizing:border-box; resize:vertical;
        font:inherit; font-size:var(--cp-fs-tiny,11px); padding:4px 6px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:6px;
        background:var(--cp-bg,#fff); color:var(--cp-text,#334155); }
      .notebox .nb-acts { margin-top:4px; display:flex; gap:6px; }`;
    }
    async fetchData(ctx) {
      return this._client.getNextActions({ conversationId: ctx.conversationId });
    }
    renderData(d) {
      const acts = Array.isArray(d.actions) ? d.actions : [];
      if (!acts.length) return `<div class="empty">${this.esc(this.emptyDataText())}</div>`;
      const esc = (s) => this.esc(s);
      const mood = String(d.current_mood || "");
      const attention = String(d.current_attention || "");
      const mm = d.mood_manual || null;
      const ai = d.ai_emotion || null;
      /* 标签值(存储 canonical) → 本地化展示名：来自服务端 tag_groups[].options[].label，
         没有映射（旧后端/自定义标签）就原样显示。 */
      const optLabel = {};
      acts.forEach((a) => {
        const groups = (a && a.config && a.config.tag_groups) || [];
        (Array.isArray(groups) ? groups : []).forEach((g) => {
          ((g && g.options) || []).forEach((o) => {
            if (o && typeof o === "object" && o.value) optLabel[o.value] = o.label || o.value;
          });
        });
      });
      const disp = (v) => optLabel[v] || v;
      /* P1-198 情绪标记状态化：顶部常驻「当前情绪」行（服务端 conv_tags∩情绪词表的
         单一事实源）+ 转向徽标（生效判据与消费链同源）+「AI 检测」行。 */
      let moodLine = `<div class="moodline">${esc(this.t("cp.nba.mood_current"))}: `;
      if (mood) {
        moodLine += `<span class="mv">🏷 ${esc(disp(mood))}</span>`;
        if (mm && String(mm.tag || "") === mood) {
          if (mm.active) {
            moodLine += `<span class="steer">${esc(this.t("cp.nba.steer_on"))}</span>`;
          } else if (mm.age_hours != null) {
            moodLine += `<span class="steer off">${esc(this.t("cp.nba.steer_off"))}</span>`;
          }
          if (mm.age_hours != null) {
            moodLine += ` <span class="mnone">· ${esc(String(mm.age_hours))}h</span>`;
          }
        }
      } else {
        moodLine += `<span class="mnone">${esc(this.t("cp.nba.mood_none"))}</span>`;
      }
      moodLine += `</div>`;
      if (ai && String(ai.label || "")) {
        const trend = { rising: "↗", falling: "↘" }[String(ai.trend || "")] || "";
        const inten = (typeof ai.intensity === "number" && ai.intensity >= 0)
          ? " " + Number(ai.intensity).toFixed(1) : "";
        moodLine += `<div class="moodline">${esc(this.t("cp.nba.ai_detect"))}: ` +
          `<span class="aiv">${esc(String(ai.label))}${esc(inten)}${trend ? " " + trend : ""}</span></div>`;
      }
      return moodLine + acts.map((a, i) => {
        const t = a.action_type;
        const cls = t === "escalate" ? "card escalate" : t === "template" ? "card template" : "card";
        const btns = [];
        if (t === "template" && a.config && a.config.template_text) {
          btns.push(`<button data-act="fill" data-idx="${i}">${esc(this.t("cp.nba.use_template"))}</button>`);
        }
        if (t === "task") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="task">${esc(this.t("cp.nba.create_task"))}</button>`);
        if (t === "tag" && a.config && (a.config.tag_groups || a.config.tag || a.config.tag_options)) {
          const groups = Array.isArray(a.config.tag_groups) && a.config.tag_groups.length
            ? a.config.tag_groups : null;
          if (groups) {
            /* 分组渲染：emotion 组按 current_mood 高亮、attention 组按 current_attention
               高亮（旧版只认 mood，attention 标记永远不亮）。 */
            groups.forEach((g) => {
              const cur = String((g && g.key) || "") === "attention" ? attention : mood;
              const inner = ((g && g.options) || []).map((o) => {
                const val = (o && typeof o === "object") ? String(o.value || "") : String(o || "");
                const lab = (o && typeof o === "object") ? String(o.label || val) : val;
                if (!val) return "";
                const on = cur && val === cur ? " class=\"on\"" : "";
                return `<button${on} data-act="exec" data-idx="${i}" data-kind="tag" data-tag="${esc(val)}">🏷 ${esc(lab)}</button>`;
              }).join("");
              const lab = String((g && g.label) || "");
              btns.push(`<span class="grp">${lab ? `<span class="grplab">${esc(lab)}</span>` : ""}${inner}</span>`);
            });
          } else {
            const opts = (a.config.tag_options || [a.config.tag]).filter(Boolean);
            opts.forEach((tag) => {
              const on = (mood && tag === mood) || (attention && tag === attention) ? " class=\"on\"" : "";
              btns.push(`<button${on} data-act="exec" data-idx="${i}" data-kind="tag" data-tag="${esc(tag)}">🏷 ${esc(disp(tag))}</button>`);
            });
          }
        }
        if (t === "escalate") btns.push(`<button class="danger" data-act="exec" data-idx="${i}" data-kind="escalate">${esc(this.t("cp.nba.escalate"))}</button>`);
        /* note：卡内 inline 输入框（2026-08-02——window.prompt 桌面壳 Electron 不支持，
           点了静默无反应；inline 两端可用，能力回归的前提）。 */
        if (t === "note") btns.push(`<button data-act="note-open" data-idx="${i}">${esc(this.t("cp.nba.add_note"))}</button>`);
        if (t === "chain") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="chain">${esc(this.t("cp.nba.start_chain"))}</button>`);
        const noteBox = (t === "note")
          ? `<div class="notebox" data-notebox="${i}" hidden>` +
            `<textarea rows="2" placeholder="${esc(this.t("cp.nba.note_prompt"))}"></textarea>` +
            `<div class="nb-acts">` +
            `<button data-act="note-save" data-idx="${i}">${esc(this.t("cp.common.save"))}</button>` +
            `<button data-act="note-cancel" data-idx="${i}">${esc(this.t("cp.common.cancel"))}</button>` +
            `</div></div>`
          : "";
        return `<div class="${cls}">` +
          `<div class="title">${esc(a.icon || "💡")} ${esc(a.name || "")}</div>` +
          (a.reason ? `<div class="reason">${esc(a.reason)}</div>` : "") +
          (btns.length ? `<div class="acts">${btns.join("")}</div>` : "") +
          noteBox +
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
      if (act === "note-open" || act === "note-cancel" || act === "note-save") {
        const box = this.shadowRoot.querySelector(`[data-notebox="${idx}"]`);
        if (!box) return;
        if (act === "note-open") {
          box.hidden = false;
          const ta = box.querySelector("textarea");
          if (ta) ta.focus();
          return;
        }
        if (act === "note-cancel") { box.hidden = true; return; }
        const ta = box.querySelector("textarea");
        const body = ta && ta.value ? ta.value.trim() : "";
        if (!body) return;
        this._exec(a.action_id || "", "note", { note_body: body });
        return;
      }
      if (act === "exec") {
        const kind = el.getAttribute("data-kind") || a.action_type;
        let config = Object.assign({}, a.config || {});
        if (kind === "tag") config = { tag: el.getAttribute("data-tag") || config.tag };
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
