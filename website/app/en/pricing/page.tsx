import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import PricingPage from "@/components/PricingPage";
import { SITE_URL } from "@/lib/site";
import { autochatOffers, tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";

const LANGUAGES = { "zh-CN": "/pricing", en: "/en/pricing", "x-default": "/pricing" };

export const metadata: Metadata = {
  title: "Pricing · ChatX token plans · BOUNDLESS",
  description:
    "Standard translation free forever with unlimited characters. AI replies, pro translation and cloned voice meter at published token rates. Free plan (10,000 bonus tokens on signup) · Personal 39 USD/mo · Flex pay-as-you-go · Team 49 USD/seat/mo (2+ seats) · Flagship from 598. Token packs from 9.9 USD, valid 12 months; annual billing = 2 months free.",
  alternates: { canonical: "/en/pricing", languages: LANGUAGES },
  openGraph: {
    title: "Pricing · Translation free forever — pay only to close · BOUNDLESS",
    description:
      "ChatX new pricing: Free / Personal 39 / Flex / Team 49 per seat / Flagship 598. Unlimited free standard translation, transparent token metering, graceful degradation — never offline.",
    url: `${SITE_URL}/en/pricing`,
  },
};

const pricingLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "ChatX — AI closing chat system (token plans)",
  description:
    "Omni-channel unified inbox + AI personas + free standard translation + human handoff. Plans include monthly tokens; packs top you up. Standard translation is free forever.",
  brand: { "@type": "Organization", name: "BOUNDLESS" },
  offers: [...autochatOffers, ...tokenPackOffers, ...translateOffers].map(toSchemaOffer),
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
