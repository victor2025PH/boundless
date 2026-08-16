import type { Metadata } from "next";
import ProductLanding from "@/components/ProductLanding";
import { landingMetadata, landingJsonLd, landingFaqJsonLd } from "@/lib/landingMeta";

// 合规隔离（lib/isolation.ts）：/face 为 gated 高风险页，主站不收录，仅供直达访问。
export const metadata: Metadata = {
  ...landingMetadata("face", "zh"),
  robots: { index: false, follow: false },
};

export default function FaceLanding() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingJsonLd("face", "zh")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingFaqJsonLd("face", "zh")) }}
      />
      <ProductLanding product="face" />
    </>
  );
}
