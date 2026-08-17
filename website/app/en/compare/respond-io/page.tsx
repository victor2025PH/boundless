import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "ChatX vs respond.io · BOUNDLESS",
  description:
    "Self-hosted human-like AI relationship operations vs enterprise omnichannel SaaS: deployment & data ownership, channels, AI depth, compliance tooling and pricing compared.",
  alternates: {
    canonical: "/en/compare/respond-io",
    languages: {
      "zh-CN": "/compare/respond-io",
      en: "/en/compare/respond-io",
      "x-default": "/compare/respond-io",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareRespondIoPageEn() {
  return <ComparePage spec={compareSpecs["respond-io"]} />;
}
