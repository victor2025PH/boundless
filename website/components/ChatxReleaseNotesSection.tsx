"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown, Download, History, RefreshCw, ShieldCheck } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import type { BrandLang } from "@/lib/brand";
import {
  CHATX_RELEASE_NOTES,
  CHATX_LATEST_VERSION,
  type ChatxReleaseTag,
} from "@/lib/chatxReleaseNotes";

/** 版本标签徽章的配色与双语文案（与幻境 STUDIO 版本更新版块保持一致视觉语言） */
const TAG_STYLE: Record<ChatxReleaseTag, string> = {
  feature: "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan",
  improve: "border-neon-violet/40 bg-neon-violet/15 text-violet-300",
  fix: "border-emerald-400/40 bg-emerald-400/10 text-emerald-300",
  security: "border-amber-400/40 bg-amber-400/10 text-amber-300",
};
const TAG_LABEL: Record<ChatxReleaseTag, { zh: string; en: string }> = {
  feature: { zh: "新功能", en: "New" },
  improve: { zh: "优化", en: "Improved" },
  fix: { zh: "修复", en: "Fixed" },
  security: { zh: "安全", en: "Security" },
};

/**
 * 智聊 ChatX 版本更新记录发布页主体（独立成页，与下载/安装教程分离）。
 * lang 由路由显式传入（/download/chatx/releases = zh，/en/... = en）。
 * 时间线骨架风格与幻境 STUDIO 的 #changelog 版块一致，数据各自单源维护。
 */
export default function ChatxReleaseNotesSection({ lang: forced }: { lang?: BrandLang }) {
  const ctx = useLang();
  const lang: BrandLang = forced ?? ctx.lang;
  const zh = lang === "zh";
  const [showAll, setShowAll] = useState(false);

  const releases = showAll ? CHATX_RELEASE_NOTES : CHATX_RELEASE_NOTES.slice(0, 3);
  const downloadHref = zh ? "/download/chatx" : "/en/download/chatx";
  const pricingHref = zh ? "/order" : "/en/order";

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-neon-cyan/15 blur-[130px]" />

      <div className="relative mx-auto max-w-4xl px-5">
        {/* 头部 */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <History className="h-3.5 w-3.5" />
            {zh ? `当前最新 v${CHATX_LATEST_VERSION} · 内置一键升级` : `Latest v${CHATX_LATEST_VERSION} · one-click in-app update`}
          </span>
          <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
            {zh ? "智聊 ChatX 版本更新记录" : "ChatX Release Notes"}
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "每一版更新了什么，一次看清。客户端内置自动更新：发现新版本点一下即完成下载安装，账号与聊天数据全部保留。"
              : "See what changed in every release. The client auto-updates: one click downloads and installs the new version — accounts and chat data are fully preserved."}
          </p>
        </Reveal>

        {/* 顶部操作条 */}
        <Reveal className="mt-8">
          <div className="glass flex flex-col items-start gap-4 rounded-2xl border border-white/10 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <RefreshCw className="mt-0.5 h-5 w-5 shrink-0 text-neon-cyan" />
              <div>
                <div className="text-sm font-medium text-white">
                  {zh ? "已经安装了？无需操作" : "Already installed? Nothing to do"}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">
                  {zh
                    ? "客户端会自动检查并在后台下载新版本，退出时安装。"
                    : "The client checks and downloads new versions in the background, installing on exit."}
                </p>
              </div>
            </div>
            <Link
              href={downloadHref}
              onClick={() => track("cta_click", { where: "chatx_releases_download" })}
              className="inline-flex shrink-0 items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
            >
              <Download className="h-4 w-4" />
              {zh ? "前往下载 / 更新" : "Download / update"}
            </Link>
          </div>
        </Reveal>

        {/* 版本时间线 */}
        <Reveal className="mt-10">
          <div className="relative space-y-6 before:absolute before:bottom-2 before:left-[15px] before:top-2 before:w-px before:bg-white/10 md:before:left-[19px]">
            {releases.map((r, i) => (
              <div key={r.version} className="relative pl-11 md:pl-14">
                <span
                  className={`absolute left-0 top-1 flex h-8 w-8 items-center justify-center rounded-full border md:h-10 md:w-10 ${
                    i === 0
                      ? "border-neon-cyan/50 bg-neon-cyan/15 text-neon-cyan"
                      : "border-white/15 bg-ink-900 text-slate-400"
                  }`}
                >
                  <History className="h-4 w-4" />
                </span>
                <div className="glass rounded-2xl border border-white/10 p-5 md:p-6">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <span className="font-mono text-base font-semibold text-white">v{r.version}</span>
                    {i === 0 && (
                      <span className="rounded-full border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-0.5 text-[11px] text-neon-cyan">
                        {zh ? "最新版本" : "Latest"}
                      </span>
                    )}
                    {r.tags.map((tag) => (
                      <span key={tag} className={`rounded-full border px-2 py-0.5 text-[11px] ${TAG_STYLE[tag]}`}>
                        {TAG_LABEL[tag][lang]}
                      </span>
                    ))}
                    <span className="ml-auto text-xs text-slate-500">{r.date}</span>
                  </div>
                  <div className="mt-2 text-sm font-medium text-slate-200">{r.title[lang]}</div>
                  <ul className="mt-3 space-y-1.5">
                    {r.highlights[lang].map((h, j) => (
                      <li key={j} className="flex items-start gap-2 text-sm leading-relaxed text-slate-400">
                        <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-neon-cyan/70" />
                        {h}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>

          {CHATX_RELEASE_NOTES.length > 3 && (
            <div className="mt-6 text-center">
              <button
                onClick={() => {
                  setShowAll((v) => !v);
                  if (!showAll) track("chatx_changelog_expand");
                }}
                className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-5 py-2 text-sm text-slate-300 transition hover:border-neon-cyan/40 hover:text-white"
              >
                <ChevronDown className={`h-4 w-4 transition-transform ${showAll ? "rotate-180" : ""}`} />
                {showAll
                  ? zh ? "收起历史版本" : "Collapse history"
                  : zh ? `查看全部 ${CHATX_RELEASE_NOTES.length} 个版本` : `Show all ${CHATX_RELEASE_NOTES.length} releases`}
              </button>
            </div>
          )}
        </Reveal>

        {/* 底部：安全说明 + 定价 CTA */}
        <Reveal className="mt-12">
          <div className="glass flex flex-col items-start gap-4 rounded-2xl border border-neon-violet/25 bg-neon-violet/[0.06] p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-neon-violet" />
              <div>
                <div className="text-sm font-medium text-white">
                  {zh ? "升级安全、可回滚" : "Safe, rollback-able updates"}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">
                  {zh
                    ? "更新走签名校验，异常自动回退上一版本；数据全部保存在本机。"
                    : "Updates are signature-verified and auto-revert on failure; all data stays on your machine."}
                </p>
              </div>
            </div>
            <Link
              href={pricingHref}
              onClick={() => track("cta_click", { where: "chatx_releases_pricing" })}
              className="text-sm text-neon-cyan transition hover:underline"
            >
              {zh ? "查看套餐与价格 →" : "See plans & pricing →"}
            </Link>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
