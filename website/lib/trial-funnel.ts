// 桌面首启向导转化漏斗聚合 —— events.jsonl 只读推导（与 intro-funnel 同构）。
//
// 事件来源：桌面壳首启向导（托管版）→ 本地后端 /api/admin/license/trial-funnel
// → 官网 POST /api/track，事件名 trial_wizard_*，sid = 机器指纹（按机去重）。
// 事件白名单在客户端两层各有一张表（desktop FR_FUNNEL_EVENTS / 后端
// trial_claim_client.FUNNEL_EVENTS），这里只认识下面六个阶段，未知的忽略。
import { readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
const MAX_LINES = 100_000;

export const TRIAL_FUNNEL_STAGES = [
  "welcome",
  "claim_submit",
  "claim_ok",
  "claim_skip",
  "gift_open",
  "done",
] as const;
export type TrialFunnelStage = (typeof TRIAL_FUNNEL_STAGES)[number];

export interface TrialFunnel {
  days: number;
  /** 每阶段触达的机器数（sid=机器指纹去重；无指纹的匿名事件按次计入） */
  machines: Record<TrialFunnelStage, number>;
  /** 相对 welcome 的转化率（3 位小数；welcome=0 时全 0） */
  rates: { claimSubmit: number; claimOk: number; done: number };
}

/** 读取近 N 天的首启向导漏斗。事件文件缺失/为空 → 全 0（页面照常渲染）。 */
export async function readTrialFunnel(days: number): Promise<TrialFunnel> {
  const d = Math.min(90, Math.max(1, Math.floor(days) || 7));
  const since = Date.now() - d * 86_400_000;

  const sids = new Map<TrialFunnelStage, Set<string>>();
  const anon = new Map<TrialFunnelStage, number>();
  for (const s of TRIAL_FUNNEL_STAGES) {
    sids.set(s, new Set());
    anon.set(s, 0);
  }

  let raw = "";
  try {
    raw = await readFile(LOG, "utf8");
  } catch {
    /* 尚无事件文件 → 全 0 */
  }

  const lines = raw.split("\n");
  for (const line of lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines) {
    if (!line || !line.includes('"trial_wizard_')) continue;
    try {
      const r = JSON.parse(line) as { t?: string; event?: string; sid?: string };
      if (!r.t || Date.parse(r.t) < since) continue;
      const stage = String(r.event || "").replace(/^trial_wizard_/, "") as TrialFunnelStage;
      if (!(TRIAL_FUNNEL_STAGES as readonly string[]).includes(stage)) continue;
      const sid = r.sid || "";
      if (sid) sids.get(stage)!.add(sid);
      else anon.set(stage, (anon.get(stage) || 0) + 1);
    } catch {
      /* 追加式 jsonl 可能留半行，跳过 */
    }
  }

  const machines = {} as Record<TrialFunnelStage, number>;
  for (const s of TRIAL_FUNNEL_STAGES) machines[s] = sids.get(s)!.size + (anon.get(s) || 0);

  const base = machines.welcome;
  const rate = (n: number) => (base ? Math.round((n / base) * 1000) / 1000 : 0);
  return {
    days: d,
    machines,
    rates: {
      claimSubmit: rate(machines.claim_submit),
      claimOk: rate(machines.claim_ok),
      done: rate(machines.done),
    },
  };
}
