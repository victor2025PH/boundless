"use strict";
/* 两端共享 · 语音克隆 / TTS / 发送（<cp-voice>）
   与统一收件箱 reply-tools + voice-enroll-panel 同源 API，布局适配侧栏纵向。
   用法:
     el.client = CopilotShared.createCopilotClient();
     el.context = { platform, accountId, chatKey, conversationId };
   监听 cp-fill 自动填入草稿文字。发送成功 emit cp-voice-sent。
   B25-①（2026-08-21）另有登记专用档：<cp-voice mode="enroll" persona="pid">——
   只渲染登记/改绑/对账段（免会话上下文，赋 client 即渲染），persona 属性预选目标
   人设；人设页与收件箱右栏共用同一登记流（单源两挂载点，避免双端重复维护）。 */
(function (root) {
  const VOICE_KEY = "ws_voice_persona_v1";
  // P0-V2b 译声开关（2026-08-30）：与主输入框「跟随翻译发声」共用同一存储键
  // （同一语义一个开关、两个入口互通；'1'/缺省=开）。目标语恒 'auto'=会话客户
  // 语言由服务端解析（与 send-voice 同源），解析不出=不译 fail-open。
  const XL_KEY = "aitr.voicexl";

  function fileToB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const s = String(r.result || "");
        const i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }

  class CpVoice extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._client = null;
      this._ctx = null;
      this._profiles = [];
      this._persona = "";
      this._enrollOpen = false;
      this._lastReconcile = null;
      // B115（实施74）：旧「从消息一键导入」预填源已随工具箱登记块撤除，
      // 本字段保留恒 null（_enroll 的 src 分支自然不走，改动面最小）。
      this._prefillSrc = null;
      // ── 生成/发送状态机（P0 2026-08-05：修「预览清不掉 / 生成入口迷失 / 连点双发」）──
      // _busy: "" | "tts" | "send"，请求期互斥闸门（连点＝双烧 GPU / 给客户双发）；
      // _previewText/_previewPersona: 最近一次试听的生成基准——发送是服务端按**当前**
      //   文本/音色重新合成，两者与基准不一致＝会发出从未试听过的内容，须标过期拦发送；
      // _epoch: 渲染代际，会话切换后在途请求的 UI 回写一律作废（防串会话）。
      this._busy = "";
      this._previewText = null;
      this._previewPersona = "";
      this._previewFilename = "";   // 所听即所发：随发送带回，服务端校验后复用试听音频
      this._previewXl = "";         // 译声维度：试听生成时的目标语设置（'auto'/''），偏离=过期
      // #121：试听时服务端**真实使用**的目标语（d.target_lang，仅 voice_translated
      // 时有值）——'auto' 的解析真相。过期判定按「有效目标语」比对，防宿主的
      // 「我的消息 →」偏好异步加载后 'auto'→'en' 这种纯表示层变化把没改任何
      // 设置的试听常亮成过期（skuio 0831 原图 924 实锤）。
      this._previewXlEff = "";
      this._xlOn = this._loadXlOn();
      this._effConvLang = null;     // 会话客户语言（effective-config 回传；null=后端未提供）
      // 当前音色主路可念语种（effective-config.voice_langs；null/空=能力未知不警示）。
      // P0 2026-08-31「日文怪声」：目标语超出引擎能力（如 IndexTTS-2 念日文）→ 生成前警示。
      this._effVoiceLangs = null;
      // ── 结果区裁决状态（P0 2026-08-31 提示风暴复盘）──────────────────────
      // _silent: 裁决阳性=阻发闸（重新生成清除）；_silentKind: 阻发原因
      // （"silent"=无声 / "garbled"=有声但念错，#161 起两者文案与出路分开）；
      // _previewFallback: 本条试听的回落原因（""=克隆声正常，
      // lang_unsupported/quota/channel/generic）；
      // _fbConfirmedKey: 降级发送已确认过的会话键（换会话/重渲失效）；
      // _staleWas: 过期沿触发观测埋点（只记 false→true 跳变，不刷屏）。
      this._silent = false;
      this._silentKind = "";
      this._previewFallback = "";
      this._fbConfirmedKey = "";
      this._staleWas = false;
      // #250（N-4 B）：本条试听的服务端终审（voice_meta.speech / speech_basis）——
      // 结果区唯一结论行 _syncNotes 的输入之一；与 _silent（含客户端兜底）合成四态。
      this._previewSpeech = "";
      this._previewBasis = "";
      this._epoch = 0;
      this._genTimer = null;
      this._hintTimer = null;
      this._hintResetMs = 4000;   // 发送成功提示驻留时长（验收脚本可调短）
      this._maxChars = 400;       // 与服务端 tts-test 上限同口径（超限前端先拦，省一次白跑）
      this.shadowRoot.innerHTML = `<style>${this._css()}</style><div class="wrap empty">${this._t("cp.voice.empty")}</div>`;
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
      this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
      this.shadowRoot.addEventListener("input", (e) => {
        if (e.target && e.target.matches && e.target.matches('[data-role="text"]')) {
          this._syncStale();
          this._syncCounter();
          this._autoGrow(e.target);
        }
      });
      this.addEventListener("cp-fill", (e) => {
        const t = (e.detail && e.detail.text) || "";
        if (!t) return;
        const ta = this.shadowRoot.querySelector('[data-role="text"]');
        if (ta) { ta.value = t; this._syncStale(); this._syncCounter(); this._autoGrow(ta); }   // 程序化赋值不触发 input，这里补判
      });
    }

    connectedCallback() {
      // 译声开关跨窗口/iframe 同步：同 origin 的其他浏览上下文改了 localStorage
      // （如主输入框翻译设置里的同名开关）→ storage 事件 → 本面板即时跟随。
      // （同文档内的直改由宿主显式调 refreshXl()，storage 事件不覆盖同文档。）
      if (!this._onStorage) {
        this._onStorage = (e) => {
          try { if (e && e.key === XL_KEY) this.refreshXl(); } catch (_e) { /* */ }
        };
      }
      try { root.addEventListener("storage", this._onStorage); } catch (_e) { /* */ }
    }

    disconnectedCallback() {
      this._stopGenTimer();
      if (this._hintTimer) { clearTimeout(this._hintTimer); this._hintTimer = null; }
      try { if (this._onStorage) root.removeEventListener("storage", this._onStorage); } catch (_e) { /* */ }
    }

    _css() {
      return `
      :host { display:block; font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#e2e8f0); }
      /* #250（N-4 B，2026-09-08）：hidden 属性必须压过本样式表里任何作者 display 规则。
         UA 样式表的 [hidden]{display:none} 级联在作者样式之下——.pv-note{display:flex} /
         .effline{display:flex} 一写，el.hidden=true 就形同虚设：钧机两张截图里
         「已核有声 + 疑似无声 + 试听已过期」三条同屏，红条橙条根本没有任何判定点亮它们，
         只是从 0831 起就没被藏住过（现有门禁只断言 el.hidden，从未看计算样式）。 */
      [hidden] { display:none !important; }
      svg.ui-ic { pointer-events:none; }  /* Shadow DOM 不吃宿主页样式：图标不抢点击，事件落宿主按钮 */
      .wrap { background:var(--cp-surface-2,#1a2332); border:1px solid var(--cp-border,#2a3544);
              border-radius:var(--cp-radius-sm,8px); padding:8px; }
      .empty { color:var(--cp-text-tiny,#94a3b8); text-align:center; padding:12px 4px; }
      .row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:6px; }
      select, input[type=text], textarea { font:inherit; font-size:var(--cp-fs-sm,12px);
        padding:4px 6px; border:1px solid var(--cp-border,#2a3544); border-radius:6px;
        background:var(--cp-surface,#0f1419); color:var(--cp-text,#e2e8f0); }
      textarea { width:100%; min-height:56px; resize:vertical; box-sizing:border-box; }
      button { font:inherit; font-size:var(--cp-fs-tiny,11px); cursor:pointer; padding:4px 8px;
        border:1px solid var(--cp-border,#2a3544); border-radius:6px;
        background:var(--cp-surface,#1a2332); color:var(--cp-text,#e2e8f0); }
      button.primary { background:var(--cp-accent,#3aa0ff); color:#fff; border-color:transparent; }
      button:disabled { opacity:.5; cursor:default; }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:4px 0; }
      /* ── P1 三区（2026-08-31）：①音色身份区 ②撰写区 ③结果区，细分隔线分区 ── */
      .zone-id { padding-bottom:6px; border-bottom:1px solid var(--cp-border,#2a3544);
                 margin-bottom:8px; }
      .zone-id .row { margin-bottom:0; }
      .zone-id select { flex:1; min-width:0; }
      /* 音色状态行：健康点 + 名字·后端 chips + 短风险 + 「详情」开合 */
      .effline { display:flex; align-items:center; flex-wrap:wrap; gap:2px 0;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8);
                 margin:6px 0 0; cursor:pointer; user-select:none; }
      .effline .dot { width:8px; height:8px; border-radius:50%; flex:none;
                      margin-right:5px; background:var(--cp-ok,#16a34a); }
      .effline .dot.warn { background:var(--cp-warn,#d97706); }
      .effline .dot.err { background:var(--cp-danger,#dc2626); }
      .effline .eff-risk { margin-left:6px; }
      .effline .eff-more { margin-left:auto; padding-left:8px; opacity:.7; flex:none; }
      .effdetail { margin:4px 0 0; padding:6px 8px; border:1px dashed var(--cp-border,#2a3544);
                   border-radius:6px; font-size:var(--cp-fs-tiny,11px);
                   color:var(--cp-text-dim,#94a3b8); }
      .effdetail > div { margin:2px 0; }
      .effdetail .eff-acts { display:flex; justify-content:flex-end; margin-top:6px; }
      .compose-foot { display:flex; flex-wrap:wrap; align-items:center; gap:4px 8px;
                      margin:4px 0 6px; }
      .compose-foot .hint { margin:0; flex:1; min-width:0; }
      .compose-foot .cnt { margin-left:auto; }
      .pv-foot { justify-content:flex-end; margin:6px 0 0; }
      .gen-btn { display:flex; width:100%; justify-content:center; align-items:center;
                 gap:5px; padding:6px 8px; font-size:var(--cp-fs-sm,12px); }
      /* 首次使用提示（可关，localStorage 记忆）：常驻三步说明降噪为一次性引导 */
      .tip { display:flex; gap:8px; align-items:flex-start; margin-top:6px; padding:6px 8px;
             border-radius:6px; background:var(--cp-accent-bg,rgba(84,167,245,.14));
             color:var(--cp-text-dim,#94a3b8); font-size:var(--cp-fs-tiny,11px); }
      .tip button { flex:none; }
      .preview { margin-top:6px; padding:6px; border:1px dashed var(--cp-border,#2a3544); border-radius:6px; }
      .preview.stale { border-color:var(--cp-warn,#d97706); }
      .preview.stale audio { opacity:.5; }
      .pv-hd { justify-content:space-between; margin-bottom:2px; }
      .pv-hd .pv-actions { display:flex; gap:6px; }
      .warn { color:var(--cp-warn,#d97706); }
      /* 结果区状态 banner（P1-3）：warn/err 两档语义色，动作钮长在 banner 里 */
      .pv-note { display:flex; align-items:flex-start; gap:6px; margin-top:6px;
                 padding:5px 7px; border-radius:6px; font-size:var(--cp-fs-tiny,11px);
                 border:1px solid transparent; }
      .pv-note.warn { background:var(--cp-warn-bg,rgba(245,185,69,.12));
                      border-color:var(--cp-warn-border,rgba(245,185,69,.38));
                      color:var(--cp-warn-ink,#d97706); }
      .pv-note.err { background:var(--cp-danger-bg,rgba(240,106,106,.14));
                     border-color:var(--cp-danger,#dc2626);
                     color:var(--cp-danger,#dc2626); }
      /* info 档（P0 2026-08-31）：刻意的语种改道是「说明」不是「警告」——
         蓝底与 warn/err 分级，警告色只留给真异常（全黄=没有重点）。 */
      .pv-note.info { background:var(--cp-accent-bg,rgba(84,167,245,.14));
                      border-color:var(--cp-accent-border,rgba(84,167,245,.38));
                      color:var(--cp-text,#e2e8f0); }
      .pv-note button { flex:none; margin-left:auto; }
      /* 单一结论行（#250 N-4 B）：整个结果区只此一处下结论——✅ 可发送 / ❌ 无声或
         念错（原因 + 重新生成）/ ⚠ 试听已过期（重新生成）/ ℹ 未能核验。四态互斥，
         同一时刻只渲染一条；「实际使用」行只报事实（人设 / 后端 / 音色），不再下结论。 */
      .pv-verdict { display:flex; align-items:flex-start; gap:6px; margin-top:6px;
                    padding:5px 7px; border-radius:6px; font-size:var(--cp-fs-tiny,11px);
                    border:1px solid transparent; line-height:1.45; }
      .pv-verdict.ok { background:var(--cp-ok-bg,rgba(22,163,74,.12));
                       border-color:var(--cp-ok-border,rgba(22,163,74,.38));
                       color:var(--cp-ok,#16a34a); }
      .pv-verdict.warn { background:var(--cp-warn-bg,rgba(245,185,69,.12));
                         border-color:var(--cp-warn-border,rgba(245,185,69,.38));
                         color:var(--cp-warn-ink,#d97706); }
      .pv-verdict.err { background:var(--cp-danger-bg,rgba(240,106,106,.14));
                        border-color:var(--cp-danger,#dc2626);
                        color:var(--cp-danger,#dc2626); }
      .pv-verdict.info { background:var(--cp-accent-bg,rgba(84,167,245,.14));
                         border-color:var(--cp-accent-border,rgba(84,167,245,.38));
                         color:var(--cp-text,#e2e8f0); }
      .pv-verdict > span { flex:1; min-width:0; }
      .pv-verdict button { flex:none; margin-left:auto; }
      canvas[data-role="wave"] { width:100%; height:24px; display:block; margin-top:2px; }
      .cnt { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .cnt.err { color:var(--cp-danger,#dc2626); font-weight:600; }
      audio { width:100%; margin-top:4px; }
      .panel { margin-top:8px; padding-top:8px; border-top:1px dashed var(--cp-border,#2a3544); }
      .panel h5 { margin:0 0 6px; font-size:12px; font-weight:600; }
      .recon { max-height:120px; overflow:auto; font-size:11px; color:var(--cp-text-dim,#94a3b8); }
      .ok { color:var(--cp-ok,#16a34a); } .err { color:var(--cp-danger,#dc2626); }
      .consent { display:flex; gap:6px; align-items:flex-start; cursor:pointer;
                 font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .consent input { margin:1px 0 0; flex:none; }
      .srcchip { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#e2e8f0);
                 background:var(--cp-surface,#0f1419); border:1px dashed var(--cp-border,#2a3544);
                 border-radius:6px; padding:4px 6px; }
      .srcchip button { padding:0 5px; line-height:16px; }
      /* P0-V2b 译声行：开关+目标语预告一行装下；提示为空时整行不占位 */
      .xlrow { gap:8px; margin:2px 0 6px; }
      .xlrow label { display:inline-flex; align-items:center; gap:4px; cursor:pointer;
                     font-size:var(--cp-fs-tiny,11px); color:var(--cp-text,#e2e8f0); }
      .xlrow input { margin:0; flex:none; }
      .xlrow .xlh { margin:0; }
      [data-role="msg"]:empty { display:none; }
      .pv-spoken { margin-top:4px; white-space:pre-wrap; word-break:break-word; }
      .pv-spoken .sp-txt { color:var(--cp-text,#e2e8f0); }
      /* 音色状态条 chip：分隔符长在后继 chip 上（折行跟内容走，不悬挂行尾） */
      .eff-bit { display:inline-block; }
      .eff-bit + .eff-bit::before { content:"·"; margin:0 4px;
        color:var(--cp-text-tiny,#94a3b8); }`;
    }

    set client(c) {
      this._client = c;
      // 登记专用档没有 context 喂入时机：赋 client 即渲染（重复赋值幂等重渲）。
      if (c && this._mode() === "enroll") this._renderEnrollOnly();
    }
    get client() { return this._client; }
    _mode() { return (this.getAttribute("mode") || "").trim(); }
    set context(ctx) {
      this._ctx = ctx;
      this._persona = this._loadPersona();
      this._render();
      this._loadProfiles();
    }
    get context() { return this._ctx; }

    /* B25-① 登记专用档（人设页挂载）：只出登记/改绑/对账段——生成/发送需要会话
       上下文，留在收件箱右栏。目标人设按 persona 属性预选，显示名空则预填人设名
       （少打一格字；仍可改）。 */
    async _renderEnrollOnly() {
      this._enrollOpen = true;
      this.shadowRoot.innerHTML = `<style>${this._css()}</style>
        <div class="wrap"><div data-role="enroll">${this._enrollHtml()}</div></div>`;
      await this._loadEnrollPersonas();
      const pid = (this.getAttribute("persona") || "").trim();
      if (pid) {
        const sel = this.shadowRoot.querySelector('[data-role="epersona"]');
        if (sel) {
          sel.value = pid;
          const opt = sel.selectedOptions && sel.selectedOptions[0];
          const nameEl = this.shadowRoot.querySelector('[data-role="ename"]');
          if (opt && opt.value === pid && nameEl && !nameEl.value) {
            nameEl.value = (opt.textContent || "").replace(/\s*🎤$/, "").trim();
          }
        }
      }
      this._reconcile();
    }

    _convKey() {
      const c = this._ctx || {};
      return c.conversationId || `${c.platform || ""}:${c.accountId || ""}:${c.chatKey || ""}`;
    }
    _loadPersona() {
      try {
        const m = JSON.parse(localStorage.getItem(VOICE_KEY) || "{}");
        return m[this._convKey()] || "";
      } catch (e) { return ""; }
    }
    _savePersona(v) {
      if (!this._ctx) return;   // 登记专用档无会话：不写「::」垃圾键进音色偏好表
      try {
        const m = JSON.parse(localStorage.getItem(VOICE_KEY) || "{}");
        m[this._convKey()] = v;
        localStorage.setItem(VOICE_KEY, JSON.stringify(m));
      } catch (e) { /* */ }
    }

    _esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    }
    _t(key, vars) {
      const f = root.CopilotShared && root.CopilotShared.t;
      return f ? f(key, vars) : key;
    }
    /* 线性 SVG 图标（P2B 统一图标语言）：库在（收件箱宿主）走全站注册表，
       否则回落 sidebar-chrome 子集表；再不行返回空串（按钮仍有 title/文字兜底）。 */
    _ic(name, size) {
      try {
        const sc = root.CopilotShared && root.CopilotShared.sidebarChrome;
        if (sc && typeof sc.uiIcon === "function") return sc.uiIcon(name, size || 13) || "";
      } catch (_e) { /* 图标缺失不阻塞渲染 */ }
      return "";
    }
    _genLabelHtml() {
      return `${this._ic("mic", 13)} ${this._esc(this._t("cp.voice.tts_btn"))}`;
    }
    /* #250（N-4 D）：「去语音页修正」深链——服务端给 voice_tab（/personas#profile=<id>&tab=voice）
       优先；缺省按 pid 拼；都没有就到人设工作室首页。与 cp-persona 的 CTA 同口径新开页。 */
    _cfgFixLink(url, pid) {
      const href = String(url || "").trim()
        || (pid ? `/personas#profile=${encodeURIComponent(String(pid))}&tab=voice` : "/personas");
      return `<a class="cta" data-role="cfg-fix" href="${this._esc(href)}" target="_blank" rel="noopener">`
        + `${this._esc(this._t("cp.voice.cfg_fix_btn"))}</a>`;
    }
    /* 配置问题码 → 人话（cp.voice.cfg_<code> 词条；没登记的原样显示不装懂） */
    _cfgCodeLabel(code) {
      const c = String(code || "").trim();
      if (!c) return "";
      const k = "cp.voice.cfg_" + c.toLowerCase();
      const v = this._t(k);
      return (v && v !== k) ? v : c;
    }

    _metaLine(d) {
      const m = (d && d.voice_meta) || {};
      const parts = [];
      if (m.persona_id) {
        // 内部 id → 显示名（P0-2 2026-08-31：「人设 lin_xiaoyu」是调试口径，
        // 坐席认的是「林小雨」；profiles 未加载/查不到时原样显示不装懂）
        const row = (this._profiles || []).find((p) => p.persona_id === m.persona_id);
        parts.push(`${this._t("cp.voice.m_persona")} ${(row && row.name) || m.persona_id}`);
      }
      if (m.provider) parts.push(`${this._t("cp.voice.m_provider")} ${this._backendLabel(m.provider)}`);
      if (m.voice) {
        // 音色代号人话化（P1-2 2026-08-31）：locale 型代号（ja-JP-NanamiNeural）
        // 坐席只需要「音色 日语」；完整代号收进悬浮 title 供排障。
        // 非 locale 值（克隆音色名）原样显示不装懂。
        const lm = /^([a-z]{2,3})-[A-Za-z]{2,4}-/.exec(String(m.voice));
        parts.push(`${this._t("cp.voice.m_voice")} ${lm ? this._langName(lm[1]) : m.voice}`);
      }
      const dur = Number(d && d.duration_sec) || 0;
      if (dur > 0) {
        const s = Math.max(1, Math.round(dur));
        parts.push(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
      }
      if (m.emotion) parts.push(`${this._t("cp.voice.m_emotion")} ${this._emoLabel(m.emotion)}`);
      if (m.fallback_from) parts.push(this._t("cp.voice.m_fallback", { from: this._backendLabel(m.fallback_from) }));
      // #161（2026-09-03）：语种改道走的是标准声，这件事必须写在**实际使用**行上
      // ——蓝条说明会被过期/无声提示按互斥规则藏起来（_syncNotes 单出口），藏掉
      // 之后坐席就再没有任何地方能看见「客户听到的不是人设的声音」。
      if (m.fallback_reason === "lang_unsupported") {
        parts.push(this._t("cp.voice.std_voice_note",
          { lang: this._langName(String(m.fallback_lang || "")) }));
      }
      // #250（N-4 B）：「✓ 已核有声」不再挂在这一行——终审结论只在结果区唯一结论行
      // 出现（_syncNotes）。#121 时把它加在这里是为了给红条「作证」，结果成了钧机截图
      // 里「已核有声 / 疑似无声 / 已过期」三条同屏的一员。实际使用行只报事实。
      if (!parts.length) return "";
      // chip 化（P1-2）：折行时分隔符跟内容走（同音色状态条 eff-bit 方案，
      // 旧 " · " 拼串窄栏折行会行尾悬挂）；原始代号串进 title，不占坐席眼球。
      const raw = [m.persona_id, m.provider, m.voice, m.emotion,
        m.fallback_from ? `fallback_from=${m.fallback_from}` : "",
        m.fallback_reason ? `reason=${m.fallback_reason}` : "",
        m.speech ? `speech=${m.speech}${m.speech_basis ? "/" + m.speech_basis : ""}` : ""]
        .filter(Boolean).join(" | ");
      const chips = parts.map((b) => `<span class="eff-bit">${this._esc(b)}</span>`).join("");
      return `<div class="hint" title="${this._esc(raw)}">`
        + `${this._esc(this._t("cp.voice.m_actual", { parts: "" }))}${chips}</div>`;
    }

    async _loadProfiles() {
      if (!this._client || !this._client.voiceProfiles) return;
      try {
        const d = await this._client.voiceProfiles();
        if (!d || !d.ok) return;
        this._profiles = d.profiles || [];
        const sel = this.shadowRoot.querySelector('[data-role="persona"]');
        if (!sel) return;
        // 三档语义（2026-08-05 P0）：""=跟随会话人设（回落链，声音随会话绑定变）；
        // "__system__"=系统通用音色（钉死全局配置，绕过 hub 人设层=最稳）；
        // 具名人设=显式克隆声。d.default 描述的是全局配置=系统通用音色的实况。
        let html = `<option value="">${this._t("cp.voice.default_voice")}</option>`;
        const dft = d.default || {};
        let sysLabel = this._t("cp.voice.system_voice");
        if (dft.is_clone) {
          sysLabel = dft.ready
            ? this._t("cp.voice.system_voice_clone")
            : this._t("cp.voice.system_voice_warn");
        }
        const sysDis = dft.is_clone && !dft.ready ? " disabled" : "";
        html += `<option value="__system__"${sysDis}>${sysLabel}</option>`;
        this._profiles.forEach((p) => {
          const tag = p.is_clone ? (p.ready ? " 🎤" : " 🎤⚠") : "";
          // P3 音色体检徽标（warn=低于正常带 / critical=疑似参考音坏或换错）。
          // 刻意不置灰：体检差仍能出声，弃用与否人决定（就绪置灰是另一语义）。
          const q = p.quality === "critical" ? this._t("cp.voice.q_crit_tag")
            : (p.quality === "warn" ? this._t("cp.voice.q_warn_tag") : "");
          const dis = p.is_clone && !p.ready ? " disabled" : "";
          html += `<option value="${this._esc(p.persona_id)}"${dis}>${this._esc(p.name)}${tag}${this._esc(q)}</option>`;
        });
        sel.innerHTML = html;
        const ok = Array.from(sel.options).some((o) => o.value === this._persona && !o.disabled);
        // #149：记忆的选择已不在列表（人设删了/未就绪被置灰）→ 状态与下拉必须同步
        // 复位。旧代码只改 sel.value、_persona 仍握着陈旧 id → 下拉显示「跟随会话
        // 人设」而请求却带着陈旧 id，正是「所见非所用」的一种形态。
        if (!ok && this._persona) {
          this._persona = "";
          this._savePersona("");
        }
        sel.value = ok ? this._persona : "";
        this._refreshEffStatus();
      } catch (e) { /* */ }
    }

    /* 音色状态条（P1 2026-08-05）：选择生效前先亮真相——当前选择实际会解析成
       谁的声（尤其「跟随会话人设」的空值到底落到哪）、克隆是否就绪、以及该音色
       是否处于「hub 严格辖区且 hub 有险」（unreachable=TCP 确定不可达必拒发；
       recent_failures=台账事后证据）。与 send-voice 同源解析（effective-config
       带 chat/account 上下文），杜绝「状态条一套、发送另一套」。代际（_epoch）
       防会话切换后的陈旧回写；客户端无该方法（旧桌面壳）→ 静默隐藏，零依赖。 */
    _hideEffStatus() {
      const box = this.shadowRoot.querySelector('[data-role="effstatus"]');
      const det = this.shadowRoot.querySelector('[data-role="effdetail"]');
      if (box) box.hidden = true;
      if (det) { det.hidden = true; det.innerHTML = ""; }
    }

    async _refreshEffStatus() {
      const box = this.shadowRoot.querySelector('[data-role="effstatus"]');
      const c = this._ctx;
      if (!box) return;
      if (!c || !c.chatKey || !this._client || !this._client.voiceEffectiveConfig) {
        this._hideEffStatus();
        this._effConvLang = null;
        this._effVoiceLangs = null;
        this._syncXlHint();
        return;
      }
      const epoch = this._epoch;
      try {
        const d = await this._client.voiceEffectiveConfig({
          persona_id: this._persona || undefined,
          chat_key: c.chatKey,
          platform: c.platform || undefined,
          account_id: c.accountId || undefined,
        });
        if (epoch !== this._epoch) return;   // 会话已切换：陈旧回写作废
        if (!d || d.ok === false) { this._hideEffStatus(); this._effVoiceLangs = null; return; }
        // 老后端护栏：响应缺新字段（hub_strict 等）＝服务端还没装载本批解析
        // （不认 chat_key 入参）——此时渲染的是「无会话上下文」的错误解析，
        // 与真实发送不同源。宁可不显示，不显示错的。
        if (!("hub_strict" in d)) { this._hideEffStatus(); this._effVoiceLangs = null; return; }
        // 译声预告数据源：会话客户语言（与发送侧 'auto' 解析同源）。旧后端缺
        // conv_lang 键 → null=不预告（特性探测，不显示错的）。
        this._effConvLang = ("conv_lang" in d) ? String(d.conv_lang || "").toLowerCase() : null;
        // 语言能力守卫数据源（特性探测：旧后端缺键/空列表 → null=不判不冤枉）
        this._effVoiceLangs = (Array.isArray(d.voice_langs) && d.voice_langs.length)
          ? d.voice_langs.map((x) => String(x || "").toLowerCase()) : null;
        this._syncXlHint();
        const pid = d.persona_id || "";
        let name;
        if (d.persona_source === "system") {
          name = this._t("cp.voice.system_voice");
        } else if (pid) {
          const row = (this._profiles || []).find((p) => p.persona_id === pid);
          name = (row && row.name) || pid;
        } else {
          name = this._t("cp.voice.eff_global");
        }
        const bits = [this._t("cp.voice.eff_line",
          { name, backend: this._backendLabel(d.backend) })];
        if (d.is_clone) bits.push(d.ready ? "🎤" : this._t("cp.voice.eff_not_ready"));
        /* P1 降噪（2026-08-31）：状态行只留 健康点+名字·后端+短风险牌，整句
           风险/来源/体检分收进「详情」折叠区——截图实录里琥珀长句常驻面板
           前三行，重要的（会拒发）与次要的（曾失败）没有层级。短牌+点色
           保住「会不会拒发」的即时可见性，全文一击展开。 */
        const qrow = pid
          ? (this._profiles || []).find((p) => p.persona_id === pid) : null;
        const qual = (qrow && (qrow.quality === "warn" || qrow.quality === "critical"))
          ? qrow : null;
        const riskShortKey = {
          unreachable: "cp.voice.eff_s_unreachable",
          engine_offline: "cp.voice.eff_s_engine_offline",
          timing_out: "cp.voice.eff_s_timing_out",
          recent_failures: "cp.voice.eff_s_recent",
        }[d.hub_risk] || "";
        // engine_offline / timing_out 两态服务端 2026-08-22 起就会报，旧前端
        // 却只认 unreachable/recent_failures（静默漏显示）——本批一并补全。
        const riskFullKey = {
          unreachable: "cp.voice.eff_hub_unreachable",
          engine_offline: "cp.voice.eff_hub_engine_offline",
          timing_out: "cp.voice.eff_hub_timing_out",
          recent_failures: "cp.voice.eff_hub_recent",
        }[d.hub_risk] || "";
        const errRisk = d.hub_risk === "unreachable"
          || d.hub_risk === "engine_offline" || d.hub_risk === "timing_out";
        /* #250（N-4 D）：配置闸预告——人设语音配置非法（克隆无录音 / 预置态挂克隆
           引擎…）→ 生成前状态行就是红点 + 「配置需修正」+ 详情里人话 + 「去语音页修正」
           深链；服务端 tts-test 会以同一函数拦下，面板不再进合成再报「疑似无声」。
           只提示类（残留预置声名、参考音本机不在）→ 详情一行说明，不改点色。 */
        const cfgBlocked = !!d.voice_config_blocked;
        const cfgAdvisory = Array.isArray(d.voice_problems)
          ? d.voice_problems.filter((p) => p && !p.blocking) : [];
        let dot = "ok";
        if (cfgBlocked || errRisk || (qual && qual.quality === "critical")) dot = "err";
        else if (riskFullKey || qual || (d.is_clone && !d.ready)) dot = "warn";
        const detParts = [];
        const srcKey = `cp.voice.eff_src_${d.persona_source || ""}`;
        const srcTxt = this._t(srcKey);
        if (srcTxt && srcTxt !== srcKey) detParts.push(`<div>${this._esc(srcTxt)}</div>`);
        if (cfgBlocked) {
          detParts.push(`<div class="err" data-role="cfg-problem">`
            + `${this._esc(d.voice_problem_text || this._t("cp.voice.cfg_blocked_generic"))} `
            + this._cfgFixLink(d.voice_tab, pid) + `</div>`);
        } else if (cfgAdvisory.length) {
          detParts.push(`<div data-role="cfg-advisory">`
            + `${this._esc(this._t("cp.voice.cfg_advisory", { codes: cfgAdvisory.map((p) => this._cfgCodeLabel(p.code)).join("、") }))} `
            + this._cfgFixLink(d.voice_tab, pid) + `</div>`);
        }
        if (riskFullKey) {
          detParts.push(`<div class="${errRisk ? "err" : "warn"}">`
            + `${this._esc(this._t(riskFullKey))}</div>`);
        }
        if (qual) {
          const qk = qual.quality === "critical"
            ? "cp.voice.eff_quality_crit" : "cp.voice.eff_quality_warn";
          const score = (Number(qual.quality_score) || 0).toFixed(2);
          detParts.push(`<div class="${qual.quality === "critical" ? "err" : "warn"}">`
            + `${this._esc(this._t(qk, { score }))}</div>`);
        }
        // chip 式渲染（P0-2 2026-08-31）：旧 " · " 字符串拼接在 🎤 这类窄尾段
        // 折行时会在行尾留一个悬挂的「·」；分隔符改挂在后继 chip 的 ::before 上，
        // 永远跟内容走不再悬空。
        const chips = bits.filter(Boolean).map((b) =>
          `<span class="eff-bit">${this._esc(b)}</span>`).join("");
        let riskShort = "";
        if (cfgBlocked) {
          riskShort = `<span class="eff-risk err" data-role="cfg-risk">`
            + `${this._esc(this._t("cp.voice.eff_s_config"))}</span>`;
        } else if (riskShortKey) {
          riskShort = `<span class="eff-risk ${errRisk ? "err" : "warn"}">`
            + `${this._esc(this._t(riskShortKey))}</span>`;
        } else if (qual) {
          const qtag = qual.quality === "critical"
            ? "cp.voice.q_crit_tag" : "cp.voice.q_warn_tag";
          riskShort = `<span class="eff-risk ${qual.quality === "critical" ? "err" : "warn"}">`
            + `${this._esc(this._t(qtag))}</span>`;
        }
        const moreHtml = detParts.length
          ? `<span class="eff-more">${this._esc(this._t("cp.voice.eff_more"))}</span>` : "";
        const det = this.shadowRoot.querySelector('[data-role="effdetail"]');
        const wasOpen = !!(det && !det.hidden && det.innerHTML);
        box.innerHTML = `<span class="dot ${dot}"></span>` + chips + riskShort + moreHtml;
        box.hidden = false;
        if (det) {
          det.innerHTML = detParts.join("");
          // 配置需修正是「生成前就该看见的出路」：详情自动展开（其余情形保持坐席的开合选择）
          det.hidden = !((wasOpen || cfgBlocked) && detParts.length);
        }
      } catch (e) {
        if (epoch === this._epoch) { this._hideEffStatus(); this._effVoiceLangs = null; }
      }
    }

    _render() {
      this._prefillSrc = null;   // 会话切换/重渲＝消息导入上下文失效，防陈旧 media_ref 被提交
      this._epoch += 1;          // 在途请求的 UI 回写按代际作废（防「已发送」串到新会话）
      this._busy = "";
      this._previewText = null;
      this._previewPersona = "";
      this._previewFilename = "";
      this._previewXl = "";
      this._previewXlEff = "";
      this._effConvLang = null;
      this._effVoiceLangs = null;
      this._silent = false;
      this._silentKind = "";
      this._previewFallback = "";
      this._fbConfirmedKey = "";
      this._staleWas = false;
      this._previewSpeech = "";
      this._previewBasis = "";
      this._stopGenTimer();
      if (this._hintTimer) { clearTimeout(this._hintTimer); this._hintTimer = null; }
      const w = this.shadowRoot.querySelector(".wrap");
      if (!this._ctx || !this._ctx.chatKey) {
        w.className = "wrap empty";
        w.textContent = this._t("cp.voice.empty");
        return;
      }
      w.className = "wrap";
      /* P1 三区重排（2026-08-31）：①音色身份区（选择器+状态行+详情）②撰写区
         （翻译开关+文本框+计数/提示）③主操作（生成钮跟在输入正下方，全宽）
         ④结果区（预览）。改前顺序=按钮在输入之前、状态插在中间，坐席动线
         上下反复跳（写字→上→下→更下）。 */
      const tipHtml = this._tipSeen() ? "" : (
        `<div class="tip" data-role="tip"><span>${this._t("cp.voice.hint")} ${this._t("cp.voice.enroll_moved")}</span>` +
        `<button data-act="tip-got">${this._t("cp.voice.tip_got")}</button></div>`);
      w.innerHTML =
        `<div class="zone-id">
          <div class="row">
            <select data-role="persona" title="${this._esc(this._t("cp.voice.persona_title"))}"></select>
            <button data-act="unbind" title="${this._esc(this._t("cp.voice.unbind_title"))}">${this._ic("trash", 13)}</button>
          </div>
          <div data-role="effstatus" data-act="eff-toggle" class="effline" hidden
               title="${this._esc(this._t("cp.voice.eff_more_t"))}"></div>
          <div data-role="effdetail" class="effdetail" hidden></div>
        </div>
        <div class="row xlrow">
          <label title="${this._esc(this._t("cp.voice.xl_follow_t"))}"><input type="checkbox" data-role="xlfollow"${this._xlOn ? " checked" : ""} /><span>${this._t("cp.voice.xl_follow")}</span></label>
          <span class="hint xlh" data-role="xlhint"></span>
        </div>
        <textarea data-role="text" placeholder="${this._esc(this._t("cp.voice.text_ph"))}"></textarea>
        <div class="compose-foot">
          <div class="hint" data-role="msg"></div>
          <span class="cnt" data-role="cnt" hidden></span>
        </div>
        <button class="primary gen-btn" data-act="tts" data-role="gen-main">${this._genLabelHtml()}</button>
        ${tipHtml}
        <div data-role="preview" class="preview" hidden></div>`;
      this._syncXlHint();
      /* B115（实施74，B25-① 口径落定）：工具箱撤克隆「录入+登记」整块——
         登记只留人设页语音区一个入口（本组件 mode="enroll" 挂载档保留）。
         生成/试听/发送功能不动；tip 里指路人设页防「功能消失」误报
         （常驻三步说明降噪为一次性引导，cp.voice.hint/enroll_moved 词条复用）。 */
    }

    _enrollHtml() {
      return `<h5>${this._t("cp.voice.enroll_h")}</h5>
        <div data-role="esrc"></div>
        <div class="row">
          <input type="file" data-role="efile" accept="audio/*,.wav,.mp3,.m4a" />
          <input type="text" data-role="ename" placeholder="${this._esc(this._t("cp.voice.ename_ph"))}" />
          <select data-role="epersona"><option value="">${this._t("cp.voice.target_persona_opt")}</option></select>
          <select data-role="elang"><option value="Japanese">${this._t("cp.lang.ja")}</option><option value="Chinese">${this._t("cp.lang.zh")}</option><option value="English">${this._t("cp.lang.en")}</option></select>
          <button data-act="enroll-submit">${this._t("cp.voice.enroll_submit")}</button>
        </div>
        <div class="row">
          <input type="text" data-role="ereftext" placeholder="${this._esc(this._t("cp.voice.reftext_ph"))}" style="flex:1;" />
        </div>
        <div class="row">
          <label class="consent"><input type="checkbox" data-role="econsent" /><span>${this._t("cp.voice.consent_label")}</span></label>
        </div>
        <div data-role="ehint" class="hint">${this._t("cp.voice.enroll_hint")}</div>
        <div data-role="audition"></div>
        <div class="panel">
          <h5>${this._ic("refresh", 12)} ${this._t("cp.voice.reuse_h")}</h5>
          <div class="row">
            <select data-role="rfrom"><option value="">${this._t("cp.voice.src_persona_opt")}</option></select>
            <span>→</span>
            <select data-role="rto"><option value="">${this._t("cp.voice.dst_persona_opt")}</option></select>
            <button data-act="rebind">${this._t("cp.voice.copy_btn")}</button>
          </div>
        </div>
        <div class="panel">
          <h5>${this._t("cp.voice.recon_h")}</h5>
          <div class="row">
            <button data-act="reconcile">${this._t("cp.voice.recon_btn")}</button>
            <button data-act="purge-orphans">${this._t("cp.voice.purge_btn")}</button>
          </div>
          <div data-role="recon" class="recon"></div>
        </div>`;
    }

    async _loadEnrollPersonas() {
      if (!this._client || !this._client.listPersonas) return;
      try {
        const d = await this._client.listPersonas();
        const list = (d && d.summary) || [];
        const fill = (sel, filterVoice) => {
          if (!sel) return;
          let h = sel === this.shadowRoot.querySelector('[data-role="epersona"]')
            ? `<option value="">${this._t("cp.voice.target_persona_opt")}</option>`
            : (sel.getAttribute("data-role") === "rfrom" ? `<option value="">${this._t("cp.voice.src_persona_opt")}</option>` : `<option value="">${this._t("cp.voice.dst_persona_opt")}</option>`);
          list.filter((s) => !filterVoice || s.has_voice).forEach((s) => {
            h += `<option value="${this._esc(s.id)}">${this._esc(s.name || s.id)}${s.has_voice ? " 🎤" : ""}</option>`;
          });
          sel.innerHTML = h;
        };
        fill(this.shadowRoot.querySelector('[data-role="epersona"]'), false);
        fill(this.shadowRoot.querySelector('[data-role="rfrom"]'), true);
        fill(this.shadowRoot.querySelector('[data-role="rto"]'), false);
      } catch (e) { /* */ }
    }

    _onChange(e) {
      const sel = e.target.closest('[data-role="persona"]');
      if (sel) {
        this._persona = sel.value;
        this._savePersona(this._persona);
        this._syncStale();   // 换音色同样使试听过期（发送按当前音色重新合成）
        this._refreshEffStatus();
        return;
      }
      const xl = e.target.closest('[data-role="xlfollow"]');
      if (xl) {
        this._xlOn = !!xl.checked;
        this._saveXlOn(this._xlOn);
        this._syncXlHint();
        this._syncStale();   // 语言维度变了：已生成的试听按过期处理（防「听中文发外语」）
        // 通知宿主（同文档的主输入框「跟随翻译发声」行同键联动 + 埋点）
        this.dispatchEvent(new CustomEvent("cp-voice-xl-changed", {
          bubbles: true, composed: true, detail: { on: this._xlOn },
        }));
      }
    }

    /* ── P0-V2b 译声（打中文 → 客户收到目标语克隆声）────────────────────────
       开关全局持久化（与主输入框共键）；目标语恒 'auto'：由服务端按会话客户
       语言解析（与 send-voice 同一函数），解析不出=不译，fail-open 念原文。 */
    _loadXlOn() {
      try { return (localStorage.getItem(XL_KEY) || "1") === "1"; } catch (e) { return true; }
    }
    _saveXlOn(v) {
      try { localStorage.setItem(XL_KEY, v ? "1" : "0"); } catch (e) { /* */ }
    }
    /* 目标语解析（2026-08-30 用户实测修正）：坐席在翻译设置里显式选了
       「我的消息 → X」→ 语音跟着译成 X（宿主经 __cpVoiceXlPref 提供；这正是
       坐席的心智模型——「我都把翻译改成英文了」）；未设置/设为 auto → 'auto'
       =会话客户语言由服务端解析。无宿主注入（桌面独立 App/iframe）→ 'auto'。 */
    _xlTarget() {
      if (!this._xlOn) return "";
      let pref = "";
      try {
        pref = String((root.__cpVoiceXlPref && root.__cpVoiceXlPref()) || "");
      } catch (e) { pref = ""; }
      pref = pref.trim().toLowerCase();
      return (pref && pref !== "auto") ? pref : "auto";
    }
    /* 宿主/其他窗口改了开关或翻译目标 → 重读并刷新 UI（checkbox+预告+过期标记）。
       开关未变也要重算预告/过期——宿主换「我的消息 →」目标语时走的就是这条。 */
    refreshXl() {
      const v = this._loadXlOn();
      if (v !== this._xlOn) {
        this._xlOn = v;
        const cb = this.shadowRoot.querySelector('[data-role="xlfollow"]');
        if (cb) cb.checked = v;
      }
      this._syncXlHint();
      this._syncStale();
    }
    /* 语言名（cp.lang.* 词条，缺键回落代码大写）；空码返回空串 */
    _langName(code) {
      const c = String(code || "").trim().toLowerCase();
      if (!c) return "";
      const k = "cp.lang." + c.replace(/-/g, "_");
      const v = this._t(k);
      return (v && v !== k) ? v : c.toUpperCase();
    }
    /* 后端内部名 → 人话标签（cp.voice.bk_* 词条；没登记的原样显示不装懂） */
    _backendLabel(code) {
      const c = String(code || "").trim();
      if (!c) return "";
      const k = "cp.voice.bk_" + c.toLowerCase();
      const v = this._t(k);
      return (v && v !== k) ? v : c;
    }
    /* 情绪内部值 → 人话标签（cp.voice.emo_* 词条；没登记的原样显示不装懂）。
       「情绪 playful」是调试口径，坐席该看到「俏皮」。 */
    _emoLabel(code) {
      const c = String(code || "").trim().toLowerCase();
      if (!c) return "";
      const k = "cp.voice.emo_" + c.replace(/[^a-z0-9]+/g, "_");
      const v = this._t(k);
      return (v && v !== k) ? v : c;
    }
    /* 译声预告行：显式目标（我的消息 → X）→ 直接「将译成 X」（不依赖后端
       conv_lang，立即可见）；auto → 按会话客户语言（后端未回传时留空不猜、
       解析不出=「客户语言未知·按原文发声」）；关=「按原文发声」；目标语为
       粤语时给音系警示（普通话音系念粤文=「唔该」念 wú gāi，穿帮级）；
       目标语超出当前音色引擎能力（P0 2026-08-31「日文怪声」：IndexTTS-2 念
       假名=不是任何语言的怪声）→ 生成前红牌警示，别让坐席靠耳朵事后发现。 */
    _syncXlHint() {
      const el = this.shadowRoot.querySelector('[data-role="xlhint"]');
      if (!el) return;
      const paint = (txt, warn) => {
        el.textContent = txt;
        el.classList.toggle("warn", !!warn);
      };
      // 同因去重（P0 2026-08-31）：预览区已有「语种改道」说明（含一键出路）时，
      // 顶部预告让位——同一件事两处黄牌是 0831 提示风暴的成因之一。
      const langNote = this.shadowRoot.querySelector(
        '[data-role="fallback-note"][data-reason="lang"]');
      if (langNote && !langNote.hidden) { paint("", false); return; }
      if (!this._xlOn) { paint(this._t("cp.voice.xl_off"), false); return; }
      const tgt = this._xlTarget();   // 'auto' | 显式语种码
      // 预告语种：显式目标立即可见；auto 用会话客户语言（null=后端未提供）
      const lang = (tgt !== "auto") ? tgt : this._effConvLang;
      if (lang === null) { paint("", false); return; }   // 特性探测：不显示错的
      if (!lang) { paint(this._t("cp.voice.xl_unknown"), false); return; }
      const code = String(lang).toLowerCase();
      if (code === "yue") { paint(this._t("cp.voice.xl_yue_warn"), true); return; }
      if (this._langUnsupported(code)) {
        paint(this._t("cp.voice.xl_engine_unsupported",
          { lang: this._langName(code) }), true);
        return;
      }
      paint(this._t("cp.voice.xl_to", { lang: this._langName(code) }), false);
    }
    /* 目标语是否超出当前音色主路的可念语种（voice_langs 缺席/空=不判不冤枉）。
       前缀比较（zh-tw→zh）：变体归母语种，避免对繁体中文这类同引擎可念的
       变体误报（粤语另有专门音系警示，不走本判定）。 */
    _langUnsupported(code) {
      const langs = this._effVoiceLangs;
      if (!Array.isArray(langs) || !langs.length) return false;
      const p = String(code || "").toLowerCase().split("-")[0];
      return !!p && langs.indexOf(p) < 0;
    }

    async _onClick(e) {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const act = b.getAttribute("data-act");
      if (act === "tts") return this._genTts();
      if (act === "send") return this._sendVoice();
      if (act === "clear-preview") { this._clearPreview(); return; }
      if (act === "cancel-gen") { this._cancelGen(); return; }
      if (act === "xl-off-regen") { this._xlOffRegen(); return; }
      if (act === "eff-toggle") { this._toggleEffDetail(); return; }
      if (act === "tip-got") { this._dismissTip(); return; }
      if (act === "unbind") return this._unbind();
      /* B115：toggle-enroll / clear-src 分支随工具箱登记块一并撤除——
         enroll-submit/force/rebind/reconcile 仍服务人设页 mode="enroll" 档。 */
      if (act === "enroll-submit") return this._enroll(false);
      if (act === "enroll-force") return this._enroll(true);
      if (act === "rebind") return this._rebind();
      if (act === "reconcile") return this._reconcile();
      if (act === "purge-orphans") return this._purgeOrphans();
      if (act === "purge-one") return this._purgeOne(b.getAttribute("data-voice"), b.getAttribute("data-force") === "1");
    }

    _text() {
      const ta = this.shadowRoot.querySelector('[data-role="text"]');
      return ta ? String(ta.value || "").trim() : "";
    }

    /* 音色详情开合（状态行整行可点；无详情内容时 no-op） */
    _toggleEffDetail() {
      const det = this.shadowRoot.querySelector('[data-role="effdetail"]');
      if (!det || !det.innerHTML) return;
      det.hidden = !det.hidden;
    }

    /* 首次使用提示（P1 降噪）：三步说明从常驻改为一次性引导，「知道了」后
       localStorage 记忆不再出现（storage 异常按已读处理，绝不常驻打扰）。 */
    _tipSeen() {
      try { return localStorage.getItem("cpv.tip.v1") === "1"; } catch (e) { return true; }
    }
    _dismissTip() {
      try { localStorage.setItem("cpv.tip.v1", "1"); } catch (e) { /* */ }
      const t = this.shadowRoot.querySelector('[data-role="tip"]');
      if (t) t.remove();
    }

    /* 输入框随内容自然长高（52→140px 封顶，超出滚动；手动拖拽 resize 仍可用） */
    _autoGrow(ta) {
      const el = ta || this.shadowRoot.querySelector('[data-role="text"]');
      if (!el) return;
      try {
        el.style.height = "auto";
        el.style.height = Math.min(140, Math.max(56, el.scrollHeight)) + "px";
      } catch (e) { /* */ }
    }

    async _genTts() {
      const text = this._text();
      if (!text) { this._hint(this._t("cp.voice.need_text"), false); return; }
      if (text.length > this._maxChars) {   // 超限前端先拦（服务端同上限，省一次白跑）
        this._hint(this._t("cp.voice.too_long", { max: this._maxChars }), false);
        return;
      }
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (!box) return;
      if (this._busy) return;   // 在途互斥：连点不重复烧合成
      const ep = this._epoch;
      const persona = this._persona || "";   // 生成基准取点击时刻（生成期间的改动会被判过期）
      const xlt = this._xlTarget();          // 译声基准同点击时刻（开关翻转会被判过期）
      this._setBusy("tts");
      box.hidden = false;
      box.classList.remove("stale");
      // 上一条的裁决状态随预览一起作废（重新生成=新的产物新的判定）
      this._silent = false;
      this._silentKind = "";
      this._previewFallback = "";
      this._staleWas = false;
      this._previewSpeech = "";
      this._previewBasis = "";
      // 等待态一次成形（计秒只更新 span，别整块重写——否则「取消」按钮每秒被销毁重建）
      box.innerHTML =
        `<span data-role="gen-wait"></span>` +
        `<div class="row pv-foot">` +
        `<button data-act="cancel-gen">${this._t("cp.voice.cancel_btn")}</button></div>`;
      const waitEl = box.querySelector('[data-role="gen-wait"]');
      const t0 = Date.now();
      this._stopGenTimer();
      // 预估时长与服务端克隆预算同公式（0.45s/字+10s，封顶 190s）；短文本多为
      // 秒级热路，报预估反而吓人 → 仅 ≥40 字才显示。
      const est = Math.min(190, Math.round(text.length * 0.45 + 10));
      const tick = () => {
        if (!waitEl) return;
        const s = Math.floor((Date.now() - t0) / 1000);
        waitEl.textContent = text.length >= 40
          ? this._t("cp.voice.gen_wait_est", { s, est })
          : this._t("cp.voice.gen_wait", { s });
      };
      tick();
      this._genTimer = setInterval(tick, 1000);
      let d = null;
      let reqFail = false;
      try {
        // 带会话上下文：试听与发送同一组解析入参（试听=发送 契约）
        const c = this._ctx || {};
        d = await this._client.voiceTts({
          text, persona_id: persona || undefined,
          chat_key: c.chatKey || undefined,
          platform: c.platform || undefined,
          account_id: c.accountId || undefined,
          // 译声：'auto'=会话客户语言（服务端解析，与发送同源）；开关关=不传
          target_lang: xlt || undefined,
        });
      } catch (e) { reqFail = true; }
      this._stopGenTimer();
      if (ep !== this._epoch) return;   // 已切会话：结果作废，不回写新会话 UI
      this._setBusy("");
      const url = d ? (d.dataUrl || d.audio_url ||
        (d.filename ? `/api/voice/tts-file/${encodeURIComponent(d.filename)}` : "")) : "";
      /* #149 选声金标（2026-09-02）：所选人设必须就是合成路由用的人设。服务端
         失配即拒（reason=voice_selection:*）；成功响应也复核 requested==resolved
         ——「选 X 出 Y」宁可报错也绝不把 Y 的声音当 X 的试听摆出来。 */
      const selBad = CpVoice.selectionMismatch(persona, d);
      const cfgBlocked = CpVoice.configBlocked(d);
      if (reqFail || !url || selBad) {
        // 失败态同样要能清除/重试——错误文案赖着不走与旧预览赖着不走是同一个病
        let msg;
        let extra = "";
        if (reqFail) {
          msg = this._t("cp.voice.req_fail");
        } else if (selBad) {
          msg = selBad === "persona_not_found"
            ? this._t("cp.voice.sel_missing")
            : this._t("cp.voice.sel_mismatch",
                { resolved: (d && (d.resolved_persona_id
                    || (d.voice_meta && d.voice_meta.persona_id))) || "-" });
          this._warnBeacon("sel_mismatch");
        } else if (cfgBlocked) {
          /* #250（N-4 D）：服务端配置闸拦下＝第三种结论「⚠ 配置需修正」——没进合成，
             不是「疑似无声」；文案用服务端人话（与人设页保存 400 同词条），出路是
             「去语音页修正」深链而不是「重试」（重试只会再撞同一道闸）。 */
          msg = this._t("cp.voice.cfg_blocked",
            { msg: (d && (d.message || this._cfgCodeLabel(cfgBlocked))) || cfgBlocked });
          extra = this._cfgFixLink(d && d.voice_tab, (d && d.persona_id) || persona);
          this._warnBeacon("config");
        } else {
          msg = this._t("cp.voice.gen_fail",
            { msg: (d && (d.message || d.error)) || this._t("cp.voice.tts_unavailable") });
        }
        this._previewText = null;
        this._previewFilename = "";
        this._previewXl = "";
        this._previewXlEff = "";
        this._previewFallback = "";
        box.innerHTML =
          `<span class="${cfgBlocked ? "warn" : "err"}" data-role="gen-error" data-reason="${this._esc(cfgBlocked ? "voice_config" : "")}">${this._esc(msg)}</span>` +
          (extra ? ` ${extra}` : "") +
          `<div class="row pv-foot">` +
          (cfgBlocked ? "" : `<button data-act="tts">${this._t("cp.voice.retry_btn")}</button>`) +
          `<button data-act="clear-preview" title="${this._esc(this._t("cp.voice.clear_t"))}">${this._ic("x", 12)}</button></div>`;
        return;
      }
      this._previewText = text;
      this._previewPersona = persona;
      this._previewFilename = String((d && d.filename) || "");
      this._previewXl = xlt;
      // #250（N-4 C）：译声基准 = 服务端**有效目标语**（不论译没译），见 xlBaseline。
      // #121 时只记「voice_translated 才有目标语、未译=空串」，于是中文客户 + 中文
      // 文本（identity 不译）的基准被当成 'auto'，与会话语言 'zh' 一比就「过期」——
      // 钧机两次生成一结束发送就灰，用户什么都没改。
      this._previewXlEff = CpVoice.xlBaseline(d, xlt, this._effConvLang);
      const _vm = ((d && d.voice_meta) || {});
      const fb = _vm.fallback_from || _vm.voice_mapped_from;
      /* 回落原因分支（P0 2026-08-31 提示风暴复盘）：服务端 fallback_reason
         （additive）优先；老后端缺键时就地推断「语种改道」（本条确已翻译且
         目标语超出主路能力）。语种改道是刻意保护——info 蓝条+一键出路；
         「通道中断→重试/报障」文案只留给真通道故障（0831 实测通道全绿却教
         坐席去报障，纯制造无效工单）。 */
      let fbReason = String(_vm.fallback_reason || "");
      let fbLang = String(_vm.fallback_lang || "");
      if (fb && !fbReason && d.voice_translated
          && this._langUnsupported(String(d.target_lang || ""))) {
        fbReason = "lang_unsupported";
        fbLang = String(d.target_lang || "");
      }
      this._previewFallback = fb ? (fbReason || "generic") : "";
      let fbNote = "";
      if (fb) {
        if (fbReason === "lang_unsupported") {
          const ln = this._langName(fbLang || String(d.target_lang || ""));
          fbNote = `<div class="pv-note info" data-role="fallback-note" data-reason="lang">`
            + `<span>${this._esc(this._t("cp.voice.fallback_lang", { lang: ln }))}</span>`
            + `<button data-act="xl-off-regen">${this._t("cp.voice.fallback_lang_btn")}</button></div>`;
        } else if (fbReason === "quota") {
          fbNote = `<div class="pv-note warn" data-role="fallback-note" data-reason="quota">`
            + `<span>${this._esc(this._t("cp.voice.fallback_quota"))}</span></div>`;
        } else {
          fbNote = `<div class="pv-note warn" data-role="fallback-note" data-reason="channel">`
            + `<span>${this._esc(this._t("cp.voice.fallback_warn"))}</span></div>`;
        }
        this._warnBeacon(fbReason === "lang_unsupported" ? "fb_lang"
          : (fbReason === "quota" ? "fb_quota" : "fb_channel"));
      }
      // 译声：把服务端真实念出的译稿亮给坐席（所见即所念）；未译=不渲染该行，
      // translated 是服务端真值，绝不谎报「已译」。
      const spokenLine = (d.voice_translated && d.spoken_text)
        ? `<div class="hint pv-spoken">${this._esc(this._t("cp.voice.spoken_as",
            { lang: this._langName(d.target_lang || "") }))}: <span class="sp-txt">${this._esc(d.spoken_text)}</span></div>`
        : "";
      /* 结果区（#250 N-4 B 重排）：回落说明（info/warn，是「说明」不是结论）+
         **唯一结论行** `[data-role="verdict"]`（✅ 可发送 / ❌ 无声或念错 / ⚠ 已过期 /
         ℹ 未能核验，由 _syncNotes 按 CpVoice.panelVerdict 单出口渲染）+ 脚部按钮：
         过期时主按钮就是「重新生成」（发送钮整个让位，不再是「灰掉的发送」——
         灰按钮说不出为什么灰、也给不出出路）。旧版红条 + 橙条两个独立 banner
         各自 hidden、各自为政，且 .pv-note{display:flex} 让 hidden 全部失效——钧机
         两张截图三条同屏就是这么来的。 */
      this._previewSpeech = String(_vm.speech || "").toLowerCase();
      this._previewBasis = String(_vm.speech_basis || "");
      box.innerHTML =
        `<div class="row pv-hd"><span>${this._ic("mic", 12)} ${this._t("cp.voice.preview")}</span>` +
        `<span class="pv-actions">` +
        `<button data-act="tts" title="${this._esc(this._t("cp.voice.regen_t"))}">${this._ic("refresh", 12)} ${this._t("cp.voice.regen_btn")}</button>` +
        `<button data-act="clear-preview" title="${this._esc(this._t("cp.voice.clear_t"))}">${this._ic("x", 12)}</button>` +
        `</span></div>` +
        `<audio controls src="${url}"></audio>` +
        `<canvas data-role="wave" aria-hidden="true"></canvas>` +
        spokenLine +
        this._metaLine(d) +
        fbNote +
        `<div class="pv-verdict" data-role="verdict" data-detector="v4-single" data-state="" data-speech="${this._esc(this._previewSpeech)}" hidden></div>` +
        `<div class="row pv-foot">` +
        `<button class="primary" data-act="tts" data-role="regen-main" hidden>${this._ic("refresh", 12)} ${this._t("cp.voice.regen_btn")}</button>` +
        `<button class="primary" data-act="send">${this._t("cp.voice.send_btn")}</button></div>`;
      // 服务端已确认无声/念错 → 不等本地解码，立即禁发（消灭「红条未出、发送可点」的空窗）
      if (_vm.speech === "silent" || _vm.speech === "garbled") {
        this._silent = true;
        this._silentKind = _vm.speech;
        this._warnBeacon(_vm.speech === "garbled" ? "garbled_server" : "silent_server");
      }
      this._syncStale();   // 生成期间若已改字/换音色，立即标过期；同时首绘结论行
      this._sniffSilence(url, _vm, Number(d && d.duration_sec) || 0);   // 有声终审：服务端裁决优先，客户端解码只兜底/画波形
    }

    /* ── #149/#121 纯核心（不依赖 this/DOM/i18n，desktop/test 常驻门禁钉住）── */

    /** 选声失配判定：返回 "" / "persona_not_found" / "persona_mismatch"。
     *  - 服务端拒绝：d.reason = "voice_selection:<why>"；
     *  - 服务端成功但复核不过：voice_meta.requested_persona_id（新后端才有）
     *    与 voice_meta.persona_id 不等，或与本地请求时的选择不等；
     *  - 旧后端（无 requested_persona_id）/ 未显式选人设 / 选「系统通用音色」→ ""。 */
    static selectionMismatch(requested, d) {
      const req = String(requested || "");
      const r = String((d && d.reason) || "");
      if (r.indexOf("voice_selection:") === 0) {
        const why = r.slice("voice_selection:".length);
        return why === "persona_not_found" ? why : "persona_mismatch";
      }
      if (!req || req === "__system__" || !d || d.ok === false) return "";
      const m = d.voice_meta || {};
      const served = String(m.requested_persona_id || "");
      if (!served) return "";                 // 旧后端：无可核对真值，不判
      const resolved = String(m.persona_id || "");
      if (served !== req || (resolved && resolved !== req)) return "persona_mismatch";
      return "";
    }

    /** 有声/无声最终裁决（#121 三进宫除根）：
     *  meta.speech  服务端终审（voiced/silent/garbled/unknown|缺省）；
     *  sniff        客户端解码结果 {peak, rms, decodedSec} 或 null（解码失败）；
     *  serverSec    服务端报告的时长（>0 才参与解码合理性闸）。
     *  规则：服务端 voiced → 绝不判无声；服务端 silent/garbled → 禁发；服务端无
     *  裁决时才用客户端双信号（峰值<0.002 且 RMS<0.0008），且解码出的时长与服务端
     *  时长相差过半＝解码器读错容器（WAV 被当 mp3 之类）→ 不判（不冤枉）。
     *  ``kind``（#161 2026-09-03）：禁发的**原因**——"silent"=没声音、"garbled"=
     *  有声但念错（克隆引擎念不了这门语言，产物是有能量的乱音）。两种事故的
     *  出路不同（无声重试即可，念错要换语言/换标准声），文案必须分开。 */
    /** 禁发红条的文案键：#161 起「疑似无声」与「疑似念错」是两条独立事故线，
     *  同一条红字既说不清原因也给不出正确出路（无声重试即可，念错必须换语言
     *  或改用标准声）。garbled=服务端终审判念错；silent=服务端确认无声；
     *  其余（unknown/缺省=客户端启发式兜底判定）沿用「疑似无声」旧文案。 */
    static blockNoteKey(speech) {
      const s = String(speech || "").toLowerCase();
      if (s === "garbled") return "cp.voice.garbled_note";
      if (s === "silent") return "cp.voice.silent_note_server";
      return "cp.voice.silent_note";
    }

    /** 结果区唯一结论（#250 N-4 B，纯函数，desktop/test 常驻门禁）。
     *  入参：speech=服务端终审（voiced/silent/garbled/unknown/''）；silent=阻发闸
     *  （服务端 silent/garbled 或客户端兜底判无声）；kind=阻发原因（silent/garbled）；
     *  stale=试听已过期。出参 {state, level, key, canSend}：
     *    stale   → warn  「试听已过期 → 重新生成」（过期＝这条产物已不代表当前选择，
     *                    它身上的有声/无声结论无论真假都只对旧产物成立，先于一切）
     *    blocked → err   「无声 / 念错 + 原因 + 重新生成」（文案键沿 blockNoteKey：
     *                    服务端确认无声 / 念错 / 客户端兜底疑似无声 三条各归各）
     *    ok      → ok    「已核有声，可发送」（服务端 voiced）
     *    unverified → info「未能核验（服务端无证据）—— 请先听一遍再发」（可发送）
     *  四态互斥、恒返回其一：同一时刻面板只可能出现一个结论。 */
    /** 服务端配置闸（#250 N-4 D，纯函数）：tts-test 回 ``reason=voice_config:<code>`` →
     *  返回问题码（非空＝被拦：不进合成、面板给 ⚠ 配置需修正 + 去语音页）；其余 ""。 */
    static configBlocked(d) {
      const r = String((d && d.reason) || "");
      if (r.indexOf("voice_config:") !== 0) return "";
      return r.slice("voice_config:".length) || "config";
    }

    /** 译声维度的过期基准（#250 N-4 C，纯函数）：这条试听生成时的**有效目标语**。
     *  d=tts-test 回包；xlt=点击时刻的目标语设置（'' 关 / 'auto' / 显式语种）；
     *  effConvLang=点击时刻已知的会话客户语言（effective-config，可为 null/''）。
     *  规则：
     *    关（''）                       → ''（发送不译）
     *    服务端确已翻译                 → d.target_lang（真实译向）
     *    服务端回了 target_lang_resolved → 照单（新后端：'auto' 解析结果，identity 不译
     *                                     时也在；解析不出为 ''）
     *    显式语种（旧后端）             → 该语种（identity 不译也是它）
     *    'auto'（旧后端）               → 客户端已知的会话语言（服务端没译＝目标语
     *                                     与原文同语种或未知，发送时解析同样的值）
     *  之后 _isStale 用它与当前有效目标语比：只有目标语**真的变了**才过期；
     *  生成完成、会话语言异步回填、面板重绘都不会翻它。 */
    static xlBaseline(d, xlt, effConvLang) {
      const raw = String(xlt || "").trim().toLowerCase();
      if (!raw) return "";
      if (d && d.voice_translated) return String(d.target_lang || "").toLowerCase();
      if (d && typeof d.target_lang_resolved === "string") return d.target_lang_resolved.toLowerCase();
      if (raw !== "auto") return raw;
      return String(effConvLang || "").toLowerCase();
    }

    static panelVerdict(o) {
      const inp = o || {};
      const speech = String(inp.speech || "").toLowerCase();
      if (inp.stale) {
        return { state: "stale", level: "warn", key: "cp.voice.stale_note", canSend: false };
      }
      if (inp.silent) {
        // 文案按**证据来源**分：服务端终审 silent/garbled 用「服务端已确认 / 念错」；
        // 服务端拿不到证据（unknown/缺键）而由客户端解码兜底判无声 → 只能说「疑似」。
        const k = (speech === "silent" || speech === "garbled") ? speech : "client";
        return { state: "blocked", level: "err", key: CpVoice.blockNoteKey(k), canSend: false };
      }
      if (speech === "voiced") {
        return { state: "ok", level: "ok", key: "cp.voice.v_ok", canSend: true };
      }
      return { state: "unverified", level: "info", key: "cp.voice.v_unverified", canSend: true };
    }

    static speechDecision(meta, sniff, serverSec) {
      const s = String((meta && meta.speech) || "").toLowerCase();
      if (s === "voiced") return { silent: false, basis: "server-voiced", kind: "" };
      if (s === "silent") return { silent: true, basis: "server-silent", kind: "silent" };
      if (s === "garbled") return { silent: true, basis: "server-garbled", kind: "garbled" };
      if (!sniff || typeof sniff.peak !== "number") return { silent: false, basis: "no-decode", kind: "" };
      const sec = Number(serverSec) || 0;
      const dec = Number(sniff.decodedSec) || 0;
      if (sec > 0.5 && dec > 0 && Math.abs(dec - sec) > Math.max(1, sec * 0.5)) {
        return { silent: false, basis: "decode-implausible", kind: "" };
      }
      const silent = sniff.peak < 0.002 && sniff.rms < 0.0008;
      return { silent, basis: silent ? "client-dual" : "client-ok", kind: silent ? "silent" : "" };
    }

    /** 有声/无声裁决 + 响度包络（B61 → #121 三进宫除根，2026-09-02）。
     *
     *  三轮误报史：0831 单信号峰值阈值、0901 双信号（峰值+RMS）——1.0.70 仍复发
     *  （KKXSTU：同一预览 wav 被 Whisper 全文转录成功，红条照亮）。根因是让
     *  **浏览器端解码启发式当终审**：容器/MIME 打架、解码器读错头都会产出一段
     *  「合法但近零」的样本，两条阈值怎么调都只是换一批误报。
     *
     *  本轮：服务端随试听响应下发终审 voice_meta.speech（转写命中 + 落盘字节能量，
     *  见 src/ai/speech_verdict.py）——voiced 绝不亮红条、silent 直接亮；只有服务端
     *  拿不到证据（unknown/旧后端）才用本地双信号兜底，且加解码合理性闸（解码时长
     *  与服务端时长相差过半＝解码器读错，不判）。裁决核心 CpVoice.speechDecision
     *  是纯函数（desktop/test 常驻门禁）。本地解码始终跑一遍——响度包络（波形条）
     *  仍是坐席「一眼看见有没有声」的视觉自证层。任何失败静默；epoch 变了
     *  （切会话/重新生成）不回写 DOM。 */
    async _sniffSilence(url, meta, serverSec) {
      const ep = this._epoch;
      let sniff = null;
      let buckets = null;
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC && window.fetch) {
          const r = await fetch(url);
          if (r.ok) {
            const buf = await r.arrayBuffer();
            const ac = new AC();
            try {
              const audio = await ac.decodeAudioData(buf);
              const nch = (audio && audio.numberOfChannels) || 0;
              if (nch && audio.length) {
                const len = audio.length;
                const step = Math.max(1, Math.floor(len / 48000));
                let peak = 0;
                let sumSq = 0;
                let n = 0;
                // 响度包络桶（P1 2026-08-31）：同一遍采样顺手聚 96 桶峰值——
                // 「有没有声」从纯凭耳朵/红条变成一眼可见的波形条（无声=扁平基线）。
                // 多声道取各道最大值（单看 0 号声道会把「内容只在右声道」判成无声）。
                const NB = 96;
                buckets = new Float32Array(NB);
                const chans = [];
                for (let c = 0; c < nch; c += 1) chans.push(audio.getChannelData(c));
                for (let i = 0; i < len; i += step) {
                  let a = 0;
                  for (let c = 0; c < chans.length; c += 1) {
                    const v = Math.abs(chans[c][i]);
                    if (v > a) a = v;
                  }
                  if (a > peak) peak = a;
                  sumSq += a * a;
                  n += 1;
                  const b = Math.min(NB - 1, Math.floor((i / len) * NB));
                  if (a > buckets[b]) buckets[b] = a;
                }
                sniff = { peak, rms: n ? Math.sqrt(sumSq / n) : 0,
                          decodedSec: audio.duration || 0 };
              }
            } finally {
              try { ac.close(); } catch (e) { /* 忽略 */ }
            }
          }
        }
      } catch (e) { sniff = null; }
      if (ep !== this._epoch) return;
      if (buckets) this._drawWave(buckets);
      const verdict = CpVoice.speechDecision(meta, sniff, serverSec);
      const note = this.shadowRoot.querySelector('[data-role="verdict"]');
      if (note) note.setAttribute("data-basis", verdict.basis);
      if (!verdict.silent || this._silent) return;   // 服务端 silent/garbled 已在渲染时禁发，不重复计数
      // 阻发闸（P0 2026-08-31）：红字劝「不要发送」但按钮仍亮蓝可点＝视觉与
      // 行为自相矛盾（0831 截图实锤）——阳性即禁发，重新生成清除。
      this._silent = true;
      this._silentKind = verdict.kind || "silent";
      this._warnBeacon("silent");
      this._syncNotes();
    }

    /* 响度包络渲染（P1 2026-08-31）：_sniffSilence 已解码的 PCM 顺手成像。
       峰值归一（听感相对量）；全静音=1px 扁平基线，视觉即「空」。画布缺席/
       任何异常静默跳过——纯增益，绝不影响主流程。 */
    _drawWave(buckets) {
      try {
        const cv = this.shadowRoot.querySelector('[data-role="wave"]');
        if (!cv || !cv.getContext) return;
        const dpr = Math.min(2, Number(root.devicePixelRatio) || 1);
        const w = Math.max(60, cv.clientWidth || 240);
        const h = Math.max(16, cv.clientHeight || 24);
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
        const g = cv.getContext("2d");
        if (!g) return;
        g.scale(dpr, dpr);
        g.clearRect(0, 0, w, h);
        let peak = 0;
        for (let i = 0; i < buckets.length; i += 1) {
          if (buckets[i] > peak) peak = buckets[i];
        }
        const n = buckets.length;
        const bw = w / n;
        const mid = h / 2;
        g.fillStyle = "rgba(84,167,245,.75)";
        for (let i = 0; i < n; i += 1) {
          const v = peak > 0 ? (buckets[i] / peak) : 0;
          const bh = Math.max(1, v * (h - 2));
          g.fillRect(i * bw + Math.min(1, bw * 0.15), mid - bh / 2,
            Math.max(1, bw * 0.7), bh);
        }
        cv.setAttribute("data-painted", "1");
      } catch (e) { /* 纯增益：失败不影响任何主流程 */ }
    }

    async _sendVoice() {
      if (this._busy) return;   // 在途互斥：连点=双发客户，必须拦
      const c = this._ctx;
      const text = this._text();
      if (!text || !c) return;
      if (this._isStale()) {   // 双保险：按钮已禁用，键盘/时序穿透也拦
        this._hint(this._t("cp.voice.stale_note"), false);
        return;
      }
      if (this._silent) {   // 阻发闸双保险（同上：时序穿透也拦）
        this._hint(this._t(this._blockTitleKey()), false);
        return;
      }
      /* 降级发送确认（P1-3 2026-08-31）：非克隆声不靠黄字自觉——发送前显式
         确认一次「客户会听到与人设不同的声音」；同会话记住选择（换会话/重渲
         即复位），不重复骚扰。原「回落必须显式」不变量由本确认承接且更强。 */
      if (this._previewFallback && this._fbConfirmedKey !== this._convKey()) {
        if (!confirm(this._t("cp.voice.fallback_send_confirm"))) return;
        this._fbConfirmedKey = this._convKey();
      }
      const ep = this._epoch;
      this._setBusy("send");
      let d = null;
      let reqFail = false;
      try {
        d = await this._client.sendVoice({
          platform: c.platform,
          account_id: c.accountId || "default",
          chat_key: c.chatKey,
          text,
          persona_id: this._persona || undefined,
          // 译声（与试听同口径）：'auto'=会话客户语言由服务端解析后先译后念；
          // fail-open 在服务端（翻译失败念原文并如实标记），前端只透传。
          target_lang: this._xlTarget() || undefined,
          // 所听即所发（P1）：带回试听产物名，服务端校验（同文本/同音色/未过期）
          // 通过则直接复用试听音频——客户听到的与坐席试听的逐字节一致，且省一次合成。
          preview_filename: this._previewFilename || undefined,
          // P0 幂等键（与主输入框语音发送同口径）：双窗口/重放场景服务端拒重，
          // 语音双发还烧双份 TTS/GPU，比文本更值得拦。
          client_msg_id: "cpv-" + Date.now().toString(36) + "-"
            + Math.random().toString(36).slice(2, 10),
        });
      } catch (e) { reqFail = true; }
      if (ep !== this._epoch) return;   // 已切会话：不把结果回写到新会话 UI
      this._setBusy("");
      if (!reqFail && d && d.ok) {
        // 发送即闭环：清预览+清文字，界面回到「可写下一条」状态（与主输入框行为对齐）
        this._clearPreview();
        const ta = this.shadowRoot.querySelector('[data-role="text"]');
        if (ta) ta.value = "";
        const m = d.voice_meta || {};
        // 译声：如实告知「客户听到的是哪种语言」（voice_meta.translated 为真值源）
        const xlNote = (m.translated && m.target_lang)
          ? ` · ${this._t("cp.voice.sent_xl", { lang: this._langName(m.target_lang) })}` : "";
        this._hint(
          this._t("cp.voice.sent")
            + (m.provider ? ` (${this._backendLabel(m.provider)}${m.emotion ? " / " + this._emoLabel(m.emotion) : ""})` : "")
            + xlNote,
          true);
        this._hintResetLater();
        this.dispatchEvent(new CustomEvent("cp-voice-sent", {
          bubbles: true, composed: true,
          // reused=true ⇒ 客户收到的就是坐席试听的那份音频；translated=true ⇒
          // 客户听到的是译稿（宿主按两维分桶埋点）
          detail: { reused: !!(d && d.reused_preview), translated: !!m.translated },
        }));
      } else if (reqFail || (d && d.status === 0 && d.code === "network")) {
        /* #164（2026-09-05 J-4 C）：没拿到 HTTP 响应（fetch TypeError / 断网 / 后端重启窗）
           ≠ 发送失败——服务端可能已收到并发出。红字「发送失败」会诱导重发（客户收两条），
           改按「结果未知」黄字 + 让宿主刷一次消息流由坐席亲眼确认；红字只留给后端明确
           返回的失败（下面分支）。同时落一行 console 供值守拿包（桌面壳 → renderer 日志）。 */
        try {
          console.warn("[send-voice] no-response url=/api/unified-inbox/send-voice src=cp-voice reason="
            + ((d && d.error) || "network"));
        } catch (_e) { /* */ }
        this._hint(this._t("cp.voice.result_unknown"), "warn");
        this.dispatchEvent(new CustomEvent("cp-voice-result-unknown", { bubbles: true, composed: true }));
      } else {
        this._hint(this._t("cp.voice.send_fail",
          { msg: (d && (d.message || d.error || d.detail || d.reason)) || this._t("cp.voice.need_online") }), false);
      }
    }

    async _unbind() {
      // "__system__"（系统通用音色）不是人设档，没有可解绑的声纹
      if (!this._persona || this._persona === "__system__") {
        this._hint(this._t("cp.voice.need_pick_unbind"), false);
        return;
      }
      if (!confirm(this._t("cp.voice.unbind_confirm"))) return;
      const purge = confirm(this._t("cp.voice.purge_cloud_confirm"));
      try {
        const d = await this._client.voiceUnbind({ persona_id: this._persona, purge_cloud: purge });
        if (d && d.ok) {
          this._hint(this._t("cp.voice.unbound"), true);
          await this._loadProfiles();
        } else this._hint((d && (d.message || d.error)) || this._t("cp.voice.unbind_fail"), false);
      } catch (e) { this._hint(this._t("cp.voice.unbind_fail"), false); }
    }

    /* B115（实施74）：prefillEnroll「从消息一键导入」随工具箱登记块一并撤除
      （撤前已无任何宿主调用方=死代码）；登记单一入口=人设页语音区
       mode="enroll" 档。_prefillSrc 字段保留恒 null（_enroll 的 src 分支自然
       不走，改动面最小）。 */

    async _enroll(force) {
      const file = this.shadowRoot.querySelector('[data-role="efile"]');
      const name = (this.shadowRoot.querySelector('[data-role="ename"]').value || "").trim();
      const persona = this.shadowRoot.querySelector('[data-role="epersona"]').value;
      const lang = this.shadowRoot.querySelector('[data-role="elang"]').value;
      const refEl = this.shadowRoot.querySelector('[data-role="ereftext"]');
      const refText = ((refEl && refEl.value) || "").trim();
      const hint = this.shadowRoot.querySelector('[data-role="ehint"]');
      const src = (this._prefillSrc && this._prefillSrc.media_ref) ? this._prefillSrc : null;
      const hasFile = !!(file && file.files && file.files[0]);
      if (!src && !hasFile) { hint.textContent = this._t("cp.voice.need_file"); return; }
      if (!name || !persona) { hint.textContent = this._t("cp.voice.need_name_persona"); return; }
      // 授权闸门（前端第一道；后端 owner_consent 同语义强校验）
      const consentEl = this.shadowRoot.querySelector('[data-role="econsent"]');
      if (!consentEl || !consentEl.checked) {
        hint.textContent = "❌ " + this._t("cp.voice.consent_need");
        return;
      }
      hint.textContent = this._t("cp.voice.enrolling");
      const extra = { owner_consent: "1" };
      if (force) extra.force = "1";
      if (src) {
        extra.media_ref = src.media_ref;
        extra.platform = src.platform;
        extra.conversation_id = src.conversation_id;
        extra.message_id = src.message_id;
      }
      try {
        let d;
        if (!src && root.shell && root.shell.voiceEnroll) {
          const b64 = await fileToB64(file.files[0]);
          d = await this._client.voiceEnroll({
            audio_b64: b64, filename: file.files[0].name,
            persona_id: persona, preferred_name: name, language_type: lang,
            reference_text: refText, ...extra,
          });
        } else {
          d = await this._client.voiceEnroll({
            ...(src ? {} : { file: file.files[0] }),
            persona_id: persona, preferred_name: name, language_type: lang,
            reference_text: refText, ...extra,
          });
        }
        if (d && d.ok) {
          // 质检回显：自动裁剪/警告项跟成功提示一起给，坐席对素材质量有数
          const qz = d.quality || {};
          const bits = [];
          if (qz.curated) bits.push(this._t("cp.voice.q_curated"));
          if (qz.level === "warn" && Array.isArray(qz.issues) && qz.issues.length) {
            bits.push(qz.issues.join(" / "));
          }
          hint.textContent = this._t("cp.voice.enroll_ok") + (bits.length ? " · " + bits.join(" · ") : "");
          this._persona = persona;
          this._savePersona(persona);
          await this._loadProfiles();
          const sel = this.shadowRoot.querySelector('[data-role="persona"]');
          if (sel) sel.value = persona;
          /* #149（2026-09-02）：登记成功广播给宿主——人设工作室抽屉据此回灌
             voice_profile/rev（否则抽屉「保存」会拿登记前的空 backend 把刚登记
             的克隆档顶掉，服务端另有守卫兜底，这里让界面同步不撒谎）。 */
          this.dispatchEvent(new CustomEvent("cp-voice-enrolled", {
            bubbles: true, composed: true,
            detail: { persona_id: persona, mode: String(d.mode || ""),
                      reference_audio_path: String(d.reference_audio_path || "") },
          }));
          await this._audition(persona);
        } else {
          const qz = (d && d.quality) || {};
          const tips = (Array.isArray(qz.tips) && qz.tips.length) ? " " + qz.tips.join(" ") : "";
          hint.textContent = "❌ " + ((d && (d.message || d.reason)) || this._t("cp.voice.enroll_failed")) + tips;
          if (d && /^ref_/.test(String(d.reason || ""))) {
            // 质检拒绝 → 主管逃生门：同素材带 force 重新提交
            hint.insertAdjacentHTML(
              "beforeend",
              ` <button data-act="enroll-force" title="${this._esc(this._t("cp.voice.force_t"))}">${this._esc(this._t("cp.voice.force_btn"))}</button>`);
          }
        }
      } catch (e) { hint.textContent = "❌ " + this._t("cp.voice.req_fail"); }
    }

    async _audition(persona_id) {
      const box = this.shadowRoot.querySelector('[data-role="audition"]');
      if (!box) return;
      box.textContent = this._t("cp.voice.gen_audition");
      try {
        const d = await this._client.voiceTts({ text: this._t("cp.voice.audition_sample"), persona_id });
        const url = d.dataUrl || d.audio_url || "";
        box.innerHTML = url
          ? `${this._ic("volume", 13)} <audio controls src="${url}" style="max-width:100%;"></audio>${this._metaLine(d)}`
          : this._t("cp.voice.audition_fail");
      } catch (e) { box.textContent = this._t("cp.voice.audition_unavailable"); }
    }

    async _rebind() {
      const from = this.shadowRoot.querySelector('[data-role="rfrom"]').value;
      const to = this.shadowRoot.querySelector('[data-role="rto"]').value;
      if (!from || !to || from === to) return;
      const d = await this._client.voiceRebind({ from_persona_id: from, to_persona_id: to });
      if (d && d.ok) { await this._loadProfiles(); await this._audition(to); }
    }

    async _reconcile() {
      const box = this.shadowRoot.querySelector('[data-role="recon"]');
      if (!box) return;
      box.textContent = this._t("cp.voice.reconciling");
      try {
        const d = await this._client.voiceReconcile();
        this._lastReconcile = d;
        const s = d.summary || {};
        let html = this._esc(this._t("cp.voice.recon_summary", {
          cloud: s.cloud_total || 0, local: s.local_voice_ids || 0, orphan: s.orphan_count || 0,
        }));
        if (!(d.orphans || []).length && !(d.dangling || []).length) html += ` <span class="ok">${this._t("cp.voice.recon_ok")}</span>`;
        box.innerHTML = html;
      } catch (e) { box.textContent = this._t("cp.voice.recon_fail"); }
    }

    async _purgeOrphans() {
      if (!confirm(this._t("cp.voice.purge_orphans_confirm"))) return;
      await this._client.voicePurgeOrphans();
      await this._reconcile();
    }

    async _purgeOne(voice, force) {
      if (!voice) return;
      await this._client.voicePurge({ voice, force: !!force });
      await this._reconcile();
    }

    /* 状态/结果提示写进专用 msg 行（P2 2026-08-30 修：旧实现 querySelector('.hint')
       首匹配命中的是「音色状态条」effstatus——它可能带着 hidden（旧后端/解析失败时），
       于是「请先输入文字/已发送/发送失败」全写进一个看不见的元素；即使可见也会顶掉
       状态行。msg 行 :empty 不占位；无 msg 行（极端回退）时回落旧行为。 */
    _hint(msg, ok) {
      const h = this.shadowRoot.querySelector('[data-role="msg"]')
        || this.shadowRoot.querySelector(".hint");
      if (h) {
        h.textContent = msg;
        // ok: true=绿 / false=红 / "warn"=黄（结果未知类，#164）/ 其它=中性
        h.className = ok === true ? "hint ok" : (ok === false ? "hint err" : (ok === "warn" ? "hint warn" : "hint"));
      }
    }

    _stopGenTimer() {
      if (this._genTimer) { clearInterval(this._genTimer); this._genTimer = null; }
    }

    _clearPreview() {
      this._stopGenTimer();
      this._previewText = null;
      this._previewPersona = "";
      this._previewFilename = "";
      this._previewXl = "";
      this._previewXlEff = "";
      this._silent = false;
      this._silentKind = "";
      this._previewFallback = "";
      this._staleWas = false;
      this._previewSpeech = "";
      this._previewBasis = "";
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (box) { box.hidden = true; box.innerHTML = ""; box.classList.remove("stale"); }
      this._syncXlHint();   // 语种改道说明随预览一起消失 → 顶部预告恢复
    }

    /* 取消生成＝代际+1（与切会话同一作废机制）：在途请求的结果回来后发现代际
       不符即静默丢弃——桌面壳/网页两端零额外桥接，服务端任务自然由 TTL 清理。 */
    _cancelGen() {
      if (this._busy !== "tts") return;
      this._epoch += 1;
      this._stopGenTimer();
      this._busy = "";
      this._setBusy("");
      this._clearPreview();
      this._hint("");   // 常驻引导语在静态行，msg 行清空即可（复写=文字出现两遍）
    }

    /* 字数计（与服务端试听上限同口径）：超限红字 + 生成按钮禁用，超限前先拦。 */
    _syncCounter() {
      const el = this.shadowRoot.querySelector('[data-role="cnt"]');
      if (!el) return;
      const n = this._text().length;
      el.hidden = n === 0;
      el.textContent = `${n}/${this._maxChars}`;
      el.classList.toggle("err", n > this._maxChars);
      const main = this.shadowRoot.querySelector('[data-role="gen-main"]');
      if (main) main.disabled = !!this._busy || n > this._maxChars;
    }

    /* 预览过期判定：文字、音色或译声设置与生成基准不一致（发送是服务端按**当前**
       文本/设置重新合成，不拦＝把从未试听过的内容发给客户——含「试听了中文原文、
       开了翻译后发出去的却是外语」这种所听非所发）。无预览/无基准恒 false。

       #121（skuio 0831 原图 924）：译声维度按**有效目标语**比对，不比原始表示——
       宿主「我的消息 →」偏好异步加载会让 _xlTarget 从 'auto' 变成 'en'，而服务端
       试听时早已按 'en' 念（d.target_lang 真值）：坐席什么都没改，raw 比对却把
       试听常亮成过期。'auto' 双侧未解析时同样视为未变。真实的目标语切换
       （en→ja / 开↔关）仍照常标过期。
       #250（钧机 1258/1315，N-4 C）：#121 只补了「译了」那一侧——「没译」（identity：
       客户中文、文本中文）时基准仍是空串 → 回落 'auto'，与会话语言 'zh' 一比即过期，
       生成一结束发送就灰。基准改由 xlBaseline 取服务端有效目标语（新后端
       target_lang_resolved / 旧后端用客户端已知会话语言），只有目标语真变才过期。 */
    _xlEffective() {
      const raw = this._xlTarget();          // '' | 'auto' | 显式语种
      if (!raw) return "";
      if (raw !== "auto") return raw;
      const conv = String(this._effConvLang || "").toLowerCase();
      return conv || "auto";
    }
    _isStale() {
      if (this._previewText == null) return false;
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (!box || box.hidden) return false;
      const baseRaw = this._previewXl || "";
      const baseEff = baseRaw === ""
        ? ""
        : (this._previewXlEff || (baseRaw !== "auto" ? baseRaw : "auto"));
      let curEff = this._xlEffective();
      // 基准本就是 'auto' 档（坐席没动过档位）且当前仍是 'auto' 未解析出会话
      // 语言、而基准有服务端真值：拿不出「变了」的证据 → 视为未变（防
      // eff-config 迟到期的窗口误报）。显式目标 → 'auto' 是真实的档位切换，
      // 不吃此豁免（发送会重新解析，可能念出另一种语言）。
      if (curEff === "auto" && baseRaw === "auto"
          && baseEff && baseEff !== "auto") curEff = baseEff;
      return this._text() !== this._previewText
        || (this._persona || "") !== this._previewPersona
        || curEff !== baseEff;
    }

    /* 结果区状态单出口（P1-1 精简版，2026-08-31 提示风暴复盘）：三条提示互斥，
       只亮一条——过期(warn,禁发) > 无声(err,禁发) > 回落说明(info/warn,确认后可发)。
       0831 截图实录回落+无声+过期三条黄红并列（外加顶部预告=四层），坐席不知道
       信哪条；并列的根因是三个显隐点各自为政，收成唯一裁决口。
       #149（2026-09-02 KKXSTU 截图复盘）过期升到最前：旧序「无声 > 过期」下，坐席
       换了下拉音色后红条仍压着过期说明，面板同时显示「下拉=美月」「实际使用=张景光」
       ——被读成「选 X 出 Y」的路由事故。过期＝这条试听已不代表当前选择，它身上的
       无声裁决无论真假都只对旧产物成立，正确出路只有重新生成；改回原选择后无声
       红条照常回来（发送在两种状态下都禁用，闸门不松）。 */
    /* 禁发原因 title/hint 的文案键（#161：念错与无声出路不同，别混成一句）。 */
    _blockTitleKey() {
      return this._silentKind === "garbled"
        ? "cp.voice.garbled_block_t" : "cp.voice.silent_block_t";
    }

    _syncNotes() {
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (!box || box.hidden) return;
      const stale = this._previewText == null ? false : this._isStale();
      const v = CpVoice.panelVerdict({
        speech: this._previewSpeech, silent: this._silent,
        kind: this._silentKind, stale,
      });
      const vEl = box.querySelector('[data-role="verdict"]');
      if (vEl) {
        /* 唯一结论行（#250）：一个元素、一段文案、一个出路按钮。过期 / 阻发时行内
           带「重新生成」（在错误发生点给出路）；可发送态不带按钮（发送在脚部）。
           title 放依据（server-voiced / transcript+energy…）供 F12 与值守对质。 */
        const withRegen = v.state === "stale" || v.state === "blocked";
        vEl.className = `pv-verdict ${v.level}`;
        vEl.setAttribute("data-state", v.state);
        vEl.setAttribute("data-speech", this._previewSpeech);
        vEl.setAttribute("data-kind", v.state === "blocked" ? (this._silentKind || "silent") : "");
        vEl.title = v.state === "ok"
          ? this._t("cp.voice.v_ok_t", { basis: this._previewBasis || "server-voiced" }) : "";
        vEl.innerHTML = `<span>${this._esc(this._t(v.key))}</span>`
          + (withRegen ? `<button data-act="tts">${this._t("cp.voice.regen_btn")}</button>` : "");
        vEl.hidden = false;
      }
      const fbEl = box.querySelector('[data-role="fallback-note"]');
      if (fbEl) fbEl.hidden = this._silent || stale;   // 回落说明让位于结论（单条出口）
      box.classList.toggle("stale", stale);
      const send = box.querySelector('[data-act="send"]');
      const regen = box.querySelector('[data-role="regen-main"]');
      if (send) {
        // 过期：发送钮整个让位给「重新生成」；阻发：发送钮留着但禁用 + title 说原因
        // （无声=真闸门：红字劝「不要发送」而按钮亮蓝可点，视觉与行为自相矛盾）
        send.hidden = stale;
        send.disabled = stale || this._silent || !!this._busy;
        send.title = this._silent ? this._t(this._blockTitleKey()) : "";
      }
      if (regen) {
        regen.hidden = !stale;
        regen.disabled = !!this._busy;
      }
      this._syncXlHint();   // 语种改道说明可见时顶部预告让位（同因去重）
    }

    /* 警示观测（P0-5）：宿主把 kind 转成 cpv_warn_* 埋点（ui-event 趋势）——
       「提示风暴」从老板肉眼发现变成看板可查。无宿主（独立挂载）静默。 */
    _warnBeacon(kind) {
      try {
        this.dispatchEvent(new CustomEvent("cp-voice-warn", {
          bubbles: true, composed: true, detail: { kind: String(kind || "") },
        }));
      } catch (e) { /* */ }
    }

    /* 语种改道说明的一键出路：关闭「跟随翻译发声」（同键联动主输入框/其他
       窗口）并立即按原文重新生成——别让坐席自己找开关、再找生成按钮。 */
    _xlOffRegen() {
      if (this._busy) return;
      this._xlOn = false;
      this._saveXlOn(false);
      const cb = this.shadowRoot.querySelector('[data-role="xlfollow"]');
      if (cb) cb.checked = false;
      this._syncXlHint();
      this.dispatchEvent(new CustomEvent("cp-voice-xl-changed", {
        bubbles: true, composed: true, detail: { on: false },
      }));
      this._genTts();
    }

    _syncStale() {
      if (this._previewText == null) return;
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (!box || box.hidden) return;
      const stale = this._isStale();
      if (stale && !this._staleWas) this._warnBeacon("stale");
      this._staleWas = stale;
      this._syncNotes();
    }

    /* 请求期按钮互斥：主生成钮换「生成中…」文案，预览区 🔁/发送一并禁用（连点=双烧/双发）。 */
    _setBusy(kind) {
      this._busy = kind || "";
      const main = this.shadowRoot.querySelector('[data-role="gen-main"]');
      if (main) {
        // 解除 busy 时超限禁用要保留（否则清 busy 会把超限文本的生成按钮重新点亮）
        main.disabled = !!this._busy || this._text().length > this._maxChars;
        if (this._busy === "tts") main.textContent = this._t("cp.voice.gen_busy_btn");
        else main.innerHTML = this._genLabelHtml();   // 恢复图标+动词标签（textContent 会抹掉 SVG）
      }
      this.shadowRoot.querySelectorAll('[data-act="tts"]').forEach((b) => { b.disabled = !!this._busy; });
      const send = this.shadowRoot.querySelector('[data-act="send"]');
      if (send) {
        send.disabled = !!this._busy || this._isStale() || this._silent;
        send.textContent = this._busy === "send"
          ? this._t("cp.voice.sending") : this._t("cp.voice.send_btn");
      }
    }

    /* 发送成功提示驻留一段时间后清空 msg 行（:empty 自动收纳；常驻引导语在
       下方静态行，无需复写——复写=同一段话在面板出现两遍）。 */
    _hintResetLater() {
      if (this._hintTimer) clearTimeout(this._hintTimer);
      this._hintTimer = setTimeout(() => {
        this._hintTimer = null;
        this._hint("");
      }, this._hintResetMs);
    }
  }

  if (!customElements.get("cp-voice")) customElements.define("cp-voice", CpVoice);
})(typeof window !== "undefined" ? window : this);
