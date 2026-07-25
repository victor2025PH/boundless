import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import DownloadHub from "@/components/DownloadHub";
import DownloadSection from "@/components/DownloadSection";
import { SITE_URL } from "@/lib/site";
import { LATEST_VERSION } from "@/lib/releaseNotes";
import { INSTALL_GUIDE, stripRich } from "@/lib/manualContent";
import { CLIENT_APPS } from "@/lib/downloads";

export const metadata: Metadata = {
  title: "下载中心 · 无界科技 BOUNDLESS",
  description:
    "一处下载全部桌面客户端：智聊 ChatX 聚合 AI 聊天工作台、AvatarHub 实时数字人引擎（声音克隆 / 实时换脸 / 数字人直播 / 克隆音同传）。Windows 安装包 SHA-256 可校验，本地部署数据不出机。",
  alternates: {
    canonical: "/download",
    languages: { "zh-CN": "/download", en: "/en/download", "x-default": "/download" },
  },
  openGraph: {
    title: "下载中心 · 无界科技 BOUNDLESS",
    description: "全部桌面客户端一处下载：智聊 ChatX、AvatarHub 实时数字人引擎。Windows 已上线。",
    url: `${SITE_URL}/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "AvatarHub",
  applicationCategory: "MultimediaApplication",
  operatingSystem: "Windows 10/11, macOS 12+",
  softwareVersion: LATEST_VERSION,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "14 天免费试用" },
  publisher: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
};

// 安装教程结构化数据：搜索结果可展示分步富摘要，步骤与页面内容同源（stripRich 剥离标记）
const howToLd = {
  "@context": "https://schema.org",
  "@type": "HowTo",
  name: "AvatarHub 客户端安装教程",
  description: "从下载安装包到验证安装的完整流程，约 10–30 分钟，零命令行。",
  totalTime: "PT30M",
  step: INSTALL_GUIDE.steps.map((s, i) => ({
    "@type": "HowToStep",
    position: i + 1,
    name: stripRich(s.title.zh),
    text: stripRich(s.detail.zh),
  })),
};

// 下载中心客户端清单（仅 public 客户端进结构化数据；gated 不背书）
const listLd = {
  "@context": "https://schema.org",
  "@type": "ItemList",
  name: "无界科技桌面客户端下载",
  itemListElement: CLIENT_APPS.filter((c) => !c.gated).map((c, i) => ({
    "@type": "ListItem",
    position: i + 1,
    name: c.name.zh,
    url: `${SITE_URL}${c.page}`,
  })),
};

export default function DownloadPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(howToLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(listLd) }} />
      <Navbar />
      <DownloadHub />
      <DownloadSection embedded />
      <Footer />
    </main>
  );
}
