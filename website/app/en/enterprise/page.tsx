import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import EnterprisePage from "@/components/EnterprisePage";
import { SITE_URL } from "@/lib/site";
import { enterpriseFaqJsonLd, enterpriseServiceJsonLd } from "@/lib/enterpriseContent";

const LANGUAGES = { "zh-CN": "/enterprise", en: "/en/enterprise", "x-default": "/enterprise" };

export const metadata: Metadata = {
  title: "Enterprise · frame deals & private deployment · ChatX · BOUNDLESS",
  description:
    "Bring AI closing chat into your organization: enterprise frame deals (managed cloud, negotiated pricing, monthly corporate invoicing) or private deployment (full engine on-prem, data never leaves your network, unlimited local-model tokens, one-time setup + annual license & care). Sales replies within 1 business day.",
  alternates: { canonical: "/en/enterprise", languages: LANGUAGES },
  openGraph: {
    title: "Enterprise · frame deals / private deployment · BOUNDLESS",
    description:
      "One AI closing-chat engine, two enterprise shapes: managed cloud frame deals, or private deployment that keeps data on-prem. Survey → PoC → deploy → care, typically live in 1–2 weeks.",
    url: `${SITE_URL}/en/enterprise`,
  },
};

export default function EnterpriseRouteEn() {
  return (
    <main className="relative min-h-screen">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(enterpriseServiceJsonLd("en")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(enterpriseFaqJsonLd("en")) }}
      />
      <Navbar />
      <EnterprisePage />
      <Footer />
    </main>
  );
}
