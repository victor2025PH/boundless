import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";

// 服务端内部事件直写 events.jsonl —— 与 /api/track 同一文件、同一行形状（sid 固定 "server"），
// 后台看板可以用同一套读取器聚合。用于 cron/巡检类系统事件入流水；失败静默：
// 可观测性绝不反噬业务主链路。

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");

export async function appendServerEvent(event: string, props: Record<string, unknown>): Promise<void> {
  try {
    const rec = {
      t: new Date().toISOString(),
      event: event.slice(0, 64),
      props,
      sid: "server",
      path: "",
      ref: "",
      utm: "",
      ua: "server",
    };
    await mkdir(path.dirname(LOG), { recursive: true });
    await appendFile(LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* ignore */
  }
}
