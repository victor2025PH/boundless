#!/usr/bin/env node
/**
 * AI 网关近 24h 耗时报表（Q-14 #262 C，2026-09-09）。
 *
 * 数据源：
 *   1. lib/ai-gateway.ts logGateway 落点 `<LEADS_DIR|~/hualing-leads>/ai-gateway.jsonl`（+ .1 轮转）
 *      —— ev:"chat"（非 vision）逐条：ms / status / model / key / prompt_chars（1.0.79 前的旧行没 model，归 "legacy"）
 *   2. nginx access.log（+ .log.1）里 `POST /api/ai/v1/chat/completions` 的状态码 —— 499（客户端先断）
 *      与 5xx 只有 nginx 看得见（应用层根本不知道客户端已经走了）。需 sudo 读；读不到就标 n/a。
 *
 * 输出：一行摘要（默认）/ --json 机读 / --md 多行。不写任何文件；cron 包装见 ai-gateway-report-cron.sh。
 *
 *   node scripts/ai-gateway-report.mjs            # 一行摘要
 *   node scripts/ai-gateway-report.mjs --md       # 多行（TG 卡片正文）
 *   node scripts/ai-gateway-report.mjs --hours 6  # 换窗口
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

const args = process.argv.slice(2);
const flag = (n) => args.includes(n);
const opt = (n, d) => {
  const i = args.indexOf(n);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};
const HOURS = Number(opt("--hours", "24")) || 24;
const NOW = Date.now();
const SINCE = NOW - HOURS * 3600 * 1000;

const DATA_DIR = process.env.LEADS_DIR || path.join(os.homedir(), "hualing-leads");
const GW_LOG = process.env.AI_GATEWAY_LOG || path.join(DATA_DIR, "ai-gateway.jsonl");
const NGINX_LOG = process.env.NGINX_ACCESS_LOG || "/var/log/nginx/access.log";
const CHAT_PATH = "/api/ai/v1/chat/completions";

function pct(arr, q) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(s.length * q))];
}

// ── 1. 网关 JSONL ──────────────────────────────────────────────────────────
function readLines(file) {
  try {
    return fs.readFileSync(file, "utf8").split("\n").filter(Boolean);
  } catch {
    return [];
  }
}
const rows = [...readLines(GW_LOG + ".1"), ...readLines(GW_LOG)];
const byModel = new Map();
// B5（2026-09-11）：真 token（route.ts 起落 pt/ct/cache_hit/cache_miss/purpose）——
// 成本从「按字符估」变可对账；缓存命中率是 1M 档能不能用得起的唯一前提。
const tok = { n: 0, pt: 0, ct: 0, hit: 0, miss: 0, reasoning: 0, in_chars: 0 };
const byPurpose = new Map();
let cooldowns = 0;
let upstreamFail = 0;
let backupHits = 0;
// ChatX 27B 中继（2026-09-18）：办公室 173 vLLM 经隧道，GPU 串行 + 网关并发闸 CHATX_MAX_INFLIGHT。
// 要回答的只有一个问题——「并发上限该不该抬 / 要不要第二台中继」：
//   busy（闸拒）多 + p95 低 → 173 还有余量，抬闸；busy 多 + p95 高 → 173 饱和，加中继或限流；
//   busy 少 + p95 高 → 不是并发问题，是 173 自己慢（别的负载）。route.ts 落 ev:"reject" why:chatx_*。
const cx = { ok: 0, fail: 0, busy: 0, unavailable: 0, ms: [] };
for (const ln of rows) {
  let r;
  try {
    r = JSON.parse(ln);
  } catch {
    continue;
  }
  const t = Date.parse(r.t || "");
  if (!Number.isFinite(t) || t < SINCE) continue;
  if (r.ev === "cooldown") {
    cooldowns++;
    continue;
  }
  if (r.ev === "reject" && String(r.why || "").startsWith("chatx_")) {
    if (r.why === "chatx_busy") cx.busy++;
    else cx.unavailable++;
    continue;
  }
  if (r.ev === "upstream_fail") {
    upstreamFail++;
  }
  if (r.ev !== "chat" && r.ev !== "upstream_fail" && r.ev !== "chatx_fail") continue;
  if (r.vision) continue;
  const m = String(r.model || "legacy");
  const slot = byModel.get(m) || { n: 0, ok: 0, fail: 0, ms: [], prompt: [], backup: 0 };
  slot.n++;
  const failed = r.ev === "upstream_fail" || r.ev === "chatx_fail" ||
    !(r.ok === 1 || (r.ok === undefined && Number(r.status) === 200));
  if (failed) slot.fail++;
  else slot.ok++;
  if (m === "chatx") {
    if (failed) cx.fail++;
    else cx.ok++;
    if (Number.isFinite(Number(r.ms))) cx.ms.push(Number(r.ms));
  }
  if (Number.isFinite(Number(r.ms))) slot.ms.push(Number(r.ms));
  if (Number.isFinite(Number(r.prompt_chars ?? r.in))) slot.prompt.push(Number(r.prompt_chars ?? r.in));
  if (r.key === "backup") {
    slot.backup++;
    backupHits++;
  }
  byModel.set(m, slot);
  // 真 usage 汇总（只算带 pt 的成功行；老流水无该字段自然不进）
  const pt = Number(r.pt), ct = Number(r.ct);
  if (r.ev === "chat" && Number.isFinite(pt) && pt > 0) {
    tok.n++;
    tok.pt += pt;
    tok.ct += Number.isFinite(ct) ? ct : 0;
    tok.hit += Number(r.cache_hit) || 0;
    tok.miss += Number(r.cache_miss) || 0;
    tok.reasoning += Number(r.reasoning) || 0;
    tok.in_chars += Number(r.prompt_chars ?? r.in) || 0;
    const pu = String(r.purpose || "unknown");
    const ps = byPurpose.get(pu) || { n: 0, pt: 0, ct: 0 };
    ps.n++;
    ps.pt += pt;
    ps.ct += Number.isFinite(ct) ? ct : 0;
    byPurpose.set(pu, ps);
  }
}

// ── 2. nginx 499 / 5xx ─────────────────────────────────────────────────────
// access.log 常规格式：`ip - - [09/Sep/2026:14:55:47 +0800] "POST /api/ai/v1/chat/completions HTTP/1.1" 200 347 ...`
const MON = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
function nginxTs(s) {
  const m = /\[(\d{2})\/(\w{3})\/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{2})(\d{2})\]/.exec(s);
  if (!m) return NaN;
  const off = (Number(m[7]) * 60 + Number(m[8]) * Math.sign(Number(m[7]) || 1)) * 60 * 1000;
  return Date.UTC(Number(m[3]), MON[m[2]], Number(m[1]), Number(m[4]), Number(m[5]), Number(m[6])) - off;
}
let ng = { available: false, total: 0, s200: 0, s499: 0, s5xx: 0, s4xx: 0 };
function readNginx(file) {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    try {
      return execFileSync("sudo", ["-n", "cat", file], { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
    } catch {
      return "";
    }
  }
}
if (!flag("--no-nginx")) {
  const text = readNginx(NGINX_LOG + ".1") + "\n" + readNginx(NGINX_LOG);
  if (text.trim()) {
    ng.available = true;
    for (const ln of text.split("\n")) {
      if (!ln.includes(CHAT_PATH)) continue;
      const t = nginxTs(ln);
      if (!Number.isFinite(t) || t < SINCE) continue;
      const m = /"\s(\d{3})\s\d+/.exec(ln) || /HTTP\/[\d.]+"\s(\d{3})/.exec(ln);
      if (!m) continue;
      const code = Number(m[1]);
      ng.total++;
      if (code === 200) ng.s200++;
      else if (code === 499) ng.s499++;
      else if (code >= 500) ng.s5xx++;
      else if (code >= 400) ng.s4xx++;
    }
  }
}

// ── 3. 汇总 ───────────────────────────────────────────────────────────────
const models = [...byModel.entries()]
  .map(([model, s]) => ({
    model,
    n: s.n,
    ok_rate: s.n ? +(s.ok / s.n).toFixed(4) : 0,
    p50_ms: pct(s.ms, 0.5),
    p95_ms: pct(s.ms, 0.95),
    max_ms: s.ms.length ? Math.max(...s.ms) : 0,
    over_40s: s.ms.filter((x) => x > 40000).length,
    prompt_chars_p50: pct(s.prompt, 0.5),
    backup: s.backup,
  }))
  .sort((a, b) => b.n - a.n);
const totalN = models.reduce((a, m) => a + m.n, 0);
const tokens = {
  calls_with_usage: tok.n,
  prompt_tokens: tok.pt,
  completion_tokens: tok.ct,
  cache_hit_tokens: tok.hit,
  cache_miss_tokens: tok.miss,
  reasoning_tokens: tok.reasoning,
  cache_hit_rate: tok.hit + tok.miss > 0 ? +(tok.hit / (tok.hit + tok.miss)).toFixed(4) : null,
  tokens_per_char: tok.in_chars > 0 ? +(tok.pt / tok.in_chars).toFixed(3) : null,
  prompt_per_call: tok.n ? Math.round(tok.pt / tok.n) : 0,
  by_purpose: [...byPurpose.entries()]
    .map(([purpose, s]) => ({ purpose, n: s.n, prompt_tokens: s.pt, completion_tokens: s.ct,
      share: tok.pt + tok.ct ? +((s.pt + s.ct) / (tok.pt + tok.ct)).toFixed(4) : 0 }))
    .sort((a, b) => b.prompt_tokens + b.completion_tokens - a.prompt_tokens - a.completion_tokens),
};
// ChatX 容量裁决（阈值可 env 调：CHATX_BUSY_RATE_HI 默认 5%，CHATX_P95_HI 默认 15s）
const CX_BUSY_HI = Number(process.env.CHATX_BUSY_RATE_HI || 0.05);
const CX_P95_HI = Number(process.env.CHATX_P95_HI_MS || 15000);
const cxAttempts = cx.ok + cx.fail + cx.busy + cx.unavailable;
const cxBusyRate = cxAttempts ? cx.busy / cxAttempts : 0;
const cxP95 = pct(cx.ms, 0.95);
let cxVerdict;
if (!cxAttempts) cxVerdict = "no_calls";
else if (cxBusyRate >= CX_BUSY_HI && cxP95 < CX_P95_HI) cxVerdict = "raise_inflight";
else if (cxBusyRate >= CX_BUSY_HI) cxVerdict = "saturated_add_relay";
else if (cxP95 >= CX_P95_HI * 2) cxVerdict = "relay_slow_not_busy";
else cxVerdict = "ok";
const CX_VERDICT_TEXT = {
  no_calls: "窗口内无 ChatX 调用",
  raise_inflight: "并发闸常满但 173 不慢 → 可抬 CHATX_MAX_INFLIGHT",
  saturated_add_relay: "并发闸常满且 p95 高 → 173 饱和，加第二台中继或限流",
  relay_slow_not_busy: "并发未满但 p95 高 → 不是闸的问题，是 173 自身慢（查同机其它负载）",
  ok: "并发上限够用",
};
const chatx = {
  attempts: cxAttempts,
  ok: cx.ok,
  fail: cx.fail,
  busy: cx.busy,
  unavailable: cx.unavailable,
  busy_rate: cxAttempts ? +cxBusyRate.toFixed(4) : null,
  p50_ms: pct(cx.ms, 0.5),
  p95_ms: cxP95,
  max_ms: cx.ms.length ? Math.max(...cx.ms) : 0,
  verdict: cxVerdict,
  verdict_text: CX_VERDICT_TEXT[cxVerdict],
};

const out = {
  window_h: HOURS,
  generated_at: new Date(NOW).toISOString(),
  gateway_log: GW_LOG,
  chat_calls: totalN,
  upstream_fail: upstreamFail,
  cooldowns,
  backup_hits: backupHits,
  models,
  chatx,
  tokens,
  nginx: {
    ...ng,
    rate_499: ng.total ? +(ng.s499 / ng.total).toFixed(4) : null,
    rate_5xx: ng.total ? +(ng.s5xx / ng.total).toFixed(4) : null,
  },
};

const pc = (x) => (x == null ? "n/a" : (x * 100).toFixed(1) + "%");
const sec = (ms) => (ms / 1000).toFixed(1) + "s";
const modelLine = models.length
  ? models.map((m) => `${m.model}: n=${m.n} ok=${pc(m.ok_rate)} p50=${sec(m.p50_ms)} p95=${sec(m.p95_ms)} max=${sec(m.max_ms)} >40s=${m.over_40s}${m.backup ? ` backup=${m.backup}` : ""}`).join(" | ")
  : "no chat calls in window";
const nginxLine = ng.available
  ? `nginx: total=${ng.total} 499=${ng.s499} (${pc(out.nginx.rate_499)}) 5xx=${ng.s5xx} (${pc(out.nginx.rate_5xx)})`
  : "nginx: n/a (access.log unreadable)";
const fmtK = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n));
const tokenLine = tokens.calls_with_usage
  ? `tokens: in=${fmtK(tokens.prompt_tokens)} out=${fmtK(tokens.completion_tokens)} cache_hit=${pc(tokens.cache_hit_rate)} ` +
    `in/call=${tokens.prompt_per_call}${tokens.reasoning_tokens ? ` reasoning=${fmtK(tokens.reasoning_tokens)}` : ""} | ` +
    tokens.by_purpose.slice(0, 5).map((p) => `${p.purpose}=${pc(p.share)}`).join(" ")
  : "tokens: n/a (流水尚无 usage 字段)";
const chatxLine = chatx.attempts
  ? `chatx: attempts=${chatx.attempts} ok=${chatx.ok} fail=${chatx.fail} busy=${chatx.busy} (${pc(chatx.busy_rate)}) unavailable=${chatx.unavailable} p50=${sec(chatx.p50_ms)} p95=${sec(chatx.p95_ms)} → ${chatx.verdict}`
  : "chatx: no calls";
const oneLine = `[ai-gw ${HOURS}h] calls=${totalN} upstream_fail=${upstreamFail} cooldowns=${cooldowns} | ${modelLine} | ${chatxLine} | ${tokenLine} | ${nginxLine}`;

if (flag("--json")) {
  console.log(JSON.stringify(out, null, 2));
} else if (flag("--md")) {
  console.log(`AI 网关近 ${HOURS}h（${new Date(NOW).toISOString().slice(0, 16).replace("T", " ")} UTC）`);
  console.log(`调用 ${totalN} · 上游失败 ${upstreamFail} · 慢模型冷却 ${cooldowns} 次 · 切备用 ${backupHits} 次`);
  for (const m of models) {
    console.log(`· ${m.model}：n=${m.n} 成功 ${pc(m.ok_rate)} p50 ${sec(m.p50_ms)} p95 ${sec(m.p95_ms)} 最慢 ${sec(m.max_ms)} 超40s ${m.over_40s} 次 prompt中位 ${m.prompt_chars_p50} 字`);
  }
  if (!models.length) console.log("· 窗口内无 chat 调用");
  if (chatx.attempts) {
    console.log(
      `· ChatX 27B 中继：请求 ${chatx.attempts} · 成功 ${chatx.ok} · 上游失败 ${chatx.fail} · 并发闸拒 ${chatx.busy}（${pc(chatx.busy_rate)}）` +
      ` · 中继未开被拒 ${chatx.unavailable} · p50 ${sec(chatx.p50_ms)} p95 ${sec(chatx.p95_ms)} 最慢 ${sec(chatx.max_ms)}`
    );
    console.log(`· ChatX 容量裁决：${chatx.verdict_text}`);
  }
  if (tokens.calls_with_usage) {
    console.log(
      `· 真 token：输入 ${fmtK(tokens.prompt_tokens)} · 输出 ${fmtK(tokens.completion_tokens)} · 缓存命中 ${pc(tokens.cache_hit_rate)}` +
      ` · 每次 prompt ${tokens.prompt_per_call} tok（${tokens.tokens_per_char ?? "?"} tok/字）` +
      (tokens.reasoning_tokens ? ` · 思维链 ${fmtK(tokens.reasoning_tokens)}` : "")
    );
    console.log("· 按用途：" + tokens.by_purpose.map((p) => `${p.purpose} ${pc(p.share)}（${p.n} 次）`).join(" · "));
  }
  console.log(
    ng.available
      ? `· nginx：${ng.total} 次 · 499（客户端先断）${ng.s499} 次 = ${pc(out.nginx.rate_499)} · 5xx ${ng.s5xx} 次 = ${pc(out.nginx.rate_5xx)}`
      : "· nginx：access.log 不可读（499 率 n/a）"
  );
  console.log("口径：p50/p95 取网关整包往返；499 只有 nginx 看得见；客户端 1.0.79 起读超时 60s ≥ 网关预算 55s。");
} else {
  console.log(oneLine);
}
