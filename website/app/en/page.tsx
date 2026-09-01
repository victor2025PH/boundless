import type { Metadata } from "next";
import SiteHome from "@/components/SiteHome";
import { content } from "@/lib/content";
import { faqPageJsonLd } from "@/lib/jsonld";

const LANGUAGES = { "zh-CN": "/", en: "/en", "x-default": "/" };

export const metadata: Metadata = {
  title: "BOUNDLESS · Communication, Boundless",
  description:
    "BOUNDLESS: AI that breaks the barriers of face, voice and language — AI face swap, voice cloning, real-time live face & voice swap, live translation, and AI auto-closing chat. Self-controlled private deployment, settled in USDT.",
  alternates: { canonical: "/en", languages: LANGUAGES },
  openGraph: {
    type: "website",
    url: "/en",
    title: "BOUNDLESS · Communication, Boundless",
    description:
      "AI face swap · voice cloning · real-time live face/voice swap · live translation · AI auto-closing chat. Private deployment, settled in USDT.",
    siteName: "BOUNDLESS",
  },
  twitter: {
    card: "summary_large_image",
    title: "BOUNDLESS · Communication, Boundless",
    description:
      "AI face swap · voice cloning · real-time live face/voice swap · live translation · AI auto-closing chat. Private deployment, USDT.",
  },
};

// 首页 FAQPage schema（英文路由 → 英文条目；与可见 Faq 组件同源，见 app/page.tsx 注释）
const faqLd = faqPageJsonLd(content.en.faq.items);

export default function HomeEn() {
  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(faqLd) }} />
      <SiteHome />
    </>
  );
}
