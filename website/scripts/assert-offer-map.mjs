#!/usr/bin/env node
// 回归守卫：lib/offer-map.ts 与 scripts/ledger-lib.mjs 的 ORDER_SKU_MAP 必须逐条一致，
// 且 chatx 三档（entry/team/flagship）齐全（防 autochat-entry 丢单回潮）。
// 纯文本正则解析，无 import 副作用；node scripts/assert-offer-map.mjs。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

/** 从文件文本里抽出 ORDER_SKU_MAP 的 "key": { skuId: "..", productId: ".." } 条目。 */
function extractMap(relPath) {
  const text = readFileSync(join(root, relPath), "utf-8");
  const start = text.indexOf("ORDER_SKU_MAP");
  if (start < 0) throw new Error(`${relPath}: 未找到 ORDER_SKU_MAP`);
  const body = text.slice(start, text.indexOf("};", start));
  const re = /"([\w-]+)"\s*:\s*\{\s*skuId:\s*"([\w-]+)"\s*,\s*productId:\s*"([\w-]+)"\s*\}/g;
  const map = {};
  let m;
  while ((m = re.exec(body)) !== null) map[m[1]] = { skuId: m[2], productId: m[3] };
  return map;
}

function eq(a, b) {
  const ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
  if (ka.join(",") !== kb.join(",")) return false;
  return ka.every((k) => a[k].skuId === b[k].skuId && a[k].productId === b[k].productId);
}

let failed = 0;
const ts = extractMap("lib/offer-map.ts");
const mjs = extractMap("scripts/ledger-lib.mjs");

// 1) 两处映射逐条一致
if (!eq(ts, mjs)) {
  failed++;
  console.error("FAIL: offer-map.ts 与 ledger-lib.mjs 的 ORDER_SKU_MAP 不一致");
  console.error("  offer-map.ts keys:", Object.keys(ts).sort().join(", "));
  console.error("  ledger-lib.mjs keys:", Object.keys(mjs).sort().join(", "));
} else {
  console.log(`OK: 两处 ORDER_SKU_MAP 一致（${Object.keys(ts).length} 条）`);
}

// 2) chatx 三档齐全（防 entry 丢单回潮）
const expect = {
  "autochat-entry": "chatx-entry",
  "autochat-team": "chatx-team",
  "autochat-flagship": "chatx-flagship",
};
for (const [plan, sku] of Object.entries(expect)) {
  if (!ts[plan] || ts[plan].skuId !== sku || ts[plan].productId !== "zhiliao") {
    failed++;
    console.error(`FAIL: offer-map 缺/错 ${plan} → ${sku} (zhiliao)`);
  }
}
if (!failed) console.log("OK: chatx 三档 entry/team/flagship 映射齐全");

process.exit(failed ? 1 : 0);
