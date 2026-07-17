"use client";

import Image from "next/image";
import { motion, useReducedMotion, type MotionProps } from "framer-motion";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { BRAND, PRODUCT_ORDER, PRODUCT_COUNT, CATEGORIES, CATEGORY_ORDER, productsInCategory } from "@/lib/brand";
import { CATEGORY_UI } from "@/lib/categoryUi";
import { PRODUCT_IMG, PRODUCT_ANCHOR, PRODUCT_LANDING, PRODUCT_OPTICAL_SCALE } from "./productMeta";
import { track } from "@/lib/track";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { localePath } from "@/lib/site";

const COPY = {
  zh: {
    kicker: "产品矩阵",
    headPrefix: "三大产品系，",
    headSuffix: "条产品线",
    sub: "智连获客成交、幻境数字分身、通达跨语沟通——三系共享一个无界底座；每条产品线可单独选用，也能组合成「获客 + 数字分身 + 自动成交」的完整闭环。",
    breakLabel: "打破",
    engineName: "无界底座 BOUNDLESS Engine",
    engineDesc: "三系产品共享同一私有化底座：数据不出网、自主可控、合规可溯源。",
    ctaPrimary: "查看套餐与价格",
    ctaSecondary: "了解品牌故事",
  },
  en: {
    kicker: "Product Matrix",
    headPrefix: "Three families, ",
    headSuffix: " product lines",
    sub: "Growth for reach & closing, Studio for digital twins, Lingo for cross-language — three families on one BOUNDLESS core. Pick any line on its own, or combine them into a full \"lead-gen + digital twin + auto-closing\" loop.",
    breakLabel: "Breaks",
    engineName: "BOUNDLESS Engine",
    engineDesc: "Three families share one private-deployment core: data stays off-net, self-controlled and verifiably compliant.",
    ctaPrimary: "View plans & pricing",
    ctaSecondary: "Read the brand story",
  },
} as const;

export default function ProductMatrix() {
  const { lang } = useLang();
  const reduced = useReducedMotion();
  const c = COPY[lang];

  /* 图标入场描边:滚入视口时青色辉光闪现一次后收敛为微光,级联点亮产品线 */
  const iconGlow = (idx: number): MotionProps =>
    reduced
      ? {}
      : {
          initial: { boxShadow: "0 0 0px 0px rgba(34, 211, 238, 0)" },
          whileInView: {
            boxShadow: [
              "0 0 0px 0px rgba(34, 211, 238, 0)",
              "0 0 26px 5px rgba(34, 211, 238, 0.45)",
              "0 0 13px 1px rgba(34, 211, 238, 0.14)",
            ],
          },
          viewport: { once: true, margin: "-80px" },
          transition: { duration: 1.3, times: [0, 0.4, 1], delay: 0.3 + idx * 0.1 },
        };

  return (
    <section id="products" className="relative py-24">
      <div className="mx-auto max-w-7xl px-5">
        <Reveal>
          <p className="text-center text-xs font-medium uppercase tracking-[0.28em] text-neon-cyan">
            {c.kicker}
          </p>
          <h2 className="mx-auto mt-3 max-w-3xl text-center text-3xl font-bold text-white md:text-4xl">
            {c.headPrefix}
            {PRODUCT_COUNT}
            {c.headSuffix}
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-center text-base text-slate-400">
            {c.sub}
          </p>
        </Reveal>

        <div className="mt-12 space-y-12">
          {CATEGORY_ORDER.map((cat) => {
            const cc = CATEGORIES[cat];
            const ui = CATEGORY_UI[cat];
            const items = productsInCategory(cat);
            const borderL =
              cat === "growth"
                ? "border-neon-cyan/50"
                : cat === "studio"
                  ? "border-neon-violet/50"
                  : "border-amber-400/50";
            return (
              <div key={cat}>
                <Reveal>
                  <div className={`mb-5 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-l-2 pl-3 ${borderL}`}>
                    <h3 className="text-lg font-bold text-white">
                      {lang === "zh" ? cc.zh : cc.en}
                      <span className={`ml-2 text-sm font-medium ${ui.label}`}>
                        {lang === "zh" ? cc.en : cc.zh}
                      </span>
                    </h3>
                    <span className="text-xs text-slate-500">
                      {c.breakLabel} · {lang === "zh" ? cc.breakZh : cc.breakEn}
                    </span>
                  </div>
                </Reveal>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {items.map((key) => {
                    const idx = PRODUCT_ORDER.indexOf(key);
                    const p = BRAND.products[key];
                    const landing = PRODUCT_LANDING[key];
                    const href = landing ? localePath(lang, landing) : PRODUCT_ANCHOR[key];
                    const optical = PRODUCT_OPTICAL_SCALE[key] ?? 1;
                    return (
                      <Reveal key={key} delay={(idx % 3) * 0.05}>
                        <a
                          href={href}
                          onClick={() => track("product_click", { key, where: "matrix" })}
                          className={`group relative flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] p-5 transition hover:bg-white/[0.05] ${ui.border}`}
                        >
                          <div className="mb-4 flex items-center justify-between">
                            <motion.span className="inline-grid place-items-center rounded-xl" {...iconGlow(idx)}>
                              <span style={optical !== 1 ? { transform: `scale(${optical})` } : undefined} className="inline-grid">
                                <Image
                                  src={PRODUCT_IMG[key]}
                                  alt={`${p.zh} ${p.en}`}
                                  width={48}
                                  height={48}
                                  className="h-12 w-12 object-contain transition-transform group-hover:scale-110"
                                  draggable={false}
                                />
                              </span>
                            </motion.span>
                            <span className="font-mono text-xs text-slate-600">0{idx + 1}</span>
                          </div>
                          <div className="flex items-baseline gap-2">
                            <span className="text-xl font-bold text-white">{p.zh}</span>
                            <span className="text-sm font-semibold text-neon-cyan">{p.en}</span>
                          </div>
                          <p className="mt-0.5 text-xs text-slate-500">{p.scene[lang]} · {p.alt}</p>
                          <p className="mt-3 flex-1 text-sm leading-relaxed text-slate-300">{p.desc[lang]}</p>
                          <p className={`mt-3 inline-flex w-fit items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-medium ${ui.chip}`}>
                            {c.breakLabel} · {p.break[lang]}
                          </p>
                        </a>
                      </Reveal>
                    );
                  })}
                </div>
              </div>
            );
          })}

          {/* 无界底座横幅（托起三系七产品） */}
          <Reveal>
            <div className="relative flex flex-col overflow-hidden rounded-2xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-5 sm:flex-row sm:items-center sm:gap-4">
              <motion.span
                className="mb-4 flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-neon-cyan/20 text-neon-cyan sm:mb-0"
                {...iconGlow(PRODUCT_ORDER.length)}
              >
                <ShieldCheck className="h-5 w-5" />
              </motion.span>
              <div>
                <div className="text-xl font-bold text-white">{c.engineName}</div>
                <p className="mt-2 text-sm leading-relaxed text-slate-300">{c.engineDesc}</p>
              </div>
            </div>
          </Reveal>
        </div>

        <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <a
            href="#pricing"
            onClick={() => track("cta_click", { where: "matrix_primary" })}
            className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
          >
            {c.ctaPrimary}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </a>
          <a
            href="/brand"
            onClick={() => track("cta_click", { where: "matrix_brand" })}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            {c.ctaSecondary}
          </a>
        </div>
      </div>
    </section>
  );
}
