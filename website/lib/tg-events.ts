import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";

/**
 * TG 端服务端事件落账：与 /api/track 同文件同格式（events.jsonl），
 * bot 侧行为（客服承接/转人工等）进同一漏斗口径，admin 统计与周报直接可读。
 */

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");

export async function trackTg(event: string, props?: Record<string, unknown>): Promise<void> {
  return trackServer(event, props, "tg", "telegram-bot");
}

/** 任意服务端落账（非 bot 侧也可用，如 /dl 分流入口）：path/ua 标明来源。 */
export async function trackServer(
  event: string,
  props?: Record<string, unknown>,
  srcPath = "server",
  ua = "server"
): Promise<void> {
  try {
    const rec = {
      t: new Date().toISOString(),
      event: String(event).slice(0, 64),
      props: props ?? null,
      sid: "",
      path: srcPath,
      ref: "",
      utm: "",
      ua,
    };
    await mkdir(path.dirname(LOG), { recursive: true });
    await appendFile(LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* never fail request flow over tracking */
  }
}
