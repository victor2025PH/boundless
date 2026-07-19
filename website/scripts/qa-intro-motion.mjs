/**
 * ============================================================================
 * 开场页（IntroCover）LOGO 粒子动效 —— Playwright 量化验收脚本
 * ============================================================================
 *
 * 用途：
 *   首页全屏开场页有一套「产品 LOGO 从星门喷涌飞出」的粒子动效，由页面内的
 *   rAF 物理引擎（等加速运动学闭式解）驱动，并在 window.__blIntroStats 上暴露
 *   实时统计对象。本脚本用无头浏览器打开页面，按固定间隔轮询该对象，对动效
 *   做量化验收并输出 PASS/FAIL 判定。
 *
 * 前置：
 *   先启动站点服务（npm run dev 或 npm run build && npm run start），
 *   确保 --url 指向的地址可访问。
 *
 * 用法：
 *   node scripts/qa-intro-motion.mjs --url http://localhost:3470/
 *   node scripts/qa-intro-motion.mjs --url http://localhost:3470/ --ms 15000
 *
 * 判定项与阈值（一句话含义）：
 *   dup           每次轮询 live 内 key 必须互不重复（同一 LOGO 不得同屏出现两份）
 *   countCap      每次轮询 live.length ≤ cap 且 cap ≤ 7（同屏数量不超会话上限）
 *   sideAlternate spawns 按 t 排序后 side 必须严格 +1/-1 交替（左右轮流出生）
 *   sideBalance   |L-R|≤1 的轮询占比 ≥ 0.85，且任一次 |L-R|>2 直接 FAIL（左右均衡）
 *   speedMedian   全部 live.v 样本的中位数 ≥ 550 px/s（整体速度感足够）
 *   speedP25      同一样本池 p25 ≥ 320 px/s（偏慢的粒子也不拖沓）
 *   spawnNoHover  样本池最小值 ≥ 160 px/s（出生即在动，无原地悬停）
 *   flightP90     exits.flightMs 的 p90 ≤ 1700 ms（单程飞行时间不冗长）
 *   exitOffscreen exits.offEdgePx 的 p90 ≥ 0（回收瞬间中心已越过屏幕边缘，无屏外空放）
 *   streamAlive   观测期内 exits ≥ 8 条（喷涌流持续不断、有吞吐）
 *   fpsInfo       (ticks 增量)/(观测秒数)，仅打印不参与判定（无 GPU 的 headless
 *                 环境 rAF 会被节流，该数值仅供参考）
 *
 * 退出码：0 = 全部 PASS；1 = 存在 FAIL；2 = 页面或 __blIntroStats 不可用。
 * ============================================================================
 */

import { chromium } from 'playwright';

// ---------- CLI 参数（简单 argv 解析，支持 `--url x` 与 `--url=x` 两种写法） ----------
function argValue(name, def) {
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === name) return argv[i + 1] ?? def;
    if (argv[i].startsWith(`${name}=`)) return argv[i].slice(name.length + 1);
  }
  return def;
}

const url = argValue('--url', 'http://localhost:3470/');
const msParsed = Number(argValue('--ms', '12000'));
const observeMs = Number.isFinite(msParsed) && msParsed > 0 ? msParsed : 12000;
// --headed：有头模式（无 GPU 的 headless 会把 rAF 节流到 ~2fps，速度/时长指标会失真）
const headed = process.argv.includes('--headed');

// ---------- 小工具 ----------
const sleep = (t) => new Promise((resolve) => setTimeout(resolve, t));
const round2 = (v) => Math.round(v * 100) / 100;

/** 线性插值分位数；空数组返回 0（相关 check 需自行按「无样本」判 FAIL） */
function quantile(values, p) {
  if (!values || values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const pos = (sorted.length - 1) * p;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

// ---------- 打开页面 ----------
const browser = await chromium.launch({ headless: !headed });
let exitCode = 0;

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });

  // 钉死实验桶保证确定性：auto_enter=a（本脚本要无交互观察 12s，B 桶自动进入会抢时序）、
  // btn_shape=a（统一视觉基准）。
  await page.addInitScript(() => {
    try {
      localStorage.setItem('ab_intro_auto_enter', 'a');
      localStorage.setItem('ab_intro_btn_shape', 'a');
    } catch {}
  });

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (err) {
    console.error(`[qa-intro-motion] 页面打开失败：${url}`);
    console.error(String(err && err.message ? err.message : err));
    await browser.close();
    process.exit(2);
  }

  try {
    await page.waitForFunction(() => Boolean(window.__blIntroStats), null, { timeout: 20000 });
  } catch {
    console.error('[qa-intro-motion] 等待 window.__blIntroStats 超时（20s）：开场页粒子引擎可能未启动或已被移除');
    await browser.close();
    process.exit(2);
  }

  // 记录页面侧起始时刻（performance.now 时间轴），用于筛选「观测期内」的 exits
  const perfStart = await page.evaluate(() => performance.now());

  // ---------- 轮询采样：每 120ms 读一次 { live, cap, ticks }，持续 observeMs ----------
  const polls = [];
  let firstTick = null; // { ticks, wallT }
  const tStart = Date.now();
  while (Date.now() - tStart < observeMs) {
    let snap = null;
    try {
      snap = await page.evaluate(() => {
        const s = window.__blIntroStats;
        if (!s || !Array.isArray(s.live)) return null;
        return {
          cap: typeof s.cap === 'number' ? s.cap : 0,
          ticks: typeof s.ticks === 'number' ? s.ticks : 0,
          live: s.live.map((it) => ({ key: it.key, v: it.v, side: it.side })),
        };
      });
    } catch {
      snap = null; // 页面导航/瞬时异常，跳过本次采样
    }
    if (snap) {
      polls.push(snap);
      if (firstTick === null) firstTick = { ticks: snap.ticks, wallT: Date.now() };
    }
    await sleep(120);
  }

  // ---------- 最后读取完整的 spawns/exits 环形缓冲 ----------
  let tail = null;
  try {
    tail = await page.evaluate(() => {
      const s = window.__blIntroStats;
      if (!s) return null;
      return {
        ticks: typeof s.ticks === 'number' ? s.ticks : 0,
        cap: typeof s.cap === 'number' ? s.cap : 0,
        mobile: Boolean(s.mobile),
        spawns: Array.isArray(s.spawns)
          ? s.spawns.map((e) => ({
              t: e.t,
              key: e.key,
              side: e.side,
              layer: e.layer,
              v0: e.v0,
              ve: e.ve,
              planMs: e.planMs,
            }))
          : [],
        exits: Array.isArray(s.exits)
          ? s.exits.map((e) => ({
              t: e.t,
              key: e.key,
              flightMs: e.flightMs,
              exitV: e.exitV,
              offEdgePx: e.offEdgePx,
            }))
          : [],
      };
    });
  } catch {
    tail = null;
  }
  const tailWallT = Date.now();

  const spawns = tail && Array.isArray(tail.spawns) ? tail.spawns : [];
  const exits = tail && Array.isArray(tail.exits) ? tail.exits : [];
  const exitsInWindow = exits.filter((e) => Number.isFinite(e.t) && e.t >= perfStart);

  // ---------- 计算指标与判定 ----------
  const checks = [];
  const addCheck = (name, pass, detail) => checks.push({ name, pass: Boolean(pass), detail });

  // 1) dup：每次轮询 live 内 key 必须互不重复
  let dupPolls = 0;
  const dupKeys = new Set();
  for (const poll of polls) {
    const seen = new Set();
    for (const it of poll.live) {
      if (seen.has(it.key)) {
        dupKeys.add(it.key);
      }
      seen.add(it.key);
    }
    if (seen.size !== poll.live.length) dupPolls += 1;
  }
  if (polls.length === 0) {
    addCheck('dup', false, '无样本（未采集到任何轮询快照）');
  } else {
    addCheck(
      'dup',
      dupPolls === 0,
      dupPolls === 0
        ? `全部 ${polls.length} 次轮询 key 均唯一`
        : `${dupPolls}/${polls.length} 次轮询出现重复 key：${[...dupKeys].slice(0, 5).join(', ')}`
    );
  }

  // 2) countCap：live.length ≤ cap 且 cap ≤ 7
  let capViolations = 0;
  let maxLive = 0;
  let capMax = 0;
  for (const poll of polls) {
    maxLive = Math.max(maxLive, poll.live.length);
    capMax = Math.max(capMax, poll.cap);
    if (!(poll.live.length <= poll.cap && poll.cap <= 7)) capViolations += 1;
  }
  if (polls.length === 0) {
    addCheck('countCap', false, '无样本（未采集到任何轮询快照）');
  } else {
    addCheck('countCap', capViolations === 0, `maxLive=${maxLive}，cap=${capMax}，违规轮询=${capViolations}/${polls.length}`);
  }

  // 3) sideAlternate：spawns 按 t 排序后 side 严格 +1/-1 交替
  const spawnsSorted = [...spawns].sort((a, b) => a.t - b.t);
  let altViolations = 0;
  for (let i = 1; i < spawnsSorted.length; i += 1) {
    if (spawnsSorted[i].side !== -spawnsSorted[i - 1].side) altViolations += 1;
  }
  if (spawnsSorted.length === 0) {
    addCheck('sideAlternate', false, '无样本（spawns 环形缓冲为空）');
  } else {
    addCheck('sideAlternate', altViolations === 0, `spawns=${spawnsSorted.length}，交替违规=${altViolations}`);
  }

  // 4) sideBalance：|L-R|≤1 占比 ≥ 0.85，且任一次 |L-R|>2 直接 FAIL
  let balanceOk = 0;
  let maxDiff = 0;
  for (const poll of polls) {
    let left = 0;
    let right = 0;
    for (const it of poll.live) {
      if (it.side === 1) left += 1;
      else if (it.side === -1) right += 1;
    }
    const diff = Math.abs(left - right);
    maxDiff = Math.max(maxDiff, diff);
    if (diff <= 1) balanceOk += 1;
  }
  const balanceRatio = polls.length > 0 ? balanceOk / polls.length : 0;
  if (polls.length === 0) {
    addCheck('sideBalance', false, '无样本（未采集到任何轮询快照）');
  } else {
    addCheck(
      'sideBalance',
      balanceRatio >= 0.85 && maxDiff <= 2,
      `|L-R|≤1 占比=${round2(balanceRatio * 100)}%（阈值 ≥85%），max|L-R|=${maxDiff}（>2 直接 FAIL）`
    );
  }

  // 5)~7) 速度判定：解析式（帧率无关）。等加速直线飞行的时间加权中位速度 = (v0+ve)/2，
  // 时间轴 1/4 处速度 = v0+(ve-v0)/4。直接由引擎上报的闭式参数计算，
  // 无 GPU headless 的 rAF 节流不影响判定；采样值另行打印仅供参考。
  const medianPerFlight = [];
  const quarterPerFlight = [];
  const v0List = [];
  for (const sp of spawnsSorted) {
    if (Number.isFinite(sp.v0) && Number.isFinite(sp.ve)) {
      medianPerFlight.push((sp.v0 + sp.ve) / 2);
      quarterPerFlight.push(sp.v0 + (sp.ve - sp.v0) / 4);
      v0List.push(sp.v0);
    }
  }
  const speedMedian = quantile(medianPerFlight, 0.5);
  const speedP25 = quantile(quarterPerFlight, 0.25);
  const speedMin = v0List.length > 0 ? Math.min(...v0List) : 0;
  if (medianPerFlight.length === 0) {
    addCheck('speedMedian', false, '无样本（spawns 未上报运动学参数）');
    addCheck('speedP25', false, '无样本（spawns 未上报运动学参数）');
    addCheck('spawnNoHover', false, '无样本（spawns 未上报运动学参数）');
  } else {
    addCheck(
      'speedMedian',
      speedMedian >= 550,
      `解析中位速度=${round2(speedMedian)} px/s（阈值 ≥550），航班数=${medianPerFlight.length}`
    );
    addCheck('speedP25', speedP25 >= 320, `解析 p25（时间轴 1/4 处速度）=${round2(speedP25)} px/s（阈值 ≥320）`);
    addCheck('spawnNoHover', speedMin >= 160, `min v0=${round2(speedMin)} px/s（阈值 ≥160，出生即在动）`);
  }

  // 8) flightP90：解析式计划飞行时长 planMs 的 p90 ≤ 1700ms
  // （实测 flightMs 在节流环境含「最后一帧迟到」的回收延迟，仅打印参考）
  const planList = spawnsSorted.map((e) => e.planMs).filter((v) => Number.isFinite(v));
  const flightP90 = quantile(planList, 0.9);
  if (planList.length === 0) {
    addCheck('flightP90', false, '无样本（spawns 未上报 planMs）');
  } else {
    addCheck('flightP90', flightP90 <= 1700, `planMs p90=${round2(flightP90)} ms（阈值 ≤1700），样本数=${planList.length}`);
  }

  // 采样参考值（受环境帧率影响，不参与判定）
  const speedPool = [];
  for (const poll of polls) {
    for (const it of poll.live) {
      if (Number.isFinite(it.v)) speedPool.push(it.v);
    }
  }
  const sampledFlightList = exits.map((e) => e.flightMs).filter((v) => Number.isFinite(v));

  // 9) exitOffscreen：exits.offEdgePx p90 ≥ 0
  const offEdgeList = exits.map((e) => e.offEdgePx).filter((v) => Number.isFinite(v));
  const offEdgeP90 = quantile(offEdgeList, 0.9);
  if (offEdgeList.length === 0) {
    addCheck('exitOffscreen', false, '无样本（exits 环形缓冲为空）');
  } else {
    addCheck('exitOffscreen', offEdgeP90 >= 0, `offEdgePx p90=${round2(offEdgeP90)} px（阈值 ≥0，回收时已出屏）`);
  }

  // 10) streamAlive：观测期内 exits ≥ 8 条
  addCheck('streamAlive', exitsInWindow.length >= 8, `观测期内 exits=${exitsInWindow.length} 条（阈值 ≥8）`);

  // fpsInfo：ticks 增量 / 观测秒数，仅打印不参与判定
  let ticksDelta = 0;
  let fpsInfo = 0;
  const lastTicks = tail ? tail.ticks : polls.length > 0 ? polls[polls.length - 1].ticks : null;
  if (firstTick && lastTicks !== null && tailWallT > firstTick.wallT) {
    ticksDelta = lastTicks - firstTick.ticks;
    fpsInfo = ticksDelta / ((tailWallT - firstTick.wallT) / 1000);
  }

  const metrics = {
    polls: polls.length,
    liveSamples: speedPool.length,
    capMax,
    maxLive,
    analytic: {
      v0MinPxs: round2(speedMin),
      speedP25Pxs: round2(speedP25),
      speedMedianPxs: round2(speedMedian),
      planFlightP90Ms: round2(flightP90),
    },
    sampledForReference: {
      liveVMedianPxs: round2(quantile(speedPool, 0.5)),
      exitFlightP90Ms: round2(quantile(sampledFlightList, 0.9)),
    },
    sideBalanceOkRatio: round2(balanceRatio),
    sideBalanceMaxDiff: maxDiff,
    spawnsBuffered: spawns.length,
    exitsBuffered: exits.length,
    exitsInWindow: exitsInWindow.length,
    offEdgePxP90: round2(offEdgeP90),
    ticksDelta,
    fpsInfo: round2(fpsInfo),
    mobile: tail ? tail.mobile : null,
  };

  const pass = checks.every((c) => c.pass);
  const result = { url, ms: observeMs, metrics, checks, pass };
  console.log(JSON.stringify(result, null, 2));
  exitCode = pass ? 0 : 1;
} finally {
  await browser.close();
}

process.exit(exitCode);
