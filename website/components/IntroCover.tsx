"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { useLang } from "@/components/LanguageContext";
import { BRAND, PRODUCT_ORDER } from "@/lib/brand";
import { PRODUCT_IMG } from "@/components/productMeta";

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
  };
}

/* ================= 组件 ================= */

export default function IntroCover() {
  const { lang } = useLang();
  const c = COPY[lang];

  const [show, setShow] = useState<boolean>(() => !dismissedInRuntime);
  const [phase, setPhase] = useState<"idle" | "warp" | "leave">("idle");
  const [soundOn, setSoundOn] = useState(false);

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
  // 随机产品 LOGO 粒子（客户端生成，避免 SSR/hydration 不一致）
  const [particles, setParticles] = useState<
    Array<{ src: string; tx: number; ty: number; r: string; sz: string; d: string; dur: string }>
  >([]);

  const startSound = useCallback(() => {
    userMutedRef.current = false;
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

  const enter = useCallback(() => {
    if (enteredRef.current) return;
    enteredRef.current = true;
    dismissedInRuntime = true;
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
    preloadTheme(); // 开场展示期间就预取配乐，点击开声时即刻可播
    document.body.style.overflow = "hidden";

    const gestureOnce = () => {
      if (!enteredRef.current) startSound();
      document.removeEventListener("pointerdown", gestureOnce);
      document.removeEventListener("keydown", gestureOnce);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Escape") enter();
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

  /* 生成随机产品 LOGO 粒子：随机图标 / 方向 / 大小 / 飞行距离 / 时长 / 延迟。
   *  抑制向上分量（ty<0 收窄）以免飘到顶部遮挡公司主 LOGO。 */
  useEffect(() => {
    if (!show) return;
    const imgs = PRODUCT_ORDER.map((k) => PRODUCT_IMG[k]);
    const N = 22;
    const list = Array.from({ length: N }, () => {
      const a = Math.random() * Math.PI * 2;
      let ty = Math.sin(a);
      if (ty < 0) ty *= 0.32; // 向上飞行大幅收窄，保护顶部主 LOGO
      return {
        src: imgs[Math.floor(Math.random() * imgs.length)],
        tx: +Math.cos(a).toFixed(3),
        ty: +ty.toFixed(3),
        r: `${(26 + Math.random() * 48).toFixed(0)}vmin`,
        sz: `${(30 + Math.random() * 60).toFixed(0)}px`,
        d: `${(Math.random() * 7).toFixed(2)}s`,
        dur: `${(5 + Math.random() * 4).toFixed(2)}s`,
      };
    });
    setParticles(list);
  }, [show]);

  /* 把产品 LOGO 喷涌原点对齐到「进入 AI 世界」按钮中心（星门口） */
  useEffect(() => {
    if (!show) return;
    const measure = () => {
      const b = enterBtnRef.current?.getBoundingClientRect();
      const o = overlayRef.current?.getBoundingClientRect();
      if (b && o && b.height > 0) setGateY(b.top - o.top + b.height / 2);
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
        if (e.deltaY > 12) enter();
      }}
      onTouchStart={(e) => {
        touchYRef.current = e.touches[0].clientY;
      }}
      onTouchMove={(e) => {
        if (touchYRef.current !== null && touchYRef.current - e.touches[0].clientY > 26) enter();
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

      {/* 产品 LOGO 从星门（进入按钮中心）由小到大、循环不断喷涌飞出 */}
      <div
        className={`bl-gate-logos${phase !== "idle" ? " launch" : ""}`}
        style={gateY != null ? ({ top: `${gateY}px` } as CSSProperties) : undefined}
        aria-hidden
      >
        {particles.map((p, i) => (
          <span
            key={i}
            className="bl-gate-logo"
            style={{ "--tx": p.tx, "--ty": p.ty, "--r": p.r, "--sz": p.sz, "--d": p.d, "--dur": p.dur } as CSSProperties}
          >
            <img src={p.src} alt="" draggable={false} />
          </span>
        ))}
      </div>

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
          {/* 按钮特效层:旋转描边光束 / 掠光 / 能量扩散环;文字逐字全息浮现 + 流光波 */}
          <button ref={enterBtnRef} type="button" className="bl-enter-btn" onClick={enter}>
            <span className="beam" aria-hidden />
            <span className="sheen" aria-hidden />
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
          <div className="bl-enter-hint">{c.hint}</div>
        </div>
      </div>

      <div className="bl-scroll-hint" aria-hidden>
        SCROLL / CLICK TO ENTER
      </div>
    </div>
  );
}
