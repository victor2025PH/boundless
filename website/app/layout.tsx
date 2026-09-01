import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";
import { LanguageProvider } from "@/components/LanguageContext";
import { TelegramProvider } from "@/components/TelegramProvider";
import GlobalChrome from "@/components/GlobalChrome";
import TgRedirect from "@/components/TgRedirect";
import { SITE_URL, CONTACT_URL, CHANNEL_URL, GROUP_URL, BOT_URL } from "@/lib/site";
import { tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";
import { studioSchemaOffers } from "@/lib/avatarhub-pricing";
import { BRAND, PRODUCT_ORDER, type ProductKey } from "@/lib/brand";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "无界科技 BOUNDLESS · 让沟通无界",
  description:
    "无界科技 BOUNDLESS：用 AI 打破语言与沟通的边界。跨境实时翻译 SCRM、AI 自动成交聊天、声音克隆与数字人，私有部署、数据不出网。BOUNDLESS: cross-border real-time translation SCRM, AI auto-closing chat, voice cloning and digital humans — privately deployed, data stays on-prem.",
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
  // 站长工具验证（GSC / Bing / Naver）：令牌写在 VPS .env.local，未配置时不渲染标签。
  // 验证一次后令牌需永久保留（各家都会周期性复查，删了会掉验证）。
  // GSC 另有一份 HTML 文件验证已在线（public/googlec972f84ce3f53ec7.html），两种方式可并存。
  // Bing（msvalidate.01）是 ChatGPT 搜索的索引底座——若走「从 GSC 一键导入」则无需此令牌。
  verification: {
    ...(process.env.GOOGLE_SITE_VERIFICATION
      ? { google: process.env.GOOGLE_SITE_VERIFICATION }
      : {}),
    ...((process.env.BING_SITE_VERIFICATION || process.env.NAVER_SITE_VERIFICATION)
      ? {
          other: {
            ...(process.env.BING_SITE_VERIFICATION
              ? { "msvalidate.01": process.env.BING_SITE_VERIFICATION }
              : {}),
            ...(process.env.NAVER_SITE_VERIFICATION
              ? { "naver-site-verification": process.env.NAVER_SITE_VERIFICATION }
              : {}),
          },
        }
      : {}),
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    title: "无界科技 BOUNDLESS · 让沟通无界",
    description:
      "跨境实时翻译 SCRM · AI 自动成交聊天 · 声音克隆 · 数字人。自主可控私有部署，数据不出网，合规可溯源。",
    siteName: "无界科技 BOUNDLESS",
    // 全站默认分享图（品牌片主视觉）：此前未配置，TG/社媒链接预览无脸
    images: [{ url: "/brand/campaign/og-film.jpg", width: 1200, height: 675, alt: "BOUNDLESS AvatarHub" }],
  },
  // 实施78 P0-2（2026-08-28）：twitter 卡刻意**只留 card 类型与图**，不再在根 layout 写
  // 中文 title/description。原因是 metadata 的继承语义——各页普遍覆写了 openGraph 但**没有
  // 覆写 twitter**，于是每个 /en 页面分享到 X / LinkedIn 时预览标题都是「无界科技 BOUNDLESS ·
  // 让沟通无界」（线上实测 twitter:title 全站中文）。去掉后 X/LinkedIn 按规范回落 og:*——
  // 中文页回落本 layout 的中文 og、英文页回落各自页面的英文 og，两侧自动正确，
  // 不需要给每页再写一份 twitter 文案（少一份要同步维护的重复口径）。
  twitter: {
    card: "summary_large_image",
    images: ["/brand/campaign/og-film.jpg"],
  },
};

// 实体锚定（实施77 GEO 批次3，2026-08-28）：AI 靠实体图谱消歧——目标是让「无界科技 / BOUNDLESS /
// 智聊 ChatX」在模型眼里是一个**可识别的实体**，而不是一串偶然同现的字。故补三样：
//   @id  → 全站可被其他 schema 节点引用的稳定标识（下载页 SoftwareApplication 的 publisher 指它）；
//   sameAs → 官方账号矩阵（Telegram 频道/群/客服/Bot）；有了 GitHub / Wikidata / 目录站条目后追加到这里；
//   knowsAbout → 我们「懂什么」的主题词，是 AI 判断「该不该在这个问题里提到我们」的直接依据。
// 纪律：sameAs 只放**我们真正控制**的官方账号页（第三方评测/媒体报道不算，那属 mentions 不属 identity）。
const ORG_ID = `${SITE_URL}/#organization`;

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  "@id": ORG_ID,
  name: "无界科技 BOUNDLESS",
  alternateName: ["BOUNDLESS", "无界科技", "华灵科技", "HuaLing Tech"],
  url: SITE_URL,
  logo: {
    "@type": "ImageObject",
    url: `${SITE_URL}/brand/logos/boundless-mark-512.png`,
    width: 512,
    height: 512,
  },
  image: `${SITE_URL}/brand/campaign/og-film.jpg`,
  slogan: "让沟通，无界 · Communication, Boundless.",
  description:
    "BOUNDLESS: an AI software company breaking the barriers of language, communication and voice — cross-border real-time translation SCRM, AI auto-closing chat, voice cloning and digital humans, on a self-controlled private-deployment base. Verifiably compliant (C2PA-watermarked).",
  knowsAbout: [
    "跨境电商客服自动化 / Cross-border e-commerce customer service automation",
    "AI 自动回复与成交跟单 / AI auto-reply and sales follow-up",
    "全渠道统一收件箱（Telegram / WhatsApp / LINE / Messenger）",
    "实时互译与拟人化翻译 / Real-time human-like translation",
    "声音克隆与语音消息 / Voice cloning and voice messaging",
    "数字人与 AI 短视频 / Digital humans and AI video",
    "私有化部署与数据主权 / Private deployment and data sovereignty",
    "AI 合规披露（EU AI Act Art. 50 / California SB 243）",
  ],
  sameAs: [CONTACT_URL, CHANNEL_URL, GROUP_URL, BOT_URL],
};

// WebSite 节点：把「站点」与「组织」在实体图谱里显式连起来（publisher 指回 Organization），
// 并声明站点提供的语言版本——多语命中是 AI 决定「给哪个地区用户推荐谁」的输入之一。
const websiteLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  "@id": `${SITE_URL}/#website`,
  url: SITE_URL,
  name: "无界科技 BOUNDLESS",
  inLanguage: ["zh-CN", "en", "ko", "ja"],
  publisher: { "@id": ORG_ID },
};

// 产品结构化数据（Service）：名称/描述取自 lib/brand.ts 单一数据源。
// 已落地定价的产品挂 offers（2026-07-18 起报价币种全线 USD）：LingoX（通译·主推现金流）/
// ChatX（自动成交三档）/ VoiceX（幻声 → 2026-08-04 起随幻境 STUDIO 会员挂牌，offers 由
// avatarhub-pricing.TIERS 派生——此前挂 voiceOffers 旧价 18/78/198 与页面五档打架）。
// 不进公开 JSON-LD 的线（2026-07-26 合规收口）：facex / matrixx（gated 合规隔离，
// 见 lib/isolation.ts）、livex（描述含 face-swap 类目词，随隔离一并撤出结构化数据）、
// fatex（未上线）；per-usage 计量 SKU 亦不进。锚点均指向仍存在的页面/section，避免坏链。
const SCHEMA_HIDDEN: ReadonlySet<ProductKey> = new Set(["facex", "livex", "matrixx", "fatex"]);
const PRODUCT_OFFERS: Partial<Record<ProductKey, Parameters<typeof toSchemaOffer>[0][]>> = {
  // 2026-08-04 通译并入智聊；2026-08-21 充值唯一化：订阅停售，chatx 结构化数据只挂
  // Token 充值档（含新人包）+ 翻译工作台（数字全部派生自 chatx-pricing.ts）。
  chatx: [...tokenPackOffers, ...translateOffers],
  voicex: studioSchemaOffers(),
};
const PRODUCT_SCHEMA_ANCHOR: Partial<Record<ProductKey, string>> = {
  reachx: "#autochat",
  chatx: "#autochat",
  voicex: "voice",
  voxx: "interpreting",
};
const productServices = PRODUCT_ORDER.filter((key) => !SCHEMA_HIDDEN.has(key)).map((key) => {
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
    url: `${SITE_URL}/${PRODUCT_SCHEMA_ANCHOR[key] ?? "#products"}`,
    ...(offers ? { offers: offers.map(toSchemaOffer) } : {}),
  };
});

// FAQPage schema 已移出根 layout（实施77 GEO 批次2，2026-08-27）：全站每页注入同一份
// 英文 FAQ 违反「一页一 FAQPage、schema 须对应本页可见内容」的规范，且会与 /compare
// /pricing 的页面级 FAQPage 撞车。现落点＝首页 app/page.tsx（zh）+ app/en/page.tsx（en），
// 语言随路由；内容仍取 content.{zh,en}.faq.items 单源。

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
        {/* 会话归因跨页暂存：AI 坐席链接可能先落首页（如收益试算器锚点 /?ref=..#autochat），
            用户逛完再点「下单」时 URL 上的 ?ref 已丢。任何页面带 ?ref 进站即暂存
            localStorage（OrderPanel 无 URL ref 时 7 天内兜底读取），归因不断链。 */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var m=location.search.match(/[?&]ref=([^&]+)/);if(m){var v=decodeURIComponent(m[1]).slice(0,160);localStorage.setItem('bl-ref',v);localStorage.setItem('bl-ref-ts',String(Date.now()));}}catch(e){}})();",
          }}
        />
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(websiteLd) }}
        />
        {productServices.map((svc) => (
          <script
            key={svc.name}
            type="application/ld+json"
            dangerouslySetInnerHTML={{ __html: JSON.stringify(svc) }}
          />
        ))}
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
