import type { Metadata } from "next";
import ProductLanding from "@/components/ProductLanding";
import { landingMetadata, landingJsonLd, landingFaqJsonLd } from "@/lib/landingMeta";

// Compliance isolation (lib/isolation.ts): /en/face is gated, noindex on the main property.
export const metadata: Metadata = {
  ...landingMetadata("face", "en"),
  robots: { index: false, follow: false },
};

export default function FaceLandingEn() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingJsonLd("face", "en")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingFaqJsonLd("face", "en")) }}
      />
      <ProductLanding product="face" />
    </>
  );
}
