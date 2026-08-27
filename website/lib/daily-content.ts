import path from "path";
import { generateText } from "./deepseek";
import { buildKnowledgeContext } from "./bot-knowledge";
import { PRODUCT_COUNT, productLineItems } from "./brand";

// Map each theme to a relevant product image (reuse the catalog assets).
// 2026-08-22 起有专属配图两张（品牌视觉语言与旧 prod-* 一致，1200x800）：
// prod-chatx.jpg=统一收件箱工作台全景 / prod-chatx2.jpg=互译→成交特写；
// 两张轮换 + voice 点缀，避免频道 feed 里同图连发的呆板感。
const THEME_IMAGE = [
  "chatx", // 周日 · 社群福利（免费开始/新人礼包）
  "chatx", // 周一 · 统一收件箱
  "chatx2", // 周二 · AI 自动成交实战
  "chatx2", // 周三 · 拟人互译
  "chatx", // 周四 · 场景拆解（询盘到成交）
  "voice", // 周五 · 语音与客户画像
  "chatx2", // 周六 · 零门槛上手
];

function imagePathForTheme(idx: number): string {
  const id = THEME_IMAGE[idx % THEME_IMAGE.length] ?? "overview";
  return path.join(process.cwd(), "public", "products", `prod-${id}.jpg`);
}

// Rotating daily themes (evergreen, product-grounded marketing — not fabricated news).
// 2026-08-21 起频道主推旗舰「智聊 ChatX」：七档选题全部围绕智聊的真实能力 / 上手路径 /
// 优惠展开（下标 = getDay()，周日=0）；其他产品线只在帖内作一句配角带过（红线见 system prompt）。
// 注意：不设「客户故事」类选题——生成模型会不受控地编造业绩数字，改用「场景拆解」讲能力。
// Real-time web/news ingestion would need a search API key (see roadmap).
const THEMES = [
  "社群福利 · 智聊 ChatX 下载即免费开始：标准翻译不限量 + 注册送体验 Token，新人 6U 大礼包双倍到账",
  "产品力 · 智聊统一收件箱：Telegram / WhatsApp / Messenger / LINE 全渠道消息一个工作台接住",
  "实战技巧 · 用智聊的 AI 拟稿与自动回复，把每一条外语询盘变成订单",
  "出海获客 · 智聊内建拟人互译：用客户的母语聊单，不像机翻像本地人",
  "场景拆解 · 从外语询盘到成交，智聊全程怎么接住（收件箱 → 互译 → AI 跟单）",
  "产品力 · 智聊的语音消息与客户画像跟进：老客户不再流失",
  "零门槛上手 · 智聊不需要显卡、不用配 API Key，普通办公电脑下载即用",
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

/** 主角检查（2026-08-21 起 ChatX 聚焦期）：日更帖正文必须点名「智聊 / ChatX」。
 *  提示词已钉主角，但模型偶发顺着知识库漂题讲别的产品——与防编造闸同族的机械兜底：
 *  重试一稿仍跑题则标 risky 降级草稿，绝不自动发布偏题帖。 */
export function mentionsFlagship(text: string): boolean {
  return /智聊|ChatX/i.test(text);
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
    `你是无界科技 BOUNDLESS 的资深社媒文案，负责官方 Telegram 频道。频道现阶段主推旗舰产品` +
    `「智聊 ChatX」——聚合 AI 聊天工作台：统一收件箱 + AI 自动成交 + 内建拟人互译，下载即免费开始；` +
    `其余产品线（${lineNames}，共 ${PRODUCT_COUNT} 条）只是背景配角。基于以下产品事实创作营销帖：\n${knowledge}\n\n` +
    `写作要求：\n` +
    `- 【主角钉死】每一帖的主角都是智聊 ChatX：能力、场景、上手路径、优惠都要落到智聊上；` +
    `其他产品最多一句带过，不得喧宾夺主。\n` +
    `- 【产品形态】智聊是 Windows 桌面客户端（下载安装即用），不是网页/浏览器工具，` +
    `不要写「打开浏览器/网页版」；上手卖点=不需要显卡、无需配 API Key。\n` +
    `- 【免费档口径】免费=标准翻译永久免费不限量 + 每月 1,000 Token + 注册送 10,000 体验 Token；` +
    `AI 用量按 Token 计费——不要写「全部功能免费 / 无限免费 / 永久免费使用」这类无限定表述。\n` +
    `- 简体中文，口吻专业又有感染力，像顶尖出海营销号。\n` +
    `- 开头一行：emoji + 抓人标题（标题里出现「智聊」或「ChatX」）。\n` +
    `- 中间 3-4 条要点，每行以 emoji 开头，短句、有冲击力。\n` +
    `- 结尾一句行动号召，优先引导「免费下载智聊 ChatX」（点帖子下方按钮直达），其次私聊咨询 / 进群。\n` +
    `- 末尾 3-5 个相关话题标签（每个都以 # 开头，建议含 #智聊 #ChatX）。\n` +
    `- 总长 120-220 字。不要使用 markdown 符号(* \` )。\n` +
    `- 【红线】严禁编造业绩与案例：不得出现营收/月入/成交天数/增长百分比/复购率等具体业绩数字，` +
    `不得虚构"某客户/一家公司"的成功故事。能力一律用「可以/能做到」的口吻描述将来时可能性，` +
    `只允许引用产品事实里真实存在的数字（如支持语种数、7×24 在线、免费额度与充值赠送比例）。`;
  const user = `今天的选题：「${picked.theme}」。围绕这个选题写一条频道营销帖。`;

  let text = "";
  let risky = false;
  let retryHint = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const raw = await generateText(system, user + (attempt === 0 ? "" : retryHint));
    if (!raw) return null;
    text = fixTags(raw.trim());
    const fab = looksFabricated(text);
    const offTopic = !mentionsFlagship(text);
    if (!fab && !offTopic) {
      risky = false;
      break;
    }
    risky = true;
    const reasons = [
      fab ? "包含虚构业绩——删除一切具体业绩数字与客户故事，只讲产品能做什么" : "",
      offTopic ? "没有点名主角——正文必须明确提到「智聊 ChatX」" : "",
    ].filter(Boolean);
    retryHint = `\n\n上一稿被驳回（${reasons.join("；")}）。请重写。`;
  }
  return { theme: picked.theme, text: escapeHtml(text), imagePath: imagePathForTheme(picked.idx), risky };
}
