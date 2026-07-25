import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight, Download } from "lucide-react";
import Navbar from "@/components/Navbar";
import ProductIcon from "@/components/ProductIcon";
import Footer from "@/components/Footer";
import { CONTACT_URL } from "@/lib/site";
import { MATRIXX } from "@/lib/matrixxContent";

export const metadata: Metadata = {
  title: "MatrixX · Telegram Fleet Operations | BOUNDLESS",
  description:
    "MatrixX: Telegram multi-account fleet operations — discovery, group monitoring, member extraction, anti-ban broadcasting and AI-team auto-reply. Scale without losing control. Local deployment, data on-device, no GPU.",
  robots: { index: false, follow: false },
  alternates: {
    canonical: "/en/matrix",
    languages: { "zh-CN": "/matrix", en: "/en/matrix", "x-default": "/matrix" },
  },
  openGraph: {
    type: "website",
    url: "/en/matrix",
    title: "MatrixX · The Fleet-Scale Barrier | BOUNDLESS",
    description: "Unified multi-account orchestration with 24/7 AI teamwork — scale without losing control.",
    siteName: "BOUNDLESS",
  },
};

const lang = "en" as const;

export default function MatrixLandingEn() {
  const h = MATRIXX.hero;
  return (
    <>
      <Navbar />
      <main className="relative min-h-screen overflow-hidden bg-ink-950 text-white">
        <div className="pointer-events-none absolute inset-0">
          <div className="absolute -top-40 left-1/2 h-[480px] w-[480px] -translate-x-1/2 rounded-full bg-neon-violet/20 blur-[140px]" />
          <div className="absolute top-1/3 -left-40 h-[360px] w-[360px] rounded-full bg-neon-cyan/15 blur-[120px]" />
        </div>

        <div className="relative mx-auto max-w-5xl px-5 pb-24 pt-28">
          <section className="text-center">
            <div className="mx-auto mb-6 flex items-center justify-center gap-3">
              <ProductIcon product="matrixx" size={56} className="h-14 w-14 object-contain" alt="MatrixX" />
              <span className="text-2xl font-bold tracking-wide">
                MatrixX <span className="text-slate-400">智控</span>
              </span>
            </div>
            <p className="mb-3 text-xs font-medium uppercase tracking-[0.3em] text-neon-cyan">{h.kicker[lang]}</p>
            <h1 className="bg-gradient-to-r from-neon-cyan via-white to-neon-violet bg-clip-text text-4xl font-black leading-tight text-transparent sm:text-6xl">
              {h.title[lang]}
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-base leading-relaxed text-slate-300 sm:text-lg">
              {h.subtitle[lang]}
            </p>
            <p className="mx-auto mt-4 text-xs text-slate-500">{h.trustline[lang]}</p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/en/matrix/download"
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-4 w-4" />
                Download the client
              </Link>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-white/30"
              >
                Talk to us on Telegram
                <ArrowRight className="h-4 w-4 transition group-hover:translate-x-0.5" />
              </a>
            </div>
          </section>

          <section className="mt-20">
            <h2 className="text-center text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
              Core capabilities
            </h2>
            <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {MATRIXX.caps.map((c) => (
                <div key={c.title[lang]} className="rounded-2xl border border-white/10 bg-white/[0.03] p-6">
                  <h3 className="text-lg font-bold text-white">{c.title[lang]}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{c.desc[lang]}</p>
                </div>
              ))}
            </div>
          </section>

          <section className="mt-16 rounded-2xl border border-white/10 bg-white/[0.02] p-8">
            <h2 className="text-center text-lg font-semibold text-white">{MATRIXX.scenarios.title[lang]}</h2>
            <div className="mx-auto mt-5 grid max-w-3xl gap-3 sm:grid-cols-2">
              {MATRIXX.scenarios.items.map((s) => (
                <div key={s[lang]} className="flex items-start gap-2 text-sm leading-relaxed text-slate-300">
                  <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-neon-cyan/80" />
                  {s[lang]}
                </div>
              ))}
            </div>
          </section>

          <section className="mx-auto mt-16 max-w-xl rounded-3xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-8 text-center">
            <h3 className="text-xl font-bold text-white">Download now and launch your first Telegram fleet</h3>
            <p className="mt-2 text-sm text-slate-400">
              Runs locally, data on-device, no GPU, ready after sign-up. As a Growth-family standalone deployment, it can be tailored to your team size and scenario.
            </p>
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/en/matrix/download"
                className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-4 w-4" />
                Download client
              </Link>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-white/30"
              >
                Contact us
              </a>
            </div>
          </section>
        </div>
      </main>
      <Footer />
    </>
  );
}
