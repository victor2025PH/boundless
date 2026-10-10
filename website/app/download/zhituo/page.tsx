import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ZhituoDownloadSection from "@/components/ZhituoDownloadSection";
import { SITE_URL } from "@/lib/site";

// 合规隔离（lib/isolation.ts）：/download/zhituo 为 gated，主站不收录，仅供直达访问。
export const metadata: Metadata = {
  title: "下载智拓客户端 · 无界科技 BOUNDLESS",
  description:
    "下载智拓（Windows）：机房真机多账号运营客户端，一体安装、按账号自动接入主控，本地部署、数据不出本机。含分步安装教程与接入说明，SHA-256 可校验。",
  robots: { index: false, follow: false },
  alternates: {
    canonical: "/download/zhituo",
    languages: { "zh-CN": "/download/zhituo", en: "/en/download/zhituo", "x-default": "/download/zhituo" },
  },
  openGraph: {
    title: "下载智拓客户端 · 无界科技 BOUNDLESS",
    description: "机房真机多账号运营客户端下载：Windows 已上线，本地部署、数据不出本机。",
    url: `${SITE_URL}/download/zhituo`,
  },
};

export default function ZhituoDownloadPage() {
  return (
    <main className="relative min-h-screen">
      <Navbar />
      <ZhituoDownloadSection lang="zh" />
      <Footer />
    </main>
  );
}