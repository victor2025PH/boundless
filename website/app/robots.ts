import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";
import { GATED_SLUGS } from "@/lib/isolation";

export default function robots(): MetadataRoute.Robots {
  // 合规隔离：gated 高风险页（lib/isolation.ts，如 /face 及其 /en 版本）禁止抓取。
  const gated = GATED_SLUGS.flatMap((slug) => [slug, `/en${slug}`]);
  return {
    rules: { userAgent: "*", allow: "/", disallow: ["/admin", "/api/", "/robot-stage", ...gated] },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
