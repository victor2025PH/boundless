import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";
import { publicPages } from "@/lib/seo";

/** 中英页面对统一带 xhtml:link 语言注解（Next 14.2+ 原生支持），
 *  与各页 metadata 的 hreflang 双保险。页面清单来自 lib/seo.ts 单一数据源；
 *  gated 高风险页（如 /face，见 lib/isolation.ts）不进 sitemap。 */
export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();
  return publicPages().flatMap((p) => {
    const zh = `${SITE_URL}${p.slug || "/"}`;
    if (!p.bilingual) {
      return [{ url: zh, lastModified: now, changeFrequency: p.changeFrequency, priority: p.priority }];
    }
    const en = `${SITE_URL}/en${p.slug}`;
    const languages: Record<string, string> = { "zh-CN": zh, en };
    for (const loc of p.locales ?? []) languages[loc] = `${SITE_URL}/${loc}${p.slug}`;
    languages["x-default"] = zh;
    const rows = [
      { url: zh, lastModified: now, changeFrequency: p.changeFrequency, priority: p.priority, alternates: { languages } },
      { url: en, lastModified: now, changeFrequency: p.changeFrequency, priority: p.enPriority ?? p.priority, alternates: { languages } },
    ];
    for (const loc of p.locales ?? []) {
      rows.push({
        url: `${SITE_URL}/${loc}${p.slug}`,
        lastModified: now,
        changeFrequency: p.changeFrequency,
        priority: p.enPriority ?? p.priority,
        alternates: { languages },
      });
    }
    return rows;
  });
}
