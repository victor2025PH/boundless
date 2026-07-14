#!/usr/bin/env node
/**
 * RUM 真实用户性能报表:聚合 events.jsonl 中的 rum 事件,输出
 * 分挡位/指针类型的 fps 分布、长任务情况、以及 data-fx 阈值调整建议。
 *
 * 用法(服务器上):
 *   node scripts/rum-report.mjs [events.jsonl 路径]
 * 默认路径 /home/ubuntu/hualing-analytics/events.jsonl
 */
import { createReadStream, existsSync } from "fs";
import readline from "readline";

const file = process.argv[2] || "/home/ubuntu/hualing-analytics/events.jsonl";
if (!existsSync(file)) {
  console.error(`找不到事件文件: ${file}`);
  process.exit(1);
}

const rows = [];
const rl = readline.createInterface({ input: createReadStream(file) });
rl.on("line", (l) => {
  try {
    const r = JSON.parse(l);
    if (r.event === "rum" && r.props && typeof r.props.fps === "number") {
      rows.push({ ...r.props, t: r.t });
    }
  } catch {
    /* 跳过坏行 */
  }
});

rl.on("close", () => {
  if (!rows.length) {
    console.log("暂无 rum 样本(等待真实用户访问积累)");
    return;
  }
  const sorted = (a) => [...a].sort((x, y) => x - y);
  const pct = (a, p) => a[Math.min(a.length - 1, Math.floor(a.length * p))];
  const fmt = (v) => String(v).padStart(7);

  console.log(`RUM 样本总数: ${rows.length}   时间范围: ${rows[0].t?.slice(0, 10)} ~ ${rows[rows.length - 1].t?.slice(0, 10)}\n`);

  // 1) 按 挡位|指针 分组的核心指标
  const groups = new Map();
  for (const r of rows) {
    const key = `${r.tier || "?"}|${r.coarse ? "touch" : "mouse"}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  console.log("挡位|指针        fps中位  fps-p10  p95帧ms  长任务中位  LCP中位ms   样本");
  for (const [k, g] of [...groups.entries()].sort()) {
    const fps = sorted(g.map((r) => r.fps));
    const p95 = sorted(g.map((r) => r.p95 ?? 0));
    const lng = sorted(g.map((r) => r.long ?? 0));
    const lcps = sorted(g.filter((r) => r.lcp != null).map((r) => r.lcp));
    console.log(
      `${k.padEnd(15)} ${fmt(pct(fps, 0.5))} ${fmt(pct(fps, 0.1))} ${fmt(pct(p95, 0.5))} ${fmt(pct(lng, 0.5))} ${fmt(lcps.length ? pct(lcps, 0.5) : "-")} ${fmt(g.length)}`
    );
  }
  // LCP>2500ms 占比:决定是否要把首屏 Reveal 改为"默认可见、JS 就绪后才做入场动画"
  const lcps = rows.filter((r) => r.lcp != null).map((r) => r.lcp);
  if (lcps.length) {
    const slow = lcps.filter((v) => v > 2500).length / lcps.length;
    console.log(`\nLCP>2.5s 占比: ${(slow * 100).toFixed(1)}%${slow > 0.25 ? "  → 建议首屏 Reveal 渐进增强化" : ""}`);
  }

  // 2) 按核数分桶:直接指导 data-fx 的 hardwareConcurrency 阈值
  const byHc = new Map();
  for (const r of rows) {
    const b = r.hc == null ? "未知" : r.hc <= 4 ? "<=4核" : r.hc <= 8 ? "5-8核" : ">8核";
    if (!byHc.has(b)) byHc.set(b, []);
    byHc.get(b).push(r.fps);
  }
  console.log("\n核数桶    fps中位  fps-p10   样本");
  for (const [k, v] of byHc) {
    const s = sorted(v);
    console.log(`${k.padEnd(8)} ${fmt(pct(s, 0.5))} ${fmt(pct(s, 0.1))} ${fmt(v.length)}`);
  }

  // 3) 阈值建议
  const lowShare = rows.filter((r) => r.fps < 45).length / rows.length;
  const highRows = rows.filter((r) => r.tier === "high");
  const highLow = highRows.length ? highRows.filter((r) => r.fps < 45).length / highRows.length : 0;
  console.log(`\nfps<45 总占比: ${(lowShare * 100).toFixed(1)}%   high 挡内 fps<45 占比: ${(highLow * 100).toFixed(1)}%`);
  if (highLow > 0.15) {
    const mid = highRows.filter((r) => r.fps < 45 && (r.hc ?? 99) <= 8).length;
    console.log(
      `→ 建议收紧分档:high 挡内 ${(highLow * 100).toFixed(0)}% 用户掉帧` +
        (mid ? `,其中 ${mid} 例核数<=8,可把低配阈值从 <=4 核提到 <=6 核观察` : ",需进一步看 dm/dpr 分布")
    );
  } else {
    console.log("→ 当前分档健康,无需调整");
  }
});
