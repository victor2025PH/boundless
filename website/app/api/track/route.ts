import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "@/lib/data-dir";
import { clientIp } from "@/lib/client-ip";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LOG =
  process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");

/** 只有广告归因链（bot → 落地 → 下载）的事件落来源 IP：日报用它看地域分布和同 IP 刷量；全站其余事件保持不记 IP。 */
const IP_EVENTS = new Set(["chatx_landing_view", "chatx_download_click"]);

export async function POST(req: NextRequest) {
  try {
    const data = await req.json();
    const rec = {
      t: new Date().toISOString(),
      event: String(data?.event ?? "").slice(0, 64),
      props: data?.props ?? null,
      sid: String(data?.sid ?? "").slice(0, 64),
      path: String(data?.path ?? "").slice(0, 200),
      ref: String(data?.ref ?? "").slice(0, 300),
      utm: String(data?.utm ?? "").slice(0, 80),
      ua: (req.headers.get("user-agent") ?? "").slice(0, 250),
      ...(IP_EVENTS.has(String(data?.event ?? "")) ? { ip: clientIp(req) } : {}),
    };
    if (rec.event) {
      await mkdir(path.dirname(LOG), { recursive: true });
      await appendFile(LOG, JSON.stringify(rec) + "\n");
    }
  } catch {
    /* never fail tracking */
  }
  return new NextResponse(null, { status: 204 });
}
