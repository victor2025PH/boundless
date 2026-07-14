"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowRight, Sparkles, ShieldCheck } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useInView } from "@/lib/useInView";
import Reveal from "./fx/Reveal";
import Magnetic from "./fx/Magnetic";
import CountUp from "./fx/CountUp";
import Tilt from "./fx/Tilt";
import AutoChatDemo from "./AutoChatDemo";
import { track } from "@/lib/track";
import { abVariant, abExpose, HERO_CTA_COPY, type AbVariant } from "@/lib/ab";

function suffixOf(v: string) {
  return v.replace(/[0-9.]/g, "");
}

/** 开场→Hero 冲越交接状态:
 *  none = 无开场(回访),标题正常显示;hold = 开场展示中,标题隐藏待命;
 *  play = 用户冲越光门,标题逐字全息聚焦 + 冲击波扩散。 */
type Handoff = "none" | "hold" | "play";

/** 把词组拆成逐字 span(保留 --i 用于级联延迟);空格转 NBSP 防塌缩。
 *  gradient: 渐变文本的 bg-clip:text 不会穿透 inline-block 子元素,
 *  逐字阶段每个字符自带 text-gradient,动画结束由父组件还原为整段文本。 */
function HandoffChars({
  text,
  state,
  base,
  gradient,
}: {
  text: string;
  state: Handoff;
  base: number;
  gradient?: boolean;
}) {
  if (state === "none") return <>{text}</>;
  const cls = `hero-ch ${state === "hold" ? "hold" : "play"}${gradient ? " text-gradient" : ""}`;
  return (
    <>
      {Array.from(text).map((ch, i) => (
        <span key={i} className={cls} style={{ "--i": base + i } as CSSProperties}>
          {ch === " " ? "\u00A0" : ch}
        </span>
      ))}
    </>
  );
}

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
    <section id="top" className="relative overflow-hidden pt-32 pb-20">
      {/* 冲越交接的径向冲击波:与开场页退场白光衔接,播完即卸载 */}
      {handoff === "play" && <Shockwave />}
      <div className="relative mx-auto grid max-w-7xl items-center gap-10 px-5 lg:grid-cols-2">
        {/* Left: copy — 首屏全部走 eager(CSS 入场):SSR 首帧即可绘制,LCP 不被动画推迟 */}
        <div className="text-center lg:text-left">
          <Reveal eager>
            <div className="mx-auto mb-6 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-1.5 text-xs text-slate-300 lg:mx-0">
              <Sparkles className="h-3.5 w-3.5 text-neon-cyan" />
              {t.hero.badge}
            </div>
          </Reveal>

          <Reveal eager delay={0.05}>
            {/* whitespace-nowrap: 词组整体换行，避免 CJK 单字孤行（如"统"字单独一行） */}
            <h1 className="mx-auto max-w-xl text-4xl font-bold leading-tight text-white md:text-6xl lg:mx-0">
              <span className="whitespace-nowrap">
                <HandoffChars text={t.hero.title} state={handoff} base={0} />
              </span>{" "}
              <span className={`whitespace-nowrap${handoff === "none" ? " text-gradient" : ""}`}>
                <HandoffChars
                  text={t.hero.titleAccent}
                  state={handoff}
                  base={Array.from(t.hero.title).length + 1}
                  gradient
                />
              </span>
            </h1>
          </Reveal>

          <Reveal eager delay={0.1}>
            <div
              ref={rotRef}
              className="mt-4 flex h-8 items-center justify-center gap-2 text-lg font-medium text-slate-300 lg:justify-start"
            >
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

          <Reveal eager delay={0.16}>
            <p className="mx-auto mt-5 max-w-xl text-base text-slate-400 md:text-lg lg:mx-0">
              {t.hero.subtitle}
            </p>
          </Reveal>

          <Reveal eager delay={0.22}>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row lg:justify-start">
              <Magnetic>
                <a
                  href="#autochat"
                  onClick={() => track("cta_click", { where: "hero_primary", ab: ctaVariant })}
                  className="cta-fx group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 font-medium text-ink-950 transition hover:opacity-90"
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

          <Reveal eager delay={0.28}>
            <p className="mt-5 flex items-center justify-center gap-2 text-xs text-slate-500 lg:justify-start">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
              {t.hero.trustline}
            </p>
          </Reveal>
        </div>

        {/* Right: AI auto-closing chat demo (primary flagship), 3D tilt + hover halo */}
        <Reveal eager delay={0.1} className="order-first flex items-center justify-center lg:order-last">
          <Tilt className="w-full max-w-[420px]">
            <AutoChatDemo />
            <span className="tilt-glow" aria-hidden />
          </Tilt>
        </Reveal>
      </div>

      {/* Stats */}
      <Reveal delay={0.1} className="mx-auto mt-14 grid max-w-4xl grid-cols-2 gap-4 px-5 md:grid-cols-4">
        {t.hero.stats.map((s) => (
          <div key={s.label} className="glass card-hover rounded-2xl px-4 py-5 text-center">
            <div className="text-gradient text-3xl font-bold">
              <CountUp value={s.value} suffix={suffixOf(s.value)} />
            </div>
            <div className="mt-1 text-xs text-slate-400">{s.label}</div>
          </div>
        ))}
      </Reveal>
    </section>
  );
}
