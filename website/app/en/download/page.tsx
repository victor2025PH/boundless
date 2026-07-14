import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import DownloadSection from "@/components/DownloadSection";
import { SITE_URL } from "@/lib/site";

const LANGUAGES = { "zh-CN": "/download", en: "/en/download", "x-default": "/download" };

export const metadata: Metadata = {
  title: "Download the Client · BOUNDLESS",
  description:
    "Download the AvatarHub real-time digital human engine (Windows / macOS): voice cloning, live face swap, digital-human streaming and interpreting. Thin-core installer with on-demand components, SHA-256 verifiable.",
  alternates: { canonical: "/en/download", languages: LANGUAGES },
  openGraph: {
    title: "Download the Client · BOUNDLESS",
    description: "AvatarHub client download: Windows available now, macOS lightweight console coming soon.",
    url: `${SITE_URL}/en/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "AvatarHub",
  applicationCategory: "MultimediaApplication",
  operatingSystem: "Windows 10/11, macOS 12+",
  offers: { "@type": "Offer", price: "0", priceCurrency: "USDT", description: "14-day free trial" },
  publisher: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
};

export default function DownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <DownloadSection />
      <Footer />
    </main>
  );
}
