// 集群算力调度快照存储（实施70 2026-08-27 老板指令：官网实时算力看板）。
// 数据流：117 集群 pusher（engines/chengjie/tools/compute_status_report.py）每 ~10s
// POST 一份全景快照（出话链三档 / 各 GPU 主机显存 / 出图卡 / hub 泊车 / 媒体服务）→
// 本模块保内存最新值 + 1h 环形历史 + 关键状态翻转「调动事件」。
// 刻意不逐份落盘快照——10s 粒度 JSONL 一天 ~15MB 纯噪音；只有事件值得留痕
//（追加 compute-events.jsonl，pm2 重启后回放最近 100 条，快照本体等下一次推送回血）。
import fs from "node:fs";
import path from "node:path";
import { DATA_DIR } from "@/lib/data-dir";

type Dict = Record<string, unknown>;
export interface ComputeSnapshot extends Dict {
  ts: number;
}
export interface ComputeEvent {
  ts: number;
  kind: string;
  text: string;
}

const EVENTS_LOG =
  process.env.COMPUTE_EVENTS_LOG || path.join(DATA_DIR, "compute-events.jsonl");
const RING_MAX = 360; // 10s 推送 ≈ 1 小时
const EVENTS_MAX = 100;

let latest: ComputeSnapshot | null = null;
const ring: ComputeSnapshot[] = [];
let events: ComputeEvent[] = loadRecentEvents();

function loadRecentEvents(): ComputeEvent[] {
  try {
    const txt = fs.readFileSync(EVENTS_LOG, "utf8");
    return txt
      .trim()
      .split(/\r?\n/)
      .slice(-EVENTS_MAX)
      .map((l) => {
        try {
          return JSON.parse(l) as ComputeEvent;
        } catch {
          return null;
        }
      })
      .filter((x): x is ComputeEvent => !!x && typeof x.ts === "number");
  } catch {
    return [];
  }
}

function boolAt(o: unknown, ...keys: string[]): boolean | null {
  let cur: unknown = o;
  for (const k of keys) {
    if (!cur || typeof cur !== "object") return null;
    cur = (cur as Dict)[k];
  }
  return typeof cur === "boolean" ? cur : null;
}

function strAt(o: unknown, ...keys: string[]): string {
  let cur: unknown = o;
  for (const k of keys) {
    if (!cur || typeof cur !== "object") return "";
    cur = (cur as Dict)[k];
  }
  return typeof cur === "string" ? cur : "";
}

// 出话链某档的人话标签：优先快照自带 role（① 主链 · 本地 vLLM …）+ 厂商，老快照回落固定词。
// 08-27 起这里写死「主胎（硅基流动）/ 备胎（DeepSeek）」，09-17 主链改锁 local 后事件流仍在说老模式。
function tierLabel(s: ComputeSnapshot | null, key: string, legacy: string): string {
  const node = key === "cloud"
    ? ((s?.chain as Dict | undefined)?.cloud ?? (s?.chain as Dict | undefined)?.primary)
    : (s?.chain as Dict | undefined)?.[key];
  const role = strAt(node, "role");
  const vendor = strAt(node, "vendor") || strAt(node, "endpoint");
  if (role) return vendor ? `${role}（${vendor}）` : role;
  return vendor ? `${legacy}（${vendor}）` : legacy;
}

function cloudOk(s: ComputeSnapshot | null): boolean | null {
  const v = boolAt(s, "chain", "cloud", "ok");
  return v === null ? boolAt(s, "chain", "primary", "ok") : v;
}

// 「调动事件」判据：三档出话链 / 引擎 / 出图卡的可用性翻转。
// [键, 取值, 掉线文案, 恢复文案]
function flags(
  s: ComputeSnapshot | null
): Array<[string, boolean | null, string, string]> {
  const c = tierLabel(s, "cloud", "云端主链档");
  const p = tierLabel(s, "pool", "云端 key 池");
  const l = tierLabel(s, "local", "本地 vLLM 档");
  return [
    ["primary", cloudOk(s), `${c}不可达`, `${c}恢复`],
    ["pool", boolAt(s, "chain", "pool", "ok"), `${p}不可达`, `${p}恢复`],
    ["local", boolAt(s, "chain", "local", "up"), `${l}不可达`, `${l}恢复`],
    ["engine", boolAt(s, "engine", "up"), "智聊引擎（18799）不可达", "智聊引擎恢复"],
    ["comfy", boolAt(s, "comfy", "ok"), "出图 ComfyUI 不可达", "出图 ComfyUI 恢复"],
  ];
}

function appendEvent(ev: ComputeEvent) {
  events.push(ev);
  if (events.length > EVENTS_MAX) events = events.slice(-EVENTS_MAX);
  try {
    fs.appendFileSync(EVENTS_LOG, JSON.stringify(ev) + "\n");
  } catch {
    /* 观测面绝不抛 */
  }
}

// 事件防抖（2026-08-27 06:15：当晨 117 两次 ~40s 网络瞬断刷出 6 条「三档同跪」事件后加）：
// 翻转须连续两份快照确认（≈20s）才落账——单拍采集抖动不再制造假事件，真实故障窗
// （≥2 拍）照旧留痕，恢复同样要两拍确认（防「闪恢复又闪断」刷屏）。
const stable: Record<string, boolean | null> = {};
const cand: Record<string, { val: boolean; n: number }> = {};

export function ingestSnapshot(snap: ComputeSnapshot) {
  const next = flags(snap);
  for (const [kind, nowVal, downText, upText] of next) {
    if (nowVal === null) continue; // 本拍不可判：不动稳定态也不累计候选
    if (stable[kind] === undefined || stable[kind] === null) {
      stable[kind] = nowVal; // 首次观测建立基线，不发事件
      continue;
    }
    if (nowVal === stable[kind]) {
      delete cand[kind]; // 回到稳定态：清候选
      continue;
    }
    const c = cand[kind];
    if (c && c.val === nowVal) c.n += 1;
    else cand[kind] = { val: nowVal, n: 1 };
    if ((cand[kind] as { n: number }).n >= 2) {
      appendEvent({ ts: snap.ts, kind, text: nowVal ? upText : downText });
      stable[kind] = nowVal;
      delete cand[kind];
    }
  }
  latest = snap;
  ring.push(snap);
  if (ring.length > RING_MAX) ring.splice(0, ring.length - RING_MAX);
}

export function statusView() {
  const now = Math.floor(Date.now() / 1000);
  return {
    latest,
    age_sec: latest ? Math.max(0, now - Number(latest.ts || 0)) : null,
    events: [...events].reverse(),
    ring_len: ring.length,
  };
}
