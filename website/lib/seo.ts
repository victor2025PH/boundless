import { SITE_URL } from "./site";
import { isGatedSlug } from "./isolation";

/** 可收录页面注册表：sitemap.xml 与 IndexNow 推送共用的单一数据源。
 *  bilingual=true 表示存在 /en 对应版本（hreflang 成对）。 */
export interface SitePage {
  slug: string; // "" 表示首页
  bilingual: boolean;
  /** zh/en 之外的额外语言版本（路由 /<locale><slug>），如 ["ko", "ja"] */
  locales?: string[];
  changeFrequency: "weekly" | "monthly" | "yearly";
  priority: number;
  enPriority?: number;
}

export const SITE_PAGES: SitePage[] = [
  { slug: "", bilingual: true, changeFrequency: "weekly", priority: 1, enPriority: 0.9 },
  { slug: "/voice", bilingual: true, locales: ["ko", "ja"], changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/face", bilingual: true, changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/interpreting", bilingual: true, changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/growth", bilingual: true, changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/order", bilingual: true, changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/download", bilingual: true, changeFrequency: "weekly", priority: 0.8, enPriority: 0.7 },
  { slug: "/manual", bilingual: true, changeFrequency: "monthly", priority: 0.6, enPriority: 0.5 },
  { slug: "/videos", bilingual: true, changeFrequency: "weekly", priority: 0.6, enPriority: 0.5 },
  { slug: "/brand", bilingual: true, changeFrequency: "monthly", priority: 0.5, enPriority: 0.45 },
  { slug: "/privacy", bilingual: true, changeFrequency: "yearly", priority: 0.3 },
  { slug: "/terms", bilingual: true, changeFrequency: "yearly", priority: 0.3 },
];

/** 合规隔离：主站对外可见的页面 = SITE_PAGES 去掉 gated（高风险）slug。
 *  gated 页面（如 /face）仍保留在 SITE_PAGES 供直达访问与 hreflang 查询，
 *  但不进 sitemap / IndexNow。gated 清单见 lib/isolation.ts。 */
export function publicPages(): SitePage[] {
  return SITE_PAGES.filter((p) => !isGatedSlug(p.slug));
}

/** 全部可收录 URL（绝对地址），zh + en + 额外语种展平；gated slug 已剔除。 */
export function indexableUrls(): string[] {
  return publicPages().flatMap((p) => {
    const zh = `${SITE_URL}${p.slug || "/"}`;
    const urls = p.bilingual ? [zh, `${SITE_URL}/en${p.slug}`] : [zh];
    for (const loc of p.locales ?? []) urls.push(`${SITE_URL}/${loc}${p.slug}`);
    return urls;
  });
}

/** 页面的完整 hreflang 语言映射（相对路径）。所有语言版本必须互列全量 alternates，
 *  metadata 与 sitemap 共用，保证 Google 要求的互相回链。 */
export function landingLanguages(slug: string): Record<string, string> {
  const page = SITE_PAGES.find((p) => p.slug === slug);
  const languages: Record<string, string> = { "zh-CN": slug || "/", en: `/en${slug}` };
  for (const loc of page?.locales ?? []) languages[loc] = `/${loc}${slug}`;
  languages["x-default"] = slug || "/";
  return languages;
}

/** IndexNow 公开验证 key：协议要求 key 可被公网读取（/<key>.txt），不属于机密。
 *  轮换时同步替换 public/<key>.txt 文件名与内容。 */
export const INDEXNOW_KEY = "556d4065bf0794fb994c2968f75c6a04";
