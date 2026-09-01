"use client";

import { useEffect, useState } from "react";
import { MessagesSquare, Headphones, ArrowRight, Check } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { BRAND, type ProductKey } from "@/lib/brand";
import ProductIcon from "./ProductIcon";
import { track } from "@/lib/track";
import { TranslateDemoPanel } from "./TranslateDemo";
import GlossaryLockDemo from "./GlossaryLockDemo";

type Track = "chat" | "interpret";

// 2026-08-04 通译并入智聊：本页从「通译/通传双产品」改为「通传主轨 + 聊天翻译（智聊内置）指路」。
// 旧深链 #chat/#lingox/#tongyi 仍然可达（readHash 兼容），落到聊天翻译能力段。
const COPY = {
  zh: {
    kicker: "通达系 · 语言之界",
    head: "同一语言之界，两种场景解法",
    sub: "会议 / 直播同声传译，交给通传 VoxX；跨境聊天互译，已内置在智聊 ChatX 里——一个客户端，翻译与成交一起搞定。",
    chatTitle: "聊天翻译",
    chatTag: "已内置于智聊 ChatX",
    chatDesc: "多平台文字 + 语音双向实时翻译、术语锁定、翻译记忆——随智聊客户端开箱即用，不再需要单独安装。",
    chatCta: "看聊天翻译能力",
    interpretCta: "看同传样片",
    chatPoints: ["多平台文字 + 语音双向互译", "术语表锁定专有名词", "统一收件箱沉淀客户资产"],
    interpretPoints: ["克隆音双向同传", "OBS 实时双语字幕", "抢话打断 · SRT 导出"],
    mergeNote: "📌 通译 LingoX 已与智聊 ChatX 合并为同一个程序：无独立通译安装包，下载智聊即可；通译授权在智聊会员中心激活，原授权继续有效。",
    mergeCta: "下载智聊 ChatX →",
    chatTrackKicker: "智聊 ChatX · 内置聊天翻译",
    chatTrackDesc: "多平台文字 + 语音双向实时翻译，术语表锁定专有名词、翻译记忆省成本，统一收件箱沉淀客户资产——不会外语的团队也能即时跟全球客户对话。",
  },
  en: {
    kicker: "Lingo family · the language barrier",
    head: "One language barrier, two scene-fit answers",
    sub: "Meeting / live-stream interpreting is VoxX; cross-border chat translation ships inside ChatX — one client for translation and closing.",
    chatTitle: "Chat translation",
    chatTag: "Built into ChatX",
    chatDesc: "Two-way real-time text + voice translation across platforms with glossary lock and translation memory — ready out of the box inside the ChatX client, no separate install.",
    chatCta: "Chat translation",
    interpretCta: "Hear interpreting",
    chatPoints: ["Text + voice, both directions", "Glossary-locked terms", "Unified inbox & customer assets"],
    interpretPoints: ["Cloned-voice two-way interpret", "OBS live bilingual subs", "Barge-in · SRT export"],
    mergeNote: "📌 LingoX is merged into ChatX — one single program: no separate LingoX installer; download ChatX. LingoX licenses activate in ChatX membership; existing licenses remain valid.",
    mergeCta: "Download ChatX →",
    chatTrackKicker: "ChatX · built-in chat translation",
    chatTrackDesc: "Two-way real-time text + voice translation across platforms, glossary-locked terms, cost-saving translation memory and a unified inbox — teams close global deals without speaking the language.",
  },
} as const;

function readHash(): Track {
  if (typeof window === "undefined") return "interpret";
  const h = window.location.hash.replace("#", "").toLowerCase();
  if (h === "chat" || h === "lingox" || h === "tongyi") return "chat";
  if (h === "interpret" || h === "voxx" || h === "tongchuan" || h === "demo") return "interpret";
  return "interpret";
}

export default function LingoDualPath() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [active, setActive] = useState<Track>("interpret");

  useEffect(() => {
    const apply = (scroll: boolean) => {
      const which = readHash();
      setActive(which);
      if (!scroll) return;
      // 首屏带 hash 进入时，等布局稳定再滚（避免双路径尚未渲染完）
      requestAnimationFrame(() => {
        const id = which === "chat" ? "chat" : "demo";
        document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    };
    apply(Boolean(window.location.hash));
    const onHash = () => apply(true);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = (which: Track) => {
    setActive(which);
    const hash = which === "chat" ? "chat" : "interpret";
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", `#${hash}`);
    }
    const el = document.getElementById(which === "chat" ? "chat" : "demo");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    track("product_click", { key: which === "chat" ? "chatx_translate" : "voxx", where: "lingo_dual" });
  };

  const voxx = BRAND.products.voxx;
  const cards: {
    id: Track;
    iconKey: ProductKey;
    icon: typeof MessagesSquare;
    title: string;
    subtitle: string;
    tag: string;
    desc: string;
    points: readonly string[];
    cta: string;
  }[] = [
    {
      id: "interpret",
      iconKey: "voxx",
      icon: Headphones,
      // 实施78 P0-2：英文路由标题用拉丁名（原恒为 voxx.zh，英文页出现「通传 VoxX」）；
      // 英文下副标题与主标题会重复，故置空由卡片自行省略。
      title: lang === "zh" ? voxx.zh : voxx.en,
      subtitle: lang === "zh" ? voxx.en : "",
      tag: voxx.scene[lang],
      desc: voxx.desc[lang],
      points: c.interpretPoints,
      cta: c.interpretCta,
    },
    {
      id: "chat",
      iconKey: "chatx",
      icon: MessagesSquare,
      title: c.chatTitle,
      subtitle: "ChatX",
      tag: c.chatTag,
      desc: c.chatDesc,
      points: c.chatPoints,
      cta: c.chatCta,
    },
  ];

  return (
    <section className="mx-auto mt-12 max-w-4xl scroll-mt-24" id="paths">
      <Reveal className="text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.28em] text-amber-300">{c.kicker}</p>
        <h2 className="mt-2 text-2xl font-bold text-white md:text-3xl">{c.head}</h2>
        <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{c.sub}</p>
      </Reveal>

      <div className="mt-7 grid gap-4 md:grid-cols-2">
        {cards.map((card, i) => {
          const Icon = card.icon;
          const on = active === card.id;
          return (
            <Reveal key={card.id} delay={i * 0.06}>
              <button
                type="button"
                onClick={() => select(card.id)}
                className={`group flex h-full w-full flex-col rounded-2xl border p-5 text-left transition ${
                  on
                    ? "border-amber-400/50 bg-amber-400/[0.07] shadow-[0_0_28px_rgba(251,191,36,0.12)]"
                    : "border-white/10 bg-ink-900/40 hover:border-amber-400/30 hover:bg-white/[0.03]"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <ProductIcon
                      product={card.iconKey}
                      size={48}
                      alt={`${card.title} ${card.subtitle}`}
                      className="h-12 w-12 object-contain"
                    />
                    <div>
                      <div className="flex items-baseline gap-2">
                        <span className="text-lg font-bold text-white">{card.title}</span>
                        <span className="text-sm font-semibold text-amber-300">{card.subtitle}</span>
                      </div>
                      <p className="mt-0.5 text-xs text-slate-500">{card.tag}</p>
                    </div>
                  </div>
                  <span
                    className={`grid h-9 w-9 place-items-center rounded-xl ${
                      on ? "bg-amber-400/20 text-amber-300" : "bg-white/5 text-slate-400"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                </div>

                <p className="mt-3 text-sm leading-relaxed text-slate-300">{card.desc}</p>

                <ul className="mt-4 space-y-1.5">
                  {card.points.map((pt) => (
                    <li key={pt} className="flex items-start gap-1.5 text-xs text-slate-400">
                      <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                      {pt}
                    </li>
                  ))}
                </ul>

                <span className="mt-5 inline-flex items-center gap-1.5 text-sm font-medium text-amber-300">
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

/** 聊天翻译能力段（原通译，2026-08-04 起为智聊内置能力）——静态对话样片 + 可交互翻译面板，与同传音频样片对等。 */
export function LingoChatTrack() {
  const { lang } = useLang();
  const c = COPY[lang];
  const items =
    lang === "zh"
      ? [
          { t: "多平台双向翻译", d: "WhatsApp / Telegram / LINE 等文字 + 语音实时互译，团队不用会外语也能跟全球客户对话。" },
          { t: "术语锁定 · 翻译记忆", d: "专有名词进术语表后不再翻错；TM 缓存越用越快，降低长期成本。" },
          { t: "统一收件箱", d: "跨平台会话沉淀为客户资产，跟进状态与旅程可追踪。" },
        ]
      : [
          { t: "Omni-channel two-way translate", d: "Real-time text + voice on WhatsApp / Telegram / LINE — teams close deals without speaking the language." },
          { t: "Glossary + translation memory", d: "Proper nouns stay locked; TM cache gets faster and cheaper over time." },
          { t: "Unified inbox", d: "Cross-platform threads become durable customer assets with trackable journeys." },
        ];

  const bubbles =
    lang === "zh"
      ? [
          { side: "them" as const, langTag: "EN · 客户看到", text: "How much for private deployment?" },
          { side: "you" as const, langTag: "ZH · 你实际输入", text: "私有部署按规模报价，含部署调试。" },
          { side: "them" as const, langTag: "EN · 客户看到", text: "Private deployment is quoted by scale, including setup." },
          { side: "you" as const, langTag: "术语锁定", text: "「BOUNDLESS Engine」永远不会被翻成「无边界引擎」。" },
        ]
      : [
          { side: "them" as const, langTag: "ZH · customer sees", text: "私有部署怎么收费？" },
          { side: "you" as const, langTag: "EN · you typed", text: "Quoted by scale, including setup and tuning." },
          { side: "them" as const, langTag: "ZH · customer sees", text: "按规模报价，含部署调试。" },
          { side: "you" as const, langTag: "Glossary lock", text: "「BOUNDLESS Engine」 never becomes a wrong literal translation." },
        ];

  return (
    <section id="chat" className="scroll-mt-24 border-y border-white/5 bg-white/[0.015] px-5 py-16">
      <div className="mx-auto max-w-5xl">
        <Reveal className="text-center">
          <p className="text-xs font-semibold uppercase tracking-[0.28em] text-amber-300">{c.chatTrackKicker}</p>
          <h2 className="mt-2 text-2xl font-bold text-white md:text-3xl">
            {lang === "zh" ? "跨境聊天翻译 · 从对话到客户资产" : "Cross-border chat translation → customer assets"}
          </h2>
          <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{c.chatTrackDesc}</p>
          <div className="mx-auto mt-4 max-w-2xl rounded-2xl border border-amber-400/30 bg-amber-400/[0.06] px-4 py-3 text-left text-sm leading-relaxed text-slate-300">
            {c.mergeNote}{" "}
            <a
              href={lang === "zh" ? "/download/chatx" : "/en/download/chatx"}
              className="text-neon-cyan hover:underline"
            >
              {c.mergeCta}
            </a>
          </div>
        </Reveal>

        <div className="mt-9 grid gap-4 sm:grid-cols-3">
          {items.map((it, i) => (
            <Reveal key={it.t} delay={i * 0.05}>
              <div className="h-full rounded-2xl border border-white/10 bg-ink-900/50 p-5">
                <h3 className="font-semibold text-white">{it.t}</h3>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{it.d}</p>
              </div>
            </Reveal>
          ))}
        </div>

        <div className="mt-10 grid items-start gap-6 lg:grid-cols-2">
          <Reveal delay={0.08}>
            <div className="rounded-3xl border border-white/10 bg-ink-900/60 p-5">
              <div className="mb-4 flex items-center justify-between">
                <p className="text-sm font-semibold text-white">
                  {lang === "zh" ? "对话样片 · 你方 ↔ 客户所见" : "Chat sample · you ↔ what they see"}
                </p>
                <span className="rounded-full border border-amber-400/25 bg-amber-400/10 px-2.5 py-0.5 text-[10px] font-medium text-amber-300">
                  {lang === "zh" ? "示意 · 术语锁定" : "Illustrative · glossary"}
                </span>
              </div>
              <div className="space-y-3">
                {bubbles.map((b, i) => (
                  <div
                    key={`${b.langTag}-${i}`}
                    className={`flex ${b.side === "you" ? "justify-end" : "justify-start"}`}
                  >
                    <div
                      className={`max-w-[88%] rounded-2xl px-3.5 py-2.5 ${
                        b.side === "you"
                          ? "rounded-br-md bg-gradient-to-br from-neon-cyan/25 to-neon-violet/20 text-white"
                          : "rounded-bl-md border border-white/10 bg-white/[0.04] text-slate-200"
                      }`}
                    >
                      <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">{b.langTag}</p>
                      <p className="text-sm leading-relaxed">{b.text}</p>
                    </div>
                  </div>
                ))}
              </div>
              <p className="mt-4 text-[11px] leading-relaxed text-slate-500">
                {lang === "zh"
                  ? "客户侧始终看到对方语言；你侧输入母语即可。专有名词由术语表锁定。"
                  : "They always see their language; you type yours. Proper nouns stay glossary-locked."}
              </p>
            </div>
          </Reveal>

          <Reveal delay={0.12}>
            <div className="space-y-6">
              <div>
                <p className="mb-3 text-sm font-semibold text-white">
                  {lang === "zh" ? "亲手试 · 同一套翻译引擎" : "Try it · same translation engine"}
                </p>
                <TranslateDemoPanel where="interpreting_chat" />
              </div>
              <GlossaryLockDemo />
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
