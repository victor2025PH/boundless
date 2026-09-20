"use strict";
const assert = require("assert");
const {
  BRAND_FALLBACK,
  normalizeLiveBrand,
  resolveBackendUrl,
  isDefaultMark,
  pickBrandLocal,
  resolveIconAction,
  shouldCheckBrand,
} = require("../brand-util.js");
const brandUtil = require("../brand-util.js");

let n = 0;
const t = (cond, msg) => { assert.ok(cond, msg); n++; };

// ── normalizeLiveBrand ──────────────────────────────────────────────
t(normalizeLiveBrand(null) === null, "null → null");
t(normalizeLiveBrand({ ok: false }) === null, "ok:false → null");
{
  const b = normalizeLiveBrand({
    product_name: "星辰", company_name: "星辰科技",
    website_url: "https://star.example", brand_mark_url: "/static/x.png", ok: true,
  });
  t(b.product === "星辰", "product mapped");
  t(b.company === "星辰科技", "company mapped");
  t(b.website === "https://star.example", "website mapped");
  t(b.mark === "/static/x.png", "mark mapped");
}
{
  // 缺字段回落 fallback（product/company/website），mark 缺失 → null
  const b = normalizeLiveBrand({ ok: true });
  t(b.product === BRAND_FALLBACK.product, "missing product → fallback");
  t(b.company === BRAND_FALLBACK.company, "missing company → fallback");
  t(b.website === BRAND_FALLBACK.website, "missing website → fallback");
  t(b.mark === null, "missing mark → null");
}
{
  // 空白串按缺失处理
  const b = normalizeLiveBrand({ product_name: "   ", ok: true });
  t(b.product === BRAND_FALLBACK.product, "blank product → fallback");
}

// ── resolveBackendUrl ───────────────────────────────────────────────
t(resolveBackendUrl("http://127.0.0.1:18799", "/static/x.png") === "http://127.0.0.1:18799/static/x.png", "relative joined");
t(resolveBackendUrl("http://127.0.0.1:18799/", "/static/x.png") === "http://127.0.0.1:18799/static/x.png", "trailing slash trimmed");
t(resolveBackendUrl("http://h", "static/x.png") === "http://h/static/x.png", "adds missing slash");
t(resolveBackendUrl("http://h", "https://cdn/x.png") === "https://cdn/x.png", "absolute passthrough");
t(resolveBackendUrl("", "/static/x.png") === "/static/x.png", "no base → passthrough");
t(resolveBackendUrl("http://h", "") === "", "empty path → empty");

// ── isDefaultMark ───────────────────────────────────────────────────
t(isDefaultMark("/static/brand/boundless-mark-256.png") === true, "default mark detected");
t(isDefaultMark("https://cdn/custom.png") === false, "custom mark not default");
t(isDefaultMark(null) === false, "null mark not default");

// ── pickBrandLocal ──────────────────────────────────────────────────
{
  // config.brand 优先
  const b = pickBrandLocal({ product: "甲", website: "https://a" }, { product: { zh: "乙" } });
  t(b.product === "甲", "config.brand product wins");
  t(b.website === "https://a", "config.brand website wins");
}
{
  // 无 config.brand → brand.json
  const b = pickBrandLocal({}, {
    product: { zh: "智聊" }, company: { zh: "无界科技" },
    links: { website: "https://bd2026.cc" }, assets: { mark: "/static/brand/boundless-mark-256.png" },
  });
  t(b.product === "智聊", "json product");
  t(b.company === "无界科技", "json company");
  t(b.website === "https://bd2026.cc", "json website");
  t(b.mark === "/static/brand/boundless-mark-256.png", "json mark");
}
{
  // 皆空 → 硬编码兜底
  const b = pickBrandLocal({}, null);
  t(b.product === BRAND_FALLBACK.product, "hardcoded fallback product");
  t(b.mark === null, "hardcoded fallback mark null");
}

// ── 语言感知（2026-08-19 i18n）──────────────────────────────────────
// brand.json 早就是 zh/en 双列，旧实现只读 zh → 英文壳照样显示中文产品名。
const REAL_JSON = require("../renderer/brand/brand.json");
{
  const zh = pickBrandLocal({}, REAL_JSON, null, "zh");
  const en = pickBrandLocal({}, REAL_JSON, null, "en");
  t(zh.product === "智聊" && zh.company === "无界科技", "zh 取 brand.json 中文列（行为不变）");
  t(en.product === "ChatX" && en.company === "BOUNDLESS", "en 取 brand.json 英文列");
  t(zh.website === en.website && zh.mark === en.mark, "website/mark 与语言无关");
}
{
  // 缺 en 列时回落该字段的中文真名，而非兜底常量——白标客户的真名比 ChatX 更对
  const b = pickBrandLocal({}, { product: { zh: "某白标" }, company: { zh: "某公司" } }, null, "en");
  t(b.product === "某白标" && b.company === "某公司", "en 缺列回落 zh 真名（不掉回 ChatX）");
}
{
  // config.brand=部署方显式覆写，语言无关，永远最高优先
  const b = pickBrandLocal({ product: "白标X" }, REAL_JSON, null, "en");
  t(b.product === "白标X", "config.brand 覆写不受语言影响");
}
{
  const fbEn = brandUtil.brandFallback("en-US");
  t(fbEn.product === "ChatX" && fbEn.company === "BOUNDLESS", "en-US 家族归 en 兜底");
  t(brandUtil.brandFallback("ja").product === "智聊", "未知语言保守回中文兜底");
  t(brandUtil.brandFallback("").website === BRAND_FALLBACK.website, "兜底 website 不随语言变");
  t(pickBrandLocal({}, null, null, "en").product === "ChatX", "皆空 + en → 英文兜底");
}
{
  // zh_hant P3：繁体列（含 zh-TW/zh-HK 别名）；vi/th/id 归拉丁列
  const hant = pickBrandLocal({}, REAL_JSON, null, "zh_hant");
  t(hant.company === "無界科技" && hant.product === "智聊", "zh_hant 取 brand.json 繁体列");
  t(pickBrandLocal({}, REAL_JSON, null, "zh-TW").company === "無界科技", "zh-TW 别名归繁体");
  t(brandUtil.brandFallback("zh-HK").company === "無界科技", "zh-HK 兜底归繁体");
  t(pickBrandLocal({}, { company: { zh: "某司" } }, null, "zh_hant").company === "某司",
    "繁体缺列回落 zh 真名（不掉回英文）");
  t(pickBrandLocal({}, REAL_JSON, null, "vi").product === "ChatX", "vi 归拉丁品牌列");
}

// ── resolveIconAction ───────────────────────────────────────────────
const DEF = "/static/brand/boundless-mark-256.png";
const CUST = "https://cdn/custom.png";
{
  // 首次遇自定义 logo（上次无）→ 下载热替换
  const a = resolveIconAction(CUST, null);
  t(a.action === "custom" && a.mark === CUST, "custom first apply");
}
{
  // 自定义未变 → 不动
  t(resolveIconAction(CUST, CUST).action === "none", "custom unchanged → none");
}
{
  // 自定义换成另一个自定义 → 再次热替换
  const a = resolveIconAction("https://cdn/other.png", CUST);
  t(a.action === "custom" && a.mark === "https://cdn/other.png", "custom → other custom");
}
{
  // 白标改回默认（上次是自定义）→ 还原内置
  const a = resolveIconAction(DEF, CUST);
  t(a.action === "default" && a.mark === null, "custom → default reverts");
  t(resolveIconAction(null, CUST).action === "default", "null after custom → default revert");
}
{
  // 默认→默认 / null→null（从未自定义过）→ 不动（窗口创建时已是本地默认图标）
  t(resolveIconAction(DEF, null).action === "none", "default with no prior → none");
  t(resolveIconAction(null, null).action === "none", "null with no prior → none");
  t(resolveIconAction(DEF, DEF).action === "none", "default→default → none");
}

// ── shouldCheckBrand ────────────────────────────────────────────────
t(shouldCheckBrand(10000, 0) === true, "first check (lastCheck 0) allowed");
t(shouldCheckBrand(5000, 3000, 3000) === false, "within interval → skip");
t(shouldCheckBrand(6000, 3000, 3000) === true, "exactly interval → allowed");
t(shouldCheckBrand(1.7e12, undefined) === true, "undefined lastCheck (real now) → allowed");

console.log(`brand-util.test.js: ${n} passed`);
