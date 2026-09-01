import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "智聊 ChatX vs respond.io 对比 · 无界科技 BOUNDLESS",
  description:
    "私有化拟人 AI 关系运营 vs 企业级全渠道客服 SaaS：部署与数据主权、渠道形态、AI 拟人化深度、合规工具与计费模式逐项对比。",
  alternates: {
    canonical: "/compare/respond-io",
    languages: {
      "zh-CN": "/compare/respond-io",
      en: "/en/compare/respond-io",
      "x-default": "/compare/respond-io",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareRespondIoPage() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["respond-io"].faq} lang="zh" />
      <ComparePage spec={compareSpecs["respond-io"]} />
    </>
  );
}
