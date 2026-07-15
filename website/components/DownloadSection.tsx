"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import {
  Apple,
  AudioLines,
  BadgeCheck,
  BookOpen,
  Bot,
  ChevronDown,
  Download,
  HardDrive,
  HelpCircle,
  History,
  Monitor,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import { openAiChat } from "./AIChat";
import Reveal from "./fx/Reveal";
import BeforeAfter from "./fx/BeforeAfter";
import { AudioClip } from "./fx/MediaClips";
import { ENGINE } from "@/lib/engineContent";
import { INSTALL_GUIDE } from "@/lib/manualContent";
import { RELEASE_NOTES, LATEST_VERSION, type ReleaseTag } from "@/lib/releaseNotes";
import { track } from "@/lib/track";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";

interface Build {
  os: string;
  ver: string;
  size: string;
  url: string;
  sha256: string;
  filename: string;
  ready: boolean;
}

// 兜底清单：/releases/release_manifest.json（发布脚本生成）不可达时展示。
const FALLBACK: Build[] = [
  {
    os: "Windows 10/11 (x64)",
    ver: LATEST_VERSION,
    size: "45 MB",
    url: `/releases/AvatarHub-Setup-${LATEST_VERSION}.exe`,
    sha256: "",
    filename: `AvatarHub-Setup-${LATEST_VERSION}.exe`,
    ready: true,
  },
  {
    os: "macOS 12+ (Apple Silicon / Intel)",
    ver: "-",
    size: "",
    url: "",
    sha256: "",
    filename: "",
    ready: false,
  },
];

/** 版本标签徽章的配色与双语文案 */
const TAG_STYLE: Record<ReleaseTag, string> = {
  feature: "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan",
  improve: "border-neon-violet/40 bg-neon-violet/10 text-neon-violet",
  fix: "border-emerald-400/40 bg-emerald-400/10 text-emerald-300",
  security: "border-amber-400/40 bg-amber-400/10 text-amber-300",
};
const TAG_LABEL: Record<ReleaseTag, { zh: string; en: string }> = {
  feature: { zh: "新功能", en: "New" },
  improve: { zh: "优化", en: "Improved" },
  fix: { zh: "修复", en: "Fixed" },
  security: { zh: "安全", en: "Security" },
};

export default function DownloadSection() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [builds, setBuilds] = useState<Build[]>(FALLBACK);
  const [openFaq, setOpenFaq] = useState<number | null>(null);
  const [showAllReleases, setShowAllReleases] = useState(false);

  useEffect(() => {
    fetch("/releases/release_manifest.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j?.builds?.length) setBuilds(j.builds as Build[]);
      })
      .catch(() => {});
  }, []);

  /** 唤起全局 AI 客服并进入安装协助场景 */
  function startInstallAssist(from: string) {
    track("install_assist_click", { from });
    openAiChat("install");
  }

  const releases = showAllReleases ? RELEASE_NOTES : RELEASE_NOTES.slice(0, 3);

  const quickNav = [
    {
      icon: HardDrive,
      title: zh ? "详细安装教程" : "Install tutorial",
      desc: zh ? "六步从下载到出声，本页直达" : "Six steps from download to first sound",
      href: "#install-guide",
    },
    {
      icon: BookOpen,
      title: zh ? "产品使用手册" : "User manual",
      desc: zh ? "在线阅读 · 可打印导出 PDF" : "Read online · printable to PDF",
      href: zh ? "/manual" : "/en/manual",
    },
    {
      icon: History,
      title: zh ? "版本更新记录" : "Release notes",
      desc: zh ? `最新 v${LATEST_VERSION} 更新了什么` : `What's new in v${LATEST_VERSION}`,
      href: "#changelog",
    },
  ];

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-neon-blue/15 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <ShieldCheck className="h-3.5 w-3.5" />
            {zh ? "薄核心安装包 · 组件按需下载 · SHA-256 可校验" : "Thin-core installer · on-demand components · SHA-256 verifiable"}
          </span>
          <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
            {zh ? "下载客户端" : "Download the Client"}
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "AvatarHub 实时数字人引擎：声音克隆、实时换脸、数字人直播、克隆音同传，本地部署数据不出机房。"
              : "AvatarHub real-time digital human engine: voice cloning, live face swap, digital-human streaming and interpreting — deployed locally, data stays on-prem."}
          </p>
        </Reveal>

        <div className="mt-12 grid gap-6 md:grid-cols-2">
          {builds.map((b, i) => {
            const mac = /mac/i.test(b.os);
            return (
              <Reveal key={b.os} delay={i * 0.08}>
                <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
                  <div className="flex items-center gap-3">
                    {mac ? <Apple className="h-8 w-8 text-slate-300" /> : <Monitor className="h-8 w-8 text-neon-cyan" />}
                    <div>
                      <div className="font-semibold text-white">{b.os}</div>
                      <div className="text-xs text-slate-500">
                        {b.ready
                          ? `${zh ? "版本" : "Version"} v${b.ver}${b.size ? ` · ${b.size}` : ""}`
                          : zh
                            ? "即将上线 · 轻量控制台 / 远程接入"
                            : "Coming soon · lightweight console / remote access"}
                      </div>
                    </div>
                  </div>

                  <div className="mt-5 flex-1">
                    {b.ready && b.url ? (
                      <a
                        href={b.url}
                        download
                        onClick={() => track("download_click", { os: b.os, ver: b.ver })}
                        className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
                      >
                        <Download className="h-4 w-4" />
                        {zh ? "下载" : "Download"} {b.filename}
                      </a>
                    ) : b.ready ? (
                      <span className="inline-block rounded-full border border-white/15 px-6 py-2.5 text-sm text-slate-500">
                        {zh ? "下载链接待发布" : "Link pending"}
                      </span>
                    ) : (
                      <a
                        href={CONTACT_URL}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-block rounded-full border border-neon-violet/40 px-6 py-2.5 text-sm text-neon-violet transition hover:bg-neon-violet/10"
                      >
                        {zh ? "上线后通知我" : "Notify me"}
                      </a>
                    )}
                  </div>

                  {b.sha256 && (
                    <div className="mt-4 break-all rounded-lg bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-slate-600">
                      SHA-256: {b.sha256}
                    </div>
                  )}
                  {mac && (
                    <p className="mt-3 text-xs leading-relaxed text-slate-500">
                      {zh
                        ? "Mac 版为轻量控制台 / 远程接入：换脸、数字人等重推理需 Windows / 服务器 N 卡，Mac 端连接远程引擎使用。"
                        : "The Mac build is a lightweight console: heavy inference (face swap, digital human) runs on a Windows / server GPU; the Mac connects remotely."}
                    </p>
                  )}
                </div>
              </Reveal>
            );
          })}
        </div>

        {/* 快速入口：教程 / 手册 / 版本更新 + AI 协助安装 */}
        <Reveal className="mt-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {quickNav.map((q) => (
              <Link
                key={q.title}
                href={q.href}
                className="glass card-hover flex items-start gap-3 rounded-2xl border border-white/10 p-4"
              >
                <q.icon className="mt-0.5 h-5 w-5 shrink-0 text-neon-cyan" />
                <div>
                  <div className="text-sm font-medium text-white">{q.title}</div>
                  <div className="mt-0.5 text-xs text-slate-500">{q.desc}</div>
                </div>
              </Link>
            ))}
            <button
              onClick={() => startInstallAssist("quick_nav")}
              className="card-hover flex items-start gap-3 rounded-2xl border border-neon-violet/30 bg-neon-violet/[0.08] p-4 text-left"
            >
              <Bot className="mt-0.5 h-5 w-5 shrink-0 text-neon-violet" />
              <div>
                <div className="text-sm font-medium text-white">{zh ? "AI 协助安装" : "AI install assistant"}</div>
                <div className="mt-0.5 text-xs text-slate-500">
                  {zh ? "对话式一步步带你装，秒回" : "Chat-guided setup, instant replies"}
                </div>
              </div>
            </button>
          </div>
        </Reveal>

        {/* 真实效果试听/对比：下载前先看引擎实际产出（复用首页实证素材，单一真相） */}
        <Reveal className="mt-12">
          <div className="grid gap-6 md:grid-cols-2">
            <div className="glass flex flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-2 font-semibold text-white">
                <AudioLines className="h-5 w-5 text-neon-cyan" />
                {zh ? "克隆音色 · 真实合成试听" : "Cloned voice · real synthesis samples"}
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {zh ? "同一克隆音色的多语种真实产出，非演员配音。" : "Real multi-language output from one cloned voice — no voice actors."}
              </p>
              <div className="mt-4 space-y-2.5">
                {ENGINE.proof.audioClips.slice(0, 3).map((clip) => (
                  <AudioClip key={clip.src} label={clip.label[lang]} src={clip.src} />
                ))}
              </div>
            </div>
            <div className="glass flex flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-2 font-semibold text-white">
                <BadgeCheck className="h-5 w-5 text-emerald-400" />
                {zh ? "实时换脸 · 前后对比" : "Live face swap · before / after"}
              </div>
              <p className="mt-1 text-xs text-slate-500">{ENGINE.proof.swapDesc[lang]}</p>
              <div className="mt-4">
                <BeforeAfter
                  before={ENGINE.proof.swapBefore}
                  after={ENGINE.proof.swapAfter}
                  beforeLabel={ENGINE.proof.beforeLabel[lang]}
                  afterLabel={ENGINE.proof.afterLabel[lang]}
                  hint={ENGINE.proof.dragHint[lang]}
                  aspectClass="aspect-[11/16]"
                  className="mx-auto max-w-[240px]"
                />
              </div>
            </div>
          </div>
        </Reveal>

        {/* 详细安装教程 */}
        <Reveal className="mt-12">
          <div id="install-guide" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-lg font-semibold text-white">
                <HardDrive className="h-5 w-5 text-neon-cyan" />
                {zh ? "详细安装教程 · 从下载到出声" : "Install tutorial · from download to first sound"}
              </div>
              <button
                onClick={() => startInstallAssist("guide_header")}
                className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/40 px-4 py-1.5 text-xs text-neon-violet transition hover:bg-neon-violet/10"
              >
                <Bot className="h-3.5 w-3.5" />
                {zh ? "AI 协助安装" : "AI install assistant"}
              </button>
            </div>
            <p className="mt-2 text-sm text-slate-500">
              {zh
                ? "全程约 10–30 分钟（视网速），零命令行。更完整的功能上手见使用手册。"
                : "Takes about 10–30 minutes depending on bandwidth, zero command line. See the manual for full feature guides."}
            </p>

            <ol className="mt-6 space-y-5">
              {INSTALL_GUIDE.steps.map((s, i) => (
                <li key={i} className="flex items-start gap-4">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet text-xs font-bold text-ink-950">
                    {i + 1}
                  </span>
                  <div className="min-w-0">
                    <div className="font-medium text-white">{s.title[lang]}</div>
                    <p className="mt-1 text-sm leading-relaxed text-slate-400">{s.detail[lang]}</p>
                    {s.sub && (
                      <ul className="mt-2 space-y-1.5">
                        {s.sub[lang].map((line, j) => (
                          <li key={j} className="flex items-start gap-2 text-xs leading-relaxed text-slate-500">
                            <Sparkles className="mt-0.5 h-3 w-3 shrink-0 text-neon-cyan/70" />
                            {line}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </li>
              ))}
            </ol>

            <div className="mt-8 flex flex-col items-start gap-4 rounded-xl border border-neon-violet/25 bg-neon-violet/[0.06] p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-3">
                <Bot className="mt-0.5 h-5 w-5 shrink-0 text-neon-violet" />
                <div>
                  <div className="text-sm font-medium text-white">
                    {zh ? "卡在某一步？让 AI 一步步带你装" : "Stuck on a step? Let the AI walk you through"}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {zh
                      ? "把报错原文发给 AI 客服，安装 / 显卡 / 组件下载问题秒回；搞不定再转人工。"
                      : "Paste the exact error to the AI assistant — install, GPU and download issues answered instantly; hand off to a human anytime."}
                  </p>
                </div>
              </div>
              <button
                onClick={() => startInstallAssist("guide_footer")}
                className="inline-flex shrink-0 items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                <Bot className="h-4 w-4" />
                {zh ? "开始 AI 协助安装" : "Start AI install assist"}
              </button>
            </div>

            <p className="mt-4 text-xs text-slate-500">
              {zh ? (
                <>推荐配置：NVIDIA 8GB+ 显存（实时换脸 / 数字人）；仅语音克隆 4GB 起。不想动手？<a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">联系 {TELEGRAM_DISPLAY}</a> 预约 99 USDT 远程代部署，装好即用。</>
              ) : (
                <>Recommended: NVIDIA GPU with 8 GB+ VRAM (live swap / digital human); 4 GB for voice-only. Hands-off? <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">Contact {TELEGRAM_DISPLAY}</a> for the 99 USDT remote install service.</>
              )}
            </p>
          </div>
        </Reveal>

        {/* 常见安装问题 */}
        <Reveal className="mt-6">
          <div className="glass rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HelpCircle className="h-5 w-5 text-neon-cyan" />
              {zh ? "常见安装问题" : "Install FAQ"}
            </div>
            <div className="mt-5 space-y-3">
              {INSTALL_GUIDE.faqs.map((f, i) => {
                const isOpen = openFaq === i;
                return (
                  <div key={i} className="overflow-hidden rounded-xl border border-white/10 bg-ink-900/60">
                    <button
                      onClick={() => setOpenFaq(isOpen ? null : i)}
                      aria-expanded={isOpen}
                      className="flex w-full items-center justify-between gap-4 px-4 py-3.5 text-left"
                    >
                      <span className="text-sm font-medium text-white">{f.q[lang]}</span>
                      <ChevronDown
                        aria-hidden
                        className={`h-4 w-4 shrink-0 text-neon-cyan transition-transform ${isOpen ? "rotate-180" : ""}`}
                      />
                    </button>
                    <AnimatePresence initial={false}>
                      {isOpen && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.25, ease: "easeInOut" }}
                        >
                          <p className="px-4 pb-4 text-sm leading-relaxed text-slate-400">{f.a[lang]}</p>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                );
              })}
            </div>
          </div>
        </Reveal>

        {/* 使用手册入口 */}
        <Reveal className="mt-6">
          <div className="glass flex flex-col items-start gap-4 rounded-2xl border border-white/10 p-6 sm:flex-row sm:items-center sm:justify-between md:p-8">
            <div className="flex items-start gap-3">
              <BookOpen className="mt-0.5 h-6 w-6 shrink-0 text-neon-cyan" />
              <div>
                <div className="font-semibold text-white">{zh ? "产品使用手册" : "User manual"}</div>
                <p className="mt-1 max-w-xl text-sm leading-relaxed text-slate-400">
                  {zh
                    ? "装好之后怎么用：声音克隆、直播换脸、手机同传、软件更新与故障排查的完整指南。支持在线阅读，也可一键打印导出 PDF 留档。"
                    : "Everything after install: voice cloning, live face swap, phone interpreting, updates and troubleshooting. Read online or print to PDF in one click."}
                </p>
              </div>
            </div>
            <Link
              href={zh ? "/manual" : "/en/manual"}
              onClick={() => track("manual_open", { from: "download_page" })}
              className="inline-flex shrink-0 items-center gap-2 rounded-full border border-neon-cyan/40 px-5 py-2.5 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
            >
              <BookOpen className="h-4 w-4" />
              {zh ? "打开使用手册" : "Open the manual"}
            </Link>
          </div>
        </Reveal>

        {/* 版本更新记录 */}
        <Reveal className="mt-12">
          <div id="changelog" className="scroll-mt-28">
            <div className="text-center">
              <h2 className="text-2xl font-bold text-white md:text-3xl">{zh ? "版本更新" : "Release notes"}</h2>
              <p className="mx-auto mt-2 max-w-xl text-sm text-slate-400">
                {zh
                  ? "客户端内置一键升级：发现新版本点一下即可完成下载安装，组件与角色数据全保留。"
                  : "One-click in-app updates: a single click downloads and installs new versions — components and character data preserved."}
              </p>
            </div>

            <div className="relative mt-8 space-y-6 before:absolute before:bottom-2 before:left-[15px] before:top-2 before:w-px before:bg-white/10 md:before:left-[19px]">
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

            {RELEASE_NOTES.length > 3 && (
              <div className="mt-6 text-center">
                <button
                  onClick={() => {
                    setShowAllReleases((v) => !v);
                    if (!showAllReleases) track("changelog_expand");
                  }}
                  className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-5 py-2 text-sm text-slate-300 transition hover:border-neon-cyan/40 hover:text-white"
                >
                  <ChevronDown className={`h-4 w-4 transition-transform ${showAllReleases ? "rotate-180" : ""}`} />
                  {showAllReleases
                    ? zh ? "收起历史版本" : "Collapse history"
                    : zh ? `查看全部 ${RELEASE_NOTES.length} 个版本` : `Show all ${RELEASE_NOTES.length} releases`}
                </button>
              </div>
            )}
          </div>
        </Reveal>

        <Reveal className="mt-10 text-center">
          <a href={zh ? "/order" : "/en/order"} className="text-sm text-neon-cyan hover:underline">
            {zh ? "查看套餐与价格，选择适合你的授权 →" : "See plans & pricing →"}
          </a>
        </Reveal>
      </div>
    </section>
  );
}
