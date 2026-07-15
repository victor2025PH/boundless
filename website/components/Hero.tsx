"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowRight, ShieldCheck, ChevronDown } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useInView } from "@/lib/useInView";
import Reveal from "./fx/Reveal";
import Magnetic from "./fx/Magnetic";
import CountUp from "./fx/CountUp";
import BorderBeam from "./fx/BorderBeam";
import MatrixRain from "./fx/MatrixRain";
import { track } from "@/lib/track";
import { abVariant, abExpose, HERO_CTA_COPY, type AbVariant } from "@/lib/ab";

function suffixOf(v: string) {
  return v.replace(/[0-9.]/g, "");
}

/** 开场→Hero 冲越交接状态:none=无开场;hold=开场展示中;play=冲越光门(触发冲击波)。 */
type Handoff = "none" | "hold" | "play";

/** 一次性冲击波覆盖层:双环扩散 + 中心闪光,2.2s 后自卸载。 */
function Shockwave() {
  const [gone, setGone] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setGone(true), 2200);
    return () => window.clearTimeout(id);
  }, []);
  if (gone) return null;
  return (
    <div className="hero-shockwave" aria-hidden>
      <div className="flash" />
      <div className="sw sw1" />
      <div className="sw sw2" />
    </div>
  );
}

export default function Hero() {
  const { t, lang } = useLang();
  const reduced = useReducedMotion();
  const [idx, setIdx] = useState(0);
  // SSR/首帧渲染对照组文案，挂载后按本地分桶切换并记曝光（同访客桶恒定，无闪烁感）
  const [ctaVariant, setCtaVariant] = useState<AbVariant>("a");
  const [handoff, setHandoff] = useState<Handoff>("none");

  useEffect(() => {
    const v = abVariant("hero_cta");
    setCtaVariant(v);
    abExpose("hero_cta", v);
  }, []);

  /* 开场页正在展示时挂起标题,冲越光门瞬间逐字聚焦(与开场退场动画重叠,叙事连续) */
  useEffect(() => {
    let introShowing = false;
    try {
      introShowing = !sessionStorage.getItem("bl-intro-seen");
    } catch {}
    if (!introShowing) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    setHandoff("hold");
    let recycle = 0;
    const onEnter = () => {
      setHandoff("play");
      // 逐字动画播完后还原为整段文本:渐变恢复连续,并清掉几十个字符 span
      recycle = window.setTimeout(() => setHandoff("none"), 3200);
    };
    window.addEventListener("bl-intro-entered", onEnter);
    // 兜底:事件丢失(异常路径)也绝不让标题永久隐藏
    const safety = window.setTimeout(() => setHandoff((h) => (h === "hold" ? "play" : h)), 20000);
    return () => {
      window.removeEventListener("bl-intro-entered", onEnter);
      window.clearTimeout(safety);
      window.clearTimeout(recycle);
    };
  }, []);

  // 轮换行滚出视口即停摆(待机不烧 CPU),回到视口自动恢复
  const { ref: rotRef, inView: rotInView } = useInView<HTMLDivElement>();
  useEffect(() => {
    if (reduced || !rotInView) return;
    const id = setInterval(() => setIdx((i) => (i + 1) % t.hero.rotating.length), 2200);
    return () => clearInterval(id);
  }, [reduced, rotInView, t.hero.rotating.length]);

  return (
    <section id="top" className="relative overflow-hidden">
      {/* 冲越交接的径向冲击波:与开场页退场白光衔接,播完即卸载 */}
      {handoff === "play" && <Shockwave />}

      {/* ===== 首屏：独占整屏 · 居中 · 黑客帝国风 ===== */}
      <div className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-5 pb-16 pt-28 text-center">
        {/* 背景特效层：数字雨 + 极光 + 网格 + 光波流动 */}
        <div className="pointer-events-none absolute inset-0 -z-10" aria-hidden>
          <MatrixRain className="absolute inset-0 h-full w-full opacity-[0.28] [mask-image:radial-gradient(ellipse_75%_65%_at_50%_45%,#000_5%,transparent_78%)]" />
          <div className="hero-aurora absolute left-1/2 top-[-6%] h-[62vmax] w-[62vmax] -translate-x-1/2 rounded-full opacity-50" />
          <div className="absolute inset-0 bg-[linear-gradient(rgba(148,163,184,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(148,163,184,0.05)_1px,transparent_1px)] bg-[size:44px_44px] [mask-image:radial-gradient(ellipse_60%_60%_at_50%_42%,#000_28%,transparent_74%)]" />
          <div className="hero-flow absolute inset-0" />
          <div className="hero-scan absolute inset-x-0 top-0 h-40 opacity-30" />
        </div>

        <Reveal eager>
          <div className="mx-auto mb-8 inline-flex items-center gap-2 rounded-full border border-neon-cyan/25 bg-neon-cyan/[0.06] px-4 py-1.5 text-xs text-slate-200 shadow-[0_0_20px_rgba(34,211,238,0.15)]">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
            </span>
            {t.hero.badge}
          </div>
        </Reveal>

        <Reveal eager delay={0.05}>
          {/* 巨型居中主标题：三行逐行向上旋转入场，到中心放大闪光；流光渐变 + 电光 + 环绕闪电 */}
          <div className="hero-title-stage relative mx-auto [perspective:900px]">
            {/* 环绕闪电（flanking 闪电束，随机闪烁） */}
            <svg className="hero-bolts pointer-events-none absolute left-1/2 top-1/2 h-[150%] w-[130%] -translate-x-1/2 -translate-y-1/2" viewBox="0 0 800 400" fill="none" aria-hidden>
              <path className="bolt b1" d="M60 40 L120 150 L80 160 L150 300" stroke="#67e8f9" strokeWidth="2" />
              <path className="bolt b2" d="M740 60 L680 170 L720 180 L640 320" stroke="#a78bfa" strokeWidth="2" />
              <path className="bolt b3" d="M700 30 L660 120 L690 128 L620 250" stroke="#6ee7b7" strokeWidth="1.5" />
            </svg>
            <h1 className="hero-matrix relative mx-auto max-w-[18ch] text-[2.5rem] font-black leading-[1.14] tracking-tight sm:text-6xl md:text-7xl">
              {t.hero.titleLines.map((line, i) => (
                <span key={line} className="hero-line block" style={{ "--i": i } as CSSProperties}>
                  {line}
                </span>
              ))}
            </h1>
          </div>
        </Reveal>

        <Reveal eager delay={0.12}>
          <div ref={rotRef} className="mt-7 flex h-9 items-center justify-center">
            <AnimatePresence mode="wait">
              <motion.span
                key={idx}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                transition={{ duration: 0.35 }}
                className="inline-flex items-center rounded-full border border-neon-cyan/25 bg-white/[0.04] px-5 py-1.5 shadow-[0_0_18px_rgba(34,211,238,0.12)] backdrop-blur-sm"
              >
                <span className="hero-subrotate text-lg font-semibold tracking-wide md:text-xl">{t.hero.rotating[idx]}</span>
              </motion.span>
            </AnimatePresence>
          </div>
        </Reveal>

        <Reveal eager delay={0.18}>
          <p className="mx-auto mt-6 max-w-3xl text-center text-base leading-relaxed text-slate-300 md:text-lg">
            {t.hero.subtitle}
          </p>
        </Reveal>

        <Reveal eager delay={0.24}>
          <div className="mt-10 flex flex-col items-center justify-center gap-4 sm:flex-row">
            <Magnetic>
              <a
                href="#autochat"
                onClick={() => track("cta_click", { where: "hero_primary", ab: ctaVariant })}
                className="btn-3d hero-cta group relative inline-flex items-center gap-2 overflow-hidden rounded-full px-9 py-4 text-base font-bold text-ink-950"
              >
                <BorderBeam />
                <span className="btn-3d-gloss pointer-events-none absolute inset-0" aria-hidden />
                <span className="hero-cta-sheen pointer-events-none absolute inset-0" aria-hidden />
                <span className="relative">{ctaVariant === "a" ? t.hero.ctaPrimary : HERO_CTA_COPY.b[lang]}</span>
                <ArrowRight className="relative h-4 w-4 transition-transform group-hover:translate-x-1" />
              </a>
            </Magnetic>
            <a
              href="#pricing"
              onClick={() => track("cta_click", { where: "hero_secondary" })}
              className="btn-3d-ghost group relative inline-flex items-center gap-2 rounded-full px-9 py-4 text-base font-semibold text-white"
            >
              <span className="btn-3d-gloss pointer-events-none absolute inset-0" aria-hidden />
              <span className="relative">{t.hero.ctaSecondary}</span>
              <ArrowRight className="relative h-4 w-4 opacity-70 transition-transform group-hover:translate-x-1 group-hover:opacity-100" />
            </a>
          </div>
        </Reveal>

        <Reveal eager delay={0.3}>
          <p className="mt-6 flex items-center justify-center gap-2 text-xs text-slate-500">
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
            {t.hero.trustline}
          </p>
        </Reveal>

        {/* Stats：居中一整行 */}
        <Reveal delay={0.36} className="mx-auto mt-12 grid w-full max-w-3xl grid-cols-2 gap-4 md:grid-cols-4">
          {t.hero.stats.map((s) => (
            <div key={s.label} className="glass card-hover rounded-2xl px-4 py-5 text-center">
              <div className="text-gradient text-3xl font-bold md:text-4xl">
                <CountUp value={s.value} suffix={suffixOf(s.value)} />
              </div>
              <div className="mt-1 text-xs text-slate-400">{s.label}</div>
            </div>
          ))}
        </Reveal>

        {/* 向下滚动提示 */}
        <div className="mt-12 flex flex-col items-center gap-1 text-slate-500">
          <span className="text-[10px] font-medium uppercase tracking-[0.35em]">Scroll</span>
          <ChevronDown className="h-4 w-4 animate-bounce" />
        </div>
      </div>
    </section>
  );
}
