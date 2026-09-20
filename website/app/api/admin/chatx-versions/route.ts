import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { DATA_DIR } from "@/lib/data-dir";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// ChatX 桌面端版本分布（2026-08-14）：读 /api/telemetry 落的 telemetry.jsonl 里的
// kind="chatx_heartbeat" 心跳（桌面壳每 24h 一次，anon_id=装机级随机 uuid），按 anon_id
// 去重取最新一条 → 「每台装机现在跑什么版本」。运营用途：给 min_supported_version
// 划强制升级线之前，先看有多少台会被划进去；发版后看升级率爬坡。
// ?days=30（默认）只统计窗口内有心跳的「活跃装机」；?days=0 全量。
const FILE = process.env.TELEMETRY_FILE || path.join(DATA_DIR, "telemetry.jsonl");
const ANN = path.join(process.cwd(), "public", "downloads", "announcements.json");

function cmpVersions(a: string, b: string): number {
  const pa = a.split(".");
  const pb = b.split(".");
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = parseInt(pa[i], 10) || 0;
    const y = parseInt(pb[i], 10) || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const daysParam = Number(new URL(req.url).searchParams.get("days") ?? "30");
  const winDays = Number.isFinite(daysParam) && daysParam > 0 ? Math.min(Math.floor(daysParam), 365) : 0;
  const since = winDays > 0 ? Math.floor(Date.now() / 1000) - winDays * 86400 : 0;

  // 心跳量级很小（装机数/天），但文件与其它回执共用——只读尾部 200k 行护住内存。
  let lines: string[] = [];
  try {
    lines = (await readFile(FILE, "utf-8")).split("\n").filter(Boolean).slice(-200000);
  } catch {
    /* 文件还不存在 = 还没有任何心跳 */
  }

  // 每台装机（anon_id）保留 received_at 最新的一条；无 anon_id 的旧行跳过（无法去重）。
  const byInstall = new Map<string, { version: string; platform: string; last: number }>();
  for (const l of lines) {
    let o: Record<string, unknown>;
    try { o = JSON.parse(l); } catch { continue; }
    if (o.kind !== "chatx_heartbeat") continue;
    const id = String(o.anon_id ?? "");
    if (!id) continue;
    const last = Number(o.received_at) || 0;
    const prev = byInstall.get(id);
    if (!prev || last > prev.last) {
      byInstall.set(id, {
        version: String(o.manifest_version ?? "") || "?",
        platform: String(o.platform ?? "") || "?",
        last,
      });
    }
  }

  const byVersion: Record<string, { count: number; last_seen: number }> = {};
  let total = 0;
  for (const rec of byInstall.values()) {
    if (since && rec.last < since) continue; // 窗口外 = 可能已卸载/长期离线，不计入活跃
    total++;
    const v = (byVersion[rec.version] ??= { count: 0, last_seen: 0 });
    v.count++;
    if (rec.last > v.last_seen) v.last_seen = rec.last;
  }

  // 强制升级线（若已设）：顺带算出「低于线的活跃装机数」——划线影响面一眼可见。
  let minSupported = "";
  try {
    const ann = JSON.parse(await readFile(ANN, "utf-8"));
    minSupported = String(ann?.min_supported_version ?? "");
  } catch { /* feed 不存在/无该字段 */ }
  let belowLine = 0;
  if (minSupported) {
    for (const [v, rec] of Object.entries(byVersion)) {
      if (v !== "?" && cmpVersions(v, minSupported) < 0) belowLine += rec.count;
    }
  }

  const rows = Object.entries(byVersion)
    .map(([version, v]) => ({
      version,
      count: v.count,
      pct: total > 0 ? Number(((v.count / total) * 100).toFixed(1)) : 0,
      last_seen: v.last_seen,
    }))
    .sort((a, b) => (a.version === "?" ? 1 : b.version === "?" ? -1 : cmpVersions(b.version, a.version)));

  return NextResponse.json({
    ok: true,
    window_days: winDays,
    active_installs: total,
    known_installs: byInstall.size,
    min_supported_version: minSupported,
    below_min_supported: minSupported ? belowLine : null,
    versions: rows,
  });
}
