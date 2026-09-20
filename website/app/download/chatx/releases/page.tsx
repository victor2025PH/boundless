import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ChatxReleaseNotesSection from "@/components/ChatxReleaseNotesSection";
import { SITE_URL } from "@/lib/site";
import { CHATX_RELEASE_NOTES } from "@/lib/chatxReleaseNotes";

const LANGUAGES = {
  "zh-CN": "/download/chatx/releases",
  en: "/en/download/chatx/releases",
  "x-default": "/download/chatx/releases",
};

export const metadata: Metadata = {
  title: "智聊 ChatX 版本更新记录 · 无界科技 BOUNDLESS",
  description:
    "智聊 ChatX 桌面客户端历代版本更新记录：每一版新增了什么、修了什么，一次看清。内置一键自动更新，账号与聊天数据全部保留。",
  alternates: { canonical: "/download/chatx/releases", languages: LANGUAGES },
  openGraph: {
    title: "智聊 ChatX 版本更新记录 · 无界科技 BOUNDLESS",
    description: "智聊 ChatX 历代版本更新记录：每一版更新了什么，一次看清。",
    url: `${SITE_URL}/download/chatx/releases`,
  },
};

// 版本更新的结构化数据（ItemList，便于搜索引擎理解发布历史）——与页面可见时间线同源。
const jsonLd = {
  "@context": "https://schema.org",
  "@type": "ItemList",
  name: "智聊 ChatX 版本更新记录",
  url: `${SITE_URL}/download/chatx/releases`,
  itemListElement: CHATX_RELEASE_NOTES.map((r, i) => ({
    "@type": "ListItem",
    position: i + 1,
    name: `智聊 ChatX v${r.version} · ${r.title.zh}`,
  })),
};

export default function ChatxReleasesPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }} />
      <Navbar />
      <ChatxReleaseNotesSection lang="zh" />
      <Footer />
    </main>
  );
}
