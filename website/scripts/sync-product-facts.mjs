/**
 * 产品事实源同步脚本：products/*\/product.yaml + platform/licensing/sku_registry.json
 *   → website/lib/generated/product-facts.json
 *
 * 为什么需要它：
 *   官网文案（价格/定位/落地页锚点）必须跟着产品事实走，而事实的单一真相在 monorepo 的
 *   `products/<拼音>/product.yaml`（产品化清单）与 `platform/licensing/sku_registry.json`
 *   （全域 SKU 注册表）。本脚本把两者机械汇入 website 内的一份生成物，供
 *   `scripts/check-content-integrity.mjs`（内容完整性门禁）做价格一致性校验，
 *   与 engines/chengjie 的「ratchet 门禁」方法论对齐：事实变更 → 重新同步 → 门禁守住文案。
 *
 * 用法（在 website/ 下）：
 *   npm run sync:facts
 *
 * 容错约定：
 *   - 某个 product.yaml 缺字段：不崩，字段置 null 并输出 warning（同时写进 JSON 的 warnings 数组）。
 *     「键不存在」记 warning；「键显式写 null」视为产品侧的明确决策（如 zhiliao landing: null），不告警。
 *   - 上游目录/注册表整体缺失（如只检出 website/ 的部署机）：若已有旧生成物则保留并退出 0（优雅降级，
 *     与 sync-brand.mjs 同款语义）；连旧生成物都没有才 exit 1。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { load as yamlLoad } from "js-yaml";

const here = dirname(fileURLToPath(import.meta.url));
const websiteRoot = join(here, "..");
const monorepoRoot = join(websiteRoot, "..");
const productsDir = join(monorepoRoot, "products");
const skuRegistryPath = join(monorepoRoot, "platform", "licensing", "sku_registry.json");
const outDir = join(websiteRoot, "lib", "generated");
const outPath = join(outDir, "product-facts.json");

const warnings = [];
function warn(msg) {
  warnings.push(msg);
  console.warn(`[sync:facts] WARN ${msg}`);
}

// ---- 优雅降级：上游不存在（部署机只带 website/）时用旧生成物 ----
if (!existsSync(productsDir)) {
  if (existsSync(outPath)) {
    console.log(`[sync:facts] 上游不存在 (${productsDir})，保留已生成的 product-facts.json（跳过同步）`);
    process.exit(0);
  }
  console.error(`[sync:facts] 上游目录不存在且无已生成副本: ${productsDir}`);
  process.exit(1);
}

/** 取对象字段；键不存在 → null + warning，键显式为 null → null 不告警。 */
function pick(obj, key, ctx) {
  if (obj == null || typeof obj !== "object") {
    warn(`${ctx}: 期望对象但拿到 ${JSON.stringify(obj)}，字段 ${key} 置 null`);
    return null;
  }
  if (!Object.hasOwn(obj, key)) {
    warn(`${ctx}: 缺字段 ${key}，置 null`);
    return null;
  }
  return obj[key] ?? null;
}

/** name/tagline 这类 {zh,en} 双语对象的规范化。 */
function bilingual(raw, ctx) {
  if (raw == null) return { zh: null, en: null };
  if (typeof raw === "string") {
    warn(`${ctx}: 期望 {zh,en} 对象但拿到字符串，zh/en 同填`);
    return { zh: raw, en: raw };
  }
  return {
    zh: pick(raw, "zh", ctx),
    en: pick(raw, "en", ctx),
  };
}

/** 价格统一成字符串（yaml 里未加引号的 TBD/数字会被解析成标量）。 */
function priceStr(v) {
  return v == null ? null : String(v);
}

// ---- 逐产品读取 product.yaml ----
const products = [];
const productDirs = readdirSync(productsDir, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name)
  .sort();

for (const dir of productDirs) {
  const yamlPath = join(productsDir, dir, "product.yaml");
  if (!existsSync(yamlPath)) {
    warn(`products/${dir}: 无 product.yaml，跳过`);
    continue;
  }
  let doc;
  try {
    doc = yamlLoad(readFileSync(yamlPath, "utf8"));
  } catch (e) {
    warn(`products/${dir}/product.yaml: YAML 解析失败（${e.message}），跳过`);
    continue;
  }
  if (doc == null || typeof doc !== "object") {
    warn(`products/${dir}/product.yaml: 内容为空或非对象，跳过`);
    continue;
  }
  const ctx = `products/${dir}`;

  const compliance = pick(doc, "compliance", ctx) ?? {};
  const website = pick(doc, "website", ctx) ?? {};

  const rawSkus = pick(doc, "skus", ctx);
  const skus = [];
  if (Array.isArray(rawSkus)) {
    rawSkus.forEach((sku, i) => {
      const sctx = `${ctx} skus[${i}]`;
      if (sku == null || typeof sku !== "object") {
        warn(`${sctx}: 非对象条目，跳过`);
        return;
      }
      skus.push({
        id: pick(sku, "id", sctx),
        name: bilingual(Object.hasOwn(sku, "name") ? sku.name : null, `${sctx}.name`),
        unit: pick(sku, "unit", sctx),
        price: priceStr(pick(sku, "price", sctx)),
        currency: pick(sku, "currency", sctx),
        note: pick(sku, "note", sctx),
      });
    });
  } else if (rawSkus != null) {
    warn(`${ctx}: skus 不是数组，置空`);
  }

  products.push({
    id: pick(doc, "id", ctx) ?? dir,
    brand_key: pick(doc, "brand_key", ctx),
    name: bilingual(Object.hasOwn(doc, "name") ? doc.name : null, `${ctx}.name`),
    category: pick(doc, "category", ctx),
    tagline: bilingual(Object.hasOwn(doc, "tagline") ? doc.tagline : null, `${ctx}.tagline`),
    compliance: {
      visibility: pick(compliance, "visibility", `${ctx}.compliance`),
      risk: pick(compliance, "risk", `${ctx}.compliance`),
    },
    website: {
      landing: pick(website, "landing", `${ctx}.website`),
      anchor: pick(website, "anchor", `${ctx}.website`),
    },
    skus,
  });
}

if (products.length === 0) {
  console.error(`[sync:facts] ${productsDir} 下没有可用的 product.yaml`);
  process.exit(1);
}

// ---- sku_registry.json 原样嵌入 ----
let skuRegistry = null;
if (existsSync(skuRegistryPath)) {
  try {
    skuRegistry = JSON.parse(readFileSync(skuRegistryPath, "utf8"));
  } catch (e) {
    warn(`sku_registry.json 解析失败（${e.message}），嵌入 null`);
  }
} else {
  warn(`缺 ${skuRegistryPath}，sku_registry 嵌入 null`);
}

const out = {
  generatedAt: new Date().toISOString(),
  note: "AUTO-GENERATED by scripts/sync-product-facts.mjs — 请勿手改；上游为 products/*/product.yaml 与 platform/licensing/sku_registry.json，改上游后跑 npm run sync:facts。",
  products,
  sku_registry: skuRegistry,
  warnings,
};

mkdirSync(outDir, { recursive: true });
writeFileSync(outPath, JSON.stringify(out, null, 2) + "\n", "utf8");
console.log(
  `[sync:facts] 已生成 lib/generated/product-facts.json（${products.length} 个产品，${products.reduce((n, p) => n + p.skus.length, 0)} 个 SKU，${warnings.length} 条 warning）`
);
