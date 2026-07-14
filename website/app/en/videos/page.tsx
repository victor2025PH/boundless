import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import VideoFeed from "@/components/VideoFeed";
import { listFeed } from "@/lib/feed-store";
import { SITE_URL } from "@/lib/site";

export const dynamic = "force-dynamic";

const LANGUAGES = { "zh-CN": "/videos", en: "/en/videos", "x-default": "/videos" };

export const metadata: Metadata = {
  title: "Video Feed · Daily Demos · BOUNDLESS",
  description:
    "Daily AvatarHub demos: live face swap, voice cloning, digital-human streaming and interpreting. One new demo every day; AI concept demos and real engine output are labeled separately.",
  alternates: { canonical: "/en/videos", languages: LANGUAGES },
  openGraph: {
    title: "Video Feed · Daily Demos · BOUNDLESS",
    description: "Face swap / voice cloning / digital-human streaming / interpreting — a new demo video every day.",
    url: `${SITE_URL}/en/videos`,
  },
};

export default async function VideosPageEn() {
  const videos = await listFeed();
  return (
    <main className="relative min-h-screen">
      <Navbar />
      <VideoFeed videos={videos} />
      <Footer />
    </main>
  );
}
