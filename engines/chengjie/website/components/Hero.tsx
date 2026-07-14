"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowRight, Sparkles, ShieldCheck, AudioLines } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import Magnetic from "./fx/Magnetic";
import CountUp from "./fx/CountUp";
import AutoChatDemo from "./AutoChatDemo";
import { AudioClip } from "./fx/MediaClips";
import { track } from "@/lib/track";
import { abVariant, abExpose, HERO_CTA_COPY, type AbVariant } from "@/lib/ab";
import { BRAND, PRODUCT_ORDER } from "@/lib/brand";
import { PRODUCT_IMG, PRODUCT_ANCHOR } from "./productMeta";

function suffixOf(v: string) {
  return v.replace(/[0-9.]/g, "");
}

export default function Hero() {
  const { t, lang } = useLang();
  const reduced = useReducedMotion();
  const [idx, setIdx] = useState(0);
  // SSR/首帧渲染对照组文案，挂载后按本地分桶切换并记曝光（同访客桶恒定，无闪烁感）
  const [ctaVariant, setCtaVariant] = useState<AbVariant>("a");

  useEffect(() => {
    const v = abVariant("hero_cta");
    setCtaVariant(v);
    abExpose("hero_cta", v);
  }, []);

  useEffect(() => {
    if (reduced) return;
    const id = setInterval(() => setIdx((i) => (i + 1) % t.hero.rotating.length), 2200);
    return () => clearInterval(id);
  }, [reduced, t.hero.rotating.length]);

  return (
    <section id="top" className="relative overflow-hidden pt-32 pb-20">
      <div className="relative mx-auto grid max-w-7xl items-center gap-10 px-5 lg:grid-cols-2">
        {/* Left: copy */}
        <div className="text-center lg:text-left">
          <Reveal>
            <div className="mx-auto mb-6 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-1.5 text-xs text-slate-300 lg:mx-0">
              <Sparkles className="h-3.5 w-3.5 text-neon-cyan" />
              {t.hero.badge}
            </div>
          </Reveal>

          <Reveal delay={0.05}>
            {/* whitespace-nowrap: 词组整体换行，避免 CJK 单字孤行（如"统"字单独一行） */}
            <h1 className="mx-auto max-w-xl text-4xl font-bold leading-tight text-white md:text-6xl lg:mx-0">
              <span className="whitespace-nowrap">{t.hero.title}</span>{" "}
              <span className="text-gradient whitespace-nowrap">{t.hero.titleAccent}</span>
            </h1>
          </Reveal>

          <Reveal delay={0.1}>
            <div className="mt-4 flex h-8 items-center justify-center gap-2 text-lg font-medium text-slate-300 lg:justify-start">
              <span className="text-neon-cyan">▍</span>
              <AnimatePresence mode="wait">
                <motion.span
                  key={idx}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -8 }}
                  transition={{ duration: 0.3 }}
                  className="text-gradient"
                >
                  {t.hero.rotating[idx]}
                </motion.span>
              </AnimatePresence>
            </div>
          </Reveal>

          <Reveal delay={0.16}>
            <p className="mx-auto mt-5 max-w-xl text-base text-slate-400 md:text-lg lg:mx-0">
              {t.hero.subtitle}
            </p>
          </Reveal>

          <Reveal delay={0.22}>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row lg:justify-start">
              <Magnetic>
                <a
                  href="#autochat"
                  onClick={() => track("cta_click", { where: "hero_primary", ab: ctaVariant })}
                  className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 font-medium text-ink-950 transition hover:opacity-90"
                >
                  {ctaVariant === "a" ? t.hero.ctaPrimary : HERO_CTA_COPY.b[lang]}
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                </a>
              </Magnetic>
              <a
                href="#pricing"
                onClick={() => track("cta_click", { where: "hero_secondary" })}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {t.hero.ctaSecondary}
              </a>
            </div>
          </Reveal>

          <Reveal delay={0.28}>
            <p className="mt-5 flex items-center justify-center gap-2 text-xs text-slate-500 lg:justify-start">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
              {t.hero.trustline}
            </p>
          </Reveal>

          {/* 首屏真实克隆音试听：点开即听引擎真实产出的克隆声 */}
          <Reveal delay={0.34}>
            <div className="mx-auto mt-6 max-w-sm lg:mx-0">
              <p className="mb-2 flex items-center justify-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-neon-cyan lg:justify-start">
                <AudioLines className="h-3.5 w-3.5" />
                {lang === "zh" ? "点开听一段 AI 克隆音（真实样片）" : "Play a real AI cloned-voice sample"}
              </p>
              <AudioClip
                label={lang === "zh" ? "AI 克隆音 · 中文样片" : "AI cloned voice · sample"}
                src="/showcase/real/voice-zh.mp3"
              />
            </div>
          </Reveal>
        </div>

        {/* Right: AI auto-closing chat demo (primary flagship) */}
        <Reveal delay={0.1} className="order-first flex items-center justify-center lg:order-last">
          <AutoChatDemo />
        </Reveal>
      </div>

      {/* Stats */}
      <Reveal delay={0.1} className="mx-auto mt-14 grid max-w-4xl grid-cols-2 gap-4 px-5 md:grid-cols-4">
        {t.hero.stats.map((s) => (
          <div key={s.label} className="glass card-hover rounded-2xl px-4 py-5 text-center">
            <div className="text-gradient text-3xl font-bold">
              {/[0-9]/.test(s.value) ? (
                <CountUp value={s.value} suffix={suffixOf(s.value)} />
              ) : (
                <span>{s.value}</span>
              )}
            </div>
            <div className="mt-1 text-xs text-slate-400">{s.label}</div>
          </div>
        ))}
      </Reveal>

      {/* 产品星阵：无界五大产品线 LOGO + 名 */}
      <Reveal delay={0.16} className="mx-auto mt-12 max-w-4xl px-5">
        <div className="rounded-3xl border border-white/10 bg-white/[0.02] px-4 py-6 backdrop-blur-sm md:px-8">
          <p className="mb-5 text-center text-xs uppercase tracking-[0.28em] text-neon-cyan">
            {lang === "zh" ? "无界五大产品线" : "Five BOUNDLESS product lines"}
          </p>
          <div className="flex flex-wrap items-start justify-center gap-5 md:gap-8">
            {PRODUCT_ORDER.map((key) => {
              const p = BRAND.products[key];
              return (
                <a
                  key={key}
                  href={PRODUCT_ANCHOR[key]}
                  onClick={() => track("product_click", { key, where: "hero_constellation" })}
                  className="group flex w-16 flex-col items-center gap-2 md:w-20"
                >
                  <span className="relative">
                    <span className="pointer-events-none absolute inset-0 rounded-full bg-neon-cyan/10 blur-md transition group-hover:bg-neon-cyan/25" />
                    <img
                      src={PRODUCT_IMG[key]}
                      alt={`${p.zh} ${p.en}`}
                      width={64}
                      height={64}
                      className="relative h-14 w-14 object-contain transition-transform group-hover:scale-110 md:h-16 md:w-16"
                      draggable={false}
                    />
                  </span>
                  <span className="text-center leading-tight">
                    <span className="block text-xs font-medium text-slate-200 group-hover:text-white">
                      {p.zh}
                    </span>
                    <span className="block text-[10px] text-neon-cyan/80">{p.en}</span>
                  </span>
                </a>
              );
            })}
          </div>
        </div>
      </Reveal>
    </section>
  );
}
