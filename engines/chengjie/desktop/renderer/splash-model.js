"use strict";
/* splash-model.js — 开机动画（首启序列）纯函数核心（P0 2026-08-22）
   ───────────────────────────────────────────────────────────────────────────
   与 campaign-model / first-run-model 同款「双端」模块：浏览器挂 window.splashModel，
   Node 走 module.exports 给 test/splash-model.test.js 直测。**本文件零 DOM 零 IPC
   零文案**——文案全部在 shell-i18n.js 词典，这里只产 key 与数字。

   核心不变量（门禁钉住，改动先读）：
   ① 进度只增不减、不到真实里程碑绝不越段（engine 段封顶 66.5 < link 起点 68，
      真实信号到达时的「脉冲跳变」是刻意的里程碑反馈，不是 bug）；
   ② 阶段只进不退（spawn 状态抖动 / failed 不把已达阶段拉回去）；
   ③ 100% 只在 wsReady（工作台真正加载完成）时出现——这是引擎侧 _loading_overlay
      「刻意不做假百分比」纪律在壳侧的对应物：分段锚点全部是真实信号，段内才用
      时间插值填充观感。 */
(function (root) {
  // 六阶段与真实信号的对应（驱动层喂 signals，这里推导）：
  //   shell  = 壳首帧（无任何信号）
  //   probe  = spawn 状态机在探测/等待（probing/idle/disabled/stopped）
  //   engine = backend.exe 冷启动中（starting）——最长的一段，用 ETA 插值
  //   link   = /login 探活通过 或 spawn 报 ready/running-external
  //   load   = 工作台 webview 真实开始导航（dom-ready/did-navigate 到 http URL）
  //   done   = renderer 状态机判 ready（onNav 确认非离线壳）
  var PHASES = ["shell", "probe", "engine", "link", "load", "done"];
  var ORDER = { shell: 0, probe: 1, engine: 2, link: 3, load: 4, done: 5 };

  var SEGMENTS = {
    shell: { lo: 0, hi: 5, ms: 400 },
    probe: { lo: 5, hi: 12, ms: 1500 },
    engine: { lo: 12, hi: 66.5, ms: 0 }, // ms=0 → 用 etaMs（自适应：上次冷启动实测时长）
    link: { lo: 68, hi: 86, ms: 2500 },
    load: { lo: 88, hi: 99, ms: 8000 },
    done: { lo: 100, hi: 100, ms: 1 },
  };

  // 各阶段氛围文案池大小（键=splash.amb.<phase>.<idx>，词条在 shell-i18n.js；
  // 池大小与词典键数由 test/splash-model.test.js 双向钉住，改一边必须改另一边）。
  var AMBIENT_COUNTS = { shell: 1, probe: 2, engine: 7, link: 3, load: 4, done: 2 };
  var AMBIENT_ROTATE_MS = 2400;

  // ETA：默认 30s（打包版冷启动 15-30s + webview 装载余量），实测值夹在 [8s, 90s]
  //（90s=backend-launcher waitForReady 的放弃线，ETA 不该比它更悲观）。
  var ETA_DEFAULT_MS = 30000;
  var ETA_MIN_MS = 8000;
  var ETA_MAX_MS = 90000;

  function clamp01(x) {
    x = +x;
    if (!isFinite(x) || x <= 0) return 0;
    return x >= 1 ? 1 : x;
  }

  // 减速缓动：前段走得快（有生命感），越接近段顶越慢（永远差一口气，等真实信号）。
  function ease(x) {
    return 1 - Math.pow(1 - clamp01(x), 1.7);
  }

  /* 信号 → 阶段（只进不退）。failed/port-conflict 不参与推导：错误是正交维度
     （spIsErrorStatus），阶段冻结在已达位置，错误面板由驱动层按 renderer 镜像事件出。 */
  function spPhase(signals, prevPhase) {
    var s = signals || {};
    var cand;
    if (s.wsReady) cand = "done";
    else if (s.wsNav) cand = "load";
    else if (s.healthOk || s.spawnStatus === "ready" || s.spawnStatus === "running-external") cand = "link";
    else if (s.spawnStatus === "starting") cand = "engine";
    else if (s.spawnStatus === "probing" || s.spawnStatus === "idle"
      || s.spawnStatus === "disabled" || s.spawnStatus === "stopped") cand = "probe";
    else cand = "shell";
    var prev = (prevPhase && ORDER[prevPhase] != null) ? prevPhase : "shell";
    return ORDER[cand] >= ORDER[prev] ? cand : prev;
  }

  /* 段内进度目标：lo + (hi-lo) * ease(elapsed/段时长)。engine 段时长=ETA。
     驱动层约定：engine 段喂「自 splash 起点的总耗时」（ETA 就是按总耗时校准的），
     其余段喂「进入该段后的耗时」。 */
  function spProgressTarget(phase, elapsedMs, etaMs) {
    var seg = SEGMENTS[phase] || SEGMENTS.shell;
    var span = seg.ms > 0 ? seg.ms : spEtaMs(etaMs);
    var v = seg.lo + (seg.hi - seg.lo) * ease((+elapsedMs || 0) / span);
    if (v < seg.lo) v = seg.lo;
    if (v > seg.hi) v = seg.hi;
    return v;
  }

  /* 只增不减 + 全局封顶。 */
  function spProgress(prev, target) {
    var p = +prev || 0;
    var t = +target || 0;
    var v = t > p ? t : p;
    return v > 100 ? 100 : v;
  }

  function spStageNum(phase) {
    return (ORDER[phase] != null ? ORDER[phase] : 0) + 1;
  }

  /* 氛围行轮换：按「进入该阶段后的耗时」确定性取池内索引（同输入恒同输出，可测；
     2.4s 一换，与打字机入场动画节拍配套）。 */
  function spAmbientIndex(phase, phaseElapsedMs) {
    var n = AMBIENT_COUNTS[phase] || 1;
    var t = +phaseElapsedMs || 0;
    if (t < 0) t = 0;
    return Math.floor(t / AMBIENT_ROTATE_MS) % n;
  }

  function spAmbientKey(phase, idx) {
    return "splash.amb." + phase + "." + (+idx || 0);
  }

  function spEtaMs(raw) {
    var n = +raw;
    if (!isFinite(n) || n <= 0) n = ETA_DEFAULT_MS;
    n = Math.round(n);
    if (n < ETA_MIN_MS) return ETA_MIN_MS;
    if (n > ETA_MAX_MS) return ETA_MAX_MS;
    return n;
  }

  /* 「预计还需」只在有把握时给：开头 2.5s 不给（还没校准感），超过 ETA 不给
     （给了就是编数字——超时后改由 spSlow 的诚实慢速文案接棒）。 */
  function spRemainSecs(elapsedMs, etaMs) {
    var e = +elapsedMs || 0;
    if (e < 2500) return null;
    var remain = (spEtaMs(etaMs) - e) / 1000;
    return remain >= 1 ? Math.ceil(remain) : null;
  }

  /* 明显超出预期（ETA 的 1.4 倍且至少超 6s）→ 换「首次较慢/仅此一次」安抚口径。 */
  function spSlow(elapsedMs, etaMs) {
    var eta = spEtaMs(etaMs);
    return (+elapsedMs || 0) > Math.max(eta * 1.4, eta + 6000);
  }

  function spIsErrorStatus(status) {
    return status === "failed" || status === "port-conflict";
  }

  /* 启动模式判定：
     · 白标（config.brand 非空）→ skip：默认品牌首屏绝不漏给白标客户（P1 再做
       按客户品牌渲染的中性版，先诚实让位）；
     · 首次探活即健康 → warm：≈1.2s 品牌闪场（老板拍板：暖启动不播全幕）；
     · reduced-motion / 用户偏好极简 → cold-min；其余 → cold-full 三幕全版。 */
  function spInitMode(input) {
    var i = input || {};
    if (i.whiteLabel) return "skip";
    if (i.healthOk) return "warm";
    if (i.reducedMotion || i.pref === "min") return "cold-min";
    return "cold-full";
  }

  /* 遥测用时长分桶（done 事件后缀，读数落 ui-event）。 */
  function spDurBucket(ms) {
    var s = (+ms || 0) / 1000;
    if (s < 10) return "lt10";
    if (s < 30) return "lt30";
    if (s < 60) return "lt60";
    return "ge60";
  }

  var api = {
    PHASES: PHASES,
    ORDER: ORDER,
    SEGMENTS: SEGMENTS,
    AMBIENT_COUNTS: AMBIENT_COUNTS,
    AMBIENT_ROTATE_MS: AMBIENT_ROTATE_MS,
    spPhase: spPhase,
    spProgressTarget: spProgressTarget,
    spProgress: spProgress,
    spStageNum: spStageNum,
    spAmbientIndex: spAmbientIndex,
    spAmbientKey: spAmbientKey,
    spEtaMs: spEtaMs,
    spRemainSecs: spRemainSecs,
    spSlow: spSlow,
    spIsErrorStatus: spIsErrorStatus,
    spInitMode: spInitMode,
    spDurBucket: spDurBucket,
  };

  root.splashModel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
