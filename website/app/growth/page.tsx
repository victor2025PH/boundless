import GrowthLanding from "@/components/GrowthLanding";
import { growthJsonLd, growthFaqJsonLd } from "@/lib/growthMeta";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "智拓 ReachX · 智聊 ChatX | 智连系获客成交 — 无界科技",
  description:
    "智连系双产品：智拓真机多号获客引流，智聊 AI 自动跟进成交。可单选或组合成从获客到成交的完整闭环，私有部署、数据不出网。",
  alternates: {
    canonical: "/growth",
    languages: { "zh-CN": "/growth", en: "/en/growth" },
  },
  openGraph: {
    type: "website",
    url: "/growth",
    title: "智连系 · 获客到成交 | 无界科技 BOUNDLESS",
    description: "智拓获客 + 智聊成交，一套底座按需组合。",
    siteName: "无界科技 BOUNDLESS",
  },
};

export default function GrowthPage() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(growthJsonLd("zh")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(growthFaqJsonLd("zh")) }}
      />
      <GrowthLanding />
    </>
  );
}
