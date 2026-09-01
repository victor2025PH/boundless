import type { Metadata } from "next";
import CompareHubPage from "@/components/CompareHubPage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareHub } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "How to Choose an AI Customer-Chat Tool for Cross-Border Sales (2026) · BOUNDLESS",
  description:
    "A five-dimension framework (data sovereignty / channel form / AI depth / translation quality / TCO) plus a quick comparison of ChatX, respond.io, SaleSmartly, SleekFlow and Wati.",
  alternates: {
    canonical: "/en/compare",
    languages: {
      "zh-CN": "/compare",
      en: "/en/compare",
      "x-default": "/compare",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareHubEn() {
  return (
    <>
      <FaqJsonLd faq={compareHub.faq} lang="en" />
      <CompareHubPage />
    </>
  );
}
