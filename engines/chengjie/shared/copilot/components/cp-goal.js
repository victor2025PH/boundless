"use strict";
/* 两端共享组件 · 工作目标(<cp-goal>)— 继承 CpPanelBase
   统一收件箱右栏「工作目标」卡（对接 /api/goals*，后端 goal_routes.py）：
   - 无目标：插画空态 + 内联建目标表单（P18 场景卡片选模板：每模板一句人话适用
     场景，custom 收进「进阶」入口且不参与默认预选；沉默 ≥72h 时推荐「沉默唤回」
     （ctx.goalHint.silentHours 由宿主喂）；期限/自治档保留偏好记忆）
   - 进行中(active/paused)：标题+状态、里程碑条(节点+当前名常显)、第X/Y天、
     今日拍(语义色条 / hold)、采纳/驳回(+5s 撤销)、按意图生成草稿、
     「AI 做了什么」进展时间线(懒取 /api/goals/{id} 的拍史)、画像/产品——
     画像缺口 chip 可点(P20)：拟稿去问(经 cp-goal-drive-draft 走既有拟稿链)
     或补录聚焦；已填 chip 点击进补录表单；双零不渲染 0% 完成度条、
     暂停/标成交(可选归因)/放弃(⋯菜单)；建目标后出一次性「接下来会发生什么」
     提示（按自治档如实说明，auto 档在 caps.bridge_enabled=false 时如实注明
     「不会自己主动发消息」——文案与真实行为一致是硬原则）
   - 终态：中性 badge + 「再设一个」为主角
   - 403：默认隐藏；inbox.goal.show_disabled_hint=1 时显示灰字提示（关闭≠不存在）
   埋点：曝光/展开设定/反馈/场景选择/进展展开 → /api/telemetry/ui-event
   文案：inbox.goal.* → window.T/Tf；共享键走 CopilotShared.t
   用法:
     const el = document.createElement('cp-goal');
     el.context = { conversationId, goalHint: { silentHours } }; */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-goal: CpPanelBase 未加载"); return; }

  const LANG = (root.CopilotShared && root.CopilotShared.lang) === "en" ? "en" : "zh";
  const TERMINAL = { done: 1, failed: 1, expired: 1, cancelled: 1 };
  const AUTONOMY = ["observe", "suggest", "auto"];
  const PUSH_LEVELS = ["none", "soft", "direct"];
  const HOLDS = ["emotion", "silent", "no_intent"];
  // P22：生命周期/人设自动创建的来源 → UI「AI 自建」徽标
  const AUTO_ORIGIN = { auto_create: 1, winback_auto: 1, retention_auto: 1, reconvert_auto: 1 };
  const PREFS_KEY = "cp_goal_form_prefs_v1";
  const UNDO_MS = 5000;

  /* C1：目标模板 → 配套跟进 SOP 映射。**单一事实源＝CopilotShared.GOAL_CHAIN_REC**
     （sidebar-chrome.js），此处仅引用——改映射去那里。链未导入/未启用则整行不显示
     （绝不复活运营刻意删掉的种子链）；旧缓存混态 → 空表＝无推荐行软降级。 */
  const CHAIN_RECO = (root.CopilotShared && root.CopilotShared.GOAL_CHAIN_REC) || {};

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
      this._formAutonomy = "";       // 场景切换重渲时保住坐席未提交的自治档选择
      this._formUsePrefDays = true;  // 首开用偏好天数；坐席换场景后跟场景默认
      this._createdHintAutonomy = ""; // 建目标后一次性「接下来会发生什么」提示
      this._createdHintTid = "";      // 建目标所用模板（配套 SOP 推荐的映射键）
      this._chainReco = null;         // {chain:{chain_id,name}, state:'idle'|'started', err}
      this._lastCid = "";
      this._profOpen = false;
      this._slotAsk = "";            // 画像缺口 chip 展开的追问行（slot key）
      this._wonFormOpen = false;
      this._moreOpen = false;
      this._undoTimer = null;
      this._undoUntil = 0;
      this._prevMsIdx = -1;
      this._celebrateMs = false;
      this._exposed = false;
      this._toastText = "";
      this._toastTimer = null;
      this._progOpen = false;        // 「AI 做了什么」进展时间线（懒取拍史）
      this._progGoalId = "";
      this._progRows = null;
      this._progLoading = false;
      this._progErr = false;

      this.shadowRoot.addEventListener("change", (e) => {
        const t = e.target;
        if (!t || !t.getAttribute) return;
        if (t.getAttribute("data-chg") === "autonomy") this._syncAutonomyHint();
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
                color:var(--cp-text-tiny,#94a3b8); white-space:nowrap; cursor:pointer; }
      .gl-tag:hover { filter:brightness(0.96); border-color:var(--cp-accent,#4f46e5);
                      color:var(--cp-accent,#4f46e5); }
      .gl-origin { display:inline-block; padding:1px 6px; border-radius:var(--cp-radius-sm,6px);
                   font-size:var(--cp-fs-tiny,11px); background:rgba(15,157,117,.12);
                   color:var(--cp-ok,#0f9d75); white-space:nowrap; }
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
      .gl-pill.none { background:color-mix(in srgb,var(--cp-text-tiny,#94a3b8) 16%,transparent);
                      color:var(--cp-text-dim,#64748b); cursor:help; }
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
      /* P20 排版收口：头部拆两行（标题行 / 完成度行）——旧版单行 flex 硬塞
         标题+双 bar+按钮 ≈304px，默认 300px 侧栏内容区只有 ~236px，溢出后
         CJK 逐字换行成竖排。规则：文本/按钮一律 nowrap，bar 弹性伸缩。 */
      .gl-prof-hd { display:flex; align-items:center; gap:6px; flex-wrap:wrap;
                    font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .gl-prof-hd .sp { flex:1 1 auto; }
      .gl-prof-hd button { font-size:10px; padding:1px 7px; white-space:nowrap; flex:0 0 auto; }
      .gl-prof-t { font-weight:600; white-space:nowrap; }
      .gl-fillrow { display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:5px;
                    font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .gl-fillbar { display:flex; align-items:center; gap:4px; flex:1 1 96px; min-width:0; }
      .gl-fillbar > span:first-child { white-space:nowrap; }
      .gl-fillbar .bar { flex:1 1 24px; min-width:24px; height:6px; border-radius:99px;
                         background:var(--cp-track,#e2e8f0); overflow:hidden; }
      .gl-fillbar .bar i { display:block; height:100%; background:var(--cp-accent,#4f46e5); }
      .gl-fillbar .bar.b2 i { background:var(--cp-ok,#0f9d75); }
      .gl-fillbar .pct { white-space:nowrap; font-variant-numeric:tabular-nums; }
      .gl-chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:5px; }
      .gl-chip { display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis;
                 white-space:nowrap; padding:1px 7px; border-radius:99px; line-height:1.5;
                 font-size:var(--cp-fs-tiny,11px); border:1px solid var(--cp-border,#e2e8f0);
                 color:var(--cp-text,#374151); background:var(--cp-surface,#fff);
                 text-decoration:none; }
      a.gl-chip:hover { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); }
      button.gl-chip { cursor:pointer; }
      button.gl-chip:hover { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); }
      .gl-chip.miss { color:var(--cp-text-tiny,#94a3b8); border-style:dashed; background:transparent; }
      button.gl-chip.miss:hover, .gl-chip.miss.on {
                 color:var(--cp-accent,#4f46e5); border-color:var(--cp-accent,#4f46e5);
                 background:var(--cp-accent-weak,rgba(79,70,229,.08)); }
      .gl-ask { margin-top:5px; padding:6px 8px; border-radius:8px;
                background:var(--cp-accent-weak,rgba(79,70,229,.06));
                border:1px dashed color-mix(in srgb,var(--cp-accent,#4f46e5) 45%,transparent); }
      .gl-ask-q { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#374151); line-height:1.5; }
      .gl-ask .acts { margin-top:4px; justify-content:flex-end; }
      .gl-prof-form { display:grid; grid-template-columns:1fr 1fr; gap:4px 6px; margin-top:4px; }
      .gl-pf-group { grid-column:1 / -1; font-size:10px; font-weight:700; letter-spacing:.4px;
                     color:var(--cp-text-tiny,#94a3b8); margin-top:3px; }
      .gl-pf-group:first-child { margin-top:0; }
      .gl-prof-form input { width:100%; box-sizing:border-box; font:inherit;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:3px 6px; }
      .gl-prof-form input::placeholder { color:var(--cp-text-tiny,#94a3b8); opacity:.75; }
      .gl-prof-empty { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                       margin-top:3px; line-height:1.45; }
      .gl-prods { display:flex; flex-direction:column; gap:4px; margin-top:5px; }
      .gl-prod { display:grid; grid-template-columns:1fr auto; gap:0 8px; align-items:baseline;
                 padding:5px 8px; border:1px solid var(--cp-border,#e2e8f0); border-radius:8px;
                 background:var(--cp-surface,#fff); text-decoration:none; color:inherit; }
      a.gl-prod:hover { border-color:var(--cp-accent,#4f46e5); }
      a.gl-prod:hover .gl-prod-nm { color:var(--cp-accent,#4f46e5); }
      .gl-prod-nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); min-width:0; overflow:hidden;
                    text-overflow:ellipsis; white-space:nowrap;
                    display:inline-flex; align-items:center; gap:3px; }
      .gl-prod-nm svg { flex:0 0 auto; opacity:.6; }
      .gl-prod-price { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                       white-space:nowrap; font-variant-numeric:tabular-nums; }
      .gl-prod-pitch { grid-column:1 / -1; font-size:var(--cp-fs-tiny,11px);
                       color:var(--cp-text-dim,#64748b); overflow:hidden;
                       text-overflow:ellipsis; white-space:nowrap; }
      .gl-empty { text-align:center; padding:8px 4px 4px; }
      .gl-empty svg { display:block; margin:0 auto 8px; color:var(--cp-accent,#4f46e5); opacity:.85; }
      .gl-toast { font-size:var(--cp-fs-tiny,11px); color:var(--cp-ok,#0f9d75); margin-top:4px; }
      .gl-draft-btn { font-size:10px; padding:1px 7px; margin-left:auto; }
      .gl-scenlist { display:flex; flex-direction:column; gap:4px; }
      .gl-scen { border:1px solid var(--cp-border,#e2e8f0); border-radius:8px; padding:6px 9px;
                 cursor:pointer; background:var(--cp-surface,#fff); }
      .gl-scen.sel { border-color:var(--cp-accent,#4f46e5);
                     background:var(--cp-accent-weak,rgba(79,70,229,.06));
                     box-shadow:0 0 0 1px var(--cp-accent,#4f46e5) inset; }
      .gl-scen-nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); display:flex; align-items:center; gap:6px; }
      .gl-scen-d { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                   line-height:1.45; margin-top:1px; }
      .gl-scen-rec { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px;
                     line-height:16px; background:var(--cp-accent,#4f46e5); color:#fff; }
      .gl-adv { background:transparent; border:none; cursor:pointer; text-align:left;
                font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                text-decoration:underline; padding:2px 0; }
      .gl-anote { color:var(--cp-warn,#b45309); }
      .gl-created { border-left-color:var(--cp-ok,#0f9d75);
                    background:color-mix(in srgb,var(--cp-ok,#0f9d75) 7%,var(--cp-surface-2,#f8fafc)); }
      .gl-created-tx { font-size:var(--cp-fs-sm,12px); line-height:1.55; color:var(--cp-text,#374151); }
      .gl-chainreco { display:flex; gap:6px; align-items:center; margin-top:6px;
                      font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#374151); }
      .gl-chainreco .tx { flex:1 1 auto; min-width:0; }
      .gl-chainreco button { font-size:11px; padding:2px 9px; flex:0 0 auto; }
      .gl-chainreco.ok { color:var(--cp-ok,#0f9d75); }
      .gl-prog { margin:var(--cp-gap-xs,4px) 0; }
      .gl-prog-btn { background:transparent; border:none; cursor:pointer; padding:2px 0;
                     font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .gl-prog-btn:hover { color:var(--cp-accent,#4f46e5); }
      .gl-prog-row { display:flex; gap:6px; align-items:baseline; font-size:var(--cp-fs-tiny,11px);
                     padding:2px 0; border-bottom:1px dashed var(--cp-border,#e2e8f0); }
      .gl-prog-row:last-child { border-bottom:none; }
      .gl-prog-day { flex:0 0 auto; color:var(--cp-text-tiny,#94a3b8); font-variant-numeric:tabular-nums; }
      .gl-prog-st { flex:0 0 auto; padding:0 6px; border-radius:99px;
                    background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .gl-prog-st.st-sent,.gl-prog-st.st-consumed {
                    background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent);
                    color:var(--cp-ok,#0f9d75); }
      .gl-prog-st.st-skipped,.gl-prog-st.st-blocked { color:var(--cp-text-tiny,#94a3b8); }
      .gl-prog-tx { flex:1 1 auto; min-width:0; color:var(--cp-text,#374151);
                    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }`;
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
        this._formTid = "";
        this._formAutonomy = "";
        this._formUsePrefDays = true;
        this._createdHintAutonomy = "";
        this._createdHintTid = "";
        this._chainReco = null;
        this._profOpen = false;
        this._slotAsk = "";
        this._wonFormOpen = false;
        this._moreOpen = false;
        this._clearUndo();
        this._prevMsIdx = -1;
        this._celebrateMs = false;
        this._exposed = false;
        this._progOpen = false;
        this._progGoalId = "";
        this._progRows = null;
        this._progLoading = false;
        this._progErr = false;
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
      const hint = this._autonomyHint(lvl) || this.t("inbox.goal.autonomy_cycle_t");
      const tag = `<button type="button" class="gl-tag" data-act="autonomy_cycle"` +
        ` title="${esc(hint)}">${esc(this._autonomyLabel(lvl))}</button>`;
      const origin = AUTO_ORIGIN[String(g.created_by || "")]
        ? `<span class="gl-origin" title="${esc(this.t("inbox.goal.origin.auto_t"))}">` +
          `${esc(this.t("inbox.goal.origin.auto"))}</span>`
        : "";

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
          // 主动作=「采纳并拟稿」（合并坐席最常见的两连击：采纳 → 按意图生成草稿）；
          // 👍 降级为「只采纳」；已采纳态仍保留拟稿入口（采纳后想动手时不断路）。
          fb = (beat.detail === "adopted")
            ? `<span class="gl-fb"><span class="gl-fbmark">\u2713 ${esc(this.t("inbox.goal.adopted_mark"))}</span>` +
              `<button class="gl-draft-btn" data-act="drive_draft" title="${esc(this.t("inbox.goal.draft_from_intent"))}">` +
              `${esc(this.t("inbox.goal.draft_from_intent"))}</button></span>`
            : `<span class="gl-fb">` +
              `<button data-act="beat_adopt" title="${esc(this.t("inbox.goal.act.adopt_only"))}">\uD83D\uDC4D</button>` +
              `<button data-act="beat_reject" title="${esc(this.t("inbox.goal.act.reject"))}">\uD83D\uDC4E</button>` +
              `<button class="primary gl-draft-btn" data-act="beat_adopt_draft" title="${esc(this.t("inbox.goal.act.adopt_draft_t"))}">` +
              `${esc(this.t("inbox.goal.act.adopt_draft"))}</button></span>`;
        }
        const pushTip = pl === "none" ? this.t("inbox.goal.push.none_tip") : "";
        today = `<div class="gl-sec ${secCls}"><div class="gl-today">` +
          `<span class="gl-pill ${pl}"${pushTip ? ` title="${esc(pushTip)}"` : ""}>` +
          `${esc(this.t("inbox.goal.push." + pl))}</span>` +
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
          ? `<div class="gl-more-pop">` +
            `<button data-act="redirect">${esc(this.t("inbox.goal.act.redirect"))}</button>` +
            `<button data-act="cancel">${esc(this.t("inbox.goal.act.cancel"))}</button></div>`
          : "") +
        `</span>`;
      const acts = `<div class="acts gl-acts">${flip}${more}` +
        `<button class="primary" data-act="won">${esc(this.t("inbox.goal.act.won"))}</button></div>`;

      let createdHint = "";
      if (this._createdHintAutonomy) {
        const ck = AUTONOMY.indexOf(this._createdHintAutonomy) >= 0
          ? this._createdHintAutonomy : "suggest";
        // C1：配套跟进 SOP 推荐（链存在且启用才显示；挂上后原位变确认行）
        let reco = "";
        const rc = this._chainReco;
        if (rc && rc.chain) {
          reco = rc.state === "started"
            ? `<div class="gl-chainreco ok">\u2713 ${esc(this.t("cp.goal.chain_reco_ok", { name: rc.chain.name }))}</div>`
            : `<div class="gl-chainreco"><span class="tx">${esc(this.t("cp.goal.chain_reco", { name: rc.chain.name }))}</span>` +
              `<button class="primary" data-act="chain_reco_start">${esc(this.t("cp.goal.chain_reco_btn"))}</button></div>` +
              (rc.err ? `<div class="gl-ferr">${esc(rc.err)}</div>` : "");
        }
        createdHint = `<div class="gl-sec gl-created">` +
          `<div class="gl-created-tx">${esc(this.t("inbox.goal.created_next." + ck))}</div>` + reco +
          `<div class="acts gl-acts"><button data-act="hint_dismiss">${esc(this.t("inbox.goal.hint_got_it"))}</button></div></div>`;
      }

      return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}${origin}${tag}</div>` + createdHint + track + meta + today +
        this._renderProgress() + this._renderProfile() + this._renderProducts(g) + acts;
    }

    /* ── 「AI 做了什么」进展时间线（拍史；懒取 + 每目标缓存）── */

    _renderProgress() {
      const esc = (s) => this.esc(s);
      if (!this._progOpen) {
        return `<div class="gl-prog"><button type="button" class="gl-prog-btn" data-act="prog_toggle">` +
          `\u25B8 ${esc(this.t("inbox.goal.progress.show"))}</button></div>`;
      }
      let body;
      if (this._progLoading) {
        body = `<div class="gl-hint">${esc(this.t("cp.common.loading"))}</div>`;
      } else if (this._progErr) {
        body = `<div class="gl-errline" data-act="prog_retry">${esc(this.t("inbox.goal.progress.err"))}</div>`;
      } else {
        const rows = (this._progRows || []).slice(0, 7).map((a) => {
          const st = String(a.status || "planned");
          const stKey = ({ planned: 1, consumed: 1, sent: 1, skipped: 1, blocked: 1 })[st] ? st : "planned";
          const day = String(a.day || "").slice(5);
          const intent = String(a.intent || "");
          const short = intent.length > 46 ? intent.slice(0, 46) + "\u2026" : intent;
          return `<div class="gl-prog-row"><span class="gl-prog-day">${esc(day)}</span>` +
            `<span class="gl-prog-st st-${esc(stKey)}">${esc(this.t("inbox.goal.beat." + stKey))}</span>` +
            `<span class="gl-prog-tx" title="${esc(intent)}">${esc(short)}</span></div>`;
        }).join("");
        body = rows || `<div class="gl-hint">${esc(this.t("inbox.goal.progress.empty"))}</div>`;
      }
      return `<div class="gl-prog open"><button type="button" class="gl-prog-btn" data-act="prog_toggle">` +
        `\u25BE ${esc(this.t("inbox.goal.progress.hide"))}</button>${body}</div>`;
    }

    async _loadProgress(force) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      if (!force && this._progGoalId === g.goal_id && Array.isArray(this._progRows)) {
        this._rerender();
        return;
      }
      this._progLoading = true;
      this._progErr = false;
      this._rerender();
      let res = null;
      try {
        res = await this._api("/api/goals/" + encodeURIComponent(g.goal_id));
      } catch (_e) { res = null; }
      this._progLoading = false;
      if (res && res.ok && res.data && Array.isArray(res.data.actions)) {
        this._progGoalId = g.goal_id;
        this._progRows = res.data.actions.filter((a) => a && a.intent);
      } else {
        this._progErr = true;
      }
      this._rerender();
    }

    _renderProducts(g) {
      const list = Array.isArray(g.products) ? g.products.filter((p) => p && p.name) : [];
      if (!list.length) return "";
      const esc = (s) => this.esc(s);
      // P20：chip → 两行 mini 卡。pitch 从 hover title 提为常显副行（触屏/桌面壳
      // 无 hover 也可达——那是坐席的现成话术）；价格右对齐；有 url 带 ↗ 线稿明示外链。
      const ext = `<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M7 17L17 7"/><path d="M9 7h8v8"/></svg>`;
      const rows = list.map((p) => {
        const nm = `<span class="gl-prod-nm">${esc(p.name)}${p.url ? ext : ""}</span>`;
        const price = p.price_from ? `<span class="gl-prod-price">${esc(p.price_from)}</span>` : "";
        const pitch = p.pitch
          ? `<span class="gl-prod-pitch" title="${esc(p.pitch)}">${esc(p.pitch)}</span>` : "";
        return p.url
          ? `<a class="gl-prod" href="${esc(p.url)}" target="_blank" rel="noopener">${nm}${price}${pitch}</a>`
          : `<span class="gl-prod">${nm}${price}${pitch}</span>`;
      }).join("");
      return `<div class="gl-sec gl-prof"><div class="gl-prof-hd">` +
        `<span class="gl-prof-t">${esc(this.t("inbox.goal.products.title"))}</span></div>` +
        `<div class="gl-prods">${rows}</div></div>`;
    }

    /* 缺口/表单共用的建议问法（i18n 键尾=profile_slots 注册表 key；
       键缺失=返回 ""，绝不裸奔键名——与 _tmplDesc 同守卫）。 */
    _slotAskText(key) {
      const v = this.t("inbox.goal.profile.ask." + key);
      return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : "";
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
        `<span class="pct">${pct(val)}%</span></span>`;
      // 双零不渲染 0% 条（空数字是反激励——与 P19 本周成果 chip 同哲学）；
      // 冷启动由 profile.empty 文案 + 缺口 chips 承担。
      const hasFill = pct(fill.relation) > 0 || pct(fill.bant) > 0;
      const fillrow = hasFill
        ? `<div class="gl-fillrow">` +
          bar(this.t("inbox.goal.profile.track.relation"), fill.relation, "") +
          bar(this.t("inbox.goal.profile.track.bant"), fill.bant, "b2") + `</div>`
        : "";
      const hd = `<div class="gl-prof-hd">` +
        `<span class="gl-prof-t">${esc(this.t("inbox.goal.profile.title"))}</span>` +
        `<span class="sp"></span>` +
        `<button data-act="prof_toggle">${esc(this.t(this._profOpen ? "cp.common.cancel" : "inbox.goal.profile.edit"))}</button></div>` +
        fillrow;

      if (this._profOpen) {
        // 按轨分组 + 建议问法当 placeholder（空输入框不再让人猜「该填什么」）；
        // lifecycle 槽（流失原因）随商机组尾，不为一个槽位单开分组。
        const grp = (label, slots) => {
          if (!slots.length) return "";
          return `<div class="gl-pf-group">${esc(label)}</div>` + slots.map((s) =>
            `<div><label class="gl-fl">${esc(s.label)}</label>` +
            `<input data-prof-key="${esc(s.key)}" value="${esc(s.value || "")}"` +
            ` placeholder="${esc(this._slotAskText(s.key))}"></div>`).join("");
        };
        const all = p.slots || [];
        const rel = all.filter((s) => s.track === "relation");
        const biz = all.filter((s) => s.track !== "relation");
        return `<div class="gl-sec gl-prof">${hd}<div class="gl-prof-form">` +
          grp(this.t("inbox.goal.profile.track.relation"), rel) +
          grp(this.t("inbox.goal.profile.track.bant"), biz) + `</div>` +
          `<div class="gl-ferr" data-ref="perr" hidden></div>` +
          `<div class="acts gl-acts"><button class="primary" data-act="prof_save">${esc(this.t("inbox.goal.profile.save"))}</button></div></div>`;
      }

      const filled = (p.slots || []).filter((s) => s.value);
      const missing = (p.slots || []).filter((s) => !s.value && s.track === "bant").slice(0, 3);
      let body = "";
      if (!filled.length && !missing.length) {
        body = `<div class="gl-prof-empty">${esc(this.t("inbox.goal.profile.empty"))}</div>`;
      } else {
        // P20：chips 全部动作化——已填=点击补录修改（来源标注收进 tooltip 省宽度）；
        // 缺口=点击展开「拟稿去问 / 我来补录」追问行（旧版是死 span，可供性错位）。
        const editT = this.t("inbox.goal.profile.edit_t");
        const chips = filled.map((s) => {
          const src = s.src
            ? this.t("inbox.goal.profile.src." + (s.src === "agent" ? "agent" : "auto")) : "";
          const tip = s.label + ": " + s.value + (src ? " \u00b7 " + src : "") + " \u00b7 " + editT;
          return `<button type="button" class="gl-chip" data-act="slot_edit"` +
            ` data-slot="${esc(s.key)}" title="${esc(tip)}">${esc(s.label)}\u00b7${esc(s.value)}</button>`;
        }).join("");
        const miss = missing.map((s) =>
          `<button type="button" class="gl-chip miss${this._slotAsk === s.key ? " on" : ""}"` +
          ` data-act="slot_menu" data-slot="${esc(s.key)}"` +
          ` title="${esc(this.t("inbox.goal.profile.miss_t", { label: s.label }))}">${esc(s.label)}?</button>`).join("");
        body = `<div class="gl-chips">${chips}${miss}</div>` + this._askRow(missing);
        if (!filled.length) {
          body = `<div class="gl-prof-empty">${esc(this.t("inbox.goal.profile.empty"))}</div>` + body;
        }
      }
      return `<div class="gl-sec gl-prof">${hd}${body}</div>`;
    }

    /* 缺口 chip 展开的追问行：建议问法 + 「我来补录 / 拟稿去问」双出口。 */
    _askRow(missing) {
      const key = this._slotAsk;
      if (!key) return "";
      const s = (missing || []).find((m) => m.key === key);
      if (!s) return "";
      const esc = (x) => this.esc(x);
      const q = this._slotAskText(key);
      const lead = q
        ? `<div class="gl-ask-q">${esc(this.t("inbox.goal.profile.ask_lead", { q }))}</div>` : "";
      return `<div class="gl-ask">${lead}<div class="acts gl-ask-acts">` +
        `<button data-act="slot_fill" data-slot="${esc(key)}">${esc(this.t("inbox.goal.profile.fill_btn"))}</button>` +
        `<button class="primary" data-act="slot_ask" data-slot="${esc(key)}">${esc(this.t("inbox.goal.profile.ask_btn"))}</button></div></div>`;
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

    /* 场景一句话说明（i18n 键尾=模板 id；键缺失=不显示，不裸奔键名） */
    _tmplDesc(tid) {
      const v = this.t("inbox.goal.tmpl_desc." + tid);
      return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : "";
    }

    /* 情境推荐：宿主经 ctx.goalHint.silentHours 喂沉默时长——只在高置信时推荐
       （沉默 ≥72h → 沉默唤回），拿不准就不贴推荐标，避免误导。 */
    _recommendTid() {
      const h = (this._ctx && this._ctx.goalHint) || null;
      const silent = h ? parseFloat(h.silentHours) : NaN;
      if (isFinite(silent) && silent >= 72 && this._tmplById("engagement_reactivate")) {
        return "engagement_reactivate";
      }
      return "";
    }

    /* auto 档的诚实注解：桥未开时如实说明「不会自己主动发消息」。
       caps 缺失（旧后端）＝不注解，只显示中性 hint，绝不猜。 */
    _autonomyNote(lvl) {
      if (String(lvl) !== "auto") return "";
      const caps = (this._templates && this._templates.caps) || null;
      if (caps && caps.bridge_enabled === false) {
        return this.t("inbox.goal.autonomy.auto_note_off");
      }
      return "";
    }

    _renderForm() {
      const esc = (s) => this.esc(s);
      const list = (this._templates && this._templates.templates) || [];
      if (!list.length) {
        return `<div class="gl-errline" data-act="open_form">${esc(this.t("inbox.goal.form.load_fail"))}</div>`;
      }
      const prefs = this._loadPrefs();
      const rec = this._recommendTid();
      if (!this._formTid || !this._tmplById(this._formTid)) {
        // 默认选中：上次偏好（custom 不参与预选，进阶路径须显式点开）→ 情境推荐 → 首个场景模板
        const prefTid = (prefs.template && prefs.template !== "custom"
          && this._tmplById(prefs.template)) ? prefs.template : "";
        const firstScen = list.find((t) => t.id !== "custom") || list[0];
        this._formTid = prefTid || rec || firstScen.id;
      }
      const tmpl = this._tmplById(this._formTid) || {};

      const scenRows = list.filter((t) => t.id !== "custom").map((t) => {
        const nm = LANG === "en" ? (t.name_en || t.name_zh || t.id) : (t.name_zh || t.name_en || t.id);
        const sel = t.id === this._formTid;
        const recBadge = (t.id === rec)
          ? `<span class="gl-scen-rec">${esc(this.t("inbox.goal.form.recommended"))}</span>` : "";
        const desc = this._tmplDesc(t.id);
        return `<div class="gl-scen${sel ? " sel" : ""}" data-act="pick_tmpl" data-tid="${esc(t.id)}"` +
          ` role="radio" aria-checked="${sel ? "true" : "false"}">` +
          `<div class="gl-scen-nm">${esc(nm)}${recBadge}</div>` +
          (desc ? `<div class="gl-scen-d">${esc(desc)}</div>` : "") + `</div>`;
      }).join("");
      const customT = this._tmplById("custom");
      let advHtml = "";
      if (customT) {
        if (this._formTid === "custom") {
          const nm = LANG === "en" ? (customT.name_en || customT.name_zh || "custom")
            : (customT.name_zh || customT.name_en || "custom");
          const desc = this._tmplDesc("custom");
          advHtml = `<div class="gl-scen sel" data-act="pick_custom" role="radio" aria-checked="true">` +
            `<div class="gl-scen-nm">${esc(nm)}</div>` +
            (desc ? `<div class="gl-scen-d">${esc(desc)}</div>` : "") + `</div>`;
        } else {
          advHtml = `<button type="button" class="gl-adv" data-act="pick_custom">${esc(this.t("inbox.goal.form.advanced"))}</button>`;
        }
      }
      // 推荐理由：只有当推荐项被选中时说一句「为什么」（数据来自宿主喂的沉默时长）
      let recNote = "";
      if (rec && this._formTid === rec) {
        const h = (this._ctx && this._ctx.goalHint) || {};
        const days = Math.max(1, Math.round((parseFloat(h.silentHours) || 0) / 24));
        recNote = `<div class="gl-hint">${esc(this.t("inbox.goal.form.rec_silent", { d: days }))}</div>`;
      }

      const levels = (this._templates.autonomy_levels && this._templates.autonomy_levels.length)
        ? this._templates.autonomy_levels : AUTONOMY;
      const curAuto = AUTONOMY.indexOf(this._formAutonomy) >= 0 ? this._formAutonomy
        : (AUTONOMY.indexOf(prefs.autonomy) >= 0 ? prefs.autonomy : "suggest");
      const aOpts = levels.map((l) =>
        `<option value="${esc(l)}"${l === curAuto ? " selected" : ""}>${esc(this._autonomyLabel(String(l)))}</option>`).join("");
      const daysVal = (this._formUsePrefDays && prefs.deadline_days > 0)
        ? prefs.deadline_days : (tmpl.default_days || 14);
      const prefNote = (prefs.template || prefs.autonomy || prefs.deadline_days)
        ? `<div class="gl-hint">${esc(this.t("inbox.goal.prefs_restored"))}</div>` : "";
      const aNote = this._autonomyNote(curAuto);
      return `<div class="gl-form">` +
        `<div class="gl-alias">${esc(this.t("inbox.goal.title_hint"))}</div>` + prefNote +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.scenario"))}</label>` +
        `<div class="gl-scenlist" role="radiogroup">${scenRows}${advHtml}</div>${recNote}</div>` +
        `<div data-ref="params">${this._paramsHtml(tmpl)}</div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.days"))}</label>` +
        `<input type="number" min="1" max="180" step="1" data-ref="days" value="${esc(String(daysVal))}"></div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.autonomy"))}</label>` +
        `<select data-chg="autonomy" data-ref="autonomy">${aOpts}</select>` +
        `<div class="gl-hint" data-ref="ahint">${esc(this._autonomyHint(curAuto))}</div>` +
        `<div class="gl-hint gl-anote" data-ref="anote"${aNote ? "" : " hidden"}>${esc(aNote)}</div></div>` +
        `<div class="gl-ferr" data-ref="ferr" hidden></div>` +
        `<div class="acts gl-acts"><button data-act="close_form">${esc(this.t("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="create">${esc(this.t("inbox.goal.form.create"))}</button></div>` +
        `</div>`;
    }

    _ref(name) { return this.shadowRoot.querySelector(`[data-ref="${name}"]`); }

    _syncAutonomyHint() {
      const a = this._ref("autonomy");
      if (!a) return;
      this._formAutonomy = String(a.value);
      const h = this._ref("ahint");
      if (h) h.textContent = this._autonomyHint(this._formAutonomy);
      const n = this._ref("anote");
      if (n) {
        const note = this._autonomyNote(this._formAutonomy);
        n.textContent = note;
        n.hidden = !note;
      }
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
        this._formTid = "";
        this._formAutonomy = "";
        this._formUsePrefDays = true;
        this._rerender();
        return;
      }
      if (act === "close_form") { this._formOpen = false; this._rerender(); return; }
      if (act === "pick_tmpl" || act === "pick_custom") {
        const tid = act === "pick_custom" ? "custom"
          : ((el && el.getAttribute("data-tid")) || "");
        if (!tid || tid === this._formTid || !this._tmplById(tid)) return;
        const aSel = this._ref("autonomy");
        if (aSel && aSel.value) this._formAutonomy = String(aSel.value);
        this._formTid = tid;
        this._formUsePrefDays = false;   // 换场景 → 期限跟场景默认，不再回放偏好
        _beacon(tid === "custom" ? "goal_form_pick_custom" : "goal_form_pick_scenario");
        this._rerender();
        return;
      }
      if (act === "hint_dismiss") {
        this._createdHintAutonomy = "";
        this._createdHintTid = "";
        this._chainReco = null;
        this._rerender();
        return;
      }
      if (act === "chain_reco_start") { await this._startRecoChain(el); return; }
      if (act === "prog_toggle") {
        this._progOpen = !this._progOpen;
        if (this._progOpen) {
          _beacon("goal_progress_open");
          await this._loadProgress(false);
        } else {
          this._rerender();
        }
        return;
      }
      if (act === "prog_retry") { await this._loadProgress(true); return; }
      if (act === "create") { await this._create(el); return; }
      if (act === "pause" || act === "resume") { await this._status(act); return; }
      if (act === "cancel") {
        this._moreOpen = false;
        await this._status("cancel");
        return;
      }
      if (act === "redirect") {
        // P22：换方向 = 放弃当前 + 打开建目标表单（「设定目标」在有目标时藏在空态里）
        this._moreOpen = false;
        if (typeof confirm === "function" &&
            !confirm(this.t("inbox.goal.redirect_confirm"))) return;
        _beacon("goal_redirect");
        await this._status("cancel", { skipConfirm: true, thenOpenForm: true });
        return;
      }
      if (act === "autonomy_cycle") {
        await this._cycleAutonomy();
        return;
      }
      if (act === "won") { this._wonFormOpen = true; this._rerender(); return; }
      if (act === "won_cancel") { this._wonFormOpen = false; this._rerender(); return; }
      if (act === "won_confirm") { await this._status("done"); return; }
      if (act === "more_toggle") { this._moreOpen = !this._moreOpen; this._rerender(); return; }
      if (act === "beat_adopt") { _beacon("goal_feedback_adopt"); await this._beatFeedback("adopt"); return; }
      if (act === "beat_adopt_draft") {
        // 复合动作：先采纳（服务端权威落账），成功后按今日意图驱动草稿——
        // 数据实锤反馈率 0%（30 拍 0 反馈），把反馈做成拟稿的顺手前置动作。
        const g0 = this._d && this._d.goal;
        const intent = g0 && g0.today && g0.today.intent;
        _beacon("goal_feedback_adopt_draft");
        const ok = await this._beatFeedback("adopt");
        if (ok && intent) this._emitDriveDraft(g0, intent);
        return;
      }
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
        this._emitDriveDraft(g, intent);
        return;
      }
      if (act === "prof_toggle") {
        this._profOpen = !this._profOpen;
        this._slotAsk = "";
        this._rerender();
        return;
      }
      if (act === "prof_save") { await this._profSave(el); return; }
      if (act === "slot_menu") {
        // 缺口 chip：展开/收起追问行（同 chip 再点=收起）
        const k = (el && el.getAttribute("data-slot")) || "";
        this._slotAsk = (this._slotAsk === k) ? "" : k;
        if (this._slotAsk) _beacon("goal_slot_menu");
        this._rerender();
        return;
      }
      if (act === "slot_ask") {
        // 拟稿去问：复用「采纳并拟稿」同一条宿主链（cp-goal-drive-draft →
        // setDirective → smart-reply.instruction）。
        const k = (el && el.getAttribute("data-slot")) || "";
        const p = this._d && this._d.__profile;
        const s = (p && Array.isArray(p.slots)) ? p.slots.find((x) => x && x.key === k) : null;
        if (!s) return;
        const q = this._slotAskText(k);
        let intent = q ? this.t("inbox.goal.profile.ask_intent", { label: s.label, q }) : "";
        if (!intent || String(intent).indexOf("inbox.goal.") === 0) intent = s.label + "?";
        _beacon("goal_slot_ask");
        this._slotAsk = "";
        this._rerender();
        const g = this._d && this._d.goal;
        this._emitDriveDraft(g, intent, {
          pushLevel: "soft",
          source: "slot",
          label: this.t("inbox.goal.profile.ask_draft_label") || "",
        });
        return;
      }
      if (act === "slot_fill" || act === "slot_edit") {
        // 我来补录（缺口）/ 点击已填 chip：开补录表单并聚焦对应字段
        const k = (el && el.getAttribute("data-slot")) || "";
        _beacon(act === "slot_fill" ? "goal_slot_fill" : "goal_slot_edit");
        this._slotAsk = "";
        this._profOpen = true;
        this._rerender();
        if (k) {
          const inp = this.shadowRoot.querySelector(`[data-prof-key="${k}"]`);
          if (inp) { try { inp.focus(); inp.scrollIntoView({ block: "nearest" }); } catch (_e) { /* soft */ } }
        }
        return;
      }
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

    /* ── C1：建目标后的配套跟进 SOP 推荐 ──────────────────────────────── */

    async _loadChainReco() {
      const target = CHAIN_RECO[this._createdHintTid];
      if (!target) { this._chainReco = null; return; }
      let res = null;
      try { res = await this._api("/api/workspace/workflow-chains"); } catch (_e) { res = null; }
      if (!this._createdHintAutonomy) return;   // 提示已被关掉 → 丢弃过期结果
      const chains = (res && res.ok && res.data && Array.isArray(res.data.chains))
        ? res.data.chains : [];
      const hit = chains.find((c) => c && c.chain_id === target && c.enabled);
      this._chainReco = hit
        ? { chain: { chain_id: hit.chain_id, name: hit.name || hit.chain_id }, state: "idle", err: "" }
        : null;
      if (this._chainReco) {
        _beacon("goal_chain_reco_show");
        if (this._d && this._d.goal) this._rerender();
      }
    }

    async _startRecoChain(el) {
      const rc = this._chainReco;
      const ctx = this._ctx || {};
      if (!rc || !rc.chain || rc.state === "started" || !ctx.conversationId) return;
      if (el) el.disabled = true;
      _beacon("goal_chain_reco_start");
      // goal_id 随启动透传 → 落执行 context（归因地基，与选择器「推荐」入口同口径）
      const gid = (this._d && this._d.goal && this._d.goal.goal_id)
        ? String(this._d.goal.goal_id) : "";
      const payload = { chain_id: rc.chain.chain_id };
      if (gid) payload.goal_id = gid;
      let res = null;
      try {
        res = await this._api(
          "/api/workspace/conv/" + encodeURIComponent(ctx.conversationId) + "/start-chain", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
      } catch (_e) { res = null; }
      if (res && res.ok && res.data && res.data.ok !== false) {
        rc.state = "started";
        rc.err = "";
        // 宿主监听 cp-action-done：出「已执行」toast + 自动刷新下方工作链卡
        this.emit("cp-action-done", { action_type: "chain_start", ok: true });
      } else {
        if (el) el.disabled = false;
        rc.err = String((res && res.data && (res.data.error || res.data.detail))
          || this.t("cp.common.save_fail")).slice(0, 120);
      }
      this._rerender();
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
        // 一次性「接下来会发生什么」提示（按所选自治档如实说明，消除「建完然后呢」断崖）
        this._createdHintAutonomy = String(body.autonomy || "suggest");
        this._createdHintTid = String(this._formTid || "");
        this._chainReco = null;
        this._loadChainReco();   // fire-and-forget：查到配套链后原位补一行推荐
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
      if (!g || !g.goal_id) return false;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/beat/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return false; }
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
        return true;
      }
      this.refresh();
      return false;
    }

    /* P22：组装坐席指令（含力度规则）并发 cp-goal-drive-draft */
    _emitDriveDraft(g, intent, opts) {
      const o = opts || {};
      const beat = (g && g.today) || {};
      const plRaw = o.pushLevel != null ? o.pushLevel : beat.push_level;
      const pl = PUSH_LEVELS.indexOf(String(plRaw)) >= 0 ? String(plRaw) : "soft";
      const pushLab = this.t("inbox.goal.push." + pl);
      const pushRule = this.t("inbox.goal.drive_rule." + pl);
      let text = this.t("inbox.goal.drive_instruction", {
        intent: String(intent || ""),
        push: pushLab,
        push_rule: pushRule,
      });
      if (!text || String(text).indexOf("inbox.goal.") === 0) {
        text = String(intent || "");
      }
      this.emit("cp-goal-drive-draft", {
        intent: String(intent || ""),
        instruction: String(text),
        pushLevel: pl,
        label: String(o.label || "").trim(),
        source: String(o.source || "beat"),
        conversationId: (g && g.conversation_id) || (this._ctx && this._ctx.conversationId) || "",
        goalId: (g && g.goal_id) || "",
      });
    }

    /* 宿主英雄卡「拟稿」：复用同一条指令拼装，零分叉 */
    driveDraftFromToday() {
      const g = this._d && this._d.goal;
      const intent = g && g.today && g.today.intent;
      if (!intent) return false;
      _beacon("goal_hero_draft");
      this._emitDriveDraft(g, intent, { source: "hero" });
      return true;
    }

    async _cycleAutonomy() {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id || (g.status !== "active" && g.status !== "paused")) return;
      const cur = String(g.autonomy || "suggest");
      const idx = AUTONOMY.indexOf(cur);
      const next = AUTONOMY[(idx >= 0 ? idx + 1 : 1) % AUTONOMY.length];
      _beacon("goal_autonomy_cycle");
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ autonomy: next }),
        });
      } catch (_e) { res = null; }
      if (res && res.ok && res.data && res.data.goal) {
        // update 端点的 goal_view 不带 today——保留本地今日拍，避免自治档一切今日条消失
        const ng = res.data.goal;
        if (g.today && !ng.today) ng.today = g.today;
        this._d = Object.assign({}, this._d, { goal: ng });
        this._flashToast(this.t("inbox.goal.autonomy_saved", {
          level: this._autonomyLabel(next),
        }));
        this._rerender();
        this.emit("cp-goal-changed", { action: "autonomy", conversationId: g.conversation_id });
        return;
      }
      this._flashToast(this.t("inbox.goal.err_retry") || this.t("inbox.goal.toast_saved"));
    }

    async _status(action, opts) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      const o = opts || {};
      if (action === "cancel" && !o.skipConfirm && typeof confirm === "function" &&
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
        if (o.thenOpenForm) {
          if (!this._templates) await this._loadTemplates();
          this._formOpen = true;
          this._formTid = "";
          this._formAutonomy = "";
          this._formUsePrefDays = true;
        }
        this._rerender();
        this.emit("cp-goal-changed", { action, conversationId: g.conversation_id });
        return;
      }
      this.refresh();
    }
  }

  if (!customElements.get("cp-goal")) customElements.define("cp-goal", CpGoal);
})(typeof window !== "undefined" ? window : this);
