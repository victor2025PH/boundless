import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { ANALYTICS_DIR } from "@/lib/data-dir";
import { trialPaidFunnel } from "@/lib/trial-paid-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// ── ChatX 日更转化读数（2026-08-22，频道 ChatX 聚焦改版的验收面）──────────────
// 四段漏斗一次输出，周审一条命令替代翻 events.jsonl：
//   ① 频道帖会话（utm campaign 前缀 daily-*，或按 medium=channel/group 看全量频道口径）
//   ② 其中到达 /download/chatx 下载页的会话
//   ③ 其中点了「chatx_download_click」真下载的会话
//   ④ 试用→付费（cohort 口径，来自 trial-paid-funnel——注册/付费天然滞后且跨设备，
//      与前三段的会话点击口径**不可直接相除**，只作同窗对照）
// GET /api/admin/chatx-funnel?days=14&prefix=daily-
//
// 会话口径与 /api/admin/stats 的发布下钻一致：一个 sid 归首个出现的 campaign；
// 到达/下载判定看该会话内任意事件（pageview 只在整页加载触发，SPA 内导航靠
// chatx_* 页内事件兜底，故用「任意事件 path 含 /download/chatx」而非仅 pageview）。

const EVENTS = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
// 读取上限比 stats(5000) 放宽：漏斗要 14-30 天窗，截断时响应里显式标 truncated。
const MAX_LINES = 20000;

interface Ev {
  t: string;
  event: string;
  sid: string;
  path: string;
  utm: string;
}

async function readEvents(): Promise<{ rows: Ev[]; truncated: boolean }> {
  try {
    const raw = await readFile(EVENTS, "utf-8");
    const lines = raw.split("\n").filter(Boolean);
    const truncated = lines.length > MAX_LINES;
    const rows: Ev[] = [];
    for (const l of lines.slice(-MAX_LINES)) {
      try {
        const o = JSON.parse(l) as Record<string, unknown>;
        rows.push({
          t: String(o.t ?? ""),
          event: String(o.event ?? ""),
          sid: String(o.sid ?? ""),
          path: String(o.path ?? ""),
          utm: String(o.utm ?? ""),
        });
      } catch {
        /* skip bad line */
      }
    }
    return { rows, truncated };
  } catch {
    return { rows: [], truncated: false };
  }
}

interface Seg {
  sessions: number;
  dl_page: number;
  dl_click: number;
}

function segRates(s: Seg) {
  const pct = (a: number, b: number) => (b ? Math.round((a / b) * 1000) / 10 : null);
  return { ...s, dl_page_rate: pct(s.dl_page, s.sessions), dl_click_rate: pct(s.dl_click, s.sessions) };
}

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const q = req.nextUrl.searchParams;
  const daysRaw = Number(q.get("days") ?? "14");
  const days = Number.isFinite(daysRaw) && daysRaw > 0 ? Math.min(Math.floor(daysRaw), 90) : 14;
  const prefix = (q.get("prefix") ?? "daily-").slice(0, 40);
  const since = Date.now() - days * 86400_000;

  const { rows, truncated } = await readEvents();

  // 会话装配：sid → 归因（首个非空 utm）+ 到达/下载标记
  interface Sess {
    campaign: string;
    medium: string;
    dlPage: boolean;
    dlClick: boolean;
  }
  const sessions = new Map<string, Sess>();
  let scanned = 0;
  for (const e of rows) {
    const t = Date.parse(e.t);
    if (!Number.isFinite(t) || t < since) continue;
    if (!e.sid) continue;
    scanned++;
    let s = sessions.get(e.sid);
    if (!s) {
      s = { campaign: "", medium: "", dlPage: false, dlClick: false };
      sessions.set(e.sid, s);
    }
    if (!s.campaign && e.utm) {
      const parts = e.utm.split("/");
      s.medium = parts[1] ?? "";
      s.campaign = parts[2] ?? "";
    }
    if (e.path.includes("/download/chatx")) s.dlPage = true;
    if (e.event === "chatx_download_click") {
      s.dlPage = true; // 点了下载必然在下载页（SPA 导航丢 pageview 时的兜底）
      s.dlClick = true;
    }
  }

  // 聚合：按 campaign 前缀（日更帖）+ 按 medium（channel/group 全量口径）
  const byCampaign = new Map<string, Seg>();
  const totalPrefix: Seg = { sessions: 0, dl_page: 0, dl_click: 0 };
  const byMedium = new Map<string, Seg>();
  const add = (agg: Seg, s: Sess) => {
    agg.sessions++;
    if (s.dlPage) agg.dl_page++;
    if (s.dlClick) agg.dl_click++;
  };
  for (const s of sessions.values()) {
    if (s.medium === "channel" || s.medium === "group" || s.medium === "bot") {
      let m = byMedium.get(s.medium);
      if (!m) byMedium.set(s.medium, (m = { sessions: 0, dl_page: 0, dl_click: 0 }));
      add(m, s);
    }
    if (!prefix || !s.campaign.startsWith(prefix)) continue;
    add(totalPrefix, s);
    let c = byCampaign.get(s.campaign);
    if (!c) byCampaign.set(s.campaign, (c = { sessions: 0, dl_page: 0, dl_click: 0 }));
    add(c, s);
  }

  // ④ 试用→付费（cohort 口径，同窗对照，非点击归因）
  const trialPaid = await trialPaidFunnel(days).catch(() => null);

  return NextResponse.json({
    ok: true,
    window_days: days,
    prefix,
    events_scanned: scanned,
    events_truncated: truncated,
    campaign_funnel: {
      ...segRates(totalPrefix),
      by_campaign: [...byCampaign.entries()]
        .sort((a, b) => (a[0] < b[0] ? 1 : -1))
        .map(([campaign, seg]) => ({ campaign, ...segRates(seg) })),
    },
    medium_funnel: Object.fromEntries(
      [...byMedium.entries()].map(([m, seg]) => [m, segRates(seg)])
    ),
    trial_paid: trialPaid,
    note:
      "trial_paid 为 cohort 口径（领试用→已付费，跨设备/滞后），与前三段会话口径不可直接相除；" +
      "改版分界 2026-08-21，对比 prefix=daily- 在分界前后的 dl_click_rate 判定 ChatX 聚焦效果。",
  });
}
