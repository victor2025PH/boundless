"use client";

import { useLang } from "./LanguageContext";
import type { FeedVideo } from "@/lib/feed-store";

// 每日视频动态列表：服务端读库后作为 props 传入（页面 force-dynamic，上架即可见）。
export default function VideoFeed({ videos }: { videos: FeedVideo[] }) {
  const { lang } = useLang();
  const zh = lang === "zh";

  return (
    <section className="mx-auto max-w-7xl px-5 pb-24 pt-28">
      <div className="mx-auto max-w-2xl text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.3em] text-neon-cyan">
          {zh ? "视频动态" : "VIDEO FEED"}
        </p>
        <h1 className="mt-3 text-3xl font-bold text-white sm:text-4xl">
          {zh ? "每日效果演示" : "Daily demo drops"}
        </h1>
        <p className="mt-4 text-sm leading-relaxed text-slate-400">
          {zh
            ? "换脸、克隆声音、数字人直播、克隆音同传——每天更新一条演示。概念演示由 AI 生成，真实效果以引擎实测输出为准。"
            : "Face swap, voice cloning, digital-human streaming and interpreting — one new demo every day. Concept demos are AI-generated; real results come from actual engine output."}
        </p>
      </div>

      {videos.length === 0 ? (
        <p className="mt-16 text-center text-sm text-slate-500">
          {zh ? "内容准备中，今天晚些时候回来看看。" : "Content is on the way — check back later today."}
        </p>
      ) : (
        <div className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {videos.map((v) => (
            <article
              key={v.id}
              className="group overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] transition hover:border-neon-cyan/40"
            >
              <div className="relative aspect-video bg-black">
                <video
                  className="h-full w-full object-contain"
                  src={v.src}
                  poster={v.poster}
                  controls
                  preload="none"
                  playsInline
                />
                {v.ai !== false && (
                  <span className="pointer-events-none absolute left-2 top-2 rounded bg-black/70 px-2 py-0.5 text-[10px] font-medium text-amber-300">
                    {zh ? "AI 概念演示" : "AI concept demo"}
                  </span>
                )}
              </div>
              <div className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="text-sm font-semibold text-white">{zh ? v.title.zh : v.title.en}</h2>
                  <time className="shrink-0 text-[11px] text-slate-500">
                    {new Date(v.date).toLocaleDateString(zh ? "zh-CN" : "en-US", { month: "short", day: "numeric" })}
                  </time>
                </div>
                <p className="mt-2 text-xs leading-relaxed text-slate-400">{zh ? v.desc.zh : v.desc.en}</p>
                {v.youtube && (
                  <a
                    href={`https://youtu.be/${v.youtube}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-3 inline-flex items-center gap-1 text-xs text-slate-500 transition hover:text-neon-cyan"
                  >
                    ▶ {zh ? "在 YouTube 观看" : "Watch on YouTube"}
                  </a>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
