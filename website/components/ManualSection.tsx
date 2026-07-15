"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BookOpen, CheckCircle2, Download, Info, Printer } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { MANUAL_SECTIONS, type ManualBlock } from "@/lib/manualContent";
import { LATEST_VERSION } from "@/lib/releaseNotes";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import { track } from "@/lib/track";

/**
 * AvatarHub 在线产品手册（/manual 与 /en/manual 共用）。
 * 左侧章节目录（滚动联动高亮），右侧正文；「打印 / 导出 PDF」走浏览器打印，
 * 配合 globals.css 的 @media print 输出白底可读的 PDF。
 */
export default function ManualSection() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const sections = MANUAL_SECTIONS[lang];
  const [active, setActive] = useState<string>(sections[0]?.id ?? "");

  // scrollspy：目录高亮跟随阅读位置
  useEffect(() => {
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) setActive(e.target.id);
        }
      },
      { rootMargin: "-20% 0px -70% 0px" }
    );
    for (const s of sections) {
      const el = document.getElementById(s.id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, [sections]);

  /** 触发浏览器打印（配合打印样式即「导出 PDF」） */
  function printManual() {
    track("manual_print");
    window.print();
  }

  return (
    <section className="manual-root relative pb-24 pt-32">
      <div className="pointer-events-none absolute right-1/4 top-24 h-80 w-80 rounded-full bg-neon-violet/15 blur-[130px]" />

      <div className="relative mx-auto max-w-6xl px-5">
        <Reveal eager>
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/30 bg-neon-violet/10 px-3 py-1 text-xs text-neon-violet">
                <BookOpen className="h-3.5 w-3.5" />
                {zh ? `适用版本 v${LATEST_VERSION} · 持续更新` : `For v${LATEST_VERSION} · continuously updated`}
              </span>
              <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
                {zh ? "AvatarHub 使用手册" : "AvatarHub User Manual"}
              </h1>
              <p className="mt-3 max-w-2xl text-slate-400">
                {zh
                  ? "从装机到直播出镜的完整指南：系统要求、安装激活、四大场景上手、更新与故障排查。"
                  : "The complete guide from install to going live: requirements, activation, the four core workflows, updates and troubleshooting."}
              </p>
            </div>
            <div className="no-print flex shrink-0 flex-wrap items-center gap-3">
              <button
                onClick={printManual}
                className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                <Printer className="h-4 w-4" />
                {zh ? "打印 / 导出 PDF" : "Print / export PDF"}
              </button>
              <Link
                href={zh ? "/download" : "/en/download"}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-5 py-2.5 text-sm text-slate-300 transition hover:border-neon-cyan/40 hover:text-white"
              >
                <Download className="h-4 w-4" />
                {zh ? "去下载客户端" : "Download the client"}
              </Link>
            </div>
          </div>
        </Reveal>

        <div className="mt-12 gap-10 lg:grid lg:grid-cols-[220px_1fr]">
          {/* 目录 */}
          <aside className="no-print mb-8 lg:mb-0">
            <nav className="glass rounded-2xl border border-white/10 p-4 lg:sticky lg:top-24">
              <div className="px-2 pb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
                {zh ? "目录" : "Contents"}
              </div>
              <ul className="space-y-0.5">
                {sections.map((s, i) => (
                  <li key={s.id}>
                    <a
                      href={`#${s.id}`}
                      className={`block rounded-lg px-2 py-1.5 text-sm transition ${
                        active === s.id
                          ? "bg-neon-cyan/10 text-neon-cyan"
                          : "text-slate-400 hover:bg-white/5 hover:text-white"
                      }`}
                    >
                      <span className="mr-1.5 font-mono text-[11px] text-slate-600">{String(i + 1).padStart(2, "0")}</span>
                      {s.title}
                    </a>
                  </li>
                ))}
              </ul>
            </nav>
          </aside>

          {/* 正文 */}
          <div className="min-w-0 space-y-10">
            {sections.map((s, i) => (
              <section key={s.id} id={s.id} className="scroll-mt-28">
                <Reveal>
                  <div className="glass rounded-2xl border border-white/10 p-6 md:p-8">
                    <h2 className="flex items-center gap-3 text-xl font-semibold text-white md:text-2xl">
                      <span className="font-mono text-sm text-neon-cyan">{String(i + 1).padStart(2, "0")}</span>
                      {s.title}
                    </h2>
                    <div className="mt-5 space-y-5">
                      {s.blocks.map((b, j) => (
                        <Block key={j} block={b} />
                      ))}
                    </div>
                  </div>
                </Reveal>
              </section>
            ))}

            <Reveal className="no-print">
              <p className="text-center text-xs text-slate-500">
                {zh ? (
                  <>手册没覆盖到？问右下角 AI 客服，或联系 <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">{TELEGRAM_DISPLAY}</a>。</>
                ) : (
                  <>Not covered here? Ask the AI assistant in the corner, or contact <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">{TELEGRAM_DISPLAY}</a>.</>
                )}
              </p>
            </Reveal>
          </div>
        </div>
      </div>
    </section>
  );
}

/** 渲染单个手册内容块（段落 / 要点 / 步骤 / 表格 / 提示） */
function Block({ block }: { block: ManualBlock }) {
  switch (block.type) {
    case "p":
      return <p className="text-sm leading-relaxed text-slate-300">{block.text}</p>;
    case "bullets":
      return (
        <ul className="space-y-2">
          {block.items.map((it, i) => (
            <li key={i} className="flex items-start gap-2.5 text-sm leading-relaxed text-slate-300">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
              {it}
            </li>
          ))}
        </ul>
      );
    case "steps":
      return (
        <ol className="space-y-3">
          {block.items.map((it, i) => (
            <li key={i} className="flex items-start gap-3">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet text-xs font-bold text-ink-950">
                {i + 1}
              </span>
              <div className="text-sm leading-relaxed">
                <span className="font-medium text-white">{it.title}</span>
                {it.detail && <p className="mt-0.5 text-slate-400">{it.detail}</p>}
              </div>
            </li>
          ))}
        </ol>
      );
    case "table":
      return (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full min-w-[420px] text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 bg-white/[0.03]">
                {block.headers.map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium text-slate-200">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i} className="border-b border-white/5 last:border-0">
                  {row.map((cell, j) => (
                    <td key={j} className={`px-4 py-2.5 ${j === 0 ? "text-slate-200" : "text-slate-400"}`}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "tip":
      return (
        <div className="flex items-start gap-2.5 rounded-xl border border-neon-cyan/25 bg-neon-cyan/[0.06] px-4 py-3 text-sm leading-relaxed text-slate-300">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
          {block.text}
        </div>
      );
  }
}
