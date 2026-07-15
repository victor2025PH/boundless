import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ManualSection from "@/components/ManualSection";
import { SITE_URL } from "@/lib/site";
import { LATEST_VERSION } from "@/lib/releaseNotes";

export const metadata: Metadata = {
  title: "AvatarHub 使用手册 · 无界科技 BOUNDLESS",
  description:
    "AvatarHub 实时数字人引擎完整使用手册：系统要求、下载安装、激活试用、声音克隆、直播换脸、手机同传、软件更新与故障排查。支持在线阅读与打印导出 PDF。",
  alternates: {
    canonical: "/manual",
    languages: { "zh-CN": "/manual", en: "/en/manual", "x-default": "/manual" },
  },
  openGraph: {
    title: "AvatarHub 使用手册 · 无界科技 BOUNDLESS",
    description: "从装机到直播出镜的完整指南，支持在线阅读与导出 PDF。",
    url: `${SITE_URL}/manual`,
  },
};

const guideLd = {
  "@context": "https://schema.org",
  "@type": "TechArticle",
  headline: "AvatarHub 使用手册",
  about: "AvatarHub 实时数字人引擎的安装、激活、使用与故障排查指南",
  inLanguage: "zh-CN",
  version: LATEST_VERSION,
  publisher: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
};

export default function ManualPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(guideLd) }} />
      <Navbar />
      <ManualSection />
      <Footer />
    </main>
  );
}
