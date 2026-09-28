import { ImageResponse } from "next/og";
import { LANDINGS, type LandingKey } from "@/lib/landingContent";
import { compareSpecs } from "@/lib/compare-content";
import { BOT_HERO_COPY, BOT_HERO_SIZE } from "@/lib/chatx-bot-hero";

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
    title: lang === "zh" ? "AI 全自动聊天" : "AI auto-chat",
    titleAccent: lang === "zh" ? "客户消息自动回复、自动成交" : "replies & closes for you",
    subtitle: "Telegram · WhatsApp · Messenger · LINE · Zalo · Instagram",
    foot:
      lang === "zh"
        ? "Windows 桌面端 · 免费开始 · 数据留本机"
        : "Windows desktop · free to start · data stays local",
  });
}

/* ── @ChatX_bot 首条海报（Telegram sendPhoto，960×720 · 4:3）────────────────
 * 广告 → bot 的第一眼。手机聊天窗按宽度铺满（≈360px），所以只放三层大字：标题 / 副标 / 两条卖点 + CTA 条，
 * 产品截图（public/products/prod-chatx.jpg）压在右下角当「产品长什么样」的背景，上方深空渐变承载文案。
 * 强调色走 growth 品类令牌（#1e8cf2），与 /download/chatx 落地页同色。文案与尺寸在 lib/chatx-bot-hero.ts（bot 侧共用）。
 * 底图由调用方以 data URL 传入（edge route 用 fetch(new URL(..., import.meta.url)) 内联）。 */
const GROWTH_ACCENT = "#1e8cf2";

export function chatxBotHeroImage(lang: "zh" | "en", bgDataUrl: string) {
  const c = BOT_HERO_COPY[lang];
  const { width: W, height: H } = BOT_HERO_SIZE;
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          position: "relative",
          background: "#05060f",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        {bgDataUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={bgDataUrl}
            alt=""
            width={900}
            height={600}
            style={{ position: "absolute", left: 180, top: 300, width: 900, height: 600, objectFit: "cover" }}
          />
        ) : null}
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            width: W,
            height: H,
            display: "flex",
            background:
              "linear-gradient(180deg, #05060f 0%, #05060f 40%, rgba(5,6,15,0.9) 54%, rgba(5,6,15,0.55) 70%, rgba(5,6,15,0.25) 100%)",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            width: W,
            height: H,
            display: "flex",
            background: "linear-gradient(90deg, rgba(5,6,15,0.85) 0%, rgba(5,6,15,0.35) 45%, rgba(5,6,15,0) 75%)",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            width: W,
            height: H,
            display: "flex",
            flexDirection: "column",
            padding: "56px 60px 0 60px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 30, color: GROWTH_ACCENT, letterSpacing: 3, fontWeight: 700 }}>
            <div style={{ display: "flex", width: 14, height: 14, borderRadius: 7, background: GROWTH_ACCENT }} />
            <span>{c.kicker}</span>
          </div>
          <div style={{ display: "flex", fontSize: 104, fontWeight: 800, lineHeight: 1.1, marginTop: 18, letterSpacing: -1 }}>
            {c.title}
          </div>
          <div style={{ display: "flex", fontSize: 46, fontWeight: 700, color: GROWTH_ACCENT, marginTop: 10, lineHeight: 1.2 }}>
            {c.titleAccent}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 44, fontSize: 34, color: "#e2e8f0" }}>
            {c.points.map((p) => (
              <div key={p} style={{ display: "flex", alignItems: "center", gap: 16 }}>
                <div style={{ display: "flex", width: 12, height: 12, borderRadius: 6, background: GROWTH_ACCENT }} />
                <span>{p}</span>
              </div>
            ))}
          </div>
          <div
            style={{
              display: "flex",
              alignSelf: "flex-start",
              marginTop: 48,
              padding: "18px 32px",
              borderRadius: 18,
              fontSize: 34,
              fontWeight: 700,
              color: "white",
              background: "linear-gradient(135deg, #0070f0 0%, #00c2ff 100%)",
              boxShadow: "0 12px 40px rgba(30,140,242,0.35)",
            }}
          >
            {c.foot}
          </div>
        </div>
      </div>
    ),
    BOT_HERO_SIZE
  );
}
