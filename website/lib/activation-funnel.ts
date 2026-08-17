/**
 * 激活漏斗聚合（P1-⑧，2026-08-10 事故链沉淀）：
 * 「上周装机的用户里有百分之几卡在扫码这一步」——此前这个生死攸关的问题无数据可答。
 *
 * 五个里程碑，全部来自**已存在**的数据面（零新增客户端上报通道，除 milestone 事件）：
 *   installed      开机心跳（client-logs.jsonl，beacon boot 行，按指纹去重）
 *   claimed        领试用（trial-claims 台账 createdAt 在窗口内）
 *   dispatched     被派发 Telegram 凭据（ai-gateway.db tg_cred_assign）
 *   account_online 首个账号接入成功（client-logs milestone 行）
 *   first_reply    首条真实出站消息（client-logs milestone 行，客户端带 10min 新鲜闸）
 *
 * 口径诚实声明：这是「窗口内活跃机器各自到达过哪一段」的运营脉搏，**不是**按安装
 * 日期切的同期群（cohort）漏斗——机器装于窗口外、本窗口才接入账号时，installed 与
 * account_online 都会计入本窗口（boot 每次启动都发，实际影响很小）。同期群版是 P2。
 * 只读、纯聚合、绝不写盘；任何数据面缺失按 0 计（页面上以空态提示区分）。
 */
import { readClientLogRows, type ClientLogRow } from "./client-logs";
import { listClaims } from "./trial-claim-store";
import { assignedMidsSince } from "./tg-cred-pool";

/** 里程碑上报能力的最低客户端版本（account_online / first_reply 从这版才有）。 */
export const MILESTONE_MIN_VER = "1.0.20";

/** "1.0.20" 式版本比较；解析不出按 0 段处理（"dev"/空串 → 一律低于门槛）。 */
export function versionAtLeast(ver: string, min: string): boolean {
  const parse = (s: string): number[] =>
    String(s || "").split(".").map((p) => parseInt(p.replace(/[^0-9]/g, ""), 10) || 0);
  const a = parse(ver);
  const b = parse(min);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0;
    const y = b[i] || 0;
    if (x !== y) return x > y;
  }
  return true;
}

export interface FunnelWindow {
  days: number;
  installed: number;
  claimed: number;
  dispatched: number;
  account_online: number;
  first_reply: number;
  /** 各段相对 installed 的转化率（installed=0 时全为 null） */
  rates: {
    claimed: number | null;
    dispatched: number | null;
    account_online: number | null;
    first_reply: number | null;
  };
  /** 数据面在不在（client-logs 文件缺失 = beacon 链没通，页面要提示而非装 0） */
  logs_present: boolean;
}

function rate(part: number, base: number): number | null {
  if (!base) return null;
  return Math.round((part / base) * 1000) / 10; // 一位小数百分比
}

export async function activationFunnel(days: number): Promise<FunnelWindow> {
  const sinceMs = Date.now() - days * 86400_000;
  const rows = await readClientLogRows(days * 24);

  const boots = new Set<string>();
  const online = new Set<string>();
  const replied = new Set<string>();
  for (const r of rows || []) {
    if (!r.fp) continue;
    if (r.logger === "beacon" && r.msg.startsWith("boot")) boots.add(r.fp);
    else if (r.logger === "milestone" && r.msg.startsWith("account_online")) online.add(r.fp);
    else if (r.logger === "milestone" && r.msg.startsWith("first_reply")) replied.add(r.fp);
  }

  let claimed = 0;
  try {
    const claims = await listClaims({ limit: 500 });
    claimed = claims.filter((c) => {
      const t = Date.parse(c.createdAt);
      return Number.isFinite(t) && t >= sinceMs;
    }).length;
  } catch {
    /* 台账缺失按 0 */
  }

  let dispatched = 0;
  try {
    dispatched = new Set(assignedMidsSince(sinceMs)).size;
  } catch {
    /* 池库缺失按 0 */
  }

  const installed = boots.size;
  return {
    days,
    installed,
    claimed,
    dispatched,
    account_online: online.size,
    first_reply: replied.size,
    rates: {
      claimed: rate(claimed, installed),
      dispatched: rate(dispatched, installed),
      account_online: rate(online.size, installed),
      first_reply: rate(replied.size, installed),
    },
    logs_present: rows !== null,
  };
}

// ── P2-⑩ 接入成功率 SLO ──────────────────────────────────────────────────────

export interface SloResult {
  evaluated: boolean;
  /** 未评估原因：no_logs（beacon 链没通）| no_capable（窗口内无新版客户端）|
   *  small_sample（合格样本不足，不下判） */
  skip_reason?: "no_logs" | "no_capable" | "small_sample";
  window_days: number;
  /** 分母：窗口内领了试用、且其机器跑着**能上报里程碑的版本**（老版本机器
   *  没有 account_online 数据，混进分母会把 SLO 打成假红）。 */
  claimed_capable: number;
  connected: number;
  pct: number | null;
  min_pct: number;
  breached: boolean;
  lines: string[];
}

function fpVersions(rows: ClientLogRow[]): Map<string, string> {
  // 每指纹取窗口内见到的最高版本（升级中的机器按新版算）
  const m = new Map<string, string>();
  for (const r of rows) {
    if (r.logger !== "beacon" || !r.fp) continue;
    const cur = m.get(r.fp);
    if (!cur || versionAtLeast(r.ver, cur)) m.set(r.fp, r.ver);
  }
  return m;
}

/**
 * 「领了试用的人里有多少接入了第一个账号」——低于阈值＝获客白花钱，必须有人知道。
 * env：ACTIVATION_SLO_MIN_PCT（默认 50）/ ACTIVATION_SLO_MIN_CLAIMS（默认 5）。
 * 三重不评估守卫（宁可沉默不可误报）：无回传数据 / 无新版客户端 / 样本太小。
 */
export async function activationSlo(days = 7): Promise<SloResult> {
  const minPct = Math.max(1, Number(process.env.ACTIVATION_SLO_MIN_PCT || 50));
  const minClaims = Math.max(1, Number(process.env.ACTIVATION_SLO_MIN_CLAIMS || 5));
  const base: SloResult = {
    evaluated: false, window_days: days, claimed_capable: 0, connected: 0,
    pct: null, min_pct: minPct, breached: false, lines: [],
  };
  const rows = await readClientLogRows(days * 24);
  if (rows === null) return { ...base, skip_reason: "no_logs" };
  const vers = fpVersions(rows);
  const capable = new Set(
    [...vers.entries()]
      .filter(([, v]) => versionAtLeast(v, MILESTONE_MIN_VER))
      .map(([fp]) => fp));
  if (!capable.size) return { ...base, skip_reason: "no_capable" };

  const sinceMs = Date.now() - days * 86400_000;
  let claims: Array<{ fingerprint: string }> = [];
  try {
    claims = (await listClaims({ limit: 500 })).filter((c) => {
      const t = Date.parse(c.createdAt);
      return Number.isFinite(t) && t >= sinceMs;
    });
  } catch {
    /* 台账缺失＝无分母 */
  }
  const claimedCapable = claims.filter((c) => capable.has(c.fingerprint));
  if (claimedCapable.length < minClaims) {
    return { ...base, claimed_capable: claimedCapable.length,
             skip_reason: "small_sample" };
  }

  const online = new Set(rows
    .filter((r) => r.logger === "milestone" && r.msg.startsWith("account_online"))
    .map((r) => r.fp));
  const connected = claimedCapable.filter((c) => online.has(c.fingerprint)).length;
  const pct = Math.round((connected / claimedCapable.length) * 1000) / 10;
  const breached = pct < minPct;

  const lines = [
    `激活 SLO（近 ${days} 天，新版客户端口径）：` +
    `领试用 ${claimedCapable.length} → 接入账号 ${connected}（${pct}%，阈值 ${minPct}%）` +
    (breached ? " ⚠️ 破线" : " ✓"),
  ];
  // P3-⑫ 破线时自带归因：告警从「哪里坏了」升级到「为什么坏」，省一轮翻看板。
  if (breached) {
    lines.push(...attributeBreach(rows, claimedCapable, online));
  }
  return {
    evaluated: true, window_days: days,
    claimed_capable: claimedCapable.length, connected, pct, min_pct: minPct,
    breached, lines,
  };
}

/**
 * 破线归因：把「没接入的机器」按卡在哪一段分桶，指向根因而非泛泛让人去翻看板。
 * - 卡在「派发前」（连凭据都没领到）→ 池/领取闸问题（当年 API_ID_INVALID 事故的上游）。
 * - 卡在「派发后」（拿了凭据但没接入）→ 登录本身失败：叠加 client-logs 的 top 归因码
 *   （cred_invalid=池组废 / tg_unreachable=网络被墙 / …），一眼区分「换池」还是「配代理」。
 */
function attributeBreach(
  rows: ClientLogRow[],
  claimedCapable: Array<{ fingerprint: string }>,
  online: Set<string>,
): string[] {
  const notOnline = claimedCapable.filter((c) => !online.has(c.fingerprint));
  if (!notOnline.length) return [];
  const dispatched = new Set(
    (() => { try { return assignedMidsSince(Date.now() - 30 * 86400_000); }
             catch { return [] as string[]; } })());
  const preDispatch = notOnline.filter((c) => !dispatched.has(c.fingerprint)).length;
  const postDispatch = notOnline.length - preDispatch;

  // client-logs 的登录失败归因码（tg_login 的 reason=xxx 落在 msg 里，仅统计合格机）
  const capableFps = new Set(claimedCapable.map((c) => c.fingerprint));
  const codeCount = new Map<string, number>();
  const RE = /reason=([a-z_]+)/;
  for (const r of rows) {
    if (!capableFps.has(r.fp)) continue;
    if (!/tg_protocol_login|发起登录失败/.test(r.logger + " " + r.msg)) continue;
    const m = RE.exec(r.msg);
    if (m) codeCount.set(m[1], (codeCount.get(m[1]) || 0) + r.n);
  }
  const topCode = [...codeCount.entries()].sort((a, b) => b[1] - a[1])[0];

  const out = [
    `  卡点分布：派发前 ${preDispatch}（池/领取闸）· 派发后 ${postDispatch}（登录失败）`,
  ];
  if (topCode) {
    const hint: Record<string, string> = {
      cred_invalid: "→ 疑似池组废，跑 tg_cred_probe 核实换池",
      tg_unreachable: "→ 客户端直连被墙，看代理下发/引导配代理",
      rate_limited: "→ 触发限流，观察或降派发频率",
    };
    out.push(`  登录失败 top 归因：${topCode[0]}×${topCode[1]} ${hint[topCode[0]] || ""}`.trimEnd());
  }
  return out;
}

// ── P2-⑪ 同期群 48h 激活率 + 挽回名单 ───────────────────────────────────────

export interface CohortResult {
  evaluated: boolean;
  window_days: number;
  /** 同期群定义：窗口内领试用、已满 48h 观察期、且机器跑新版客户端。 */
  cohort: number;
  activated_48h: number;
  pct: number | null;
}

/** 「本周新客 48h 内接入率」——按领试用时刻起算的真同期群（未满 48h 的不下判）。 */
export async function cohortActivation(days = 7): Promise<CohortResult> {
  const rows = await readClientLogRows(days * 24 + 48);
  const base: CohortResult = {
    evaluated: false, window_days: days, cohort: 0, activated_48h: 0, pct: null,
  };
  if (rows === null) return base;
  const vers = fpVersions(rows);
  const sinceMs = Date.now() - days * 86400_000;
  const matureBefore = Date.now() - 48 * 3600_000;
  let claims: Array<{ fingerprint: string; createdAt: string }> = [];
  try {
    claims = await listClaims({ limit: 500 });
  } catch {
    return base;
  }
  // 每指纹最早 account_online 时刻
  const firstOnline = new Map<string, number>();
  for (const r of rows) {
    if (r.logger !== "milestone" || !r.msg.startsWith("account_online")) continue;
    const t = Date.parse(r.t);
    if (!Number.isFinite(t)) continue;
    const cur = firstOnline.get(r.fp);
    if (cur === undefined || t < cur) firstOnline.set(r.fp, t);
  }
  const cohort = claims.filter((c) => {
    const t = Date.parse(c.createdAt);
    return Number.isFinite(t) && t >= sinceMs && t <= matureBefore
      && versionAtLeast(vers.get(c.fingerprint) || "", MILESTONE_MIN_VER);
  });
  if (!cohort.length) return base;
  const activated = cohort.filter((c) => {
    const on = firstOnline.get(c.fingerprint);
    const claimedAt = Date.parse(c.createdAt);
    return on !== undefined && on <= claimedAt + 48 * 3600_000;
  }).length;
  return {
    evaluated: true, window_days: days, cohort: cohort.length,
    activated_48h: activated,
    pct: Math.round((activated / cohort.length) * 1000) / 10,
  };
}

export interface WinbackRow {
  fp: string;
  contact: string;
  contactKind: string;
  claimedAt: string;
  ver: string;
  capable: boolean;
  dispatched: boolean;
  connected: boolean;
  /** P3-⑬ 客服已联系时刻（空=未联系）+ 操作人，避免重复外呼。 */
  contactedAt?: string;
  contactedBy?: string;
}

/**
 * 挽回名单：窗口内领了试用却没接入账号的机器 + 台账联系方式（客服外呼素材）。
 * 老版本机器如实标注 capable=false（它们判不出接入态，别当成失败客户去打扰）。
 */
export async function winbackList(days = 7, limit = 100): Promise<WinbackRow[]> {
  const rows = (await readClientLogRows(days * 24)) || [];
  const vers = fpVersions(rows);
  const online = new Set(rows
    .filter((r) => r.logger === "milestone" && r.msg.startsWith("account_online"))
    .map((r) => r.fp));
  const dispatched = new Set(assignedMidsSince(Date.now() - days * 86400_000));
  const sinceMs = Date.now() - days * 86400_000;
  let claims: Awaited<ReturnType<typeof listClaims>> = [];
  try {
    claims = await listClaims({ limit: 500 });
  } catch {
    return [];
  }
  let contacted: Record<string, { contactedAt: string; by: string }> = {};
  try {
    const { contactedMap } = await import("./winback-store");
    contacted = await contactedMap();
  } catch {
    /* 台账缺失＝全部未联系 */
  }
  return claims
    .filter((c) => {
      const t = Date.parse(c.createdAt);
      return Number.isFinite(t) && t >= sinceMs && !online.has(c.fingerprint);
    })
    .sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1))
    .slice(0, Math.max(1, limit))
    .map((c) => {
      const oc = contacted[c.fingerprint];
      return {
        fp: c.fingerprint,
        contact: c.contact,
        contactKind: c.contactKind,
        claimedAt: c.createdAt,
        ver: vers.get(c.fingerprint) || "",
        capable: versionAtLeast(vers.get(c.fingerprint) || "", MILESTONE_MIN_VER),
        dispatched: dispatched.has(c.fingerprint),
        connected: false,
        ...(oc ? { contactedAt: oc.contactedAt, contactedBy: oc.by } : {}),
      };
    });
}
