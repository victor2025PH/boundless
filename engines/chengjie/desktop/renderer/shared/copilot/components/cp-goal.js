"use strict";
/* 两端共享组件 · 工作目标(<cp-goal>)— 继承 CpPanelBase
   统一收件箱右栏「工作目标」卡（对接 /api/goals*，后端 goal_routes.py）：
   - 无目标：插画空态 + 两步建目标向导（P24 改版）：
     第一步（栏内）＝按业务分组的场景卡（转化/关系/唤回，类别色+线稿图标，
     每模板一句人话适用场景）+ 底部「自定义目标」独立高亮卡（进阶入口，
     不参与默认预选）；沉默 ≥72h 时推荐「沉默唤回」（ctx.goalHint.silentHours
     由宿主喂）。第二步＝场景专属设置弹层（shadow 内 fixed 居中 modal）：
     「AI 会怎么推进」节奏预览（milestones + push_curve）→ 场景参数人话控件
     （解锁项/会员档下拉读 /api/monetize/catalog 或 templates.pickers，称呼
     字段自动跟随所选项；阶段下拉中文化；亲密度滑杆；备注示例 chips；
     留存续费自动继承参数折叠进「高级」）→ 期限 → AI 参与度三张单选卡 +
     主动触达信息气泡。期限/自治档保留偏好记忆；未注册参数回落通用输入框。
     弹层输入有**草稿幸存层**（P0 2026-08-04）：逐键快照进 sessionStorage（按会话
     id，24h TTL），背板误触/宿主重喂 context/取数失败/页面刷新都不丢；编辑期
     同会话的外部刷新一律挂起（set context 覆写），表单关闭后补刷。
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
  /* i18n P0（2026-08-19）：今日意图展示态——en UI 优先载荷的 intent_en
     （templates.py 同池英文变体），缺失回落中文原文（旧后端/存量行零破坏）。
     注意：发给拟稿链的**指令**仍用中文 intent 本体（prompt 口径不变），
     只有「给坐席看」的文案走本函数。 */
  function intentDisp(o) {
    if (!o) return "";
    if (LANG === "en") {
      const en = String(o.intent_en || "");
      if (en) return en;
    }
    return String(o.intent || "");
  }
  const TERMINAL = { done: 1, failed: 1, expired: 1, cancelled: 1 };
  const AUTONOMY = ["observe", "suggest", "auto"];
  // close=收口档（P3 2026-08-30：限时档剩余 <35% 升档产生）
  const PUSH_LEVELS = ["none", "soft", "direct", "close"];
  const HOLDS = ["emotion", "silent", "no_intent", "pace_cap"];
  const PACES = ["natural", "today", "session"];
  /* 旧后端 templates 响应可能缺 sprint_ok；与 pace.SPRINT_OK 对齐的前端回落。 */
  const SPRINT_OK_FALLBACK = {
    custom: 1, conversion_unlock: 1, conversion_subscribe: 1, acquire_and_convert: 1,
  };
  // P22：生命周期/人设自动创建的来源 → UI「AI 自建」徽标
  const AUTO_ORIGIN = { auto_create: 1, winback_auto: 1, retention_auto: 1, reconvert_auto: 1 };
  const PREFS_KEY = "cp_goal_form_prefs_v1";
  // 表单草稿幸存层（P0 2026-08-04）：第二步弹层输入的实时快照，键=会话 id。
  // 刻意用 sessionStorage（同标签页作用域）：跨「误触关闭/外部重渲染/页面刷新」
  // 存活，关标签页即清——共用工位不跨天残留与客户相关的文本。
  const DRAFT_KEY_PREFIX = "cp_goal_form_draft_v1:";
  const DRAFT_TTL_MS = 24 * 3600 * 1000;
  const UNDO_MS = 5000;
  // 场景卡分组序（模板 kind → 组；未知 kind 追加到尾部「其他」组）。
  // 2026-08-18 摸底置顶：组序=经营漏斗序（先摸清客户 → 建关系 → 转化收钱 →
  // 救流失），不是「谁最想要钱」序——入口引导坐席按正确顺序开目标，摸底产出的
  // 画像（年龄/职业/预算）直接反哺后面转化目标的选品与报价。
  const KIND_ORDER = ["discovery", "conversion", "relationship", "engagement"];
  // 完成通知链路状态（模块级缓存 5min：跨会话/跨组件实例共享一次探测；
  // 端点未装载（旧后端/重启窗未到）→ data=null → 整行隐藏＝特性探测，绝不裸奔）
  const NOTIFY_STATUS = { at: 0, data: null, inflight: false };
  // 摸底槽位 chips 回落表（后端 pickers.discovery_slots 优先；与 profile_slots 登记对齐）
  const FALLBACK_DISCOVERY_SLOTS = [
    { key: "age", track: "relation", label_zh: "年龄", label_en: "Age" },
    { key: "occupation", track: "relation", label_zh: "职业/生意", label_en: "Occupation" },
    { key: "location", track: "relation", label_zh: "坐标", label_en: "Location" },
    { key: "interests", track: "relation", label_zh: "兴趣", label_en: "Interests" },
    { key: "name", track: "relation", label_zh: "称呼", label_en: "Name" },
    { key: "need", track: "bant", label_zh: "业务痛点", label_en: "Pain point" },
    { key: "channel", track: "bant", label_zh: "在用平台", label_en: "Channels" },
    { key: "team_size", track: "bant", label_zh: "团队规模", label_en: "Team size" },
    { key: "budget", track: "bant", label_zh: "预算档", label_en: "Budget" },
    { key: "authority", track: "bant", label_zh: "决策角色", label_en: "Authority" },
    { key: "timeline", track: "bant", label_zh: "上线时间", label_en: "Timeline" },
  ];
  const DISCOVERY_NOTE_RE = /获取|了解|摸底|年龄|职业|几岁|做什么|画像|兴趣|坐标|城市|age|occupation|profile|discover|learn.*(age|job|work)/i;
  // 漏斗阶段词表兜底（与 contacts Journey STAGE_ORDER 对齐；后端 templates
  // 响应带 pickers.stages 时优先用服务端数据，这里只是旧后端回落）
  const STAGES = ["initial", "contacted", "engaged", "qualified",
    "handoff_ready", "handed_off", "converted"];
  // 折叠进「高级参数」的字段（通常由系统自动带入，一般不用改）
  const ADV_PARAMS = {
    retention_expand: ["product_id", "last_plan", "last_period", "base_goal"],
    engagement_reactivate: ["product_id", "last_plan"],
  };

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
      this._formStep = 1;            // 两步向导：1=选场景（栏内） 2=场景设置弹层
      this._formTid = "";
      this._formAutonomy = "";       // 参与度单选卡当前选择（跨 DOM 更新持久）
      this._formUsePrefDays = true;  // 首开用偏好天数；坐席换场景后跟场景默认
      this._formPace = "natural";    // 推进节奏：自然天 / 今天收口 / 这轮聊完
      this._formForce = "steady";    // 冲刺推进力度：稳妥 / 全力（P3 2026-08-30）
      this._jumpOffer = false;       // 撞 active_limit 后的「插队开冲刺」提议行
      this._resumeGoalId = "";       // 插队回程票：冲刺终局要恢复的长线目标
      this._spTick = 0;              // 冲刺倒计时 30s 活字计时器
      this._catalog = null;          // /api/monetize/catalog 解析结果（解锁项/会员档）
      this._catalogTried = false;    // 只试一次，失败回落通用输入框
      this._unlockSel = "";          // 解锁项下拉选中 id（"__custom__"=高级手填）
      this._tierSel = "";            // 会员档下拉选中 id
      this._labelTouched = false;    // 坐席手改过「称呼」→ 停止自动跟随
      this._lastAutoLabel = "";      // 上次自动填入的称呼（判断可否覆盖）
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
      // 调整期限内联表单（P25）：开合态 + 输入暂存 + 归属目标（防换会话串卡）
      this._deadlineOpen = false;
      this._deadlineVal = null;
      this._deadlineForGid = "";
      this._benchCache = {};        // P1：模板 → 近30天基准（null=无足量数据）
      this._lastDeadlineErr = "";
      // 草稿幸存层 + 编辑防打断（P0 2026-08-04；详见 _draftCapture/_applyFormDraft/set context）
      this._formDraft = null;       // 当前会话未提交草稿（sessionStorage 镜像）
      this._formBaseline = null;    // 第二步「出厂快照」＝判脏基准（草稿应用前拍）
      this._ctxDeferred = false;    // 编辑期被挂起的宿主 context 重喂（关表单后补刷）
      this._restoring = false;      // 草稿回填进行中（抑制埋点/焦点抢占）
      this._mhintTimer = null;      // 弹层内提示条自动隐藏定时器
      // P1 2026-08-04：弹层键盘/焦点体验（初始聚焦、重建后焦点回位、Tab 陷阱）
      this._modalWasOpen = false;   // 弹层是否已呈现过（区分「新开聚焦」vs「重建回位」）
      this._lastFocusRef = "";      // 弹层内最后聚焦的控件（param key 或 __days__）

      this.shadowRoot.addEventListener("change", (e) => {
        const t = e.target;
        if (!t || !t.getAttribute) return;
        const chg = t.getAttribute("data-chg");
        if (chg === "unlock_sel") this._onUnlockSel(t);
        else if (chg === "tier_sel") this._onTierSel(t);
        this._draftCapture();   // 任何控件变化实时进草稿（select/checkbox 走 change）
      });
      this.shadowRoot.addEventListener("input", (e) => {
        const t = e.target;
        if (!t || !t.getAttribute) return;
        const chg = t.getAttribute("data-chg");
        if (chg === "range") {
          const out = this._ref("rangeval");
          if (out) out.textContent = String(t.value);
        } else if (chg === "item_label") {
          this._labelTouched = true;   // 坐席自己写了称呼 → 不再自动跟随下拉
        } else if (chg === "dl_days") {
          // 改期限动态提示：只改 DOM 文本不重渲染（保输入焦点）
          this._deadlineVal = t.value;
          const g = this._d && this._d.goal;
          const h = this.shadowRoot.querySelector('[data-ref="dl_hint"]');
          if (g && h) h.textContent = this._deadlineHint(g, t.value);
          const er = this.shadowRoot.querySelector('[data-ref="dl_err"]');
          if (er) er.hidden = true;
        } else if (chg === "note_disc") {
          // 自定义 note 像摸底诉求 → 显/隐「改用客户摸底」条（不重渲染保焦点）
          const tip = this._ref("disc_tip");
          if (tip) tip.hidden = !DISCOVERY_NOTE_RE.test(String(t.value || ""));
        }
        if (t.getAttribute("data-ref") === "days") this._updatePaceLine();
        this._draftCapture();   // 逐键实时进草稿（文本/数字/滑杆走 input）
      });
      this.shadowRoot.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this._deadlineOpen) {
          e.preventDefault();
          this.onAction("deadline_cancel", null);
          return;
        }
        if (e.key === "Escape" && this._formOpen && this._formStep === 2) {
          e.preventDefault();
          this.onAction("form_back", null);
          return;
        }
        if (e.key === "Tab" && this._formOpen && this._formStep === 2) {
          this._trapModalTab(e);   // P1：对话框语义——Tab 只在弹层内循环
          return;
        }
        if (e.key !== "Enter") return;
        if ((e.ctrlKey || e.metaKey) && this._formOpen && this._formStep === 2) {
          // P1：Ctrl/Cmd+Enter＝创建（键盘流不必够到弹层底部按钮）
          e.preventDefault();
          const cbtn = this.shadowRoot.querySelector('.gl-mfoot [data-act="create"]');
          if (cbtn && !cbtn.disabled) this.onAction("create", cbtn);
          return;
        }
        const t = e.target;
        if (!t || !t.getAttribute) return;
        if (t.getAttribute("data-prof-key") != null) {
          e.preventDefault();
          this._profSave(this.shadowRoot.querySelector('[data-act="prof_save"]'));
        }
      });
      // P1：记住弹层内最后聚焦的控件——整块重建后焦点回到编辑位而不是丢到 body
      this.shadowRoot.addEventListener("focusin", (e) => {
        if (!this._formOpen || this._formStep !== 2) return;
        const t = e.target;
        if (!t || !t.getAttribute) return;
        const k = t.getAttribute("data-param-key")
          || (t.getAttribute("data-ref") === "days" ? "__days__" : "");
        if (k) this._lastFocusRef = k;
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
      /* P3b 时间兑底段：虚纹=「节奏保底走到的」，与实心信号段诚实区分 */
      .gl-seg.done.time { background:repeating-linear-gradient(90deg,
                          var(--cp-ok,#0f9d75) 0 4px, transparent 4px 7px); }
      .gl-seg.cur.time { background:repeating-linear-gradient(90deg,
                         var(--cp-accent,#4f46e5) 0 4px, transparent 4px 7px); }
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
      /* 「第X/Y天」可点改期限：视觉与旧纯文本一致，hover 才显出可交互 */
      .gl-meta-btn { background:transparent; border:none; padding:0; margin:0; cursor:pointer;
                     font:inherit; font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); }
      .gl-meta-btn:hover { color:var(--cp-accent,#4f46e5); }
      /* P3b 临期紧迫：时间 ≥80% 且进度 <50% → 警示色（点开即改期限） */
      .gl-meta-btn.urgent { color:var(--cp-warn,#b45309); font-weight:700; }
      .gl-meta-btn.urgent:hover { color:var(--cp-warn,#b45309); text-decoration:underline; }
      .gl-meta-pen { margin-left:4px; opacity:.55; font-size:10px; }
      .gl-dl-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
      .gl-dl-row input { width:64px; flex:0 0 auto; }
      .gl-dl-chips { display:flex; flex-wrap:wrap; gap:4px; }
      .gl-chip.on { color:var(--cp-accent,#4f46e5); border-color:var(--cp-accent,#4f46e5);
                    background:var(--cp-accent-weak,rgba(79,70,229,.08)); }
      /* 临期决策行（P1）：琥珀 chip + 就地动作，样式对齐 .gl-badge.warn */
      .gl-due { display:flex; align-items:center; gap:6px; flex-wrap:wrap;
                margin:2px 0 var(--cp-gap-xs,4px); }
      .gl-due-chip { display:inline-block; padding:1px 7px; border-radius:99px; line-height:1.5;
                     font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn,#b45309);
                     border:1px solid color-mix(in srgb,var(--cp-warn,#d97706) 40%,transparent);
                     background:color-mix(in srgb,var(--cp-warn,#d97706) 12%,transparent); }
      .gl-due button { font-size:var(--cp-fs-tiny,11px); padding:1px 8px; }
      .gl-extend { display:inline-flex; gap:4px; margin-left:4px; vertical-align:middle; }
      .gl-extend button { font-size:10px; padding:1px 6px; }
      .gl-meta-txt { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                     font-variant-numeric:tabular-nums; }
      .gl-meta-txt.urgent { color:var(--cp-warn,#b45309); font-weight:700; }
      /* 达成信号提示（像是达成了→去标成交）：ok 绿系区分于推进/让路条 */
      .gl-outcome { display:flex; gap:6px; align-items:center;
                    border-left-color:var(--cp-ok,#0f9d75);
                    background:color-mix(in srgb,var(--cp-ok,#0f9d75) 8%,var(--cp-surface-2,#f8fafc)); }
      .gl-outcome-tx { flex:1 1 auto; min-width:0; font-size:var(--cp-fs-sm,12px);
                       color:var(--cp-text,#374151); line-height:1.5; }
      .gl-outcome button { flex:0 0 auto; font-size:11px; padding:2px 9px; }
      /* 买家信号（问价→趁热开限时目标）：sprint 琥珀系 */
      .gl-buysig { display:flex; gap:6px; align-items:center;
                   border-left-color:var(--cp-goal-sprint,#d97706);
                   background:color-mix(in srgb,var(--cp-goal-sprint,#d97706) 9%,var(--cp-surface-2,#f8fafc)); }
      .gl-buysig-tx { flex:1 1 auto; min-width:0; font-size:var(--cp-fs-sm,12px);
                      color:var(--cp-text,#374151); line-height:1.5; }
      .gl-buysig button.primary { flex:0 0 auto; font-size:11px; padding:2px 9px; }
      .gl-buysig-x { flex:0 0 auto; border:none; background:transparent; cursor:pointer;
                     color:var(--cp-text-tiny,#94a3b8); padding:2px 4px; font-size:12px; }
      .gl-buysig-x:hover { color:var(--cp-text,#1e293b); }
      /* 画像槽位陈旧（>90 天未更新）：虚线降饱和 + ⏳ */
      .gl-slotchk.stale { border-style:dashed; opacity:.78; }
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
      /* close 收口档（P3 2026-08-30）：琥珀系=冲刺视觉语言（与买家信号条同族） */
      .gl-pill.close { background:color-mix(in srgb,var(--cp-goal-sprint,#d97706) 18%,transparent);
                       color:var(--cp-goal-sprint,#b45309); cursor:help; }
      /* 冲刺调度状态行 + 立即推进（P3 2026-08-30） */
      .gl-sprint-line { display:flex; gap:6px; align-items:center; justify-content:space-between;
                        font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                        margin-top:2px; flex:1; min-width:0; }
      /* D1b：统一状态行。blocked=中性虚线；live=绿点。临期警示仍只在 meta urgent。 */
      .gl-status { display:flex; gap:6px; align-items:flex-start; margin-top:2px; }
      .gl-status-dot { flex:0 0 auto; width:6px; height:6px; margin-top:5px; border-radius:50%;
                       background:var(--cp-text-tiny,#94a3b8); }
      .gl-status.live .gl-status-dot { background:var(--cp-ok,#16a34a); }
      .gl-status.blocked { padding:2px 6px 3px; border:1px dashed var(--cp-border,#cbd5e1);
                           border-radius:6px; }
      .gl-status.blocked .gl-status-dot { background:var(--cp-text-tiny,#94a3b8); }
      /* M-7 D（#236）：stalled=红点红字（24h 有排期零真发）；idle=灰点（等待首拍，不冒充推进中） */
      .gl-status.stalled { padding:2px 6px 3px; border:1px solid color-mix(in srgb,var(--cp-danger,#dc2626) 45%,transparent);
                           border-radius:6px; color:var(--cp-danger,#dc2626); }
      .gl-status.stalled .gl-status-dot { background:var(--cp-danger,#dc2626); }
      .gl-status.idle .gl-status-dot { background:var(--cp-text-tiny,#94a3b8); }
      .gl-tag.warn { border-style:dashed; color:var(--cp-danger,#dc2626);
                     border-color:var(--cp-danger,#dc2626); }
      /* #166 引擎真相：auto 档但推进器不能真出手 → 标签打叉 + 卡上一行说清为什么
         （不再只在建目标表单里提一次；「自动推进 · 进行中」不能是空头承诺） */
      .gl-tag.off { border-style:dashed; color:var(--cp-warn,#b45309);
                    border-color:var(--cp-warn,#b45309); }
      .gl-engine-note { font-size:var(--cp-fs-tiny,11px); line-height:1.45; margin:2px 0 4px;
                        color:var(--cp-warn,#b45309); }
      /* O-3 D（#236）：诚实三计数行「注入 N 次 · 主动出手 N 次（已 H 小时）· 已采集 N/M」
         ——三个数各自口径，红字只在「过了首拍时刻仍零真发」（不冒充推进、也不喊错） */
      .gl-counts { display:flex; flex-wrap:wrap; gap:2px 6px; align-items:center; margin:2px 0 0;
                   font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); line-height:1.5; }
      .gl-counts .red { color:var(--cp-danger,#dc2626); font-weight:700; }
      .gl-counts .warn { color:var(--cp-warn,#b45309); }
      .gl-counts-sep { color:var(--cp-text-tiny,#94a3b8); }
      /* O-3 D：采集链不可用黄条（画像 AI 抽取未开 / 记忆抽取白名单空）——不让 0/4 静默 */
      .gl-capture-warn { display:flex; gap:6px; align-items:flex-start; margin:4px 0 0; padding:4px 8px;
                         border-radius:6px; font-size:var(--cp-fs-tiny,11px); line-height:1.45;
                         color:var(--cp-warn,#b45309);
                         border:1px solid color-mix(in srgb,var(--cp-warn,#d97706) 45%,transparent);
                         background:color-mix(in srgb,var(--cp-warn,#d97706) 10%,transparent); }
      .gl-capture-warn .gl-capture-why { display:block; }
      .gl-auto-card.off { opacity:.62; border-style:dashed; }
      .gl-auto-card.off.sel { opacity:.85; }
      .gl-sprint-line .gl-nudge { flex:0 0 auto; font-size:var(--cp-fs-tiny,11px); padding:1px 8px;
                        border-color:var(--cp-goal-sprint,#d97706); color:var(--cp-goal-sprint,#b45309);
                        background:color-mix(in srgb,var(--cp-goal-sprint,#d97706) 8%,transparent); }
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
      .gl-more-pop button.safe { color:var(--cp-text,#374151); }
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
      .gl-form select,.gl-form input,.gl-form textarea { width:100%; box-sizing:border-box; font:inherit;
                 font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:4px 7px; }
      .gl-form textarea { resize:vertical; min-height:54px; line-height:1.5; }
      .gl-form input[type="range"] { padding:0; border:none; background:transparent; }
      .gl-form input:focus,.gl-form textarea:focus,.gl-form select:focus {
        outline:none; border-color:var(--cp-accent,#4f46e5);
        box-shadow:0 0 0 2px var(--cp-accent-weak,rgba(30,140,242,.22)); }
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
      /* N-3 #241：敏感槽（收入 / 资产）锁标；「+ 自定义标签」虚线加号 chip */
      .gl-chip.sens { border-style:dotted; }
      .gl-chip.sens::before { content:"\\1F512\\FE0E "; font-size:10px; opacity:.75; }
      .gl-chip.add { border-style:dashed; color:var(--cp-text-dim,#64748b); background:transparent; }
      .gl-chip.add:hover { color:var(--cp-accent,#4f46e5); }
      .gl-sens-hint { margin-top:3px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      /* N-3 #241：摸底目标「标记达成」前的已采集字段核对清单 */
      .gl-wc { margin:4px 0 6px; padding:6px 8px; border-radius:8px; background:var(--cp-surface-2,#f8fafc);
               border:1px solid var(--cp-border,#e2e8f0); }
      .gl-wc-t { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:3px; }
      .gl-wc-row { display:flex; gap:6px; align-items:center; font-size:var(--cp-fs-sm,12px); line-height:1.6; }
      .gl-wc-row .gl-wc-mark { width:12px; color:var(--cp-text-tiny,#94a3b8); }
      .gl-wc-row.ok .gl-wc-mark { color:var(--cp-ok,#0f9d75); }
      .gl-wc-row .gl-wc-lab { color:var(--cp-text-dim,#64748b); }
      .gl-wc-row .gl-wc-val { color:var(--cp-text,#374151); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .gl-wc-row .gl-link { margin-left:auto; }
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
      /* ── 建目标向导 · 第一步：分组场景卡（类别色条 + 线稿图标）────────── */
      .gl-scenlist { display:flex; flex-direction:column; gap:4px; }
      .gl-grp { display:flex; align-items:center; gap:5px;
                font-size:10px; font-weight:700; letter-spacing:.4px;
                color:var(--cp-text-dim,#64748b); margin:7px 0 0;
                padding-top:6px; border-top:1px dashed var(--cp-border,#e2e8f0); }
      .gl-grp:first-child { margin-top:0; padding-top:0; border-top:none; }
      .gl-grp-dot { flex:0 0 auto; width:7px; height:7px; border-radius:99px;
                    background:var(--cp-border,#cbd5e1); }
      .gl-grp.gk-conversion .gl-grp-dot { background:var(--cp-goal-conv,#b45309); }
      .gl-grp.gk-relationship .gl-grp-dot { background:var(--cp-goal-rel,#e11d48); }
      .gl-grp.gk-engagement .gl-grp-dot { background:var(--cp-goal-eng,#0284c7); }
      .gl-grp.gk-discovery .gl-grp-dot { background:var(--cp-goal-disc,#0d9488); }
      .gl-grp-n { flex:0 0 auto; font-size:9px; font-weight:600; line-height:14px;
                  min-width:14px; text-align:center; padding:0 4px; border-radius:99px;
                  background:var(--cp-surface-2,#f1f5f9); color:var(--cp-text-tiny,#94a3b8); }
      /* 摸底组「从这里开始」引导（只出现在首组，微文案克制不抢卡片） */
      .gl-grp-hint { font-size:var(--cp-fs-tiny,11px); line-height:1.4;
                     color:var(--cp-goal-disc,#0d9488); margin:2px 0 1px;
                     display:flex; gap:4px; align-items:flex-start; }
      .gl-grp-hint::before { content:"\u2726"; flex:0 0 auto; opacity:.8; }
      /* 场景卡 30 天基准线（有样本才渲染；帮坐席选前建立合理预期） */
      .gl-scen-bench { font-size:10px; color:var(--cp-text-tiny,#94a3b8);
                       margin-top:2px; font-variant-numeric:tabular-nums; }
      /* 终局卡：达成金带 + 关键事实 chips + 完成通知状态行 */
      .gl-term-done { border-left:3px solid var(--cp-gold,#ca8a04);
                      border-radius:8px; padding:6px 8px; margin:0 -2px;
                      background:color-mix(in srgb,var(--cp-gold,#ca8a04) 7%,var(--cp-surface,#fff)); }
      .gl-done-facts { display:flex; flex-wrap:wrap; gap:4px; margin-top:5px; }
      .gl-done-chip { font-size:10px; line-height:17px; padding:0 7px;
                      border-radius:99px; font-variant-numeric:tabular-nums;
                      border:1px solid color-mix(in srgb,var(--cp-gold,#ca8a04) 45%,transparent);
                      color:var(--cp-warn,#b45309);
                      background:color-mix(in srgb,var(--cp-gold,#ca8a04) 10%,transparent); }
      .gl-done-pushed { border-color:color-mix(in srgb,var(--cp-ok,#0f9d75) 45%,transparent);
                        color:var(--cp-ok,#0f9d75);
                        background:color-mix(in srgb,var(--cp-ok,#0f9d75) 9%,transparent); }
      .gl-notify { display:flex; align-items:center; gap:6px; flex-wrap:wrap;
                   margin-top:6px; padding-top:5px;
                   border-top:1px dashed var(--cp-border,#e2e8f0); }
      .gl-notify-ok, .gl-notify-warn { font-size:10px; line-height:1.45; }
      .gl-notify-ok { color:var(--cp-text-tiny,#94a3b8); }
      .gl-notify-warn { color:var(--cp-warn,#b45309); }
      .gl-notify-link { font-size:10px; padding:0 8px; line-height:17px;
                        border-radius:99px; cursor:pointer;
                        border:1px solid var(--cp-warn,#b45309);
                        color:var(--cp-warn,#b45309); background:transparent; }
      .gl-notify-link:hover { background:color-mix(in srgb,var(--cp-warn,#b45309) 10%,transparent); }
      /* P3b：点名收件人计数 chip（hover 见名单） */
      .gl-ne-chip { font-size:10px; line-height:17px; color:var(--cp-text-dim,#64748b);
                    padding:0 6px; border:1px solid var(--cp-border,#e2e8f0);
                    border-radius:99px; cursor:default; }
      /* P3b：「标成交」＝收益动作，emerald 与通用主按钮区分（暂停=素、更多=链） */
      .gl-acts .gl-win { background:var(--cp-ok,#0f9d75); border-color:var(--cp-ok,#0f9d75); }
      .gl-acts .gl-win:hover { filter:brightness(1.07); }
      /* P3 自助绑定迷你表单（通知行内展开） */
      .gl-nb { flex-basis:100%; margin-top:4px; }
      .gl-nb-row { display:flex; gap:4px; margin-top:3px; }
      .gl-nb-row input { flex:1 1 auto; min-width:0; font:inherit;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#1e293b);
                 background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); padding:3px 7px; }
      .gl-nb-row button { flex:0 0 auto; font-size:10px; }
      .gl-nb-msg { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn,#b45309);
                   line-height:1.5; margin-top:3px; }
      .gl-nb-msg.ok { color:var(--cp-ok,#0f9d75); }
      .gl-scen { display:flex; gap:8px; align-items:flex-start;
                 border:1px solid var(--cp-border,#e2e8f0);
                 border-left:3px solid var(--cp-border,#e2e8f0);
                 border-radius:8px; padding:7px 9px;
                 cursor:pointer; background:var(--cp-surface,#fff); }
      .gl-scen:hover { box-shadow:0 1px 6px rgba(15,23,42,.10); }
      .gl-scen.sel { border-color:var(--cp-accent,#4f46e5);
                     background:var(--cp-accent-weak,rgba(79,70,229,.06));
                     box-shadow:0 0 0 1px var(--cp-accent,#4f46e5) inset; }
      /* 类别色条放在 .sel 之后：选中态也保留业务类别色 */
      .gl-scen.gk-conversion { border-left-color:var(--cp-goal-conv,#b45309); }
      .gl-scen.gk-relationship { border-left-color:var(--cp-goal-rel,#e11d48); }
      .gl-scen.gk-engagement { border-left-color:var(--cp-goal-eng,#0284c7); }
      .gl-scen.gk-discovery { border-left-color:var(--cp-goal-disc,#0d9488); }
      .gl-scen-ic { flex:0 0 auto; width:15px; height:15px; margin-top:1px;
                    color:var(--cp-text-dim,#64748b); }
      .gl-scen-ic svg { display:block; width:100%; height:100%; }
      .gk-conversion .gl-scen-ic { color:var(--cp-goal-conv,#b45309); }
      .gk-relationship .gl-scen-ic { color:var(--cp-goal-rel,#e11d48); }
      .gk-engagement .gl-scen-ic { color:var(--cp-goal-eng,#0284c7); }
      .gk-discovery .gl-scen-ic { color:var(--cp-goal-disc,#0d9488); }
      .gk-custom .gl-scen-ic,.gl-scen-custom .gl-scen-ic { color:var(--cp-violet,#7c3aed); }
      .gl-scen-mn { flex:1 1 auto; min-width:0; }
      .gl-scen-nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); display:flex; align-items:center;
                    gap:6px; flex-wrap:wrap; }
      .gl-scen-d { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                   line-height:1.45; margin-top:1px; }
      .gl-scen-rec { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px;
                     line-height:16px; background:var(--cp-accent,#4f46e5); color:#fff; }
      .gl-scen-last { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px;
                      line-height:15px; border:1px solid var(--cp-border,#e2e8f0);
                      color:var(--cp-text-tiny,#94a3b8); }
      /* 自定义目标＝独立高亮卡（violet 描边+浅底；比场景卡更醒目的进阶入口） */
      .gl-scen-custom { border-style:dashed;
                        border-color:color-mix(in srgb,var(--cp-violet,#7c3aed) 55%,var(--cp-border,#e2e8f0));
                        border-left-style:solid; border-left-width:3px;
                        border-left-color:var(--cp-violet,#7c3aed);
                        background:color-mix(in srgb,var(--cp-violet,#7c3aed) 6%,var(--cp-surface,#fff)); }
      .gl-scen-custom.sel { border-color:var(--cp-violet,#7c3aed);
                            box-shadow:0 0 0 1px var(--cp-violet,#7c3aed) inset;
                            background:color-mix(in srgb,var(--cp-violet,#7c3aed) 11%,var(--cp-surface,#fff)); }
      .gl-adv-pill { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px;
                     line-height:15px; border:1px solid var(--cp-violet,#7c3aed);
                     color:var(--cp-violet,#7c3aed);
                     background:color-mix(in srgb,var(--cp-violet,#7c3aed) 10%,transparent); }
      .gl-anote { color:var(--cp-warn,#b45309); }
      /* ── 建目标向导 · 第二步：场景设置弹层（fixed 居中，逃离窄栏约束）──── */
      .gl-ov { position:fixed; inset:0; z-index:9999;
               background:rgba(15,23,42,.48); display:flex; align-items:center;
               justify-content:center; padding:16px 10px; }
      .gl-modal { width:min(94vw,400px); max-height:min(86vh,660px);
                  display:flex; flex-direction:column; overflow:hidden;
                  background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b);
                  border:1px solid var(--cp-border,#e2e8f0); border-radius:14px;
                  box-shadow:0 24px 64px rgba(15,23,42,.30); }
      .gl-mband { flex:0 0 auto; height:4px; background:var(--cp-accent,#4f46e5); }
      .gl-modal.gk-conversion .gl-mband { background:var(--cp-goal-conv,#b45309); }
      .gl-modal.gk-relationship .gl-mband { background:var(--cp-goal-rel,#e11d48); }
      .gl-modal.gk-engagement .gl-mband { background:var(--cp-goal-eng,#0284c7); }
      .gl-modal.gk-discovery .gl-mband { background:var(--cp-goal-disc,#0d9488); }
      .gl-modal.gk-custom .gl-mband { background:var(--cp-violet,#7c3aed); }
      .gl-modal.sprint .gl-mband { background:var(--cp-goal-sprint,#d97706); }
      /* 摸底进度打勾清单 + 建目标槽位 chips */
      .gl-slots { margin-top:6px; }
      .gl-slots-hd { font-size:var(--cp-fs-tiny,11px); font-weight:600;
                     color:var(--cp-text-dim,#64748b); margin-bottom:4px; }
      .gl-slots-row { display:flex; flex-wrap:wrap; gap:4px; }
      .gl-slotchk { display:inline-flex; align-items:center; gap:3px;
                    font-size:10px; padding:1px 7px; border-radius:99px;
                    border:1px solid var(--cp-border,#e2e8f0);
                    color:var(--cp-text-tiny,#94a3b8); background:transparent; }
      .gl-slotchk.on { color:var(--cp-ok,#0f9d75); border-color:rgba(15,157,117,.45);
                       background:rgba(15,157,117,.08); }
      /* P3b：人工确认的槽位=实边+深底（可信度分层；auto/llm 维持浅描边） */
      .gl-slotchk.on.src-agent { border-color:var(--cp-ok,#0f9d75);
                                 background:rgba(15,157,117,.16); font-weight:600; }
      .gl-slotpicks { margin-top:4px; }
      .gl-disc-tip { margin-top:6px; padding:6px 8px; border-radius:8px;
                     background:rgba(13,148,136,.08);
                     border:1px solid rgba(13,148,136,.28);
                     font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                     line-height:1.45; }
      .gl-disc-tip .acts { margin-top:4px; }
      .gl-mhd { display:flex; gap:8px; align-items:flex-start; padding:12px 14px 2px; }
      .gl-mhd .gl-scen-ic { width:18px; height:18px; margin-top:1px; }
      .gl-mtitle { flex:1 1 auto; min-width:0; font-size:14px;
                   font-weight:var(--cp-fw-bold,700); color:var(--cp-text,#1e293b); }
      .gl-mclose { flex:0 0 auto; border:none; background:transparent; cursor:pointer;
                   font-size:15px; line-height:1; color:var(--cp-text-tiny,#94a3b8);
                   padding:2px 6px; border-radius:6px; }
      .gl-mclose:hover { color:var(--cp-text,#1e293b); background:var(--cp-surface-2,#f8fafc); }
      .gl-mdesc { padding:2px 14px 0; font-size:var(--cp-fs-sm,12px);
                  color:var(--cp-text-dim,#64748b); line-height:1.5; }
      .gl-mbody { padding:10px 14px 12px; overflow:auto; }
      .gl-mfoot { flex:0 0 auto; display:flex; gap:6px; justify-content:flex-end;
                  align-items:center; padding:9px 14px;
                  border-top:1px solid var(--cp-border,#e2e8f0);
                  background:var(--cp-surface-2,#f8fafc); }
      /* 背板误触反馈（gl-mfoot 左侧轻提示，DOM 级显隐不重渲染）；
         .dim＝P1 常驻「自动暂存中」灰字微反馈（警示态优先） */
      .gl-mhint { flex:1 1 auto; min-width:0; margin-right:auto; text-align:left;
                  font-size:var(--cp-fs-tiny,11px); line-height:1.4;
                  color:var(--cp-warn,#b45309); }
      .gl-mhint.dim { color:var(--cp-text-tiny,#94a3b8); }
      /* P1：窄屏（手机竖屏）弹层改底部抽屉——贴 dvh，软键盘弹出不顶飞输入框 */
      @media (max-width: 520px) {
        .gl-ov { align-items:flex-end; padding:0; }
        .gl-modal { width:100vw; max-width:100vw; border-radius:14px 14px 0 0;
                    max-height:92vh; max-height:92dvh; }
      }
      /* 草稿恢复提示条（mbody 顶部）+ 场景卡「有草稿」徽标 */
      .gl-mrestore { display:flex; gap:8px; align-items:center; justify-content:space-between;
                     padding:5px 9px; border-radius:8px; margin-bottom:2px;
                     font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                     background:color-mix(in srgb,var(--cp-ok,#0f9d75) 9%,var(--cp-surface,#fff));
                     border:1px solid color-mix(in srgb,var(--cp-ok,#0f9d75) 30%,transparent); }
      .gl-mrestore button { flex:0 0 auto; font-size:var(--cp-fs-tiny,11px); padding:2px 8px; }
      .gl-scen-draftb { flex:0 0 auto; font-size:10px; padding:0 6px; border-radius:99px;
                        line-height:16px; color:#fff; background:var(--cp-ok,#0f9d75); }
      .gl-msec-t { font-size:10px; font-weight:700; letter-spacing:.4px;
                   color:var(--cp-text-tiny,#94a3b8); margin-bottom:3px; }
      /* 节奏预览（AI 会怎么推进）：编号节点 + 里程碑名 + 推进力度 pill */
      .gl-arc { border:1px solid var(--cp-border,#e2e8f0); border-radius:10px;
                padding:7px 10px; background:var(--cp-surface-2,#f8fafc); }
      .gl-arc-row { display:flex; gap:7px; align-items:center; padding:2px 0;
                    font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151); }
      .gl-arc-idx { flex:0 0 auto; width:16px; height:16px; border-radius:50%;
                    background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b);
                    font-size:10px; line-height:16px; text-align:center; font-weight:700; }
      .gl-arc-nm { flex:1 1 auto; min-width:0; }
      /* AI 参与度三张单选卡（替代原生 select，选中态 accent 描边） */
      .gl-auto-cards { display:flex; flex-direction:row; flex-wrap:wrap; gap:4px; }
      .gl-auto-card { flex:1 1 88px; border:1px solid var(--cp-border,#e2e8f0); border-radius:8px;
                      padding:5px 7px; cursor:pointer; background:var(--cp-surface,#fff); }
      .gl-auto-card.sel { border-color:var(--cp-accent,#4f46e5);
                          background:var(--cp-accent-weak,rgba(79,70,229,.06));
                          box-shadow:0 0 0 1px var(--cp-accent,#4f46e5) inset; }
      .gl-auto-nm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); display:flex; align-items:center; gap:6px; }
      .gl-auto-d { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                   line-height:1.45; margin-top:1px; }
      /* 限时节奏分段（自然天 / 今天收口 / 这轮聊完） */
      .gl-pace { display:flex; gap:0; border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:8px; overflow:hidden; }
      .gl-pace button { flex:1; font:inherit; font-size:var(--cp-fs-tiny,11px);
                        padding:6px 4px; border:0; border-right:1px solid var(--cp-border,#e2e8f0);
                        background:var(--cp-surface,#fff); color:var(--cp-text-dim,#64748b);
                        cursor:pointer; }
      .gl-pace button:last-child { border-right:0; }
      .gl-pace button.on { background:var(--cp-accent-weak,rgba(79,70,229,.08));
                           color:var(--cp-accent,#4f46e5); font-weight:700; }
      .gl-modal.sprint .gl-pace button.on {
        background:color-mix(in srgb,var(--cp-goal-sprint,#d97706) 16%,transparent);
        color:var(--cp-goal-sprint,#d97706); }
      .gl-pace-hint { font-size:10px; color:var(--cp-text-tiny,#94a3b8);
                      line-height:1.4; margin-top:3px; }
      .gl-hz-chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:4px; }
      .gl-pace-line { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                      line-height:1.45; }
      /* 信息气泡（中性说明，替代橙色警告字——「未开主动触达」是状态不是错误） */
      .gl-note-info { display:flex; gap:6px; align-items:flex-start; padding:6px 9px;
                      border-radius:8px; font-size:var(--cp-fs-tiny,11px); line-height:1.5;
                      color:var(--cp-text-dim,#64748b);
                      background:var(--cp-accent-bg,#eef6fe);
                      border:1px solid color-mix(in srgb,var(--cp-accent,#1e8cf2) 26%,transparent); }
      .gl-note-info svg { flex:0 0 auto; width:13px; height:13px; margin-top:1px;
                          color:var(--cp-accent,#1e8cf2); }
      /* 亲密度滑杆 */
      .gl-range-row { display:flex; gap:8px; align-items:center; }
      .gl-range-row input { flex:1 1 auto; }
      .gl-range-val { flex:0 0 auto; min-width:26px; text-align:right;
                      font-size:var(--cp-fs-sm,12px); font-weight:700;
                      color:var(--cp-accent,#4f46e5); font-variant-numeric:tabular-nums; }
      .gl-range-scale { display:flex; justify-content:space-between; font-size:10px;
                        color:var(--cp-text-tiny,#94a3b8); margin-top:1px; }
      /* 示例 chips 行（点击填入 textarea） */
      .gl-exrow { display:flex; flex-wrap:wrap; gap:4px; margin-top:4px; }
      /* 「高级参数」折叠（自动继承字段收进来，别吓住新手） */
      details.gl-advp { border:1px dashed var(--cp-border,#e2e8f0); border-radius:8px;
                        padding:5px 8px; }
      details.gl-advp summary { cursor:pointer; font-size:var(--cp-fs-tiny,11px);
                                color:var(--cp-text-dim,#64748b); }
      .gl-advp-bd { display:flex; flex-direction:column; gap:6px; margin-top:6px; }
      .gl-params { display:flex; flex-direction:column; gap:6px; }
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
                    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      /* M-7 A（#236）：状态行里可点的「已推进 N 拍 / 今天被拦 N 次」+ 每一拍清单行 */
      .gl-beats-btn { background:transparent; border:none; padding:0; cursor:pointer; font:inherit;
                      color:inherit; text-decoration:underline dotted; text-underline-offset:2px; }
      .gl-beats-btn:hover { color:var(--cp-accent,#4f46e5); }
      .gl-beats-btn.warn { color:var(--cp-warn,#b45309); }
      .gl-beat { display:flex; gap:6px; align-items:baseline; font-size:var(--cp-fs-tiny,11px);
                 padding:3px 0; border-bottom:1px dashed var(--cp-border,#e2e8f0); flex-wrap:wrap; }
      .gl-beat:last-child { border-bottom:none; }
      .gl-beat-time { flex:0 0 auto; color:var(--cp-text-tiny,#94a3b8); font-variant-numeric:tabular-nums; }
      .gl-beat-kind { flex:0 0 auto; font-weight:600; color:var(--cp-text-dim,#64748b); }
      .gl-beat.kind-sent .gl-beat-kind { color:var(--cp-ok,#0f9d75); }
      .gl-beat.kind-blocked .gl-beat-kind,.gl-beat.st-failed .gl-beat-kind { color:var(--cp-warn,#b45309); }
      .gl-beat-st { flex:0 0 auto; padding:0 6px; border-radius:99px;
                    background:var(--cp-track,#e2e8f0); color:var(--cp-text-dim,#64748b); }
      .gl-beat.st-sent .gl-beat-st { background:color-mix(in srgb,var(--cp-ok,#0f9d75) 14%,transparent);
                                     color:var(--cp-ok,#0f9d75); }
      .gl-beat.st-failed .gl-beat-st,.gl-beat.st-expired .gl-beat-st,.gl-beat.st-blocked .gl-beat-st {
                    background:color-mix(in srgb,var(--cp-warn,#b45309) 14%,transparent);
                    color:var(--cp-warn,#b45309); }
      .gl-beat-phase { flex:0 0 auto; color:var(--cp-text-tiny,#94a3b8); font-family:ui-monospace,monospace; }
      .gl-beat-tx { flex:1 1 100%; min-width:0; color:var(--cp-text,#374151);
                    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .gl-beat-other { flex:0 0 auto; color:var(--cp-text-tiny,#94a3b8); font-style:italic; }
      .gl-beat-jump { flex:0 0 auto; background:transparent; border:none; padding:0; cursor:pointer;
                      font-size:inherit; color:var(--cp-accent,#4f46e5); text-decoration:underline; }
      /* M-7 C（#236）：到期结算摘要 + 「上一个目标已结算」行 */
      .gl-settle { margin:var(--cp-gap-xs,4px) 0; }
      .gl-settle-btn { background:transparent; border:none; cursor:pointer; padding:2px 0;
                       font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .gl-settle-btn:hover { color:var(--cp-accent,#4f46e5); }
      .gl-settle-line { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#374151); margin:2px 0; }
      .gl-settle-why { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn,#b45309); margin:0 0 4px;
                       padding-left:8px; border-left:2px solid var(--cp-warn,#b45309); }
      .gl-prev-settled { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                         margin:2px 0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }`;
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
        this._formStep = 1;
        this._formTid = "";
        this._formAutonomy = "";
        this._formPace = "natural";
        this._formUsePrefDays = true;
        this._resetParamState();
        this._createdHintAutonomy = "";
        this._createdHintTid = "";
        this._chainReco = null;
        this._profOpen = false;
        this._slotAsk = "";
        this._wonFormOpen = false;
        this._moreOpen = false;
        this._nbOpen = false;      // P3 自助绑定表单不跨会话残留
        this._nbMsg = "";
        this._clearUndo();
        this._prevMsIdx = -1;
        this._celebrateMs = false;
        this._exposed = false;
        this._progOpen = false;
        this._progGoalId = "";
        this._progRows = null;
        this._progLoading = false;
        this._progErr = false;
        this._settleOpen = "";
        // 草稿：内存态随会话复位；sessionStorage 里按 cid 各存各的，切回来还能续写
        this._formDraft = null;
        this._formBaseline = null;
        this._ctxDeferred = false;
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
        // 取数瞬时失败不打断编辑：表单开着就保表单（旧行为＝错误行顶掉表单，输入全丢）
        if (this._formOpen) return this._renderForm() + this._toastHtml();
        return `<div class="gl-errline" data-act="retry">${this.esc(this.t("inbox.goal.err_retry"))}</div>`;
      }
      const g = d.goal || null;
      if (g && !TERMINAL[g.status]) {
        // 编辑中冒出进行中目标（AI 自建 P22 / 另一窗口刚建）：不再静默强关表单——
        // 保留编辑态，坐席若坚持创建由后端 active_limit 如实 409、错误就地显示。
        if (this._formOpen) {
          this._emitLoadedSignal(g);
          return this._renderForm() + this._toastHtml();
        }
        // #111（0831 skuio「点标成交没反应」实锤）：这里**不得**复位成交表单标志
        // ——点「标成交」的 onAction 恰是「置位 → _rerender → 本函数」，旧的无条件
        // 复位把表单在渲染前掐灭＝按钮点了永远没反应（真浏览器门禁 S22 钉住）。
        // 会话切换的复位已由 set context 的重置块负责，此处不需要第二份。
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
      return this._renderEmpty(last, d.signal_hint) + this._toastHtml();
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
          intentDisplay: intentDisp(beat),
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
      // M-7 C（#236）：上一个目标 7 天内到期 → 「已到期 · 查看结算」跟在结果后面
      return `<div class="gl-last"><span>${this.esc(this.t("inbox.goal.last_label"))}</span>` +
        `<span>${this.esc(last.title || last.template_name || "")}</span>${this._statusBadge(last.status)}</div>` +
        this._settlementHtml(last);
    }

    _emptyIllust() {
      // 轻量旗帜线稿（空态视觉锚点）
      return `<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true"><path d="M4 22V4"/><path d="M4 4h11l-1.6 3.2L15 10.5H4"/>
        <circle cx="18" cy="6" r="2.2"/></svg>`;
    }

    _sigDismissed(sig) {
      try {
        const k = "cp_goal_sig_dismiss:" + this._draftCid();
        const v = parseFloat(root.sessionStorage
          && root.sessionStorage.getItem(k));
        return isFinite(v) && v >= parseFloat((sig && sig.ts) || 0);
      } catch (_e) { return false; }
    }

    _renderEmpty(last, sig) {
      const esc = (s) => this.esc(s);
      // 买家信号（P1 2026-08-29）：无目标 + 近窗在问价/要购买方式 → 趁热
      // 直达「今天收口」限时表单。只提示不动作；✕=本会话本条信号内不再提。
      let sigBar = "";
      if (sig && sig.v && String(sig.kind || "buying") === "buying"
          && !this._sigDismissed(sig)) {
        sigBar = `<div class="gl-sec gl-buysig">` +
          `<span class="gl-buysig-tx">\uD83D\uDD25 ${esc(this.t("inbox.goal.signal.buying", { v: String(sig.v) }))}</span>` +
          `<button type="button" class="primary" data-act="open_form_sprint">${esc(this.t("inbox.goal.signal.open_btn"))}</button>` +
          `<button type="button" class="gl-buysig-x" data-act="signal_dismiss" data-ts="${esc(String(sig.ts || 0))}"` +
          ` title="${esc(this.t("inbox.goal.signal.dismiss"))}" aria-label="${esc(this.t("inbox.goal.signal.dismiss"))}">\u2715</button></div>`;
      }
      // 上一个目标疑似达成没人点（P2/P3 2026-08-30）：空态也给补录入口——
      // 终局卡只在刚过期那一轮可见，之后都停在这个空态
      const lastRevive = this._reviveBarHtml(last);
      return sigBar + `<div class="gl-empty">${this._emptyIllust()}` +
        `<div class="gl-lead">${esc(this.t("inbox.goal.empty_lead"))}</div>` +
        `<div class="gl-alias">${esc(this.t("inbox.goal.alias_note"))}</div>` +
        `<div class="acts" style="justify-content:center">` +
        `<button class="primary" data-act="open_form">${esc(this.t("inbox.goal.set_btn"))}</button></div></div>` +
        lastRevive + this._lastLine(last);
    }

    _renderActive(g) {
      const esc = (s) => this.esc(s);
      const lvl = String(g.autonomy || "suggest");
      // #166：auto 档但引擎在本目标节奏/平台下不能真出手 → 标签打叉（虚线警示色）
      // + 提示语点名原因；卡身另起一行说明（见 engineNote），不再只靠 hover
      const gpace = String(g.pace || "natural");
      const engA = (lvl === "auto" && g.status === "active")
        ? this._autoEngine(gpace, (this._d && this._d.engine) || null) : null;
      const engOff = !!(engA && !engA.on);
      const engWhy = engOff ? this._engineWhy(engA.blockers) : "";
      // M-7 D（#236）：⚠ 必须说清是什么——引擎层没生效（engOff）之外，运行时闸拦住
      // （auto_state=blocked）与单目标 stalled（24h 有排期零真发）也打 ⚠，tooltip 带原因；
      // 卡身状态行（sprintLine）同源同文案，不再出现「自动推进⚠」却不说 ⚠ 是什么。
      const liveA = (lvl === "auto" && g.status === "active"
        && g.sprint_live && typeof g.sprint_live === "object") ? g.sprint_live : null;
      const aState = liveA ? String(liveA.auto_state || "") : "";
      const rtWarn = !!(liveA && !engOff && (aState === "blocked" || aState === "stalled"));
      let hint;
      if (engOff) {
        hint = this.t("inbox.goal.autonomy.auto_ineffective_t", { why: engWhy });
      } else if (rtWarn && aState === "stalled") {
        const lb = liveA.last_block && liveA.last_block.reason ? this._blockedLabel(liveA.last_block.reason) : "";
        hint = this.t("inbox.goal.auto.tag_stalled_t", { why: lb || this.t("inbox.goal.blocked.unknown") });
      } else if (rtWarn) {
        hint = this.t("inbox.goal.auto.tag_blocked_t", { why: this._engineWhy(liveA.blockers) });
      } else {
        hint = this._autonomyHint(lvl) || this.t("inbox.goal.autonomy_cycle_t");
      }
      const tagWarn = engOff || rtWarn;
      const tag = `<button type="button" class="gl-tag${engOff ? " off" : ""}${rtWarn ? " warn" : ""}" data-act="autonomy_cycle"` +
        ` title="${esc(hint)}">${esc(this._autonomyLabel(lvl))}${tagWarn ? " \u26A0" : ""}</button>`;
      const origin = AUTO_ORIGIN[String(g.created_by || "")]
        ? `<span class="gl-origin" title="${esc(this.t("inbox.goal.origin.auto_t"))}">` +
          `${esc(this.t("inbox.goal.origin.auto"))}</span>`
        : "";

      const ms = Array.isArray(g.milestones) ? g.milestones : [];
      const mi = parseInt(g.milestone_idx, 10) || 0;
      // P3b 诚实轨道：摸底类按槽位填充率倒推「信号能到的里程碑」（与 ledger
      // 同阈值 0.5→2 / 0.8→3；首格=对方开口属信号，放行），超出的段=时间兑底
      // 走到的 → 虚纹呈现——坐席不再被「按时走格子」的进度骗（28 失守全停
      // idx=4 曾被误读成跑完弧线，同一教训的卡面版）。非摸底模板无判据不标。
      let sigMi = -1;
      const spRows = Array.isArray(g.slots_progress) ? g.slots_progress : [];
      if (spRows.length) {
        const fillR = spRows.filter((s) => s && s.filled).length / spRows.length;
        sigMi = fillR >= 0.8 ? 3 : (fillR >= 0.5 ? 2 : 1);
      }
      let track = "";
      let msCur = "";
      if (ms.length) {
        track = `<div class="gl-track-wrap"><div class="gl-track">` + ms.map((m, i) => {
          let cls = i < mi ? "gl-seg done" : (i === mi ? "gl-seg cur" : "gl-seg");
          // 该段代表的「已达位置」：done 段=越过 i 到了 i+1；cur 段=正处 mi
          const reached = i < mi ? i + 1 : (i === mi ? mi : -1);
          const timeDriven = sigMi >= 0 && reached > sigMi;
          if (timeDriven) cls += " time";
          if (i === mi && this._celebrateMs) cls += " celebrate";
          const nm = LANG === "en" ? (m.en || m.zh || "") : (m.zh || m.en || "");
          const tip = timeDriven
            ? nm + " · " + this.t("inbox.goal.ms_time_t") : nm;
          return `<div class="${cls}" title="${esc(tip)}"></div>`;
        }).join("") + `</div>`;
        const cur = ms[Math.min(mi, ms.length - 1)] || {};
        const curNm = LANG === "en" ? (cur.en || cur.zh || "") : (cur.zh || cur.en || "");
        if (curNm) {
          msCur = `<div class="gl-ms-cur">${esc(this.t("inbox.goal.ms_current", { name: curNm }))}</div>`;
        }
        track += msCur + `</div>`;
      }

      const pct = Math.max(0, Math.min(100, Math.round((parseFloat(g.progress) || 0) * 100)));
      // 「第X/Y天」即改期限入口（P25）；换了会话/目标自动收起旧表单
      if (this._deadlineOpen && this._deadlineForGid !== String(g.goal_id || "")) {
        this._deadlineOpen = false;
        this._deadlineVal = null;
      }
      const paceNow = String(g.pace || "natural");
      const isSprint = paceNow === "today" || paceNow === "session";
      let dayTxt;
      let urgent;
      if (isSprint) {
        const rem = (g.remaining_sec != null)
          ? parseFloat(g.remaining_sec)
          : Math.max(0, (parseFloat(g.deadline_ts) || 0) - (Date.now() / 1000));
        const tot = parseFloat(g.total_sec) || 0;
        dayTxt = this.t("inbox.goal.remaining", { t: this._fmtRemain(rem) });
        urgent = String(g.status) === "active" && tot > 0
          && ((tot - rem) / tot) >= 0.8 && pct < 50;
      } else {
        dayTxt = this.t("inbox.goal.day_of", { day: g.day_index || 1, total: g.total_days || 0 });
        const totD = parseInt(g.total_days, 10) || 0;
        urgent = String(g.status) === "active" && totD > 0
          && (parseInt(g.day_index, 10) || 1) / totD >= 0.8 && pct < 50;
      }
      const ext = (isSprint && g.status === "active")
        ? `<span class="gl-extend">` +
          `<button type="button" data-act="extend_30m">${esc(this.t("inbox.goal.extend_30m"))}</button>` +
          `<button type="button" data-act="extend_2h">${esc(this.t("inbox.goal.extend_2h"))}</button></span>`
        : "";
      // #166 进度口径标注：自定义/未知模板自然档的 progress 只跟日历爬（ledger else
      // 分支），「89%」是**时间**不是推进——按 beats.progress_kind 标「时间 89%」，并
      // 并排给真出过手的拍数「动作 N/M 拍」（N=consumed/sent 且力度非 none，M=限时
      // 档封顶 / 自然档总天数）。旧后端无 beats ＝维持原样。
      const bt = (g.beats && typeof g.beats === "object") ? g.beats : null;
      const pctTxt = (bt && bt.progress_kind === "time")
        ? this.t("inbox.goal.meta.time_pct", { pct })
        : `${pct}%`;
      const beatsTxt = (bt && (parseInt(bt.cap, 10) || 0) > 0)
        ? ` · ${this.t("inbox.goal.meta.beats", { n: parseInt(bt.used, 10) || 0, m: parseInt(bt.cap, 10) || 0 })}`
        : "";
      /* 限时节奏用倒计时 + 顺延按钮，不走「改成天数」表单（分钟级期限填成整数天会立刻过期）。
         倒计时活字（P3 2026-08-30）：sp_remain 由 30s tick 原位刷新，不整卡重渲染。 */
      const metaInner = isSprint
        ? `<span class="gl-meta-txt${urgent ? " urgent" : ""}">${urgent ? "\u23F3 " : ""}` +
          `<span data-ref="sp_remain" data-dl="${esc(String(parseFloat(g.deadline_ts) || 0))}">${esc(dayTxt)}</span>` +
          ` · ${esc(pctTxt)}${esc(beatsTxt)}</span>${ext}`
        : `<button type="button" class="gl-meta-btn${urgent ? " urgent" : ""}" data-act="deadline_toggle"` +
          ` title="${esc(this.t(urgent ? "inbox.goal.meta.urgent_t" : "inbox.goal.deadline.edit_t"))}">` +
          `${urgent ? "\u23F3 " : ""}${esc(dayTxt)} · ${esc(pctTxt)}${esc(beatsTxt)}` +
          `<span class="gl-meta-pen" aria-hidden="true">\u270E</span></button>`;
      // 调度状态行（P3 2026-08-30；D1b 自然档也走 sprint_live）：
      // 「已推进几拍 · 下一次跟进几点 / 引擎没开只等对方开口」+ 冲刺手动加速。
      let sprintLine = "";
      const live = (g.status === "active"
        && g.sprint_live && typeof g.sprint_live === "object")
        ? g.sprint_live : null;
      if (live) {
        const bits = [];
        // M-7 A（#236）：N 只数「主动真发」（后端 sprint_live.beats_used 读 beat_sent
        // 事件，与看门狗同口径）；可点开每一拍。顺势带入回复次数与「今天被拦」并排
        // 说清——「已推进 2 拍」到底是两条消息还是两次顺势带入，不再让人猜。
        const bn = parseInt(live.beats_used, 10) || 0;
        const tr = (live.trace && typeof live.trace === "object") ? live.trace : null;
        if (bn > 0) {
          bits.push(`<button type="button" class="gl-beats-btn" data-act="prog_toggle" ` +
            `title="${esc(this.t("inbox.goal.sprint.beats_t"))}">` +
            `${esc(this.t("inbox.goal.sprint.beats", { n: bn }))}</button>`);
        }
        if (tr) {
          const inj = parseInt(tr.injected, 10) || 0;
          if (inj > 0) bits.push(esc(this.t("inbox.goal.sprint.injected", { n: inj })));
          const td = (tr.today && typeof tr.today === "object") ? tr.today : {};
          const bl = parseInt(td.blocked, 10) || 0;
          if (bl > 0) {
            const why = this._blockedWhy(td.blocked_by);
            bits.push(`<button type="button" class="gl-beats-btn warn" data-act="prog_toggle">` +
              `${esc(this.t("inbox.goal.sprint.blocked_today", { n: bl, why }))}</button>`);
          }
        }
        if (!live.ticker_on) {
          // #166：sprint 开着却被派发终点（care 关闸/dry_run）或平台白名单拦住 →
          // 点名原因，不再一律说「引擎未开启」（说错原因＝运维去翻错开关）
          const blk = Array.isArray(live.blockers) ? live.blockers : [];
          bits.push(esc((live.ticker_enabled && blk.length)
            ? this.t("inbox.goal.sprint.engine_blocked", { why: this._engineWhy(blk) })
            : this.t("inbox.goal.sprint.engine_off")));
        } else if (Array.isArray(live.blockers) && live.blockers.length) {
          // D1b P0-4：引擎配置全绿但**运行时闸**没过（会话人审档 / 危机窗 / opt-out /
          // 读不到收件箱）→ 点名原因。此前落到「等对方开口」——坐席以为在等客户，
          // 其实推进器一条都不会排。
          bits.push(esc(this.t("inbox.goal.sprint.engine_blocked", { why: this._engineWhy(live.blockers) })));
        } else if (String(g.autonomy || "") === "auto") {
          // M-7 D（#236）：「自动推进中」只在近 24h 真发 ≥1（active）或 24h 内有排期
          // （waiting_first 带预计时刻）时说；其余按后端 auto_state 三选一说真相——
          // 今日已达上限明日几点 / 等待首拍 / 被 X 拦住。旧文案「等对方回复中；到点未回
          // 会主动出手」在 BABY BEAR 上挂了 4 天，一拍没发。
          const nts = parseFloat(live.next_phase_ts) || 0;
          const hhmm = (ts) => {
            const nd = new Date(ts * 1000);
            return ("0" + nd.getHours()).slice(-2) + ":" + ("0" + nd.getMinutes()).slice(-2);
          };
          const lbWhy = (live.last_block && live.last_block.reason)
            ? this._blockedLabel(live.last_block.reason) : "";
          const st = String(live.auto_state || "");
          if (st === "active") {
            bits.push(esc(this.t("inbox.goal.auto.active", { n: parseInt(live.sent_24h, 10) || 0 })));
            if (nts > Date.now() / 1000) bits.push(esc(this.t("inbox.goal.sprint.next_beat", { t: hhmm(nts) })));
          } else if (st === "cap_reached") {
            bits.push(esc(this.t("inbox.goal.auto.cap_reached", { t: nts > 0 ? hhmm(nts) : "--:--" })));
          } else if (st === "stalled") {
            bits.push(esc(this.t("inbox.goal.auto.stalled", {
              why: lbWhy ? this.t("inbox.goal.auto.stalled_why", { why: lbWhy }) : "" })));
          } else if (st === "blocked_recent") {
            bits.push(esc(this.t("inbox.goal.auto.blocked_recent", { why: lbWhy || this.t("inbox.goal.blocked.unknown") })));
          } else if (nts > Date.now() / 1000 && nts <= Date.now() / 1000 + 86400) {
            bits.push(esc(this.t("inbox.goal.auto.waiting_first", { t: hhmm(nts) })));
          } else {
            bits.push(esc(this.t("inbox.goal.auto.waiting_first_nt")));
          }
        }
        const nudge = live.nudgeable
          ? `<button type="button" class="gl-nudge" data-act="sprint_nudge">` +
            `${esc(this.t("inbox.goal.sprint.nudge_btn"))}</button>`
          : "";
        const aSt = String(live.auto_state || "");
        const stCls = (!live.ticker_on || (Array.isArray(live.blockers) && live.blockers.length)
          || aSt === "cap_reached" || aSt === "blocked_recent")
          ? "blocked" : (aSt === "stalled" ? "stalled" : (aSt === "active" ? "live" : "idle"));
        if (bits.length || nudge) {
          sprintLine = `<div class="gl-status ${stCls}"><span class="gl-status-dot" aria-hidden="true"></span>` +
            `<div class="gl-sprint-line"><span>${bits.join(" · ")}</span>${nudge}</div></div>`;
        }
      }
      // O-3 D（#236 HM7XBA）：诚实三计数「注入 N 次 · 主动出手 N 次（已 H 小时）· 已采集 N/M」
      // ——注入＝每稿；主动出手＝beat_sent（与看门狗同口径，含手动摸底）；已采集＝打勾清单。
      // 「主动出手 0 次」红字只在后端 zero_send_alarm（过了首拍时刻仍零真发、期限未到）；
      // 还没到点时 tooltip 写「最早 HH:MM 出手」，不跟 watchdog 一样对自然档喊错。
      let countsLine = "";
      const cnt = (live && live.counts && typeof live.counts === "object") ? live.counts : null;
      if (cnt && g.status === "active") {
        const fmtHM = (ts) => {
          const nd = new Date(ts * 1000);
          const today = new Date();
          const hm = ("0" + nd.getHours()).slice(-2) + ":" + ("0" + nd.getMinutes()).slice(-2);
          return nd.toDateString() === today.toDateString()
            ? hm : `${nd.getMonth() + 1}/${nd.getDate()} ${hm}`;
        };
        const inj = parseInt(cnt.injected, 10) || 0;
        const sentN = parseInt(cnt.sent, 10) || 0;
        const zh = parseFloat(cnt.zero_send_hours) || 0;
        const alarm = !!cnt.zero_send_alarm;
        const fe = parseFloat(cnt.first_eligible_ts) || 0;
        const parts = [];
        parts.push(`<span title="${esc(this.t("inbox.goal.counts.injected_t"))}">` +
          `${esc(this.t("inbox.goal.counts.injected", { n: inj }))}</span>`);
        const sentTxt = (sentN === 0 && zh >= 1)
          ? this.t("inbox.goal.counts.sent_hours", { n: sentN, h: Math.round(zh) })
          : this.t("inbox.goal.counts.sent", { n: sentN });
        const sentTip = alarm
          ? this.t("inbox.goal.counts.alarm_t")
          : ((sentN === 0 && fe > Date.now() / 1000)
            ? this.t("inbox.goal.counts.first_eligible_t", { t: fmtHM(fe) })
            : this.t("inbox.goal.counts.sent_t"));
        parts.push(`<span class="${alarm ? "red" : ""}" data-ref="cnt_sent" title="${esc(sentTip)}">` +
          `${alarm ? "\u26A0 " : ""}${esc(sentTxt)}</span>`);
        const spc = Array.isArray(g.slots_progress) ? g.slots_progress : [];
        if (spc.length) {
          const filledC = spc.filter((s) => s && s.filled).length;
          parts.push(`<span title="${esc(this.t("inbox.goal.counts.captured_t"))}">` +
            `${esc(this.t("inbox.goal.counts.captured", { n: filledC, m: spc.length }))}</span>`);
        }
        const pa = parseInt(cnt.probe_asked, 10) || 0;
        const pm = parseInt(cnt.probe_missed, 10) || 0;
        if (pa || pm) {
          parts.push(`<span class="${pm > pa ? "warn" : ""}" title="${esc(this.t("inbox.goal.counts.probe_t"))}">` +
            `${esc(this.t("inbox.goal.counts.probe", { a: pa, m: pm }))}</span>`);
        }
        countsLine = `<div class="gl-counts">${parts.join('<span class="gl-counts-sep">\u00b7</span>')}</div>`;
      }
      // #166：auto 档而引擎不能真出手 → 卡身一行说清（sprintLine 已点名 ticker
      // 关闸时不重复；运行时闸由 sprintLine 的 engine_blocked 覆盖）
      const engineNote = (engOff && !(live && !live.ticker_on))
        ? `<div class="gl-engine-note">\u26A0 ${esc(this.t("inbox.goal.engine.card_off", { why: engWhy }))}</div>`
        : "";
      // M-5 A（#217）：用户版存量「转化成交」目标——模板已下线（新建入口已收起），
      // 目标不删，卡上说清建议改自定义目标；后端缺键（partner / internal）零渲染
      const retiredNote = g.template_retired
        ? `<div class="gl-engine-note">\u26A0 ${esc(this.t("inbox.goal.template_retired"))}</div>`
        : "";
      // M-7 C（#236）：新目标刚建、上一个同会话目标 7 天内刚到期 → 一行「上一个目标
      // 已结算：出手 X 拍，原因」（用户重建后卡片回「起步」，以为引擎被重置）
      const prevSettled = this._prevSettledHtml();
      const meta = `<div class="gl-meta">${metaInner}</div>` + sprintLine + countsLine + engineNote + retiredNote + prevSettled +
        (isSprint ? "" : (this._deadlineOpen ? this._renderDeadlineForm(g) : this._renderDueRow(g)));
      if (isSprint && g.status === "active") this._armSprintTick();

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
              `<button data-act="beat_adopt" title="${esc(this.t("inbox.goal.act.adopt_only_t"))}">\uD83D\uDC4D</button>` +
              `<button data-act="beat_reject" title="${esc(this.t("inbox.goal.act.reject_t"))}">\uD83D\uDC4E</button>` +
              `<button class="primary gl-draft-btn" data-act="beat_adopt_draft" title="${esc(this.t("inbox.goal.act.adopt_draft_t"))}">` +
              `${esc(this.t("inbox.goal.act.adopt_draft"))}</button></span>`;
        }
        const pushTip = pl === "none" ? this.t("inbox.goal.push.none_tip") : "";
        today = `<div class="gl-sec ${secCls}"><div class="gl-today">` +
          `<span class="gl-pill ${pl}"${pushTip ? ` title="${esc(pushTip)}"` : ""}>` +
          `${esc(this.t("inbox.goal.push." + pl))}</span>` +
          `<span class="gl-intent">${esc(intentDisp(beat))}</span>${fb}</div></div>`;
      } else if (HOLDS.indexOf(String(g.hold)) >= 0) {
        today = `<div class="gl-sec hold">${esc(this.t("inbox.goal.hold." + String(g.hold)))}</div>`;
      }

      // 达成信号提示（P1 2026-08-29）：后端检出高置信信号（如对方留了联系
      // 方式）→ 绿条 +「去标成交」。只提示不自动结算，确认权在坐席。
      let outcome = "";
      const osig = (g.params && typeof g.params === "object")
        ? g.params.outcome_signal : null;
      if (osig && osig.v && String(osig.kind || "contact") === "contact"
          && g.status === "active" && !this._wonFormOpen) {
        outcome = `<div class="gl-sec gl-outcome">` +
          `<span class="gl-outcome-tx">\uD83C\uDFAF ${esc(this.t("inbox.goal.outcome.contact", { v: String(osig.v) }))}</span>` +
          `<button type="button" class="primary" data-act="won">${esc(this._tDomain("inbox.goal.outcome.confirm"))}</button></div>`;
      }

      if (this._wonFormOpen) {
        return `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
          `${this._statusBadge(g.status)}</div>` +
          `<div class="gl-form"><div class="gl-lead">${esc(this._tDomain("inbox.goal.won_meta_title"))}</div>` +
          this._wonChecklistHtml(g) + this._wonFieldsHtml() +
          `<div class="acts gl-acts"><button data-act="won_cancel">${esc(this.t("cp.common.cancel"))}</button>` +
          `<button class="primary" data-act="won_confirm">${esc(this._tDomain("inbox.goal.won_meta_confirm"))}</button></div></div>`;
      }

      const paused = g.status === "paused";
      const flip = paused
        ? `<button data-act="resume">${esc(this.t("inbox.goal.act.resume"))}</button>`
        : `<button data-act="pause">${esc(this.t("inbox.goal.act.pause"))}</button>`;
      const more = `<span class="gl-more">` +
        `<button class="gl-link" data-act="more_toggle">${esc(this.t("inbox.goal.more_menu"))} \u22EF</button>` +
        (this._moreOpen
          ? `<div class="gl-more-pop">` +
            (isSprint ? "" : `<button class="safe" data-act="deadline_toggle">${esc(this.t("inbox.goal.deadline.title"))}</button>`) +
            `<button data-act="redirect">${esc(this.t("inbox.goal.act.redirect"))}</button>` +
            `<button data-act="cancel">${esc(this.t("inbox.goal.act.cancel"))}</button></div>`
          : "") +
        `</span>`;
      const acts = `<div class="acts gl-acts">${flip}${more}` +
        `<button class="primary gl-win" data-act="won">${esc(this._tDomain("inbox.goal.act.won"))}</button></div>`;

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
        `${this._statusBadge(g.status)}${origin}${tag}</div>` + createdHint + track + meta + outcome + today +
        this._renderSlotsProgress(g) + this._renderProgress() + this._renderProfile() +
        this._renderProducts(g) + acts + this._notifyRowHtml();
    }

    /* P28：摸底类目标 params.slots → 打勾清单（API slots_progress）。
       客户答了职业下轮刷新即打勾；缺清单=非摸底目标，零渲染。 */
    _renderSlotsProgress(g) {
      const rows = (g && Array.isArray(g.slots_progress)) ? g.slots_progress : [];
      if (!rows.length) return "";
      const esc = (s) => this.esc(s);
      const filled = rows.filter((s) => s && s.filled).length;
      const chips = rows.map((s) => {
        const on = !!s.filled;
        // P3b 来源标注：auto 正则 / llm 抽取 / agent 人工——「AI 猜的还是人
        // 核实的」进 tooltip；人工确认档加实心边框（handoff 惯例：可信度可见）
        const src = (on && /^(auto|llm|agent)$/.test(String(s.src || "")))
          ? String(s.src) : "";
        const srcLab = src ? this.t("inbox.goal.slots.src." + src) : "";
        // P1 时效感知：值超 90 天未更新 → ⏳ 提醒顺口再确认（不是判错，是提醒）
        const stale = !!(on && s.stale);
        const tip = on
          ? (String(s.label || s.key) + (s.value ? ": " + s.value : "")
             + (srcLab ? " \u00b7 " + srcLab : "")
             + (stale ? " \u00b7 " + this.t("inbox.goal.slots.stale_t") : ""))
          : this.t("inbox.goal.slots.miss_t", { label: s.label || s.key });
        return `<span class="gl-slotchk${on ? " on" : ""}${src ? " src-" + src : ""}${stale ? " stale" : ""}" title="${esc(tip)}">` +
          `${on ? "\u2713" : "\u25CB"} ${esc(s.label || s.key)}` +
          (on && s.value ? `\u00b7${esc(s.value)}` : "") +
          (stale ? "\u23F3" : "") + `</span>`;
      }).join("");
      // O-3 D（#236）：采集链不可用 → 黄条把原因说全（画像 AI 抽取未开 / 记忆抽取白名单空），
      // 不再让 0/4 静默得像「客户没说」。后端缺 capture 键（旧后端）＝不渲染。
      let captureWarn = "";
      const cap = (g.capture && typeof g.capture === "object") ? g.capture : null;
      if (cap && cap.ok === false) {
        const reasons = Array.isArray(cap.reasons) ? cap.reasons : [];
        const why = reasons
          .map((r) => this.t("inbox.goal.capture." + String(r)))
          .filter((s) => s && !/^inbox\.goal\.capture\./.test(s));
        if (why.length) {
          captureWarn = `<div class="gl-capture-warn" role="note">` +
            `<span aria-hidden="true">\u26A0</span><div>` +
            `<strong>${esc(this.t("inbox.goal.capture.unavailable"))}</strong>` +
            why.map((w) => `<span class="gl-capture-why">${esc(w)}</span>`).join("") +
            `</div></div>`;
        }
      }
      return `<div class="gl-sec gl-slots"><div class="gl-slots-hd">` +
        `${esc(this.t("inbox.goal.slots.title"))} · ${filled}/${rows.length}</div>` +
        `<div class="gl-slots-row">${chips}</div>${captureWarn}</div>`;
    }

    /* ── 调整期限（改节奏）内联表单（P25）── */

    _renderDeadlineForm(g) {
      const esc = (s) => this.esc(s);
      const minD = Math.max(1, parseInt(g.day_index, 10) || 1);
      const cur = Math.max(minD, parseInt(g.total_days, 10) || minD);
      const raw = (this._deadlineVal == null || this._deadlineVal === "") ? cur : this._deadlineVal;
      const val = parseInt(raw, 10);
      const mk = (days, label, tip, on) =>
        `<button type="button" class="gl-chip${on ? " on" : ""}" data-act="deadline_chip"` +
        ` data-days="${days}"${tip ? ` title="${esc(tip)}"` : ""}>${esc(label)}</button>`;
      const chips = [mk(minD, this.t("inbox.goal.deadline.chip_today"),
        this.t("inbox.goal.deadline.chip_today_t"), val === minD)];
      [3, 7, 14].forEach((d) => {
        if (d <= minD) return;          // 低于下限的预设不出现（出现即误导）
        chips.push(mk(d, this.t("inbox.goal.deadline.chip_days", { n: d }), "", val === d));
      });
      const bench = this._benchLine(g);
      return `<div class="gl-form gl-dl">` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.deadline.days_label"))}</label>` +
        `<div class="gl-dl-row">` +
        `<input type="number" min="${minD}" max="180" step="1" data-ref="dl_days"` +
        ` data-chg="dl_days" value="${esc(String(isFinite(val) ? val : cur))}">` +
        `<span class="gl-dl-chips">${chips.join("")}</span></div>` +
        `<div class="gl-hint" data-ref="dl_hint">${esc(this._deadlineHint(g, raw))}</div>` +
        (bench ? `<div class="gl-hint gl-dl-bench">${esc(bench)}</div>` : "") +
        `<div class="gl-ferr" data-ref="dl_err" hidden></div></div>` +
        `<div class="acts gl-acts"><button data-act="deadline_cancel">${esc(this.t("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="deadline_save">${esc(this.t("inbox.goal.deadline.save"))}</button></div>` +
        `</div>`;
    }

    /* 临期决策行（P1）：最后一天/倒数第二天把「快到期」从静默变成显式选择。
       只在 active 且期限表单未展开时出现（表单里已有同款控件，不重复）。 */
    _renderDueRow(g) {
      if (String(g.status) !== "active") return "";
      const total = parseInt(g.total_days, 10) || 0;
      const day = parseInt(g.day_index, 10) || 0;
      if (total <= 0 || day < total - 1) return "";
      const esc = (s) => this.esc(s);
      const dueToday = day >= total;
      const labKey = dueToday ? "inbox.goal.due.today" : "inbox.goal.due.tomorrow";
      const tipKey = dueToday ? "inbox.goal.due.today_t" : "inbox.goal.due.tomorrow_t";
      let acts = "";
      if (!dueToday) {
        acts += `<button type="button" data-act="due_wrap"` +
          ` title="${esc(this.t("inbox.goal.deadline.chip_today_t"))}">` +
          `${esc(this.t("inbox.goal.deadline.chip_today"))}</button>`;
      }
      acts += `<button type="button" data-act="due_extend">${esc(this.t("inbox.goal.due.extend7"))}</button>`;
      return `<div class="gl-due"><span class="gl-due-chip" title="${esc(this.t(tipKey))}">` +
        `\u23F3 ${esc(this.t(labKey))}</span>${acts}</div>`;
    }

    /* 近30天同模板基准线（P1）：终局 ≥3 才显示——小样本读数比没数据更误导 */
    _benchLine(g) {
      const b = this._benchCache ? this._benchCache[String(g.template || "")] : null;
      if (!b || !b.n) return "";
      if (b.days != null) {
        return this.t("inbox.goal.deadline.bench",
          { rate: b.rate, n: b.n, days: b.days });
      }
      return this.t("inbox.goal.deadline.bench_no_avg", { rate: b.rate, n: b.n });
    }

    async _loadBenchmark(tid) {
      tid = String(tid || "");
      if (!tid || (tid in this._benchCache)) return;   // null 占位=已取过/取数中
      this._benchCache[tid] = null;
      let res = null;
      try {
        res = await this._api("/api/goals/report?days=30");
      } catch (_e) { res = null; }
      const bt = (res && res.ok && res.data && res.data.by_template)
        ? res.data.by_template[tid] : null;
      if (bt) {
        // organic 终局＝done+failed+expired（cancelled 是人的放弃，不进分母）
        const org = (parseInt(bt.done, 10) || 0) + (parseInt(bt.failed, 10) || 0)
          + (parseInt(bt.expired, 10) || 0);
        if (org >= 3) {
          this._benchCache[tid] = {
            n: org,
            rate: Math.round(((parseInt(bt.done, 10) || 0) / org) * 100),
            days: (bt.avg_days_to_done == null) ? null
              : Math.round(Number(bt.avg_days_to_done) * 10) / 10,
          };
        }
      }
      // 基准线到达＝DOM 注入而非 _rerender（与动态提示同模式）：重渲染会把
      // 正在输入的坐席焦点抢走、把探针/外部引用的节点整批作废——表单重开时
      // 由渲染路径从缓存直出，两条路都只有一份 .gl-dl-bench。
      if (this._deadlineOpen) {
        const g = this._d && this._d.goal;
        const line = g ? this._benchLine(g) : "";
        if (line && !this.shadowRoot.querySelector(".gl-dl-bench")) {
          const h = this.shadowRoot.querySelector('[data-ref="dl_hint"]');
          if (h && h.parentNode) {
            const div = document.createElement("div");
            div.className = "gl-hint gl-dl-bench";
            div.textContent = line;
            h.parentNode.insertBefore(div, h.nextSibling);
          }
        }
      }
    }

    /* 全模板基准线一次取齐（step-1 场景卡「近30天达成率」用；与 _loadBenchmark
       同一 report 数据源同一 organic 口径。第一步纯卡片零输入框 → 数据到达后
       _rerender 安全不丢焦点；失败清 tried 位允许下次重试）。 */
    async _loadBenchAll() {
      if (this._benchAllTried) return;
      this._benchAllTried = true;
      let res = null;
      try {
        res = await this._api("/api/goals/report?days=30");
      } catch (_e) { res = null; }
      const bt = (res && res.ok && res.data && res.data.by_template)
        ? res.data.by_template : null;
      if (!bt) { this._benchAllTried = false; return; }
      Object.keys(bt).forEach((tid) => {
        const b = bt[tid] || {};
        const org = (parseInt(b.done, 10) || 0) + (parseInt(b.failed, 10) || 0)
          + (parseInt(b.expired, 10) || 0);
        if (org >= 3) {
          this._benchCache[tid] = {
            n: org,
            rate: Math.round(((parseInt(b.done, 10) || 0) / org) * 100),
            days: (b.avg_days_to_done == null) ? null
              : Math.round(Number(b.avg_days_to_done) * 10) / 10,
          };
        } else if (!(tid in this._benchCache)) {
          this._benchCache[tid] = null;   // 样本不足＝不显示（小样本比没数据更误导）
        }
      });
      if (this._formOpen && this._formStep === 1) this._rerender();
    }

    /* ── 完成通知链路状态（P0 2026-08-18：达成后会不会有人收到推送，面板可见）── */

    _notifyStatusCached() {
      return (Date.now() - NOTIFY_STATUS.at < 300000) ? NOTIFY_STATUS.data : null;
    }

    _loadNotifyStatus() {
      if (NOTIFY_STATUS.inflight
          || (Date.now() - NOTIFY_STATUS.at < 300000)) return;
      NOTIFY_STATUS.inflight = true;
      this._api("/api/goals/notify-status").then((res) => {
        NOTIFY_STATUS.at = Date.now();
        NOTIFY_STATUS.data = (res && res.ok && res.data && res.data.ok)
          ? res.data : null;
        NOTIFY_STATUS.inflight = false;
        this._fillNotifyRow();
      }).catch(() => {
        NOTIFY_STATUS.at = Date.now();
        NOTIFY_STATUS.data = null;
        NOTIFY_STATUS.inflight = false;
      });
    }

    _notifyRowInner(st) {
      const esc = (s) => this.esc(s);
      if (!st) return "";
      // J-4 G（2026-09-05）：服务端 nag_suppressed（桌面包 client 形态 / 非管理角色）
      // → 「扫描未开 / 推送未接通」黄字整行不渲染（返回 "" ＝行保持 hidden）；
      // 这两条 nag 只有内部运维能处置，对坐席机用户是常驻的无能为力。push_covered
      // 的绿字（正面信息）不受影响。旧后端缺字段＝undefined＝旧行为。
      const nag = !st.nag_suppressed;
      if (!st.notify_enabled) {
        if (!nag) return "";
        return `<span class="gl-notify-warn" title="${esc(this.t("inbox.goal.notify.scan_off_t"))}">` +
          `${esc(this.t("inbox.goal.notify.scan_off"))}</span>`;
      }
      if (st.push_covered) {
        // P2：定向副本开着且当前登录人已绑定通知号 → 「管理员 + 你」。
        // P3：未绑定 → 就地「接收推送」自助绑定（保存即真发测试验证可达——
        // Telegram bot 私聊不了没跟它说过话的人，这是绑定后收不到的最高发故障）
        const mine = st.push_agent && st.self_bound;
        const k = mine ? "inbox.goal.notify.push_on_agent" : "inbox.goal.notify.push_on";
        let bind = "";
        if (st.push_agent && !st.self_bound) {
          bind = `<button type="button" class="gl-notify-link" data-act="notify_bind_open">` +
            `${esc(this.t("inbox.goal.notify.bind_btn"))}</button>`;
        }
        return `<span class="gl-notify-ok" title="${esc(this.t(k + "_t"))}">` +
          `${esc(this.t(k))}</span>` + bind + this._neChipHtml() +
          this._nbFormHtml() + this._neFormHtml();
      }
      // 渠道未订阅「目标达成」：给能开接通弹窗的角色一个直达按钮（宿主
      // wsAlertlinkOpen 只对 master/operator 渲染——坐席只看提示不受挫）
      if (!nag) return "";
      const canConnect = typeof root.wsAlertlinkOpen === "function";
      return `<span class="gl-notify-warn" title="${esc(this.t("inbox.goal.notify.push_off_t"))}">` +
        `${esc(this.t("inbox.goal.notify.push_off"))}</span>` +
        (canConnect
          ? `<button type="button" class="gl-notify-link" data-act="notify_connect">` +
            `${esc(this.t("inbox.goal.notify.connect"))}</button>`
          : "");
    }

    /* 渲染占位（数据没到＝hidden；到达后 _fillNotifyRow DOM 注入，不整块重渲染
       ——active 卡上可能开着期限/成交表单，重渲染会抢焦点） */
    _notifyRowHtml() {
      this._loadNotifyStatus();
      const inner = this._notifyRowInner(this._notifyStatusCached());
      return `<div class="gl-notify" data-ref="notifyrow"${inner ? "" : " hidden"}>${inner}</div>`;
    }

    _fillNotifyRow() {
      try {
        const el = this.shadowRoot
          && this.shadowRoot.querySelector('[data-ref="notifyrow"]');
        if (!el) return;
        const inner = this._notifyRowInner(this._notifyStatusCached());
        if (inner) { el.innerHTML = inner; el.hidden = false; }
      } catch (_e) { /* soft */ }
    }

    /* ── P3：自助绑定迷你表单（通知行内展开；保存=写绑定+立即真发测试）── */

    _nbFormHtml() {
      if (!this._nbOpen) return "";
      const esc = (s) => this.esc(s);
      const busy = !!this._nbBusy;
      return `<div class="gl-nb">` +
        `<div class="gl-hint">${esc(this.t("inbox.goal.notify.bind_hint"))}</div>` +
        `<div class="gl-nb-row">` +
        `<input type="text" data-ref="nb_chat" inputmode="numeric" autocomplete="off"` +
        ` spellcheck="false" placeholder="${esc(this.t("inbox.goal.notify.bind_ph"))}"` +
        ` value="${esc(this._nbVal || "")}">` +
        `<button type="button" class="primary" data-act="notify_bind_save"${busy ? " disabled" : ""}>` +
        `${esc(this.t(busy ? "inbox.goal.notify.bind_busy" : "inbox.goal.notify.bind_save"))}</button>` +
        `<button type="button" data-act="notify_bind_close">${esc(this.t("cp.common.cancel"))}</button>` +
        `</div>` +
        (this._nbMsg
          ? `<div class="gl-nb-msg${this._nbMsgOk ? " ok" : ""}">${esc(this._nbMsg)}</div>`
          : "") +
        `</div>`;
    }

    async _nbSave() {
      const inp = this._ref("nb_chat");
      const val = String((inp && inp.value) || "").trim();
      this._nbVal = val;
      if (!/^-?\d{1,20}$/.test(val)) {
        this._nbMsg = this.t("inbox.goal.notify.bind_bad");
        this._nbMsgOk = false;
        this._rerender();
        return;
      }
      this._nbBusy = true;
      this._nbMsg = "";
      this._rerender();
      _beacon("goal_notify_bind_save");
      let res = null;
      try {
        res = await this._api("/api/workspace/my-notify-binding", {
          method: "POST", body: new URLSearchParams({ tg_chat_id: val }),
        });
      } catch (_e) { res = null; }
      if (!(res && res.ok && res.data && res.data.ok)) {
        this._nbBusy = false;
        this._nbMsg = String((res && res.data && res.data.detail)
          || this.t("inbox.goal.err_retry")).slice(0, 160);
        this._nbMsgOk = false;
        this._rerender();
        return;
      }
      // 绑定已落库 → 立即真发一条测试（可达性当场验证，不等第一单成交）
      let tres = null;
      try {
        tres = await this._api("/api/workspace/my-notify-binding/test", {
          method: "POST", body: new URLSearchParams(),
        });
      } catch (_e) { tres = null; }
      this._nbBusy = false;
      if (tres && tres.ok && tres.data && tres.data.ok) {
        this._nbMsg = this.t("inbox.goal.notify.bind_ok");
        this._nbMsgOk = true;
        _beacon("goal_notify_bind_ok");
      } else {
        // 绑定保存成功、测试没送达——把服务端原因给到人（自查后重存即重测）
        this._nbMsg = String((tres && tres.data && tres.data.detail)
          || this.t("inbox.goal.notify.bind_test_fail")).slice(0, 200);
        this._nbMsgOk = false;
        _beacon("goal_notify_bind_testfail");
      }
      NOTIFY_STATUS.at = 0;      // 失效缓存 → 行升格「管理员 + 你」
      this._loadNotifyStatus();
      this._rerender();
    }

    /* ── P3b：逐目标点名收件人（params.notify_extra；通知行内编辑，走既有
       /update 路由 params 合并——消毒/去重/封顶在服务端，前端只透传）── */

    _neList() {
      const g = this._d && this._d.goal;
      const raw = g && g.params && g.params.notify_extra;
      return Array.isArray(raw) ? raw.map((x) => String(x)) : [];
    }

    _neChipHtml() {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id
          || (g.status !== "active" && g.status !== "paused")) return "";
      const esc = (s) => this.esc(s);
      const list = this._neList();
      const chip = list.length
        ? `<span class="gl-ne-chip" title="${esc(this.t("inbox.goal.notify.extra_n_t", { list: list.join(", ") }))}">` +
          `${esc(this.t("inbox.goal.notify.extra_n", { n: list.length }))}</span>`
        : "";
      return chip +
        `<button type="button" class="gl-notify-link" data-act="notify_extra_toggle"` +
        ` title="${esc(this.t("inbox.goal.notify.extra_btn_t"))}">` +
        `${esc(this.t("inbox.goal.notify.extra_btn"))}</button>`;
    }

    _neFormHtml() {
      if (!this._neOpen) return "";
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return "";
      const esc = (s) => this.esc(s);
      const busy = !!this._neBusy;
      const val = this._neVal != null ? this._neVal : this._neList().join(", ");
      return `<div class="gl-nb">` +
        `<div class="gl-hint">${esc(this.t("inbox.goal.notify.extra_hint"))}</div>` +
        `<div class="gl-nb-row">` +
        `<input type="text" data-ref="ne_input" autocomplete="off"` +
        ` spellcheck="false" placeholder="${esc(this.t("inbox.goal.notify.extra_ph"))}"` +
        ` value="${esc(val)}">` +
        `<button type="button" class="primary" data-act="notify_extra_save"${busy ? " disabled" : ""}>` +
        `${esc(this.t(busy ? "inbox.goal.notify.bind_busy" : "inbox.goal.notify.extra_save"))}</button>` +
        `<button type="button" data-act="notify_extra_close">${esc(this.t("cp.common.cancel"))}</button>` +
        `</div>` +
        (this._neMsg
          ? `<div class="gl-nb-msg${this._neMsgOk ? " ok" : ""}">${esc(this._neMsg)}</div>`
          : "") +
        `</div>`;
    }

    async _neSave() {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      const inp = this._ref("ne_input");
      const val = String((inp && inp.value) || "").trim();
      this._neVal = val;
      this._neBusy = true;
      this._neMsg = "";
      this._rerender();
      _beacon("goal_notify_extra_save");
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ params: { notify_extra: val } }),
        });
      } catch (_e) { res = null; }
      this._neBusy = false;
      if (res && res.ok && res.data && res.data.goal) {
        const ng = res.data.goal;
        if (g.today && !ng.today) ng.today = g.today;   // 同 _postDeadline 兜底
        this._d = Object.assign({}, this._d, { goal: ng });
        const n = (ng.params && Array.isArray(ng.params.notify_extra))
          ? ng.params.notify_extra.length : 0;
        this._neMsg = n ? this.t("inbox.goal.notify.extra_ok", { n })
          : this.t("inbox.goal.notify.extra_cleared");
        this._neMsgOk = true;
        this._neVal = null;
      } else {
        this._neMsg = String((res && res.data && res.data.detail)
          || this.t("inbox.goal.err_retry")).slice(0, 160);
        this._neMsgOk = false;
      }
      this._rerender();
    }

    /* 动态后果提示：加速/放缓/今天收口/低于下限，与后端护栏同一判定式 */
    _deadlineHint(g, raw) {
      const minD = Math.max(1, parseInt(g.day_index, 10) || 1);
      const cur = parseInt(g.total_days, 10) || 0;
      const v = parseInt(raw, 10);
      if (!isFinite(v) || v < minD || v > 180) {
        return this.t("inbox.goal.deadline.err_min", { day: minD });
      }
      if (v === minD) return this.t("inbox.goal.deadline.hint_today");
      if (cur && v < cur) return this.t("inbox.goal.deadline.hint_faster");
      if (cur && v > cur) return this.t("inbox.goal.deadline.hint_slower");
      return this.t("inbox.goal.deadline.hint_same");
    }

    /* 改期限的唯一 POST 出口：表单保存与临期快捷动作共用（口径零分叉）。
       成功＝收表单/换视图/toast/回源刷新；失败＝把 detail 暂存给调用方渲染。 */
    async _postDeadline(days, opts) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return false;
      this._lastDeadlineErr = "";
      let res = null;
      try {
        res = await this._api(`/api/goals/${encodeURIComponent(g.goal_id)}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ deadline_days: days }),
        });
      } catch (_e) { res = null; }
      if (res && res.status === 403) { this._hideCard(!_showDisabledHint()); return false; }
      if (res && res.ok && res.data && res.data.goal) {
        this._deadlineOpen = false;
        this._deadlineVal = null;
        const ng = res.data.goal;
        if (g.today && !ng.today) ng.today = g.today;   // 旧后端 update 不带 today
        this._d = Object.assign({}, this._d, { goal: ng });
        this._flashToast((opts && opts.quiet) || days < 1
          ? this.t("inbox.goal.toast_saved")
          : this.t("inbox.goal.deadline.saved", { n: days }));
        this._rerender();
        this.emit("cp-goal-changed", { action: "deadline", conversationId: g.conversation_id });
        this.refresh();   // 回源刷一次：产品行/今日拍与服务端结算完全对齐
        return true;
      }
      const detail = res && res.data && res.data.detail;
      this._lastDeadlineErr = detail ? String(detail) : "";
      return false;
    }

    async _saveDeadline(btn) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      const inp = this._ref("dl_days");
      const v = parseInt(inp && inp.value != null && inp.value !== ""
        ? inp.value : this._deadlineVal, 10);
      const minD = Math.max(1, parseInt(g.day_index, 10) || 1);
      const err = this._ref("dl_err");
      if (!isFinite(v) || v < minD || v > 180) {
        // 预检与后端护栏同口径：低于「已进行天数」＝立即过期，压根不发请求
        if (err) {
          err.textContent = this.t("inbox.goal.deadline.err_min", { day: minD });
          err.hidden = false;
        }
        return;
      }
      if (v === (parseInt(g.total_days, 10) || 0)) {
        // 没改值＝没事可做：静默收起，不发一个无意义请求、不落无意义事件
        this._deadlineOpen = false;
        this._deadlineVal = null;
        this._rerender();
        return;
      }
      if (btn) btn.disabled = true;
      _beacon("goal_deadline_save");
      const ok = await this._postDeadline(v);
      if (!ok) {
        if (btn) btn.disabled = false;
        const e2 = this._ref("dl_err");
        if (e2) {
          e2.textContent = (this._lastDeadlineErr
            || this.t("inbox.goal.err_retry")).slice(0, 160);
          e2.hidden = false;
        }
      }
    }

    /* ── 「AI 做了什么」＝每一拍清单（M-7 A #236；懒取 /api/goals/{id}/beats + 每目标缓存）──
       此前读 goal_actions 拍史（planned/consumed…），用户看不出「到底有没有发出去一条消息」。
       现在每行＝一拍：第 N 拍 · 时刻 · 主动发出/回复带方向/被拦下/待预览 · 说了什么 ·
       投递真相（已投递/排队中/发送失败）· 可跳到那条消息；被拦的写明被什么拦。 */

    _blockedWhy(byMap) {
      const m = (byMap && typeof byMap === "object") ? byMap : {};
      const keys = Object.keys(m).sort((a, b) => (m[b] || 0) - (m[a] || 0)).slice(0, 2);
      return keys.map((k) => this._blockedLabel(k)).join("、");
    }

    _blockedLabel(reason) {
      const r = String(reason || "").trim();
      if (!r) return this.t("inbox.goal.blocked.unknown");
      if (r.indexOf("preview:") === 0) return this.t("inbox.goal.blocked.first_send_preview");
      const v = this.t("inbox.goal.blocked." + r);
      return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : r;
    }

    _fmtBeatTime(ts) {
      const t = parseFloat(ts) || 0;
      if (!t) return "";
      const d = new Date(t * 1000);
      const now = new Date();
      const hhmm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
      const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
        && d.getDate() === now.getDate();
      return sameDay ? hhmm : `${("0" + (d.getMonth() + 1)).slice(-2)}-${("0" + d.getDate()).slice(-2)} ${hhmm}`;
    }

    _renderBeatRow(b, curConv) {
      const esc = (s) => this.esc(s);
      const kind = ({ sent: 1, injected: 1, blocked: 1, preview: 1 })[String(b.kind)] ? String(b.kind) : "sent";
      const st = String(b.status || "");
      const stLabel = st ? this.t("inbox.goal.beatst." + st) : "";
      const stTxt = (stLabel && String(stLabel).indexOf("inbox.goal.") !== 0) ? stLabel : st;
      const head = kind === "sent" && b.n
        ? this.t("inbox.goal.beats.n", { n: parseInt(b.n, 10) || 0 })
        : this.t("inbox.goal.beatk." + kind);
      let tx = String(b.text_head || "");
      if (kind === "blocked") tx = this._blockedLabel(b.reason) + (tx ? `：${tx}` : "");
      else if (kind === "preview") tx = this._blockedLabel("first_send_preview") + (tx ? `：${tx}` : "");
      else if (kind === "sent" && st === "failed" && b.reason) tx = (tx ? tx + " · " : "") + String(b.reason);
      const short = tx.length > 72 ? tx.slice(0, 72) + "\u2026" : tx;
      const other = (b.conversation_id && curConv && b.conversation_id !== curConv)
        ? `<span class="gl-beat-other" title="${esc(b.conversation_id)}">${esc(this.t("inbox.goal.beats.other_conv"))}</span>`
        : "";
      const jump = (kind === "sent" && st === "sent" && (b.message_id || b.conversation_id))
        ? `<button type="button" class="gl-beat-jump" data-act="beat_jump" data-conv="${esc(b.conversation_id || "")}"` +
          ` data-mid="${esc(b.message_id || "")}" data-ts="${esc(String(b.sent_at || b.ts || ""))}">` +
          `${esc(this.t("inbox.goal.beats.jump"))}</button>`
        : "";
      const phase = String(b.phase || "");
      return `<div class="gl-beat kind-${kind} st-${esc(st)}">` +
        `<span class="gl-beat-time">${esc(this._fmtBeatTime(b.sent_at || b.ts))}</span>` +
        `<span class="gl-beat-kind">${esc(head)}</span>` +
        (stTxt ? `<span class="gl-beat-st">${esc(stTxt)}</span>` : "") +
        (phase && kind === "sent" ? `<span class="gl-beat-phase">${esc(phase)}</span>` : "") +
        `<span class="gl-beat-tx" title="${esc(tx)}">${esc(short)}</span>${other}${jump}</div>`;
    }

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
        const curConv = String((this._d && this._d.goal && this._d.goal.conversation_id) || this._lastCid || "");
        const rows = (this._progRows || []).slice().reverse().slice(0, 12)
          .map((b) => this._renderBeatRow(b, curConv)).join("");
        body = rows || `<div class="gl-hint">${esc(this.t("inbox.goal.beats.empty"))}</div>`;
      }
      return `<div class="gl-prog open"><button type="button" class="gl-prog-btn" data-act="prog_toggle">` +
        `\u25BE ${esc(this.t("inbox.goal.progress.hide"))}</button>${body}</div>`;
    }

    async _loadProgress(force, gidOverride) {
      // gidOverride（M-7 C）：终态卡/空态「查看结算」里看上一个目标的每一拍
      const g = this._d && this._d.goal;
      const gid = String(gidOverride || (g && g.goal_id) || "");
      if (!gid) return;
      if (!force && this._progGoalId === gid && Array.isArray(this._progRows)) {
        this._rerender();
        return;
      }
      this._progLoading = true;
      this._progErr = false;
      this._rerender();
      let res = null;
      try {
        res = await this._api("/api/goals/" + encodeURIComponent(gid) + "/beats");
      } catch (_e) { res = null; }
      this._progLoading = false;
      if (res && res.ok && res.data && Array.isArray(res.data.beats)) {
        this._progGoalId = gid;
        this._progRows = res.data.beats;
      } else {
        this._progErr = true;
      }
      this._rerender();
    }

    /* 跳到那条消息——与工作台既有跳转口径对齐（通知中心点击 / ?conv=&mid= 深链同源）：
       同文档宿主（unified_inbox 直挂 cp-goal）→ window.__wsFocusConv(conv, mid)：选中会话 +
       翻页找到气泡滚动高亮；iframe 宿主（copilot/app.html 多窗）→ 事件 cp-goal-jump-message
       由 app.html 桥 postMessage 给父窗口，父窗口同样落到 __wsFocusConv。 */
    _jumpBeat(el) {
      const conv = String(el.getAttribute("data-conv") || "");
      const mid = String(el.getAttribute("data-mid") || "");
      const ts = parseFloat(el.getAttribute("data-ts")) || 0;
      _beacon("goal_beat_jump");
      let handled = false;
      try {
        if (typeof root.__wsFocusConv === "function") {
          handled = root.__wsFocusConv(conv, mid || undefined) !== false;
        }
      } catch (_e) { handled = false; }
      if (!handled) this.emit("cp-goal-jump-message", { conversation_id: conv, message_id: mid, ts });
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

    /* 业务域（N-3 #241）：后端随 for-conversation / templates 下发 business_domain；
       陪伴 = 画像不渲染商机区、「标成交」改「标记达成」（达成结果 + 备注，无产品 / 金额）。
       缺键（旧后端）→ 按销售域＝旧行为。 */
    _isCompanion() {
      const bd = (this._d && this._d.business_domain)
        || (this._templates && this._templates.business_domain) || "";
      return String(bd) === "companion";
    }

    /* 域感知文案：陪伴域优先取 <key>_c，缺键回落 <key>。 */
    _tDomain(key) {
      if (this._isCompanion()) {
        const v = this.t(key + "_c");
        if (v && String(v).indexOf("inbox.goal.") !== 0) return String(v);
      }
      return this.t(key);
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
      // N-3 #241（TN736F）：画像字段集 = 后端按业务域给的同一份 schema；分组随
      // p.tracks（销售 关系+商机 / 陪伴 关系+个人情况），旧后端缺键回落 relation+bant。
      const tracks = (Array.isArray(p.tracks) && p.tracks.length === 2) ? p.tracks : ["relation", "bant"];
      const sec = String(p.secondary_track || tracks[1] || "bant");
      const trackLabel = (t) => {
        const v = this.t("inbox.goal.profile.track." + t);
        return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : String(t);
      };
      // 双零不渲染 0% 条（空数字是反激励——与 P19 本周成果 chip 同哲学）；
      // 冷启动由 profile.empty 文案 + 缺口 chips 承担。
      const hasFill = pct(fill[tracks[0]]) > 0 || pct(fill[sec]) > 0;
      const fillrow = hasFill
        ? `<div class="gl-fillrow">` +
          bar(trackLabel(tracks[0]), fill[tracks[0]], "") +
          bar(trackLabel(sec), fill[sec], "b2") + `</div>`
        : "";
      const hd = `<div class="gl-prof-hd">` +
        `<span class="gl-prof-t">${esc(this.t("inbox.goal.profile.title"))}</span>` +
        `<span class="sp"></span>` +
        `<button data-act="prof_toggle">${esc(this.t(this._profOpen ? "cp.common.cancel" : "inbox.goal.profile.edit"))}</button></div>` +
        fillrow;

      if (this._profOpen) {
        // 按轨分组 + 建议问法当 placeholder（空输入框不再让人猜「该填什么」）；
        // lifecycle 槽（流失原因）随第二组尾，不为一个槽位单开分组；自定义标签自成一组；
        // 不在当前域表但有值的存量槽（extra）归「其他已记录」——不丢、可改、可清。
        const grp = (label, slots) => {
          if (!slots.length) return "";
          return `<div class="gl-pf-group">${esc(label)}</div>` + slots.map((s) =>
            `<div><label class="gl-fl">${esc(s.label)}${s.sensitive ? " \uD83D\uDD12" : ""}</label>` +
            `<input data-prof-key="${esc(s.key)}" value="${esc(s.value || "")}"` +
            ` placeholder="${esc(this._slotAskText(s.key))}"` +
            `${s.sensitive ? ` title="${esc(this.t("inbox.goal.profile.sensitive_t"))}"` : ""}></div>`).join("");
        };
        const all = p.slots || [];
        const rel = all.filter((s) => !s.extra && s.track === tracks[0]);
        const second = all.filter((s) => !s.extra && s.track !== tracks[0] && s.track !== "custom");
        const custom = all.filter((s) => !s.extra && s.track === "custom");
        const extra = all.filter((s) => s.extra);
        return `<div class="gl-sec gl-prof">${hd}<div class="gl-prof-form">` +
          grp(trackLabel(tracks[0]), rel) +
          grp(trackLabel(sec), second) +
          grp(trackLabel("custom"), custom) +
          grp(trackLabel("extra"), extra) + `</div>` +
          `<div class="gl-ferr" data-ref="perr" hidden></div>` +
          `<div class="acts gl-acts"><button class="primary" data-act="prof_save">${esc(this.t("inbox.goal.profile.save"))}</button></div></div>`;
      }

      const filled = (p.slots || []).filter((s) => s.value);
      // 缺口 chips：后端按域给的 missing（陪伴 = 个人情况缺口）；旧后端回落第二轨筛
      // 敏感槽（收入 / 资产）不出缺口 chip——「拟稿去问」就是直接问；要填走「补录」
      const missKeys = Array.isArray(p.missing) ? p.missing : null;
      const missing = (p.slots || []).filter((s) => !s.value && !s.extra && !s.sensitive &&
        (missKeys ? missKeys.indexOf(s.key) >= 0 : s.track === sec)).slice(0, 3);
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

    /* result 原始串（order:ref / manual:by / 信号名）→ 完成方式人话标签 */
    _doneKindLabel(result) {
      const r = String(result || "");
      const k = r.indexOf("order:") === 0 ? "order"
        : (r.indexOf("manual:") === 0 ? "manual" : (r ? "auto" : ""));
      if (!k) return "";
      const v = this._tDomain("inbox.goal.done.kind." + k);
      return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : "";
    }

    /* 「标成交 / 标记达成」表单字段（N-3 #241 VAQGZY ②）：字段来自业务域——
       陪伴 = 达成结果（关系升温 / 见面 / 转付费陪伴 / 其他）+ 备注；销售 = 产品 + 金额。 */
    _wonFieldsHtml() {
      const esc = (s) => this.esc(s);
      if (this._isCompanion()) {
        const opts = ["warm", "meet", "paid", "other"].map((k) =>
          `<option value="${esc(this.t("inbox.goal.won_outcome." + k))}">${esc(this.t("inbox.goal.won_outcome." + k))}</option>`).join("");
        return `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_outcome"))}</label>` +
          `<select data-ref="won_outcome"><option value="">${esc(this.t("inbox.goal.won_outcome.none"))}</option>${opts}</select></div>` +
          `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_note"))}</label>` +
          `<input data-ref="won_note" autocomplete="off" maxlength="200"></div>`;
      }
      return `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_product"))}</label>` +
        `<input data-ref="won_product" autocomplete="off"></div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.won_meta_amount"))}</label>` +
        `<input type="number" step="any" data-ref="won_amount"></div>`;
    }

    /* 摸底类目标完成态（VAQGZY ③）：先核对已采集字段清单，缺的可点「补录」——
       不是产品 / 金额。数据源＝后端 slots_progress（目标卡打勾清单同源）。 */
    _wonChecklistHtml(g) {
      const esc = (s) => this.esc(s);
      const rows = (g && Array.isArray(g.slots_progress)) ? g.slots_progress : [];
      if (!rows.length) return "";
      const items = rows.map((s) => {
        const ok = !!s.filled;
        return `<div class="gl-wc-row${ok ? " ok" : ""}">` +
          `<span class="gl-wc-mark">${ok ? "\u2713" : "\u25CB"}</span>` +
          `<span class="gl-wc-lab">${esc(s.label || s.key)}</span>` +
          (ok ? `<span class="gl-wc-val">${esc(String(s.value || ""))}</span>`
            : `<button type="button" class="gl-link" data-act="slot_edit" data-slot="${esc(s.key)}">${esc(this.t("inbox.goal.profile.fill_btn"))}</button>`) +
          `</div>`;
      }).join("");
      return `<div class="gl-wc"><div class="gl-wc-t">${esc(this.t("inbox.goal.won_checklist_t"))}</div>${items}</div>`;
    }

    /* 终局卡（2026-08-18 升级）：达成＝收入时刻——金带 + 完成方式/用时 chips +
       48h 跟进黄金窗提示 + 转化类顺手接留存目标；失守保持低压灰。原始 result
       串降级进 tooltip（「order:ORD-9」直晒是黑话）。 */
    _renderTerminal(g) {
      const esc = (s) => this.esc(s);
      const done = String(g.status) === "done";
      const endTs = parseFloat(g.done_at) || parseFloat(g.updated_at) || 0;
      const start = parseFloat(g.start_ts) || 0;
      const paceT = String(g.pace || "natural");
      const sprintT = paceT === "today" || paceT === "session";
      // 限时目标用时按分钟/小时口径（「用时 0 天」是废话）；自然天维持天口径
      const days = (!sprintT && done && endTs > 0 && start > 0 && endTs > start)
        ? Math.round(((endTs - start) / 86400) * 10) / 10 : null;
      const durTxt = (sprintT && done && endTs > start && start > 0)
        ? this.t("inbox.goal.done.dur", { t: this._fmtRemain(endTs - start) })
        : "";
      // 限时复盘（P2）：用了几拍 + 最后一拍意图（tooltip）——「停在哪」
      // 失守时比成功时更该看见
      const beatsN = parseInt(g.beats_used, 10);
      const beatsTxt = (sprintT && isFinite(beatsN) && beatsN > 0)
        ? this.t("inbox.goal.term.beats", { n: beatsN }) : "";
      const kindTxt = done ? this._doneKindLabel(g.result) : "";
      // 「已推送提醒」回执（P2）：服务端读通知幂等标记附 notified 键——
      // true=完成推送真的发出去过；缺键/false（含扫描器 60s 内未跑到）不渲染
      const pushed = done && g.notified === true;
      let facts = "";
      if (kindTxt || days != null || durTxt || beatsTxt || pushed) {
        facts = `<div class="gl-done-facts">` +
          (kindTxt ? `<span class="gl-done-chip" title="${esc(String(g.result || ""))}">${esc(kindTxt)}</span>` : "") +
          (days != null ? `<span class="gl-done-chip">${esc(this.t("inbox.goal.done.days", { n: days }))}</span>` : "") +
          (durTxt ? `<span class="gl-done-chip">${esc(durTxt)}</span>` : "") +
          (beatsTxt ? `<span class="gl-done-chip" title="${esc(String(g.last_beat_intent || ""))}">${esc(beatsTxt)}</span>` : "") +
          (pushed ? `<span class="gl-done-chip gl-done-pushed" title="${esc(this.t("inbox.goal.done.pushed_t"))}">${esc(this.t("inbox.goal.done.pushed"))}</span>` : "") +
          `</div>`;
      }
      const hint = done
        ? `<div class="gl-hint">${esc(this.t("inbox.goal.done.next_hint"))}</div>` : "";
      // 转化类达成 → 一键接续留存目标（retention 模板在库才给；参数继承上单，
      // 与 service 的订单自动链同语义——这里是人工成交/无订单 ref 场景的手动口）
      let chain = "";
      const tmpl = this._tmplById(String(g.template || ""));
      if (done && g.template !== "retention_expand"
          && tmpl && String(tmpl.kind || "") === "conversion"
          && this._tmplById("retention_expand")) {
        chain = `<button data-act="chain_retention">${esc(this.t("inbox.goal.done.chain_btn"))}</button>`;
      }
      const rawResult = (!done && g.result)
        ? `<div class="gl-result">${esc(g.result)}</div>` : "";
      // 疑似达成补录（P2/P3 2026-08-30）：到期前检出过联系方式信号但没人点确认
      // → expired 不是终审——「补录成交」走 expired→done 迁移（与迟到订单复活同哲学）
      const revive = this._reviveBarHtml(g);
      // M-7 C（#236）：到期/失守/达成的结算摘要（7 天内可见）——不再静默消失
      const settle = this._settlementHtml(g);
      return `<div class="${done ? "gl-term-done" : "gl-term"}">` +
        `<div class="gl-hdr"><span class="gl-title">${esc(g.title || g.template_name || "")}</span>` +
        `${this._statusBadge(g.status)}</div>` + facts + rawResult + settle + revive + hint +
        `<div class="acts gl-acts">${chain}` +
        `<button class="primary" data-act="open_form">${esc(this.t("inbox.goal.again_btn"))}</button></div>` +
        `</div>` + this._notifyRowHtml();
    }

    /* ── M-7 C（#236）：到期结算——终态卡 / 空态「上一个目标」/ 新目标卡「上一个已结算」共用 ── */

    _settleReasonText(s) {
      const r = String((s && s.reason) || "");
      if (!r) return this.t("inbox.goal.settle.reason.unknown");
      if (r.indexOf("blocked:") === 0) {
        return this.t("inbox.goal.settle.reason.blocked", { why: this._blockedLabel(r.slice(8)) });
      }
      if (r.indexOf("engine:") === 0) {
        return this.t("inbox.goal.settle.reason.engine", { why: this._engineWhy([r.slice(7)]) });
      }
      const v = this.t("inbox.goal.settle.reason." + r);
      return (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : this.t("inbox.goal.settle.reason.unknown");
    }

    _settlementHtml(g) {
      const esc = (s) => this.esc(s);
      const s = g && g.settlement && typeof g.settlement === "object" ? g.settlement : null;
      if (!s || !g.settled_recent) return "";
      const n = (k) => (s[k] != null ? parseInt(s[k], 10) || 0 : 0);
      const done = String(g.status) === "done";
      const openKey = String(g.goal_id || "");
      const open = this._settleOpen === openKey;
      const btn = `<button type="button" class="gl-settle-btn" data-act="settle_toggle" data-gid="${esc(openKey)}">` +
        `${open ? "\u25BE " + esc(this.t("inbox.goal.settle.hide"))
                : "\u25B8 " + esc(this.t(done ? "inbox.goal.settle.view_done" : "inbox.goal.settle.view"))}</button>`;
      if (!open) return `<div class="gl-settle">${btn}</div>`;
      const line = this.t("inbox.goal.settle.line", {
        planned: n("planned"), sent: n("sent"), injected: n("injected"),
        blocked: n("blocked"), replies: n("replies"),
      });
      return `<div class="gl-settle open">${btn}` +
        `<div class="gl-settle-line">${esc(line)}</div>` +
        `<div class="gl-settle-why">${esc(this._settleReasonText(s))}</div>` +
        `<button type="button" class="gl-beats-btn" data-act="prog_toggle_gid" data-gid="${esc(openKey)}">` +
        `${esc(this.t("inbox.goal.progress.show"))}</button>` +
        (this._progGoalId === openKey && this._progOpen ? this._renderProgress() : "") +
        `</div>`;
    }

    _prevSettledHtml() {
      const p = this._d && this._d.prev_settled;
      if (!p || !p.settlement) return "";
      const esc = (s) => this.esc(s);
      const s = p.settlement;
      return `<div class="gl-prev-settled" title="${esc(this.t("inbox.goal.settle.line", {
        planned: s.planned || 0, sent: s.sent || 0, injected: s.injected || 0,
        blocked: s.blocked || 0, replies: s.replies || 0 }))}">` +
        `\u2713 ${esc(this.t("inbox.goal.settle.prev", {
          title: String(p.title || "").slice(0, 24), sent: s.sent || 0,
          why: this._settleReasonText(s) }))}</div>`;
    }

    _reviveBarHtml(g) {
      const esc = (s) => this.esc(s);
      if (!g || String(g.status) !== "expired") return "";
      const osig = (g.params && typeof g.params === "object")
        ? g.params.outcome_signal : null;
      if (!osig || !osig.v) return "";
      return `<div class="gl-sec gl-outcome">` +
        `<span class="gl-outcome-tx">\uD83C\uDFAF ${esc(this.t("inbox.goal.outcome.revive", { v: String(osig.v) }))}</span>` +
        `<button type="button" class="primary" data-act="revive_won">` +
        `${esc(this.t("inbox.goal.outcome.revive_btn"))}</button></div>`;
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

    _sprintOk(tmpl) {
      if (!tmpl) return false;
      if (tmpl.sprint_ok === true) return true;
      if (tmpl.sprint_ok === false) return false;
      return !!SPRINT_OK_FALLBACK[String(tmpl.id || "")];
    }

    _normPace(p) {
      const s = String(p || "").toLowerCase();
      return PACES.indexOf(s) >= 0 ? s : "natural";
    }

    _defaultHorizon(pace, tmpl) {
      if (pace === "session") return 60;
      if (pace === "today") return 8;
      return (tmpl && tmpl.default_days) || 14;
    }

    _hoursUntilMidnight() {
      const n = new Date();
      const end = new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1);
      const h = (end - n) / 3600000;
      return Math.max(2, Math.min(12, Math.round(h * 10) / 10));
    }

    _fmtRemain(sec) {
      const s = Math.max(0, parseFloat(sec) || 0);
      if (s < 90) return LANG === "en" ? "<1 min" : "不到1分钟";
      if (s < 3600) {
        const m = Math.max(1, Math.round(s / 60));
        return LANG === "en" ? (m + " min") : (m + " 分钟");
      }
      const h = s / 3600;
      if (h < 10) {
        const t = h.toFixed(1);
        return LANG === "en" ? (t + " h") : (t + " 小时");
      }
      const hr = Math.round(h);
      return LANG === "en" ? (hr + " h") : (hr + " 小时");
    }

    _paceLineText(pace, daysVal, autonomy) {
      const pLab = this.t("inbox.goal.form.pace." + pace);
      let hz;
      if (pace === "session") hz = LANG === "en" ? (daysVal + " min") : (daysVal + " 分钟");
      else if (pace === "today") hz = LANG === "en" ? (daysVal + " h") : (daysVal + " 小时");
      else hz = LANG === "en" ? (daysVal + " days") : (daysVal + " 天");
      const aLab = this._autonomyLabel(autonomy);
      const line = this.t("inbox.goal.form.pace_line", { pace: pLab, horizon: hz, autonomy: aLab });
      return (line && String(line).indexOf("inbox.goal.") !== 0)
        ? line : (pLab + " · " + hz + " · " + aLab);
    }

    _updatePaceLine() {
      const el = this._ref("pace_line");
      if (!el) return;
      const dy = this._ref("days");
      el.textContent = this._paceLineText(
        this._formPace || "natural", dy ? (dy.value || "") : "", this._formAutonomy || "auto");
    }

    _horizonChipsHtml(pace) {
      const esc = (s) => this.esc(s);
      let chips = [];
      if (pace === "session") chips = [30, 60, 90];
      else if (pace === "today") chips = [3, 4, 8];   // 3h=老板口径的经典冲刺窗
      else chips = [7, 14, 30];
      const suffix = pace === "session" ? (LANG === "en" ? "m" : "分")
        : (pace === "today" ? "h" : "");
      let html = chips.map((v) =>
        `<button type="button" class="gl-chip" data-act="horizon_chip" data-val="${v}">` +
        `${esc(String(v) + suffix)}</button>`).join("");
      if (pace === "today") {
        html += `<button type="button" class="gl-chip" data-act="horizon_today_end">` +
          `${esc(this.t("inbox.goal.form.chip_today_end"))}</button>`;
      }
      return `<div class="gl-hz-chips">${html}</div>`;
    }

    _paceSegHtml(pace) {
      const esc = (s) => this.esc(s);
      // 文案与行为一致（P3 2026-08-30）：冲刺推进器开着 → 限时档如实描述
      // 「对方不开口也会主动出手」；关着 → 维持「对方开口才推进」的旧口径
      const engineOn = !!(this._templates && this._templates.caps
        && this._templates.caps.sprint_enabled);
      let hint = "";
      if (engineOn && pace !== "natural") {
        hint = this.t("inbox.goal.form.pace." + pace + "_hint_engine");
        if (!hint || String(hint).indexOf("inbox.goal.") === 0) hint = "";
      }
      if (!hint) hint = this.t("inbox.goal.form.pace." + pace + "_hint");
      const hintOk = hint && String(hint).indexOf("inbox.goal.") !== 0;
      return `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.pace"))}</label>` +
        `<div class="gl-pace" role="radiogroup">` + PACES.map((p) =>
          `<button type="button" class="${p === pace ? "on" : ""}" data-act="pick_pace"` +
          ` data-pace="${p}" role="radio" aria-checked="${p === pace ? "true" : "false"}">` +
          `${esc(this.t("inbox.goal.form.pace." + p))}</button>`).join("") +
        `</div>` +
        (hintOk ? `<div class="gl-pace-hint">${esc(hint)}</div>` : "") + `</div>`;
    }

    _horizonInputHtml(pace, daysVal) {
      const esc = (s) => this.esc(s);
      let min = "1", max = "180", step = "1", lab = this.t("inbox.goal.form.days");
      let help = this.t("inbox.goal.form.days_help");
      if (pace === "session") {
        min = "15"; max = "120"; step = "5";
        lab = this.t("inbox.goal.form.horizon_min");
        help = this.t("inbox.goal.form.days_help_session");
      } else if (pace === "today") {
        min = "2"; max = "12"; step = "0.5";
        lab = this.t("inbox.goal.form.horizon_h");
        help = this.t("inbox.goal.form.days_help_today");
      }
      return `<div><label class="gl-fl">${esc(lab)}</label>` +
        `<input type="number" min="${min}" max="${max}" step="${step}" data-ref="days"` +
        ` value="${esc(String(daysVal))}">` +
        this._horizonChipsHtml(pace) +
        this._helpHtml(help) + `</div>`;
    }

    async _extendDeadline(addSec) {
      const g = this._d && this._d.goal;
      const start = parseFloat(g && g.start_ts) || 0;
      const dl = parseFloat(g && g.deadline_ts) || 0;
      if (!(start > 0 && dl > start)) return;
      const days = (dl - start + addSec) / 86400;
      _beacon("goal_extend");
      await this._postDeadline(days, { quiet: true });
    }

    /* ── 冲刺（P3 2026-08-30）：倒计时活字 / 立即推进 / 补录成交 / 插队 ── */

    _armSprintTick() {
      if (this._spTick) return;
      this._spTick = setInterval(() => {
        try {
          if (!this.isConnected) {
            clearInterval(this._spTick); this._spTick = 0; return;
          }
          const el = this.shadowRoot.querySelector('[data-ref="sp_remain"]');
          if (!el) {           // 卡片已切走/终局 → 停表（下次渲染重新武装）
            clearInterval(this._spTick); this._spTick = 0; return;
          }
          const dl = parseFloat(el.getAttribute("data-dl")) || 0;
          if (!dl) return;
          const rem = dl - Date.now() / 1000;
          if (rem <= 0) {      // 到期：整卡刷新拿终局（settle-on-read）
            clearInterval(this._spTick); this._spTick = 0;
            this.refresh(); return;
          }
          el.textContent = this.t("inbox.goal.remaining",
            { t: this._fmtRemain(rem) });
        } catch (_e) { /* 计时器绝不抛 */ }
      }, 30000);
    }

    async _sprintNudge(btn) {
      const g = this._d && this._d.goal;
      if (!g || !g.goal_id) return;
      if (btn) btn.disabled = true;
      let res = null;
      try {
        res = await this._api(
          `/api/goals/${encodeURIComponent(g.goal_id)}/sprint/nudge`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: "{}" });
      } catch (_e) { res = null; }
      _beacon("goal_sprint_nudge");
      if (res && res.ok && res.data && res.data.ok) {
        this._flashToast(this.t("inbox.goal.sprint.nudge_ok"));
        this.refresh();
        return;
      }
      const detail = res && res.data && res.data.detail;
      this._flashToast((typeof detail === "string" && detail)
        || this.t("cp.base.err"));
      if (btn) btn.disabled = false;
    }

    async _reviveWon(btn) {
      // 补录成交：expired 但曾检出达成信号的目标（d.goal 刚过期 / d.last 历史）
      const d = this._d || {};
      const g = (d.goal && TERMINAL[String(d.goal.status)] ? d.goal : null)
        || d.last || null;
      if (!g || !g.goal_id) return;
      if (btn) btn.disabled = true;
      let res = null;
      try {
        res = await this._api(
          `/api/goals/${encodeURIComponent(g.goal_id)}/status`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "done" }) });
      } catch (_e) { res = null; }
      _beacon("goal_revive_won");
      if (res && res.ok) {
        this._flashToast(this.t("inbox.goal.outcome.revive_ok"));
        this.emit("cp-goal-changed",
          { action: "revive", conversationId: (this._ctx || {}).conversationId });
        this.refresh();
        return;
      }
      if (btn) btn.disabled = false;
    }

    async _jumpSprint(btn) {
      /* 冲刺插队：暂停当前长线目标 → 带 resume_goal_id 重新提交冲刺表单
         （后端在冲刺终局自动恢复长线目标——「回程票」）。 */
      const ctx = this._ctx || {};
      if (!ctx.conversationId) return;
      if (btn) btn.disabled = true;
      let cur = null;
      try {
        const r = await this._api(
          "/api/goals/for-conversation?conversation_id=" +
          encodeURIComponent(ctx.conversationId));
        cur = r && r.ok && r.data && r.data.goal;
      } catch (_e) { cur = null; }
      if (!cur || !cur.goal_id || TERMINAL[String(cur.status)]) {
        this._jumpOffer = false;
        if (btn) btn.disabled = false;
        this._rerender();
        return;
      }
      let pr = null;
      try {
        pr = await this._api(
          `/api/goals/${encodeURIComponent(cur.goal_id)}/status`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "pause" }) });
      } catch (_e) { pr = null; }
      if (!(pr && pr.ok)) {
        if (btn) btn.disabled = false;
        return;
      }
      this._resumeGoalId = String(cur.goal_id);
      this._jumpOffer = false;
      _beacon("goal_sprint_jump");
      await this._create(null);
    }

    _forceSegHtml() {
      const esc = (s) => this.esc(s);
      const cur = this._formForce === "max" ? "max" : "steady";
      const hint = cur === "max"
        ? this.t("inbox.goal.form.force.max_hint") : "";
      return `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.force"))}</label>` +
        `<div class="gl-pace" role="radiogroup">` +
        ["steady", "max"].map((f) =>
          `<button type="button" class="${f === cur ? "on" : ""}" data-act="pick_force"` +
          ` data-force="${f}" role="radio" aria-checked="${f === cur ? "true" : "false"}">` +
          `${esc(this.t("inbox.goal.form.force." + f))}</button>`).join("") +
        `</div>` +
        (hint ? `<div class="gl-pace-hint">${esc(hint)}</div>` : "") + `</div>`;
    }

    /* ── 建目标向导 · 参数控件注册表 ──────────────────────────────────────
       已知 (模板, 参数) 渲染人话控件（下拉/滑杆/textarea/示例 chips）；
       未注册参数回落通用输入框——未来新模板零前端改动也能用。
       label/help 优先走 i18n 键（模板热更零重启），键缺失回落后端 schema
       的 label_zh/en（绝不裸奔键名）。 */

    _paramLabel(tid, p) {
      const v = this.t("inbox.goal.param_label." + tid + "." + p.key);
      if (v && String(v).indexOf("inbox.goal.") !== 0) return String(v);
      return LANG === "en" ? (p.label_en || p.label_zh || p.key)
        : (p.label_zh || p.label_en || p.key);
    }

    _paramHelp(tid, p) {
      const v = this.t("inbox.goal.param_help." + tid + "." + p.key);
      if (v && String(v).indexOf("inbox.goal.") !== 0) return String(v);
      // 后端 schema 若带 help_zh/en（包2 起），作为未登记键的回落
      const h = LANG === "en" ? (p.help_en || p.help_zh) : (p.help_zh || p.help_en);
      return h ? String(h) : "";
    }

    _helpHtml(text) {
      return text ? `<div class="gl-hint">${this.esc(text)}</div>` : "";
    }

    /* pickers 优先（包2 后端 templates 响应自带），否则用 /api/monetize/catalog */
    _unlockItems() {
      const pk = this._templates && this._templates.pickers;
      if (pk && Array.isArray(pk.unlock_items) && pk.unlock_items.length) return pk.unlock_items;
      return (this._catalog && this._catalog.items) || [];
    }

    _tierItems() {
      const pk = this._templates && this._templates.pickers;
      if (pk && Array.isArray(pk.tiers) && pk.tiers.length) return pk.tiers;
      return (this._catalog && this._catalog.tiers) || [];
    }

    _stageList() {
      const pk = this._templates && this._templates.pickers;
      if (pk && Array.isArray(pk.stages) && pk.stages.length) return pk.stages;
      return STAGES;
    }

    _needsCatalog(tid) {
      if (tid === "conversion_unlock") return !this._unlockItems().length;
      if (tid === "conversion_subscribe") return !this._tierItems().length;
      return false;
    }

    async _loadCatalog() {
      this._catalogTried = true;
      let res = null;
      try { res = await this._api("/api/monetize/catalog"); } catch (_e) { res = null; }
      const cat = res && res.ok && res.data && res.data.catalog;
      if (!cat || typeof cat !== "object") { this._catalog = null; return; }
      const currency = String(cat.currency || "USD");
      const items = [];
      const rawItems = cat.items || {};
      Object.keys(rawItems).forEach((id) => {
        const c = rawItems[id] || {};
        items.push({ id, label: String(c.label || id), price: parseFloat(c.price) || 0 });
      });
      const tiers = [];
      const rawTiers = cat.tiers || {};
      Object.keys(rawTiers).forEach((id) => {
        if (id === "free") return;   // 卖「免费档」没有意义
        const c = rawTiers[id] || {};
        tiers.push({ id, label: String(c.label || id), monthly: parseFloat(c.monthly) || 0 });
      });
      this._catalog = { currency, items, tiers };
    }

    _money(n) {
      const v = Math.round((parseFloat(n) || 0) * 100) / 100;
      if (!v) return "";
      const pk = this._templates && this._templates.pickers;
      const cur = String((pk && pk.currency)
        || (this._catalog && this._catalog.currency) || "USD");
      return cur === "USD" ? "$" + v : v + " " + cur;
    }

    _unlockLabelFor(id) {
      const hit = this._unlockItems().find((i) => i && i.id === id);
      return hit ? String(hit.label || "") : "";
    }

    _tierLabelFor(id) {
      const hit = this._tierItems().find((i) => i && i.id === id);
      return hit ? String(hit.label || "") : "";
    }

    _paramControl(tmpl, p) {
      const tid = String(tmpl.id || "");
      const key = String(p.key || "");
      if (tid === "conversion_unlock" && key === "item_id") return this._unlockPickerHtml(p);
      if (tid === "conversion_subscribe" && key === "tier") return this._tierPickerHtml(p);
      if ((tid === "conversion_unlock" || tid === "conversion_subscribe") && key === "item_label") {
        return this._itemLabelHtml(tmpl, p);
      }
      if (tid === "relationship_stage" && key === "target_stage") return this._stageSelectHtml(p);
      if (tid === "relationship_intimacy" && key === "target_score") return this._intimacySliderHtml(p);
      if (key === "product_id") return this._productSelectHtml(tmpl, p);
      if (key === "slots") return this._slotsPickerHtml(tmpl, p);
      if (key === "note") return this._noteHtml(tmpl, p);
      return this._genericParamHtml(tmpl, p);
    }

    _discoverySlotOptions() {
      const pk = this._templates && this._templates.pickers;
      if (pk && Array.isArray(pk.discovery_slots) && pk.discovery_slots.length) {
        return pk.discovery_slots;
      }
      return FALLBACK_DISCOVERY_SLOTS;
    }

    _parseSlotsCsv(raw) {
      // 自定义标签键形如 x_3f9a1c…（N-3 #241）——放行数字
      return String(raw || "").split(/[,，\s]+/).map((s) => s.trim().toLowerCase())
        .filter((s) => /^[a-z][a-z0-9_]*$/.test(s));
    }

    /* 摸底目标「要了解的信息」：多选 chips（隐藏域写 CSV，与后端 parse_selected_slots 同口径）
       N-3 #241：可选项按业务域由后端 pickers.discovery_slots 给（陪伴 = 关系 + 个人情况）；
       sensitive 槽默认不勾（模板 default 不含）、带锁标与提示；末尾「+ 自定义标签」。 */
    _slotsPickerHtml(tmpl, p) {
      const esc = (s) => this.esc(s);
      const opts = this._discoverySlotOptions();
      let selected = this._parseSlotsCsv(p.default);
      if (!selected.length) selected = ["age", "occupation", "location", "interests"];
      const sel = {};
      selected.forEach((k) => { sel[k] = 1; });
      let anySens = false;
      const chips = opts.map((s) => {
        const k = String(s.key || "");
        const lab = LANG === "en" ? (s.label_en || s.label_zh || k)
          : (s.label_zh || s.label_en || k);
        const on = !!sel[k];
        const sens = !!s.sensitive;
        if (sens) anySens = true;
        const tip = sens ? this.t("inbox.goal.form.slot_sensitive_t")
          : (s.custom ? this.t("inbox.goal.form.slot_custom_t") : "");
        return `<button type="button" class="gl-chip${on ? " on" : ""}${sens ? " sens" : ""}` +
          `${s.custom ? " custom" : ""}" data-act="slot_toggle"` +
          ` data-slot="${esc(k)}" aria-pressed="${on ? "true" : "false"}"` +
          `${tip ? ` title="${esc(tip)}"` : ""}>${esc(lab)}</button>`;
      }).join("");
      const addBtn = `<button type="button" class="gl-chip add" data-act="slot_custom_add"` +
        ` title="${esc(this.t("inbox.goal.form.slot_custom_t"))}">` +
        `${esc(this.t("inbox.goal.form.slot_custom_add"))}</button>`;
      const sensHint = anySens
        ? `<div class="gl-sens-hint">\uD83D\uDD12 ${esc(this.t("inbox.goal.form.slot_sensitive_t"))}</div>` : "";
      return `<div><label class="gl-fl">${esc(this._paramLabel(tmpl.id, p))}</label>` +
        `<input type="hidden" data-param-key="slots" data-ref="slots_val" value="${esc(selected.join(","))}">` +
        `<div class="gl-chips gl-slotpicks" data-ref="slotpicks">${chips}${addBtn}</div>` +
        sensHint + `<div class="gl-ferr" data-ref="slot_custom_err" hidden></div>` +
        this._helpHtml(this._paramHelp(tmpl.id, p)) + `</div>`;
    }

    /* 「+ 自定义标签」：prompt 文案 → POST /api/goals/custom-slots → 后端回整张
       discovery_slots（配置是唯一事实源）→ 覆盖 pickers → 新键置勾 → 重画 chips。
       旧后端无端点（404）→ 报「添加失败」，绝不静默。 */
    async _addCustomSlot() {
      const label = String((root.prompt && root.prompt(this.t("inbox.goal.form.slot_custom_prompt"), "")) || "").trim();
      if (!label) return;
      const err = this._ref("slot_custom_err");
      const show = (msg) => { if (err) { err.textContent = msg; err.hidden = !msg; } };
      show(this.t("inbox.goal.form.slot_custom_saving"));
      let res = null;
      try {
        res = await this._api("/api/goals/custom-slots", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: label.slice(0, 24) }),
        });
      } catch (_e) { res = null; }
      if (!res || !res.ok || !res.data || !Array.isArray(res.data.discovery_slots)) {
        const detail = res && res.data && (res.data.detail || res.data.error);
        show(this.t("inbox.goal.form.slot_custom_fail") + (detail ? "：" + detail : ""));
        return;
      }
      show("");
      if (!this._templates) this._templates = {};
      if (!this._templates.pickers) this._templates.pickers = {};
      this._templates.pickers.discovery_slots = res.data.discovery_slots;
      const key = String(res.data.added_key || "");
      const hid = this._ref("slots_val");
      const cur = hid ? this._parseSlotsCsv(hid.value) : [];
      if (key && cur.indexOf(key) < 0) cur.push(key);
      // 重画整块 picker（chips 集合变了，不能只 toggle）
      const wrap = this._ref("slotpicks");
      if (wrap && wrap.parentElement) {
        const tmpl = this._tmplById(this._formTid) || this._tmplById("profile_discovery");
        const pdef = ((tmpl && tmpl.params) || []).find((x) => x.key === "slots") || { key: "slots", default: "" };
        const holder = document.createElement("div");
        holder.innerHTML = this._slotsPickerHtml(tmpl || { id: "profile_discovery", params: [] },
          Object.assign({}, pdef, { default: cur.join(",") }));
        const fresh = holder.firstElementChild;
        if (fresh) wrap.parentElement.replaceWith(fresh);
      }
      this._draftCapture();
      _beacon("goal_slot_custom_add");
    }

    _syncSlotChips() {
      const hid = this._ref("slots_val");
      const wrap = this._ref("slotpicks");
      if (!hid || !wrap) return;
      const sel = {};
      this._parseSlotsCsv(hid.value).forEach((k) => { sel[k] = 1; });
      wrap.querySelectorAll("button[data-slot]").forEach((b) => {
        const on = !!sel[b.getAttribute("data-slot")];
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    _toggleSlotChip(key) {
      const k = String(key || "").trim().toLowerCase();
      if (!k) return;
      const hid = this._ref("slots_val");
      if (!hid) return;
      const cur = this._parseSlotsCsv(hid.value);
      const idx = cur.indexOf(k);
      if (idx >= 0) {
        if (cur.length <= 1) return;   // 至少留一项——空 slots 摸底目标等于无目标
        cur.splice(idx, 1);
      } else {
        cur.push(k);
      }
      hid.value = cur.join(",");
      this._syncSlotChips();
      this._draftCapture();
    }

    /* 从自定义 note 推断该勾哪些槽（关键词→槽位；无命中回落默认四项） */
    _inferSlotsFromNote(note) {
      const t = String(note || "");
      const hits = [];
      const rules = [
        [/年龄|几岁|age/i, "age"],
        [/职业|生意|工作|occupation|job|work/i, "occupation"],
        [/城市|坐标|在哪|location|city/i, "location"],
        [/兴趣|爱好|喜欢|interest/i, "interests"],
        [/称呼|叫什么|name/i, "name"],
        [/痛点|头疼|need|pain/i, "need"],
        [/平台|渠道|channel/i, "channel"],
        [/预算|budget/i, "budget"],
        [/团队|几个人|team/i, "team_size"],
        [/拍板|决策|authority/i, "authority"],
        [/上线|什么时候|timeline/i, "timeline"],
      ];
      rules.forEach(([re, key]) => { if (re.test(t) && hits.indexOf(key) < 0) hits.push(key); });
      return hits.length ? hits : ["age", "occupation", "location", "interests"];
    }

    /* 主推产品下拉（数据=templates.pickers.site_products，包2 后端提供；
       旧后端无此段 → 回落通用输入框）。首项「不指定」=AI 按画像自动选品。 */
    _productSelectHtml(tmpl, p) {
      const esc = (s) => this.esc(s);
      const pk = this._templates && this._templates.pickers;
      const prods = (pk && Array.isArray(pk.site_products)) ? pk.site_products : [];
      if (!prods.length) return this._genericParamHtml(tmpl, p);
      const dv = p.default == null ? "" : String(p.default);
      const opts = `<option value=""${dv ? "" : " selected"}>` +
        `${esc(this.t("inbox.goal.form.product_auto_opt"))}</option>` +
        prods.map((pr) => {
          const nm = LANG === "en" ? (pr.name_en || pr.name_zh || pr.id)
            : (pr.name_zh || pr.name_en || pr.id);
          const tx = String(nm) + (pr.price_from ? " \u00b7 " + pr.price_from : "");
          return `<option value="${esc(pr.id)}"${pr.id === dv ? " selected" : ""}>${esc(tx)}</option>`;
        }).join("");
      return `<div><label class="gl-fl">${esc(this._paramLabel(tmpl.id, p))}</label>` +
        `<select data-param-key="${esc(p.key)}">${opts}</select>` +
        this._helpHtml(this._paramHelp(tmpl.id, p)) + `</div>`;
    }

    _genericParamHtml(tmpl, p) {
      const esc = (s) => this.esc(s);
      const typ = String(p.type || "").toLowerCase() === "number" ? "number" : "text";
      const dv = p.default == null ? "" : String(p.default);
      return `<div><label class="gl-fl">${esc(this._paramLabel(tmpl.id, p))}</label>` +
        `<input type="${typ}" data-param-key="${esc(p.key)}" value="${esc(dv)}"${typ === "number" ? ` step="any"` : ""}>` +
        this._helpHtml(this._paramHelp(tmpl.id, p)) + `</div>`;
    }

    /* 「卖什么」下拉：目录项（label · 价格）+「自定义…」高级手填。
       item_id 是功能字段（ledger 对照权益自动判达成），下拉=数据质量护栏。 */
    _unlockPickerHtml(p) {
      const esc = (s) => this.esc(s);
      const items = this._unlockItems();
      if (!items.length) {
        // 目录不可用 → 通用输入框 + 帮助（绝不空板）
        return this._genericParamHtml({ id: "conversion_unlock" }, p);
      }
      let cur = this._unlockSel;
      if (!cur) {
        const dv = p.default == null ? "" : String(p.default);
        cur = items.some((i) => i.id === dv) ? dv : items[0].id;
        this._unlockSel = cur;
      }
      const isCustom = cur === "__custom__";
      const opts = items.map((i) => {
        const price = this._money(i.price);
        const tx = String(i.label || i.id) + (price ? " \u00b7 " + price : "");
        return `<option value="${esc(i.id)}"${i.id === cur ? " selected" : ""}>${esc(tx)}</option>`;
      }).join("") +
        `<option value="__custom__"${isCustom ? " selected" : ""}>${esc(this.t("inbox.goal.form.unlock_custom_opt"))}</option>`;
      return `<div><label class="gl-fl">${esc(this._paramLabel("conversion_unlock", p))}</label>` +
        `<select data-chg="unlock_sel"${isCustom ? "" : ` data-param-key="item_id"`}>${opts}</select>` +
        this._helpHtml(this._paramHelp("conversion_unlock", p)) +
        `<div class="gl-advp-bd" data-ref="unlock_custom"${isCustom ? "" : " hidden"}>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.unlock_custom_id"))}</label>` +
        `<input data-ref="unlock_custom_id"${isCustom ? ` data-param-key="item_id"` : ""}` +
        ` placeholder="${esc(this.t("inbox.goal.form.unlock_custom_id_ph"))}">` +
        this._helpHtml(this.t("inbox.goal.form.unlock_custom_id_help")) + `</div></div></div>`;
    }

    _tierPickerHtml(p) {
      const esc = (s) => this.esc(s);
      const tiers = this._tierItems();
      if (!tiers.length) return this._genericParamHtml({ id: "conversion_subscribe" }, p);
      let cur = this._tierSel;
      if (!cur) {
        const dv = p.default == null ? "" : String(p.default);
        cur = tiers.some((t) => t.id === dv) ? dv : tiers[0].id;
        this._tierSel = cur;
      }
      const opts = tiers.map((t) => {
        const price = this._money(t.monthly);
        const tx = String(t.label || t.id)
          + (price ? " \u00b7 " + price + this.t("inbox.goal.form.per_month") : "");
        return `<option value="${esc(t.id)}"${t.id === cur ? " selected" : ""}>${esc(tx)}</option>`;
      }).join("");
      return `<div><label class="gl-fl">${esc(this._paramLabel("conversion_subscribe", p))}</label>` +
        `<select data-chg="tier_sel" data-param-key="tier">${opts}</select>` +
        this._helpHtml(this._paramHelp("conversion_subscribe", p)) + `</div>`;
    }

    /* 「聊天里怎么称呼它」：自动跟随所选目录项，坐席可改（改过即不再覆盖） */
    _itemLabelHtml(tmpl, p) {
      const esc = (s) => this.esc(s);
      const tid = String(tmpl.id || "");
      let init = "";
      if (this._lastAutoLabel) {
        init = this._lastAutoLabel;
      } else if (tid === "conversion_unlock") {
        init = (this._unlockSel && this._unlockSel !== "__custom__")
          ? this._unlockLabelFor(this._unlockSel) : "";
        if (!init && !this._unlockItems().length) init = p.default == null ? "" : String(p.default);
      } else {
        init = this._tierSel ? this._tierLabelFor(this._tierSel) : "";
        if (!init && !this._tierItems().length) init = p.default == null ? "" : String(p.default);
      }
      if (init && !this._labelTouched) this._lastAutoLabel = init;
      let ph = this.t("inbox.goal.form.item_label_ph");
      if (ph && String(ph).indexOf("inbox.goal.") === 0) ph = "";
      return `<div><label class="gl-fl">${esc(this._paramLabel(tid, p))}</label>` +
        `<input data-param-key="item_label" data-chg="item_label" value="${esc(init)}"` +
        (ph ? ` placeholder="${esc(ph)}"` : "") + `>` +
        this._helpHtml(this._paramHelp(tid, p)) + `</div>`;
    }

    _stageSelectHtml(p) {
      const esc = (s) => this.esc(s);
      const dv = p.default == null ? "" : String(p.default);
      const opts = this._stageList().map((s) => {
        const v = this.t("inbox.goal.stage." + s);
        const nm = (v && String(v).indexOf("inbox.goal.") !== 0) ? String(v) : s;
        return `<option value="${esc(s)}"${s === dv ? " selected" : ""}>${esc(nm)}</option>`;
      }).join("");
      return `<div><label class="gl-fl">${esc(this._paramLabel("relationship_stage", p))}</label>` +
        `<select data-param-key="target_stage">${opts}</select>` +
        this._helpHtml(this._paramHelp("relationship_stage", p)) + `</div>`;
    }

    _intimacySliderHtml(p) {
      const esc = (s) => this.esc(s);
      const v0 = Math.max(0, Math.min(100, Math.round(parseFloat(p.default) || 55)));
      return `<div><label class="gl-fl">${esc(this._paramLabel("relationship_intimacy", p))}</label>` +
        `<div class="gl-range-row">` +
        `<input type="range" min="0" max="100" step="1" value="${v0}"` +
        ` data-param-key="target_score" data-chg="range">` +
        `<span class="gl-range-val" data-ref="rangeval">${v0}</span></div>` +
        `<div class="gl-range-scale"><span>${esc(this.t("inbox.goal.form.intimacy_lo"))}</span>` +
        `<span>${esc(this.t("inbox.goal.form.intimacy_mid"))}</span>` +
        `<span>${esc(this.t("inbox.goal.form.intimacy_hi"))}</span></div>` +
        this._helpHtml(this._paramHelp("relationship_intimacy", p)) + `</div>`;
    }

    _noteHtml(tmpl, p) {
      const esc = (s) => this.esc(s);
      const tid = String(tmpl.id || "");
      const phKey = tid === "custom" ? "inbox.goal.form.note_ph_custom"
        : (tid === "engagement_reactivate" ? "inbox.goal.form.note_ph_reactivate" : "");
      let ph = phKey ? this.t(phKey) : "";
      if (ph && String(ph).indexOf("inbox.goal.") === 0) ph = "";
      let extra = "";
      if (tid === "custom") {
        const chips = [1, 2, 3].map((i) => {
          const full = this.t("inbox.goal.form.note_ex" + i);
          if (!full || String(full).indexOf("inbox.goal.") === 0) return "";
          const chip = this.t("inbox.goal.form.note_ex" + i + "_chip");
          const lab = (chip && String(chip).indexOf("inbox.goal.") !== 0) ? chip : full;
          return `<button type="button" class="gl-chip" data-act="note_example"` +
            ` data-ex="${i}">${esc(lab)}</button>`;
        }).join("");
        if (chips) {
          extra = `<div class="gl-hint">${esc(this.t("inbox.goal.form.note_ex_t"))}</div>` +
            `<div class="gl-exrow">${chips}</div>`;
        }
        // P28：自定义 note 像「获取年龄/职业」时，一键改走摸底模板（状态机+打勾）
        if (this._tmplById("profile_discovery")) {
          const noteNow = (this._formDraft && this._formDraft.tid === "custom"
            && this._formDraft.params && this._formDraft.params.note)
            ? String(this._formDraft.params.note) : String(p.default || "");
          const hot = DISCOVERY_NOTE_RE.test(noteNow);
          extra += `<div class="gl-disc-tip" data-ref="disc_tip"${hot ? "" : " hidden"}>` +
            `<div>${esc(this.t("inbox.goal.form.rec_discovery"))}</div>` +
            `<div class="acts"><button type="button" class="primary" data-act="switch_discovery">` +
            `${esc(this.t("inbox.goal.form.rec_discovery_btn"))}</button></div></div>` +
            `<div class="gl-hint">${esc(this.t("inbox.goal.form.rec_discovery_soft"))}</div>`;
        }
      }
      const dv = p.default == null ? "" : String(p.default);
      return `<div><label class="gl-fl">${esc(this._paramLabel(tid, p))}</label>` +
        `<textarea data-param-key="note" rows="3" data-chg="note_disc"` +
        `${ph ? ` placeholder="${esc(ph)}"` : ""}>${esc(dv)}</textarea>` +
        this._helpHtml(this._paramHelp(tid, p)) + extra + `</div>`;
    }

    _paramsHtml(tmpl) {
      const esc = (s) => this.esc(s);
      const params = (tmpl && tmpl.params) || [];
      if (!params.length) return `<div data-ref="params" class="gl-params"></div>`;
      const advKeys = ADV_PARAMS[String(tmpl.id || "")] || [];
      const vis = params.filter((p) => advKeys.indexOf(p.key) < 0);
      const adv = params.filter((p) => advKeys.indexOf(p.key) >= 0);
      let html = vis.map((p) => this._paramControl(tmpl, p)).join("");
      if (adv.length) {
        // 自动继承字段折叠：留存续费/唤回的产品与套餐通常由系统在成交后带入
        html += `<details class="gl-advp"><summary>${esc(this.t("inbox.goal.form.adv_params"))}</summary>` +
          `<div class="gl-advp-bd">${adv.map((p) => this._paramControl(tmpl, p)).join("")}</div></details>`;
      }
      return `<div data-ref="params" class="gl-params">${html}</div>`;
    }

    /* 下拉联动：解锁项/会员档变化 → 同步 item_id 归属 + 称呼自动跟随 */
    _onUnlockSel(sel) {
      const v = String(sel.value || "");
      this._unlockSel = v;
      const wrap = this._ref("unlock_custom");
      const cid = this._ref("unlock_custom_id");
      const isCustom = v === "__custom__";
      if (wrap) wrap.hidden = !isCustom;
      if (isCustom) {
        sel.removeAttribute("data-param-key");
        if (cid) {
          cid.setAttribute("data-param-key", "item_id");
          if (!this._restoring) { try { cid.focus(); } catch (_e) { /* soft */ } }
        }
        if (!this._restoring) _beacon("goal_form_unlock_custom");
      } else {
        sel.setAttribute("data-param-key", "item_id");
        if (cid) cid.removeAttribute("data-param-key");
        this._autoFillLabel(this._unlockLabelFor(v));
      }
    }

    _onTierSel(sel) {
      const v = String(sel.value || "");
      this._tierSel = v;
      this._autoFillLabel(this._tierLabelFor(v));
    }

    _autoFillLabel(label) {
      const inp = this.shadowRoot.querySelector('input[data-param-key="item_label"]');
      if (!inp || this._labelTouched) return;
      const cur = String(inp.value || "");
      if (!cur || cur === this._lastAutoLabel) {
        inp.value = String(label || "");
        this._lastAutoLabel = String(label || "");
      }
    }

    /* 场景类别（custom 独立成类）→ 图标 / 色条 / 弹层色带共用 */
    _kindOf(tmpl) {
      if (String(tmpl && tmpl.id) === "custom") return "custom";
      const k = String((tmpl && tmpl.kind) || "");
      return k || "other";
    }

    _kindIcon(kind) {
      const P = {
        conversion: '<path d="M20.6 13.4 11 3.8H4v7l9.6 9.6a2 2 0 0 0 2.8 0l4.2-4.2a2 2 0 0 0 0-2.8Z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
        relationship: '<path d="M12 20.7S4.6 16.1 2.7 11.9A5.3 5.3 0 0 1 12 6.6a5.3 5.3 0 0 1 9.3 5.3C19.4 16.1 12 20.7 12 20.7Z"/>',
        engagement: '<path d="M18 8.5a6 6 0 1 0-12 0c0 6.5-2.5 6.5-2.5 8.5h17c0-2-2.5-2-2.5-8.5"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
        discovery: '<path d="M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"/><path d="M2 21v-1a5 5 0 0 1 5-5h4"/><path d="M19 8v6"/><path d="M16 11h6"/>',
        custom: '<path d="M17.2 3.3a2.6 2.6 0 1 1 3.6 3.6L8 19.7 2.8 21.2 4.3 16Z"/>',
      };
      return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"` +
        ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
        (P[kind] || P.conversion) + `</svg>`;
    }

    _infoIcon() {
      return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"` +
        ` stroke-linecap="round" aria-hidden="true">` +
        `<circle cx="12" cy="12" r="9"/><path d="M12 8h.01"/><path d="M11.2 12H12v4h.8"/></svg>`;
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

    /* #166 引擎真相（单一判定口）：auto 档在给定节奏下能不能**真的自己出手**。
       数据源：卡片走 for-conversation 的 engine（按本会话平台判白名单），表单走
       /api/goals/templates 的 caps；两者字段同名。返回 {on, blockers} 或 null
       （旧后端缺字段＝不猜，维持中性文案）。
       限时档看 sprint_effective（sprint 开 + 非 dry_run + 平台白名单；D1b 起
       care 开关不再入闸）。自然档看 natural_auto_effective（优先 sprint 的
       natural_daily，否则回落 bridge + proactive_topic）。老字段作回落。 */
    _autoEngine(pace, src) {
      const e = src || (this._d && this._d.engine)
        || (this._templates && this._templates.caps) || null;
      if (!e || typeof e !== "object") return null;
      const p = this._normPace(pace);
      const arr = (x) => Array.isArray(x) ? x.map(String) : [];
      if (p === "today" || p === "session") {
        if (typeof e.sprint_effective === "boolean") {
          return { on: e.sprint_effective, blockers: arr(e.sprint_blockers) };
        }
        if (typeof e.sprint_enabled === "boolean") {
          return { on: e.sprint_enabled, blockers: e.sprint_enabled ? [] : ["sprint_disabled"] };
        }
        return null;
      }
      if (typeof e.natural_auto_effective === "boolean") {
        return { on: e.natural_auto_effective, blockers: arr(e.natural_auto_blockers) };
      }
      if (typeof e.bridge_enabled === "boolean") {
        return { on: e.bridge_enabled, blockers: e.bridge_enabled ? [] : ["bridge_disabled"] };
      }
      return null;
    }

    /* 阻塞点码 → 人话（未知码原样显示，不吞） */
    _engineWhy(blockers) {
      const out = [];
      (blockers || []).forEach((b) => {
        const k = "inbox.goal.engine.blk." + String(b);
        const s = this.t(k);
        out.push((s && String(s).indexOf("inbox.goal.") !== 0) ? s : String(b));
      });
      return out.join(LANG === "en" ? ", " : "、");
    }

    /* auto 档的诚实注解：推进器/主动桥在当前节奏下不能真出手时如实说明
       「不会自己主动发消息，只在对方来消息时带方向」并点名原因。
       caps 缺失（旧后端）＝不注解，只显示中性 hint，绝不猜。 */
    _autonomyNote(lvl) {
      if (String(lvl) !== "auto") return "";
      const caps = (this._templates && this._templates.caps) || null;
      const pace = this._normPace(this._formPace);
      const eng = this._autoEngine(pace, caps);
      if (!eng || eng.on) return "";
      const sprintBand = pace === "today" || pace === "session";
      const base = this.t(sprintBand
        ? "inbox.goal.autonomy.auto_note_sprint_off"
        : "inbox.goal.autonomy.auto_note_off");
      const why = this._engineWhy(eng.blockers);
      return why ? this.t("inbox.goal.autonomy.auto_note_why", { note: base, why }) : base;
    }

    /* ── 建目标向导（P24：一页堆全 → 两步）──────────────────────────────
       第一步（栏内）＝按业务分组的场景卡；第二步＝场景专属设置弹层。
       弹层打开时第一步仍渲染在卡内（返回时零闪烁）。 */
    _renderForm() {
      const list = (this._templates && this._templates.templates) || [];
      if (!list.length) {
        return `<div class="gl-errline" data-act="open_form">${this.esc(this.t("inbox.goal.form.load_fail"))}</div>`;
      }
      const step1 = this._renderFormStep1(list);
      if (this._formStep === 2 && this._formTid && this._tmplById(this._formTid)) {
        return step1 + this._renderFormModal();
      }
      return step1;
    }

    _renderFormStep1(list) {
      const esc = (s) => this.esc(s);
      const prefs = this._loadPrefs();
      const rec = this._recommendTid();
      // 「上次」徽标只贴场景卡（custom 不参与——进阶路径须显式选择，P18 决策）
      const lastTid = (prefs.template && prefs.template !== "custom"
        && this._tmplById(prefs.template)) ? prefs.template : "";
      // 「有草稿」徽标：本会话有未提交草稿的场景（返回第一步后草稿去向可见）
      const draftTid = (this._formDraft && this._formDraft.cid === this._draftCid())
        ? String(this._formDraft.tid || "") : "";
      const draftBadge = `<span class="gl-scen-draftb">${esc(this.t("inbox.goal.form.draft_badge"))}</span>`;
      const groups = {};
      list.filter((t) => t.id !== "custom").forEach((t) => {
        const k = String(t.kind || "other");
        (groups[k] = groups[k] || []).push(t);
      });
      const order = KIND_ORDER.concat(
        Object.keys(groups).filter((k) => KIND_ORDER.indexOf(k) < 0));
      const card = (t) => {
        const kind = this._kindOf(t);
        const nm = LANG === "en" ? (t.name_en || t.name_zh || t.id) : (t.name_zh || t.name_en || t.id);
        const sel = t.id === this._formTid && this._formStep === 2;
        const badges =
          (t.id === rec ? `<span class="gl-scen-rec">${esc(this.t("inbox.goal.form.recommended"))}</span>` : "") +
          (t.id === lastTid ? `<span class="gl-scen-last">${esc(this.t("inbox.goal.form.last_used"))}</span>` : "") +
          (t.id === draftTid ? draftBadge : "");
        const desc = this._tmplDesc(t.id);
        // 30 天基准线（organic 样本 ≥3 才显示；选场景时就建立「几天见效/达成率」
        // 预期，不用等到改期限才看见）
        const b = this._benchCache ? this._benchCache[t.id] : null;
        const bench = (b && b.n)
          ? `<div class="gl-scen-bench">${esc(this.t("inbox.goal.form.bench_card", { rate: b.rate, n: b.n }))}` +
            (b.days != null ? esc(` · ${b.days}d`) : "") + `</div>`
          : "";
        return `<div class="gl-scen gk-${esc(kind)}${sel ? " sel" : ""}" data-act="pick_tmpl"` +
          ` data-tid="${esc(t.id)}" role="radio" aria-checked="${sel ? "true" : "false"}">` +
          `<span class="gl-scen-ic">${this._kindIcon(kind)}</span>` +
          `<div class="gl-scen-mn"><div class="gl-scen-nm">${esc(nm)}${badges}</div>` +
          (desc ? `<div class="gl-scen-d">${esc(desc)}</div>` : "") + bench + `</div></div>`;
      };
      let rows = "";
      order.forEach((k) => {
        const grp = groups[k];
        if (!grp || !grp.length) return;
        const gv = this.t("inbox.goal.form.grp." + k);
        if (gv && String(gv).indexOf("inbox.goal.") !== 0) {
          rows += `<div class="gl-grp gk-${esc(k)}"><span class="gl-grp-dot"></span>` +
            `<span class="gl-grp-nm">${esc(gv)}</span>` +
            `<span class="gl-grp-n">${grp.length}</span></div>`;
          if (k === "discovery") {
            const sh = this.t("inbox.goal.form.grp_start_hint");
            if (sh && String(sh).indexOf("inbox.goal.") !== 0) {
              rows += `<div class="gl-grp-hint">${esc(sh)}</div>`;
            }
          }
        }
        rows += grp.map(card).join("");
      });
      // 自定义目标＝独立高亮卡（进阶入口；与场景卡同级、视觉更醒目）
      const customT = this._tmplById("custom");
      let advHtml = "";
      if (customT) {
        const nm = LANG === "en" ? (customT.name_en || customT.name_zh || "custom")
          : (customT.name_zh || customT.name_en || "custom");
        const sel = this._formTid === "custom" && this._formStep === 2;
        const desc = this._tmplDesc("custom");
        advHtml = `<div class="gl-scen gl-scen-custom${sel ? " sel" : ""}" data-act="pick_custom"` +
          ` role="radio" aria-checked="${sel ? "true" : "false"}">` +
          `<span class="gl-scen-ic">${this._kindIcon("custom")}</span>` +
          `<div class="gl-scen-mn"><div class="gl-scen-nm">${esc(nm)}` +
          `<span class="gl-adv-pill">${esc(this.t("inbox.goal.form.adv_pill"))}</span>` +
          (draftTid === "custom" ? draftBadge : "") + `</div>` +
          (desc ? `<div class="gl-scen-d">${esc(desc)}</div>` : "") + `</div></div>`;
      }
      // 推荐理由（数据来自宿主喂的沉默时长；有推荐才说「为什么」）
      let recNote = "";
      if (rec) {
        const h = (this._ctx && this._ctx.goalHint) || {};
        const days = Math.max(1, Math.round((parseFloat(h.silentHours) || 0) / 24));
        recNote = `<div class="gl-hint">${esc(this.t("inbox.goal.form.rec_silent", { d: days }))}</div>`;
      }
      return `<div class="gl-form">` +
        `<div class="gl-alias">${esc(this.t("inbox.goal.title_hint"))}</div>` +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.scenario"))}</label>` +
        `<div class="gl-hint">${esc(this.t("inbox.goal.form.step1_lead"))}</div>` +
        `<div class="gl-scenlist" role="radiogroup">${rows}${advHtml}</div>${recNote}</div>` +
        `<div class="acts gl-acts"><button data-act="close_form">${esc(this.t("cp.common.cancel"))}</button></div>` +
        `</div>`;
    }

    /* 第二步：场景专属设置弹层（fixed 居中）。退出语义（P0/P1 2026-08-04）：
       「返回」/Esc＝回第一步；×＝关闭整个表单（对话框惯例，与「返回」分离）；
       背板点击＝判脏——干净回第一步、有输入不关闭只提示已暂存（误触防线）。
       所有退出路径草稿都在（sessionStorage），重进同场景/重开表单即恢复。 */
    _renderFormModal() {
      const esc = (s) => this.esc(s);
      const tmpl = this._tmplById(this._formTid) || {};
      const kind = this._kindOf(tmpl);
      const nm = LANG === "en" ? (tmpl.name_en || tmpl.name_zh || tmpl.id)
        : (tmpl.name_zh || tmpl.name_en || tmpl.id);
      const desc = this._tmplDesc(tmpl.id);
      const prefs = this._loadPrefs();
      const pace = this._sprintOk(tmpl) ? this._normPace(this._formPace) : "natural";
      this._formPace = pace;
      let daysVal;
      if (this._formDraft && this._formDraft.tid === tmpl.id && this._formDraft.days) {
        daysVal = this._formDraft.days;
      } else if (this._formUsePrefDays && prefs.template === tmpl.id) {
        if (pace === "natural" && prefs.deadline_days > 0
            && this._normPace(prefs.pace || "natural") === "natural") {
          daysVal = prefs.deadline_days;
        } else if (pace !== "natural" && this._normPace(prefs.pace) === pace
            && prefs.horizon > 0) {
          daysVal = prefs.horizon;
        }
      }
      if (daysVal == null) daysVal = this._defaultHorizon(pace, tmpl);
      const sprintBand = pace === "today" || pace === "session";
      // 全自动为主（2026-08-12 运营方针）：缺省 auto；偏好记忆只回放 suggest/auto
      // ——observe 是「这个目标先看看」的一次性选择，不许粘成后续所有目标的默认
      // （实录：一次选了只观察，之后三个目标全默认 observe → 坐席以为功能没生效）
      let prefAuto = (AUTONOMY.indexOf(prefs.autonomy) >= 0
        && prefs.autonomy !== "observe") ? prefs.autonomy : "auto";
      // #166：推进器/主动桥在本节奏下不能真出手 → 缺省不落到灰掉的「自动推进」
      // （skuio 实录：默认 auto → 卡上「自动推进 · 进行中」却什么都不会主动做）；
      // 坐席仍可显式点选 auto（卡内有原因说明）。只改缺省，不动本次表单里已选的值。
      const engDef = this._autoEngine(pace, (this._templates && this._templates.caps) || null);
      if (engDef && !engDef.on && prefAuto === "auto") prefAuto = "suggest";
      const curAuto = AUTONOMY.indexOf(this._formAutonomy) >= 0
        ? this._formAutonomy : prefAuto;
      this._formAutonomy = (sprintBand && curAuto === "observe") ? "suggest" : curAuto;
      const aNote = this._autonomyNoteFull(this._formAutonomy);
      const prefNote = (this._formUsePrefDays && prefs.template === tmpl.id
        && ((pace === "natural" && prefs.deadline_days > 0)
          || (pace !== "natural" && prefs.horizon > 0)))
        ? `<div class="gl-hint">${esc(this.t("inbox.goal.prefs_restored"))}</div>` : "";
      const hasParams = ((tmpl.params) || []).length > 0;
      const paramsSec = hasParams
        ? `<div><div class="gl-msec-t">${esc(this.t("inbox.goal.form.params_title"))}</div>` +
          this._paramsHtml(tmpl) + `</div>`
        : this._paramsHtml(tmpl);
      // 草稿恢复提示条（仅当本会话草稿属于当前场景时显示；「清空重填」＝显式弃稿口）
      const restoreBar = (this._formDraft && this._formDraft.tid === String(tmpl.id || "")
        && this._formDraft.cid === this._draftCid())
        ? `<div class="gl-mrestore"><span>${esc(this.t("inbox.goal.form.draft_restored"))}</span>` +
          `<button type="button" data-act="draft_clear">${esc(this.t("inbox.goal.form.draft_clear"))}</button></div>`
        : "";
      // 背板＝ov_dismiss（判脏：干净回第一步/有输入不关闭），显式退出走 ×/「返回」/Esc
      return `<div class="gl-ov" data-act="ov_dismiss" role="presentation">` +
        `<div class="gl-modal gk-${esc(kind)}${sprintBand ? " sprint" : ""}" data-act="modal_noop" role="dialog"` +
        ` aria-modal="true" aria-label="${esc(nm)}">` +
        `<div class="gl-mband"></div>` +
        `<div class="gl-mhd"><span class="gl-scen-ic">${this._kindIcon(kind)}</span>` +
        `<span class="gl-mtitle">${esc(nm)}</span>` +
        `<button type="button" class="gl-mclose" data-act="close_form"` +
        ` title="${esc(this.t("inbox.goal.form.close"))}"` +
        ` aria-label="${esc(this.t("inbox.goal.form.close"))}">\u2715</button></div>` +
        (desc ? `<div class="gl-mdesc">${esc(desc)}</div>` : "") +
        `<div class="gl-mbody gl-form">` +
        restoreBar +
        this._arcHtml(tmpl, pace) +
        paramsSec +
        prefNote +
        (this._sprintOk(tmpl) ? this._paceSegHtml(pace) : "") +
        this._horizonInputHtml(pace, daysVal) +
        (sprintBand ? this._forceSegHtml() : "") +
        `<div><label class="gl-fl">${esc(this.t("inbox.goal.form.autonomy"))}</label>` +
        this._autonomyCardsHtml(this._formAutonomy, sprintBand) +
        `<input type="hidden" data-ref="autonomy" value="${esc(this._formAutonomy)}">` +
        `<div class="gl-note-info" data-ref="anote"${aNote ? "" : " hidden"}>${this._infoIcon()}` +
        `<span data-ref="anotetx">${esc(aNote)}</span></div></div>` +
        `<div class="gl-pace-line" data-ref="pace_line">${esc(this._paceLineText(pace, daysVal, this._formAutonomy))}</div>` +
        `<div class="gl-ferr" data-ref="ferr" hidden></div>` +
        (this._jumpOffer && sprintBand
          ? `<div class="gl-mrestore"><span>${esc(this.t("inbox.goal.sprint.jump_note"))}</span>` +
            `<button type="button" class="primary" data-act="jump_sprint">` +
            `${esc(this.t("inbox.goal.sprint.jump_btn"))}</button></div>`
          : "") +
        `</div>` +
        `<div class="gl-mfoot"><span class="gl-mhint" data-ref="mhint" hidden></span>` +
        `<button data-act="form_back">${esc(this.t("inbox.goal.form.back"))}</button>` +
        `<button class="primary" data-act="create">${esc(this.t("inbox.goal.form.create"))}</button></div>` +
        `</div></div>`;
    }

    /* 「AI 会怎么推进」节奏预览：里程碑 + 推进力度（push_curve 由包2 后端
       templates 响应提供；旧后端无此字段 → 只显示里程碑名，不猜力度）。 */
    _arcHtml(tmpl, pace) {
      const esc = (s) => this.esc(s);
      pace = this._normPace(pace || this._formPace);
      let rows = "";
      if (pace === "session" || pace === "today") {
        // 限时档＝按拍序的三步短弧（后端 sprint_push：首拍 soft、之后 direct）
        const pref = pace === "session" ? "arc_session_" : "arc_today_";
        const names = [
          this.t("inbox.goal.form." + pref + "1"),
          this.t("inbox.goal.form." + pref + "2"),
          this.t("inbox.goal.form." + pref + "3"),
        ];
        const curve = ["soft", "direct", "direct"];
        rows = names.map((nm, i) => {
          const pl = curve[i];
          const pill = `<span class="gl-pill ${pl}">${esc(this.t("inbox.goal.push." + pl))}</span>`;
          return `<div class="gl-arc-row"><span class="gl-arc-idx">${i + 1}</span>` +
            `<span class="gl-arc-nm">${esc(nm)}</span>${pill}</div>`;
        }).join("");
      } else {
        const ms = Array.isArray(tmpl.milestones) ? tmpl.milestones : [];
        if (!ms.length) return "";
        const curve = Array.isArray(tmpl.push_curve) ? tmpl.push_curve : [];
        rows = ms.map((m, i) => {
          const nm = LANG === "en" ? (m.en || m.zh || "") : (m.zh || m.en || "");
          let pill = "";
          if (curve.length) {
            const pl = String(curve[Math.min(i, curve.length - 1)] || "");
            if (PUSH_LEVELS.indexOf(pl) >= 0) {
              pill = `<span class="gl-pill ${pl}">${esc(this.t("inbox.goal.push." + pl))}</span>`;
            }
          }
          return `<div class="gl-arc-row"><span class="gl-arc-idx">${i + 1}</span>` +
            `<span class="gl-arc-nm">${esc(nm)}</span>${pill}</div>`;
        }).join("");
      }
      const hint = (pace === "today" || pace === "session")
        ? this.t("inbox.goal.form.arc_hint_sprint")
        : this.t("inbox.goal.form.arc_hint");
      return `<div><div class="gl-msec-t">${esc(this.t("inbox.goal.form.arc_title"))}</div>` +
        `<div class="gl-arc">${rows}</div>` +
        `<div class="gl-hint">${esc(hint)}</div></div>`;
    }

    /* AI 参与度三张单选卡（点选走 pick_autonomy，DOM 级切换不重渲染表单）。
       展示序＝auto 优先、observe 垫底（2026-08-12 全自动为主：推荐徽标随迁 auto，
       只观察降为末位进阶选项；后端 autonomy_levels 只作成员集，展示序前端定）。 */
    _autonomyCardsHtml(cur, sprintBand) {
      const esc = (s) => this.esc(s);
      const raw = (this._templates && this._templates.autonomy_levels
        && this._templates.autonomy_levels.length)
        ? this._templates.autonomy_levels : AUTONOMY;
      const rank = { auto: 0, suggest: 1, observe: 2 };
      const levels = raw.slice()
        .filter((l) => !(sprintBand && String(l) === "observe"))
        .sort((a, b) =>
          (rank[String(a)] == null ? 9 : rank[String(a)])
          - (rank[String(b)] == null ? 9 : rank[String(b)]));
      // #166：推进器/主动桥在当前节奏下不能真出手 → 「自动推进」卡灰掉（虚线+降透明，
      // 仍可点选——它仍是合法档位，只是此刻等价于顺势建议）、推荐徽标让给「顺势建议」、
      // 卡内说明换成引擎未生效的原因。caps 缺失（旧后端）＝维持原样不猜。
      const eng = this._autoEngine(this._formPace, (this._templates && this._templates.caps) || null);
      const autoOff = !!(eng && !eng.on);
      return `<div class="gl-auto-cards" role="radiogroup">` + levels.map((l) => {
        const lvl = String(l);
        const sel = lvl === cur;
        const recOn = autoOff ? lvl === "suggest" : lvl === "auto";
        const rec = recOn
          ? `<span class="gl-scen-rec">${esc(this.t("inbox.goal.form.recommended"))}</span>` : "";
        const off = autoOff && lvl === "auto";
        const desc = off ? (this._autonomyNote("auto") || this._autonomyHint(lvl)) : this._autonomyHint(lvl);
        return `<div class="gl-auto-card${sel ? " sel" : ""}${off ? " off" : ""}" data-act="pick_autonomy"` +
          ` data-lvl="${esc(lvl)}" role="radio" aria-checked="${sel ? "true" : "false"}"` +
          `${off ? ` title="${esc(this._autonomyNote("auto"))}"` : ""}>` +
          `<div class="gl-auto-nm">${esc(this._autonomyLabel(lvl))}${off ? " \u26A0" : ""}${rec}</div>` +
          `<div class="gl-auto-d"${sel ? "" : " hidden"}>${esc(desc)}</div></div>`;
      }).join("") + `</div>`;
    }

    _ref(name) { return this.shadowRoot.querySelector(`[data-ref="${name}"]`); }

    /* auto 档注解 + 开启指路（有注解才拼指路句，正常态不贴说明书） */
    _autonomyNoteFull(lvl) {
      const base = this._autonomyNote(lvl);
      if (!base) return "";
      const extra = this.t("inbox.goal.form.auto_note_help");
      return (extra && String(extra).indexOf("inbox.goal.") !== 0)
        ? base + " " + extra : base;
    }

    /* 参与度单选卡点选：类切换 + 隐藏 input 同步 + 注解气泡更新（零重渲染） */
    _setAutonomy(lvl) {
      if (AUTONOMY.indexOf(lvl) < 0) return;
      const tmpl = this._tmplById(this._formTid);
      const pace = this._normPace(this._formPace);
      if ((pace === "today" || pace === "session") && this._sprintOk(tmpl) && lvl === "observe") {
        lvl = "suggest";
      }
      this._formAutonomy = lvl;
      const hid = this._ref("autonomy");
      if (hid) hid.value = lvl;
      this.shadowRoot.querySelectorAll(".gl-auto-card").forEach((c) => {
        const on = c.getAttribute("data-lvl") === lvl;
        c.classList.toggle("sel", on);
        c.setAttribute("aria-checked", on ? "true" : "false");
        const hint = c.querySelector(".gl-auto-d");
        if (hint) hint.hidden = !on;
      });
      const note = this._autonomyNoteFull(lvl);
      const n = this._ref("anote");
      const tx = this._ref("anotetx");
      if (tx) tx.textContent = note;
      if (n) n.hidden = !note;
      this._updatePaceLine();
    }

    /* 弹层参数控件的会话内状态（换场景/重开表单/切会话时清零） */
    _resetParamState() {
      this._unlockSel = "";
      this._tierSel = "";
      this._labelTouched = false;
      this._lastAutoLabel = "";
    }

    _rerender() {
      this._render(this.renderData(this._d || { goal: null, last: null }));
    }

    /* ── 草稿幸存层 + 编辑防打断（P0 2026-08-04）───────────────────────────
       病灶：表单值只活在 DOM 里，而本组件的 DOM 会被三类事件整块重建——
       ①背板误触/Esc（form_back 重进时按模板默认值重建）②宿主重喂 context
       （轮询身份合并/peer 解析/回前台补轮 → refresh() 先渲 loading 再重建）
       ③renderData 分支切换（出错行/进行中目标强关表单）。修法＝三层：
       输入实时快照（_draftCapture）→ 重建后原样回填（_applyFormDraft）→
       编辑期挂起同会话的外部刷新（set context 覆写）。 */

    _draftCid() { return String((this._ctx && this._ctx.conversationId) || ""); }

    _loadDraftStore(cid) {
      const key = String(cid || this._draftCid());
      if (!key) return null;
      try {
        const raw = root.sessionStorage && root.sessionStorage.getItem(DRAFT_KEY_PREFIX + key);
        if (!raw) return null;
        const d = JSON.parse(raw);
        if (!d || d.v !== 1 || d.cid !== key || !d.tid) { this._clearDraftStore(key); return null; }
        if (!isFinite(d.ts) || (Date.now() - d.ts) > DRAFT_TTL_MS) { this._clearDraftStore(key); return null; }
        return d;
      } catch (_e) { return null; }
    }

    _clearDraftStore(cid) {
      const key = String(cid || this._draftCid());
      if (!key) return;
      try { root.sessionStorage && root.sessionStorage.removeItem(DRAFT_KEY_PREFIX + key); } catch (_e) {}
    }

    /* 第二步当前值全量快照（与 _create 的取值口径一致：一切 [data-param-key] + 期限 + 参与度） */
    _formSnapshot() {
      const snap = {
        tid: this._formTid, autonomy: this._formAutonomy || "", params: {},
        days: "", pace: this._formPace || "natural",
      };
      this.shadowRoot.querySelectorAll("[data-param-key]").forEach((el) => {
        const k = el.getAttribute("data-param-key");
        if (k) snap.params[k] = String(el.value == null ? "" : el.value);
      });
      const dy = this._ref("days");
      if (dy) snap.days = String(dy.value == null ? "" : dy.value);
      return snap;
    }

    _snapshotDiffers(a, b) {
      if (!a || !b) return true;
      if (a.tid !== b.tid || String(a.days) !== String(b.days)
        || String(a.autonomy) !== String(b.autonomy)
        || String(a.pace || "natural") !== String(b.pace || "natural")) return true;
      const keys = {};
      Object.keys(a.params || {}).forEach((k) => { keys[k] = 1; });
      Object.keys(b.params || {}).forEach((k) => { keys[k] = 1; });
      return Object.keys(keys).some((k) =>
        String((a.params || {})[k] == null ? "" : a.params[k])
        !== String((b.params || {})[k] == null ? "" : b.params[k]));
    }

    /* 判脏＝当前值 vs 本场景「出厂快照」。恢复过草稿的表单天然算脏（同样受保护）。 */
    _formDirty() {
      if (!this._formOpen || this._formStep !== 2 || !this._formBaseline) return false;
      return this._snapshotDiffers(this._formSnapshot(), this._formBaseline);
    }

    /* 输入实时快照进内存 + sessionStorage；改回与出厂一致＝撤草稿（不留幻影）。 */
    _draftCapture() {
      if (this._restoring || !this._formOpen || this._formStep !== 2 || !this._formTid) return;
      const cid = this._draftCid();
      if (!cid) return;
      const snap = this._formSnapshot();
      if (this._formBaseline && !this._snapshotDiffers(snap, this._formBaseline)) {
        this._formDraft = null;
        this._clearDraftStore(cid);
        this._autosaveNote(false);   // 改回与出厂一致＝撤草稿，微反馈同步熄灭
        return;
      }
      const d = {
        v: 1, cid, tid: this._formTid, ts: Date.now(),
        params: snap.params, days: snap.days, autonomy: snap.autonomy,
        pace: snap.pace || this._formPace || "natural",
        unlockSel: this._unlockSel, tierSel: this._tierSel, labelTouched: !!this._labelTouched,
      };
      this._formDraft = d;
      try {
        root.sessionStorage && root.sessionStorage.setItem(DRAFT_KEY_PREFIX + cid, JSON.stringify(d));
      } catch (_e) { /* 存不进（隐私模式配额等）＝退化为内存草稿，不阻断 */ }
      this._autosaveNote(true);   // P1：常驻「输入自动暂存中」微反馈
    }

    /* 重建后的 DOM 回填草稿值（顺序敏感：labelTouched → 解锁/会员下拉（会重排
       data-param-key 归属）→ 参数值 → 期限 → 参与度 → 滑杆读数）。 */
    _applyFormDraft() {
      const d = this._formDraft;
      if (!d || d.tid !== this._formTid || d.cid !== this._draftCid()) return;
      this._restoring = true;
      try {
        const sr = this.shadowRoot;
        this._labelTouched = !!d.labelTouched;
        const us = sr.querySelector('select[data-chg="unlock_sel"]');
        if (us && d.unlockSel) {
          us.value = d.unlockSel;
          if (us.value === d.unlockSel) this._onUnlockSel(us);
        }
        const tsel = sr.querySelector('select[data-chg="tier_sel"]');
        if (tsel && d.tierSel) {
          tsel.value = d.tierSel;
          if (tsel.value === d.tierSel) this._tierSel = d.tierSel;
        }
        Object.keys(d.params || {}).forEach((k) => {
          if (!/^[\w.-]+$/.test(k)) return;   // 键来自模板注册表；防御非常规键进选择器
          const el = sr.querySelector(`[data-param-key="${k}"]`);
          if (el && d.params[k] != null) el.value = String(d.params[k]);
        });
        this._syncSlotChips();   // slots 隐藏域回填后同步 chips 选中态
        const tip = this._ref("disc_tip");
        if (tip && d.params && d.params.note != null) {
          tip.hidden = !DISCOVERY_NOTE_RE.test(String(d.params.note));
        }
        const dy = this._ref("days");
        if (dy && d.days) dy.value = String(d.days);
        if (d.pace) this._formPace = this._normPace(d.pace);
        if (d.autonomy && AUTONOMY.indexOf(d.autonomy) >= 0) this._setAutonomy(d.autonomy);
        this._updatePaceLine();
        const rg = sr.querySelector('input[data-chg="range"]');
        if (rg) {
          const out = this._ref("rangeval");
          if (out) out.textContent = String(rg.value);
        }
      } finally { this._restoring = false; }
    }

    /* 弹层内轻提示（背板误触反馈，琥珀警示）：只改 DOM 不重渲染——重渲染会丢
       输入焦点。警示退场后回落 P1 的常驻「自动暂存中」灰字微反馈。 */
    _modalHint(text) {
      const el = this._ref("mhint");
      if (!el) return;
      el.textContent = String(text || "");
      el.classList.add("warn");
      el.classList.remove("dim");
      el.hidden = !text;
      if (this._mhintTimer) clearTimeout(this._mhintTimer);
      if (text) {
        this._mhintTimer = setTimeout(() => {
          this._mhintTimer = null;
          const cur = this._ref("mhint");
          if (cur) { cur.hidden = true; cur.classList.remove("warn"); }
          this._autosaveNote(this._formDirty());
        }, 4000);
      }
    }

    /* P1：常驻自动暂存微反馈（footer 左侧灰字）——「输入是安全的」要说出来才算数；
       警示提示占用期间不覆盖。DOM 级显隐，零重渲染。 */
    _autosaveNote(on) {
      if (this._mhintTimer) return;
      const el = this._ref("mhint");
      if (!el) return;
      el.classList.remove("warn");
      el.classList.toggle("dim", !!on);
      el.textContent = on ? this.t("inbox.goal.form.autosave_note") : "";
      el.hidden = !on;
    }

    /* P1：弹层焦点陷阱——Tab 只在弹层内循环（aria-modal 对话框语义），
       不许跑进被遮罩盖住的工作台。 */
    _trapModalTab(e) {
      const modal = this.shadowRoot.querySelector(".gl-modal");
      if (!modal) return;
      const foc = Array.from(modal.querySelectorAll(
        "button, input, select, textarea, summary, [tabindex]"))
        .filter((n) => !n.disabled && n.getAttribute("tabindex") !== "-1"
          && n.offsetParent !== null);
      if (!foc.length) return;
      const first = foc[0];
      const last = foc[foc.length - 1];
      const cur = this.shadowRoot.activeElement;
      if (!modal.contains(cur)) { e.preventDefault(); first.focus(); return; }
      if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); return; }
      if (e.shiftKey && cur === first) { e.preventDefault(); last.focus(); }
    }

    /* P1：弹层初始聚焦（新开＝第一个可输入控件）/ 焦点回位（重建后回编辑位）。 */
    _focusModal(justOpened) {
      const sr = this.shadowRoot;
      let target = null;
      if (!justOpened && this._lastFocusRef) {
        target = this._lastFocusRef === "__days__"
          ? this._ref("days")
          : (/^[\w.-]+$/.test(this._lastFocusRef)
            ? sr.querySelector(`.gl-modal [data-param-key="${this._lastFocusRef}"]`)
            : null);
      }
      if (!target) {
        target = sr.querySelector(
          '.gl-mbody textarea, .gl-mbody input:not([type="hidden"]), .gl-mbody select');
      }
      if (!target) return;
      try { target.focus({ preventScroll: !justOpened }); }
      catch (_e) { try { target.focus(); } catch (_e2) { /* soft */ } }
    }

    /* 任何整块重渲染（refresh / _rerender 两条路径都经 _render）之后：
       先拍「出厂快照」（草稿应用前＝判脏基准），再回填草稿——表单值在重建中幸存；
       再补 P1 体验层：自动暂存微反馈 + 初始聚焦/焦点回位。 */
    _render(html) {
      super._render(html);
      if (this._formOpen && this._formStep === 2) {
        const justOpened = !this._modalWasOpen;
        this._modalWasOpen = true;
        if (!this._formBaseline || this._formBaseline.tid !== this._formTid) {
          this._formBaseline = this._formSnapshot();
        }
        this._applyFormDraft();
        this._autosaveNote(this._formDirty());
        this._focusModal(justOpened);
      } else {
        this._modalWasOpen = false;
      }
    }

    /* 编辑防打断硬不变量：表单打开期，同会话的宿主重喂（轮询身份合并/peer 解析/
       回前台补轮）只更新 _ctx 并挂起刷新，表单关闭后补刷；换会话不受此限（走正常
       refresh → fetchData 会重置表单，草稿已在 sessionStorage，回来可续写）。 */
    get context() { return this._ctx; }
    set context(ctx) {
      const prev = this._ctx;
      const sameCid = !!(prev && ctx && prev.conversationId
        && prev.conversationId === ctx.conversationId);
      this._ctx = ctx;
      if (sameCid && this._formOpen) {
        if (!this._ctxDeferred) _beacon("goal_ctx_deferred");   // 每次编辑会话只记一次
        this._ctxDeferred = true;
        return;
      }
      this.refresh();
    }

    async refresh() {
      this._ctxDeferred = false;   // 真刷新即清挂起标记（create 成功等内部刷新同样收口）
      return super.refresh();
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
        this._loadBenchAll();   // fire-and-forget：场景卡 30 天基准线（到货补渲染）
        this._formOpen = true;
        this._formStep = 1;
        this._formTid = "";
        this._formAutonomy = "";
        this._formPace = "natural";
        this._formUsePrefDays = true;
        this._resetParamState();
        this._formBaseline = null;
        // 断点续写：本会话有未提交草稿（含页面刷新前留下的）→ 直接回到第二步续写
        const dr = this._loadDraftStore();
        this._formDraft = dr;
        if (dr && dr.tid && this._tmplById(dr.tid)) {
          this._formTid = dr.tid;
          this._formStep = 2;
          this._formUsePrefDays = false;   // 期限以草稿为准（回填在 _applyFormDraft）
          if (dr.pace) this._formPace = this._normPace(dr.pace);
          if (this._needsCatalog(dr.tid) && this._catalog == null && !this._catalogTried) {
            await this._loadCatalog();
          }
          _beacon("goal_form_draft_restore");
        }
        this._rerender();
        return;
      }
      if (act === "close_form") {
        this._formOpen = false;
        this._formStep = 1;
        // 编辑期挂起的外部刷新此刻补上（数据新鲜度在表单关闭后立即恢复）
        if (this._ctxDeferred) { this.refresh(); return; }
        this._rerender();
        return;
      }
      if (act === "signal_dismiss") {
        // ✕＝本会话对「这条及更早」的买家信号不再提（sessionStorage 存信号 ts）
        try {
          root.sessionStorage && root.sessionStorage.setItem(
            "cp_goal_sig_dismiss:" + this._draftCid(),
            String((el && el.getAttribute("data-ts")) || (Date.now() / 1000)));
        } catch (_e) { /* ignore */ }
        _beacon("goal_signal_dismiss");
        this._rerender();
        return;
      }
      if (act === "open_form_sprint") {
        // 买家信号直达：custom + 今天收口，跳过第一步（趁热收口的最短路径）
        if (el) el.disabled = true;
        _beacon("goal_signal_open");
        if (!this._templates) await this._loadTemplates();
        this._loadBenchAll();
        this._formOpen = true;
        if (!this._templates || !this._tmplById("custom")) {
          this._formStep = 1;          // 模板取数失败 → 回落普通第一步
          this._formTid = "";
          this._rerender();
          return;
        }
        this._formTid = "custom";
        this._formStep = 2;
        this._formPace = this._sprintOk(this._tmplById("custom"))
          ? "today" : "natural";
        this._formAutonomy = "";
        this._formUsePrefDays = false;
        this._resetParamState();
        this._formBaseline = null;
        const drs = this._loadDraftStore();
        this._formDraft = drs;
        if (drs && drs.tid === "custom" && drs.pace) {
          this._formPace = this._normPace(drs.pace);
        }
        this._rerender();
        return;
      }
      if (act === "ov_dismiss") {
        // 背板点击：干净表单＝照旧回第一步；已有输入＝不关闭（误触是丢内容主因），
        // 就地提示已自动暂存 + 指路显式退出（返回/Esc）。
        if (this._formDirty()) {
          _beacon("goal_form_backdrop_dirty");
          this._draftCapture();   // input 事件已实时捕获，这里双保险
          this._modalHint(this.t("inbox.goal.form.draft_saved_hint"));
          return;
        }
        this.onAction("form_back", null);
        return;
      }
      if (act === "draft_clear") {
        this._formDraft = null;
        this._clearDraftStore();
        this._formBaseline = null;
        this._resetParamState();
        this._formAutonomy = "";
        const _prefs = this._loadPrefs();
        const sprint = this._sprintOk(this._tmplById(this._formTid));
        if (!sprint) this._formPace = "natural";
        else if (_prefs.template === this._formTid && PACES.indexOf(_prefs.pace) >= 0)
          this._formPace = _prefs.pace;
        else this._formPace = "natural";
        this._formUsePrefDays = (_prefs.template === this._formTid && (
          (this._formPace === "natural" && _prefs.deadline_days > 0)
          || (this._formPace !== "natural" && _prefs.horizon > 0)));
        _beacon("goal_form_draft_clear");
        this._rerender();
        return;
      }
      if (act === "form_back") {
        // 弹层 → 回第一步（Esc/×/「返回」+ 干净背板同入口；草稿已在，重进同场景即恢复）
        this._formStep = 1;
        _beacon("goal_form_back");
        this._rerender();
        return;
      }
      if (act === "pick_tmpl" || act === "pick_custom") {
        const tid = act === "pick_custom" ? "custom"
          : ((el && el.getAttribute("data-tid")) || "");
        if (!tid || !this._tmplById(tid)) return;
        const prefs = this._loadPrefs();
        this._formTid = tid;
        this._formStep = 2;
        const sprint = this._sprintOk(this._tmplById(tid));
        if (!sprint) this._formPace = "natural";
        else if (prefs.template === tid && PACES.indexOf(prefs.pace) >= 0)
          this._formPace = prefs.pace;
        else this._formPace = "natural";
        // 期限偏好只在「又选了上次那个场景」且节奏对得上时回放；换场景跟场景默认
        this._formUsePrefDays = (prefs.template === tid && (
          (this._formPace === "natural" && prefs.deadline_days > 0)
          || (this._formPace !== "natural" && prefs.horizon > 0)));
        this._resetParamState();
        this._formBaseline = null;   // 换场景重拍出厂快照
        if (this._formDraft && this._formDraft.tid === tid) {
          this._formUsePrefDays = false;
          if (this._formDraft.pace) this._formPace = this._normPace(this._formDraft.pace);
        }
        _beacon(tid === "custom" ? "goal_form_pick_custom" : "goal_form_pick_scenario");
        // 解锁项/会员档需要价目表：先取再开弹层（避免控件回落后再替换闪烁）
        if (this._needsCatalog(tid) && this._catalog == null && !this._catalogTried) {
          await this._loadCatalog();
        }
        this._rerender();
        return;
      }
      if (act === "pick_autonomy") {
        this._setAutonomy(String((el && el.getAttribute("data-lvl")) || ""));
        this._draftCapture();   // 参与度点选走 DOM 级切换不触发 input/change，手动进草稿
        return;
      }
      if (act === "pick_pace") {
        const p = this._normPace(el && el.getAttribute("data-pace"));
        if (!this._sprintOk(this._tmplById(this._formTid))) return;
        if (p === this._formPace) return;
        this._formPace = p;
        if ((p === "today" || p === "session") && this._formAutonomy === "observe") {
          this._formAutonomy = "suggest";
        }
        const tmpl = this._tmplById(this._formTid) || {};
        const def = String(this._defaultHorizon(p, tmpl));
        if (this._formDraft && this._formDraft.tid === this._formTid) {
          this._formDraft.pace = p;
          this._formDraft.days = def;
        }
        this._formUsePrefDays = false;
        this._formBaseline = null;
        _beacon("goal_pick_pace");
        this._rerender();
        return;
      }
      if (act === "horizon_chip") {
        const v = String((el && el.getAttribute("data-val")) || "");
        const dy = this._ref("days");
        if (dy && v) { dy.value = v; this._draftCapture(); this._updatePaceLine(); }
        return;
      }
      if (act === "horizon_today_end") {
        const dy = this._ref("days");
        if (dy) {
          dy.value = String(this._hoursUntilMidnight());
          this._draftCapture();
          this._updatePaceLine();
        }
        return;
      }
      if (act === "extend_30m") { await this._extendDeadline(30 * 60); return; }
      if (act === "extend_2h") { await this._extendDeadline(2 * 3600); return; }
      if (act === "pick_force") {
        const f = (el && el.getAttribute("data-force")) === "max"
          ? "max" : "steady";
        if (f === this._formForce) return;
        this._formForce = f;
        _beacon("goal_pick_force_" + f);
        this._rerender();
        return;
      }
      if (act === "sprint_nudge") { await this._sprintNudge(el); return; }
      if (act === "revive_won") { await this._reviveWon(el); return; }
      if (act === "jump_sprint") { await this._jumpSprint(el); return; }
      if (act === "note_example") {
        const i = (el && el.getAttribute("data-ex")) || "";
        const tx = this.t("inbox.goal.form.note_ex" + i);
        if (!tx || String(tx).indexOf("inbox.goal.") === 0) return;
        const ta = this.shadowRoot.querySelector('textarea[data-param-key="note"]');
        if (ta) {
          ta.value = String(tx);
          try { ta.focus(); } catch (_e) { /* soft */ }
          const tip = this._ref("disc_tip");
          if (tip) tip.hidden = !DISCOVERY_NOTE_RE.test(String(tx));
          this._draftCapture();   // 程序化填入不触发 input 事件，手动进草稿
        }
        _beacon("goal_note_example");
        return;
      }
      if (act === "slot_toggle") {
        this._toggleSlotChip((el && el.getAttribute("data-slot")) || "");
        _beacon("goal_slot_toggle");
        return;
      }
      if (act === "slot_custom_add") {
        this._addCustomSlot();
        return;
      }
      if (act === "switch_discovery") {
        // 自定义 → 客户摸底：保留 note 作补充方向，按 note 关键词预勾槽位
        if (!this._tmplById("profile_discovery")) return;
        const ta = this.shadowRoot.querySelector('textarea[data-param-key="note"]');
        const note = ta ? String(ta.value || "") : "";
        const slots = this._inferSlotsFromNote(note);
        this._formTid = "profile_discovery";
        this._formStep = 2;
        this._formPace = "natural";
        this._formUsePrefDays = false;
        this._resetParamState();
        this._formBaseline = null;
        this._formDraft = {
          v: 1, cid: this._draftCid(), tid: "profile_discovery", ts: Date.now(),
          params: { slots: slots.join(","), note: note },
          days: "", autonomy: this._formAutonomy || "auto", pace: "natural",
          unlockSel: "", tierSel: "", labelTouched: false,
        };
        _beacon("goal_switch_discovery");
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
      if (act === "chain_retention") {
        // 达成的转化目标 → 一键接续留存目标（2026-08-18）：参数继承上单
        // （product_id/last_plan/base_goal），创建仍走表单第二步人工确认——
        // 与 service 的订单自动链同语义，这里补的是人工成交/无订单 ref 场景。
        _beacon("goal_chain_retention");
        if (!this._templates) await this._loadTemplates();
        if (!this._tmplById("retention_expand")) return;
        const src = (this._d && (this._d.goal || this._d.last)) || {};
        const p = (src && src.params) || {};
        this._formOpen = true;
        this._formStep = 2;
        this._formTid = "retention_expand";
        this._formAutonomy = "";
        this._formUsePrefDays = false;
        this._resetParamState();
        this._formBaseline = null;
        this._formDraft = {
          v: 1, cid: this._draftCid(), tid: "retention_expand", ts: Date.now(),
          params: {
            product_id: String(p.product_id || p.item_id || ""),
            last_plan: String(p.last_plan || ""),
            base_goal: String(src.goal_id || ""),
          },
          days: "", autonomy: this._formAutonomy || "auto",
          unlockSel: "", tierSel: "", labelTouched: false,
        };
        this._rerender();
        return;
      }
      if (act === "notify_connect") {
        // 宿主外壳的告警接通弹窗（master/operator 才有；坐席侧按钮本就不渲染）
        _beacon("goal_notify_connect");
        try {
          if (typeof root.wsAlertlinkOpen === "function") root.wsAlertlinkOpen();
        } catch (_e) { /* soft */ }
        return;
      }
      if (act === "notify_bind_open") {
        this._nbOpen = true;
        this._nbMsg = "";
        this._nbVal = "";
        _beacon("goal_notify_bind_open");
        this._rerender();
        setTimeout(() => {
          const i = this._ref("nb_chat");
          if (i) { try { i.focus(); } catch (_e) { /* soft */ } }
        }, 30);
        return;
      }
      if (act === "notify_bind_close") {
        this._nbOpen = false;
        this._nbMsg = "";
        this._rerender();
        return;
      }
      if (act === "notify_bind_save") { await this._nbSave(); return; }
      if (act === "notify_extra_toggle") {
        this._neOpen = !this._neOpen;
        this._neMsg = "";
        if (this._neOpen) {
          _beacon("goal_notify_extra_open");
          this._rerender();
          setTimeout(() => {
            const i = this._ref("ne_input");
            if (i) { try { i.focus(); } catch (_e) { /* soft */ } }
          }, 30);
        } else {
          this._neVal = null;
          this._rerender();
        }
        return;
      }
      if (act === "notify_extra_close") {
        this._neOpen = false;
        this._neMsg = "";
        this._neVal = null;
        this._rerender();
        return;
      }
      if (act === "notify_extra_save") { await this._neSave(); return; }
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
      if (act === "prog_retry") { await this._loadProgress(true, this._progGoalId || ""); return; }
      if (act === "beat_jump") { this._jumpBeat(el); return; }
      if (act === "settle_toggle") {
        // M-7 C：结算摘要展开/收起（按目标 id 记，切目标自然收起）
        const gid = String(el.getAttribute("data-gid") || "");
        this._settleOpen = (this._settleOpen === gid) ? "" : gid;
        if (this._settleOpen) _beacon("goal_settlement_open");
        this._rerender();
        return;
      }
      if (act === "prog_toggle_gid") {
        const gid = String(el.getAttribute("data-gid") || "");
        if (this._progOpen && this._progGoalId === gid) { this._progOpen = false; this._rerender(); return; }
        this._progOpen = true;
        _beacon("goal_progress_open");
        await this._loadProgress(false, gid);
        return;
      }
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
      if (act === "deadline_toggle") {
        const g0 = this._d && this._d.goal;
        if (!g0 || !g0.goal_id) return;
        const p0 = String(g0.pace || "natural");
        if (p0 === "today" || p0 === "session") return;
        this._moreOpen = false;
        this._deadlineOpen = !this._deadlineOpen;
        this._deadlineForGid = String(g0.goal_id || "");
        this._deadlineVal = null;      // 重开时回读当前期限
        if (this._deadlineOpen) {
          _beacon("goal_deadline_open");
          this._loadBenchmark(g0.template);   // fire-and-forget，取到再补渲染
        }
        this._rerender();
        return;
      }
      if (act === "due_wrap" || act === "due_extend") {
        // 临期快捷动作：今天收口（=当前天数）/ 延长 7 天（走同一 POST 出口）
        const g1 = this._d && this._d.goal;
        if (!g1 || !g1.goal_id) return;
        const day1 = Math.max(1, parseInt(g1.day_index, 10) || 1);
        const total1 = parseInt(g1.total_days, 10) || day1;
        const days = act === "due_wrap" ? day1 : Math.min(180, total1 + 7);
        if (el) el.disabled = true;
        _beacon(act === "due_wrap" ? "goal_due_wrap" : "goal_due_extend");
        const ok = await this._postDeadline(days);
        if (!ok) {
          if (el) el.disabled = false;
          this._flashToast(this.t("inbox.goal.err_retry"));
          this._rerender();
        }
        return;
      }
      if (act === "deadline_chip") {
        const d = parseInt((el && el.getAttribute("data-days")) || "", 10);
        if (isFinite(d)) this._deadlineVal = d;
        this._rerender();
        return;
      }
      if (act === "deadline_cancel") {
        this._deadlineOpen = false;
        this._deadlineVal = null;
        this._rerender();
        return;
      }
      if (act === "deadline_save") { await this._saveDeadline(el); return; }
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
        // 从「标记达成」核对清单点「补录」过来：先收起达成表单，否则画像编辑区不渲染
        this._wonFormOpen = false;
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
        const typ = inp.getAttribute("type");
        params[k] = ((typ === "number" || typ === "range") && isFinite(num)) ? num : v;
      });
      const body = { conversation_id: ctx.conversationId, template: this._formTid, params };
      const aSel = this._ref("autonomy");
      if (aSel && aSel.value) body.autonomy = String(aSel.value);
      const daysEl = this._ref("days");
      const raw = daysEl ? parseFloat(daysEl.value) : 0;
      const pace = this._sprintOk(this._tmplById(this._formTid))
        ? this._normPace(this._formPace) : "natural";
      body.pace = pace;
      if (isFinite(raw) && raw > 0) {
        if (pace === "session") body.deadline_days = raw / 1440;
        else if (pace === "today") body.deadline_days = raw / 24;
        else body.deadline_days = raw;
      }
      if ((pace === "today" || pace === "session") && body.autonomy === "observe") {
        body.autonomy = "suggest";
      }
      // 冲刺附加参数（P3 2026-08-30）：全力档 + 插队回程票（仅限时档携带）
      if (pace !== "natural") {
        if (this._formForce === "max") params.sprint_mode = "max";
        if (this._resumeGoalId) params.resume_goal_id = this._resumeGoalId;
      }

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
        const prev = this._loadPrefs();
        const saved = {
          template: this._formTid,
          autonomy: body.autonomy || "auto",
          pace: pace,
        };
        if (pace === "natural") {
          saved.deadline_days = body.deadline_days || 0;
          if (typeof prev.horizon === "number") saved.horizon = prev.horizon;
        } else {
          saved.horizon = raw;
          saved.deadline_days = (typeof prev.deadline_days === "number" && prev.deadline_days > 0)
            ? prev.deadline_days : 0;
        }
        this._savePrefs(saved);
        this._formOpen = false;
        this._formStep = 1;
        this._formDraft = null;          // 已提交＝草稿使命完成
        this._clearDraftStore(ctx.conversationId);
        this._formBaseline = null;
        this._resumeGoalId = "";         // 回程票已随目标入库
        this._jumpOffer = false;
        // 一次性「接下来会发生什么」提示（按所选自治档如实说明，消除「建完然后呢」断崖）
        this._createdHintAutonomy = String(body.autonomy || "auto");
        this._createdHintTid = String(this._formTid || "");
        // P3 2026-08-13：后端开了 goal_auto_attach 且已自动挂上推荐链 →
        // 直接渲染「已挂上 ✓」（再出启动按钮会撞 chain_already_running 409）；
        // 未自动挂（开关关/无推荐/闸未过）→ 旧行为：查推荐补按钮行。
        const att = res.data.auto_attached_chain;
        if (att && att.chain_id) {
          this._chainReco = {
            chain: { chain_id: String(att.chain_id), name: String(att.name || att.chain_id) },
            state: "started", err: "",
          };
          _beacon("goal_chain_auto_attached");
        } else {
          this._chainReco = null;
          this._loadChainReco();   // fire-and-forget：查到配套链后原位补一行推荐
        }
        _beacon("goal_create_ok");
        _beacon("goal_create_ok_" + String(this._formTid || ""));  // 每模板漏斗
        this.emit("cp-goal-changed", { action: "create", conversationId: ctx.conversationId });
        this.refresh();
        return;
      }
      if (btn) btn.disabled = false;
      // 撞每会话活跃上限 + 正在建冲刺（P3 2026-08-30）→ 出「暂停长线插队」
      // 提议行（重渲染表单带出按钮；ferr 文案照常显示后端 detail）
      if (res && res.status === 409 && pace !== "natural" && !this._jumpOffer) {
        this._jumpOffer = true;
        this._rerender();
      }
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
          body: JSON.stringify({
            verdict,
            day: (g.today && g.today.day) ? String(g.today.day) : undefined,
          }),
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
        // 展示态（en UI 用；指令 text/intent 保持中文权威口径不变）
        intentDisplay: intentDisp(beat) || String(intent || ""),
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
        const outcome = this._ref("won_outcome");
        const note = this._ref("won_note");
        const meta = {};
        if (product && String(product.value || "").trim()) meta.product = String(product.value).trim();
        if (amount && String(amount.value || "").trim()) {
          const n = parseFloat(amount.value);
          if (isFinite(n)) meta.amount = n;
        }
        // 陪伴域「标记达成」：达成结果 + 备注（N-3 #241）
        if (outcome && String(outcome.value || "").trim()) meta.outcome = String(outcome.value).trim();
        if (note && String(note.value || "").trim()) meta.note = String(note.value).trim();
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
          this._formStep = 1;
          this._formTid = "";
          this._formAutonomy = "";
          this._formPace = "natural";
          this._formUsePrefDays = true;
          this._resetParamState();
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
