import { NextResponse } from "next/server";
import { TELEGRAM_CHANNEL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 官方频道公开预览页(t.me/s/<channel>)自动拉取：让首页「每日真实案例」随频道更新而更新。
 *  15 分钟内存缓存；拉取失败回退陈旧缓存；解析为纯文本（不透传 HTML，杜绝注入）。 */

interface FeedPost {
  id: string;
  url: string;
  text: string;
  date: string;
  photo?: string;
}

let cache: { ts: number; posts: FeedPost[] } | null = null;
const TTL_MS = 15 * 60 * 1000;
const MAX_POSTS = 6;

function decodeEntities(s: string): string {
  return s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

function parsePosts(html: string): FeedPost[] {
  const posts: FeedPost[] = [];
  const blocks = html.split("tgme_widget_message_wrap").slice(1);
  for (const b of blocks) {
    const id = b.match(/data-post="([^"]+)"/)?.[1];
    if (!id) continue;
    if (b.includes("service_message")) continue; // 置顶/建频道等服务消息不算案例
    const rawText = b.match(/tgme_widget_message_text[^>]*>([\s\S]*?)<\/div>/)?.[1] ?? "";
    const text = decodeEntities(
      rawText
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<[^>]+>/g, "")
    )
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    const date = b.match(/<time datetime="([^"]+)"/)?.[1] ?? "";
    const photo = b.match(/message_photo_wrap[^>]*background-image:url\('([^']+)'\)/)?.[1];
    if (!text && !photo) continue; // 服务消息（建频道等）不展示
    posts.push({ id, url: `https://t.me/${id}`, text, date, ...(photo ? { photo } : {}) });
  }
  // 预览页按时间正序排列，翻转为最新在前
  return posts.reverse().slice(0, MAX_POSTS);
}

export async function GET() {
  const now = Date.now();
  if (cache && now - cache.ts < TTL_MS) {
    return NextResponse.json({ ok: true, posts: cache.posts, cached: true });
  }
  try {
    const res = await fetch(`https://t.me/s/${TELEGRAM_CHANNEL}`, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; HualingSite/1.0)" },
      signal: AbortSignal.timeout(8000),
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`t.me HTTP ${res.status}`);
    const posts = parsePosts(await res.text());
    cache = { ts: now, posts };
    return NextResponse.json({ ok: true, posts });
  } catch {
    // 拉取失败：有陈旧缓存用陈旧缓存，没有则空列表（前端优雅隐藏）
    if (cache) return NextResponse.json({ ok: true, posts: cache.posts, stale: true });
    return NextResponse.json({ ok: false, posts: [] });
  }
}
