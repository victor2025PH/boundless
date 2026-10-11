import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import PricingPage from "@/components/PricingPage";
import { SITE_URL } from "@/lib/site";
import { tokenPackOffers, translateOffers, toSchemaOffer } from "@/lib/pricing";
import { pricingFaqJsonLd } from "@/lib/pricing-faq";

const LANGUAGES = { "zh-CN": "/pricing", en: "/en/pricing", "x-default": "/pricing" };

export const metadata: Metadata = {
  title: "Pricing · ChatX — start free, pay by top-up, no subscription · BOUNDLESS",
  description:
    "Start free and pay only by top-up — no subscription: download and go. Standard translation uses this deployment's local model, with no per-character token charge and no paid translation API; if that model is not set, the line waits for a person. 1,000 tokens/mo plus 10,000 on signup. Top up from 50U at 1U = 1,500 tokens; first top-up earns up to +40% (100U +5% · 200U +10% · 500U +20% · 1000U +30% · 5000U +35% · 10000U +40%). Newcomer pack: 6U for 18,000 tokens at double rate within 72h of signup. Enterprise partnership and private deployment quoted by sales.",
  alternates: { canonical: "/en/pricing", languages: LANGUAGES },
  openGraph: {
    title: "Pricing · Start free, top up as you go · BOUNDLESS",
    description:
      "ChatX top-up pricing: from 50U with up to +40% on your first top-up; newcomer 6U pack lands 18,000 tokens at double rate. Standard translation uses the local model and does not call a paid API. Enterprise and private deployment by quote.",
    url: `${SITE_URL}/en/pricing`,
  },
};

const pricingLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "ChatX — AI closing chat system (start free · pay by top-up)",
  description:
    "Omni-channel inbox, including Douyin enterprise and TikTok official customer service, plus AI personas, local-model standard translation, and human handoff. Start free, top up as you go: from 50U with up to +40% on the first top-up; newcomer 6U pack at double rate. Standard translation does not call a paid API; if the local model is not set, a person takes the line.",
  brand: { "@type": "Organization", name: "BOUNDLESS" },
  offers: [...tokenPackOffers, ...translateOffers].map(toSchemaOffer),
};

// Page-level FAQPage schema (GEO batch 2) — same source as the visible FAQ (lib/pricing-faq).
const faqLd = pricingFaqJsonLd(false);

export default function PricingRouteEn() {
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
