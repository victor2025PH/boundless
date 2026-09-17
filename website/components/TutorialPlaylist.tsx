"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Check, Download, ListVideo, MessageCircle, Mic2, MonitorPlay, Play, SkipForward, X } from "lucide-react";
import { track } from "@/lib/track";
import { GROUP_URL } from "@/lib/site";
import { getLocal, setLocal } from "@/lib/safe-storage";
import {
  CHATX_TUTORIALS,
  CHATX_TUTORIAL_COUNT,
  CHATX_TUTORIAL_TOTAL_SEC,
  fmtDuration,
  tutorialUtm,
  type TutorialEpisode,
  type TutorialLang,
} from "@/lib/chatx-tutorials";

/** 每集观看进度（0..100，取最大值）持久化键 */
const LS_KEY = "bl-chatx-tut-progress";
/** 自动连播倒计时（秒） */
const AUTO_NEXT_S = 4;
/** ≥ 该进度视为「已看」 */
const WATCHED_PCT = 90;

type Progress = Record<string, number>;

function readProgress(): Progress {
  try {
    const raw = getLocal(LS_KEY);
    const parsed = raw ? (JSON.parse(raw) as Progress) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function epBadge(e: TutorialEpisode, zh: boolean): string {
  if (e.ep === 0) return zh ? "装" : "0";
  return String(e.ep).padStart(2, "0");
}

/**
 * 智聊教程播放列表：主播放器 + 剧集列表（YouTube playlist 模式），连播 / ?ep= 深链 / 本机进度 / 下载 CTA / 埋点。
 * lang 由路由显式传入（/chatx/tutorials = zh，/en/chatx/tutorials = en），不读浏览器偏好，保证 URL 与正文语言一致。
 * 视频只有一个 <video> 节点，切集换 src；全局 VideoSpotlight 会在播放时自动压暗背景动效。
 */
export default function TutorialPlaylist({ lang }: { lang: TutorialLang }) {
  const zh = lang === "zh";
  const videoRef = useRef<HTMLVideoElement>(null);
  const [idx, setIdx] = useState(() => Math.max(0, CHATX_TUTORIALS.findIndex((e) => e.ep === 1)));
  const [progress, setProgress] = useState<Progress>({});
  const [countdown, setCountdown] = useState<number | null>(null);
  const [finished, setFinished] = useState(false);
  const autoplayNext = useRef(false);
  /** 用户切过集 / 带 ?ep= 进来之后才把集号写回 URL（首次落地不改地址，保持 canonical 干净） */
  const syncUrl = useRef(false);
  const playedRef = useRef<Set<string>>(new Set());
  const quartileRef = useRef<Record<string, number>>({});

  const cur = CHATX_TUTORIALS[idx];
  const next = CHATX_TUTORIALS[idx + 1];
  const watchedCount = useMemo(
    () => CHATX_TUTORIALS.filter((e) => (progress[e.id] ?? 0) >= WATCHED_PCT).length,
    [progress],
  );

  // 首帧后：读本机进度；?ep= 深链优先，其次「继续观看」（第一集未看完的），否则 E1。
  useEffect(() => {
    const p = readProgress();
    setProgress(p);
    const m = window.location.search.match(/[?&]ep=([A-Za-z]\d{1,2})/);
    const deep = m ? CHATX_TUTORIALS.findIndex((e) => e.id === m[1].toUpperCase()) : -1;
    if (deep >= 0) {
      syncUrl.current = true;
      setIdx(deep);
      track("tutorial_deeplink", { ep: CHATX_TUTORIALS[deep].id, lang });
      return;
    }
    const hasAny = Object.keys(p).length > 0;
    if (hasAny) {
      const resume = CHATX_TUTORIALS.findIndex((e) => e.ep > 0 && (p[e.id] ?? 0) < WATCHED_PCT);
      if (resume >= 0) setIdx(resume);
    }
  }, [lang]);

  // 切集：同步 URL、重载视频；来自连播/点击「下一集」时自动开播（用户已与页面交互，浏览器允许）。
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    if (syncUrl.current) {
      const url = new URL(window.location.href);
      url.searchParams.set("ep", cur.id);
      window.history.replaceState(null, "", url.pathname + url.search);
    }
    el.load();
    if (autoplayNext.current) {
      autoplayNext.current = false;
      void el.play().catch(() => {});
    }
  }, [cur]);

  // 连播倒计时
  useEffect(() => {
    if (countdown === null) return;
    if (countdown <= 0) {
      setCountdown(null);
      if (next) {
        autoplayNext.current = true;
        syncUrl.current = true;
        track("tutorial_next", { ep: cur.id, to: next.id, auto: true, lang });
        setIdx(idx + 1);
      }
      return;
    }
    const t = setTimeout(() => setCountdown((c) => (c === null ? null : c - 1)), 1000);
    return () => clearTimeout(t);
  }, [countdown, next, idx, cur.id, lang]);

  const savePct = useCallback((id: string, pct: number) => {
    setProgress((prev) => {
      if ((prev[id] ?? 0) >= pct) return prev;
      const merged = { ...prev, [id]: pct };
      setLocal(LS_KEY, JSON.stringify(merged));
      return merged;
    });
  }, []);

  const select = (i: number, where: string) => {
    if (i === idx) {
      void videoRef.current?.play().catch(() => {});
      return;
    }
    setCountdown(null);
    setFinished(false);
    autoplayNext.current = true;
    syncUrl.current = true;
    track("tutorial_select", { ep: CHATX_TUTORIALS[i].id, where, lang });
    setIdx(i);
  };

  const onPlay = () => {
    setFinished(false);
    if (playedRef.current.has(cur.id)) return;
    playedRef.current.add(cur.id);
    track("tutorial_play", { ep: cur.id, lang });
  };

  const onTimeUpdate = () => {
    const el = videoRef.current;
    if (!el || !el.duration || !playedRef.current.has(cur.id)) return;
    const pct = Math.min(100, Math.floor((el.currentTime / el.duration) * 100));
    if (pct % 5 === 0) savePct(cur.id, pct);
    const q = Math.floor(pct / 25);
    if (q > (quartileRef.current[cur.id] ?? 0) && q < 4) {
      quartileRef.current[cur.id] = q;
      track("tutorial_progress", { ep: cur.id, pct: q * 25, lang });
    }
  };

  const onEnded = () => {
    savePct(cur.id, 100);
    track("tutorial_done", { ep: cur.id, lang });
    if (next) setCountdown(AUTO_NEXT_S);
    else {
      setFinished(true);
      track("tutorial_series_done", { lang });
    }
  };

  const dlHref = tutorialUtm(cur, lang);
  const totalLabel = `${Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60)}`;

  return (
    <section className="mx-auto max-w-7xl px-5 pb-24 pt-28">
      {/* 页头 */}
      <div className="mx-auto max-w-3xl text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.3em] text-neon-cyan">
          {zh ? "智聊 CHATX · 视频教程" : "CHATX · VIDEO TUTORIALS"}
        </p>
        <h1 className="mt-3 text-3xl font-bold text-white sm:text-4xl">
          {zh
            ? `${CHATX_TUTORIAL_COUNT} 集，约 ${totalLabel} 分钟学会智聊`
            : `Learn ChatX in ${CHATX_TUTORIAL_COUNT} episodes · about ${totalLabel} minutes`}
        </h1>
        <p className="mt-4 text-sm leading-relaxed text-slate-400">
          {zh
            ? "每集一个功能闭环：进入 → 操作 → 结果，全部是生产站真机录屏。先装好，再从统一收件箱看到充值额度；看完一集自动播下一集。"
            : "One feature per episode — open it, do it, see the result — all recorded on the live product. Start with the install, then run from the unified inbox through to billing; the next episode plays automatically."}
        </p>
        <div className="mt-5 flex flex-wrap items-center justify-center gap-2 text-[11px]">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 font-medium text-neon-cyan">
            <MonitorPlay className="h-3.5 w-3.5" />
            {zh ? "真机录屏 · 非 AI 概念演示" : "Real screen recording · not an AI concept demo"}
          </span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-slate-400">
            <Mic2 className="h-3.5 w-3.5" />
            {zh ? "歌声与说唱由幻声 VoiceX 生成" : "Vocals & rap generated by VoiceX"}
          </span>
          <span className="inline-flex items-center rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-slate-400">
            {zh ? "中文字幕已烧入 · 静音也能看" : "Chinese narration · Chinese captions burned in · English version in production"}
          </span>
        </div>
      </div>

      <div className="mt-10 grid gap-6 lg:grid-cols-[3fr,2fr]">
        {/* 主播放器 */}
        <div>
          <div className="glass relative overflow-hidden rounded-2xl border border-neon-cyan/25 shadow-[0_0_50px_rgba(34,211,238,0.10)] max-lg:sticky max-lg:top-[60px] max-lg:z-10">
            <video
              ref={videoRef}
              className="aspect-video w-full bg-ink-950"
              src={cur.src}
              poster={cur.poster}
              controls
              playsInline
              preload="metadata"
              aria-label={`${cur.id} · ${cur.title[lang]}`}
              onPlay={onPlay}
              onTimeUpdate={onTimeUpdate}
              onEnded={onEnded}
            />
            <span className="pointer-events-none absolute left-3 top-3 rounded-md bg-ink-950/80 px-2 py-0.5 text-[11px] font-semibold text-neon-cyan">
              {cur.ep === 0 ? (zh ? "安装" : "Install") : `${zh ? "第" : "EP"} ${cur.ep}${zh ? " 集" : ""}`}
            </span>

            {/* 连播倒计时 */}
            {countdown !== null && next && (
              <div className="absolute inset-0 flex items-center justify-center bg-ink-950/75 p-4 backdrop-blur-sm">
                <div className="w-full max-w-sm rounded-2xl border border-white/10 bg-ink-900/90 p-5 text-center">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">
                    {zh ? `${countdown} 秒后播放下一集` : `Next episode in ${countdown}s`}
                  </p>
                  <p className="mt-2 text-base font-semibold text-white">
                    {epBadge(next, zh)} · {next.title[lang]}
                  </p>
                  <div className="mt-4 flex justify-center gap-2">
                    <button
                      onClick={() => {
                        setCountdown(null);
                        autoplayNext.current = true;
                        track("tutorial_next", { ep: cur.id, to: next.id, auto: false, lang });
                        setIdx(idx + 1);
                      }}
                      className="inline-flex items-center gap-1.5 rounded-full bg-neon-blue px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110"
                    >
                      <SkipForward className="h-3.5 w-3.5" />
                      {zh ? "立即播放" : "Play now"}
                    </button>
                    <button
                      onClick={() => setCountdown(null)}
                      className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-4 py-2 text-xs text-slate-300 transition hover:border-white/30 hover:text-white"
                    >
                      <X className="h-3.5 w-3.5" />
                      {zh ? "取消" : "Cancel"}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* 全部看完 */}
            {finished && (
              <div className="absolute inset-0 flex items-center justify-center bg-ink-950/80 p-4 backdrop-blur-sm">
                <div className="w-full max-w-md rounded-2xl border border-neon-cyan/30 bg-ink-900/90 p-6 text-center">
                  <p className="text-xs uppercase tracking-[0.2em] text-neon-cyan">{zh ? "全部看完" : "All done"}</p>
                  <p className="mt-2 text-lg font-bold text-white">
                    {zh ? "现在去装一个，免费开始" : "Now install it and start for free"}
                  </p>
                  <p className="mt-2 text-xs leading-relaxed text-slate-400">
                    {zh
                      ? "标准翻译永久免费，不用显卡、不用 API Key；装好后有问题点右下角小智球。"
                      : "Standard translation is free forever; no GPU or API key needed. Once installed, ask the Xiaozhi bubble any time."}
                  </p>
                  <div className="mt-4 flex flex-wrap justify-center gap-2">
                    <Link
                      href={dlHref}
                      onClick={() => track("tutorial_cta", { ep: cur.id, where: "series_done", lang })}
                      className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-semibold text-ink-950 transition hover:opacity-90"
                    >
                      <Download className="h-4 w-4" />
                      {zh ? "免费下载智聊" : "Download ChatX free"}
                    </Link>
                    <button
                      onClick={() => {
                        setFinished(false);
                        select(0, "series_done_replay");
                      }}
                      className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-4 py-2.5 text-sm text-slate-300 transition hover:border-white/30 hover:text-white"
                    >
                      <Play className="h-4 w-4" />
                      {zh ? "从安装集重看" : "Replay from install"}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* 当前集信息 */}
          <div className="mt-5">
            <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">
              {cur.id} · {cur.feature[lang]} · {fmtDuration(cur.durationSec)}
            </p>
            <h2 className="mt-1.5 text-xl font-bold leading-snug text-white sm:text-2xl">{cur.title[lang]}</h2>
            <p className="mt-2 text-sm leading-relaxed text-slate-400">{cur.desc[lang]}</p>
          </div>

          {/* CTA 条：教程 → 下载页（utm 归因 t-e01-zh） */}
          <div className="mt-5 flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/[0.03] p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-xs leading-relaxed text-slate-400">
              <span className="font-medium text-white">
                {zh ? "看着顺手？装一个自己点点。" : "Looks useful? Install it and click along."}
              </span>{" "}
              {zh ? "Windows 客户端 · 免费开始 · 标准翻译永久免费。" : "Windows client · free to start · standard translation free forever."}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <Link
                href={dlHref}
                onClick={() => track("tutorial_cta", { ep: cur.id, where: "player_bar", lang })}
                className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-xs font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-3.5 w-3.5" />
                {zh ? "免费下载智聊" : "Download ChatX"}
              </Link>
              <a
                href={GROUP_URL}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => track("tutorial_cta", { ep: cur.id, where: "tg_group", lang })}
                className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-4 py-2 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
              >
                <MessageCircle className="h-3.5 w-3.5" />
                {zh ? "进 TG 交流群提问" : "Ask in the Telegram group"}
              </a>
            </div>
          </div>
        </div>

        {/* 剧集列表 */}
        <aside aria-label={zh ? "播放列表" : "Playlist"}>
          <div className="glass rounded-2xl border border-white/10 p-3">
            <div className="flex items-center justify-between px-1 pb-2 pt-1">
              <div className="inline-flex items-center gap-1.5 text-sm font-semibold text-white">
                <ListVideo className="h-4 w-4 text-neon-cyan" />
                {zh ? "播放列表" : "Playlist"}
              </div>
              <span className="text-[11px] text-slate-500">
                {zh
                  ? `已看 ${watchedCount} / ${CHATX_TUTORIALS.length}`
                  : `${watchedCount} / ${CHATX_TUTORIALS.length} watched`}
              </span>
            </div>
            <ol className="flex max-h-[70vh] flex-col gap-1.5 overflow-y-auto pr-0.5">
              {CHATX_TUTORIALS.map((e, i) => {
                const active = i === idx;
                const pct = progress[e.id] ?? 0;
                const watched = pct >= WATCHED_PCT;
                return (
                  <li key={e.id}>
                    <button
                      onClick={() => select(i, "list")}
                      aria-current={active ? "true" : undefined}
                      className={`relative flex min-h-[56px] w-full items-center gap-3 overflow-hidden rounded-xl border px-2.5 py-2 text-left transition ${
                        active
                          ? "border-neon-cyan/60 bg-neon-cyan/10"
                          : "border-white/10 bg-white/[0.02] hover:border-neon-cyan/40 hover:bg-white/[0.05]"
                      }`}
                    >
                      <span
                        className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg text-xs font-bold ${
                          active ? "bg-neon-cyan text-ink-950" : "bg-neon-cyan/15 text-neon-cyan"
                        }`}
                      >
                        {epBadge(e, zh)}
                      </span>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={e.poster}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        className="hidden h-12 w-[84px] shrink-0 rounded-md border border-white/10 object-cover sm:block"
                      />
                      <span className="min-w-0 flex-1">
                        <span className={`block truncate text-sm ${active ? "font-semibold text-white" : "text-slate-200"}`}>
                          {e.title[lang]}
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-slate-500">
                          {e.feature[lang]} · {fmtDuration(e.durationSec)}
                        </span>
                      </span>
                      <span className="shrink-0 text-slate-500">
                        {watched ? (
                          <Check className="h-4 w-4 text-emerald-400" aria-label={zh ? "已看" : "watched"} />
                        ) : active ? (
                          <Play className="h-4 w-4 text-neon-cyan" />
                        ) : null}
                      </span>
                      {pct > 0 && pct < WATCHED_PCT && (
                        <span className="absolute inset-x-0 bottom-0 h-0.5 bg-white/10">
                          <span className="block h-full bg-neon-cyan/70" style={{ width: `${pct}%` }} />
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ol>
          </div>
          <p className="mt-3 px-1 text-[11px] leading-relaxed text-slate-500">
            {zh
              ? "进度只存在你这台浏览器里。教程按「功能」而非按钮位置讲，适用 1.0.8x 及以后版本。"
              : "Progress is stored only in this browser. Episodes teach by feature rather than button position; valid for 1.0.8x and later."}
          </p>
        </aside>
      </div>
    </section>
  );
}
