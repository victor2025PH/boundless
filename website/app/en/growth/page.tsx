import GrowthLanding from "@/components/GrowthLanding";
import { growthJsonLd, growthFaqJsonLd } from "@/lib/growthMeta";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "ReachX · ChatX | Growth lead-gen & closing — BOUNDLESS",
  description:
    "Growth family: ReachX real-device lead-gen and ChatX AI closing. Pick one or combine into a full loop — privately deployed, data stays off-net.",
  alternates: {
    canonical: "/en/growth",
    languages: { "zh-CN": "/growth", en: "/en/growth" },
  },
  openGraph: {
    type: "website",
    url: "/en/growth",
    title: "Growth · reach to close | BOUNDLESS",
    description: "ReachX lead-gen + ChatX closing on one core.",
    siteName: "BOUNDLESS",
  },
};

export default function GrowthPageEn() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(growthJsonLd("en")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(growthFaqJsonLd("en")) }}
      />
      <GrowthLanding />
    </>
  );
}
