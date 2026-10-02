import { readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";
import { fmtDur } from "./fmt-dur";
import { personaZh } from "./chatx-persona";
import { AD_CATEGORIES, AD_CREATIVES, parseAdSrc } from "./chatx-ad-src";

export { fmtDur };

/**
 * ChatX 广告日报：把 events.jsonl 里 bot / 落地页 / 下载 / 小程序的埋点按「日 × 来源码」聚合成
 * 投放能直接看的口径，并给出可执行的诊断。**人**一律按 Telegram uid 去重（落地/点击拿不到 uid 时
 * 退回会话 sid、再退回 IP），所以同一人点三次下载只算 1 个下载人。
 *
 * 口径：
 *   starts      /start 次数；users = 去重人数；newUsers = 第一次来的人数（chatx_bot_start.first）
 *   landUsers   到落地页的人数（chatx_landing_view）
 *   clickUsers  点了下载按钮的人数（chatx_download_click）
 *   dlUsers     真实请求了安装包的人数（download_redirect.installer，非 HEAD / Range）
 *   chatUsers   在 bot 或小程序里聊过的人数；msgs = 用户发出的消息条数（AI 回复、知识库回复、语音、非文字都算一条，
 *               点按钮 / 命令不算对话）
 *   sessions    互动会话数：同一 uid 相邻事件间隔 ≤30 分钟算一段；durSec = 各段首尾时长之和
 *   ips         去重来源 IP 数（只来自落地 / 安装包请求这两条 HTTP 请求；Telegram 不透传用户 IP）
 *   pushSent / pushReplied  主动推送发出数 / 推送后 24h 内回话人数
 *
 * 另按日出一份「功能面」（features，不分来源）：语音进/出条数与失败原因、人设消息分布与切换、推送失败数，
 * 用来调语音配额 / 看人设受欢迎程度，与投放漏斗分开看。
 */

export type Ev = {
  t: string;
  event: string;
  props: Record<string, unknown> | null;
  sid?: string;
  ip?: string;
  ua?: string;
  path?: string;
};

export type DailySrcRow = {
  day: string;
  src: string;
  starts: number;
  users: number;
  newUsers: number;
  landUsers: number;
  clickUsers: number;
  dlUsers: number;
  chatUsers: number;
  msgs: number;
  voiceMsgs: number;
  leads: number;
  sessions: number;
  durSec: number;
  avgDurSec: number;
  ips: number;
  topIps: { ip: string; n: number }[];
  pushSent: number;
  pushReplied: number;
};

export type DailyTotals = Omit<DailySrcRow, "src">;

export type Insight = { level: "good" | "warn" | "bad" | "info"; src?: string; text: string };

/** 语音 / 人设 / 推送的日功能面（不分来源）。fail 的 key 是埋点 why：tts_failed / asr_failed / too_long / cooldown / user_quota / global_quota / send_failed。 */
export type DailyFeatures = {
  day: string;
  voiceIn: number;
  voiceInOk: number;
  voiceInFail: Record<string, number>;
  voiceInUsers: number;
  voiceOut: number;
  voiceOutOk: number;
  voiceOutFail: Record<string, number>;
  voiceOutSec: number;
  voiceOutUsers: number;
  /** 各人设下用户发出的消息数（chatx_bot_ai.persona，缺省算 xiaojie）。 */
  personaMsgs: Record<string, number>;
  /** 各人设下开口聊过的人数。 */
  personaUsers: Record<string, number>;
  /** 切换到某人设的次数（/persona 命令或按钮，不含重复选同一人设）。 */
  personaSwitches: Record<string, number>;
  pushFail: number;
  /** 用户在 /voices 里选音色的次数，key = 音色（SSB#### / mine / default）。 */
  voicePicks: Record<string, number>;
  /** 成功发出的语音回复按音色分（voice 缺省看 ref：persona / default）。 */
  voiceOutByVoice: Record<string, number>;
  /** 克隆漏斗：开始 → 同意 → 样本通过；fail 的 key 是拒收原因。 */
  cloneStart: number;
  cloneConsent: number;
  cloneOk: number;
  cloneFail: Record<string, number>;
  cloneDel: number;
  /** 克隆成功按广告来源分。 */
  cloneOkBySrc: Record<string, number>;
  /** 报障时给出的已知问题（按 id）：展示 → 用户点「解决了」/「转人工」。 */
  knownShown: Record<string, number>;
  knownSolved: Record<string, number>;
  knownEscalated: Record<string, number>;
  /** 定时帖按钮点击（key = 帖子 id） */
  postClicks: Record<string, number>;
  /** 经帖子按钮进 bot 的人数 / 其中请求了安装包的人数（key = 帖子 id，按 uid 去重） */
  postUsers: Record<string, number>;
  postDl: Record<string, number>;
};

/** 单帖漏斗：按钮点击 → 进 bot 的人 → 请求安装包的人（下载按 uid 归到该人最近一次 /start 带的帖子）。 */
export type PostFunnel = { post: number; clicks: number; users: number; dlUsers: number };

export type ChatxDailyReport = {
  days: string[];
  rows: DailySrcRow[];
  totals: DailyTotals[];
  bySrc: (Omit<DailySrcRow, "day"> & { days: number })[];
  features: DailyFeatures[];
  /** 整个窗口（所有日 × 所有来源）按人去重的合计。 */
  overall: Omit<DailySrcRow, "day" | "src">;
  insights: Insight[];
  generatedAt: string;
};

const EVENTS = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
export const TZ_MS = Number(process.env.TZ_OFFSET ?? 8) * 3600_000;
const SESSION_GAP_MS = 30 * 60_000;
const PUSH_REPLY_WINDOW_MS = 24 * 3600_000;

export function dayKey(iso: string, tzMs = TZ_MS): string {
  const t = Date.parse(iso);
  if (isNaN(t)) return "unknown";
  return new Date(t + tzMs).toISOString().slice(0, 10);
}

/** 今天（运营时区）往前 n 天的日期键列表，旧 → 新。 */
export function recentDays(n: number, now = Date.now(), tzMs = TZ_MS): string[] {
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) out.push(new Date(now + tzMs - i * 86_400_000).toISOString().slice(0, 10));
  return out;
}

export async function readEvents(file = EVENTS, sinceMs = 0): Promise<Ev[]> {
  let raw = "";
  try {
    raw = await readFile(file, "utf-8");
  } catch {
    return [];
  }
  const out: Ev[] = [];
  for (const l of raw.split("\n")) {
    if (!l) continue;
    try {
      const e = JSON.parse(l) as Ev;
      if (!e || typeof e.event !== "string" || typeof e.t !== "string") continue;
      if (sinceMs && Date.parse(e.t) < sinceMs) continue;
      out.push(e);
    } catch {
      /* skip bad line */
    }
  }
  return out;
}

const SRC_RE = /^[A-Za-z0-9_-]{1,48}$/;
function srcOf(e: Ev): string {
  const s = e.props?.src;
  return typeof s === "string" && SRC_RE.test(s) ? s : "organic";
}
function uidOf(e: Ev): number | undefined {
  const p = e.props ?? {};
  const v = p.uid ?? p.tg;
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d{1,20}$/.test(v) ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : undefined;
}
/** 人的标识：uid > 会话 sid > IP；三者都没有就按事件自身算一个人（不会漏计，只可能高估）。 */
function personKey(e: Ev): string {
  const uid = uidOf(e);
  if (uid) return `u:${uid}`;
  if (e.sid) return `s:${e.sid}`;
  if (e.ip && e.ip !== "unknown") return `i:${e.ip}`;
  return `e:${e.t}`;
}

/** 用户主动发出的对话消息（bot 文本 / 语音 / 非文字，小程序 AI 提问）。 */
const MSG_EVENTS = new Set(["chatx_bot_ai", "chatx_bot_nontext", "chatx_bot_voice_in"]);
/** 算进互动会话时长的用户行为（bot 主动发的提醒 / 推送不算）。 */
const ACTIVITY_EVENTS = new Set([
  "chatx_bot_start",
  "chatx_bot_ai",
  "chatx_bot_nontext",
  "chatx_bot_voice_in",
  "chatx_bot_cmd",
  "chatx_bot_lead",
  "chatx_bot_human",
  "chatx_bot_persona",
  "chatx_landing_view",
  "chatx_download_click",
  "download_redirect",
]);

type Acc = {
  starts: number;
  users: Set<string>;
  newUsers: Set<string>;
  land: Set<string>;
  click: Set<string>;
  dl: Set<string>;
  chat: Set<string>;
  msgs: number;
  voiceMsgs: number;
  leads: number;
  ips: Map<string, number>;
  pushSent: number;
  pushReplied: Set<string>;
  sessions: number;
  durSec: number;
};

function newAcc(): Acc {
  return {
    starts: 0,
    users: new Set(),
    newUsers: new Set(),
    land: new Set(),
    click: new Set(),
    dl: new Set(),
    chat: new Set(),
    msgs: 0,
    voiceMsgs: 0,
    leads: 0,
    ips: new Map(),
    pushSent: 0,
    pushReplied: new Set(),
    sessions: 0,
    durSec: 0,
  };
}

function postOf(e: Ev): number | undefined {
  const v = e.props?.post;
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d{1,9}$/.test(v) ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

/** uid → 最近一次带帖子号的 /start；不带帖子号的 /start（直接搜 bot / 广告深链）不覆盖，下载仍算帖子的。 */
function postByUidOf(events: Ev[]): Map<number, number> {
  const m = new Map<number, number>();
  for (const e of events) {
    if (e.event !== "chatx_bot_start") continue;
    const uid = uidOf(e);
    const post = postOf(e);
    if (uid && post) m.set(uid, post);
  }
  return m;
}

/**
 * 全期单帖漏斗（不分日），给运营页的帖子表用。传 groupKey（帖子 id → 系列 id）就按系列合计，
 * 人数在整个系列内按 uid 去重（同一人点了两期只算一个人）。
 */
export function buildPostFunnel(
  events: Ev[],
  groupKey: (post: number) => number = (p) => p,
  win: { from?: number; to?: number } = {}
): Map<number, PostFunnel> {
  const postByUid = postByUidOf(events);
  const inWin = (e: Ev) => {
    const t = Date.parse(e.t);
    return (win.from === undefined || t >= win.from) && (win.to === undefined || t < win.to);
  };
  const acc = new Map<number, { clicks: number; users: Set<number>; dl: Set<number> }>();
  const cell = (post: number) => {
    const k = groupKey(post);
    let a = acc.get(k);
    if (!a) acc.set(k, (a = { clicks: 0, users: new Set(), dl: new Set() }));
    return a;
  };
  for (const e of events) {
    if (!inWin(e)) continue;
    if (e.event === "tg_post_click") {
      const post = postOf(e);
      if (post) cell(post).clicks++;
    } else if (e.event === "chatx_bot_start") {
      const post = postOf(e);
      const uid = uidOf(e);
      if (post && uid) cell(post).users.add(uid);
    } else if (e.event === "download_redirect" && e.props?.installer !== false) {
      const uid = uidOf(e);
      const post = uid && postByUid.get(uid);
      if (post) cell(post).dl.add(uid);
    }
  }
  const out = new Map<number, PostFunnel>();
  for (const [post, a] of acc) out.set(post, { post, clicks: a.clicks, users: a.users.size, dlUsers: a.dl.size });
  return out;
}

function isChatxMiniChat(e: Ev): boolean {
  return e.event === "miniapp_chat" && e.props?.scene === "chatx";
}

const bump = (m: Record<string, number>, k: string, n = 1) => {
  m[k] = (m[k] ?? 0) + n;
};
const PERSONA_RE = /^[a-z]{1,16}$/;
function personaOf(e: Ev): string {
  const p = e.props?.persona;
  return typeof p === "string" && PERSONA_RE.test(p) ? p : "xiaojie";
}

export function buildFeatures(events: Ev[], days: string[], srcFor: (e: Ev) => string = srcOf): DailyFeatures[] {
  type FAcc = Omit<DailyFeatures, "voiceInUsers" | "voiceOutUsers" | "personaUsers" | "postUsers" | "postDl"> & {
    vinUsers: Set<string>;
    voutUsers: Set<string>;
    pUsers: Map<string, Set<string>>;
    postU: Map<string, Set<number>>;
    postD: Map<string, Set<number>>;
  };
  const postByUid = postByUidOf(events);
  const addTo = (m: Map<string, Set<number>>, k: string, uid: number) => {
    let s = m.get(k);
    if (!s) m.set(k, (s = new Set()));
    s.add(uid);
  };
  const acc = new Map<string, FAcc>();
  for (const day of days) {
    acc.set(day, {
      day,
      voiceIn: 0,
      voiceInOk: 0,
      voiceInFail: {},
      voiceOut: 0,
      voiceOutOk: 0,
      voiceOutFail: {},
      voiceOutSec: 0,
      personaMsgs: {},
      personaSwitches: {},
      pushFail: 0,
      voicePicks: {},
      voiceOutByVoice: {},
      cloneStart: 0,
      cloneConsent: 0,
      cloneOk: 0,
      cloneFail: {},
      cloneDel: 0,
      cloneOkBySrc: {},
      knownShown: {},
      knownSolved: {},
      knownEscalated: {},
      postClicks: {},
      vinUsers: new Set(),
      voutUsers: new Set(),
      pUsers: new Map(),
      postU: new Map(),
      postD: new Map(),
    });
  }
  for (const e of events) {
    const a = acc.get(dayKey(e.t));
    if (!a) continue;
    const p = e.props ?? {};
    const why = typeof p.why === "string" ? p.why : "unknown";
    if (e.event === "chatx_bot_voice_in") {
      a.voiceIn++;
      a.vinUsers.add(personKey(e));
      if (p.ok === false) bump(a.voiceInFail, why);
      else a.voiceInOk++;
    } else if (e.event === "chatx_bot_voice_out") {
      a.voiceOut++;
      a.voutUsers.add(personKey(e));
      if (p.ok === false) bump(a.voiceOutFail, why);
      else {
        a.voiceOutOk++;
        if (typeof p.sec === "number") a.voiceOutSec += p.sec;
        bump(a.voiceOutByVoice, typeof p.voice === "string" ? p.voice : typeof p.ref === "string" ? p.ref : "default");
      }
    } else if (e.event === "chatx_bot_voice_pick" && typeof p.voice === "string") {
      bump(a.voicePicks, p.voice);
    } else if (e.event === "chatx_bot_voice_clone") {
      if (p.step === "start") a.cloneStart++;
      else if (p.step === "consent") a.cloneConsent++;
      else if (p.step === "delete") a.cloneDel++;
      else if (p.step === "sample" && p.ok === true) {
        a.cloneOk++;
        bump(a.cloneOkBySrc, srcFor(e));
      } else if (p.step === "sample") bump(a.cloneFail, typeof p.issue === "string" ? p.issue : why);
    } else if (e.event === "chatx_bot_bug" && typeof p.known === "string" && p.known) {
      if (p.step === "note" || p.step === "fp") bump(a.knownShown, p.known);
      else if (p.step === "solved") bump(a.knownSolved, p.known);
      else if (p.step === "escalate") bump(a.knownEscalated, p.known);
    } else if (e.event === "tg_post_click" && p.post !== undefined) {
      bump(a.postClicks, String(p.post));
    } else if (e.event === "chatx_bot_start" && postOf(e) && uidOf(e)) {
      addTo(a.postU, String(postOf(e)), uidOf(e)!);
    } else if (e.event === "download_redirect" && p.installer !== false && uidOf(e) && postByUid.has(uidOf(e)!)) {
      addTo(a.postD, String(postByUid.get(uidOf(e)!)), uidOf(e)!);
    } else if (e.event === "chatx_bot_ai") {
      const per = personaOf(e);
      bump(a.personaMsgs, per);
      let s = a.pUsers.get(per);
      if (!s) a.pUsers.set(per, (s = new Set()));
      s.add(personKey(e));
    } else if (e.event === "chatx_bot_cmd" && p.cmd === "persona" && typeof p.from === "string" && p.from !== p.persona) {
      bump(a.personaSwitches, personaOf(e));
    } else if (e.event === "chatx_bot_push" && p.ok === false) {
      a.pushFail++;
    }
  }
  return days
    .slice()
    .reverse()
    .map((day) => {
      const { vinUsers, voutUsers, pUsers, postU, postD, ...rest } = acc.get(day)!;
      const sizes = (m: Map<string, Set<number | string>>) => Object.fromEntries([...m].map(([k, s]) => [k, s.size]));
      return { ...rest, voiceInUsers: vinUsers.size, voiceOutUsers: voutUsers.size, personaUsers: sizes(pUsers), postUsers: sizes(postU), postDl: sizes(postD) };
    });
}

/** 同一 uid 的活动按 30 分钟间隔切成会话，会话归到首事件的日 × 来源。 */
function sessionize(events: Ev[], srcByUid: Map<number, string>): { day: string; src: string; durSec: number }[] {
  const byUid = new Map<number, { t: number; day: string; src: string }[]>();
  for (const e of events) {
    if (!ACTIVITY_EVENTS.has(e.event)) continue;
    const uid = uidOf(e);
    if (!uid) continue;
    const t = Date.parse(e.t);
    if (isNaN(t)) continue;
    const list = byUid.get(uid) ?? [];
    list.push({ t, day: dayKey(e.t), src: srcOf(e) });
    byUid.set(uid, list);
  }
  const out: { day: string; src: string; durSec: number }[] = [];
  for (const [uid, list] of byUid) {
    list.sort((a, b) => a.t - b.t);
    let start = list[0];
    let last = list[0].t;
    for (let i = 1; i <= list.length; i++) {
      const cur = list[i];
      if (cur && cur.t - last <= SESSION_GAP_MS) {
        last = cur.t;
        continue;
      }
      out.push({ day: start.day, src: srcByUid.get(uid) ?? start.src, durSec: Math.round((last - start.t) / 1000) });
      if (cur) {
        start = cur;
        last = cur.t;
      }
    }
  }
  return out;
}

export function buildDailyReport(events: Ev[], days: string[], now = Date.now()): ChatxDailyReport {
  const daySet = new Set(days);
  // 用户 → 最近一次 /start 的来源：让落地 / 下载 / 对话这些不带 src 的事件也能归到人所属的广告。
  const srcByUid = new Map<number, string>();
  for (const e of events) {
    if (e.event !== "chatx_bot_start") continue;
    const uid = uidOf(e);
    if (uid) srcByUid.set(uid, srcOf(e));
  }
  const attributedSrc = (e: Ev): string => {
    const explicit = e.props?.src;
    if (typeof explicit === "string" && SRC_RE.test(explicit)) return explicit;
    const uid = uidOf(e);
    return (uid && srcByUid.get(uid)) || "organic";
  };

  const cells = new Map<string, Acc>();
  const cell = (day: string, src: string) => {
    const k = `${day}\u0000${src}`;
    let a = cells.get(k);
    if (!a) {
      a = newAcc();
      cells.set(k, a);
    }
    return a;
  };

  // 主动推送：发出时间按 uid 记，之后 24h 内该用户任何一条消息算「推送后回话」
  const pushAt = new Map<number, { t: number; day: string; src: string }[]>();
  for (const e of events) {
    if (e.event !== "chatx_bot_push" || e.props?.ok === false) continue;
    const uid = uidOf(e);
    if (!uid) continue;
    const l = pushAt.get(uid) ?? [];
    l.push({ t: Date.parse(e.t), day: dayKey(e.t), src: attributedSrc(e) });
    pushAt.set(uid, l);
  }
  const pushRepliedMarked = new Set<string>();

  for (const e of events) {
    const day = dayKey(e.t);
    if (!daySet.has(day)) continue;
    const ev = e.event;
    const who = personKey(e);
    const src = attributedSrc(e);

    if (ev === "chatx_bot_start") {
      const a = cell(day, src);
      a.starts++;
      a.users.add(who);
      if (e.props?.first === true) a.newUsers.add(who);
    } else if (ev === "chatx_landing_view") {
      const a = cell(day, src);
      a.land.add(who);
      if (e.ip && e.ip !== "unknown") a.ips.set(e.ip, (a.ips.get(e.ip) ?? 0) + 1);
    } else if (ev === "chatx_download_click") {
      cell(day, src).click.add(who);
    } else if (ev === "download_redirect") {
      const a = cell(day, src);
      if (e.props?.installer !== false) a.dl.add(who);
      if (e.ip && e.ip !== "unknown") a.ips.set(e.ip, (a.ips.get(e.ip) ?? 0) + 1);
    } else if (MSG_EVENTS.has(ev) || isChatxMiniChat(e)) {
      const a = cell(day, src);
      a.msgs++;
      a.chat.add(who);
      if (ev === "chatx_bot_voice_in") a.voiceMsgs++;
      const uid = uidOf(e);
      const t = Date.parse(e.t);
      if (uid && pushAt.has(uid)) {
        for (const p of pushAt.get(uid)!) {
          const k = `${uid}:${p.t}`;
          if (t >= p.t && t - p.t <= PUSH_REPLY_WINDOW_MS && !pushRepliedMarked.has(k)) {
            pushRepliedMarked.add(k);
            if (daySet.has(p.day)) cell(p.day, p.src).pushReplied.add(String(uid));
          }
        }
      }
    } else if (ev === "chatx_bot_lead") {
      cell(day, src).leads++;
    } else if (ev === "chatx_bot_push") {
      if (e.props?.ok !== false) cell(day, src).pushSent++;
    }
  }

  for (const s of sessionize(events, srcByUid)) {
    if (!daySet.has(s.day)) continue;
    const a = cell(s.day, s.src);
    a.sessions++;
    a.durSec += s.durSec;
  }

  const toRow = (day: string, src: string, a: Acc): DailySrcRow => ({
    day,
    src,
    starts: a.starts,
    users: a.users.size,
    newUsers: a.newUsers.size,
    landUsers: a.land.size,
    clickUsers: a.click.size,
    dlUsers: a.dl.size,
    chatUsers: a.chat.size,
    msgs: a.msgs,
    voiceMsgs: a.voiceMsgs,
    leads: a.leads,
    sessions: a.sessions,
    durSec: a.durSec,
    avgDurSec: a.sessions ? Math.round(a.durSec / a.sessions) : 0,
    ips: a.ips.size,
    topIps: [...a.ips.entries()]
      .sort((x, y) => y[1] - x[1])
      .slice(0, 3)
      .map(([ip, n]) => ({ ip, n })),
    pushSent: a.pushSent,
    pushReplied: a.pushReplied.size,
  });

  const rows: DailySrcRow[] = [];
  for (const [k, a] of cells) {
    const [day, src] = k.split("\u0000");
    rows.push(toRow(day, src, a));
  }
  rows.sort((x, y) => (x.day === y.day ? y.users - x.users || y.starts - x.starts : x.day < y.day ? 1 : -1));

  // 日合计 / 来源合计：重新按人去重（不能把各来源的人数直接相加）
  const dayAcc = new Map<string, Acc>();
  const srcAcc = new Map<string, Acc & { days: Set<string> }>();
  for (const [k, a] of cells) {
    const [day, src] = k.split("\u0000");
    for (const [key, target] of [
      [day, dayAcc] as const,
      [src, srcAcc] as const,
    ]) {
      let t = target.get(key);
      if (!t) {
        t = Object.assign(newAcc(), { days: new Set<string>() });
        target.set(key, t);
      }
      t.starts += a.starts;
      a.users.forEach((u) => t!.users.add(u));
      a.newUsers.forEach((u) => t!.newUsers.add(u));
      a.land.forEach((u) => t!.land.add(u));
      a.click.forEach((u) => t!.click.add(u));
      a.dl.forEach((u) => t!.dl.add(u));
      a.chat.forEach((u) => t!.chat.add(u));
      t.msgs += a.msgs;
      t.voiceMsgs += a.voiceMsgs;
      t.leads += a.leads;
      a.ips.forEach((n, ip) => t!.ips.set(ip, (t!.ips.get(ip) ?? 0) + n));
      t.pushSent += a.pushSent;
      a.pushReplied.forEach((u) => t!.pushReplied.add(u));
      t.sessions += a.sessions;
      t.durSec += a.durSec;
      if ("days" in t) (t as Acc & { days: Set<string> }).days.add(day);
    }
  }
  const totals: DailyTotals[] = days
    .slice()
    .reverse()
    .map((day) => {
      const a = dayAcc.get(day) ?? newAcc();
      const { src: _s, ...rest } = toRow(day, "", a);
      void _s;
      return rest;
    });
  const bySrc = [...srcAcc.entries()]
    .map(([src, a]) => {
      const { day: _d, ...rest } = toRow("", src, a);
      void _d;
      return { ...rest, days: a.days.size };
    })
    .sort((x, y) => y.users - x.users || y.starts - x.starts);

  const all = newAcc();
  for (const a of cells.values()) {
    all.starts += a.starts;
    a.users.forEach((u) => all.users.add(u));
    a.newUsers.forEach((u) => all.newUsers.add(u));
    a.land.forEach((u) => all.land.add(u));
    a.click.forEach((u) => all.click.add(u));
    a.dl.forEach((u) => all.dl.add(u));
    a.chat.forEach((u) => all.chat.add(u));
    all.msgs += a.msgs;
    all.voiceMsgs += a.voiceMsgs;
    all.leads += a.leads;
    a.ips.forEach((n, ip) => all.ips.set(ip, (all.ips.get(ip) ?? 0) + n));
    all.pushSent += a.pushSent;
    a.pushReplied.forEach((u) => all.pushReplied.add(u));
    all.sessions += a.sessions;
    all.durSec += a.durSec;
  }
  const { day: _od, src: _os, ...overall } = toRow("", "", all);
  void _od;
  void _os;

  const features = buildFeatures(events, days, attributedSrc);
  return { days, rows, totals, bySrc, features, overall, insights: [...buildInsights(bySrc, totals, rows, now), ...buildFeatureInsights(features)], generatedAt: new Date(now).toISOString() };
}

const sumRec = (m: Record<string, number>) => Object.values(m).reduce((a, b) => a + b, 0);
const topKey = (m: Record<string, number>) => Object.entries(m).sort((x, y) => y[1] - x[1])[0]?.[0];

const WHY_ZH: Record<string, string> = {
  tts_failed: "TTS 中继失败（查 GPU / 中继进程）",
  send_failed: "Telegram 发送失败",
  asr_failed: "ASR 转写失败（查中继 / 音频格式）",
  too_long: "内容超长",
  cooldown: "触发 12 秒冷却",
  user_quota: "撞到每人每日上限（CHATX_VOICE_USER_DAILY_MAX）",
  global_quota: "撞到全局每日上限（CHATX_VOICE_DAILY_MAX）",
};

const CLONE_WHY_ZH: Record<string, string> = {
  rejected: "体检不通过",
  too_long: "录音超过 60 秒",
  not_decodable: "音频解不开",
  download_failed: "文件下载失败",
};

/** 功能面诊断：语音失败率、配额是否卡人、人设分布；样本 ≥5 才下结论。 */
export function buildFeatureInsights(features: DailyFeatures[]): Insight[] {
  const out: Insight[] = [];
  const MIN = 5;
  const all = features.reduce(
    (a, f) => {
      a.vin += f.voiceIn;
      a.vinOk += f.voiceInOk;
      a.vout += f.voiceOut;
      a.voutOk += f.voiceOutOk;
      a.voutSec += f.voiceOutSec;
      for (const [k, n] of Object.entries(f.voiceInFail)) bump(a.vinFail, k, n);
      for (const [k, n] of Object.entries(f.voiceOutFail)) bump(a.voutFail, k, n);
      for (const [k, n] of Object.entries(f.personaMsgs)) bump(a.pMsgs, k, n);
      for (const [k, n] of Object.entries(f.personaSwitches)) bump(a.pSw, k, n);
      a.pushFail += f.pushFail;
      a.cStart += f.cloneStart;
      a.cConsent += f.cloneConsent;
      a.cOk += f.cloneOk;
      for (const [k, n] of Object.entries(f.cloneFail)) bump(a.cFail, k, n);
      return a;
    },
    { vin: 0, vinOk: 0, vout: 0, voutOk: 0, voutSec: 0, vinFail: {} as Record<string, number>, voutFail: {} as Record<string, number>, pMsgs: {} as Record<string, number>, pSw: {} as Record<string, number>, pushFail: 0, cStart: 0, cConsent: 0, cOk: 0, cFail: {} as Record<string, number> }
  );

  if (all.vout >= MIN) {
    const failN = all.vout - all.voutOk;
    const rate = pct(failN, all.vout);
    const quota = (all.voutFail.user_quota ?? 0) + (all.voutFail.global_quota ?? 0) + (all.voutFail.cooldown ?? 0);
    if (rate >= 20) {
      const top = topKey(all.voutFail)!;
      out.push({
        level: "bad",
        text: `语音回复 ${all.vout} 次里 ${failN} 次没发出去（${rate}%），主要原因：${WHY_ZH[top] ?? top}（${all.voutFail[top]} 次）。`,
      });
    } else if (quota >= Math.max(3, all.vout * 0.1)) {
      out.push({
        level: "warn",
        text: `语音有 ${quota} 次被配额 / 冷却挡住（占 ${pct(quota, all.vout)}%），用户想听却没听到：GPU 扛得住就调大 CHATX_VOICE_USER_DAILY_MAX / CHATX_VOICE_DAILY_MAX。`,
      });
    } else {
      out.push({ level: "info", text: `语音回复 ${all.voutOk}/${all.vout} 成功，累计 ${fmtDur(Math.round(all.voutSec))} 音频；用户发来语音 ${all.vin} 条、转写成功 ${all.vinOk}。` });
    }
  }
  if (all.vin >= MIN && pct(all.vin - all.vinOk, all.vin) >= 30) {
    const top = topKey(all.vinFail)!;
    out.push({ level: "warn", text: `用户语音 ${all.vin} 条有 ${all.vin - all.vinOk} 条没听懂（${pct(all.vin - all.vinOk, all.vin)}%），主因：${WHY_ZH[top] ?? top}。` });
  }

  const pTotal = sumRec(all.pMsgs);
  const nonDefault = Object.entries(all.pMsgs).filter(([k]) => k !== "xiaojie");
  if (pTotal >= MIN && nonDefault.length) {
    const parts = nonDefault.map(([k, n]) => `${personaZh(k)} ${pct(n, pTotal)}%`).join("、");
    const sw = sumRec(all.pSw);
    out.push({ level: "info", text: `人设：${pTotal} 句对话里 ${parts}（切换 ${sw} 次）；非默认人设占比高说明「可配人设」是卖点，投放素材可以带上。` });
  }
  if (all.cStart >= MIN) {
    const failN = sumRec(all.cFail);
    const top = topKey(all.cFail);
    if (all.cConsent < all.cStart * 0.5) {
      out.push({ level: "warn", text: `克隆声音：${all.cStart} 人点开、只有 ${all.cConsent} 人同意授权（${pct(all.cConsent, all.cStart)}%），授权说明可能太吓人或太长。` });
    } else if (top && failN >= Math.max(3, all.cOk)) {
      out.push({ level: "warn", text: `克隆声音：样本被拒 ${failN} 次、成功 ${all.cOk} 次，主因 ${CLONE_WHY_ZH[top] ?? top}（${all.cFail[top]} 次），录音提示需要更具体。` });
    } else {
      out.push({ level: "info", text: `克隆声音：开始 ${all.cStart} → 同意 ${all.cConsent} → 成功 ${all.cOk}（${pct(all.cOk, all.cStart)}%）。` });
    }
  }
  if (all.pushFail >= MIN) {
    out.push({ level: "warn", text: `主动推送有 ${all.pushFail} 条发送失败（多为用户拉黑 / 停用，已自动退订），属正常流失。` });
  }
  return out;
}

const pct = (a: number, b: number) => (b > 0 ? Math.round((a / b) * 100) : 0);

/**
 * 投放诊断：只在样本够（≥5 人）时下结论，避免小样本噪音。
 * 规则来自漏斗常识：start 多但到落地少 = 素材/首屏没接住；到落地多但不下载 = 落地页或设备（手机进来下不了）；
 * 聊得多不下载 = AI 没在推成交；同 IP 反复来 = 刷量/自测；推送回话率看主动对话是否有效。
 */
export function buildInsights(
  bySrc: ChatxDailyReport["bySrc"],
  totals: DailyTotals[],
  rows: DailySrcRow[],
  now = Date.now()
): Insight[] {
  const out: Insight[] = [];
  const MIN = 5;
  const sized = bySrc.filter((s) => s.users >= MIN && s.src !== "organic");

  if (sized.length >= 2) {
    const ranked = sized.slice().sort((x, y) => pct(y.dlUsers, y.users) - pct(x.dlUsers, x.users));
    const best = ranked[0];
    const worst = ranked[ranked.length - 1];
    const bestRate = pct(best.dlUsers, best.users);
    const worstRate = pct(worst.dlUsers, worst.users);
    if (bestRate > 0) {
      out.push({
        level: "good",
        src: best.src,
        text: `「${best.src}」人→下载 ${bestRate}%（${best.dlUsers}/${best.users}），是当前最优来源，预算优先加到它。`,
      });
    }
    if (worst !== best && bestRate >= worstRate * 2 && bestRate - worstRate >= 5) {
      out.push({
        level: "bad",
        src: worst.src,
        text: `「${worst.src}」人→下载只有 ${worstRate}%（${worst.dlUsers}/${worst.users}），不到最优来源一半，建议先停或换素材再投。`,
      });
    }
  }

  for (const s of sized) {
    const landRate = pct(s.landUsers, s.users);
    const dlOfLand = pct(s.dlUsers, s.landUsers);
    if (landRate < 30) {
      out.push({
        level: "warn",
        src: s.src,
        text: `「${s.src}」${s.users} 人进 bot 只有 ${landRate}% 点到落地页：广告承诺和 bot 首屏没对上，检查素材文案是否与「AI 全自动聊天」一致，或首条 caption 把下载按钮往上提。`,
      });
    } else if (s.landUsers >= MIN && dlOfLand < 25) {
      out.push({
        level: "warn",
        src: s.src,
        text: `「${s.src}」到落地页 ${s.landUsers} 人只有 ${dlOfLand}% 请求了安装包：多半是手机端进来（Windows 包下不了），投放定向加「桌面端」或素材里提示「手机点下载可发到电脑」。`,
      });
    }
    if (s.chatUsers >= MIN && s.dlUsers === 0) {
      out.push({
        level: "warn",
        src: s.src,
        text: `「${s.src}」有 ${s.chatUsers} 人聊了 ${s.msgs} 句但 0 人下载：这批人在问价/问能力，AI 提示词里的下载引导要加强，或让人工客服接一下这个来源。`,
      });
    }
  }

  // 同 IP 刷量：单日单来源同一 IP ≥5 次落地/安装包请求（点击不计，同一人落地后点几次是正常的）
  let hotShown = 0;
  for (const r of rows) {
    const hot = r.topIps.find((x) => x.n >= 5);
    if (!hot || hotShown >= 3) continue;
    hotShown++;
    out.push({
      level: "warn",
      src: r.src,
      text: `${r.day}「${r.src}」IP ${hot.ip} 出现 ${hot.n} 次落地/安装包请求：可能是自测或刷量，这个来源的数字要打折看。`,
    });
  }

  // 趋势：今天 vs 前 7 天日均
  if (totals.length >= 3) {
    const today = totals[0];
    const prev = totals.slice(1, 8);
    const avg = prev.reduce((n, d) => n + d.users, 0) / prev.length;
    if (avg >= 3 && today.users >= avg * 1.5) {
      out.push({ level: "good", text: `今日进 bot ${today.users} 人，是前 ${prev.length} 天日均 ${avg.toFixed(1)} 的 ${(today.users / avg).toFixed(1)} 倍。` });
    } else if (avg >= 5 && today.users <= avg * 0.5 && new Date(now + TZ_MS).getUTCHours() >= 18) {
      out.push({ level: "bad", text: `今日进 bot 只有 ${today.users} 人，不到前 ${prev.length} 天日均 ${avg.toFixed(1)} 的一半：检查广告是否被暂停/预算耗尽。` });
    }
  }

  // 互动质量
  const all = totals.reduce(
    (a, d) => ({ sessions: a.sessions + d.sessions, dur: a.dur + d.durSec, msgs: a.msgs + d.msgs, chat: a.chat + d.chatUsers, users: a.users + d.users, push: a.push + d.pushSent, pushRe: a.pushRe + d.pushReplied }),
    { sessions: 0, dur: 0, msgs: 0, chat: 0, users: 0, push: 0, pushRe: 0 }
  );
  if (all.sessions >= MIN) {
    const avg = Math.round(all.dur / all.sessions);
    out.push({
      level: "info",
      text: `期内 ${all.sessions} 段互动、平均 ${fmtDur(avg)}，共 ${all.msgs} 句对话；${all.users} 个进 bot 的人里 ${all.chat} 人开口聊了（${pct(all.chat, all.users)}%）。`,
    });
  }
  if (all.push >= MIN) {
    const rate = pct(all.pushRe, all.push);
    out.push({
      level: rate >= 10 ? "good" : "warn",
      text: `主动推送 ${all.push} 条，24h 内 ${all.pushRe} 人回话（${rate}%）${rate < 10 ? "：回话率偏低，换资讯选题或缩短文案" : ""}。`,
    });
  }

  if (!out.length) {
    out.push({ level: "info", text: "样本还不够（单来源 <5 人），先跑一两天再看结论；期间重点看每天「进 bot 人数」是否随预算上涨。" });
  }
  return out;
}

/** 管理员 Telegram 日报正文（HTML）：昨日合计 + 来源 TOP + 诊断，一屏读完。 */
export type AdGroupRow = { key: string; srcs: number; users: number; landUsers: number; dlUsers: number };

/** 把 ad_<类目>_<素材>NN 来源行按类目 / 素材汇总（非 ad_ 码不计），按进人数降序。 */
export function groupAdRows(rows: Pick<DailySrcRow, "src" | "users" | "landUsers" | "dlUsers">[]): { byCat: AdGroupRow[]; byCreative: AdGroupRow[] } {
  const cat = new Map<string, AdGroupRow>();
  const cre = new Map<string, AdGroupRow>();
  const add = (m: Map<string, AdGroupRow>, key: string, r: (typeof rows)[number]) => {
    const g = m.get(key) ?? { key, srcs: 0, users: 0, landUsers: 0, dlUsers: 0 };
    g.srcs++;
    g.users += r.users;
    g.landUsers += r.landUsers;
    g.dlUsers += r.dlUsers;
    m.set(key, g);
  };
  for (const r of rows) {
    const p = parseAdSrc(r.src);
    if (!p) continue;
    add(cat, p.cat, r);
    add(cre, p.creative, r);
  }
  const sort = (m: Map<string, AdGroupRow>) => [...m.values()].sort((a, b) => b.users - a.users || b.dlUsers - a.dlUsers);
  return { byCat: sort(cat), byCreative: sort(cre) };
}

export function formatDailyDigest(report: ChatxDailyReport, day: string): string {
  const t = report.totals.find((x) => x.day === day);
  const rows = report.rows.filter((r) => r.day === day).slice(0, 6);
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines: string[] = [`📊 <b>ChatX 广告日报 · ${day}</b>`];
  if (!t || (t.starts === 0 && t.landUsers === 0 && t.dlUsers === 0 && t.msgs === 0)) {
    lines.push("昨天没有任何 bot / 落地 / 下载数据。");
  } else {
    lines.push(
      `👥 进 bot <b>${t.users}</b> 人（新 ${t.newUsers}，start ${t.starts} 次）`,
      `🌐 到落地 <b>${t.landUsers}</b> 人 · 点下载 <b>${t.clickUsers}</b> 人 · 请求安装包 <b>${t.dlUsers}</b> 人`,
      `💬 ${t.chatUsers} 人聊了 ${t.msgs} 句${t.voiceMsgs ? `（语音 ${t.voiceMsgs}）` : ""} · ${t.sessions} 段互动 · 平均 ${fmtDur(t.avgDurSec)}`,
      `📍 来源 IP ${t.ips} 个${t.leads ? ` · 留资 ${t.leads}` : ""}${t.pushSent ? ` · 推送 ${t.pushSent} 条/回话 ${t.pushReplied}` : ""}`
    );
    const f = report.features.find((x) => x.day === day);
    if (f && (f.voiceIn || f.voiceOut)) {
      const fails = (m: Record<string, number>) => {
        const n = sumRec(m);
        return n ? `，失败 ${n}（${Object.entries(m).map(([k, v]) => `${k} ${v}`).join(" / ")}）` : "";
      };
      lines.push(`🎙 语音：收到 ${f.voiceIn} 条${fails(f.voiceInFail)} · 回出 ${f.voiceOutOk} 条${fails(f.voiceOutFail)}`);
    }
    if (f) {
      const others = Object.entries(f.personaMsgs).filter(([k]) => k !== "xiaojie");
      if (others.length) {
        lines.push(`🎭 人设：${Object.entries(f.personaMsgs).map(([k, n]) => `${personaZh(k)} ${n} 句/${f.personaUsers[k] ?? 0} 人`).join(" · ")}${sumRec(f.personaSwitches) ? `（切换 ${sumRec(f.personaSwitches)} 次）` : ""}`);
      }
    }
    if (f && (f.cloneStart || sumRec(f.voicePicks) || f.cloneOk)) {
      const picks = Object.entries(f.voicePicks).sort((x, y) => y[1] - x[1]).slice(0, 3).map(([k, n]) => `${k === "mine" ? "我的声音" : k} ${n}`).join(" / ");
      const bySrc = Object.entries(f.cloneOkBySrc).map(([k, n]) => `${esc(k)} ${n}`).join(" / ");
      lines.push(`🎧 音色：选择 ${sumRec(f.voicePicks)} 次${picks ? `（${picks}）` : ""} · 克隆 ${f.cloneStart} → 同意 ${f.cloneConsent} → 成功 ${f.cloneOk}${bySrc ? `（${bySrc}）` : ""}`);
    }
    if (f && sumRec(f.knownShown)) {
      const top = Object.entries(f.knownShown)
        .sort((x, y) => y[1] - x[1])
        .slice(0, 4)
        .map(([k, n]) => `${esc(k)} ${n}→✅${f.knownSolved[k] ?? 0}/👤${f.knownEscalated[k] ?? 0}`)
        .join(" · ");
      lines.push(`💡 已知问题：给出 ${sumRec(f.knownShown)} 次 · 解决 ${sumRec(f.knownSolved)} · 仍转人工 ${sumRec(f.knownEscalated)}（${top}）`);
    }
    if (f && (sumRec(f.postClicks) || sumRec(f.postUsers) || sumRec(f.postDl))) {
      const ids = new Set([...Object.keys(f.postClicks), ...Object.keys(f.postUsers), ...Object.keys(f.postDl)]);
      const top = [...ids]
        .map((k) => ({ k, c: f.postClicks[k] ?? 0, u: f.postUsers[k] ?? 0, d: f.postDl[k] ?? 0 }))
        .sort((x, y) => y.d - x.d || y.u - x.u || y.c - x.c)
        .slice(0, 4)
        .map((x) => `#${esc(x.k)} ${x.c}→${x.u}→${x.d}`)
        .join(" · ");
      lines.push(`📣 帖子（点击→进 bot→下载人）：${sumRec(f.postClicks)}→${sumRec(f.postUsers)}→${sumRec(f.postDl)}（${top}）`);
    }
    const groups = groupAdRows(report.rows.filter((r) => r.day === day));
    if (groups.byCat.length > 1 || groups.byCreative.length > 1) {
      const fmt = (g: AdGroupRow[], zh: Record<string, string>) => g.slice(0, 4).map((x) => `${zh[x.key] ?? x.key} ${x.users}→${x.dlUsers}`).join(" · ");
      lines.push(`🗂 按类目（进人→下载）：${fmt(groups.byCat, AD_CATEGORIES)}`, `🎨 按素材：${fmt(groups.byCreative, AD_CREATIVES)}`);
    }
    if (rows.length) {
      lines.push("", "<b>按来源</b>（进人 → 落地 → 下载人）");
      for (const r of rows) {
        lines.push(`• <code>${esc(r.src)}</code>  ${r.users} → ${r.landUsers} → <b>${r.dlUsers}</b>${r.msgs ? `  💬${r.msgs}` : ""}`);
      }
    }
  }
  const tips = report.insights.filter((i) => i.level !== "info").slice(0, 4);
  if (tips.length) {
    lines.push("", "<b>诊断</b>");
    const icon = { good: "✅", warn: "⚠️", bad: "🔻", info: "ℹ️" } as const;
    for (const i of tips) lines.push(`${icon[i.level]} ${esc(i.text)}`);
  }
  return lines.join("\n");
}

const addRec = (into: Record<string, number>, m: Record<string, number>) => {
  for (const [k, v] of Object.entries(m)) into[k] = (into[k] ?? 0) + v;
};

/** 多日功能面里可直接相加的次数类指标（人数类跨日不可相加，不在此列）。 */
export function sumFeatures(features: DailyFeatures[]) {
  const o = {
    voiceIn: 0,
    voiceInFail: {} as Record<string, number>,
    voiceOutOk: 0,
    voiceOutFail: {} as Record<string, number>,
    voiceOutSec: 0,
    personaMsgs: {} as Record<string, number>,
    personaSwitches: {} as Record<string, number>,
    voicePicks: {} as Record<string, number>,
    cloneStart: 0,
    cloneConsent: 0,
    cloneOk: 0,
    knownShown: {} as Record<string, number>,
    knownSolved: {} as Record<string, number>,
    knownEscalated: {} as Record<string, number>,
  };
  for (const f of features) {
    o.voiceIn += f.voiceIn;
    o.voiceOutOk += f.voiceOutOk;
    o.voiceOutSec += f.voiceOutSec;
    o.cloneStart += f.cloneStart;
    o.cloneConsent += f.cloneConsent;
    o.cloneOk += f.cloneOk;
    addRec(o.voiceInFail, f.voiceInFail);
    addRec(o.voiceOutFail, f.voiceOutFail);
    addRec(o.personaMsgs, f.personaMsgs);
    addRec(o.personaSwitches, f.personaSwitches);
    addRec(o.voicePicks, f.voicePicks);
    addRec(o.knownShown, f.knownShown);
    addRec(o.knownSolved, f.knownSolved);
    addRec(o.knownEscalated, f.knownEscalated);
  }
  return o;
}

export type WeeklyExtras = { posts?: PostFunnel[]; supportLine?: string | null };

/** 管理员周报：本周（cur.days）按人去重的合计 + 较上周变化 + 每日走势 + 语音 / 人设 / 已知问题 / 帖子 / 客服 / 来源排行。 */
export function formatWeeklyDigest(cur: ChatxDailyReport, prev: ChatxDailyReport, x: WeeklyExtras = {}): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const vs = (a: number, b: number) => (a === b ? "持平" : `${a > b ? "+" : ""}${a - b}`);
  const o = cur.overall;
  const p = prev.overall;
  const md = (d: string) => d.slice(5);
  const lines: string[] = [`📅 <b>ChatX 周报 · ${md(cur.days[0])} ~ ${md(cur.days[cur.days.length - 1])}</b>`];
  if (o.starts === 0 && o.landUsers === 0 && o.dlUsers === 0 && o.msgs === 0) {
    lines.push("本周没有任何 bot / 落地 / 下载数据。");
  } else {
    lines.push(
      `👥 进 bot <b>${o.users}</b> 人（新 ${o.newUsers}）· 较上周 ${vs(o.users, p.users)}`,
      `🌐 到落地 <b>${o.landUsers}</b> 人 · 点下载 <b>${o.clickUsers}</b> 人 · 请求安装包 <b>${o.dlUsers}</b> 人（较上周 ${vs(o.dlUsers, p.dlUsers)}）`,
      `💬 ${o.chatUsers} 人聊了 ${o.msgs} 句（较上周 ${vs(o.msgs, p.msgs)}）· ${o.sessions} 段互动 · 平均 ${fmtDur(o.avgDurSec)}${o.pushSent ? ` · 推送 ${o.pushSent}/回话 ${o.pushReplied}` : ""}`,
      `📈 每日进人→下载：${cur.totals.slice().reverse().map((t) => `${md(t.day)} ${t.users}→${t.dlUsers}`).join(" · ")}`
    );
    const f = sumFeatures(cur.features);
    const fails = (m: Record<string, number>) => {
      const n = sumRec(m);
      return n ? `，失败 ${n}（${Object.entries(m).map(([k, v]) => `${k} ${v}`).join(" / ")}）` : "";
    };
    if (f.voiceIn || f.voiceOutOk || sumRec(f.voiceOutFail)) {
      lines.push(`🎙 语音：收到 ${f.voiceIn} 条${fails(f.voiceInFail)} · 回出 ${f.voiceOutOk} 条（${fmtDur(Math.round(f.voiceOutSec))}）${fails(f.voiceOutFail)}`);
    }
    if (sumRec(f.personaMsgs)) {
      const pm = Object.entries(f.personaMsgs).sort((a, b) => b[1] - a[1]);
      const total = sumRec(f.personaMsgs);
      lines.push(`🎭 人设：${pm.map(([k, n]) => `${personaZh(k)} ${n} 句（${Math.round((n / total) * 100)}%）`).join(" · ")}${sumRec(f.personaSwitches) ? ` · 切换 ${sumRec(f.personaSwitches)} 次` : ""}`);
    }
    if (f.cloneStart || sumRec(f.voicePicks)) {
      lines.push(`🎧 音色：选择 ${sumRec(f.voicePicks)} 次 · 克隆 ${f.cloneStart} → 同意 ${f.cloneConsent} → 成功 ${f.cloneOk}`);
    }
    if (sumRec(f.knownShown)) {
      const top = Object.entries(f.knownShown).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, n]) => `${esc(k)} ${n}→✅${f.knownSolved[k] ?? 0}/👤${f.knownEscalated[k] ?? 0}`).join(" · ");
      lines.push(`💡 已知问题：给出 ${sumRec(f.knownShown)} 次 · 解决 ${sumRec(f.knownSolved)} · 仍转人工 ${sumRec(f.knownEscalated)}（${top}）`);
    }
  }
  const posts = (x.posts ?? []).filter((q) => q.clicks || q.users || q.dlUsers).sort((a, b) => b.dlUsers - a.dlUsers || b.users - a.users || b.clicks - a.clicks);
  if (posts.length) {
    const s = posts.reduce((a, q) => ({ c: a.c + q.clicks, u: a.u + q.users, d: a.d + q.dlUsers }), { c: 0, u: 0, d: 0 });
    lines.push(`📣 帖子/系列（点击→进 bot→下载人）：${s.c}→${s.u}→${s.d}（${posts.slice(0, 5).map((q) => `#${q.post} ${q.clicks}→${q.users}→${q.dlUsers}`).join(" · ")}）`);
  }
  if (x.supportLine) lines.push(x.supportLine);
  const rows = cur.bySrc.slice(0, 8);
  if (rows.length) {
    const prevBy = new Map(prev.bySrc.map((r) => [r.src, r]));
    lines.push("", "<b>按来源</b>（进人 → 落地 → 下载人 · 较上周进人）");
    for (const r of rows) {
      lines.push(`• <code>${esc(r.src)}</code>  ${r.users} → ${r.landUsers} → <b>${r.dlUsers}</b>  ${vs(r.users, prevBy.get(r.src)?.users ?? 0)}`);
    }
  }
  const tips = cur.insights.filter((i) => i.level !== "info").slice(0, 5);
  if (tips.length) {
    lines.push("", "<b>诊断</b>");
    const icon = { good: "✅", warn: "⚠️", bad: "🔻", info: "ℹ️" } as const;
    for (const i of tips) lines.push(`${icon[i.level]} ${esc(i.text)}`);
  }
  return lines.join("\n");
}
