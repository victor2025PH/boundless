"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  motion,
  animate,
  useSpring,
  useVelocity,
  useAnimationFrame,
  useTransform,
  useScroll,
  useMotionTemplate,
  useMotionValue,
  useReducedMotion,
  AnimatePresence,
  type Variants,
  type MotionValue,
} from "framer-motion";
import { Activity } from "lucide-react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";

/**
 * 无界科技 · 会飞的 AI 机器人（EveBot）。
 * 桌面端：漂浮/飞行（带俯仰与着陆回弹）/表情/资讯全息/眼神跟随；悬停 → 低位五指挥手问好。
 * 移动端：轻量版（缩放 55%，仅待机动画与播报，无飞行/避让）。
 * 点击机器人 → 派发 `bl:open-chat` 打开 AI 客服；点击全息播报 → 带该版块种子问题开客服。
 * 碰撞避让零逐帧 DOM 读取。调试：URL 加 ?robot=idle_news 等可锁定姿态。
 */

/** 机器人容器尺寸（px），与 w-32 h-44 保持一致 */
const BOT_W = 128;
const BOT_H = 176;

/**
 * 休息位（相对视口右下角，px）。bottom=160 经矩形推算：
 * md 断点客服按钮避让区（含 18px padding）上缘在视口底部 154px 处，
 * 机器人下缘 160px > 154px，静止时避让弹簧不再持续发力（原 bottom-24 恒被推挤）。
 */
const HOME = { right: 24, bottom: 160 };

/**
 * 身体解剖常量（容器坐标系）。
 * 肩位 = 蛋形身体上缘（≈65px）下方 5~13px，手臂自然垂在身体两侧，
 * 修复原先臂根挂在头部高度形成的“兔耳”观感。
 */
const ANATOMY = {
  /** 臂根挂载点距容器顶部 */
  armTop: 70,
  /** 臂根距容器左/右内边距 */
  armInset: 19,
  /** 左臂肩关节旋转原点（臂 SVG 内坐标） */
  shoulderLeft: "16px 8px",
  /** 右臂肩关节旋转原点（镜像后臂 SVG 内坐标） */
  shoulderRight: "2px 8px",
} as const;

/** 头颈之间的能量光束 */
const NeuralNeck = ({ color }: { color: string }) => (
  <motion.div
    className="absolute left-1/2 top-[25%] -translate-x-1/2 w-3 h-10 z-10 overflow-hidden pointer-events-none"
    animate={{ opacity: 1, height: 28 }}
    transition={{ duration: 0.4 }}
  >
    <div className="w-full h-full flex flex-col items-center justify-center">
      <div className="w-[1px] h-full transition-colors duration-1000" style={{ backgroundColor: `${color}4D`, boxShadow: `0 0 5px ${color}` }} />
    </div>
  </motion.div>
);

/** 头顶全息资讯面板：播报能力话术；整面板可点击，带当前版块的种子问题打开 AI 客服 */
const NewsHologram = ({ active, text, cta, color, onCta }: { active: boolean; text: string; cta: string; color: string; onCta?: () => void }) => (
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

/** 花瓣形手臂。渐变 id 按侧唯一，避免两臂渲染重复 SVG id */
const EveArm = ({ side }: { side: "left" | "right" }) => {
  const gid = `eve-arm-grad-${side}`;
  return (
    <svg width="18" height="64" viewBox="0 0 18 64" fill="none" className="drop-shadow-sm" style={{ transform: side === "right" ? "scaleX(-1)" : undefined }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="40%" stopColor="#eef2ff" />
          <stop offset="100%" stopColor="#dbeafe" />
        </linearGradient>
      </defs>
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62C17 62.5 18 60 18 56C18 56 18 10 16 2Z" fill={`url(#${gid})`} />
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62" stroke="white" strokeWidth="0.5" strokeOpacity="0.8" fill="none" />
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62C17 62.5 18 60 18 56C18 56 18 10 16 2Z" stroke="rgba(0,0,0,0.05)" strokeWidth="0.5" />
    </svg>
  );
};

/** 五根手指的布局参数（挥手时从掌心逐根展开）：x 定位 / 高度 / 张开角 / 宽度 */
const FINGERS = [
  { x: 2.5, h: 8, r: -30, w: 3 },
  { x: 6, h: 10.5, r: -14, w: 3 },
  { x: 9.5, h: 11.5, r: 0, w: 3 },
  { x: 13.2, h: 10, r: 14, w: 3 },
  { x: 16.8, h: 7.5, r: 32, w: 3.6 },
] as const;

/** 单根手指的展开动画：随 custom 索引错峰弹出，营造“机械展指”质感 */
const fingerVariants: Variants = {
  hidden: { scaleY: 0.12, opacity: 0 },
  shown: (i: number) => ({
    scaleY: 1,
    opacity: 1,
    transition: { delay: 0.2 + i * 0.05, type: "spring", stiffness: 430, damping: 21 },
  }),
};

/** 掌心出现动画 */
const palmVariants: Variants = {
  hidden: { scale: 0.35, opacity: 0 },
  shown: { scale: 1, opacity: 1, transition: { delay: 0.08, type: "spring", stiffness: 320, damping: 20 } },
};

/** 五指手掌（挥手时出现在左臂末端），材质与臂/身体同源的白→冰蓝 */
const EveHand = () => (
  <div className="relative h-[26px] w-6 drop-shadow-sm">
    {FINGERS.map((f, i) => (
      <motion.div
        key={i}
        custom={i}
        variants={fingerVariants}
        className="absolute rounded-full"
        style={{
          left: f.x,
          bottom: 9,
          width: f.w,
          height: f.h,
          transformOrigin: "50% 100%",
          rotate: f.r,
          background: "linear-gradient(to top, #e0f2fe, #ffffff)",
          boxShadow: "0 0 2px rgba(0,0,0,0.06)",
        }}
      />
    ))}
    <motion.div
      variants={palmVariants}
      /* 不用 -translate-x-1/2 居中：framer 动画 scale 时会覆盖 transform，改用显式 left */
      className="absolute bottom-0 h-[13px] w-[15px]"
      style={{
        left: 4.5,
        borderRadius: "48% 48% 46% 46%",
        background: "radial-gradient(circle at 38% 28%, #ffffff 0%, #e0f2fe 62%, #cffafe 100%)",
        boxShadow: "inset -1px -1.5px 3px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.08)",
      }}
    />
  </div>
);

/**
 * 腕部动画：招手时手掌以腕关节为轴左右摆动（±20° 左右），
 * 大臂只负责一次性抬起，双关节运动比原先整臂 -140° 甩动自然得多。
 * 手掌基础角 -76° 抵消大臂 +76° 抬起，保证五指全球朝上。
 */
const handWrapperVariants = (reduced: boolean): Variants => ({
  hidden: { opacity: 0, scale: 0.3, rotate: -76, transition: { duration: 0.22 } },
  shown: {
    opacity: 1,
    scale: 1,
    rotate: reduced ? -76 : [-76, -56, -90, -58, -86, -68, -76],
    transition: {
      opacity: { duration: 0.15 },
      scale: { type: "spring", stiffness: 300, damping: 18 },
      rotate: reduced
        ? { duration: 0.3 }
        : { delay: 0.5, duration: 1.6, repeat: Infinity, repeatDelay: 0.35, ease: "easeInOut" },
    },
  },
});

type EyeExpr = "normal" | "happy" | "blink" | "focused" | "scanning" | "scared" | "wink";

/** 数码眼：横纹发光屏，支持多种表情形变 */
const DigitalEye = ({ expression, color }: { expression: EyeExpr; color: string }) => {
  const variants: Record<string, { scaleY: number; scaleX: number; borderRadius: string; height: string }> = {
    normal: { scaleY: 1, scaleX: 1, borderRadius: "50%", height: "16px" },
    blink: { scaleY: 0.1, scaleX: 1.1, borderRadius: "50%", height: "16px" },
    happy: { scaleY: 0.6, scaleX: 1.1, borderRadius: "50% 50% 20% 20%", height: "16px" },
    focused: { scaleY: 0.7, scaleX: 0.9, borderRadius: "30%", height: "14px" },
    scanning: { scaleY: 1.1, scaleX: 0.8, borderRadius: "50%", height: "18px" },
    scared: { scaleY: 1.3, scaleX: 0.7, borderRadius: "40%", height: "20px" },
    wink: { scaleY: 0.1, scaleX: 1.1, borderRadius: "50%", height: "16px" },
  };
  return (
    <motion.div
      className="relative w-6 bg-[#001020] overflow-hidden transition-colors duration-1000"
      style={{ boxShadow: `0 0 5px ${color}80`, border: `1px solid ${color}40` }}
      animate={variants[expression === "wink" ? "blink" : expression]}
      transition={{ type: "spring", stiffness: 300, damping: 20 }}
    >
      <motion.div className="absolute inset-0 bg-black" initial={{ opacity: 0 }} animate={{ opacity: expression === "happy" ? 1 : 0 }} style={{ clipPath: "polygon(0% 50%, 100% 50%, 100% 100%, 0% 100%)" }} />
      {expression === "scanning" && <motion.div className="absolute inset-0 bg-white/50 h-[2px]" animate={{ top: ["0%", "100%", "0%"] }} transition={{ duration: 1, repeat: Infinity, ease: "linear" }} />}
      <div className="absolute inset-0 flex flex-col justify-center gap-[1px] opacity-90">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="w-full h-[2px] transition-colors duration-1000" style={{ backgroundColor: color, boxShadow: `0 0 2px ${color}`, opacity: 1 - Math.abs(2 - i) * 0.25 }} />
        ))}
      </div>
      <div className="absolute inset-0 blur-sm transition-colors duration-1000" style={{ backgroundColor: `${color}40` }} />
    </motion.div>
  );
};

export type BotMode = "flying" | "falling" | "idle_base" | "idle_wave" | "idle_dance" | "idle_scan" | "idle_news" | "idle_spin";

/**
 * 左臂姿态表（角度符号：正 = 顺时针 = 左臂向外张开）。
 * 挥手仅抬到 +76°（指尖约在胸口高度），满足“招手时手臂也要低”。
 */
const leftArmVariants: Variants = {
  idle_base: { x: 0, y: 0, rotate: 7 },
  idle_scan: { x: 0, y: 0, rotate: 7 },
  idle_wave: { x: -2, y: -2, rotate: 76, transition: { type: "spring", stiffness: 170, damping: 15 } },
  idle_dance: { x: -7, y: -4, rotate: [6, 44, 6], transition: { rotate: { repeat: Infinity, duration: 0.4 } } },
  idle_news: { x: -1, y: 0, rotate: 12 },
  idle_spin: { x: 0, y: 0, rotate: 7 },
  flying: { x: -3, y: 6, rotate: 42 },
  falling: { x: -12, y: -13, rotate: 124 },
};

/** 右臂姿态表（镜像）：挥手时轻微外张作配重，重心不歪 */
const rightArmVariants: Variants = {
  idle_base: { x: 0, y: 0, rotate: -7 },
  idle_scan: { x: 0, y: 0, rotate: -7 },
  idle_wave: { x: 2, y: 1, rotate: -13 },
  idle_dance: { x: 7, y: -4, rotate: [-6, -44, -6], transition: { rotate: { repeat: Infinity, duration: 0.4, delay: 0.2 } } },
  idle_news: { x: 1, y: 0, rotate: -12 },
  idle_spin: { x: 0, y: 0, rotate: -7 },
  flying: { x: 3, y: 6, rotate: -42 },
  falling: { x: 12, y: -13, rotate: -124 },
};

/** 呼吸微摆生效的姿态：待机类动作时手臂随身体轻晃 ±1.6°，静态也是“活”的 */
const SWAY_MODES = new Set<BotMode>(["idle_base", "idle_scan", "idle_news", "idle_spin"]);

/** 手臂呼吸微摆（嵌套节点实现，避免与姿态弹簧在同一元素上打架） */
const armSwayVariants = (side: "left" | "right"): Variants => ({
  sway: {
    rotate: side === "left" ? [1.6, -1.6, 1.6] : [-1.6, 1.6, -1.6],
    transition: { repeat: Infinity, duration: 2.5, ease: "easeInOut" },
  },
  still: { rotate: 0, transition: { duration: 0.35 } },
});

/** 身体姿态表：飞行倾角/俯仰改由外层 MotionValue 直驱，此处不再承担 */
const buildBodyVariants = (reduced: boolean): Variants => ({
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

type EveBotProps = {
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
};

/** 机器人本体：头 / 颈 / 蛋形身体 / 双臂（左臂带五指手掌）/ 推进器光焰 / 悬浮光池。
 *  已导出：/robot-stage 素材舞台页复用同一实现，保证站内外 IP 形象一致。 */
export const EveBot: React.FC<EveBotProps> = ({ mode, isHovered, newsText, newsCta, scrollTilt, flightRotate, gazeX, gazeY, squashY, shadowOpacity, onNewsCta, reduced }) => {
  const [eyeExpression, setEyeExpression] = useState<EyeExpr>("normal");
  const [eyeColor, setEyeColor] = useState("#22d3ee");
  const [spinRotation, setSpinRotation] = useState(0);
  const [wink, setWink] = useState(false);
  const waving = mode === "idle_wave";
  /* 着陆回弹的挤压-拉伸：Y 压缩时 X 反向微胖，卡通物理更可信 */
  const squashX = useTransform(squashY, (v) => 1 + (1 - v) * 0.55);
  const swayOn = !reduced && SWAY_MODES.has(mode);

  /* 眼睛霓虹色轮换（页面隐藏时暂停） */
  useEffect(() => {
    const colors = ["#22d3ee", "#8b5cf6", "#06b6d4", "#a855f7", "#38bdf8"];
    let i = 0;
    const t = setInterval(() => {
      if (document.hidden) return;
      i = (i + 1) % colors.length;
      setEyeColor(colors[i]);
    }, 4000);
    return () => clearInterval(t);
  }, []);

  /* 表情状态机 + 待机随机眨眼 */
  useEffect(() => {
    if (isHovered) return setEyeExpression("happy");
    if (mode === "falling") return setEyeExpression("scared");
    if (mode === "flying") return setEyeExpression("focused");
    if (mode === "idle_scan") return setEyeExpression("scanning");
    if (mode === "idle_wave" || mode === "idle_dance") return setEyeExpression("happy");
    if (mode === "idle_news") return setEyeExpression("focused");
    let alive = true;
    const blinkLoop = () => {
      if (!alive) return;
      if (mode === "idle_base" && !isHovered) {
        setEyeExpression("blink");
        setTimeout(() => setEyeExpression("normal"), 150);
      }
      setTimeout(blinkLoop, 2000 + Math.random() * 3000);
    };
    const timer = setTimeout(blinkLoop, 2000);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [mode, isHovered]);

  /* 挥手时眨一次单眼（启用原先闲置的 wink 表情） */
  useEffect(() => {
    if (!waving || reduced) return;
    const t1 = setTimeout(() => setWink(true), 750);
    const t2 = setTimeout(() => setWink(false), 950);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      setWink(false);
    };
  }, [waving, reduced]);

  useEffect(() => {
    if (mode === "idle_spin") setSpinRotation((p) => p + 360);
  }, [mode]);

  return (
    <motion.div
      className="relative w-32 h-44 flex flex-col items-center justify-center [transform-style:preserve-3d]"
      style={{ rotateX: scrollTilt, rotate: flightRotate }}
      animate={{ rotateY: spinRotation }}
      transition={{ rotateY: { duration: 1, ease: "backOut" } }}
    >
      {/* 着陆回弹层：只承担挤压-拉伸，与姿态变体解耦 */}
      <motion.div className="relative w-full h-full [transform-style:preserve-3d]" style={{ scaleY: squashY, scaleX: squashX, transformOrigin: "50% 86%" }}>
        <motion.div
          className="relative w-full h-full flex flex-col items-center justify-center [transform-style:preserve-3d]"
          variants={buildBodyVariants(reduced)}
          animate={mode === "idle_spin" ? undefined : mode}
          transition={{ type: "spring", stiffness: 100, damping: 20 }}
        >
          <NewsHologram active={mode === "idle_news"} text={newsText} cta={newsCta} color={eyeColor} onCta={onNewsCta} />
          <motion.div
            className="relative z-30 w-[4.4rem] h-[3.2rem] bg-white rounded-[50%_50%_45%_45%] shadow-[inset_0_-2px_6px_rgba(0,0,0,0.15),0_5px_15px_rgba(0,0,0,0.1)] overflow-hidden flex items-center justify-center"
            style={{ background: "radial-gradient(circle at 50% 10%, #ffffff 0%, #ecfeff 60%, #cffafe 100%)" }}
            animate={{ y: mode === "idle_dance" ? -2 : -8 }}
          >
            <div className="absolute top-1 left-1/4 w-1/2 h-1/2 bg-white opacity-60 rounded-full blur-[2px]" />
            <div className="w-[88%] h-[75%] bg-black rounded-[45%_45%_50%_50%] flex items-center justify-center gap-3 relative shadow-[inset_0_0_10px_rgba(255,255,255,0.15)] overflow-hidden border border-zinc-800/50 mt-1">
              {/* 眼神跟随：整对眼睛朝鼠标方向微移（MotionValue 直驱，零重渲染） */}
              <motion.div className="flex gap-3" style={{ x: gazeX, y: gazeY }}>
                <DigitalEye expression={wink ? "wink" : eyeExpression} color={eyeColor} />
                <DigitalEye expression={eyeExpression} color={eyeColor} />
              </motion.div>
            </div>
          </motion.div>
          <NeuralNeck color={eyeColor} />
          <div className="relative z-20 w-[4rem] h-[5.5rem] mt-[-10px]">
            <div className="w-full h-full bg-white relative overflow-hidden" style={{ background: "radial-gradient(circle at 30% 50%, #ffffff 0%, #ecfeff 50%, #cffafe 100%)", borderRadius: "30% 30% 50% 50% / 20% 20% 80% 80%", boxShadow: "inset -5px -5px 15px rgba(0,0,0,0.05), inset 5px 5px 15px rgba(255,255,255,1), 0 10px 25px rgba(0,0,0,0.1)" }}>
              <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[90%] h-5 bg-gradient-to-b from-[#a5f3fc] to-transparent rounded-b-full opacity-40 blur-[1px]" />
              <div className="absolute top-[45%] left-1/2 -translate-x-1/2 w-8 h-8 flex items-center justify-center opacity-100">
                <div className="w-2 h-2 rounded-full animate-pulse transition-colors duration-1000" style={{ backgroundColor: eyeColor, boxShadow: `0 0 10px ${eyeColor}` }} />
                <div className="absolute w-full h-[1px] bg-black/5 top-1/2 -translate-y-1/2" />
                <div className="absolute h-full w-[1px] bg-black/5 left-1/2 -translate-x-1/2" />
              </div>
            </div>
          </div>
          {/* 左臂（屏幕左侧 = 机器人右手）：朝页面内容方向挥手，不会被视口右缘裁切 */}
          <motion.div
            className="absolute z-10"
            style={{ left: ANATOMY.armInset, top: ANATOMY.armTop, transformOrigin: ANATOMY.shoulderLeft }}
            variants={leftArmVariants}
            animate={mode}
          >
            <motion.div style={{ transformOrigin: ANATOMY.shoulderLeft }} variants={armSwayVariants("left")} animate={swayOn ? "sway" : "still"}>
              <EveArm side="left" />
              <motion.div
                className="eve-hand absolute"
                style={{ left: 4, top: 40, transformOrigin: "12px 22px" }}
                variants={handWrapperVariants(reduced)}
                initial="hidden"
                animate={waving ? "shown" : "hidden"}
              >
                <EveHand />
              </motion.div>
            </motion.div>
          </motion.div>
          {/* 右臂：挥手时仅轻微外张配重 */}
          <motion.div
            className="absolute z-10"
            style={{ right: ANATOMY.armInset, top: ANATOMY.armTop, transformOrigin: ANATOMY.shoulderRight }}
            variants={rightArmVariants}
            animate={mode}
          >
            <motion.div style={{ transformOrigin: ANATOMY.shoulderRight }} variants={armSwayVariants("right")} animate={swayOn ? "sway" : "still"}>
              <EveArm side="right" />
            </motion.div>
          </motion.div>
          {/* 推进器：用 framer 的 x 居中而非 translate 类（会被 transform 动画覆盖，存量bug） */}
          <motion.div className="absolute top-[88%] left-[49%] -z-10" style={{ x: "-50%" }} animate={{ opacity: mode === "flying" || mode === "idle_dance" ? 0.8 : 0.4, scaleY: mode === "flying" ? 1.5 : 0.8 }}>
            <div className="w-6 h-12 rounded-full blur-[6px] transition-colors duration-1000" style={{ background: `linear-gradient(to top, transparent, ${eyeColor}, white)` }} />
          </motion.div>
        </motion.div>
      </motion.div>
      {/* 悬浮光池：机身辉光在“地面”的反射（深色页面上用光而非阴影表达高度），
          呼吸节奏与身体 2.5s 浮动同步，飞行/拖拽越远越暗 */}
      <motion.div className="pointer-events-none absolute left-1/2 top-[97%] -z-20 -translate-x-1/2" style={{ opacity: shadowOpacity }}>
        <motion.div
          className="h-3 w-16 rounded-[50%] blur-[6px] transition-colors duration-1000"
          style={{ background: `radial-gradient(ellipse at center, ${eyeColor}55 0%, ${eyeColor}18 55%, transparent 75%)` }}
          animate={reduced ? { scaleX: 1, opacity: 0.8 } : { scaleX: [1, 0.82, 1], opacity: [0.9, 0.55, 0.9] }}
          transition={reduced ? undefined : { repeat: Infinity, duration: 2.5, ease: "easeInOut" }}
        />
      </motion.div>
    </motion.div>
  );
};

/** 通用资讯池（未识别到特定版块时使用） */
const NEWS = {
  zh: ["扫描出海获客机会…", "AI 拟人翻译已就绪…", "多号矩阵 7×24 运转中…", "监测实时换脸链路…", "分析客户成交意向…", "同步 6 大产品能力…", "私有部署 · 数据不出网…", "自动跟单催单进行中…"],
  en: ["Scanning lead-gen ops…", "Human-like translation ready…", "Multi-account matrix 24/7…", "Monitoring live face-swap…", "Analyzing buyer intent…", "Syncing 6 product lines…", "Private deploy · off-net…", "Auto follow-up running…"],
};

/** 场景化资讯池：随访客正在浏览的版块切换话术（IntersectionObserver 感知） */
const SECTION_NEWS: Record<"zh" | "en", Record<string, string[]>> = {
  zh: {
    autochat: ["AI 正在自动接待询盘…", "拟人回复 · 客户无感知…", "自动成交流程演示中…"],
    products: ["6 大引擎能力已就绪…", "翻译 · 换脸 · 矩阵一站集成…", "挑一个引擎试试？"],
    pricing: ["按需订阅 · 支持私有化…", "算一算你的获客 ROI…", "方案可按业务定制…"],
    cases: ["实测数据 · 转化提升显著…", "看看同行的用法…"],
    proof: ["真实交付截图在此…", "数据不注水 · 可复核…"],
    contact: ["留下需求 · 1 对 1 方案…", "工程师在线 · 随时可聊…"],
  },
  en: {
    autochat: ["AI answering inquiries live…", "Human-like replies, seamless…", "Auto-closing demo running…"],
    products: ["6 engines ready to deploy…", "Translate · Swap · Matrix in one…", "Pick an engine to try?"],
    pricing: ["Subscribe or self-host…", "Estimate your lead-gen ROI…", "Plans tailored to your ops…"],
    cases: ["Field-tested conversion lift…", "See how peers use it…"],
    proof: ["Real delivery screenshots…", "Verifiable numbers only…"],
    contact: ["Leave a brief, get a plan…", "Engineers online now…"],
  },
};

/** 点击全息播报时带进客服的种子问题：把“被动曝光”直接变成对话线索 */
const SECTION_SEED: Record<"zh" | "en", Record<string, string>> = {
  zh: {
    top: "介绍一下你们的核心能力和适合我的方案",
    autochat: "AI 自动成交聊天怎么部署？怎么收费？",
    products: "帮我介绍下你们 6 大产品能力分别解决什么问题",
    pricing: "帮我算一下价格方案和获客 ROI",
    cases: "有哪些实测案例和转化数据？",
    proof: "交付数据和真实截图能详细讲讲吗？",
    contact: "我想要 1 对 1 定制方案，怎么对接？",
  },
  en: {
    top: "Give me an overview of your core capabilities and the right plan for me",
    autochat: "How do I deploy AI auto-closing chat, and what does it cost?",
    products: "Walk me through your 6 product lines and what each solves",
    pricing: "Help me estimate pricing and lead-gen ROI",
    cases: "What field-tested cases and conversion data do you have?",
    proof: "Can you detail your delivery data and real screenshots?",
    contact: "I want a tailored 1-on-1 plan — how do we start?",
  },
};

/** 挥手问候等一次性行为的会话级标记 */
const GREET_KEY = "bl-sprite-greeted";

/** 点击这些元素不触发机器人飞行（避免干扰正常交互） */
const FLY_IGNORE = "a,button,input,textarea,select,label,[role='button'],[data-robot-avoid='true'],.ai-sprite-container";

/** 调试用的可锁定姿态白名单（URL ?robot=idle_news 等），供视觉回归与联调 */
const DEBUG_MODES: BotMode[] = ["idle_wave", "idle_news", "idle_dance", "idle_scan", "idle_spin", "flying", "falling"];

export default function AISprite() {
  const { lang } = useLang();
  const reduced = useReducedMotion() ?? false;
  const { scrollY } = useScroll();
  const [mode, setMode] = useState<BotMode>("idle_base");
  const [newsText, setNewsText] = useState("");
  const [isHovered, setIsHovered] = useState(false);
  /* SSR 先按桌面渲染，挂载后由 matchMedia 校正；移动端走轻量行为分级 */
  const [isDesktop, setIsDesktop] = useState(true);
  const isHoveredRef = useRef(false);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    const apply = () => setIsDesktop(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  /* ---- 运动值：全部走 MotionValue 直驱，动画期间零 React 重渲染 ---- */
  const springScrollVelocity = useVelocity(scrollY);
  const clickX = useSpring(0, { stiffness: 60, damping: 15 });
  const clickY = useSpring(0, { stiffness: 60, damping: 15 });
  const flightVelX = useVelocity(clickX);
  const flightVelY = useVelocity(clickY);
  const avoidX = useSpring(0, { stiffness: 100, damping: 20 });
  const avoidY = useSpring(0, { stiffness: 100, damping: 20 });
  const rawDragY = useTransform(springScrollVelocity, [-3000, 3000], [-150, 150]);
  const smoothDragY = useSpring(rawDragY, { stiffness: 100, damping: 20 });
  const zeroMV = useMotionValue(0);
  /* 滚动俯仰 + 飞行垂直俯仰：叠加为总俯仰角（rotateX） */
  const tiltRaw = useTransform(springScrollVelocity, (v) => Math.max(Math.min(v * 0.05, 30), -30));
  const tiltSpring = useSpring(tiltRaw, { stiffness: 200, damping: 28 });
  const pitchRaw = useTransform(flightVelY, (v) => Math.max(Math.min(-v * 0.02, 14), -14));
  const pitchSpring = useSpring(pitchRaw, { stiffness: 140, damping: 18 });
  const totalTilt = useTransform([tiltSpring, pitchSpring], (vals) => (vals[0] as number) + (vals[1] as number));
  const flightRotateRaw = useTransform(flightVelX, (v) => Math.max(Math.min(v * 0.05, 30), -30));
  const flightRotate = useSpring(flightRotateRaw, { stiffness: 140, damping: 18 });
  const gazeX = useSpring(0, { stiffness: 120, damping: 16 });
  const gazeY = useSpring(0, { stiffness: 120, damping: 16 });
  /* 着陆回弹（scaleY），由 rAF 在飞行/坠落结束瞬间触发一次 */
  const squashY = useMotionValue(1);
  /* 悬浮光池亮度：离家越远（飞行/拖拽）越暗；desktopFlag 让移动端忽略拖拽项 */
  const desktopFlag = useMotionValue(1);
  useEffect(() => {
    desktopFlag.set(!reduced && isDesktop ? 1 : 0);
  }, [reduced, isDesktop, desktopFlag]);
  const shadowOpacity = useTransform([clickX, clickY, smoothDragY, desktopFlag], (vals) => {
    const [x, y, d, f] = vals as number[];
    const away = Math.hypot(x, y) * 0.9 + Math.abs(d * f);
    return Math.max(0.1, 0.55 - away / 320);
  });
  const dragTerm = reduced || !isDesktop ? zeroMV : smoothDragY;
  const combinedY = useMotionTemplate`calc(${clickY}px + ${dragTerm}px + ${avoidY}px)`;
  const combinedX = useMotionTemplate`calc(${clickX}px + ${avoidX}px)`;

  /* ---- 缓存：休息位坐标 + 避让区矩形，rAF 内零 DOM 读取 ---- */
  const homeRef = useRef({ left: 0, top: 0 });
  const avoidRectsRef = useRef<Array<{ l: number; t: number; r: number; b: number }>>([]);
  const currentSectionRef = useRef<string>("top");
  const greetUntilRef = useRef(0);
  const homeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastHoverTrackRef = useRef(0);
  const lastFlyTrackRef = useRef(0);
  const forcedModeRef = useRef<BotMode | null>(null);

  /* ---- 调试模式：?robot=idle_news 锁定姿态，供视觉回归与联调 ---- */
  useEffect(() => {
    try {
      const m = new URLSearchParams(window.location.search).get("robot") as BotMode | null;
      if (m && DEBUG_MODES.includes(m)) {
        forcedModeRef.current = m;
        if (m === "idle_news") setNewsText((NEWS[lang] ?? NEWS.en)[0]);
        setMode(m);
      }
    } catch {}
  }, [lang]);

  useEffect(() => {
    const refreshHome = () => {
      homeRef.current = { left: window.innerWidth - HOME.right - BOT_W, top: window.innerHeight - HOME.bottom - BOT_H };
    };
    const refreshRects = () => {
      const els = document.querySelectorAll('[data-robot-avoid="true"]');
      const arr: Array<{ l: number; t: number; r: number; b: number }> = [];
      els.forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) arr.push({ l: r.left, t: r.top, r: r.right, b: r.bottom });
      });
      avoidRectsRef.current = arr;
    };
    refreshHome();
    refreshRects();
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        refreshRects();
      });
    };
    const onResize = () => {
      refreshHome();
      onScroll();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize);
    const requery = setInterval(() => {
      if (!document.hidden) refreshRects();
    }, 1500);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
      clearInterval(requery);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  /* ---- 感知当前浏览版块，供场景化播报选池与种子问题 ---- */
  useEffect(() => {
    const ids = ["top", "autochat", "products", "pricing", "cases", "proof", "contact"];
    const els = ids.map((id) => document.getElementById(id)).filter((el): el is HTMLElement => !!el);
    if (!els.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) currentSectionRef.current = e.target.id;
      },
      { rootMargin: "-35% 0px -45% 0px" }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  /* ---- 行为调度：每 6s 随机小动作；隐藏页 / 高速滚动 / 悬停 / 调试锁定时静默 ---- */
  useEffect(() => {
    if (reduced) return;
    const loop = setInterval(() => {
      if (forcedModeRef.current) return;
      if (document.hidden || isHoveredRef.current) return;
      if (Math.abs(springScrollVelocity.get()) > 100 || Math.abs(flightVelX.get()) > 10) return;
      const rand = Math.random();
      let next: BotMode = "idle_base";
      if (rand > 0.95) next = "idle_spin";
      else if (rand > 0.9) next = "idle_dance";
      else if (rand > 0.8) next = "idle_wave";
      else if (rand > 0.65) {
        next = "idle_news";
        const pool = SECTION_NEWS[lang]?.[currentSectionRef.current] ?? NEWS[lang] ?? NEWS.en;
        const txt = pool[Math.floor(Math.random() * pool.length)];
        setNewsText(txt);
        track("sprite_news_impression", { text: txt, section: currentSectionRef.current });
      } else if (rand > 0.5) next = "idle_scan";
      if (next === "idle_wave") greetUntilRef.current = Date.now() + 3000;
      setMode(next);
      if (next !== "idle_base") {
        const dur = next === "idle_dance" ? 3600 : next === "idle_news" ? 5000 : next === "idle_spin" ? 1500 : 3000;
        setTimeout(() => setMode((p) => (p === next ? "idle_base" : p)), dur);
      }
    }, 6000);
    return () => clearInterval(loop);
  }, [reduced, lang, springScrollVelocity, flightVelX]);

  /* ---- 进场问好：入场动画落定后自动挥手一次（每会话一次；开场页存在时等它退场） ---- */
  useEffect(() => {
    if (reduced) return;
    try {
      if (sessionStorage.getItem(GREET_KEY)) return;
    } catch {}
    let fired = false;
    const timers: Array<ReturnType<typeof setTimeout>> = [];
    const greet = () => {
      if (fired) return;
      fired = true;
      timers.push(
        setTimeout(() => {
          if (isHoveredRef.current || forcedModeRef.current) return;
          greetUntilRef.current = Date.now() + 2800;
          setMode("idle_wave");
          track("sprite_greet");
          try {
            sessionStorage.setItem(GREET_KEY, "1");
          } catch {}
          timers.push(setTimeout(() => setMode((p) => (p === "idle_wave" ? "idle_base" : p)), 2800));
        }, 1600)
      );
    };
    let introShowing = false;
    try {
      introShowing = !sessionStorage.getItem("bl-intro-seen");
    } catch {}
    const onIntroEnter = () => greet();
    if (introShowing) {
      window.addEventListener("bl-intro-entered", onIntroEnter, { once: true });
      timers.push(setTimeout(greet, 15000)); // 兜底：事件丢失也保证问好
    } else {
      timers.push(setTimeout(greet, 1200)); // 等入场弹簧基本落定再问好
    }
    return () => {
      window.removeEventListener("bl-intro-entered", onIntroEnter);
      timers.forEach(clearTimeout);
    };
  }, [reduced]);

  /* ---- 点击页面空白处 → 飞过去；8s 无事自动飞回休息位（桌面端专属） ---- */
  useEffect(() => {
    if (reduced || !isDesktop) return;
    const handleClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest(FLY_IGNORE)) return;
      if (window.getSelection()?.toString()) return;
      const cx = homeRef.current.left + BOT_W / 2;
      const cy = homeRef.current.top + BOT_H / 2;
      clickX.set(e.clientX - cx);
      clickY.set(e.clientY - cy);
      const now = Date.now();
      if (now - lastFlyTrackRef.current > 5000) {
        lastFlyTrackRef.current = now;
        track("sprite_fly");
      }
      if (homeTimerRef.current) clearTimeout(homeTimerRef.current);
      homeTimerRef.current = setTimeout(() => {
        clickX.set(0);
        clickY.set(0);
      }, 8000);
    };
    window.addEventListener("click", handleClick);
    return () => {
      window.removeEventListener("click", handleClick);
      if (homeTimerRef.current) clearTimeout(homeTimerRef.current);
    };
  }, [clickX, clickY, reduced, isDesktop]);

  /* ---- 眼神跟随：rAF 节流的指针追踪（纯数学推导机器人位置，无 DOM 读取，桌面端专属） ---- */
  useEffect(() => {
    if (reduced || !isDesktop) return;
    let raf = 0;
    const onMove = (e: PointerEvent) => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const cx = homeRef.current.left + clickX.get() + avoidX.get() + BOT_W / 2;
        const cy = homeRef.current.top + clickY.get() + smoothDragY.get() + avoidY.get() + BOT_H / 2 - 40;
        gazeX.set(Math.max(-2.4, Math.min(2.4, (e.clientX - cx) / 160)));
        gazeY.set(Math.max(-1.6, Math.min(1.6, (e.clientY - cy) / 200)));
      });
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [reduced, isDesktop, clickX, clickY, smoothDragY, avoidX, avoidY, gazeX, gazeY]);

  /**
   * 避让推挤：机器人包围盒 vs 避让区（含 padding），沿穿透较浅的轴推出。
   * 比原先的圆形近似更贴合矩形按钮，且不再逐帧 gBCR。
   */
  const resolveAvoid = (rx: number, ry: number) => {
    const PAD = 18;
    let ax = 0;
    let ay = 0;
    let hit = false;
    for (const rc of avoidRectsRef.current) {
      const l = rc.l - PAD;
      const t = rc.t - PAD;
      const r = rc.r + PAD;
      const b = rc.b + PAD;
      const ox = Math.min(rx + BOT_W, r) - Math.max(rx, l);
      const oy = Math.min(ry + BOT_H, b) - Math.max(ry, t);
      if (ox <= 0 || oy <= 0) continue;
      hit = true;
      if (ox < oy) ax += (rx + BOT_W / 2 < (l + r) / 2 ? -1 : 1) * (ox + 8);
      else ay += (ry + BOT_H / 2 < (t + b) / 2 ? -1 : 1) * (oy + 8);
    }
    return { hit, ax, ay };
  };

  /* ---- 逐帧状态机：滚动/飞行触发姿态切换 + 避让 + 着陆回弹（桌面端） ---- */
  useAnimationFrame(() => {
    const hovered = isHoveredRef.current;
    const forced = forcedModeRef.current;
    if (!reduced && isDesktop) {
      const v = springScrollVelocity.get();
      const speed = Math.hypot(flightVelX.get(), flightVelY.get());
      const rx = homeRef.current.left + clickX.get() + avoidX.get();
      const ry = homeRef.current.top + clickY.get() + smoothDragY.get() + avoidY.get();
      const col = resolveAvoid(rx, ry);
      avoidX.set(col.hit ? col.ax : 0);
      avoidY.set(col.hit ? col.ay : 0);
      if (!forced) {
        const SCROLL_TH = 500;
        const FLIGHT_TH = 50;
        if (v > SCROLL_TH) {
          if (mode !== "falling") setMode("falling");
          return;
        }
        if (v < -SCROLL_TH || speed > FLIGHT_TH) {
          if (mode !== "flying") setMode("flying");
          return;
        }
        if (mode === "falling" || mode === "flying") {
          setMode(hovered ? "idle_wave" : "idle_base");
          /* 着陆缓冲：一次挤压-回弹，速度归零的瞬间落地更有“重量感” */
          animate(squashY, [1, 0.9, 1.045, 1], { duration: 0.55, times: [0, 0.35, 0.7, 1], ease: "easeOut" });
          return;
        }
      }
    }
    if (forced) return;
    if (hovered) {
      if (mode !== "idle_wave") setMode("idle_wave");
    } else if (mode === "idle_wave" && Date.now() > greetUntilRef.current) {
      /* 悬停结束即收手（原实现会对着空气挥到下个调度周期） */
      setMode("idle_base");
    }
  });

  /** 打开 AI 客服（点击 / 键盘 / 全息面板均汇聚于此） */
  const openChatEvent = (from: string, seed?: string) => {
    window.dispatchEvent(new CustomEvent("bl:open-chat", { detail: { from, seed } }));
  };
  const handleRobotClick = () => {
    track("ai_sprite_click", { mode });
    openChatEvent("sprite");
  };
  const handleNewsCta = () => {
    const section = currentSectionRef.current;
    const seedPool = SECTION_SEED[lang] ?? SECTION_SEED.en;
    track("sprite_news_click", { section, text: newsText });
    openChatEvent("hologram", seedPool[section] ?? seedPool.top);
  };

  return (
    <motion.div
      className="fixed bottom-40 right-3 md:right-6 [perspective:1000px] pointer-events-none ai-sprite-container"
      style={{ zIndex: isHovered ? 100 : 50, x: combinedX, y: combinedY }}
    >
      {/* 响应式体型：移动端缩到 55%（轻量版），桌面端原尺寸；
          缩放放在独立节点上，避免与 framer 的 transform 写入互相覆盖 */}
      <div className="origin-bottom-right scale-[0.55] md:scale-100">
        <motion.div
          className="cursor-pointer pointer-events-auto outline-none focus-visible:ring-2 focus-visible:ring-neon-cyan/60 rounded-[2.5rem]"
          role="button"
          tabIndex={0}
          aria-label={lang === "zh" ? "打开 AI 客服对话" : "Open AI chat"}
          onMouseEnter={() => {
            setIsHovered(true);
            isHoveredRef.current = true;
            const now = Date.now();
            if (now - lastHoverTrackRef.current > 5000) {
              lastHoverTrackRef.current = now;
              track("sprite_hover");
            }
          }}
          onMouseLeave={() => {
            setIsHovered(false);
            isHoveredRef.current = false;
          }}
          onFocus={() => {
            setIsHovered(true);
            isHoveredRef.current = true;
          }}
          onBlur={() => {
            setIsHovered(false);
            isHoveredRef.current = false;
          }}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            handleRobotClick();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleRobotClick();
            }
          }}
          whileHover={{ scale: 1.1 }}
          whileTap={{ scale: 0.95 }}
          initial={reduced ? { opacity: 0 } : { y: 200, opacity: 0 }}
          animate={reduced ? { opacity: 1 } : { y: 0, opacity: 1 }}
          transition={{ type: "spring", stiffness: 50, damping: 20, delay: 0.6 }}
        >
          {/* 悬停提示气泡：给“可点击开客服”一个明确的转化引导 */}
          <AnimatePresence>
            {isHovered && (
              <motion.div
                className="pointer-events-none absolute right-full top-8 mr-1 whitespace-nowrap rounded-full border border-neon-cyan/30 bg-ink-900/90 px-3 py-1.5 text-xs font-medium text-neon-cyan shadow-lg backdrop-blur"
                initial={{ opacity: 0, x: 6, scale: 0.9 }}
                animate={{ opacity: 1, x: 0, scale: 1 }}
                exit={{ opacity: 0, x: 4, scale: 0.95 }}
                transition={{ duration: 0.18 }}
              >
                {lang === "zh" ? "点我 · AI 客服" : "Chat with AI"}
              </motion.div>
            )}
          </AnimatePresence>
          <EveBot
            mode={mode}
            isHovered={isHovered}
            newsText={newsText}
            newsCta={lang === "zh" ? "点我 · 立即咨询 →" : "Tap me to chat →"}
            scrollTilt={reduced || !isDesktop ? zeroMV : totalTilt}
            flightRotate={reduced || !isDesktop ? zeroMV : flightRotate}
            gazeX={gazeX}
            gazeY={gazeY}
            squashY={squashY}
            shadowOpacity={shadowOpacity}
            onNewsCta={handleNewsCta}
            reduced={reduced}
          />
        </motion.div>
      </div>
    </motion.div>
  );
}
