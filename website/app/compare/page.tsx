import type { Metadata } from "next";
import CompareHubPage from "@/components/CompareHubPage";
import FaqJsonLd from "@/components/FaqJsonLd";
import { compareHub } from "@/lib/compare-content";

export const metadata: Metadata = {
  title: "跨境电商 AI 客服工具怎么选（2026 实战指南）· 无界科技 BOUNDLESS",
  description:
    "五维选型框架（数据主权 / 渠道形态 / AI 深度 / 翻译质量 / 总拥有成本）+ 智聊 ChatX、respond.io、SaleSmartly、SleekFlow、Wati 五款工具速览与逐一对比。",
  alternates: {
    canonical: "/compare",
    languages: {
      "zh-CN": "/compare",
      en: "/en/compare",
      "x-default": "/compare",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompareHubZh() {
  return (
    <>
      <FaqJsonLd faq={compareHub.faq} lang="zh" />
      <CompareHubPage />
    </>
  );
}
