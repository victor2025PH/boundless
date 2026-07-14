import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

// 每日视频动态存储：video-feed.json。
// 视频文件本体由 nginx 直接托管（/var/www/media/feed → 站点 /media/feed/*），
// 部署/重建不影响已发布内容；这里只存元数据（与 order-store 同款原子写 + 串行化）。

const DB = process.env.VIDEO_FEED_DB || path.join(DATA_DIR, "video-feed.json");

export interface FeedVideo {
  /** 唯一 ID（默认 YYYYMMDD-slug），幂等上架/去重广播的键。 */
  id: string;
  /** 发布时间 ISO。 */
  date: string;
  title: { zh: string; en: string };
  desc: { zh: string; en: string };
  /** 站内路径，如 /media/feed/20260712-live.mp4（nginx alias 托管）。 */
  src: string;
  poster?: string;
  /** YouTube videoId（有则页面上挂外链）。 */
  youtube?: string;
  /** AI 概念演示标记：true/缺省 显示「AI 概念演示」徽章；false 表示真实引擎输出。 */
  ai?: boolean;
  /** Telegram 频道广播回执（幂等：已有 message_id 不再重发）。 */
  tg?: { message_id?: number; posted_at?: string };
}

interface FeedDb {
  version: 1;
  videos: Record<string, FeedVideo>;
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<FeedDb> {
  try {
    const parsed = JSON.parse(await readFile(DB, "utf-8"));
    if (parsed?.videos) return parsed as FeedDb;
  } catch {
    /* first run */
  }
  return { version: 1, videos: {} };
}

async function writeDb(db: FeedDb) {
  await mkdir(path.dirname(DB), { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

/** 最新在前的视频列表。 */
export async function listFeed(limit = 60): Promise<FeedVideo[]> {
  const db = await readDb();
  return Object.values(db.videos)
    .sort((a, b) => (a.date < b.date ? 1 : -1))
    .slice(0, limit);
}

/** 幂等上架：同 id 覆盖更新（保留已有 tg 回执），返回是否新建。 */
export async function upsertFeedVideo(v: FeedVideo): Promise<{ created: boolean; video: FeedVideo }> {
  return serialize(async () => {
    const db = await readDb();
    const old = db.videos[v.id];
    const merged: FeedVideo = { ...old, ...v, tg: old?.tg ?? v.tg };
    db.videos[v.id] = merged;
    await writeDb(db);
    return { created: !old, video: merged };
  });
}

/** 记录频道广播回执（去重锚点）。 */
export async function markFeedBroadcast(id: string, messageId: number): Promise<void> {
  return serialize(async () => {
    const db = await readDb();
    const v = db.videos[id];
    if (!v) return;
    v.tg = { message_id: messageId, posted_at: new Date().toISOString() };
    await writeDb(db);
  });
}

/** 下架（测试清理/纠错用）。 */
export async function removeFeedVideo(id: string): Promise<boolean> {
  return serialize(async () => {
    const db = await readDb();
    if (!db.videos[id]) return false;
    delete db.videos[id];
    await writeDb(db);
    return true;
  });
}
