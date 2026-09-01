import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "ChatX vs SleekFlow · BOUNDLESS",
  description:
    "Private-domain AI closing vs social-commerce messaging SaaS: deployment & data ownership, channels, AI depth, translation and pricing compared — with FAQs.",
  alternates: {
    canonical: "/en/compare/sleekflow",
    languages: {
      "zh-CN": "/compare/sleekflow",
      en: "/en/compare/sleekflow",
      "x-default": "/compare/sleekflow",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareSleekflowPageEn() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["sleekflow"].faq} lang="en" />
      <ComparePage spec={compareSpecs["sleekflow"]} />
    </>
  );
}
