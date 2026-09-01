import type { Metadata } from "next";
import { Suspense } from "react";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxDownloadSection from "@/components/ChatxDownloadSection";
import InviteRefBanner from "@/components/InviteRefBanner";
import MobileDesktopHandoff from "@/components/MobileDesktopHandoff";
import { SITE_URL } from "@/lib/site";
import { chatxDownloadJsonLd } from "@/lib/chatxContent";

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

// SoftwareApplication + FAQPage 两个节点（实施77 GEO 批次3）：构造在 lib/chatxContent.ts，
// 与页面可见内容同源——FAQ 折叠面板显示什么，schema 就是什么。
const ld = chatxDownloadJsonLd("zh", SITE_URL);

export default function ChatxDownloadPage() {
  return (
    <main className="relative min-h-screen">
      {ld.map((node, i) => (
        <script
          key={i}
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(node) }}
        />
      ))}
      <Navbar />
      {/* ?ref=ZL-XXXXXX 邀请归因横幅（useSearchParams 需 Suspense，页面保持静态渲染） */}
      <Suspense fallback={null}>
        <InviteRefBanner lang="zh" />
      </Suspense>
      {/* 实施78 P1-2：移动端交接卡（仅 <md 显示）。挂在页面层而不是塞进
          ChatxDownloadSection——那个文件属并行线在改，页面层插入零冲突。 */}
      <MobileDesktopHandoff lang="zh" />
      <ChatxDownloadSection lang="zh" />
      <Footer />
    </main>
  );
}
