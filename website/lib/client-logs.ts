/**
 * 客户端错误回传（/api/client-log）的读侧聚合：console「客户端错误」看板数据源。
 *
 * 读 DATA_DIR/client-logs.jsonl（每行一事件），按时间窗过滤后出四类聚合：
 *   · 设备×版本（谁的机器、什么版本在报错/启动）
 *   · 错误 Top（logger + 消息头，合并计数含 n）
 *   · 崩溃（exit_sentinel 上报的上次非正常退出）
 *   · 版本分布（安装/在跑版本面）
 * 只读、纯函数、绝不写盘；文件缺失/脏行跳过（回传是旁路，看板不该因脏数据崩）。
 */
import { readFile, stat } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const LOG = process.env.CLIENT_LOG_PATH || path.join(DATA_DIR, "client-logs.jsonl");
const MAX_SCAN_BYTES = 5 * 1024 * 1024; // 只读尾部 5MB，防大文件拖垮页面

export interface ClientLogRow {
  t: string;
  ip: string;
  fp: string;
  ver: string;
  ts: number;
  logger: string;
  level: string;
  msg: string;
  n: number;
}

export interface ClientLogSummary {
  present: boolean;
  window_hours: number;
  total_events: number;
  machines: number;
  by_version: Array<{ ver: string; events: number; machines: number }>;
  top_errors: Array<{ logger: string; msg: string; count: number; machines: number; last: string }>;
  crashes: Array<{ t: string; fp: string; ver: string; msg: string }>;
  recent: ClientLogRow[];
}

function parseRow(line: string): ClientLogRow | null {
  try {
    const j = JSON.parse(line);
    if (!j || typeof j !== "object") return null;
    return {
      t: String(j.t || ""),
      ip: String(j.ip || ""),
      fp: String(j.fp || ""),
      ver: String(j.ver || ""),
      ts: Number(j.ts) || 0,
      logger: String(j.logger || ""),
      level: String(j.level || ""),
      msg: String(j.msg || ""),
      n: Math.max(1, Number(j.n) || 1),
    };
  } catch {
    return null;
  }
}

export async function summarizeClientLogs(windowHours = 24): Promise<ClientLogSummary> {
  const empty: ClientLogSummary = {
    present: false, window_hours: windowHours, total_events: 0, machines: 0,
    by_version: [], top_errors: [], crashes: [], recent: [],
  };
  let raw = "";
  try {
    const st = await stat(LOG);
    const start = Math.max(0, st.size - MAX_SCAN_BYTES);
    const buf = await readFile(LOG);
    raw = buf.subarray(start).toString("utf8");
  } catch {
    return empty;
  }
  const since = Date.now() - windowHours * 3600 * 1000;
  const rows: ClientLogRow[] = [];
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    const r = parseRow(line);
    if (!r) continue;
    const tms = Date.parse(r.t);
    if (Number.isFinite(tms) && tms < since) continue;
    rows.push(r);
  }
  if (!rows.length) return { ...empty, present: true };

  const machines = new Set(rows.map((r) => r.fp).filter(Boolean));

  // 版本分布
  const verMap = new Map<string, { events: number; machines: Set<string> }>();
  for (const r of rows) {
    const v = r.ver || "?";
    const e = verMap.get(v) || { events: 0, machines: new Set<string>() };
    e.events += r.n;
    if (r.fp) e.machines.add(r.fp);
    verMap.set(v, e);
  }
  const by_version = [...verMap.entries()]
    .map(([ver, e]) => ({ ver, events: e.events, machines: e.machines.size }))
    .sort((a, b) => b.events - a.events);

  // 错误 Top（只算 ERROR/WARNING，跳过 boot/INFO 心跳）
  const errKey = (r: ClientLogRow) => `${r.logger}\u0001${r.msg.slice(0, 80)}`;
  const errMap = new Map<string, { logger: string; msg: string; count: number; machines: Set<string>; last: string }>();
  const crashes: ClientLogSummary["crashes"] = [];
  for (const r of rows) {
    if (r.logger === "beacon") continue; // boot 心跳不算错误
    if (r.logger === "exit_sentinel") {
      crashes.push({ t: r.t, fp: r.fp, ver: r.ver, msg: r.msg });
      continue;
    }
    const lvl = r.level.toUpperCase();
    if (lvl !== "ERROR" && lvl !== "WARNING" && lvl !== "CRITICAL") continue;
    const k = errKey(r);
    const e = errMap.get(k) || { logger: r.logger, msg: r.msg, count: 0, machines: new Set<string>(), last: r.t };
    e.count += r.n;
    if (r.fp) e.machines.add(r.fp);
    if (r.t > e.last) e.last = r.t;
    errMap.set(k, e);
  }
  const top_errors = [...errMap.values()]
    .map((e) => ({ logger: e.logger, msg: e.msg, count: e.count, machines: e.machines.size, last: e.last }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 20);

  const recent = rows.slice(-40).reverse();

  return {
    present: true,
    window_hours: windowHours,
    total_events: rows.reduce((s, r) => s + r.n, 0),
    machines: machines.size,
    by_version,
    top_errors,
    crashes: crashes.slice(-15).reverse(),
    recent,
  };
}
