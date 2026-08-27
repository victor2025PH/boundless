"use strict";
/* 桌面壳品牌工具（纯函数，便于 node 直跑单测）。
   网络拉取 / electron nativeImage / setIcon 等副作用留在 main.js；
   这里只做：字段归一化、URL 拼接、默认 mark 判定、本地兜底选择。 */

// 出厂兜底品牌。**双语**（2026-08-19 i18n）：此前只有中文，于是英文壳的关于框/窗口
// 标题在「后端 branding 拉不到 + brand.json 缺失」时会蹦出「智聊 / 无界科技」。
// brand.json 本就 zh/en 双列（product.en=ChatX、company.en=BOUNDLESS），兜底与它对齐。
const BRAND_FALLBACK_I18N = Object.freeze({
  zh: Object.freeze({ product: "智聊", company: "无界科技" }),
  zh_hant: Object.freeze({ product: "智聊", company: "無界科技" }),
  en: Object.freeze({ product: "ChatX", company: "BOUNDLESS" }),
});
const BRAND_FALLBACK = Object.freeze({
  product: BRAND_FALLBACK_I18N.zh.product,
  company: BRAND_FALLBACK_I18N.zh.company,
  website: "https://bd2026.cc",
  mark: null,
});

/* 品牌语言列判定（与 shellLang()/i18n_packs.UI_LANGS 对齐）：
   - en 家族 + 扩展语 vi/th/id → 'en'（拉丁品牌名 ChatX 对非中文坐席可读）；
   - zh_hant / zh-TW / zh-HK → 'zh_hant'（無界科技）；
   - 其余（zh / 空 / 未知）→ 'zh'（保守口径：只对白名单语言改变行为）。 */
function _brandLangKey(lang) {
  const l = String(lang || "").trim().toLowerCase().replace(/-/g, "_");
  if (l === "en" || l.indexOf("en_") === 0) return "en";
  if (l === "zh_hant" || l === "zh_tw" || l === "zh_hk") return "zh_hant";
  const base = l.split("_")[0];
  if (base === "vi" || base === "th" || base === "id") return "en";
  return "zh";
}

/** 出厂兜底按语言取（en/vi/th/id → 拉丁列；zh_hant → 繁体列；其余=zh）。 */
function brandFallback(lang) {
  const k = _brandLangKey(lang);
  const v = BRAND_FALLBACK_I18N[k] || BRAND_FALLBACK_I18N.zh;
  return Object.assign({}, BRAND_FALLBACK, v);
}

/** 把后端 /api/admin/branding 响应归一成统一形状；缺字段回落 fallback。 */
function normalizeLiveBrand(d, fallback = BRAND_FALLBACK) {
  if (!d || typeof d !== "object" || d.ok === false) return null;
  const s = (v) => String(v == null ? "" : v).trim();
  return {
    product: s(d.product_name) || fallback.product,
    company: s(d.company_name) || fallback.company,
    website: s(d.website_url) || fallback.website,
    mark: d.brand_mark_url || null,
  };
}

/** /static 相对路径 → 后端绝对 URL；已是 http(s) 原样返回；无 base 时原样返回。 */
function resolveBackendUrl(baseUrl, p) {
  const s = String(p || "").trim();
  if (!s) return "";
  if (/^https?:\/\//i.test(s)) return s;
  const base = String(baseUrl || "").replace(/\/+$/, "");
  return base ? `${base}${s.startsWith("/") ? "" : "/"}${s}` : s;
}

/** 是否内置无界默认 mark（默认已由本地图标覆盖，无需远程下载热替换）。 */
function isDefaultMark(mark) {
  return String(mark || "").endsWith("boundless-mark-256.png");
}

/** 本地兜底：config.brand → brand.json → 硬编码默认。
 *  lang（"en" / 其余=zh）决定从 brand.json 取 `product.en` 还是 `product.zh`——
 *  旧实现**只读 zh**，白标包在英文界面下产品名照样是中文（brand.json 早有 en 列，
 *  纯粹没接）。config.brand 是部署方显式覆写（白标客户自己填的名字），语言无关，
 *  仍最高优先；缺 en 时回落 zh 而非兜底常量（宁可显示中文真名，不显示 ChatX 错名）。*/
function pickBrandLocal(configBrand, brandJson, fallback, lang) {
  const fb = fallback || brandFallback(lang);
  const langKey = _brandLangKey(lang);
  const b = configBrand || {};
  if (b.website || b.product) return Object.assign({}, fb, b);
  const j = brandJson || {};
  const p = j.product || {};
  const c = j.company || {};
  const links = j.links || {};
  const assets = j.assets || {};
  if (p.zh || p.en || c.zh || c.en || links.website || assets.mark) {
    // 列缺失回落方向：en→zh；zh_hant→zh→en（宁可简体真名，不显示错语言）
    const pick = (o) => (langKey === "en" ? o.en || o.zh
      : langKey === "zh_hant" ? o.zh_hant || o.zh || o.en
        : o.zh || o.en) || "";
    return {
      product: pick(p) || fb.product,
      company: pick(c) || fb.company,
      website: links.website || fb.website,
      mark: assets.mark || null,
    };
  }
  return Object.assign({}, fb);
}

/** 依「当前生效 mark」与「上次已应用 mark」决策原生图标动作（纯函数，便于单测）。
 *  - custom  : 当前是白标自定义 logo 且与上次不同 → 需下载并热替换。
 *  - default : 当前回到默认/无 mark 但上次是自定义 → 需还原内置图标（白标改回默认）。
 *  - none    : 无变化 → 不动，避免无谓下载/闪烁。 */
function resolveIconAction(currentMark, lastAppliedMark) {
  const cur = currentMark || null;
  const last = lastAppliedMark || null;
  const curIsCustom = !!cur && !isDefaultMark(cur);
  const lastIsCustom = !!last && !isDefaultMark(last);
  if (curIsCustom) return { action: cur !== last ? "custom" : "none", mark: cur };
  if (lastIsCustom) return { action: "default", mark: null };
  return { action: "none", mark: null };
}

/** focus 可能高频触发；节流：距上次检查够久才允许再拉品牌。 */
function shouldCheckBrand(now, lastCheck, minIntervalMs = 3000) {
  return Number(now) - Number(lastCheck || 0) >= minIntervalMs;
}

module.exports = {
  BRAND_FALLBACK,
  BRAND_FALLBACK_I18N,
  brandFallback,
  normalizeLiveBrand,
  resolveBackendUrl,
  isDefaultMark,
  pickBrandLocal,
  resolveIconAction,
  shouldCheckBrand,
};
