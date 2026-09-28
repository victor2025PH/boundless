/**
 * 已知问题追问：bot 给出已知问题解决步骤后，用户「解决了 / 转人工」都没点 → 过一段时间追问一次。
 * 单文件 JSON（DATA_DIR/chatx_bug_followups.json），原子写 + 串行化；每个 bot+用户只保留最近一条。
 */
import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const FILE = process.env.CHATX_BUG_FOLLOWUP_FILE || path.join(DATA_DIR, "chatx_bug_followups.json");
const KEEP_MS = 3 * 86400_000;
export const FOLLOWUP_AFTER_MS = 30 * 60_000;
export const FOLLOWUP_MAX_AGE_MS = 24 * 3600_000;

export type BugFollowup = { botId: string; uid: number; chatId: number; lang: string; known: string; src: string; shownAt: string; askedAt?: string; doneAt?: string };

async function load(): Promise<BugFollowup[]> {
  try {
    const raw = JSON.parse(await readFile(FILE, "utf-8")) as unknown;
    return Array.isArray(raw) ? (raw as BugFollowup[]) : [];
  } catch {
    return [];
  }
}

let chain: Promise<unknown> = Promise.resolve();
function mutate<T>(fn: (list: BugFollowup[]) => T, now: number): Promise<T> {
  const run = chain.then(async () => {
    let list = await load();
    const out = fn(list);
    list = list.filter((f) => now - Date.parse(f.shownAt) < KEEP_MS);
    await mkdir(path.dirname(FILE), { recursive: true });
    const tmp = `${FILE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(list), "utf-8");
    await rename(tmp, FILE);
    return out;
  });
  chain = run.catch(() => undefined);
  return run;
}

const same = (f: BugFollowup, botId: string, uid: number) => f.botId === botId && f.uid === uid;

/** 给出已知问题方案时登记（覆盖该用户之前的记录）。 */
export function recordKnownShown(f: Omit<BugFollowup, "shownAt" | "askedAt" | "doneAt">, now = Date.now()): Promise<void> {
  return mutate((list) => {
    const i = list.findIndex((x) => same(x, f.botId, f.uid));
    if (i >= 0) list.splice(i, 1);
    list.push({ ...f, shownAt: new Date(now).toISOString() });
  }, now);
}

/** 用户点了「解决了 / 转人工」：结束追问，返回当时给出的已知问题 id。 */
export function markFollowupDone(botId: string, uid: number, now = Date.now()): Promise<string | undefined> {
  return mutate((list) => {
    const f = list.find((x) => same(x, botId, uid) && !x.doneAt);
    if (!f) return undefined;
    f.doneAt = new Date(now).toISOString();
    return f.known;
  }, now);
}

/** 取出到点该追问的记录并标记 askedAt（每条只追问一次）。 */
export function takeDueFollowups(now = Date.now(), afterMs = FOLLOWUP_AFTER_MS): Promise<BugFollowup[]> {
  return mutate((list) => {
    const out: BugFollowup[] = [];
    for (const f of list) {
      const age = now - Date.parse(f.shownAt);
      if (!f.doneAt && !f.askedAt && age >= afterMs && age < FOLLOWUP_MAX_AGE_MS) {
        f.askedAt = new Date(now).toISOString();
        out.push({ ...f });
      }
    }
    return out;
  }, now);
}
