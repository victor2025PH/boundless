import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";
import { LanguageProvider } from "@/components/LanguageContext";
import { TelegramProvider } from "@/components/TelegramProvider";
import GlobalChrome from "@/components/GlobalChrome";
import TgRedirect from "@/components/TgRedirect";
import { SITE_URL, CONTACT_URL } from "@/lib/site";
import { content } from "@/lib/content";
import { realtimeOffers, autochatOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";
import { BRAND, PRODUCT_ORDER, type ProductKey } from "@/lib/brand";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "无界科技 BOUNDLESS · 让沟通无界",
  description:
    "无界科技 BOUNDLESS：用 AI 打破语言、沟通、声音、容貌的边界。跨境实时翻译 SCRM、AI 自动成交聊天、声音克隆、数字人；换脸 / 直播分身按需定制。私有部署、合规可溯源。BOUNDLESS: cross-border real-time translation SCRM, AI auto-closing chat, voice cloning and digital humans; face & live-swap available as custom projects — privately deployed, verifiably compliant.",
  keywords: [
    "无界科技",
    "BOUNDLESS",
    "跨境翻译",
    "实时翻译",
    "翻译SCRM",
    "AI自动成交",
    "聊天聚合",
    "声音克隆",
    "数字人",
    "AI换脸",
    "私有部署",
    "合规可溯源",
    // 旧品牌词保留，承接更名期的搜索流量
    "华灵科技",
    "HuaLing Tech",
    "华影",
    "灵犀",
  ],
  alternates: {
    canonical: "/",
    languages: { "zh-CN": "/", en: "/en", "x-default": "/" },
  },
  // 站长工具验证（GSC / Naver）：令牌写在 VPS .env.local，未配置时不渲染标签。
  // 验证一次后令牌需永久保留（GSC 会周期性复查）。
  verification: {
    ...(process.env.GOOGLE_SITE_VERIFICATION
      ? { google: process.env.GOOGLE_SITE_VERIFICATION }
      : {}),
    ...(process.env.NAVER_SITE_VERIFICATION
      ? { other: { "naver-site-verification": process.env.NAVER_SITE_VERIFICATION } }
      : {}),
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    title: "无界科技 BOUNDLESS · 让沟通无界",
    description:
      "跨境实时翻译 SCRM · AI 自动成交聊天 · 声音克隆 · 数字人。换脸 / 直播分身按需定制。自主可控私有部署，合规可溯源。",
    siteName: "无界科技 BOUNDLESS",
  },
  twitter: {
    card: "summary_large_image",
    title: "无界科技 BOUNDLESS · 让沟通无界",
    description:
      "跨境实时翻译 SCRM · AI 自动成交聊天 · 声音克隆 · 数字人。换脸 / 直播分身按需定制。私有部署，合规可溯源。",
  },
};

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "无界科技 BOUNDLESS",
  url: SITE_URL,
  slogan: "让沟通，无界 · Communication, Boundless.",
  description:
    "BOUNDLESS: an AI software company breaking the barriers of language, communication, voice and identity — cross-border real-time translation SCRM, AI auto-closing chat, voice cloning and digital humans, with face & live-swap as custom projects, on a self-controlled private-deployment base. Verifiably compliant (C2PA-watermarked).",
  sameAs: [CONTACT_URL],
};

// 产品结构化数据（Service）：名称/描述取自 lib/brand.ts 单一数据源。
// 已落地定价的产品挂 offers：LingoX（通译·主推现金流，USD）/ ChatX（自动成交）/
// LiveX（实时换脸换声·定制部署，USDT 遗留轨道）。其余定价上线再补。
// 锚点均指向已存在的首页 section，避免坏链。
const PRODUCT_OFFERS: Partial<Record<ProductKey, Parameters<typeof toSchemaOffer>[0][]>> = {
  lingox: translateOffers,
  chatx: autochatOffers,
  livex: realtimeOffers,
};
const PRODUCT_SCHEMA_ANCHOR: Record<ProductKey, string> = {
  reachx: "#autochat",
  chatx: "#autochat",
  facex: "#showcase",
  voicex: "#realtime",
  livex: "#realtime",
  lingox: "#translate",
  voxx: "#showcase",
};
const productServices = PRODUCT_ORDER.map((key) => {
  const p = BRAND.products[key];
  const offers = PRODUCT_OFFERS[key];
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: `${p.en} (${p.zh}) — ${p.desc.en}`,
    serviceType: p.desc.en,
    description: `${p.en}: ${p.desc.en}. Part of BOUNDLESS — breaking ${p.break.en}. Privately deployed on your own hardware, data stays off the public net, verifiably compliant.`,
    provider: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
    areaServed: "Global",
    url: `${SITE_URL}/${PRODUCT_SCHEMA_ANCHOR[key]}`,
    ...(offers ? { offers: offers.map(toSchemaOffer) } : {}),
  };
});

const faqLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: content.en.faq.items.map((it) => ({
    "@type": "Question",
    name: it.q,
    acceptedAnswer: { "@type": "Answer", text: it.a },
  })),
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // 活动皮肤:在 .env.local 设 NEXT_PUBLIC_FX_THEME=gold|emerald|crimson 后重新构建,
  // 全站背景氛围整体换色(预设见 globals.css);未设置时保持默认青紫
  const fxTheme = process.env.NEXT_PUBLIC_FX_THEME;
  return (
    <html lang="zh-CN" {...(fxTheme ? { "data-theme": fxTheme } : {})}>
      <body>
        {/* Set <html lang> to match the route locale before hydration (no dynamic render cost).
            Static HTML defaults to zh-CN; this corrects /en* for screen readers & JS crawlers. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var p=location.pathname;var m=p.match(/^\\/(ko|ja)(\\/|$)/);document.documentElement.lang=m?m[1]:(p==='/en'||p.indexOf('/en/')===0)?'en':'zh-CN';}catch(e){}})();",
          }}
        />
        {/* 特效静态挡位:低内存/少核设备在首帧前降档(样式按 html[data-fx] 裁剪),
            避免弱 GPU 桌面机被判为高配后掉帧。运行时 FPS 探针属 P2,后续叠加。 */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var n=navigator,low=(n.deviceMemory&&n.deviceMemory<=4)||(n.hardwareConcurrency&&n.hardwareConcurrency<=4);document.documentElement.setAttribute('data-fx',low?'low':'high');}catch(e){}})();",
          }}
        />
        {/* 白天本色模式:首帧前判定,防止先黑后白的闪烁。
            手动选择(localStorage bl-mode)优先,否则跟随系统 prefers-color-scheme;
            /admin(夜间工作台)与 /app(Telegram 内嵌,随 TG 深色主题)不参与。
            切换按钮见 components/ModeToggle.tsx */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var p=location.pathname;if(p.indexOf('/admin')===0||p==='/app'||p.indexOf('/app/')===0)return;var m=null;try{m=localStorage.getItem('bl-mode')}catch(e){}var day=m?m==='day':matchMedia('(prefers-color-scheme: light)').matches;if(day)document.documentElement.setAttribute('data-mode','day');}catch(e){}})();",
          }}
        />
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
        {productServices.map((svc) => (
          <script
            key={svc.name}
            type="application/ld+json"
            dangerouslySetInnerHTML={{ __html: JSON.stringify(svc) }}
          />
        ))}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(faqLd) }}
        />
        <TelegramProvider>
          <LanguageProvider>
            <TgRedirect />
            <GlobalChrome />
            {children}
          </LanguageProvider>
        </TelegramProvider>
      </body>
    </html>
  );
}
