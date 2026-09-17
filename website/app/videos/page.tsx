import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import VideoFeed from "@/components/VideoFeed";
import { listFeed } from "@/lib/feed-store";
import { SITE_URL } from "@/lib/site";
import { BRAND_FILM } from "@/lib/film";

export const dynamic = "force-dynamic";

// 2026-09-17：标题/描述去掉「每日 / 每天更新」承诺（流最近一条 08-19），并把智聊视频教程列为本页入口之一。
export const metadata: Metadata = {
  title: "视频中心 · 品牌片 / 真机实测 / 智聊教程 · 无界科技 BOUNDLESS",
  description:
    "无界科技视频中心：3 分钟品牌片、实时同传与数字人真机实测、AI 概念演示，以及智聊 ChatX 12 集视频教程入口。AI 概念演示与真实引擎输出分开标注。",
  alternates: {
    canonical: "/videos",
    languages: { "zh-CN": "/videos", en: "/en/videos", "x-default": "/videos" },
  },
  openGraph: {
    title: "视频中心 · 品牌片 / 真机实测 / 智聊教程 · 无界科技 BOUNDLESS",
    description: "品牌片、同传 / 数字人真机实测、概念演示与智聊 12 集视频教程，一页看全。",
    url: `${SITE_URL}/videos`,
    images: [{ url: BRAND_FILM.og.zh, width: 1200, height: 675 }],
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
