import type { Metadata } from "next";
import { FAMILY_PITCH } from "@/lib/brand";

// /brand 是客户端组件页：metadata 必须放 layout，否则会继承根布局的
// canonical:"/"，导致品牌页被搜索引擎当成首页副本而不收录。
// 描述文案直接复用 FAMILY_PITCH.zh.sub（单一真相），不在此另起一份产品数/边界数的
// 独立文案——避免未来产品线增减时，这里被 repo_doctor 的硬编码数字门禁再次抓到。
export const metadata: Metadata = {
  title: "品牌故事 · 无界科技 BOUNDLESS",
  description: `无界科技 BOUNDLESS 品牌故事：${FAMILY_PITCH.zh.sub}`,
  alternates: {
    canonical: "/brand",
    languages: { "zh-CN": "/brand", en: "/en/brand" },
  },
  openGraph: {
    type: "website",
    url: "/brand",
    title: "品牌故事 · 无界科技 BOUNDLESS",
    description: "让沟通，无界。我们用 AI 拆掉触达、成交、规模、容貌、声音、身份、语言的七道墙。",
    siteName: "无界科技 BOUNDLESS",
  },
  robots: { index: true, follow: true },
};

export default function BrandLayout({ children }: { children: React.ReactNode }) {
  return children;
}
