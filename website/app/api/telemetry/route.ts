import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { NextRequest, NextResponse } from "next/server";
import { DATA_DIR } from "@/lib/data-dir";
import { clientIp, rateOk } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 匿名安装/健康回执接收端（2026-08-05 落地；schema 白名单与厂商侧参考实现
// license_server.py _sanitize_receipt 逐字段一致）：不可信客户端输入只保留已知字段并
// 强制类型/截断/限长——白名单之外（含任何意外 PII / 超大字段）一律丢弃。
const FILE = process.env.TELEMETRY_FILE || path.join(DATA_DIR, "telemetry.jsonl");
const MAX_BODY = 64 * 1024;

const s = (v: unknown, n: number) => String(v ?? "").slice(0, n);
const toInt = (v: unknown, d = 0) => (Number.isFinite(Number(v)) ? Math.trunc(Number(v)) : d);
const toFloat = (v: unknown, d = 0) => (Number.isFinite(Number(v)) ? Number(v) : d);

export async function POST(req: NextRequest) {
  if (!rateOk("tele:" + clientIp(req), 60)) {
    return NextResponse.json({ ok: false, error: "too many" }, { status: 429 });
  }
  if (Number(req.headers.get("content-length") || 0) > MAX_BODY) {
    return NextResponse.json({ ok: false, error: "请求体过大。" }, { status: 413 });
  }
  let o: Record<string, unknown> = {};
  try {
    o = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "回执格式非法。" }, { status: 400 });
  }
  if (!o || typeof o !== "object" || Array.isArray(o)) {
    return NextResponse.json({ ok: false, error: "回执格式非法。" }, { status: 400 });
  }
  const srcs = Array.isArray(o.sources) ? (o.sources as unknown[]) : [];
  const items = Array.isArray(o.items) ? (o.items as unknown[]) : [];
  const rec: Record<string, unknown> = {
    schema: toInt(o.schema, 1),
    ts: s(o.ts, 32),
    kind: s(o.kind, 16),
    manifest_version: s(o.manifest_version, 32),
    channel: s(o.channel, 24),
    edition: s(o.edition, 24),
    platform: s(o.platform, 24),
    sources: srcs.slice(0, 10).map((x) => s(x, 80)),
    total_bytes: toInt(o.total_bytes),
    total_secs: toFloat(o.total_secs),
    ok: toInt(o.ok),
    fail: toInt(o.fail),
    received_at: Math.floor(Date.now() / 1000),
  };
  if (o.anon_id) rec.anon_id = s(o.anon_id, 64);
  if (o.gpu) rec.gpu = s(o.gpu, 48);
  if (o.vram_gb !== undefined && o.vram_gb !== null) rec.vram_gb = toInt(o.vram_gb);
  // patch = ChatX 桌面壳已落地的热补丁号（0/缺省=未打）。manifest_version 保持纯
  // semver，热补丁不改它——版本分布仍按安装包聚合，patch 只是同一安装包内的细分。
  // 仅 chatx_heartbeat 会带，故不进 license_server.py 那份厂商侧参考实现。
  if (o.patch !== undefined && o.patch !== null) rec.patch = toInt(o.patch);
  rec.items = items.slice(0, 200).map((it) => {
    const d = (it && typeof it === "object" ? it : {}) as Record<string, unknown>;
    return {
      cid: s(d.cid, 64),
      ok: Boolean(d.ok ?? true),
      bytes: toInt(d.bytes),
      secs: toFloat(d.secs),
      err: s(d.err, 40),
    };
  });
  await mkdir(path.dirname(FILE), { recursive: true });
  await appendFile(FILE, JSON.stringify(rec) + "\n", "utf-8");
  return NextResponse.json({ ok: true });
}
