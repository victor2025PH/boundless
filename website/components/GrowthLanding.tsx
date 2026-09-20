"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, Check, Target, MessagesSquare, Send, Home, Languages } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BrandMark from "./BrandMark";
import Footer from "./Footer";
import AutoChatDemo from "./AutoChatDemo";
import LandingFamilyNav, { type LandingNavFocus } from "./LandingFamilyNav";
import { BRAND } from "@/lib/brand";
import ProductIcon from "./ProductIcon";
import { GROWTH_FAQ } from "@/lib/growthContent";
import { CONTACT_URL, localePath } from "@/lib/site";
import { track } from "@/lib/track";

type TrackId = "reach" | "chat";

const COPY = {
  zh: {
    badge: "智连系 · 获客到成交",
    title: "先触达，再成交",
    accent: "智聊 + 智拓",
    sub: "智聊用 AI 承接多平台会话，自动跟进、推进成交；智拓提供真机集群获客方案，邀请制评估、私有化交付。两条产品可单选，也可串成从触达到成交的闭环。",
    points: ["AI 多平台聊天成交", "真机集群获客 · 邀请制评估", "私有部署 · 数据不出网"],
    reachCta: "看获客方案",
    chatCta: "看成交能力",
    book: "预约方案咨询",
    reachBook: "预约邀请制评估",
    home: "返回首页",
    reachHead: "智拓 ReachX · 真机集群获客 · 邀请制",
    chatHead: "智聊 ChatX · AI 成交",
    reachItems: [
      { t: "真机集群 · 多号管理", d: "主控 + Worker 真机集群统一管理多账号，覆盖 Facebook / Messenger / TikTok / Instagram 等平台，动作与节奏按你的合规边界配置。" },
      { t: "群成员提取 · 打招呼引流", d: "把公开群里的潜客沉淀进私域漏斗，触达节奏按平台规则配置，放量走人工闸门把关。" },
      { t: "邀请制评估 · 私有化交付", d: "不做自助开通：先评估你的场景与合规边界，再私有化部署交付，全程手动闸门控制。" },
    ],
    chatItems: [
      { t: "聚合收件箱", d: "多平台会话统一承接，AI 自动开发客户、推进成交。" },
      { t: "拟人多语种", d: "翻译 + 人设话术，对方不易察觉是 AI。" },
      { t: "人工一键接管", d: "关键节点随时切入真人，成交节奏可控。" },
    ],
  },
  en: {
    badge: "Growth · reach to close",
    title: "Reach first, then close",
    accent: "ChatX + ReachX",
    sub: "ChatX picks up conversations across platforms and closes with AI follow-ups; ReachX is a real-device lead-gen program — invite-only assessment, private delivery. Pick one, or chain them into a reach-to-close loop.",
    points: ["AI omni-channel closing", "Real-device lead-gen · invite-only", "Private deploy · off-net"],
    reachCta: "Lead-gen program",
    chatCta: "Closing capabilities",
    book: "Book a consult",
    reachBook: "Request an invite-only assessment",
    home: "Home",
    reachHead: "ReachX · real-device lead-gen · invite-only",
    chatHead: "ChatX · AI closing",
    reachItems: [
      { t: "Real-device cluster · multi-account", d: "A controller + worker device cluster manages accounts across Facebook / Messenger / TikTok / Instagram, with actions and pacing configured to your compliance boundaries." },
      { t: "Group extract · greet & funnel", d: "Move public-group prospects into your private funnel, paced to platform rules with a manual gate on volume." },
      { t: "Invite-only · private delivery", d: "No self-serve signup: we assess your scenario and compliance boundaries first, then deliver as a private deployment under manual gating." },
    ],
    chatItems: [
      { t: "Unified inbox", d: "Omni-channel threads with AI that develops and closes." },
      { t: "Human-like multilingual", d: "Translation + persona scripts — hard to tell it's AI." },
      { t: "One-tap human takeover", d: "Jump in at key moments; keep control of the close." },
    ],
  },
} as const;

function readHash(): TrackId {
  if (typeof window === "undefined") return "chat";
  const h = window.location.hash.replace("#", "").toLowerCase();
  if (h === "reach" || h === "reachx" || h === "zhituo") return "reach";
  return "chat";
}

export default function GrowthLanding() {
  const { lang, toggle } = useLang();
  const c = COPY[lang];
  const [active, setActive] = useState<TrackId>("chat");
  const home = localePath(lang, "/");
  const navFocus: LandingNavFocus = "growth";

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

  const select = (which: TrackId) => {
    setActive(which);
    window.history.replaceState(null, "", `#${which}`);
    document.getElementById(which)?.scrollIntoView({ behavior: "smooth", block: "start" });
    track("product_click", { key: which === "reach" ? "reachx" : "chatx", where: "growth_dual" });
  };

  // 主推位：智聊（生产运行）在前，智拓（邀请制）为辅
  const cards: { id: TrackId; key: "reachx" | "chatx"; icon: typeof Target; cta: string }[] = [
    { id: "chat", key: "chatx", icon: MessagesSquare, cta: c.chatCta },
    { id: "reach", key: "reachx", icon: Target, cta: c.reachCta },
  ];

  return (
    <main className="relative min-h-screen">
      <header className="fixed inset-x-0 top-0 z-50 glass">
        <nav className="mx-auto flex max-w-6xl items-center justify-between px-5 py-3.5">
          <div className="flex items-center gap-3">
            <Link href={home} className="flex items-center gap-2">
              <BrandMark className="h-8 w-8" />
              {/* 实施78 P0-2：英文路由只出 BOUNDLESS（与 Navbar/Footer/BrandShowcase 同口径） */}
              <span className="hidden text-base font-semibold text-white sm:inline">
                {lang === "zh" ? (
                  <>
                    {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
                  </>
                ) : (
                  BRAND.company.en
                )}
              </span>
            </Link>
            <span className="hidden rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-xs text-slate-300 md:inline">
              {c.badge}
            </span>
          </div>
          <div className="flex items-center gap-2.5">
            <Link
              href={home}
              className="hidden items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300 sm:inline-flex"
            >
              <Home className="h-3.5 w-3.5" />
              {c.home}
            </Link>
            <button
              type="button"
              onClick={toggle}
              className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300"
            >
              <Languages className="h-3.5 w-3.5" />
              {lang === "zh" ? "EN" : "中文"}
            </button>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "growth_nav" })}
              className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-1.5 text-xs font-semibold text-ink-950"
            >
              <Send className="h-3.5 w-3.5" />
              {c.book}
            </a>
          </div>
        </nav>
        <LandingFamilyNav product={navFocus} />
      </header>

      <section className="relative overflow-hidden px-5 pb-12 pt-36 md:pt-40">
        <div className="mx-auto max-w-3xl text-center">
          <Reveal>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3.5 py-1 text-xs font-medium text-neon-cyan">
              {c.badge}
            </span>
          </Reveal>
          <Reveal delay={0.05}>
            <h1 className="mt-5 text-4xl font-bold text-white md:text-5xl">
              {c.title} <span className="text-gradient">{c.accent}</span>
            </h1>
          </Reveal>
          <Reveal delay={0.1}>
            <p className="mx-auto mt-5 max-w-2xl text-base text-slate-400 md:text-lg">{c.sub}</p>
          </Reveal>
          <Reveal delay={0.15}>
            <ul className="mx-auto mt-6 flex max-w-2xl flex-wrap items-center justify-center gap-x-6 gap-y-2">
              {c.points.map((pt) => (
                <li key={pt} className="flex items-center gap-1.5 text-xs text-slate-300">
                  <Check className="h-3.5 w-3.5 text-emerald-400" />
                  {pt}
                </li>
              ))}
            </ul>
          </Reveal>
          <Reveal delay={0.2}>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "growth_hero" })}
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-medium text-ink-950"
              >
                {c.book}
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </a>
              <a
                href="#paths"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200"
              >
                {lang === "zh" ? "先选场景" : "Pick a scene"}
              </a>
            </div>
          </Reveal>
        </div>

        <section id="paths" className="mx-auto mt-12 max-w-4xl scroll-mt-28">
          <div className="grid gap-4 md:grid-cols-2">
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
                        ? "border-neon-cyan/50 bg-neon-cyan/[0.07] shadow-[0_0_28px_rgba(34,211,238,0.12)]"
                        : "border-white/10 bg-ink-900/40 hover:border-neon-cyan/30"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-center gap-3">
                        <ProductIcon
                          product={card.key}
                          size={48}
                          alt={`${p.zh} ${p.en}`}
                          className="h-12 w-12 object-contain"
                        />
                        <div>
                          {/* P0-2：中文页「智聊 ChatX」双写，英文页只出拉丁名 */}
                          <div className="flex items-baseline gap-2">
                            {lang === "zh" && <span className="text-lg font-bold text-white">{p.zh}</span>}
                            <span
                              className={
                                lang === "zh"
                                  ? "text-sm font-semibold text-neon-cyan"
                                  : "text-lg font-bold text-white"
                              }
                            >
                              {p.en}
                            </span>
                          </div>
                          <p className="mt-0.5 text-xs text-slate-500">{p.scene[lang]}</p>
                        </div>
                      </div>
                      <span className={`grid h-9 w-9 place-items-center rounded-xl ${on ? "bg-neon-cyan/20 text-neon-cyan" : "bg-white/5 text-slate-400"}`}>
                        <Icon className="h-4 w-4" />
                      </span>
                    </div>
                    <p className="mt-3 text-sm leading-relaxed text-slate-300">{p.desc[lang]}</p>
                    <span className="mt-5 inline-flex items-center gap-1.5 text-sm font-medium text-neon-cyan">
                      {card.cta}
                      <ArrowRight className="h-3.5 w-3.5" />
                    </span>
                  </button>
                </Reveal>
              );
            })}
          </div>
        </section>
      </section>

      {/* 主推：智聊 ChatX（生产运行）在前 */}
      <section id="chat" className="scroll-mt-28 border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-5xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">{c.chatHead}</h2>
            <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{BRAND.products.chatx.desc[lang]}</p>
          </Reveal>
          <div className="mt-9 grid items-center gap-8 lg:grid-cols-[1fr_auto]">
            <div className="grid gap-4 sm:grid-cols-1">
              {c.chatItems.map((it, i) => (
                <Reveal key={it.t} delay={i * 0.05}>
                  <div className="h-full rounded-2xl border border-white/10 bg-ink-900/50 p-5">
                    <h3 className="font-semibold text-white">{it.t}</h3>
                    <p className="mt-2 text-sm leading-relaxed text-slate-400">{it.d}</p>
                  </div>
                </Reveal>
              ))}
            </div>
            {/* 首页同款收件箱动画演示：真实组件复用，不复制实现 */}
            <Reveal delay={0.1}>
              <AutoChatDemo />
            </Reveal>
          </div>
          <Reveal delay={0.1} className="mt-10 text-center">
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "growth_final" })}
              className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-semibold text-ink-950"
            >
              <Send className="h-4 w-4" />
              {c.book}
            </a>
          </Reveal>
        </div>
      </section>

      {/* 辅：智拓 ReachX（邀请制评估 · 私有化交付） */}
      <section id="reach" className="scroll-mt-28 px-5 py-16">
        <div className="mx-auto max-w-5xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">{c.reachHead}</h2>
            <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{BRAND.products.reachx.desc[lang]}</p>
          </Reveal>
          <div className="mt-9 grid gap-4 sm:grid-cols-3">
            {c.reachItems.map((it, i) => (
              <Reveal key={it.t} delay={i * 0.05}>
                <div className="h-full rounded-2xl border border-white/10 bg-ink-900/50 p-5">
                  <h3 className="font-semibold text-white">{it.t}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{it.d}</p>
                </div>
              </Reveal>
            ))}
          </div>
          <Reveal delay={0.1} className="mt-8 flex flex-col items-center justify-center gap-3 text-center sm:flex-row">
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "growth_reach" })}
              className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-2.5 text-sm font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
            >
              <Send className="h-3.5 w-3.5" />
              {c.reachBook}
            </a>
            <Link href={localePath(lang, "/#autochat")} className="text-sm text-neon-cyan hover:underline">
              {lang === "zh" ? "回首页看获客 / 成交演示 →" : "See lead-gen / closing demos on home →"}
            </Link>
          </Reveal>
        </div>
      </section>

      {/* FAQ（与 page.tsx 的 FAQPage JSON-LD 同源 GROWTH_FAQ） */}
      <section className="border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-3xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "常见问题" : "FAQ"}
            </h2>
          </Reveal>
          <div className="mt-8 space-y-3">
            {GROWTH_FAQ[lang].map((f, i) => (
              <Reveal key={f.q} delay={i * 0.04}>
                <details className="group rounded-2xl border border-white/10 bg-ink-900/50 p-5 open:border-neon-cyan/25">
                  <summary className="cursor-pointer list-none font-medium text-white marker:hidden">
                    {f.q}
                  </summary>
                  <p className="mt-3 text-sm leading-relaxed text-slate-400">{f.a}</p>
                </details>
              </Reveal>
            ))}
          </div>
          <Reveal delay={0.1} className="mt-6 text-center">
            <Link
              href={localePath(lang, "/#faq")}
              className="text-sm text-slate-400 underline-offset-4 transition hover:text-neon-cyan hover:underline"
            >
              {lang === "zh" ? "更多问题 → 首页完整 FAQ" : "More questions → full FAQ on the homepage"}
            </Link>
          </Reveal>
        </div>
      </section>

      <Footer />
    </main>
  );
}
