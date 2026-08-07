"use client";

import { useEffect, useState } from "react";
import { ArrowDown, ArrowRight, Download, ShieldCheck } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import ProductIcon from "./ProductIcon";
import BrandMark from "./BrandMark";
import { CLIENT_APPS, PLATFORM_LABEL, parseLatestYml, formatMb, type ClientApp } from "@/lib/downloads";
import { CATEGORIES } from "@/lib/brand";
import { localePath } from "@/lib/site";
import { track } from "@/lib/track";

/** 运行时清单校正：发布脚本更新服务器 manifest 后，卡片版本/大小即时跟上，
 *  不等 releaseNotes/chatxContent 构建常量改版（防止与页内详情区并排出现两个版本号）。 */
type LiveMeta = { version?: string; size?: string };

function useLiveVersions(): Record<string, LiveMeta> {
  const [live, setLive] = useState<Record<string, LiveMeta>>({});
  useEffect(() => {
    fetch("/releases/release_manifest.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        const b = j?.builds?.find((x: { ready?: boolean }) => x?.ready) ?? j?.builds?.[0];
        if (b?.ver) setLive((o) => ({ ...o, avatarhub: { version: b.ver, size: b.size || undefined } }));
      })
      .catch(() => {});
    fetch("/downloads/manifest.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j?.version) setLive((o) => ({ ...o, chatx: { version: j.version, size: j.size_mb ? `${j.size_mb} MB` : undefined } }));
      })
      .catch(() => {});
    // MatrixX 复用 electron-updater 的 latest.yml（发布脚本必产物，天然免维护）
    fetch("/releases/matrixx/latest.yml")
      .then((r) => (r.ok ? r.text() : null))
      .then((t) => {
        const m = t ? parseLatestYml(t) : null;
        if (m) setLive((o) => ({ ...o, matrixx: { version: m.version, size: formatMb(m.sizeBytes) || undefined } }));
      })
      .catch(() => {});
  }, []);
  return live;
}

/** 三系 accent → 静态 Tailwind 类（不可动态拼接，故写全映射） */
const ACCENT: Record<string, { chip: string; icon: string; hover: string }> = {
  cyan: {
    chip: "border-neon-cyan/30 bg-neon-cyan/10 text-neon-cyan",
    icon: "text-neon-cyan",
    hover: "hover:border-neon-cyan/40",
  },
  violet: {
    chip: "border-neon-violet/35 bg-neon-violet/10 text-violet-300",
    icon: "text-neon-violet",
    hover: "hover:border-neon-violet/45",
  },
  amber: {
    chip: "border-amber-400/35 bg-amber-400/10 text-amber-300",
    icon: "text-amber-300",
    hover: "hover:border-amber-400/45",
  },
};

/**
 * 下载中心首屏：全部桌面客户端一览。
 * 挂在 /download 页顶部（幻境 STUDIO 详情区之上）；幻境 STUDIO 卡片就地锚点下滑，
 * 其余客户端跳各自下载页。gated 客户端（isolation.ts 裁定）链接 nofollow，
 * 文案用注册表里的中性口径。
 */
export default function DownloadHub() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const live = useLiveVersions();

  return (
    <section className="relative pt-32">
      <div className="pointer-events-none absolute left-1/4 top-20 h-72 w-72 rounded-full bg-neon-cyan/10 blur-[120px]" />
      <div className="pointer-events-none absolute right-1/4 top-40 h-72 w-72 rounded-full bg-neon-violet/10 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <ShieldCheck className="h-3.5 w-3.5" />
            {zh
              ? "官方下载 · SHA-256 可校验 · 本地部署数据不出机"
              : "Official downloads · SHA-256 verifiable · local-first, data stays on-device"}
          </span>
          <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
            {zh ? "下载中心" : "Download Center"}
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "一处获取全部桌面客户端：选择你的产品，Windows 安装包即点即下。"
              : "Every desktop client in one place — pick your product and download for Windows."}
          </p>
        </Reveal>

        <div className="mt-10 grid gap-5 md:grid-cols-3">
          {CLIENT_APPS.map((app, i) => (
            <Reveal key={app.key} delay={i * 0.08}>
              <ClientCard app={app} zh={zh} live={live[app.key]} />
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

function ClientCard({ app, zh, live }: { app: ClientApp; zh: boolean; live?: LiveMeta }) {
  const lang = zh ? "zh" : "en";
  const accent = ACCENT[CATEGORIES[app.family].accent] ?? ACCENT.cyan;
  const version = live?.version || app.version;
  const sizeLabel = live?.size || app.sizeLabel[lang];
  // 幻境 STUDIO 的详情就在本页下方：锚点下滑而非跳页，少一次导航。
  const inPage = app.page === "/download";
  const href = inPage ? "#avatarhub" : localePath(lang, app.page);

  return (
    <a
      href={href}
      rel={app.gated ? "nofollow" : undefined}
      onClick={() => track("download_hub_click", { client: app.key })}
      className={`glass card-hover group flex h-full flex-col rounded-2xl border border-white/10 p-5 transition ${accent.hover}`}
    >
      <div className="flex items-center gap-3">
        {app.productIcon ? (
          <ProductIcon product={app.productIcon} size={40} alt="" className="h-10 w-10 object-contain" />
        ) : app.iconSrc ? (
          // eslint-disable-next-line @next/next/no-img-element -- 客户端级图标，静态 PNG 即可
          <img src={app.iconSrc} alt="" width={40} height={40} className="h-10 w-10 object-contain" draggable={false} />
        ) : (
          <BrandMark className="h-10 w-10" />
        )}
        <div className="min-w-0">
          <div className="truncate font-semibold text-white">
            {app.name[lang]}
            <span className="ml-1.5 text-xs font-normal text-slate-500">{app.subName}</span>
          </div>
          <div className={`mt-0.5 inline-flex rounded-full border px-2 py-px text-[10px] ${accent.chip}`}>
            {zh ? CATEGORIES[app.family].zh : CATEGORIES[app.family].en}
          </div>
        </div>
      </div>

      <p className="mt-3 flex-1 text-xs leading-relaxed text-slate-400">{app.tagline[lang]}</p>

      <div className="mt-4 flex flex-wrap items-center gap-1.5 text-[11px]">
        <span
          className={`rounded-full border px-2 py-0.5 ${
            app.platforms.windows === "available"
              ? "border-emerald-400/35 bg-emerald-400/10 text-emerald-300"
              : "border-white/10 text-slate-500"
          }`}
        >
          Windows · {PLATFORM_LABEL[app.platforms.windows][lang]}
        </span>
        <span className="rounded-full border border-white/10 px-2 py-0.5 text-slate-500">
          macOS · {PLATFORM_LABEL[app.platforms.macos][lang]}
        </span>
      </div>

      <div className="mt-4 flex items-center justify-between border-t border-white/5 pt-3.5">
        <span className="font-mono text-xs text-slate-500">
          v{version} · {sizeLabel}
        </span>
        <span className={`inline-flex items-center gap-1 text-xs font-medium ${accent.icon}`}>
          {inPage ? (
            <>
              {zh ? "本页下方" : "On this page"}
              <ArrowDown className="h-3.5 w-3.5 transition group-hover:translate-y-0.5" />
            </>
          ) : (
            <>
              <Download className="h-3.5 w-3.5" />
              {zh ? "前往下载" : "Get it"}
              <ArrowRight className="h-3.5 w-3.5 transition group-hover:translate-x-0.5" />
            </>
          )}
        </span>
      </div>
    </a>
  );
}
