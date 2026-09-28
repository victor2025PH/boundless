"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowRightLeft,
  Check,
  ChevronDown,
  ClipboardCheck,
  Clock,
  Copy,
  Cpu,
  Download,
  HardDrive,
  HelpCircle,
  History,
  KeyRound,
  MessageCircle,
  Monitor,
  PlayCircle,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import { CHATX_TUTORIALS, CHATX_TUTORIALS_PATH, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC, fmtDuration } from "@/lib/chatx-tutorials";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import RichText from "./RichText";
import ProductIcon from "./ProductIcon";
import ProductScreenshots from "./ProductScreenshots";
import { CHATX } from "@/lib/chatxContent";
import { dlHref } from "@/lib/mirror";
import { track, trackOnce } from "@/lib/track";
import { getSrc, getTgUid } from "@/lib/attribution";
import { getLocal } from "@/lib/safe-storage";
import { COOKIE_CONSENT_KEY, COOKIE_DECIDED_EVENT } from "./CookieConsent";
import { CONTACT_EMAIL, CONTACT_EMAIL_URL, CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
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
 * 智聊 ChatX 专属下载页主体（与幻境 STUDIO / 智控的下载组件隔离，零回归风险——
 * 三个客户端上手卡点不同，内容单源各自维护，骨架风格保持一致）。
 * lang 由路由显式传入（/download/chatx = zh，/en/download/chatx = en）。
 */
export default function ChatxDownloadSection({ lang: forced }: { lang?: BrandLang }) {
  const ctx = useLang();
  const lang: BrandLang = forced ?? ctx.lang;
  const zh = lang === "zh";
  const [openFaq, setOpenFaq] = useState<number | null>(null);
  const [mf, setMf] = useState<ChatxManifest | null>(null);
  // 广告来源码：Telegram 广告 → @ChatX_bot → 本页 ?src=；跟到下载点击埋点与 /dl 分流入口（服务端落账）。
  const [src, setSrc] = useState("");
  // bot 用户 id（深链 ?tg=）：只原样带到 /dl，让 bot 知道这个人下过了（追发提醒跳过）。
  const [tg, setTg] = useState("");
  // 点下载后的页内反馈：浏览器下载条在视线外，很多人会以为没点中而重复点/离开。
  const [downloaded, setDownloaded] = useState(false);
  const [shaCopied, setShaCopied] = useState(false);
  // 首屏下载卡滚出视口后，桌面端底部出吸底下载条（移动端已有全站 StickyCTA，且装不了 exe）。
  const [showDock, setShowDock] = useState(false);
  // cookie 条与吸底条同在屏底：未处理前让位（同 StickyCTA）。
  const [cookiePending, setCookiePending] = useState(false);
  const dlCardRef = useRef<HTMLDivElement>(null);
  const shaRef = useRef<HTMLElement>(null);

  const d = CHATX.download;
  // 运行时清单优先（发布脚本随安装包一起更新），构建时常量兜底。
  const version = mf?.version || d.version;
  const filename = mf?.filename || d.filename;
  const sizeLabel = mf?.size_mb ? `${mf.size_mb} MB` : d.size[lang];
  const sha256 = mf?.sha256 || d.sha256;
  // /dl 分流：476MB 大包镜像健康走 R2 边缘，否则自动回落本站 /downloads/
  const dlQuery = new URLSearchParams();
  if (src) dlQuery.set("src", src);
  if (tg) dlQuery.set("tg", tg);
  const dlQs = dlQuery.toString();
  const url = dlHref(`downloads/${filename}`) + (dlQs ? `?${dlQs}` : "");

  useEffect(() => {
    const s = getSrc();
    const t = getTgUid();
    setSrc(s);
    setTg(t);
    // 落地页到达（带来源码）：补上 start → 落地 → 下载漏斗中间一节；全站 pageview 不带 src。
    // 按 src 会话内只计一次，与 admin 来源表「→落地」的人次口径一致；tg=bot 用户 id 让日报能按人去重、把 IP 落到人。
    trackOnce(`chatx_landing_view:${s || "-"}`, "chatx_landing_view", { src: s || undefined, tg: t || undefined });
  }, []);

  useEffect(() => {
    const el = dlCardRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      ([e]) => setShowDock(!e.isIntersecting && e.boundingClientRect.top < 0),
      { threshold: 0 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    setCookiePending(!getLocal(COOKIE_CONSENT_KEY));
    const onDecided = () => setCookiePending(false);
    window.addEventListener(COOKIE_DECIDED_EVENT, onDecided);
    return () => window.removeEventListener(COOKIE_DECIDED_EVENT, onDecided);
  }, []);

  const dockVisible = showDock && !cookiePending;

  const onDownload = (where: string) => {
    track("chatx_download_click", { os: "windows", ver: version, src: src || undefined, tg: tg || undefined, where });
    setDownloaded(true);
  };

  const copySha = async () => {
    if (!sha256) return;
    try {
      await navigator.clipboard.writeText(sha256);
      setShaCopied(true);
      setTimeout(() => setShaCopied(false), 2000);
    } catch {
      // 非安全上下文 / 旧浏览器：选中 hash 文本，用户 Ctrl+C / 长按复制
      const el = shaRef.current;
      if (el) window.getSelection()?.selectAllChildren(el);
    }
  };

  useEffect(() => {
    fetch(d.manifestUrl)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j?.version) setMf(j as ChatxManifest);
      })
      .catch(() => {});
  }, [d.manifestUrl]);

  const installVideo = CHATX_TUTORIALS.find((e) => e.ep === 0);
  const tutorialsHref = zh ? CHATX_TUTORIALS_PATH : `/en${CHATX_TUTORIALS_PATH}`;
  const quickNav = [
    {
      icon: HardDrive,
      title: zh ? "分步安装教程" : "Step-by-step install",
      desc: zh ? "五步从下载到开始接待" : "Five steps from download to first chat",
      href: "#install-guide",
      external: false,
    },
    {
      // 2026-09-17：视频教程合集（装完不会用是下载后最大流失点）
      icon: PlayCircle,
      title: zh ? "视频教程" : "Video tutorials",
      desc: zh
        ? `${CHATX_TUTORIAL_COUNT} 集约 ${Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60)} 分钟学会`
        : `${CHATX_TUTORIAL_COUNT} episodes · ~${Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60)} min`,
      href: tutorialsHref,
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
      icon: History,
      title: zh ? "版本更新记录" : "Release notes",
      desc: zh ? `最新 v${version} 更新了什么` : `What's new in v${version}`,
      href: zh ? "/download/chatx/releases" : "/en/download/chatx/releases",
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
      {/* 智连系（growth）光环：ChatX 属智连系，页面强调色统一走 growth 令牌而非全站通用 neon-cyan */}
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-growth-500/20 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        {/* 头部：一条徽章 + 标题 + 一句话，首屏只留「这是什么 / 值不值得下」 */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-growth-400/30 bg-growth-500/10 px-4 py-1 text-xs font-medium text-growth-300">
            <KeyRound className="h-3.5 w-3.5" />
            {zh ? "免费下载 · 无需 API Key · 免显卡 · 数据本地保存" : "Free · no API key · no GPU · local-first data"}
          </span>
          <div className="mt-5 flex items-center justify-center gap-3">
            <ProductIcon product="chatx" size={48} alt="智聊 ChatX" className="h-12 w-12 object-contain" />
            <h1 className="text-3xl font-bold text-white md:text-5xl">
              {zh ? "下载智聊 ChatX 客户端" : "Download the ChatX Client"}
            </h1>
          </div>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "全渠道统一收件箱 + AI 自动拟稿 / 自动回复 + 实时互译，一个 Windows 客户端全部就位。"
              : "Unified omni-channel inbox + AI drafting / auto-reply + live translation — all in one Windows client."}
          </p>
        </Reveal>

        {/* 下载卡片：Windows 主卡（占 3/5）+ 侧栏（macOS 规划 / 通译并入） */}
        <div className="mt-8 grid gap-5 md:grid-cols-5">
          <Reveal className="md:col-span-3">
            <div
              ref={dlCardRef}
              className="glass relative flex h-full flex-col overflow-hidden rounded-2xl border border-growth-400/30 p-6 shadow-[0_20px_60px_-24px_rgba(30,140,242,0.45)]"
            >
              <div className="pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full bg-growth-500/15 blur-[60px]" />
              <div className="relative flex items-center gap-3">
                <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-ring-growth">
                  <Monitor className="h-6 w-6 text-white" />
                </span>
                <div>
                  <div className="font-semibold text-white">{d.os[lang]}</div>
                  <div className="text-xs text-slate-500">
                    v{version} · {sizeLabel} · {zh ? "内置自动更新" : "auto-updates"}
                  </div>
                </div>
              </div>

              <div className="relative mt-5 flex-1">
                <a
                  href={url}
                  download
                  onClick={() => onDownload("card")}
                  className="chatx-cta inline-flex w-full items-center justify-center gap-2 rounded-full bg-growth-600 px-7 py-3 text-base font-semibold shadow-primary transition hover:bg-growth-500 sm:w-auto"
                >
                  <Download className="h-5 w-5" />
                  {zh ? "免费下载 Windows 版" : "Download for Windows — free"}
                </a>
                <p className="mt-2.5 text-xs text-slate-500">
                  {zh
                    ? `${filename} · 双击安装，免费开始，无需绑卡`
                    : `${filename} · run the installer, free to start, no card`}
                </p>

                {/* 下载已开始：接住下一步（装机 / SmartScreen），减少下载后流失 */}
                <AnimatePresence initial={false}>
                  {downloaded && (
                    <motion.div
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0 }}
                      className="mt-4 flex items-start gap-2 rounded-xl border border-emerald-400/30 bg-emerald-400/[0.08] px-3.5 py-3 text-xs leading-relaxed text-slate-300"
                      role="status"
                    >
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                      <span>
                        {zh ? (
                          <>
                            <b className="text-white">下载已开始。</b>装的时候 Windows 弹「SmartScreen」是正常的——点「更多信息 → 仍要运行」即可。
                            <a href="#install-guide" className="ml-1 text-growth-300 underline decoration-dotted underline-offset-2 hover:text-white">
                              看分步安装教程 →
                            </a>
                          </>
                        ) : (
                          <>
                            <b className="text-white">Download started.</b> A Windows SmartScreen prompt is expected — choose &quot;More info → Run anyway&quot;.
                            <a href="#install-guide" className="ml-1 text-growth-300 underline decoration-dotted underline-offset-2 hover:text-white">
                              Install guide →
                            </a>
                          </>
                        )}
                      </span>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <ul className="relative mt-5 flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-slate-400">
                <li className="inline-flex items-center gap-1.5">
                  <ShieldCheck className="h-3.5 w-3.5 text-growth-400" />
                  {zh ? "数据只存本机" : "Data stays local"}
                </li>
                <li className="inline-flex items-center gap-1.5">
                  <Cpu className="h-3.5 w-3.5 text-growth-400" />
                  {zh ? "免显卡，普通办公电脑即可" : "No GPU — any office PC"}
                </li>
                <li className="inline-flex items-center gap-1.5">
                  <RefreshCw className="h-3.5 w-3.5 text-growth-400" />
                  {zh ? "自动更新" : "Auto-update"}
                </li>
              </ul>

              <details className="group relative mt-4 rounded-lg bg-ink-950/60 px-3 py-2 text-[11px] text-slate-500">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-1.5">
                    <ShieldCheck className="h-3.5 w-3.5" />
                    {zh ? "校验安装包完整性（SHA-256）" : "Verify the installer (SHA-256)"}
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
                </summary>
                <div className="mt-2 flex items-start gap-2">
                  <code ref={shaRef} className="min-w-0 flex-1 break-all font-mono text-slate-400">
                    {sha256 || (zh ? "发布时公布" : "published at release")}
                  </code>
                  {sha256 && (
                    <button
                      type="button"
                      onClick={copySha}
                      aria-label={zh ? "复制 SHA-256" : "Copy SHA-256"}
                      className="shrink-0 rounded-md border border-white/10 p-1 text-slate-400 transition hover:border-growth-400/40 hover:text-white"
                    >
                      {shaCopied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                    </button>
                  )}
                </div>
                <p className="mt-1.5 text-slate-600">
                  {zh
                    ? "PowerShell：Get-FileHash .\\" + filename + " -Algorithm SHA256"
                    : "PowerShell: Get-FileHash .\\" + filename + " -Algorithm SHA256"}
                </p>
              </details>
            </div>
          </Reveal>

          <Reveal delay={0.08} className="md:col-span-2">
            <div className="flex h-full flex-col gap-4">
              <div className="glass flex-1 rounded-2xl border border-white/10 p-5">
                <div className="flex items-center gap-2.5">
                  <Monitor className="h-5 w-5 text-slate-500" />
                  <div className="text-sm font-semibold text-white">macOS</div>
                  <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] text-slate-500">
                    {zh ? "规划中" : "Planned"}
                  </span>
                </div>
                <p className="mt-2.5 text-xs leading-relaxed text-slate-500">{d.macNote[lang]}</p>
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "chatx_download_mac_notify" })}
                  className="mt-3 inline-flex items-center gap-1 text-xs text-growth-300 underline decoration-dotted underline-offset-2 transition hover:text-white"
                >
                  {zh ? "上线后通知我 →" : "Notify me when it ships →"}
                </a>
              </div>

              {/* 通译并入说明：找「通译客户端」的用户在这里得到确定答案（通达橙 = lingo 系令牌） */}
              <div className="glass rounded-2xl border border-lingo-400/25 p-5">
                <div className="flex items-center gap-2.5 text-sm font-semibold text-white">
                  <ArrowRightLeft className="h-4 w-4 text-lingo-400" />
                  {zh ? "通译 LingoX 用户看这里" : "LingoX users"}
                </div>
                <p className="mt-2 text-xs leading-relaxed text-slate-400">
                  {zh
                    ? "通译已并入本客户端，不用再单独下载；通译套餐授权在「会员中心」粘贴激活，原授权继续有效。"
                    : "LingoX is merged into this client — no separate download. Activate your LingoX license in the membership center; existing licenses remain valid."}
                </p>
              </div>
            </div>
          </Reveal>
        </div>

        {/* 快速入口 */}
        <Reveal className="mt-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {quickNav.map((q) => {
              const inner = (
                <>
                  <q.icon className="mt-0.5 h-5 w-5 shrink-0 text-growth-400" />
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

        {/* 真实界面截图（实施78 P0-5/P0-6）：放在下载按钮之后、教程之前——
            「让我下 442MB 未签名 exe，却不给我看软件长什么样」是最先要答的疑问 */}
        <ProductScreenshots lang={lang} />

        {/* 装前自查 + 分步安装教程 */}
        <Reveal className="mt-12">
          <div id="install-guide" className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-2 text-lg font-semibold text-white">
              <HardDrive className="h-5 w-5 text-growth-400" />
              {zh ? "分步安装教程 · 从下载到开始接待" : "Install guide · from download to first chat"}
            </div>
            <p className="mt-2 text-sm text-slate-500">
              {zh
                ? "全程约 10–15 分钟，零命令行。装完即是完整工作台，数据全部保存在本机。"
                : "About 10–15 minutes, zero command line. You get the full workspace; all data stays on your machine."}
            </p>

            {/* 2026-09-17：安装集视频（lib/chatx-tutorials.ts 单一真相）嵌在文字步骤之上；
                不自动播、preload=none，只在用户点播时才拉流。全部 12 集去合集页。 */}
            {installVideo && (
              <div className="mt-5 grid gap-4 lg:grid-cols-[3fr,2fr]">
                <div className="overflow-hidden rounded-xl border border-growth-400/25 bg-ink-950 shadow-[0_0_40px_rgba(30,140,242,0.10)]">
                  <video
                    className="aspect-video w-full"
                    src={installVideo.src}
                    poster={installVideo.poster}
                    controls
                    playsInline
                    preload="none"
                    aria-label={installVideo.title[lang]}
                    onPlay={(e) => {
                      const el = e.currentTarget;
                      if (el.dataset.played) return;
                      el.dataset.played = "1";
                      track("tutorial_play", { ep: installVideo.id, lang, where: "download_page" });
                    }}
                  />
                </div>
                <div className="flex flex-col justify-center">
                  <p className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.2em] text-growth-300">
                    <PlayCircle className="h-3.5 w-3.5" />
                    {zh ? "视频版 · 安装集" : "Video · install episode"}
                  </p>
                  <div className="mt-1.5 text-base font-semibold text-white">
                    {installVideo.title[lang]}
                    <span className="ml-2 text-xs font-normal text-slate-500">{fmtDuration(installVideo.durationSec)}</span>
                  </div>
                  <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{installVideo.desc[lang]}</p>
                  <a
                    href={tutorialsHref}
                    onClick={() => track("tutorial_entry_click", { where: "download_install_guide" })}
                    className="mt-3 inline-flex w-fit items-center gap-1.5 rounded-full border border-growth-400/40 px-4 py-1.5 text-xs text-growth-300 transition hover:bg-growth-500/10"
                  >
                    {zh
                      ? `装好之后怎么用？看全部 ${CHATX_TUTORIAL_COUNT} 集 →`
                      : `Installed — now what? All ${CHATX_TUTORIAL_COUNT} episodes →`}
                  </a>
                </div>
              </div>
            )}

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
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center chatx-cta rounded-full bg-ring-growth text-xs font-bold">
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
                    {"blocker" in s && s.blocker && (
                      <div className="mt-2 flex items-start gap-2 rounded-lg border border-rose-400/30 bg-rose-400/[0.07] px-3 py-2.5 text-xs leading-relaxed text-slate-300">
                        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400" />
                        <span>
                          <RichText text={s.blocker[lang]} />
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
              <HelpCircle className="h-5 w-5 text-growth-400" />
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
                        className={`h-4 w-4 shrink-0 text-growth-400 transition-transform ${isOpen ? "rotate-180" : ""}`}
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
          <div className="glass flex flex-col items-start gap-4 rounded-2xl border border-growth-400/25 bg-growth-500/[0.06] p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <MessageCircle className="mt-0.5 h-5 w-5 shrink-0 text-growth-400" />
              <div>
                <div className="text-sm font-medium text-white">
                  {zh ? "安装遇到问题，或想要团队方案？" : "Install trouble, or need a team plan?"}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">
                  {zh
                    ? "把报错截图发给客服，安装 / 接入 / 授权问题秒回。"
                    : "Send a screenshot to support — install, onboarding and licensing answered fast."}
                </p>
                {/* 邮箱兜底（实施78 U5）：此前整页唯一联系方式是 Telegram，而从 AI/搜索
                    进来的国际访客常常不用 Telegram，也不会为问一个问题装一个 App。
                    判空渲染——lib/site.CONTACT_EMAIL 为空时这行不出现，绝不显示会退信的地址。 */}
                {CONTACT_EMAIL && (
                  <p className="mt-1.5 text-xs text-slate-500">
                    {zh ? "不用 Telegram？发邮件也行：" : "Not on Telegram? Email us: "}
                    <a
                      href={CONTACT_EMAIL_URL}
                      onClick={() => track("cta_click", { where: "chatx_download_email" })}
                      className="text-growth-300 underline decoration-dotted underline-offset-2 transition hover:text-white"
                    >
                      {CONTACT_EMAIL}
                    </a>
                  </p>
                )}
              </div>
            </div>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "chatx_download_footer" })}
              className="chatx-cta inline-flex shrink-0 items-center gap-2 rounded-full bg-growth-600 px-5 py-2.5 text-sm font-medium shadow-primary transition hover:bg-growth-500"
            >
              <MessageCircle className="h-4 w-4" />
              {zh ? `联系 ${TELEGRAM_DISPLAY}` : `Contact ${TELEGRAM_DISPLAY}`}
            </a>
          </div>
        </Reveal>
      </div>

      {/* 吸底下载条（桌面端）：首屏下载卡滚出视口后出现，看截图 / 教程 / FAQ 时随时能下；
          用全站 .sticky-cta 钩子，导航抽屉打开时同样让位。 */}
      <div
        aria-hidden={!dockVisible}
        className={`sticky-cta fixed inset-x-0 bottom-0 z-[var(--z-sticky)] hidden transition-transform duration-300 lg:block ${
          dockVisible ? "translate-y-0" : "pointer-events-none translate-y-full"
        }`}
      >
        <div className="mx-auto max-w-5xl px-5 pb-4">
          <div className="glass flex items-center justify-between gap-4 rounded-2xl border border-growth-400/30 px-5 py-3 shadow-[0_16px_48px_-16px_rgba(30,140,242,0.45)]">
            <div className="flex min-w-0 items-center gap-3">
              <ProductIcon product="chatx" size={32} alt="" className="h-8 w-8 shrink-0 object-contain" />
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-white">
                  {zh ? "智聊 ChatX · Windows 客户端" : "ChatX · Windows client"}
                </div>
                <div className="truncate text-xs text-slate-500">
                  v{version} · {sizeLabel} · {zh ? "免费 · 无需 API Key" : "free · no API key"}
                </div>
              </div>
            </div>
            <a
              href={url}
              download
              tabIndex={dockVisible ? 0 : -1}
              onClick={() => onDownload("dock")}
              className="chatx-cta inline-flex shrink-0 items-center gap-2 rounded-full bg-growth-600 px-5 py-2.5 text-sm font-semibold shadow-primary transition hover:bg-growth-500"
            >
              <Download className="h-4 w-4" />
              {zh ? "免费下载" : "Download free"}
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
