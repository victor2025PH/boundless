import type { Metadata } from "next";

// TG 小程序壳页：内容与官网重复且面向 Telegram WebView，不应被搜索引擎收录。
// 不设 canonical 继承（覆盖根布局的 canonical:"/"），明确 noindex。
export const metadata: Metadata = {
  title: "无界科技 · Telegram 小程序",
  alternates: { canonical: "/app" },
  robots: { index: false, follow: false },
};

export default function MiniAppLayout({ children }: { children: React.ReactNode }) {
  return children;
}
