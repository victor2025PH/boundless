import path from "path";
import { generateText } from "./deepseek";
import { buildKnowledgeContext } from "./bot-knowledge";
import { PRODUCT_COUNT, productLineItems } from "./brand";

// Map each theme to a relevant product image (reuse the catalog assets).
const THEME_IMAGE = [
  "overview", // 行业趋势
  "translate", // 实战技巧 · AI 自动成交
  "digital-human", // 客户故事
  "faceswap", // 产品力 · 实时换脸换声
  "translate", // 出海获客 · 多语种翻译
  "voice", // 避坑指南 · 翻译对比
  "overview", // 社群福利
];

function imagePathForTheme(idx: number): string {
  const id = THEME_IMAGE[idx % THEME_IMAGE.length] ?? "overview";
  return path.join(process.cwd(), "public", "products", `prod-${id}.jpg`);
}

// Rotating daily themes (evergreen, product-grounded marketing — not fabricated news).
// 注意：不设「客户故事」类选题——生成模型会不受控地编造业绩数字，改用「场景拆解」讲能力。
// Real-time web/news ingestion would need a search API key (see roadmap).
const THEMES = [
  "行业趋势 · AI 出海获客的最新打法",
  "实战技巧 · 用 AI 自动成交聊天把流量变订单",
  "场景拆解 · 从外语询盘到成交，AI 全程怎么接住",
  "产品力 · 实时换脸换声在直播/视频通话的应用",
  "出海获客 · 多语种拟人翻译如何拿下海外客户",
  "避坑指南 · 传统翻译软件 vs AI 拟人翻译",
  "社群福利 · 关注频道进群解锁专属优惠",
];

export function listThemes(): string[] {
  return THEMES;
}

export function themeForToday(): { idx: number; theme: string } {
  const idx = new Date().getDay() % THEMES.length;
  return { idx, theme: THEMES[idx] };
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** 编造业绩检测：无人审核的自动发布必须过这道闸。
 *  模型即使被提示词禁止，仍会写出「单月营收破5万」「3天成交」这类虚构成果——
 *  命中即判为不可直接发布（可重试或降级为草稿）。 */
export function looksFabricated(text: string): string | null {
  const patterns: [RegExp, string][] = [
    [/营收|月入|赚了|利润|流水/u, "revenue-claim"],
    [/破\s*[0-9一二三四五六七八九十百千万]+\s*[万千单]/u, "amount-claim"],
    [/[0-9一二三]+\s*(天|小时|周)[内]?(拿下|成交|回本|出单)/u, "time-to-close-claim"],
    [/翻(倍|了\s*[0-9]+)|增长\s*[0-9]+\s*%|提升\s*[0-9]+\s*%/u, "growth-claim"],
    [/复购率|转化率\s*[0-9]/u, "rate-claim"],
    [/某(客户|老板|团队)|一家.{0,8}(公司|电商|团队).{0,12}(靠|用).{0,30}(卖到|成交|拿下)/u, "fake-story"],
  ];
  for (const [re, tag] of patterns) if (re.test(text)) return tag;
  return null;
}

/** 末行话题标签规整：把丢了 # 的标签补上（模型偶发漏写首个 #）。 */
function fixTags(text: string): string {
  const lines = text.trimEnd().split("\n");
  const last = lines[lines.length - 1] ?? "";
  if (!last.includes("#")) return text;
  lines[lines.length - 1] = last
    .split(/\s+/)
    .map((tok) => (tok && !tok.startsWith("#") ? `#${tok}` : tok))
    .join(" ");
  return lines.join("\n");
}

export interface DailyPost {
  theme: string;
  text: string;
  imagePath: string;
  /** 两次生成都命中编造检测时为 true：调用方不得自动发布，应降级为草稿 */
  risky: boolean;
}

/** Generate a daily channel post for a given (or today's) theme. HTML-safe.
 *  内置一次「防编造」重试；仍不干净则标记 risky，由调用方决定降级。 */
export async function generateDailyPost(themeIdx?: number): Promise<DailyPost | null> {
  const picked =
    typeof themeIdx === "number"
      ? { idx: themeIdx % THEMES.length, theme: THEMES[themeIdx % THEMES.length] }
      : themeForToday();

  const knowledge = buildKnowledgeContext("zh");
  const lineNames = productLineItems("zh")
    .map((it) => it.name)
    .join("、");
  const system =
    `你是无界科技 BOUNDLESS 的资深社媒文案，负责官方 Telegram 频道（${PRODUCT_COUNT} 条产品线：${lineNames}）。基于以下产品事实创作营销帖：\n${knowledge}\n\n` +
    `写作要求：\n` +
    `- 简体中文，口吻专业又有感染力，像顶尖出海营销号。\n` +
    `- 开头一行：emoji + 抓人标题。\n` +
    `- 中间 3-4 条要点，每行以 emoji 开头，短句、有冲击力。\n` +
    `- 结尾一句行动号召，引导私聊咨询 / 进群 / 打开小程序。\n` +
    `- 末尾 3-5 个相关话题标签（每个都以 # 开头）。\n` +
    `- 总长 120-220 字。不要使用 markdown 符号(* \` )。\n` +
    `- 【红线】严禁编造业绩与案例：不得出现营收/月入/成交天数/增长百分比/复购率等具体业绩数字，` +
    `不得虚构"某客户/一家公司"的成功故事。能力一律用「可以/能做到」的口吻描述将来时可能性，` +
    `只允许引用产品事实里真实存在的数字（如支持语种数、7×24 在线）。`;
  const user = `今天的选题：「${picked.theme}」。围绕这个选题写一条频道营销帖。`;

  let text = "";
  let risky = false;
  for (let attempt = 0; attempt < 2; attempt++) {
    const extra =
      attempt === 0
        ? ""
        : `\n\n上一稿因包含虚构业绩被驳回。重写：删除一切具体业绩数字与客户故事，只讲产品能做什么。`;
    const raw = await generateText(system, user + extra);
    if (!raw) return null;
    text = fixTags(raw.trim());
    const hit = looksFabricated(text);
    if (!hit) {
      risky = false;
      break;
    }
    risky = true;
  }
  return { theme: picked.theme, text: escapeHtml(text), imagePath: imagePathForTheme(picked.idx), risky };
}
