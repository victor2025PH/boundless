import type { Metadata } from "next";
import Link from "next/link";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import FilmPlayer from "@/components/FilmPlayer";
import { BRAND_FILM } from "@/lib/film";
import { SITE_URL, CHANNEL_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "品牌片 · 3 分钟真机实测 · 无界科技 BOUNDLESS",
  description:
    "这部片子没有摄影师、配音员和翻译：同声传译真实通话、数字人口播、声音克隆与唱歌，全部真机实测——包括主持人本人也是引擎合成。右上角绿标全程不摘。",
  alternates: {
    canonical: "/film",
    languages: { "zh-CN": "/film", en: "/en/film", "x-default": "/film" },
  },
  openGraph: {
    title: "这部片子，没有摄影师、配音员和翻译",
    description: "3 分钟看完六个功能的真机实测——包括正在介绍它的主持人，也是它做的。",
    url: `${SITE_URL}/film`,
    images: [{ url: BRAND_FILM.og.zh, width: 1200, height: 675 }],
  },
  twitter: { card: "summary_large_image", images: [BRAND_FILM.og.zh] },
};

// VideoObject 结构化数据。措辞遵守 2026-07-26 合规收口：换脸类目词不进公开 JSON-LD
// （页面正文不受限），能力描述用同传/数字人/声音克隆。
const videoLd = {
  "@context": "https://schema.org",
  "@type": "VideoObject",
  name: "BOUNDLESS AvatarHub — 3 分钟真机实测品牌片",
  description:
    "Real engine walkthrough: a real interpreted phone call, digital-human presenting, voice cloning and singing — all running live on one machine, including the synthetic host.",
  thumbnailUrl: `${SITE_URL}${BRAND_FILM.og.zh}`,
  uploadDate: BRAND_FILM.uploadDate,
  duration: "PT2M23S",
  contentUrl: `${SITE_URL}${BRAND_FILM.src.zh}`,
  inLanguage: "zh-CN",
};

export default function FilmPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(videoLd) }} />
      <Navbar />
      <section className="mx-auto max-w-5xl px-5 pb-24 pt-28">
        <p className="text-xs font-semibold uppercase tracking-[0.3em] text-emerald-300">
          BRAND FILM · REAL ENGINE OUTPUT
        </p>
        <h1 className="mt-3 text-3xl font-bold leading-tight text-white sm:text-5xl">
          {BRAND_FILM.title.zh}
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-slate-400">{BRAND_FILM.tagline.zh}</p>
        <div className="mt-8">
          <FilmPlayer lang="zh" />
        </div>

        <div className="mt-12 grid gap-4 sm:grid-cols-2">
          <Link
            href="/videos"
            className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40"
          >
            <div className="text-sm font-semibold text-white">想自己跑一遍？</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              两条真机实操教学（实时同传通话 / 直播换背景）已在视频动态，每天还有新演示。
            </p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ 视频动态</span>
          </Link>
          <a
            href={CHANNEL_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40"
          >
            <div className="text-sm font-semibold text-white">Telegram 频道</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              每日真机实测第一时间发频道，可以直接在群里提问领试用。
            </p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ t.me/hykj7</span>
          </a>
        </div>

        <div className="mt-10 flex flex-wrap gap-3">
          <Link
            href="/download"
            className="rounded-full bg-neon-blue px-6 py-3 text-sm font-semibold text-white transition hover:brightness-110"
          >
            3 天全功能免费试用
          </Link>
          <Link
            href="/order"
            className="rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            方案与价格
          </Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
