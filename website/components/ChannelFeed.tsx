"use client";

import { useEffect, useRef, useState } from "react";
import { Send, ArrowUpRight } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { ENGINE } from "@/lib/engineContent";
import { CHANNEL_URL } from "@/lib/site";
import { track } from "@/lib/track";

interface FeedPost {
  id: string;
  url: string;
  text: string;
  date: string;
  photo?: string;
}

/** 官方频道最新案例流：进入视口才拉取（省流量），拿不到内容时整块优雅隐藏。 */
export default function ChannelFeed() {
  const { lang } = useLang();
  const p = ENGINE.proof;
  const [posts, setPosts] = useState<FeedPost[] | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const fetched = useRef(false);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const load = () => {
      if (fetched.current) return;
      fetched.current = true;
      fetch("/api/channel-feed")
        .then((r) => r.json())
        .then((d) => setPosts(Array.isArray(d.posts) ? d.posts.slice(0, 3) : []))
        .catch(() => setPosts([]));
    };
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          load();
          io.disconnect();
        }
      },
      { rootMargin: "400px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const fmtDate = (iso: string) => {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleDateString(lang === "zh" ? "zh-CN" : "en-US", {
        month: "short",
        day: "numeric",
      });
    } catch {
      return "";
    }
  };

  // 占位元素始终渲染（触发视口加载）；无内容时不占空间
  if (posts !== null && posts.length === 0) return <div ref={rootRef} />;

  return (
    <div ref={rootRef} className="mt-16">
      <Reveal className="mb-8 text-center">
        <h3 className="text-2xl font-bold text-white">{p.feedTitle[lang]}</h3>
        <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{p.feedDesc[lang]}</p>
      </Reveal>

      {posts === null ? (
        // 骨架屏：等待视口内首次拉取
        <div className="grid gap-4 md:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-44 animate-pulse rounded-2xl border border-white/5 bg-ink-900/50" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-3">
          {posts.map((post, i) => (
            <Reveal key={post.id} delay={i * 0.05}>
              <a
                href={post.url}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("feed_post_click", { id: post.id })}
                className="group flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/50 transition hover:border-neon-cyan/35 hover:bg-ink-900/80"
              >
                {post.photo && (
                  // Telegram CDN 外链缩略图：referrerPolicy 防泄源，懒加载
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={post.photo}
                    alt=""
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className="h-36 w-full object-cover"
                  />
                )}
                <div className="flex flex-1 flex-col p-4">
                  <p className="flex-1 whitespace-pre-line text-sm leading-relaxed text-slate-300 line-clamp-5">
                    {post.text || (lang === "zh" ? "查看图文案例" : "View the case post")}
                  </p>
                  <div className="mt-3 flex items-center justify-between border-t border-white/5 pt-3">
                    <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                      <Send className="h-3 w-3 text-neon-cyan/70" />
                      {fmtDate(post.date)}
                    </span>
                    <span className="inline-flex items-center gap-1 text-xs font-medium text-neon-cyan opacity-0 transition group-hover:opacity-100">
                      {lang === "zh" ? "在 Telegram 查看" : "Open in Telegram"}
                      <ArrowUpRight className="h-3 w-3" />
                    </span>
                  </div>
                </div>
              </a>
            </Reveal>
          ))}
        </div>
      )}

      <Reveal delay={0.1} className="mt-6 text-center">
        <a
          href={CHANNEL_URL}
          target="_blank"
          rel="noreferrer"
          onClick={() => track("cta_click", { where: "feed_subscribe" })}
          className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-2.5 text-sm font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
        >
          <Send className="h-4 w-4" />
          {p.feedCta[lang]}
        </a>
      </Reveal>
    </div>
  );
}
