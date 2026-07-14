import type { Metadata } from "next";
import { LANDINGS, type LandingKey } from "./landingContent";
import { SITE_URL } from "./site";
import { landingLanguages } from "./seo";

/** 落地页 metadata / JSON-LD 统一生成，zh、en 两个路由各调用一次。 */
export function landingMetadata(product: LandingKey, lang: "zh" | "en"): Metadata {
  const L = LANDINGS[product];
  const path = lang === "zh" ? L.slug : `/en${L.slug}`;
  // hreflang 全量互相回链（含 ko/ja 等小语种版本），语言清单来自 lib/seo.ts 单一数据源。
  const languages = landingLanguages(L.slug);
  return {
    title: L.seo.title[lang],
    description: L.seo.description[lang],
    keywords: L.seo.keywords,
    alternates: { canonical: path, languages },
    openGraph: {
      type: "website",
      url: path,
      title: L.seo.title[lang],
      description: L.seo.description[lang],
      siteName: lang === "zh" ? "无界科技 BOUNDLESS" : "BOUNDLESS",
    },
    twitter: {
      card: "summary_large_image",
      title: L.seo.title[lang],
      description: L.seo.description[lang],
    },
  };
}

export function landingJsonLd(product: LandingKey, lang: "zh" | "en") {
  const L = LANDINGS[product];
  const path = lang === "zh" ? L.slug : `/en${L.slug}`;
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: L.seo.title[lang],
    serviceType: L.productLine[lang],
    description: L.seo.description[lang],
    provider: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
    areaServed: "Global",
    url: `${SITE_URL}${path}`,
  };
}

export function landingFaqJsonLd(product: LandingKey, lang: "zh" | "en") {
  const L = LANDINGS[product];
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: L.faq.map((f) => ({
      "@type": "Question",
      name: f.q[lang],
      acceptedAnswer: { "@type": "Answer", text: f.a[lang] },
    })),
  };
}
