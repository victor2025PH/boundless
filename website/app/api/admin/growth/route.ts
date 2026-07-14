import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { ANALYTICS_DIR } from "@/lib/data-dir";
import { TELEGRAM_CHANNEL, TELEGRAM_GROUP } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 频道/群订阅数每日快照（cron 触发 POST）。
 *  官网带来的关注沉淀在频道里，这条曲线是「内容 → 粉丝」的增长面板数据源。 */

const LOG = path.join(ANALYTICS_DIR, "growth.jsonl");

async function memberCount(token: string, chat: string): Promise<number | null> {
  try {
    const res = await fetch(
      `https://api.telegram.org/bot${token}/getChatMemberCount?chat_id=${encodeURIComponent(chat)}`,
      { signal: AbortSignal.timeout(8000) }
    );
    const data = await res.json();
    return data?.ok ? Number(data.result) : null;
  } catch {
    return null;
  }
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return NextResponse.json({ ok: false, error: "no_bot_token" }, { status: 503 });

  const [channel, group] = await Promise.all([
    memberCount(token, `@${TELEGRAM_CHANNEL}`),
    memberCount(token, `@${TELEGRAM_GROUP}`),
  ]);
  if (channel === null && group === null) {
    return NextResponse.json({ ok: false, error: "tg_unreachable" }, { status: 502 });
  }

  const rec = { t: new Date().toISOString(), channel, group };
  await mkdir(path.dirname(LOG), { recursive: true });
  await appendFile(LOG, JSON.stringify(rec) + "\n");
  return NextResponse.json({ ok: true, ...rec });
}

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  try {
    const raw = await readFile(LOG, "utf-8");
    const rows = raw
      .split("\n")
      .filter(Boolean)
      .slice(-90)
      .map((l) => {
        try {
          return JSON.parse(l);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
    return NextResponse.json({ ok: true, rows });
  } catch {
    return NextResponse.json({ ok: true, rows: [] });
  }
}
