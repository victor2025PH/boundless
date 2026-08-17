"use strict";

// 翻译调度器纯逻辑 + 调度单测（无框架,node 直跑）：node test/translate-scheduler.test.js
const assert = require("assert");
const {
  normalizeText, isMeaningful, scriptCounts, looksLikeTargetLang,
  createLRU, planBatches, createBudget, createTranslateScheduler, alignBatchResponse,
} = require("../../shared/inject/translate-scheduler.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// ── 归一化 / 意义判定 ─────────────────────────────────────────────────────────
ok("norm 折叠空白", normalizeText("  a\n b  ") === "a b");
ok("norm null 安全", normalizeText(null) === "");
ok("meaningful 同文→false", isMeaningful("hi", "hi") === false);
ok("meaningful 归一化同文→false", isMeaningful("  hi ", "hi") === false);
ok("meaningful 异文→true", isMeaningful("hi", "你好") === true);
ok("meaningful 空译→false", isMeaningful("hi", "") === false);

// ── scriptCounts / 语言守卫 ───────────────────────────────────────────────────
const sc = scriptCounts("你好ab");
ok("scriptCounts han", sc.han === 2 && sc.latin === 2);
ok("guard 中文→zh true", looksLikeTargetLang("你好世界", "zh") === true);
ok("guard 英文→zh false", looksLikeTargetLang("hello world", "zh") === false);
ok("guard 英文→en true", looksLikeTargetLang("hello world", "en") === true);
ok("guard 中文→en false", looksLikeTargetLang("你好", "en") === false);
ok("guard 纯符号→true(跳过)", looksLikeTargetLang("!!! 123 :)", "zh") === true);
ok("guard 日文假名→zh false(去翻)", looksLikeTargetLang("こんにちは", "zh") === false);
ok("guard 西里尔→en false(去翻)", looksLikeTargetLang("Привет", "en") === false);
ok("guard 未知目标语→false(fail-open)", looksLikeTargetLang("anything", "sw") === false);

// ── LRU ───────────────────────────────────────────────────────────────────────
const lru = createLRU(2);
lru.put("a", 1); lru.put("b", 2); lru.put("c", 3); // a 被挤出
ok("lru 超额删队首", lru.has("a") === false && lru.has("b") && lru.has("c"));
lru.get("b");        // b 回插到队尾
lru.put("d", 4);     // 挤出队首 c,b 存活
ok("lru get 回插保活", lru.has("b") === true && lru.has("c") === false && lru.has("d") === true);
ok("lru size", lru.size === 2);

// ── alignBatchResponse（对齐 /translate-batch 响应回请求顺序）─────────────────
const ab = alignBatchResponse(["a", "b", "c"], {
  ok: true,
  items: [
    { id: "0", translation: { ok: true, translated_text: "TA" } },
    { id: "2", translation: { ok: true, translated_text: "TC" } }, // 乱序 + 缺 id=1
  ],
});
ok("align 按 id 回填", ab[0] === "TA" && ab[2] === "TC");
ok("align 缺项→空串", ab[1] === "");
ok("align 显式失败→空串", alignBatchResponse(["x"], { items: [{ id: "0", translation: { ok: false, translated_text: "不该用" } }] })[0] === "");
ok("align 兼容 text 字段", alignBatchResponse(["x"], { items: [{ id: "0", translation: { text: "TX" } }] })[0] === "TX");
ok("align 空响应→全空", alignBatchResponse(["x", "y"], null).join("|") === "|");
ok("align 长度对齐请求", alignBatchResponse(["x", "y", "z"], { items: [] }).length === 3);

// ── planBatches ───────────────────────────────────────────────────────────────
const pb = planBatches([1, 2, 3, 4, 5], 2);
ok("planBatches 切批", pb.length === 3 && pb[0].length === 2 && pb[2].length === 1);
ok("planBatches 空", planBatches([], 3).length === 0);

// ── createBudget（注入时钟,滑动窗）────────────────────────────────────────────
let clock = 1000;
const budget = createBudget({ limit: 10, windowMs: 1000, now: () => clock });
ok("budget 首次消费", budget.tryConsume(6) === true && budget.remaining() === 4);
ok("budget 超额拒绝", budget.tryConsume(6) === false);
clock += 1500; // 窗口滑走
ok("budget 窗口滑走后恢复", budget.tryConsume(6) === true);
ok("budget 无限档不拦", createBudget({ limit: 0 }).tryConsume(9999) === true);

// ── 调度器（异步:语言守卫/缓存/inflight 去重/批处理/意义/错误/预算）────────────
(async () => {
  // 传输 stub:记录每次调用,回 "T:"+原文
  function makeTransport() {
    const calls = [];
    const fn = async (texts) => { calls.push(texts.slice()); return texts.map((t) => "T:" + t); };
    fn.calls = calls;
    return fn;
  }

  // 语言守卫:中文文本 target=zh 直接跳过,不进传输
  {
    const tr = makeTransport();
    const s = createTranslateScheduler({ translateBatch: tr, targetLang: "zh" });
    const r = await s.request("你好呀");
    ok("sched 已是目标语跳过", r.reason === "already_target" && r.source === "guard");
    await s.flush();
    ok("sched 守卫命中不进传输", tr.calls.length === 0 && s.stats().skipped_lang === 1);
  }

  // 缓存:同句翻两次,传输只调一次
  {
    const tr = makeTransport();
    const s = createTranslateScheduler({ translateBatch: tr, targetLang: "zh" });
    const p1 = s.request("hello");
    await s.flush();
    const r1 = await p1;
    ok("sched 首译走网络", r1.source === "net" && r1.translated === "T:hello" && r1.meaningful === true);
    const r2 = await s.request("hello"); // 命中缓存,无需 flush
    ok("sched 二次命中缓存", r2.source === "cache" && r2.translated === "T:hello");
    ok("sched 传输仅一次", tr.calls.length === 1 && s.stats().cache_hits === 1);
  }

  // inflight 去重:同句在途共享同一 Promise
  {
    const tr = makeTransport();
    const s = createTranslateScheduler({ translateBatch: tr, targetLang: "zh" });
    const a = s.request("apple");
    const b = s.request("apple");
    ok("sched inflight 同 Promise", a === b);
    await s.flush();
    await a;
    ok("sched inflight 只翻一次", tr.calls[0].length === 1);
  }

  // 批处理:多条不同文本合成一批
  {
    const tr = makeTransport();
    const s = createTranslateScheduler({ translateBatch: tr, targetLang: "zh", maxBatch: 16 });
    const ps = [s.request("one"), s.request("two"), s.request("three")];
    await s.flush();
    const rs = await Promise.all(ps);
    ok("sched 合并成一批", tr.calls.length === 1 && tr.calls[0].length === 3);
    ok("sched 批内逐条回填", rs[2].translated === "T:three");
  }

  // 意义判定:文本非目标语（过守卫）但传输回同文（echo）→ same / 不 meaningful
  {
    const s = createTranslateScheduler({ translateBatch: async (t) => t.slice(), targetLang: "zh" });
    const p = s.request("hello"); // 英文 → 过 zh 守卫 → 进传输 → 传输 echo
    await s.flush();
    const r = await p;
    ok("sched 回同文→same", r.ok === true && r.meaningful === false && r.reason === "same");
  }

  // 错误:传输回空 → ok:false，且清 inflight（可重试）
  {
    const s = createTranslateScheduler({ translateBatch: async () => [], targetLang: "zh" });
    const p = s.request("retry-me");
    await s.flush();
    const r = await p;
    ok("sched 传输空→error", r.ok === false && r.reason === "error");
    const p2 = s.request("retry-me"); // 清了 inflight,应重新入队
    ok("sched 错误后可重试(未命中缓存)", s.stats().queued === 1);
    // 让第二次成功
    s.request("retry-me"); // inflight 去重,不新增
    await s.flush().catch(() => {});
    await p2.catch(() => {});
  }

  // 预算:小额度拦下
  {
    const s = createTranslateScheduler({
      translateBatch: makeTransport(), targetLang: "zh",
      budget: createBudget({ limit: 3 }),
    });
    const r = await s.request("hello"); // 5 字符 > 3
    ok("sched 预算不足拦下", r.ok === false && r.reason === "budget" && s.stats().denied_budget === 1);
  }

  // autoFlush:设去抖窗后无需手动 flush,request 自行结算（生产集成路径）
  {
    const s = createTranslateScheduler({ translateBatch: async (t) => t.map((x) => "T:" + x), targetLang: "zh", autoFlushMs: 5 });
    const r = await s.request("auto flush me"); // 不调 flush(),等去抖窗自动跑
    ok("sched autoFlush 自动结算", r.ok === true && r.translated === "T:auto flush me");
  }

  console.log(`translate-scheduler.test.js: ${pass} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
