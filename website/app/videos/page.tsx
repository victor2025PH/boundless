import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import VideoFeed from "@/components/VideoFeed";
import { listFeed } from "@/lib/feed-store";
import { SITE_URL } from "@/lib/site";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "视频动态 · 每日效果演示 · 无界科技 BOUNDLESS",
  description:
    "AvatarHub 每日效果演示：实时换脸、声音克隆、数字人直播、克隆音同传。每天一条新演示，AI 概念演示与真实引擎输出分开标注。",
  alternates: {
    canonical: "/videos",
    languages: { "zh-CN": "/videos", en: "/en/videos", "x-default": "/videos" },
  },
  openGraph: {
    title: "视频动态 · 每日效果演示 · 无界科技 BOUNDLESS",
    description: "换脸 / 克隆声音 / 数字人直播 / 克隆音同传，每天更新一条演示视频。",
    url: `${SITE_URL}/videos`,
  },
};

export default async function VideosPage() {
  const videos = await listFeed();
  return (
    <main className="relative min-h-screen">
      <Navbar />
      <VideoFeed videos={videos} />
      <Footer />
    </main>
  );
}
