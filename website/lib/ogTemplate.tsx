import { ImageResponse } from "next/og";
import { LANDINGS, type LandingKey } from "@/lib/landingContent";
import { compareSpecs } from "@/lib/compare-content";

/** 落地页专属 OG 分享图（TG/社媒分享卡片）。与根 OG 同风格，突出各产品线卖点。 */

export const OG_SIZE = { width: 1200, height: 630 };

const ACCENT: Record<LandingKey, string> = {
  voice: "#22d3ee",
  face: "#8b5cf6",
  interpreting: "#34d399",
  fate: "#ce56cb", // 紫粉（productMeta PRODUCT_GLOW fatex 同源）
};

const TAGLINE: Record<LandingKey, { zh: string; en: string }> = {
  voice: {
    zh: "三引擎克隆 · 情感语气 · 直播/电话/对话可用",
    en: "Tri-engine cloning · emotion & prosody · live, calls, chat",
  },
  face: {
    zh: "高清实时换脸 · 低延迟 · 直播与视频通话",
    en: "HD real-time face swap · low latency · live & video calls",
  },
  interpreting: {
    zh: "你的声音说外语 · 实时同传 · 多语种",
    en: "Your voice, other languages · real-time · multilingual",
  },
  fate: {
    zh: "八字排盘 · 每日灵签 · 人生 K 线",
    en: "BaZi charting · daily sign · life K-line",
  },
};

// 底行信任线：销售线共用「私有部署 · USDT」口径；幻缘是陪伴产品，改免责基调。
const FOOT_LINE: Record<"default" | "fate", { zh: string; en: string }> = {
  default: {
    zh: "私有部署 · 真机实测 · USDT 结算",
    en: "Private deployment · real-machine demos · USDT",
  },
  fate: {
    zh: "知缘知运 · 运势是倾向，不是命令",
    en: "Ask fate, chart life — a tendency, not a command",
  },
};

export async function landingOgImage(key: LandingKey, lang: "zh" | "en") {
  const c = LANDINGS[key];
  const accent = ACCENT[key];
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "72px 88px",
          background: "radial-gradient(circle at 18% 18%, #1a1d3a, #05060f 62%)",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", fontSize: 30, color: accent, letterSpacing: 4 }}>
          {lang === "zh" ? "无界科技 · BOUNDLESS" : "BOUNDLESS TECH"}
        </div>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            fontSize: 62,
            fontWeight: 800,
            marginTop: 30,
            lineHeight: 1.18,
            maxWidth: 1000,
          }}
        >
          <span>{c.hero.title[lang]}</span>
          <span style={{ color: accent }}>{c.hero.accent[lang]}</span>
        </div>
        <div style={{ display: "flex", fontSize: 30, color: "#94a3b8", marginTop: 30 }}>
          {TAGLINE[key][lang]}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            fontSize: 26,
            color: "#8b5cf6",
            marginTop: 42,
          }}
        >
          <div style={{ display: "flex", width: 46, height: 4, background: accent, borderRadius: 2 }} />
          <span>{FOOT_LINE[key === "fate" ? "fate" : "default"][lang]}</span>
        </div>
      </div>
    ),
    OG_SIZE
  );
}

/* ── ChatX 系页面 OG 共享版式（实施78 P1-4 / 问题 A4）───────────────────────
 * 背景：`/compare/*`、`/compare` 枢纽页、`/download/chatx` 此前都**没有**自己的
 * opengraph-image，全部回落到根 OG——那是一张中文品牌图「让沟通，无界」。把
 * `/en/compare/respond-io` 分享到 Reddit / LinkedIn，预览图与页面内容毫无关系。
 *
 * 分工（两条 agent 线同批做，已合流）：逐家对比卡 `compareOgImage` 在本文件上方
 * 单独实现（它的版面按「ChatX vs 某家」两行大标题调过，不走这套壳）；枢纽页
 * `compareHubOgImage` 与下载页 `downloadOgImage` 共用下面这个壳。
 *
 * 刻意**不改** landingOgImage：它已在服务 4 条落地页 × 4 个语言目录，改它等于让
 * 已上线的分享卡片承担回归风险。
 */

/** ChatX 品牌紫红，与 components/productMeta.ts 的 PRODUCT_GLOW.chatx ("217,70,239") 同源。
 *  与上方逐家对比卡的 cyan 刻意分色：那是「对某一家的对比」，这套壳是「产品自己的卡」。 */
const CHATX_ACCENT = "#d946ef";

/** 底行差异化三点：与 docs/实施78A §2.5「差异化三句」同源——改这里要同改那份弹药包，
 *  否则目录站文案与分享卡片会给出两套卖点。 */
const CHATX_FOOT: Record<"zh" | "en", string> = {
  zh: "Zalo / LINE 原生支持 · 数据留本机 · 按量计费不按坐席",
  en: "Zalo & LINE included · data stays local · usage-based, not per-seat",
};

function chatxOgShell(opts: {
  kicker: string;
  title: string;
  /** 主标题的强调后半段（如 "vs respond.io"）；省略则整句用白色。 */
  titleAccent?: string;
  subtitle: string;
  foot: string;
}) {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "72px 88px",
          background: "radial-gradient(circle at 18% 18%, #241a3a, #05060f 62%)",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", fontSize: 28, color: CHATX_ACCENT, letterSpacing: 4 }}>
          {opts.kicker}
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            // 用 gap 而不是给后半段加 marginLeft：标题长到换行时（枢纽页的
            // 「how to choose (2026)」就会）marginLeft 会变成**行首缩进**，看着像排错了
            gap: 18,
            fontSize: 64,
            fontWeight: 800,
            marginTop: 28,
            lineHeight: 1.18,
            maxWidth: 1010,
          }}
        >
          <span>{opts.title}</span>
          {opts.titleAccent ? (
            <span style={{ color: CHATX_ACCENT }}>{opts.titleAccent}</span>
          ) : null}
        </div>
        <div
          style={{
            display: "flex",
            fontSize: 30,
            color: "#94a3b8",
            marginTop: 28,
            maxWidth: 1010,
            lineHeight: 1.35,
          }}
        >
          {opts.subtitle}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            fontSize: 25,
            color: "#c084fc",
            marginTop: 40,
          }}
        >
          <div
            style={{ display: "flex", width: 46, height: 4, background: CHATX_ACCENT, borderRadius: 2 }}
          />
          <span>{opts.foot}</span>
        </div>
      </div>
    ),
    OG_SIZE
  );
}

/** 逐家对比卡的副标题：刻意用**固定短句**而不是 `spec.tagline`——630px 高的卡片放长句
 *  等于没人看得清（这条判断来自并行线，采纳）。 */
const COMPARE_EDGE: Record<"zh" | "en", string> = {
  zh: "私有部署 · 按量计费 · 六渠道统一收件箱",
  en: "Self-hosted · pay-as-you-go · one inbox, six channels",
};

/** 逐家竞品对比页 OG（`/compare/<slug>` 与 `/en/compare/<slug>`）。
 *  竞品名从 compare-content 的 `compareSpecs` 派生——加对比页不用改这里。
 *
 *  ⚠ 合流记录（2026-08-28）：本函数一度被两条 agent 线各写一版，随后**双方为了互相
 *  避让又各自删掉了自己那版**，于是 8 个路由同时引用了不存在的导出（tsc TS2724）。
 *  现统一为这一份、与枢纽页/下载页共用 `chatxOgShell`。要改请改这里，别再起第二份实现。 */
export function compareOgImage(slug: string, lang: "zh" | "en") {
  const spec = compareSpecs[slug];
  return chatxOgShell({
    kicker: lang === "zh" ? "对比选型 · BOUNDLESS ChatX" : "COMPARISON · BOUNDLESS ChatX",
    title: lang === "zh" ? "智聊 ChatX" : "ChatX",
    // slug 与页面目录同源，理论上必命中；未登记时退化成产品自身卡，而不是让 next/og 500
    titleAccent: spec ? `vs ${spec.name}` : undefined,
    subtitle: COMPARE_EDGE[lang],
    foot: CHATX_FOOT[lang],
  });
}

/** 选型枢纽页 OG（`/compare` 与 `/en/compare`；实施78 P1-4 补，另一条线）。
 *  枢纽页不是「对某一家的对比」，标题走「怎么选」的疑问句式——它就是买家在 AI 里的原始问法，
 *  分享到 Reddit / LinkedIn 时也比「ChatX vs 某家」更像中立内容、更容易被点。 */
export function compareHubOgImage(lang: "zh" | "en") {
  return chatxOgShell({
    kicker: lang === "zh" ? "选型指南 · BOUNDLESS ChatX" : "BUYER'S GUIDE · BOUNDLESS ChatX",
    title: lang === "zh" ? "跨境 AI 客服工具" : "AI customer-chat tools",
    titleAccent: lang === "zh" ? "怎么选（2026）" : "how to choose (2026)",
    subtitle:
      lang === "zh"
        ? "五维选型框架 · 五款工具逐项对比：数据主权 / 渠道形态 / AI 深度 / 计费 / 合规"
        : "A five-dimension framework across five tools: data sovereignty, channels, AI depth, billing, compliance",
    foot: CHATX_FOOT[lang],
  });
}

/** 客户端下载页 OG（`/download/chatx` 与 `/en/download/chatx`）。
 *  刻意不放版本号：那会让 OG 图变成第 4 处版本事实源（构建时快照，发版后就是旧的）。 */
export function downloadOgImage(lang: "zh" | "en") {
  return chatxOgShell({
    kicker: lang === "zh" ? "下载 · BOUNDLESS ChatX" : "DOWNLOAD · BOUNDLESS ChatX",
    title: lang === "zh" ? "六平台，一个 AI 收件箱" : "One AI inbox,",
    titleAccent: lang === "zh" ? undefined : "six platforms",
    subtitle: "Telegram · WhatsApp · Messenger · LINE · Zalo · Instagram",
    foot:
      lang === "zh"
        ? "Windows 桌面端 · 免费开始 · 数据留本机"
        : "Windows desktop · free to start · data stays local",
  });
}
