"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ChevronDown,
  ClipboardCheck,
  Clock,
  Download,
  HardDrive,
  HelpCircle,
  MessageCircle,
  Monitor,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import RichText from "./RichText";
import ProductIcon from "./ProductIcon";
import { CHATX } from "@/lib/chatxContent";
import { track } from "@/lib/track";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import type { BrandLang } from "@/lib/brand";

/** /downloads/manifest.json 的运行时形态（打包脚本生成；构建时兜底见 chatxContent） */
interface ChatxManifest {
  version?: string;
  filename?: string;
  size_mb?: string;
  sha256?: string;
  signed?: boolean;
}

/**
 * 智聊 ChatX 专属下载页主体（与 AvatarHub / 智控的下载组件隔离，零回归风险——
 * 三个客户端上手卡点不同，内容单源各自维护，骨架风格保持一致）。
 * lang 由路由显式传入（/download/chatx = zh，/en/download/chatx = en）。
 */
export default function ChatxDownloadSection({ lang: forced }: { lang?: BrandLang }) {
  const ctx = useLang();
  const lang: BrandLang = forced ?? ctx.lang;
  const zh = lang === "zh";
  const [openFaq, setOpenFaq] = useState<number | null>(null);
  const [mf, setMf] = useState<ChatxManifest | null>(null);

  const d = CHATX.download;
  // 运行时清单优先（发布脚本随安装包一起更新），构建时常量兜底。
  const version = mf?.version || d.version;
  const filename = mf?.filename || d.filename;
  const sizeLabel = mf?.size_mb ? `${mf.size_mb} MB` : d.size[lang];
  const sha256 = mf?.sha256 || d.sha256;
  const url = `/downloads/${filename}`;

  useEffect(() => {
    fetch(d.manifestUrl)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j?.version) setMf(j as ChatxManifest);
      })
      .catch(() => {});
  }, [d.manifestUrl]);

  const quickNav = [
    {
      icon: HardDrive,
      title: zh ? "分步安装教程" : "Step-by-step install",
      desc: zh ? "五步从下载到开始接待" : "Five steps from download to first chat",
      href: "#install-guide",
      external: false,
    },
    {
      icon: HelpCircle,
      title: zh ? "常见问题" : "Install FAQ",
      desc: zh ? "SmartScreen / 更新 / 数据安全" : "SmartScreen / updates / data safety",
      href: "#faq",
      external: false,
    },
    {
      icon: RefreshCw,
      title: zh ? "内置自动更新" : "Auto-update built in",
      desc: zh ? "启动检查 · 后台下载 · 退出安装" : "Check on launch, install on exit",
      href: "#faq",
      external: false,
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
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-neon-cyan/15 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        {/* 头部 */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <ShieldCheck className="h-3.5 w-3.5" />
            {zh
              ? "数据本地保存 · 免显卡 · 内置自动更新 · SHA-256 可校验"
              : "Local-first data · no GPU · auto-update built in · SHA-256 verifiable"}
          </span>
          <div className="mt-5 flex items-center justify-center gap-3">
            <ProductIcon product="chatx" size={48} alt="智聊 ChatX" className="h-12 w-12 object-contain" />
            <h1 className="text-3xl font-bold text-white md:text-5xl">
              {zh ? "下载智聊 ChatX 客户端" : "Download the ChatX Client"}
            </h1>
          </div>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "聚合 AI 聊天工作台：全渠道统一收件箱、AI 自动拟稿 / 自动回复、实时互译、语音消息与客户画像，一个桌面客户端全部就位。"
              : "The omni-channel AI chat workspace: unified inbox, AI drafting / auto-reply, live translation, voice messages and customer profiles — all in one desktop client."}
          </p>
        </Reveal>

        {/* 下载卡片：Windows 主力 + macOS 规划中 */}
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
                  onClick={() => track("chatx_download_click", { os: "windows", ver: version })}
                  className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
                >
                  <Download className="h-4 w-4" />
                  {zh ? "下载" : "Download"} {filename}
                </a>
              </div>
              <div className="mt-4 break-all rounded-lg bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-slate-600">
                SHA-256: {sha256 || (zh ? "发布时公布" : "published at release")}
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.08}>
            <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-3">
                <Monitor className="h-8 w-8 text-slate-500" />
                <div>
                  <div className="font-semibold text-white">macOS</div>
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
                <a
                  key={q.title}
                  href={q.href}
                  className="glass card-hover flex items-start gap-3 rounded-2xl border border-white/10 p-4"
                >
                  {inner}
                </a>
              );
            })}
          </div>
        </Reveal>

        {/* 装前自查 + 分步安装教程 */}
        <Reveal className="mt-12">
          <div id="install-guide" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HardDrive className="h-5 w-5 text-neon-cyan" />
              {zh ? "分步安装教程 · 从下载到开始接待" : "Install guide · from download to first chat"}
            </div>
            <p className="mt-2 text-sm text-slate-500">
              {zh
                ? "全程约 10–15 分钟，零命令行。装完即是完整工作台，数据全部保存在本机。"
                : "About 10–15 minutes, zero command line. You get the full workspace; all data stays on your machine."}
            </p>

            <div className="mt-5 rounded-xl border border-white/10 bg-ink-950/40 p-4">
              <div className="flex items-center gap-2 text-sm font-medium text-white">
                <ClipboardCheck className="h-4 w-4 text-emerald-400" />
                {zh ? "装前 30 秒自查" : "30-second pre-check"}
              </div>
              <ul className="mt-3 grid gap-2 sm:grid-cols-2">
                {CHATX.preCheck.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-slate-400">
                    <span className="mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400/80" />
                    {c[lang]}
                  </li>
                ))}
              </ul>
            </div>

            <ol className="mt-6 space-y-6">
              {CHATX.install.steps.map((s, i) => (
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

        {/* 常见问题 */}
        <Reveal className="mt-6">
          <div id="faq" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HelpCircle className="h-5 w-5 text-neon-cyan" />
              {zh ? "常见问题" : "FAQ"}
            </div>
            <div className="mt-5 space-y-3">
              {CHATX.install.faqs.map((f, i) => {
                const isOpen = openFaq === i;
                return (
                  <div key={i} className="overflow-hidden rounded-xl border border-white/10 bg-ink-900/60">
                    <button
                      onClick={() => {
                        setOpenFaq(isOpen ? null : i);
                        if (!isOpen) track("chatx_faq_open", { q: f.q.zh });
                      }}
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

        {/* 底部联系条 */}
        <Reveal className="mt-6">
          <div className="glass flex flex-col items-start gap-4 rounded-2xl border border-neon-violet/25 bg-neon-violet/[0.06] p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <MessageCircle className="mt-0.5 h-5 w-5 shrink-0 text-neon-violet" />
              <div>
                <div className="text-sm font-medium text-white">
                  {zh ? "安装遇到问题，或想要团队方案？" : "Install trouble, or need a team plan?"}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">
                  {zh
                    ? "把报错截图发给客服，安装 / 接入 / 授权问题秒回。"
                    : "Send a screenshot to support — install, onboarding and licensing answered fast."}
                </p>
              </div>
            </div>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "chatx_download_footer" })}
              className="inline-flex shrink-0 items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
            >
              <MessageCircle className="h-4 w-4" />
              {zh ? `联系 ${TELEGRAM_DISPLAY}` : `Contact ${TELEGRAM_DISPLAY}`}
            </a>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
