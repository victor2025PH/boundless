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
     纯增强零依赖)。
   P0/P1 增量(2026-08-12,「零使用」复盘——4 条种子链 0 次启动的根因是认知错位+断层):
   - 定位说明恒在:SOP 只按时提醒坐席,绝不自动替坐席给客户发消息(mode_hint 行 +
     empty_lead 诚实化)——「启动了怎么客户没动静」的期待错位从源头消除;
   - 「采用并拟稿」CTA:running/paused 卡展示最近一次已发出的话术建议
     (后端 actionable_note),一键经 cp-goal-drive-draft 既有宿主链(web 收件箱 +
     app 模式两端都已监听,零宿主改动) → setDirective → 自动生成草稿——
     「提醒」变「行动」;
   - 暂停/恢复/跳步/重试:按后端 caps.exec_ops 显隐(旧后端未重启 → 无 caps →
     按钮不出现,绝不「点了 404」);跳步带确认;失败一键重试。 */
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

  /* UI 漏斗埋点（与 cp-goal 同通道同姿势；埋点永不阻断） */
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
    } catch (_e) { /* ignore */ }
  }

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
      this._execOps = false;     // P1：后端 caps.exec_ops（暂停/跳步/重试端点已装载）
      this._opErr = null;        // P1：{id, msg} 单卡操作失败提示（下次成功/刷新即清）
      // 实施92c 旅程条：{cid, stage, src, deals, enabled} / missing=旧后端 404 整条隐藏
      this._journey = null;
      this._jnEdit = false;      // 阶段切换行开合
      this._jnBusy = false;      // 成交/阶段请求在途防双击
      this._jnMsg = "";          // 一次性反馈（已记一单/失败原因）
      // 实施93b 一键发链：目标缓存（全局非会话级）+ 选择行开合
      this._ctaTargets = null;
      this._ctaBase = "";
      this._jnLinkPick = false;
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
      .stp-more { font-size:10px; color:var(--cp-text-tiny,#94a3b8); padding:2px 0; }
      .mode-hint { font-size:10px; color:var(--cp-text-tiny,#94a3b8); line-height:1.4;
                   margin-bottom:var(--cp-gap-xs,4px); }
      .todo { display:flex; gap:6px; align-items:center; margin-top:4px; padding:5px 7px;
              border-radius:6px; background:var(--cp-accent-weak,rgba(79,70,229,.08)); }
      .todo-tx { flex:1 1 auto; min-width:0; font-size:var(--cp-fs-tiny,11px);
                 color:var(--cp-text,#374151); overflow:hidden; text-overflow:ellipsis;
                 white-space:nowrap; }
      .todo button { flex:0 0 auto; }
      .bdg.paused { background:color-mix(in srgb,var(--cp-warn,#d97706) 14%,transparent);
                    color:var(--cp-warn,#d97706); }
      /* 实施92c 旅程条：阶段 chip + 标记成交（打招呼→成交的坐席侧入口） */
      .jn { display:flex; gap:6px; align-items:center; flex-wrap:wrap;
            padding:5px 7px; margin-bottom:var(--cp-gap-xs,4px); border-radius:6px;
            background:var(--cp-surface-2,#f8fafc);
            border:1px solid var(--cp-border,#e2e8f0); }
      .jn-lb { font-size:10px; color:var(--cp-text-tiny,#94a3b8); flex:0 0 auto; }
      .jn-chip { border:0; cursor:pointer; padding:1px 8px; border-radius:99px;
                 font-size:10px; line-height:17px; font-weight:var(--cp-fw-bold,600);
                 background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .jn-chip.on { background:var(--cp-accent-weak,rgba(79,70,229,.12));
                    color:var(--cp-accent,#4f46e5); }
      .jn-chip.deal { background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent);
                      color:var(--cp-ok,#0f9d75); }
      .jn-deals { font-size:10px; color:var(--cp-text-dim,#64748b); flex:0 0 auto; }
      .jn-sp { flex:1 1 auto; }
      .jn-msg { font-size:10px; color:var(--cp-ok,#0f9d75); flex:0 0 auto; }
      .jn-edit { display:flex; gap:4px; flex-wrap:wrap; padding:0 2px 5px; }
      .jn-undo { border:0; background:transparent; cursor:pointer; font-size:10px;
                 color:var(--cp-text-tiny,#94a3b8); text-decoration:underline; }
      .jn-undo:hover { color:var(--cp-danger,#dc2626); }
      .jn-ev { display:flex; gap:6px; align-items:center; flex-wrap:wrap;
               font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
               padding:0 2px 5px; }
      .jn-sug { color:var(--cp-accent,#4f46e5); }`;
    }

    /* 旅程阶段人话标签（顺序=后端 STAGE_ORDER，改一处必同改）。
       实施93：guide 风味（journey 响应 flavor）时 quoting/deal/repeat 换
       引导语系文案（已引导/已转化/再转化）——内部 id 不变只换展示。 */
    _jnStages() { return ["new", "contacted", "nurturing", "quoting", "deal", "repeat"]; }

    _jnFlavor() {
      return String((this._journey && this._journey.flavor) || "sales");
    }

    _jnLabel(stage) {
      let key = String(stage || "unset");
      if (this._jnFlavor() === "guide"
          && ["quoting", "deal", "repeat"].indexOf(key) >= 0) {
        key += "_g";
      }
      const k = "cp.journey.stage." + key;
      const v = this.t(k);
      return (v && v.indexOf("cp.journey.") !== 0) ? v : String(stage || "");
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
        this._opErr = null;
        this._journey = null;
        this._jnEdit = false;
        this._jnBusy = false;
        this._jnMsg = "";
        this._jnLinkPick = false;   // 目标缓存全局，选择行随会话收起
      }
      const res = await this._client.getChainExecutions({ conversationId: cid, limit: 8 });
      // C3：模块关（后端 403）→ 整卡隐藏（与 cp-goal 同模式：关闭 ≠ 报错）。
      // 旧 client 的 _get 不带 status → 永不命中，按普通错误走，软降级。
      if (res && res.ok === false && res.status === 403) {
        this._hideCard(true);
        return { ok: false, __forbidden: true };
      }
      this._hideCard(false);
      // P1 能力位：暂停/跳步/重试端点已装载才显操作按钮（旧后端无 caps=隐藏）
      this._execOps = !!(res && res.caps && res.caps.exec_ops);
      // 实施92c：旅程条异步补载（裸 fetch 与 _loadGoalRec 同口径——该端点属
      // journey 域，桌面壳 file:// 无同源 API → 整条隐藏；绝不阻塞主渲染）
      this._kickJourney(cid);
      return res;
    }

    _kickJourney(cid) {
      if (!HAS_HTTP || !cid) return;
      if (this._journey && this._journey.cid === cid) return;   // 会话内缓存
      const self = this;
      fetch("/api/workspace/conv/" + encodeURIComponent(cid) + "/journey")
        .then((r) => {
          if (r.status === 404 || r.status === 403) return { __missing: true };
          return r.ok ? r.json() : null;
        })
        .then((d) => {
          if (!d) return;
          if (d.__missing) {
            self._journey = { cid, missing: true };
          } else {
            self._journey = {
              cid,
              stage: String(d.stage || ""),
              src: String(d.src || ""),
              deals: Array.isArray(d.deals) ? d.deals : [],
              enabled: !!d.journey_enabled,
              flavor: String(d.flavor || "sales"),
              evidence: (d.evidence && typeof d.evidence === "object")
                ? d.evidence : {},
              suggested: (d.suggested_chain && d.suggested_chain.chain_id)
                ? d.suggested_chain : null,
            };
          }
          if (self._lastCid === cid) self._rerender();
        })
        .catch(() => { /* 网络异常＝本轮无旅程条，不打扰主卡 */ });
    }

    _hideCard(hide) {
      const card = this.closest("[data-cp-card]");
      if (card) card.hidden = !!hide;
    }

    /* ── 渲染 ── */

    renderData(d) {
      const execs = Array.isArray(d && d.executions) ? d.executions : [];
      let html = this._renderJourney(execs);
      html += this._pickOpen ? this._renderPicker() : "";
      if (!execs.length) {
        if (!this._pickOpen) html += this._renderEmpty();
        return html;
      }
      // P0 定位说明恒在：SOP=按时提醒人跟进，不自动发消息（期待错位的源头治理）
      html += `<div class="mode-hint">${this.esc(this.t("cp.chain.mode_hint"))}</div>`;
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

    /* 实施92c 旅程条：阶段 chip（点击展开切换行）+ 成交计数 + 标记成交/撤销。
       journey 数据未到/旧后端 404/桌面壳 file:// → 返回空串整条隐藏（特性探测）。 */
    _renderJourney(execs) {
      const j = this._journey;
      if (!j || j.missing || j.cid !== this._lastCid) return "";
      const esc = (s) => this.esc(s);
      const stage = String(j.stage || "");
      const chipCls = "jn-chip" + (stage === "deal" || stage === "repeat"
        ? " deal" : (stage ? " on" : ""));
      const deals = (j.deals || []).filter((x) => !x.revoked);
      const dealsLine = deals.length
        ? `<span class="jn-deals">💰 ${esc(this.t("cp.journey.deals_n", { n: deals.length }))}</span>` +
          `<button type="button" class="jn-undo" data-act="jn_revoke" title="${esc(this.t("cp.journey.revoke_confirm"))}">${esc(this.t("cp.journey.revoke"))}</button>`
        : "";
      const msg = this._jnMsg
        ? `<span class="jn-msg">${esc(this._jnMsg)}</span>` : "";
      const markKey = this._jnFlavor() === "guide"
        ? "cp.journey.mark_conv" : "cp.journey.mark_deal";
      let html = `<div class="jn">` +
        `<span class="jn-lb">${esc(this.t("cp.journey.stage_label"))}</span>` +
        `<button type="button" class="${chipCls}" data-act="jn_edit" ` +
        `title="${esc(this.t("cp.journey.stage_hint"))}">${esc(this._jnLabel(stage))}</button>` +
        dealsLine + msg + `<span class="jn-sp"></span>` +
        `<button type="button" class="jn-chip" data-act="jn_link" ` +
        `title="${esc(this.t("cp.journey.link_hint"))}" ` +
        `aria-label="${esc(this.t("cp.journey.link_hint"))}">\u{1F517}</button>` +
        `<button type="button" class="jn-chip" data-act="jn_deal">` +
        `${esc(this.t(markKey))}</button>` +
        `</div>`;
      if (this._jnEdit) {
        html += `<div class="jn-edit">` + this._jnStages().map((s) =>
          `<button type="button" class="jn-chip${s === stage ? " on" : ""}" ` +
          `data-act="jn_set" data-stage="${esc(s)}">${esc(this._jnLabel(s))}</button>`,
        ).join("") + `</div>`;
      }
      // 实施93b 一键发链目标选择行（仅当多目标时展开；单目标直铸不进这里）
      if (this._jnLinkPick) {
        const ts = (this._ctaTargets || []).filter((t) => t && t.enabled);
        html += `<div class="jn-edit">` +
          `<span class="jn-lb">${esc(this.t("cp.journey.link_pick"))}</span>` +
          (ts.length
            ? ts.map((t) =>
              `<button type="button" class="jn-chip" data-act="jn_link_go" ` +
              `data-id="${esc(t.target_id)}">${esc(t.name || t.target_id)}</button>`,
            ).join("")
            : `<span class="jn-msg">${esc(this.t("cp.journey.link_none"))}</span>`) +
          `</div>`;
      }
      html += this._renderEvidence(j, execs);
      return html;
    }

    /* 实施92d「为什么是现在」证据行 + 一键建议（有界建议卡结构：触发原因
       可量化、动作可一键、可忽略不纠缠）。低于 1h 的等待不渲染（噪音地板）。 */
    _renderEvidence(j, execs) {
      const esc = (s) => this.esc(s);
      const ev = (j && j.evidence) || {};
      const wait = Number(ev.wait_hours) || 0;
      const parts = [];
      if (wait >= 1 && ev.waiting_on === "customer") {
        parts.push("⏳ " + esc(this.t("cp.journey.ev_wait_customer",
          { t: this._fmtDur(wait * 3600) })));
      } else if (wait >= 1 && ev.waiting_on === "us") {
        parts.push("📥 " + esc(this.t("cp.journey.ev_wait_us",
          { t: this._fmtDur(wait * 3600) })));
      }
      // 建议链：仅当前会话无在途执行（running/paused）时展示——链在场＝
      // 节奏已被接管，再建议第二条是添乱
      let suggest = "";
      const hasLive = (execs || []).some((x) =>
        x && (x.status === "running" || x.status === "paused"));
      if (j && j.suggested && !hasLive) {
        suggest = `<span class="jn-sug">${esc(this.t("cp.journey.suggest",
          { name: String(j.suggested.name || "") }))}</span>` +
          `<button type="button" class="jn-chip on" data-act="start_run" ` +
          `data-id="${esc(j.suggested.chain_id)}">` +
          `${esc(this.t("cp.journey.suggest_btn"))}</button>`;
      }
      if (!parts.length && !suggest) return "";
      return `<div class="jn-ev">` +
        (parts.length ? `<span>${parts.join(" · ")}</span>` : "") +
        suggest + `</div>`;
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
          const autoBdg = c.exec_mode === "auto"
            ? `<span class="pk-n pk-rec" title="${esc(this.t("cp.chain.auto_badge_hint"))}">\u26A1 ${esc(this.t("cp.chain.auto_badge"))}</span>` : "";
          return `<div class="pk-row" data-act="start_run" data-id="${esc(c.chain_id)}" role="button">` +
            `<div class="pk-nm"><span class="nm">${esc(c.name || c.chain_id)}</span>` + rec + autoBdg +
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
        : status === "paused" ? "paused"
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

      // P1 行动闭环：最近已发出的话术建议 + 「采用并拟稿」（宿主拟稿链见 onAction）
      const todo = ((status === "running" || status === "paused") && ex.actionable_note)
        ? `<div class="todo"><span class="todo-tx" title="${esc(ex.actionable_note)}">📌 ${esc(String(ex.actionable_note).slice(0, 64))}</span>` +
          `<button class="primary" data-act="drive_draft" data-id="${esc(ex.exec_id)}">${esc(this.t("cp.chain.drive_btn"))}</button></div>`
        : "";

      // 步骤明细抽屉（steps_preview 已带全量前 8 步，纯前端零请求）
      const hasSteps = (ex.steps_preview || []).length > 0;
      const open = hasSteps && !!this._openExecs[ex.exec_id];
      const drawerBtn = hasSteps
        ? `<button type="button" class="stp-btn" data-act="steps_toggle" data-id="${esc(ex.exec_id)}">` +
          (open ? `\u25BE ${esc(this.t("cp.chain.steps_hide"))}` : `\u25B8 ${esc(this.t("cp.chain.steps_show"))}`) +
          `</button>` : "";
      const drawer = open ? this._renderSteps(ex) : "";
      // 操作组：caps.exec_ops（后端已装载新端点）才出暂停/恢复/跳步/重试；
      // 取消按旧行为恒在 running（paused 的取消也需新后端，同 caps 门控）
      const ops = [];
      const eid = esc(ex.exec_id);
      if (this._execOps) {
        if (status === "running") {
          ops.push(`<button data-act="skip" data-id="${eid}">${esc(this.t("cp.chain.skip_btn"))}</button>`);
          ops.push(`<button data-act="pause" data-id="${eid}">${esc(this.t("cp.chain.pause_btn"))}</button>`);
        } else if (status === "paused") {
          ops.push(`<button class="primary" data-act="resume" data-id="${eid}">${esc(this.t("cp.chain.resume_btn"))}</button>`);
        } else if (status === "failed") {
          ops.push(`<button data-act="retry" data-id="${eid}">${esc(this.t("cp.chain.retry_btn"))}</button>`);
        }
      }
      if (status === "running" || (this._execOps && status === "paused")) {
        ops.push(`<button data-act="cancel" data-id="${eid}">${esc(this.t("cp.common.cancel"))}</button>`);
      }
      const opErr = (this._opErr && this._opErr.id === ex.exec_id)
        ? `<div class="pk-err">${esc(this.t("cp.chain.op_fail", { msg: this._opErr.msg }))}</div>` : "";
      const foot = (drawerBtn || ops.length)
        ? `<div class="acts" style="justify-content:space-between;align-items:center;">` +
          `<span>${drawerBtn}</span><span style="display:flex;gap:4px;">${ops.join("")}</span></div>` : "";

      const autoBdg = (ex.exec_mode === "auto")
        ? `<span class="bdg" title="${esc(this.t("cp.chain.auto_badge_hint"))}">\u26A1 ${esc(this.t("cp.chain.auto_badge"))}</span>` : "";
      return `<div class="${cls}">` +
        `<div class="hd"><span class="name" title="${esc(ex.chain_name || "")}">${esc(ex.chain_name || "")}</span>` +
        autoBdg +
        `<span class="bdg${bdgCls ? " " + bdgCls : ""}">${esc(ex.status_label || status)}</span></div>` +
        (segs ? `<div class="trk">${segs}</div>` : "") +
        meta + note + todo + last + drawer + opErr + foot +
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
      if (act === "jn_edit") {
        this._jnEdit = !this._jnEdit;
        this._rerender();
        return;
      }
      if (act === "jn_set") {
        await this._journeySetStage(el && el.getAttribute("data-stage"));
        return;
      }
      if (act === "jn_deal") {
        await this._journeyMarkDeal();
        return;
      }
      if (act === "jn_link") {
        await this._jnLinkToggle();
        return;
      }
      if (act === "jn_link_go") {
        await this._jnLinkMint(el && el.getAttribute("data-id"));
        return;
      }
      if (act === "jn_revoke") {
        await this._journeyRevokeDeal();
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
      if (act === "drive_draft") {
        this._driveDraft(el);
        return;
      }
      if (act === "pause" || act === "resume" || act === "skip" || act === "retry") {
        await this._execOp(act, el);
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

    /* P1「采用并拟稿」：把最近已发出的话术建议包成坐席指令，走 cp-goal-drive-draft
       既有宿主链（web 收件箱 + app 模式两端都监听：setDirective → 自动生成草稿）。
       事件名带 goal 是历史命名，语义早已是通用「驱动拟稿」通道（英雄卡/缺口 chip/
       本卡共用）——复用=零宿主改动，且两端表面自动同权。 */
    _driveDraft(el) {
      const eid = (el && el.getAttribute("data-id")) || "";
      const execs = (this._d && this._d.executions) || [];
      const ex = execs.find((x) => x && x.exec_id === eid);
      const note = String((ex && ex.actionable_note) || "").trim();
      if (!ex || !note) return;
      let text = this.t("cp.chain.drive_instruction",
        { chain: String(ex.chain_name || ""), text: note,
          n: (parseInt(ex.actionable_step_idx, 10) || 0) + 1 });
      if (!text || String(text).indexOf("cp.chain.") === 0) text = note;
      this.emit("cp-goal-drive-draft", {
        intent: note,
        instruction: String(text),
        pushLevel: "soft",
        label: String(ex.chain_name || ""),
        source: "chain",
      });
      _beacon("chain_drive_draft");
    }

    /* P1 执行操作（暂停/恢复/跳步/重试）：同构薄壳——client 方法缺失（桌面壳
       桥未升级）→ 单卡就地报错不炸面板；成功 → cp-action-done + 整卡刷新。 */
    async _execOp(act, el) {
      const eid = (el && el.getAttribute("data-id")) || "";
      if (!eid || this._busy) return;
      if (act === "skip" && typeof confirm === "function"
          && !confirm(this.t("cp.chain.skip_confirm"))) return;
      const method = { pause: "pauseChainExecution", resume: "resumeChainExecution",
        skip: "skipChainStep", retry: "retryChainExecution" }[act];
      const fn = this._client && this._client[method];
      if (typeof fn !== "function") {
        this._opErr = { id: eid, msg: "client outdated" };
        this._rerender();
        return;
      }
      this._busy = true;
      if (el) el.disabled = true;
      let res = null;
      try { res = await this._client[method]({ execId: eid }); } catch (_e) { res = null; }
      this._busy = false;
      if (res && res.ok) {
        this._opErr = null;
        _beacon("chain_op_" + act);
        this.emit("cp-action-done", { action_type: "chain_" + act, ok: true });
        this.refresh();
        return;
      }
      this._opErr = { id: eid, msg: String((res && res.error) || "network error").slice(0, 120) };
      this._rerender();
    }

    /* ── 实施92c 旅程条动作（裸 fetch 同 _kickJourney 口径；busy 互斥防双击；
       成功后清缓存重拉=阶段/成交台账以服务端为准，不本地猜） ── */

    async _journeyFetch(path, body) {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      let d = null;
      try { d = await r.json(); } catch (_e) { d = null; }
      return { httpOk: r.ok, data: d };
    }

    _journeyReload(flash) {
      const cid = this._lastCid;
      this._journey = null;          // 清会话缓存 → _kickJourney 重拉
      this._jnMsg = String(flash || "");
      this._kickJourney(cid);
      this._rerender();
      if (this._jnMsg) {
        setTimeout(() => {
          this._jnMsg = "";
          try { this._rerender(); } catch (_e) { /* ignore */ }
        }, 4000);
      }
    }

    async _journeySetStage(stage) {
      const cid = this._lastCid;
      if (!cid || !stage || this._jnBusy) return;
      this._jnBusy = true;
      let ok = false;
      try {
        const r = await this._journeyFetch(
          "/api/workspace/conv/" + encodeURIComponent(cid) + "/journey/stage",
          { stage });
        ok = !!(r.httpOk && r.data && r.data.ok);
      } catch (_e) { ok = false; }
      this._jnBusy = false;
      this._jnEdit = false;
      if (ok) {
        _beacon("journey_stage_set");
        this._journeyReload("");
      } else {
        this._jnMsg = this.t("cp.journey.op_fail");
        this._rerender();
      }
    }

    async _journeyMarkDeal() {
      const cid = this._lastCid;
      if (!cid || this._jnBusy) return;
      let amount = 0;
      // #173：原生 prompt 在 Electron 渲染进程会抛，统一走页内弹层 window.uiPrompt；
      // 宿主未装载弹层则按「不填金额」记 0（与旧的 typeof 守卫语义一致）。
      if (typeof window.uiPrompt === "function") {
        const raw = await window.uiPrompt(this.t("cp.journey.deal_prompt"), "", { type: "text" });
        if (raw === null) return;                    // 取消＝不记
        amount = parseFloat(String(raw).replace(/[^\d.]/g, "")) || 0;
      }
      this._jnBusy = true;
      let res = null;
      try {
        res = await this._journeyFetch(
          "/api/workspace/conv/" + encodeURIComponent(cid) + "/deal",
          { amount });
      } catch (_e) { res = null; }
      this._jnBusy = false;
      if (res && res.httpOk && res.data && res.data.ok) {
        _beacon("journey_deal_marked");
        this.emit("cp-action-done", { action_type: "journey_deal", ok: true });
        this._journeyReload(this.t("cp.journey.deal_done"));
        return;
      }
      this._jnMsg = this.t("cp.journey.deal_fail",
        { msg: String((res && res.data && res.data.detail) || "network").slice(0, 40) });
      this._rerender();
    }

    /* ── 实施93b 一键发链：铸客户专属追踪短链 → cp-fill 填入输入框 ──
       单启用目标直铸零选择；多目标展开选择行；public_base 未配置/无目标/
       旧后端 404 → 一次性提示不打扰。铸链即服务端推进「已引导」，成功后
       重拉旅程条让阶段 chip 即时跟上。 */

    async _jnLinkToggle() {
      if (this._jnLinkPick) {
        this._jnLinkPick = false;
        this._rerender();
        return;
      }
      if (!HAS_HTTP || this._jnBusy) return;
      if (!this._ctaTargets) {
        try {
          const r = await fetch("/api/workspace/cta-targets");
          if (r.ok) {
            const d = await r.json();
            this._ctaTargets = Array.isArray(d && d.targets) ? d.targets : [];
            this._ctaBase = String((d && d.public_base) || "");
          } else {
            this._ctaTargets = [];
            this._ctaBase = "";
          }
        } catch (_e) {
          this._ctaTargets = [];
          this._ctaBase = "";
        }
      }
      if (!this._ctaBase) {
        this._jnMsg = this.t("cp.journey.link_nobase");
        this._rerender();
        return;
      }
      const enabled = (this._ctaTargets || []).filter((t) => t && t.enabled);
      if (!enabled.length) {
        this._jnMsg = this.t("cp.journey.link_none");
        this._rerender();
        return;
      }
      if (enabled.length === 1) {
        await this._jnLinkMint(enabled[0].target_id);
        return;
      }
      this._jnLinkPick = true;
      this._rerender();
    }

    async _jnLinkMint(targetId) {
      const cid = this._lastCid;
      if (!cid || !targetId || this._jnBusy) return;
      this._jnBusy = true;
      let res = null;
      try {
        res = await this._journeyFetch(
          "/api/workspace/conv/" + encodeURIComponent(cid) + "/cta-link",
          { target_id: targetId });
      } catch (_e) { res = null; }
      this._jnBusy = false;
      this._jnLinkPick = false;
      const url = (res && res.httpOk && res.data && res.data.ok
        && res.data.url) || "";
      if (url) {
        _beacon("journey_link_minted");
        this.emit("cp-fill", { text: String(url), source: "journey_link" });
        this.emit("cp-action-done", { action_type: "journey_link", ok: true });
        this._journeyReload(this.t("cp.journey.link_filled"));
        return;
      }
      this._jnMsg = this.t("cp.journey.link_fail",
        { msg: String((res && res.data && res.data.detail) || "network").slice(0, 40) });
      this._rerender();
    }

    async _journeyRevokeDeal() {
      const cid = this._lastCid;
      const j = this._journey;
      if (!cid || !j || this._jnBusy) return;
      const deals = (j.deals || []).filter((x) => !x.revoked);
      if (!deals.length) return;
      if (typeof confirm === "function"
          && !confirm(this.t("cp.journey.revoke_confirm"))) return;
      const latest = deals[0];                       // 服务端按 ts DESC 返回
      this._jnBusy = true;
      let ok = false;
      try {
        const r = await this._journeyFetch(
          "/api/workspace/conv/" + encodeURIComponent(cid)
          + "/deal/" + encodeURIComponent(latest.id) + "/revoke", {});
        ok = !!(r.httpOk && r.data && r.data.ok);
      } catch (_e) { ok = false; }
      this._jnBusy = false;
      if (ok) {
        _beacon("journey_deal_revoked");
        this._journeyReload("");
      } else {
        this._jnMsg = this.t("cp.journey.op_fail");
        this._rerender();
      }
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
