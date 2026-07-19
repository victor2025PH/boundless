"use client";

import Image from "next/image";
import { useEffect, useState, type CSSProperties } from "react";
import { useReducedMotionSafe } from "@/components/fx/useReducedMotionSafe";
import { useLang } from "@/components/LanguageContext";
import Reveal from "@/components/fx/Reveal";
import { track } from "@/lib/track";
import {
  BRAND,
  CATEGORIES,
  CATEGORY_ORDER,
  FAMILY_PITCH,
  PRODUCT_COUNT,
  PRODUCT_ORDER,
  productsInCategory,
  type CategoryKey,
  type ProductKey,
} from "@/lib/brand";
import { CATEGORY_UI } from "@/lib/categoryUi";
import { localePath } from "@/lib/site";
import { PRODUCT_IMG, PRODUCT_ANCHOR, PRODUCT_LANDING, PRODUCT_OPTICAL_SCALE, PRODUCT_GLOW } from "@/components/productMeta";

/** 品牌家族展示带：无界公司主标 + 三系七产品 LOGO 星阵。
 *  与下方 ProductMatrix（详细目录卡片）分工不同——这里是品牌形象「全家福」：
 *  公司 ∞ 主标为核，七款产品按三系成簇环绕，共享「无界底座」。 */

const COPY = {
  zh: {
    kicker: "无界科技 · 品牌家族",
    coreLabel: "无界底座",
    coreSub: "BOUNDLESS ENGINE",
    breakLabel: "破",
    familyHint: `${PRODUCT_COUNT} 款产品 · 三系一底座`,
  },
  en: {
    kicker: "BOUNDLESS · Product Family",
    coreLabel: "BOUNDLESS Engine",
    coreSub: "无 界 底 座",
    breakLabel: "Breaks",
    familyHint: `${PRODUCT_COUNT} products · three families, one core`,
  },
} as const;

function ProductTile({
  keyName,
  idx,
  lang,
  breakLabel,
  cat,
  float,
  landed,
}: {
  keyName: ProductKey;
  idx: number;
  lang: "zh" | "en";
  breakLabel: string;
  cat: CategoryKey;
  float: boolean;
  /** 开场页冲越交接：true 时图标按序「落位」（缩放弹入 + 产品主色辉光闪现） */
  landed: boolean;
}) {
  const accent = CATEGORY_UI[cat];
  const p = BRAND.products[keyName];
  const landing = PRODUCT_LANDING[keyName];
  const href = landing ? localePath(lang, landing) : PRODUCT_ANCHOR[keyName];
  const optical = PRODUCT_OPTICAL_SCALE[keyName] ?? 1;

  return (
    <a
      href={href}
      onClick={() => track("product_click", { key: keyName, where: "brand_showcase" })}
      className="group flex w-[7.25rem] flex-col items-center text-center sm:w-32"
    >
      <span className="relative grid h-24 w-24 place-items-center md:h-28 md:w-28">
        <span
          className={`pointer-events-none absolute inset-2 rounded-[28%] bg-gradient-to-br from-white/[0.06] to-white/[0.01] ring-1 ring-white/10 transition duration-500 ${accent.ring}`}
        />
        <span
          className={`pointer-events-none absolute inset-0 rounded-full opacity-0 blur-xl transition duration-500 group-hover:opacity-100 ${accent.glow}`}
        />
        <span
          className={`relative inline-grid place-items-center${landed ? " bl-land" : ""}`}
          style={
            {
              ...(optical !== 1 ? { transform: `scale(${optical})` } : {}),
              ...(landed
                ? { "--land-delay": `${PRODUCT_ORDER.indexOf(keyName) * 95}ms`, "--land-glow": PRODUCT_GLOW[keyName] }
                : {}),
            } as CSSProperties
          }
        >
          <Image
            src={PRODUCT_IMG[keyName]}
            alt={`${p.zh} ${p.en}`}
            width={96}
            height={96}
            className={`relative h-16 w-16 object-contain transition-transform duration-500 group-hover:scale-110 md:h-20 md:w-20 ${float ? "animate-float" : ""}`}
            style={float ? { animationDelay: `${idx * 0.55}s` } : undefined}
            draggable={false}
          />
        </span>
      </span>

      <span className="mt-4 block text-base font-bold text-white transition-colors group-hover:text-neon-cyan md:text-lg">
        {p.zh}
      </span>
      <span className="mt-0.5 block text-xs font-semibold uppercase tracking-wider text-neon-cyan/80">
        {p.en}
      </span>
      <span className="mt-1 block text-[11px] leading-snug text-slate-500">{p.scene[lang]}</span>

      <span
        className={`mt-2.5 inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${accent.chip}`}
      >
        <span className="shrink-0">{breakLabel}</span>
        <span className="truncate">· {p.break[lang]}</span>
      </span>
    </a>
  );
}

export default function BrandShowcase() {
  const { lang } = useLang();
  // 水合安全版：float 切换 animate-float 类名，SSR/首帧必须一致
  const reduced = useReducedMotionSafe();
  const c = COPY[lang];
  const pitch = FAMILY_PITCH[lang];
  const float = !reduced;

  /* 开场页冲越交接的「落位」终点：开场里喷涌的七款 LOGO，在冲越瞬间（bl-intro-entered）
   * 依次弹入品牌全家福的对应图标位——把开场动效的叙事收进正文，形成完整闭环。
   * 仅本会话确实展示了开场页时才武装监听；reduced-motion 下 CSS 侧动画为 none。 */
  const [landed, setLanded] = useState(false);
  useEffect(() => {
    let introPending = false;
    try {
      introPending = !sessionStorage.getItem("bl-intro-seen");
    } catch {}
    if (!introPending) return;
    const onEnter = () => setLanded(true);
    window.addEventListener("bl-intro-entered", onEnter, { once: true });
    return () => window.removeEventListener("bl-intro-entered", onEnter);
  }, []);

  return (
    <section id="family" className="relative overflow-hidden py-24">
      <div className="pointer-events-none absolute inset-0 -z-10">
        <div className="absolute left-1/2 top-10 h-72 w-72 -translate-x-1/2 rounded-full bg-neon-cyan/10 blur-[120px]" />
        <div className="absolute left-1/2 top-1/2 h-[38rem] w-[38rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-neon-violet/[0.06] blur-[150px]" />
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/10 to-transparent" />
      </div>

      <div className="mx-auto max-w-6xl px-5">
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
              className={`${float ? "animate-float " : ""}relative h-28 w-28 object-contain drop-shadow-[0_0_30px_rgba(34,211,238,0.35)] md:h-36 md:w-36`}
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

          <h3 className="mt-9 text-2xl font-bold text-white md:text-3xl">{pitch.headline}</h3>
          <p className="mx-auto mt-3 max-w-2xl text-sm leading-relaxed text-slate-400 md:text-base">{pitch.sub}</p>

          <span className="mt-8 inline-flex items-center gap-2 rounded-full border border-neon-cyan/30 bg-gradient-to-r from-neon-cyan/10 to-neon-violet/10 px-5 py-2 backdrop-blur-sm">
            <span className="grid h-5 w-5 place-items-center rounded-full bg-neon-cyan/20 text-[11px] text-neon-cyan">∞</span>
            <span className="text-sm font-semibold text-white">{c.coreLabel}</span>
            <span className="text-[10px] font-medium uppercase tracking-[0.2em] text-neon-cyan/80">{c.coreSub}</span>
          </span>
          <p className="mt-3 text-[11px] tracking-wide text-slate-500">{c.familyHint}</p>
        </Reveal>

        <div className="mx-auto mt-8 h-8 w-px bg-gradient-to-b from-neon-cyan/40 to-transparent" aria-hidden />

        {/* 三系产品簇：桌面 2|3|2 并排，移动端按系纵向堆叠 */}
        <div className="mt-2 grid grid-cols-1 gap-10 md:grid-cols-3 md:gap-0">
          {CATEGORY_ORDER.map((cat, catIdx) => {
            const cc = CATEGORIES[cat];
            const accent = CATEGORY_UI[cat];
            const items = productsInCategory(cat);
            return (
              <div
                key={cat}
                className={`flex flex-col items-center px-2 md:px-4 ${
                  catIdx > 0 ? "md:border-l md:border-white/10" : ""
                }`}
              >
                <Reveal delay={0.08 + catIdx * 0.06} className="w-full">
                  <div className="mb-5 flex flex-col items-center text-center">
                    <span className={`text-xs font-semibold uppercase tracking-[0.28em] ${accent.label}`}>
                      {lang === "zh" ? cc.zh : cc.en}
                      <span className="ml-2 font-medium text-slate-500">
                        {lang === "zh" ? cc.en : cc.zh}
                      </span>
                    </span>
                    <span className="mt-1 text-[11px] text-slate-500">
                      {c.breakLabel} · {lang === "zh" ? cc.breakZh : cc.breakEn}
                    </span>
                  </div>

                  <div className="flex flex-wrap items-start justify-center gap-x-3 gap-y-8 sm:gap-x-5">
                    {items.map((key) => (
                      <ProductTile
                        key={key}
                        keyName={key}
                        idx={PRODUCT_ORDER.indexOf(key)}
                        lang={lang}
                        breakLabel={c.breakLabel}
                        cat={cat}
                        float={float}
                        landed={landed}
                      />
                    ))}
                  </div>
                </Reveal>

                {catIdx < CATEGORY_ORDER.length - 1 && (
                  <div
                    className="mx-auto mt-10 h-px w-20 bg-gradient-to-r from-transparent via-white/15 to-transparent md:hidden"
                    aria-hidden
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
