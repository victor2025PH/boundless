"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ChevronDown,
  ClipboardCheck,
  Clock,
  Download,
  HardDrive,
  HelpCircle,
  KeyRound,
  MessageCircle,
  Monitor,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import RichText from "./RichText";
import ProductIcon from "./ProductIcon";
import { MATRIXX, MATRIXX_RELEASE_BASE } from "@/lib/matrixxContent";
import { parseLatestYml, formatMb } from "@/lib/downloads";
import { track } from "@/lib/track";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import type { BrandLang } from "@/lib/brand";

/**
 * 智控 MatrixX 专属下载页主体（与 AvatarHub 的 DownloadSection 隔离，零回归风险）。
 * lang 由所属路由（/matrix/download 或 /en/matrix/download）显式传入，保证语言随 URL 正确；
 * 未传时回退到全局语言上下文。
 */
export default function MatrixxDownloadSection({ lang: forced }: { lang?: BrandLang }) {
  const ctx = useLang();
  const lang: BrandLang = forced ?? ctx.lang;
  const zh = lang === "zh";
  const [openFaq, setOpenFaq] = useState<number | null>(null);

  const d = MATRIXX.download;

  // 运行时清单校正：electron-updater 的 latest.yml 是发布脚本必产物，以它为准，
  // 发新版只传文件即可、页面版本/大小/下载链自动跟上（构建常量仅兜底）。
  const [live, setLive] = useState<{ version: string; filename: string; sizeLabel: string } | null>(null);
  useEffect(() => {
    fetch(`${MATRIXX_RELEASE_BASE}/latest.yml`)
      .then((r) => (r.ok ? r.text() : null))
      .then((t) => {
        const m = t ? parseLatestYml(t) : null;
        if (m?.filename) setLive({ version: m.version, filename: m.filename, sizeLabel: formatMb(m.sizeBytes) });
      })
      .catch(() => {});
  }, []);
  const version = live?.version ?? d.version;
  const filename = live?.filename ?? d.filename;
  const sizeLabel = live?.sizeLabel || d.size[lang];
  const url = live ? `${MATRIXX_RELEASE_BASE}/${live.filename}` : d.url;
  // 构建常量里的 SHA-256 只对应它同版的安装包；服务器已发更新版时展示口径切「与最新发布一致」，
  // 绝不给新包配旧校验值（比不显示更糟）。
  const shaFresh = version === d.version;

  const quickNav = [
    {
      icon: HardDrive,
      title: zh ? "分步安装教程" : "Step-by-step install",
      desc: zh ? "六步从下载到跑通第一个矩阵" : "Six steps to your first fleet",
      href: "#install-guide",
    },
    {
      icon: KeyRound,
      title: zh ? "获取 API 凭据" : "Get API credentials",
      desc: zh ? "my.telegram.org 申请图解" : "How to get api_id / api_hash",
      href: "#api-guide",
    },
    {
      icon: HelpCircle,
      title: zh ? "常见问题" : "Install FAQ",
      desc: zh ? "SmartScreen / 登录 / 防封" : "SmartScreen / login / anti-ban",
      href: "#faq",
    },
    {
      icon: MessageCircle,
      title: zh ? "联系客服" : "Contact support",
      desc: zh ? `Telegram ${TELEGRAM_DISPLAY} · 秒回` : `Telegram ${TELEGRAM_DISPLAY}`,
      href: CONTACT_URL,
      external: true,
    },
  ];

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-neon-blue/15 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        {/* 头部 */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <ShieldCheck className="h-3.5 w-3.5" />
            {zh
              ? "本地运行 · 数据不出网 · 免显卡 · SHA-256 可校验"
              : "Runs locally · data on-device · no GPU · SHA-256 verifiable"}
          </span>
          <div className="mt-5 flex items-center justify-center gap-3">
            <ProductIcon product="matrixx" size={48} className="h-12 w-12 object-contain" alt="智控 MatrixX" />
            <h1 className="text-3xl font-bold text-white md:text-5xl">
              {zh ? "下载智控 MatrixX 客户端" : "Download the MatrixX Client"}
            </h1>
          </div>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "Telegram 多账号矩阵化运营：搜索发现、群监控、成员提取、消息群发防封、AI 自动回复，本地部署数据不出本机。"
              : "Telegram fleet operations: discovery, group monitoring, member extraction, anti-ban broadcasting and AI auto-reply — deployed locally, data stays on your machine."}
          </p>
        </Reveal>

        {/* 下载卡片：Windows 主力 + Mac 规划中 */}
        <div className="mt-12 grid gap-6 md:grid-cols-2">
          <Reveal>
            <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-3">
                <Monitor className="h-8 w-8 text-neon-cyan" />
                <div>
                  <div className="font-semibold text-white">{d.os[lang]}</div>
                  <div className="text-xs text-slate-500">
                    {zh ? "版本" : "Version"} v{version} · {sizeLabel}
                  </div>
                </div>
              </div>
              <div className="mt-5 flex-1">
                <a
                  href={url}
                  download
                  onClick={() => track("matrixx_download_click", { os: "windows", ver: version })}
                  className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
                >
                  <Download className="h-4 w-4" />
                  {zh ? "下载" : "Download"} {filename}
                </a>
              </div>
              <div className="mt-4 break-all rounded-lg bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-slate-600">
                SHA-256:{" "}
                {shaFresh && d.sha256
                  ? d.sha256
                  : zh
                    ? "以最新发布为准（安装包由 latest.yml 内 SHA-512 校验）"
                    : "verified via SHA-512 in latest.yml for the current release"}
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.08}>
            <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-3">
                <Monitor className="h-8 w-8 text-slate-500" />
                <div>
                  <div className="font-semibold text-white">macOS / Linux</div>
                  <div className="text-xs text-slate-500">{zh ? "规划中" : "Planned"}</div>
                </div>
              </div>
              <div className="mt-5 flex-1">
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block rounded-full border border-neon-cyan/40 px-6 py-2.5 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
                >
                  {zh ? "上线后通知我" : "Notify me"}
                </a>
              </div>
              <p className="mt-4 text-xs leading-relaxed text-slate-500">{d.macNote[lang]}</p>
            </div>
          </Reveal>
        </div>

        {/* 快速入口 */}
        <Reveal className="mt-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {quickNav.map((q) => {
              const inner = (
                <>
                  <q.icon className="mt-0.5 h-5 w-5 shrink-0 text-neon-cyan" />
                  <div>
                    <div className="text-sm font-medium text-white">{q.title}</div>
                    <div className="mt-0.5 text-xs text-slate-500">{q.desc}</div>
                  </div>
                </>
              );
              return q.external ? (
                <a
                  key={q.title}
                  href={q.href}
                  target="_blank"
                  rel="noreferrer"
                  className="glass card-hover flex items-start gap-3 rounded-2xl border border-white/10 p-4"
                >
                  {inner}
                </a>
              ) : (
                <Link
                  key={q.title}
                  href={q.href}
                  className="glass card-hover flex items-start gap-3 rounded-2xl border border-white/10 p-4"
                >
                  {inner}
                </Link>
              );
            })}
          </div>
        </Reveal>

        {/* 装前自查 + 分步安装教程 */}
        <Reveal className="mt-12">
          <div id="install-guide" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HardDrive className="h-5 w-5 text-neon-cyan" />
              {zh ? "分步安装教程 · 从下载到跑通" : "Install guide · from download to first fleet"}
            </div>
            <p className="mt-2 text-sm text-slate-500">
              {zh
                ? "全程约 10 分钟，零命令行。带虚线下划线的术语，悬停 / 点击可看解释。"
                : "About 10 minutes, zero command line. Hover / tap dotted terms for explanations."}
            </p>

            <div className="mt-5 rounded-xl border border-white/10 bg-ink-950/40 p-4">
              <div className="flex items-center gap-2 text-sm font-medium text-white">
                <ClipboardCheck className="h-4 w-4 text-emerald-400" />
                {zh ? "装前 30 秒自查" : "30-second pre-check"}
              </div>
              <ul className="mt-3 grid gap-2 sm:grid-cols-2">
                {MATRIXX.preCheck.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-slate-400">
                    <span className="mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400/80" />
                    {c[lang]}
                  </li>
                ))}
              </ul>
            </div>

            <ol className="mt-6 space-y-6">
              {MATRIXX.steps.map((s, i) => (
                <li key={i} className="flex items-start gap-4">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet text-xs font-bold text-ink-950">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="font-medium text-white">{s.title[lang]}</span>
                      {"time" in s && s.time && (
                        <span className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2 py-0.5 text-[11px] text-slate-500">
                          <Clock className="h-3 w-3" />
                          {s.time[lang]}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-sm leading-relaxed text-slate-400">
                      <RichText text={s.detail[lang]} />
                    </p>
                    {"sub" in s && s.sub && (
                      <ul className="mt-2 space-y-1.5">
                        {s.sub[lang].map((line, j) => (
                          <li key={j} className="flex items-start gap-2 text-xs leading-relaxed text-slate-500">
                            <Sparkles className="mt-0.5 h-3 w-3 shrink-0 text-neon-cyan/70" />
                            <span>
                              <RichText text={line} />
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {"warn" in s && s.warn && (
                      <div className="mt-2.5 flex items-start gap-2 rounded-lg border border-amber-400/25 bg-amber-400/[0.06] px-3 py-2.5 text-xs leading-relaxed text-slate-300">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
                        <span>
                          <RichText text={s.warn[lang]} />
                        </span>
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </Reveal>

        {/* API 凭据获取专区（本产品最大卡点，单独强调） */}
        <Reveal className="mt-6">
          <div id="api-guide" className="glass scroll-mt-28 rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.04] p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <KeyRound className="h-5 w-5 text-neon-cyan" />
              {zh ? "获取 Telegram API 凭据（api_id / api_hash）" : "Get your Telegram API credentials"}
            </div>
            <p className="mt-2 text-sm leading-relaxed text-slate-400">
              {zh
                ? "连接 Telegram 账号需要一组官方 API 凭据，免费申请、几分钟完成、一次配置长期有效。"
                : "Connecting Telegram accounts needs one set of official API credentials — free, a few minutes, set once and reuse."}
            </p>
            <ol className="mt-4 space-y-2.5">
              {(zh
                ? [
                    "打开 my.telegram.org，用你的 Telegram 手机号登录（会收到验证码）。",
                    "进入 API development tools，随意填写 App title 与 short name 创建应用。",
                    "复制页面给出的 api_id（一串数字）与 api_hash（一串字母数字）。",
                    "回到智控 MatrixX「API 凭据」页粘贴保存——完成，凭据只存本机。",
                  ]
                : [
                    "Open my.telegram.org and sign in with your Telegram phone number (you'll get a code).",
                    "Go to API development tools; create an app with any App title and short name.",
                    "Copy the api_id (digits) and api_hash (alphanumeric) shown.",
                    "Paste them into MatrixX's API credentials page — done, stored only on your machine.",
                  ]
              ).map((line, i) => (
                <li key={i} className="flex items-start gap-3 text-sm leading-relaxed text-slate-300">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-neon-cyan/40 text-[11px] text-neon-cyan">
                    {i + 1}
                  </span>
                  {line}
                </li>
              ))}
            </ol>
            <a
              href="https://my.telegram.org"
              target="_blank"
              rel="noreferrer"
              className="mt-5 inline-flex items-center gap-2 rounded-full border border-neon-cyan/40 px-5 py-2 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
            >
              <KeyRound className="h-4 w-4" />
              {zh ? "前往 my.telegram.org" : "Go to my.telegram.org"}
            </a>
          </div>
        </Reveal>

        {/* 常见问题 */}
        <Reveal className="mt-6">
          <div id="faq" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HelpCircle className="h-5 w-5 text-neon-cyan" />
              {zh ? "常见问题" : "FAQ"}
            </div>
            <div className="mt-5 space-y-3">
              {MATRIXX.faqs.map((f, i) => {
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
                          <p className="px-4 pb-4 text-sm leading-relaxed text-slate-400">
                            <RichText text={f.a[lang]} />
                          </p>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                );
              })}
            </div>
          </div>
        </Reveal>

        {/* 版本 + 内置自动更新 */}
        <Reveal className="mt-6">
          <div className="glass flex flex-col items-start gap-3 rounded-2xl border border-white/10 p-6 sm:flex-row sm:items-center sm:justify-between md:p-8">
            <div className="flex items-start gap-3">
              <RefreshCw className="mt-0.5 h-6 w-6 shrink-0 text-neon-cyan" />
              <div>
                <div className="font-semibold text-white">
                  {zh ? `当前版本 v${version} · 内置自动更新` : `Current v${version} · built-in auto-update`}
                </div>
                <p className="mt-1 max-w-xl text-sm leading-relaxed text-slate-400">
                  {zh
                    ? "客户端启动后自动检测新版本、后台下载，退出时自动安装——账号与数据全部保留，无需手动重装。"
                    : "The client checks for updates on launch, downloads in the background and installs on exit — accounts and data preserved, no manual reinstall."}
                </p>
              </div>
            </div>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex shrink-0 items-center gap-2 rounded-full border border-neon-cyan/40 px-5 py-2.5 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
            >
              <MessageCircle className="h-4 w-4" />
              {zh ? "咨询与授权" : "Sales & licensing"}
            </a>
          </div>
        </Reveal>

        <Reveal className="mt-10 text-center">
          <Link href={zh ? "/matrix" : "/en/matrix"} className="text-sm text-neon-cyan hover:underline">
            {zh ? "← 返回智控 MatrixX 产品介绍" : "← Back to MatrixX overview"}
          </Link>
        </Reveal>
      </div>
    </section>
  );
}
