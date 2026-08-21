import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import PricingPage from "@/components/PricingPage";
import { SITE_URL } from "@/lib/site";
import { tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";

const LANGUAGES = { "zh-CN": "/pricing", en: "/en/pricing", "x-default": "/pricing" };

export const metadata: Metadata = {
  title: "Pricing · ChatX — start free, pay by top-up, no subscription · BOUNDLESS",
  description:
    "Start free and pay only by top-up — no subscription: download & go, unlimited free standard translation, 1,000 tokens/mo plus 10,000 on signup. Top up from 50U at 1U = 1,500 tokens; first top-up earns up to +40% (100U +5% · 200U +10% · 500U +20% · 1000U +30% · 5000U +35% · 10000U +40%). Newcomer pack: 6U for 18,000 tokens at double rate within 72h of signup. Enterprise partnership and private deployment quoted by sales.",
  alternates: { canonical: "/en/pricing", languages: LANGUAGES },
  openGraph: {
    title: "Pricing · Start free, top up as you go · BOUNDLESS",
    description:
      "ChatX top-up pricing: from 50U with up to +40% on your first top-up; newcomer 6U pack lands 18,000 tokens at double rate. Unlimited free standard translation — never offline. Enterprise & private deployment by quote.",
    url: `${SITE_URL}/en/pricing`,
  },
};

const pricingLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "ChatX — AI closing chat system (start free · pay by top-up)",
  description:
    "Omni-channel unified inbox + AI personas + free standard translation + human handoff. Start free, top up as you go: from 50U with up to +40% on the first top-up; newcomer 6U pack at double rate. Standard translation is free forever.",
  brand: { "@type": "Organization", name: "BOUNDLESS" },
  offers: [...tokenPackOffers, ...translateOffers].map(toSchemaOffer),
};

export default function PricingRouteEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(pricingLd) }} />
      <Navbar />
      <PricingPage />
      <Footer />
    </main>
  );
}
