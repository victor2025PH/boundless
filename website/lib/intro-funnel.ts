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

/* 自动化流量识别：QA 验收脚本（HeadlessChrome/Playwright）与爬虫会大量触发 intro 事件——
 * 实测上线首日 25 条事件里 22 条来自验收脚本，不滤会把 A/B 实验读数彻底污染。
 * 默认排除，include_bots=1 可带回（排障/对账用）。 */
const BOT_UA = /HeadlessChrome|bot|spider|crawl|playwright|puppeteer|lighthouse|phantomjs|selenium/i;

function quantile(sorted: number[], p: number): number {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * p;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return Math.round(sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo));
}

/** 读取近 N 天的开场页漏斗。事件文件不存在/为空 → 全 0 结构（不抛错，页面照常渲染）。
 *  默认排除自动化流量（UA 命中 BOT_UA），includeBots=true 时不过滤。 */
export async function readIntroFunnel(days: number, opts?: { includeBots?: boolean }): Promise<IntroFunnel> {
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
        ua?: string;
        props?: { method?: string; dwellMs?: number | null } | null;
      };
      const sid = r.sid || "";
      if (!sid || !r.t || Date.parse(r.t) < since) continue;
      if (!opts?.includeBots && r.ua && BOT_UA.test(r.ua)) continue;
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

/* ================= A/B 实验读数（intro_* 实验专用） =================
 * 决策要看的是「同一会话」的曝光桶 × 进入行为：按 sid 把 ab_expose 与
 * intro_shown/intro_enter 连起来，每个变体给出 曝光会话/展示/进入/进入率/停留分布。
 * 与漏斗同一份事件源、同一套 bot 过滤——两张卡的数字天然对得上。 */

export interface IntroExperimentVariant {
  exposed: number; // 曝光会话数（该 sid 被分进此桶）
  shown: number; // 其中真正看到开场页的会话
  enter: number; // 其中进入正文的会话
  enterRate: number; // enter / shown（3 位小数；shown=0 时为 0）
  dwellMs: { median: number; p90: number };
}

export interface IntroExperimentReadout {
  experiment: string;
  variants: Record<string, IntroExperimentVariant>;
}

export async function readIntroExperiments(
  days: number,
  opts?: { includeBots?: boolean }
): Promise<{ days: number; experiments: IntroExperimentReadout[] }> {
  const d = Math.min(90, Math.max(1, Math.floor(days) || 7));
  const since = Date.now() - d * 86_400_000;

  // sid → 该会话的实验分桶（同实验取首次曝光）/ 是否展示 / 进入停留
  const buckets = new Map<string, Map<string, string>>();
  const shownSids = new Set<string>();
  const enterBySid = new Map<string, number | null>();

  let raw = "";
  try {
    raw = await readFile(LOG, "utf8");
  } catch {
    /* 尚无事件文件 → 空读数 */
  }

  const lines = raw.split("\n");
  for (const line of lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines) {
    // intro_* 实验的 ab_expose 行 props 里含 "intro_，与 intro_ 事件共用同一预筛
    if (!line || !line.includes('"intro_')) continue;
    try {
      const r = JSON.parse(line) as {
        t?: string;
        event?: string;
        sid?: string;
        ua?: string;
        props?: { experiment?: string; variant?: string; dwellMs?: number | null } | null;
      };
      const sid = r.sid || "";
      if (!sid || !r.t || Date.parse(r.t) < since) continue;
      if (!opts?.includeBots && r.ua && BOT_UA.test(r.ua)) continue;
      if (r.event === "ab_expose" && r.props?.experiment?.startsWith("intro_") && r.props.variant) {
        let m = buckets.get(sid);
        if (!m) {
          m = new Map();
          buckets.set(sid, m);
        }
        if (!m.has(r.props.experiment)) m.set(r.props.experiment, r.props.variant);
      } else if (r.event === "intro_shown") {
        shownSids.add(sid);
      } else if (r.event === "intro_enter" && !enterBySid.has(sid)) {
        const dw = r.props?.dwellMs;
        enterBySid.set(sid, typeof dw === "number" && dw >= 0 ? dw : null);
      }
    } catch {
      /* 跳过坏行 */
    }
  }

  // 聚合：experiment → variant → 指标
  const agg = new Map<string, Map<string, { exposed: number; shown: number; enter: number; dwells: number[] }>>();
  for (const [sid, m] of buckets) {
    for (const [exp, variant] of m) {
      let vm = agg.get(exp);
      if (!vm) {
        vm = new Map();
        agg.set(exp, vm);
      }
      let v = vm.get(variant);
      if (!v) {
        v = { exposed: 0, shown: 0, enter: 0, dwells: [] };
        vm.set(variant, v);
      }
      v.exposed += 1;
      if (shownSids.has(sid)) v.shown += 1;
      if (enterBySid.has(sid)) {
        v.enter += 1;
        const dw = enterBySid.get(sid);
        if (dw != null) v.dwells.push(dw);
      }
    }
  }

  const experiments: IntroExperimentReadout[] = [...agg.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([experiment, vm]) => {
      const variants: Record<string, IntroExperimentVariant> = {};
      for (const [variant, v] of [...vm.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
        v.dwells.sort((a, b) => a - b);
        variants[variant] = {
          exposed: v.exposed,
          shown: v.shown,
          enter: v.enter,
          enterRate: v.shown ? Math.round((v.enter / v.shown) * 1000) / 1000 : 0,
          dwellMs: { median: quantile(v.dwells, 0.5), p90: quantile(v.dwells, 0.9) },
        };
      }
      return { experiment, variants };
    });

  return { days: d, experiments };
}
