"use client";

import Link from "next/link";
import { useLang } from "./LanguageContext";
import { localePath } from "@/lib/site";
import { BRAND_FILM } from "@/lib/film";
import FilmPlayer from "./FilmPlayer";
import type { FeedVideo } from "@/lib/feed-store";
import { PlayCircle } from "lucide-react";
import { track } from "@/lib/track";
import { CHATX_TUTORIALS, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC } from "@/lib/chatx-tutorials";

// 视频动态列表：服务端读库后作为 props 传入（页面 force-dynamic，上架即可见）。
// 顶部固定「精选 · 品牌片」大卡（lib/film.ts 单一真相）——流按日期滚动，精选位不随流走。
// 2026-09-17：文案去掉「每日 / 每天更新」承诺（最近一条 08-19，说了做不到反伤可信度）；
// 精选卡下加「智聊视频教程」入口卡（合集自有页 /chatx/tutorials，不混进日期流）。
export default function VideoFeed({ videos }: { videos: FeedVideo[] }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const tutMin = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
  const tutPosters = CHATX_TUTORIALS.filter((e) => e.ep > 0).slice(0, 3);

  return (
    <section className="mx-auto max-w-7xl px-5 pb-24 pt-28">
      <div className="mx-auto max-w-2xl text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.3em] text-neon-cyan">
          {zh ? "视频中心" : "VIDEO HUB"}
        </p>
        <h1 className="mt-3 text-3xl font-bold text-white sm:text-4xl">
          {zh ? "品牌片 · 真机实测 · 效果演示" : "Brand film · hands-on · demos"}
        </h1>
        <p className="mt-4 text-sm leading-relaxed text-slate-400">
          {zh
            ? "换脸、克隆声音、数字人直播、克隆音同传的实测与演示。概念演示由 AI 生成并标注，真实效果以引擎实测输出为准。"
            : "Hands-on runs and demos of face swap, voice cloning, digital-human streaming and interpreting. AI-generated concept demos are labeled; real results come from actual engine output."}
        </p>
      </div>

      {/* 智聊教程入口卡 */}
      <Link
        href={localePath(lang, "/chatx/tutorials")}
        onClick={() => track("tutorial_entry_click", { where: "videos" })}
        className="glass card-hover mx-auto mt-10 flex max-w-5xl flex-col gap-4 rounded-2xl border border-neon-cyan/25 p-5 sm:flex-row sm:items-center"
      >
        <div className="flex shrink-0 -space-x-6">
          {tutPosters.map((e, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={e.id}
              src={e.poster}
              alt=""
              loading="lazy"
              className="h-[62px] w-[110px] rounded-lg border border-white/15 object-cover shadow-lg"
              style={{ zIndex: 3 - i }}
            />
          ))}
        </div>
        <div className="min-w-0 flex-1">
          <p className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.2em] text-neon-cyan">
            <PlayCircle className="h-3.5 w-3.5" />
            {zh ? "智聊 ChatX · 视频教程" : "ChatX · video tutorials"}
          </p>
          <h2 className="mt-1 text-lg font-bold text-white">
            {zh
              ? `${CHATX_TUTORIAL_COUNT} 集，约 ${tutMin} 分钟学会智聊`
              : `Learn ChatX in ${CHATX_TUTORIAL_COUNT} episodes · about ${tutMin} minutes`}
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-slate-400">
            {zh
              ? "安装 → 统一收件箱 → 接入四平台 → 互译 → AI 拟稿 → 人设 → 知识库 → 克隆语音 → 目标 → 关怀 → 小智 → 护栏 → 充值。真机录屏，看完自动播下一集。"
              : "Install → inbox → channels → translation → AI drafts → persona → knowledge → voice → goals → care → Xiaozhi → guardrails → billing. Real screen recordings, auto-plays the next episode."}
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-neon-blue px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110">
          {zh ? "开始学 →" : "Start →"}
        </span>
      </Link>

      <div className="mx-auto mt-12 max-w-5xl overflow-hidden rounded-2xl border border-emerald-300/25 bg-white/[0.03]">
        <div className="grid gap-0 lg:grid-cols-[3fr,2fr]">
          <div className="relative p-4 pb-0 lg:pb-4">
            <span className="pointer-events-none absolute left-7 top-7 z-10 rounded-full bg-emerald-400/90 px-2.5 py-0.5 text-[11px] font-semibold text-ink-950">
              {zh ? "精选 · 品牌片" : "FEATURED · BRAND FILM"}
            </span>
            <FilmPlayer lang={lang} compact />
          </div>
          <div className="flex flex-col justify-center p-6">
            <h2 className="text-lg font-bold leading-snug text-white">{BRAND_FILM.title[lang]}</h2>
            <p className="mt-2 text-xs leading-relaxed text-slate-400">{BRAND_FILM.tagline[lang]}</p>
            <Link
              href={localePath(lang, "/film")}
              className="mt-4 inline-block w-fit rounded-full bg-neon-blue px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110"
            >
              {zh
                ? `完整页 · 章节跳转（${BRAND_FILM.durationLabel.zh}）`
                : `Film page · chapters (${BRAND_FILM.durationLabel.en})`}
            </Link>
          </div>
        </div>
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
                    {/* 固定时区：客户端组件也会在服务端渲染，Node(UTC) 与浏览器(UTC+8) 跨日会 hydration 不一致 */}
                    {new Date(v.date).toLocaleDateString(zh ? "zh-CN" : "en-US", { month: "short", day: "numeric", timeZone: "Asia/Shanghai" })}
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
