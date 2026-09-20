import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "ChatX vs Wati · BOUNDLESS",
  description:
    "Multi-channel human-like AI operations vs WhatsApp-first support tooling: channel coverage, deployment & data ownership, AI depth, translation and pricing compared — with FAQs.",
  alternates: {
    canonical: "/en/compare/wati",
    languages: {
      "zh-CN": "/compare/wati",
      en: "/en/compare/wati",
      "x-default": "/compare/wati",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareWatiPageEn() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["wati"].faq} lang="en" />
      <ComparePage spec={compareSpecs["wati"]} />
    </>
  );
}
