import { NextRequest, NextResponse } from "next/server";
import { bugPullKey, listBugCallbacks } from "@/lib/bug-callback-store";

/**
 * 报障验证按钮回调的拉取口（实施82 P2）：生产机 117 的 duty_callback_poll
 * 定时 GET，取走 webhook 落盘的按钮事件回写工单。
 *
 * 鉴权＝`x-bug-key` header 必须等于 sha256(bot token) 前 32 位（双方从既有
 * 共享秘密派生，见 bug-callback-store.bugPullKey）；未配 token 时整口关闭。
 * 只读不删——消费端按 `since` 水位增量拉，文件老化由 VPS 侧日常清理策略管。
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const key = bugPullKey();
  if (!key || req.headers.get("x-bug-key") !== key) {
    return NextResponse.json({ ok: false }, { status: 403 });
  }
  const since = Number(req.nextUrl.searchParams.get("since") || 0) || 0;
  const events = await listBugCallbacks(since);
  return NextResponse.json({ ok: true, now: Date.now() / 1000, events });
}
