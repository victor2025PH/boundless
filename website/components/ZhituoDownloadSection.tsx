"use client";

import { useState } from "react";
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
  Server,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import RichText from "./RichText";
import { ZHITUO } from "@/lib/zhituoContent";
import { track } from "@/lib/track";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import type { BrandLang } from "@/lib/brand";

/**
 * 智拓 专属下载页主体（与智聊 ChatX / 智控 MatrixX 的下载组件隔离，零回归风险）。
 * 视觉沿用下载页统一骨架（growth 家族青色），lang 由路由显式传入。
 * 合规隔离：本页为 gated，主站不收录（见 app/download/zhituo/page.tsx 的 robots 设置）。
 */
export default function ZhituoDownloadSection({ lang: forced }: { lang?: BrandLang }) {
  const ctx = useLang();
  const lang: BrandLang = forced ?? ctx.lang;
  const zh = lang === "zh";
  const [openFaq, setOpenFaq] = useState<number | null>(null);

  const d = ZHITUO.download;
  const version = d.version;
  const sizeLabel = d.size[lang];

  const quickNav = [
    {
      icon: HardDrive,
      title: zh ? "分步安装教程" : "Step-by-step install",
      desc: zh ? "六步从下载到接入主控" : "Six steps to enroll",
      href: "#install-guide",
    },
    {
      icon: KeyRound,
      title: zh ? "接入主控说明" : "How enrollment works",
      desc: zh ? "账号认证 · 自动接入" : "Account auth · auto-enroll",
      href: "#enroll-guide",
    },
    {
      icon: HelpCircle,
      title: zh ? "常见问题" : "Install FAQ",
      desc: zh ? "SmartScreen / 组件 / 接入" : "SmartScreen / component / enroll",
      href: "#faq",
    },
    {
      icon: MessageCircle,
      title: zh ? "联系客服" : "Contact support",
      desc: zh ? `Telegram ${TELEGRAM_DISPLAY}` : `Telegram ${TELEGRAM_DISPLAY}`,
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
            {ZHITUO.hero.trustline[lang]}
          </span>
          <div className="mt-5 flex items-center justify-center gap-3">
            <Server className="h-11 w-11 text-neon-cyan" />
            <h1 className="text-3xl font-bold text-white md:text-5xl">
              {zh ? "下载智拓客户端" : "Download the Zhituo Client"}
            </h1>
          </div>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "机房真机多账号运营客户端：一体安装、按账号自动接入主控，本地部署、数据不出本机。"
              : "On-prem multi-account operations client: one-shot install, account-based auto-enrollment, deployed locally, data stays on your machine."}
          </p>
        </Reveal>

        {/* 下载卡片：一体安装包 + 仅节点 */}
        <div className="mt-12 grid gap-6 md:grid-cols-2">
          <Reveal>
            <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-3">
                <Monitor className="h-8 w-8 text-neon-cyan" />
                <div>
                  <div className="font-semibold text-white">{d.os[lang]}</div>
                  <div className="text-xs text-slate-500">
                    {zh ? "一体安装包 版本" : "All-in-one version"} v{version} · {sizeLabel}
                  </div>
                </div>
              </div>
              <div className="mt-5 flex-1">
                <a
                  href={d.url}
                  download
                  rel="nofollow"
                  onClick={() => track("zhituo_download_click", { kind: "allinone", ver: version })}
                  className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
                >
                  <Download className="h-4 w-4" />
                  {zh ? "下载一体安装包" : "Download all-in-one"} {version}
                </a>
                <p className="mt-3 text-xs leading-relaxed text-slate-500">{d.note[lang]}</p>
              </div>
              <div className="mt-4 break-all rounded-lg bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-slate-600">
                SHA-256: {d.sha256}
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.08}>
            <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-3">
                <Server className="h-8 w-8 text-slate-400" />
                <div>
                  <div className="font-semibold text-white">{zh ? "仅节点安装包" : "Node-only installer"}</div>
                  <div className="text-xs text-slate-500">{d.nodeOnly.filename}</div>
                </div>
              </div>
              <div className="mt-5 flex-1">
                <a
                  href={d.nodeOnly.url}
                  download
                  rel="nofollow"
                  onClick={() => track("zhituo_download_click", { kind: "nodeonly", ver: version })}
                  className="inline-block rounded-full border border-neon-cyan/40 px-6 py-2.5 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
                >
                  {zh ? "下载仅节点包" : "Download node-only"}
                </a>
                <p className="mt-4 text-xs leading-relaxed text-slate-500">{d.nodeOnly.note[lang]}</p>
              </div>
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
              {zh ? "分步安装教程 · 从下载到接入" : "Install guide · from download to enroll"}
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
                {ZHITUO.preCheck.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-slate-400">
                    <span className="mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400/80" />
                    {c[lang]}
                  </li>
                ))}
              </ul>
            </div>

            <ol className="mt-6 space-y-6">
              {ZHITUO.steps.map((s, i) => (
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

        {/* 接入主控说明（本产品关键一步，单独强调） */}
        <Reveal className="mt-6">
          <div id="enroll-guide" className="glass scroll-mt-28 rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.04] p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <KeyRound className="h-5 w-5 text-neon-cyan" />
              {zh ? "接入主控是怎么回事" : "How enrollment works"}
            </div>
            <p className="mt-2 text-sm leading-relaxed text-slate-400">
              {zh
                ? "机房电脑（副控）用主控智拓后台的登录账号认证，通过后自动接入、不需要人工批准；用哪个主控账号登录，这台电脑就归哪个主控。"
                : "A room PC authenticates with the master console's login account; on success it enrolls automatically with no manual approval — the account you log in with decides which master it joins."}
            </p>
            <ol className="mt-4 space-y-2.5">
              {(zh
                ? [
                    "在主控智拓后台（局域网地址，如 http://192.168.0.176:18080）拿到你的登录账号和密码。",
                    "安装时选「机房节点」，填电脑编号，再填这组主控账号密码。",
                    "认证通过后自动建立受控连接并接入——有失败限流、可吊销、留接入记录。",
                    "现在不方便就勾「暂不接入」，装完后在桌面「智拓后台」或「重新接入主控」里补。",
                  ]
                : [
                    "Get your login account and password from the master console on your LAN (e.g. http://192.168.0.176:18080).",
                    "During install pick Room Node, enter the PC code, then this master account and password.",
                    "On success it sets up the controlled connection and enrolls — rate-limited, revocable, audit-logged.",
                    "Not convenient now? Tick Enroll later and finish from the desktop console or Re-enroll afterwards.",
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
              {ZHITUO.faqs.map((f, i) => {
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

        {/* 版本 + 升级说明 */}
        <Reveal className="mt-6">
          <div className="glass flex flex-col items-start gap-3 rounded-2xl border border-white/10 p-6 sm:flex-row sm:items-center sm:justify-between md:p-8">
            <div className="flex items-start gap-3">
              <RefreshCw className="mt-0.5 h-6 w-6 shrink-0 text-neon-cyan" />
              <div>
                <div className="font-semibold text-white">
                  {zh ? `当前版本 v${version}` : `Current v${version}`}
                </div>
                <p className="mt-1 max-w-xl text-sm leading-relaxed text-slate-400">
                  {zh
                    ? "升级直接重新运行最新一体安装包即可：已装群控组件会先停占用再升级、失败不影响节点，接入配置与密钥全部保留。"
                    : "To upgrade, just re-run the latest all-in-one installer: the control component is stopped before upgrade, a failure won't block the node, and enrollment config and keys are preserved."}
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
      </div>
    </section>
  );
}