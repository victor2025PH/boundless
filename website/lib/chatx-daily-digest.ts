import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { SITE_URL } from "./site";
import { notifyAdmins } from "./order-store";
import { buildDailyReport, buildPostFunnel, formatDailyDigest, formatWeeklyDigest, readEvents, recentDays, TZ_MS } from "./chatx-report";
import { formatSupportDayLine, listTickets, supportDayStats } from "./chatx-tickets";
import { loadHub } from "./tg-hub-store";
import { listPosts, seriesKeyFn } from "./tg-posts";

/**
 * 每天一次把「昨天」的 ChatX 广告日报推给管理员（主 bot 的 admin chats），由 /api/admin/schedule/run 的 cron 驱动。
 * 幂等：每个日期只发一次，发送前先在账本占位（宁漏不重，与 24h 追发同策略）；错过小时不补发上上天。
 */
const DIGEST_HOUR = Number(process.env.CHATX_DIGEST_HOUR ?? 9);
const WEEKLY_DOW = Number(process.env.CHATX_WEEKLY_DOW ?? 1);
const DIGEST_LOG = process.env.CHATX_DIGEST_LOG || path.join(DATA_DIR, "chatx_digest_sent.jsonl");

export async function runDailyDigest(now = Date.now(), force = false): Promise<{ day: string; sent: boolean; reason?: string }> {
  const local = new Date(now + TZ_MS);
  const day = new Date(now - 86_400_000 + TZ_MS).toISOString().slice(0, 10);
  if (!force && local.getUTCHours() !== DIGEST_HOUR) return { day, sent: false, reason: "not_hour" };
  if (!force && (await sentKeys("day")).has(day)) return { day, sent: false, reason: "already_sent" };
  try {
    await mkdir(path.dirname(DIGEST_LOG), { recursive: true });
    await appendFile(DIGEST_LOG, JSON.stringify({ t: new Date(now).toISOString(), day }) + "\n");
  } catch {
    return { day, sent: false, reason: "ledger_write_failed" };
  }
  const days = recentDays(7, now - 86_400_000);
  const events = await readEvents(undefined, now - 9 * 86_400_000);
  const report = buildDailyReport(events, days, now);
  let text = formatDailyDigest(report, day);
  const supportLine = await supportLineFor(day).catch(() => null);
  if (supportLine) text += "\n\n" + supportLine;
  await notifyAdmins(text, [[{ text: "📈 打开 admin 日报", url: `${SITE_URL}/admin` }]]);
  return { day, sent: true };
}

async function supportLineFor(day: string, spanDays = 1): Promise<string | null> {
  const from = Date.parse(`${day}T00:00:00Z`) - TZ_MS;
  const slaMin = (await loadHub()).support.slaMin;
  return formatSupportDayLine(supportDayStats(await listTickets(), from, from + spanDays * 86_400_000, slaMin), slaMin);
}

/**
 * 每周一（CHATX_WEEKLY_DOW，0=周日）日报同一小时，把上周一~周日的周报推给管理员；同一周只发一次（账本 week 键）。
 */
export async function runWeeklyDigest(now = Date.now(), force = false): Promise<{ week: string; sent: boolean; reason?: string }> {
  const local = new Date(now + TZ_MS);
  const days = recentDays(7, now - 86_400_000);
  const week = days[0];
  if (!force && (local.getUTCDay() !== WEEKLY_DOW || local.getUTCHours() !== DIGEST_HOUR)) return { week, sent: false, reason: "not_time" };
  if (!force && (await sentKeys("week")).has(week)) return { week, sent: false, reason: "already_sent" };
  try {
    await mkdir(path.dirname(DIGEST_LOG), { recursive: true });
    await appendFile(DIGEST_LOG, JSON.stringify({ t: new Date(now).toISOString(), week }) + "\n");
  } catch {
    return { week, sent: false, reason: "ledger_write_failed" };
  }
  const events = await readEvents(undefined, now - 17 * 86_400_000);
  const cur = buildDailyReport(events, days, now);
  const prev = buildDailyReport(events, recentDays(7, now - 8 * 86_400_000), now);
  const from = Date.parse(`${week}T00:00:00Z`) - TZ_MS;
  const posts = await listPosts().catch(() => []);
  const funnel = buildPostFunnel(events, seriesKeyFn(posts), { from, to: from + 7 * 86_400_000 });
  const supportLine = await supportLineFor(week, 7).catch(() => null);
  const text = formatWeeklyDigest(cur, prev, { posts: [...funnel.values()], supportLine });
  await notifyAdmins(text, [[{ text: "📈 打开 admin 日报", url: `${SITE_URL}/admin` }]]);
  return { week, sent: true };
}

async function sentKeys(field: "day" | "week"): Promise<Set<string>> {
  try {
    const raw = await readFile(DIGEST_LOG, "utf8");
    const out = new Set<string>();
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line) as { day?: string; week?: string };
        const v = r[field];
        if (v) out.add(v);
      } catch {
        /* skip */
      }
    }
    return out;
  } catch {
    return new Set();
  }
}
