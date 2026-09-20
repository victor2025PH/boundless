import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "智聊 ChatX vs SaleSmartly 对比 · 无界科技 BOUNDLESS",
  description:
    "AI 主动成交的私有化关系运营 vs 跨境社媒聚合客服 SaaS：部署与数据主权、AI 拟人化深度、翻译、合规工具与计费模式逐项对比。",
  alternates: {
    canonical: "/compare/salesmartly",
    languages: {
      "zh-CN": "/compare/salesmartly",
      en: "/en/compare/salesmartly",
      "x-default": "/compare/salesmartly",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareSalesmartlyPage() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["salesmartly"].faq} lang="zh" />
      <ComparePage spec={compareSpecs["salesmartly"]} />
    </>
  );
}
