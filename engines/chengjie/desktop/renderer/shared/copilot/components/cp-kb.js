"use strict";
/* 两端共享 · 知识库 / 快捷回复 cp-kb（P1-4 第二刀收编；P0 改版 + P1 编辑闭环 2026-08-18）
   收编桌面原生 aside 的「知识库」「快捷回复」两卡为统一 App 的一张卡——原生面板
   退役的能力前置（web 原生右栏刻意不挂：composer 的 / 指令面板与 KB 自动推荐浮层
   已承担同职能，右栏再放一份=双入口）。
   - 装载即取快捷回复（GET /api/unified-inbox/templates，一次性；响应含个人常用语
     source="mine" + 团队话术 + can_edit 权限探针）；
   - 输入即本地过滤；回车 / 点「搜」才打 GET /api/unified-inbox/kb-search
     （代际守卫，慢响应不覆盖新查询）；
   - 「填入」→ emit('cp-fill')（宿主整体替换 composer——填入必须是显式按钮，
     整卡点击只做展开，防误触覆盖坐席打了一半的稿子）。
   P0 要点（修「greeting #2 看不懂 + 视觉单调 + 点了没反馈」）：
   - 显示名治理主责在后端聚合层（tp_nm_* 词条 + 系统模板准入剔除）；本组件 _curate
     是**旧后端桥**——label 仍是机器键名形态时就地治理，后端装载后自动休眠；
   - 卡片点击展开全文（clamp 2 行）、填入按钮 900ms「✓ 已填入」、分类色左边条、
     团队区前 10 条+展开全部、KB 命中高置信绿点、骨架屏；
   - 用量埋点 cpkb_*（sendBeacon /api/telemetry/ui-event；file:// 宿主静默跳过）。
   P1 要点（编辑闭环，全部特性探测——旧后端 can_edit 缺席 / 旧适配器缺方法时入口隐藏）：
   - 「我的常用语」坐席私有层：POST /api/unified-inbox/quick-replies 增删改
     （服务端 KV + 上限/去重/长度守卫），新增排最前、两击确认删除；
   - 团队话术内联编辑：仅 can_edit（master/admin 或桌面 token 链）显示 ✎，写走
     admin 既有 PUT /api/templates/{key}（快照/审计/热失效免费）——读改回写用
     「原文精确匹配」定位条目，被并发改过 → 提示冲突并自动刷新，绝不盲覆盖；
   - 变量模板（含 {order_number} 等占位符）填入前先弹变量补全行，杜绝把
     花括号原样发给客户；检测源无关（个人/团队/KB 命中一视同仁）。
   P2 要点（用量机制先上线，样本门槛当等待期，2026-08-18 同日）：
   - refresh 会话无关化：换会话不再重拉重渲（保住编辑表单/过滤词），增删改走
     _reload 显式回源；
   - 用量记账：团队话术填入按键遥测 cpkb_use_<key>（有界集合），个人常用语只记
     localStorage 本地频次并驱动本机「高频靠前」排序（id 无界绝不按条上报）；
     周读裁决 CLI＝tools/quickreply_usage_report.py（纪元 2026-08-18）；
   - 新增表单「引用输入框内容」：同源 App 模式只读宿主 composer 预填（读≠写，
     写仍走 cp-fill 契约；PiP/桌面原生读不到→按钮隐藏）；
   - KB 命中行 can_edit 下带「去知识库编辑」深链。
   client 需实现:replyTemplates / kbSearch（缺失→空态优雅降级）；
   编辑能力需 quickReplyMutate / teamTemplates / teamTemplateUpdate（缺失→隐藏）。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  const VISIBLE_N = 10;
  const MINE_MAX_CHARS = 500;
  const MACHINE_RE = /^[a-z0-9_.]+(?: #\d+)?$/;
  const VAR_RE = /\{[A-Za-z0-9_]+\}/;
  const VAR_ALL_RE = /\{([A-Za-z0-9_]+)\}/g;
  const CLAUSE_RE = /[，。！？!?；;：:,…~～\n]/;
  /* 旧后端桥的分类回落表（新后端直接回 category 字段，此表随桥休眠） */
  const CAT_FALLBACK = {
    greeting: "greet", welcome: "greet", farewell: "greet",
    order_query: "order", order_confirm: "order", status_check: "order",
    status_query: "order", payment_remind: "order",
    price_check: "biz", rate_query: "biz", rate_update: "biz",
    channel_info: "biz", channel_enable: "biz", channel_disable: "biz",
    complaint: "aftersale", help: "aftersale", maintenance: "aftersale",
    small_talk: "chat", small_talk_short: "chat",
  };

  /* P2：个人常用语「本地使用频次」（localStorage，只影响本机排序，零服务端）。
     个人条目 id 无界（文本指纹）——绝不按条上报遥测；团队话术才按键计数（见 _noteUse）。 */
  const USE_LS_KEY = "cpkb_mine_use_v1";
  const USE_LS_CAP = 60;
  const TEAM_KEY_RE = /^[a-z][a-z0-9_]{0,23}$/;
  function readLocalUse() {
    try { return JSON.parse(localStorage.getItem(USE_LS_KEY) || "{}") || {}; }
    catch (_e) { return {}; }
  }
  function bumpLocalUse(id) {
    if (!id) return;
    try {
      const m = readLocalUse();
      const it = m[id] || { n: 0, ts: 0 };
      it.n = (it.n || 0) + 1;
      it.ts = Date.now();
      m[id] = it;
      const ids = Object.keys(m);
      if (ids.length > USE_LS_CAP) {
        ids.sort((a, b) => ((m[a] && m[a].ts) || 0) - ((m[b] && m[b].ts) || 0));
        for (let i = 0; i < ids.length - USE_LS_CAP; i++) delete m[ids[i]];
      }
      localStorage.setItem(USE_LS_KEY, JSON.stringify(m));
    } catch (_e) { /* 本地计数失败不阻断填入 */ }
  }

  class CpKb extends Base {
    styles() {
      return `
      .srch { display:flex; gap:6px; align-items:center; }
      .srch .qwrap { flex:1; min-width:0; position:relative; }
      .srch input { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
                    border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
                    background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b);
                    padding:6px 26px 6px 9px; transition:border-color var(--cp-dur,.2s); }
      .srch input:focus { outline:none; border-color:var(--cp-accent,#6366f1); }
      .srch input::placeholder { color:var(--cp-text-tiny,#94a3b8); }
      .srch .qx { position:absolute; right:2px; top:50%; transform:translateY(-50%);
                  display:none; border:none; background:transparent; padding:2px 7px;
                  font-size:13px; line-height:1; color:var(--cp-text-tiny,#94a3b8); cursor:pointer; }
      .srch .qx.show { display:block; }
      .srch .qx:hover { color:var(--cp-text,#1e293b); }
      .srch .go { background:var(--cp-accent,#6366f1); color:#fff; border-color:transparent;
                  font-size:var(--cp-fs-sm,12px); padding:6px 13px; font-weight:var(--cp-fw-medium,500); }
      .srch .go:hover { background:var(--cp-accent-hover,#818cf8); }
      .sec-hd { display:flex; align-items:center; gap:6px; margin:10px 0 6px; }
      .sec-ttl { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,700);
                 color:var(--cp-text,#1e293b); }
      .sec-n { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
               background:var(--cp-bg-soft,#f1f5f9); border-radius:8px; padding:0 7px; line-height:16px; }
      .sec-hd .add { margin-left:auto; color:var(--cp-accent-deep,#4f46e5);
                     background:transparent; border-style:dashed; padding:2px 9px; }
      .sec-hd .add:hover { background:var(--cp-accent-weak,rgba(99,102,241,.07));
                           border-color:var(--cp-accent,#6366f1); }
      .qc { cursor:pointer; margin-bottom:6px; padding:7px 9px;
            transition:background var(--cp-dur,.2s), border-color var(--cp-dur,.2s); }
      .qc:hover { background:var(--cp-accent-weak,rgba(99,102,241,.07)); }
      .qc .ttl { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-sm,12px);
                 font-weight:var(--cp-fw-bold,700); color:var(--cp-text,#1e293b); margin-bottom:2px; }
      .qc .bd { font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); line-height:1.5;
                word-break:break-word; display:-webkit-box; -webkit-line-clamp:2;
                -webkit-box-orient:vertical; overflow:hidden; }
      .qc.expanded .bd { display:block; -webkit-line-clamp:unset; overflow:visible;
                         color:var(--cp-text,#1e293b); }
      .qc .acts { margin-top:5px; display:flex; gap:6px; align-items:center; }
      .qc .fill { color:var(--cp-accent-deep,#4f46e5); background:var(--cp-accent-bg,rgba(99,102,241,.10));
                  border-color:transparent; font-weight:var(--cp-fw-medium,500); padding:3px 11px; }
      .qc .fill:hover { background:var(--cp-accent,#6366f1); color:#fff; }
      .qc .fill.done { color:var(--cp-ok,#16a34a); background:var(--cp-ok-bg,rgba(22,163,74,.12)); }
      .qc .edit-b { color:var(--cp-text-dim,#64748b); background:transparent;
                    border-color:transparent; padding:3px 7px; }
      .qc .edit-b:hover { color:var(--cp-accent-deep,#4f46e5);
                          background:var(--cp-accent-weak,rgba(99,102,241,.07)); }
      .qc.c-greet { border-left-color:var(--cp-accent,#6366f1); }
      .qc.c-order { border-left-color:var(--cp-gold,#b45309); }
      .qc.c-biz { border-left-color:var(--cp-goal-disc,#0d9488); }
      .qc.c-aftersale { border-left-color:var(--cp-warn,#d97706); }
      .qc.c-chat { border-left-color:var(--cp-violet,#7c3aed); }
      .qc.c-mine { border-left-color:var(--cp-ok,#16a34a); }
      .qc.c-kb { border-left-color:var(--cp-goal-eng,#0284c7); }
      .conf { width:6px; height:6px; border-radius:50%; flex:none;
              background:var(--cp-ok,#16a34a); box-shadow:0 0 0 3px var(--cp-ok-bg,rgba(22,163,74,.15)); }
      .more { width:100%; text-align:center; margin-top:2px; background:transparent;
              border-style:dashed; color:var(--cp-text-dim,#64748b); padding:5px 9px; }
      .more:hover { color:var(--cp-accent-deep,#4f46e5); border-color:var(--cp-accent,#6366f1); }
      .kb-res { margin-top:8px; }
      .kb-res:empty { margin-top:0; }
      .stat { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); padding:6px 2px; }
      .sk { height:34px; border-radius:var(--cp-radius-sm,6px); margin-bottom:5px;
            background:linear-gradient(90deg, var(--cp-surface-2,#f1f5f9) 25%,
                       var(--cp-border,#e2e8f0) 50%, var(--cp-surface-2,#f1f5f9) 75%);
            background-size:200% 100%; animation:cpkbsk 1.1s linear infinite; }
      @keyframes cpkbsk { from { background-position:200% 0; } to { background-position:-200% 0; } }
      .hint { margin-top:8px; font-size:10px; color:var(--cp-text-tiny,#94a3b8); line-height:1.5; }
      .edform { cursor:default; }
      .edform textarea { width:100%; box-sizing:border-box; min-height:64px; resize:vertical;
                         font:inherit; font-size:var(--cp-fs-sm,12px); line-height:1.5;
                         border:1px solid var(--cp-accent,#6366f1); border-radius:var(--cp-radius-sm,6px);
                         background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:6px 8px; }
      .edform textarea:focus { outline:none; }
      .ed-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:3px;
                min-height:0; }
      .ed-cnt { font-size:10px; color:var(--cp-text-tiny,#94a3b8); margin-left:auto; }
      .vf { margin-top:5px; cursor:default; }
      .vf .vf-h { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:3px; }
      .vf input { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
                  border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
                  background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b);
                  padding:4px 8px; margin-bottom:4px; }
      .vf input:focus { outline:none; border-color:var(--cp-accent,#6366f1); }
      .vf input.bad { border-color:var(--cp-danger,#dc2626); }
      .vf .vf-name { font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .src-lnk { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 text-decoration:none; }
      .src-lnk:hover { color:var(--cp-accent-deep,#4f46e5); text-decoration:underline; }
      .lang-b { font-size:10px; color:var(--cp-text-dim,#64748b); background:var(--cp-bg-soft,#f1f5f9);
                border-radius:8px; padding:0 6px; line-height:15px; flex:none; cursor:help; }
      .lang-b.on { color:var(--cp-accent-deep,#4f46e5); background:var(--cp-accent-weak,rgba(99,102,241,.12)); }
      .i18n-prev { display:none; margin-top:4px; font-size:var(--cp-fs-sm,12px); line-height:1.5;
                   color:var(--cp-text,#1e293b); word-break:break-word;
                   border-top:1px dashed var(--cp-border,#e2e8f0); padding-top:4px; }
      .qc.expanded .i18n-prev { display:block; }
      .i18n-prev .ip-l { color:var(--cp-accent-deep,#4f46e5); font-size:10px; margin-right:4px; }
      .i18n-prev .ip-m { color:var(--cp-warn,#d97706); margin-right:4px; cursor:help; }`;
    }

    async fetchData(_ctx) {
      // 模板一次性拉取（会话无关）；kb 搜索由用户触发，不在装载路径上
      if (!this._client || !this._client.replyTemplates) return { ok: true, templates: [] };
      try {
        const d = await this._client.replyTemplates();
        return { ok: true, templates: (d && d.templates) || [], can_edit: d ? d.can_edit : undefined };
      } catch (_e) { return { ok: true, templates: [] }; }
    }

    /* P2：本卡数据与会话无关（模板/常用语全局一份）——换会话不重拉不重渲，
       保住打开中的编辑表单与过滤词（宿主每次切会话都 set context → refresh，
       旧行为=每次切会话白拉一次接口 + 把正在编辑的表单打掉）。首次装载与
       显式回源（_reload：增删改之后）仍走基类全流程。刻意接受的代价：后端
       热升级后的新字段要等页面刷新才可见——ui-build 陈旧页横幅本就会催刷新。 */
    async refresh() {
      const ctx = this._ctx;
      if (this._d && ctx && ctx.conversationId) return;
      return super.refresh();
    }

    async _reload() {
      this._d = null;
      return super.refresh();
    }

    _tplBody(t) {
      return typeof t === "string" ? t : (t.text || t.content || t.body || t.template || "");
    }

    _clause(text) {
      const raw = String(text || "").trim();
      const head = (raw.split(CLAUSE_RE)[0] || "").trim() || raw;
      return head.slice(0, 14);
    }

    /* V1：当前会话客户语言（宿主 cp-context.customer.language 喂入，旧宿主缺字段
       → "" → 变体选择整体休眠）。归一化取两位主码，与后端 normalize 同粒度。 */
    _convLang() {
      const l = String((((this._ctx || {}).customer || {}).language) || "").trim().toLowerCase();
      return l ? l.split("-")[0] : "";
    }

    _langName(code) {
      const k = "cp.lang." + String(code || "");
      const n = this.t(k);
      return n === k ? String(code || "") : n;
    }

    /* 旧后端桥：label 仍是机器键名时就地治理（后端聚合层已治理后此处自动休眠）。 */
    _curate(list) {
      const out = [];
      for (const t of (list || [])) {
        const text = String(this._tplBody(t) || "").trim();
        if (!text) continue;
        const source = (t && t.source) || "";
        let label = String((typeof t === "string" ? "" : (t.title || t.name || t.label)) || "").trim();
        let cat = String((t && t.category) || "").trim();
        if (source !== "mine" && label && MACHINE_RE.test(label)) {
          const m = label.match(/^(.*?) #(\d+)$/);
          const base = m ? m[1] : label;
          const n = m ? parseInt(m[2], 10) : 1;
          if (base.indexOf(".") >= 0) continue;               // error.general 等系统响应映射
          if (base === "test" || base.indexOf("gxp_") === 0) continue;  // 自检/机器人流程模板
          if (VAR_RE.test(text)) continue;                    // 旧后端无变量表单语义，先不展示
          label = this._clause(text) + (n > 1 ? this.t("cp.kb.alt_n", { n: n }) : "");
          if (!cat) cat = CAT_FALLBACK[base] || "";
        }
        if (!label) label = this._clause(text);
        out.push({
          label: label, text: text, source: source,
          category: cat || "custom",
          key: (t && t.key) || "",
          id: (t && t.id) || "",
        });
        if (out.length >= 80) break;
      }
      return out;
    }

    _beacon(action) {
      try {
        if (!navigator.sendBeacon) return;
        if (String(location.protocol).indexOf("http") !== 0) return; // 桌面原生 file:// 宿主：无同源后端，静默跳过
        const body = JSON.stringify({ page: "/copilot/kb", action: String(action || "") });
        navigator.sendBeacon("/api/telemetry/ui-event", new Blob([body], { type: "application/json" }));
      } catch (_e) { /* 埋点绝不阻塞主链 */ }
    }

    /* P2 用量记账（每次真实「填入」调用一次）：
       - 团队话术 → 按键遥测 `cpkb_use_<key>`（键=运营管理的有界集合，格式受
         _ACTION_RE 约束；trend 库另有日 distinct 150 封顶折叠保护）——
         周读 CLI 据此裁决「哪条常用、哪条清退」；
       - 个人常用语 → 只记 localStorage 本地频次（id 无界，绝不按条上报），
         驱动本机「高频靠前」排序。 */
    _noteUse(t) {
      if (!t) return;
      if (t.source === "mine") { bumpLocalUse(t.id); return; }
      if (t.source === "templates" && t.key && TEAM_KEY_RE.test(String(t.key))) {
        this._beacon("cpkb_use_" + String(t.key));
      }
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      this._all = this._curate(d.templates || []);
      // P2：个人常用语按本地使用频次排序（高频靠前，只影响本机视图）；并列保持
      // 服务端序（新存在前）。排序在渲染时刻定格——会话中连点不会当场重排（防跳动）。
      const use = readLocalUse();
      const mineRows = this._all.filter((t) => t.source === "mine");
      const restRows = this._all.filter((t) => t.source !== "mine");
      mineRows.sort((a, b) =>
        (((use[b.id] || {}).n || 0) - ((use[a.id] || {}).n || 0)));
      this._all = mineRows.concat(restRows);
      this._tpls = this._all; // 兼容旧字段名
      this._q = "";
      this._showAll = false;
      this._hits = null;
      this._edit = null;
      this._varFill = null;
      // P1 特性探测：can_edit 字段在 = 新后端已装载；编辑方法在 = 适配器有写通道。
      const c = this._client || {};
      this._mineReady = d.can_edit !== undefined && typeof c.quickReplyMutate === "function";
      this._canEdit = d.can_edit === true &&
        typeof c.teamTemplates === "function" && typeof c.teamTemplateUpdate === "function";
      // 整块重渲（含换会话）＝作废一切在途搜索：旧响应不得落进新渲染的结果区
      this._searchEpoch = (this._searchEpoch || 0) + 1;
      const mineSec = this._mineReady
        ? `<div class="tsec"><div class="sec-hd">` +
          `<span class="sec-ttl">${esc(this.t("cp.kb.mine_title"))}</span>` +
          `<span class="sec-n mine-n">0</span>` +
          `<button class="add" data-act="mine-add">${esc(this.t("cp.kb.add_btn"))}</button>` +
          `</div><div class="mine-list"></div></div>`
        : "";
      const html = `<div class="srch">` +
        `<span class="qwrap">` +
        `<input class="kb-q" type="text" placeholder="${esc(this.t("cp.kb.search_ph"))}" />` +
        `<button class="qx" data-act="q-clear" title="${esc(this.t("cp.kb.clear"))}">✕</button>` +
        `</span>` +
        `<button class="go" data-act="kb-go">${esc(this.t("cp.kb.search_btn"))}</button>` +
        `</div>` +
        `<div class="kb-res"></div>` +
        mineSec +
        `<div class="tsec">` +
        `<div class="sec-hd"><span class="sec-ttl">${esc(this.t("cp.kb.tpl_title"))}</span>` +
        `<span class="sec-n team-n">0</span></div>` +
        `<div class="tpl-list"></div>` +
        `</div>` +
        `<div class="hint">${esc(this.t("cp.kb.hint"))}</div>`;
      // 列表体在 _render 后由 _syncTplList 填充（与过滤/编辑态共用同一渲染路径）
      return html;
    }

    // ── 行渲染 ─────────────────────────────────────────────────────────

    _cardHtml(t, idx) {
      const esc = (s) => this.esc(s);
      const cat = String(t.category || "custom");
      const catName = this.t("cp.kb.cat_" + cat);
      const editable = (t.source === "mine" && this._mineReady) ||
        (this._canEdit && t.source === "templates" && t.key);
      const editBtn = editable
        ? `<button class="edit-b" data-act="ed-open" data-i="${idx}" title="${esc(this.t("cp.kb.edit_tip"))}">✎</button>`
        : "";
      // V1 多语言变体：徽章=有几种语言备稿；命中当前会话语言时点亮，「填入」直给
      // 该语言版（正文仍显示中文＝坐席读得懂的对照，展开态露外语预览与机器稿标注）。
      const cl = this._convLang();
      const langs = t.i18n ? Object.keys(t.i18n) : [];
      const v = (cl && cl !== "zh" && t.i18n && t.i18n[cl] && t.i18n[cl].text) ? t.i18n[cl] : null;
      const badge = langs.length
        ? `<span class="lang-b${v ? " on" : ""}" title="${esc(this.t("cp.kb.variant_langs", { langs: langs.join(", ") }))}">🌐${langs.length}</span>`
        : "";
      const prev = v
        ? `<div class="i18n-prev"><span class="ip-l">${esc(this._langName(cl))}</span>` +
          (v.approved ? "" : `<span class="ip-m" title="${esc(this.t("cp.kb.variant_machine"))}">◌</span>`) +
          `<span class="ip-t">${esc(v.text)}</span></div>`
        : "";
      const fillTip = v ? ` title="${esc(this.t("cp.kb.fill_lang_tip", { lang: this._langName(cl) }))}"` : "";
      const zhBtn = v
        ? `<button class="edit-b" data-act="fill-zh" data-i="${idx}">${esc(this.t("cp.kb.fill_zh"))}</button>`
        : "";
      const varForm = (this._varFill && this._varFill.kind === "tpl" && this._varFill.i === idx)
        ? this._varFormHtml((this._varFill.text || t.text), "tpl", idx) : "";
      return `<div class="card qc c-${esc(cat)}" data-act="tpl-x" data-i="${idx}"` +
        ` title="${esc(catName === "cp.kb.cat_" + cat ? "" : catName)}">` +
        `<div class="ttl">${esc(t.label || "")}${badge}</div>` +
        `<div class="bd">${esc(t.text || "")}</div>` + prev +
        (varForm || `<div class="acts">` +
          `<button class="fill" data-act="fill-tpl" data-i="${idx}"${fillTip}>${esc(this.t("cp.kb.fill"))}</button>` +
          zhBtn + editBtn + `</div>`) +
        `</div>`;
    }

    _editFormHtml() {
      const esc = (s) => this.esc(s);
      const ed = this._edit || {};
      const isMine = ed.mode === "mine" || ed.mode === "add";
      const delBtn = ed.mode === "mine"
        ? `<button class="danger" data-act="ed-del">${esc(ed.delArmed ? this.t("cp.kb.del_confirm") : this.t("cp.common.delete"))}</button>`
        : "";
      // maxlength 只限个人常用语（服务端 500 上限同口径）；团队话术可以更长，
      // 加 maxlength 会把超长团队文本在编辑框里静默截断。
      const maxAttr = isMine ? ` maxlength="${MINE_MAX_CHARS}"` : "";
      const cnt = isMine
        ? `<span class="ed-cnt">${(ed.draft || "").length}/${MINE_MAX_CHARS}</span>` : "";
      // P2：新增表单空白时给「引用输入框内容」——把坐席刚打好的话一键收进常用语
      //（读不到宿主 composer 的宿主形态下按钮自然不出现）。
      const quote = (ed.mode === "add" && !(ed.draft || "").trim() && this._composerText())
        ? `<button data-act="ed-quote">${esc(this.t("cp.kb.quote_btn"))}</button>` : "";
      return `<div class="card qc ${isMine ? "c-mine" : "c-custom"} edform">` +
        `<textarea class="ed-ta" placeholder="${esc(this.t("cp.kb.add_ph"))}"${maxAttr}>${esc(ed.draft || "")}</textarea>` +
        `<div class="ed-err">${esc(ed.err || "")}</div>` +
        `<div class="acts">` +
        `<button class="primary" data-act="ed-save"${ed.busy ? " disabled" : ""}>${esc(this.t("cp.common.save"))}</button>` +
        `<button data-act="ed-cancel">${esc(this.t("cp.common.cancel"))}</button>` +
        quote + delBtn + cnt +
        `</div></div>`;
    }

    /* 只读例外（P2）：同源 App 模式下直接读宿主 composer 现值，供「存常用语」预填。
       写入永远走 cp-fill 事件（基类契约不破，读≠写）；读不到（PiP 独立窗/桌面原生
       renderer/任何跨源受限）→ 返 ""，引用按钮自然不出现。 */
    _composerText() {
      try {
        if (window.parent && window.parent !== window) {
          const ta = window.parent.document.getElementById("reply-ta");
          if (ta && typeof ta.value === "string") return String(ta.value).trim();
        }
      } catch (_e) { /* 跨源受限：静默 */ }
      return "";
    }

    _varFormHtml(text, kind, idx) {
      const esc = (s) => this.esc(s);
      const names = this._varNames(text);
      const inputs = names.map((n) =>
        `<span class="vf-name">${esc(n)}</span>` +
        `<input data-var="${esc(n)}" type="text" />`).join("");
      return `<div class="vf" data-act="vf-box">` +
        `<div class="vf-h">${esc(this.t("cp.kb.var_hint"))}</div>` + inputs +
        `<div class="acts">` +
        `<button class="fill" data-act="var-go" data-kind="${esc(kind)}" data-i="${idx}">${esc(this.t("cp.kb.var_go"))}</button>` +
        `<button data-act="var-x">${esc(this.t("cp.common.cancel"))}</button>` +
        `</div></div>`;
    }

    _varNames(text) {
      const seen = [];
      let m;
      VAR_ALL_RE.lastIndex = 0;
      while ((m = VAR_ALL_RE.exec(String(text || ""))) !== null) {
        if (seen.indexOf(m[1]) < 0) seen.push(m[1]);
      }
      return seen;
    }

    _visibleRows(source) {
      const q = String(this._q || "").trim().toLowerCase();
      let items = (this._all || []).map((t, i) => ({ t: t, i: i }));
      items = items.filter((x) => (source === "mine") === (x.t.source === "mine"));
      if (q) {
        items = items.filter((x) =>
          (String(x.t.label || "") + "\n" + String(x.t.text || "")).toLowerCase().indexOf(q) >= 0);
      }
      return items;
    }

    _mineListHtml() {
      const esc = (s) => this.esc(s);
      const items = this._visibleRows("mine");
      const q = String(this._q || "").trim();
      let rows = "";
      if (this._edit && this._edit.mode === "add") rows += this._editFormHtml();
      rows += items.map((x) =>
        (this._edit && this._edit.mode === "mine" && this._edit.id === x.t.id)
          ? this._editFormHtml()
          : this._cardHtml(x.t, x.i)).join("");
      if (!rows) rows = `<div class="stat">${esc(q ? this.t("cp.kb.no_tpl_match") : this.t("cp.kb.mine_empty"))}</div>`;
      return rows;
    }

    _teamListHtml() {
      const esc = (s) => this.esc(s);
      const items = this._visibleRows("team");
      const q = String(this._q || "").trim();
      if (!items.length) return `<div class="stat">${esc(q ? this.t("cp.kb.no_tpl_match") : this.t("cp.kb.tpl_empty"))}</div>`;
      let rows = items, moreBtn = "";
      if (!q && items.length > VISIBLE_N) {
        // 编辑中不折叠：被编辑条目可能在折叠区，折叠会让表单凭空消失
        const editingTeam = this._edit && this._edit.mode === "team";
        if (!this._showAll && !editingTeam) {
          rows = items.slice(0, VISIBLE_N);
          moreBtn = `<button class="more" data-act="more">${esc(this.t("cp.kb.expand_all", { n: items.length }))}</button>`;
        } else if (this._showAll) {
          moreBtn = `<button class="more" data-act="less">${esc(this.t("cp.kb.collapse", { n: VISIBLE_N }))}</button>`;
        }
      }
      return rows.map((x) =>
        (this._edit && this._edit.mode === "team" && this._edit.i === x.i)
          ? this._editFormHtml()
          : this._cardHtml(x.t, x.i)).join("") + moreBtn;
    }

    _syncTplList() {
      const mine = this.shadowRoot.querySelector(".mine-list");
      if (mine) mine.innerHTML = this._mineListHtml();
      const team = this.shadowRoot.querySelector(".tpl-list");
      if (team) team.innerHTML = this._teamListHtml();
      const mn = this.shadowRoot.querySelector(".mine-n");
      if (mn) mn.textContent = String(this._visibleRows("mine").length);
      const tn = this.shadowRoot.querySelector(".team-n");
      if (tn) tn.textContent = String(this._visibleRows("team").length);
      this._bindEditForm();
    }

    _bindEditForm() {
      const ta = this.shadowRoot.querySelector(".ed-ta");
      if (!ta || !this._edit) return;
      ta.addEventListener("input", () => {
        if (!this._edit) return;
        this._edit.draft = ta.value;
        const cnt = this.shadowRoot.querySelector(".ed-cnt");
        if (cnt) cnt.textContent = `${ta.value.length}/${MINE_MAX_CHARS}`;
      });
      if (this._edit.focusPending) {
        this._edit.focusPending = false;
        try { ta.focus(); ta.selectionStart = ta.value.length; } catch (_e) { /* 焦点失败不阻断 */ }
      }
    }

    _render(html) {
      super._render(html);
      const q = this.shadowRoot.querySelector(".kb-q");
      if (!q) return;
      q.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); this._search(); }
      });
      q.addEventListener("input", () => this._onQueryInput());
      this._syncTplList();
    }

    _onQueryInput() {
      const qEl = this.shadowRoot.querySelector(".kb-q");
      const xEl = this.shadowRoot.querySelector(".qx");
      this._q = qEl ? String(qEl.value || "") : "";
      if (xEl) xEl.classList.toggle("show", !!this._q.trim());
      if (!this._q.trim()) {
        // 清空输入 = 复位：陈旧的知识库命中一并清掉，防「过滤词没了结果还挂着」
        const res = this.shadowRoot.querySelector(".kb-res");
        if (res) res.innerHTML = "";
        this._hits = null;
        this._searchEpoch = (this._searchEpoch || 0) + 1; // 在途搜索作废
      }
      this._syncTplList();
    }

    async _search() {
      const ctx = this._ctx || {};
      const qEl = this.shadowRoot.querySelector(".kb-q");
      const res = this.shadowRoot.querySelector(".kb-res");
      if (!qEl || !res || !this._client || !this._client.kbSearch) return;
      const q = String(qEl.value || "").trim();
      if (!q) { res.innerHTML = ""; this._hits = null; return; }
      const ep = ++this._searchEpoch;
      res.innerHTML = `<div class="sk"></div><div class="sk"></div>`;
      this._beacon("cpkb_search");
      let d;
      try {
        d = await this._client.kbSearch({
          q: q, platform: ctx.platform || "", lang: this._convLang() });
      } catch (_e) {
        if (ep === this._searchEpoch) res.innerHTML = `<div class="stat">${this.esc(this.t("cp.kb.err"))}</div>`;
        return;
      }
      if (ep !== this._searchEpoch) return; // 已被更新的查询/清空取代，丢弃过期响应
      this._hits = ((d && d.entries) || []).filter((en) => en && en.answer);
      if (!this._hits.length) {
        res.innerHTML = `<div class="stat">${this.esc(this.t("cp.kb.no_hit"))}</div>`;
        return;
      }
      res.innerHTML =
        `<div class="sec-hd"><span class="sec-ttl">${this.esc(this.t("cp.kb.kb_hits"))}</span>` +
        `<span class="sec-n">${this._hits.length}</span></div>` +
        this._hits.map((en, i) => this._hitHtml(en, i)).join("");
    }

    _hitHtml(en, i) {
      const esc = (s) => this.esc(s);
      const ans = String(en.answer || "");
      let ttl = String(en.title || en.category || "").trim();
      // 冷启动迁移曾把机器键 Title Case 成英文标题（"Order Query"）：中文界面且答案是
      // 中文时，改用答案首句当标题——坐席不用读英文猜条目是什么。
      const lang = (root.CopilotShared && root.CopilotShared.lang) || "zh";
      if (lang === "zh" && ttl && /^[\x00-\x7f]+$/.test(ttl) && /[\u4e00-\u9fff]/.test(ans)) {
        ttl = this._clause(ans);
      }
      if (!ttl) ttl = this._clause(ans);
      const hi = String(en.confidence || "") === "high";
      // V0：answer 已按会话语言取译稿时给语言徽章（机器稿另标 ◌）——坐席知道
      // 自己看到/将填入的是哪个语言的哪种置信度稿。
      const langBadge = en.translated
        ? `<span class="lang-b on">🌐${esc(this._langName(String(en.lang || "")))}</span>` +
          (en.trans_machine ? `<span class="ip-m" title="${esc(this.t("cp.kb.variant_machine"))}">◌</span>` : "")
        : "";
      const varForm = (this._varFill && this._varFill.kind === "hit" && this._varFill.i === i)
        ? this._varFormHtml((this._varFill.text || ans), "hit", i) : "";
      // P2：内容管理员（can_edit 同权限位）看到条目有错可一键跳知识库后台修——
      // <a> 挂 data-act="kb-lnk"（无处理器的空动作）只为吞掉卡片展开切换，不拦导航。
      const adminLnk = this._canEdit
        ? `<a class="src-lnk" data-act="kb-lnk" href="/knowledge" target="_blank" rel="noopener">${esc(this.t("cp.kb.kb_edit_link"))}</a>`
        : "";
      return `<div class="card qc c-kb" data-act="hit-x" data-i="${i}">` +
        `<div class="ttl">${hi ? `<span class="conf" title="${esc(this.t("cp.kb.conf_high"))}"></span>` : ""}${esc(ttl)}${langBadge}</div>` +
        `<div class="bd">${esc(ans)}</div>` +
        (varForm || `<div class="acts"><button class="fill" data-act="fill-hit" data-i="${i}">${esc(this.t("cp.kb.fill"))}</button>${adminLnk}</div>`) +
        `</div>`;
    }

    _flashFilled(btn, langUsed) {
      try {
        if (btn.__kbT) clearTimeout(btn.__kbT);
        if (!btn.__kbOrig) btn.__kbOrig = btn.textContent;
        btn.textContent = langUsed
          ? this.t("cp.kb.filled_lang", { lang: this._langName(langUsed) })
          : this.t("cp.kb.filled");
        btn.classList.add("done");
        btn.disabled = true;
        btn.__kbT = setTimeout(() => {
          btn.textContent = btn.__kbOrig;
          btn.classList.remove("done");
          btn.disabled = false;
        }, 900);
      } catch (_e) { /* 反馈失败不影响已完成的填入 */ }
    }

    // ── 编辑闭环 ────────────────────────────────────────────────────────

    _errText(r) {
      const code = String((r && r.error) || "");
      if (code === "full") return this.t("cp.kb.err_full", { n: (r && r.max_items) || 20 });
      if (code === "too_long") return this.t("cp.kb.err_too_long", { n: MINE_MAX_CHARS });
      if (code === "duplicate" || code === "empty_text" || code === "not_found") {
        return this.t("cp.kb.err_" + code);
      }
      if (r && r.status === 403) return this.t("cp.kb.err_perm");
      return this.t("cp.kb.err_api");
    }

    async _mineMutate(payload, beacon) {
      const r = await this._client.quickReplyMutate(payload);
      if (!r || r.ok === false) {
        if (this._edit) { this._edit.err = this._errText(r); this._edit.busy = false; this._syncTplList(); }
        return false;
      }
      this._beacon(beacon);
      this._edit = null;
      this._varFill = null;
      await this._reload(); // 服务端为真相源：改完显式回源（refresh 已会话无关化）
      return true;
    }

    async _saveEdit() {
      const ed = this._edit;
      if (!ed || ed.busy) return;
      const draft = String(ed.draft || "").trim();
      if (!draft) { ed.err = this.t("cp.kb.err_empty_text"); this._syncTplList(); return; }
      ed.busy = true; ed.err = ""; this._syncTplList();
      if (ed.mode === "add") { await this._mineMutate({ action: "add", text: draft }, "cpkb_mine_add"); return; }
      if (ed.mode === "mine") { await this._mineMutate({ action: "update", id: ed.id, text: draft }, "cpkb_mine_edit"); return; }
      if (ed.mode === "team") { await this._saveTeam(draft); return; }
    }

    /* 团队话术保存：读全量 → 原文精确匹配定位 → 整值回写 admin PUT。
       匹配不到 = 被并发改过 → 冲突提示 + 回源刷新，绝不盲覆盖别人的修改。 */
    async _saveTeam(draft) {
      const ed = this._edit;
      const fail = (msg) => { ed.err = msg; ed.busy = false; this._syncTplList(); };
      let cur;
      try { cur = await this._client.teamTemplates(); } catch (_e) { cur = null; }
      if (!cur || cur.ok === false) { fail(this._errText(cur)); return; }
      const val = cur[ed.key];
      let newVal = null;
      if (Array.isArray(val)) {
        const i = val.indexOf(ed.orig);
        if (i < 0) { await this._teamConflict(); return; }
        newVal = val.slice();
        newVal[i] = draft;
      } else if (typeof val === "string") {
        if (val !== ed.orig) { await this._teamConflict(); return; }
        newVal = draft;
      } else { await this._teamConflict(); return; }
      const r = await this._client.teamTemplateUpdate({ key: ed.key, value: newVal });
      if (!r || r.ok === false) { fail(this._errText(r)); return; }
      this._beacon("cpkb_team_edit");
      this._edit = null;
      await this._reload();
    }

    async _teamConflict() {
      // 冲突语义：先如实告知，再回源刷新（提示文案已说明「已为你刷新」）
      const msg = this.t("cp.kb.err_conflict");
      this._edit = null;
      await this._reload();
      const team = this.shadowRoot.querySelector(".tpl-list");
      if (team) team.insertAdjacentHTML("afterbegin", `<div class="ed-err">${this.esc(msg)}</div>`);
    }

    async _mineDelete() {
      const ed = this._edit;
      if (!ed || ed.mode !== "mine" || ed.busy) return;
      if (!ed.delArmed) { ed.delArmed = true; this._syncTplList(); return; } // 两击确认
      ed.busy = true;
      await this._mineMutate({ action: "delete", id: ed.id }, "cpkb_mine_del");
    }

    // ── 变量填入 ────────────────────────────────────────────────────────

    _fillWithVars(kind, i, el) {
      const box = el.closest(".vf");
      if (!box) return;
      const inputs = box.querySelectorAll("input[data-var]");
      const values = {};
      let bad = false;
      inputs.forEach((inp) => {
        const v = String(inp.value || "").trim();
        inp.classList.toggle("bad", !v);
        if (!v) bad = true;
        values[inp.getAttribute("data-var")] = v;
      });
      if (bad) return;
      // 变体感知：表单打开时刻已定格候选文本（可能是外语变体），确认时用它
      let text = String((this._varFill && this._varFill.text) || "");
      const langUsed = String((this._varFill && this._varFill.lang) || "");
      if (!text) {
        text = kind === "hit"
          ? String((this._hits && this._hits[i] && this._hits[i].answer) || "")
          : String((this._all && this._all[i] && this._all[i].text) || "");
      }
      text = text.replace(VAR_ALL_RE, (_m, name) => (values[name] != null ? values[name] : _m));
      this._varFill = null;
      this.emit("cp-fill", { text: text });
      this._flashFilled(el, langUsed);
      if (kind === "tpl") this._noteUse(this._all && this._all[i]);
      this._beacon("cpkb_var_fill");
      // 表单收场：变量卡回到普通形态
      if (kind === "hit") { this._rerenderHits(); } else { this._syncTplList(); }
    }

    _rerenderHits() {
      const res = this.shadowRoot.querySelector(".kb-res");
      if (!res || !this._hits || !this._hits.length) return;
      res.innerHTML =
        `<div class="sec-hd"><span class="sec-ttl">${this.esc(this.t("cp.kb.kb_hits"))}</span>` +
        `<span class="sec-n">${this._hits.length}</span></div>` +
        this._hits.map((en, i) => this._hitHtml(en, i)).join("");
    }

    // ── 动作分发 ────────────────────────────────────────────────────────

    onAction(act, el) {
      if (act === "kb-go") { this._search(); return; }
      if (act === "q-clear") {
        const qEl = this.shadowRoot.querySelector(".kb-q");
        if (qEl) { qEl.value = ""; qEl.focus(); }
        this._onQueryInput();
        return;
      }
      if (act === "more") { this._showAll = true; this._syncTplList(); return; }
      if (act === "less") { this._showAll = false; this._syncTplList(); return; }
      if (act === "mine-add") {
        this._edit = { mode: "add", draft: "", focusPending: true };
        this._varFill = null;
        this._syncTplList();
        return;
      }
      if (act === "ed-open") {
        const i = parseInt(el.getAttribute("data-i") || "-1", 10);
        const t = this._all && this._all[i];
        if (!t) return;
        this._varFill = null;
        this._edit = (t.source === "mine")
          ? { mode: "mine", id: t.id, draft: t.text, focusPending: true }
          : { mode: "team", i: i, key: t.key, orig: t.text, draft: t.text, focusPending: true };
        this._syncTplList();
        return;
      }
      if (act === "ed-cancel") { this._edit = null; this._syncTplList(); return; }
      if (act === "ed-save") { this._saveEdit(); return; }
      if (act === "ed-del") { this._mineDelete(); return; }
      if (act === "ed-quote") {
        const qt = this._composerText();
        if (qt && this._edit) {
          this._edit.draft = qt.slice(0, MINE_MAX_CHARS);
          this._edit.focusPending = true;
          this._syncTplList();
          this._beacon("cpkb_quote_used"); // B0：跨宿主补桥的裁决数据（阈值触发制）
        }
        return;
      }
      if (act === "var-x") {
        const wasHit = this._varFill && this._varFill.kind === "hit";
        this._varFill = null;
        if (wasHit) { this._rerenderHits(); } else { this._syncTplList(); }
        return;
      }
      if (act === "var-go") {
        this._fillWithVars(el.getAttribute("data-kind") || "tpl",
          parseInt(el.getAttribute("data-i") || "-1", 10), el);
        return;
      }
      if (act === "vf-box") return; // 变量表单容器：吞掉冒泡，不触发卡片展开
      if (act === "tpl-x" || act === "hit-x") { el.classList.toggle("expanded"); return; }
      const i = parseInt(el.getAttribute("data-i") || el.getAttribute("data-idx") || "-1", 10);
      let text = "";
      let langUsed = "";
      if (act === "fill-hit" && this._hits && this._hits[i]) {
        text = String(this._hits[i].answer || "");
      }
      if ((act === "fill-tpl" || act === "fill-zh") && this._all && this._all[i] != null) {
        const t = this._all[i];
        text = String(t.text || "");
        const cl = this._convLang();
        if (act === "fill-tpl" && cl && cl !== "zh" && t.source === "templates") {
          // V1：外语会话优先填该语言的人审/机器稿变体；无变体回落中文原文
          //（出站机翻链继续兜底）。hit/miss 埋点只在真外语会话计，供变体库补货裁决。
          const v = t.i18n && t.i18n[cl];
          if (v && v.text) { text = String(v.text); langUsed = cl; }
          this._beacon(v && v.text ? "cpkb_fill_lang_hit" : "cpkb_fill_lang_miss");
        }
        if (act === "fill-zh" && langUsed === "") {
          // 显式改发中文原文（有变体仍选原文=对变体的不信任信号，单独计数）
          this._beacon("cpkb_fill_zh_override");
        }
      }
      if (!text) return;
      // 含变量 → 先弹补全表单（源无关：个人/团队/变体/KB 命中一视同仁），杜绝 {xxx} 原样发出
      if (VAR_RE.test(text)) {
        this._varFill = { kind: act === "fill-hit" ? "hit" : "tpl", i: i, text: text, lang: langUsed };
        if (this._varFill.kind === "hit") { this._rerenderHits(); } else { this._syncTplList(); }
        return;
      }
      this.emit("cp-fill", { text: text });
      this._flashFilled(el, langUsed);
      if (act === "fill-tpl" || act === "fill-zh") this._noteUse(this._all && this._all[i]);
      this._beacon(act === "fill-hit" ? "cpkb_fill_hit" : "cpkb_fill_tpl");
    }
  }

  if (!customElements.get("cp-kb")) customElements.define("cp-kb", CpKb);
})(typeof window !== "undefined" ? window : this);
