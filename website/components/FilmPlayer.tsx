"use client";

import { useEffect, useRef, useState } from "react";
import { track } from "@/lib/track";
import { BRAND_FILM, type FilmLang } from "@/lib/film";

function fmt(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** 品牌片播放器：海报点播（不自动播）+ 章节跳转 + 播放深度埋点（与 /order showcase 同管道）。
 *  compact=true 用于列表卡位（隐藏章节条）。全局 VideoSpotlight 捕获 play 事件自动压暗背景。 */
export default function FilmPlayer({ lang, compact = false }: { lang: FilmLang; compact?: boolean }) {
  const zh = lang === "zh";
  const ref = useRef<HTMLVideoElement>(null);
  const [active, setActive] = useState(-1);

  const seek = (t: number, i: number) => {
    const el = ref.current;
    if (!el) return;
    el.currentTime = t;
    void el.play();
    setActive(i);
    track("film_chapter", { lang, t: Math.round(t) });
  };

  // ?t= 章节深链（产品页/TG 帖直达某一幕）。读 location 而非 useSearchParams：
  // 后者要求页面包 Suspense 边界，纯客户端读参零构建负担。不自动播（浏览器策略+礼貌），
  // 定位到该幕等用户点播；preload=none 时先要元数据否则 currentTime 会被吞。
  useEffect(() => {
    if (compact) return;
    const m = window.location.search.match(/[?&]t=(\d+(?:\.\d+)?)/);
    if (!m) return;
    const t = Number(m[1]);
    const el = ref.current;
    if (!el || !Number.isFinite(t) || t <= 0 || t >= BRAND_FILM.durationSec[lang]) return;
    const apply = () => {
      el.currentTime = t;
    };
    el.preload = "metadata";
    if (el.readyState >= 1) apply();
    else el.addEventListener("loadedmetadata", apply, { once: true });
    el.load();
    let best = -1;
    BRAND_FILM.chapters.forEach((c, i) => {
      if (c.t[lang] <= t + 0.01) best = i;
    });
    setActive(best);
    track("film_deeplink", { lang, t: Math.round(t) });
  }, [lang, compact]);

  return (
    <div>
      <div className="glass overflow-hidden rounded-2xl border border-neon-cyan/25 shadow-[0_0_50px_rgba(34,211,238,0.10)]">
        <video
          ref={ref}
          controls
          playsInline
          preload="none"
          poster={BRAND_FILM.poster[lang]}
          src={BRAND_FILM.src[lang]}
          className="aspect-video w-full bg-ink-950"
          onPlay={(e) => {
            const el = e.currentTarget;
            if (el.dataset.played) return;
            el.dataset.played = "1";
            track("film_play", { lang });
          }}
          onTimeUpdate={(e) => {
            const el = e.currentTarget;
            // 深链 seek 也会触发 timeupdate：未真实开播不记进度（实测污染过一条 25% 事件）
            if (!el.dataset.played || !el.duration) return;
            const q = Math.floor((el.currentTime / el.duration) * 4);
            const prev = Number(el.dataset.q || 0);
            if (q > prev && q < 4) {
              el.dataset.q = String(q);
              track("film_progress", { lang, pct: q * 25 });
            }
          }}
          onEnded={() => track("film_done", { lang })}
        />
      </div>
      {!compact && (
        <div className="mt-4 flex flex-wrap gap-2">
          {BRAND_FILM.chapters.map((c, i) => (
            <button
              key={c.label.en}
              onClick={() => seek(c.t[lang], i)}
              className={`rounded-full border px-3 py-1.5 text-xs transition ${
                active === i
                  ? "border-neon-cyan/60 bg-neon-cyan/10 text-neon-cyan"
                  : "border-white/10 bg-white/[0.03] text-slate-300 hover:border-neon-cyan/40 hover:text-white"
              }`}
            >
              {fmt(c.t[lang])} · {c.label[lang]}
            </button>
          ))}
        </div>
      )}
      <p className="mt-3 text-xs font-medium text-emerald-300/90">
        ✓ {BRAND_FILM.badgeNote[lang]}
        {!compact && (
          <span className="ml-2 text-slate-500">
            {zh ? "本片含中文字幕。" : "English captions burned in."}
          </span>
        )}
      </p>
    </div>
  );
}
