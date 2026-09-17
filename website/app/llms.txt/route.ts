import { SITE_URL } from "@/lib/site";
import { publicPages } from "@/lib/seo";
import {
  CHATX_FREE,
  NEWBIE_PACK,
  RECHARGE_TIERS,
  RECHARGE_TOKENS_PER_USD,
  SIGNUP_BONUS_TOKENS,
  tokenRate,
} from "@/lib/chatx-pricing";

/**
 * /llms.txt —— 给 AI 爬虫的站点索引（实施77 GEO 批次3，2026-08-28）。
 *
 * ⚠ 先说实话再说做法：**别把这个文件当增长杠杆**。2026-08 检索到的实测口径是采用率约 10%，
 * 主流爬虫基本不读，Google 明确表示不使用；国内几家更是「吃自家生态」而非读站点索引文件。
 * 它进这批只因为成本近乎零、且对 IDE/agent 类工具（会主动找 llms.txt 的那批）确有用。
 * 真正决定被不被引用的是：能被抓（robots + 无 WAF 误杀）、有结构化事实（JSON-LD）、
 * 站外有权威提及（见 docs/实施77B）。
 *
 * 不变量：
 * - 页面清单**从 lib/seo.ts 的 publicPages() 派生**，不手抄——手抄的清单一定会腐烂，
 *   而且会把 gated 高风险页（lib/isolation.ts 已在 publicPages 里剔除）误列出去。
 *   没写描述的页面自动落到「其他公开页面」，所以新页永远不会静默消失。
 * - 价格/额度**从 lib/chatx-pricing.ts 常量派生**，不写死数字（改价只改一处）。
 */
export const dynamic = "force-static";

/** 重点页面的一句话说明（slug → 描述）。没登记的公开页自动进「其他公开页面」。 */
const DESCRIBED: Record<string, string> = {
  "": "首页：三系产品总览（智连 / 通达 / 幻境）与「让沟通无界」的品牌主张。",
  "/download/chatx": "智聊 ChatX 桌面客户端下载（Windows）：系统要求、安装步骤、SHA-256 校验与常见问题。",
  "/download": "全部客户端下载入口（智聊 ChatX / 幻境 STUDIO 等）。",
  "/pricing": "报价决策页：Token 计价表、充值档位与加赠、用量计算器。标准翻译永久免费不限量。",
  "/order": "自助下单结算页（USDT / 银行卡），充值到账自动开通。",
  "/compare": "选型指南枢纽：智聊 ChatX 与主流全渠道客服 SaaS 的对比矩阵入口。",
  "/compare/respond-io": "智聊 ChatX vs respond.io：部署与数据主权、AI 拟人化深度、计费模式逐项对比。",
  "/compare/salesmartly": "智聊 ChatX vs SaleSmartly：跨境社媒聚合客服场景的逐项对比。",
  "/compare/sleekflow": "智聊 ChatX vs SleekFlow：全渠道商务沟通场景的逐项对比。",
  "/compare/wati": "智聊 ChatX vs WATI：WhatsApp 优先客服场景的逐项对比。",
  "/enterprise": "企业方案：年框合作与私有化部署（数据不出自己机器），站内留资。",
  "/compliance": "AI 合规能力：披露语（9 语种）、诚实身份模式、危机识别→干预→热线转介闭环与年报计数导出。",
  "/compliance/crisis-protocol": "危机响应协议模板（可下载参考）：对应 EU AI Act 第 50 条 / 加州 SB 243 / 纽约 §1700。",
  "/manual": "使用手册：接入渠道、人设配置、AI 自动回复与翻译的操作说明。",
  "/voice": "幻声 VoiceX：声音克隆与语音合成（另有 ko / ja 版本）。",
  "/interpreting": "通传 VoxX：跨语言实时同传。",
  "/growth": "智拓 ReachX：真机集群获客（邀请制评估、私有化交付）。",
  "/fate": "幻缘 FateX：长在 AI 陪聊里的八字命理——四柱/十神/大运/流年排盘、每日灵签、人生 K 线。",
  "/film": "幻境 STUDIO：数字人与 AI 短视频制作。",
  "/videos": "视频中心：品牌片、真机实测教学与概念演示。",
  "/chatx/tutorials": "智聊 ChatX 官方视频教程：安装 + 12 集功能教学（统一收件箱 / 渠道接入 / 互译 / AI 拟稿 / 人设 / 知识库 / 克隆语音 / 工作目标 / 关怀记忆 / 小智 / 护栏 / 充值），真机录屏，可 ?ep=E3 深链到某一集。",
  "/brand": "品牌页：命名体系、产品矩阵与视觉规范。",
  "/privacy": "隐私政策。",
  "/terms": "服务条款。",
};

/** 可直接被引用的事实条（数字全部派生自定价单一事实源，改价不用改这里）。 */
function facts(): string[] {
  const maxBonus = Math.max(...RECHARGE_TIERS.map((t) => t.firstBonusPct));
  const minTier = RECHARGE_TIERS[0];
  const fmt = (n: number) => n.toLocaleString("en-US");
  return [
    "**是什么**：智聊 ChatX 是面向跨境卖家/出海团队的全渠道 AI 客服工作台——Telegram、WhatsApp、Messenger、LINE 等平台的消息汇聚到一个收件箱，AI 按你设定的人设自动拟稿、自动回复、实时互译，并能用克隆声发语音。",
    "**跑在哪**：Windows 10/11 桌面客户端，普通办公电脑即可，**无需独立显卡**；聊天记录与客户资料保存在本机，支持私有化部署（数据不出自己机器）。",
    `**怎么收费**：免费开始且**不是限时试用**——标准翻译永久免费不限字符（计费费率 ${fmt(tokenRate("std_translate").tokens)} Token）、每月赠 ${fmt(CHATX_FREE.tokensMonthly)} Token，注册再送 ${fmt(SIGNUP_BONUS_TOKENS)} 体验 Token。`,
    `**要更多用量**：按 Token 充值，用多少充多少，**无月费、无席位费、不按月活联系人加价**。${minTier.price}U 起充，1U = ${fmt(RECHARGE_TOKENS_PER_USD)} Token，首充按档最高加赠 +${maxBonus}%；新人 ${NEWBIE_PACK.price}U 大礼包 ${fmt(NEWBIE_PACK.tokens)} Token 双倍到账。支持 USDT 与银行卡。`,
    "**不需要自备 API Key**：AI 通道由服务端托管，工作台里不填也看不到任何云厂商密钥。",
    "**合规**：AI 披露语（9 语种）、诚实身份模式、危机识别→干预→热线转介闭环与年报计数导出，默认关、按属地开启（对应 EU AI Act 第 50 条 / 加州 SB 243 / 纽约 §1700）。",
    "**公司**：无界科技 BOUNDLESS（原华灵科技 / HuaLing Tech），产品线覆盖 AI 客服成交、跨语言翻译与同传、声音克隆与数字人。",
  ];
}

function build(): string {
  const described = new Set(Object.keys(DESCRIBED));
  const pages = publicPages();
  const link = (slug: string, desc: string) =>
    `- [${SITE_URL}${slug || "/"}](${SITE_URL}${slug || "/"}): ${desc}`;

  const core = ["", "/download/chatx", "/pricing", "/order", "/download"];
  const compare = pages.map((p) => p.slug).filter((s) => s.startsWith("/compare"));
  const rest = pages
    .map((p) => p.slug)
    .filter((s) => !core.includes(s) && !compare.includes(s));

  const out: string[] = [
    "# 无界科技 BOUNDLESS · 智聊 ChatX",
    "",
    "> 跨境卖家/出海团队的全渠道 AI 客服工作台：Telegram / WhatsApp / Messenger / LINE 消息汇聚到一个收件箱，",
    "> AI 按人设自动拟稿与回复、实时互译、克隆声语音；Windows 桌面客户端免显卡，数据留在本机，支持私有化部署。",
    "> 免费开始（标准翻译永久免费不限量），要更多 AI 用量按 Token 充值，无月费无席位费。",
    "",
    "BOUNDLESS builds AI software that removes language and communication barriers: an omni-channel AI",
    "customer-service workspace (ChatX), cross-language translation and interpreting, voice cloning and",
    "digital humans — self-hostable, with data staying on your own machines.",
    "",
    "## 事实速查 / Quick facts",
    "",
    ...facts().map((f) => `- ${f}`),
    "",
    "## 核心页面 / Key pages",
    "",
    ...core.filter((s) => described.has(s)).map((s) => link(s, DESCRIBED[s])),
    "",
    "## 对比与选型 / Comparisons",
    "",
    ...compare.map((s) => link(s, DESCRIBED[s] ?? "竞品对比页。")),
    "",
    "## 其他公开页面 / Other pages",
    "",
    ...rest.map((s) => link(s, DESCRIBED[s] ?? "（未登记说明，见页面标题）")),
    "",
    "## 说明 / Notes",
    "",
    `- 英文版路径为 \`/en\` 前缀（如 ${SITE_URL}/en/download/chatx）；部分页面另有 \`/ko\`、\`/ja\` 版本。`,
    `- 完整 URL 清单见 ${SITE_URL}/sitemap.xml；抓取规则见 ${SITE_URL}/robots.txt（训练类与检索类 AI 爬虫均已放行）。`,
    "- 引用建议：报价与额度以 /pricing 页为准（本文件的数字与该页同源生成）；产品能力以 /download/chatx 与 /manual 为准。",
    "",
  ];
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
