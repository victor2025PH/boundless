import { SITE_URL } from "@/lib/site";
import { GATED_SLUGS } from "@/lib/isolation";
import { AI_BOT_GROUPS } from "@/lib/ai-crawlers";

/**
 * /robots.txt（实施77 GEO 批次3，2026-08-28）。
 *
 * 从 Next 内建的 `app/robots.ts`（MetadataRoute.Robots）改为手写路由，原因是内建生成器只能
 * 输出 User-agent/Allow/Disallow/Sitemap 四类行——而本批次要表达的三件事它都写不出来：
 * 注释（放行 AI 爬虫的理由，给未来维护者）、Content-Signal 声明、以及按厂商分块的结构。
 *
 * 不变量：
 * - **禁抓清单 DISALLOW 是唯一事实源**，`*` 组与每个 AI 厂商组逐字复用同一份。robots.txt
 *   的组不继承，任何一组漏写 = gated 高风险页（lib/isolation.ts）对该爬虫单独敞开。
 * - public/ 下不得放同名 robots.txt（静态文件会整体覆盖本路由，且绕过 gated 派生逻辑）。
 * - 改完必须重新构建部署才生效（本站是构建产物，不像引擎侧模板热更新）。
 */
export const dynamic = "force-static";

/** 禁抓：后台 / 内部接口 / 调试页 + gated 高风险页（zh 与 /en 成对）。 */
const DISALLOW: string[] = [
  "/admin",
  "/api/",
  "/robot-stage",
  // 安装包与分流器（2026-09-10 下载台账批）：二进制无索引价值，且 GPTBot/Bingbot 抓 exe 只是
  // 白耗 R2/主站流量并污染下载台账。下载页本身（/download/*）照常可抓。
  "/dl/",
  "/downloads/",
  ...GATED_SLUGS.flatMap((slug) => [slug, `/en${slug}`]),
];

function group(agents: string[]): string {
  return [...agents.map((a) => `User-agent: ${a}`), "Allow: /", ...DISALLOW.map((d) => `Disallow: ${d}`)].join("\n");
}

function build(): string {
  const out: string[] = [
    "# bd2026.cc — 无界科技 BOUNDLESS",
    "# 立场：我们是产品方，被抓取是被推荐的前提。训练类 / 检索类 / 用户触发类 AI 爬虫一律放行，",
    "# 唯一不许抓的是后台与 gated 高风险页（见下方 Disallow，源头 lib/isolation.ts）。",
    "# 生成自 app/robots.txt/route.ts；UA 名单见 lib/ai-crawlers.ts。",
    "",
    "User-agent: *",
    // Cloudflare 2025 提出的内容用途声明：搜索索引 / AI 实时引用 / AI 训练，三项全允许。
    // 非标准指令，解析器遇到未知行会跳过，无副作用。
    "Content-Signal: search=yes, ai-input=yes, ai-train=yes",
    "Allow: /",
    ...DISALLOW.map((d) => `Disallow: ${d}`),
  ];

  for (const g of AI_BOT_GROUPS) {
    out.push("", `# ${g.note}`);
    if (g.solo) {
      for (const agent of g.agents) out.push("", group([agent]));
    } else {
      out.push(group(g.agents));
    }
  }

  out.push("", `Sitemap: ${SITE_URL}/sitemap.xml`, "");
  return out.join("\n");
}

export function GET(): Response {
  return new Response(build(), {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=3600, stale-while-revalidate=86400",
    },
  });
}
