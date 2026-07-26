"use strict";
/* 两端共享组件 · 工作目标(<cp-goal>)— 继承 CpPanelBase
   统一收件箱右栏「工作目标」卡（对接 /api/goals*，后端 goal_routes.py）：
   - 无目标：引导文案 + 内联建目标表单（模板/参数/期限/自治档）
   - 进行中(active/paused)：标题+状态、4 段里程碑条、第X/Y天+进度、今日意图(push pill / hold 提示)、
     暂停/恢复/放弃
   - 今日拍坐席反馈（P2）：采纳(✓ 正向信号) / 驳回(今日拍置 skipped 停注入，
     回流 planner 明日降档)；已驳回 → 显示「今天只陪伴」行
   - 终态(done/failed/expired/cancelled)：结果 badge + result + 「再设一个」
   数据走同源 fetch（浏览器 cookie session；本组件不依赖 copilot-client 方法）。
   功能未开(companion.goals.enabled=false → 全端点 403)时自动隐藏宿主整卡；
   网络错误安静重试一次后显示可点重试的轻量错误行。
   文案：inbox.goal.* 键在宿主整包词典（src/web/i18n_packs/goals.py → window.T/Tf）；
   共享键(cp.common.* 与 cp.base.*)仍走 CopilotShared.t（cp-i18n.js）。
   用法:
     const el = document.createElement('cp-goal');
     el.context = { conversationId };   // 宿主会话切换时重设 context 即自动重拉 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-goal: CpPanelBase 未加载"); return; }

  const LANG = (root.CopilotShared && root.CopilotShared.lang) === "en" ? "en" : "zh";
  const TERMINAL = { done: 1, failed: 1, expired: 1, cancelled: 1 };
  const AUTONOMY = ["observe", "suggest", "auto"];
  const PUSH_LEVELS = ["none", "soft", "direct"];
  const HOLDS = ["emotion", "silent", "no_intent"];

  class CpGoal extends Base {
    constructor() {
      super();
      // 本组件自带同源 fetch，不调用 client 方法；给个占位对象满足基类
      // 「client 已设才拉数」的守卫，宿主再注入真 client 也不冲突。
      this._client = this._client || {};
      this._templates = null;   // GET /api/goals/templates 缓存（模板库全局，跨会话复用）
      this._formOpen = false;
      this._formTid = "";
      this._lastCid = "";
      // 表单下拉联动走 change 委托（与基类 click 委托同模式；不用内联 handler）。
      this.shadowRoot.addEventListener("change", (e) => {
        const t = e.target;
        if (!t || !t.getAttribute) return;
        const chg = t.getAttribute("data-chg");
        if (chg === "template") this._syncFormTemplate();
        else if (chg === "autonomy") this._syncAutonomyHint();
      });
    }

    /* inbox.goal.* 词条在宿主整包（web_i18n pack goals.py，经 window.T/Tf 注入页面）——
       cp-i18n.js 词典不含该域；其余键回落基类（CopilotShared.t）。 */
    t(key, vars) {
      if (key && key.indexOf("inbox.goal.") === 0 && typeof root.T === "function") {
        return (vars && typeof root.Tf === "function") ? root.Tf(key, vars) : root.T(key);
      }
      return super.t(key, vars);
    }

    errText() { return this.t("inbox.goal.err"); }

    styles() {
      return `
      .gl-lead { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b);
                 line-height:1.5; margin-bottom:var(--cp-gap-sm,6px); }
      .gl-hdr { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
      .gl-title { font-weight:var(--cp-fw-bold,700); font-size:var(--cp-fs,13px);
                  color:var(--cp-text,#1e293b); flex:1 1 auto; min-width:0; overflow-wrap:anywhere; }
      .gl-badge { display:inline-block; padding:1px 7px; border-radius:99px; white-space:nowrap;
                  font-size:var(--cp-fs-tiny,11px); background:var(--cp-track,#e2e8f0);
                  color:var(--cp-text-dim,#64748b); }
      .gl-badge.acc { background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .gl-badge.ok { background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent); color:var(--cp-ok,#0f9d75); }
      .gl-badge.warn { background:color-mix(in srgb,var(--cp-warn,#d97706) 12%,transparent); color:var(--cp-warn,#d97706); }
      .gl-tag { display:inline-block; padding:1px 6px; border-radius:var(--cp-radius-sm,6px);
                font-size:var(--cp-fs-tiny,11px); border:1px solid var(--cp-border,#e2e8f0);
                color:var(--cp-text-tiny,#94a3b8); white-space:nowrap; }
      .gl-track { display:flex; gap:3px; margin:var(--cp-gap-sm,6px) 0 2px; }
      .gl-seg { flex:1; height:6px; border-radius:99px; background:var(--cp-track,#e2e8f0); }
      .gl-seg.done { background:var(--cp-ok,#0f9d75); }
      .gl-seg.cur { background:var(--cp-accent,#4f46e5);
                    box-shadow:0 0 0 1px color-mix(in srgb,var(--cp-accent,#4f46e5) 30%,transparent);
                    animation:gl-pulse 1.6s ease-in-out infinite; }
      @keyframes gl-pulse { 0%,100% { opacity:1; } 50% { opacity:.45; } }
      .gl-meta { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b);
                 margin:2px 0 var(--cp-gap-xs,4px); }
      .gl-today { display:flex; gap:6px; align-items:flex-start; border-radius:8px; padding:6px 8px;
                  margin:var(--cp-gap-xs,4px) 0; background:var(--cp-surface-2,#f8fafc);
                  border:1px solid var(--cp-border,#e2e8f0); font-size:var(--cp-fs-sm,12px);
                  color:var(--cp-text,#374151); line-height:1.5; flex-wrap:wrap; }
      .gl-today.hold { color:var(--cp-text-dim,#64748b); font-style:italic; }
      .gl-today .gl-intent { flex:1 1 auto; min-width:0; }
      .gl-fb { display:flex; gap:4px; margin-left:auto; flex:0 0 auto; }
      .gl-fb button { font-size:10px; padding:1px 7px; }
      .gl-fbmark { flex:0 0 auto; margin-left:auto; font-size:var(--cp-fs-tiny,11px);
                   color:var(--cp-ok,#0f9d75); white-space:nowrap; }
      .gl-pill { flex:0 0 auto; padding:0 6px; border-radius:99px; font-size:var(--cp-fs-tiny,11px);
                 line-height:18px; white-space:nowrap; background:var(--cp-track,#e2e8f0);
                 color:var(--cp-text-dim,#64748b); }
      .gl-pill.soft { background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .gl-pill.direct { background:color-mix(in srgb,var(--cp-warn,#d97706) 12%,transparent); color:var(--cp-warn,#d97706); }
      .gl-acts { justify-content:flex-end; margin-top:var(--cp-gap-sm,6px); }
      .gl-result { font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151);
                   line-height:1.5; margin:var(--cp-gap-xs,4px) 0; }
      .gl-last { display:flex; gap:5px; align-items:center; flex-wrap:wrap;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 margin-top:var(--cp-gap-sm,6px); }
      .gl-last .gl-badge { font-size:10px; padding:0 6px; }
      .gl-errline { font-size:var(--cp-fs-sm,12px); color:var(--cp-danger,#dc2626); cursor:pointer; }
      .gl-form { display:flex; flex-direction:column; gap:var(--cp-gap-sm,6px); }
      .gl-fl { display:block; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:2px; }
      .gl-form select,.gl-form input { width:100%; box-sizing:border-box; font:inherit;
                 font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:4px 7px; }
      .gl-hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 line-height:1.45; margin-top:2px; }
      .gl-ferr { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); line-height:1.45; }`;
    }

    /* ── 数据 ── */

    async _api(url, opts) {
      const r = await fetch(url, opts);
      let d = null;
      try { d = await r.json(); } catch (_e) { d = null; }
      return { status: r.status, ok: r.ok, data: d };
    }

    async fetchData(ctx) {
      const cid = ctx.conversationId;
      if (this._lastCid !== cid) {   // 会话切换：收起残留表单态
        this._lastCid = cid;
        this._formOpen = false;
      }
      const url = "/api/goals/for-conversation?conversation_id=" + encodeURIComponent(cid);
      let res = null;
      try {
        res = await this._api(url);
      } catch (_e) {
        // 网络错误：安静重试一次，仍失败 → 轻量错误行（可点重试）
        await new Promise((rs) => setTimeout(rs, 600));
        try { res = await this._api(url); } catch (_e2) { return { __error: true }; }
      }
      if (res.status === 403) return { __forbidden: true };   // 功能未开 → 整卡隐藏
      if (!res.ok || !res.data) return { __error: true };
      return res.data;
    }

    /* ── 渲染 ── */

    renderData(d) {
      if (d.__forbidden) { this._hideCard(true); return `<div class="empty" hidden></div>`; }
      this._hideCard(false);
      if (d.__error) {
        return `<div class="gl-errline" data-act="retry">${this.esc(this.t("inbox.goal.err_retry"))}</div>`;
      }
      const g = d.goal || null;
      if (g && !TERMINAL[g.status]) { this._formOpen = false; return this._renderActive(g); }
      const term = (g && TERMINAL[g.status]) ? g : null;
      const last = term || d.last || null;
      if (this._formOpen) return this._renderForm() + this._lastLine(last);
      if (term) return this._renderTerminal(term);
      return this._renderEmpty(last);
    }

    _hideCard(hide) {
      const card = this.closest("[data-cp-card]");
      if (card) card.hidden = !!hide;
    }

    _statusBadge(st) {
      st = String(st || "");
      const map = {
        active: ["inbox.goal.status.active", "acc", ""],
        paused: ["inbox.goal.status.paused", "warn", ""],
        done: ["inbox.goal.status.done", "ok", "\uD83C\uDF89 "],   // 🎉
        failed: ["inbox.goal.status.missed", "", ""],
        expired: ["inbox.goal.status.missed", "", ""],
        cancelled: ["inbox.goal.status.cancelled", "", ""],
      };
      const m = map[st];
      const txt = m ? (m[2] + this.t(m[0])) : st;
      return `<span class="gl-badge${m && m[1] ? " " + m[1] : ""}">${this.esc(txt)}</span>`;
    }

    _autonomyLabel(l) { return AUTONOMY.indexOf(l) >= 0 ? this.t("inbox.goal.autonomy." + l) : l; }
    _autonomyHint(l) { return AUTONOMY.indexOf(l) >= 0 ? this.t("inbox.goal.autonomy." + l + "_hint") : ""; }

    _lastLine(last) {
      if (!last) return "";
      return `<div class="gl-last"><span>${this.esc(this.t("inbox.goal.last_label"))}</span>` +
        `<span>${this.esc(last.title || last.template_name || "")}</span>${this._statusBadge(last.status)}</div>`;
    }

    _renderEmpty(last) {
      const esc = (s) => this.esc(s);
      return `<div class="gl-lead">${esc(this.t("inbox.goal.empty_lead"))}</div>` +
        `<div class="acts"><button class="primary" data-act="open_form">${esc(this.t("inbox.goal.set_btn"))}</button></div>` +
        this._lastLine(last);
    }

    _renderActive(g) {
      const esc = (s) => this.esc(s);
      const lvl = String(g.autonomy || "suggest");
      const hint = this._autonomyHint(lvl);
      const tag = `<span class="gl-tag"${hint ? ` title="${esc(hint)}"` : ""}>${esc(this._autonomyLabel(lvl))}</span>`;

      const ms = Array.isArray(g.milestones) ? g.milestones : [];
      const mi = parseInt(g.milestone_idx, 10) || 0;
      let track = "";
      if (ms.length) {
        track = `<div class="gl-track">` + ms.map((m, i) => {
          const cls = i < mi ? "gl-seg done" : (i === mi ? "gl-seg cur" : "gl-seg");
          const nm = LANG === "en" ? (m.en || m.zh || "") : (m.zh || m.en || "");
          return `<div class="${cls}" title="${esc(nm)}"></div>`;
        }).join("") + `</div>`;
      }

      const pct = Math.max(0, Math.min(100, Math.round((parseFloat(g.progress) || 0) * 100)));
      const meta = `<div class="gl-meta">${esc(this.t("inbox.goal.day_of", { day: g.day_index || 1, total: g.total_days || 0 }))} · ${pct}%</div>`;

      let today = "";
      const beat = g.today;
      if (beat && (beat.status === "skipped" || beat.status === "blocked")) {
        // 坐席已驳回今日拍 → 今天不再注入/不带意图（明日 planner 自动降档）
        today = `<div class="gl-today hold">${esc(this.t("inbox.goal.today_rejected"))}</div>`;
      } else if (beat && beat.intent) {
        const pl = PUSH_LEVELS.indexOf(String(beat.push_level)) >= 0 ? String(beat.push_level) : "soft";
        let fb = "";
        if (g.status === "active") {
          fb = (beat.detail === "adopted")
            ? `<span class="gl-fbmark">✓ ${esc(this.t("inbox.goal.adopted_mark"))}</span>`
            : `<span class="gl-fb"><button data-act="beat_adopt" title="${esc(this.t("inbox.goal.act.adopt"))}">👍</button>` +
              `<button data-act="beat_reject" title="${esc(this.t("inbox.goal.act.reject"))}">👎</button></span>`;
        }
        today = `<div class="gl-today"><span class="gl-pill ${pl}">${esc(this.t("inbox.goal.push." + pl))}</span>` +
          `<span class="gl-intent">${esc(beat.intent)}</span>${fb}</div>`;
      } else if (HOLDS.indexOf(String(g.hold)) >= 0) {
        today = `<div class="gl-today hold">${esc(this.t("inbox.goal.hold." + String(g.hold)))}</div>`;
      }

      const paused = g.status === "paused";
      const flip = paused
        ? `<button data-act="resume">${esc(this.t("inbox.goal.act.resume"))}</button>`
        : `<button data-act="pause">${esc(this.t("inbox.goal.act.pause"))}</button>`;
      const acts = `<div class="acts gl-acts">${flip}` +
        `<button class="danger" data-act="cancel">${esc(this.t("inbox.goal.act.cancel"))}</button></div>`;

      return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}${tag}</div>` + track + meta + today + acts;
    }

    _renderTerminal(g) {
      const esc = (s) => this.esc(s);
      const result = g.result ? `<div class="gl-result">${esc(g.result)}</div>` : "";
      return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}</div>` + result +
        `<div class="acts gl-acts"><button class="primary" data-act="open_form">${esc(this.t("inbox.goal.again_btn"))}</button></div>`;
    }

    _tmplById(tid) {
      const list = (this._templates && this._templates.templates) || [];
      for (let i = 0; i < list.length; i++) { if (list[i].id === tid) return list[i]; }
      return null;
    }

    _paramsHtml(tmpl) {
      const esc = (s) => this.esc(s);
      return ((tmpl && tmpl.params) || []).map((p) => {
        const label = LANG === "en" ? (p.label_en || p.label_zh || p.key) : (p.label_zh || p.label_en || p.key);
        const typ = String(p.type || "").toLowerCase() === "number" ? "number" : "text";
        const dv = p.default == null ? "" : String(p.default);
        return `<div><label class="gl-fl">${esc(label)}</label>` +
          `<input type="${typ}" data-param-key="${esc(p.key)}" value="${esc(dv)}"${typ === "number" ? ` step="any"` : ""}></div>`;
      }).join("");
    }

    _renderForm() {
      const esc = (s) => this.esc(s);
      const list = (this._templates && this._templates.templates) || [];
      if (!list.length) {
        // 模板库拉取失败 → 可点重试（open_form 会重新拉模板）
        return `<div class="gl-errline" data-act="open_form">${esc(this.t("inbox.goal.form.load_fail"))}</div>`;
      }
      if (!this._formTid || !this._tmplById(this._formTid)) this._formTid = list[0].id;
      const tmpl = this._tmplById(this._formTid) || {};
      const opts = list.map((t) => {
        const nm = LANG === "en" ? (t.name_en || t.name_zh || t.id) : (t.name_zh || t.name_en || t.id);
        return `<option value="${esc(t.id)}"${t.id === this._formTid ? " selected" : ""}>${esc(nm)}</option>`;
      }).join("");
      const levels = (this._templates.autonomy_levels && this._templates.autonomy_levels.length)
        ? this._templates.autonomy_levels : AUTONOMY;
      const aOpts = levels.map((l) =>
        `<option value="${esc(l)}"${l === "suggest" ? " selected" : ""}>${esc(this._autonomyLabel(String(l)))}</option>`).join("");
      return `<div class="gl-form">` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.template"))}</label>` +
        `<select data-chg="template" data-ref="tid">${opts}</select></div>` +
        `<div data-ref="params">${this._paramsHtml(tmpl)}</div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.days"))}</label>` +
        `<input type="number" min="1" max="180" step="1" data-ref="days" value="${esc(String(tmpl.default_days || 14))}"></div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.autonomy"))}</label>` +
        `<select data-chg="autonomy" data-ref="autonomy">${aOpts}</select>` +
        `<div class="gl-hint" data-ref="ahint">${esc(this._autonomyHint("suggest"))}</div></div>` +
        `<div class="gl-ferr" data-ref="ferr" hidden></div>` +
        `<div class="acts gl-acts"><button data-act="close_form">${esc(this.t("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="create">${esc(this.t("inbox.goal.form.create"))}</button></div>` +
        `</div>`;
    }

    _ref(name) { return this.shadowRoot.querySelector(`[data-ref="${name}"]`); }

    _syncFormTemplate() {
      const sel = this._ref("tid");
      if (!sel) return;
      this._formTid = sel.value;
      const tmpl = this._tmplById(this._formTid) || {};
      const box = this._ref("params");
      if (box) box.innerHTML = this._paramsHtml(tmpl);
      const days = this._ref("days");
      if (days) days.value = String(tmpl.default_days || 14);
    }

    _syncAutonomyHint() {
      const a = this._ref("autonomy");
      const h = this._ref("ahint");
      if (a && h) h.textContent = this._autonomyHint(String(a.value));
    }

    _rerender() {
      this._render(this.renderData(this._d || { goal: null, last: null }));
    }

    /* ── 动作 ── */

    async onAction(act, el) {
      if (act === "retry") { this.refresh(); return; }
      if (act === "open_form") {
        if (el) el.disabled = true;
        if (!this._templates) await this._loadTemplates();
        this._formOpen = true;
        this._rerender();
        return;
      }
      if (act === "close_form") { this._formOpen = false; this._rerender(); return; }
      if (act === "create") { await this._create(el); return; }
      if (act === "pause" || act === "resume" || act === "cancel") { await this._status(act); return; }
      if (act === "beat_adopt") { await this._beatFeedback("adopt"); return; }
      if (act === "beat_reject") {
        if (typeof confirm === "function" && !confirm(this.t("inbox.goal.reject_confirm"))) return;
        await this._beatFeedback("reject");
        return;
      }
    }

    async _loadTemplates() {
      let res = null;
      try { res = await this._api("/api/goals/templates"); } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(true); return false; }
      if (res && res.ok && res.data && Array.isArray(res.data.templates)) {
        this._templates = res.data;
        return true;
      }
      this._templates = null;
      return false;
    }

    async _create(btn) {
      const ctx = this._ctx || {};
      if (!ctx.conversationId || !this._formTid) return;
      const params = {};
      this.shadowRoot.querySelectorAll("[data-param-key]").forEach((inp) => {
        const k = inp.getAttribute("data-param-key");
        const v = String(inp.value == null ? "" : inp.value).trim();
        if (!k || !v) return;   // 留空 → 走模板默认
        const num = parseFloat(v);
        params[k] = (inp.getAttribute("type") === "number" && isFinite(num)) ? num : v;
      });
      const body = { conversation_id: ctx.conversationId, template: this._formTid, params };
      const aSel = this._ref("autonomy");
      if (aSel && aSel.value) body.autonomy = String(aSel.value);
      const daysEl = this._ref("days");
      const days = daysEl ? parseFloat(daysEl.value) : 0;
      if (isFinite(days) && days > 0) body.deadline_days = days;

      if (btn) btn.disabled = true;
      let res = null;
      try {
        res = await this._api("/api/goals", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(true); return; }
      if (res && res.ok && res.data && res.data.ok !== false) {
        this._formOpen = false;
        this.emit("cp-goal-changed", { action: "create", conversationId: ctx.conversationId });
        this.refresh();   // 以服务器权威视图进入「进行中」态
        return;
      }
      if (btn) btn.disabled = false;
      const fe = this._ref("ferr");
      if (fe) {
        const detail = res && res.data && res.data.detail;   // 后端 detail 已 i18n，直接展示
        fe.textContent = (typeof detail === "string" && detail) ? detail : this.t("cp.common.save_fail");
        fe.hidden = false;
      }
    }

    async _beatFeedback(verdict) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/beat/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(true); return; }
      if (res && res.ok && res.data && res.data.goal) {
        this._d = Object.assign({}, this._d, { goal: res.data.goal });
        this._rerender();
        this.emit("cp-goal-changed", { action: "beat_" + verdict, conversationId: g.conversation_id });
        return;
      }
      this.refresh();   // 失败回到服务器权威状态（按钮态一并复位）
    }

    async _status(action) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      if (action === "cancel" && typeof confirm === "function" &&
          !confirm(this.t("inbox.goal.cancel_confirm"))) return;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(true); return; }
      if (res && res.ok && res.data && res.data.goal) {
        // cancel → 终态视图（结果 badge + 再设一个）；pause/resume → 就地更新
        this._d = Object.assign({}, this._d, { goal: res.data.goal });
        this._rerender();
        this.emit("cp-goal-changed", { action, conversationId: g.conversation_id });
        return;
      }
      this.refresh();   // 失败回到服务器权威状态（按钮态一并复位）
    }
  }

  if (!customElements.get("cp-goal")) customElements.define("cp-goal", CpGoal);
})(typeof window !== "undefined" ? window : this);
