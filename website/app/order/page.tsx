import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import OrderPanel from "@/components/OrderPanel";
import { SITE_URL } from "@/lib/site";
import { TIERS } from "@/lib/avatarhub-pricing";

export const metadata: Metadata = {
  title: "购买与下单 · 无界科技 BOUNDLESS",
  description:
    "幻境 STUDIO 会员套餐与授权购买：免费换脸（带水印）起步，付费解锁 AI 作图、视频换脸、直播换脸、变声器与同声传译。引擎跑在你自己的设备上，用量不限、不按字符或时长计费；设备自备、协助部署。月付 / 季付 / 年付，全程 USDT 结算；旗舰版私有部署请咨询客服获取报价方案。",
  alternates: {
    canonical: "/order",
    languages: { "zh-CN": "/order", en: "/en/order", "x-default": "/order" },
  },
  openGraph: {
    title: "购买与下单 · 无界科技 BOUNDLESS",
    description: "幻境 STUDIO 免费换脸起步，会员 39 USD/月起（月付 / 季付 / 年付），本机算力用量不限；旗舰版私有部署咨询客服。支持 USDT 结算，数据不出机房。",
    url: `${SITE_URL}/order`,
  },
};

const offersLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "幻境 STUDIO — BOUNDLESS 实时数字人引擎",
  description: "AI 作图、图片 / 视频换脸、直播实时换脸、变声器与克隆音同传的本地部署引擎，会员订阅制。",
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
