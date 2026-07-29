import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir, rename, stat } from "fs/promises";
import path from "path";
import { clientIp } from "@/lib/client-ip";
import { DATA_DIR } from "@/lib/data-dir";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/client-log —— 桌面客户端错误回传归集（公网测试/正式用户的远程可观测）。
 *
 * body: { fingerprint, version, events: [{ts, logger, level, msg, n?}] ≤10 }
 * 落盘: DATA_DIR/client-logs.jsonl（每事件一行，附服务端时间与 IP 前缀）
 *
 * 安全/防刷：蜜罐 hp、IP+指纹滑动窗限流、消息强制截断、事件数上限、文件轮转。
 * 不需要令牌——错误可能发生在领取试用之前（那正是最需要看到的时刻）。
 */

const LOG = process.env.CLIENT_LOG_PATH || path.join(DATA_DIR, "client-logs.jsonl");
const LOG_MAX = 20 * 1024 * 1024;
const WINDOW_MS = 10 * 60 * 1000;
const MAX_PER_IP = 60;
const MAX_PER_FP = 40;
const hits = new Map<string, number[]>();

function limited(key: string, max: number): boolean {
  const now = Date.now();
  const arr = (hits.get(key) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(key, arr);
  if (hits.size > 8000) {
    for (const [k, v] of hits) {
      if (!v.length || now - v[v.length - 1]! > WINDOW_MS) hits.delete(k);
    }
  }
  return arr.length > max;
}

function clean(s: unknown, max: number): string {
  return String(s ?? "").replace(/[\r\n\t]/g, " ").slice(0, max);
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => ({}));
    if (String(data?.hp || "").trim()) return new NextResponse(null, { status: 204 });

    const ip = clientIp(req);
    if (limited(`ip:${ip}`, MAX_PER_IP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }
    const fp = clean(data?.fingerprint, 40).toUpperCase();
    if (fp && limited(`fp:${fp}`, MAX_PER_FP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }

    const version = clean(data?.version, 20);
    const events = Array.isArray(data?.events) ? data.events.slice(0, 10) : [];
    if (!events.length) return new NextResponse(null, { status: 204 });

    await mkdir(path.dirname(LOG), { recursive: true });
    try {
      const st = await stat(LOG);
      if (st.size > LOG_MAX) await rename(LOG, LOG + ".1");
    } catch {
      /* 文件还不存在 */
    }
    const now = new Date().toISOString();
    let lines = "";
    for (const e of events) {
      lines += JSON.stringify({
        t: now,
        ip,
        fp,
        ver: version,
        ts: Number(e?.ts) || 0,
        logger: clean(e?.logger, 80),
        level: clean(e?.level, 10),
        msg: clean(e?.msg, 300),
        n: Math.min(Math.max(1, Number(e?.n) || 1), 9999),
      }) + "\n";
    }
    await appendFile(LOG, lines);
    return new NextResponse(null, { status: 204 });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
