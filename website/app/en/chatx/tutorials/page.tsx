import type { Metadata } from "next";
import Link from "next/link";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import TutorialPlaylist from "@/components/TutorialPlaylist";
import { SITE_URL } from "@/lib/site";
import { CHATX_TUTORIALS_OG, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC } from "@/lib/chatx-tutorials";
import { chatxTutorialsJsonLd } from "@/lib/chatx-tutorials-ld";

const MIN = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
const LANGUAGES = { "zh-CN": "/chatx/tutorials", en: "/en/chatx/tutorials", "x-default": "/chatx/tutorials" };
const TITLE = `ChatX Video Tutorials · ${CHATX_TUTORIAL_COUNT} episodes, ~${MIN} minutes · BOUNDLESS`;
const DESC =
  "Official ChatX video tutorials: unified inbox, connecting four platforms by QR, human-like translation, AI drafts and auto-reply, persona studio, knowledge base, voice cloning, goals, care schedule, the Xiaozhi assistant, guardrails and billing — one feature per episode, recorded on the live product. Chinese narration with Chinese captions; English version in production.";

export const metadata: Metadata = {
  title: TITLE,
  description: DESC,
  alternates: { canonical: "/en/chatx/tutorials", languages: LANGUAGES },
  openGraph: {
    title: `Learn ChatX in ${CHATX_TUTORIAL_COUNT} episodes · ChatX video tutorials`,
    description: DESC,
    url: `${SITE_URL}/en/chatx/tutorials`,
    images: [{ url: CHATX_TUTORIALS_OG, width: 1200, height: 630 }],
  },
  twitter: { card: "summary_large_image", images: [CHATX_TUTORIALS_OG] },
};

const ld = chatxTutorialsJsonLd("en");

export default function ChatxTutorialsPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(ld) }} />
      <Navbar />
      <TutorialPlaylist lang="en" />
      <section className="mx-auto max-w-7xl px-5 pb-20">
        <div className="grid gap-4 sm:grid-cols-3">
          <Link href="/en/download/chatx#install-guide" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">Written install guide</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">Pre-check, the SmartScreen prompt, SHA-256 verification — five steps from download to first chat.</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ /en/download/chatx</span>
          </Link>
          <Link href="/en/pricing" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">Rates & top-up tiers</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">Standard translation is free forever; Tokens are pay-as-you-go with no monthly or seat fees.</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ Pricing</span>
          </Link>
          <Link href="/en/videos" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">Brand film & demos</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">The 3-minute brand film, real-engine interpreting / digital-human demos and concept demos live in the video hub.</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ Video hub</span>
          </Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
