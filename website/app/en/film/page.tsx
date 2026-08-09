import type { Metadata } from "next";
import Link from "next/link";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import FilmPlayer from "@/components/FilmPlayer";
import { BRAND_FILM } from "@/lib/film";
import { SITE_URL, CHANNEL_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Brand Film · 3-minute Live Demo · BOUNDLESS",
  description:
    "No camera crew, no voice actor, no translator: a real interpreted phone call, digital-human presenting, voice cloning and singing — all running live on one machine. The green badge stays on for the entire film.",
  alternates: {
    canonical: "/en/film",
    languages: { "zh-CN": "/film", en: "/en/film", "x-default": "/film" },
  },
  openGraph: {
    title: "No camera crew. No voice actor. No translator.",
    description:
      "Six features, three minutes, all running live on one machine — including the host presenting them.",
    url: `${SITE_URL}/en/film`,
    images: [{ url: BRAND_FILM.og.en, width: 1200, height: 675 }],
  },
  twitter: { card: "summary_large_image", images: [BRAND_FILM.og.en] },
};

const videoLd = {
  "@context": "https://schema.org",
  "@type": "VideoObject",
  name: "BOUNDLESS AvatarHub — the 3-minute live demo film",
  description:
    "Real engine walkthrough: a real interpreted phone call, digital-human presenting, voice cloning and singing — all running live on one machine, including the synthetic host.",
  thumbnailUrl: `${SITE_URL}${BRAND_FILM.og.en}`,
  uploadDate: BRAND_FILM.uploadDate,
  duration: "PT3M4S",
  contentUrl: `${SITE_URL}${BRAND_FILM.src.en}`,
  inLanguage: "en",
};

export default function FilmPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(videoLd) }} />
      <Navbar />
      <section className="mx-auto max-w-5xl px-5 pb-24 pt-28">
        <p className="text-xs font-semibold uppercase tracking-[0.3em] text-emerald-300">
          BRAND FILM · REAL ENGINE OUTPUT
        </p>
        <h1 className="mt-3 text-3xl font-bold leading-tight text-white sm:text-5xl">
          {BRAND_FILM.title.en}
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-slate-400">{BRAND_FILM.tagline.en}</p>
        <div className="mt-8">
          <FilmPlayer lang="en" />
        </div>

        <div className="mt-12 grid gap-4 sm:grid-cols-2">
          <Link
            href="/en/videos"
            className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40"
          >
            <div className="text-sm font-semibold text-white">Want to run it yourself?</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              Two hands-on tutorials (interpreted live call / live background swap) are in the video feed,
              with a new demo every day.
            </p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ Video feed</span>
          </Link>
          <a
            href={CHANNEL_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40"
          >
            <div className="text-sm font-semibold text-white">Telegram channel</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              Daily real-engine demos land on the channel first. Questions and trials in the group.
            </p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ t.me/hykj7</span>
          </a>
        </div>

        <div className="mt-10 flex flex-wrap gap-3">
          <Link
            href="/en/download"
            className="rounded-full bg-neon-blue px-6 py-3 text-sm font-semibold text-white transition hover:brightness-110"
          >
            Start the 14-day full trial
          </Link>
          <Link
            href="/en/order"
            className="rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            Plans & pricing
          </Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
