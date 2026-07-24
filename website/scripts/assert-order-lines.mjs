#!/usr/bin/env node
// 回归守卫：lib/order-lines.ts 的 ChatX/LingoX offer key 必须：
//  1) 全部出现在 lib/offer-map.ts（否则下单后 resolveOrderSku → null → 无法自动履约）；
//  2) 价格从 lib/pricing.ts 同 id offer 派生（防 order-lines 手写数字漂移）；
//  3) translate-team 文案含 3M/300 万字符（与引擎签发额度同源）。
// 纯文本正则，无 import 副作用；node scripts/assert-order-lines.mjs。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const read = (rel) => readFileSync(join(root, rel), "utf-8");

function offerKeysFromOrderLines() {
  const text = read("lib/order-lines.ts");
  const keys = [];
  const re = /key:\s*"(autochat-[\w-]+|translate-[\w-]+)"/g;
  let m;
  while ((m = re.exec(text)) !== null) keys.push(m[1]);
  return keys;
}

function offerMapKeys() {
  const text = read("lib/offer-map.ts");
  const start = text.indexOf("ORDER_SKU_MAP");
  const body = text.slice(start, text.indexOf("};", start));
  const keys = [];
  const re = /"([\w-]+)"\s*:\s*\{/g;
  let m;
  while ((m = re.exec(body)) !== null) keys.push(m[1]);
  return new Set(keys);
}

function pricingPrice(id) {
  const text = read("lib/pricing.ts");
  // 匹配 id: "…" … price: "N" 同对象块（非贪婪到下一个 id 或闭合）
  const re = new RegExp(
    `id:\\s*"${id}"[\\s\\S]*?price:\\s*"(\\d+(?:\\.\\d+)?)"`,
  );
  const m = text.match(re);
  return m ? m[1] : null;
}

function priceOfCall(id) {
  // order-lines 用 priceOf(autochatOffers|translateOffers, "id")——不得出现字面 monthly: 58
  const text = read("lib/order-lines.ts");
  const re = new RegExp(
    `key:\\s*"${id}"[\\s\\S]*?monthly:\\s*priceOf\\(\\w+,\\s*"${id}"\\)`,
  );
  return re.test(text);
}

let failed = 0;
const keys = offerKeysFromOrderLines();
const map = offerMapKeys();

if (keys.length < 6) {
  failed++;
  console.error(`FAIL: order-lines 期望 ≥6 个 chatx/lingox key，实际 ${keys.length}`);
} else {
  console.log(`OK: order-lines 收集到 ${keys.length} 个 offer key`);
}

for (const k of keys) {
  if (!map.has(k)) {
    failed++;
    console.error(`FAIL: order-lines key ${k} 不在 offer-map → 下单无法自动履约`);
  }
  if (!priceOfCall(k)) {
    failed++;
    console.error(`FAIL: ${k} 未用 priceOf(..., "${k}") 派生价格（禁止手写数字）`);
  }
  if (!pricingPrice(k)) {
    failed++;
    console.error(`FAIL: pricing.ts 缺 offer id=${k}`);
  }
}
if (!failed) console.log("OK: 全部 key ∈ offer-map 且价格从 pricing.ts 派生");

const ol = read("lib/order-lines.ts");
if (!/300\s*万字符|3M chars/.test(ol)) {
  failed++;
  console.error("FAIL: translate-team 文案未声明 300万/3M 字符额度（与引擎签发口径对齐）");
} else {
  console.log("OK: translate-team 文案含 300万/3M 字符额度");
}

process.exit(failed ? 1 : 0);
