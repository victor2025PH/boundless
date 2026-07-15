import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ManualSection from "@/components/ManualSection";
import { SITE_URL } from "@/lib/site";
import { LATEST_VERSION } from "@/lib/releaseNotes";

const LANGUAGES = { "zh-CN": "/manual", en: "/en/manual", "x-default": "/manual" };

export const metadata: Metadata = {
  title: "AvatarHub User Manual · BOUNDLESS",
  description:
    "The complete AvatarHub manual: system requirements, download & install, activation, voice cloning, live face swap, phone interpreting, updates and troubleshooting. Read online or print to PDF.",
  alternates: { canonical: "/en/manual", languages: LANGUAGES },
  openGraph: {
    title: "AvatarHub User Manual · BOUNDLESS",
    description: "The complete guide from install to going live. Read online or export as PDF.",
    url: `${SITE_URL}/en/manual`,
  },
};

const guideLd = {
  "@context": "https://schema.org",
  "@type": "TechArticle",
  headline: "AvatarHub User Manual",
  about: "Installation, activation, usage and troubleshooting guide for the AvatarHub real-time digital human engine",
  inLanguage: "en",
  version: LATEST_VERSION,
  publisher: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
};

export default function ManualPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(guideLd) }} />
      <Navbar />
      <ManualSection />
      <Footer />
    </main>
  );
}
