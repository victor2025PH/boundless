"use client";

import { FileCheck, Globe, ShieldCheck } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import CountUp from "./fx/CountUp";
import { BrandGlyph, BRAND_BG } from "./brandIcons";

/** 平台图标：brandIcons 有官方字形的用字形；Web（网页客服）这类非品牌渠道用通用图标。 */
function PlatformGlyph({ name, className }: { name: string; className?: string }) {
  if (name === "Web") return <Globe className={className} aria-hidden />;
  return <BrandGlyph name={name} className={className} />;
}

export default function TrustBar() {
  const { t } = useLang();
  // 前 4 张 = 主数字大卡，其余 = 次级紧凑卡（数据顺序即层级，见 content.ts trust.stats 注释）
  const heroStats = t.trust.stats.slice(0, 4);
  const subStats = t.trust.stats.slice(4);

  return (
    <section className="relative border-y border-white/5 py-16">
      <div className="mx-auto max-w-7xl px-5">
        {/* ── 平台墙 · 第一层：已深度对接（品牌色亮起 + 在线点 + 能力小注） ── */}
        <p className="text-center text-xs uppercase tracking-widest text-slate-500">
          {t.trust.platformsLabel}
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          {t.trust.platformsLive.map((p) => (
            <div
              key={p.name}
              className="group flex items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.03] py-2.5 pl-3 pr-4 transition hover:border-white/25 hover:bg-white/[0.06]"
            >
              <span
                className="grid h-10 w-10 shrink-0 place-items-center rounded-xl text-white transition group-hover:scale-110"
                style={{ background: BRAND_BG[p.name] ?? "#64748b" }}
              >
                <PlatformGlyph name={p.name} className="h-5 w-5" />
              </span>
              <span>
                <span className="flex items-center gap-1.5 text-sm font-medium text-slate-200 transition group-hover:text-white">
                  {p.label ?? p.name}
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"
                    aria-hidden
                  />
                </span>
                <span className="block text-[11px] text-slate-500">{p.note}</span>
              </span>
            </div>
          ))}
        </div>

        {/* ── 平台墙 · 第二层：陆续接入（灰阶待点亮，hover 恢复品牌色） ── */}
        <p className="mt-8 text-center text-[11px] uppercase tracking-widest text-slate-600">
          {t.trust.platformsComingLabel}
        </p>
        <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
          {t.trust.platformsComing.map((name) => (
            <div
              key={name}
              className="group flex items-center gap-2 rounded-full border border-white/[0.07] bg-white/[0.02] py-1.5 pl-2 pr-3 opacity-70 transition hover:border-white/15 hover:opacity-100"
            >
              <span
                className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-white grayscale transition group-hover:grayscale-0"
                style={{ background: BRAND_BG[name] ?? "#64748b" }}
              >
                <PlatformGlyph name={name} className="h-3.5 w-3.5" />
              </span>
              <span className="text-xs text-slate-400 transition group-hover:text-slate-200">{name}</span>
            </div>
          ))}
        </div>

        {/* ── 工程事实：主数字 4 大卡 + 次级 4 紧凑卡 ── */}
        <div className="mt-14 text-center">
          <h2 className="text-2xl font-bold text-white md:text-3xl">{t.trust.statsTitle}</h2>
          <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{t.trust.statsSubtitle}</p>
        </div>
        <div className="mt-8 grid grid-cols-2 gap-4 md:grid-cols-4">
          {heroStats.map((s, i) => (
            <Reveal key={s.label} delay={i * 0.06}>
              <div className="glass card-hover flex h-full flex-col rounded-2xl px-4 py-6 text-center">
                <div className="text-gradient text-3xl font-bold md:text-4xl">
                  <CountUp value={s.value} suffix={s.suffix} />
                </div>
                <div className="mt-2 text-xs text-slate-300">{s.label}</div>
                {s.sub && <div className="mt-1.5 text-[11px] leading-relaxed text-slate-500">{s.sub}</div>}
              </div>
            </Reveal>
          ))}
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          {subStats.map((s, i) => (
            <Reveal key={s.label} delay={0.24 + i * 0.06}>
              <div className="card-hover flex h-full flex-col rounded-2xl border border-white/[0.07] bg-white/[0.02] px-4 py-4 text-center">
                <div className="text-xl font-bold text-slate-100 md:text-2xl">
                  <CountUp value={s.value} suffix={s.suffix} />
                </div>
                <div className="mt-1.5 text-xs text-slate-400">{s.label}</div>
                {s.sub && <div className="mt-1 text-[11px] leading-relaxed text-slate-600">{s.sub}</div>}
              </div>
            </Reveal>
          ))}
        </div>

        {/* ── 证据卡（非好评卡）：中性来源徽标，出处见卡底 role 行 ── */}
        <div className="mt-16 text-center">
          <h2 className="text-2xl font-bold text-white md:text-3xl">{t.trust.testimonialsTitle}</h2>
        </div>
        <div className="mt-8 grid gap-5 md:grid-cols-3">
          {t.trust.testimonials.map((tm, i) => (
            <Reveal key={tm.name} delay={i * 0.08}>
              <figure className="card-hover flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-6">
                <div className="flex items-center justify-between">
                  <ShieldCheck className="h-6 w-6 text-neon-cyan/70" aria-hidden />
                  <FileCheck className="h-4 w-4 text-slate-500" aria-hidden />
                </div>
                <blockquote className="mt-3 flex-1 text-sm leading-relaxed text-slate-300">
                  &ldquo;{tm.quote}&rdquo;
                </blockquote>
                <figcaption className="mt-4 flex items-center gap-3">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-neon-cyan/30 to-neon-violet/30 text-sm font-semibold text-white ring-1 ring-white/10">
                    {tm.name.slice(0, 1)}
                  </span>
                  <span>
                    <span className="block text-sm font-medium text-white">{tm.name}</span>
                    <span className="block text-xs text-slate-500">{tm.role}</span>
                  </span>
                </figcaption>
              </figure>
            </Reveal>
          ))}
        </div>

        <p className="mx-auto mt-8 max-w-3xl text-center text-xs leading-relaxed text-slate-500">
          {t.trust.disclaimer}
        </p>
      </div>
    </section>
  );
}
