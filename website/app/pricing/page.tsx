import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import PricingPage from "@/components/PricingPage";
import { SITE_URL } from "@/lib/site";
import { tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";
import { pricingFaqJsonLd } from "@/lib/pricing-faq";

const LANGUAGES = { "zh-CN": "/pricing", en: "/en/pricing", "x-default": "/pricing" };

export const metadata: Metadata = {
  title: "价格 · 智聊 ChatX 按充值计费 · 不订阅 · 无界科技 BOUNDLESS",
  description:
    "免费开始，按充值计费，不订阅：下载即用 + 标准翻译永久免费不限字符 + 每月 1,000 Token（注册再送 10,000）。充值 50U 起、1U = 1,500 Token，首充加赠最高 +40%（100U +5% · 200U +10% · 500U +20% · 1000U +30% · 5000U +35% · 10000U +40%）；新人 6U 大礼包 18,000 Token 双倍到账（注册 72 小时内）。企业合作年框与企业级私有化部署面议。",
  alternates: { canonical: "/pricing", languages: LANGUAGES },
  openGraph: {
    title: "价格 · 免费开始，充多少用多少 · 无界科技",
    description:
      "智聊 ChatX 充值计费：50U 起充、首充最高 +40%，新人 6U 大礼包双倍到账。标准翻译免费不限量，用尽自动降级永不断线；企业合作 / 私有化部署面议。",
    url: `${SITE_URL}/pricing`,
  },
};

// 充值计费结构化数据（充值档 + 新人包 + 翻译工作台；数字派生自 chatx-pricing 单源；
// 2026-08-21 充值唯一化：停售订阅不进 JSON-LD）
const pricingLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "智聊 ChatX — AI 成交聊天系统（免费开始 · 按充值计费）",
  description:
    "多平台统一收件箱 + AI 人设承接 + 免费标准翻译 + 人工接管。免费开始，充多少用多少：50U 起充、首充最高 +40%、新人 6U 大礼包双倍到账；标准翻译永久免费不限字符。",
  brand: { "@type": "Organization", name: "无界科技 BOUNDLESS" },
  offers: [...tokenPackOffers, ...translateOffers].map(toSchemaOffer),
};

// 页面级 FAQPage schema（实施77 GEO 批次2）：与 PricingPage 可见 FAQ 同源（lib/pricing-faq）。
const faqLd = pricingFaqJsonLd(true);

export default function PricingRoute() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(pricingLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(faqLd) }} />
      <Navbar />
      <PricingPage />
      <Footer />
    </main>
  );
}
