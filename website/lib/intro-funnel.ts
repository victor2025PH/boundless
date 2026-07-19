// 开场页（IntroCover）转化漏斗聚合 —— events.jsonl 只读推导。
// 为什么单独成库：/api/console/intro-funnel（脚本/巡检 curl）与 /console 总览卡片（服务端直出）
// 共用同一份口径，避免「API 一个算法、页面另一个算法」的口径漂移。
//
// 口径说明（与埋点事件一一对应，见 components/IntroCover.tsx）：
//   intro_shown / intro_first_gesture / intro_sound_on / intro_enter
//   全部按 sid（会话）去重——同一会话刷新多次只算一次，漏斗分母分子口径一致；
//   dwellMs 取该会话 intro_enter.props.dwellMs（展示→进入的停留毫秒）。
import { readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");

// events.jsonl 是全站事件流水，长期运行可达数十万行；只回读末尾这么多行——
// intro_* 只占流水一小部分，该量足以覆盖 90 天窗口，同时避免整文件常驻内存。
const MAX_LINES = 100_000;

export interface IntroFunnel {
  days: number;
  sessions: { shown: number; gesture: number; soundOn: number; enter: number };
  enterByMethod: { click: number; scroll: number; touch: number; key: number; auto: number };
  dwellMs: { median: number; p90: number };
  rates: { gestureRate: number; soundRate: number; enterRate: number };
}

// auto = intro_auto_enter 实验 B 桶的「无操作 12s 自动进入」
const ENTER_METHODS = ["click", "scroll", "touch", "key", "auto"] as const;
type EnterMethod = (typeof ENTER_METHODS)[number];

function quantile(sorted: number[], p: number): number {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * p;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return Math.round(sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo));
}

/** 读取近 N 天的开场页漏斗。事件文件不存在/为空 → 全 0 结构（不抛错，页面照常渲染）。 */
export async function readIntroFunnel(days: number): Promise<IntroFunnel> {
  const d = Math.min(90, Math.max(1, Math.floor(days) || 7));
  const since = Date.now() - d * 86_400_000;

  const shown = new Set<string>();
  const gesture = new Set<string>();
  const soundOn = new Set<string>();
  // 同一会话可能既点按钮又滚动（极端竞态），取首个 intro_enter 为准
  const enter = new Map<string, { method: EnterMethod | null; dwellMs: number | null }>();

  let raw = "";
  try {
    raw = await readFile(LOG, "utf8");
  } catch {
    /* 尚无事件文件 → 返回全 0 */
  }

  const lines = raw.split("\n");
  for (const line of lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines) {
    // 快速预筛：绝大多数行不是 intro 事件，跳过 JSON.parse 的开销
    if (!line || !line.includes('"intro_')) continue;
    try {
      const r = JSON.parse(line) as {
        t?: string;
        event?: string;
        sid?: string;
        props?: { method?: string; dwellMs?: number | null } | null;
      };
      const sid = r.sid || "";
      if (!sid || !r.t || Date.parse(r.t) < since) continue;
      switch (r.event) {
        case "intro_shown":
          shown.add(sid);
          break;
        case "intro_first_gesture":
          gesture.add(sid);
          break;
        case "intro_sound_on":
          soundOn.add(sid);
          break;
        case "intro_enter": {
          if (enter.has(sid)) break;
          const m = (r.props?.method ?? "") as string;
          // 未知 method 不硬归类：仍计入进入会话数，只是不进任何方式桶（防脏数据污染分布）
          const method = (ENTER_METHODS as readonly string[]).includes(m) ? (m as EnterMethod) : null;
          const dwell = r.props?.dwellMs;
          enter.set(sid, { method, dwellMs: typeof dwell === "number" && dwell >= 0 ? dwell : null });
          break;
        }
      }
    } catch {
      /* 跳过坏行：追加式 jsonl 在进程被杀时可能留半行 */
    }
  }

  const enterByMethod = { click: 0, scroll: 0, touch: 0, key: 0, auto: 0 };
  const dwells: number[] = [];
  for (const e of enter.values()) {
    if (e.method) enterByMethod[e.method] += 1;
    if (e.dwellMs != null) dwells.push(e.dwellMs);
  }
  dwells.sort((a, b) => a - b);

  const nShown = shown.size;
  const rate = (n: number) => (nShown ? Math.round((n / nShown) * 1000) / 1000 : 0);
  return {
    days: d,
    sessions: { shown: nShown, gesture: gesture.size, soundOn: soundOn.size, enter: enter.size },
    enterByMethod,
    dwellMs: { median: quantile(dwells, 0.5), p90: quantile(dwells, 0.9) },
    rates: { gestureRate: rate(gesture.size), soundRate: rate(soundOn.size), enterRate: rate(enter.size) },
  };
}
