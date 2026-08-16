import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import OrderPanel from "@/components/OrderPanel";
import { SITE_URL } from "@/lib/site";
import { TIERS } from "@/lib/avatarhub-pricing";

export const metadata: Metadata = {
  title: "购买与下单 · 无界科技 BOUNDLESS",
  description:
    "AvatarHub 会员套餐与授权购买：声音克隆、实时换脸、数字人直播、克隆音同传。引擎跑在你自己的设备上，用量不限、不按字符或时长计费；设备自备、协助部署。月付 / 年付（送 2 个月 + 首年 8 折），全程 USDT 结算。",
  alternates: {
    canonical: "/order",
    languages: { "zh-CN": "/order", en: "/en/order", "x-default": "/order" },
  },
  openGraph: {
    title: "购买与下单 · 无界科技 BOUNDLESS",
    description: "会员套餐 39–699 USD/月，本机算力用量不限，年付送 2 个月 + 首年 8 折。支持 USDT 结算，数据不出机房。",
    url: `${SITE_URL}/order`,
  },
};

const offersLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "AvatarHub — BOUNDLESS 实时数字人引擎",
  description: "声音克隆、实时换脸、数字人直播、克隆音同传的本地部署引擎，会员订阅制。",
  brand: { "@type": "Organization", name: "无界科技 BOUNDLESS" },
  offers: TIERS.filter((t) => t.monthly > 0).map((t) => ({
    "@type": "Offer",
    name: t.name.zh,
    price: String(t.monthly),
    priceCurrency: "USD",
    description: `${t.audience.zh} · ${t.feats.zh.join(" · ")}`,
  })),
};

export default function OrderPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(offersLd) }} />
      <Navbar />
      <OrderPanel />
      <Footer />
    </main>
  );
}
