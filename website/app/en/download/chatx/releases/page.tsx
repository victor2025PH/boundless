import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxReleaseNotesSection from "@/components/ChatxReleaseNotesSection";
import { SITE_URL } from "@/lib/site";
import { CHATX_RELEASE_NOTES } from "@/lib/chatxReleaseNotes";

const LANGUAGES = {
  "zh-CN": "/download/chatx/releases",
  en: "/en/download/chatx/releases",
  "x-default": "/download/chatx/releases",
};

export const metadata: Metadata = {
  title: "ChatX Release Notes · BOUNDLESS",
  description:
    "Full release-note history for the ChatX desktop client: what's new and what's fixed in every version. One-click auto-update built in; accounts and chat data are fully preserved.",
  alternates: { canonical: "/en/download/chatx/releases", languages: LANGUAGES },
  openGraph: {
    title: "ChatX Release Notes · BOUNDLESS",
    description: "The full ChatX release-note history — see what changed in every version.",
    url: `${SITE_URL}/en/download/chatx/releases`,
  },
};

// Structured data (ItemList) mirroring the visible timeline, so it never drifts from the page.
const jsonLd = {
  "@context": "https://schema.org",
  "@type": "ItemList",
  name: "ChatX Release Notes",
  url: `${SITE_URL}/en/download/chatx/releases`,
  itemListElement: CHATX_RELEASE_NOTES.map((r, i) => ({
    "@type": "ListItem",
    position: i + 1,
    name: `ChatX v${r.version} · ${r.title.en}`,
  })),
};

export default function ChatxReleasesPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }} />
      <Navbar />
      <ChatxReleaseNotesSection lang="en" />
      <Footer />
    </main>
  );
}
