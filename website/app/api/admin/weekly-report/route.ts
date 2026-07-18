import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { getAdminChats } from "@/lib/admin-store";
import { listLeads } from "@/lib/lead-store";
import { addDraft, listDrafts } from "@/lib/schedule-store";
import { dragonWeekly } from "@/lib/dragon-store";
import { ANALYTICS_DIR } from "@/lib/data-dir";
import { SITE_URL } from "@/lib/site";
import { indexableUrls } from "@/lib/seo";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 每周一早晨 cron 触发：把过去 7 天的经营数据打包成一条 TG 消息推给管理员。
 *  没绑管理员时返回 preview 不报错——绑定后自然生效。 */

const EVENTS = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
const GROWTH = path.join(ANALYTICS_DIR, "growth.jsonl");
const INDEXNOW_LOG = path.join(ANALYTICS_DIR, "indexnow.jsonl");
const WATCHDOG_LOG = process.env.WATCHDOG_LOG || "/home/ubuntu/health-watchdog.log";

const DAY = 24 * 3600 * 1000;

async function readJsonl(file: string, max = 20000): Promise<Record<string, unknown>[]> {
  try {
    const raw = await readFile(file, "utf-8");
    const out: Record<string, unknown>[] = [];
    for (const l of raw.split("\n").filter(Boolean).slice(-max)) {
      try {
        out.push(JSON.parse(l));
      } catch {
        /* skip */
      }
    }
    return out;
  } catch {
    return [];
  }
}

function ts(e: Record<string, unknown>): number {
  const t = Date.parse(String(e.t ?? e.ts ?? ""));
  return isNaN(t) ? 0 : t;
}

function fmtDelta(cur: number, prev: number): string {
  const d = cur - prev;
  return d > 0 ? `+${d}` : String(d);
}

function fmtDay(ms: number): string {
  return new Date(ms + 8 * 3600 * 1000).toISOString().slice(5, 10);
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => ({}));
  const dryRun = body?.dryRun === true;

  const now = Date.now();
  const wkStart = now - 7 * DAY;
  const prevStart = now - 14 * DAY;

  // ── 官网核心指标：本周 vs 上周 ──
  const events = await readJsonl(EVENTS);
  const inWeek = (e: Record<string, unknown>) => ts(e) >= wkStart;
  const inPrev = (e: Record<string, unknown>) => ts(e) >= prevStart && ts(e) < wkStart;
  const count = (ev: string, win: (e: Record<string, unknown>) => boolean) =>
    events.filter((e) => e.event === ev && win(e)).length;

  const pv = count("pageview", inWeek);
  const pvPrev = count("pageview", inPrev);
  const cta = count("cta_click", inWeek);
  const ctaPrev = count("cta_click", inPrev);

  const leads = await listLeads();
  const newLeads = leads.filter((l) => Date.parse(l.firstSeen) >= wkStart).length;
  const newLeadsPrev = leads.filter((l) => {
    const t = Date.parse(l.firstSeen);
    return t >= prevStart && t < wkStart;
  }).length;

  // ── 本周最强入口 Top3（会话数，官网 utm campaign + 小程序 cmp 合并口径）──
  const campBySid: Record<string, { cmp: string; convert: boolean }> = {};
  for (const e of events) {
    if (!inWeek(e)) continue;
    const sid = String(e.sid ?? "");
    if (!sid) continue;
    const ev = String(e.event ?? "");
    const rec = (campBySid[sid] ??= { cmp: "", convert: false });
    if (ev === "miniapp_open") {
      const p = (e.props ?? {}) as Record<string, unknown>;
      const c = String(p.cmp ?? "");
      if (c && !rec.cmp) rec.cmp = c;
    } else if (!ev.startsWith("miniapp_") && !rec.cmp && e.utm) {
      const seg = String(e.utm).split("/")[2] ?? "";
      if (seg && seg !== "-") rec.cmp = seg;
    }
    if (ev === "lead_submit" || ev === "miniapp_lead" || ev === "miniapp_unlock") rec.convert = true;
  }
  const campAgg: Record<string, { sessions: number; convert: number }> = {};
  for (const r of Object.values(campBySid)) {
    if (!r.cmp) continue;
    const a = (campAgg[r.cmp] ??= { sessions: 0, convert: 0 });
    a.sessions++;
    if (r.convert) a.convert++;
  }
  const topCamps = Object.entries(campAgg)
    .sort((a, b) => b[1].sessions - a[1].sessions)
    .slice(0, 3);

  // ── A/B 实验：会话口径（同一会话多次曝光/点击只算一次），双比例 z 检验自动给结论 ──
  // 归因约定与 stats 路由一致：cta_click 带 props.ab 视为 hero_cta 实验的转化。
  const abSessions: Record<string, Record<string, { exposed: Set<string>; converted: Set<string> }>> = {};
  for (const e of events) {
    if (!inWeek(e)) continue;
    const sid = String(e.sid ?? "");
    if (!sid) continue;
    const props = (e.props ?? {}) as Record<string, unknown>;
    if (e.event === "ab_expose") {
      const exp = String(props.experiment ?? "?");
      const v = String(props.variant ?? "?");
      const bucket = ((abSessions[exp] ??= {})[v] ??= { exposed: new Set(), converted: new Set() });
      bucket.exposed.add(sid);
    } else if (e.event === "cta_click" && props.ab) {
      const bucket = ((abSessions["hero_cta"] ??= {})[String(props.ab)] ??= {
        exposed: new Set(),
        converted: new Set(),
      });
      bucket.converted.add(sid);
    }
  }
  const abLines: string[] = [];
  const MIN_N = 100; // 每版本最少会话数，低于此不下结论
  for (const [exp, variants] of Object.entries(abSessions)) {
    const vs = Object.entries(variants)
      .map(([v, s]) => {
        // 点击但无曝光记录的会话（打点丢失）不计入转化，保持分子 ⊆ 分母
        const n = s.exposed.size;
        const x = [...s.converted].filter((sid) => s.exposed.has(sid)).length;
        return { v, n, x, rate: n > 0 ? x / n : 0 };
      })
      .sort((a, b) => a.v.localeCompare(b.v));
    if (vs.every((s) => s.n === 0)) continue;
    abLines.push(
      `· ${exp}：` +
        vs.map((s) => `${s.v} 曝光 ${s.n} → 转化 ${s.x}（${(s.rate * 100).toFixed(1)}%）`).join("｜")
    );
    if (vs.length === 2) {
      const [A, B] = vs;
      if (A.n >= MIN_N && B.n >= MIN_N) {
        const pool = (A.x + B.x) / (A.n + B.n);
        const se = Math.sqrt(pool * (1 - pool) * (1 / A.n + 1 / B.n));
        const z = se > 0 ? (A.rate - B.rate) / se : 0;
        if (Math.abs(z) >= 1.96) {
          const win = z > 0 ? A : B;
          abLines.push(`  ✅ ${win.v} 版显著胜出（z=${Math.abs(z).toFixed(2)}，95% 置信），建议固化 ${win.v} 版文案`);
        } else {
          abLines.push(`  ⏸ 样本已达标但差异不显著（z=${Math.abs(z).toFixed(2)}），可再跑一周或判平收掉`);
        }
      } else {
        abLines.push(`  ⏳ 样本不足（各需 ≥${MIN_N} 会话），继续积累`);
      }
    }
  }

  // ── 频道/群订阅：区间首尾快照对比 ──
  const growthRows = (await readJsonl(GROWTH)).filter((g) => ts(g) >= prevStart);
  const nums = (k: "channel" | "group") =>
    growthRows.map((g) => g[k]).filter((v): v is number => typeof v === "number");
  const chSeries = nums("channel");
  const grSeries = nums("group");

  // ── SEO 巡检：sitemap 可达 + 关键页全 200 + 最近一次 IndexNow 推送 ──
  const seoLines: string[] = [];
  try {
    const sm = await fetch(`${SITE_URL}/sitemap.xml`, { signal: AbortSignal.timeout(10000) });
    const urlCount = sm.ok ? ((await sm.text()).match(/<loc>/g) ?? []).length : 0;
    seoLines.push(sm.ok ? `· sitemap：正常（${urlCount} 个 URL）` : `· sitemap：异常 HTTP ${sm.status} ⚠️`);
  } catch {
    seoLines.push("· sitemap：无法访问 ⚠️");
  }
  const pages = indexableUrls();
  const checks = await Promise.allSettled(
    pages.map((u) => fetch(u, { signal: AbortSignal.timeout(10000) }))
  );
  const badPages = pages.filter((_, i) => {
    const c = checks[i];
    return c.status !== "fulfilled" || !c.value.ok;
  });
  seoLines.push(
    badPages.length === 0
      ? `· 关键页面：${pages.length}/${pages.length} 全部 200`
      : `· 关键页面：${pages.length - badPages.length}/${pages.length} 正常，异常 ${badPages
          .slice(0, 3)
          .map((u) => new URL(u).pathname)
          .join(" ")} ⚠️`
  );
  const inRows = await readJsonl(INDEXNOW_LOG, 50);
  const lastIn = inRows[inRows.length - 1];
  if (lastIn) {
    const okIn = lastIn.ok === true;
    seoLines.push(
      `· IndexNow：${okIn ? "正常" : `异常（${lastIn.status || lastIn.error}）⚠️`} · 最近提交 ${lastIn.submitted} 条（${fmtDay(ts(lastIn))}）`
    );
  } else {
    seoLines.push("· IndexNow：暂无推送记录");
  }

  // ── 运营状态：待审草稿 + 本周自愈重启次数 ──
  const drafts = await listDrafts();
  const riskyDrafts = drafts.filter((d) => d.source === "ai-risky").length;
  let restarts = 0;
  try {
    const log = await readFile(WATCHDOG_LOG, "utf-8");
    for (const line of log.split("\n")) {
      if (!line.includes("restarting pm2")) continue;
      const m = line.match(/^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]/);
      if (m && Date.parse(m[1].replace(" ", "T") + "+08:00") >= wkStart) restarts++;
    }
  } catch {
    /* log absent — fine */
  }

  const lines: string[] = [
    `📊 无界科技 · 运营周报（${fmtDay(wkStart)} ~ ${fmtDay(now)}）`,
    ``,
    `🌐 官网`,
    `· 浏览 PV：${pv}（上周 ${pvPrev}，${fmtDelta(pv, pvPrev)}）`,
    `· CTA 点击：${cta}（上周 ${ctaPrev}，${fmtDelta(cta, ctaPrev)}）`,
    `· 新增留资：${newLeads}（上周 ${newLeadsPrev}，${fmtDelta(newLeads, newLeadsPrev)}）`,
  ];
  if (topCamps.length > 0) {
    lines.push(``, `📣 本周最强入口（会话 / 转化）`);
    topCamps.forEach(([k, v], i) => lines.push(`${i + 1}. ${k}：${v.sessions} / ${v.convert}`));
  }

  // ── 龙珠彩蛋周况 + 频道战报草稿（人审后一键发布，走既有草稿→定时贴流程）──
  const dg = await dragonWeekly().catch(() => null);
  if (dg) {
    const dgPearls = count("dragon_pearl", inWeek);
    const dgPearlsPrev = count("dragon_pearl", inPrev);
    /* 龙珠参与会话留资对比：见过星珠曝光 vs 有深层互动（收珠/分享/召唤等）— 相关参考非因果 */
    const DRAGON_ENGAGE = new Set([
      "dragon_pearl",
      "dragon_summon_open",
      "dragon_wish",
      "dragon_assist",
      "dragon_share_copy",
      "dragon_codex_open",
      "dragon_codex_view",
      "dragon_tg_click",
      "dragon_tg_sync_click",
      "dragon_tray_skin_switch",
    ]);
    const dgBySid: Record<string, Set<string>> = {};
    for (const e of events) {
      if (!inWeek(e)) continue;
      const ev = String(e.event ?? "");
      if (ev.startsWith("miniapp_")) continue;
      const sid = String(e.sid ?? "");
      if (!sid) continue;
      if (
        ev !== "pageview" &&
        ev !== "lead_submit" &&
        ev !== "dragon_offer_impression" &&
        !DRAGON_ENGAGE.has(ev)
      ) {
        continue;
      }
      (dgBySid[sid] ??= new Set()).add(ev);
    }
    let dgSeen = 0;
    let dgEngaged = 0;
    let dgEngagedLeads = 0;
    let dgSeenOnlyLeads = 0;
    for (const set of Object.values(dgBySid)) {
      if (!set.has("pageview") || !set.has("dragon_offer_impression")) continue;
      dgSeen++;
      const engaged = [...set].some((x) => DRAGON_ENGAGE.has(x));
      if (engaged) {
        dgEngaged++;
        if (set.has("lead_submit")) dgEngagedLeads++;
      } else if (set.has("lead_submit")) {
        dgSeenOnlyLeads++;
      }
    }
    const pct = (n: number, d: number) => (d > 0 ? Number(((n / d) * 100).toFixed(1)) : 0);
    const engRate = pct(dgEngagedLeads, dgEngaged);
    const seenOnlyRate = pct(dgSeenOnlyLeads, Math.max(0, dgSeen - dgEngaged));
    lines.push(
      ``,
      `🐉 龙珠彩蛋`,
      `· 收珠：${dgPearls}（上周 ${dgPearlsPrev}，${fmtDelta(dgPearls, dgPearlsPrev)}）· 新玩家 ${dg.weekNewCollectors}`,
      `· 召唤：本周 ${dg.weekSummons} 次 · 祥龙持有 ${dg.skinOwners} 人 · 本月体验名额剩 ${dg.trialLeft}`,
      `· 参与留资率：互动 ${engRate}% vs 仅曝光 ${seenOnlyRate}%（互动 ${dgEngaged} / 仅曝光 ${Math.max(0, dgSeen - dgEngaged)} 会话；相关参考）`
    );
    if (!dryRun && (dg.weekSummons > 0 || dg.weekNewCollectors > 0)) {
      const helperLine =
        dg.topHelpers.length > 0
          ? `\n🌟 本周点星人榜：${dg.topHelpers.map((h, i) => `${["🥇", "🥈", "🥉"][i]}${h.name} ×${h.n}`).join("  ")}`
          : "";
      const warText = [
        `🐉 界龙战报 · 七星聚集中`,
        ``,
        `本周已有 ${dg.weekSummons} 位旅人集齐北斗七星、召唤界龙成功${dg.weekNewCollectors > 0 ? `，${dg.weekNewCollectors} 位新旅人开始点星` : ""}。${helperLine}`,
        `本月「愿·体验」名额还剩 ${dg.trialLeft} 份——每天来官网点亮一颗星珠，七天召唤神龙许愿：体验月卡 / 祥龙形态 / 机缘礼包。`,
        ``,
        `我的图鉴 → ${SITE_URL}/loong`,
        `七星既聚，龙行无界 → ${SITE_URL}`,
      ].join("\n");
      await addDraft({ text: warText, theme: "dragon-weekly", source: "dragon-weekly" }).catch(() => null);
      lines.push(`· 频道战报草稿已生成（后台「草稿」审核后发布）`);
    }
  }
  if (abLines.length > 0) {
    lines.push(``, `🧪 A/B 实验（会话去重口径）`, ...abLines);
  }
  if (chSeries.length > 0 || grSeries.length > 0) {
    lines.push(``, `📢 频道 & 群`);
    if (chSeries.length > 0)
      lines.push(`· 频道订阅：${chSeries[chSeries.length - 1]}（${fmtDelta(chSeries[chSeries.length - 1], chSeries[0])}）`);
    if (grSeries.length > 0)
      lines.push(`· 群成员：${grSeries[grSeries.length - 1]}（${fmtDelta(grSeries[grSeries.length - 1], grSeries[0])}）`);
  }
  lines.push(``, `🔍 SEO 巡检`, ...seoLines);
  lines.push(``, `🤖 运营状态`);
  lines.push(`· 待审草稿：${drafts.length} 条${riskyDrafts > 0 ? `（其中风险拦截 ${riskyDrafts} 条，请尽快人审）` : ""}`);
  lines.push(`· 网站自愈重启：${restarts} 次${restarts > 0 ? "（有异常，建议看看后台健康页）" : ""}`);
  lines.push(``, `👉 后台看板：${SITE_URL}/admin`);
  const text = lines.join("\n");

  // 第二条：产品运营速览（订单漏斗/升级成功率/错误簇/诊断包），与 TG /ops 命令同源。
  let opsText = "";
  try {
    const { buildOpsSummary, formatOpsSummary } = await import("@/lib/ops-summary");
    opsText = formatOpsSummary(await buildOpsSummary());
  } catch {
    /* 速览失败不拖累周报主体 */
  }

  if (dryRun) return NextResponse.json({ ok: true, sent: 0, dryRun: true, preview: text, opsPreview: opsText });

  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chats = await getAdminChats();
  if (!token || chats.length === 0) {
    return NextResponse.json({ ok: true, sent: 0, reason: "no_recipients", preview: text });
  }
  const send = (chat: string, body: Record<string, unknown>) =>
    fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, disable_web_page_preview: true, ...body }),
      signal: AbortSignal.timeout(8000),
    }).then((r) => r.json());
  const results = await Promise.allSettled(
    chats.map(async (chat) => {
      const main = await send(chat, { text });
      if (opsText) await send(chat, { text: opsText, parse_mode: "HTML" }).catch(() => null);
      return main;
    })
  );
  const sent = results.filter((r) => r.status === "fulfilled" && r.value?.ok).length;
  return NextResponse.json({ ok: sent > 0 || chats.length === 0, sent, total: chats.length });
}
