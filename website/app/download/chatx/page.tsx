import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxDownloadSection from "@/components/ChatxDownloadSection";
import { SITE_URL } from "@/lib/site";
import { CHATX } from "@/lib/chatxContent";

export const metadata: Metadata = {
  title: "下载智聊 ChatX 客户端 · 无界科技 BOUNDLESS",
  description:
    "下载智聊 ChatX 桌面客户端（Windows）：全渠道统一收件箱、AI 自动拟稿与自动回复、实时互译、语音消息与客户画像。数据本地保存、免显卡、内置自动更新，SHA-256 可校验。",
  alternates: {
    canonical: "/download/chatx",
    languages: { "zh-CN": "/download/chatx", en: "/en/download/chatx", "x-default": "/download/chatx" },
  },
  openGraph: {
    title: "下载智聊 ChatX 客户端 · 无界科技 BOUNDLESS",
    description: "聚合 AI 聊天工作台桌面客户端下载：Windows 已上线，数据本地保存，内置自动更新。",
    url: `${SITE_URL}/download/chatx`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "智聊 ChatX",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Windows 10/11",
  softwareVersion: CHATX.download.version,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "免费下载试用" },
  publisher: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
};

export default function ChatxDownloadPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <ChatxDownloadSection lang="zh" />
      <Footer />
    </main>
  );
}
