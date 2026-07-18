// /growth 结构化数据（Service + FAQPage）。FAQ 内容与页面渲染共用 GROWTH_FAQ。
import { BRAND, type BrandLang } from "./brand";
import { GROWTH_FAQ } from "./growthContent";
import { SITE_URL } from "./site";

export function growthJsonLd(lang: BrandLang) {
  const url = `${SITE_URL}${lang === "zh" ? "" : "/en"}/growth`;
  const products = (["reachx", "chatx"] as const).map((k) => {
    const p = BRAND.products[k];
    return {
      "@type": "Service",
      name: `${p.zh} ${p.en}`,
      description: p.desc[lang],
      provider: { "@type": "Organization", name: BRAND.company.full, url: SITE_URL },
      areaServed: "Worldwide",
      url: `${url}#${k === "reachx" ? "reach" : "chat"}`,
    };
  });
  return {
    "@context": "https://schema.org",
    "@graph": products,
  };
}

export function growthFaqJsonLd(lang: BrandLang) {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: GROWTH_FAQ[lang].map((f) => ({
      "@type": "Question",
      name: f.q,
      acceptedAnswer: { "@type": "Answer", text: f.a },
    })),
  };
}
