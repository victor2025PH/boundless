import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import VideoFeed from "@/components/VideoFeed";
import { listFeed } from "@/lib/feed-store";
import { SITE_URL } from "@/lib/site";
import { BRAND_FILM } from "@/lib/film";

export const dynamic = "force-dynamic";

const LANGUAGES = { "zh-CN": "/videos", en: "/en/videos", "x-default": "/videos" };

export const metadata: Metadata = {
  title: "Video Hub · Brand Film / Hands-on / ChatX Tutorials · BOUNDLESS",
  description:
    "BOUNDLESS video hub: the 3-minute brand film, real-engine interpreting and digital-human runs, AI concept demos, and the entry to the 12-episode ChatX tutorial series. AI concept demos and real engine output are labeled separately.",
  alternates: { canonical: "/en/videos", languages: LANGUAGES },
  openGraph: {
    title: "Video Hub · Brand Film / Hands-on / ChatX Tutorials · BOUNDLESS",
    description: "Brand film, interpreting / digital-human hands-on runs, concept demos and the ChatX tutorial series — all on one page.",
    url: `${SITE_URL}/en/videos`,
    images: [{ url: BRAND_FILM.og.en, width: 1200, height: 675 }],
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
