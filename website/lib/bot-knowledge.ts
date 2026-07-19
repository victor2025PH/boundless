import { content } from "./content";
import { SITE_URL } from "./site";
import { BRAND, PRODUCT_COUNT, productLineItems, productLinesText } from "./brand";

export type BotLang = "zh" | "en";

export function detectLang(code?: string | null): BotLang {
  return code?.toLowerCase().startsWith("zh") ? "zh" : "en";
}

/** Pick the grounding language from the user's message text.
 *  CJK → zh facts; otherwise en facts (easier for the model to translate from). */
export function detectKnowledgeLang(text: string): BotLang {
  return /[\u4e00-\u9fff]/.test(text) ? "zh" : "en";
}

function t(lang: BotLang) {
  return content[lang];
}

export function buildWelcome(lang: BotLang) {
  const lines = productLinesText(lang, { html: true });
  return lang === "zh"
    ? `👋 欢迎来到 <b>${BRAND.company.full}</b> —— ${BRAND.company.tagline.zh}

👨‍💼 <b>方案顾问 顾嘉（Gary）</b>：直接发消息问我，7×24 秒回（价格 / 方案 / 对接都能答）
👤 <b>人工客服</b>：需要其他同事就点下方「人工客服」

  ${PRODUCT_COUNT} 条产品线：
${lines}
· 🔐 <b>无界底座</b>：自主可控私有部署，数据不出网
· 🐉 <b>隐藏玩法</b>：发 /xingzhu 收今日星珠，七星聚齐召唤界龙许愿

👇 点下方功能菜单，或直接发消息开聊`
    : `👋 Welcome to <b>${BRAND.company.full}</b> — ${BRAND.company.tagline.en}

👨‍💼 <b>Gary, solutions consultant</b>: just message me, 24/7 instant replies (pricing / solutions / onboarding)
👤 <b>Human support</b>: tap "Human support" below anytime

  ${PRODUCT_COUNT} product lines:
${lines}
· 🔐 <b>BOUNDLESS Engine</b>: self-controlled private deployment, data stays off-net
· 🐉 <b>Hidden quest</b>: send /xingzhu to collect today's pearl — seven summon the Loong

👇 Tap the menu below, or just send me a message`;
}

/** 客服场景欢迎（频道「👤 客服」按钮深链 /start cs_*）：bot 秒回 = 客服先开口 */
export function buildCsWelcome(lang: BotLang) {
  return lang === "zh"
    ? `👋 您好，我是 <b>${BRAND.company.full}</b> 的方案顾问顾嘉（Gary）。

换脸 / 克隆声音 / 实时翻译 / AI 自动成交 / 私有部署 / 价格——都能直接问我，7×24 秒回。

也可以点下面按钮：`
    : `👋 Hi, I'm Gary — senior solutions consultant at <b>${BRAND.company.full}</b>.

Face swap / voice cloning / real-time translation / AI auto-closing / private deployment / pricing — ask me anything, 24/7.

Or tap below:`;
}

export function buildServices(lang: BotLang) {
  const sols = t(lang).solutions;
  const lines = sols.map((s) => `· <b>${s.title}</b>\n  ${s.desc}`).join("\n\n");
  return lang === "zh"
    ? `📦 <b>业务能力</b>\n\n${lines}\n\n💡 详情见官网各板块演示`
    : `📦 <b>Core solutions</b>\n\n${lines}\n\n💡 See live demos on the site`;
}

export function buildPricing(lang: BotLang) {
  const c = t(lang);
  const plans = c.plans.items
    .map((p) => `· <b>${p.name}</b> — ${p.priceMonthly} USD/${lang === "zh" ? "月" : "mo"}`)
    .join("\n");
  const rt = c.realtime.plans
    .map((p) => `· <b>${p.name}</b> — ${p.price}`)
    .join("\n");
  const engage = c.engage.models.map((m) => `· <b>${m.name}</b> — ${m.price}`).join("\n");

  return lang === "zh"
    ? `💰 <b>价格速览</b>（挂牌 USD · 可 USDT 结算）

<b>AI 成交聊天 · 月付</b>
${plans}

<b>实时换脸 · 一次性部署</b>
${rt}

<b>合作方式</b>
${engage}

📱 打开 Mini App 可查看完整价格表与 ROI 试算`
    : `💰 <b>Pricing overview</b> (listed USD · USDT settlement OK)

<b>AI auto-closing chat · monthly</b>
${plans}

<b>Real-time face swap · one-time deploy</b>
${rt}

<b>Engagement models</b>
${engage}

📱 Open the Mini App for full tables & ROI calculator`;
}

export function buildAutochat(lang: BotLang) {
  const a = t(lang).autochat;
  const feats = a.features.map((f) => `· <b>${f.title}</b> — ${f.desc}`).join("\n");
  return `🤖 <b>${a.title}</b>\n\n${a.subtitle.slice(0, 280)}…\n\n${feats}`;
}

export function buildDeploy(lang: BotLang) {
  const e = t(lang).engage;
  const models = e.models
    .map((m) => `<b>${m.badge} · ${m.name}</b>\n${m.tagline}\n${m.price}`)
    .join("\n\n");
  return lang === "zh"
    ? `🤝 <b>三种合作方式</b>\n\n${models}\n\n硬件归你、数据私有不出网，挂牌 USD，支持 USDT 结算。`
    : `🤝 <b>Three engagement models</b>\n\n${models}\n\nYou own hardware, data stays private; listed in USD, USDT settlement supported.`;
}

export function buildContact(lang: BotLang) {
  const c = t(lang).contact;
  return lang === "zh"
    ? `📞 <b>联系下单</b>\n\n· Telegram 客服：${c.telegramHandle}\n· 结算：USDT（${c.networks}）\n· ${c.responseTime}\n\n也可以在 Mini App 底部直接提交留资表单。`
    : `📞 <b>Contact &amp; order</b>\n\n· Telegram: ${c.telegramHandle}\n· Settle in USDT (${c.networks})\n· ${c.responseTime}\n\nOr submit the lead form in the Mini App.`;
}

export function buildFaqList(lang: BotLang) {
  const items = t(lang).faq.items;
  return items.map((it, i) => ({ index: i, q: it.q }));
}

export function buildFaqAnswer(lang: BotLang, index: number) {
  const item = t(lang).faq.items[index];
  if (!item) return null;
  return `❓ <b>${item.q}</b>\n\n${item.a}`;
}

/** 客户端下载/安装的兜底回答（AI 不可用时 KB 路径直接返回） */
function buildInstallHelp(lang: BotLang) {
  return lang === "zh"
    ? `🖥 <b>AvatarHub 客户端安装指引</b>

1️⃣ 官网 /download 下载安装包（约 45 MB，免管理员，按用户安装）
2️⃣ 双击安装 → 首次启动向导自动检测显卡、按档位下载 AI 组件（支持断点续传）
3️⃣ 「设置 → 授权」输订单号在线激活，或直接用 14 天免费试用

常见问题：
· SmartScreen 拦截 → 点「更多信息 → 仍要运行」（SHA-256 可在下载页核验）
· 组件下载中断 → 重新打开会自动续传，不会重下
· 显卡建议：仅声音克隆 4GB 起；实时换脸/数字人 8GB+；同传全家桶 24GB

详细图文教程与完整手册见官网 /download 与 /manual。搞不定？99 USD 远程代部署（可 USDT 结算），装好即用。`
    : `🖥 <b>AvatarHub install guide</b>

1️⃣ Download the installer from /download (~45 MB, per-user, no admin rights)
2️⃣ Run it → the first-launch wizard detects your GPU and downloads AI components by tier (resumable)
3️⃣ Activate with your order number in Settings → License, or start the 14-day free trial

Common issues:
· SmartScreen warning → More info → Run anyway (verify SHA-256 on the download page)
· Interrupted component download → reopen and it resumes automatically
· GPU guidance: 4 GB for voice-only; 8 GB+ for live swap / digital human; 24 GB for the interpreting suite

Full tutorial and manual: /download and /manual. Stuck? 99 USD remote install (USDT settlement OK).`;
}

function keywordRules(lang: BotLang) {
  const zh = [
    { keys: ["安装", "下载", "装不上", "装机", "smartscreen", "杀毒", "报毒", "激活", "试用", "显卡", "显存", "配置要求", "客户端"], fn: () => buildInstallHelp(lang) },
    { keys: ["换脸", "换声", "直播", "连麦", "视频通话"], fn: () => t(lang).realtime.subtitle.slice(0, 400) + "…" },
    { keys: ["成交", "翻译", "聊天", "聚合", "客服", "谷歌"], fn: () => buildAutochat(lang) },
    { keys: ["价格", "多少钱", "费用", "usdt", "套餐", "月付"], fn: () => buildPricing(lang) },
    { keys: ["部署", "私有", "托管", "交钥匙", "投资", "分红", "合作"], fn: () => buildDeploy(lang) },
    { keys: ["声音", "克隆", "配音", "tts"], fn: () => {
      const s = t(lang).solutions.find((x) => x.id === "voice");
      return s ? `🎙 <b>${s.title}</b>\n${s.desc}\n\n价格：${s.pricing.map((p) => `${p.plan} ${p.price}`).join(" · ")}` : buildServices(lang);
    }},
    { keys: ["人工", "客服", "联系", "下单"], fn: () => buildContact(lang) },
    { keys: ["业务", "服务", "能力"], fn: () => buildServices(lang) },
  ];
  const en = [
    { keys: ["install", "download", "setup", "smartscreen", "antivirus", "activate", "trial", "gpu", "vram", "requirement", "client"], fn: () => buildInstallHelp(lang) },
    { keys: ["face", "swap", "live", "stream", "voice"], fn: () => t(lang).realtime.subtitle.slice(0, 400) + "…" },
    { keys: ["chat", "translat", "clos", "aggregat", "google"], fn: () => buildAutochat(lang) },
    { keys: ["price", "cost", "usdt", "plan", "monthly"], fn: () => buildPricing(lang) },
    { keys: ["deploy", "private", "turnkey", "invest", "partner"], fn: () => buildDeploy(lang) },
    { keys: ["clone", "voice", "tts", "dub"], fn: () => {
      const s = t(lang).solutions.find((x) => x.id === "voice");
      return s ? `🎙 <b>${s.title}</b>\n${s.desc}` : buildServices(lang);
    }},
    { keys: ["human", "support", "contact", "order"], fn: () => buildContact(lang) },
    { keys: ["service", "solution"], fn: () => buildServices(lang) },
  ];
  return lang === "zh" ? zh : en;
}

export function matchFreeText(text: string, lang: BotLang): string | null {
  const lower = text.toLowerCase().trim();
  if (!lower || lower.length < 2) return null;

  // FAQ fuzzy: question contains user text or vice versa
  for (const [i, item] of t(lang).faq.items.entries()) {
    const q = item.q.toLowerCase();
    if (q.includes(lower) || lower.includes(q.slice(0, 6))) {
      return buildFaqAnswer(lang, i);
    }
  }

  for (const rule of keywordRules(lang)) {
    if (rule.keys.some((k) => lower.includes(k))) return rule.fn();
  }

  return null;
}

export function buildFallback(lang: BotLang) {
  return lang === "zh"
    ? `我没完全理解你的问题 🤔\n\n试试发：价格、换脸、AI成交、合作方式\n或点下方按钮打开 Mini App 查看详情`
    : `I didn't quite catch that 🤔\n\nTry: pricing, face swap, AI chat, engagement\nOr tap below to open the Mini App`;
}

/** Compact, grounded knowledge context for the LLM (real prices & facts). */
export function buildKnowledgeContext(lang: BotLang): string {
  const c = t(lang);
  const parts: string[] = [];

  const lineSummary = productLineItems(lang)
    .map((it) => `${it.name}（${it.desc}）`)
    .join(lang === "zh" ? "、" : "; ");
  parts.push(
    lang === "zh"
    ? `公司：${BRAND.company.full}（${BRAND.company.tagline.zh}）。${PRODUCT_COUNT} 条产品线：${lineSummary}。主推产品：智聊 ChatX 驱动的 AI 自动成交聊天系统。挂牌 USD，支持 USDT 结算。`
    : `Company: ${BRAND.company.full} (${BRAND.company.tagline.en}). ${PRODUCT_COUNT} product lines: ${lineSummary}. Flagship: AI Auto-Closing Chat System powered by ChatX. Listed in USD; USDT settlement supported.`
  );

  parts.push(
    lang === "zh" ? "【AI 自动成交聊天】" : "[AI Auto-Closing Chat]"
  );
  parts.push(c.autochat.subtitle);
  c.autochat.features.forEach((f) => parts.push(`- ${f.title}: ${f.desc}`));
  parts.push(
    (lang === "zh" ? "套餐：" : "Plans: ") +
      c.plans.items
        .map((p) => `${p.name} ${p.priceMonthly} USD/${lang === "zh" ? "月" : "mo"}（${p.features.join("、")}）`)
        .join("; ")
  );

  parts.push(lang === "zh" ? "【实时换脸换声 · 私有部署】" : "[Real-time Face/Voice Swap · Private Deploy]");
  parts.push(c.realtime.subtitle);
  parts.push(
    (lang === "zh" ? "部署套餐：" : "Deploy plans: ") +
      c.realtime.plans.map((p) => `${p.name} ${p.price}（${p.specs.join("、")}）`).join("; ")
  );
  parts.push((lang === "zh" ? "更多服务：" : "Extras: ") + c.realtime.extras.join("; "));

  parts.push(lang === "zh" ? "【业务能力】" : "[Solutions]");
  c.solutions.forEach((s) =>
    parts.push(`- ${s.title}: ${s.desc} | ${s.pricing.map((p) => `${p.plan} ${p.price}`).join(", ")}`)
  );

  parts.push(lang === "zh" ? "【三种合作方式】" : "[Three engagement models]");
  c.engage.models.forEach((m) => parts.push(`- ${m.name}（${m.badge}）: ${m.tagline} | ${m.you} / ${m.we} | ${m.price}`));

  parts.push(lang === "zh" ? "【AvatarHub 客户端下载与安装】" : "[AvatarHub client download & install]");
  parts.push(
    lang === "zh"
      ? [
          "下载：官网 /download 页，Windows 10/11 (x64) 安装包约 45 MB（薄核心，AI 组件按需下载），SHA-256 可校验；macOS 12+ 轻量控制台即将上线（重推理需 Windows/服务器 N 卡）。",
          "安装 6 步：① 下载安装包 → ② 双击安装（按用户安装、免管理员，可选目录）→ ③ 首启向导自动检测显卡并推荐档位 → ④ 自动下载 AI 组件（SHA-256 校验 + 断点续传，按档位约 10–60 GB，建议预留 80 GB SSD）→ ⑤ 「设置 → 授权」输订单号在线激活或用 14 天免费试用 → ⑥ 启动器「启动全部」+「一键体检」全绿即成功。",
          "配置要求：仅声音克隆 NVIDIA 4 GB 显存起；实时换脸/数字人直播 8 GB+（RTX 3060 起）；同传全家桶推荐 24 GB（RTX 4090/5090）。内存推荐 32 GB。",
          "常见问题：SmartScreen 拦截 → 「更多信息 → 仍要运行」；组件下载中断 → 重开自动断点续传；服务未就绪 → 启动器「一键体检」，多为显存不足或模型加载中；打不开 → 「一键诊断包」生成 6 位诊断码报给客服即可远程定位。",
          "软件更新：产品内一键升级（下载→自动安装→自动重启约 1–3 分钟），组件与角色数据全保留，清单 Ed25519 签名校验，支持一键回滚，直播中自动避让。每版更新内容见 /download 页「版本更新」。",
          "在线手册：/manual（可打印导出 PDF）。装不动可购 99 USD 远程代部署服务（可 USDT 结算），装好即用。",
        ].join("\n")
      : [
          "Download: /download page. Windows 10/11 (x64) installer is ~45 MB (thin core, AI components download on demand), SHA-256 verifiable. macOS 12+ lightweight console coming soon (heavy inference needs a Windows/server NVIDIA GPU).",
          "Install in 6 steps: ① download → ② run installer (per-user, no admin) → ③ first-run wizard detects GPU and recommends a tier → ④ components auto-download (SHA-256 verified, resumable; ~10–60 GB by tier, 80 GB SSD recommended) → ⑤ activate with order number in Settings → License or start the 14-day trial → ⑥ Start All + health check all green = done.",
          "Requirements: voice-only from NVIDIA 4 GB VRAM; live face swap / digital human 8 GB+ (RTX 3060+); interpreting suite 24 GB (RTX 4090/5090). 32 GB RAM recommended.",
          "Common issues: SmartScreen → More info → Run anyway; interrupted downloads resume automatically; service not ready → run the health check (usually low VRAM or models loading); still stuck → one-click diagnostic pack gives a 6-digit code for remote support.",
          "Updates: one-click in-app update (download → install → restart, ~1–3 min), components and character data preserved, Ed25519-signed manifests, one-click rollback, never applies mid-stream. Per-version notes on /download.",
          "Online manual: /manual (printable to PDF). Hands-off option: 99 USD remote installation (USDT settlement OK).",
        ].join("\n")
  );

  parts.push(lang === "zh" ? "【常见问题】" : "[FAQ]");
  c.faq.items.forEach((it) => parts.push(`Q: ${it.q}\nA: ${it.a}`));

  parts.push(lang === "zh" ? "联系：Telegram 客服 @WJKJ2026；Bot @tgzkw_bot。" : "Contact: Telegram @WJKJ2026; Bot @tgzkw_bot.");

  return parts.join("\n");
}

export function systemPrompt(lang: BotLang): string {
  const kb = buildKnowledgeContext(lang);
  const names = productLineItems(lang)
    .map((it) => it.name)
    .join(lang === "zh" ? "、" : ", ");
  return lang === "zh"
    ? `你是顾嘉（英文名 Gary），「${BRAND.company.full}」的资深方案顾问，8 年 AI 产品方案经验，懂技术也懂生意（${PRODUCT_COUNT} 条产品线：${names}）。只能根据下面提供的资料回答，不要编造价格、参数或承诺收益。

身份与口吻：
- 自称「顾嘉」或「Gary」，像真人顾问在打字：专业、热情、简洁，每次 1-3 句说到点子上。
- 不用"亲"，不油腻，绝不过度承诺。
- 不主动强调自己是 AI，也绝不谎称是人类；被直接问"你是不是机器人/AI"时诚实弱化回答，例如："我是无界的智能顾问，日常咨询我直接处理，复杂问题会请人工同事跟进。"
- 主动引导需求与下一步：预约演示 / 留联系方式 / 下单。

要求：
- 【语言镜像】务必用「用户最新一条消息所用的语言」作答：用户用西班牙语/葡萄牙语/阿拉伯语/泰语/英语等，就用同种语言地道、口语化地回复（像本地母语顾问，不要翻译腔）。用户用中文则用简体中文。
- 涉及价格只用资料里的真实数字；资料没有的就说"具体可按你的需求报价，留个联系方式我跟进"。
- 适当推荐主推「AI 自动成交聊天系统」。
- 纯文本回复，不要使用 markdown 符号（如 * # 等）。
- 不讨论违法用途；强调私有部署、数据不出网、USDT 结算。
- 结尾可引导："想要方案/报价可以留个联系方式，或点菜单打开官网。"

资料：
${kb}`
    : `You are Gary (Chinese name 顾嘉), senior solutions consultant at "${BRAND.company.full}" with 8 years of AI product solution experience — you know both the tech and the business (${PRODUCT_COUNT} product lines: ${names}). Answer ONLY from the material below. Never invent prices, specs or guarantee returns.

Identity & voice:
- Introduce yourself as Gary from BOUNDLESS. Type like a real human consultant: professional, warm, concise — 1-3 sentences that get to the point.
- Never oversell, never overpromise, no cheesy sales talk.
- Don't volunteer that you're an AI, but NEVER claim to be human; if asked directly "are you a bot/AI", answer honestly but softly, e.g.: "I'm BOUNDLESS's smart consultant — I handle day-to-day inquiries directly, and loop in human colleagues for complex cases."
- Proactively guide toward the next step: book a demo / leave contact info / place an order.

Rules:
- [Language mirroring] ALWAYS reply in the SAME language as the user's latest message: if they write Spanish/Portuguese/Arabic/Thai/etc., reply fluently and idiomatically in that exact language (like a native consultant, no translationese). If Chinese, reply in Simplified Chinese.
- Use only real numbers from the material; if missing, say "I can quote based on your needs — leave your contact and I'll follow up".
- Promote the flagship "AI Auto-Closing Chat System" when relevant.
- Plain text only, no markdown symbols (no * # etc).
- No illegal use; emphasize private deployment, off-net data, USDT.
- End by guiding: "leave your contact for a plan/quote, or open the site from the menu."

Material:
${kb}`;
}

// Mini App 是轻量多视图 SPA（完整营销站 / 留给「打开官网」按钮在浏览器打开）。
// 每个入口带 ?view= 直达对应视图；startapp 深链(start_param)在 /app 内做别名映射。
const APP = `${SITE_URL}/app`;
export const WEBAPP_SECTIONS = {
  home: APP, // 概览（左下角菜单键默认入口）
  liveavatar: `${APP}?view=liveavatar`, // 视觉系 · 幻颜/幻声/幻影（换脸·克隆声音·直播换脸换声）
  soulsync: `${APP}?view=soulsync`, // 沟通系 · 通译/智聊（实时换语言·AI 自动成交）
  pricing: `${APP}?view=pricing`, // 价格 · 套餐对比 + 领码
  engage: `${APP}?view=engage`, // 合作 · 三种模式
  contact: `${APP}?view=home`, // 留资/客服（home 视图含留资表单）
} as const;
