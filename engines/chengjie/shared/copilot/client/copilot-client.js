"use strict";
/* 两端共享 · 数据适配层。组件只依赖统一接口,不关心传输:
   - 桌面:window.shell.*  → IPC → main.js → 后端(规避 CSP/CORS)
   - 网页:fetch('/api/...') 同源带 session
   经典脚本,挂 window.CopilotShared,不引外链(满足 CSP 'self')。 */
(function (root) {
  // 复刻后端 conv_id 公式:src/inbox/normalizer.py::conv_id
  function conversationId(platform, accountId, chatKey) {
    return `${platform}:${accountId}:${chatKey}`;
  }

  // 可选鉴权 token:浏览器靠同源 cookie,桌面 iframe 由壳注入 token(同一 WebClient 跨端)。
  let _authToken = "";
  function setAuthToken(t) { _authToken = String(t || ""); }
  function _authHeaders(base) {
    const h = Object.assign({}, base || {});
    if (_authToken) h["Authorization"] = `Bearer ${_authToken}`;
    return h;
  }

  /* —— CSRF 自带通行证（2026-07-31，修「切换失败，请重试」403 事故）——
     全站写请求须过 CSRF 中间件（X-CSRF-Token / Bearer / 同源 Origin·Referer 三选一）。
     此前该头只由 workspace_base 的页面级 fetch 补丁注入——共享组件在 iframe App /
     其他宿主里一张证都不带，实测 Chromium 同源 POST 不发 Origin，整条写通道只挂在
     Referer 一根线上（隐私扩展/企业策略/反代改写 Host 即全灭）。传输层自带凭证 =
     显式契约：任何宿主引入本客户端即获得完整写能力，不再依赖宿主补丁。 */
  function _readCsrfCookie() {
    if (typeof document === "undefined") return "";
    const m = String(document.cookie || "").match(/(?:^|;\s*)csrf_token=([^;]*)/);
    return m ? m[1] : "";
  }
  let _csrfSeeding = null;
  function _seedCsrfCookie() {
    // cookie 缺失（浏览器重启后会话级 cookie 消失等）→ 打一发任意安全方法请求，
    // 中间件会在响应上补种 csrf_token。共享在途 Promise 防并发写时重复播种。
    if (typeof document === "undefined") return Promise.resolve(false);
    if (!_csrfSeeding) {
      _csrfSeeding = fetch("/manifest.webmanifest", {
        method: "HEAD", cache: "no-store", credentials: "same-origin",
      }).catch(() => null).then(() => { _csrfSeeding = null; return !!_readCsrfCookie(); });
    }
    return _csrfSeeding;
  }
  function _writeHeaders(base) {
    const h = _authHeaders(base);
    const tok = _readCsrfCookie();
    if (tok) h["X-CSRF-Token"] = tok;
    return h;
  }

  // —— 网页适配器:同源 fetch ——
  class WebCopilotClient {
    /* 读请求：非 2xx 归一化为 {ok:false, status, code, error}（与 _post 同口径）。
       此前 4xx/5xx 只回裸 {detail} → 组件拿不到 status，「模块关闭(403) → 整卡隐藏」
       与「普通错误 → 可重试」无法区分。2xx 仍原样透传后端 JSON（零行为变化）。 */
    async _get(url) {
      let r;
      try {
        r = await fetch(url, { headers: _authHeaders() });
      } catch (e) {
        return { ok: false, status: 0, code: "network",
                 error: String((e && e.message) || e || "network error") };
      }
      let d = null;
      try { d = await r.json(); } catch (_e) { d = null; }
      if (r.ok) {
        if (d === null) return { ok: false, status: r.status, code: "badjson", error: "invalid JSON response" };
        return d;
      }
      const out = (d && typeof d === "object") ? d : {};
      if (out.ok === undefined) out.ok = false;
      if (out.status === undefined) out.status = r.status;
      if (!out.error) out.error = String(out.detail || r.statusText || ("HTTP " + r.status));
      return out;
    }
    /* 写请求统一出口：自带 CSRF 头；非 2xx 归一化为 {ok:false, status, code, error}
       （error 取后端已 i18n 的 detail，组件据此分型提示，不再一律「请重试」）。
       403+code=csrf 且未带 Bearer → 补种 cookie 后重试一次（中间件拒绝发生在业务
       逻辑之前，服务端零副作用，安全可重放）；网络层异常不再向上抛，统一 status:0。 */
    async _post(url, body, _retried) {
      return this._writeJson("POST", url, body, _retried);
    }
    /* PUT 通道（P1 2026-08-18：cp-kb 团队话术编辑走 admin 既有 PUT /api/templates/{key}）
       ——与 _post 完全同一套 CSRF/归一化/重试语义，仅 method 不同。 */
    async _put(url, body, _retried) {
      return this._writeJson("PUT", url, body, _retried);
    }
    async _writeJson(method, url, body, _retried) {
      if (!_authToken && !_readCsrfCookie()) await _seedCsrfCookie();
      let r;
      try {
        r = await fetch(url, {
          method: method,
          headers: _writeHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(body || {}),
        });
      } catch (e) {
        return { ok: false, status: 0, code: "network",
                 error: String((e && e.message) || e || "network error") };
      }
      let d = null;
      try { d = await r.json(); } catch (_e) { d = null; }
      if (r.ok) {
        if (d === null) return { ok: false, status: r.status, code: "badjson", error: "invalid JSON response" };
        return d;
      }
      if (r.status === 403 && d && d.code === "csrf" && !_retried && !_authToken) {
        const seeded = await _seedCsrfCookie();
        if (seeded) return this._writeJson(method, url, body, true);
      }
      const out = (d && typeof d === "object") ? d : {};
      if (out.ok === undefined) out.ok = false;
      if (out.status === undefined) out.status = r.status;
      if (!out.error) out.error = String(out.detail || r.statusText || ("HTTP " + r.status));
      return out;
    }
    /* multipart 写通道（P1 导入上传用）：与 _post 同 CSRF 语义；
       不设 Content-Type（浏览器自带 boundary）。 */
    async _postForm(url, formData, _retried) {
      if (!_authToken && !_readCsrfCookie()) await _seedCsrfCookie();
      let r;
      try {
        r = await fetch(url, { method: "POST", headers: _writeHeaders({}), body: formData });
      } catch (e) {
        return { ok: false, status: 0, code: "network",
                 error: String((e && e.message) || e || "network error") };
      }
      let d = null;
      try { d = await r.json(); } catch (_e) { d = null; }
      if (r.ok) {
        if (d === null) return { ok: false, status: r.status, code: "badjson", error: "invalid JSON response" };
        return d;
      }
      if (r.status === 403 && d && d.code === "csrf" && !_retried && !_authToken) {
        const seeded = await _seedCsrfCookie();
        if (seeded) return this._postForm(url, formData, true);
      }
      const out = (d && typeof d === "object") ? d : {};
      if (out.ok === undefined) out.ok = false;
      if (out.status === undefined) out.status = r.status;
      if (!out.error) out.error = String(out.detail || r.statusText || ("HTTP " + r.status));
      return out;
    }
    /* 跨平台档案（cp-origin，2026-08-18）：ctx 缺显式三元组时从 conversationId
       （platform:account:chat_key，chat_key 可含冒号）拆解兜底。 */
    _originParams({ conversationId, platform, accountId, chatKey }) {
      if (platform && chatKey) {
        return { platform: platform, accountId: accountId || "default", chatKey: chatKey };
      }
      const s = String(conversationId || "");
      const i = s.indexOf(":"); const j = i >= 0 ? s.indexOf(":", i + 1) : -1;
      if (i < 0 || j < 0) return null;
      return { platform: s.slice(0, i), accountId: s.slice(i + 1, j) || "default", chatKey: s.slice(j + 1) };
    }
    async getOrigin(ctx) {
      const p = this._originParams(ctx || {});
      if (!p) return { ok: false, error: "missing conversation context" };
      return this._get(
        `/api/workspace/origin?platform=${encodeURIComponent(p.platform)}` +
        `&account_id=${encodeURIComponent(p.accountId)}&chat_key=${encodeURIComponent(p.chatKey)}`);
    }
    async saveOrigin(ctx) {
      const p = this._originParams(ctx || {});
      if (!p) return { ok: false, error: "missing conversation context" };
      return this._post(`/api/workspace/origin`, {
        platform: p.platform, account_id: p.accountId, chat_key: p.chatKey,
        profile: (ctx && ctx.profile) || {}, facts: (ctx && ctx.facts) || [],
      });
    }
    /* P1 聊天记录导入：解析（multipart 预览，不落库）/ 确认写入 / 整批撤销 */
    async importOriginParse(ctx) {
      const p = this._originParams(ctx || {});
      if (!p) return { ok: false, error: "missing conversation context" };
      if (!ctx || !ctx.file) return { ok: false, error: "missing file" };
      const fd = new FormData();
      fd.append("file", ctx.file, ctx.file.name || "chat.txt");
      fd.append("platform", p.platform);
      fd.append("account_id", p.accountId);
      fd.append("chat_key", p.chatKey);
      fd.append("source_channel", ctx.sourceChannel || "");
      fd.append("source_label", ctx.sourceLabel || "");
      fd.append("customer_sender", ctx.customerSender || "");
      return this._postForm(`/api/workspace/origin/import/parse`, fd);
    }
    async importOriginConfirm(ctx) {
      const p = this._originParams(ctx || {});
      if (!p) return { ok: false, error: "missing conversation context" };
      return this._post(`/api/workspace/origin/import/confirm`, {
        platform: p.platform, account_id: p.accountId, chat_key: p.chatKey,
        source_channel: (ctx && ctx.sourceChannel) || "",
        source_label: (ctx && ctx.sourceLabel) || "",
        file_name: (ctx && ctx.fileName) || "",
        file_sha256: (ctx && ctx.fileSha256) || "",
        msg_count: (ctx && ctx.msgCount) || 0,
        date_from: (ctx && ctx.dateFrom) || "",
        date_to: (ctx && ctx.dateTo) || "",
        topics: (ctx && ctx.topics) || [],
        note: (ctx && ctx.note) || "",
        facts: (ctx && ctx.facts) || [],
      });
    }
    async importOriginRevoke({ batchId }) {
      if (!batchId) return { ok: false, error: "missing batchId" };
      return this._post(`/api/workspace/origin/import/revoke`, { batch_id: batchId });
    }
    async getRelStage({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage`);
    }
    async confirmStage({ conversationId: cid }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/confirm`, {});
    }
    async downgradeStage({ conversationId: cid, reason }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/downgrade`, { reason });
    }
    async reunionStage({ conversationId: cid }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/reunion`, {});
    }
    async syncContactStage({ contactId, mode }) {
      return this._post(`/api/workspace/contact/${encodeURIComponent(contactId)}/relationship-stage/sync`, { mode: mode || "to_contact" });
    }
    async listPersonas() {
      return this._get(`/api/personas/profiles`);
    }
    async getPersonaBindings() {
      return this._get(`/api/persona/bindings`);
    }
    async bindPersona({ chatKey, persona }) {
      return this._post(`/api/persona/bind`, { chat_id: chatKey, persona });
    }
    async unbindPersona({ chatKey }) {
      return this._post(`/api/persona/unbind`, { chat_id: chatKey });
    }
    /* 会话级人设覆写（2026-07-26 方案 A）:读生效全景 / 换绑 / 解除 / 账号级整号换绑 */
    async personaEffective({ conversationId: cid, platform, accountId, chatKey }) {
      const q = cid
        ? `conversation_id=${encodeURIComponent(cid)}`
        : `platform=${encodeURIComponent(platform || "")}` +
          `&account_id=${encodeURIComponent(accountId || "")}` +
          `&chat_key=${encodeURIComponent(chatKey || "")}`;
      return this._get(`/api/persona/effective?${q}`);
    }
    async bindConvPersona({ conversationId: cid, profileId }) {
      return this._post(`/api/persona/bind`,
        { scope: "conversation", conversation_id: cid, profile_id: profileId });
    }
    async unbindConvPersona({ conversationId: cid }) {
      return this._post(`/api/persona/unbind`,
        { scope: "conversation", conversation_id: cid });
    }
    async setAccountPersona({ platform, accountId, profileId }) {
      return this._post(`/api/persona/account-persona`,
        { platform, account_id: accountId, profile_id: profileId || "" });
    }
    async getCollabContext({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/collab-context`);
    }
    // ── 内部注解（P1-4 第二刀：cp-collab notes 子分区。读走 collab-context 自带的
    //    recent_notes 零新读端点，这里只补写入口；编辑/删除有作者权限闸，v1 留在网页端）──
    async addConvNote({ conversationId: cid, body, mentions }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/notes`,
        { body: body || "", mentions: mentions || [] });
    }
    // ── 知识库 / 快捷回复（P1-4：收编桌面原生 aside 的 kb/tpl 卡进统一 App；
    //    web 原生右栏走 composer 的 / 指令面板与 KB 自动推荐浮层，不在右栏重复）──
    async kbSearch({ q, platform, intent, limit, lang }) {
      // lang（V0 2026-08-18）：会话客户语言——外语会话的命中答案优先取已入库译稿
      return this._get(`/api/unified-inbox/kb-search?q=${encodeURIComponent(q || "")}` +
        `&platform=${encodeURIComponent(platform || "")}&intent=${encodeURIComponent(intent || "")}` +
        `&limit=${encodeURIComponent(limit || 6)}&lang=${encodeURIComponent(lang || "")}`);
    }
    async replyTemplates() {
      return this._get(`/api/unified-inbox/templates`);
    }
    // ── P1（2026-08-18）cp-kb 编辑闭环 ──
    // 个人常用语增删改（服务端 KV，坐席私有）；团队话术读改走 admin 既有端点
    // （PUT 自带快照/审计/热失效，绝不另造写入口）。组件按方法存在性特性探测，
    // 旧适配器（桌面原生 renderer 客户端等）缺方法 → 编辑入口自动隐藏。
    async quickReplyMutate({ action, text, id }) {
      return this._post(`/api/unified-inbox/quick-replies`,
        { action: action || "", text: text || "", id: id || "" });
    }
    async teamTemplates() {
      return this._get(`/api/templates`);
    }
    async teamTemplateUpdate({ key, value }) {
      return this._put(`/api/templates/${encodeURIComponent(key)}`, { value: value });
    }
    async getChainExecutions({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/chain-executions?limit=${encodeURIComponent(limit || 8)}`);
    }
    async cancelChainExecution({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/cancel`, {});
    }
    /* P1 2026-08-12 执行操作四件套（按钮由后端 caps.exec_ops 门控，旧后端不出现） */
    async pauseChainExecution({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/pause`, {});
    }
    async resumeChainExecution({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/resume`, {});
    }
    async skipChainStep({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/skip-step`, {});
    }
    async retryChainExecution({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/retry`, {});
    }
    async listWorkflowChains() {
      return this._get(`/api/workspace/workflow-chains`);
    }
    async seedStarterChains() {
      return this._post(`/api/workspace/workflow-chains/seed`, {});
    }
    async startChain({ conversationId: cid, chainId, goalId }) {
      if (!cid || !chainId) return { ok: false, error: "missing conversationId/chainId" };
      const body = { chain_id: chainId };
      if (goalId) body.goal_id = goalId;   // C2：目标归因（可选，无则零变化）
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/start-chain`, body);
    }
    async getHistory({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/unified-inbox/history?conversation_id=${encodeURIComponent(cid)}&limit=${encodeURIComponent(limit || 30)}`);
    }
    async smartReply(payload) {
      return this._post(`/api/desktop/smart-reply`, payload || {});
    }
    async guardCheck({ text }) {
      return this._post(`/api/desktop/guard-check`, { text });
    }
    async translate({ text, target_lang }) {
      const r = await this._post(`/api/unified-inbox/translate`, { text, target_lang });
      const t = (r && r.translation) || {};
      return { ok: !!(r && r.ok), text: t.translated_text || "" };
    }
    // P4-C：服务端「默认回复语言」（账号>平台>全局）。草稿语言选择器无会话级记忆时取作默认。
    async defaultReplyLang({ platform, account_id } = {}) {
      const qs = new URLSearchParams();
      if (platform) qs.set("platform", platform);
      if (account_id) qs.set("account_id", account_id);
      const q = qs.toString();
      return this._get(`/api/unified-inbox/default-reply-lang${q ? "?" + q : ""}`);
    }
    // AI 对话分析（风险预判 + 阶梯话术 + 摘要）。两端 iframe 同源直达。
    async analyze({ text, messages, chat }) {
      return this._post(`/api/unified-inbox/analyze`, { text: text || "", messages: messages || [], chat: chat || {} });
    }
    // —— 会话运维（P1-4 2026-08-12，统一 App cp-conv-ops 用；网页原生右栏走宿主内联实现同一批端点）——
    async getAutomation({ platform, accountId, chatKey }) {
      return this._get(`/api/unified-inbox/automation?platform=${encodeURIComponent(platform || "")}` +
        `&account_id=${encodeURIComponent(accountId || "default")}&chat_key=${encodeURIComponent(chatKey || "")}`);
    }
    async setAutomationMode({ platform, accountId, chatKey, mode, confirmGroup }) {
      const body = { platform: platform || "", account_id: accountId || "default",
        chat_key: chatKey || "", mode: mode };
      if (confirmGroup) body.confirm_group = true;
      return this._post(`/api/unified-inbox/automation`, body);
    }
    async automationStats({ platform, accountId, chatKey }) {
      return this._get(`/api/unified-inbox/automation-stats?platform=${encodeURIComponent(platform || "")}` +
        `&account_id=${encodeURIComponent(accountId || "default")}&chat_key=${encodeURIComponent(chatKey || "")}`);
    }
    async archiveConversation({ conversationId, archived }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(conversationId)}/archive`,
        { archived: archived !== false });
    }
    async snoozeConversation({ conversationId, minutes }) {
      return this._post(`/api/workspace/conversation/${encodeURIComponent(conversationId)}/snooze`,
        { minutes: minutes });
    }
    // P0 搁置可见化（2026-08-14）：取消搁置 + 搁置清单读回（cp-conv-ops 持久状态行用；
    // listSnoozed 是旧后端缺 automation.snooze_until 搭便车字段时的回落读回路径）
    async unsnoozeConversation({ conversationId }) {
      return this._post(`/api/workspace/conversation/${encodeURIComponent(conversationId)}/unsnooze`, {});
    }
    async listSnoozed() {
      return this._get(`/api/workspace/snoozed`);
    }
    // —— 账号管理（Phase 2，两端共用）——
    async listAccounts() {
      return this._get(`/api/accounts`);
    }
    async getPlatformModes({ platform }) {
      return this._get(`/api/platforms/${encodeURIComponent(platform)}/modes`);
    }
    async startLogin({ platform, mode, account_id, label, proxy_id, use_fingerprint }) {
      return this._post(`/api/platforms/${encodeURIComponent(platform)}/login/start`,
        { mode, account_id, label, proxy_id, use_fingerprint });
    }
    async loginStatus({ platform, login_id }) {
      return this._get(`/api/platforms/${encodeURIComponent(platform)}/login/${encodeURIComponent(login_id)}/status`);
    }
    async cancelLogin({ platform, login_id }) {
      return this._post(`/api/platforms/${encodeURIComponent(platform)}/login/${encodeURIComponent(login_id)}/cancel`, {});
    }
    async accountStart({ platform, account_id }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/start`, {});
    }
    async accountStop({ platform, account_id }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/stop`, {});
    }
    async setAutoReply({ platform, account_id, enabled }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/auto-reply`, { enabled: !!enabled });
    }
    async setAccountOverride({ platform, account_id, override }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/auto-reply/override`, override || {});
    }
    async autoReplyAudit({ limit, platform, account_id, since } = {}) {
      const qs = new URLSearchParams();
      if (limit != null) qs.set("limit", String(limit));
      if (platform) qs.set("platform", platform);
      if (account_id) qs.set("account_id", account_id);
      if (since != null) qs.set("since", String(since));
      const q = qs.toString();
      return this._get(`/api/accounts/auto-reply/audit${q ? "?" + q : ""}`);
    }
    async autoReplyConfig() {
      return this._get(`/api/accounts/auto-reply/config`);
    }
    async autoReplyHealth() {
      return this._get(`/api/accounts/auto-reply/health`);
    }
    async autoReplyWebhooks() {
      return this._get(`/api/accounts/auto-reply/webhooks`);
    }
    async alertCatalog() {
      return this._get(`/api/accounts/auto-reply/alert-catalog`);
    }
    async setAutoReplyWebhooks(list) {
      return this._post(`/api/accounts/auto-reply/webhooks`, { webhooks: list || [] });
    }
    async testAutoReplyWebhook(payload) {
      return this._post(`/api/accounts/auto-reply/webhooks/test`, payload || {});
    }
    async setAutoReplyConfig(settings) {
      return this._post(`/api/accounts/auto-reply/config`, settings || {});
    }
    // SSE 实时流（同源带 cookie）。鉴权/CSP 失败 → onError 回落轮询。
    openAuditStream(onItem, onError) {
      if (typeof EventSource === "undefined") return null;
      let es;
      try { es = new EventSource(`/api/accounts/auto-reply/stream`); }
      catch (e) { return null; }
      es.onmessage = (ev) => {
        try { const d = JSON.parse(ev.data); if (d && d.id) onItem(d); } catch (e) { /* */ }
      };
      es.onerror = () => { try { es.close(); } catch (e) { /* */ } if (onError) onError(); };
      return es;
    }
    // —— 语音克隆 / TTS（与统一收件箱同源）——
    async voiceProfiles() { return this._get("/api/voice/profiles"); }
    /* 音色状态条数据源（P1 2026-08-05）：带会话上下文的实际解析快照
       （将用谁的声/后端/就绪/hub 风险），与 send-voice 同源解析。 */
    async voiceEffectiveConfig({ persona_id, chat_key, platform, account_id }) {
      const q = new URLSearchParams();
      if (persona_id) q.set("persona_id", persona_id);
      if (chat_key) q.set("chat_key", chat_key);
      if (platform) q.set("platform", platform);
      if (account_id) q.set("account_id", account_id);
      return this._get(`/api/voice/effective-config?${q.toString()}`);
    }
    async voiceTts({ text, persona_id, chat_key, platform, account_id, target_lang, confirm_system_voice }) {
      // 会话上下文（可选）：带上后试听与 send-voice 走同一组解析入参
      // （试听=发送 契约；不带=旧行为，服务端按全局回落解析）。
      // P0-V2b 译声：target_lang（'auto'=会话客户语言，服务端解析）→ 先译后念；
      // 不传=旧行为按原文发声。fail-open 全在服务端，这里只透传。
      return this._post("/api/voice/tts-test", {
        text, persona_id: persona_id || undefined,
        chat_key: chat_key || undefined,
        platform: platform || undefined,
        account_id: account_id || undefined,
        target_lang: target_lang || undefined,
        // Q-22 #287：系统音二次确认位——只在坐席点了「用系统音发」才带 1；不带=服务端阻断不出系统音
        confirm_system_voice: confirm_system_voice ? 1 : undefined,
      });
    }
    async sendVoice(body) { return this._post("/api/unified-inbox/send-voice", body || {}); }
    async voiceReconcile() { return this._get("/api/voice/reconcile"); }
    async voicePurge(body) { return this._post("/api/voice/purge", body || {}); }
    async voicePurgeOrphans() { return this._post("/api/voice/purge-orphans", {}); }
    async voiceUnbind({ persona_id, purge_cloud }) {
      const q = purge_cloud ? "?purge_cloud=1" : "";
      // DELETE 同属写方法，走 CSRF 头（与 _post 同一通行证）
      const r = await fetch(`/api/voice/profiles/${encodeURIComponent(persona_id || "")}${q}`, {
        method: "DELETE", headers: _writeHeaders(),
      });
      return await r.json();
    }
    async voiceRebind(body) { return this._post("/api/voice/rebind", body || {}); }
    async voiceEnroll(payload) {
      const fd = new FormData();
      const p = payload || {};
      if (p.file) fd.append("file", p.file);
      fd.append("persona_id", String(p.persona_id || ""));
      fd.append("preferred_name", String(p.preferred_name || ""));
      fd.append("language_type", String(p.language_type || "Japanese"));
      if (p.reference_text) fd.append("reference_text", String(p.reference_text));
      // P0「从消息一键导入」扩展字段（缺省不带＝旧口径零变化）：media_ref 让服务端
      // 直读会话归档语音；owner_consent/force 过授权与质检门；其余三个是音色溯源。
      for (const k of ["media_ref", "owner_consent", "force",
                       "platform", "conversation_id", "message_id"]) {
        if (p[k] !== undefined && p[k] !== null && String(p[k]) !== "") {
          fd.append(k, String(p[k]));
        }
      }
      // multipart 写请求同样要过 CSRF（S3 起非 JSON 写也强校验）；Content-Type 由浏览器带 boundary
      const r = await fetch("/api/voice/enroll", { method: "POST", headers: _writeHeaders(), body: fd });
      return await r.json();
    }
    // —— 单次翻译工具（工具箱 cp-xlate-tools，2026-08-17）：与收件箱「对话翻译」
    //    弹层同一批端点（unified_inbox_translate_routes），零后端改动。
    //    body 契约见各端点 docstring；auto 目标语需带会话三元组（组件侧负责）。
    async xlateImage({ imageB64, targetLang }) {
      return this._post(`/api/unified-inbox/translate-image`,
        { image_b64: imageB64, target_lang: targetLang });
    }
    async xlateVoice({ audioB64, targetLang }) {
      return this._post(`/api/unified-inbox/translate-voice`,
        { audio_b64: audioB64, target_lang: targetLang });
    }
    async xlateCompare(body) {
      return this._post(`/api/unified-inbox/translate-compare`, body || {});
    }
    async xlateDocument(body) {
      return this._post(`/api/unified-inbox/translate-document`, body || {});
    }
    async xlateDocumentFile(body) {
      return this._post(`/api/unified-inbox/translate-document-file`, body || {});
    }
    // —— AI 生成图片（工具箱 cp-image，2026-08-21）：坐席手动出图 → VLM 后验 →
    //    复用既有 send-media 发送 / 存入相册。generate 是重操作（子进程打 ComfyUI），
    //    组件侧自管 busy/超时。——
    async imageConfig() { return this._get(`/api/image/config`); }
    async imageGenerate(body) { return this._post(`/api/image/generate`, body || {}); }
    async imageSaveAlbum(body) { return this._post(`/api/image/save-album`, body || {}); }
    // —— P1（2026-08-22）：异步任务三件套 + 相册优先 + 发送回写账本。
    //    组件按 config.jobs_api/album_pick 特性探测——旧后端缺方法/404 自动退回同步链。
    async imageJobCreate(body) { return this._post(`/api/image/jobs`, body || {}); }
    async imageJobStatus(jobId) { return this._get(`/api/image/jobs/${encodeURIComponent(jobId)}`); }
    async imageJobCancel(jobId) { return this._post(`/api/image/jobs/${encodeURIComponent(jobId)}/cancel`, {}); }
    async imageAlbumStock(personaId, scene) {
      const q = new URLSearchParams({ persona_id: personaId || "", scene: scene || "" });
      return this._get(`/api/image/album-stock?${q.toString()}`);
    }
    async imageMarkSent(body) { return this._post(`/api/image/mark-sent`, body || {}); }
    async imageSceneHints(personaId) {
      const q = new URLSearchParams({ persona_id: personaId || "" });
      return this._get(`/api/image/scene-hints?${q.toString()}`);
    }
    /* 发送生成图到当前会话：**不新造发送链**——取生成预览 blob 走既有 send-media
       （幂等/未送达回执/接管全复用）。blob 由组件 fetch(preview_url) 得到。 */
    async sendMedia({ platform, accountId, chatKey, blob, filename, caption, clientMsgId }) {
      const fd = new FormData();
      fd.append("file", blob, filename || "gen.png");
      fd.append("platform", platform || "");
      fd.append("account_id", accountId || "default");
      fd.append("chat_key", chatKey || "");
      fd.append("caption", caption || "");
      if (clientMsgId) fd.append("client_msg_id", clientMsgId);
      return this._postForm(`/api/unified-inbox/send-media`, fd);
    }
    // —— 智能养号（工具箱 cp-nurture，2026-08-21）：状态总览（复用 fleet-health）+
    //    用户自配每号养护方案（写 ops.nurture overlay）。——
    async nurtureStatus() { return this._get(`/api/nurture/status`); }
    async nurtureConfig() { return this._get(`/api/nurture/config`); }
    async nurtureSave(body) { return this._post(`/api/nurture/config`, body || {}); }
    async nurtureEngine(body) { return this._post(`/api/nurture/engine`, body || {}); }
    async nurtureShadow(limit) { return this._get(`/api/nurture/shadow?limit=${encodeURIComponent(limit || 50)}`); }
    async nurtureProbe(body) { return this._post(`/api/nurture/probe`, body || {}); }
  }

  // —— 桌面适配器:经 window.shell IPC ——
  class DesktopCopilotClient {
    _shell() { return root.shell || {}; }
    async getRelStage({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.relStage ? s.relStage({ conversation_id: cid }) : { ok: false, error: "shell.relStage 未暴露" };
    }
    async confirmStage({ conversationId: cid }) {
      const s = this._shell();
      return s.relConfirm ? s.relConfirm({ conversation_id: cid }) : { ok: false };
    }
    async downgradeStage({ conversationId: cid, reason }) {
      const s = this._shell();
      return s.relDowngrade ? s.relDowngrade({ conversation_id: cid, reason }) : { ok: false };
    }
    async reunionStage({ conversationId: cid }) {
      const s = this._shell();
      return s.relReunion ? s.relReunion({ conversation_id: cid }) : { ok: false };
    }
    async syncContactStage({ contactId, mode }) {
      const s = this._shell();
      return s.relSync ? s.relSync({ contact_id: contactId, mode: mode || "to_contact" }) : { ok: false };
    }
    async listPersonas() {
      const s = this._shell();
      return s.personas ? s.personas() : { ok: false, error: "shell.personas 未暴露" };
    }
    async getPersonaBindings() {
      const s = this._shell();
      return s.personaBindings ? s.personaBindings() : { ok: false, error: "shell.personaBindings 未暴露" };
    }
    async bindPersona({ chatKey, persona }) {
      const s = this._shell();
      return s.personaBind ? s.personaBind({ chat_id: chatKey, persona }) : { ok: false };
    }
    async unbindPersona({ chatKey }) {
      const s = this._shell();
      return s.personaUnbind ? s.personaUnbind({ chat_id: chatKey }) : { ok: false };
    }
    /* 会话级覆写:壳未暴露对应 IPC 时优雅降级（组件退回 legacy 绑定 UI）*/
    async personaEffective({ conversationId: cid, platform, accountId, chatKey }) {
      const s = this._shell();
      return s.personaEffective
        ? s.personaEffective({ conversation_id: cid, platform, account_id: accountId, chat_key: chatKey })
        : { ok: false, error: "shell.personaEffective 未暴露" };
    }
    async bindConvPersona({ conversationId: cid, profileId }) {
      const s = this._shell();
      return s.personaBindConv
        ? s.personaBindConv({ conversation_id: cid, profile_id: profileId })
        : { ok: false, error: "shell.personaBindConv 未暴露" };
    }
    async unbindConvPersona({ conversationId: cid }) {
      const s = this._shell();
      return s.personaUnbindConv
        ? s.personaUnbindConv({ conversation_id: cid })
        : { ok: false, error: "shell.personaUnbindConv 未暴露" };
    }
    async setAccountPersona({ platform, accountId, profileId }) {
      const s = this._shell();
      return s.personaAccountSet
        ? s.personaAccountSet({ platform, account_id: accountId, profile_id: profileId || "" })
        : { ok: false, error: "shell.personaAccountSet 未暴露" };
    }
    async getCollabContext({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.collabContext ? s.collabContext({ conversation_id: cid }) : { ok: false, error: "shell.collabContext 未暴露" };
    }
    async getChainExecutions({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.chainExecutions ? s.chainExecutions({ conversation_id: cid, limit: limit || 8 }) : { ok: false, error: "shell.chainExecutions 未暴露" };
    }
    async cancelChainExecution({ execId }) {
      const s = this._shell();
      return s.chainCancel ? s.chainCancel({ exec_id: execId }) : { ok: false };
    }
    /* P1 执行操作：壳桥未升级 → 软失败（按钮本就依赖 caps，走到这里是兜底） */
    async pauseChainExecution({ execId }) {
      const s = this._shell();
      return s.chainPause ? s.chainPause({ exec_id: execId }) : { ok: false, error: "shell.chainPause 未暴露" };
    }
    async resumeChainExecution({ execId }) {
      const s = this._shell();
      return s.chainResume ? s.chainResume({ exec_id: execId }) : { ok: false, error: "shell.chainResume 未暴露" };
    }
    async skipChainStep({ execId }) {
      const s = this._shell();
      return s.chainSkipStep ? s.chainSkipStep({ exec_id: execId }) : { ok: false, error: "shell.chainSkipStep 未暴露" };
    }
    async retryChainExecution({ execId }) {
      const s = this._shell();
      return s.chainRetry ? s.chainRetry({ exec_id: execId }) : { ok: false, error: "shell.chainRetry 未暴露" };
    }
    async listWorkflowChains() {
      const s = this._shell();
      return s.workflowChains ? s.workflowChains({}) : { ok: false, error: "shell.workflowChains 未暴露" };
    }
    async seedStarterChains() {
      const s = this._shell();
      return s.seedStarterChains ? s.seedStarterChains({}) : { ok: false, error: "shell.seedStarterChains 未暴露" };
    }
    async startChain({ conversationId: cid, chainId, goalId }) {
      const s = this._shell();
      const payload = { conversation_id: cid, chain_id: chainId };
      if (goalId) payload.goal_id = goalId;   // 壳未识别时多余键被忽略，无害
      return s.startChain
        ? s.startChain(payload)
        : { ok: false, error: "shell.startChain 未暴露" };
    }
    async getHistory({ conversationId: cid, limit }) {
      // 桌面壳按 platform/account/chat_key 取 live thread;按 conversation_id 自取留待 iframe 同源态用 Web 适配器
      const s = this._shell();
      return s.historyByConv ? s.historyByConv({ conversation_id: cid, limit: limit || 30 }) : { ok: false, error: "shell.historyByConv 未暴露" };
    }
    async smartReply(payload) {
      const s = this._shell();
      return s.smartReply ? s.smartReply(payload || {}) : { ok: false, error: "shell.smartReply 未暴露" };
    }
    async guardCheck({ text }) {
      const s = this._shell();
      return s.guardCheck ? s.guardCheck({ text }) : { ok: false };
    }
    async translate({ text, target_lang }) {
      const s = this._shell();
      return s.translate ? s.translate({ text, target_lang }) : { ok: false, error: "shell.translate 未暴露" };
    }
    // P4-C：桌面壳未暴露此 IPC（草稿组件跑在同源 copilot iframe，走 Web 适配器取）→ 优雅回落。
    async defaultReplyLang(args) {
      const s = this._shell();
      return s.defaultReplyLang ? s.defaultReplyLang(args || {}) : { ok: false, error: "shell.defaultReplyLang 未暴露" };
    }
    async analyze(args) {
      const s = this._shell();
      return s.analyze ? s.analyze(args || {}) : { ok: false, error: "shell.analyze 未暴露" };
    }
    // —— 账号管理（Phase 2，两端共用）——
    async listAccounts() {
      const s = this._shell();
      return s.accountsList ? s.accountsList() : { ok: false, error: "shell.accountsList 未暴露" };
    }
    async getPlatformModes({ platform }) {
      const s = this._shell();
      return s.platformModes ? s.platformModes({ platform }) : { ok: false, error: "shell.platformModes 未暴露" };
    }
    async startLogin(args) {
      const s = this._shell();
      return s.loginStart ? s.loginStart(args || {}) : { ok: false, error: "shell.loginStart 未暴露" };
    }
    async loginStatus(args) {
      const s = this._shell();
      return s.loginStatus ? s.loginStatus(args || {}) : { ok: false, error: "shell.loginStatus 未暴露" };
    }
    async cancelLogin(args) {
      const s = this._shell();
      return s.loginCancel ? s.loginCancel(args || {}) : { ok: false };
    }
    async accountStart(args) {
      const s = this._shell();
      return s.accountStart ? s.accountStart(args || {}) : { ok: false };
    }
    async accountStop(args) {
      const s = this._shell();
      return s.accountStop ? s.accountStop(args || {}) : { ok: false };
    }
    async setAutoReply(args) {
      const s = this._shell();
      return s.setAutoReply ? s.setAutoReply(args || {}) : { ok: false, error: "shell.setAutoReply 未暴露" };
    }
    async setAccountOverride(args) {
      const s = this._shell();
      return s.setAccountOverride ? s.setAccountOverride(args || {}) : { ok: false, error: "shell.setAccountOverride 未暴露" };
    }
    async autoReplyAudit(args) {
      const s = this._shell();
      return s.autoReplyAudit ? s.autoReplyAudit(args || {}) : { ok: false, error: "shell.autoReplyAudit 未暴露" };
    }
    async autoReplyConfig() {
      const s = this._shell();
      return s.autoReplyConfig ? s.autoReplyConfig() : { ok: false, error: "shell.autoReplyConfig 未暴露" };
    }
    async autoReplyHealth() {
      const s = this._shell();
      return s.autoReplyHealth ? s.autoReplyHealth() : { ok: false, error: "shell.autoReplyHealth 未暴露" };
    }
    async autoReplyWebhooks() {
      const s = this._shell();
      return s.autoReplyWebhooks ? s.autoReplyWebhooks() : { ok: false, error: "shell.autoReplyWebhooks 未暴露" };
    }
    async alertCatalog() {
      const s = this._shell();
      return s.alertCatalog ? s.alertCatalog() : { ok: false, error: "shell.alertCatalog 未暴露" };
    }
    async setAutoReplyWebhooks(list) {
      const s = this._shell();
      return s.setAutoReplyWebhooks ? s.setAutoReplyWebhooks(list || []) : { ok: false, error: "shell.setAutoReplyWebhooks 未暴露" };
    }
    async testAutoReplyWebhook(payload) {
      const s = this._shell();
      return s.testAutoReplyWebhook ? s.testAutoReplyWebhook(payload || {}) : { ok: false, error: "shell.testAutoReplyWebhook 未暴露" };
    }
    async setAutoReplyConfig(settings) {
      const s = this._shell();
      return s.setAutoReplyConfig ? s.setAutoReplyConfig(settings || {}) : { ok: false, error: "shell.setAutoReplyConfig 未暴露" };
    }
    async voiceProfiles() {
      const s = this._shell();
      return s.voiceProfiles ? s.voiceProfiles() : { ok: false };
    }
    async voiceEffectiveConfig(args) {
      const s = this._shell();
      return s.voiceEffectiveConfig ? s.voiceEffectiveConfig(args || {}) : { ok: false };
    }
    async voiceTts(args) {
      const s = this._shell();
      return s.voiceTts ? s.voiceTts(args || {}) : { ok: false };
    }
    async sendVoice(body) {
      const s = this._shell();
      return s.sendVoice ? s.sendVoice(body || {}) : { ok: false };
    }
    async voiceReconcile() {
      const s = this._shell();
      return s.voiceReconcile ? s.voiceReconcile() : { ok: false };
    }
    async voicePurge(body) {
      const s = this._shell();
      return s.voicePurge ? s.voicePurge(body || {}) : { ok: false };
    }
    async voicePurgeOrphans() {
      const s = this._shell();
      return s.voicePurgeOrphans ? s.voicePurgeOrphans() : { ok: false };
    }
    async voiceUnbind(args) {
      const s = this._shell();
      return s.voiceUnbind ? s.voiceUnbind(args || {}) : { ok: false };
    }
    async voiceRebind(body) {
      const s = this._shell();
      return s.voiceRebind ? s.voiceRebind(body || {}) : { ok: false };
    }
    async voiceEnroll(payload) {
      const s = this._shell();
      return s.voiceEnroll ? s.voiceEnroll(payload || {}) : { ok: false, error: "shell.voiceEnroll 未暴露" };
    }
    // —— 单次翻译工具：桌面原生 aside 走 IPC（未暴露=软失败保接口对齐；
    //    统一 App iframe 场景实际用 Web 适配器，不经这里）——
    async xlateImage(args) {
      const s = this._shell();
      return s.xlateImage ? s.xlateImage(args || {}) : { ok: false, error: "shell.xlateImage 未暴露" };
    }
    async xlateVoice(args) {
      const s = this._shell();
      return s.xlateVoice ? s.xlateVoice(args || {}) : { ok: false, error: "shell.xlateVoice 未暴露" };
    }
    async xlateCompare(body) {
      const s = this._shell();
      return s.xlateCompare ? s.xlateCompare(body || {}) : { ok: false, error: "shell.xlateCompare 未暴露" };
    }
    async xlateDocument(body) {
      const s = this._shell();
      return s.xlateDocument ? s.xlateDocument(body || {}) : { ok: false, error: "shell.xlateDocument 未暴露" };
    }
    async xlateDocumentFile(body) {
      const s = this._shell();
      return s.xlateDocumentFile ? s.xlateDocumentFile(body || {}) : { ok: false, error: "shell.xlateDocumentFile 未暴露" };
    }
    // —— AI 生成图片 / 智能养号（cp-image / cp-nurture）：P0 仅网页原生右栏（cookie 态
    //    WebCopilotClient）；桌面 IPC 未桥 → 软失败（组件按方法存在性/返回 ok 优雅降级）——
    async imageConfig() { const s = this._shell(); return s.imageConfig ? s.imageConfig() : { ok: false, error: "shell.imageConfig 未暴露" }; }
    async imageGenerate(body) { const s = this._shell(); return s.imageGenerate ? s.imageGenerate(body || {}) : { ok: false, error: "shell.imageGenerate 未暴露" }; }
    async imageSaveAlbum(body) { const s = this._shell(); return s.imageSaveAlbum ? s.imageSaveAlbum(body || {}) : { ok: false, error: "shell.imageSaveAlbum 未暴露" }; }
    async imageJobCreate(body) { const s = this._shell(); return s.imageJobCreate ? s.imageJobCreate(body || {}) : { ok: false, error: "shell.imageJobCreate 未暴露" }; }
    async imageJobStatus(jobId) { const s = this._shell(); return s.imageJobStatus ? s.imageJobStatus(jobId) : { ok: false, error: "shell.imageJobStatus 未暴露" }; }
    async imageJobCancel(jobId) { const s = this._shell(); return s.imageJobCancel ? s.imageJobCancel(jobId) : { ok: false, error: "shell.imageJobCancel 未暴露" }; }
    async imageAlbumStock(personaId, scene) { const s = this._shell(); return s.imageAlbumStock ? s.imageAlbumStock(personaId, scene) : { ok: false, error: "shell.imageAlbumStock 未暴露" }; }
    async imageMarkSent(body) { const s = this._shell(); return s.imageMarkSent ? s.imageMarkSent(body || {}) : { ok: false, error: "shell.imageMarkSent 未暴露" }; }
    async imageSceneHints(personaId) { const s = this._shell(); return s.imageSceneHints ? s.imageSceneHints(personaId) : { ok: false, error: "shell.imageSceneHints 未暴露" }; }
    async sendMedia(args) { const s = this._shell(); return s.sendMedia ? s.sendMedia(args || {}) : { ok: false, error: "shell.sendMedia 未暴露" }; }
    async nurtureStatus() { const s = this._shell(); return s.nurtureStatus ? s.nurtureStatus() : { ok: false, error: "shell.nurtureStatus 未暴露" }; }
    async nurtureConfig() { const s = this._shell(); return s.nurtureConfig ? s.nurtureConfig() : { ok: false, error: "shell.nurtureConfig 未暴露" }; }
    async nurtureSave(body) { const s = this._shell(); return s.nurtureSave ? s.nurtureSave(body || {}) : { ok: false, error: "shell.nurtureSave 未暴露" }; }
    async nurtureEngine(body) { const s = this._shell(); return s.nurtureEngine ? s.nurtureEngine(body || {}) : { ok: false, error: "shell.nurtureEngine 未暴露" }; }
    async nurtureShadow(limit) { const s = this._shell(); return s.nurtureShadow ? s.nurtureShadow(limit || 50) : { ok: false, error: "shell.nurtureShadow 未暴露" }; }
    async nurtureProbe(body) { const s = this._shell(); return s.nurtureProbe ? s.nurtureProbe(body || {}) : { ok: false, error: "shell.nurtureProbe 未暴露" }; }
  }

  function createCopilotClient() {
    return root.shell ? new DesktopCopilotClient() : new WebCopilotClient();
  }

  root.CopilotShared = Object.assign(root.CopilotShared || {}, {
    conversationId,
    setAuthToken,
    // 供裸 fetch 组件（cp-goal 等直连 /api 的面板）与宿主 fetch 补丁取鉴权头：
    // 浏览器同源 cookie 场景返回空对象（零行为变化），桌面 iframe token 场景带 Bearer。
    authHeaders: _authHeaders,
    WebCopilotClient,
    DesktopCopilotClient,
    createCopilotClient,
  });
})(typeof window !== "undefined" ? window : this);
