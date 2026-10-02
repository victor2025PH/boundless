import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

// @ChatX_bot 用户偏好：chatx_bot_prefs.json（uid → 推送开关 / 人设 / 语音）。
// 与 feed-store 同款原子写 + 串行化；量级是「进过 bot 的人」（万级），单文件 JSON 足够。

const DB = process.env.CHATX_BOT_PREFS || path.join(DATA_DIR, "chatx_bot_prefs.json");

export type ChatxPersona = "xiaojie" | "lover" | "sales";

/** 与智聊 voice_enroll.build_lan_voice_profile 同义：零样本克隆，reference_audio_path + owner_consent 齐备才算 ready。 */
export interface ChatxMyVoice {
  path: string;
  sec: number;
  grade: "green" | "yellow";
  score: number;
  owner_consent: true;
  consentAt: string;
  createdAt: string;
}

export interface ChatxPrefs {
  /** 每日资讯推送；缺省 = 开（/stop 关，/news 开） */
  push?: boolean;
  /** 对话人设；缺省 = 小界 */
  persona?: ChatxPersona;
  /** 语音回复；缺省跟人设（恋爱陪聊默认开） */
  voice?: boolean;
  /** 各人设选的音色：音色库 spk / "mine"（我的克隆音）/ "default"；缺省用人设推荐音色（chatx-voicepack） */
  voices?: Partial<Record<ChatxPersona, string>>;
  /** 用户自己的克隆参考音（本人授权后才写入；删除即回落库音色） */
  myVoice?: ChatxMyVoice;
  /** 点了「同意并开始录」的时间：之后 10 分钟内收到的语音当作克隆样本 */
  cloneArmedAt?: string;
  updatedAt?: string;
}

interface PrefsDb {
  version: 1;
  users: Record<string, ChatxPrefs>;
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<PrefsDb> {
  try {
    const parsed = JSON.parse(await readFile(DB, "utf-8")) as Partial<PrefsDb>;
    if (parsed?.users) return { version: 1, users: parsed.users };
  } catch {
    /* first run */
  }
  return { version: 1, users: {} };
}

async function writeDb(db: PrefsDb) {
  await mkdir(path.dirname(DB), { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

export async function getPrefs(uid: number): Promise<ChatxPrefs> {
  const db = await readDb();
  return db.users[String(uid)] ?? {};
}

export async function setPrefs(uid: number, patch: Partial<ChatxPrefs>): Promise<ChatxPrefs> {
  return serialize(async () => {
    const db = await readDb();
    const cur = db.users[String(uid)] ?? {};
    const next: ChatxPrefs = { ...cur, ...patch, updatedAt: new Date().toISOString() };
    db.users[String(uid)] = next;
    await writeDb(db);
    return next;
  });
}

/** 一次读全量（推送前过滤退订用户），避免每人一次 IO。 */
export async function allPrefs(): Promise<Map<number, ChatxPrefs>> {
  const db = await readDb();
  const out = new Map<number, ChatxPrefs>();
  for (const [k, v] of Object.entries(db.users)) {
    const uid = Number(k);
    if (Number.isFinite(uid)) out.set(uid, v);
  }
  return out;
}
