import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import MatrixxDownloadSection from "@/components/MatrixxDownloadSection";
import { SITE_URL } from "@/lib/site";
import { MATRIXX } from "@/lib/matrixxContent";

// 合规隔离（lib/isolation.ts）：/matrix 及其下载页为 gated，主站不收录，仅供直达访问。
export const metadata: Metadata = {
  title: "下载智控 MatrixX 客户端 · 无界科技 BOUNDLESS",
  description:
    "下载智控 MatrixX（Windows）：Telegram 多账号矩阵化运营，搜索发现 / 群监控 / 成员提取 / 消息群发防封 / AI 自动回复，本地部署数据不出本机。含分步安装教程与 API 凭据获取指南。",
  robots: { index: false, follow: false },
  alternates: {
    canonical: "/matrix/download",
    languages: { "zh-CN": "/matrix/download", en: "/en/matrix/download", "x-default": "/matrix/download" },
  },
  openGraph: {
    title: "下载智控 MatrixX 客户端 · 无界科技 BOUNDLESS",
    description: "Telegram 多账号矩阵化运营客户端下载：Windows 已上线，本地部署、数据不出本机。",
    url: `${SITE_URL}/matrix/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "智控 MatrixX",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Windows 10/11",
  softwareVersion: MATRIXX.download.version,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "免费下载试用" },
  publisher: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
};

export default function MatrixDownloadPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <MatrixxDownloadSection lang="zh" />
      <Footer />
    </main>
  );
}
