/**
 * AI 爬虫准入登记表（实施77 GEO 批次3，2026-08-28）。
 *
 * 立场与内容站相反：内容站怕语料被白嫖所以封 GPTBot/ClaudeBot，**我们是产品方**——
 * 进训练语料 = 模型「天生认识」智聊（比实时检索引用更深更持久），进检索索引 = AI 答案
 * 里的「引用来源」，被用户触发抓取 = 客户把我们的链接丢给 AI 时读得到。三类一律放行。
 *
 * ⚠ 两条施工纪律：
 * 1. **每个显式 UA 组必须重复同一份 disallow**。robots.txt 的组匹配语义是「命中最具体的
 *    那一组，其余组一概不继承」——给 GPTBot 单开一组却漏写 disallow，等于把 gated 高风险页
 *    （lib/isolation.ts）向 AI 爬虫单独敞开。app/robots.txt/route.ts 统一拼装，勿在别处另写。
 * 2. **Bytespider / Sogou 单独成块**（solo:true）。站长社区多次实测这两家在「多 UA 堆叠成
 *    一组」时照爬不误，拆成单 UA 块才生效。当前是放行场景，写法差异无害；保持这个写法是为了
 *    将来真要收紧时改一个字段就生效，而不是届时才发现规则没被读。
 *
 * UA 基准＝2026-08 检索核对：OpenAI / Anthropic / Perplexity / Apple / Meta 官方爬虫文档、
 * Kimi 官方 crawlers 政策页（KimiBot / Kimi-SearchBot / Kimi-User）、豆包 Doubaobot + Bytespider。
 * 名单按「只增不减」维护：出了新 UA 就补进对应分组，robots 路由无需改。
 *
 * ⚠ robots.txt 是必要不充分条件：CDN/WAF 的 bot 管理会在边缘层静默推翻它。
 * bd2026.cc 2026-08-28 实测直连 nginx（响应头无 cf-ray / server=nginx1.24），当前无此风险；
 * 将来若把主站挂到 Cloudflare 之类后面，必须同步关掉「AI 爬虫拦截」预设并复验 access log。
 */

export interface BotGroup {
  /** 生成到 robots.txt 里的组注释：写「为什么放行」，给未来的维护者看。 */
  note: string;
  agents: string[];
  /** true = 每个 UA 各自成块（见文件头纪律 2）。 */
  solo?: boolean;
}

export const AI_BOT_GROUPS: BotGroup[] = [
  {
    note: "OpenAI：GPTBot=训练语料，OAI-SearchBot=ChatGPT 搜索索引（AI 答案的引用池），ChatGPT-User/OAI-AdsBot=用户触发抓取",
    agents: ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "OAI-AdsBot"],
  },
  {
    note: "Anthropic：ClaudeBot=训练，Claude-SearchBot=检索索引，Claude-User=用户触发；anthropic-ai/Claude-Web 为历史 UA，一并放行",
    agents: ["ClaudeBot", "Claude-SearchBot", "Claude-User", "anthropic-ai", "Claude-Web"],
  },
  {
    note: "Google/Gemini：Google-Extended 是训练/接地的控制令牌（非爬虫），放行=允许 Gemini 用我们的内容作答；经典 Googlebot 走 * 组不单列",
    agents: ["Google-Extended", "GoogleOther"],
  },
  {
    note: "Apple：Applebot-Extended 是训练控制令牌（Applebot 本体走 * 组）",
    agents: ["Applebot-Extended"],
  },
  { note: "Perplexity：检索索引 + 用户触发", agents: ["PerplexityBot", "Perplexity-User"] },
  {
    note: "Meta（Llama / Meta AI）",
    agents: ["meta-externalagent", "meta-externalfetcher", "FacebookBot"],
  },
  {
    note: "其余国际 AI：Amazon Rufus/Alexa、Common Crawl（多数开源训练集的上游）、Mistral、Ai2、Cohere、Diffbot 知识图谱、DuckAssist、You.com",
    agents: [
      "Amazonbot",
      "CCBot",
      "MistralAI-User",
      "AI2Bot",
      "cohere-ai",
      "cohere-training-data-crawler",
      "Diffbot",
      "DuckAssistBot",
      "YouBot",
    ],
  },
  {
    note: "国内大模型：豆包(Doubaobot/TikTokSpider)、DeepSeek、Kimi(官方三件套)、腾讯元宝、百度文心(Baiduspider)、华为小艺(PetalBot)、神马、360",
    agents: [
      "Doubaobot",
      "TikTokSpider",
      "DeepSeekBot",
      "KimiBot",
      "Kimi-SearchBot",
      "Kimi-User",
      "MoonshotBot",
      "YuanbaoBot",
      "Baiduspider",
      "PetalBot",
      "Yisouspider",
      "360Spider",
    ],
  },
  {
    note: "字节 Bytespider 与搜狗：实测无视堆叠组，必须单 UA 成块（见 lib/ai-crawlers.ts 纪律 2）",
    agents: ["Bytespider", "Sogou web spider", "Sogou inst spider"],
    solo: true,
  },
];
