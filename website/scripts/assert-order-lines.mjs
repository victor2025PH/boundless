#!/usr/bin/env node
// 回归守卫（2026-08-19 Token 定价改版重写）：
//  1) chatx-pricing.ts 里全部可购 plan key（autochat-* 订阅 / token-pack-* / translate-workbench）
//     必须出现在 lib/offer-map.ts（否则下单后 resolveOrderSku → null → 无法自动履约）；
//  2) lib/order-lines.ts 禁止手写价格数字（monthly: <字面量>）——一律派生自 chatx-pricing.ts；
//  3) 【跨仓单源闸】chatx-pricing.ts 的挂牌价必须与 platform/licensing/sku_registry.json
//     （products/*/product.yaml 生成）逐一相等——「官网价 ≠ 注册表价」在此直接红灯，
//     把「改价两处一起改」从注释纪律升级为机器断言（registry 不存在时警告跳过，
//     兼容 website 独立部署上下文）。
// 纯文本正则 + JSON 解析，无 import 副作用；node scripts/assert-order-lines.mjs。
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const read = (rel) => readFileSync(join(root, rel), "utf-8");

let failed = 0;

/* ── 1) 可购 plan key 全量 ∈ offer-map ─────────────────────────────────── */

const pricingSrc = read("lib/chatx-pricing.ts");

// 订阅档 key（跳过免费档：free 不产生订单）；2026-08-20 起加购通道 = recharge-*（含新人包）
const planKeys = [...pricingSrc.matchAll(/key:\s*"(autochat-[\w-]+)"/g)].map((m) => m[1]);
const buyablePlans = planKeys.filter((k) => k !== "autochat-free" && k !== "autochat-flex");
const rechargeKeys = [...pricingSrc.matchAll(/key:\s*"(recharge-[\w-]+)"/g)].map((m) => m[1]);
const workbenchKey = (pricingSrc.match(/key:\s*"(translate-workbench)"/) || [])[1];
const buyable = [...new Set([...buyablePlans, ...rechargeKeys, ...(workbenchKey ? [workbenchKey] : [])])];

const mapText = read("lib/offer-map.ts");
const mapBody = mapText.slice(mapText.indexOf("ORDER_SKU_MAP"), mapText.indexOf("};", mapText.indexOf("ORDER_SKU_MAP")));
const mapKeys = new Set([...mapBody.matchAll(/"([\w-]+)"\s*:\s*\{/g)].map((m) => m[1]));

if (buyable.length < 10) {
  failed++;
  console.error(`FAIL: chatx-pricing 可购 key 期望 ≥10（3 订阅 + 5 充值档 + 新人包 + 工作台），实际 ${buyable.length}`);
} else {
  console.log(`OK: chatx-pricing 收集到 ${buyable.length} 个可购 key`);
}
for (const k of buyable) {
  if (!mapKeys.has(k)) {
    failed++;
    console.error(`FAIL: 可购 key ${k} 不在 offer-map → 下单无法自动履约`);
  }
}
if (!failed) console.log("OK: 全部可购 key ∈ offer-map");

/* ── 2) order-lines 禁止手写价格 ───────────────────────────────────────── */

const ol = read("lib/order-lines.ts");
const literalPrice = ol.match(/monthly:\s*\d/);
if (literalPrice) {
  failed++;
  console.error("FAIL: order-lines.ts 出现手写价格（monthly: <数字>）——必须派生自 chatx-pricing.ts");
} else {
  console.log("OK: order-lines 无手写价格，全部派生");
}

/* ── 3) 跨仓单源闸：chatx-pricing.ts ⟺ sku_registry.json 价格逐一相等 ──── */

const regPath = join(root, "..", "platform", "licensing", "sku_registry.json");
if (!existsSync(regPath)) {
  console.warn("WARN: sku_registry.json 不存在（独立部署上下文？），跳过跨仓价格比对");
} else {
  const reg = JSON.parse(readFileSync(regPath, "utf-8"));
  const regPrice = new Map(reg.flat_skus.map((s) => [s.sku_id, String(s.price)]));

  /** 从 chatx-pricing.ts 抽 [skuId → 官网价]：订阅档（skuId + monthly）与充值档（skuId + price）。 */
  const sitePrices = new Map();
  for (const m of pricingSrc.matchAll(/skuId:\s*"([\w-]+)",[\s\S]{0,200}?(?:monthly|price):\s*([\d.]+)/g)) {
    sitePrices.set(m[1], m[2]);
  }
  // 充值档对象形状是 { key, skuId, price, firstBonusPct }（skuId 在 price 前）；订阅是 skuId 后跟 monthly。
  for (const m of pricingSrc.matchAll(/key:\s*"(recharge-[\w-]+)",\s*skuId:\s*"([\w-]+)",\s*price:\s*([\d.]+)/g)) {
    sitePrices.set(m[2], m[3]);
  }

  const mustMatch = [
    // 停售订阅三档（2026-08-21）仍留在比对清单：legacy 台账价与注册表分叉同样是事故
    "chatx-personal", "chatx-pro", "chatx-flagship", "lingox-workbench",
    "recharge-50", "recharge-100", "recharge-200", "recharge-500", "recharge-1000",
    "recharge-5000", "recharge-10000", "recharge-newbie-6",
  ];
  let checked = 0;
  for (const sku of mustMatch) {
    const site = sitePrices.get(sku);
    const regv = regPrice.get(sku);
    if (site == null) {
      failed++;
      console.error(`FAIL: chatx-pricing.ts 抽不到 ${sku} 的价格（源码形状变了？更新本脚本正则）`);
      continue;
    }
    if (regv == null) {
      failed++;
      console.error(`FAIL: sku_registry.json 缺 ${sku}（product.yaml 忘登记？跑 tools/build_sku_registry.py）`);
      continue;
    }
    if (Number(site) !== Number(regv)) {
      failed++;
      console.error(`FAIL: ${sku} 价格分叉：官网 ${site} vs 注册表 ${regv}（改价必须两处同批改）`);
      continue;
    }
    checked++;
  }
  if (checked === mustMatch.length) console.log(`OK: 官网 ⟺ SKU 注册表价格一致（${checked} 个 SKU）`);
}

process.exit(failed ? 1 : 0);
