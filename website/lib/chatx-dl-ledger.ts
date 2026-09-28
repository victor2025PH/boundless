/**
 * @ChatX_bot 用户级下载回执：bot 深链带 tg=<uid> → 落地页 / 交接链接原样带到 /dl → 这里落一条。
 * 与 events.jsonl 的 download_redirect 并行（那边给 admin 按 src 统计），本账本只给 bot 自己用：
 * 24h 追发只发「没请求过安装包」的人，admin 来源表数「下载人数」。小文件，整读即可。
 */
import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const LOG = process.env.CHATX_BOT_DL_LOG || path.join(DATA_DIR, "chatx_bot_downloads.jsonl");

const TG_UID_RE = /^\d{1,20}$/;

/** Telegram 用户 id（纯数字）。URL 上的 tg= 只认这个格式，其余当没有。 */
export function isValidTgUid(v: string | null | undefined): v is string {
  return typeof v === "string" && TG_UID_RE.test(v);
}

export interface BotDownloadRec {
  t: string;
  uid: number;
  src: string;
  file: string;
}

export async function recordBotDownload(rec: Omit<BotDownloadRec, "t">): Promise<void> {
  try {
    await mkdir(path.dirname(LOG), { recursive: true });
    await appendFile(LOG, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* never fail a download over bookkeeping */
  }
}

/** 已请求过安装包的 uid 集合。 */
export async function downloadedUids(): Promise<Set<number>> {
  const out = new Set<number>();
  try {
    const raw = await readFile(LOG, "utf-8");
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line) as Partial<BotDownloadRec>;
        if (typeof r.uid === "number") out.add(r.uid);
      } catch {
        /* skip bad line */
      }
    }
  } catch {
    /* no ledger yet */
  }
  return out;
}
