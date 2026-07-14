"use client";

import Image from "next/image";
import { useLang } from "@/components/LanguageContext";
import Reveal from "@/components/fx/Reveal";
import { track } from "@/lib/track";
import { BRAND, PRODUCT_ORDER } from "@/lib/brand";
import { PRODUCT_IMG, PRODUCT_ANCHOR, PRODUCT_LANDING } from "@/components/productMeta";

/** 品牌家族展示带：无界公司主标 + 全产品 LOGO 星阵。
 *  与下方 ProductMatrix（详细目录卡片）分工不同——这里是品牌形象「全家福」：
 *  公司 ∞ 主标为核，六大产品玻璃 LOGO 环绕，共享「无界底座」。 */

const COPY = {
  zh: {
    kicker: "无界科技 · 品牌家族",
    coreLabel: "无界底座",
    coreSub: "BOUNDLESS ENGINE",
    headline: "一个无界底座，六大 AI 产品",
    sub: "同一套私有化底座，打破触达、容貌、声音、身份、语言、成交六道边界——从获客到成交，按需单选或组合成完整闭环。",
    breakLabel: "破",
  },
  en: {
    kicker: "BOUNDLESS · Product Family",
    coreLabel: "BOUNDLESS Engine",
    coreSub: "无 界 底 座",
    headline: "One core. Six AI products.",
    sub: "One private-deployment core breaks the barriers of face, voice, identity, language and sales — pick one, or combine them into a full loop.",
    breakLabel: "Breaks",
  },
} as const;

export default function BrandShowcase() {
  const { lang } = useLang();
  const c = COPY[lang];

  return (
    <section id="family" className="relative overflow-hidden py-24">
      {/* 氛围光晕 */}
      <div className="pointer-events-none absolute inset-0 -z-10">
        <div className="absolute left-1/2 top-10 h-72 w-72 -translate-x-1/2 rounded-full bg-neon-cyan/10 blur-[120px]" />
        <div className="absolute left-1/2 top-1/2 h-[38rem] w-[38rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-neon-violet/[0.06] blur-[150px]" />
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/10 to-transparent" />
      </div>

      <div className="mx-auto max-w-6xl px-5">
        {/* ── 公司主标锁定图 ── */}
        <Reveal className="flex flex-col items-center text-center">
          <p className="mb-6 text-xs font-medium uppercase tracking-[0.32em] text-neon-cyan">{c.kicker}</p>

          <div className="relative flex items-center justify-center">
            <span className="pointer-events-none absolute h-44 w-44 rounded-full bg-neon-cyan/20 blur-3xl" />
            <span className="pointer-events-none absolute h-56 w-56 rounded-full border border-white/5" />
            <span className="pointer-events-none absolute h-72 w-72 rounded-full border border-white/[0.04]" />
            <Image
              src="/brand/logos/boundless-mark-512.png"
              alt={BRAND.company.full}
              width={160}
              height={160}
              priority
              className="animate-float relative h-28 w-28 object-contain drop-shadow-[0_0_30px_rgba(34,211,238,0.35)] md:h-36 md:w-36"
              draggable={false}
            />
          </div>

          <h2 className="mt-7 text-4xl font-black tracking-tight text-white md:text-6xl">
            {BRAND.company.zh}
            <span className="text-gradient ml-3 tracking-[0.12em]">{BRAND.company.en}</span>
          </h2>
          <p className="mt-3 text-sm tracking-[0.4em] text-slate-400 md:text-base">
            {lang === "zh" ? BRAND.company.tagline.zh : BRAND.company.tagline.en}
          </p>

          <h3 className="mt-10 text-2xl font-bold text-white md:text-3xl">{c.headline}</h3>
          <p className="mx-auto mt-3 max-w-2xl text-sm leading-relaxed text-slate-400 md:text-base">{c.sub}</p>
        </Reveal>

        {/* ── 无界底座 pill（核 → 产品 的纽带）── */}
        <Reveal delay={0.08} className="mt-10 flex items-center justify-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-neon-cyan/30 bg-gradient-to-r from-neon-cyan/10 to-neon-violet/10 px-5 py-2 backdrop-blur-sm">
            <span className="grid h-5 w-5 place-items-center rounded-full bg-neon-cyan/20 text-[11px] text-neon-cyan">∞</span>
            <span className="text-sm font-semibold text-white">{c.coreLabel}</span>
            <span className="text-[10px] font-medium uppercase tracking-[0.2em] text-neon-cyan/80">{c.coreSub}</span>
          </span>
        </Reveal>

        {/* 连接线：底座 pill → 产品星阵 */}
        <div className="mx-auto mt-2 h-8 w-px bg-gradient-to-b from-neon-cyan/40 to-transparent" aria-hidden />

        {/* ── 产品 LOGO 星阵 ── */}
        <div className="grid grid-cols-2 gap-x-4 gap-y-10 sm:grid-cols-3 md:flex md:flex-wrap md:items-start md:justify-center md:gap-x-10 md:gap-y-12 lg:gap-x-14">
          {PRODUCT_ORDER.map((key, i) => {
            const p = BRAND.products[key];
            const landing = PRODUCT_LANDING[key];
            const href = landing ? (lang === "zh" ? landing : `/en${landing}`) : PRODUCT_ANCHOR[key];
            return (
              <Reveal key={key} delay={0.12 + i * 0.08} className="flex justify-center">
                <a
                  href={href}
                  onClick={() => track("product_click", { key, where: "brand_showcase" })}
                  className="group flex w-28 flex-col items-center text-center md:w-32"
                >
                  {/* 图标 + 发光台座 */}
                  <span className="relative grid h-24 w-24 place-items-center md:h-28 md:w-28">
                    <span className="pointer-events-none absolute inset-2 rounded-[28%] bg-gradient-to-br from-white/[0.06] to-white/[0.01] ring-1 ring-white/10 transition duration-500 group-hover:ring-neon-cyan/40" />
                    <span className="pointer-events-none absolute inset-0 rounded-full bg-neon-cyan/10 opacity-0 blur-xl transition duration-500 group-hover:opacity-100" />
                    <Image
                      src={PRODUCT_IMG[key]}
                      alt={`${p.zh} ${p.en}`}
                      width={96}
                      height={96}
                      className="animate-float relative h-16 w-16 object-contain transition-transform duration-500 group-hover:scale-110 md:h-20 md:w-20"
                      style={{ animationDelay: `${i * 0.6}s` }}
                      draggable={false}
                    />
                  </span>

                  {/* 名称 */}
                  <span className="mt-4 block text-base font-bold text-white transition-colors group-hover:text-neon-cyan md:text-lg">
                    {p.zh}
                  </span>
                  <span className="mt-0.5 block text-xs font-semibold uppercase tracking-wider text-neon-cyan/80">
                    {p.en}
                  </span>

                  {/* 破界 chip */}
                  <span className="mt-2.5 inline-flex items-center gap-1 rounded-full border border-neon-violet/25 bg-neon-violet/10 px-2.5 py-0.5 text-[10px] font-medium text-neon-violet">
                    {c.breakLabel} · {p.break[lang]}
                  </span>
                </a>
              </Reveal>
            );
          })}
        </div>
      </div>
    </section>
  );
}
