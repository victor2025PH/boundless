"use strict";
/* 两端共享 · 会话运维 cp-conv-ops（P1-4，2026-08-12；P0 assist-only 2026-08-13；
   P0 搁置可见化 2026-08-14——修坐席实测「点搁置似乎没任何改变，然后又跳回原来状态」）：
     ① 搁置状态**持久渲染**：🌙 已搁置至 HH:MM + [取消搁置]，替代旧 2.5s 一闪而过的
        「已搁置 ✓」（_flash 已删）。状态读回优先 automation 响应搭便车字段 snooze_until
        （服务端随下个重启窗生效）；旧后端缺该字段自动回落既有 GET /api/workspace/snoozed
        权威清单——热更期零断档（feat 特性探测，与 reply-settings 守卫卡同模式）。
        操作成功用 POST 响应的 snooze_until 就地重渲，零额外请求零竞态。
     ② 语义微文案：搁置＝暂时移出「待接管/超时提醒」，不影响 AI 自动回复——档位行照旧
        显示「全自动运行中」不是 bug，是刻意的正交性可视化（搁置管的是告警队列可见性）。
     ③ 静默死路收口：client 缺方法 → 按钮禁用带 tooltip（旧实现 return 装死，坐席以为坏了）；
        失败在 .op-err 行显示服务端 i18n 后的具体原因（6s 自清），不再一律「操作失败」。
     ④ 埋点 cpops_*（sendBeacon → /api/telemetry/ui-event，与 cp-goal 同模式）：
        pause / snooze60 / snooze240 / unsnooze / archive / open_inbox + fail_* 分桶。
   原有功能不变：档位状态 + 全自动今日读数 +「暂停全自动」（只降不升）+ 归档两步确认；
   Path2 assistOnly 诚实卡；成功后 emit('cp-convops-changed',{kind}) 宿主联动。
   依赖 client：getAutomation / setAutomationMode / automationStats / archiveConversation /
   snoozeConversation / unsnoozeConversation / listSnoozed（缺方法 → 对应按钮禁用降级）。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  /* 「永久搁置」哨兵（2100-01-01 UTC）＝后端 store.SNOOZE_FOREVER_TS 的前端镜像
     （与 unified_inbox.html 同值同判据）。永久≠静音：客户来消息仍立即重浮。 */
  const SNOOZE_FOREVER_TS = 4102444800;
  function _isForever(ts) { return !!ts && ts >= SNOOZE_FOREVER_TS - 1; }

  function _beacon(action) {
    try {
      const body = JSON.stringify({
        page: (root.location && root.location.pathname) || "/copilot/app",
        action: String(action || "").slice(0, 64),
      });
      if (typeof root.navigator !== "undefined" && root.navigator.sendBeacon) {
        root.navigator.sendBeacon(
          "/api/telemetry/ui-event",
          new Blob([body], { type: "application/json" }));
      }
    } catch (_e) { /* 埋点永不阻断 */ }
  }

  class CpConvOps extends Base {
    styles() {
      return `
      .mode-row { display:flex; align-items:center; gap:7px; margin-bottom:6px; }
      .mode-dot { width:8px; height:8px; border-radius:50%; background:var(--cp-text-tiny,#94a3b8); flex-shrink:0; }
      .mode-dot.auto { background:var(--cp-ok,#16a34a); }
      .mode-dot.review { background:var(--cp-accent,#4f46e5); }
      .mode-dot.warn { background:var(--cp-warn,#d97706); }
      .mode-txt { font-size:var(--cp-fs-sm,12px); font-weight:600; }
      .stats { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:6px; }
      .snooze-row { font-size:var(--cp-fs-sm,12px); color:var(--cp-warn,#d97706); font-weight:600;
                    line-height:1.5; margin-bottom:6px; }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin-top:7px; line-height:1.5; }
      .snz-hint { margin-top:5px; color:var(--cp-text-dim,#64748b); }
      .op-err { font-size:var(--cp-fs-tiny,11px); color:var(--cp-danger,#dc2626); margin-top:5px; line-height:1.5; }
      .assist { font-size:var(--cp-fs-tiny,11px); color:var(--cp-warn,#d97706); line-height:1.55; margin:0 0 8px; }
      .ok-flash { color:var(--cp-ok,#16a34a); }`;
    }

    emptyText() { return this.t("cp.base.empty"); }

    _assistOnly(ctx) {
      const caps = (ctx && ctx.caps) || (this._ctx && this._ctx.caps) || {};
      return caps.assistOnly === true;
    }

    async fetchData(ctx) {
      // Path2 官方网页（assistOnly）：本页不接 ingest/投递 → 不读「运行中」档位装绿，
      // 直接诚实空态，避免「全自动运行中 + 今日 0」双谎。
      if (this._assistOnly(ctx)) return { ok: true, assistOnly: true };
      if (!this._client || !this._client.getAutomation) return { ok: false };
      const q = { platform: ctx.platform, accountId: ctx.accountId, chatKey: ctx.chatKey };
      const d = await this._client.getAutomation(q);
      if (!d || d.ok === false) return d;
      const out = { ok: true, mode: d.mode || "review" };
      // 搁置状态读回：优先搭便车字段（同一请求零开销）；旧后端缺字段 → 回落权威清单。
      // 读回失败按「未搁置」渲染（操作后仍会用 POST 响应就地更新，不至于误导）。
      if (typeof d.snooze_until === "number") {
        out.snoozeUntil = Number(d.snooze_until) || 0;
      } else if (this._client.listSnoozed && ctx.conversationId) {
        try {
          const sn = await this._client.listSnoozed();
          if (sn && sn.ok !== false && Array.isArray(sn.items)) {
            const hit = sn.items.find(
              (it) => String(it.conversation_id) === String(ctx.conversationId));
            out.snoozeUntil = hit ? Number(hit.snooze_until || 0) : 0;
          }
        } catch (_e) { /* 软失败：按未搁置渲染 */ }
      }
      if (out.mode === "auto_ai" && this._client.automationStats) {
        try {
          const s = await this._client.automationStats(q);
          if (s && s.ok !== false) out.stats = s.stats || null;
        } catch (_e) { /* 读数是增强，失败不阻塞档位显示 */ }
      }
      return out;
    }

    /* 绝对时刻的人话表述：今天 → HH:MM；更远 → M/D HH:MM（跟随本地时区）。 */
    _fmtUntil(tsSec) {
      const d = new Date(tsSec * 1000);
      const hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
      if (d.toDateString() === new Date().toDateString()) return hm;
      return (d.getMonth() + 1) + "/" + d.getDate() + " " + hm;
    }

    _snoozedNow(d) {
      const sn = Number((d && d.snoozeUntil) || 0);
      return sn > 0 && sn * 1000 > Date.now() ? sn : 0;
    }

    renderData(d) {
      if (d && d.assistOnly) {
        return `<div class="mode-row"><span class="mode-dot warn"></span>` +
          `<span class="mode-txt">${this.esc(this.t("cp.convops.assist_title"))}</span></div>` +
          `<div class="assist">${this.esc(this.t("cp.convops.assist_body"))}</div>` +
          `<div class="acts"><button data-act="open-inbox">${this.esc(this.t("cp.convops.open_inbox"))}</button></div>` +
          `<div class="hint">${this.esc(this.t("cp.convops.assist_hint"))}</div>`;
      }
      const mode = String(d.mode || "review");
      const known = { auto_ai: 1, review: 1, multi_choice: 1, manual: 1 };
      const label = known[mode] ? this.t("cp.convops.mode_" + mode) : mode;
      const dotCls = mode === "auto_ai" ? "auto" : (mode === "review" ? "review" : "");
      let html = `<div class="mode-row"><span class="mode-dot ${dotCls}"></span>` +
        `<span class="mode-txt">${this.esc(label)}</span></div>`;
      if (mode === "auto_ai" && d.stats) {
        let st = this.t("cp.convops.stats", {
          sent: d.stats.autosend || 0, blocked: d.stats.blocked || 0 });
        if ((d.stats.failed || 0) > 0) st += this.t("cp.convops.stats_fail", { n: d.stats.failed });
        html += `<div class="stats">${this.esc(st)}</div>`;
      }
      // —— 持久搁置状态行（P0 2026-08-14）：搁置中一直可见，直到取消/到点/客户来消息 ——
      const snUntil = this._snoozedNow(d);
      if (snUntil) {
        const txt = _isForever(snUntil)
          ? this.t("cp.convops.snoozed_forever")
          : this.t("cp.convops.snoozed_until", { t: this._fmtUntil(snUntil) });
        html += `<div class="snooze-row">🌙 ${this.esc(txt)}</div>`;
      }
      // 死路收口：缺 client 方法 / 缺会话 id → 禁用 + tooltip（不再静默 return 装死）
      const cid = String((this._ctx && this._ctx.conversationId) || "");
      const can = (m) => !!(this._client && typeof this._client[m] === "function");
      const dis = (ok) => ok ? "" : ` disabled title="${this.esc(this.t("cp.convops.na_t"))}"`;
      const btns = [];
      if (mode === "auto_ai") {
        btns.push(`<button data-act="pause"${dis(can("setAutomationMode"))} ` +
          `title="${this.esc(this.t("cp.convops.pause_t"))}">` +
          `${this.esc(this.t("cp.convops.pause"))}</button>`);
      }
      if (snUntil) {
        btns.push(`<button data-act="unsnooze"${dis(can("unsnoozeConversation") && !!cid)}>` +
          `${this.esc(this.t("cp.convops.unsnooze"))}</button>`);
      } else {
        const okSn = can("snoozeConversation") && !!cid;
        btns.push(`<button data-act="snooze60"${dis(okSn)}>${this.esc(this.t("cp.convops.snooze1h"))}</button>`);
        btns.push(`<button data-act="snooze240"${dis(okSn)}>${this.esc(this.t("cp.convops.snooze4h"))}</button>`);
      }
      btns.push(`<button data-act="archive"${dis(can("archiveConversation") && !!cid)}>` +
        `${this.esc(this.t("cp.convops.archive"))}</button>`);
      html += `<div class="acts">${btns.join("")}</div>`;
      // 语义微文案：只在「可搁置」形态下显示（搁置中状态行本身已解释语义）
      if (!snUntil) {
        html += `<div class="hint snz-hint">${this.esc(this.t("cp.convops.snooze_hint"))}</div>`;
      }
      html += `<div class="hint">${this.esc(this.t("cp.convops.hint_full"))}</div>`;
      return html;
    }

    /* 归档两步确认（与 cp-accounts._armConfirm 同模式）：首点变红色确认态，4s 自动还原 */
    _arm(el) {
      el.setAttribute("data-armed", "1");
      el.dataset.origText = el.textContent;
      el.dataset.origCls = el.className;
      el.textContent = this.t("cp.convops.confirm_archive");
      el.classList.add("danger");
      setTimeout(() => {
        if (!el.isConnected || el.getAttribute("data-armed") !== "1") return;
        el.removeAttribute("data-armed");
        el.textContent = el.dataset.origText || "";
        el.className = el.dataset.origCls || "";
      }, 4000);
    }

    /* 失败反馈：按钮短暂置灰还原 + .op-err 行显示服务端具体原因（6s 自清）。
       client 已把非 2xx 归一化为 {ok:false, error:<后端 i18n 后的 detail>}，
       这里直接透传——「为什么失败」坐席可读，不再一律「操作失败」。 */
    _fail(el, msg) {
      const t = el.textContent;
      el.textContent = this.t("cp.convops.fail");
      el.disabled = true;
      setTimeout(() => {
        if (!el.isConnected) return;
        el.disabled = false; el.textContent = t;
      }, 2200);
      this._showErr(msg);
    }

    _showErr(msg) {
      try {
        const w = this._wrap();
        if (!w) return;
        let el = w.querySelector(".op-err");
        if (!el) {
          el = document.createElement("div");
          el.className = "op-err";
          w.appendChild(el);
        }
        el.textContent = String(msg || this.t("cp.convops.fail"));
        clearTimeout(this._errTimer);
        this._errTimer = setTimeout(() => { if (el.isConnected) el.remove(); }, 6000);
      } catch (_e) { /* 错误提示自身绝不抛 */ }
    }

    /* 操作成功后的就地重渲：用响应值更新本地快照，不重发请求（零竞态零延迟）。 */
    _patchAndRender(patch) {
      if (!this._d) return;
      Object.assign(this._d, patch || {});
      this._render(this.renderData(this._d));
    }

    async onAction(act, el) {
      const ctx = this._ctx;
      if (!ctx) return;
      if (act === "open-inbox") {
        _beacon("cpops_open_inbox");
        // 桌面壳 iframe → postMessage；网页宿主可另接 cp-open-inbox 事件
        this.emit("cp-open-inbox", { platform: ctx.platform || "", accountId: ctx.accountId || "" });
        try {
          if (window.parent && window.parent !== window) {
            window.parent.postMessage({ type: "cp-open-inbox", platform: ctx.platform || "" }, "*");
          }
        } catch (_e) { /* ignore */ }
        return;
      }
      if (!this._client) return;
      if (act === "pause") {
        if (!this._client.setAutomationMode) return;
        el.disabled = true;
        try {
          // 降档方向固定 review（网页版有 localStorage 记忆上次人工档；App 无此状态，
          // review 是最保守且与网页 pause 兜底一致的落点）
          const r = await this._client.setAutomationMode({
            platform: ctx.platform, accountId: ctx.accountId, chatKey: ctx.chatKey, mode: "review" });
          if (!r || r.ok === false) {
            _beacon("cpops_fail_pause");
            this._fail(el, r && r.error);
            return;
          }
          _beacon("cpops_pause");
          this.emit("cp-convops-changed", { kind: "mode", mode: "review" });
          this.refresh();   // 重渲：档位行变人审、暂停按钮消失
        } catch (_e) { _beacon("cpops_fail_pause"); this._fail(el); }
        return;
      }
      if (act === "snooze60" || act === "snooze240") {
        if (!this._client.snoozeConversation || !ctx.conversationId) return;
        el.disabled = true;
        try {
          const r = await this._client.snoozeConversation({
            conversationId: ctx.conversationId, minutes: act === "snooze60" ? 60 : 240 });
          if (!r || r.ok === false) {
            _beacon("cpops_fail_snooze");
            this._fail(el, r && r.error);
            return;
          }
          _beacon("cpops_" + act);
          this.emit("cp-convops-changed", { kind: "snooze" });
          // 持久状态行：POST 响应自带 snooze_until，就地重渲（不再一闪而过）
          this._patchAndRender({ snoozeUntil: Number(r.snooze_until || 0) });
        } catch (_e) { _beacon("cpops_fail_snooze"); this._fail(el); }
        return;
      }
      if (act === "unsnooze") {
        if (!this._client.unsnoozeConversation || !ctx.conversationId) return;
        el.disabled = true;
        try {
          const r = await this._client.unsnoozeConversation({ conversationId: ctx.conversationId });
          if (!r || r.ok === false) {
            _beacon("cpops_fail_unsnooze");
            this._fail(el, r && r.error);
            return;
          }
          _beacon("cpops_unsnooze");
          this.emit("cp-convops-changed", { kind: "snooze" });
          this._patchAndRender({ snoozeUntil: 0 });
        } catch (_e) { _beacon("cpops_fail_unsnooze"); this._fail(el); }
        return;
      }
      if (act === "archive") {
        if (!this._client.archiveConversation || !ctx.conversationId) return;
        if (el.getAttribute("data-armed") !== "1") { this._arm(el); return; }
        el.disabled = true;
        try {
          const r = await this._client.archiveConversation({
            conversationId: ctx.conversationId, archived: true });
          if (!r || r.ok === false) {
            _beacon("cpops_fail_archive");
            el.disabled = false;
            this._fail(el, r && r.error);
            return;
          }
          _beacon("cpops_archive");
          this.emit("cp-convops-changed", { kind: "archive" });
          this._render(`<div class="empty ok-flash">${this.esc(this.t("cp.convops.archived"))}</div>`);
        } catch (_e) {
          _beacon("cpops_fail_archive");
          el.disabled = false;
          this._fail(el);
        }
      }
    }
  }

  if (!customElements.get("cp-conv-ops")) customElements.define("cp-conv-ops", CpConvOps);
})(typeof window !== "undefined" ? window : this);
