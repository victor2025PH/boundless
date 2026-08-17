/*
 * cp-tg-members —— 副驾「工具箱」：Telegram 群成员提取（坐席在群会话里顺手拉当前群）。
 *
 * 定位：轻量入口。只做「在当前 Telegram 群会话里一键拉『发过言且非管理员』的成员入库」+
 * 今日额度水位 + 导出。多号协同 / 批量 / 选过滤器 / 成员表格属独立管理台（后续）。
 *
 * 自管型 Web Component（不继承 CpPanelBase，风格对齐 cp-voice/cp-accounts），
 * Shadow DOM 隔离样式；i18n 走内联 zh/en（P0，挂载到宿主后可迁 cp-i18n.js）。
 * 后端契约：/api/tg-members/{quota,jobs,members,members/export}（见 group_members_routes.py）。
 *
 * ⚠ 发布：本文件在 shared/copilot 与 desktop/renderer/shared/copilot 两树必须字节一致
 * （test_copilot_shared_sync）；挂载到 unified_inbox.html 时记得加 <script ?v=> 并 bump ui-build。
 */
(function (root) {
  "use strict";
  var W = root;

  function t(zh, en) {
    try {
      var lang = (document.documentElement.lang || "").toLowerCase();
      return lang.indexOf("en") === 0 ? en : zh;
    } catch (e) {
      return zh;
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  async function api(url, opts) {
    var res = await fetch(
      url,
      Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {})
    );
    var data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }
    return { ok: res.ok, status: res.status, data: data };
  }

  var CSS =
    ":host{display:block;font-size:12.5px;color:var(--cp-text,#1f2937)}" +
    ".wrap{display:flex;flex-direction:column;gap:10px}" +
    ".empty{padding:10px;color:var(--cp-text-dim,#6b7280);line-height:1.5}" +
    ".quota .qhd{display:flex;justify-content:space-between;font-size:11.5px;color:var(--cp-text-dim,#6b7280);margin-bottom:4px}" +
    ".bar{height:8px;border-radius:6px;background:var(--cp-border,#e5e7eb);overflow:hidden}" +
    ".bar i{display:block;height:100%;border-radius:6px;transition:width .3s ease}" +
    ".bar i.ok{background:var(--cp-ok,#0f9d75)}" +
    ".bar i.warn{background:var(--cp-warn,#d97706)}" +
    ".bar i.danger{background:var(--cp-danger,#dc2626)}" +
    ".counts{font-size:12px;color:var(--cp-text,#374151)}" +
    ".counts b{color:var(--cp-accent,#1e8cf2)}" +
    ".acts{display:flex;gap:8px;flex-wrap:wrap}" +
    "button{font:inherit;cursor:pointer;border:1px solid var(--cp-border,#d1d5db);background:#fff;color:var(--cp-text,#374151);border-radius:8px;padding:6px 10px}" +
    "button:hover{border-color:var(--cp-accent,#1e8cf2)}" +
    "button.primary{background:var(--cp-accent,#1e8cf2);border-color:var(--cp-accent,#1e8cf2);color:#fff}" +
    "button[disabled]{opacity:.5;cursor:not-allowed}" +
    ".status{font-size:12px;color:var(--cp-text,#374151);background:var(--cp-bg-soft,#f3f4f6);border-radius:8px;padding:6px 8px}" +
    ".hint{font-size:11px;color:var(--cp-text-dim,#9ca3af);line-height:1.5}";

  class CpTgMembers extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._ctx = null;
      this._client = null;
      this._acct = "";
      this._group = "";
      this._supported = false;
      this._quota = null;
      this._counts = null;
      this._status = "";
      this._jobId = "";
      this._poll = null;
      this._busy = false;
    }

    set client(c) {
      this._client = c;
    }

    set context(ctx) {
      this._ctx = ctx || null;
      this._parseCtx();
      this.refresh();
    }

    connectedCallback() {
      this._render();
    }

    disconnectedCallback() {
      this._stopPoll();
    }

    _parseCtx() {
      var ctx = this._ctx || {};
      var platform = String(ctx.platform || "").toLowerCase();
      var acct = ctx.accountId || ctx.account_id || "";
      var chatKey = ctx.chatKey || ctx.chat_key || "";
      // 回落：从 conversationId 解析 platform:account:chat_key
      if ((!platform || !acct || !chatKey) && ctx.conversationId) {
        var parts = String(ctx.conversationId).split(":");
        if (parts.length >= 3) {
          platform = platform || String(parts[0]).toLowerCase();
          acct = acct || parts[1];
          chatKey = chatKey || parts.slice(2).join(":");
        }
      }
      this._acct = String(acct || "");
      this._group = String(chatKey || "");
      // 群/超级群：telegram 且 chat_key 为负号开头（TG 群 id 为负）
      this._supported = platform === "telegram" && /^-/.test(this._group);
    }

    async refresh() {
      if (!this._supported) {
        this._render();
        return;
      }
      try {
        var q = await api("/api/tg-members/quota?account_id=" + encodeURIComponent(this._acct));
        this._quota = q.ok ? q.data : null;
        var m = await api(
          "/api/tg-members/members?group_id=" + encodeURIComponent(this._group) + "&limit=1"
        );
        this._counts = m.ok ? m.data : null;
      } catch (e) {
        /* best-effort，取不到就渲染已有 */
      }
      this._render();
    }

    async _pull() {
      if (this._busy) return;
      this._busy = true;
      this._status = t("正在拉取…", "Extracting…");
      this._render();
      var r = await api("/api/tg-members/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: this._acct, group: this._group, filter: "spoke_no_admin" }),
      });
      if (!r.ok) {
        this._busy = false;
        this._status = t("启动失败：", "Failed: ") + esc((r.data && r.data.detail) || r.status);
        this._render();
        return;
      }
      this._jobId = r.data.job_id || "";
      this._startPoll();
    }

    _startPoll() {
      this._stopPoll();
      var self = this;
      var tries = 0;
      this._poll = setInterval(async function () {
        tries++;
        if (!self._jobId || tries > 150) {
          self._stopPoll();
          self._busy = false;
          return;
        }
        var r = await api("/api/tg-members/jobs/" + encodeURIComponent(self._jobId));
        var job = r.ok ? r.data.job || null : null;
        if (!job) return;
        self._status =
          t("已入库 ", "Pulled ") +
          (job.pulled_total || 0) +
          t(" 人 · 去重 ", " · dedup ") +
          (job.dedup_skipped || 0);
        var st = job.status;
        if (st === "done" || st === "stopped" || st === "error") {
          self._stopPoll();
          self._busy = false;
          if (st === "error") self._status = t("提取出错：", "Error: ") + esc(job.last_error || "");
          self.refresh();
        }
      }, 2000);
    }

    _stopPoll() {
      if (this._poll) {
        clearInterval(this._poll);
        this._poll = null;
      }
    }

    _onClick(e) {
      var el = e.target;
      var act = el && el.getAttribute ? el.getAttribute("data-act") : "";
      if (!act) return;
      if (act === "pull") this._pull();
      else if (act === "export")
        W.open(
          "/api/tg-members/members/export?group_id=" +
            encodeURIComponent(this._group) +
            "&only=spoke_no_admin",
          "_blank"
        );
    }

    _render() {
      var rootEl = this.shadowRoot;
      if (!this._supported) {
        rootEl.innerHTML =
          "<style>" +
          CSS +
          "</style><div class='empty'>" +
          t(
            "在 Telegram 群会话里可一键拉取「发过言的非管理员」成员；批量 / 多号请去管理台。",
            "Open a Telegram group chat to pull active non-admin members; use the console for batch / multi-account."
          ) +
          "</div>";
        return;
      }
      var q = this._quota || { cap: 0, used_today: 0, remaining: 0 };
      var pct = q.cap > 0 ? Math.min(100, Math.round(((q.used_today || 0) / q.cap) * 100)) : 0;
      var tone = pct >= 90 ? "danger" : pct >= 75 ? "warn" : "ok";
      var c = this._counts || {};
      var noBudget = (q.remaining || 0) <= 0;
      var html = "<style>" + CSS + "</style><div class='wrap'>";
      html +=
        "<div class='quota'><div class='qhd'><span>" +
        t("今日额度", "Today's quota") +
        "</span><span>" +
        (q.used_today || 0) +
        " / " +
        (q.cap || 0) +
        "</span></div><div class='bar'><i class='" +
        tone +
        "' style='width:" +
        pct +
        "%'></i></div></div>";
      html +=
        "<div class='counts'>" +
        t("库里本群：", "In this group: ") +
        "<b>" +
        (c.total || 0) +
        "</b> " +
        t("人 · 可聊 ", "· spoke ") +
        "<b>" +
        (c.spoke_no_admin || 0) +
        "</b></div>";
      html += "<div class='acts'>";
      html +=
        "<button class='primary' data-act='pull' " +
        (this._busy || noBudget ? "disabled" : "") +
        ">" +
        (noBudget ? t("今日额度已用完", "Quota used up") : t("🧲 拉取本群可聊成员", "🧲 Pull active members")) +
        "</button>";
      html += "<button data-act='export'>" + t("导出 CSV", "Export CSV") + "</button>";
      html += "</div>";
      if (this._status) html += "<div class='status'>" + this._status + "</div>";
      html +=
        "<div class='hint'>" +
        t(
          "只拉「发过言且非管理员」的人；限速 + 去重，每天到额度自动停。",
          "Active non-admins only; rate-limited + de-duped; auto-stops at daily quota."
        ) +
        "</div>";
      html += "</div>";
      rootEl.innerHTML = html;
      var self = this;
      rootEl.querySelectorAll("button[data-act]").forEach(function (b) {
        b.addEventListener("click", function (e) {
          self._onClick(e);
        });
      });
    }
  }

  if (!customElements.get("cp-tg-members")) customElements.define("cp-tg-members", CpTgMembers);
})(typeof window !== "undefined" ? window : this);
