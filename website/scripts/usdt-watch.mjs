#!/usr/bin/env node
// TRC20 USDT 到账监听（半自动核销）：轮询 TronGrid，把「到账金额」与待付款订单的
// 唯一付款尾数（pay_amount）精确匹配 → 自动标记订单已到账 + Telegram 通知管理员一键开通。
//
// 服务器 cron（每 2 分钟）：
//   */2 * * * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/usdt-watch.mjs >> /home/ubuntu/usdt-watch.log 2>&1
//
// 依赖环境（读 .env.local）：
//   NEXT_PUBLIC_USDT_ADDR   收款地址（未配置则本脚本静默退出——就绪待启用）
//   TELEGRAM_BOT_TOKEN      管理员通知
//   ADMIN_KEY / TELEGRAM_SETUP_KEY  更新订单状态（调本机 API）
// 可选：TRONGRID_API_KEY（无 key 也可用，限速更严）
import { readFileSync, writeFileSync, existsSync } from "fs";
import path from "path";
import os from "os";

const APP_DIR = process.cwd();
const STATE = path.join(os.homedir(), ".usdt-watch-state.json");
const USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"; // TRON 主网 USDT

// ── 读 .env.local（不依赖 dotenv 包）──
function loadEnv() {
  const env = { ...process.env };
  try {
    // 按 /\r?\n/ 切行：.env.local 常由 Windows 机上传（CRLF），而 JS 正则的 `.`
    // 不匹配 \r —— 旧写法 split("\n") 后 `(.*)$` 在带 \r 的行上整体失配，
    // 所有键都读不出来（2026-07-13 实锤：ADMIN_KEY/SITE_URL 全 undefined，401 收场）。
    for (const line of readFileSync(path.join(APP_DIR, ".env.local"), "utf-8").split(/\r?\n/)) {
      const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
      if (m && !env[m[1]]) env[m[1]] = m[2].trim();
    }
  } catch {}
  return env;
}

const env = loadEnv();
const ADDR = (env.NEXT_PUBLIC_USDT_ADDR || "").trim();
// 演练模式：--mock-tx <json>（内容 [{transaction_id, value(原始 6 位小数单位), block_timestamp}]）
// 跳过 TronGrid 直接喂交易，用于不花真钱的全链路演练；cron 正常运行不受影响。
const mockIdx = process.argv.indexOf("--mock-tx");
const MOCK = mockIdx > 0 ? process.argv[mockIdx + 1] : "";
if (!ADDR && !MOCK) process.exit(0); // 未配置收款地址：就绪待启用，静默退出

const SITE = env.NEXT_PUBLIC_SITE_URL || "http://127.0.0.1:3000";
const KEY = env.ADMIN_KEY || env.TELEGRAM_SETUP_KEY || "";
const BOT = env.TELEGRAM_BOT_TOKEN || "";

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE, "utf-8"));
  } catch {
    return { last_ts: Date.now() - 3600_000, seen: [] }; // 首跑只看最近 1 小时
  }
}

async function tgNotify(text) {
  if (!BOT) return;
  // 管理员 chat 列表由网站维护；走网站的通知不现实（无该 API），直接读 admin-chats store
  let chats = [];
  try {
    const p = env.ADMIN_CHAT_STORE || path.join(os.homedir(), "hualing-leads", "admin_chats.json");
    if (existsSync(p)) {
      const raw = JSON.parse(readFileSync(p, "utf-8"));
      chats = Array.isArray(raw) ? raw : raw?.chats || [];
    }
  } catch {}
  if (env.TELEGRAM_CHAT_ID) chats.push(...env.TELEGRAM_CHAT_ID.split(","));
  for (const chat_id of new Set(chats.map((c) => String(c).trim()).filter(Boolean))) {
    await fetch(`https://api.telegram.org/bot${BOT}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id, text }),
    }).catch(() => {});
  }
}

async function main() {
  const state = loadState();

  // 1) 待付款订单（含唯一尾数）
  const ordersDb = path.join(os.homedir(), "hualing-leads", "orders-db.json");
  if (!existsSync(ordersDb)) return;
  const pending = Object.values(JSON.parse(readFileSync(ordersDb, "utf-8")).orders || {}).filter(
    (o) => o.status === "pending" && o.pay_amount > 0
  );
  if (!pending.length) return;

  // 2) 拉最近 TRC20 入账（TronGrid 主源，TronScan 兜底——公共 API 谁抽风都不至于漏账）
  let txs = [];
  let fetchOk = true;
  if (MOCK) {
    txs = JSON.parse(MOCK);
    console.log(`[usdt-watch] mock mode: ${txs.length} tx injected`);
  } else {
    const got = (await fetchTronGrid().catch((e) => (console.error(`[usdt-watch] ${e.message}`), null)))
      ?? (await fetchTronScan().catch((e) => (console.error(`[usdt-watch] ${e.message}`), null)));
    fetchOk = got !== null;
    txs = got || [];
  }

  async function fetchTronGrid() {
    const url =
      `https://api.trongrid.io/v1/accounts/${ADDR}/transactions/trc20` +
      `?only_confirmed=true&only_to=true&limit=50&contract_address=${USDT_CONTRACT}&min_timestamp=${state.last_ts}`;
    const headers = env.TRONGRID_API_KEY ? { "TRON-PRO-API-KEY": env.TRONGRID_API_KEY } : {};
    const res = await fetch(url, { headers });
    if (!res.ok) throw new Error(`trongrid http ${res.status}`);
    return (await res.json()).data || [];
  }

  async function fetchTronScan() {
    // TronScan 公共接口（无需 key）：字段名不同，归一化成 TronGrid 形状
    const url =
      `https://apilist.tronscanapi.com/api/token_trc20/transfers` +
      `?limit=50&start=0&toAddress=${ADDR}&contract_address=${USDT_CONTRACT}&confirm=true`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`tronscan http ${res.status}`);
    const list = (await res.json()).token_transfers || [];
    console.log(`[usdt-watch] fallback to tronscan (${list.length} tx)`);
    return list
      .filter((t) => (t.block_ts || 0) >= state.last_ts)
      .map((t) => ({ transaction_id: t.transaction_id, value: t.quant, block_timestamp: t.block_ts }));
  }

  // 3) 金额精确匹配（USDT 6 位小数 → 元），命中即标记已到账
  let matched = 0;
  for (const tx of txs) {
    if (state.seen.includes(tx.transaction_id)) continue;
    const amount = Number(tx.value) / 1e6;
    const hit = pending.find((o) => Math.abs(o.pay_amount - amount) < 0.001);
    state.seen.push(tx.transaction_id);
    if (!hit) {
      if (amount >= 1) await tgNotify(`💰 收到 ${amount} USDT（未匹配到订单）\ntx: ${tx.transaction_id}`);
      continue;
    }
    const r = await fetch(`${SITE}/api/admin/order-status`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-setup-key": KEY },
      body: JSON.stringify({ id: hit.id, status: "paid" }),
    }).catch(() => null);
    const ok = r && r.ok;
    matched++;
    await tgNotify(
      `✅ 到账自动核销${ok ? "" : "（改状态失败，请手动）"}\n订单 ${hit.id} · ${hit.plan}\n` +
        `到账 ${amount} USDT = 应付 ${hit.pay_amount}\n联系：${hit.contact}\n` +
        `下一步：签发授权后点「标记已开通」\ntx: ${tx.transaction_id}`
    );
    console.log(`[usdt-watch] matched ${hit.id} <- ${amount} USDT`);
  }

  if (!MOCK) {
    // 只在真实拉链成功时推进水位（两个 API 全挂时不推进，恢复后不漏账）；
    // 演练模式不落任何状态，假 tx 不污染 seen 池。
    if (fetchOk) {
      state.last_ts = Math.max(state.last_ts, ...txs.map((t) => t.block_timestamp || 0), Date.now() - 600_000);
      state.seen = state.seen.slice(-500);
      writeFileSync(STATE, JSON.stringify(state));
    }
  }
  if (matched) console.log(`[usdt-watch] ${matched} order(s) auto-marked paid`);
}

main().catch((e) => console.error("[usdt-watch]", e));
