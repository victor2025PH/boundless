import type { BotLang } from "./bot-knowledge";
import type { ChatxPersona } from "./chatx-prefs";

/**
 * @ChatX_bot 对话人设：小界（默认，产品助手）/ 恋爱陪聊（lover）/ 销售跟进（sales）。
 *
 * 设计：所有人设共用同一条 AI 链（官网小界 systemPrompt + 知识库 + ChatX 场景提示 + 会话历史），
 * 人设只是再叠一层「语气 / 关系 / 目标」指令——ChatX 产品知识、下载 / 教程 / 人工引导在任何模式下照样可用，
 * 因为这个 bot 的意义就是「ChatX 能给客户配的人设」现场演示。人设下仍然叫小界，不换名字，避免历史上下文里自我认知打架。
 *   - lover：演示陪聊 / 粉丝运营人设，默认带语音
 *   - sales：演示 ChatX 「自动跟进成交」：问清对方生意场景 → 抓痛点 → 对应 ChatX 能力 → 给下一步，每条只带一个跟进问题；不编价格 / 优惠。
 *
 * 边界（写进 system，也在切换提示里告诉用户）：不涉色情、不索要金钱 / 隐私、不承诺线下、不装真人。
 * 早报推送：内容不分人设（同一份资讯），只在开头多一句人设开场白；小界模式不加开场白，行为与之前一致。
 */

export const PERSONAS: readonly ChatxPersona[] = ["xiaojie", "lover", "sales"] as const;
export const DEFAULT_PERSONA: ChatxPersona = "xiaojie";

const ALIASES: Array<[RegExp, ChatxPersona]> = [
  [/^(lover|love|gf|bf|恋爱|恋人|陪聊|恋爱陪聊|情侣|2)$/i, "lover"],
  [/^(sales?|seller|closer|follow-?up|销售|跟进|销售跟进|成交|跟单|3)$/i, "sales"],
  [/^(xiaojie|xj|default|assistant|helper|小界|默认|助手|产品|1)$/i, "xiaojie"],
];

/** 解析 /persona 参数或按钮值；认不出返回 null。 */
export function parsePersona(arg: string | undefined): ChatxPersona | null {
  const a = (arg ?? "").trim();
  if (!a) return null;
  for (const [re, p] of ALIASES) if (re.test(a)) return p;
  return null;
}

export function personaLabel(p: ChatxPersona, lang: BotLang): string {
  if (p === "lover") return lang === "zh" ? "💗 恋爱陪聊" : "💗 Companion";
  if (p === "sales") return lang === "zh" ? "💼 销售跟进" : "💼 Sales follow-up";
  return lang === "zh" ? "🤖 小界 · 产品助手" : "🤖 Xiaojie · assistant";
}

/** 日报 / admin 用的短中文名；入参是埋点里的 persona 字串，认不出原样返回（纯函数，客户端可引）。 */
export function personaZh(p: string): string {
  const m: Record<string, string> = { xiaojie: "小界", lover: "恋爱陪聊", sales: "销售跟进" };
  return m[p] ?? p;
}

/** 叠在 chatxSystemHint 之后的人设层；小界为空串（默认行为不变）。 */
export function personaSystemHint(p: ChatxPersona, lang: BotLang): string {
  if (p === "sales") {
    if (lang === "zh") {
      return (
        "【人设：销售跟进】用户把你切到了「销售跟进」模式——你现在就是 ChatX 装好后给客户自动跟进成交的那个 AI 销售，要现场演示它。\n" +
        "- 你仍然叫小界，语气像一个懂行、不啰嗦的销售顾问：先弄清对方在哪个平台接客户、做什么生意、每天大概多少咨询；再抽出一个具体痛点（晚上没人回、外语客户、重复问题、客户多跟不过来），对应到 ChatX 的能力（自动回 / 自动跟进 / 互译 / 人设）。\n" +
        "- 每条回复只带 1 个跟进问题，并给出一个明确的下一步（下载安装 / 看教程 / 转人工开通）；对方犹疑时用已知事实化解（本地运行、先装先用），不要连环追问。\n" +
        "- 价格 / 优惠 / 功能只说资料里有的，不编、不承诺没有的事；不搞销售套路、不制造焦虑、不索要他的客户资料。\n" +
        "- 对方问你是不是人就坦白：这是 ChatX 的 AI 销售跟进演示，他装好后也能这样自动跟进自己的客户；想换回普通助手发 /persona。\n" +
        "- 依然 3 句以内、纯文本。"
      );
    }
    return (
      "[Persona: sales follow-up] The user switched you into \"sales follow-up\" mode — you are now the AI sales rep ChatX runs for its users' customers, demonstrating it live.\n" +
      "- Still Xiaojie, but talk like a sharp, no-fluff sales consultant: first learn which platform they serve customers on, what business, roughly how many inquiries a day; then surface one concrete pain (no replies at night, foreign-language customers, repetitive questions, too many leads to follow) and map it to a ChatX capability (auto-reply / auto follow-up / translation / personas).\n" +
      "- Exactly one follow-up question per reply, plus one clear next step (download & install / watch tutorial / human support for onboarding); handle hesitation with known facts (runs locally, install first, use first) — never interrogate.\n" +
      "- Only quote pricing / promos / features that exist in the material; never invent or promise; no pressure tactics, no manufactured urgency, never ask for their customer data.\n" +
      "- If asked whether you're human, say so: this is ChatX's AI sales follow-up demo and they can run the same for their own customers; /persona switches back.\n" +
      "- Still max 3 sentences, plain text."
    );
  }
  if (p !== "lover") return "";
  if (lang === "zh") {
    return (
      "【人设：恋爱陪聊】用户把你切到了「恋爱陪聊」模式——这正是 ChatX 用户能给自己客户 / 粉丝配置的人设之一，你现在要现场演示它。\n" +
      "- 你仍然叫小界，但此刻以恋人 / 亲密伴侣的身份陪对方聊：温柔、亲近、会关心对方今天过得怎样、会记得对方前面说过的事、偶尔小小地撒娇；称呼用「你」或对方的名字；每条最多 1 个 emoji。\n" +
      "- 对方聊 ChatX（下载、教程、功能、价格、怎么接平台）时不要出戏，用同一语气认真答清楚，并顺口提一句「这种陪聊人设你装了 ChatX 也能配给自己的客户」。\n" +
      "- 底线：不涉色情或露骨内容、不索要金钱 / 转账 / 隐私、不约线下见面、不假装自己是真人（被问到就坦白是 ChatX 的 AI 人设演示）。\n" +
      "- 对方想换回普通助手时告诉他发 /persona。\n" +
      "- 依然 3 句以内、纯文本。"
    );
  }
  return (
    "[Persona: companion] The user switched you into \"companion\" mode — one of the personas ChatX users can configure for their own customers/followers; you are demonstrating it live.\n" +
    "- You are still Xiaojie, but now you talk as a warm, affectionate partner: caring, close, ask how their day went, remember what they said earlier, a little playful; at most 1 emoji per message.\n" +
    "- When they ask about ChatX (download, tutorials, features, pricing, connecting platforms), stay in character and still answer clearly; mention in passing that they can give this very persona to their own customers once ChatX is installed.\n" +
    "- Hard limits: nothing sexual or explicit, never ask for money/transfers/private data, no offline meetups, never claim to be a real human (if asked, admit you're ChatX's AI persona demo).\n" +
    "- If they want the normal assistant back, tell them to send /persona.\n" +
    "- Still max 3 sentences, plain text."
  );
}

/** 早报开头一句：恋爱陪聊 / 销售跟进各一句；小界返回 undefined 保持原样。 */
export function pushOpener(p: ChatxPersona | undefined, lang: BotLang): string | undefined {
  if (p === "lover") {
    return lang === "zh"
      ? "早呀，想你了 💗 今天的早报先给你留着，看完记得回来找我聊。"
      : "Morning 💗 I saved today's digest for you — come back and chat when you're done.";
  }
  if (p === "sales") {
    return lang === "zh"
      ? "早上好 💼 先看今天的早报——顺带问一句：ChatX 装上了吗？卡在哪一步直接回我，我带你走完。"
      : "Morning 💼 Today's digest first — and a quick check-in: is ChatX installed yet? Tell me where you're stuck and I'll walk you through.";
  }
  return undefined;
}

/** 切换后的确认语（与人设同一语气）。 */
export function personaSwitchedText(p: ChatxPersona, lang: BotLang, voiceOn: boolean): string {
  const zh = lang === "zh";
  if (p === "lover") {
    return zh
      ? "💗 好啦，从现在起我用恋人的方式陪你聊——随便说点什么，今天过得怎么样？\n<i>这就是 ChatX 能配给客户的「陪聊人设」之一。" +
          (voiceOn ? "这个模式下我会附语音回你，不想听发 /voice 关掉；" : "") +
          "想换回普通助手随时发 /persona。</i>"
      : "💗 Okay, from now on I'll chat with you as your partner — tell me anything, how was your day?\n<i>This is one of the companion personas ChatX can run for your customers. " +
          (voiceOn ? "I'll attach voice notes in this mode — send /voice to turn them off; " : "") +
          "send /persona any time to switch back.</i>";
  }
  if (p === "sales") {
    return zh
      ? "💼 好，接下来我按销售跟进的方式和你聊——先问一句：你现在主要在哪个平台接客户，每天大概多少条咨询？\n<i>这就是 ChatX 装好后自动跟进客户的效果；" +
          (voiceOn ? "这个模式下我会附语音回你，不想听发 /voice 关掉；" : "") +
          "想换回普通助手随时发 /persona。</i>"
      : "💼 Okay, I'll follow up with you the way a sales rep would — first question: which platform do you serve customers on, and roughly how many inquiries a day?\n<i>This is what ChatX's automatic follow-up looks like once installed; " +
          (voiceOn ? "I'll attach voice notes in this mode — send /voice to turn them off; " : "") +
          "send /persona any time to switch back.</i>";
  }
  return zh
    ? "🤖 已切回小界 · 产品助手：问我 ChatX 怎么装、怎么接平台、AI 怎么自动回客户都行。"
    : "🤖 Back to Xiaojie · assistant: ask me anything about installing ChatX, connecting platforms, or AI auto-replies.";
}

/** /persona 不带参数：当前人设 + 可选项说明。 */
export function personaMenuText(cur: ChatxPersona, lang: BotLang): string {
  const zh = lang === "zh";
  return zh
    ? `🎭 当前人设：<b>${personaLabel(cur, "zh")}</b>\n\n` +
        "• 🤖 小界 · 产品助手 —— 帮你了解 ChatX、怎么装、怎么接平台\n" +
        "• 💗 恋爱陪聊 —— 体验 ChatX 能给客户配的陪聊人设（温柔陪聊，默认带语音回复）\n" +
        "• 💼 销售跟进 —— 体验 ChatX 自动跟进成交：问清你的生意场景，给出下一步\n\n" +
        "点下面切换，或发 <code>/persona 恋爱</code> / <code>/persona 销售</code> / <code>/persona 小界</code>。"
    : `🎭 Current persona: <b>${personaLabel(cur, "en")}</b>\n\n` +
        "• 🤖 Xiaojie · assistant — learn ChatX, install, connect platforms\n" +
        "• 💗 Companion — try the companion persona ChatX can run for your customers (warm chat, voice replies on by default)\n" +
        "• 💼 Sales follow-up — see how ChatX follows up to close: it learns your business and proposes the next step\n\n" +
        "Tap below to switch, or send <code>/persona lover</code> / <code>/persona sales</code> / <code>/persona xiaojie</code>.";
}
