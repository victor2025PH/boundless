import fs from "fs/promises";
import crypto from "crypto";
import path from "path";
import { DATA_DIR } from "./data-dir";

/**
 * 报障群「验证按钮」回调事件存储（实施82 P2，2026-08-29）。
 *
 * 链路：@tgzkw_bot 在报障群发的修复回访带 inline 按钮（✅ 修好了 / ❌ 还是不行，
 * callback_data = `btv:<ticket>:<y|n>:<reporterId>`）→ Telegram 把 callback_query
 * 打进本站 webhook（该 bot 的更新流固定挂在 /api/telegram/webhook，绝不外移）→
 * webhook 即时 answerCallback + 落一行 JSONL 到这里 → 生产机 117 的
 * duty_callback_poll 定时拉取（GET /api/telegram/bug-callbacks）回写工单状态。
 *
 * 为什么是「存储 + 拉取」而不是 VPS 直连 117：117 在 LAN 后面，本站到 117 没有
 * 稳定入向通道；反向「117 定时拉 VPS」是既有先例（诊断包 diag-request 同款），
 * 且断网期事件不丢（JSONL 落盘，恢复后续拉）。
 */
export type BugCallbackEvent = {
  ts: number;            // epoch 秒
  ticket: number;
  verdict: "y" | "n";
  from_id: number;
  username?: string;
  first_name?: string;
  chat_id: number;
  message_id?: number;
};

const FILE = path.join(DATA_DIR, "bug-callbacks.jsonl");

export async function appendBugCallback(evt: BugCallbackEvent): Promise<void> {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.appendFile(FILE, JSON.stringify(evt) + "\n", "utf-8");
}

export async function listBugCallbacks(sinceTs: number, limit = 200): Promise<BugCallbackEvent[]> {
  let raw = "";
  try {
    raw = await fs.readFile(FILE, "utf-8");
  } catch {
    return [];
  }
  const out: BugCallbackEvent[] = [];
  for (const line of raw.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      const evt = JSON.parse(t) as BugCallbackEvent;
      if (Number(evt.ts) > sinceTs) out.push(evt);
    } catch {
      /* skip broken line */
    }
  }
  return out.slice(-limit);
}

/**
 * 拉取端鉴权 key：从双方**已经共享**的 bot token 派生（sha256 前 32 位），
 * 零新密钥分发——117 本地也有同一个 token（notify_webhooks.json），同式派生即可。
 * 不直接用 token 当 header：日志/代理若记 header，泄的是单向哈希而非 token 本体。
 */
export function bugPullKey(): string {
  const token = process.env.TELEGRAM_BOT_TOKEN || "";
  if (!token) return "";
  return crypto.createHash("sha256").update(token).digest("hex").slice(0, 32);
}
