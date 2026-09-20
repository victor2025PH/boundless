import type { Metadata } from "next";
import ComparePage from "@/components/ComparePage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareSpecs } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "智聊 ChatX vs Wati 对比 · 无界科技 BOUNDLESS",
  description:
    "多渠道 AI 拟人经营 vs WhatsApp 单渠道客服工具：渠道覆盖、部署与数据主权、AI 深度、翻译与计费模式逐项对比，含常见问题解答。",
  alternates: {
    canonical: "/compare/wati",
    languages: {
      "zh-CN": "/compare/wati",
      en: "/en/compare/wati",
      "x-default": "/compare/wati",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareWatiPage() {
  return (
    <>
      <FaqJsonLd faq={compareSpecs["wati"].faq} lang="zh" />
      <ComparePage spec={compareSpecs["wati"]} />
    </>
  );
}
