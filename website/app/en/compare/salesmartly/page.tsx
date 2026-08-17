import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "ChatX vs SaleSmartly · BOUNDLESS",
  description:
    "Proactive AI closing with self-hosted deployment vs cross-border social-inbox SaaS: data ownership, AI depth, translation, compliance tooling and pricing compared.",
  alternates: {
    canonical: "/en/compare/salesmartly",
    languages: {
      "zh-CN": "/compare/salesmartly",
      en: "/en/compare/salesmartly",
      "x-default": "/compare/salesmartly",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareSalesmartlyPageEn() {
  return <ComparePage spec={compareSpecs["salesmartly"]} />;
}
