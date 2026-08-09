"use client";

import Link from "next/link";
import { useLang } from "./LanguageContext";
import { localePath } from "@/lib/site";
import { BRAND_FILM } from "@/lib/film";
import FilmPlayer from "./FilmPlayer";

/** 首页影院区（Hero 之后第二屏）：主张 → 证据的叙事位。
 *  文案纪律：自证式钩子 + CTA 带时长承诺；绿色只用于信任标记。 */
export default function FilmSection() {
  const { lang } = useLang();
  const zh = lang === "zh";

  return (
    <section id="film" className="relative border-y border-white/5 bg-white/[0.015] py-20">
      <div className="pointer-events-none absolute inset-0 bg-grid-glow opacity-40" />
      <div className="relative mx-auto grid max-w-7xl items-center gap-10 px-5 lg:grid-cols-[5fr,7fr]">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-emerald-300">
            {zh ? "REAL ENGINE OUTPUT · 真实引擎输出" : "REAL ENGINE OUTPUT"}
          </p>
          <h2 className="mt-4 text-3xl font-bold leading-tight text-white sm:text-4xl">
            {BRAND_FILM.title[lang]}
          </h2>
          <p className="mt-4 text-sm leading-relaxed text-slate-400">{BRAND_FILM.tagline[lang]}</p>
          <div className="mt-7 flex flex-wrap gap-3">
            <Link
              href={localePath(lang, "/film")}
              className="rounded-full bg-neon-blue px-5 py-2.5 text-sm font-semibold text-white transition hover:brightness-110"
            >
              {zh
                ? `完整页 · 章节跳转（${BRAND_FILM.durationLabel.zh}）`
                : `Film page · chapters (${BRAND_FILM.durationLabel.en})`}
            </Link>
            <Link
              href={localePath(lang, "/download")}
              className="rounded-full border border-white/15 px-5 py-2.5 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
            >
              {zh ? "14 天全功能试用" : "Start the 14-day full trial"}
            </Link>
          </div>
        </div>
        <FilmPlayer lang={lang} />
      </div>
    </section>
  );
}
