import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { SITE_URL } from "@/lib/site";
import { indexableUrls, INDEXNOW_KEY } from "@/lib/seo";
import { ANALYTICS_DIR } from "@/lib/data-dir";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LOG = path.join(ANALYTICS_DIR, "indexnow.jsonl");

async function logResult(rec: Record<string, unknown>) {
  try {
    await mkdir(ANALYTICS_DIR, { recursive: true });
    await appendFile(LOG, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* 日志失败不影响主流程 */
  }
}

/** IndexNow 推送：部署完成后由 deploy.sh 触发，把全部可收录 URL 通知
 *  api.indexnow.org（自动同步给 Bing / Naver / Seznam / Yandex 等成员引擎）。
 *  Google 不支持 IndexNow，靠 GSC + sitemap 覆盖。 */
export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const host = new URL(SITE_URL).host;
  const urlList = indexableUrls();
  try {
    const res = await fetch("https://api.indexnow.org/indexnow", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        host,
        key: INDEXNOW_KEY,
        keyLocation: `${SITE_URL}/${INDEXNOW_KEY}.txt`,
        urlList,
      }),
      signal: AbortSignal.timeout(15000),
    });
    // 200 = 已接收；202 = 已接收待验 key。其余状态原样透出便于排查。
    const accepted = res.status === 200 || res.status === 202;
    await logResult({ status: res.status, submitted: urlList.length, ok: accepted });
    return NextResponse.json({ ok: accepted, status: res.status, submitted: urlList.length });
  } catch (e) {
    const error = e instanceof Error ? e.message : "indexnow_failed";
    await logResult({ status: 0, submitted: urlList.length, ok: false, error });
    return NextResponse.json({ ok: false, error }, { status: 502 });
  }
}
