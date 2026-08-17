"use strict";

/* 翻译调度器（单一事实来源，桌面 preload 与浏览器扩展共用）。
 *
 * 背景：现有 core.js 的「自动翻译」是每个气泡各发一次 translate 请求，无缓存、无去重、
 * 无批处理、无语言守卫、无预算。竞品的「全局翻译」若照这个实现开起来，会：
 *   ①对同一句话（滚动来回出现）反复烧字符；②多账号同时刷屏时把后端打爆；
 *   ③把本来就是中文的消息也送去翻译（garble + 白烧额度）；④免费额度悄悄见底。
 *
 * 本模块把「要不要翻、翻过没有、能不能翻」的决策收成纯函数 + 一个可注入时钟/传输的
 * 调度器,让自动全局翻译变得省字符、抗刷屏、可观测：
 *   - 语言守卫 looksLikeTargetLang：已是目标语（如已是中文）直接跳过,fail-open。
 *   - LRU 缓存：同句话只翻一次（滚动/重进会话不重复烧）。
 *   - inflight 去重：同句话在途时共享同一个 Promise。
 *   - 批处理 planBatches：窗口内合并成一次 translateBatch 调用。
 *   - 字符预算 createBudget：滑动窗内超额即拒发,保护免费额度（把「配额焦虑」变成硬护栏）。
 *
 * 全部纯逻辑 + 依赖注入（translateBatch / now）,零 DOM / 零 electron,Node 可直跑单测。
 */

// ── 文本归一化（缓存键 & 意义判定共用）──────────────────────────────────────
function normalizeText(s) {
  return String(s == null ? "" : s).replace(/\s+/g, " ").trim();
}

// 译文是否「有意义」：非空且与原文归一化后不同（否则是「≈原文」,不该顶掉原文展示）。
function isMeaningful(orig, translated) {
  const a = normalizeText(orig).toLowerCase();
  const b = normalizeText(translated).toLowerCase();
  return !!b && a !== b;
}

// ── 语言守卫（廉价启发式；后端仍做真检测,故这里只做「明显已是目标语」的省钱短路）──
// 统计三类「字母」占比：汉字 / 拉丁 / 其它书写系统字母（假名·谚文·西里尔·阿拉伯…）。
// 目的只有一个：判断「这段文本是不是已经基本是目标语了」,是则不必再翻。宁可漏判（多翻
// 一次）也不误判（把外语当目标语而不翻）——所以判不准时一律返回 false（fail-open,去翻）。
function scriptCounts(text) {
  const s = String(text || "");
  let han = 0, latin = 0, other = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if ((c >= 0x4e00 && c <= 0x9fff) || (c >= 0x3400 && c <= 0x4dbf)) han++;
    else if ((c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a) ||
             (c >= 0xc0 && c <= 0x24f)) latin++;
    else if ((c >= 0x3040 && c <= 0x30ff) ||   // 日文假名
             (c >= 0xac00 && c <= 0xd7a3) ||    // 谚文
             (c >= 0x0400 && c <= 0x04ff) ||    // 西里尔
             (c >= 0x0600 && c <= 0x06ff) ||    // 阿拉伯
             (c >= 0x0e00 && c <= 0x0e7f)) other++; // 泰文
  }
  return { han, latin, other, letters: han + latin + other };
}

function looksLikeTargetLang(text, lang) {
  const t = String(lang || "").toLowerCase();
  const c = scriptCounts(text);
  if (c.letters === 0) return true; // 纯符号/表情/数字：翻了也无意义,当作「已是目标语」跳过
  if (t.indexOf("zh") === 0 || t === "cn") return c.han / c.letters >= 0.6;
  // 拉丁语族目标语（en/es/fr/pt/id/vi/de/it…）:主要由拉丁字母构成即视为已达标
  const latinLangs = ["en", "es", "fr", "pt", "id", "vi", "de", "it", "nl", "tr", "pl"];
  if (latinLangs.indexOf(t.slice(0, 2)) >= 0) return c.latin / c.letters >= 0.6;
  return false; // 其它目标语无廉价判据 → fail-open 去翻
}

// ── LRU 缓存（Map 插入序即 LRU；get 命中回插到队尾,超额删队首）───────────────
function createLRU(max) {
  const cap = Math.max(1, max | 0 || 200);
  const m = new Map();
  return {
    has: (k) => m.has(k),
    get(k) {
      if (!m.has(k)) return undefined;
      const v = m.get(k);
      m.delete(k);
      m.set(k, v);
      return v;
    },
    put(k, v) {
      if (m.has(k)) m.delete(k);
      m.set(k, v);
      while (m.size > cap) m.delete(m.keys().next().value);
    },
    get size() { return m.size; },
  };
}

// ── 批处理规划（纯）：把 texts 切成每批 ≤ maxBatch 的数组 ─────────────────────
function planBatches(texts, maxBatch) {
  const n = Math.max(1, maxBatch | 0 || 16);
  const out = [];
  const arr = Array.isArray(texts) ? texts : [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

// ── 字符预算（滑动窗,纯 + 注入时钟）：保护免费额度,超额拒发 ────────────────
function createBudget(opts) {
  const o = opts || {};
  const limit = o.limit != null ? o.limit : 0;       // 0/负 = 不限（护栏关闭）
  const windowMs = o.windowMs || 60000;
  const now = typeof o.now === "function" ? o.now : Date.now;
  let events = []; // {ts, chars}
  function prune(t) {
    const cutoff = t - windowMs;
    if (events.length && events[0].ts <= cutoff) {
      events = events.filter((e) => e.ts > cutoff);
    }
  }
  function used() {
    prune(now());
    return events.reduce((s, e) => s + e.chars, 0);
  }
  return {
    tryConsume(chars) {
      const c = Math.max(0, chars | 0);
      if (!(limit > 0)) return true; // 未设限
      const t = now();
      prune(t);
      const cur = events.reduce((s, e) => s + e.chars, 0);
      if (cur + c > limit) return false;
      events.push({ ts: t, chars: c });
      return true;
    },
    used,
    remaining() { return limit > 0 ? Math.max(0, limit - used()) : Infinity; },
  };
}

// ── 调度器 ───────────────────────────────────────────────────────────────────
// deps: {
//   translateBatch(texts, targetLang) -> Promise<string[]>  // 必填传输（可 stub）
//   targetLang() | targetLang: string                        // 目标语（默认 zh）
//   cacheMax, maxBatch, budget?(createBudget 实例), now?, langGuard?(默认开)
// }
function createTranslateScheduler(deps) {
  const d = deps || {};
  const transport = typeof d.translateBatch === "function" ? d.translateBatch : null;
  const cache = createLRU(d.cacheMax || 500);
  const maxBatch = d.maxBatch || 16;
  const budget = d.budget || null;
  const langGuardOn = d.langGuard !== false;
  const getTarget = typeof d.targetLang === "function"
    ? d.targetLang
    : () => String(d.targetLang || "zh");

  // autoFlushMs>0：request 触发去抖窗,窗口内合批后自动 flush（生产集成用,省得调用方
  // 自己写去抖）；0/未设 = 纯手动 flush（单测确定性）。
  const autoFlushMs = d.autoFlushMs > 0 ? d.autoFlushMs : 0;

  const inflight = new Map(); // key -> Promise
  let queue = [];             // {key, text, target, resolve}
  let _timer = null;
  const stats = {
    requested: 0, cache_hits: 0, skipped_lang: 0, denied_budget: 0,
    batches: 0, translated: 0, translated_chars: 0, errors: 0,
  };

  function keyOf(text, target) { return target + "\u0001" + normalizeText(text); }

  function _armAutoFlush() {
    if (!autoFlushMs || typeof setTimeout !== "function" || _timer) return;
    _timer = setTimeout(() => { _timer = null; flush(); }, autoFlushMs);
  }

  // 请求翻译一段文本 → Promise<{ok, text, translated, meaningful, reason, source}>
  function request(text) {
    stats.requested++;
    const target = getTarget();
    const norm = normalizeText(text);
    if (!norm) {
      return Promise.resolve({ ok: true, text: "", translated: "", meaningful: false, reason: "empty", source: "guard" });
    }
    if (langGuardOn && looksLikeTargetLang(norm, target)) {
      stats.skipped_lang++;
      return Promise.resolve({ ok: true, text: norm, translated: norm, meaningful: false, reason: "already_target", source: "guard" });
    }
    const key = keyOf(norm, target);
    if (cache.has(key)) {
      stats.cache_hits++;
      const v = cache.get(key);
      return Promise.resolve(Object.assign({}, v, { source: "cache" }));
    }
    if (inflight.has(key)) return inflight.get(key);
    if (budget && !budget.tryConsume(norm.length)) {
      stats.denied_budget++;
      return Promise.resolve({ ok: false, text: norm, translated: "", meaningful: false, reason: "budget", source: "guard" });
    }
    let resolveFn;
    const p = new Promise((res) => { resolveFn = res; });
    inflight.set(key, p);
    queue.push({ key, text: norm, target, resolve: resolveFn });
    _armAutoFlush();
    return p;
  }

  // 把当前队列合并成若干批,逐批调用 transport,回填缓存并结算 Promise。
  async function flush() {
    if (_timer) { try { clearTimeout(_timer); } catch (e) { /* noop */ } _timer = null; }
    if (!queue.length) return;
    const pending = queue;
    queue = [];
    // 同 target 才能合批；本调度器单目标语,直接按 target 分组稳妥
    const byTarget = new Map();
    for (const item of pending) {
      if (!byTarget.has(item.target)) byTarget.set(item.target, []);
      byTarget.get(item.target).push(item);
    }
    for (const [target, items] of byTarget) {
      const batches = planBatches(items, maxBatch);
      for (const batch of batches) {
        stats.batches++;
        const texts = batch.map((b) => b.text);
        let results = null;
        try {
          results = transport ? await transport(texts, target) : null;
        } catch (e) {
          results = null;
        }
        for (let i = 0; i < batch.length; i++) {
          const item = batch[i];
          const tr = results && results[i] != null ? String(results[i]) : "";
          if (!tr) {
            stats.errors++;
            inflight.delete(item.key);
            item.resolve({ ok: false, text: item.text, translated: "", meaningful: false, reason: "error", source: "net" });
            continue;
          }
          const meaningful = isMeaningful(item.text, tr);
          const record = { ok: true, text: item.text, translated: tr, meaningful, reason: meaningful ? "ok" : "same" };
          cache.put(item.key, record);
          stats.translated++;
          stats.translated_chars += item.text.length;
          inflight.delete(item.key);
          item.resolve(Object.assign({}, record, { source: "net" }));
        }
      }
    }
  }

  return {
    request,
    flush,
    stats: () => Object.assign({}, stats, { cache_size: cache.size, queued: queue.length }),
    _cache: cache,
  };
}

// 把 /api/unified-inbox/translate-batch 的响应对齐回「请求顺序的 string[]」。
// 端点返回 { items:[{id, translation:{translated_text, ok?...}}] };调用方以「索引字符串」当 id,
// 故按 id 回填,缺失/显式失败(ok===false)→ ""（调度器据此记 error,可重试）。纯函数,可单测。
function alignBatchResponse(texts, apiResp) {
  const arr = Array.isArray(texts) ? texts : [];
  const items = (apiResp && apiResp.items) || [];
  const byId = new Map();
  for (const it of items) {
    if (!it) continue;
    const tr = it.translation || {};
    const t = tr.translated_text || tr.text || tr.translated || "";
    byId.set(String(it.id), tr.ok === false ? "" : String(t || ""));
  }
  return arr.map((_, i) => (byId.has(String(i)) ? byId.get(String(i)) : ""));
}

const _api = {
  normalizeText, isMeaningful, scriptCounts, looksLikeTargetLang,
  createLRU, planBatches, createBudget, createTranslateScheduler, alignBatchResponse,
};
if (typeof module !== "undefined" && module.exports) module.exports = _api;
if (typeof globalThis !== "undefined") globalThis.ATranslateScheduler = _api;
