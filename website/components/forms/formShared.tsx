"use client";

import { AnimatePresence, motion, type MotionValue, type Variants } from "framer-motion";
import { Activity } from "lucide-react";

/**
 * 三形态（EveBot / DemonForm / LoongForm）共用的类型与原子组件。
 * 从 AISprite 拆出，供 forms/* 独立形态文件复用，避免循环依赖。
 */

export type BotMode = "flying" | "falling" | "idle_base" | "idle_wave" | "idle_dance" | "idle_scan" | "idle_news" | "idle_spin";

export type EyeExpr = "normal" | "happy" | "blink" | "focused" | "scanning" | "scared" | "wink";

export type DemonProps = {
  mode: BotMode;
  isHovered: boolean;
  newsText: string;
  newsCta: string;
  scrollTilt: MotionValue<number>;
  flightRotate: MotionValue<number>;
  gazeX: MotionValue<number>;
  gazeY: MotionValue<number>;
  squashY: MotionValue<number>;
  shadowOpacity: MotionValue<number>;
  onNewsCta: () => void;
  reduced: boolean;
  lowFx?: boolean;
  /** 本体是否可见（false=已化蝠群飞行中）。false→true 边沿触发“落地展翼”仪式 */
  revealed?: boolean;
};

/** 头顶全息资讯面板：播报能力话术；整面板可点击，带当前版块的种子问题打开 AI 客服 */
export const NewsHologram = ({ active, text, cta, color, onCta }: { active: boolean; text: string; cta: string; color: string; onCta?: () => void }) => (
  <AnimatePresence>
    {active && (
      <motion.div
        className="absolute bottom-[105%] left-1/2 w-48 z-50 pointer-events-none"
        /* 水平定位交给 framer 的 x：类名 translate 会被 framer 写 transform 时清掉（存量bug），
           -58% 让面板略向页面内容侧偏，远离视口右缘 */
        style={{ x: "-58%" }}
        initial={{ opacity: 0, scale: 0.8, y: 10, rotateX: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0, rotateX: 0 }}
        exit={{ opacity: 0, scale: 0.8, y: 5 }}
      >
        <motion.div
          className="pointer-events-auto cursor-pointer bg-black/80 border backdrop-blur-md rounded-lg p-3 relative overflow-hidden"
          style={{ borderColor: `${color}60`, boxShadow: `0 0 15px ${color}20` }}
          whileHover={{ scale: 1.03 }}
          whileTap={{ scale: 0.97 }}
          onClick={(e) => {
            e.stopPropagation();
            onCta?.();
          }}
        >
          <div className="absolute inset-0 bg-[linear-gradient(rgba(0,0,0,0)_50%,rgba(0,0,0,0.2)_50%),linear-gradient(90deg,rgba(255,0,0,0.06),rgba(0,255,0,0.02),rgba(0,0,255,0.06))] bg-[length:100%_2px,3px_100%] pointer-events-none opacity-50" />
          <div className="flex items-center gap-2 mb-1 border-b border-white/10 pb-1">
            <Activity className="w-3 h-3 animate-pulse" style={{ color }} />
            <span className="text-[10px] font-mono font-bold tracking-wider text-zinc-300">BOUNDLESS_AI</span>
          </div>
          <div className="text-xs text-white font-sans leading-tight relative z-10">{text}</div>
          <div className="mt-1.5 border-t border-white/10 pt-1 text-[10px] font-medium relative z-10" style={{ color }}>{cta}</div>
        </motion.div>
        <div className="absolute top-full left-1/2 -translate-x-1/2 w-8 h-8 opacity-50 blur-md" style={{ background: `conic-gradient(from 180deg at 50% 0%, transparent 45%, ${color} 50%, transparent 55%)` }} />
      </motion.div>
    )}
  </AnimatePresence>
);

/** 上升火花：恶魔态环绕机身的红色余烬粒子（低配/reduced 关闭） */
const EMBER_SEEDS = [
  { x: -22, delay: 0, dur: 2.6, size: 3 },
  { x: -8, delay: 0.8, dur: 3.1, size: 2 },
  { x: 10, delay: 1.5, dur: 2.4, size: 2.5 },
  { x: 24, delay: 0.4, dur: 2.9, size: 2 },
  { x: 2, delay: 2.0, dur: 3.3, size: 3 },
];
export const DemonEmbers = ({ color }: { color: string }) => (
  <div className="pointer-events-none absolute left-1/2 top-1/2 z-0 -translate-x-1/2">
    {EMBER_SEEDS.map((e, i) => (
      <motion.span
        key={i}
        className="absolute block rounded-full"
        style={{ left: e.x, width: e.size, height: e.size, background: color, boxShadow: `0 0 6px ${color}` }}
        initial={{ y: 20, opacity: 0 }}
        animate={{ y: [-6, -54], opacity: [0, 0.9, 0], scale: [1, 0.4] }}
        transition={{ duration: e.dur, delay: e.delay, repeat: Infinity, ease: "easeOut" }}
      />
    ))}
  </div>
);

/** 身体姿态表：飞行倾角/俯仰改由外层 MotionValue 直驱，此处不再承担 */
export const buildBodyVariants = (reduced: boolean): Variants => ({
  idle_base: reduced
    ? { y: -4, rotate: 0 }
    : { y: [0, -8, 0], rotate: 0, transition: { y: { repeat: Infinity, duration: 2.5, ease: "easeInOut" } } },
  idle_scan: { y: -5, rotate: [0, -5, 5, 0], transition: { rotate: { duration: 2, ease: "easeInOut" } } },
  idle_wave: { y: -4, rotate: -3 },
  idle_dance: { y: [0, -15, 0], rotate: [-3, 3, -3], transition: { y: { repeat: Infinity, duration: 0.4, ease: "easeOut" }, rotate: { repeat: Infinity, duration: 0.8, ease: "linear" } } },
  idle_news: { y: 0, rotate: 0 },
  flying: { rotate: 0, y: -20 },
  falling: { rotate: -15, y: 30 },
});
