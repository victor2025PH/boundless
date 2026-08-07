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
    "STUDIO plans and licensing: free face swap (watermarked) to start; paid tiers unlock AI image gen, video face swap, live face swap, voice changer and interpreting. Runs on your own hardware — unlimited usage, no per-character or per-minute metering; we assist deployment. Monthly, quarterly or annual billing, settled in USDT; Flagship private deployment is quoted by sales.",
  alternates: { canonical: "/en/order", languages: LANGUAGES },
  openGraph: {
    title: "Plans & Ordering · BOUNDLESS",
    description: "STUDIO starts free (watermarked face swap); paid plans from 39 USD/mo with monthly / quarterly / annual billing, unlimited usage on your own hardware. Flagship private deployment: contact sales. Data stays on-prem.",
    url: `${SITE_URL}/en/order`,
  },
};

const offersLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "STUDIO — BOUNDLESS real-time digital human engine",
  description: "Locally deployed engine for AI image gen, photo / video face swap, live face swap, voice changer and cloned-voice interpreting.",
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
