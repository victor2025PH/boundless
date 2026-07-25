import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxDownloadSection from "@/components/ChatxDownloadSection";
import { SITE_URL } from "@/lib/site";
import { CHATX } from "@/lib/chatxContent";

const LANGUAGES = { "zh-CN": "/download/chatx", en: "/en/download/chatx", "x-default": "/download/chatx" };

export const metadata: Metadata = {
  title: "Download the ChatX Client · BOUNDLESS",
  description:
    "Download the ChatX desktop client (Windows): unified omni-channel inbox, AI drafting and auto-reply, live translation, voice messages and customer profiles. Local-first data, no GPU required, auto-update built in, SHA-256 verifiable.",
  alternates: { canonical: "/en/download/chatx", languages: LANGUAGES },
  openGraph: {
    title: "Download the ChatX Client · BOUNDLESS",
    description: "The omni-channel AI chat workspace for desktop: Windows available now, local-first data, auto-update built in.",
    url: `${SITE_URL}/en/download/chatx`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "ChatX",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Windows 10/11",
  softwareVersion: CHATX.download.version,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "Free download & trial" },
  publisher: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
};

export default function ChatxDownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <ChatxDownloadSection lang="en" />
      <Footer />
    </main>
  );
}
