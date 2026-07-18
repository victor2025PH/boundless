"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import { Images, MonitorPlay, ArrowRight, Check } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { BRAND } from "@/lib/brand";
import { PRODUCT_IMG } from "./productMeta";
import { track } from "@/lib/track";

type Track = "swap" | "live";

const COPY = {
  zh: {
    kicker: "幻境系 · 两条分身产品",
    head: "同一副面孔，两种用法",
    sub: "幻颜做图片 / 视频换脸出片，幻影做直播实时换脸与活体数字人开播——别把出片和开播混成一件事。",
    swapCta: "看换脸样片",
    liveCta: "看直播分身",
    swapPoints: ["图片 / 视频成片级换脸", "GFPGAN / CodeFormer 精修", "三路并发批量出片"],
    livePoints: ["直播实时换脸 25fps", "活体数字人 · 会眨眼摆头", "虚拟摄像头 / OBS 即插即用"],
  },
  en: {
    kicker: "Studio family · two twin products",
    head: "One face, two ways to use it",
    sub: "FaceX produces swapped images & videos; LiveX runs real-time live swap and living digital humans — production and streaming are different jobs.",
    swapCta: "See swap samples",
    liveCta: "See the live twin",
    swapPoints: ["Production-grade image / video swap", "GFPGAN / CodeFormer refinement", "Batched 3-way output"],
    livePoints: ["Real-time live swap at 25fps", "Living digital human — blinks & moves", "Virtual camera / OBS plug-and-play"],
  },
} as const;

function readHash(): Track {
  if (typeof window === "undefined") return "live";
  const h = window.location.hash.replace("#", "").toLowerCase();
  if (h === "swap" || h === "facex" || h === "huanyan") return "swap";
  return "live";
}

export default function StudioDualPath() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [active, setActive] = useState<Track>("live");

  useEffect(() => {
    const apply = (scroll: boolean) => {
      const which = readHash();
      setActive(which);
      if (!scroll) return;
      requestAnimationFrame(() => {
        document.getElementById(which)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    };
    apply(Boolean(window.location.hash));
    const onHash = () => apply(true);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = (which: Track) => {
    setActive(which);
    window.history.replaceState(null, "", `#${which}`);
    document.getElementById(which)?.scrollIntoView({ behavior: "smooth", block: "start" });
    track("product_click", { key: which === "swap" ? "facex" : "livex", where: "studio_dual" });
  };

  const cards: { id: Track; key: "facex" | "livex"; icon: typeof Images; points: readonly string[]; cta: string }[] = [
    { id: "swap", key: "facex", icon: Images, points: c.swapPoints, cta: c.swapCta },
    { id: "live", key: "livex", icon: MonitorPlay, points: c.livePoints, cta: c.liveCta },
  ];

  return (
    <section className="mx-auto mt-12 max-w-4xl scroll-mt-24" id="paths">
      <Reveal className="text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.28em] text-neon-violet">{c.kicker}</p>
        <h2 className="mt-2 text-2xl font-bold text-white md:text-3xl">{c.head}</h2>
        <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{c.sub}</p>
      </Reveal>

      <div className="mt-7 grid gap-4 md:grid-cols-2">
        {cards.map((card, i) => {
          const p = BRAND.products[card.key];
          const Icon = card.icon;
          const on = active === card.id;
          return (
            <Reveal key={card.id} delay={i * 0.06}>
              <button
                type="button"
                onClick={() => select(card.id)}
                className={`group flex h-full w-full flex-col rounded-2xl border p-5 text-left transition ${
                  on
                    ? "border-neon-violet/50 bg-neon-violet/[0.07] shadow-[0_0_28px_rgba(139,92,246,0.14)]"
                    : "border-white/10 bg-ink-900/40 hover:border-neon-violet/30 hover:bg-white/[0.03]"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <Image
                      src={PRODUCT_IMG[card.key]}
                      alt={`${p.zh} ${p.en}`}
                      width={48}
                      height={48}
                      className="h-12 w-12 object-contain"
                      draggable={false}
                    />
                    <div>
                      <div className="flex items-baseline gap-2">
                        <span className="text-lg font-bold text-white">{p.zh}</span>
                        <span className="text-sm font-semibold text-neon-violet">{p.en}</span>
                      </div>
                      <p className="mt-0.5 text-xs text-slate-500">{p.scene[lang]}</p>
                    </div>
                  </div>
                  <span
                    className={`grid h-9 w-9 place-items-center rounded-xl ${
                      on ? "bg-neon-violet/20 text-neon-violet" : "bg-white/5 text-slate-400"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                </div>

                <p className="mt-3 text-sm leading-relaxed text-slate-300">{p.desc[lang]}</p>

                <ul className="mt-4 space-y-1.5">
                  {card.points.map((pt) => (
                    <li key={pt} className="flex items-start gap-1.5 text-xs text-slate-400">
                      <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                      {pt}
                    </li>
                  ))}
                </ul>

                <span className="mt-5 inline-flex items-center gap-1.5 text-sm font-medium text-neon-violet">
                  {card.cta}
                  <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                </span>
              </button>
            </Reveal>
          );
        })}
      </div>
    </section>
  );
}
