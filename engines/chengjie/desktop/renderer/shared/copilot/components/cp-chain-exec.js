"use strict";
/* 两端共享组件 · 跟进 SOP / 工作链(<cp-chain-exec>)— 继承 CpPanelBase
   对等网页端 _loadChainExecutions:执行卡(链名/状态徽章/分段进度轨/下一步倒计时(人话)/
   最后结果)+ 取消(running)。空态=价值说明 + 内联「启动工作链」选择器(列已启用链，
   点选即对当前会话 start-chain) + 「管理工作链」深链 /workflows；有执行时底部保留
   同款轻量启动/管理入口。取消/启动后派发 cp-action-done 并刷新。
   用法:
     const el = document.createElement('cp-chain-exec');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getChainExecutions / cancelChainExecution / listWorkflowChains / startChain
   （旧 client 缺后两者时启动入口软降级为错误提示，不炸面板。）
   C1/C3 增量:
   - 模块关(inbox.workflows.enabled=false → 后端 403)时整卡自动隐藏(与 cp-goal 同模式);
   - 选择器目标感知:当前会话有活跃工作目标且模板命中 GOAL_CHAIN_REC → 对应种子链
     置顶 + 「推荐」徽标,启动时带 goal_id 落执行归因(goals 关/无目标/桌面壳 → 无徽标,
     纯增强零依赖)。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-chain-exec: CpPanelBase 未加载"); return; }

  // 管理页深链仅在 http(s) 宿主渲染（桌面壳 file:// 上下文没有 /workflows 路由）
  const HAS_HTTP = (() => {
    try { return /^https?:$/.test(root.location.protocol); } catch (_e) { return false; }
  })();

  /* 宿主抽屉接管（2026-08-09）：坐席台（unified_inbox）暴露
     window.__wsOpenWorkflowsDrawer 时，「管理工作链」改为在坐席台内开抽屉
     （/workspace/workflows?embed=1），不再 target=_blank 弹新标签页丢会话上下文。
     App 模式下本组件活在同源 iframe（/copilot/app.html）里 → 经 parent 探测；
     跨源/无宿主函数（独立打开、桌面壳）→ 返回 null 走旧深链行为，零破坏。 */
  function _hostDrawerOpener() {
    try { if (typeof root.__wsOpenWorkflowsDrawer === "function") return root.__wsOpenWorkflowsDrawer; } catch (_e) { /* ignore */ }
    try {
      const p = root.parent;
      if (p && p !== root && typeof p.__wsOpenWorkflowsDrawer === "function") return p.__wsOpenWorkflowsDrawer;
    } catch (_e) { /* 跨源 parent 访问抛错＝无宿主接管 */ }
    return null;
  }

  /* C1：目标模板 → 种子链 静态映射（推荐的单一事实源；两侧 id 由
     tests/test_workflows_feature_flag.py 钉住必须真实存在——防幽灵推荐）。
     只登记高置信对应；relationship_* / custom 诚实不推荐。 */
  /* 单一事实源：CopilotShared.GOAL_CHAIN_REC（sidebar-chrome.js），此处仅引用——
     改映射去那里。旧缓存混态（sidebar-chrome 未刷新）→ 空表＝无推荐徽标软降级。 */
  const GOAL_CHAIN_REC = (root.CopilotShared && root.CopilotShared.GOAL_CHAIN_REC) || {};

  class CpChainExec extends Base {
    constructor() {
      super();
      this._lastCid = "";
      this._pickOpen = false;    // 内联启动选择器开合
      this._chains = null;       // 已启用链缓存（会话切换时失效）
      this._pickLoading = false;
      this._pickErr = false;
      this._startErr = "";       // 上次启动失败的服务端文案
      this._busy = false;        // 启动在途防双击
      this._openExecs = {};      // 步骤明细抽屉开合（exec_id → bool）
      this._goalRec = null;      // C1：{cid, goalId, chainId} 活跃目标推荐缓存（按会话）
    }

    emptyText() { return this.t("cp.chain.empty"); }
    emptyDataText() { return this.t("cp.chain.no_data"); }
    errText() { return this.t("cp.chain.err"); }

    styles() {
      return `
      .exec { border:1px solid var(--cp-border,#e2e8f0); border-radius:8px;
              padding:6px 9px; margin-bottom:var(--cp-gap-xs,4px); background:var(--cp-surface,#fff); }
      .exec.failed { border-color:color-mix(in srgb,var(--cp-danger,#dc2626) 55%,transparent); }
      .hd { display:flex; align-items:center; gap:6px; }
      .name { flex:1 1 auto; min-width:0; font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
              color:var(--cp-text,#1e293b); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .bdg { flex:0 0 auto; padding:0 7px; border-radius:99px; font-size:10px; line-height:17px;
             white-space:nowrap; background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .bdg.run { background:var(--cp-accent-weak,rgba(79,70,229,.12)); color:var(--cp-accent,#4f46e5);
                 animation:cx-pulse 1.6s ease-in-out infinite; }
      .bdg.ok { background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent); color:var(--cp-ok,#0f9d75); }
      .bdg.fail { background:color-mix(in srgb,var(--cp-danger,#dc2626) 12%,transparent); color:var(--cp-danger,#dc2626); }
      @keyframes cx-pulse { 0%,100% { opacity:1; } 50% { opacity:.55; } }
      .trk { display:flex; gap:3px; margin:6px 0 3px; }
      .sg { flex:1; height:5px; border-radius:99px; background:var(--cp-track,#e2e8f0); }
      .sg.done { background:var(--cp-ok,#0f9d75); }
      .sg.cur { background:var(--cp-accent,#4f46e5); animation:cx-pulse 1.6s ease-in-out infinite; }
      .sg.fail { background:var(--cp-danger,#dc2626); animation:none; }
      .meta { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .nxt { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#374151); margin-top:2px;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .last { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin-top:2px;
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .exec .acts { justify-content:flex-end; }
      .ft { display:flex; gap:8px; align-items:center; justify-content:flex-end; margin-top:var(--cp-gap-xs,4px); }
      .mng { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); text-decoration:underline; }
      .mng:hover { color:var(--cp-accent,#4f46e5); }
      .cx-empty { text-align:center; padding:8px 4px 4px; }
      .cx-empty svg { display:block; margin:0 auto 8px; color:var(--cp-accent,#4f46e5); opacity:.85; }
      .cx-lead { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); line-height:1.55;
                 margin-bottom:var(--cp-gap-sm,6px); text-align:left; }
      .cx-acts { display:flex; gap:8px; align-items:center; justify-content:center; flex-wrap:wrap; }
      .pick { border:1px solid var(--cp-border,#e2e8f0); border-radius:8px; padding:8px 9px;
              margin-bottom:var(--cp-gap-sm,6px); background:var(--cp-surface-2,#f8fafc); }
      .pk-t { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px);
              color:var(--cp-text-dim,#64748b); margin-bottom:5px; }
      .pk-t .sp { flex:1 1 auto; }
      .pk-t button { font-size:10px; padding:1px 7px; }
      .pk-row { border:1px solid var(--cp-border,#e2e8f0); border-radius:8px; padding:6px 9px;
                margin-bottom:4px; cursor:pointer; background:var(--cp-surface,#fff); }
      .pk-row:hover { border-color:var(--cp-accent,#4f46e5); }
      .pk-nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); color:var(--cp-text,#1e293b);
               display:flex; gap:6px; align-items:center; min-width:0; }
      .pk-nm .nm { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pk-n { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px; line-height:16px;
              background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .pk-d { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-top:1px;
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pk-hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); line-height:1.5; }
      .pk-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:4px; }
      .pk-retry { cursor:pointer; text-decoration:underline; }
      .pk-rec { background:var(--cp-accent-weak,rgba(79,70,229,.12)); color:var(--cp-accent,#4f46e5); }
      .stp-btn { background:transparent; border:none; cursor:pointer; padding:2px 0;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .stp-btn:hover { color:var(--cp-accent,#4f46e5); }
      .stp-row { display:flex; gap:6px; align-items:baseline; font-size:var(--cp-fs-tiny,11px);
                 padding:2px 0; border-bottom:1px dashed var(--cp-border,#e2e8f0); }
      .stp-row:last-of-type { border-bottom:none; }
      .stp-ic { flex:0 0 12px; text-align:center; color:var(--cp-text-tiny,#94a3b8); }
      .stp-row.ok .stp-ic { color:var(--cp-ok,#0f9d75); }
      .stp-row.cur .stp-ic { color:var(--cp-accent,#4f46e5); }
      .stp-row.fail .stp-ic { color:var(--cp-danger,#dc2626); }
      .stp-lb { flex:0 0 auto; color:var(--cp-text,#374151); font-weight:var(--cp-fw-bold,600); }
      .stp-delay { flex:0 0 auto; font-size:10px; padding:0 5px; border-radius:99px;
                   background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .stp-note { flex:1 1 auto; min-width:0; color:var(--cp-text-dim,#64748b); overflow-wrap:anywhere; }
      .stp-more { font-size:10px; color:var(--cp-text-tiny,#94a3b8); padding:2px 0; }`;
    }

    async fetchData(ctx) {
      const cid = ctx.conversationId;
      if (this._lastCid !== cid) {
        this._lastCid = cid;
        this._pickOpen = false;
        this._chains = null;
        this._pickLoading = false;
        this._pickErr = false;
        this._startErr = "";
        this._busy = false;
        this._openExecs = {};
        this._goalRec = null;
      }
      const res = await this._client.getChainExecutions({ conversationId: cid, limit: 8 });
      // C3：模块关（后端 403）→ 整卡隐藏（与 cp-goal 同模式：关闭 ≠ 报错）。
      // 旧 client 的 _get 不带 status → 永不命中，按普通错误走，软降级。
      if (res && res.ok === false && res.status === 403) {
        this._hideCard(true);
        return { ok: false, __forbidden: true };
      }
      this._hideCard(false);
      return res;
    }

    _hideCard(hide) {
      const card = this.closest("[data-cp-card]");
      if (card) card.hidden = !!hide;
    }

    /* ── 渲染 ── */

    renderData(d) {
      const execs = Array.isArray(d && d.executions) ? d.executions : [];
      let html = this._pickOpen ? this._renderPicker() : "";
      if (!execs.length) {
        if (!this._pickOpen) html += this._renderEmpty();
        return html;
      }
      html += execs.map((ex) => this._renderExec(ex)).join("");
      if (!this._pickOpen) {
        html += `<div class="ft"><button data-act="start_pick">${this.esc(this.t("cp.chain.start_btn"))}</button>` +
          this._manageLink() + `</div>`;
      }
      return html;
    }

    _manageLink() {
      if (!HAS_HTTP) return "";
      if (_hostDrawerOpener()) {
        return `<a class="mng" data-act="manage" role="button" tabindex="0">` +
          `${this.esc(this.t("cp.chain.manage_link"))}</a>`;
      }
      return `<a class="mng" href="/workflows" target="_blank" rel="noopener">` +
        `${this.esc(this.t("cp.chain.manage_link"))}</a>`;
    }

    _emptyIllust() {
      return `<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
    }

    _renderEmpty() {
      const esc = (s) => this.esc(s);
      return `<div class="cx-empty">${this._emptyIllust()}` +
        `<div class="cx-lead">${esc(this.t("cp.chain.empty_lead"))}</div>` +
        `<div class="cx-acts"><button class="primary" data-act="start_pick">${esc(this.t("cp.chain.start_btn"))}</button>` +
        this._manageLink() + `</div></div>`;
    }

    _renderPicker() {
      const esc = (s) => this.esc(s);
      const hd = `<div class="pk-t"><span>${esc(this.t("cp.chain.pick_title"))}</span><span class="sp"></span>` +
        `<button data-act="pick_close">${esc(this.t("cp.common.cancel"))}</button></div>`;
      let body;
      if (this._pickLoading) {
        body = `<div class="pk-hint">${esc(this.t("cp.common.loading"))}</div>`;
      } else if (this._pickErr) {
        body = `<div class="pk-err pk-retry" data-act="pick_retry">${esc(this.t("cp.chain.pick_fail"))}</div>`;
      } else {
        // C1：活跃目标命中的推荐链置顶 + 「推荐」徽标（无推荐时保持原序零变化）
        const recId = (this._goalRec && this._goalRec.chainId) || "";
        const list = (this._chains || []).slice();
        if (recId) list.sort((a, b) => Number(b.chain_id === recId) - Number(a.chain_id === recId));
        const rows = list.map((c) => {
          const steps = Array.isArray(c.steps) ? c.steps : [];
          const first = steps[0] || {};
          const preview = String(first.note || first.text || "").slice(0, 40);
          const rec = (recId && c.chain_id === recId)
            ? `<span class="pk-n pk-rec" title="${esc(this.t("cp.chain.rec_hint"))}">\u2605 ${esc(this.t("cp.chain.rec_badge"))}</span>` : "";
          return `<div class="pk-row" data-act="start_run" data-id="${esc(c.chain_id)}" role="button">` +
            `<div class="pk-nm"><span class="nm">${esc(c.name || c.chain_id)}</span>` + rec +
            `<span class="pk-n">${esc(this.t("cp.chain.pick_steps", { n: steps.length }))}</span></div>` +
            (preview ? `<div class="pk-d">${esc(preview)}</div>` : "") + `</div>`;
        }).join("");
        // 零备货冷启动：内联一键导入出厂模板链（幂等），不用离开会话去管理页
        body = rows || `<div class="pk-hint">${esc(this.t("cp.chain.pick_none"))}` +
          (HAS_HTTP ? ` ${this._manageLink()}` : "") + `</div>` +
          `<div class="acts" style="margin-top:6px;"><button data-act="pick_seed">${esc(this.t("cp.chain.seed_btn"))}</button></div>`;
      }
      const err = this._startErr
        ? `<div class="pk-err">${esc(this.t("cp.chain.start_fail", { msg: this._startErr }))}</div>` : "";
      return `<div class="pick">${hd}${body}${err}</div>`;
    }

    _renderExec(ex) {
      const esc = (s) => this.esc(s);
      const status = String(ex.status || "pending");
      const bdgCls = status === "running" ? "run"
        : status === "completed" ? "ok"
        : status === "failed" ? "fail" : "";
      const cls = "exec" + (status === "failed" ? " failed" : "");

      const curIdx = parseInt(ex.current_step, 10) || 0;
      const segs = (ex.steps_preview || []).map((s, i) => {
        let sc = "sg";
        if (s.done) sc += " done";
        else if (s.active) sc += " cur";
        else if (status === "failed" && i === curIdx) sc += " fail";
        const tip = `${i + 1}. ${s.action_label || s.action_type || ""}` +
          (s.note ? `: ${s.note}` : "") +
          (s.delay_hours > 0 ? ` · +${s.delay_hours}h` : "");
        return `<div class="${sc}" title="${esc(tip)}"></div>`;
      }).join("");

      let timing = "";
      if (status === "running") {
        if (ex.countdown_sec > 0) {
          timing = ` · ⏱ ${esc(this.t("cp.chain.next_in", { t: this._fmtDur(ex.countdown_sec) }))}`;
        } else if (ex.is_due) {
          timing = ` · ⏱ ${esc(this.t("cp.chain.due_now"))}`;
        }
      }
      const meta = `<div class="meta">${esc(this.t("cp.chain.step", { cur: ex.current_step_display, total: ex.total_steps }))}` +
        (ex.current_step_label ? ` · ${esc(ex.current_step_label)}` : "") + timing + `</div>`;

      const note = (status === "running" && ex.current_step_note)
        ? `<div class="nxt" title="${esc(ex.current_step_note)}">→ ${esc(String(ex.current_step_note).slice(0, 60))}</div>` : "";
      const last = (ex.last_result && ex.last_result.text)
        ? `<div class="last" title="${esc(ex.last_result.text)}">${esc(String(ex.last_result.text).slice(0, 80))}</div>` : "";

      // 步骤明细抽屉（steps_preview 已带全量前 8 步，纯前端零请求）
      const hasSteps = (ex.steps_preview || []).length > 0;
      const open = hasSteps && !!this._openExecs[ex.exec_id];
      const drawerBtn = hasSteps
        ? `<button type="button" class="stp-btn" data-act="steps_toggle" data-id="${esc(ex.exec_id)}">` +
          (open ? `\u25BE ${esc(this.t("cp.chain.steps_hide"))}` : `\u25B8 ${esc(this.t("cp.chain.steps_show"))}`) +
          `</button>` : "";
      const drawer = open ? this._renderSteps(ex) : "";
      const cancel = status === "running"
        ? `<button data-act="cancel" data-id="${esc(ex.exec_id)}">${esc(this.t("cp.common.cancel"))}</button>` : "";
      const foot = (drawerBtn || cancel)
        ? `<div class="acts" style="justify-content:space-between;align-items:center;">` +
          `<span>${drawerBtn}</span><span>${cancel}</span></div>` : "";

      return `<div class="${cls}">` +
        `<div class="hd"><span class="name" title="${esc(ex.chain_name || "")}">${esc(ex.chain_name || "")}</span>` +
        `<span class="bdg${bdgCls ? " " + bdgCls : ""}">${esc(ex.status_label || status)}</span></div>` +
        (segs ? `<div class="trk">${segs}</div>` : "") +
        meta + note + last + drawer + foot +
        `</div>`;
    }

    _renderSteps(ex) {
      const esc = (s) => this.esc(s);
      const status = String(ex.status || "");
      const curIdx = parseInt(ex.current_step, 10) || 0;
      const rows = (ex.steps_preview || []).map((s, i) => {
        let ic = "\u25CB", cls = "";                       // ○ 待执行
        if (s.done) { ic = "\u2713"; cls = "ok"; }         // ✓ 已完成
        else if (status === "failed" && i === curIdx) { ic = "\u2715"; cls = "fail"; }  // ✕ 失败于此
        else if (s.active) { ic = "\u25B6"; cls = "cur"; } // ▶ 进行中
        const delay = s.delay_hours > 0 ? `<span class="stp-delay">+${esc(s.delay_hours)}h</span>` : "";
        const note = s.note ? `<span class="stp-note">${esc(s.note)}</span>` : "";
        return `<div class="stp-row${cls ? " " + cls : ""}"><span class="stp-ic">${ic}</span>` +
          `<span class="stp-lb">${esc(s.action_label || s.action_type || "")}</span>${delay}${note}</div>`;
      }).join("");
      const more = (ex.total_steps || 0) > (ex.steps_preview || []).length
        ? `<div class="stp-more">${esc(this.t("cp.chain.steps_more", { n: ex.total_steps }))}</div>` : "";
      return `<div>${rows}${more}</div>`;
    }

    /* 倒计时人话化：7200s → 「2小时0分/2h」而非裸秒数 */
    _fmtDur(sec) {
      sec = Math.max(0, Math.floor(Number(sec) || 0));
      if (sec >= 3600) {
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        return m ? this.t("cp.chain.t_hm", { h, m }) : this.t("cp.chain.t_h", { h });
      }
      if (sec >= 60) return this.t("cp.chain.t_m", { m: Math.floor(sec / 60) });
      return this.t("cp.chain.t_s", { s: sec });
    }

    _rerender() {
      this._render(this.renderData(this._d || { executions: [] }));
    }

    /* ── 动作 ── */

    async onAction(act, el) {
      if (act === "manage") {
        const fn = _hostDrawerOpener();
        if (fn) { try { fn("chains"); } catch (_e) { /* ignore */ } }
        return;
      }
      if (act === "pick_close") {
        this._pickOpen = false;
        this._startErr = "";
        this._rerender();
        return;
      }
      if (act === "start_pick" || act === "pick_retry") {
        this._pickOpen = true;
        this._startErr = "";
        // 链列表与目标推荐并行取数，都归位后再补一次渲染（徽标不闪跳）
        await Promise.all([
          this._loadChains(act === "pick_retry"),
          this._loadGoalRec(),
        ]);
        this._rerender();
        return;
      }
      if (act === "start_run") {
        await this._startChain(el);
        return;
      }
      if (act === "pick_seed") {
        await this._seedChains(el);
        return;
      }
      if (act === "steps_toggle") {
        const eid = el && el.getAttribute("data-id");
        if (eid) {
          this._openExecs[eid] = !this._openExecs[eid];
          this._rerender();
        }
        return;
      }
      if (act !== "cancel") return;
      const execId = el.getAttribute("data-id");
      if (!execId) return;
      if (typeof confirm === "function" && !confirm(this.t("cp.chain.cancel_confirm"))) return;
      el.disabled = true;
      let ok = false;
      try {
        const r = await this._client.cancelChainExecution({ execId });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this.emit("cp-action-done", { action_type: "chain_cancel", ok });
      this.refresh();
    }

    /* C1：读当前会话活跃工作目标 → 推荐种子链。软失败设计：goals 模块关(403)/
       无活跃目标/模板无映射/桌面壳(file:// 无同源 API)/网络异常 → 全部落
       {chainId:""} 无徽标；绝不阻塞选择器，绝不重试轰后端（会话内缓存）。
       走裸 fetch 而非 client 适配器：与 cp-goal 同源同口径（该端点属 goals 域，
       桌面壳本就无桥）。 */
    async _loadGoalRec() {
      const cid = (this._ctx || {}).conversationId || "";
      if (!HAS_HTTP || !cid) { this._goalRec = null; return; }
      if (this._goalRec && this._goalRec.cid === cid) return;   // 会话内缓存
      let goal = null;
      try {
        const r = await fetch(
          "/api/goals/for-conversation?conversation_id=" + encodeURIComponent(cid));
        if (r.ok) {
          const d = await r.json();
          goal = (d && d.goal) || null;
        }
      } catch (_e) { goal = null; }
      if (goal && String(goal.status || "") === "active") {
        this._goalRec = {
          cid,
          goalId: String(goal.goal_id || ""),
          chainId: GOAL_CHAIN_REC[String(goal.template || "")] || "",
        };
      } else {
        this._goalRec = { cid, goalId: "", chainId: "" };
      }
    }

    async _loadChains(force) {
      if (!force && Array.isArray(this._chains)) { this._rerender(); return; }
      const fn = this._client && this._client.listWorkflowChains;
      if (typeof fn !== "function") {
        // 旧 client（混缓存 / 桥未升级）：软降级为可重试错误，不炸面板
        this._chains = null;
        this._pickErr = true;
        this._pickLoading = false;
        this._rerender();
        return;
      }
      this._pickLoading = true;
      this._pickErr = false;
      this._rerender();
      let res = null;
      try { res = await this._client.listWorkflowChains(); } catch (_e) { res = null; }
      this._pickLoading = false;
      if (res && res.ok !== false && Array.isArray(res.chains)) {
        this._chains = res.chains.filter((c) => c && c.enabled);
        this._pickErr = false;
      } else {
        this._chains = null;
        this._pickErr = true;
      }
      this._rerender();
    }

    async _seedChains(el) {
      if (this._busy) return;
      const fn = this._client && this._client.seedStarterChains;
      if (typeof fn !== "function") {
        this._startErr = "client outdated";
        this._rerender();
        return;
      }
      this._busy = true;
      if (el) el.disabled = true;
      let res = null;
      try { res = await this._client.seedStarterChains(); } catch (_e) { res = null; }
      this._busy = false;
      if (res && res.ok) {
        this._startErr = "";
        await this._loadChains(true);   // 导入成功 → 强刷列表，种子链即刻可选
        return;
      }
      this._startErr = String((res && res.error) || "network error").slice(0, 120);
      this._rerender();
    }

    async _startChain(el) {
      if (this._busy) return;
      const ctx = this._ctx || {};
      const chainId = el && el.getAttribute("data-id");
      if (!ctx.conversationId || !chainId) return;
      const fn = this._client && this._client.startChain;
      if (typeof fn !== "function") {
        this._startErr = "client outdated";
        this._rerender();
        return;
      }
      this._busy = true;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      // C2：会话有活跃目标即带 goal_id（归因口径=「目标进行期间启动的链」，与后端
      // 事件台账 record_chain_event 同语义；不限于点了推荐项）。旧 client 忽略该参。
      const goalId = (this._goalRec && this._goalRec.cid === ctx.conversationId
        && this._goalRec.goalId) || "";
      let res = null;
      try {
        res = await this._client.startChain({ conversationId: ctx.conversationId, chainId, goalId });
      } catch (_e) { res = null; }
      this._busy = false;
      if (res && res.ok) {
        this._pickOpen = false;
        this._startErr = "";
        this.emit("cp-action-done", { action_type: "chain_start", ok: true });
        this.refresh();
        return;
      }
      this._startErr = String((res && res.error) || "network error").slice(0, 120);
      this._rerender();
    }
  }

  if (!customElements.get("cp-chain-exec")) customElements.define("cp-chain-exec", CpChainExec);
})(typeof window !== "undefined" ? window : this);
