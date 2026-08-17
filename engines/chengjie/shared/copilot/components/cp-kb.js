"use strict";
/* 两端共享 · 知识库 / 快捷回复 cp-kb（P1-4 第二刀，2026-08-12）
   收编桌面原生 aside 的「知识库」「快捷回复」两卡为统一 App 的一张卡——原生面板
   退役的能力前置（web 原生右栏刻意不挂：composer 的 / 指令面板与 KB 自动推荐浮层
   已承担同职能，右栏再放一份=双入口）。
   - 装载即取快捷回复模板（GET /api/unified-inbox/templates，一次性）；
   - 搜索走 GET /api/unified-inbox/kb-search（回车或点「搜」）；
   - 命中条目 / 模板点「填入」→ emit('cp-fill')——App 壳已把该事件桥给宿主
     （网页填 reply-ta / 桌面壳注入 composer），零新宿主接线。
   client 需实现:replyTemplates / kbSearch（缺失→空态优雅降级）。 */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) return;

  class CpKb extends Base {
    styles() {
      return `
      .srch { display:flex; gap:6px; }
      .srch input { flex:1; min-width:0; font:inherit; font-size:var(--cp-fs-sm,12px);
                    border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
                    background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); padding:5px 8px; }
      .kb-res { margin-top:6px; }
      .hit { border:1px solid var(--cp-border,#eef2f7); border-radius:var(--cp-radius-sm,6px);
             padding:6px 8px; margin-bottom:5px; }
      .hit .ttl { font-size:var(--cp-fs-sm,12px); font-weight:600; margin-bottom:2px; }
      .hit .bd { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b);
                 line-height:1.45; word-break:break-word; }
      .hit .acts { margin-top:4px; }
      .tsec { margin-top:8px; padding-top:8px; border-top:1px dashed var(--cp-border,#e2e8f0); }
      .tsec-ttl { font-size:var(--cp-fs-tiny,11px); font-weight:600; color:var(--cp-text-dim,#64748b); margin-bottom:5px; }
      .hint { margin-top:6px; font-size:10px; color:var(--cp-text-tiny,#94a3b8); }
      .stat { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); padding:4px 0; }`;
    }

    async fetchData(_ctx) {
      // 模板一次性拉取（会话无关）；kb 搜索由用户触发，不在装载路径上
      if (!this._client || !this._client.replyTemplates) return { ok: true, templates: [] };
      try {
        const d = await this._client.replyTemplates();
        return { ok: true, templates: (d && d.templates) || [] };
      } catch (_e) { return { ok: true, templates: [] }; }
    }

    _tplBody(t) {
      return typeof t === "string" ? t : (t.text || t.content || t.body || t.template || "");
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      this._tpls = (d.templates || []).filter((t) => this._tplBody(t)).slice(0, 12);
      const tplRows = this._tpls.map((t, i) => {
        const title = (typeof t === "string" ? "" : (t.title || t.name || t.label)) || "";
        const body = this._tplBody(t);
        return `<div class="hit">` +
          (title ? `<div class="ttl">${esc(title)}</div>` : "") +
          `<div class="bd">${esc(body.slice(0, 120))}${body.length > 120 ? "…" : ""}</div>` +
          `<div class="acts"><button data-act="fill-tpl" data-idx="${i}">${esc(this.t("cp.kb.fill"))}</button></div>` +
          `</div>`;
      }).join("");
      return `<div class="srch">` +
        `<input class="kb-q" type="text" placeholder="${esc(this.t("cp.kb.search_ph"))}" />` +
        `<button data-act="kb-go">${esc(this.t("cp.kb.search_btn"))}</button></div>` +
        `<div class="kb-res"></div>` +
        `<div class="tsec"><div class="tsec-ttl">${esc(this.t("cp.kb.tpl_title"))}</div>` +
        (tplRows || `<div class="stat">${esc(this.t("cp.kb.tpl_empty"))}</div>`) + `</div>` +
        `<div class="hint">${esc(this.t("cp.kb.hint"))}</div>`;
    }

    _render(html) {
      super._render(html);
      const q = this.shadowRoot.querySelector(".kb-q");
      if (q) q.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); this._search(); }
      });
    }

    async _search() {
      const ctx = this._ctx || {};
      const qEl = this.shadowRoot.querySelector(".kb-q");
      const res = this.shadowRoot.querySelector(".kb-res");
      if (!qEl || !res || !this._client || !this._client.kbSearch) return;
      const q = String(qEl.value || "").trim();
      if (!q) { res.innerHTML = ""; return; }
      res.innerHTML = `<div class="stat">${this.esc(this.t("cp.kb.searching"))}</div>`;
      let d;
      try {
        d = await this._client.kbSearch({ q: q, platform: ctx.platform || "" });
      } catch (_e) {
        res.innerHTML = `<div class="stat">${this.esc(this.t("cp.kb.err"))}</div>`;
        return;
      }
      this._hits = ((d && d.entries) || []).filter((en) => en && en.answer);
      if (!this._hits.length) {
        res.innerHTML = `<div class="stat">${this.esc(this.t("cp.kb.no_hit"))}</div>`;
        return;
      }
      res.innerHTML = this._hits.map((en, i) => {
        const ans = String(en.answer || "");
        return `<div class="hit">` +
          `<div class="ttl">${this.esc(en.title || en.category || "")}</div>` +
          `<div class="bd">${this.esc(ans.slice(0, 160))}${ans.length > 160 ? "…" : ""}</div>` +
          `<div class="acts"><button data-act="fill-hit" data-idx="${i}">${this.esc(this.t("cp.kb.fill"))}</button></div>` +
          `</div>`;
      }).join("");
    }

    onAction(act, el) {
      if (act === "kb-go") { this._search(); return; }
      const i = parseInt(el.getAttribute("data-idx") || "-1", 10);
      let text = "";
      if (act === "fill-hit" && this._hits && this._hits[i]) text = String(this._hits[i].answer || "");
      if (act === "fill-tpl" && this._tpls && this._tpls[i] != null) text = this._tplBody(this._tpls[i]);
      if (text) this.emit("cp-fill", { text: text });
    }
  }

  if (!customElements.get("cp-kb")) customElements.define("cp-kb", CpKb);
})(typeof window !== "undefined" ? window : this);
