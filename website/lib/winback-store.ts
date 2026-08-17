/**
 * 挽回外呼状态（P3-⑬）：记「哪些滞留用户客服已经联系过」，避免重复打扰 + 留闭环。
 *
 * 刻意**独立于** trial-claim-store：那里的写路径绑着厂商机签发协议（license/topup
 * voucher），不该被一个运营侧的「已联系」标记搅进去。这里就是一个按指纹去重的
 * 极简 JSON 台账，只读挽回名单时合并进来。
 *
 * 隐私：只存指纹（非秘密，界面本就显示）+ 联系时刻 + 操作人 + 可选备注；
 * 绝不存聊天内容。备注由运营自填跟进结果，与商机 note 同纪律。
 */
import { readFile, writeFile, mkdir } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DB = process.env.WINBACK_DB || path.join(DATA_DIR, "winback-outreach.json");

export interface WinbackOutreach {
  contactedAt: string;
  by: string;
  note?: string;
}

interface Db {
  version: 1;
  byFp: Record<string, WinbackOutreach>;
}

// 单进程写串行化（与 trial-claim-store 同款，避免并发重写互相覆盖）
let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<Db> {
  try {
    const parsed = JSON.parse(await readFile(DB, "utf8"));
    if (parsed?.byFp && typeof parsed.byFp === "object") {
      return { version: 1, byFp: parsed.byFp };
    }
  } catch {
    /* fresh */
  }
  return { version: 1, byFp: {} };
}

/** 读全量已联系映射（挽回名单渲染时合并；文件缺失=空 map，绝不抛）。 */
export async function contactedMap(): Promise<Record<string, WinbackOutreach>> {
  return (await readDb()).byFp;
}

function normFp(raw: string): string {
  return String(raw || "").trim().toUpperCase().replace(/[^A-Z0-9-]/g, "").slice(0, 64);
}

/** 标记某指纹「已联系」（幂等覆盖：重复标记刷新时刻/备注，不报错）。 */
export async function markContacted(
  fingerprint: string, by: string, note?: string
): Promise<{ ok: boolean; error?: string }> {
  const fp = normFp(fingerprint);
  if (!fp || fp.length < 8) return { ok: false, error: "bad_fingerprint" };
  return serialize(async () => {
    const db = await readDb();
    db.byFp[fp] = {
      contactedAt: new Date().toISOString(),
      by: String(by || "").slice(0, 40) || "console",
      ...(note ? { note: String(note).slice(0, 200) } : {}),
    };
    await mkdir(path.dirname(DB), { recursive: true });
    await writeFile(DB, JSON.stringify(db, null, 2));
    return { ok: true };
  });
}

/** 取消标记（误标回退）。 */
export async function unmarkContacted(fingerprint: string): Promise<{ ok: boolean }> {
  const fp = normFp(fingerprint);
  if (!fp) return { ok: false };
  return serialize(async () => {
    const db = await readDb();
    if (db.byFp[fp]) {
      delete db.byFp[fp];
      await writeFile(DB, JSON.stringify(db, null, 2));
    }
    return { ok: true };
  });
}
