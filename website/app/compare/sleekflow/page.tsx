import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "智聊 ChatX vs SleekFlow 对比 · 无界科技 BOUNDLESS",
  description:
    "私域 AI 拟人成交 vs 社交电商客服营销 SaaS：部署与数据主权、渠道形态、AI 深度、翻译与计费模式逐项对比，含常见问题解答。",
  alternates: {
    canonical: "/compare/sleekflow",
    languages: {
      "zh-CN": "/compare/sleekflow",
      en: "/en/compare/sleekflow",
      "x-default": "/compare/sleekflow",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareSleekflowPage() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["sleekflow"].faq} lang="zh" />
      <ComparePage spec={compareSpecs["sleekflow"]} />
    </>
  );
}
