import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import OrderPanel from "@/components/OrderPanel";
import { SITE_URL } from "@/lib/site";
import { TIERS } from "@/lib/avatarhub-pricing";

const LANGUAGES = { "zh-CN": "/order", en: "/en/order", "x-default": "/order" };

export const metadata: Metadata = {
  title: "Plans & Ordering · BOUNDLESS",
  description:
    "AvatarHub plans and licensing: voice cloning, live face swap, digital-human streaming and interpreting. Runs on your own hardware — unlimited usage, no per-character or per-minute metering; we assist deployment. Monthly or annual (2 months free + 20% off year one), settled in USDT.",
  alternates: { canonical: "/en/order", languages: LANGUAGES },
  openGraph: {
    title: "Plans & Ordering · BOUNDLESS",
    description: "Plans from 39 to 699 USD/mo, unlimited usage on your own hardware. Annual gets 2 months free + 20% off year one. Data stays on-prem.",
    url: `${SITE_URL}/en/order`,
  },
};

const offersLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "AvatarHub — BOUNDLESS real-time digital human engine",
  description: "Locally deployed engine for voice cloning, live face swap, digital-human streaming and interpreting.",
  brand: { "@type": "Organization", name: "BOUNDLESS" },
  offers: TIERS.filter((t) => t.monthly > 0).map((t) => ({
    "@type": "Offer",
    name: t.name.en,
    price: String(t.monthly),
    priceCurrency: "USD",
    description: `${t.audience.en} · ${t.feats.en.join(" · ")}`,
  })),
};

export default function OrderPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(offersLd) }} />
      <Navbar />
      <OrderPanel />
      <Footer />
    </main>
  );
}
