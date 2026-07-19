"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { useLang } from "@/components/LanguageContext";
import { BRAND, PRODUCT_ORDER, type ProductKey } from "@/lib/brand";
import { PRODUCT_IMG, PRODUCT_GLOW } from "@/components/productMeta";
import { track } from "@/lib/track";
import { abVariant, abExpose } from "@/lib/ab";

/** 科幻开场页（进入 AI 世界）：全屏星际之门场景 + WebAudio 合成氛围音乐。
 *  - 每个会话只出现一次（sessionStorage），站内往返不重复打扰；
 *  - 音乐由 WebAudio 实时合成（无音频文件、无版权问题），首次手势后才出声（浏览器自动播放策略）；
 *  - 点击"进入"触发星流加速 + 光门冲越动画，随后音乐在正文里缓缓消散；
 *  - prefers-reduced-motion 下跳过动画，仅保留静态画面与按钮。 */

const SEEN_KEY = "bl-intro-seen";

/* 同一 JS 运行时内（SPA 内部跳转回首页）直接跳过，避免一帧闪现；
 * 服务器端渲染时恒为 false，保证 SSR/hydration 一致。 */
let dismissedInRuntime = false;

const COPY = {
  zh: {
    title: "无界科技",
    sub: "B O U N D L E S S",
    tagline: "让沟通，无界",
    taglineSub: "COMMUNICATION, BOUNDLESS.",
    enter: "进入 AI 世界",
    hint: "建议开启声音 · 滚动或点击进入",
    soundOff: "开启音效",
    soundOn: "声音开启",
  },
  en: {
    title: "BOUNDLESS",
    sub: "无 界 科 技",
    tagline: "Communication, Boundless.",
    taglineSub: "让沟通，无界",
    enter: "ENTER THE AI WORLD",
    hint: "Sound on recommended · scroll or click",
    soundOff: "ENABLE SOUND",
    soundOn: "SOUND ON",
  },
} as const;

/* ================= 音频引擎：真实史诗配乐 + 少量科技感音效 =================
 * 主音乐: /intro/theme.mp3 —— "Epic Cinematic Victory" (PaulYudin, Pixabay 内容许可，
 * 可免费商用无需署名)。组件挂载即预取，首次手势后解码循环播放。
 * 科技感来自一次性音效：开声时的数字启动扫频、进入时的冲越声 + 低频撞击。
 * mp3 加载失败时回退到干净的合成和弦垫（无噪声层）。 */

const THEME_URL = "/intro/theme.mp3";
let themePromise: Promise<ArrayBuffer | null> | null = null;

function preloadTheme() {
  if (!themePromise) {
    themePromise = fetch(THEME_URL)
      .then((r) => (r.ok ? r.arrayBuffer() : null))
      .catch(() => null);
  }
  return themePromise;
}

interface AudioHandle {
  ctx: AudioContext;
  swell: () => void;
  whoosh: () => void;
  impact: () => void;
  fadeOut: (sec: number) => void;
  setOn: (on: boolean) => void;
  /** 主输出瞬时响度 0..1（RMS），供按钮光晕做「聆听态」律动；静音时自然归零 */
  level: () => number;
}

function buildAudio(): AudioHandle | null {
  const AC: typeof AudioContext | undefined =
    typeof window !== "undefined"
      ? window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      : undefined;
  if (!AC) return null;

  const actx = new AC();
  const master = actx.createGain();
  master.gain.value = 0;
  const comp = actx.createDynamicsCompressor();
  comp.threshold.value = -18;
  comp.knee.value = 16;
  comp.ratio.value = 4;
  master.connect(comp);
  comp.connect(actx.destination);

  const musicG = actx.createGain();
  musicG.gain.value = 0.9;
  musicG.connect(master);

  /* 音乐律动采样：analyser 挂在 master 输出（增益后），静音时信号为 0 → 律动自然归零。
   * 供开场按钮光晕/流光做与音乐同呼吸的"聆听态"（Siri 隐喻）。 */
  const analyser = actx.createAnalyser();
  analyser.fftSize = 256;
  master.connect(analyser);
  const ampBuf = new Uint8Array(analyser.frequencyBinCount);

  /* FX 用的短噪声缓冲（仅瞬态音效，非持续噪声层） */
  const nBuf = actx.createBuffer(1, actx.sampleRate * 2, actx.sampleRate);
  const nd = nBuf.getChannelData(0);
  for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;

  /* ---- 主音乐：解码后无缝循环 ---- */
  let musicSrc: AudioBufferSourceNode | null = null;
  let musicBuf: AudioBuffer | null = null;
  let musicFailed = false;
  let wantMusic = false;
  let fallbackStarted = false;
  let fallbackStop: (() => void) | null = null;

  const startMusic = () => {
    if (musicSrc || !musicBuf || actx.state === "closed") return;
    musicSrc = actx.createBufferSource();
    musicSrc.buffer = musicBuf;
    musicSrc.loop = true;
    musicSrc.connect(musicG);
    musicSrc.start();
  };

  /* ---- 回退：干净的合成和弦垫（Asus2add9 ↔ Fmaj9 缓慢交替，无噪声） ---- */
  const startFallback = () => {
    if (fallbackStarted || actx.state === "closed") return;
    fallbackStarted = true;
    const sub = actx.createOscillator();
    sub.type = "sine";
    sub.frequency.value = 55;
    const subG = actx.createGain();
    subG.gain.value = 0.14;
    sub.connect(subG);
    subG.connect(master);
    sub.start();

    const lp = actx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 750;
    lp.Q.value = 0.4;
    const padOut = actx.createGain();
    padOut.gain.value = 0.13;
    lp.connect(padOut);
    padOut.connect(master);

    const makeChord = (freqs: number[]): GainNode => {
      const g = actx.createGain();
      g.gain.value = 0;
      freqs.forEach((f, idx) => {
        [-4, 3].forEach((cents) => {
          const o = actx.createOscillator();
          o.type = idx < 2 ? "triangle" : "sine";
          o.frequency.value = f;
          o.detune.value = cents;
          const og = actx.createGain();
          og.gain.value = idx === 0 ? 0.5 : 0.34;
          o.connect(og);
          og.connect(g);
          o.start();
        });
      });
      g.connect(lp);
      return g;
    };
    const gA = makeChord([110.0, 164.81, 246.94, 369.99]);
    const gB = makeChord([87.31, 130.81, 196.0, 329.63]);
    gA.gain.value = 1;
    let which = true;
    const xfTimer = window.setInterval(() => {
      if (actx.state === "closed") {
        window.clearInterval(xfTimer);
        return;
      }
      const t = actx.currentTime;
      const fadeIn = which ? gA : gB;
      const fadeOutG = which ? gB : gA;
      fadeIn.gain.cancelScheduledValues(t);
      fadeOutG.gain.cancelScheduledValues(t);
      fadeIn.gain.setValueAtTime(fadeIn.gain.value, t);
      fadeOutG.gain.setValueAtTime(fadeOutG.gain.value, t);
      fadeIn.gain.linearRampToValueAtTime(1, t + 7);
      fadeOutG.gain.linearRampToValueAtTime(0, t + 7);
      which = !which;
    }, 16000);
    fallbackStop = () => window.clearInterval(xfTimer);
  };

  preloadTheme().then((ab) => {
    if (!ab || actx.state === "closed") {
      musicFailed = true;
      if (wantMusic) startFallback();
      return;
    }
    // Safari 旧版需要回调式 decodeAudioData，这里统一用 Promise 包装
    actx
      .decodeAudioData(ab.slice(0))
      .then((buf) => {
        musicBuf = buf;
        if (wantMusic) startMusic();
      })
      .catch(() => {
        musicFailed = true;
        if (wantMusic) startFallback();
      });
  });

  /* ---- 科技感音效 ---- */

  /* 开声瞬间：数字系统启动 —— 上行扫频 + 轻微确认音 */
  const boot = () => {
    if (actx.state === "closed") return;
    const t = actx.currentTime;
    const src = actx.createBufferSource();
    src.buffer = nBuf;
    const bp = actx.createBiquadFilter();
    bp.type = "bandpass";
    bp.Q.value = 2.2;
    bp.frequency.setValueAtTime(320, t);
    bp.frequency.exponentialRampToValueAtTime(3600, t + 0.55);
    const g = actx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.09, t + 0.3);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.7);
    src.connect(bp);
    bp.connect(g);
    g.connect(master);
    src.start(t);
    src.stop(t + 0.8);

    const o = actx.createOscillator();
    o.type = "sine";
    o.frequency.setValueAtTime(880, t + 0.32);
    o.frequency.exponentialRampToValueAtTime(1760, t + 0.46);
    const og = actx.createGain();
    og.gain.setValueAtTime(0.0001, t + 0.32);
    og.gain.exponentialRampToValueAtTime(0.045, t + 0.38);
    og.gain.exponentialRampToValueAtTime(0.0001, t + 0.85);
    o.connect(og);
    og.connect(master);
    o.start(t + 0.32);
    o.stop(t + 0.9);
  };

  return {
    ctx: actx,
    swell() {
      // 用户可能把"进入"当作第一次交互：这里也要负责把音乐拉起来
      wantMusic = true;
      if (musicBuf) startMusic();
      else if (musicFailed) startFallback();
      const t = actx.currentTime;
      master.gain.cancelScheduledValues(t);
      master.gain.setValueAtTime(master.gain.value, t);
      master.gain.linearRampToValueAtTime(0.95, t + 0.9);
      master.gain.linearRampToValueAtTime(0.6, t + 3.2);
    },
    whoosh() {
      const t = actx.currentTime;
      const src = actx.createBufferSource();
      src.buffer = nBuf;
      src.loop = true;
      const hp = actx.createBiquadFilter();
      hp.type = "bandpass";
      hp.Q.value = 1.1;
      hp.frequency.setValueAtTime(180, t);
      hp.frequency.exponentialRampToValueAtTime(2400, t + 1.3);
      const g = actx.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.32, t + 0.85);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 1.8);
      src.connect(hp);
      hp.connect(g);
      g.connect(master);
      src.start(t);
      src.stop(t + 2);
    },
    /* 冲越光门瞬间的低频撞击 + 短促镲片 */
    impact() {
      if (actx.state === "closed") return;
      const t = actx.currentTime + 1.05; // 与 warp 白光爆发的时间点对齐
      const o = actx.createOscillator();
      o.type = "sine";
      o.frequency.setValueAtTime(72, t);
      o.frequency.exponentialRampToValueAtTime(34, t + 0.9);
      const og = actx.createGain();
      og.gain.setValueAtTime(0.0001, t);
      og.gain.exponentialRampToValueAtTime(0.5, t + 0.04);
      og.gain.exponentialRampToValueAtTime(0.0001, t + 1.4);
      o.connect(og);
      og.connect(master);
      o.start(t);
      o.stop(t + 1.5);

      const crash = actx.createBufferSource();
      crash.buffer = nBuf;
      const hp = actx.createBiquadFilter();
      hp.type = "highpass";
      hp.frequency.value = 5200;
      const cg = actx.createGain();
      cg.gain.setValueAtTime(0.0001, t);
      cg.gain.exponentialRampToValueAtTime(0.07, t + 0.02);
      cg.gain.exponentialRampToValueAtTime(0.0001, t + 1.3);
      crash.connect(hp);
      hp.connect(cg);
      cg.connect(master);
      crash.start(t);
      crash.stop(t + 1.4);
    },
    fadeOut(sec: number) {
      const t = actx.currentTime;
      master.gain.cancelScheduledValues(t);
      master.gain.setValueAtTime(master.gain.value, t);
      master.gain.linearRampToValueAtTime(0.0001, t + sec);
      fallbackStop?.();
      window.setTimeout(() => {
        actx.close().catch(() => {});
      }, sec * 1000 + 200);
    },
    setOn(on: boolean) {
      const t = actx.currentTime;
      if (on) {
        const wasSilent = master.gain.value < 0.05;
        wantMusic = true;
        if (musicBuf) startMusic();
        else if (musicFailed) startFallback();
        if (wasSilent) boot();
      }
      master.gain.cancelScheduledValues(t);
      master.gain.setValueAtTime(master.gain.value, t);
      master.gain.linearRampToValueAtTime(on ? 0.6 : 0.0001, t + (on ? 1.6 : 0.4));
    },
    level() {
      if (actx.state !== "running") return 0;
      analyser.getByteTimeDomainData(ampBuf);
      let sum = 0;
      for (let i = 0; i < ampBuf.length; i++) {
        const d = (ampBuf[i] - 128) / 128;
        sum += d * d;
      }
      // 音乐 RMS 通常 0.05~0.3，×3.2 拉到可感知区间再截断
      return Math.min(1, Math.sqrt(sum / ampBuf.length) * 3.2);
    },
  };
}

/* ================= 星门产品 LOGO 粒子引擎（rAF 物理驱动） =================
 * 为什么不用 CSS keyframes：贝塞尔时间函数 + 冲出余量会把「最快、最大」的一段推到屏幕外播放
 * （实测出屏发生在时间轴 57%~70% 处，其后全是观众看不见的空放）。这里改为等加速运动学的
 * 闭式解 d(t) = v0·t + a·t²/2：
 *  - 出生即 ~250px/s（无原地悬停），出屏瞬间 950~1600px/s，加速全程都发生在屏内；
 *  - 位置由绝对时间求值，与帧率无关（后台节流/低端机不改变轨迹，只降采样）；
 *  - 尺寸随行程占比 p=d/D 按 p^1.7 放大——越贴近屏幕边缘越急剧变大（迎面冲来），出屏即回收；
 *  - 发射器全局左右严格交替 + 随机间隔，同侧数量差恒 ≤1，堆积不可能再发生；
 *  - 7 图标轮转发牌，同屏同款恒 ≤1；同屏总数 ≤ 会话随机上限（桌面 5~7、移动 4~5，恒 ≤8）；
 *  - 三层景深（远小慢暗 / 中 / 近大快亮），拖尾光迹画在星流 canvas 上，长度天然 ∝ 速度。 */

function shuffleArr<T>(arr: T[]): T[] {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** 三层景深：尺寸/速度/透明度/拖尾/辉光五维分层，近快远慢构成透视纵深。 */
const GATE_LAYERS = [
  { cls: "bl-gl-far", sizeMul: 0.62, vMul: 0.82, alpha: 0.55, trailA: 0.14, glowPx: 6, glowA: 0.35, weight: 0.3 },
  { cls: "bl-gl-mid", sizeMul: 1, vMul: 1, alpha: 0.85, trailA: 0.22, glowPx: 12, glowA: 0.55, weight: 0.45 },
  { cls: "bl-gl-near", sizeMul: 1.35, vMul: 1.22, alpha: 1, trailA: 0.3, glowPx: 16, glowA: 0.7, weight: 0.25 },
] as const;
type GateLayer = (typeof GATE_LAYERS)[number];

type GateP = {
  key: ProductKey;
  el: HTMLSpanElement;
  born: number; // performance.now()
  ux: number;
  uy: number;
  side: 1 | -1;
  layer: GateLayer;
  baseSz: number; // px（已含层级/移动端系数）
  v0: number; // px/s
  acc: number; // px/s²
  dExit: number; // 回收距离（中心越过屏幕边缘 + 放大后半径 + 余量）
  dEdge: number; // 中心恰好到屏幕边缘的距离（用于 offEdgePx 统计）
  sStart: number;
  sEnd: number;
  alpha: number;
  trailA: number;
  color: string;
  blurDone: boolean;
  trail: { x: number; y: number }[];
};

/** QA 观测钩子（scripts/qa-intro-motion.mjs 消费）：引擎每 tick 原地更新。 */
type BlIntroStats = {
  t0: number;
  ticks: number;
  cap: number;
  mobile: boolean;
  lite: boolean;
  /** v0/ve/planMs 为闭式运动学参数（出生速度/出屏末速/计划飞行时长）：
   *  QA 用它们做帧率无关的解析验收——无 GPU 的 headless 环境 rAF 会被节流，
   *  采样式测速会失真，而真实浏览器中轨迹与这组参数严格一致。 */
  spawns: { t: number; key: string; side: 1 | -1; layer: string; v0: number; ve: number; planMs: number }[];
  exits: { t: number; key: string; flightMs: number; exitV: number; offEdgePx: number }[];
  live: { key: string; x: number; y: number; v: number; scale: number; side: 1 | -1 }[];
};

type GateRipple = { x: number; y: number; t0: number; dur: number; r0: number; r1: number; color: string };

type GateEngine = {
  live: GateP[];
  queue: ProductKey[]; // 洗牌后的图标轮转队列（发牌去重）
  side: 1 | -1; // 上一次发射侧，下一次取反 → 严格交替
  nextAt: number;
  cap: number;
  mobile: boolean;
  /** 低端机降级：deviceMemory ≤4GB 或 ≤4 核 → 关拖尾/涟漪/出生模糊、粒子 ≤4、发射放缓 */
  lite: boolean;
  ripples: GateRipple[]; // 星门出生涟漪（画在星流 canvas 上）
  stats: BlIntroStats;
};

/** 方向随机但限定在指定侧的 128° 扇区（右 -38°→90°、左 90°→218°），
 *  正上方 ±52° 锥区永久豁免——不打扰顶部公司主 LOGO 与标题。 */
function pickGateAngle(side: 1 | -1): number {
  const half = (52 * Math.PI) / 180;
  const span = Math.PI - half;
  return side === 1 ? -Math.PI / 2 + half + Math.random() * span : Math.PI / 2 + Math.random() * span;
}

function pickGateLayer(): GateLayer {
  const r = Math.random();
  let acc = 0;
  for (const l of GATE_LAYERS) {
    acc += l.weight;
    if (r < acc) return l;
  }
  return GATE_LAYERS[1];
}

/** 发射一颗粒子：闭式运动学参数 + 创建 DOM（React 之外的专属容器，避免热路径重渲染）。 */
function spawnGateParticle(
  eng: GateEngine,
  host: HTMLElement,
  W: number,
  H: number,
  originY: number,
  now: number
): GateP | null {
  const key = eng.queue.find((k) => !eng.live.some((p) => p.key === k));
  if (!key) return null;
  eng.queue.splice(eng.queue.indexOf(key), 1);
  eng.queue.push(key); // 轮转到队尾，长期均匀曝光 7 款图标
  eng.side = eng.side === 1 ? -1 : 1;
  const side = eng.side;
  const layer = pickGateLayer();
  const ang = pickGateAngle(side);
  const ux = Math.cos(ang);
  const uy = Math.sin(ang);
  const baseSz = (26 + Math.random() * 40) * layer.sizeMul * (eng.mobile ? 0.75 : 1);
  const sStart = 0.5;
  const sEnd = 1.9 + Math.random() * 0.5;
  const margin = (baseSz * sEnd) / 2 + 12; // 放大后的半径 + 余量，保证回收时已完整出屏
  const hD = Math.abs(ux) > 1e-3 ? (W * 0.5) / Math.abs(ux) : Infinity;
  const vD = Math.abs(uy) > 1e-3 ? (uy < 0 ? originY : H - originY) / Math.abs(uy) : Infinity;
  const dEdge = Math.min(hD, vD);
  const dExit = dEdge + margin / Math.max(0.2, Math.max(Math.abs(ux), Math.abs(uy)));
  const v0 = (240 + Math.random() * 60) * layer.vMul;
  // 末速随机，但设与距离挂钩的下限：T = 2D/(v0+ve) ≤ ~1.5s，长对角线也绝不拖沓
  const ve = Math.max((980 + Math.random() * 350) * layer.vMul, (2 * dExit) / 1.5 - v0);
  const acc = (ve * ve - v0 * v0) / (2 * dExit);

  const el = document.createElement("span");
  el.className = `bl-gate-logo ${layer.cls}`;
  el.style.width = `${baseSz.toFixed(0)}px`;
  el.style.height = `${baseSz.toFixed(0)}px`;
  el.style.marginLeft = `${(-baseSz / 2).toFixed(0)}px`;
  el.style.marginTop = `${(-baseSz / 2).toFixed(0)}px`;
  el.style.opacity = "0";
  // launch（冲越光门）CSS 动画仍按方向炸出，沿用这两个变量
  el.style.setProperty("--tx", ux.toFixed(3));
  el.style.setProperty("--ty", uy.toFixed(3));
  const img = document.createElement("img");
  img.src = PRODUCT_IMG[key];
  img.alt = "";
  img.draggable = false;
  img.decoding = "async";
  // C4 产品主色辉光：辉光/拖尾/涟漪同色，强化产品识别（近层追加紫晕纵深）
  const glow = PRODUCT_GLOW[key];
  img.style.filter =
    `drop-shadow(0 0 ${layer.glowPx}px rgba(${glow},${layer.glowA}))` +
    (layer.cls === "bl-gl-near" ? " drop-shadow(0 0 34px rgba(167,139,250,0.35))" : "");
  el.appendChild(img);
  host.appendChild(el);

  return {
    key,
    el,
    born: now,
    ux,
    uy,
    side,
    layer,
    baseSz,
    v0,
    acc,
    dExit,
    dEdge,
    sStart,
    sEnd,
    alpha: layer.alpha,
    trailA: layer.trailA,
    color: glow,
    blurDone: false,
    trail: [],
  };
}

/** 引擎每帧步进：出生调度 → 闭式位置求值 → 出屏回收 → 拖尾绘制 → QA 统计。
 *  由星流 canvas 的同一个 rAF 循环调用（单循环、拖尾与星流零同步误差）。
 *  amp（0..1 音乐响度）驱动发射节奏：响度满格时发射间隔约收紧 45%——音乐越燃喷涌越密。 */
function tickGateEngine(
  eng: GateEngine,
  host: HTMLElement,
  ctx: CanvasRenderingContext2D,
  W: number,
  H: number,
  originY: number,
  now: number,
  amp: number
) {
  if (now >= eng.nextAt && eng.live.length < eng.cap) {
    const p = spawnGateParticle(eng, host, W, H, originY, now);
    if (p) {
      eng.live.push(p);
      const ve = Math.sqrt(p.v0 * p.v0 + 2 * p.acc * p.dExit);
      eng.stats.spawns.push({
        t: now,
        key: p.key,
        side: p.side,
        layer: p.layer.cls,
        v0: Math.round(p.v0),
        ve: Math.round(ve),
        planMs: Math.round(((2 * p.dExit) / (p.v0 + ve)) * 1000),
      });
      if (eng.stats.spawns.length > 120) eng.stats.spawns.shift();
      // C3 星门涟漪：出生瞬间从按钮口荡出一圈同色微光（低端机豁免）
      if (!eng.lite) {
        eng.ripples.push({
          x: W * 0.5,
          y: originY,
          t0: now,
          dur: 260,
          r0: 6,
          r1: 22 + p.baseSz * 0.35,
          color: p.color,
        });
      }
    }
    eng.nextAt =
      now + (150 + Math.random() * 260) * (eng.mobile ? 1.35 : 1) * (eng.lite ? 1.25 : 1) * (1 - 0.45 * amp);
  }

  // 涟漪绘制：260ms 内半径缓出扩张、透明度线性消散
  for (let i = eng.ripples.length - 1; i >= 0; i--) {
    const rp = eng.ripples[i];
    const p = (now - rp.t0) / rp.dur;
    if (p >= 1) {
      eng.ripples.splice(i, 1);
      continue;
    }
    const ease = 1 - (1 - p) * (1 - p);
    ctx.strokeStyle = `rgba(${rp.color},${(0.4 * (1 - p)).toFixed(3)})`;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(rp.x, rp.y, rp.r0 + (rp.r1 - rp.r0) * ease, 0, Math.PI * 2);
    ctx.stroke();
  }

  const originX = W * 0.5;
  for (let i = eng.live.length - 1; i >= 0; i--) {
    const p = eng.live[i];
    const t = (now - p.born) / 1000;
    const d = p.v0 * t + 0.5 * p.acc * t * t;
    const v = p.v0 + p.acc * t;
    if (d >= p.dExit) {
      eng.stats.exits.push({
        t: now,
        key: p.key,
        flightMs: Math.round(now - p.born),
        exitV: Math.round(v),
        offEdgePx: Math.round(d - p.dEdge),
      });
      if (eng.stats.exits.length > 120) eng.stats.exits.shift();
      p.el.remove();
      eng.live.splice(i, 1);
      continue;
    }
    const prog = d / p.dExit;
    const s = p.sStart + (p.sEnd - p.sStart) * Math.pow(prog, 1.7);
    const px = p.ux * d;
    const py = p.uy * d;
    p.el.style.transform = `translate3d(${px.toFixed(1)}px, ${py.toFixed(1)}px, 0) scale(${s.toFixed(3)})`;
    const fadeIn = Math.min(1, (now - p.born) / 180);
    p.el.style.opacity = (p.alpha * fadeIn).toFixed(3);
    // 出生 0.18s 凝聚效果：blur 4→0 只在出生窗口逐帧更新，此后清除（低端机直接豁免）
    if (!eng.lite) {
      if (fadeIn < 1) p.el.style.filter = `blur(${(4 * (1 - fadeIn)).toFixed(1)}px)`;
      else if (!p.blurDone) {
        p.el.style.filter = "";
        p.blurDone = true;
      }
    }
    // 拖尾光迹：最近 5 个采样点连线，新亮旧暗；点距天然 ∝ 速度 → 越快尾越长
    if (!eng.lite) {
      const gx = originX + px;
      const gy = originY + py;
      p.trail.push({ x: gx, y: gy });
      if (p.trail.length > 5) p.trail.shift();
      for (let j = 1; j < p.trail.length; j++) {
        const aSeg = p.trailA * (j / p.trail.length) * fadeIn;
        ctx.strokeStyle = `rgba(${p.color},${aSeg.toFixed(3)})`;
        ctx.lineWidth = Math.max(1, (s * p.baseSz) / 30);
        ctx.beginPath();
        ctx.moveTo(p.trail[j - 1].x, p.trail[j - 1].y);
        ctx.lineTo(p.trail[j].x, p.trail[j].y);
        ctx.stroke();
      }
    }
  }

  eng.stats.ticks++;
  eng.stats.live = eng.live.map((p) => {
    const t = (now - p.born) / 1000;
    const d = p.v0 * t + 0.5 * p.acc * t * t;
    return {
      key: p.key,
      x: originX + p.ux * d,
      y: originY + p.uy * d,
      v: Math.round(p.v0 + p.acc * t),
      scale: +(p.sStart + (p.sEnd - p.sStart) * Math.pow(Math.min(1, d / p.dExit), 1.7)).toFixed(2),
      side: p.side,
    };
  });
}

/* ================= 组件 ================= */

export default function IntroCover() {
  const { lang } = useLang();
  const c = COPY[lang];

  const [show, setShow] = useState<boolean>(() => !dismissedInRuntime);
  const [phase, setPhase] = useState<"idle" | "warp" | "leave">("idle");
  const [soundOn, setSoundOn] = useState(false);
  // A/B intro_auto_enter B 桶：无操作 12s 自动进入；≤3s 时把剩余秒数显示在 hint 行
  const [autoLeft, setAutoLeft] = useState<number | null>(null);

  const overlayRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef<AudioHandle | null>(null);
  const enteredRef = useRef(false);
  const userMutedRef = useRef(false);
  const targetSpeedRef = useRef(0.0022);
  const timersRef = useRef<number[]>([]);
  const reducedRef = useRef(false);
  const touchYRef = useRef<number | null>(null);
  const enterBtnRef = useRef<HTMLButtonElement>(null);
  const [gateY, setGateY] = useState<number | null>(null);
  // LOGO 粒子引擎（DOM 直驱，React 只渲染空容器 → SSR/hydration 天然一致，热路径零重渲染）
  const gateHostRef = useRef<HTMLDivElement>(null);
  const gateYRef = useRef<number | null>(null);
  const engineRef = useRef<GateEngine | null>(null);
  const phaseRef = useRef<"idle" | "warp" | "leave">("idle");
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);
  // C6 转化埋点：开场展示时刻（算停留时长）与 sound_on 只报一次的闸门
  const shownAtRef = useRef(0);
  const soundTrackedRef = useRef(false);
  // Siri 按钮指针跟随（rAF 节流写 CSS 变量，避免 pointermove 频率打满主线程）
  const btnFxRafRef = useRef(0);
  // 声音可视化：主输出响度的指数平滑值 → 按钮 --amp
  const ampRef = useRef(0);
  // 波浪形变滤镜的位移强度节点：rAF 每帧写 scale（基础蠕动 + 音乐响度 + 按压充能）
  const waveDispRef = useRef<SVGFEDisplacementMapElement>(null);

  const startSound = useCallback(() => {
    userMutedRef.current = false;
    if (!soundTrackedRef.current) {
      soundTrackedRef.current = true;
      track("intro_sound_on");
    }
    const existing = audioRef.current;
    if (existing) {
      const apply = () => {
        // 进场瞬间由 swell() 负责音量曲线，避免二次 ramp 抵消
        if (!enteredRef.current) existing.setOn(true);
        setSoundOn(true);
      };
      if (existing.ctx.state === "running") apply();
      else existing.ctx.resume().then(apply).catch(() => {});
      return;
    }
    const handle = buildAudio();
    if (!handle) return;
    audioRef.current = handle;
    handle.ctx
      .resume()
      .then(() => {
        if (!enteredRef.current) handle.setOn(true);
        setSoundOn(true);
      })
      .catch(() => {});
  }, []);

  const stopSound = useCallback(() => {
    userMutedRef.current = true;
    audioRef.current?.setOn(false);
    setSoundOn(false);
  }, []);

  /* ===== Siri 风格按钮交互：指针跟随光斑 + 轻量 3D 倾斜（rAF 节流），按压充能由 data-charging 驱动 ===== */
  const onBtnPointerMove = useCallback((e: React.PointerEvent<HTMLButtonElement>) => {
    if (reducedRef.current) return;
    const el = enterBtnRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const mx = Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100));
    const my = Math.max(0, Math.min(100, ((e.clientY - r.top) / r.height) * 100));
    cancelAnimationFrame(btnFxRafRef.current);
    btnFxRafRef.current = requestAnimationFrame(() => {
      el.style.setProperty("--mx", `${mx.toFixed(1)}%`);
      el.style.setProperty("--my", `${my.toFixed(1)}%`);
      el.style.setProperty("--rx", `${((my - 50) / -14).toFixed(2)}deg`);
      el.style.setProperty("--ry", `${((mx - 50) / 22).toFixed(2)}deg`);
    });
  }, []);

  const onBtnPointerLeave = useCallback(() => {
    const el = enterBtnRef.current;
    if (!el) return;
    cancelAnimationFrame(btnFxRafRef.current);
    el.style.setProperty("--mx", "50%");
    el.style.setProperty("--my", "50%");
    el.style.setProperty("--rx", "0deg");
    el.style.setProperty("--ry", "0deg");
    delete el.dataset.charging;
  }, []);

  const enter = useCallback((method: "click" | "scroll" | "touch" | "key" | "auto" = "click") => {
    if (enteredRef.current) return;
    enteredRef.current = true;
    dismissedInRuntime = true;
    // 进入方式 + 停留时长 + 是否开声，一个事件看全进入行为（比拆多个事件更好做漏斗）
    track("intro_enter", {
      method,
      dwellMs: shownAtRef.current ? Math.round(performance.now() - shownAtRef.current) : null,
      sound: !userMutedRef.current && soundTrackedRef.current,
    });
    try {
      sessionStorage.setItem(SEEN_KEY, "1");
    } catch {}
    // 尊重用户手动静音的选择；否则进场时音乐涌起 + 冲越声 + 光门撞击
    if (!userMutedRef.current) {
      startSound();
      audioRef.current?.swell();
      audioRef.current?.whoosh();
      if (!reducedRef.current) audioRef.current?.impact();
    }
    const reduced = reducedRef.current;
    if (!reduced) targetSpeedRef.current = 0.075;
    setPhase("warp");
    timersRef.current.push(
      window.setTimeout(() => {
        setPhase("leave");
        document.body.style.overflow = "";
        // 通知正文(Hero)开始"冲越交接"动画:标题逐字聚焦 + 冲击波
        window.dispatchEvent(new Event("bl-intro-entered"));
      }, reduced ? 100 : 1250),
      window.setTimeout(() => {
        setShow(false);
        audioRef.current?.fadeOut(6); // 进入正文后 6s 内淡出退场，不在内页残留
      }, reduced ? 700 : 2400)
    );
  }, [startSound]);

  /* 会话内只出现一次 + 锁滚动 + 全局手势/键盘 */
  useEffect(() => {
    reducedRef.current = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let seen = false;
    try {
      seen = !!sessionStorage.getItem(SEEN_KEY);
    } catch {}
    if (seen) {
      dismissedInRuntime = true;
      setShow(false);
      return;
    }
    // 桌面端开场展示期间就预取配乐，点击开声时即刻可播；移动端省流量，首次手势后再拉取
    if (!window.matchMedia("(max-width: 767px)").matches) preloadTheme();
    document.body.style.overflow = "hidden";
    shownAtRef.current = performance.now();
    track("intro_shown");

    const gestureOnce = () => {
      track("intro_first_gesture", {
        sinceShownMs: shownAtRef.current ? Math.round(performance.now() - shownAtRef.current) : null,
      });
      if (!enteredRef.current) startSound();
      document.removeEventListener("pointerdown", gestureOnce);
      document.removeEventListener("keydown", gestureOnce);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Escape") enter("key");
    };
    document.addEventListener("pointerdown", gestureOnce);
    document.addEventListener("keydown", gestureOnce);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", gestureOnce);
      document.removeEventListener("keydown", gestureOnce);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [enter, startSound]);

  /* A/B 实验 intro_auto_enter：B 桶在完全无操作 12s 后自动进入正文（最后 3s 在 hint 行倒计时，
   * 任意操作立即取消且本次会话不再武装）；A 桶仅曝光作对照。要回答的问题：自动进入能否
   * 挽回「看完动画就走神/流失」的会话，而不打扰主动探索的用户。 */
  useEffect(() => {
    if (!show) return;
    const variant = abVariant("intro_auto_enter");
    abExpose("intro_auto_enter", variant);
    if (variant !== "b") return;
    const deadline = performance.now() + 12_000;
    const cancelEvents = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"] as const;
    const timer = window.setInterval(() => {
      const left = deadline - performance.now();
      if (left <= 0) {
        window.clearInterval(timer);
        if (!enteredRef.current) enter("auto");
        return;
      }
      setAutoLeft(left <= 3200 ? Math.ceil(left / 1000) : null);
    }, 250);
    const cancel = () => {
      window.clearInterval(timer);
      setAutoLeft(null);
      cancelEvents.forEach((ev) => document.removeEventListener(ev, cancel));
    };
    cancelEvents.forEach((ev) => document.addEventListener(ev, cancel, { passive: true }));
    return () => {
      window.clearInterval(timer);
      cancelEvents.forEach((ev) => document.removeEventListener(ev, cancel));
    };
  }, [show, enter]);

  /* 星流画布：从光门向外辐射的星际穿越粒子 */
  useEffect(() => {
    if (!show) return;
    const canvas = canvasRef.current;
    const overlay = overlayRef.current;
    if (!canvas || !overlay) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (reducedRef.current) return;

    const DPR = Math.min(window.devicePixelRatio || 1, 2);
    let W = 0, H = 0, CX = 0, CY = 0;
    let raf = 0;
    let speed = 0.0022;
    type Star = { x: number; y: number; z: number; hue: number };
    const stars: Star[] = [];

    const spawn = (anywhere: boolean): Star => ({
      x: Math.random() * 2 - 1,
      y: Math.random() * 2 - 1,
      z: anywhere ? 0.12 + Math.random() * 0.88 : 1,
      hue: Math.random(),
    });

    const resize = () => {
      W = overlay.clientWidth;
      H = overlay.clientHeight;
      canvas.width = W * DPR;
      canvas.height = H * DPR;
      canvas.style.width = `${W}px`;
      canvas.style.height = `${H}px`;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      CX = W * 0.5;
      CY = H * 0.58;
      const count = Math.min(300, Math.round((W * H) / 6500));
      if (stars.length > count) stars.length = count;
      while (stars.length < count) stars.push(spawn(true));
    };

    const tick = () => {
      speed += (targetSpeedRef.current - speed) * 0.045;
      ctx.clearRect(0, 0, W, H);
      const scale = Math.min(W, H) * 0.9;
      for (let i = 0; i < stars.length; i++) {
        const s = stars[i];
        const pz = s.z;
        s.z -= speed * (0.4 + s.z);
        if (s.z <= 0.03) {
          stars[i] = spawn(false);
          continue;
        }
        const px = CX + (s.x / s.z) * scale;
        const py = CY + (s.y / s.z) * scale;
        if (px < -60 || px > W + 60 || py < -60 || py > H + 60) {
          stars[i] = spawn(false);
          continue;
        }
        const qx = CX + (s.x / pz) * scale;
        const qy = CY + (s.y / pz) * scale;
        const depth = 1 - s.z;
        const alpha = Math.max(0, Math.min(1, depth * 1.1)) * 0.85;
        const color = s.hue < 0.55 ? "240,253,255" : s.hue < 0.8 ? "103,232,249" : "196,181,253";
        ctx.strokeStyle = `rgba(${color},${alpha.toFixed(3)})`;
        ctx.lineWidth = Math.max(0.5, depth * 2.1);
        ctx.beginPath();
        ctx.moveTo(qx, qy);
        ctx.lineTo(px, py);
        ctx.stroke();
      }
      // LOGO 粒子引擎与星流共用本循环：warp/leave 阶段冻结（CSS launch 动画接管）
      const eng = engineRef.current;
      const host = gateHostRef.current;
      if (eng && host && phaseRef.current === "idle") {
        tickGateEngine(eng, host, ctx, W, H, gateYRef.current ?? H * 0.72, performance.now(), ampRef.current);
      }
      // 声音律动 → 按钮「聆听态」：主输出 RMS 指数平滑后写入 --amp（光晕/流光亮度联动），静音自然归零
      const ampTarget = audioRef.current?.level() ?? 0;
      ampRef.current += (ampTarget - ampRef.current) * 0.18;
      const btnEl = enterBtnRef.current;
      if (btnEl) btnEl.style.setProperty("--amp", ampRef.current < 0.005 ? "0" : ampRef.current.toFixed(3));
      // 波浪边缘随声而动：位移强度 = 基础蠕动 + 音乐响度 + 按压充能（只改 scale，
      // 湍流噪声场本身有 SMIL 缓摆，浏览器只重算置换、不重算噪声，逐帧成本可忽略）
      const disp = waveDispRef.current;
      if (disp) {
        disp.scale.baseVal = 6 + ampRef.current * 8 + (btnEl?.dataset.charging === "1" ? 3 : 0);
      }
      raf = requestAnimationFrame(tick);
    };

    resize();
    window.addEventListener("resize", resize);
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [show]);

  /* LOGO 粒子引擎初始化：会话随机上限（桌面 5~7、移动 4~5，恒 ≤8），洗牌轮转发牌；
   *  同时预热 7 张图标（浏览器缓存 + 解码），出生瞬间不再有解码抖动。 */
  useEffect(() => {
    if (!show || reducedRef.current) return;
    const mobile = window.matchMedia("(max-width: 767px)").matches;
    const nav = navigator as Navigator & { deviceMemory?: number };
    const lite = (nav.deviceMemory ?? 8) <= 4 || (navigator.hardwareConcurrency || 8) <= 4;
    const cap = Math.min(
      lite ? 4 : 7,
      mobile ? 4 + Math.floor(Math.random() * 2) : 5 + Math.floor(Math.random() * 3)
    );
    const stats: BlIntroStats = {
      t0: performance.now(),
      ticks: 0,
      cap,
      mobile,
      lite,
      spawns: [],
      exits: [],
      live: [],
    };
    engineRef.current = {
      live: [],
      queue: shuffleArr([...PRODUCT_ORDER]),
      side: Math.random() < 0.5 ? 1 : -1,
      nextAt: performance.now() + 320,
      cap,
      mobile,
      lite,
      ripples: [],
      stats,
    };
    (window as unknown as { __blIntroStats?: BlIntroStats }).__blIntroStats = stats;
    PRODUCT_ORDER.forEach((k) => {
      const im = new Image();
      im.src = PRODUCT_IMG[k];
    });
    const host = gateHostRef.current;
    return () => {
      engineRef.current = null;
      if (host) host.innerHTML = "";
      delete (window as unknown as { __blIntroStats?: BlIntroStats }).__blIntroStats;
    };
  }, [show]);

  /* prefers-reduced-motion：不跑引擎，静态散布 5 颗做装饰（左右交替、避开顶部锥区）。 */
  useEffect(() => {
    if (!show || !reducedRef.current) return;
    const host = gateHostRef.current;
    if (!host) return;
    const W = window.innerWidth;
    const H = window.innerHeight;
    const oY = H * 0.72;
    shuffleArr([...PRODUCT_ORDER])
      .slice(0, 5)
      .forEach((k, i) => {
        const side: 1 | -1 = i % 2 === 0 ? 1 : -1;
        const ang = pickGateAngle(side);
        const ux = Math.cos(ang);
        const uy = Math.sin(ang);
        const hD = Math.abs(ux) > 1e-3 ? (W * 0.5) / Math.abs(ux) : Infinity;
        const vD = Math.abs(uy) > 1e-3 ? (uy < 0 ? oY : H - oY) / Math.abs(uy) : Infinity;
        const d = Math.min(hD, vD) * (0.35 + 0.13 * (i % 3));
        const sz = 34 + (i % 3) * 10;
        const el = document.createElement("span");
        el.className = "bl-gate-logo bl-gl-mid";
        el.style.width = `${sz}px`;
        el.style.height = `${sz}px`;
        el.style.marginLeft = `${-sz / 2}px`;
        el.style.marginTop = `${-sz / 2}px`;
        el.style.opacity = "0.55";
        el.style.transform = `translate3d(${(ux * d).toFixed(0)}px, ${(uy * d).toFixed(0)}px, 0)`;
        const img = document.createElement("img");
        img.src = PRODUCT_IMG[k];
        img.alt = "";
        img.draggable = false;
        el.appendChild(img);
        host.appendChild(el);
      });
    return () => {
      host.innerHTML = "";
    };
  }, [show]);

  /* 把产品 LOGO 喷涌原点对齐到「进入 AI 世界」按钮中心（星门口） */
  useEffect(() => {
    if (!show) return;
    const measure = () => {
      const b = enterBtnRef.current?.getBoundingClientRect();
      const o = overlayRef.current?.getBoundingClientRect();
      if (b && o && b.height > 0) {
        const y = b.top - o.top + b.height / 2;
        gateYRef.current = y; // 引擎热路径读 ref，避免依赖 React 状态
        setGateY(y);
      }
    };
    // 入场元素有 rise-in 动画，稍延迟并多测几次，确保按钮落位后再对齐
    const t1 = window.setTimeout(measure, 300);
    const t2 = window.setTimeout(measure, 1400);
    window.addEventListener("resize", measure);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.removeEventListener("resize", measure);
    };
  }, [show]);

  /* 组件真正卸载（离开页面）时的兜底清理 */
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((t) => window.clearTimeout(t));
      if (audioRef.current && audioRef.current.ctx.state !== "closed") {
        audioRef.current.ctx.close().catch(() => {});
      }
    };
  }, []);

  if (!show) return null;

  return (
    <div
      id="bl-intro"
      ref={overlayRef}
      className={`bl-intro keep-dark${phase === "warp" || phase === "leave" ? " warping" : ""}${phase === "leave" ? " leaving" : ""}`}
      onWheel={(e) => {
        if (e.deltaY > 12) enter("scroll");
      }}
      onTouchStart={(e) => {
        touchYRef.current = e.touches[0].clientY;
      }}
      onTouchMove={(e) => {
        if (touchYRef.current !== null && touchYRef.current - e.touches[0].clientY > 26) enter("touch");
      }}
      role="dialog"
      aria-label={lang === "zh" ? "开场页" : "Intro"}
    >
      {/* 同会话重复访问：hydration 前直接隐藏，避免闪现 */}
      <script
        dangerouslySetInnerHTML={{
          __html:
            "try{if(sessionStorage.getItem('" +
            SEEN_KEY +
            "')){var e=document.getElementById('bl-intro');if(e)e.style.display='none';document.body.style.overflow=''}else{document.body.style.overflow='hidden'}}catch(e){}",
        }}
      />
      <noscript>
        <style>{`#bl-intro{display:none}`}</style>
      </noscript>
      {/* 按视口宽高比只预载对应背景图，压缩首屏 LCP */}
      <link rel="preload" as="image" href="/intro/cosmos-wide.jpg" media="(min-aspect-ratio: 4/5)" />
      <link rel="preload" as="image" href="/intro/cosmos-tall.jpg" media="(max-aspect-ratio: 4/5)" />

      <div className="bl-intro-bg" aria-hidden />
      <canvas ref={canvasRef} className="bl-intro-stars" aria-hidden />
      <div className="bl-intro-tint" aria-hidden />
      <div className="bl-warp-glow" aria-hidden />

      {/* 产品 LOGO 星门喷涌：容器由 rAF 引擎直驱（tickGateEngine），React 只负责空容器与 launch 类 */}
      <div
        ref={gateHostRef}
        className={`bl-gate-logos${phase !== "idle" ? " launch" : ""}`}
        style={gateY != null ? ({ top: `${gateY}px` } as CSSProperties) : undefined}
        aria-hidden
      />

      <button
        type="button"
        className={`bl-sound-toggle${soundOn ? " on" : ""}`}
        aria-pressed={soundOn}
        onClick={(e) => {
          e.stopPropagation();
          if (soundOn) stopSound();
          else startSound();
        }}
      >
        <span className="bars" aria-hidden>
          <i />
          <i />
          <i />
        </span>
        <span>{soundOn ? c.soundOn : c.soundOff}</span>
      </button>

      <div className="bl-intro-content">
        <div className="bl-stage bl-kicker">
          BOUNDLESS <span className="dot">·</span> AI ENGINE
          <span className="zh-part">
            {" "}
            <span className="dot">·</span> 无界引擎
          </span>
        </div>
        {/* 公司主标 + 无界科技 组成居中锁定图 */}
        <div className="bl-stage bl-brandmark">
          <img src="/brand/logos/boundless-mark-512.png" alt={BRAND.company.full} draggable={false} />
        </div>
        <div className="bl-title-zh">{c.title}</div>
        <div className="bl-stage bl-title-en">{c.sub}</div>
        <div className="bl-stage bl-tagline">
          {c.tagline}
          <span className="en">{c.taglineSub}</span>
        </div>
        <div className="bl-stage bl-five">
          {lang === "zh" ? (
            <>
              穿越 <b>获客</b> · <b>容貌</b> · <b>声音</b> · <b>身份</b> · <b>语言</b> · <b>成交</b> 六重边界
            </>
          ) : (
            <>
              BEYOND <b>REACH</b> · <b>FACE</b> · <b>VOICE</b> · <b>IDENTITY</b> · <b>LANGUAGE</b> · <b>DEALS</b>
            </>
          )}
        </div>
        <div className="bl-stage bl-enter-wrap">
          {/* Siri 风格按钮：苹果式有机波浪轮廓（湍流置换滤镜 + blob 圆角形变，双尺度叠加）
              + 多彩流光描边 + 呼吸光晕 + 指针跟随光斑/3D 倾斜 + 按压充能 + 能量扩散环；
              形状层（.siri-shape）被滤镜扭曲成波浪，文字层独立在外保持锐利可读 */}
          <button
            ref={enterBtnRef}
            type="button"
            className="bl-enter-btn"
            onClick={() => enter("click")}
            onPointerMove={onBtnPointerMove}
            onPointerLeave={onBtnPointerLeave}
            onPointerDown={(e) => {
              e.currentTarget.dataset.charging = "1";
            }}
            onPointerUp={(e) => {
              delete e.currentTarget.dataset.charging;
            }}
            onPointerCancel={(e) => {
              delete e.currentTarget.dataset.charging;
            }}
          >
            {/* 湍流置换滤镜：横向低频/纵向高频适配宽扁轮廓；噪声场 SMIL 缓摆产生「蠕动」，
                位移强度由 rAF 写入（基础 6 + 音乐响度×8 + 充能 +3），reduced-motion 下 CSS 侧不引用本滤镜 */}
            <svg className="bl-wave-defs" aria-hidden focusable="false">
              <filter id="bl-btn-wave" x="-25%" y="-60%" width="150%" height="220%">
                <feTurbulence type="fractalNoise" baseFrequency="0.009 0.032" numOctaves="2" seed="7" result="n">
                  <animate
                    attributeName="baseFrequency"
                    dur="9s"
                    values="0.009 0.032;0.012 0.026;0.009 0.032"
                    repeatCount="indefinite"
                  />
                </feTurbulence>
                <feDisplacementMap ref={waveDispRef} in="SourceGraphic" in2="n" scale="6" xChannelSelector="R" yChannelSelector="G" />
              </filter>
            </svg>
            {/* 光晕本身是重模糊，不进滤镜（省一大圈滤镜栅格区域）；波浪只作用于清晰轮廓层 */}
            <span className="siri-halo" aria-hidden />
            <span className="siri-shape" aria-hidden>
              <span className="siri-blob" />
              <span className="siri-ring">
                <i className="flow" />
              </span>
              <span className="siri-glass" />
            </span>
            <span className="ring r1" aria-hidden />
            <span className="ring r2" aria-hidden />
            <span className="label">
              {Array.from(c.enter).map((ch, i) => (
                <span key={i} className="ch" style={{ "--i": i } as CSSProperties}>
                  {ch === " " ? "\u00A0" : ch}
                </span>
              ))}
            </span>
            <span className="arrow" aria-hidden>
              →
            </span>
          </button>
          <div className="bl-enter-hint">
            {autoLeft != null
              ? lang === "zh"
                ? `${autoLeft} 秒后自动进入 · 任意操作取消`
                : `AUTO-ENTERING IN ${autoLeft}S · INTERACT TO CANCEL`
              : c.hint}
          </div>
        </div>
      </div>

      <div className="bl-scroll-hint" aria-hidden>
        SCROLL / CLICK TO ENTER
      </div>
    </div>
  );
}
