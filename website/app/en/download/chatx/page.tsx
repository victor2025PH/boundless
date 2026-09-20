import type { Metadata } from "next";
import { Suspense } from "react";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxDownloadSection from "@/components/ChatxDownloadSection";
import InviteRefBanner from "@/components/InviteRefBanner";
import MobileDesktopHandoff from "@/components/MobileDesktopHandoff";
import { SITE_URL } from "@/lib/site";
import { chatxDownloadJsonLd } from "@/lib/chatxContent";

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

// SoftwareApplication + FAQPage nodes (impl-77 GEO batch 3): built in lib/chatxContent.ts so the
// zh and en pages cannot drift apart, and so the FAQ schema mirrors the visible accordion exactly.
const ld = chatxDownloadJsonLd("en", SITE_URL);

export default function ChatxDownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      {ld.map((node, i) => (
        <script
          key={i}
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(node) }}
        />
      ))}
      <Navbar />
      {/* ?ref=ZL-XXXXXX referral banner (useSearchParams needs Suspense; page stays static) */}
      <Suspense fallback={null}>
        <InviteRefBanner lang="en" />
      </Suspense>
      {/* impl-78 P1-2: mobile → desktop handoff card (below md only). Mounted at page level
          rather than inside ChatxDownloadSection, which a parallel line is editing. */}
      <MobileDesktopHandoff lang="en" />
      <ChatxDownloadSection lang="en" />
      <Footer />
    </main>
  );
}
