"use strict";
/* 两端共享 · 智能养号 cp-nurture（工具箱「智能养号」，2026-08-21；2026-08-22 可懂化改版）
   机群养护总览 + 用户自配每号养护方案。状态复用既有 fleet-health（账号健康红绿灯 +
   生命周期分布），配置写 ops.nurture overlay（保注释、热生效）。

   2026-08-22 改版要点（「智能养号卡优化」方案 P1/P2，术语表见 cp-i18n.js 养号分区注释）：
   - 受众分层：坐席看「健康判词 + 三步引导 + 账号方案」；探针 / 配置警告收进
     「高级（管理员）」折叠；后端 can_write=false（viewer）时隐藏全部写控件
     （与路由 403 同语义；旧后端缺该字段按可写处理＝fail-open 不回归）。
   - 引擎三态改引导式步骤条：配方案 → 模拟运行 → 自动养护；「开始自动养护」走内联
     确认（写明试点账号影响面），后端本就强制先 dry 后 live，UI 顺应而非报错教育。
   - 模拟可见化：消费 GET /api/nurture/shadow（client.nurtureShadow 两端早已有约）——
     「模拟运行」不再是黑盒，最近计划/执行逐条可读；探针真跑成功后顺手刷新该列表。
   - 账号人话名：status 每行 label（后端注册表 label/meta 回落；旧后端缺字段回落短 key），
     完整 key 收进悬浮 title；探针下拉同样用人话名。
   - 危险动作显式化：探针「真跑一次」两击确认带 armed 危险态（红底），不再只换文字。
   - 视觉：字号最小 11px（--cp-fs-tiny），状态色走 --cp-* token（暗色自适配），
     行为改多选 chips，账号行两行网格，行内有改动才点亮「保存」（dirty 态）。

   2026-08-28 分区折叠：**每个分区标题都是折叠开关**（概览 / 引擎 / 模拟记录 /
   账号方案 / 高级），折叠态按分区存 localStorage（跨标签页与重开保留）。两条
   不变量：① **折叠后标题行必须仍带该块的关键读数**（灯色判词 / 引擎状态 /
   记录条数 / n-t 已启用 + 未保存红点 / 警告数）——只藏不留摘要＝把信息弄丢，
   比长面板更糟；② 默认只收起「高级（管理员）」，其余展开——默认全收＝新用户
   打开是一排空标题，自证不了面板有什么。折叠只是隐藏 DOM，`_work`（编辑中方案）
   是内存里的单一事实源，收起再展开原样还在。

   2026-08-28 第二批（长列表 + 词义）：
   - 账号方案 >CAP+1 个截到 CAP 行 + 「显示全部 n 个」，**在养的号一个都不截**
     （理由见 ACCT_VISIBLE_CAP 注释）；
   - 「养号中」KPI 与行徽章改「起号期」+ 悬浮口径——该数出自
     `account_signals.lifecycle_stage`（账号注册天数 < warmup_ramp_days），与
     「已启用养护」不是一回事；同词并排曾被读成自相矛盾（实况 2 起号期 / 0-10 已启用）；
   - intro 引导只在账号方案展开时出现（不指着收起的东西说「在下方…」）。

   弱会话依赖：不需要选中会话。context 重喂不重渲（本卡与会话无关）。
   client 需实现 nurtureStatus/nurtureSave（缺失如实报）；nurtureShadow/nurtureEngine/
   nurtureProbe 按存在性优雅降级。动态键（stage_/beh_ 等）渲染前有 raw-key 自检：
   词典查不到就显示原始值，绝不把 cp.* 键名裸串放上屏。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  const STAGES = ["active", "warming", "pending", "restricted", "banned", "offline"];

  /* 分区折叠态：localStorage 记忆（跨标签页/重开保留）。默认值只收「高级」——
     见顶部注释不变量②。localStorage 不可用（隐私模式/沙箱）时全链软失败，
     折叠仍在本次会话内工作，只是不记忆。 */
  const FOLD_KEY = "cp.nurture.folds.v1";
  const FOLD_DEFAULT = { advanced: 1 };

  /* 账号方案长列表截断（2026-08-28）：号一多，这一块就吃掉整个右栏（10 个号 ≈
     750px，405px 壳里概览+引擎已占掉一半）。两条约束：
     ① 只有 > CAP+1 才截——藏 1 个换一个按钮是净亏；
     ② **只藏没启用养护的号**，正在养的一个都不藏（那些是有后果的号，
        「列表太长」不是把它们藏起来的理由）。故 on 多于 CAP 时如实超出。 */
  const ACCT_VISIBLE_CAP = 5;

  /* ntr_ 埋点（与 cp-goal 同机制）：sendBeacon → /api/telemetry/ui-event，
     读数 /api/admin/ui-event-trend?prefix=ntr_。best-effort 永不阻断。 */
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

  class CpNurture extends Base {
    constructor() {
      super();
      this._booted = false;
      this._data = null;
      this._shadow = null;      // 影子样本（引擎开过才拉；null=还没拉）
      this._expanded = {};      // 展开行为 chips 的账号 key 集
      this._work = {};          // 每号编辑中方案（未保存工作副本，单一事实源）
      this._baseline = {};      // 每号服务端基线 JSON（判 dirty）
      this._folds = this._readFolds();  // 分区折叠态（localStorage 记忆）
      this._acctShowAll = false;        // 账号长列表「显示全部」（会话内，不记忆）
      this._goliveArm = false;  // go_live 内联确认态
      this.shadowRoot.addEventListener("change", (e) => this._onFieldChange(e));
    }

    styles() {
      return `
      .nt-ro { font-size:var(--cp-fs-tiny,11px); padding:5px 8px; border-radius:7px; margin-bottom:8px;
        background:var(--cp-bg-soft,#f1f5f9); color:var(--cp-text-dim,#64748b);
        border:1px solid var(--cp-border,#e2e8f0); }
      /* 分区标题＝折叠开关：本块须整体覆盖基类 button 的边框/底色/内距 */
      .nt-sec { display:flex; align-items:center; gap:6px; margin:10px 0 6px; width:100%;
        font:inherit; font-size:var(--cp-fs-tiny,11px); font-weight:700; text-align:left;
        color:var(--cp-text-dim,#64748b); background:transparent; border:0;
        border-radius:6px; padding:2px 3px; cursor:pointer; }
      .nt-sec:first-child { margin-top:0; }
      .nt-sec:hover { color:var(--cp-text,#1e293b); background:var(--cp-bg-soft,#f1f5f9); }
      .nt-sec .car { flex-shrink:0; width:9px; font-size:9px; line-height:1;
        color:var(--cp-text-tiny,#94a3b8); }
      .nt-sec .tl { flex-shrink:0; }
      .nt-sec .cnt { min-width:0; font-weight:400; color:var(--cp-text-tiny,#94a3b8);
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .nt-sec .cnt.dry { color:var(--cp-warn-ink,#b45309); }
      .nt-sec .cnt.live { color:var(--cp-ok,#0f9d75); }
      .nt-sec .nt-light { width:9px; height:9px; }
      /* 折叠后「有未保存改动」的唯一可见处（展开时看得见亮着的保存钮） */
      .nt-sec .dirty { flex-shrink:0; margin-left:auto; font-size:9px; line-height:1;
        color:var(--cp-accent,#6366f1); }
      .nt-health { display:flex; align-items:center; gap:8px; padding:8px 10px; margin-bottom:6px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:9px; background:var(--cp-surface-2,#f8fafc); }
      .nt-light { width:11px; height:11px; border-radius:50%; flex-shrink:0; }
      .nt-light.g { background:var(--cp-ok,#0f9d75); }
      .nt-light.a { background:var(--cp-warn,#d97706); }
      .nt-light.r { background:var(--cp-danger,#dc2626); }
      .nt-light.u { background:var(--cp-text-tiny,#94a3b8); }
      .nt-verdict { flex:1; min-width:0; font-size:var(--cp-fs-sm,12px); font-weight:600;
        color:var(--cp-text,#1e293b); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .nt-sum { display:flex; gap:6px; margin-bottom:8px; }
      .nt-kpi { flex:1; min-width:0; text-align:center; padding:6px 4px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:8px; background:var(--cp-surface-2,#f8fafc); }
      .nt-kpi .n { font-size:15px; font-weight:700; line-height:1.15; color:var(--cp-text,#1e293b); }
      .nt-kpi .n.warm { color:var(--cp-warn,#d97706); }
      .nt-kpi .n.bad { color:var(--cp-danger,#dc2626); }
      .nt-kpi .l { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-top:2px;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .nt-intro { font-size:var(--cp-fs-tiny,11px); line-height:1.55; padding:7px 9px; border-radius:8px;
        margin-bottom:8px; background:var(--cp-accent-bg,rgba(99,102,241,.08)); color:var(--cp-text,#1e293b);
        border:1px solid var(--cp-accent-weak,rgba(99,102,241,.25)); }
      .nt-note { font-size:var(--cp-fs-tiny,11px); line-height:1.5; padding:6px 8px; border-radius:7px;
        margin-top:6px; background:var(--cp-warn-bg,#fffbeb); color:var(--cp-warn-ink,#b45309);
        border:1px solid var(--cp-warn-border,#fde68a); }
      .nt-eng { border:1px solid var(--cp-border,#e2e8f0); border-radius:9px; padding:9px 10px;
        margin-bottom:2px; background:var(--cp-surface,#fff); }
      .nt-steps { display:flex; align-items:center; gap:4px; margin-bottom:8px; }
      .nt-step { display:flex; align-items:center; gap:5px; min-width:0; }
      .nt-step .dot { width:18px; height:18px; border-radius:50%; flex-shrink:0; display:inline-flex;
        align-items:center; justify-content:center; font-size:11px; font-weight:700;
        background:var(--cp-bg-soft,#f1f5f9); color:var(--cp-text-dim,#64748b);
        border:1px solid var(--cp-border,#e2e8f0); }
      .nt-step .lb { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .nt-step.cur .dot { background:var(--cp-accent,#6366f1); border-color:var(--cp-accent,#6366f1); color:#fff; }
      .nt-step.cur .lb { color:var(--cp-text,#1e293b); font-weight:600; }
      .nt-step.done .dot { background:var(--cp-ok-bg,rgba(15,157,117,.12));
        border-color:var(--cp-ok,#0f9d75); color:var(--cp-ok,#0f9d75); }
      .nt-step.done .lb { color:var(--cp-ok,#0f9d75); }
      .nt-sep { flex:1; height:1px; min-width:8px; background:var(--cp-border,#e2e8f0); }
      .nt-eng-state { font-size:var(--cp-fs-sm,12px); font-weight:600; display:block; margin-bottom:7px; }
      .nt-eng-state.off { color:var(--cp-text-dim,#64748b); }
      .nt-eng-state.dry { color:var(--cp-warn-ink,#b45309); }
      .nt-eng-state.live { color:var(--cp-ok,#0f9d75); }
      .nt-eng-btns { display:flex; gap:6px; flex-wrap:wrap; }
      .nt-eng-btns button { font-size:var(--cp-fs-tiny,11px); padding:4px 12px; }
      .nt-confirm { border:1px solid var(--cp-danger,#dc2626); background:var(--cp-danger-bg,rgba(220,38,38,.06));
        border-radius:8px; padding:7px 9px; margin-bottom:6px; font-size:var(--cp-fs-tiny,11px);
        line-height:1.55; color:var(--cp-text,#1e293b); }
      .nt-confirm .plist { margin-top:4px; color:var(--cp-text-dim,#64748b); }
      .nt-confirm .btns { display:flex; gap:6px; margin-top:6px; }
      .nt-confirm button.golive-yes { background:var(--cp-danger,#dc2626);
        border-color:var(--cp-danger,#dc2626); color:#fff; }
      .nt-eng-foot { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin-top:6px; }
      .nt-warns { display:flex; flex-direction:column; gap:4px; margin:0 0 8px; }
      .nt-warn { font-size:var(--cp-fs-tiny,11px); line-height:1.5; padding:5px 8px; border-radius:6px; }
      .nt-warn.warn { background:var(--cp-warn-bg,#fffbeb); color:var(--cp-warn-ink,#b45309);
        border:1px solid var(--cp-warn-border,#fde68a); }
      .nt-warn.info { background:var(--cp-bg-soft,#f1f5f9); color:var(--cp-text-dim,#64748b);
        border:1px solid var(--cp-border,#e2e8f0); }
      .nt-shadow { border:1px solid var(--cp-border,#e2e8f0); border-radius:9px; padding:7px 9px;
        margin-bottom:2px; background:var(--cp-surface,#fff); }
      .nt-sh-row { display:flex; align-items:center; gap:6px; padding:4px 0; min-width:0;
        font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
        border-top:1px dashed var(--cp-border,#e2e8f0); }
      .nt-sh-row:first-of-type { border-top:0; }
      .nt-sh-ago { flex-shrink:0; color:var(--cp-text-tiny,#94a3b8); }
      .nt-sh-nm { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
        color:var(--cp-text,#1e293b); }
      .nt-sh-kind { flex-shrink:0; }
      .nt-sh-chip { flex-shrink:0; margin-left:auto; font-size:11px; padding:0 7px;
        border-radius:8px; line-height:1.6; }
      .nt-sh-chip.dry { background:var(--cp-bg-soft,#f1f5f9); color:var(--cp-text-dim,#64748b); }
      .nt-sh-chip.real { background:var(--cp-ok-bg,rgba(15,157,117,.12)); color:var(--cp-ok,#0f9d75); }
      .nt-sh-chip.fail { background:var(--cp-danger-bg,rgba(220,38,38,.08)); color:var(--cp-danger,#dc2626); }
      .nt-empty { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); line-height:1.5; }
      .nt-acct-more { width:100%; margin-bottom:6px; padding:4px 8px;
        font-size:var(--cp-fs-tiny,11px); border-style:dashed;
        color:var(--cp-text-dim,#64748b); }
      .nt-acct-more:hover { color:var(--cp-text,#1e293b); }
      .nt-acct { border:1px solid var(--cp-border,#e2e8f0); border-radius:9px; padding:8px 9px;
        margin-bottom:6px; background:var(--cp-surface,#fff); }
      .nt-acct .hd { display:flex; align-items:center; gap:6px; min-width:0; }
      .nt-plat { flex-shrink:0; font-size:11px; padding:0 6px; border-radius:6px; line-height:1.7;
        background:var(--cp-bg-soft,#f1f5f9); color:var(--cp-text-dim,#64748b);
        border:1px solid var(--cp-border,#e2e8f0); }
      .nt-acct .nm { flex:1; min-width:0; font-size:var(--cp-fs-sm,12px); font-weight:600;
        color:var(--cp-text,#1e293b); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .nt-pilot { flex-shrink:0; font:inherit; font-size:11px; padding:0 8px; line-height:1.7;
        border-radius:999px; cursor:pointer; border:1px dashed var(--cp-border,#e2e8f0);
        background:transparent; color:var(--cp-text-tiny,#94a3b8); }
      .nt-pilot[aria-pressed="true"] { border-style:solid; border-color:var(--cp-accent,#6366f1);
        background:var(--cp-accent-bg,rgba(99,102,241,.10));
        color:var(--cp-accent-deep,var(--cp-accent,#6366f1)); font-weight:600; }
      .nt-pilot.armed { border-style:solid; background:var(--cp-danger,#dc2626);
        border-color:var(--cp-danger,#dc2626); color:#fff; }
      .nt-pilot:disabled { cursor:default; opacity:.65; }
      .nt-stage { flex-shrink:0; font-size:11px; padding:1px 7px; border-radius:9px; color:#fff; }
      .nt-stage.st-active { background:var(--cp-ok,#0f9d75); }
      .nt-stage.st-warming { background:var(--cp-warn,#d97706); }
      .nt-stage.st-pending { background:var(--cp-text-dim,#64748b); }
      .nt-stage.st-restricted, .nt-stage.st-banned { background:var(--cp-danger,#dc2626); }
      .nt-stage.st-offline { background:var(--cp-text-tiny,#94a3b8); }
      .nt-acct .ctl { display:flex; align-items:center; gap:7px; margin-top:7px; flex-wrap:wrap; }
      .nt-sw { position:relative; width:34px; height:19px; flex-shrink:0; cursor:pointer; }
      .nt-sw input { opacity:0; width:0; height:0; }
      .nt-sw .track { position:absolute; inset:0; background:var(--cp-border,#cbd5e1);
        border-radius:19px; transition:.15s; }
      .nt-sw input:checked + .track { background:var(--cp-accent,#6366f1); }
      .nt-sw .track:before { content:""; position:absolute; width:15px; height:15px; left:2px; top:2px;
        background:#fff; border-radius:50%; transition:.15s; }
      .nt-sw input:checked + .track:before { transform:translateX(15px); }
      .nt-sw input:disabled + .track { opacity:.5; cursor:not-allowed; }
      .nt-sw-lb { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .nt-acct select { font:inherit; font-size:var(--cp-fs-tiny,11px);
        border:1px solid var(--cp-border,#e2e8f0); border-radius:6px;
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:2px 6px; }
      .nt-rowmsg { font-size:var(--cp-fs-tiny,11px); }
      .nt-beh { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; padding-top:8px;
        border-top:1px dashed var(--cp-border,#e2e8f0); }
      .nt-chip { font:inherit; font-size:var(--cp-fs-tiny,11px); padding:3px 11px; border-radius:999px;
        cursor:pointer; border:1px solid var(--cp-border,#e2e8f0);
        background:var(--cp-surface,#fff); color:var(--cp-text-dim,#64748b); }
      .nt-chip[aria-pressed="true"] { background:var(--cp-accent-bg,rgba(99,102,241,.10));
        border-color:var(--cp-accent,#6366f1);
        color:var(--cp-accent-deep,var(--cp-accent,#6366f1)); font-weight:600; }
      .nt-chip.risk { color:var(--cp-danger,#dc2626); border-color:var(--cp-danger,#dc2626); opacity:.9; }
      .nt-chip.risk[aria-pressed="true"] { background:var(--cp-danger-bg,rgba(220,38,38,.08));
        border-color:var(--cp-danger,#dc2626); color:var(--cp-danger,#dc2626); }
      .nt-chip:disabled { opacity:.55; cursor:default; }
      .nt-adv { margin:8px 0 0; }
      /* 高级区标题与其它分区同款（样式全由 .nt-sec 提供，这里只调外距） */
      .nt-adv-hd { margin:0 0 6px; }
      .nt-probe { border:1px dashed var(--cp-border,#e2e8f0); border-radius:8px; padding:8px 9px; }
      .nt-probe-hd { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
        margin-bottom:6px; line-height:1.5; }
      .nt-probe-row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
      .nt-probe select { font:inherit; font-size:var(--cp-fs-tiny,11px); max-width:150px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:6px;
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:2px 6px; }
      .nt-probe button { font-size:var(--cp-fs-tiny,11px); padding:3px 10px; }
      .nt-probe button.run { color:var(--cp-danger,#dc2626); border-color:var(--cp-danger,#dc2626); }
      .nt-probe button.run.armed { background:var(--cp-danger,#dc2626);
        border-color:var(--cp-danger,#dc2626); color:#fff; }
      .nt-probe-msg { font-size:var(--cp-fs-tiny,11px); margin-top:5px; line-height:1.5; }
      .nt-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:6px; }
      .nt-hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
        margin-top:8px; line-height:1.55; }`;
    }

    connectedCallback() {
      if (this._booted) return;
      this._booted = true;
      this._load();
    }
    async refresh() { this._notifyLoaded({ ok: true }); }
    _cl() {
      if (!this._client && root.CopilotShared && root.CopilotShared.createCopilotClient) {
        try { this._client = root.CopilotShared.createCopilotClient(); } catch (_e) { /* null */ }
      }
      return this._client;
    }
    _canWrite() { const d = this._data; return !d || d.can_write !== false; }

    /* 动态键 raw-key 自检：词典查不到 → 显示原始值而非 cp.* 键名裸串 */
    _tt(key, fallback) {
      const v = this.t(key);
      return v === key ? String(fallback == null ? "" : fallback) : v;
    }

    // ── 分区折叠 ─────────────────────────────────────────────────────────
    _readFolds() {
      try {
        const raw = root.localStorage && root.localStorage.getItem(FOLD_KEY);
        const o = raw ? JSON.parse(raw) : null;
        return (o && typeof o === "object") ? o : {};
      } catch (_e) { return {}; }
    }
    _writeFolds() {
      try {
        root.localStorage && root.localStorage.setItem(FOLD_KEY, JSON.stringify(this._folds || {}));
      } catch (_e) { /* 记不住不影响本次会话折叠 */ }
    }
    _isFolded(id) {
      const m = this._folds || {};
      return (id in m) ? !!m[id] : !!FOLD_DEFAULT[id];
    }
    _toggleFold(id) {
      if (!id) return;
      const folded = !this._isFolded(id);
      this._folds[id] = folded ? 1 : 0;
      this._writeFolds();
      // 展开「高级」沿用既有埋点名 ntr_adv_open（读数看板与门禁都认它）
      _beacon(folded ? "ntr_fold_" + id
        : (id === "advanced" ? "ntr_adv_open" : "ntr_unfold_" + id));
      this._renderAll();
    }
    /* 分区标题＝折叠开关。summaryHtml 由调用方按折叠态决定：折叠时必须给出
       该块的关键读数（不变量①），展开时通常留空（内容自己在下面）。 */
    _secHead(id, title, summaryHtml, extraCls) {
      const folded = this._isFolded(id);
      if (this._secIds) this._secIds.push(id);   // 本轮真渲染出来的分区（判「全收起」）
      return '<button type="button" class="nt-sec' + (extraCls ? " " + extraCls : "") +
        '" data-act="fold" data-fold="' + this.esc(id) +
        '" aria-expanded="' + (folded ? "false" : "true") + '">' +
        '<span class="car">' + (folded ? "▸" : "▾") + "</span>" +
        '<span class="tl">' + this.esc(title) + "</span>" +
        (summaryHtml || "") + "</button>";
    }

    async _load() {
      const client = this._cl();
      if (!client || !client.nurtureStatus) { this._render('<div class="nt-hint">' + this.esc(this.t("cp.nurture.no_client")) + "</div>"); return; }
      this._render('<div class="nt-hint">' + this.esc(this.t("cp.nurture.loading")) + "</div>");
      let d;
      try { d = await client.nurtureStatus(); } catch (_e) { d = null; }
      if (!d || !d.ok) { this._render('<div class="nt-err">' + this.esc(this.t("cp.nurture.load_fail")) + "</div>"); return; }
      this._data = d;
      this._goliveArm = false;
      this._work = {};
      this._baseline = {};
      const behKeys = d.behaviors || ["browse", "read", "react", "self_chat"];
      (d.accounts || []).forEach((a) => {
        const key = String(a.nurture_key || "");
        const p = a.nurture_plan || {};
        const behaviors = {};
        behKeys.forEach((b) => { behaviors[b] = !!(p.behaviors && p.behaviors[b]); });
        const w = { enabled: !!p.enabled, profile: String(p.profile || "balanced"), behaviors: behaviors };
        this._work[key] = w;
        this._baseline[key] = JSON.stringify(w);
      });
      this._notifyLoaded({ ok: true });
      this._renderAll();
      this._loadShadow();   // best-effort，回来单独回填列表，不阻塞主渲染
    }

    async _loadShadow() {
      const d = this._data || {};
      const eng = d.engine || {};
      const led = eng.ledger || {};
      if (!eng.enabled && !led.shadow_len) return;   // 引擎没开且无历史=没得看
      const client = this._cl();
      let s = null;
      if (client && client.nurtureShadow) {
        try { s = await client.nurtureShadow(8); } catch (_e) { s = null; }
      }
      this._shadow = (s && s.ok && Array.isArray(s.samples)) ? s.samples : [];
      this._renderShadowInto();
    }

    _renderShadowInto() {
      const box = this.shadowRoot.querySelector('[data-role="shadow-list"]');
      if (box) box.innerHTML = this._shadowRowsHtml();
    }

    // ── 渲染 ─────────────────────────────────────────────────────────────
    _renderAll() {
      const esc = (s) => this.esc(s);
      this._secIds = [];        // _secHead 逐个登记，末尾据此判「是不是全收起了」
      const d = this._data || {};
      const lc = d.lifecycle || {};
      const eng = d.engine || {};
      const canW = this._canWrite();
      const accounts = d.accounts || [];
      const active = lc.active || 0;
      const warming = lc.warming || 0;
      const risk = (lc.restricted || 0) + (lc.banned || 0);

      // 机群概览：健康大灯 + 一句话判词 + 三个有语境数字
      const fleet = d.fleet || {};
      const light = String(fleet.fleet_light || fleet.light || "").toLowerCase();
      const lcls = light === "green" ? "g" : light === "amber" ? "a" : light === "red" ? "r" : "u";
      const verdict = this.t("cp.nurture.verdict_" +
        (light === "green" ? "green" : light === "amber" ? "amber" : light === "red" ? "red" : "unknown"));
      // hint 给「起号期」用：这个数来自账号注册天数，不是「已配养护方案」的数，
      // 两个数并排放最容易被读成矛盾（实况 2 起号期 / 0-10 已启用）→ 口径写在悬浮里
      const kpi = (n, label, cls, hint) =>
        '<div class="nt-kpi"' + (hint ? ' title="' + esc(hint) + '"' : "") + ">" +
        '<div class="n' + (cls ? " " + cls : "") + '">' + n + "</div>" +
        '<div class="l" title="' + esc(hint || label) + '">' + esc(label) + "</div></div>";
      // 折叠时标题行接过灯 + 判词：概览这块的全部价值就是这一句，不能只剩标题
      const ovFolded = this._isFolded("overview");
      const overview =
        this._secHead("overview", this.t("cp.nurture.sec_overview"),
          ovFolded ? '<span class="nt-light ' + lcls + '"></span>' +
            '<span class="cnt" title="' + esc(verdict) + '">' + esc(verdict) + "</span>" : "") +
        (ovFolded ? "" :
          '<div class="nt-health" title="' + esc(this.t("cp.nurture.health")) + '">' +
          '<span class="nt-light ' + lcls + '"></span>' +
          '<span class="nt-verdict" title="' + esc(verdict) + '">' + esc(verdict) + "</span></div>" +
          '<div class="nt-sum">' +
          kpi(active, this.t("cp.nurture.active"), "") +
          kpi(warming, this.t("cp.nurture.warming"), warming ? "warm" : "",
            this.t("cp.nurture.warming_hint")) +
          kpi(risk, this.t("cp.nurture.risk"), risk ? "bad" : "") +
          "</div>");

      const ro = canW ? "" : '<div class="nt-ro">' + esc(this.t("cp.nurture.readonly")) + "</div>";
      // intro 横幅：引擎暂停期常驻 DOM（nurtured>0 时 hidden）——保存后 _syncCounts
      // 就地隐现，不整块重渲（重渲会冲掉行内「已保存 ✓」反馈）。
      // 账号方案折起时不出这条引导：它说的「在下方给账号开启养护」指向的东西被
      // 收起来了，留着就是指着空处说话。
      const intro = (canW && accounts.length && !eng.enabled && !this._isFolded("accounts"))
        ? '<div class="nt-intro" data-role="intro"' + ((d.nurtured || 0) ? " hidden" : "") + ">" +
          esc(this.t("cp.nurture.intro_empty")) + "</div>" : "";

      const body = ro + overview + intro +
        this._engineHtml(d, canW) +
        this._shadowSectionHtml(d) +
        this._accountsHtml(d, canW) +
        (canW ? this._advancedHtml(d) : "");
      // 全部分区收起 → 连底部科普脚注一起收（否则「全收起」后它反而是屏上最大一块
      // 文字，折叠等于没把空间还回来）。展开任意一块即回来。
      const allFolded = this._secIds.length > 0 && this._secIds.every((id) => this._isFolded(id));
      this._render(body + '<div data-role="err"></div>' +
        (allFolded ? "" : '<div class="nt-hint">' + esc(this.t("cp.nurture.hint")) + "</div>"));
      this._renderShadowInto();   // 已有样本时立即回填（重渲后列表容器是新的）
    }

    _engineHtml(d, canW) {
      const esc = (s) => this.esc(s);
      const eng = d.engine || {};
      const nurtured = d.nurtured || 0;
      const paused = !eng.enabled;
      const dry = !!(eng.enabled && eng.dry_run);
      const live = !!(eng.enabled && !eng.dry_run);
      const step = (idx, label, state) =>
        '<div class="nt-step ' + state + '" data-step="' + idx + '"><span class="dot">' +
        (state === "done" ? "✓" : idx) + "</span>" +
        '<span class="lb" title="' + esc(label) + '">' + esc(label) + "</span></div>";
      const s1 = nurtured > 0 ? "done" : "cur";
      const s2 = dry ? "cur" : (live ? "done" : "off");
      const s3 = live ? "cur" : "off";
      const steps = '<div class="nt-steps">' +
        step(1, this.t("cp.nurture.step_plan"), s1) + '<span class="nt-sep"></span>' +
        step(2, this.t("cp.nurture.step_dry"), s2) + '<span class="nt-sep"></span>' +
        step(3, this.t("cp.nurture.step_live"), s3) + "</div>";
      let state, cls;
      if (paused) { state = this.t("cp.nurture.eng_paused"); cls = "off"; }
      else if (dry) { state = this.t("cp.nurture.eng_dry"); cls = "dry"; }
      else { state = this.t("cp.nurture.eng_live", { n: eng.canary_count || 0 }); cls = "live"; }
      const stopped = (eng.enabled && !eng.running) ? (" · " + this.t("cp.nurture.eng_stopped")) : "";
      const noCanary = (live && !(eng.canary_count))
        ? '<div class="nt-note">' + esc(this.t("cp.nurture.eng_no_canary")) + "</div>" : "";
      // 引擎开着却零启用方案＝最高频误配（lint 警告收进了高级折叠，这一条必须在
      // 决策现场内联可见，否则「模拟运行中但记录永远是空的」没人看得懂）。
      const noPlans = (eng.enabled && !(nurtured > 0))
        ? '<div class="nt-note">' + esc(this.t("cp.nurture.warn_no_enabled_accounts")) + "</div>" : "";
      // 折叠时标题行接过引擎状态（含状态色）——「现在是暂停还是在真发」是这块的
      // 首要事实，收起后也必须一眼可见；折叠即不再构建按钮/确认框。
      // **两条误配警示折叠后照旧显示**：折叠是「我知道了，先收起来」，不是
      // 「把出问题的事实一起藏掉」——它们本就只在误配时才出现。
      const engFolded = this._isFolded("engine");
      const engHead = this._secHead("engine", this.t("cp.nurture.sec_engine"),
        engFolded ? '<span class="cnt ' + cls + '" title="' + esc(state + stopped) + '">' +
          esc(state + stopped) + "</span>" : "");
      if (engFolded) return engHead + noCanary + noPlans;
      let btns = "";
      if (canW) {
        if (this._goliveArm) {
          const n = eng.canary_count || 0;
          const txt = n > 0
            ? this.t("cp.nurture.golive_confirm", { n: n })
            : this.t("cp.nurture.golive_confirm_zero");
          // 试点名单点名（数字之外给出「到底是哪几个号」——运营确认的是名单不是计数）；
          // 旧后端无 is_canary 字段 → 不出名单行（fail-hidden）。
          let pilotsLine = "";
          const pilots = (d.accounts || []).filter((a) => a.is_canary === true);
          if (n > 0 && pilots.length) {
            const names = pilots.slice(0, 3).map((a) => {
              const key = String(a.nurture_key || "");
              return (a.label && String(a.label)) || this._short(key.slice(key.indexOf(":") + 1) || key);
            });
            let joined = names.join(" / ");
            if (pilots.length > 3) {
              joined += " " + this.t("cp.nurture.golive_more", { n: pilots.length - 3 });
            }
            pilotsLine = '<div class="plist">' +
              esc(this.t("cp.nurture.golive_pilots", { names: joined })) + "</div>";
          }
          btns = '<div class="nt-confirm">' + esc(txt) + pilotsLine +
            '<div class="btns"><button type="button" class="golive-yes" data-act="golive-yes">' +
            esc(this.t("cp.nurture.golive_ok")) + "</button>" +
            '<button type="button" data-act="golive-no">' + esc(this.t("cp.nurture.golive_cancel")) +
            "</button></div></div>";
        } else if (paused) {
          // 零启用方案时模拟运行必然空转（调度器只为 enabled 账号出计划）→ 禁用 +
          // 悬浮提示指路第 1 步，不给「点了没反应」的死路按钮。
          const needPlan = !(nurtured > 0);
          btns = '<div class="nt-eng-btns"><button type="button" class="primary" data-act="eng" data-eng="enable_dry" data-role="eng-try"' +
            (needPlan ? ' disabled title="' + esc(this.t("cp.nurture.eng_try_need_plan")) + '"' : "") + ">" +
            esc(this.t("cp.nurture.eng_try")) + "</button></div>";
        } else if (dry) {
          btns = '<div class="nt-eng-btns"><button type="button" class="primary" data-act="golive-ask">' +
            esc(this.t("cp.nurture.eng_golive")) + "</button>" +
            '<button type="button" data-act="eng" data-eng="pause">' + esc(this.t("cp.nurture.eng_pause")) +
            "</button></div>";
        } else {
          btns = '<div class="nt-eng-btns"><button type="button" data-act="eng" data-eng="pause">' +
            esc(this.t("cp.nurture.eng_pause")) + "</button></div>";
        }
      }
      const led = eng.ledger || {};
      const p = led.planned || 0;
      const ex = led.executed || 0;
      const foot = (p === 0 && ex === 0)
        ? this.t("cp.nurture.eng_foot_zero")
        : this.t("cp.nurture.eng_foot", { p: p, e: ex });
      return engHead +
        '<div class="nt-eng">' + steps +
        '<span class="nt-eng-state ' + cls + '">' + esc(state) + esc(stopped) + "</span>" +
        btns + '<div class="nt-eng-foot">' + esc(foot) + "</div>" + noCanary + noPlans + "</div>";
    }

    _shadowSectionHtml(d) {
      const eng = (d || {}).engine || {};
      const led = eng.ledger || {};
      const has = this._shadow && this._shadow.length;
      if (!eng.enabled && !led.shadow_len && !has) return "";
      const folded = this._isFolded("shadow");
      const head = this._secHead("shadow", this.t("cp.nurture.shadow_hd"),
        (folded && has) ? '<span class="cnt">' + this._shadow.length + "</span>" : "");
      if (folded) return head;
      return head +
        '<div class="nt-shadow"><div data-role="shadow-list">' + this._shadowRowsHtml() + "</div></div>";
    }

    _shadowRowsHtml() {
      const esc = (s) => this.esc(s);
      const list = this._shadow;
      if (!list) return '<div class="nt-empty">' + esc(this.t("cp.nurture.loading")) + "</div>";
      if (!list.length) return '<div class="nt-empty">' + esc(this.t("cp.nurture.shadow_empty")) + "</div>";
      return list.map((r) => {
        const key = String(r.key || "");
        const kind = this._tt("cp.nurture.beh_" + String(r.kind || ""), r.kind);
        let chip, cls;
        if (r.ok === false) { chip = this.t("cp.nurture.shadow_fail"); cls = "fail"; }
        else if (r.dry_run) { chip = this.t("cp.nurture.shadow_dry"); cls = "dry"; }
        else { chip = this.t("cp.nurture.shadow_real"); cls = "real"; }
        return '<div class="nt-sh-row"><span class="nt-sh-ago">' + esc(this._ago(r.ts)) + "</span>" +
          '<span class="nt-sh-nm" title="' + esc(key) + '">' + esc(this._acctLabel(key)) + "</span>" +
          '<span class="nt-sh-kind">' + esc(kind) + "</span>" +
          '<span class="nt-sh-chip ' + cls + '">' + esc(chip) + "</span></div>";
      }).join("");
    }

    _accountsHtml(d, canW) {
      const esc = (s) => this.esc(s);
      const accounts = d.accounts || [];
      const folded = this._isFolded("accounts");
      // 「n/t 已启用」折不折叠都在（_syncCounts 就地回填靠这个 data-role，
      // 折叠态下它仍在 DOM 里 → 保存后计数照常更新）
      const cnt = accounts.length
        ? '<span class="cnt" data-role="acct-cnt">' +
          esc(this.t("cp.nurture.acct_count", { n: d.nurtured || 0, t: accounts.length })) + "</span>"
        : "";
      const dirtyN = folded
        ? Object.keys(this._work).filter((k) => this._isDirty(k)).length : 0;
      const dirty = dirtyN
        ? '<span class="dirty" title="' + esc(this.t("cp.nurture.save")) + '">●</span>' : "";
      const head = this._secHead("accounts", this.t("cp.nurture.sec_accounts"), cnt + dirty);
      if (folded) return head;
      const vis = this._visibleAccounts(accounts);
      const rows = vis.rows.map((a) => this._acctHtml(a, canW)).join("") ||
        '<div class="nt-empty">' + esc(this.t("cp.nurture.no_accts")) + "</div>";
      let more = "";
      if (vis.hidden) {
        more = '<button type="button" class="nt-acct-more" data-act="acct-all">' +
          esc(this.t("cp.nurture.acct_show_all", { n: accounts.length })) + "</button>";
      } else if (this._acctShowAll && accounts.length > ACCT_VISIBLE_CAP + 1) {
        more = '<button type="button" class="nt-acct-more" data-act="acct-less">' +
          esc(this.t("cp.nurture.acct_show_less")) + "</button>";
      }
      return head + rows + more;
    }

    /* 「这个号在养吗」——工作副本优先于服务端基线（未保存的开关也算，否则刚打开
       开关的号会在下一次重渲里被截断藏走）。 */
    _acctOn(a) {
      const w = this._work[String(a.nurture_key || "")];
      return w ? !!w.enabled : !!((a.nurture_plan || {}).enabled);
    }
    /* 截断选择：保持原序，可见集 = 全部在养的号 ∪ 其余补到 CAP。 */
    _visibleAccounts(accounts) {
      if (this._acctShowAll || accounts.length <= ACCT_VISIBLE_CAP + 1) {
        return { rows: accounts, hidden: 0 };
      }
      const on = accounts.filter((a) => this._acctOn(a));
      const room = Math.max(0, ACCT_VISIBLE_CAP - on.length);
      const keep = {};
      on.concat(accounts.filter((a) => !this._acctOn(a)).slice(0, room))
        .forEach((a) => { keep[String(a.nurture_key || "")] = 1; });
      const rows = accounts.filter((a) => keep[String(a.nurture_key || "")]);
      return { rows: rows, hidden: accounts.length - rows.length };
    }

    _acctHtml(a, canW) {
      const esc = (s) => this.esc(s);
      const key = String(a.nurture_key || "");
      const w = this._work[key] || { enabled: false, profile: "balanced", behaviors: {} };
      const stage = String(a.stage || "offline");
      const stCls = "st-" + (STAGES.indexOf(stage) >= 0 ? stage : "offline");
      const platform = key.indexOf(":") > 0 ? key.slice(0, key.indexOf(":")) : "";
      const rawId = key.indexOf(":") > 0 ? key.slice(key.indexOf(":") + 1) : key;
      const name = (a.label && String(a.label)) || this._short(rawId);
      const profiles = (this._data && this._data.profiles) || ["conservative", "balanced", "aggressive"];
      const behaviors = (this._data && this._data.behaviors) || ["browse", "read", "react", "self_chat"];
      const pOpts = profiles.map((p) =>
        '<option value="' + esc(p) + '"' + (p === w.profile ? " selected" : "") + ">" +
        esc(this._tt("cp.nurture.prof_" + p, p)) + "</option>").join("");
      const expanded = !!this._expanded[key];
      const dis = canW ? "" : " disabled";
      const behHtml = expanded
        ? '<div class="nt-beh">' + behaviors.map((b) => {
            const risk = b === "self_chat";
            const on = !!w.behaviors[b];
            return '<button type="button" class="nt-chip' + (risk ? " risk" : "") + '" data-act="beh" data-beh="' +
              esc(b) + '" aria-pressed="' + (on ? "true" : "false") + '"' + dis + ">" +
              esc(this._tt("cp.nurture.beh_" + b, b)) + "</button>";
          }).join("") + "</div>"
        : "";
      // 试点 chip（P1 试点 UI 化）：旧后端无 is_canary 字段 → fail-hidden 不出 chip；
      // viewer 只读可见不可点（试点身份是重要状态，隐藏比禁用更糟）。
      const pilotChip = (a.is_canary === undefined) ? "" :
        '<button type="button" class="nt-pilot" data-act="pilot" aria-pressed="' +
        (a.is_canary ? "true" : "false") + '" title="' +
        esc(this.t(a.is_canary ? "cp.nurture.pilot_title_on" : "cp.nurture.pilot_title_off")) +
        '"' + dis + ">" + esc(this.t("cp.nurture.pilot")) + "</button>";
      return '<div class="nt-acct" data-key="' + esc(key) + '">' +
        '<div class="hd">' +
        (platform ? '<span class="nt-plat">' + esc(platform) + "</span>" : "") +
        '<span class="nm" title="' + esc(key) + '">' + esc(name) + "</span>" +
        pilotChip +
        '<span class="nt-stage ' + stCls + '">' + esc(this._tt("cp.nurture.stage_" + stage, stage)) + "</span></div>" +
        '<div class="ctl">' +
        '<label class="nt-sw"><input type="checkbox" data-role="on"' + (w.enabled ? " checked" : "") + dis +
        ' /><span class="track"></span></label>' +
        '<span class="nt-sw-lb">' + esc(this.t("cp.nurture.on")) + "</span>" +
        '<select data-role="profile"' + dis + ">" + pOpts + "</select>" +
        '<button type="button" data-act="more">' +
        esc(expanded ? this.t("cp.nurture.less") : this.t("cp.nurture.more")) + "</button>" +
        (canW
          ? '<button type="button" class="nt-save primary" data-act="save"' +
            (this._isDirty(key) ? "" : " disabled") + ">" + esc(this.t("cp.nurture.save")) + "</button>"
          : "") +
        '<span class="nt-rowmsg" data-role="msg"></span>' +
        "</div>" + behHtml + "</div>";
    }

    _advancedHtml(d) {
      const folded = this._isFolded("advanced");   // 唯一默认收起的分区
      const ws = (d && d.warnings) || [];
      const head = this._secHead("advanced", this.t("cp.nurture.sec_advanced"),
        (folded && ws.length) ? '<span class="cnt">⚠ ' + ws.length + "</span>" : "",
        "nt-adv-hd");
      const body = folded ? ""
        : '<div class="nt-adv-bd">' + this._warningsHtml(d) + this._probeHtml(d) + "</div>";
      return '<div class="nt-adv">' + head + body + "</div>";
    }

    _warningsHtml(d) {
      const ws = (d && d.warnings) || [];
      if (!ws.length) return "";
      const esc = (s) => this.esc(s);
      return '<div class="nt-warns">' + ws.map((w) => {
        const k = "cp.nurture.warn_" + String(w.code || "");
        const raw = this.t(k, w.key ? { key: w.key } : undefined);
        const txt = raw === k ? String(w.code || "") + (w.key ? " " + w.key : "") : raw;
        const sev = w.severity === "warn" ? "warn" : "info";
        return '<div class="nt-warn ' + sev + '">' + esc(txt) + "</div>";
      }).join("") + "</div>";
    }

    _probeHtml(d) {
      const esc = (s) => this.esc(s);
      const accts = (d && d.accounts) || [];
      if (!accts.length) return "";  // 无在册号=没得试
      const aOpts = accts.map((a) => {
        const key = String(a.nurture_key || "");
        const nm = (a.label && String(a.label)) || key;
        return '<option value="' + esc(key) + '">' +
          esc(nm.length > 30 ? nm.slice(0, 28) + "…" : nm) + "</option>";
      }).join("");
      const kinds = ["read", "self_chat", "online", "browse", "react"];
      const kOpts = kinds.map((k) =>
        '<option value="' + k + '">' + esc(this._tt("cp.nurture.beh_" + k, k)) + "</option>").join("");
      return '<div class="nt-probe"><div class="nt-probe-hd">' + esc(this.t("cp.nurture.probe_hd")) + "</div>" +
        '<div class="nt-probe-row">' +
        '<select data-role="probe-acct">' + aOpts + "</select>" +
        '<select data-role="probe-kind">' + kOpts + "</select>" +
        '<button type="button" data-act="probe-pre">' + esc(this.t("cp.nurture.probe_pre")) + "</button>" +
        '<button type="button" class="run" data-act="probe-run">' + esc(this.t("cp.nurture.probe_run")) + "</button>" +
        "</div><div class=\"nt-probe-msg\" data-role=\"probe-msg\"></div></div>";
    }

    // ── 交互 ─────────────────────────────────────────────────────────────
    onAction(act, el) {
      if (act === "eng") {
        const a = el.getAttribute("data-eng");
        _beacon(a === "enable_dry" ? "ntr_sim_start" : "ntr_pause");
        this._saveEngine(a);
        return;
      }
      if (act === "golive-ask") { _beacon("ntr_golive_ask"); this._goliveArm = true; this._renderAll(); return; }
      if (act === "golive-no") { _beacon("ntr_golive_cancel"); this._goliveArm = false; this._renderAll(); return; }
      if (act === "golive-yes") { _beacon("ntr_golive_confirm"); this._goliveArm = false; this._saveEngine("go_live"); return; }
      if (act === "fold") { this._toggleFold(el.getAttribute("data-fold")); return; }
      if (act === "acct-all" || act === "acct-less") {
        this._acctShowAll = (act === "acct-all");
        _beacon(this._acctShowAll ? "ntr_acct_show_all" : "ntr_acct_show_less");
        this._renderAll();
        return;
      }
      if (act === "probe-pre") { _beacon("ntr_probe_pre"); this._probe(false, null); return; }
      if (act === "probe-run") { this._probeRun(el); return; }
      const row = el.closest(".nt-acct");
      const key = row ? row.getAttribute("data-key") : "";
      if (!key) return;
      if (act === "pilot") {
        // 引擎正在自动养护时「设为试点」＝该号立即参与真实动作 → 两击 armed 确认；
        // 取消试点/引擎未 live 时直接生效（可逆、缩小影响面的方向不设阻力）。
        const on = el.getAttribute("aria-pressed") === "true";
        const eng = (this._data || {}).engine || {};
        const live = !!(eng.enabled && !eng.dry_run);
        if (!on && live && el.getAttribute("data-armed") !== "1") {
          el.setAttribute("data-armed", "1");
          el.classList.add("armed");
          el.setAttribute("title", this.t("cp.nurture.pilot_confirm"));
          setTimeout(() => {
            if (el && el.getAttribute("data-armed") === "1") {
              el.removeAttribute("data-armed");
              el.classList.remove("armed");
              el.setAttribute("title", this.t("cp.nurture.pilot_title_off"));
            }
          }, 3000);
          return;
        }
        el.removeAttribute("data-armed");
        el.classList.remove("armed");
        this._setPilot(key, !on, el, row);
        return;
      }
      if (act === "more") {
        this._expanded[key] = !this._expanded[key];
        this._renderAll();
        return;
      }
      if (act === "beh") {
        const b = el.getAttribute("data-beh") || "";
        const w = this._work[key];
        if (!w || !b) return;
        w.behaviors[b] = !w.behaviors[b];
        el.setAttribute("aria-pressed", w.behaviors[b] ? "true" : "false");
        this._syncRowDirty(key);
        return;
      }
      if (act === "save") { this._saveAcct(row, key, el); return; }
    }

    _onFieldChange(e) {
      const el = e.target;
      if (!el || !el.closest) return;
      const row = el.closest(".nt-acct");
      if (!row) return;
      const key = row.getAttribute("data-key") || "";
      const w = this._work[key];
      if (!w) return;
      const role = el.getAttribute("data-role");
      if (role === "on") w.enabled = !!el.checked;
      else if (role === "profile") w.profile = String(el.value || "balanced");
      else return;
      this._syncRowDirty(key);
    }

    /* 试点增删：写 ops.nurture.canary_accounts（服务端单一 UI 写入口），
       成功后整卡回源——试点数影响 go_live 确认文案/引擎状态行，就地补丁不如回源可靠。 */
    async _setPilot(key, pilot, el, row) {
      const client = this._cl();
      if (!client || !client.nurtureSave) return;
      if (el) el.disabled = true;
      try {
        const d = await client.nurtureSave({ canary: { key: key, pilot: !!pilot } });
        if (!d || !d.ok) {
          if (el) el.disabled = false;
          const msg = row && row.querySelector('[data-role="msg"]');
          if (msg) { msg.textContent = this.t("cp.nurture.pilot_fail"); msg.style.color = "var(--cp-danger,#dc2626)"; }
          return;
        }
        _beacon(pilot ? "ntr_pilot_on" : "ntr_pilot_off");
        this._load();
      } catch (_e) {
        if (el) el.disabled = false;
        const msg = row && row.querySelector('[data-role="msg"]');
        if (msg) { msg.textContent = this.t("cp.nurture.net_err"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
    }

    _isDirty(key) {
      const w = this._work[key];
      return !!w && JSON.stringify(w) !== this._baseline[key];
    }
    _rowEl(key) {
      const rows = this.shadowRoot.querySelectorAll(".nt-acct");
      for (let i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-key") === key) return rows[i];
      }
      return null;
    }
    _syncRowDirty(key) {
      const row = this._rowEl(key);
      if (!row) return;
      const btn = row.querySelector('[data-act="save"]');
      if (btn) btn.disabled = !this._isDirty(key);
      if (this._isDirty(key)) {
        const msg = row.querySelector('[data-role="msg"]');
        if (msg) msg.textContent = "";
      }
    }
    /* 保存后就地同步「n/t 已启用」小计、步骤 1 状态、intro 横幅与模拟运行按钮
       （整块重渲会冲掉「已保存 ✓」反馈，故全部就地更新） */
    _syncCounts() {
      const d = this._data || {};
      const n = d.nurtured || 0;
      const cnt = this.shadowRoot.querySelector('[data-role="acct-cnt"]');
      if (cnt) cnt.textContent = this.t("cp.nurture.acct_count", { n: n, t: (d.accounts || []).length });
      const s1 = this.shadowRoot.querySelector('[data-step="1"]');
      if (s1) {
        const done = n > 0;
        s1.className = "nt-step " + (done ? "done" : "cur");
        const dot = s1.querySelector(".dot");
        if (dot) dot.textContent = done ? "✓" : "1";
      }
      const intro = this.shadowRoot.querySelector('[data-role="intro"]');
      if (intro) intro.hidden = n > 0;
      const tryBtn = this.shadowRoot.querySelector('[data-role="eng-try"]');
      if (tryBtn) {
        tryBtn.disabled = !(n > 0);
        if (n > 0) tryBtn.removeAttribute("title");
        else tryBtn.setAttribute("title", this.t("cp.nurture.eng_try_need_plan"));
      }
    }

    _ago(ts) {
      if (!ts) return "";
      const dif = Math.max(0, Date.now() / 1000 - (Number(ts) || 0));
      if (dif < 90) return this.t("cp.nurture.ago_now");
      if (dif < 3600) return this.t("cp.nurture.ago_min", { m: Math.max(2, Math.round(dif / 60)) });
      if (dif < 172800) return this.t("cp.nurture.ago_hour", { h: Math.max(1, Math.round(dif / 3600)) });
      return this.t("cp.nurture.ago_day", { d: Math.max(2, Math.round(dif / 86400)) });
    }
    _short(s) {
      const v = String(s || "");
      return v.length > 24 ? v.slice(0, 22) + "…" : v;
    }
    _acctLabel(key) {
      const d = this._data || {};
      const a = (d.accounts || []).find((x) => String(x.nurture_key || "") === key);
      const lb = a && a.label ? String(a.label) : "";
      return lb || this._short(key);
    }

    _probeRun(btn) {
      // 真跑一次=两击确认（首击进入 armed 危险态：红底 + 确认文案，3s 未确认自动回退）
      if (btn && btn.getAttribute("data-armed") === "1") {
        btn.removeAttribute("data-armed");
        btn.classList.remove("armed");
        btn.textContent = this.t("cp.nurture.probe_run");
        _beacon("ntr_probe_run");
        this._probe(true, btn);
        return;
      }
      if (btn) {
        btn.setAttribute("data-armed", "1");
        btn.classList.add("armed");
        btn.textContent = this.t("cp.nurture.probe_confirm");
        setTimeout(() => {
          if (btn && btn.getAttribute("data-armed") === "1") {
            btn.removeAttribute("data-armed");
            btn.classList.remove("armed");
            btn.textContent = this.t("cp.nurture.probe_run");
          }
        }, 3000);
      }
    }

    async _probe(confirm, _btn) {
      const sr = this.shadowRoot;
      const acct = (sr.querySelector('[data-role="probe-acct"]') || {}).value || "";
      const kind = (sr.querySelector('[data-role="probe-kind"]') || {}).value || "";
      const msg = sr.querySelector('[data-role="probe-msg"]');
      if (!acct || acct.indexOf(":") < 0) return;
      const client = this._cl();
      if (!client || !client.nurtureProbe) return;
      const platform = acct.slice(0, acct.indexOf(":"));
      const account_id = acct.slice(acct.indexOf(":") + 1);
      if (msg) { msg.textContent = this.t("cp.nurture.probe_running"); msg.style.color = "var(--cp-text-tiny,#94a3b8)"; }
      try {
        const d = await client.nurtureProbe({ platform: platform, account_id: account_id, kind: kind, confirm: !!confirm });
        if (!d || !d.ok) {
          if (msg) {
            msg.textContent = d && d.reason === "engine_not_loaded"
              ? this.t("cp.nurture.probe_not_loaded") : this.t("cp.nurture.net_err");
            msg.style.color = "var(--cp-danger,#dc2626)";
          }
          return;
        }
        const r = d.result || {};
        let txt, clr;
        if (!r.executable) { txt = this.t("cp.nurture.probe_no_adapter"); clr = "var(--cp-text-dim,#64748b)"; }
        else if (r.blocked) { txt = this.t("cp.nurture.probe_blocked"); clr = "var(--cp-danger,#dc2626)"; }
        else if (!confirm) { txt = this.t("cp.nurture.probe_ok_preview"); clr = "var(--cp-ok,#0f9d75)"; }
        else if (r.ok) { txt = this.t("cp.nurture.probe_ran_ok", { ms: r.latency_ms || 0 }); clr = "var(--cp-ok,#0f9d75)"; }
        else { txt = this.t("cp.nurture.probe_ran_fail", { d: String(r.detail || "") }); clr = "var(--cp-danger,#dc2626)"; }
        if (msg) { msg.textContent = txt; msg.style.color = clr; }
        if (confirm && r && r.executable && !r.blocked) this._loadShadow();  // 真跑落账 → 记录区可见
      } catch (_e) {
        if (msg) { msg.textContent = this.t("cp.nurture.net_err"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
    }

    async _saveEngine(action) {
      if (!action) return;
      const client = this._cl();
      if (!client || !client.nurtureEngine) return;
      try {
        const d = await client.nurtureEngine({ action: action });
        if (!d || !d.ok) {
          this._errBox(d && d.reason === "not_enabled"
            ? this.t("cp.nurture.eng_need_dry_first")
            : this.t("cp.nurture.save_fail", { r: String((d && (d.error || d.detail || d.reason)) || "") }));
          return;
        }
        this._load();   // 重载刷新引擎状态（work/baseline 一并回源）
      } catch (_e) { this._errBox(this.t("cp.nurture.net_err")); }
    }

    async _saveAcct(row, key, btn) {
      if (!row || !key) return;
      const client = this._cl();
      if (!client || !client.nurtureSave) return;
      const w = this._work[key];
      if (!w) return;
      const parts = key.split(":");
      const platform = parts[0] || "";
      const account_id = parts.slice(1).join(":") || "default";
      const plan = {
        enabled: !!w.enabled,
        profile: w.profile || "balanced",
        behaviors: Object.assign({}, w.behaviors),
      };
      const msg = row.querySelector('[data-role="msg"]');
      if (btn) btn.disabled = true;
      try {
        const d = await client.nurtureSave({ account: { platform: platform, account_id: account_id, plan: plan } });
        if (!d || !d.ok) {
          if (btn) btn.disabled = !this._isDirty(key);
          if (msg) { msg.textContent = this.t("cp.nurture.save_err"); msg.style.color = "var(--cp-danger,#dc2626)"; }
          return;
        }
        _beacon("ntr_save");
        this._baseline[key] = JSON.stringify(w);
        if (this._data) {
          const a = (this._data.accounts || []).find((x) => String(x.nurture_key || "") === key);
          if (a) a.nurture_plan = Object.assign({}, a.nurture_plan || {}, plan);
          this._data.nurtured = (this._data.accounts || []).reduce((acc, x) => {
            const kk = String(x.nurture_key || "");
            const ww = this._work[kk];
            return acc + ((ww ? ww.enabled : !!((x.nurture_plan || {}).enabled)) ? 1 : 0);
          }, 0);
        }
        this._syncCounts();
        if (btn) btn.disabled = true;
        if (msg) { msg.textContent = this.t("cp.nurture.saved"); msg.style.color = "var(--cp-ok,#0f9d75)"; }
        setTimeout(() => { if (msg) msg.textContent = ""; }, 2200);
      } catch (_e) {
        if (btn) btn.disabled = !this._isDirty(key);
        if (msg) { msg.textContent = this.t("cp.nurture.save_err"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
    }

    _errBox(m) {
      const e = this.shadowRoot.querySelector('[data-role="err"]');
      if (e) { e.innerHTML = '<div class="nt-err">' + this.esc(m) + "</div>"; setTimeout(() => { if (e) e.innerHTML = ""; }, 3000); }
    }
    tf(key, vars) { return this.t(key, vars); }
  }

  if (!customElements.get("cp-nurture")) customElements.define("cp-nurture", CpNurture);
})(typeof window !== "undefined" ? window : this);
