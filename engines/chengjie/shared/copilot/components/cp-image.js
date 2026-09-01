"use strict";
/* 两端共享 · AI 生成图片 cp-image（工具箱「AI 生成图片」，2026-08-21）
   坐席手动出图：选人设/场景/引擎 → 现场生成（ComfyUI）→ VLM 后验 → 预览 →
   发送到当前会话 / 存入相册。直连 /api/image/*（后端复用 SelfieProvider+image_gate），
   发送走既有 send-media（零新发送链）。

   设计不变量（对齐 cp-xlate-tools）：
   - 弱会话依赖：无会话也能生成/存册/下载；仅「发送到会话」需要会话上下文（无则禁用）。
   - context 重喂**绝不整块重渲**（防打断进行中的生成/预览查看）——refresh 只更新会话
     三元组与发送按钮可用性。
   - 生成是重操作（子进程打 ComfyUI，数十秒）：busy 互斥 + 计秒 + 代际 _tok（切参数/
     再次生成后旧响应丢弃）。
   - 引擎按局域网算力分配（后端 comfy_infer --engine）：flux_pulid（现役锁脸）/
     qwen_edit（Qwen-Image-Edit-2511 一致性）/ z_image（速度档）。
   2026-08-22 错误面收口（「模型被清空」事故沉淀，四条硬不变量）：
   ① 失败按后端 code 出人话标题+建议（cp.image.errc_* / errs_*，契约门禁
      tests/test_cp_app_i18n_keys.py 钉住）；原始 reason 进「技术详情」折叠 +
      「复制诊断信息」按钮——内部 IP/堆栈不再糊坐席一脸，也不再把「显存腾挪
      过程日志」当死因展示。
   ② 引擎部署态诚实化：config.engines_info.deployed=false 的引擎置灰（选了必
      400 的选项不该可选）；comfy_ok=false 出「服务器不可达」预警；全引擎未
      部署 → 生成按钮禁用 + 顶部红条（2026-08-22 事故形态：模型目录被清空）。
   ③ 无基准脸人设禁用「锁脸」并警示（静默出随机脸=对客户「换人」级穿帮）。
   ④ _renderForm 重渲前快照/回填表单值（prompt/scene/size/lock）——切换类型
      不再吞掉坐席打了一半的提示词（对齐「表单内容不因重渲丢失」工作台纪律）。
   P1 增量（2026-08-22 同日，特性探测向后兼容——旧后端缺旗标自动退回旧行为）：
   ⑤ 异步任务化（config.jobs_api）：imageJobCreate → 1.5s 轮询 → 可取消
      （ComfyUI /interrupt 配合；同步长连接挂 2-5 分钟不可取消的病根拔掉）；
      等待期渲染按所选比例的骨架屏 + 取消按钮。
   ⑥ 相册优先（config.album_pick）：选场景即秒查存货，有货出缩略条「点选直接
      发送」——零等待零 GPU，与自动链「相册优先于现场出图」同哲学。
   ⑦ 发送成功回写媒体账本（imageMarkSent best-effort）：重发冷却/服装连续窗
      从此认得手动发出的图（一致性旁路收口）。
   ⑧ 会话内最近 3 张历史缩略图（连生几张挑一张的自然工作流）+ 预览点击放大。
   P0 增量（2026-08-28，「一屏三处不可信」实录：横幅永久误报 + 4 张裂图 + 灰字看不清）：
   ⑨ 忙闲判据换成**队列深度/在途单**（cfg.busy_signal 特性探测），显存只用来判
      「要不要预热」且按所选引擎的真实闸门（qwen_edit 18≠写死 14）；面板不再出现
      显存数字与「算力/腾挪」等内部词（白标/演示会露馅，且坐席无从处置）。
   ⑩ 存货缩略图带 onerror：坏图就地摘掉、张数改成真实可发数、全裂则整块不渲染
      并上报一次遥测——绝不出现「宣称 N 张可发」配一排裂图框。
   ⑪ config 快照按 TTL 续期（此前只在 connectedCallback 取一次，配额/忙闲/部署态
      整个会话冻结）；只补状态块，绝不重渲表单。
   client 需实现 imageConfig/imageGenerate/imageSaveAlbum/sendMedia（缺失如实报，不静默）；
   P1 方法（imageJobCreate/Status/Cancel、imageAlbumStock、imageMarkSent）缺失时优雅退回。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  const IC = {
    gen: '<path d="M12 3v3m0 12v3M5.6 5.6l2.1 2.1m8.6 8.6l2.1 2.1M3 12h3m12 0h3M5.6 18.4l2.1-2.1m8.6-8.6l2.1-2.1"/><circle cx="12" cy="12" r="3.2"/>',
    send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    album: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="9" r="1.6"/><path d="M21 15l-5-5L5 21"/>',
    redo: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    dl: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  };
  function svg(name, size) {
    const s = size || 14;
    return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true" style="flex-shrink:0;">' + (IC[name] || "") + "</svg>";
  }
  // 场景快捷（点击填入场景框；仅出图提示 + VLM 场景后验用，纯便利）。
  const SCENES = ["cafe", "office", "gym", "beach", "bedroom", "street", "home", "park"];
  const SIZES = [["1024x1024", "1:1"], ["832x1216", "3:4"], ["1216x832", "4:3"]];
  // config 快照续期节流：忙闲/配额/部署态会变，但探针有成本（ComfyUI 往返）。
  const CFG_TTL_MS = 60000;

  class CpImage extends Base {
    constructor() {
      super();
      this._busy = false;
      this._tok = 0;
      this._booted = false;
      this._cfg = null;         // /api/image/config 结果
      this._cfgTs = 0;          // 该快照的取回时刻（TTL 续期用）
      this._result = null;      // 最近一次生成 {preview_url, filename, path, engine}
      this._mode = "selfie";   // selfie | object
      this._engine = "";
      this._persona = "";
      this._secTimer = null;
      this._deploy = {};        // 引擎部署态 {id: true|false|null(未知)}
      this._lastFail = null;    // 最近失败 {code, reason}（复制诊断用）
      this._lockPref = null;    // 坐席对「锁脸」的显式偏好（null=默认勾选）
      this._jobId = "";         // 进行中的异步任务（取消/轮询用）
      this._hist = [];          // 会话内最近生成 [{preview_url,...}]（cap 3）
      this._stockTimer = null;  // 相册存货查询 debounce
      this._stockTok = 0;       // 存货查询代际（防慢响应回写旧场景）
      this._stockBeaconed = false;  // 「存货缩略图全裂」只上报一次
      this._sceneHints = null;  // P2 场景热度 [{scene,demand,unmet,stock}]（null=未取）
    }

    styles() {
      return `
      .im-row { display:flex; align-items:center; gap:6px; margin-bottom:7px; flex-wrap:wrap; }
      .im-row .lbl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); flex-shrink:0; min-width:38px; }
      .im-row select, .im-row input[type=text] { font:inherit; font-size:var(--cp-fs-tiny,11px);
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:3px 6px; flex:1; min-width:80px; }
      .im-seg { display:inline-flex; border:1px solid var(--cp-border,#e2e8f0); border-radius:7px; overflow:hidden; }
      .im-seg button { font:inherit; font-size:var(--cp-fs-tiny,11px); padding:3px 10px; border:0;
        background:var(--cp-surface,#fff); color:var(--cp-text-dim,#64748b); cursor:pointer; }
      .im-seg button.on { background:var(--cp-accent,#6366f1); color:#fff; }
      textarea.im-prompt { width:100%; box-sizing:border-box; min-height:56px; resize:vertical;
        font:inherit; font-size:var(--cp-fs-sm,12px); line-height:1.5; padding:6px 8px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); margin-bottom:6px; }
      .im-chips { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:7px; }
      .im-chip { font:inherit; font-size:var(--cp-fs-tiny,12px); padding:2px 8px; cursor:pointer;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:11px;
        background:var(--cp-surface-2,#f8fafc); color:var(--cp-text-dim,#64748b); }
      .im-chip:hover { border-color:var(--cp-accent,#6366f1); }
      .im-lock { display:inline-flex; align-items:center; gap:4px; font-size:var(--cp-fs-tiny,12px); color:var(--cp-text-dim,#64748b); cursor:pointer; }
      .im-gen { display:inline-flex; align-items:center; gap:5px; font:inherit; font-size:var(--cp-fs-sm,12px);
        font-weight:600; padding:7px 14px; border:0; border-radius:8px; cursor:pointer;
        background:var(--cp-accent,#6366f1); color:#fff; }
      .im-gen[disabled] { opacity:.55; cursor:not-allowed; }
      .im-status { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:7px 0 0; }
      .im-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:7px; line-height:1.5; }
      .im-preview { margin-top:8px; }
      .im-preview img { max-width:100%; border-radius:8px; border:1px solid var(--cp-border,#e2e8f0); display:block; }
      .im-badge { display:inline-flex; align-items:center; gap:4px; font-size:var(--cp-fs-tiny,12px); color:var(--cp-ok,#0f9d75);
        margin-top:5px; }
      .im-acts { display:flex; flex-wrap:wrap; gap:5px; margin-top:7px; }
      .im-acts button { display:inline-flex; align-items:center; gap:4px; font:inherit; font-size:var(--cp-fs-tiny,11px);
        padding:5px 9px; border:1px solid var(--cp-border,#e2e8f0); border-radius:7px;
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); cursor:pointer; }
      .im-acts button.primary { background:var(--cp-accent,#6366f1); color:#fff; border-color:var(--cp-accent,#6366f1); }
      .im-acts button[disabled] { opacity:.5; cursor:not-allowed; }
      .im-hint { margin-top:7px; font-size:var(--cp-fs-tiny,12px); color:var(--cp-text-tiny,#94a3b8); line-height:1.5; }
      .im-cap { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-tiny,11px);
        border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:4px 7px; margin-top:6px; }
      .im-warn { font-size:var(--cp-fs-tiny,11px); line-height:1.55; color:var(--cp-warn-ink,#b45309);
        background:var(--cp-warn-bg,#fffbeb); border:1px solid var(--cp-warn-border,#fde68a);
        border-radius:7px; padding:5px 8px; margin-bottom:7px; }
      .im-warn.crit { color:var(--cp-danger,#dc2626); border-color:var(--cp-danger,#dc2626); }
      .im-warn[hidden] { display:none; }
      /* 中性知情条（排队/预热）：琥珀=需要人处理，「会慢一点」不是那个语义——
         用警示色说中性信息，久了坐席对真警告也不看了（告警疲劳）。 */
      .im-note { font-size:var(--cp-fs-tiny,11px); line-height:1.55; color:var(--cp-text-dim,#64748b);
        background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
        border-radius:7px; padding:5px 8px; margin-bottom:7px; }
      .im-errcard { margin-top:8px; padding:8px 10px; border-radius:8px;
        background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
        border-left:3px solid var(--cp-danger,#dc2626); }
      .im-err-ttl { font-size:var(--cp-fs-sm,12px); font-weight:700; color:var(--cp-danger,#dc2626); }
      .im-err-sug { margin-top:4px; font-size:var(--cp-fs-tiny,11px); line-height:1.55;
        color:var(--cp-text,#1e293b); }
      .im-err-tech { margin-top:6px; }
      .im-err-tech summary { font-size:var(--cp-fs-tiny,12px); color:var(--cp-text-tiny,#94a3b8); cursor:pointer; }
      .im-err-tech pre { margin:4px 0 0; padding:6px 7px; font-size:var(--cp-fs-tiny,12px); line-height:1.45;
        white-space:pre-wrap; word-break:break-all; max-height:130px; overflow:auto;
        background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
        border-radius:6px; color:var(--cp-text-dim,#64748b); }
      .im-skel { width:100%; border-radius:8px; border:1px solid var(--cp-border,#e2e8f0);
        background:linear-gradient(100deg, var(--cp-surface-2,#f1f5f9) 40%,
          var(--cp-surface,#fff) 50%, var(--cp-surface-2,#f1f5f9) 60%);
        background-size:200% 100%; margin-top:8px; }
      @media (prefers-reduced-motion: no-preference) {
        .im-skel { animation: imShimmer 1.4s linear infinite; }
        @keyframes imShimmer { from { background-position:120% 0; } to { background-position:-80% 0; } }
      }
      .im-skel-bar { display:flex; align-items:center; justify-content:space-between;
        gap:6px; margin-top:6px; }
      .im-cancel { font:inherit; font-size:var(--cp-fs-tiny,11px); padding:4px 10px;
        border:1px solid var(--cp-border,#e2e8f0); border-radius:7px; cursor:pointer;
        background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .im-stock { margin:2px 0 7px; padding:6px 8px; border-radius:8px;
        border:1px dashed var(--cp-border,#e2e8f0); background:var(--cp-surface-2,#f8fafc); }
      .im-stock-line { font-size:var(--cp-fs-tiny,12px); color:var(--cp-text-dim,#64748b); margin-bottom:5px; }
      .im-stock-row { display:flex; gap:5px; flex-wrap:wrap; }
      .im-stock-row img { width:52px; height:52px; object-fit:cover; border-radius:6px;
        border:1px solid var(--cp-border,#e2e8f0); cursor:pointer; display:block; }
      .im-stock-row img:hover { border-color:var(--cp-accent,#6366f1); }
      .im-hist { margin-top:7px; }
      .im-hist .ttl { font-size:var(--cp-fs-tiny,12px); color:var(--cp-text-tiny,#94a3b8); margin-bottom:4px; }
      .im-hist-row { display:flex; gap:5px; }
      .im-hist-row img { width:44px; height:44px; object-fit:cover; border-radius:6px;
        border:1px solid var(--cp-border,#e2e8f0); cursor:pointer; opacity:.85; }
      .im-hist-row img:hover { opacity:1; border-color:var(--cp-accent,#6366f1); }
      .im-preview img.im-fadein { opacity:0; transition:opacity .25s ease-out; }
      .im-preview img.im-fadein.on { opacity:1; }
      .im-preview img { cursor:zoom-in; }
      .im-zoom { position:fixed; inset:0; z-index:99; background:rgba(15,23,42,.82);
        display:flex; align-items:center; justify-content:center; cursor:zoom-out; }
      .im-zoom img { max-width:94vw; max-height:92vh; border-radius:10px; }`;
    }

    connectedCallback() {
      if (this._booted) return;
      this._booted = true;
      this._loadConfig();
    }
    /* 弱会话：context 重喂只刷新发送按钮可用性，绝不重渲打断生成/预览。
       顺带按 TTL 悄悄续一次 config——此前 config 只在 connectedCallback 取一次，
       忙闲/日配额/引擎部署态从打开面板那刻起整个会话冻结（额度永远显示 0、
       预警条腾完显存也不消失）。只补状态块，绝不碰表单。 */
    async refresh() {
      this._notifyLoaded({ ok: true });
      this._syncSendBtn();
      this._maybeRefetchConfig();
    }
    _maybeRefetchConfig(force) {
      if (!this._cfg || this._busy) return;   // 生成中不打扰（也别浪费探针）
      const now = Date.now();
      if (!force && now - (this._cfgTs || 0) < CFG_TTL_MS) return;
      this._cfgTs = now;
      const client = this._cl();
      if (!client || !client.imageConfig) return;
      Promise.resolve(client.imageConfig()).then((d) => {
        if (!d || !d.ok || !d.enabled) return;
        this._cfg = d;
        this._deploy = {};
        (d.engines_info || []).forEach((x) => { if (x && x.id) this._deploy[x.id] = x.deployed; });
        this._syncStatusBlocks();
        const b = this.shadowRoot && this.shadowRoot.querySelector('[data-act="gen"]');
        if (b && !this._busy) b.disabled = this._allUndeployed();
      }, () => { /* 续期失败=保留旧快照，不打断坐席 */ });
    }
    _cl() {
      if (!this._client && root.CopilotShared && root.CopilotShared.createCopilotClient) {
        try { this._client = root.CopilotShared.createCopilotClient(); } catch (_e) { /* null */ }
      }
      return this._client;
    }
    _hasCtx() { return !!(this._ctx && this._ctx.conversationId); }

    async _loadConfig() {
      const client = this._cl();
      if (!client || !client.imageConfig) { this._renderDisabled(this.t("cp.image.no_client")); return; }
      this._render('<div class="im-status">' + this.esc(this.t("cp.image.loading")) + "</div>");
      let d;
      try { d = await client.imageConfig(); } catch (_e) { d = null; }
      if (!d || !d.ok) { this._renderDisabled(this.t("cp.image.load_fail")); return; }
      if (!d.enabled) { this._renderDisabled(this.t("cp.image.disabled")); return; }
      this._cfg = d;
      this._cfgTs = Date.now();
      this._deploy = {};
      (d.engines_info || []).forEach((x) => {
        if (x && x.id) this._deploy[x.id] = x.deployed;
      });
      // 默认引擎优先选「未被判死」的——默认位落在 deployed=false 上等于一打开就踩 400。
      let eng = String(d.default_engine || (d.engines || [])[0] || "flux_pulid");
      if (this._deploy[eng] === false) {
        const ok = (d.engines || []).find((e) => this._deploy[e] !== false);
        if (ok) eng = ok;
      }
      this._engine = eng;
      this._persona = String(d.default_persona || ((d.personas || [])[0] || {}).id || "");
      this._jobsApi = !!d.jobs_api;
      this._albumPick = !!d.album_pick;
      this._renderForm();
      this._loadStock();
      if (d.scene_hints) this._loadSceneHints();
    }
    /* P2 场景热度：客户点名需求 × 相册供给 → chips 重排（未兑现在前 + 🔥 标记）。
       软失败=保持默认顺序（提示是增强不是依赖）。 */
    async _loadSceneHints() {
      const client = this._cl();
      if (!client || !client.imageSceneHints) return;
      let d = null;
      try { d = await client.imageSceneHints(this._persona); } catch (_e) { d = null; }
      this._sceneHints = (d && d.ok && d.scenes) || [];
      this._renderChips();
    }
    _chipsHtml() {
      const esc = (s) => this.esc(s);
      const hints = this._sceneHints || [];
      const byScene = {};
      hints.forEach((h) => { if (h && h.scene) byScene[h.scene] = h; });
      // 排序：未兑现↓ → 需求↓ 的点名场景在前（仅收录 canonical chips 池内的），
      // 余下按默认顺序——chips 总量不变，只调优先级+加热度标记。
      const hot = hints.map((h) => h.scene).filter((s) => SCENES.indexOf(s) >= 0);
      const rest = SCENES.filter((s) => hot.indexOf(s) < 0);
      return hot.concat(rest).map((s) => {
        const h = byScene[s];
        const fire = h && h.unmet > 0;
        const title = fire ? this.tf("cp.image.chip_unmet_t", { n: h.unmet }) : "";
        return '<button type="button" class="im-chip" data-act="scene" data-scene="' + s +
          '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" +
          (fire ? "🔥" : "") + esc(this.t("cp.image.scene_" + s)) + "</button>";
      }).join("");
    }
    _renderChips() {
      const el = this.shadowRoot && this.shadowRoot.querySelector('[data-role="chipwrap"]');
      if (el) el.innerHTML = this._chipsHtml();
    }
    _allUndeployed() {
      const engines = (this._cfg && this._cfg.engines) || [];
      return engines.length > 0 && engines.every((e) => this._deploy[e] === false);
    }
    _personaHasFace() {
      const ps = (this._cfg && this._cfg.personas) || [];
      const p = ps.find((x) => x && x.id === this._persona);
      return p ? !!p.has_face_ref : false;
    }
    _renderDisabled(msg) {
      this._render('<div class="im-hint">' + this.esc(msg || "") + "</div>");
    }

    /* ── 顶部预警区（2026-08-28 收口）───────────────────────────────────
       分层：① 全引擎未部署=红（坐席自己救不了，必须找运维）② 服务不可达=黄
       ③ 排队/预热=中性知情条。**刻意不再出现显存数字与「算力」字样**：
       - 旧判据 `vram_free_gb < 14` 说的是「显存被占满」，不是「卡在忙」——同一
         块卡还常驻着聊天兜底 30B（keep_alive 30m），空闲显存长期 0.x G 而 GPU
         利用率为 0，于是那条「出图卡当前较忙」一挂就永不消失（实录）；
       - 且它对坐席泄露内部实现（显存/腾挪/算力），白标与演示场合直接露馅。
       忙闲改吃后端 queue_pending/busy_jobs；显存只用来判「要不要预热」。
       旧后端无 busy_signal → 整块不出（宁可不说，也不说错）。 */
    _warnsHtml() {
      const esc = (s) => this.esc(s);
      const cfg = this._cfg || {};
      if (this._allUndeployed()) {
        return '<div class="im-warn crit">' + esc(this.t("cp.image.all_undeployed")) + "</div>";
      }
      if (cfg.comfy_ok === false) {
        return '<div class="im-warn">' + esc(this.t("cp.image.comfy_down_warn")) + "</div>";
      }
      if (!cfg.busy_signal) return "";
      // 我们自己的在途单也在 ComfyUI 队列里 → 取较大值，相加会双计。
      const q = Math.max(Number(cfg.queue_pending) || 0, Number(cfg.busy_jobs) || 0);
      if (q > 0) {
        return '<div class="im-note">' + esc(this.tf("cp.image.queue_wait", { n: q })) + "</div>";
      }
      if (this._needsWarmup()) {
        return '<div class="im-note">' + esc(this.t("cp.image.warmup_wait")) + "</div>";
      }
      return "";
    }
    /* 本次出图要不要先卸模型腾显存（≈多等一分钟）：按**所选引擎**真实闸门判
       （后端 engines_info[].min_free_gb＝真正传给 comfy_infer 的 --min-free-gb，
       qwen_edit 是 18 不是 14）。闸门/显存任一未知 → 不判，不吓唬人。 */
    _needsWarmup() {
      const cfg = this._cfg || {};
      const free = cfg.vram_free_gb;
      if (typeof free !== "number" || free < 0) return false;
      const info = (cfg.engines_info || []).find((x) => x && x.id === this._engine);
      const min = info && typeof info.min_free_gb === "number" ? info.min_free_gb : 0;
      return min > 0 && free < min;
    }
    _quotaHtml() {
      const cfg = this._cfg || {};
      if (!((Number(cfg.daily_quota) || 0) > 0)) return "";
      return '<div class="im-hint" style="margin:0 0 6px;">' +
        this.esc(this.tf("cp.image.quota_line",
                         { u: Number(cfg.quota_used) || 0,
                           q: Number(cfg.daily_quota) })) + "</div>";
    }
    _syncStatusBlocks() {
      const sr = this.shadowRoot;
      if (!sr) return;
      const w = sr.querySelector('[data-role="warns"]');
      if (w) w.innerHTML = this._warnsHtml();
      const q = sr.querySelector('[data-role="quota"]');
      if (q) q.innerHTML = this._quotaHtml();
    }

    /* 重渲前快照表单值——切换类型/人设联动重渲不得吞掉坐席打了一半的输入。 */
    _fieldSnapshot() {
      const sr = this.shadowRoot;
      const g = (role) => sr && sr.querySelector('[data-role="' + role + '"]');
      const lockEl = g("lock");
      return {
        prompt: (g("prompt") || {}).value || "",
        scene: (g("scene") || {}).value || "",
        size: (g("size") || {}).value || "",
        lock: lockEl ? !!lockEl.checked : null,
      };
    }
    _restoreFields(snap) {
      if (!snap) return;
      const sr = this.shadowRoot;
      const g = (role) => sr && sr.querySelector('[data-role="' + role + '"]');
      const p = g("prompt"); if (p && snap.prompt) p.value = snap.prompt;
      const s = g("scene"); if (s && snap.scene) s.value = snap.scene;
      const z = g("size");
      if (z && snap.size && Array.prototype.some.call(z.options, (o) => o.value === snap.size)) {
        z.value = snap.size;
      }
    }
    _renderForm() {
      const esc = (s) => this.esc(s);
      const cfg = this._cfg || {};
      const personas = cfg.personas || [];
      const engines = cfg.engines || ["flux_pulid"];
      const snap = this._fieldSnapshot();
      const pOpts = personas.map((p) =>
        '<option value="' + esc(p.id) + '"' + (p.id === this._persona ? " selected" : "") + ">" +
        esc(p.name || p.id) + (p.has_face_ref ? " " + this.t("cp.image.face_tag") : "") + "</option>").join("");
      const eOpts = engines.map((e) => {
        const dep = this._deploy[e];
        const label = this._engineLabel(e) + (dep === false ? " " + this.t("cp.image.eng_undeployed") : "");
        return '<option value="' + esc(e) + '"' + (e === this._engine ? " selected" : "") +
          (dep === false ? " disabled" : "") + ">" + esc(label) + "</option>";
      }).join("");
      const sOpts = SIZES.map((s) =>
        '<option value="' + s[0] + '">' + s[1] + " · " + s[0] + "</option>").join("");
      const chips = this._chipsHtml();
      const isSelfie = this._mode === "selfie";
      const allDown = this._allUndeployed();
      // 预警区与配额行都做成**可独立重渲的容器**：引擎切换/配置重取只补这两块，
      // 绝不整块重渲（会打断坐席打了一半的提示词）。
      const warns = '<div data-role="warns">' + this._warnsHtml() + "</div>";
      const quotaLine = '<div data-role="quota">' + this._quotaHtml() + "</div>";
      const html =
        warns + quotaLine +
        '<div class="im-row"><span class="lbl">' + esc(this.t("cp.image.persona")) + '</span>' +
        (personas.length
          ? '<select data-role="persona">' + pOpts + "</select>"
          : '<span class="im-hint" style="margin:0;">' + esc(this.t("cp.image.no_persona")) + "</span>") +
        "</div>" +
        '<div class="im-row"><span class="lbl">' + esc(this.t("cp.image.mode")) + '</span>' +
        '<span class="im-seg">' +
        '<button type="button" data-act="mode" data-mode="selfie" class="' + (isSelfie ? "on" : "") + '">' +
        esc(this.t("cp.image.mode_selfie")) + "</button>" +
        '<button type="button" data-act="mode" data-mode="object" class="' + (isSelfie ? "" : "on") + '">' +
        esc(this.t("cp.image.mode_object")) + "</button></span>" +
        (isSelfie ? '<label class="im-lock">' + svg("lock", 12) +
          '<input type="checkbox" data-role="lock" checked /> ' + esc(this.t("cp.image.lock_face")) + "</label>" : "") +
        "</div>" +
        (isSelfie ? '<div class="im-warn" data-role="facewarn" hidden>' +
          esc(this.t("cp.image.face_ref_warn")) + "</div>" : "") +
        '<div class="im-row"><span class="lbl">' + esc(this.t("cp.image.engine")) + '</span>' +
        '<select data-role="engine">' + eOpts + "</select>" +
        '<select data-role="size">' + sOpts + "</select></div>" +
        '<textarea class="im-prompt" data-role="prompt" placeholder="' +
        esc(isSelfie ? this.t("cp.image.ph_selfie") : this.t("cp.image.ph_object")) + '"></textarea>' +
        '<div class="im-chips"><span data-role="chipwrap" style="display:contents;">' + chips +
        '</span><input type="text" data-role="scene" placeholder="' +
        esc(this.t("cp.image.scene_ph")) + '" style="flex:1;min-width:90px;font-size:var(--cp-fs-tiny,12px);border:1px solid var(--cp-border,#e2e8f0);border-radius:11px;padding:2px 8px;background:var(--cp-surface,#fff);color:var(--cp-text,#1e293b);" /></div>' +
        '<div data-role="stock"></div>' +
        '<button type="button" class="im-gen" data-act="gen"' + (allDown ? " disabled" : "") + '>' +
        svg("gen", 14) + " " + esc(this.t("cp.image.gen_btn")) + "</button>" +
        '<div class="im-status" data-role="status"></div>' +
        '<div data-role="out"></div>' +
        '<div class="im-hint">' + esc(this.t("cp.image.hint")) + "</div>";
      this._render(html);
      this._wire();
      this._restoreFields(snap);
      this._syncFaceLock();
    }
    /* 无基准脸人设：禁用锁脸 + 显示警示（静默出随机脸=「换人」级穿帮）；
       有基准脸：恢复坐席显式偏好（默认勾选）。人设切换就地打补丁，不整块重渲。 */
    _syncFaceLock() {
      const sr = this.shadowRoot;
      const lockEl = sr && sr.querySelector('[data-role="lock"]');
      const warnEl = sr && sr.querySelector('[data-role="facewarn"]');
      if (!lockEl) return;
      const has = this._personaHasFace();
      lockEl.disabled = !has;
      lockEl.checked = has ? (this._lockPref !== false) : false;
      if (warnEl) warnEl.hidden = has;
    }
    _engineLabel(e) {
      const k = "cp.image.eng_" + e;
      const v = this.t(k);
      return v === k ? e : v;
    }
    _wire() {
      const sr = this.shadowRoot;
      const ps = sr.querySelector('[data-role="persona"]');
      if (ps) ps.addEventListener("change", () => {
        this._persona = ps.value || "";
        this._syncFaceLock();
        this._loadStock();
        if (this._cfg && this._cfg.scene_hints) this._loadSceneHints();
      });
      const es = sr.querySelector('[data-role="engine"]');
      if (es) es.addEventListener("change", () => {
        this._engine = es.value || this._engine;
        this._syncStatusBlocks();   // 预热提示按新引擎的显存闸门重算
      });
      const lk = sr.querySelector('[data-role="lock"]');
      if (lk) lk.addEventListener("change", () => {
        if (!lk.disabled) this._lockPref = !!lk.checked;
      });
      const sc = sr.querySelector('[data-role="scene"]');
      if (sc) sc.addEventListener("input", () => { this._loadStock(); });
    }
    /* ── 相册优先层：场景/人设一变即秒查存货（debounce 400ms + 代际防旧回写）。
       有货 → 缩略条「点选直接发送」；无货/未启用 → 静默（不占版面）。 */
    _loadStock() {
      if (!this._albumPick || !this._persona) { this._renderStock([]); return; }
      if (this._stockTimer) clearTimeout(this._stockTimer);
      this._stockTimer = setTimeout(async () => {
        const tok = ++this._stockTok;
        const client = this._cl();
        if (!client || !client.imageAlbumStock) return;
        let d = null;
        try { d = await client.imageAlbumStock(this._persona, this._val("scene")); }
        catch (_e) { d = null; }
        if (tok !== this._stockTok) return;
        this._stockItems = (d && d.ok && d.items) || [];
        this._renderStock(this._stockItems);
      }, 400);
    }
    _renderStock(items) {
      const el = this.shadowRoot && this.shadowRoot.querySelector('[data-role="stock"]');
      if (!el) return;
      if (!items || !items.length) { el.innerHTML = ""; return; }
      const shown = items.slice(0, 6);
      const thumbs = shown.map((it, i) =>
        '<img src="' + this.esc(it.url) + '" loading="lazy" alt="" data-act="stockpick" data-idx="' + i + '" title="' +
        this.esc(it.caption || it.scene_class || "") + '" />').join("");
      el.innerHTML =
        '<div class="im-stock"><div class="im-stock-line" data-role="stockline">' +
        this.esc(this.tf("cp.image.stock_line", { n: shown.length })) + "</div>" +
        '<div class="im-stock-row">' + thumbs + "</div></div>";
      this._wireStockFallback(el);
    }
    /* 缩略图加载失败就地摘掉，张数按**真正能发的**改写；一张都活不下来 → 整块
       不渲染 + 上报一次。此前无 onerror：坏 URL 留下浏览器裂图框，而卡片还在宣称
       「相册已有 N 张，点选可直接发送」（点了也没反应）——比什么都不显示更糟，
       且坏了只能等坐席报障（2026-08-28 实录：后端 URL 拼错，4 张全裂无人知）。 */
    _wireStockFallback(el) {
      const row = el.querySelector(".im-stock-row");
      if (!row) return;
      const imgs = Array.prototype.slice.call(row.querySelectorAll("img"));
      let alive = imgs.length;
      imgs.forEach((img) => {
        img.addEventListener("error", () => {
          try { img.remove(); } catch (_e) { /* 已移除 */ }
          alive -= 1;
          if (alive <= 0) { el.innerHTML = ""; this._beaconStockBroken(); return; }
          const line = el.querySelector('[data-role="stockline"]');
          if (line) line.textContent = this.tf("cp.image.stock_line", { n: alive });
        }, { once: true });
      });
    }
    /* 存货缩略图全裂 = 服务端 URL/消毒口径坏了，坐席看不出所以然。每个面板生命
       周期至多报一次，进既有前端错误遥测（只送消毒字段，无路径无原文）。 */
    _beaconStockBroken() {
      if (this._stockBeaconed) return;
      this._stockBeaconed = true;
      try {
        if (!navigator.sendBeacon) return;
        const body = JSON.stringify({
          page: (location && location.pathname) || "", fn: "cp-image:stock",
          type: "stock_thumb_broken",
        });
        navigator.sendBeacon("/api/telemetry/frontend-error",
                             new Blob([body], { type: "application/json" }));
      } catch (_e) { /* 遥测绝不影响功能 */ }
    }
    _status(msg) {
      const el = this.shadowRoot.querySelector('[data-role="status"]');
      if (el) el.textContent = msg || "";
    }
    _out(html) {
      const el = this.shadowRoot.querySelector('[data-role="out"]');
      if (el) el.innerHTML = html || "";
      return el;
    }
    _err(msg) {
      this._status("");
      this._out('<div class="im-err">' + this.esc(msg) + "</div>");
    }
    _val(role) {
      const el = this.shadowRoot.querySelector('[data-role="' + role + '"]');
      return el ? String(el.value || "").trim() : "";
    }

    onAction(act, el) {
      if (act === "mode") {
        this._mode = el.getAttribute("data-mode") || "selfie";
        this._renderForm();
        return;
      }
      if (act === "scene") {
        const sc = el.getAttribute("data-scene") || "";
        const inp = this.shadowRoot.querySelector('[data-role="scene"]');
        if (inp) inp.value = this.t("cp.image.scene_" + sc) || sc;
        return;
      }
      if (act === "gen") { this._generate(); return; }
      if (act === "send") { this._send(el); return; }
      if (act === "save") { this._saveAlbum(el); return; }
      if (act === "redo") { this._generate(); return; }
      if (act === "dl") { this._download(); return; }
      if (act === "copydiag") { this._copyDiag(el); return; }
      if (act === "cancelgen") { this._cancelGen(); return; }
      if (act === "stockpick") { this._pickStock(el); return; }
      if (act === "histpick") { this._pickHist(el); return; }
      if (act === "zoom") { this._zoom(el); return; }
    }
    /* 相册存货点选：直接成为「当前结果」（发送/下载可用；存册隐藏——已在册）。 */
    _pickStock(el) {
      const i = parseInt(el.getAttribute("data-idx") || "-1", 10);
      const it = (this._stockItems || [])[i];
      if (!it) return;
      this._result = {
        ok: true, preview_url: it.url, path: it.path,
        filename: (it.path || "").split(/[\\/]/).pop() || "album.jpg",
        engine: "album", album: true, media_id: it.media_id || "",
      };
      this._status("");
      this._renderResult(this._result);
    }
    _pickHist(el) {
      const i = parseInt(el.getAttribute("data-idx") || "-1", 10);
      const it = this._hist[i];
      if (!it) return;
      this._result = it;
      this._renderResult(it);
    }
    _zoom(el) {
      const src = el.getAttribute("src") || (this._result && this._result.preview_url) || "";
      if (!src) return;
      const ov = document.createElement("div");
      ov.className = "im-zoom";
      const img = document.createElement("img");
      img.src = src;
      ov.appendChild(img);
      ov.addEventListener("click", () => { try { ov.remove(); } catch (_e) { /* 已移除 */ } });
      // 挂 shadowRoot 内（样式作用域）；fixed 定位不受宿主布局影响
      this.shadowRoot.appendChild(ov);
    }

    async _generate() {
      if (this._busy) return;
      const prompt = this._val("prompt");
      if (!prompt) { this._err(this.t("cp.image.prompt_empty")); return; }
      const client = this._cl();
      if (!client || !client.imageGenerate) { this._err(this.t("cp.image.no_client")); return; }
      const engine = (this.shadowRoot.querySelector('[data-role="engine"]') || {}).value || this._engine;
      const size = (this.shadowRoot.querySelector('[data-role="size"]') || {}).value || "1024x1024";
      const [w, h] = size.split("x").map((n) => parseInt(n, 10) || 1024);
      const lockEl = this.shadowRoot.querySelector('[data-role="lock"]');
      const lock = this._mode === "selfie" && (!lockEl || (lockEl.checked && !lockEl.disabled));
      const body = {
        persona_id: this._persona, prompt: prompt, mode: this._mode,
        engine: engine, scene: this._val("scene"), lock_face: lock,
        width: w, height: h,
      };
      const tok = ++this._tok;
      this._busy = true;
      this._result = null;
      this._jobId = "";
      this._renderSkeleton(w, h);
      this._setGenDisabled(true);
      this._startTick(tok);
      const finish = (d) => {
        if (tok !== this._tok) return;
        this._stopTick();
        this._busy = false;
        this._jobId = "";
        this._setGenDisabled(false);
        this._maybeRefetchConfig(true);   // 配额已消耗/队列已变，立刻对账
        if (!d || !d.ok) { this._renderFail(d || {}); return; }
        this._result = d;
        this._pushHist(d);
        this._renderResult(d);
      };
      const fail = (e) => {
        if (tok !== this._tok) return;
        this._stopTick();
        this._busy = false;
        this._jobId = "";
        this._setGenDisabled(false);
        this._renderFail({ code: "network",
                           reason: String((e && e.message) || e || "") });
      };
      // 异步任务路径（后端 jobs_api）：可取消 + 断连自愈（轮询丢一拍无妨）；
      // 旧后端/客户端缺方法 → 同步路径（原行为）。
      if (this._jobsApi && client.imageJobCreate && client.imageJobStatus) {
        try {
          const c = await client.imageJobCreate(body);
          if (tok !== this._tok) return;
          if (!c || !c.ok || !c.job_id) { finish(c || {}); return; }
          this._jobId = String(c.job_id);
          this._pollJob(tok, this._jobId, finish, fail);
        } catch (e) { fail(e); }
        return;
      }
      try {
        const d = await client.imageGenerate(body);
        finish(d);
      } catch (e) { fail(e); }
    }
    async _pollJob(tok, jobId, finish, fail) {
      const client = this._cl();
      let misses = 0;
      const step = async () => {
        if (tok !== this._tok) return;
        let s = null;
        try { s = await client.imageJobStatus(jobId); } catch (_e) { s = null; }
        if (tok !== this._tok) return;
        if (!s || !s.ok) {
          misses += 1;
          if (misses >= 4) { fail(new Error("job poll lost")); return; }
          setTimeout(step, 2000);
          return;
        }
        misses = 0;
        if (s.status === "done" || s.status === "failed") {
          finish(s.result || {});
          return;
        }
        if (s.status === "cancelled") {
          this._stopTick();
          this._busy = false;
          this._jobId = "";
          this._setGenDisabled(false);
          this._out("");
          this._status(this.t("cp.image.canceled"));
          return;
        }
        setTimeout(step, 1500);
      };
      setTimeout(step, 1200);
    }
    async _cancelGen() {
      const jobId = this._jobId;
      // 无任务 id（同步路径）：本地代际作废——响应回来即被丢弃（GPU 侧无法中断）。
      ++this._tok;
      this._stopTick();
      this._busy = false;
      this._setGenDisabled(false);
      this._out("");
      this._status(this.t("cp.image.canceled"));
      if (jobId) {
        const client = this._cl();
        try { if (client && client.imageJobCancel) await client.imageJobCancel(jobId); }
        catch (_e) { /* best-effort */ }
      }
      this._jobId = "";
    }
    _renderSkeleton(w, h) {
      const ratio = (w > 0 && h > 0) ? (w + " / " + h) : "1 / 1";
      this._out(
        '<div class="im-skel" style="aspect-ratio:' + ratio + ';max-height:240px;"></div>' +
        '<div class="im-skel-bar"><span class="im-status" data-role="skelstage">' +
        this.esc(this.t("cp.image.stage_running")) + "</span>" +
        '<button type="button" class="im-cancel" data-act="cancelgen">' +
        this.esc(this.t("cp.image.cancel")) + "</button></div>");
    }
    _pushHist(d) {
      if (!d || !d.preview_url) return;
      this._hist = [d].concat(
        this._hist.filter((x) => x.preview_url !== d.preview_url)).slice(0, 3);
    }
    _setGenDisabled(v) {
      const b = this.shadowRoot.querySelector('[data-act="gen"]');
      if (!b) return;
      b.disabled = !!v || this._allUndeployed();
      // busy 期按钮自述状态（对齐 cp-voice gen_busy_btn 先例），恢复时还原图标+文案。
      b.innerHTML = v ? this.esc(this.t("cp.image.gen_busy"))
        : svg("gen", 14) + " " + this.esc(this.t("cp.image.gen_btn"));
    }
    /* 结构化失败卡：人话标题+建议（按 code 取 errc_* / errs_*，缺键回落通用）+
       可折叠技术详情 + 重试/复制诊断。原始 reason 绝不再当正文糊坐席一脸。 */
    _renderFail(d) {
      this._status("");
      const code = String((d && d.code) || "unknown");
      const reason = String((d && (d.reason || d.error)) || "");
      this._lastFail = { code: code, reason: reason };
      const tk = "cp.image.errc_" + code;
      let title = this.t(tk);
      if (title === tk) title = this.t("cp.image.err_ttl_generic");
      const sk = "cp.image.errs_" + code;
      let sug = this.t(sk);
      if (sug === sk) sug = this.t("cp.image.errs_unknown");
      const html =
        '<div class="im-errcard">' +
        '<div class="im-err-ttl">⛔ ' + this.esc(title) + "</div>" +
        (sug ? '<div class="im-err-sug">' + this.esc(sug) + "</div>" : "") +
        (reason ? '<details class="im-err-tech"><summary>' +
          this.esc(this.t("cp.image.tech_detail")) + "</summary><pre>" +
          this.esc(reason) + "</pre></details>" : "") +
        '<div class="im-acts">' +
        '<button type="button" data-act="redo">' + svg("redo", 12) + " " +
        this.esc(this.t("cp.image.retry")) + "</button>" +
        '<button type="button" data-act="copydiag">' +
        this.esc(this.t("cp.image.copy_diag")) + "</button>" +
        "</div></div>";
      this._out(html);
    }
    /* 一键复制诊断信息（对接报障入口线：坐席贴给运维即可定位，不必截图红字）。 */
    _copyDiag(btn) {
      const f = this._lastFail || {};
      const c = this._ctx || {};
      const text = [
        "cp-image diag @ " + new Date().toISOString(),
        "code=" + (f.code || "-"),
        "engine=" + (this._engine || "-"),
        "persona=" + (this._persona || "-"),
        "mode=" + this._mode + " scene=" + (this._val("scene") || "-"),
        "conv=" + (c.conversationId || "-"),
        "reason=" + (f.reason || "-"),
      ].join("\n");
      const done = () => { if (btn) btn.textContent = this.t("cp.image.copied"); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, done);
          return;
        }
      } catch (_e) { /* 回落 execCommand */ }
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        done();
      } catch (_e) { done(); }
    }
    _startTick(tok) {
      let n = 0;
      this._status(this.tf("cp.image.generating", { s: 0 }));
      this._stopTick();
      this._secTimer = setInterval(() => {
        if (tok !== this._tok) { this._stopTick(); return; }
        n += 1;
        this._status(this.tf("cp.image.generating", { s: n }));
      }, 1000);
    }
    _stopTick() {
      if (this._secTimer) { clearInterval(this._secTimer); this._secTimer = null; }
    }
    _renderResult(d) {
      const esc = (s) => this.esc(s);
      this._status("");
      const isAlbum = !!(d && d.album);
      const cap = (this._mode === "selfie" || isAlbum)
        ? '<input type="text" class="im-cap" data-role="caption" placeholder="' + esc(this.t("cp.image.caption_ph")) + '" />' : "";
      // 徽章按实陈述：体检项是尺度/场景（不是身份）；同脸校验真跑通过才加「同脸 ✓」。
      const badge = isAlbum
        ? esc(this.t("cp.image.album_badge"))
        : esc(this.tf("cp.image.done_badge", { e: this._engineLabel(d.engine) })) +
          (d.identity === "ok" ? " " + esc(this.t("cp.image.badge_identity")) : "");
      const hist = this._hist.length
        ? '<div class="im-hist"><div class="ttl">' + esc(this.t("cp.image.hist_ttl")) + '</div>' +
          '<div class="im-hist-row">' + this._hist.map((x, i) =>
            '<img src="' + esc(x.preview_url) + '" alt="" data-act="histpick" data-idx="' + i + '" />').join("") +
          "</div></div>"
        : "";
      const html =
        '<div class="im-preview"><img class="im-fadein" src="' + esc(d.preview_url) +
        '" alt="preview" data-act="zoom" />' +
        '<div class="im-badge">' + svg(isAlbum ? "album" : "gen", 11) + badge + "</div></div>" +
        cap +
        '<div class="im-acts">' +
        '<button type="button" class="primary" data-act="send"' + (this._hasCtx() ? "" : " disabled") + '>' +
        svg("send", 12) + " " + esc(this.t("cp.image.send")) + "</button>" +
        (isAlbum ? "" : '<button type="button" data-act="save">' + svg("album", 12) + " " + esc(this.t("cp.image.save_album")) + "</button>") +
        '<button type="button" data-act="redo">' + svg("redo", 12) + " " + esc(this.t("cp.image.redo")) + "</button>" +
        '<button type="button" data-act="dl">' + svg("dl", 12) + " " + esc(this.t("cp.image.download")) + "</button>" +
        "</div>" +
        (this._hasCtx() ? "" : '<div class="im-hint">' + esc(this.t("cp.image.need_conv_send")) + "</div>") +
        hist;
      const out = this._out(html);
      // 图片淡入（加载完成再显形，防半加载闪烁；reduced-motion 用户由 CSS 兜底）
      try {
        const img = out && out.querySelector("img.im-fadein");
        if (img) {
          const on = () => { try { img.classList.add("on"); } catch (_e) { /* 无害 */ } };
          if (img.complete) on(); else img.addEventListener("load", on, { once: true });
          setTimeout(on, 1500);  // 兜底：load 事件丢失也不至于永久透明
        }
      } catch (_e) { /* 渲染增强失败不影响功能 */ }
    }

    _syncSendBtn() {
      const b = this.shadowRoot && this.shadowRoot.querySelector('[data-act="send"]');
      if (b) b.disabled = !this._hasCtx();
    }

    async _send(btn) {
      if (!this._result || !this._hasCtx()) return;
      const client = this._cl();
      if (!client || !client.sendMedia) { this._err(this.t("cp.image.no_client")); return; }
      const c = this._ctx || {};
      if (btn) { btn.disabled = true; }
      this._status(this.t("cp.image.sending"));
      try {
        const r = await fetch(this._result.preview_url, {
          headers: (root.CopilotShared && root.CopilotShared.authHeaders)
            ? root.CopilotShared.authHeaders() : {},
          credentials: "same-origin",
        });
        if (!r.ok) throw new Error("blob " + r.status);
        const blob = await r.blob();
        const cap = this._val("caption");
        const d = await client.sendMedia({
          platform: c.platform || "", accountId: c.accountId || "default",
          chatKey: c.chatKey || "", blob: blob, filename: this._result.filename || "gen.png",
          caption: cap, clientMsgId: "cpimg-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8),
        });
        this._status("");
        if (!d || !d.ok) { this._err(this.tf("cp.image.send_fail", { r: String((d && (d.error || d.detail)) || "") })); if (btn) btn.disabled = false; return; }
        // 复用宿主既有事件：发媒体后刷新会话/消息流（与 cp-voice-sent 同哲学）。
        try { this.emit("cp-image-sent", { conversationId: c.conversationId }); } catch (_e) { /* 事件不阻断 */ }
        if (btn) { btn.textContent = this.t("cp.image.sent"); }
        // P1：回写媒体账本（best-effort）——自动链的重发冷却/服装连续窗认得这次发送。
        try {
          if (client.imageMarkSent) {
            client.imageMarkSent({
              persona_id: this._persona,
              path: this._result.path || "",
              media_id: this._result.media_id || "",
              scene: this._val("scene"),
              album: !!this._result.album,
              platform: c.platform || "", account_id: c.accountId || "default",
              chat_key: c.chatKey || "",
            });
          }
        } catch (_e) { /* 账本回写失败不影响已完成的发送 */ }
      } catch (_e) {
        this._status("");
        this._err(this.t("cp.image.net_err"));
        if (btn) btn.disabled = false;
      }
    }

    async _saveAlbum(btn) {
      if (!this._result) return;
      const client = this._cl();
      if (!client || !client.imageSaveAlbum) { this._err(this.t("cp.image.no_client")); return; }
      if (btn) btn.disabled = true;
      try {
        const d = await client.imageSaveAlbum({
          persona_id: this._persona, path: this._result.path, scene: this._val("scene"),
        });
        if (!d || !d.ok) { this._err(this.tf("cp.image.save_fail", { r: String((d && (d.error || d.detail)) || "") })); if (btn) btn.disabled = false; return; }
        if (btn) { btn.textContent = this.t("cp.image.saved"); }
      } catch (_e) {
        this._err(this.t("cp.image.net_err"));
        if (btn) btn.disabled = false;
      }
    }

    _download() {
      if (!this._result || !this._result.preview_url) return;
      const a = document.createElement("a");
      a.href = this._result.preview_url;
      a.download = this._result.filename || "generated.png";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { try { a.remove(); } catch (_e) { /* 清理尽力 */ } }, 0);
    }
    tf(key, vars) { return this.t(key, vars); }
  }

  if (!customElements.get("cp-image")) customElements.define("cp-image", CpImage);
})(typeof window !== "undefined" ? window : this);
