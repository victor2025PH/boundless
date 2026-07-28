import { ImageResponse } from "next/og";
import { LANDINGS, type LandingKey } from "@/lib/landingContent";

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
