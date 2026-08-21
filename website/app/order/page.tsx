import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import OrderPanel from "@/components/OrderPanel";
import { SITE_URL } from "@/lib/site";
import { TIERS } from "@/lib/avatarhub-pricing";

export const metadata: Metadata = {
  title: "购买与下单 · 无界科技 BOUNDLESS",
  description:
    "自助购买：幻境 STUDIO 会员（免费换脸起步，本机算力用量不限）；智聊 ChatX 按充值计费不订阅（免费开始 · 充值 50U 起 · 首充最高 +40% · 新人 6U 大礼包，标准翻译永久免费），Token 跨智聊/通译通用。USDT / 银行卡结算，到账自动开通。",
  alternates: {
    canonical: "/order",
    languages: { "zh-CN": "/order", en: "/en/order", "x-default": "/order" },
  },
  openGraph: {
    title: "购买与下单 · 无界科技 BOUNDLESS",
    // 与主 description 同一叙事顺序（充值唯一化后主推=智聊充值；此前这里还是纯 STUDIO 旧文案）
    description:
      "告别订阅：智聊 ChatX 充多少用多少——1U = 1,500 Token，首充最高 +40%，新人 6U 大礼包 18,000 Token，标准翻译永久免费。幻境 STUDIO 免费换脸起步、本机算力用量不限。USDT / 银行卡结算，到账自动开通。",
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
