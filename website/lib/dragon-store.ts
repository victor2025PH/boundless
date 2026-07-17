import { mkdir, readFile, writeFile, rename } from "fs/promises";
import path from "path";
import crypto from "crypto";
import { DATA_DIR } from "./data-dir";

/**
 * 「七星聚 · 龙行无界」龙珠彩蛋的服务端权威存储。
 *
 * 设计要点（与方案一致）：
 * - 星珠按 UTC+8 自然日发放，每访客每日一颗，幂等；
 * - 断签不清零：滚动周期 = 首颗星珠起 30 天，期内累计 7 颗即可召唤（比"连续 7 天否则归零"友好，
 *   Duolingo 公开数据表明断签清零是第一大流失触发器）；连续 7 天额外记「北斗正位」（perfect）；
 * - 祈愿三选一：trial（月卡体验，月度限量）/ skin（祥龙金鳞皮肤，即时生效，零成本）/ gift（机缘礼包）；
 * - trial/gift 签发一次性兑换码（LOONG-XXXXXX），在 Telegram 机器人内核销 → 天然绑定可触达线索；
 * - 兑换码独立于周期存储（codes 表），周期重置不影响已发码的核销。
 *
 * 访客身份：HMAC 签名的 httpOnly cookie（bl_vid），伪造 vid 无法通过验签；
 * 珠/愿全部服务端记账，客户端仅展示。
 */

const STORE = process.env.DRAGON_STORE || path.join(DATA_DIR, "dragon-quest.json");

export const PEARLS_TO_SUMMON = 7;
/** 滚动周期：首颗星珠起 30 天内集齐；过期未集齐则新一轮重新开始 */
const CYCLE_DAYS = 30;
/** 兑换码有效期 */
const CODE_TTL_DAYS = 14;
/** 与 admin/stats 同口径的日界时区（默认 UTC+8） */
const TZ_MS = Number(process.env.TZ_OFFSET ?? 8) * 3600 * 1000;
/** trial（体验月卡）每自然月配额，成本封顶 */
const TRIAL_QUOTA = Number(process.env.DRAGON_TRIAL_QUOTA ?? 50);

export type WishKind = "trial" | "skin" | "gift";

export interface DragonRec {
  vid: string;
  /** 当前周期已收集的日键（"2026-07-16"，UTC+8），升序 */
  pearls: string[];
  /** 当前周期首颗星珠时间（ms） */
  cycleStart: number;
  /** 当前周期由好友助力点亮的日键（限额 ASSIST_CAP/周期） */
  assists?: string[];
  /** 当前周期的祈愿（集齐后三选一，选定即锁定至下一周期） */
  wish?: WishKind;
  wishAt?: string;
  /** 当前周期签发的兑换码（trial/gift） */
  code?: string;
  /** 历史累计召唤（集齐）次数 */
  summons: number;
  /** 「祥龙金鳞」永久皮肤（wish=skin 或 perfect 加成获得） */
  loongSkin: boolean;
  /** 历史最佳连续天数 */
  bestStreak: number;
  /** 绑定的 Telegram 用户（bot 端 /xingzhu 与网页进度合并） */
  tgUserId?: number;
  /** 月令龙鳞：完成周期当月获得一枚（月键 "2026-07"，每月最多一枚），集 3 枚可领大奖 */
  scales?: string[];
  /** 已兑换「界龙之约」次数（成就/图鉴用） */
  grandClaims?: number;
  /** bot 每日提醒订阅 + 最近一次提醒日（去重） */
  remind?: boolean;
  lastRemind?: string;
  /** 作为"点星人"成功助力他人的时间戳（保留近 14 天，周榜用） */
  assistLog?: number[];
  /** 成功助力累计（终身计数，成就用） */
  assistsGiven?: number;
  /** TG 昵称（绑定时记录，周榜展示用；脱敏后出现在频道战报） */
  tgName?: string;
  createdAt: string;
  updatedAt: string;
}

export interface DragonCode {
  code: string;
  vid: string;
  /** trial/gift=单周期祈愿；grand=三枚月鳞兑换的「界龙之约」大奖 */
  kind: Exclude<WishKind, "skin"> | "grand";
  /** 北斗正位（7 天全连续）达成的码，机器人话术给额外加成 */
  perfect: boolean;
  issuedAt: string;
  expiresAt: string;
  redeemed: boolean;
  redeemedAt?: string;
  /** 核销的 TG chat/user id 与展示名（留资归因） */
  redeemedBy?: number;
  redeemedName?: string;
}

interface DragonDb {
  version: 1;
  byVid: Record<string, DragonRec>;
  codes: Record<string, DragonCode>;
  /** TG userId → vid（一个 TG 账号只跟一份进度） */
  tgByUser?: Record<string, string>;
}

/** 好友助力：每周期最多代点的星珠数 */
const ASSIST_CAP = 2;
/** 月令龙鳞：兑换「界龙之约」大奖所需枚数 */
const SCALES_FOR_GRAND = 3;
/** 大奖码有效期（天）——比普通码长，跨月收集后给足冗余 */
const GRAND_TTL_DAYS = 30;

/* ---------- 文件存储（与 unlock-store 同模式：串行化 + 原子写） ---------- */

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<DragonDb> {
  try {
    const raw = await readFile(STORE, "utf-8");
    const parsed = JSON.parse(raw);
    if (parsed?.byVid) return { codes: {}, tgByUser: {}, ...parsed } as DragonDb;
  } catch {
    /* fresh */
  }
  return { version: 1, byVid: {}, codes: {}, tgByUser: {} };
}

async function writeDb(db: DragonDb) {
  await mkdir(path.dirname(STORE), { recursive: true });
  const tmp = STORE + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, STORE);
}

/* ---------- 访客身份：HMAC 签名 cookie ---------- */

const SECRET = process.env.DRAGON_SECRET || process.env.TELEGRAM_SETUP_KEY || "loong-dev-secret";

function sign(vid: string): string {
  return crypto.createHmac("sha256", SECRET).update(vid).digest("hex").slice(0, 16);
}

export function newVisitorToken(): { vid: string; token: string } {
  const vid = crypto.randomUUID();
  return { vid, token: `${vid}.${sign(vid)}` };
}

/** 验签并取回 vid；签名不符（伪造/换密钥）返回 null → 视为新访客 */
export function verifyVisitorToken(token: string | undefined | null): string | null {
  if (!token) return null;
  const dot = token.lastIndexOf(".");
  if (dot <= 0) return null;
  const vid = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  if (!/^[0-9a-f-]{16,64}$/i.test(vid)) return null;
  const expect = sign(vid);
  try {
    if (crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expect))) return vid;
  } catch {
    /* length mismatch */
  }
  return null;
}

/** 访客自己的签名令牌（分享助力/TG 绑定用；只能证明"这是某访客"，无法伪造他人） */
export function tokenOf(vid: string): string {
  return `${vid}.${sign(vid)}`;
}

/**
 * TG /start 载荷只许 [A-Za-z0-9_-]（无点号），用定长拼接替代 "." 分隔：
 * vid 是 36 位 UUID + 16 位 hex 签名 → "xz_" + 52 字符 ≤ 64 上限。
 */
export function packStartToken(vid: string): string {
  return `xz_${vid}${sign(vid)}`;
}

export function parseStartToken(payload: string): string | null {
  const m = payload.match(/^xz_([0-9a-f-]{36})([0-9a-f]{16})$/i);
  if (!m) return null;
  return verifyVisitorToken(`${m[1]}.${m[2]}`);
}

/* ---------- 日界与周期 ---------- */

export function dayKey(now = Date.now()): string {
  return new Date(now + TZ_MS).toISOString().slice(0, 10);
}

function monthKey(iso: string): string {
  return iso.slice(0, 7);
}

/** 尾部连续天数：从最后一颗珠往前数连续日 */
function trailingStreak(pearls: string[]): number {
  if (!pearls.length) return 0;
  let streak = 1;
  for (let i = pearls.length - 1; i > 0; i--) {
    const prev = Date.parse(pearls[i - 1]);
    const cur = Date.parse(pearls[i]);
    if (cur - prev === 86400000) streak += 1;
    else break;
  }
  return streak;
}

/** 就地清空当前周期（新一轮从下一颗珠开始） */
function resetCycle(rec: DragonRec) {
  rec.pearls = [];
  rec.cycleStart = 0;
  rec.assists = [];
  rec.wish = undefined;
  rec.wishAt = undefined;
  rec.code = undefined;
}

/** 周期过期（未集齐且超 30 天）→ 就地重置为新周期 */
function maybeExpireCycle(rec: DragonRec, now: number) {
  if (rec.pearls.length === 0) return;
  const done = rec.pearls.length >= PEARLS_TO_SUMMON;
  if (!done && now - rec.cycleStart > CYCLE_DAYS * 86400000) {
    resetCycle(rec);
  }
}

function blankRec(vid: string, now: number): DragonRec {
  return {
    vid,
    pearls: [],
    cycleStart: 0,
    summons: 0,
    loongSkin: false,
    bestStreak: 0,
    createdAt: new Date(now).toISOString(),
    updatedAt: new Date(now).toISOString(),
  };
}

/* ---------- 对外状态（发给客户端的最小视图） ---------- */

export interface DragonState {
  collected: number;
  todayCollected: boolean;
  /** 已收集日键（客户端画北斗用） */
  days: string[];
  streak: number;
  /** 本周期 7 天全连续 */
  perfect: boolean;
  /** 集齐且未许愿 → 可召唤 */
  canSummon: boolean;
  wish: WishKind | null;
  code: string | null;
  loongSkin: boolean;
  summons: number;
  /** 当前周期截止（0=尚未开始） */
  cycleEndsAt: number;
  /** 本月 trial 剩余名额（三选一界面展示"限量"） */
  trialLeft: number;
  /** 本周期还可被好友助力的次数 */
  assistsLeft: number;
  /** 是否已绑定 Telegram（bot 端同步收珠） */
  tgBound: boolean;
  /** 月令龙鳞数（0..∞，3 枚可兑大奖） */
  scales: number;
  /** 月鳞 ≥3，可领「界龙之约」大奖 */
  grandReady: boolean;
  /** 历史最佳连续天数（成就「北斗正位」） */
  bestStreak: number;
  /** 已兑「界龙之约」次数（成就） */
  grandClaims: number;
  /** 成功助力他人的累计次数（点星人） */
  assistsGiven: number;
}

function toState(rec: DragonRec, trialLeft: number, now: number): DragonState {
  const streak = trailingStreak(rec.pearls);
  return {
    collected: Math.min(PEARLS_TO_SUMMON, rec.pearls.length),
    todayCollected: rec.pearls.includes(dayKey(now)),
    days: [...rec.pearls],
    streak,
    perfect: rec.pearls.length >= PEARLS_TO_SUMMON && streak >= PEARLS_TO_SUMMON,
    canSummon: rec.pearls.length >= PEARLS_TO_SUMMON && !rec.wish,
    wish: rec.wish ?? null,
    code: rec.code ?? null,
    loongSkin: rec.loongSkin,
    summons: rec.summons,
    cycleEndsAt: rec.cycleStart ? rec.cycleStart + CYCLE_DAYS * 86400000 : 0,
    trialLeft,
    assistsLeft: Math.max(0, ASSIST_CAP - (rec.assists?.length ?? 0)),
    tgBound: !!rec.tgUserId,
    scales: rec.scales?.length ?? 0,
    grandReady: (rec.scales?.length ?? 0) >= SCALES_FOR_GRAND,
    bestStreak: rec.bestStreak,
    grandClaims: rec.grandClaims ?? 0,
    assistsGiven: rec.assistsGiven ?? 0,
  };
}

async function trialLeftThisMonth(db: DragonDb, now: number): Promise<number> {
  const mk = monthKey(new Date(now + TZ_MS).toISOString());
  const used = Object.values(db.codes).filter((c) => c.kind === "trial" && monthKey(c.issuedAt) === mk).length;
  return Math.max(0, TRIAL_QUOTA - used);
}

/** 只读状态（GET；不创建记录） */
export async function getDragonState(vid: string, now = Date.now()): Promise<DragonState> {
  const db = await readDb();
  const rec = db.byVid[vid];
  const trialLeft = await trialLeftThisMonth(db, now);
  if (!rec) return toState(blankRec(vid, now), trialLeft, now);
  maybeExpireCycle(rec, now); // 只影响返回视图；过期落库推迟到下次写操作，读路径保持无写
  return toState(rec, trialLeft, now);
}

/* ---------- 收珠 ---------- */

export interface CollectResult {
  ok: boolean;
  /** 今天这颗是第几颗（1-based；already/blocked 时为 0） */
  index: number;
  already: boolean;
  state: DragonState;
}

/** 收珠核心（调用方须已持有 serialize 锁并负责 writeDb）。assist=true 记入助力配额 */
function collectCore(rec: DragonRec, now: number, assist = false): { ok: boolean; index: number; already: boolean; changed: boolean } {
  maybeExpireCycle(rec, now);
  const today = dayKey(now);

  if (rec.pearls.includes(today)) return { ok: true, index: 0, already: true, changed: false };
  // 集齐但未许愿：先许愿再开新一轮（避免珠数溢出语义混乱）
  if (rec.pearls.length >= PEARLS_TO_SUMMON && !rec.wish) return { ok: false, index: 0, already: false, changed: false };
  // 上一轮已完成（集齐+已许愿）：这颗珠开启新一轮
  if (rec.pearls.length >= PEARLS_TO_SUMMON && rec.wish) resetCycle(rec);

  if (assist) {
    const used = rec.assists?.length ?? 0;
    if (used >= ASSIST_CAP) return { ok: false, index: 0, already: false, changed: false };
    (rec.assists ??= []).push(today);
  }
  if (rec.pearls.length === 0) rec.cycleStart = now;
  rec.pearls.push(today);
  rec.pearls.sort();
  if (rec.pearls.length >= PEARLS_TO_SUMMON) rec.summons += 1;
  rec.bestStreak = Math.max(rec.bestStreak, trailingStreak(rec.pearls));
  rec.updatedAt = new Date(now).toISOString();
  return { ok: true, index: rec.pearls.length, already: false, changed: true };
}

export async function collectPearl(vid: string, now = Date.now()): Promise<CollectResult> {
  return serialize(async () => {
    const db = await readDb();
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, now));
    const r = collectCore(rec, now);
    if (r.changed) await writeDb(db);
    const trialLeft = await trialLeftThisMonth(db, now);
    return { ok: r.ok, index: r.index, already: r.already, state: toState(rec, trialLeft, now) };
  });
}

export interface AssistResult {
  ok: boolean;
  reason?: "self" | "already" | "cap" | "blocked";
  /** 被助力方当前进度（展示"帮 TA 点亮了第 N 颗"） */
  collected: number;
  /** 双向奖励：帮点者自己的今日星珠也自动入袋（0=今日已收过/被阻） */
  helperIndex: number;
  helperCollected: number;
}

/**
 * 好友助力：持分享令牌者为分享人点亮"今日星珠"，同时自己的今日星珠也自动入袋（双向奖励）。
 * 限制：不能助力自己；分享人当日已收则幂等返回；每周期最多被助力 ASSIST_CAP 次。
 */
export async function assistPearl(sharerVid: string, helperVid: string, now = Date.now()): Promise<AssistResult> {
  return serialize(async () => {
    if (sharerVid === helperVid) return { ok: false, reason: "self", collected: 0, helperIndex: 0, helperCollected: 0 } as AssistResult;
    const db = await readDb();
    const rec = db.byVid[sharerVid] ?? (db.byVid[sharerVid] = blankRec(sharerVid, now));
    const r = collectCore(rec, now, true);
    /* 双向奖励：无论分享方是否成功（already/cap），来访者自己的今日珠都自动入袋——
       点开好友链接本身就是一次"到访"，与手动点珠等价，不构成额外滥用面 */
    const helper = db.byVid[helperVid] ?? (db.byVid[helperVid] = blankRec(helperVid, now));
    const hr = collectCore(helper, now);
    /* 点星人记账：真正点亮了对方的珠才计（already/cap 不算），滚动保留 14 天供周榜 */
    if (r.ok && !r.already) {
      helper.assistsGiven = (helper.assistsGiven ?? 0) + 1;
      helper.assistLog = [...(helper.assistLog ?? []).filter((ts) => now - ts < 14 * 86400000), now].slice(-50);
    }
    if (r.changed || hr.changed) await writeDb(db);

    const collected = Math.min(PEARLS_TO_SUMMON, rec.pearls.length);
    const helperCollected = Math.min(PEARLS_TO_SUMMON, helper.pearls.length);
    const base = { collected, helperIndex: hr.ok && !hr.already ? hr.index : 0, helperCollected };
    if (r.already) return { ok: false, reason: "already", ...base } as AssistResult;
    if (!r.ok) {
      const capped = (rec.assists?.length ?? 0) >= ASSIST_CAP;
      return { ok: false, reason: capped ? "cap" : "blocked", ...base } as AssistResult;
    }
    return { ok: true, ...base } as AssistResult;
  });
}

/* ---------- 月令龙鳞：三鳞兑「界龙之约」 ---------- */

export interface GrandResult {
  ok: boolean;
  reason?: "not_ready";
  code?: string;
  state: DragonState;
}

export async function claimGrand(vid: string, now = Date.now()): Promise<GrandResult> {
  return serialize(async () => {
    const db = await readDb();
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, now));
    if ((rec.scales?.length ?? 0) < SCALES_FOR_GRAND) {
      return { ok: false, reason: "not_ready", state: toState(rec, await trialLeftThisMonth(db, now), now) } as GrandResult;
    }
    rec.scales = (rec.scales ?? []).slice(SCALES_FOR_GRAND); // 消耗最早的 3 枚
    rec.grandClaims = (rec.grandClaims ?? 0) + 1;
    let code = genCode();
    while (db.codes[code]) code = genCode();
    db.codes[code] = {
      code,
      vid,
      kind: "grand",
      perfect: false,
      issuedAt: new Date(now).toISOString(),
      expiresAt: new Date(now + GRAND_TTL_DAYS * 86400000).toISOString(),
      redeemed: false,
    };
    rec.updatedAt = new Date(now).toISOString();
    await writeDb(db);
    return { ok: true, code, state: toState(rec, await trialLeftThisMonth(db, now), now) } as GrandResult;
  });
}

/* ---------- bot 每日提醒 ---------- */

/** 订阅/退订每日收珠提醒（bot 内联按钮） */
export async function setRemindByTg(userId: number, on: boolean): Promise<boolean> {
  return serialize(async () => {
    const db = await readDb();
    db.tgByUser ??= {};
    const key = String(userId);
    const vid = db.tgByUser[key] ?? `tg:${userId}`;
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, Date.now()));
    if (!db.tgByUser[key]) {
      db.tgByUser[key] = vid;
      rec.tgUserId = userId;
    }
    rec.remind = on;
    await writeDb(db);
    return on;
  });
}

/** 认领今日应发的提醒（乐观去重：先记 lastRemind 再发，失败也不会当日重发轰炸） */
export async function claimDueReminders(now = Date.now()): Promise<Array<{ userId: number; collected: number }>> {
  return serialize(async () => {
    const db = await readDb();
    const today = dayKey(now);
    const due: Array<{ userId: number; collected: number }> = [];
    for (const rec of Object.values(db.byVid)) {
      if (!rec.tgUserId || !rec.remind) continue;
      if (rec.lastRemind === today) continue;
      maybeExpireCycle(rec, now);
      if (rec.pearls.includes(today)) continue;
      if (rec.pearls.length >= PEARLS_TO_SUMMON) continue; // 集齐待许愿的不催
      rec.lastRemind = today;
      due.push({ userId: rec.tgUserId, collected: Math.min(PEARLS_TO_SUMMON, rec.pearls.length) });
    }
    if (due.length) await writeDb(db);
    return due;
  });
}

/* ---------- Telegram 绑定与 bot 端收珠 ---------- */

/** 把 src 的进度并入 target（TG 换绑/合并 bot 孤儿记录时） */
function mergeInto(target: DragonRec, src: DragonRec) {
  target.pearls = [...new Set([...target.pearls, ...src.pearls])].sort();
  target.assists = [...new Set([...(target.assists ?? []), ...(src.assists ?? [])])].sort();
  if (src.cycleStart) target.cycleStart = target.cycleStart ? Math.min(target.cycleStart, src.cycleStart) : src.cycleStart;
  target.wish = target.wish ?? src.wish;
  target.wishAt = target.wishAt ?? src.wishAt;
  target.code = target.code ?? src.code;
  target.summons += src.summons;
  target.loongSkin = target.loongSkin || src.loongSkin;
  target.bestStreak = Math.max(target.bestStreak, src.bestStreak);
}

export interface BindResult {
  ok: boolean;
  merged: boolean;
  state: DragonState;
}

/** 绑定 TG 账号到网页访客：bot 端此前的孤儿进度（tg:<id>）或旧 vid 一并合入 */
export async function bindTelegram(vid: string, userId: number, name?: string, now = Date.now()): Promise<BindResult> {
  return serialize(async () => {
    const db = await readDb();
    db.tgByUser ??= {};
    const key = String(userId);
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, now));
    const prevVid = db.tgByUser[key];
    let merged = false;
    if (prevVid && prevVid !== vid && db.byVid[prevVid]) {
      mergeInto(rec, db.byVid[prevVid]);
      delete db.byVid[prevVid];
      merged = true;
    }
    rec.tgUserId = userId;
    if (name) rec.tgName = name.slice(0, 32);
    db.tgByUser[key] = vid;
    rec.updatedAt = new Date(now).toISOString();
    await writeDb(db);
    const trialLeft = await trialLeftThisMonth(db, now);
    return { ok: true, merged, state: toState(rec, trialLeft, now) };
  });
}

/** bot 端 /xingzhu 收珠：已绑定走网页记录，未绑定挂孤儿记录（tg:<id>），日后绑定自动合并 */
export async function collectPearlByTg(userId: number, now = Date.now()): Promise<CollectResult & { bound: boolean }> {
  return serialize(async () => {
    const db = await readDb();
    db.tgByUser ??= {};
    const key = String(userId);
    const bound = !!db.tgByUser[key] && !db.tgByUser[key].startsWith("tg:");
    const vid = db.tgByUser[key] ?? `tg:${userId}`;
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, now));
    if (!db.tgByUser[key]) {
      db.tgByUser[key] = vid;
      rec.tgUserId = userId;
    }
    const r = collectCore(rec, now);
    if (r.changed || !bound) await writeDb(db);
    const trialLeft = await trialLeftThisMonth(db, now);
    return { ok: r.ok, index: r.index, already: r.already, bound, state: toState(rec, trialLeft, now) };
  });
}

/* ---------- 祈愿 ---------- */

function genCode(): string {
  const raw = crypto.randomBytes(8).toString("base64").replace(/[^A-Z0-9]/gi, "").toUpperCase();
  return `LOONG-${raw.slice(0, 6).padEnd(6, "8")}`;
}

export interface WishResult {
  ok: boolean;
  reason?: "not_ready" | "already_wished" | "quota";
  code?: string;
  state: DragonState;
}

export async function makeWish(vid: string, kind: WishKind, now = Date.now()): Promise<WishResult> {
  return serialize(async () => {
    const db = await readDb();
    const rec = db.byVid[vid] ?? (db.byVid[vid] = blankRec(vid, now));
    maybeExpireCycle(rec, now);
    const trialLeft = await trialLeftThisMonth(db, now);

    if (rec.pearls.length < PEARLS_TO_SUMMON) {
      return { ok: false, reason: "not_ready", state: toState(rec, trialLeft, now) };
    }
    if (rec.wish) {
      return { ok: false, reason: "already_wished", code: rec.code, state: toState(rec, trialLeft, now) };
    }
    if (kind === "trial" && trialLeft <= 0) {
      return { ok: false, reason: "quota", state: toState(rec, trialLeft, now) };
    }

    const perfect = trailingStreak(rec.pearls) >= PEARLS_TO_SUMMON;
    rec.wish = kind;
    rec.wishAt = new Date(now).toISOString();
    /* 月令龙鳞：完成周期当月获得一枚（每自然月最多一枚），集 3 枚可兑「界龙之约」 */
    const mk = monthKey(new Date(now + TZ_MS).toISOString());
    if (!(rec.scales ?? []).includes(mk)) (rec.scales ??= []).push(mk);

    if (kind === "skin") {
      rec.loongSkin = true;
    } else {
      let code = genCode();
      while (db.codes[code]) code = genCode(); // 碰撞重生成
      db.codes[code] = {
        code,
        vid,
        kind,
        perfect,
        issuedAt: new Date(now).toISOString(),
        expiresAt: new Date(now + CODE_TTL_DAYS * 86400000).toISOString(),
        redeemed: false,
      };
      rec.code = code;
    }
    // 北斗正位加成：无论许什么愿，皮肤直接一并解锁（零成本高感知，鼓励连续 7 天）
    if (perfect) rec.loongSkin = true;

    rec.updatedAt = new Date(now).toISOString();
    await writeDb(db);
    const left = await trialLeftThisMonth(db, now);
    return { ok: true, code: rec.code, state: toState(rec, left, now) };
  });
}

/* ---------- Telegram 核销 ---------- */

export type DragonRedeem =
  | { ok: true; rec: DragonCode; already: boolean }
  | { ok: false; reason: "not_found" | "expired"; rec?: DragonCode };

export async function redeemDragonCode(code: string, by: number, name?: string): Promise<DragonRedeem> {
  const norm = code.trim().toUpperCase();
  return serialize(async () => {
    const db = await readDb();
    const rec = db.codes[norm];
    if (!rec) return { ok: false, reason: "not_found" } as DragonRedeem;
    if (!rec.redeemed && Date.now() > Date.parse(rec.expiresAt)) {
      return { ok: false, reason: "expired", rec } as DragonRedeem;
    }
    const already = rec.redeemed;
    if (!rec.redeemed) {
      rec.redeemed = true;
      rec.redeemedAt = new Date().toISOString();
      rec.redeemedBy = by;
      if (name) rec.redeemedName = name.slice(0, 64);
      await writeDb(db);
    }
    return { ok: true, rec, already } as DragonRedeem;
  });
}

/* ---------- 管理面板计数 ---------- */

export interface DragonCounts {
  visitors: number;
  activeCycles: number;
  summons: number;
  wishes: { trial: number; skin: number; gift: number };
  codesIssued: number;
  codesRedeemed: number;
  skinOwners: number;
  trialLeftThisMonth: number;
  scalesHeld: number;
  grandIssued: number;
  grandRedeemed: number;
  tgBound: number;
  reminders: number;
}

export async function dragonCounts(now = Date.now()): Promise<DragonCounts> {
  const db = await readDb();
  const recs = Object.values(db.byVid);
  const codes = Object.values(db.codes);
  const wishes = { trial: 0, skin: 0, gift: 0 };
  for (const c of codes) if (c.kind !== "grand") wishes[c.kind] += 1;
  wishes.skin = recs.filter((r) => r.wish === "skin").length;
  return {
    visitors: recs.length,
    activeCycles: recs.filter((r) => r.pearls.length > 0 && r.pearls.length < PEARLS_TO_SUMMON).length,
    summons: recs.reduce((s, r) => s + r.summons, 0),
    wishes,
    codesIssued: codes.length,
    codesRedeemed: codes.filter((c) => c.redeemed).length,
    skinOwners: recs.filter((r) => r.loongSkin).length,
    trialLeftThisMonth: await trialLeftThisMonth(db, now),
    scalesHeld: recs.reduce((s, r) => s + (r.scales?.length ?? 0), 0),
    grandIssued: codes.filter((c) => c.kind === "grand").length,
    grandRedeemed: codes.filter((c) => c.kind === "grand" && c.redeemed).length,
    tgBound: recs.filter((r) => !!r.tgUserId && !r.vid.startsWith("tg:")).length,
    reminders: recs.filter((r) => r.remind).length,
  };
}

/** 昵称脱敏：只露首字符（"阿明"→"阿＊"，"Alice"→"A＊＊"），频道公示不暴露完整身份 */
function maskName(name: string): string {
  const n = name.trim();
  if (!n) return "无名旅人";
  return n.slice(0, 1) + "＊".repeat(Math.min(3, Math.max(1, n.length - 1)));
}

/** 频道战报数据（周报/自动帖用）：近 7 天召唤、本月名额、点星人周榜（TG 绑定者，脱敏） */
export async function dragonWeekly(now = Date.now()): Promise<{
  weekSummons: number;
  weekNewCollectors: number;
  trialLeft: number;
  skinOwners: number;
  topHelpers: Array<{ name: string; n: number }>;
}> {
  const db = await readDb();
  const recs = Object.values(db.byVid);
  const wkStart = now - 7 * 86400000;
  const topHelpers = recs
    .map((r) => ({
      name: r.tgName ? maskName(r.tgName) : "",
      n: (r.assistLog ?? []).filter((ts) => ts >= wkStart).length,
    }))
    .filter((h) => h.name && h.n > 0)
    .sort((a, b) => b.n - a.n)
    .slice(0, 3);
  return {
    weekSummons: recs.filter((r) => r.wishAt && Date.parse(r.wishAt) >= wkStart).length,
    weekNewCollectors: recs.filter((r) => Date.parse(r.createdAt) >= wkStart && r.pearls.length > 0).length,
    trialLeft: await trialLeftThisMonth(db, now),
    skinOwners: recs.filter((r) => r.loongSkin).length,
    topHelpers,
  };
}
