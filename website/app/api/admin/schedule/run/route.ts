import { NextRequest, NextResponse } from "next/server";
import { runDuePosts } from "@/lib/schedule-store";
import { claimDueReminders } from "@/lib/dragon-store";
import { sendText } from "@/lib/telegram-bot";
import { SITE_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const REMIND_HOUR = Number(process.env.DRAGON_REMIND_HOUR ?? 19);
const TZ_MS = Number(process.env.TZ_OFFSET ?? 8) * 3600 * 1000;

/** 龙珠每日提醒：仅在设定小时派发；claimDueReminders 内置当日去重（乐观认领） */
async function runDragonReminders(): Promise<number> {
  const hour = new Date(Date.now() + TZ_MS).getUTCHours();
  if (hour !== REMIND_HOUR) return 0;
  const due = await claimDueReminders();
  let sent = 0;
  for (const d of due) {
    const bar = `${"●".repeat(d.collected)}${"○".repeat(Math.max(0, 7 - d.collected))}`;
    const r = await sendText(
      d.userId,
      `🌟 今日星珠还没点亮 ${bar}（${d.collected}/7）\n发 /xingzhu 收下今天这颗，或回官网找 EVE 领取。`,
      [[{ text: "🐉 去官网收珠", url: `${SITE_URL}/?utm_source=telegram&utm_medium=bot&utm_campaign=xz-remind` }]]
    ).catch(() => null);
    if (r) sent += 1;
  }
  return sent;
}

/** Manual / external-cron trigger：定时贴发送 + 龙珠每日提醒（同一 cron 复用） */
export async function POST(req: NextRequest) {
  const key = process.env.TELEGRAM_SETUP_KEY;
  if (!key) return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  const given = req.headers.get("x-setup-key") || req.nextUrl.searchParams.get("key");
  if (given !== key) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  const r = await runDuePosts();
  const reminded = await runDragonReminders().catch(() => 0);
  return NextResponse.json({ ok: true, ...r, reminded });
}
