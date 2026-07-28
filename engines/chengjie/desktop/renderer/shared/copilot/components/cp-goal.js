"use strict";
/* 两端共享组件 · 工作目标(<cp-goal>)— 继承 CpPanelBase
   统一收件箱右栏「工作目标」卡（对接 /api/goals*，后端 goal_routes.py）：
   - 无目标：插画空态 + 内联建目标表单（模板/参数/期限/自治档；偏好记忆）
   - 进行中(active/paused)：标题+状态、里程碑条(节点+当前名常显)、第X/Y天、
     今日拍(语义色条 / hold)、采纳/驳回(+5s 撤销)、按意图生成草稿、画像/产品、
     暂停/标成交(可选归因)/放弃(⋯菜单)
   - 终态：中性 badge + 「再设一个」为主角
   - 403：默认隐藏；inbox.goal.show_disabled_hint=1 时显示灰字提示（关闭≠不存在）
   埋点：曝光/展开设定/反馈 → /api/telemetry/ui-event（复用既有通道）
   文案：inbox.goal.* → window.T/Tf；共享键走 CopilotShared.t
   用法:
     const el = document.createElement('cp-goal');
     el.context = { conversationId }; */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-goal: CpPanelBase 未加载"); return; }

  const LANG = (root.CopilotShared && root.CopilotShared.lang) === "en" ? "en" : "zh";
  const TERMINAL = { done: 1, failed: 1, expired: 1, cancelled: 1 };
  const AUTONOMY = ["observe", "suggest", "auto"];
  const PUSH_LEVELS = ["none", "soft", "direct"];
  const HOLDS = ["emotion", "silent", "no_intent"];
  const PREFS_KEY = "cp_goal_form_prefs_v1";
  const UNDO_MS = 5000;

  function _showDisabledHint() {
    try {
      if (root.__GOAL_SHOW_DISABLED_HINT__ === true) return true;
      if (root.__GOAL_SHOW_DISABLED_HINT__ === false) return false;
      const v = root.localStorage && root.localStorage.getItem("inbox.goal.show_disabled_hint");
      return v === "1" || v === "true";
    } catch (_e) { return false; }
  }

  function _beacon(action) {
    try {
      const body = JSON.stringify({
        page: (root.location && root.location.pathname) || "/workspace",
        action: String(action || "").slice(0, 64),
      });
      if (typeof root.navigator !== "undefined" && root.navigator.sendBeacon) {
        root.navigator.sendBeacon(
          "/api/telemetry/ui-event",
          new Blob([body], { type: "application/json" }));
      }
    } catch (_e) { /* 埋点永不阻断 */ }
  }

  class CpGoal extends Base {
    constructor() {
      super();
      this._client = this._client || {};
      this._templates = null;
      this._formOpen = false;
      this._formTid = "";
      this._lastCid = "";
      this._profOpen = false;
      this._wonFormOpen = false;
      this._moreOpen = false;
      this._undoTimer = null;
      this._undoUntil = 0;
      this._prevMsIdx = -1;
      this._celebrateMs = false;
      this._exposed = false;
      this._toastText = "";
      this._toastTimer = null;

      this.shadowRoot.addEventListener("change", (e) => {
        const t = e.target;
        if (!t || !t.getAttribute) return;
        const chg = t.getAttribute("data-chg");
        if (chg === "template") this._syncFormTemplate();
        else if (chg === "autonomy") this._syncAutonomyHint();
      });
      this.shadowRoot.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const t = e.target;
        if (!t || !t.getAttribute) return;
        if (t.getAttribute("data-prof-key") != null) {
          e.preventDefault();
          this._profSave(this.shadowRoot.querySelector('[data-act="prof_save"]'));
        }
      });
      // 宿主深链 / 英雄卡 CTA：打开建目标表单
      this.addEventListener("cp-goal-open-form", () => {
        this.onAction("open_form", null);
      });
    }

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
      .gl-alias { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:-2px 0 6px; }
      .gl-hdr { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
      .gl-title { font-weight:var(--cp-fw-bold,700); font-size:var(--cp-fs,13px);
                  color:var(--cp-text,#1e293b); flex:1 1 auto; min-width:0; overflow-wrap:anywhere; }
      .gl-badge { display:inline-block; padding:1px 7px; border-radius:99px; white-space:nowrap;
                  font-size:var(--cp-fs-tiny,11px); background:var(--cp-track,#e2e8f0);
                  color:var(--cp-text-dim,#64748b); }
      .gl-badge.acc { background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .gl-badge.ok { background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent); color:var(--cp-ok,#0f9d75); }
      .gl-badge.warn { background:color-mix(in srgb,var(--cp-warn,#d97706) 12%,transparent); color:var(--cp-warn,#d97706); }
      .gl-badge.muted { background:color-mix(in srgb,var(--cp-text-dim,#64748b) 12%,transparent);
                        color:var(--cp-text-dim,#64748b); }
      .gl-tag { display:inline-block; padding:1px 6px; border-radius:var(--cp-radius-sm,6px);
                font-size:var(--cp-fs-tiny,11px); border:1px solid var(--cp-border,#e2e8f0);
                color:var(--cp-text-tiny,#94a3b8); white-space:nowrap; }
      .gl-track-wrap { margin:var(--cp-gap-sm,6px) 0 2px; }
      .gl-track { display:flex; gap:3px; align-items:center; }
      .gl-seg { flex:1; height:6px; border-radius:99px; background:var(--cp-track,#e2e8f0);
                position:relative; }
      .gl-seg::after { content:""; position:absolute; right:-2px; top:50%; width:8px; height:8px;
                       margin-top:-4px; border-radius:50%; background:inherit;
                       box-shadow:0 0 0 1px var(--cp-surface,#fff); }
      .gl-seg.done { background:var(--cp-ok,#0f9d75); }
      .gl-seg.cur { background:var(--cp-accent,#4f46e5);
                    box-shadow:0 0 0 1px color-mix(in srgb,var(--cp-accent,#4f46e5) 30%,transparent);
                    animation:gl-pulse 1.6s ease-in-out infinite; }
      .gl-seg.cur.celebrate { animation:gl-celebrate .55s ease-out; }
      @keyframes gl-pulse { 0%,100% { opacity:1; } 50% { opacity:.45; } }
      @keyframes gl-celebrate { 0% { transform:scale(1); } 40% { transform:scale(1.35); }
                                100% { transform:scale(1); } }
      .gl-ms-cur { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                   margin:3px 0 2px; }
      .gl-meta { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b);
                 margin:2px 0 var(--cp-gap-xs,4px); }
      .gl-sec { border-radius:8px; padding:7px 9px; margin:var(--cp-gap-xs,4px) 0;
                background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
                border-left:3px solid var(--cp-border,#e2e8f0); }
      .gl-sec.soft { border-left-color:var(--cp-accent,#4f46e5); }
      .gl-sec.direct { border-left-color:var(--cp-accent-deep,var(--cp-accent,#3730a3));
                       background:color-mix(in srgb,var(--cp-accent,#4f46e5) 6%,var(--cp-surface-2,#f8fafc)); }
      .gl-sec.hold { border-left-color:var(--cp-text-tiny,#94a3b8); color:var(--cp-text-dim,#64748b);
                     font-style:italic;
                     background:repeating-linear-gradient(-45deg,var(--cp-surface-2,#f8fafc),
                       var(--cp-surface-2,#f8fafc) 6px,color-mix(in srgb,var(--cp-text-tiny,#94a3b8) 8%,transparent) 6px,
                       color-mix(in srgb,var(--cp-text-tiny,#94a3b8) 8%,transparent) 12px); }
      .gl-today { display:flex; gap:6px; align-items:flex-start;
                  font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151);
                  line-height:1.5; flex-wrap:wrap; }
      .gl-today .gl-intent { flex:1 1 auto; min-width:0; }
      .gl-fb { display:flex; gap:4px; margin-left:auto; flex:0 0 auto; align-items:center; }
      .gl-fb button { font-size:11px; padding:2px 8px; }
      .gl-fbmark { flex:0 0 auto; margin-left:auto; font-size:var(--cp-fs-tiny,11px);
                   color:var(--cp-ok,#0f9d75); white-space:nowrap; }
      .gl-undo { display:flex; gap:6px; align-items:center; flex-wrap:wrap;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .gl-undo button { font-size:11px; padding:1px 8px; color:var(--cp-accent,#4f46e5);
                        border-color:var(--cp-accent,#4f46e5); }
      .gl-pill { flex:0 0 auto; padding:0 6px; border-radius:99px; font-size:var(--cp-fs-tiny,11px);
                 line-height:18px; white-space:nowrap; background:var(--cp-track,#e2e8f0);
                 color:var(--cp-text-dim,#64748b); }
      .gl-pill.soft { background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .gl-pill.direct { background:color-mix(in srgb,var(--cp-accent,#3730a3) 14%,transparent);
                        color:var(--cp-accent-deep,var(--cp-accent,#3730a3)); }
      .gl-acts { justify-content:flex-end; margin-top:var(--cp-gap-sm,6px); align-items:center; }
      .gl-acts .gl-link { background:transparent; border:none; color:var(--cp-text-tiny,#94a3b8);
                          text-decoration:underline; padding:2px 4px; }
      .gl-more { position:relative; }
      .gl-more-pop { position:absolute; right:0; bottom:100%; margin-bottom:4px; z-index:3;
                     min-width:110px; padding:4px; border-radius:8px;
                     background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                     box-shadow:0 4px 14px rgba(15,23,42,.08); }
      .gl-more-pop button { display:block; width:100%; text-align:left; margin:0; border:none;
                            background:transparent; color:var(--cp-danger,#dc2626); padding:5px 8px; }
      .gl-result { font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151);
                   line-height:1.5; margin:var(--cp-gap-xs,4px) 0; }
      .gl-last { display:flex; gap:5px; align-items:center; flex-wrap:wrap;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 margin-top:var(--cp-gap-sm,6px); }
      .gl-last .gl-badge { font-size:10px; padding:0 6px; }
      .gl-errline { font-size:var(--cp-fs-sm,12px); color:var(--cp-danger,#dc2626); cursor:pointer; }
      .gl-disabled { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-tiny,#94a3b8);
                     line-height:1.5; padding:4px 0; }
      .gl-form { display:flex; flex-direction:column; gap:var(--cp-gap-sm,6px); }
      .gl-fl { display:block; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:2px; }
      .gl-form select,.gl-form input { width:100%; box-sizing:border-box; font:inherit;
                 font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:4px 7px; }
      .gl-hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 line-height:1.45; margin-top:2px; }
      .gl-ferr { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); line-height:1.45; }
      .gl-prof { margin-top:var(--cp-gap-sm,6px); }
      .gl-prof-hd { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px);
                    color:var(--cp-text-dim,#64748b); }
      .gl-prof-hd .sp { flex:1 1 auto; }
      .gl-prof-hd button { font-size:10px; padding:1px 7px; }
      .gl-fillbar { display:flex; align-items:center; gap:4px; }
      .gl-fillbar .bar { width:52px; height:5px; border-radius:99px; background:var(--cp-track,#e2e8f0);
                         overflow:hidden; }
      .gl-fillbar .bar i { display:block; height:100%; background:var(--cp-accent,#4f46e5); }
      .gl-fillbar .bar.b2 i { background:var(--cp-ok,#0f9d75); }
      .gl-chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:4px; }
      .gl-chip { display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis;
                 white-space:nowrap; padding:1px 7px; border-radius:99px;
                 font-size:var(--cp-fs-tiny,11px); border:1px solid var(--cp-border,#e2e8f0);
                 color:var(--cp-text,#374151); background:var(--cp-surface,#fff);
                 text-decoration:none; }
      a.gl-chip:hover { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); }
      .gl-chip.miss { color:var(--cp-text-tiny,#94a3b8); border-style:dashed; background:transparent; }
      .gl-chip .src { opacity:.55; margin-left:3px; font-size:10px; }
      .gl-prof-form { display:grid; grid-template-columns:1fr 1fr; gap:4px 6px; margin-top:4px; }
      .gl-prof-form input { width:100%; box-sizing:border-box; font:inherit;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:3px 6px; }
      .gl-prof-empty { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                       margin-top:3px; line-height:1.45; }
      .gl-empty { text-align:center; padding:8px 4px 4px; }
      .gl-empty svg { display:block; margin:0 auto 8px; color:var(--cp-accent,#4f46e5); opacity:.85; }
      .gl-toast { font-size:var(--cp-fs-tiny,11px); color:var(--cp-ok,#0f9d75); margin-top:4px; }
      .gl-draft-btn { font-size:10px; padding:1px 7px; margin-left:auto; }`;
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
      if (this._lastCid !== cid) {
        this._lastCid = cid;
        this._formOpen = false;
        this._profOpen = false;
        this._wonFormOpen = false;
        this._moreOpen = false;
        this._clearUndo();
        this._prevMsIdx = -1;
        this._celebrateMs = false;
        this._exposed = false;
      }
      const url = "/api/goals/for-conversation?conversation_id=" + encodeURIComponent(cid);
      let res = null;
      try {
        res = await this._api(url);
      } catch (_e) {
        await new Promise((rs) => setTimeout(rs, 600));
        try { res = await this._api(url); } catch (_e2) { return { __error: true }; }
      }
      if (res.status === 403) {
        try { console.debug("[cp-goal] hidden: feature disabled (403)", cid); } catch (_e) {}
        return { __forbidden: true };
      }
      if (!res.ok || !res.data) return { __error: true };
      const d = res.data;
      const g = d.goal;
      if (g && g.profile_slots && !TERMINAL[g.status]) {
        try {
          const pr = await this._api(
            "/api/goals/profile?conversation_id=" + encodeURIComponent(cid));
          if (pr.ok && pr.data && Array.isArray(pr.data.slots)) d.__profile = pr.data;
        } catch (_e) { /* soft */ }
      }
      return d;
    }

    /* ── 渲染 ── */

    renderData(d) {
      if (d.__forbidden) {
        if (_showDisabledHint()) {
          this._hideCard(false);
          return `<div class="gl-disabled">${this.esc(this.t("inbox.goal.disabled_hint"))}</div>`;
        }
        this._hideCard(true);
        return `<div class="empty" hidden></div>`;
      }
      this._hideCard(false);
      if (!this._exposed) {
        this._exposed = true;
        _beacon("goal_card_expose");
      }
      if (d.__error) {
        return `<div class="gl-errline" data-act="retry">${this.esc(this.t("inbox.goal.err_retry"))}</div>`;
      }
      const g = d.goal || null;
      if (g && !TERMINAL[g.status]) {
        this._formOpen = false;
        this._wonFormOpen = false;
        const mi = parseInt(g.milestone_idx, 10) || 0;
        if (this._prevMsIdx >= 0 && mi > this._prevMsIdx) this._celebrateMs = true;
        this._prevMsIdx = mi;
        const html = this._renderActive(g);
        this._celebrateMs = false;
        this._emitLoadedSignal(g);
        return html + this._toastHtml();
      }
      const term = (g && TERMINAL[g.status]) ? g : null;
      const last = term || d.last || null;
      this._emitLoadedSignal(null);
      if (this._formOpen) return this._renderForm() + this._lastLine(last) + this._toastHtml();
      if (term) return this._renderTerminal(term) + this._toastHtml();
      return this._renderEmpty(last) + this._toastHtml();
    }

    _emitLoadedSignal(g) {
      try {
        const beat = g && g.today;
        const pending = !!(g && g.status === "active" && beat && beat.intent
          && beat.detail !== "adopted"
          && beat.status !== "skipped" && beat.status !== "blocked");
        this.emit("cp-goal-loaded", {
          conversationId: (this._ctx && this._ctx.conversationId) || "",
          active: !!(g && !TERMINAL[g.status]),
          pendingFeedback: pending,
          hold: (g && g.hold) || "",
          dayIndex: g ? (g.day_index || 1) : 0,
          totalDays: g ? (g.total_days || 0) : 0,
          intent: (beat && beat.intent) || "",
          pushLevel: (beat && beat.push_level) || "",
          status: g ? g.status : "",
          title: g ? (g.title || g.template_name || "") : "",
        });
      } catch (_e) { /* host optional */ }
    }

    _hideCard(hide) {
      const card = this.closest("[data-cp-card]");
      if (card) card.hidden = !!hide;
    }

    _toastHtml() {
      if (!this._toastText) return "";
      return `<div class="gl-toast">${this.esc(this._toastText)}</div>`;
    }

    _flashToast(text) {
      this._toastText = text || "";
      if (this._toastTimer) clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => {
        this._toastText = "";
        this._toastTimer = null;
        if (this._d && !this._d.__forbidden) this._rerender();
      }, 2200);
      this._rerender();
    }

    _statusBadge(st) {
      st = String(st || "");
      const map = {
        active: ["inbox.goal.status.active", "acc", ""],
        paused: ["inbox.goal.status.paused", "warn", ""],
        done: ["inbox.goal.status.done", "ok", "\uD83C\uDF89 "],
        failed: ["inbox.goal.status.missed", "muted", ""],
        expired: ["inbox.goal.status.missed", "muted", ""],
        cancelled: ["inbox.goal.status.cancelled", "muted", ""],
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

    _emptyIllust() {
      // 轻量旗帜线稿（空态视觉锚点）
      return `<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true"><path d="M4 22V4"/><path d="M4 4h11l-1.6 3.2L15 10.5H4"/>
        <circle cx="18" cy="6" r="2.2"/></svg>`;
    }

    _renderEmpty(last) {
      const esc = (s) => this.esc(s);
      return `<div class="gl-empty">${this._emptyIllust()}` +
        `<div class="gl-lead">${esc(this.t("inbox.goal.empty_lead"))}</div>` +
        `<div class="gl-alias">${esc(this.t("inbox.goal.alias_note"))}</div>` +
        `<div class="acts" style="justify-content:center">` +
        `<button class="primary" data-act="open_form">${esc(this.t("inbox.goal.set_btn"))}</button></div></div>` +
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
      let msCur = "";
      if (ms.length) {
        track = `<div class="gl-track-wrap"><div class="gl-track">` + ms.map((m, i) => {
          let cls = i < mi ? "gl-seg done" : (i === mi ? "gl-seg cur" : "gl-seg");
          if (i === mi && this._celebrateMs) cls += " celebrate";
          const nm = LANG === "en" ? (m.en || m.zh || "") : (m.zh || m.en || "");
          return `<div class="${cls}" title="${esc(nm)}"></div>`;
        }).join("") + `</div>`;
        const cur = ms[Math.min(mi, ms.length - 1)] || {};
        const curNm = LANG === "en" ? (cur.en || cur.zh || "") : (cur.zh || cur.en || "");
        if (curNm) {
          msCur = `<div class="gl-ms-cur">${esc(this.t("inbox.goal.ms_current", { name: curNm }))}</div>`;
        }
        track += msCur + `</div>`;
      }

      const pct = Math.max(0, Math.min(100, Math.round((parseFloat(g.progress) || 0) * 100)));
      const meta = `<div class="gl-meta">${esc(this.t("inbox.goal.day_of", { day: g.day_index || 1, total: g.total_days || 0 }))} · ${pct}%</div>`;

      let today = "";
      const beat = g.today;
      const undoLive = this._undoUntil && Date.now() < this._undoUntil;
      if (undoLive || (beat && (beat.status === "skipped" || beat.status === "blocked"))) {
        today = `<div class="gl-sec hold"><div class="gl-undo">` +
          `<span>${esc(this.t("inbox.goal.rejected_undo"))}</span>` +
          (undoLive ? `<button data-act="beat_undo">${esc(this.t("inbox.goal.undo"))}</button>` : "") +
          `</div></div>`;
      } else if (beat && beat.intent) {
        const pl = PUSH_LEVELS.indexOf(String(beat.push_level)) >= 0 ? String(beat.push_level) : "soft";
        const secCls = pl === "direct" ? "direct" : (pl === "soft" ? "soft" : "");
        let fb = "";
        if (g.status === "active") {
          fb = (beat.detail === "adopted")
            ? `<span class="gl-fbmark">\u2713 ${esc(this.t("inbox.goal.adopted_mark"))}</span>`
            : `<span class="gl-fb">` +
              `<button data-act="beat_adopt" title="${esc(this.t("inbox.goal.act.adopt"))}">\uD83D\uDC4D</button>` +
              `<button data-act="beat_reject" title="${esc(this.t("inbox.goal.act.reject"))}">\uD83D\uDC4E</button>` +
              `<button class="gl-draft-btn" data-act="drive_draft" title="${esc(this.t("inbox.goal.draft_from_intent"))}">` +
              `${esc(this.t("inbox.goal.draft_from_intent"))}</button></span>`;
        }
        today = `<div class="gl-sec ${secCls}"><div class="gl-today">` +
          `<span class="gl-pill ${pl}">${esc(this.t("inbox.goal.push." + pl))}</span>` +
          `<span class="gl-intent">${esc(beat.intent)}</span>${fb}</div></div>`;
      } else if (HOLDS.indexOf(String(g.hold)) >= 0) {
        today = `<div class="gl-sec hold">${esc(this.t("inbox.goal.hold." + String(g.hold)))}</div>`;
      }

      if (this._wonFormOpen) {
        return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
          `${this._statusBadge(g.status)}</div>` +
          `<div class="gl-form"><div class="gl-lead">${esc(this.t("inbox.goal.won_meta_title"))}</div>` +
          `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_product"))}</label>` +
          `<input data-ref="won_product" autocomplete="off"></div>` +
          `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_amount"))}</label>` +
          `<input type="number" step="any" data-ref="won_amount"></div>` +
          `<div class="acts gl-acts"><button data-act="won_cancel">${esc(this.t("cp.common.cancel"))}</button>` +
          `<button class="primary" data-act="won_confirm">${esc(this.t("inbox.goal.won_meta_confirm"))}</button></div></div>`;
      }

      const paused = g.status === "paused";
      const flip = paused
        ? `<button data-act="resume">${esc(this.t("inbox.goal.act.resume"))}</button>`
        : `<button data-act="pause">${esc(this.t("inbox.goal.act.pause"))}</button>`;
      const more = `<span class="gl-more">` +
        `<button class="gl-link" data-act="more_toggle">${esc(this.t("inbox.goal.more_menu"))} \u22EF</button>` +
        (this._moreOpen
          ? `<div class="gl-more-pop"><button data-act="cancel">${esc(this.t("inbox.goal.act.cancel"))}</button></div>`
          : "") +
        `</span>`;
      const acts = `<div class="acts gl-acts">${flip}${more}` +
        `<button class="primary" data-act="won">${esc(this.t("inbox.goal.act.won"))}</button></div>`;

      return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}${tag}</div>` + track + meta + today +
        this._renderProfile() + this._renderProducts(g) + acts;
    }

    _renderProducts(g) {
      const list = Array.isArray(g.products) ? g.products.filter((p) => p && p.name) : [];
      if (!list.length) return "";
      const esc = (s) => this.esc(s);
      const chips = list.map((p) => {
        const price = p.price_from ? ` <span class="src">${esc(p.price_from)}</span>` : "";
        const body = `${esc(p.name)}${price}`;
        const tip = p.pitch ? ` title="${esc(p.pitch)}"` : "";
        return p.url
          ? `<a class="gl-chip" href="${esc(p.url)}" target="_blank" rel="noopener"${tip}>${body}</a>`
          : `<span class="gl-chip"${tip}>${body}</span>`;
      }).join("");
      return `<div class="gl-sec gl-prof"><div class="gl-prof-hd">` +
        `<span>${esc(this.t("inbox.goal.products.title"))}</span></div>` +
        `<div class="gl-chips">${chips}</div></div>`;
    }

    _renderProfile() {
      const p = this._d && this._d.__profile;
      if (!p) return "";
      const esc = (s) => this.esc(s);
      const fill = p.fill || {};
      const pct = (x) => Math.max(0, Math.min(100, Math.round((parseFloat(x) || 0) * 100)));
      const bar = (label, val, cls) =>
        `<span class="gl-fillbar"><span>${esc(label)}</span>` +
        `<span class="bar${cls ? " " + cls : ""}"><i style="width:${pct(val)}%"></i></span>` +
        `<span>${pct(val)}%</span></span>`;
      const hd = `<div class="gl-prof-hd"><span>${esc(this.t("inbox.goal.profile.title"))}</span>` +
        bar(this.t("inbox.goal.profile.track.relation"), fill.relation, "") +
        bar(this.t("inbox.goal.profile.track.bant"), fill.bant, "b2") +
        `<span class="sp"></span>` +
        `<button data-act="prof_toggle">${esc(this.t(this._profOpen ? "cp.common.cancel" : "inbox.goal.profile.edit"))}</button></div>`;

      if (this._profOpen) {
        const inputs = (p.slots || []).map((s) =>
          `<div><label class="gl-fl">${esc(s.label)}</label>` +
          `<input data-prof-key="${esc(s.key)}" value="${esc(s.value || "")}"></div>`).join("");
        return `<div class="gl-sec gl-prof">${hd}<div class="gl-prof-form">${inputs}</div>` +
          `<div class="gl-ferr" data-ref="perr" hidden></div>` +
          `<div class="acts gl-acts"><button class="primary" data-act="prof_save">${esc(this.t("inbox.goal.profile.save"))}</button></div></div>`;
      }

      const filled = (p.slots || []).filter((s) => s.value);
      const missing = (p.slots || []).filter((s) => !s.value && s.track === "bant").slice(0, 3);
      let body = "";
      if (!filled.length && !missing.length) {
        body = `<div class="gl-prof-empty">${esc(this.t("inbox.goal.profile.empty"))}</div>`;
      } else {
        const chips = filled.map((s) => {
          const src = s.src ? `<span class="src">${esc(this.t("inbox.goal.profile.src." + (s.src === "agent" ? "agent" : "auto")))}</span>` : "";
          return `<span class="gl-chip" title="${esc(s.label + ": " + s.value)}">${esc(s.label)}\u00b7${esc(s.value)}${src}</span>`;
        }).join("");
        const miss = missing.map((s) =>
          `<span class="gl-chip miss">${esc(s.label)}?</span>`).join("");
        body = `<div class="gl-chips">${chips}${miss}</div>`;
        if (!filled.length) {
          body = `<div class="gl-prof-empty">${esc(this.t("inbox.goal.profile.empty"))}</div>` + body;
        }
      }
      return `<div class="gl-sec gl-prof">${hd}${body}</div>`;
    }

    _renderTerminal(g) {
      const esc = (s) => this.esc(s);
      const result = g.result ? `<div class="gl-result">${esc(g.result)}</div>` : "";
      return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}</div>` + result +
        `<div class="acts gl-acts"><button class="primary" data-act="open_form">${esc(this.t("inbox.goal.again_btn"))}</button></div>`;
    }

    _loadPrefs() {
      try {
        const raw = root.localStorage && root.localStorage.getItem(PREFS_KEY);
        return raw ? (JSON.parse(raw) || {}) : {};
      } catch (_e) { return {}; }
    }

    _savePrefs(p) {
      try {
        root.localStorage && root.localStorage.setItem(PREFS_KEY, JSON.stringify(p || {}));
      } catch (_e) { /* ignore */ }
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
        return `<div class="gl-errline" data-act="open_form">${esc(this.t("inbox.goal.form.load_fail"))}</div>`;
      }
      const prefs = this._loadPrefs();
      if (!this._formTid || !this._tmplById(this._formTid)) {
        this._formTid = (prefs.template && this._tmplById(prefs.template))
          ? prefs.template : list[0].id;
      }
      const tmpl = this._tmplById(this._formTid) || {};
      const opts = list.map((t) => {
        const nm = LANG === "en" ? (t.name_en || t.name_zh || t.id) : (t.name_zh || t.name_en || t.id);
        return `<option value="${esc(t.id)}"${t.id === this._formTid ? " selected" : ""}>${esc(nm)}</option>`;
      }).join("");
      const levels = (this._templates.autonomy_levels && this._templates.autonomy_levels.length)
        ? this._templates.autonomy_levels : AUTONOMY;
      const prefAuto = AUTONOMY.indexOf(prefs.autonomy) >= 0 ? prefs.autonomy : "suggest";
      const aOpts = levels.map((l) =>
        `<option value="${esc(l)}"${l === prefAuto ? " selected" : ""}>${esc(this._autonomyLabel(String(l)))}</option>`).join("");
      const daysVal = (prefs.deadline_days > 0) ? prefs.deadline_days : (tmpl.default_days || 14);
      const prefNote = (prefs.template || prefs.autonomy || prefs.deadline_days)
        ? `<div class="gl-hint">${esc(this.t("inbox.goal.prefs_restored"))}</div>` : "";
      return `<div class="gl-form">` +
        `<div class="gl-alias">${esc(this.t("inbox.goal.title_hint"))}</div>` + prefNote +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.template"))}</label>` +
        `<select data-chg="template" data-ref="tid">${opts}</select></div>` +
        `<div data-ref="params">${this._paramsHtml(tmpl)}</div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.days"))}</label>` +
        `<input type="number" min="1" max="180" step="1" data-ref="days" value="${esc(String(daysVal))}"></div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.autonomy"))}</label>` +
        `<select data-chg="autonomy" data-ref="autonomy">${aOpts}</select>` +
        `<div class="gl-hint" data-ref="ahint">${esc(this._autonomyHint(prefAuto))}</div></div>` +
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

    _clearUndo() {
      if (this._undoTimer) { clearTimeout(this._undoTimer); this._undoTimer = null; }
      this._undoUntil = 0;
    }

    /* ── 动作 ── */

    async onAction(act, el) {
      if (act === "retry") { this.refresh(); return; }
      if (act === "open_form") {
        if (el) el.disabled = true;
        _beacon("goal_set_click");
        if (!this._templates) await this._loadTemplates();
        this._formOpen = true;
        this._rerender();
        return;
      }
      if (act === "close_form") { this._formOpen = false; this._rerender(); return; }
      if (act === "create") { await this._create(el); return; }
      if (act === "pause" || act === "resume") { await this._status(act); return; }
      if (act === "cancel") {
        this._moreOpen = false;
        await this._status("cancel");
        return;
      }
      if (act === "won") { this._wonFormOpen = true; this._rerender(); return; }
      if (act === "won_cancel") { this._wonFormOpen = false; this._rerender(); return; }
      if (act === "won_confirm") { await this._status("done"); return; }
      if (act === "more_toggle") { this._moreOpen = !this._moreOpen; this._rerender(); return; }
      if (act === "beat_adopt") { _beacon("goal_feedback_adopt"); await this._beatFeedback("adopt"); return; }
      if (act === "beat_reject") {
        if (typeof confirm === "function" && !confirm(this.t("inbox.goal.reject_confirm"))) return;
        _beacon("goal_feedback_reject");
        await this._beatFeedback("reject");
        return;
      }
      if (act === "beat_undo") { _beacon("goal_feedback_undo"); await this._beatFeedback("undo"); return; }
      if (act === "drive_draft") {
        const g = this._d && this._d.goal;
        const intent = g && g.today && g.today.intent;
        if (!intent) return;
        _beacon("goal_drive_draft");
        this.emit("cp-goal-drive-draft", {
          intent: String(intent),
          conversationId: g.conversation_id || (this._ctx && this._ctx.conversationId) || "",
          goalId: g.goal_id || "",
        });
        return;
      }
      if (act === "prof_toggle") { this._profOpen = !this._profOpen; this._rerender(); return; }
      if (act === "prof_save") { await this._profSave(el); return; }
    }

    async _profSave(btn) {
      const ctx = this._ctx || {};
      if (!ctx.conversationId) return;
      const fields = {};
      this.shadowRoot.querySelectorAll("[data-prof-key]").forEach((inp) => {
        const k = inp.getAttribute("data-prof-key");
        if (k) fields[k] = String(inp.value == null ? "" : inp.value).trim();
      });
      if (btn) btn.disabled = true;
      let res = null;
      try {
        res = await this._api("/api/goals/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: ctx.conversationId, fields }),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return; }
      if (res && res.ok && res.data && Array.isArray(res.data.slots)) {
        this._profOpen = false;
        this._d = Object.assign({}, this._d, { __profile: res.data });
        this._flashToast(this.t("inbox.goal.toast_saved") || this.t("inbox.goal.profile.saved"));
        this.emit("cp-goal-changed", { action: "profile", conversationId: ctx.conversationId });
        return;
      }
      if (btn) btn.disabled = false;
      const pe = this._ref("perr");
      if (pe) {
        const detail = res && res.data && res.data.detail;
        pe.textContent = (typeof detail === "string" && detail) ? detail : this.t("inbox.goal.profile.err");
        pe.hidden = false;
      }
    }

    async _loadTemplates() {
      let res = null;
      try { res = await this._api("/api/goals/templates"); } catch (_e) { res = null; }
      if (res && res.status === 403) {
        if (_showDisabledHint()) {
          this._d = { __forbidden: true };
          this._rerender();
        } else this._hideCard(true);
        return false;
      }
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
        if (!k || !v) return;
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
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return; }
      if (res && res.ok && res.data && res.data.ok !== false) {
        this._savePrefs({
          template: this._formTid,
          autonomy: body.autonomy || "suggest",
          deadline_days: body.deadline_days || 0,
        });
        this._formOpen = false;
        _beacon("goal_create_ok");
        this.emit("cp-goal-changed", { action: "create", conversationId: ctx.conversationId });
        this.refresh();
        return;
      }
      if (btn) btn.disabled = false;
      const fe = this._ref("ferr");
      if (fe) {
        const detail = res && res.data && res.data.detail;
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
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return; }
      if (res && res.ok && res.data && res.data.goal) {
        this._d = Object.assign({}, this._d, { goal: res.data.goal });
        if (verdict === "reject") {
          this._clearUndo();
          this._undoUntil = Date.now() + UNDO_MS;
          this._undoTimer = setTimeout(() => {
            this._undoUntil = 0;
            this._undoTimer = null;
            if (this._d && this._d.goal) this._rerender();
          }, UNDO_MS);
        } else if (verdict === "undo") {
          this._clearUndo();
        }
        this._rerender();
        this.emit("cp-goal-changed", { action: "beat_" + verdict, conversationId: g.conversation_id });
        return;
      }
      this.refresh();
    }

    async _status(action) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      if (action === "cancel" && typeof confirm === "function" &&
          !confirm(this.t("inbox.goal.cancel_confirm"))) return;

      const body = { action };
      if (action === "done") {
        const product = this._ref("won_product");
        const amount = this._ref("won_amount");
        const meta = {};
        if (product && String(product.value || "").trim()) meta.product = String(product.value).trim();
        if (amount && String(amount.value || "").trim()) {
          const n = parseFloat(amount.value);
          if (isFinite(n)) meta.amount = n;
        }
        if (Object.keys(meta).length) body.meta = meta;
      }

      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return; }
      if (res && res.ok && res.data && res.data.goal) {
        this._wonFormOpen = false;
        this._moreOpen = false;
        this._d = Object.assign({}, this._d, { goal: res.data.goal });
        this._rerender();
        this.emit("cp-goal-changed", { action, conversationId: g.conversation_id });
        return;
      }
      this.refresh();
    }
  }

  if (!customElements.get("cp-goal")) customElements.define("cp-goal", CpGoal);
})(typeof window !== "undefined" ? window : this);
