import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import DownloadHub from "@/components/DownloadHub";
import DownloadSection from "@/components/DownloadSection";
import { SITE_URL } from "@/lib/site";
import { LATEST_VERSION } from "@/lib/releaseNotes";
import { INSTALL_GUIDE, stripRich } from "@/lib/manualContent";
import { CLIENT_APPS } from "@/lib/downloads";

const LANGUAGES = { "zh-CN": "/download", en: "/en/download", "x-default": "/download" };

export const metadata: Metadata = {
  title: "Download Center · BOUNDLESS",
  description:
    "Every desktop client in one place: ChatX, the omni-channel AI chat workspace, and AvatarHub, the real-time digital human engine (voice cloning, live face swap, streaming, interpreting). Windows installers, SHA-256 verifiable, local-first.",
  alternates: { canonical: "/en/download", languages: LANGUAGES },
  openGraph: {
    title: "Download Center · BOUNDLESS",
    description: "All desktop clients in one place: ChatX and AvatarHub. Windows available now.",
    url: `${SITE_URL}/en/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "AvatarHub",
  applicationCategory: "MultimediaApplication",
  operatingSystem: "Windows 10/11, macOS 12+",
  softwareVersion: LATEST_VERSION,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "14-day free trial" },
  publisher: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
};

// HowTo rich-result markup; steps share the same data source as the page (markup stripped)
const howToLd = {
  "@context": "https://schema.org",
  "@type": "HowTo",
  name: "AvatarHub client installation guide",
  description: "From download to verified install in about 10–30 minutes, zero command line.",
  totalTime: "PT30M",
  step: INSTALL_GUIDE.steps.map((s, i) => ({
    "@type": "HowToStep",
    position: i + 1,
    name: stripRich(s.title.en),
    text: stripRich(s.detail.en),
  })),
};

// Download-center client list (public clients only; gated apps are never endorsed in structured data)
const listLd = {
  "@context": "https://schema.org",
  "@type": "ItemList",
  name: "BOUNDLESS desktop client downloads",
  itemListElement: CLIENT_APPS.filter((c) => !c.gated).map((c, i) => ({
    "@type": "ListItem",
    position: i + 1,
    name: c.name.en,
    url: `${SITE_URL}/en${c.page === "/download" ? "/download" : c.page}`,
  })),
};

export default function DownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(howToLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(listLd) }} />
      <Navbar />
      <DownloadHub />
      <DownloadSection embedded />
      <Footer />
    </main>
  );
}
