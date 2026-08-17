"use strict";
/* 两端共享组件 · 会话人设(<cp-persona>)— 继承 CpPanelBase
   2026-08-17 呈现层升级(绑定语义零改动):
   ① 硬编码亮色 hex 全部令牌化(--cp-ok-bg/--cp-warn-* 两主题各取值)——暗色主题下
      选中卡曾是「近白字压浅绿底」完全不可读(对比度 ~1.05:1)的实锤事故;
   ② 人设身份色盘首字头像(_pcolor 与宿主身份条 _acctColor('persona:'+pid) 逐字节
      同构——同一人设在 列表卡/生效横幅/底部身份条 三处同色,跨区域身份色语言);
   ③ 列表稳定分区排序(生效者最前>账号默认次之>其余保运营配置原序,不做隐形重排)、
      同名人设 ·id 消歧、tier 徽章 hover 人话解释、空态「去创建」CTA、
      键盘可达(Enter/Space 委托 + 方向键/Home/End + focus-visible);
   ④ 确认弹窗 from→to 色盘、列表底渐隐(仅溢出时亮)、usage_7d 角标、方向键循环。
   2026-07-26 方案 A 重写:三层真相 UI ——
   ① 生效横幅:这条会话此刻实际以谁的身份说话(与出站链同一 resolver 的读侧,
      /api/persona/effective),tier 徽章标明来源(会话覆写/账号人设/legacy/域默认);
   ② 会话级覆写选择器(开关 inbox.persona_conv_override.enabled 开启时):卡片列表 +
      搜索,点选即换绑当前会话(有既往绑定/出站历史时先弹确认——用户拍板的弹窗口径);
   ③ 优雅降级:effective API 不可用(RPA 2 段键会话/桌面壳未暴露 IPC)或开关关 →
      回落 legacy peer 级绑定 UI(原 select,行为与旧版完全一致)。
   组件只写后端(bindings_runtime.yaml 三段键),变更后派发 cp-persona-changed 供宿主联动。
   用法:
     const el = document.createElement('cp-persona');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey, platform, accountId };
   client 需实现:listPersonas / getPersonaBindings / bindPersona / unbindPersona
     + personaEffective / bindConvPersona / unbindConvPersona(缺失时自动降级 legacy)*/
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-persona: CpPanelBase 未加载"); return; }

  const TIER_KEYS = {
    conv_override: "cp.persona.tier.conv_override",
    account_profile: "cp.persona.tier.account_profile",
    chat_binding: "cp.persona.tier.chat_binding",
    domain: "cp.persona.tier.domain",
    default: "cp.persona.tier.default",
  };

  /* 人设身份色盘:与宿主 unified_inbox._acctColor('persona:'+pid) **逐字节同构**
     (哈希 h*31+charCode >>>0 × 同一 8 色盘)。抽成模块级纯函数,门禁可钉
     「右栏色盘 ≡ 底部身份条圆点」而不用起 DOM。改色盘务必同步宿主 _ACCT_PALETTE。 */
  const PERSONA_DISC_PALETTE = ["#3b82f6", "#06b6d4", "#10b981", "#6366f1",
    "#0ea5e9", "#14b8a6", "#8b5cf6", "#f59e0b"];
  function personaDiscColor(pid) {
    const s = "persona:" + String(pid || "");
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return PERSONA_DISC_PALETTE[h % PERSONA_DISC_PALETTE.length];
  }
  /* 列表分区排序:0=当前生效 1=账号默认 2=其余。纯函数,不按用量重排
     (用量只做角标——隐形重排会让运营「刚配的新人设找不到」)。 */
  function personaListRank(id, effId, acctId) {
    if (id && id === effId) return 0;
    if (id && id === acctId) return 1;
    return 2;
  }

  class CpPersona extends Base {
    constructor() {
      super();
      this._pending = null;   // 待确认的换绑 {pid, toName}
      this._lastAttempt = null;   // 最近一次换绑尝试 {kind:'conv'|'acct', pid}（失败重试用）
      this._busy = false;
      // select 变更非点击,基类只委托 click,这里补 change 委托(legacy 模式用)
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="persona"]');
        if (s && !s.disabled) this._onSelect(s.value);
      });
      // 搜索:input 事件就地过滤卡片(不重渲染 → 不丢焦点)
      this.shadowRoot.addEventListener("input", (e) => {
        const inp = e.target.closest('input[data-role="psearch"]');
        if (inp) this._filterCards(inp.value);
      });
      // 键盘可达:div 卡片(tabindex)上 Enter/Space 触发 data-act。
      // 真按钮/链接/表单控件跳过——它们的 Enter 会派生原生 click,再触发一次
      // 基类 click 委托就是双发。方向键/Home/End 在 listbox 内移动焦点。
      this.shadowRoot.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Home" || e.key === "End") {
          if (e.target.closest("input,textarea,select")) return;
          const list = this.shadowRoot.querySelector(".plist");
          if (!list) return;
          const cards = Array.prototype.slice.call(list.querySelectorAll(".pcard:not(.hide)"));
          if (!cards.length) return;
          const cur = cards.indexOf(e.target.closest(".pcard"));
          let next = 0;
          if (e.key === "End") next = cards.length - 1;
          else if (e.key === "ArrowDown") next = cur < 0 ? 0 : (cur + 1) % cards.length;
          else if (e.key === "ArrowUp") next = cur < 0 ? cards.length - 1 : (cur - 1 + cards.length) % cards.length;
          e.preventDefault();
          cards[next].focus();
          return;
        }
        if (e.key !== "Enter" && e.key !== " ") return;
        if (e.target.closest("button,a,select,input,textarea")) return;
        const b = e.target.closest("[data-act]");
        if (b && !b.disabled) { e.preventDefault(); this.onAction(b.getAttribute("data-act"), b); }
      });
    }
    emptyText() { return this.t("cp.persona.empty"); }
    emptyDataText() { return this.t("cp.persona.no_data"); }
    errText() { return this.t("cp.persona.err"); }
    styles() {
      return `
      .lbl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:var(--cp-gap-xs,4px); }
      select { width:100%; font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .src { font-size:var(--cp-fs-tiny,11px); margin-top:var(--cp-gap-xs,4px); }
      .src.bound { color:var(--cp-ok,#0f9d75); }
      .src.unbound { color:var(--cp-text-tiny,#94a3b8); }
      /* —— 生效横幅 —— */
      .eff { display:flex; align-items:center; gap:6px; padding:6px 8px; margin-bottom:6px;
             border-radius:var(--cp-radius-sm,6px); background:var(--cp-surface-2,#f8fafc);
             border:1px solid var(--cp-border,#e2e8f0);
             border-left:3px solid var(--cp-accent,#4f46e5); }
      .eff.t-conv { border-left-color:var(--cp-ok,#0f9d75); }
      .eff.t-legacy { border-left-color:var(--cp-warn-ink,#b45309); }
      .eff .nm { flex:1; min-width:0; font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); color:var(--cp-text,#1e293b);
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .eff .tier { flex:none; font-size:10px; padding:1px 6px; border-radius:999px;
                   background:var(--cp-accent-bg,#eef2ff); color:var(--cp-accent,#4f46e5); }
      .eff .tier[title]:not([title=""]) { cursor:help; }
      .eff.t-conv .tier { background:var(--cp-ok-bg,#ecfdf5); color:var(--cp-ok,#0f9d75); }
      .eff.t-legacy .tier { background:var(--cp-warn-bg,#fffbeb); color:var(--cp-warn-ink,#b45309); }
      /* 人设身份色盘(与宿主身份条圆点同色源):首字符盘,跨区域同色=同一人设 */
      .pdisc { flex:none; width:20px; height:20px; border-radius:50%; display:inline-flex;
               align-items:center; justify-content:center; font-size:10px; font-weight:700;
               color:#fff; letter-spacing:.3px; user-select:none; }
      .eff .pdisc { width:22px; height:22px; font-size:11px; }
      .warn { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn-ink,#b45309); background:var(--cp-warn-bg,#fffbeb);
              border:1px solid var(--cp-warn-border,#fde68a); border-radius:var(--cp-radius-sm,6px);
              padding:4px 6px; margin-bottom:6px; }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:4px 0 6px; }
      /* —— 卡片选择器 —— */
      .psearch { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
                 padding:4px 8px; margin-bottom:4px; border:1px solid var(--cp-border,#e2e8f0);
                 border-radius:var(--cp-radius-sm,6px); background:var(--cp-surface,#fff);
                 color:var(--cp-text,#1e293b); }
      .plist-wrap { position:relative; }
      .plist { max-height:min(40vh,320px); overflow-y:auto; display:flex; flex-direction:column; gap:3px; }
      /* 底渐隐:只在列表真溢出时亮(is-ovf);短名单不盖最后一张 */
      .plist-wrap::after { content:""; pointer-events:none; position:absolute; left:0; right:0; bottom:0;
                           height:14px; border-radius:0 0 var(--cp-radius-sm,6px) var(--cp-radius-sm,6px);
                           background:linear-gradient(to bottom, transparent, var(--cp-surface,#fff));
                           opacity:0; transition:opacity .15s ease; }
      .plist-wrap.is-ovf::after { opacity:1; }
      .pcard { display:flex; align-items:center; gap:7px; padding:6px 8px; cursor:pointer;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff);
               transition:background-color .12s ease,border-color .12s ease; }
      .pcard:hover { border-color:var(--cp-accent,#4f46e5); }
      .pcard:focus-visible { outline:2px solid var(--cp-accent,#4f46e5); outline-offset:1px; }
      .pcard.on { border-color:var(--cp-ok,#0f9d75); background:var(--cp-ok-bg,#ecfdf5); }
      .pcard.on .pnm { color:var(--cp-ok,#0f9d75); }
      .pcard .pnm { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                    color:var(--cp-text,#1e293b); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pcard .pusage { flex:none; font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .pcard .pid { flex:none; font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .pcard .pmic { flex:none; display:inline-flex; color:var(--cp-text-dim,#64748b); }
      .pcard .prole { flex:1; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pcard .pmark { flex:none; font-size:10px; color:var(--cp-ok,#0f9d75); }
      .pcard .ptag { flex:none; font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .pcard .pacct { flex:none; font-size:10px; padding:1px 6px; border-radius:999px;
                      border:1px solid var(--cp-border,#e2e8f0); color:var(--cp-text-dim,#64748b);
                      background:var(--cp-surface,#fff); cursor:pointer; visibility:hidden; }
      .pcard:hover .pacct, .pcard:focus-within .pacct { visibility:visible; }
      /* 触屏设备没有 hover——「整号」入口常显（P1：hover-only 在平板/手机上等于不存在） */
      @media (hover: none) { .pcard .pacct { visibility:visible; } }
      .pcard .pacct:hover { border-color:var(--cp-accent,#4f46e5); color:var(--cp-accent,#4f46e5); }
      /* 通用 hide：failtip/nomatch 初始即带 hide class。shadow DOM 不继承页面样式，
         此前只有 .pcard.hide 一条规则 → 「切换失败，请重试」「没有匹配的人设」两个
         本应隐藏的元素**常显**，被误读成真实换绑失败（2026-08-01 实锤）。 */
      .hide { display:none; }
      .pcard.hide { display:none; }
      .nomatch { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); padding:4px 2px; }
      .cta { display:inline-block; margin-top:4px; font-size:var(--cp-fs-sm,12px);
             color:var(--cp-accent,#4f46e5); text-decoration:none; }
      .cta:hover { text-decoration:underline; }
      .clr { margin-top:6px; }
      .busy .plist,.busy .clr { pointer-events:none; opacity:.55; }
      /* —— 确认弹窗 —— */
      .cfm-mask { position:fixed; inset:0; background:rgba(15,23,42,.45); z-index:9998; }
      .cfm { position:fixed; left:50%; top:38%; transform:translate(-50%,-50%); z-index:9999;
             width:min(320px,86vw); background:var(--cp-surface,#fff);
             border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius,10px);
             padding:12px; box-shadow:0 12px 32px rgba(15,23,42,.22); }
      .cfm .ct { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); margin-bottom:6px; }
      .cfm-swap { display:flex; align-items:center; gap:6px; margin-bottom:8px; }
      .cfm-swap .sn { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600);
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:88px; }
      .cfm-swap .arrow { flex:none; color:var(--cp-text-tiny,#94a3b8); font-size:13px; }
      .cfm .cb { font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#374151); line-height:1.5; }
      .cfm .cw { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn-ink,#b45309); background:var(--cp-warn-bg,#fffbeb);
                 border:1px solid var(--cp-warn-border,#fde68a); border-radius:var(--cp-radius-sm,6px);
                 padding:4px 6px; margin-top:6px; }
      .cfm .cacts { display:flex; gap:6px; justify-content:flex-end; margin-top:10px; }
      .failtip { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:4px; }
      .failtip .fstill { color:var(--cp-text-dim,#64748b); margin-top:2px; }
      .failtip .facts { display:flex; gap:6px; margin-top:4px; }
      .failtip .facts button { font:inherit; font-size:var(--cp-fs-tiny,11px); padding:2px 8px;
                               cursor:pointer; border:1px solid var(--cp-border,#e2e8f0);
                               border-radius:var(--cp-radius-sm,6px); background:var(--cp-surface,#fff);
                               color:var(--cp-text,#1e293b); }
      .failtip .facts button:hover { border-color:var(--cp-accent,#4f46e5); }`;
    }

    async fetchData(ctx) {
      // effective(3 段键才可用) 与 人设清单/legacy 绑定 并行取
      const cid = String(ctx.conversationId || "");
      const canEff = cid.split(":").length >= 3 && this._client.personaEffective;
      const [list, binds, eff] = await Promise.all([
        this._client.listPersonas(),
        this._client.getPersonaBindings().catch(() => null),
        canEff
          ? this._client.personaEffective({ conversationId: cid }).catch(() => null)
          : Promise.resolve(null),
      ]);
      if (list && list.ok === false) return list;
      const summary = (list && list.summary) || [];
      const profiles = (list && list.profiles) || {};
      const bindings = (binds && binds.bindings) || {};
      const bound = (ctx.chatKey && bindings[ctx.chatKey]) || null;
      return {
        ok: true, summary, profiles,
        eff: (eff && eff.ok) ? eff : null,
        boundId: bound ? (bound.id || "") : "",
        boundName: bound ? (bound.name || "") : "",
      };
    }

    /* —— 渲染 —— */

    renderData(d) {
      if (d.eff) return this._renderEffective(d);
      return this._renderLegacy(d);
    }

    _tierChip(tier) {
      const k = TIER_KEYS[tier] || TIER_KEYS.default;
      return this.t(k);
    }

    /* tier 徽章 hover 人话解释;词条缺失(t 回落 key 本身)返回空串不当 tooltip 展示 */
    _tierTip(tier) {
      const k = (TIER_KEYS[tier] || TIER_KEYS.default) + ".tip";
      const s = this.t(k);
      return s === k ? "" : s;
    }

    _pcolor(pid) { return personaDiscColor(pid); }

    _disc(pid, name) {
      const s = String(name || pid || "").trim();
      const initial = s ? s.charAt(0).toUpperCase() : "?";
      return `<span class="pdisc" style="background:${this._pcolor(pid)}" aria-hidden="true">${this.esc(initial)}</span>`;
    }

    _renderEffective(d) {
      const esc = (s) => this.esc(s);
      const eff = d.eff;
      const e = eff.effective || {};
      const tier = String(e.tier || "default");
      const tone = tier === "conv_override" ? "t-conv"
        : (tier === "chat_binding" ? "t-legacy" : "");
      const voice = e.has_voice ? " 🎙" : "";
      const tierTip = this._tierTip(tier);
      let html =
        `<div class="lbl">${esc(this.t("cp.persona.eff_label"))}</div>` +
        `<div class="eff ${tone}">` +
        // 有身份才出色盘头像——「当前生效:—」旁挂彩色「?」盘是视觉噪音
        ((e.id || e.name) ? this._disc(e.id || "", e.name || e.id || "") : "") +
        `<span class="nm">${esc(this.t("cp.persona.eff_speaking", { name: (e.name || e.id || "—") + voice }))}</span>` +
        `<span class="tier" title="${esc(tierTip)}">${esc(this._tierChip(tier))}</span>` +
        `</div>`;
      if (eff.legacy_suppressed && eff.legacy && eff.legacy.name) {
        html += `<div class="warn">${esc(this.t("cp.persona.legacy_suppressed", { name: eff.legacy.name }))}</div>`;
      }
      if (!eff.enabled) {
        // 开关关:横幅照说真话,下面回落 legacy 绑定 UI(旧语义)
        html += `<div class="hint">${esc(this.t("cp.persona.disabled_hint"))}</div>`;
        html += this._legacyBody(d);
        return html;
      }
      // 覆写选择器:搜索 + 卡片
      const summary = Array.isArray(d.summary) ? d.summary : [];
      const convId = (eff.conv && eff.conv.id) || "";
      const acctId = (eff.account && eff.account.id) || "";
      html += `<div class="lbl">${esc(this.t("cp.persona.conv_pick"))}</div>`;
      // 空态死胡同收口:没有人设时给「去创建」出路而不是空列表
      if (!summary.length) {
        html += `<div class="nomatch">${esc(this.t("cp.persona.no_data"))}</div>` +
          `<a class="cta" href="/personas" target="_blank" rel="noopener">${esc(this.t("cp.persona.create_cta"))}</a>` +
          `<div class="failtip hide" data-role="failtip">${esc(this.t("cp.persona.switch_fail"))}</div>` +
          `<div data-role="cfm-slot"></div>`;
        return html;
      }
      if (summary.length > 5) {
        html += `<input class="psearch" data-role="psearch" type="text" placeholder="${esc(this.t("cp.persona.search_ph"))}">`;
      }
      // 「整号」mini 入口（hover 显现）：把整个账号切到该人设——运营拍板不限 master，
      // 但必须能定位 platform+account（RPA 2 段键会话定位不了 → 不渲染入口）。
      const canAcct = !!this._acctRef();
      // 同名消歧:重名人设(生产实录两个「小雨」)在名字后缀淡色 ·id
      const nameCount = {};
      summary.forEach((p) => {
        const k = String((p && p.name) || (p && p.id) || "");
        nameCount[k] = (nameCount[k] || 0) + 1;
      });
      // 稳定分区:生效者最前 > 账号默认次之 > 其余保运营配置原序
      // (可预期性优先——不按用量等隐形指标重排;Array.sort 稳定)
      const effId = String(e.id || "");
      const ordered = summary.slice().sort((a, b) =>
        personaListRank(a.id, effId, acctId) - personaListRank(b.id, effId, acctId));
      const cards = ordered.map((p) => {
        const on = p.id === convId;
        const isAcct = p.id === acctId;
        const q = `${p.name || ""} ${p.role || ""} ${p.id || ""}`.toLowerCase();
        const dup = (nameCount[String(p.name || p.id)] || 0) > 1;
        const usage = Number(p.usage_7d || 0);
        const tipParts = [String(p.name || p.id)];
        if (p.role) tipParts.push(String(p.role));
        tipParts.push(String(p.id));
        if (usage > 0) tipParts.push(this.t("cp.persona.usage_7d_t", { n: usage }));
        return `<div class="pcard${on ? " on" : ""}" data-act="pick" data-pid="${esc(p.id)}" data-q="${esc(q)}"` +
          ` role="option" tabindex="0" aria-selected="${on ? "true" : "false"}" title="${esc(tipParts.join(" · "))}">` +
          this._disc(p.id, p.name || p.id) +
          `<span class="pnm">${esc(p.name || p.id)}</span>` +
          (dup ? `<span class="pid">·${esc(p.id)}</span>` : "") +
          `<span class="prole">${esc(p.role || "")}</span>` +
          (usage > 0 ? `<span class="pusage" title="${esc(this.t("cp.persona.usage_7d_t", { n: usage }))}">${esc(this.t("cp.persona.usage_7d_short", { n: usage }))}</span>` : "") +
          (p.has_voice ? `<span class="pmic" title="${esc(this.t("cp.persona.has_voice_t"))}">${this.ic("mic", 11)}</span>` : "") +
          (on ? `<span class="pmark">✓</span>` : "") +
          (isAcct && !on ? `<span class="ptag">${esc(this._tierChip("account_profile"))}</span>` : "") +
          (canAcct && !isAcct
            ? `<span class="pacct" data-act="pick-acct" data-pid="${esc(p.id)}" role="button" tabindex="0" title="${esc(this.t("cp.persona.acct_btn_title"))}">${esc(this.t("cp.persona.acct_btn"))}</span>`
            : "") +
          `</div>`;
      });
      html += `<div class="plist-wrap"><div class="plist" role="listbox" aria-label="${esc(this.t("cp.persona.conv_pick"))}">${cards.join("")}` +
        `<div class="nomatch hide" data-role="nomatch">${esc(this.t("cp.persona.no_match"))}</div></div></div>`;
      if (convId) {
        html += `<div class="clr"><button data-act="clear">${esc(this.t("cp.persona.clear_override"))}</button></div>`;
      }
      html += `<div class="failtip hide" data-role="failtip">${esc(this.t("cp.persona.switch_fail"))}</div>`;
      html += `<div data-role="cfm-slot"></div>`;
      return html;
    }

    _legacyBody(d) {
      const esc = (s) => this.esc(s);
      const summary = Array.isArray(d.summary) ? d.summary : [];
      const boundId = d.boundId || "";
      const opts = [`<option value="">${esc(this.t("cp.persona.unbound_opt"))}</option>`]
        .concat(summary.map((p) => {
          const label = p.role ? `${p.name} (${p.role})` : (p.name || p.id);
          const sel = p.id === boundId ? " selected" : "";
          return `<option value="${esc(p.id)}"${sel}>${esc(label)}</option>`;
        }));
      const src = boundId
        ? `<div class="src bound">${esc(this.t("cp.persona.bound", { name: d.boundName || boundId }))}</div>`
        : `<div class="src unbound">${esc(this.t("cp.persona.unbound"))}</div>`;
      /* P1-198 账号级默认升为主操作：客户实测按 peer 逐个绑（每接一个新客户都要
         重新绑一次）不符合「绑一次跟账号走」的预期。legacy 模式此前只有 peer 下拉，
         账号级入口仅存在于 conv_override 开启后的卡片 UI——这里补上（API 早已存在）。 */
      let acctRow = "";
      if (this._acctRef() && this._client && this._client.setAccountPersona) {
        acctRow =
          `<div class="clr"><button class="primary" data-act="legacy-acct">${esc(this.t("cp.persona.legacy_acct_btn"))}</button></div>` +
          `<div class="hint">${esc(this.t("cp.persona.legacy_acct_hint"))}</div>`;
      }
      // 空态死胡同收口(与 effective 分支同语义):没有人设先给「去创建」出路
      const ctaRow = summary.length ? "" :
        `<div><a class="cta" href="/personas" target="_blank" rel="noopener">${esc(this.t("cp.persona.create_cta"))}</a></div>`;
      return `<select data-role="persona">${opts.join("")}</select>` + src + ctaRow + acctRow +
        `<div class="failtip hide" data-role="failtip">${esc(this.t("cp.persona.switch_fail"))}</div>` +
        `<div data-role="cfm-slot"></div>`;
    }

    _renderLegacy(d) {
      return `<div class="lbl">${this.esc(this.t("cp.persona.label"))}</div>` + this._legacyBody(d);
    }

    /* —— 搜索过滤(就地,不重渲染) —— */
    _filterCards(q) {
      const needle = String(q || "").trim().toLowerCase();
      let vis = 0;
      this.shadowRoot.querySelectorAll(".pcard").forEach((el) => {
        const hit = !needle || (el.getAttribute("data-q") || "").indexOf(needle) >= 0;
        el.classList.toggle("hide", !hit);
        if (hit) vis += 1;
      });
      const nm = this.shadowRoot.querySelector('[data-role="nomatch"]');
      if (nm) nm.classList.toggle("hide", vis > 0);
      this._syncPlistFade();
    }

    _render(html) {
      super._render(html);
      this._syncPlistFade();
    }
    /* 短名单不盖渐隐;过滤后高度变了再量一次。rAF=等 max-height 布局落地。 */
    _syncPlistFade() {
      const run = () => {
        const wrap = this.shadowRoot && this.shadowRoot.querySelector(".plist-wrap");
        const list = wrap && wrap.querySelector(".plist");
        if (!wrap || !list) return;
        wrap.classList.toggle("is-ovf", list.scrollHeight > list.clientHeight + 2);
      };
      try { requestAnimationFrame(run); } catch (_e) { run(); }
    }

    /* —— 点击处理 —— */
    onAction(act, el) {
      if (this._busy) return;
      if (act === "pick") this._onPickConv(el.getAttribute("data-pid") || "");
      else if (act === "pick-acct") this._onPickAccount(el.getAttribute("data-pid") || "");
      else if (act === "legacy-acct") this._onLegacyAccount();
      else if (act === "clear") this._onPickConv("");
      else if (act === "cfm-ok") { const p = this._pending; this._pending = null; this._closeConfirm(); if (p) this._doBindConv(p.pid); }
      else if (act === "cfm-acct") { const p = this._pending; this._pending = null; this._closeConfirm(); if (p) this._doBindAccount(p.pid); }
      else if (act === "cfm-cancel") { this._pending = null; this._closeConfirm(); }
      // —— 失败出路（P1）——
      else if (act === "fail-refresh") this.refresh();
      else if (act === "fail-reload") {
        // 刷新宿主页（401 过期 → 会被带去登录页；csrf → 重载拿全新凭证链）。
        // 桌面 iframe 的 top 是壳页面（跨源访问会抛）→ 回落刷新 iframe 自身。
        try { (window.top || window).location.reload(); }
        catch (_e) { location.reload(); }
      }
      else if (act === "fail-retry") {
        const a = this._lastAttempt;
        if (!a) { this.refresh(); return; }
        if (a.kind === "acct") this._doBindAccount(a.pid);
        else this._doBindConv(a.pid);
      }
    }

    /* —— 失败分型「纯核心」（2026-07-31 P3 抽出常驻门禁）——
       这四个方法**只吃参数、不碰 this / DOM / i18n**，是「所有失败一律『请重试』」
       误导的正解所在，也是最容易随手改错的地方（改一个 status 分支，坐席看到的
       文案/出路/上报口径就变了）。抽成纯函数后由 desktop/test/cp-persona-failtype.test.js
       常驻钉死每个分支；_failText/_showFail/_reportFail 只做「取词 + 拼 DOM」。 */

    /* 失败文案 i18n key（""=未分型，调用方回落通用「切换失败」）。
       客户端已把非 2xx 归一化为 {status, code, error/detail}；code=csrf 是后端结构化
       标记，detail 正则是旧后端（无 code 字段）的兜底识别路径。 */
    _failKey(status, code, detail) {
      if (status === 0) return "cp.persona.fail_net";
      if (status === 401) return "cp.persona.fail_auth";
      if (code === "csrf" || (status === 403 && /csrf/i.test(String(detail || ""))))
        return "cp.persona.fail_csrf";
      if (status === 403) return "cp.persona.fail_denied";
      if (status === 404) return "cp.persona.fail_gone";
      if (status === 409) return "cp.persona.fail_conflict";
      return "";
    }

    /* beacon 上报的错误类型（须在 frontend_error_stats._KNOWN_TYPES 白名单内，
       否则后端折叠成 "Error" → by_type 里看不见这几类 HTTP 失败）。 */
    _failEtype(status, code) {
      if (status === 0) return "neterr";
      if (status === 401) return "http_401";
      if (status === 403) return code === "csrf" ? "http_403_csrf" : "http_403";
      if (status === 404) return "http_404";
      if (status === 409) return "http_409";
      if (status >= 500) return "http_5xx";
      return "Error";
    }

    /* 出路按钮（{act,label} 数组，label 为 i18n key）：确定性拒绝绝不给「重试」
       （4xx 重试必然复现），网络/5xx 才给「重试」+「刷新面板」。 */
    _failActions(status, code) {
      if (status === 401)
        return [{ act: "fail-reload", label: "cp.persona.act_relogin" }];
      if (code === "csrf" || status === 403)
        return [{ act: "fail-reload", label: "cp.persona.act_reload" }];
      if (status === 404 || status === 409)
        return [{ act: "fail-refresh", label: "cp.persona.act_refresh" }];
      return [
        { act: "fail-retry", label: "cp.persona.act_retry" },
        { act: "fail-refresh", label: "cp.persona.act_refresh" },
      ];
    }

    /* 能否断言「本次操作未生效」：仅 4xx（服务端确定性拒绝、肯定没执行）才断言；
       网络类/5xx 请求可能已落地（响应丢失≠没执行）→ 绝不谎报状态。 */
    _failAssertsUnchanged(status) {
      return status >= 400 && status < 500;
    }

    /* —— 失败分型（2026-07-31，修「所有失败一律『请重试』」的误导）——
       服务端 detail 已按请求语言翻好 → 有则并入展示（verbatim 直显是本仓 i18n 约定）。 */
    _failText(r) {
      const status = Number(r && r.status);
      const code = String((r && r.code) || "");
      const detail = String((r && (r.error || r.detail)) || "").slice(0, 140);
      const key = this._failKey(status, code, detail);
      const base = key ? this.t(key) : this.t("cp.persona.switch_fail");
      // 已分型的类别文案自带出路；未分型时若有后端 detail，附上帮助定位
      return (!key && detail) ? `${base} · ${detail}` : base;
    }

    /* 失败提示（P1 升级）：分型文案 + **出路按钮** + 状态重申。
       - 确定性拒绝（4xx）：服务端肯定没执行 → 可以放心断言「当前仍生效：X」，
         消除坐席「到底切没切成」的悬置；按钮给对应出路（重新登录/刷新页面/刷新面板），
         绝不再让人对确定性失败干「重试」。
       - 网络类/5xx：请求可能已落地（响应丢失≠没执行）→ **不断言状态**，
         给「重试」+「刷新面板」（刷新拉回服务端真相，本身就是最诚实的答案）。 */
    _showFail(r) {
      const tip = this.shadowRoot.querySelector('[data-role="failtip"]');
      if (!tip) return;
      const esc = (s) => this.esc(s);
      const status = Number((r && r.status) || 0);
      const code = String((r && r.code) || "");
      const acts = this._failActions(status, code)
        .map((a) => `<button data-act="${a.act}">${esc(this.t(a.label))}</button>`)
        .join("");
      let still = "";
      if (this._failAssertsUnchanged(status)) {
        const e = (this._d && this._d.eff && this._d.eff.effective) || {};
        const effName = e.name || e.id || "";
        if (effName) {
          still = `<div class="fstill">${esc(this.t("cp.persona.still_effective", { name: effName }))}</div>`;
        }
      }
      tip.innerHTML = `<div>${esc(this._failText(r))}</div>${still}<div class="facts">${acts}</div>`;
      const detail = String((r && (r.error || r.detail)) || "");
      tip.title = [status ? `HTTP ${status}` : "", detail].filter(Boolean).join(" · ");
      tip.classList.remove("hide");
    }

    /* 失败自动上报（fire-and-forget）：接入既有 dead-click 遥测通道，
       按 (page, fn, http 分型) 计数进 ops 概览——用户不再是唯一的传感器。 */
    _reportFail(fn, r) {
      try {
        if (typeof navigator === "undefined" || !navigator.sendBeacon) return;
        const etype = this._failEtype(Number(r && r.status), String((r && r.code) || ""));
        const page = (typeof location !== "undefined" && location.pathname) || "copilot";
        navigator.sendBeacon("/api/telemetry/frontend-error", new Blob([JSON.stringify({
          page, fn, type: etype, endpoint: "/api/persona/bind",
        })], { type: "application/json" }));
      } catch (_e) { /* 观测通道绝不影响主流程 */ }
    }

    /* 账号定位（整号切换用）:优先 ctx 显式字段，缺则拆 3 段 conversationId。*/
    _acctRef() {
      const ctx = this._ctx || {};
      let plat = String(ctx.platform || "");
      let acct = String(ctx.accountId || "");
      if (!plat || !acct) {
        const parts = String(ctx.conversationId || "").split(":");
        if (parts.length >= 3) { plat = plat || parts[0]; acct = acct || parts[1]; }
      }
      return (plat && acct) ? { platform: plat, accountId: acct } : null;
    }

    /* 语音能力提示行(换绑弹窗):目标人设没配语音 / 不在自动语音灰度名单 → 预警。
       解除覆写(pid="")回到账号人设=现状,不提示。*/
    _voiceHints(pid) {
      if (!pid) return [];
      const eff = this._d && this._d.eff;
      const summary = (this._d && this._d.summary) || [];
      const p = summary.find((s) => s && s.id === pid)
        || ((this._d && this._d.profiles) || {})[pid] || {};
      const name = p.name || pid;
      const hasVoice = !!(p.has_voice
        || ((p.voice_profile || {}).enabled || (p.voice_profile || {}).backend));
      if (!hasVoice) return [this.t("cp.persona.voice_hint_novoice", { name })];
      const va = (eff && eff.voice_autosend) || {};
      const allow = Array.isArray(va.allowlist) ? va.allowlist : [];
      if (va.enabled && allow.length && allow.indexOf(pid) < 0) {
        return [this.t("cp.persona.voice_hint_gated", { name })];
      }
      return [];
    }

    _onPickConv(pid) {
      const eff = this._d && this._d.eff;
      if (!eff) return;
      const curConv = (eff.conv && eff.conv.id) || "";
      if (pid && pid === curConv) return;          // 点了当前覆写 → 无操作
      if (!pid && !curConv) return;                // 无覆写时点解除 → 无操作
      // 弹窗口径(用户拍板):有既往绑定(覆写/legacy) 或 有出站历史才弹;否则静默直绑
      const hadBinding = !!(eff.conv || eff.legacy);
      if (hadBinding || eff.has_outbound) {
        const fromName = (eff.effective && (eff.effective.name || eff.effective.id)) || "—";
        let toName;
        if (pid) {
          const p = (this._d.profiles || {})[pid] || {};
          toName = p.name || pid;
        } else {
          // 解除覆写 → 回到账号人设/默认
          toName = (eff.account && eff.account.name) || this._tierChip("default");
        }
        this._pending = { pid };
        this._openConfirm({
          pid, fromName, toName,
          fromId: (eff.effective && eff.effective.id) || "",
          toId: pid || ((eff.account && eff.account.id) || ""),
          hasOutbound: !!eff.has_outbound,
          scopes: (pid && this._acctRef()) ? ["conv", "account"] : ["conv"],
        });
        return;
      }
      this._doBindConv(pid);
    }

    /* 卡片「整号」入口:恒弹确认(影响账号全部会话,无静默路径)。*/
    _onPickAccount(pid) {
      if (!pid || !this._acctRef()) return;
      const eff = this._d && this._d.eff;
      const fromName = (eff && eff.account && (eff.account.name || eff.account.id))
        || (eff && eff.effective && (eff.effective.name || eff.effective.id)) || "—";
      const p = ((this._d && this._d.profiles) || {})[pid] || {};
      this._pending = { pid };
      this._openConfirm({
        pid, fromName, toName: p.name || pid,
        fromId: (eff && eff.account && eff.account.id) || "",
        toId: pid,
        hasOutbound: false, scopes: ["account"],
      });
    }

    /* P1-198 legacy 模式「设为账号默认」：取下拉当前选中人设 → 恒弹确认后整号换绑。
       未选人设时如实提示（绝不静默无反应）。 */
    _onLegacyAccount() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const pid = sel ? sel.value : "";
      const tip = this.shadowRoot.querySelector('[data-role="failtip"]');
      if (!pid) {
        if (tip) {
          tip.textContent = this.t("cp.persona.legacy_acct_pick_first");
          tip.classList.remove("hide");
        }
        return;
      }
      if (tip) tip.classList.add("hide");
      if (!this._acctRef()) return;
      const p = ((this._d && this._d.profiles) || {})[pid] || {};
      this._pending = { pid };
      this._openConfirm({
        pid, fromName: "—", toName: p.name || pid,
        fromId: "", toId: pid,
        hasOutbound: false, scopes: ["account"],
      });
    }

    _openConfirm(opt) {
      const esc = (s) => this.esc(s);
      const slot = this.shadowRoot.querySelector('[data-role="cfm-slot"]');
      if (!slot) return;
      const scopes = opt.scopes || ["conv"];
      const convScope = scopes.indexOf("conv") >= 0;
      const acctScope = scopes.indexOf("account") >= 0;
      const acct = this._acctRef() || {};
      let body;
      if (!convScope && acctScope) {
        body = this.t("cp.persona.confirm_acct_body",
          { acct: acct.accountId || "?", to: opt.toName });
      } else {
        body = opt.pid
          ? this.t("cp.persona.confirm_switch", { from: opt.fromName, to: opt.toName })
          : this.t("cp.persona.confirm_clear", { to: opt.toName });
      }
      let warn = "";
      if (opt.hasOutbound) {
        warn += `<div class="cw">${esc(this.t("cp.persona.confirm_outbound", { from: opt.fromName }))}</div>`;
      }
      if (acctScope) {
        warn += `<div class="cw">${esc(this.t("cp.persona.confirm_acct_warn"))}</div>`;
      }
      this._voiceHints(opt.pid).forEach((h) => {
        warn += `<div class="cw">${esc(h)}</div>`;
      });
      const btns = [`<button data-act="cfm-cancel">${esc(this.t("cp.persona.confirm_cancel"))}</button>`];
      if (acctScope) {
        btns.push(`<button class="${convScope ? "" : "primary"}" data-act="cfm-acct">${esc(this.t("cp.persona.confirm_acct_btn"))}</button>`);
      }
      if (convScope) {
        btns.push(`<button class="primary" data-act="cfm-ok">${esc(this.t(acctScope ? "cp.persona.confirm_conv_btn" : "cp.persona.confirm_ok"))}</button>`);
      }
      const fromId = opt.fromId || "";
      const toId = opt.toId || opt.pid || "";
      const swap = (fromId || toId)
        ? `<div class="cfm-swap" aria-hidden="true">` +
          (fromId ? this._disc(fromId, opt.fromName) + `<span class="sn">${esc(opt.fromName || "")}</span>` : `<span class="sn">${esc(opt.fromName || "—")}</span>`) +
          `<span class="arrow">→</span>` +
          (toId ? this._disc(toId, opt.toName) + `<span class="sn">${esc(opt.toName || "")}</span>` : `<span class="sn">${esc(opt.toName || "—")}</span>`) +
          `</div>`
        : "";
      slot.innerHTML =
        `<div class="cfm-mask" data-act="cfm-cancel"></div>` +
        `<div class="cfm">` +
        `<div class="ct">${esc(this.t("cp.persona.confirm_title"))}</div>` +
        swap +
        `<div class="cb">${esc(body)}</div>` + warn +
        `<div class="cacts">${btns.join("")}</div></div>`;
    }

    _closeConfirm() {
      const slot = this.shadowRoot.querySelector('[data-role="cfm-slot"]');
      if (slot) slot.innerHTML = "";
    }

    async _doBindConv(pid) {
      const ctx = this._ctx || {};
      const cid = String(ctx.conversationId || "");
      if (!cid) return;
      this._lastAttempt = { kind: "conv", pid };   // 失败提示里「重试」按钮的重放依据
      this._busy = true;
      const wrap = this._wrap();
      if (wrap) wrap.classList.add("busy");
      let ok = false;
      let res = null;
      try {
        res = pid
          ? await this._client.bindConvPersona({ conversationId: cid, profileId: pid })
          : await this._client.unbindConvPersona({ conversationId: cid });
        ok = !!(res && res.ok);
      } catch (e) { ok = false; res = { ok: false, status: 0, error: String((e && e.message) || e) }; }
      this._busy = false;
      if (wrap) wrap.classList.remove("busy");
      // personaName:换绑目标显示名;解除覆写时=回落的账号人设名(宿主 toast 用)
      let pname = "";
      if (pid) {
        const p = (this._d && this._d.profiles || {})[pid] || {};
        pname = p.name || pid;
      } else {
        const eff = this._d && this._d.eff;
        pname = (eff && eff.account && eff.account.name) || "";
      }
      this.emit("cp-persona-changed", {
        scope: "conversation", personaId: pid, personaName: pname, ok,
        conversationId: cid, chatKey: ctx.chatKey || "",
        status: (res && res.status), code: (res && res.code) || "",
        error: ok ? "" : this._failText(res),
      });
      if (!ok) {
        this._showFail(res);
        this._reportFail("persona_bind_conv", res);
        return;
      }
      this.refresh();
    }

    /* —— 账号级整号切换(写 registry meta,影响该账号全部会话;会话覆写不受动) —— */
    async _doBindAccount(pid) {
      const ref = this._acctRef();
      if (!pid || !ref || !this._client.setAccountPersona) return;
      this._lastAttempt = { kind: "acct", pid };
      this._busy = true;
      const wrap = this._wrap();
      if (wrap) wrap.classList.add("busy");
      let ok = false;
      let res = null;
      try {
        res = await this._client.setAccountPersona({
          platform: ref.platform, accountId: ref.accountId, profileId: pid,
        });
        ok = !!(res && res.ok);
      } catch (e) { ok = false; res = { ok: false, status: 0, error: String((e && e.message) || e) }; }
      this._busy = false;
      if (wrap) wrap.classList.remove("busy");
      const p = ((this._d && this._d.profiles) || {})[pid] || {};
      this.emit("cp-persona-changed", {
        scope: "account", personaId: pid, personaName: p.name || pid, ok,
        platform: ref.platform, accountId: ref.accountId,
        conversationId: (this._ctx && this._ctx.conversationId) || "",
        chatKey: (this._ctx && this._ctx.chatKey) || "",
        status: (res && res.status), code: (res && res.code) || "",
        error: ok ? "" : this._failText(res),
      });
      if (!ok) {
        this._showFail(res);
        this._reportFail("persona_bind_acct", res);
        return;
      }
      this.refresh();
    }

    /* —— legacy select(开关关/RPA 2 段键会话,原语义不变) —— */
    async _onSelect(pid) {
      const chatKey = this._ctx && this._ctx.chatKey;
      if (!chatKey) return;
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      if (sel) sel.disabled = true;
      let ok = false;
      let res = null;
      try {
        if (pid) {
          const persona = (this._d && this._d.profiles || {})[pid];
          if (!persona) { if (sel) sel.disabled = false; return; }
          res = await this._client.bindPersona({ chatKey, persona });
          ok = !!(res && res.ok);
        } else {
          res = await this._client.unbindPersona({ chatKey });
          ok = !!(res && res.ok);
        }
      } catch (e) { ok = false; res = { ok: false, status: 0, error: String((e && e.message) || e) }; }
      if (!ok) this._reportFail("persona_bind_legacy", res);
      this.emit("cp-persona-changed", {
        personaId: pid, chatKey, ok,
        status: (res && res.status), code: (res && res.code) || "",
        error: ok ? "" : this._failText(res),
      });
      this.refresh();
    }
  }

  CpPersona.discColor = personaDiscColor;
  CpPersona.listRank = personaListRank;
  if (!customElements.get("cp-persona")) customElements.define("cp-persona", CpPersona);
})(typeof window !== "undefined" ? window : this);
