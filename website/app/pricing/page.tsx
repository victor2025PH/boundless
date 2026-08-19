import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import PricingPage from "@/components/PricingPage";
import { SITE_URL } from "@/lib/site";
import { autochatOffers, tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";

const LANGUAGES = { "zh-CN": "/pricing", en: "/en/pricing", "x-default": "/pricing" };

export const metadata: Metadata = {
  title: "价格 · 智聊 ChatX Token 分层套餐 · 无界科技 BOUNDLESS",
  description:
    "标准翻译永久免费不限字符；AI 回复 / 专业翻译 / 克隆语音按公示 Token 费率计量。免费版下载即用（注册送 10,000 体验 Token）· 个人版 39 USD/月 · 按量版 0 月费 · 团队版 49 USD/坐席/月（≥2 席）· 旗舰私有化 598 起。Token 包 9.9 USD 起，跨产品通用、12 个月有效；年付送 2 个月。",
  alternates: { canonical: "/pricing", languages: LANGUAGES },
  openGraph: {
    title: "价格 · 翻译永久免费，只为成交付费 · 无界科技",
    description:
      "智聊 ChatX 新价格体系：免费版 / 个人版 39 / 按量版 / 团队版 49 每坐席 / 旗舰 598。标准翻译免费不限量，AI 用量按 Token 透明计价，用尽自动降级永不断线。",
    url: `${SITE_URL}/pricing`,
  },
};

// Token 分层体系全量结构化数据（订阅 + Token 包 + 翻译工作台；数字派生自 chatx-pricing 单源）
const pricingLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "智聊 ChatX — AI 成交聊天系统（Token 分层套餐）",
  description:
    "多平台统一收件箱 + AI 人设承接 + 免费标准翻译 + 人工接管。订阅含每月 Token，超出按 Token 包加购；标准翻译永久免费不限字符。",
  brand: { "@type": "Organization", name: "无界科技 BOUNDLESS" },
  offers: [...autochatOffers, ...tokenPackOffers, ...translateOffers].map(toSchemaOffer),
};

export default function PricingRoute() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(pricingLd) }} />
      <Navbar />
      <PricingPage />
      <Footer />
    </main>
  );
}
