import type { Metadata } from "next";

// /brand 是客户端组件页：metadata 必须放 layout，否则会继承根布局的
// canonical:"/"，导致品牌页被搜索引擎当成首页副本而不收录。
export const metadata: Metadata = {
  title: "品牌故事 · 无界科技 BOUNDLESS",
  description:
    "无界科技 BOUNDLESS 品牌故事：用 AI 打破触达、成交、容貌、声音、身份、语言六道边界——七条产品线如何从获客到成交，让任何人以任意面孔、声音、语言实时沟通并自动成交。",
  alternates: {
    canonical: "/brand",
    languages: { "zh-CN": "/brand", en: "/en/brand" },
  },
  openGraph: {
    type: "website",
    url: "/brand",
    title: "品牌故事 · 无界科技 BOUNDLESS",
    description: "让沟通，无界。我们用 AI 拆掉触达、容貌、声音、语言、成交的六道墙。",
    siteName: "无界科技 BOUNDLESS",
  },
  robots: { index: true, follow: true },
};

export default function BrandLayout({ children }: { children: React.ReactNode }) {
  return children;
}
