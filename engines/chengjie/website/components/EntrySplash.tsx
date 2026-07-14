"use client";

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { ArrowRight, Volume2, VolumeX } from "lucide-react";
import Image from "next/image";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";

// 星空「进入 AI 世界」入场页：整屏宇宙背景 + 品牌主标 + 背景音乐。
// 只在每个浏览器会话首次访问首页时出现（sessionStorage 记忆）；点击「进入」时
// 借用户手势启动 BGM（规避浏览器自动播放拦截），随后整屏淡出并卸载。
const SESSION_KEY = "bl-entered";
const BGM_SRC = "/audio/entrance-bgm.mp3";

export default function EntrySplash() {
  const { lang } = useLang();
  const reduced = useReducedMotion();
  const [show, setShow] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  // 首帧不渲染，挂载后再判断，避免 SSR 与水合不一致导致闪烁。
  useEffect(() => {
    try {
      if (sessionStorage.getItem(SESSION_KEY) === "1") return;
    } catch {}
    setShow(true);
  }, []);

  // 星空粒子动画（缓慢漂移 + 闪烁）。prefers-reduced-motion 时退化为静态点。
  useEffect(() => {
    if (!show) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    type Star = { x: number; y: number; z: number; r: number; tw: number };
    let stars: Star[] = [];

    const resize = () => {
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.min(220, Math.floor((w * h) / 7000));
      stars = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        z: Math.random() * 0.8 + 0.2,
        r: Math.random() * 1.6 + 0.4,
        tw: Math.random() * Math.PI * 2,
      }));
    };
    resize();
    window.addEventListener("resize", resize);

    const render = () => {
      ctx.clearRect(0, 0, w, h);
      // 深空渐变底
      const g = ctx.createRadialGradient(w / 2, h * 0.42, 0, w / 2, h * 0.42, Math.max(w, h) * 0.75);
      g.addColorStop(0, "rgba(20,26,54,0.55)");
      g.addColorStop(0.5, "rgba(8,10,26,0.35)");
      g.addColorStop(1, "rgba(2,3,10,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      for (const s of stars) {
        s.tw += 0.02 * s.z;
        const alpha = 0.35 + Math.sin(s.tw) * 0.35 + 0.3;
        s.y += s.z * 0.15; // 缓慢下沉漂移
        if (s.y > h) {
          s.y = 0;
          s.x = Math.random() * w;
        }
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${180 + Math.floor(s.z * 60)}, ${220}, 255, ${alpha})`;
        ctx.shadowBlur = 6 * s.z;
        ctx.shadowColor = "rgba(120,200,255,0.8)";
        ctx.fill();
      }
      ctx.shadowBlur = 0;
      raf = requestAnimationFrame(render);
    };

    if (reduced) {
      // 静态：画一次
      ctx.clearRect(0, 0, w, h);
      for (const s of stars) {
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(200,224,255,0.7)`;
        ctx.fill();
      }
    } else {
      raf = requestAnimationFrame(render);
    }

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [show, reduced]);

  const dismiss = (withSound: boolean) => {
    try {
      sessionStorage.setItem(SESSION_KEY, "1");
    } catch {}
    if (withSound && audioRef.current) {
      audioRef.current.volume = 0.45;
      audioRef.current.loop = true;
      void audioRef.current.play().catch(() => {});
    }
    track("entry_splash", { action: withSound ? "enter_sound" : "enter_muted" });
    setShow(false);
  };

  const t =
    lang === "zh"
      ? {
          tagline: "让沟通，无界",
          sub: "AI 自动成交 · 多语种拟人翻译 · 数字分身",
          enter: "进入 AI 世界",
          muted: "静音进入",
          hint: "点击进入即开启背景音乐",
        }
      : {
          tagline: "Communication, Boundless.",
          sub: "AI auto-closing · human-like translation · digital avatars",
          enter: "Enter the AI World",
          muted: "Enter muted",
          hint: "Entering will turn on background music",
        };

  return (
    <AnimatePresence>
      {show && (
        <motion.div
          key="entry-splash"
          initial={{ opacity: 1 }}
          exit={{ opacity: 0, scale: 1.04, filter: "blur(6px)" }}
          transition={{ duration: 0.8, ease: [0.22, 1, 0.36, 1] }}
          className="fixed inset-0 z-[200] flex items-center justify-center overflow-hidden bg-[#02030a]"
        >
          <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
          {/* 光晕 */}
          <div className="pointer-events-none absolute left-1/2 top-[38%] h-[40vmin] w-[40vmin] -translate-x-1/2 -translate-y-1/2 rounded-full bg-neon-cyan/15 blur-[120px]" />
          <div className="pointer-events-none absolute bottom-0 left-0 h-40 w-full bg-gradient-to-t from-[#02030a] to-transparent" />

          <div className="relative z-10 flex flex-col items-center px-6 text-center">
            <motion.div
              initial={{ opacity: 0, y: 20, scale: 0.9 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1] }}
              className="relative"
            >
              <span className="pointer-events-none absolute inset-0 -z-10 m-auto h-40 w-40 rounded-full bg-neon-cyan/20 blur-3xl" />
              <Image
                src="/brand/logos/boundless-mark-256.png"
                alt="无界科技 BOUNDLESS"
                width={128}
                height={128}
                priority
                className={`h-24 w-24 object-contain md:h-32 md:w-32 ${reduced ? "" : "animate-float"}`}
                draggable={false}
              />
            </motion.div>

            <motion.h1
              initial={{ opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.15, duration: 0.8 }}
              className="mt-6 text-4xl font-black tracking-tight text-white md:text-6xl"
            >
              无界科技
              <span className="text-gradient ml-3">BOUNDLESS</span>
            </motion.h1>

            <motion.p
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.28, duration: 0.8 }}
              className="mt-3 text-sm tracking-[0.4em] text-slate-300 md:text-base"
            >
              {t.tagline}
            </motion.p>
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.4, duration: 0.8 }}
              className="mt-2 text-xs text-slate-500 md:text-sm"
            >
              {t.sub}
            </motion.p>

            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.5, duration: 0.8 }}
              className="mt-10 flex flex-col items-center gap-3 sm:flex-row"
            >
              <button
                onClick={() => dismiss(true)}
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-8 py-3.5 text-base font-semibold text-ink-950 shadow-[0_0_40px_rgba(34,211,238,0.35)] transition hover:opacity-90"
              >
                <Volume2 className="h-4 w-4" />
                {t.enter}
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </button>
              <button
                onClick={() => dismiss(false)}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3.5 text-sm font-medium text-slate-300 transition hover:border-white/30 hover:text-white"
              >
                <VolumeX className="h-4 w-4" />
                {t.muted}
              </button>
            </motion.div>

            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.7, duration: 0.8 }}
              className="mt-4 text-[11px] text-slate-600"
            >
              {t.hint}
            </motion.p>
          </div>

          <audio ref={audioRef} src={BGM_SRC} preload="auto" />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
