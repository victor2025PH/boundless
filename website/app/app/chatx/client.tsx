"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { content } from "@/lib/content";
import { CONTACT_URL, GROUP_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { getSrc, isValidSrc, rememberSrc } from "@/lib/attribution";
import { chatxMiniAppOutLink, handoffShareText, telegramShareHref } from "@/lib/chatx-handoff";
import { CHATX_TUTORIALS, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC, fmtDuration } from "@/lib/chatx-tutorials";
import { CHATX_PLATFORM_COUNT, platformsInline } from "@/lib/chatx-bot-hero";
import type { ChatLinkAction } from "@/lib/chat-links";
import { AiChat, ChatTheater, LeadForm, SectionTitle } from "../views";

type Lang = "zh" | "en";

// Telegram 桌面端能直接下载 Windows 安装包；手机端主按钮改成「发到电脑」（收藏消息，电脑端同步）
const DESKTOP_PLATFORMS = new Set(["tdesktop", "macos", "weba", "webk", "web", "unigram"]);

const FEATURES: { icon: string; zh: [string, string]; en: [string, string] }[] = [
  { icon: "🤖", zh: ["AI 自动回复", "客户消息秒回，全自动 / 人审放行 / 仅拟稿三档可切"], en: ["AI auto-reply", "Instant replies; fully automatic / human-approved / draft-only"] },
  { icon: "📚", zh: ["你的话术 + 人设", "喂进产品资料，AI 按你的风格聊、按你的流程推进成交"], en: ["Your script & persona", "Feed it your material; AI chats in your style and pushes the sale"] },
  { icon: "🌐", zh: ["外语自动互译", "客户说什么语言都能聊，收发双向翻译"], en: ["Live translation", "Any language in, your language out — both directions"] },
  { icon: "📨", zh: [`${CHATX_PLATFORM_COUNT} 平台多账号`, `${platformsInline("zh")} 一屏管`], en: [`${CHATX_PLATFORM_COUNT} platforms`, `${platformsInline("en")} on one screen`] },
  { icon: "🎙", zh: ["语音消息", "语音转写、克隆音色回复"], en: ["Voice messages", "Transcribe and reply in your cloned voice"] },
  { icon: "🔒", zh: ["本地运行", "数据留在自己电脑，免显卡、无需 API Key"], en: ["Runs locally", "Data stays on your PC; no GPU, no API key"] },
];

function haptic(kind: "light" | "success" = "light") {
  try {
    const h = window.Telegram?.WebApp?.HapticFeedback;
    if (kind === "success") h?.notificationOccurred?.("success");
    else h?.impactOccurred?.("light");
  } catch {
    /* non-fatal */
  }
}

function openLink(url: string) {
  try {
    const tg = window.Telegram?.WebApp as { openTelegramLink?: (u: string) => void; openLink?: (u: string) => void } | undefined;
    if (url.startsWith("https://t.me/") && tg?.openTelegramLink) {
      tg.openTelegramLink(url);
      return;
    }
    if (tg?.openLink) {
      tg.openLink(url);
      return;
    }
  } catch {
    /* fall through */
  }
  window.open(url, "_blank");
}

export default function ChatxMiniAppClient({ initialSrc }: { initialSrc: string }) {
  const [lang, setLang] = useState<Lang>("zh");
  const [src, setSrc] = useState(initialSrc);
  const [desktop, setDesktop] = useState(false);
  const [tgName, setTgName] = useState("");
  const [tgUid, setTgUid] = useState("");
  const [leadName, setLeadName] = useState("");
  const [leadContact, setLeadContact] = useState("");
  const opened = useRef(false);
  const zh = lang === "zh";
  const t = content[lang];

  // 来源码优先级：URL ?src（服务端已读）→ startapp start_param → initData 回源查最近 /start → 30 天本地存储
  useEffect(() => {
    let platform = "web";
    let resolved = initialSrc;
    let initData = "";
    try {
      const tg = window.Telegram?.WebApp;
      if (tg) {
        tg.ready();
        tg.expand();
        platform = tg.platform || "tg";
        initData = tg.initData ?? "";
        const u = tg.initDataUnsafe?.user;
        if (typeof u?.id === "number" && u.id > 0) setTgUid(String(u.id));
        if (u?.first_name) {
          setTgName(u.first_name);
          setLeadName(u.first_name);
        }
        if (u?.username) setLeadContact(`@${u.username}`);
        if (u?.language_code && !u.language_code.startsWith("zh")) setLang("en");
        const sp = tg.initDataUnsafe?.start_param ?? "";
        if (!resolved && isValidSrc(sp)) resolved = sp;
      } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
        setLang("en");
      }
    } catch {
      /* non-fatal */
    }
    setDesktop(DESKTOP_PLATFORMS.has(platform));
    if (!resolved) resolved = getSrc();
    if (resolved) {
      rememberSrc(resolved);
      setSrc(resolved);
    }

    if (!opened.current) {
      opened.current = true;
      track("miniapp_open", { view: "chatx", source: resolved || "direct", platform });
    }

    if (!resolved && initData) {
      fetch("/api/telegram/chatx/miniapp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData }),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((d: { ok?: boolean; src?: string } | null) => {
          if (d?.ok && isValidSrc(d.src) && d.src !== "organic") {
            rememberSrc(d.src);
            setSrc(d.src);
          }
        })
        .catch(() => null);
    }
  }, [initialSrc]);

  const downloadUrl = useMemo(() => chatxMiniAppOutLink("download", lang, src, tgUid), [lang, src, tgUid]);
  const tutorialsUrl = useMemo(() => chatxMiniAppOutLink("tutorials", lang, src), [lang, src]);

  const goDownload = useCallback(() => {
    haptic("light");
    track("chatx_download_click", { src: src || "organic", where: "miniapp", lang });
    track("miniapp_cta", { view: "chatx", interest: "download" });
    openLink(downloadUrl);
  }, [downloadUrl, src, lang]);

  const goHandoff = useCallback(() => {
    haptic("light");
    track("miniapp_cta", { view: "chatx", interest: "handoff", src: src || "organic" });
    openLink(telegramShareHref(downloadUrl, handoffShareText(lang)));
  }, [downloadUrl, src, lang]);

  const goTutorials = useCallback(() => {
    haptic("light");
    track("miniapp_cta", { view: "chatx", interest: "tutorials" });
    openLink(tutorialsUrl);
  }, [tutorialsUrl]);

  const goContact = useCallback(() => {
    haptic("light");
    track("miniapp_cta", { view: "chatx", interest: "human" });
    openLink(CONTACT_URL);
  }, []);

  const primary = desktop
    ? { text: zh ? "📥 下载 Windows 版（免费）" : "📥 Download for Windows (free)", action: goDownload }
    : { text: zh ? "📤 发到电脑下载" : "📤 Send to my computer", action: goHandoff };

  // AI 回答里的下载 / 教程链接 → 页面自己的 CTA（手机端下载自动变“发到电脑”，埋点 / utm 与首屏按钮一致）；其他链接走 Telegram openLink
  const aiLinkAction = useCallback(
    (url: string): ChatLinkAction | null => {
      let u: URL;
      try {
        u = new URL(url);
      } catch {
        return null;
      }
      const p = u.pathname;
      if (/\/download\/chatx\/?$/.test(p) || p.startsWith("/dl/")) {
        return {
          label: primary.text,
          onClick: () => {
            track("miniapp_cta", { view: "chatx", interest: "ai_link_download" });
            primary.action();
          },
        };
      }
      if (/\/chatx\/tutorials/.test(p)) {
        return {
          label: zh ? "🎬 看视频教程" : "🎬 Watch tutorials",
          onClick: () => {
            track("miniapp_cta", { view: "chatx", interest: "ai_link_tutorials" });
            goTutorials();
          },
        };
      }
      if (url === CONTACT_URL) return { label: zh ? "👤 人工客服" : "👤 Human support", onClick: goContact };
      return { label: `${u.hostname} ↗`, onClick: () => openLink(url) };
    },
    [primary.text, primary.action, zh, goTutorials, goContact]
  );

  // Telegram MainButton = 首屏主 CTA（growth 品类色）
  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    const mb = tg?.MainButton as
      | (NonNullable<typeof tg>["MainButton"] & {
          setParams?: (p: { text?: string; color?: string; text_color?: string; is_visible?: boolean }) => void;
        })
      | undefined;
    if (!mb) return;
    const onClick = () => primary.action();
    try {
      if (typeof mb.setParams === "function") {
        mb.setParams({ text: primary.text, color: "#1e8cf2", text_color: "#ffffff", is_visible: true });
      } else {
        mb.text = primary.text;
        mb.color = "#1e8cf2";
        mb.textColor = "#ffffff";
        mb.show();
      }
      mb.onClick(onClick);
    } catch {
      /* MainButton unavailable — inline CTAs still work */
    }
    return () => {
      try {
        mb.offClick(onClick);
      } catch {
        /* ignore */
      }
    };
  }, [primary.text, primary.action]);

  const mins = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
  const episodes = CHATX_TUTORIALS.slice(0, 4);

  return (
    <main className="mx-auto min-h-screen max-w-lg bg-slate-950 px-4 pb-24 pt-4 text-slate-100">
      <header className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold">智聊 ChatX</div>
          <div className="text-[11px] text-growth-300">{zh ? "AI 全自动聊天 · 无界科技" : "AI auto-chat · BOUNDLESS"}</div>
        </div>
        <button onClick={() => setLang(zh ? "en" : "zh")} className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300">
          {zh ? "EN" : "中文"}
        </button>
      </header>

      {/* hero */}
      <section className="mt-4 rounded-2xl border border-growth-400/30 bg-gradient-to-br from-growth-500/15 via-slate-900 to-slate-900 p-4">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-growth-400/30 bg-growth-500/10 px-2.5 py-0.5 text-[11px] font-medium text-growth-300">
          {zh ? "Windows · 免费下载 · 无需 API Key" : "Windows · free download · no API key"}
        </span>
        <h1 className="mt-2 text-2xl font-extrabold leading-tight text-white">{zh ? "AI 全自动聊天" : "AI auto-chat"}</h1>
        <p className="mt-1.5 text-sm leading-relaxed text-slate-300">
          {zh
            ? "客户发来消息，AI 按你的话术自动回复，24 小时自动跟进、自动成交；外语客户自动互译。"
            : "When a customer messages you, AI replies in your own voice, follows up and closes 24/7 — with live translation."}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(zh ? ["全自动", "人审放行", "仅拟稿"] : ["Fully automatic", "Human-approved", "Draft-only"]).map((c) => (
            <span key={c} className="rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-growth-300">
              {c}
            </span>
          ))}
        </div>
        <button
          onClick={primary.action}
          className="mt-4 block w-full rounded-xl bg-growth-600 py-3 text-center text-sm font-semibold text-white transition active:scale-[0.99]"
        >
          {primary.text}
        </button>
        {desktop ? (
          <p className="mt-2 text-center text-[11px] text-slate-500">{zh ? "Windows 10/11 · 安装约 1 分钟" : "Windows 10/11 · installs in about a minute"}</p>
        ) : (
          <button onClick={goDownload} className="mt-2 block w-full text-center text-[11px] text-slate-400 underline-offset-2 hover:underline">
            {zh ? "手机先看下载页 →" : "Open the download page here →"}
          </button>
        )}
      </section>

      {/* live demo: 真 AI 对话 */}
      <div className="mt-4">
        <AiChat
          key={`${lang}:${tgName}`}
          t={t}
          zh={zh}
          scene={{ name: "chatx", src }}
          greeting={
            zh
              ? `你好${tgName ? ` ${tgName}` : ""}，我是小界。ChatX 装到 Windows 电脑上，你的客户发来消息，AI 会按你的话术自动回、自动跟进成交。想了解哪一块？`
              : `Hi${tgName ? ` ${tgName}` : ""}, I'm Jie. Install ChatX on your Windows PC and AI answers your customers in your own words, follows up and closes for you. What would you like to know?`
          }
          linkAction={aiLinkAction}
          accent="growth"
          title={zh ? "现在就试：你说一句，AI 秒回" : "Try it now: say something, AI replies"}
          examples={zh ? "例如：能接 WhatsApp 吗？怎么让 AI 按我的话术回？要花钱吗？" : "e.g. Does it work with WhatsApp? How does it learn my script? Is it free?"}
        />
        <p className="mt-1.5 px-1 text-[11px] text-slate-500">{zh ? "👆 你现在聊的，就是 ChatX 给你客户用的那套 AI 自动回复。" : "👆 This is the same AI auto-reply ChatX runs for your customers."}</p>
      </div>

      {/* 效果剧场（官网小程序同款） */}
      <SectionTitle icon="💬" title={zh ? "客户视角：AI 是这样回的" : "What your customer sees"} sub={zh ? "外语进、你的话术出，还能用你的声音发语音" : "Foreign language in, your script out — even as a voice note in your voice"} />
      <ChatTheater t={t} />

      {/* 能做什么 */}
      <SectionTitle icon="✨" title={zh ? "能做什么" : "What it does"} />
      <div className="grid grid-cols-2 gap-2">
        {FEATURES.map((f) => {
          const [title, desc] = f[lang];
          return (
            <div key={title} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
              <div className="text-sm font-semibold text-white">
                {f.icon} {title}
              </div>
              <div className="mt-1 text-[11px] leading-relaxed text-slate-400">{desc}</div>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {t.trust.platformsLive.map((p) => (
          <span key={p.name} className="rounded-lg border border-slate-700 bg-slate-900/60 px-2.5 py-1 text-[11px] text-slate-300">
            {p.label ?? p.name}
          </span>
        ))}
      </div>

      {/* 教程 */}
      <SectionTitle
        icon="📖"
        title={zh ? `${mins} 分钟学会 · ${CHATX_TUTORIAL_COUNT} 集视频教程` : `Learn in ${mins} min · ${CHATX_TUTORIAL_COUNT} short videos`}
        sub={zh ? "先装（1 分钟），再看接平台、喂话术、开自动回复" : "Install first (1 min), then connect platforms, feed your script, switch on auto-reply"}
      />
      <div className="space-y-2">
        {episodes.map((e) => (
          <button
            key={e.id}
            onClick={() => {
              haptic("light");
              track("miniapp_cta", { view: "chatx", interest: `tutorial_${e.id}` });
              const u = new URL(tutorialsUrl);
              u.searchParams.set("ep", e.id);
              openLink(u.toString());
            }}
            className="flex w-full items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-2.5 text-left transition active:scale-[0.99]"
          >
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-growth-500/15 text-xs font-bold text-growth-300">{e.ep === 0 ? "T1" : `E${e.ep}`}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-white">{e.title[lang]}</span>
              <span className="block truncate text-[11px] text-slate-400">{e.feature[lang]}</span>
            </span>
            <span className="shrink-0 text-[11px] text-slate-500">{fmtDuration(e.durationSec)}</span>
          </button>
        ))}
      </div>
      <button onClick={goTutorials} className="mt-2 block w-full rounded-xl border border-growth-400/30 py-2.5 text-center text-sm font-medium text-growth-300 transition active:scale-[0.99]">
        {zh ? `看全部 ${CHATX_TUTORIAL_COUNT} 集 →` : `All ${CHATX_TUTORIAL_COUNT} episodes →`}
      </button>

      {/* 留资 + 人工 */}
      <div className="mt-6">
        <LeadForm
          t={t}
          zh={zh}
          presetInterest={zh ? "智聊 ChatX 下载咨询" : "ChatX download inquiry"}
          view="chatx"
          utm={`telegram/chatx_miniapp/${src || "organic"}`}
          name={leadName}
          setName={setLeadName}
          contact={leadContact}
          setContact={setLeadContact}
        />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        <button onClick={goContact} className="rounded-xl border border-slate-700 py-2.5 text-sm text-slate-200 transition active:scale-[0.99]">
          👤 {zh ? "人工客服" : "Human support"}
        </button>
        <button
          onClick={() => {
            haptic("light");
            track("miniapp_cta", { view: "chatx", interest: "group" });
            openLink(GROUP_URL);
          }}
          className="rounded-xl border border-slate-700 py-2.5 text-sm text-slate-200 transition active:scale-[0.99]"
        >
          💬 {zh ? "加入交流群" : "Join the community"}
        </button>
      </div>
      <p className="mt-6 text-center text-[10px] text-slate-600">{zh ? "智聊 ChatX · 无界科技 BOUNDLESS" : "ChatX · BOUNDLESS"}</p>
    </main>
  );
}
