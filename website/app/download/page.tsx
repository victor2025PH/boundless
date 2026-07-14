import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import DownloadSection from "@/components/DownloadSection";
import { SITE_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "下载客户端 · 无界科技 BOUNDLESS",
  description:
    "下载 AvatarHub 实时数字人引擎客户端（Windows / macOS）：声音克隆、实时换脸、数字人直播、克隆音同传。薄核心安装包，组件按需下载，SHA-256 可校验。",
  alternates: {
    canonical: "/download",
    languages: { "zh-CN": "/download", en: "/en/download", "x-default": "/download" },
  },
  openGraph: {
    title: "下载客户端 · 无界科技 BOUNDLESS",
    description: "AvatarHub 客户端下载：Windows 已上线，macOS 轻量控制台即将上线。",
    url: `${SITE_URL}/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "AvatarHub",
  applicationCategory: "MultimediaApplication",
  operatingSystem: "Windows 10/11, macOS 12+",
  offers: { "@type": "Offer", price: "0", priceCurrency: "USDT", description: "14 天免费试用" },
  publisher: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
};

export default function DownloadPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <DownloadSection />
      <Footer />
    </main>
  );
}
