import { NextRequest, NextResponse } from "next/server";
import { SITE_URL } from "@/lib/site";
import { trackServer } from "@/lib/tg-events";
import { buttonSrc, listPosts, postTargetUrl, recordPostClick, seriesOf } from "@/lib/tg-posts";

/** 定时帖按钮跳转：计一次点击（帖子计数 + events.jsonl 的 tg_post_click），再 302 到按钮链接（t.me 深链附带帖子号，供 bot /start 归因）。爬虫 / 链接预览不计数。 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BOT_UA = /bot|crawler|spider|preview|curl|wget/i;

export async function GET(req: NextRequest, { params }: { params: { id: string } }) {
  if (!/^\d{1,9}$/.test(params.id)) return NextResponse.redirect(SITE_URL, 302);
  const id = Number(params.id);
  const ua = req.headers.get("user-agent") ?? "";
  const counted = !BOT_UA.test(ua);
  const post = counted ? await recordPostClick(id) : ((await listPosts()).find((p) => p.id === id) ?? null);
  const url = post ? postTargetUrl(post) : undefined;
  if (!post || !url) return NextResponse.redirect(SITE_URL, 302);
  if (counted) await trackServer("tg_post_click", { post: post.id, series: seriesOf(post), chat: post.chatId, bot: post.botId, src: buttonSrc(post.button!.url) }, `/r/p/${post.id}`, ua.slice(0, 200));
  return NextResponse.redirect(url, 302);
}
