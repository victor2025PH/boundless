"use client";

import {
  AnimatePresence,
  motion,
  type MotionValue,
  type Variants,
} from "framer-motion";
import { Activity } from "lucide-react";

/**
 * 三形态（EveBot / DemonForm / LoongForm）共用的类型与原子组件。
 * 从 AISprite 拆出，供 forms/* 独立形态文件复用，避免循环依赖。
 */

export type BotMode =
  | "flying"
  | "falling"
  | "idle_base"
  | "idle_wave"
  | "idle_dance"
  | "idle_scan"
  | "idle_news"
  | "idle_spin"
  /** 点头致意：短促上下点两下 */
  | "idle_nod"
  /** 伸懒腰：双臂外展、身体微升 */
  | "idle_stretch"
  /** 好奇歪头：向内容侧倾一下再回正 */
  | "idle_tilt"
  /** 邀约：左臂朝内容区轻指一下 */
  | "idle_invite"
  /** 警觉欢迎：微抬头扫一眼（回首页 / 回标签） */
  | "idle_alert"
  /** 轻鼓：双臂在胸前合一下（案例区低概率） */
  | "idle_clap"
  /** 害羞收臂：连点彩蛋蓄力时微缩提示 */
  | "idle_shy";

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
  idle_spin: { y: -4, rotate: 0 },
  /* 点头：两次短促下沉，读作“嗯嗯” */
  idle_nod: {
    y: [-4, 2, -4, 2, -4],
    rotate: 0,
    transition: { duration: 1.35, ease: "easeInOut", times: [0, 0.22, 0.45, 0.68, 1] },
  },
  /* 伸懒腰：微升停顿再落回 */
  idle_stretch: {
    y: [-4, -14, -14, -4],
    rotate: 0,
    transition: { duration: 2.2, ease: "easeInOut", times: [0, 0.25, 0.7, 1] },
  },
  /* 好奇：向页面内容侧（左）歪头再回正 */
  idle_tilt: {
    y: -5,
    rotate: [0, -14, -14, 0],
    transition: { duration: 2.0, ease: "easeInOut", times: [0, 0.28, 0.72, 1] },
  },
  /* 邀约：身体略倾内容侧，配合左臂指意 */
  idle_invite: {
    y: -6,
    rotate: [0, -8, -8, 0],
    transition: { duration: 2.1, ease: "easeInOut", times: [0, 0.25, 0.7, 1] },
  },
  /* 欢迎回：微抬头停顿再回正 */
  idle_alert: {
    y: [-4, -10, -10, -4],
    rotate: [0, 4, 4, 0],
    transition: { duration: 1.9, ease: "easeInOut", times: [0, 0.28, 0.68, 1] },
  },
  /* 轻鼓：两次小弹，读作“啪啪”肯定 */
  idle_clap: {
    y: [-4, -9, -4, -9, -4],
    rotate: 0,
    transition: { duration: 1.55, ease: "easeInOut", times: [0, 0.22, 0.45, 0.68, 1] },
  },
  /* 害羞：微缩下沉再回，配合收臂 */
  idle_shy: {
    y: [-4, 2, 2, -4],
    rotate: [0, 3, 3, 0],
    transition: { duration: 1.6, ease: "easeInOut", times: [0, 0.3, 0.7, 1] },
  },
  flying: { rotate: 0, y: -20 },
  falling: { rotate: -15, y: 30 },
});

/**
 * 身体解剖常量（容器坐标系）。
 * 肩位 = 蛋形身体上缘（≈65px）下方 5~13px，手臂自然垂在身体两侧，
 * 修复原先臂根挂在头部高度形成的“兔耳”观感。
 */
export const ANATOMY = {
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
export const NeuralNeck = ({ color }: { color: string }) => (
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


/** 花瓣形手臂（原造型）：柔和肩线收成尖端，无手指。渐变 id 按侧+皮肤唯一。 */
export const EveArm = ({ side, stops = ["#ffffff", "#eef2ff", "#dbeafe"], edge = "white" }: { side: "left" | "right"; stops?: [string, string, string]; edge?: string }) => {
  const gid = `eve-arm-grad-${side}-${stops[0].replace("#", "")}`;
  return (
    <svg width="18" height="64" viewBox="0 0 18 64" fill="none" className="drop-shadow-sm" style={{ transform: side === "right" ? "scaleX(-1)" : undefined }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={stops[0]} />
          <stop offset="40%" stopColor={stops[1]} />
          <stop offset="100%" stopColor={stops[2]} />
        </linearGradient>
      </defs>
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62C17 62.5 18 60 18 56C18 56 18 10 16 2Z" fill={`url(#${gid})`} />
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62" stroke={edge} strokeWidth="0.5" strokeOpacity="0.8" fill="none" />
      <path d="M16 2C16 2 4 10 4 28C4 50 12 60 16 62C17 62.5 18 60 18 56C18 56 18 10 16 2Z" stroke="rgba(0,0,0,0.05)" strokeWidth="0.5" />
    </svg>
  );
};

/** 数码眼：原横纹椭圆屏造型；只抬高亮度与外溢光。tilt=恶魔倾角、screen=屏底色 */
export const DigitalEye = ({ expression, color, tilt = 0, screen = "#001020" }: { expression: EyeExpr; color: string; tilt?: number; screen?: string }) => {
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
      className="relative w-6 overflow-hidden transition-colors duration-1000"
      style={{ backgroundColor: screen, boxShadow: `0 0 8px ${color}aa, 0 0 14px ${color}55`, border: `1px solid ${color}66`, rotate: tilt }}
      animate={variants[expression === "wink" ? "blink" : expression]}
      transition={{ type: "spring", stiffness: 300, damping: 20 }}
    >
      <motion.div className="absolute inset-0 bg-black" initial={{ opacity: 0 }} animate={{ opacity: expression === "happy" ? 1 : 0 }} style={{ clipPath: "polygon(0% 50%, 100% 50%, 100% 100%, 0% 100%)" }} />
      {expression === "scanning" && <motion.div className="absolute inset-0 bg-white/50 h-[2px]" animate={{ top: ["0%", "100%", "0%"] }} transition={{ duration: 1, repeat: Infinity, ease: "linear" }} />}
      <div className="absolute inset-0 flex flex-col justify-center gap-[1px] opacity-95">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="w-full h-[2px] transition-colors duration-1000" style={{ backgroundColor: color, boxShadow: `0 0 3px ${color}`, opacity: 1 - Math.abs(2 - i) * 0.18 }} />
        ))}
      </div>
      <div className="absolute inset-0 blur-sm transition-colors duration-1000" style={{ backgroundColor: `${color}55` }} />
    </motion.div>
  );
};

/** 皮肤：normal=默认冰蓝 IP；demon=隐藏彩蛋恶魔形态；loong=龙珠彩蛋「祥龙金鳞」（均纯外观） */
export type Skin = "normal" | "demon" | "loong";

/**
 * 皮肤配色表：恶魔态只换“皮”（配色 + 装饰件），骨架/动画/交互全部复用。
 * eyeColors 供眼睛/光束/推进器/光池取色轮换；body/head 为躯干与头壳的材质。
 */
export const SKIN: Record<Skin, {
  eyeColors: string[];
  eyeScreen: string;
  eyeTilt: number;
  body: string;
  bodyShadow: string;
  head: string;
  headHi: string;
  armStops: [string, string, string];
}> = {
  normal: {
    /* ∞ 七色加亮版：造型不变，只让屏幕更亮 */
    eyeColors: ["#22e8ff", "#4d8fff", "#a55cff", "#ef4dff", "#ff7ab8", "#ff9630", "#ffc028"],
    eyeScreen: "#001020",
    eyeTilt: 0,
    body: "radial-gradient(circle at 30% 50%, #ffffff 0%, #ecfeff 50%, #cffafe 100%)",
    bodyShadow: "inset -5px -5px 15px rgba(0,0,0,0.05), inset 5px 5px 15px rgba(255,255,255,1), 0 10px 25px rgba(0,0,0,0.1)",
    head: "radial-gradient(circle at 50% 10%, #ffffff 0%, #ecfeff 60%, #cffafe 100%)",
    headHi: "rgba(255,255,255,0.6)",
    armStops: ["#ffffff", "#eef2ff", "#dbeafe"],
  },
  demon: {
    eyeColors: ["#ef4444", "#f43f5e", "#dc2626", "#fb7185", "#e11d48"],
    eyeScreen: "#12040a",
    eyeTilt: 12,
    body: "radial-gradient(circle at 30% 42%, #3b2230 0%, #23131c 55%, #140a10 100%)",
    bodyShadow: "inset -5px -5px 15px rgba(0,0,0,0.45), inset 4px 4px 14px rgba(190,40,70,0.3), 0 10px 28px rgba(190,20,50,0.3)",
    head: "radial-gradient(circle at 50% 12%, #4a2836 0%, #281521 60%, #160b12 100%)",
    headHi: "rgba(255,120,150,0.35)",
    armStops: ["#3f2433", "#291923", "#150c12"],
  },
  /** 祥龙金鳞：暖金白瓷 + 金瞳偶闪青光（青金 = 品牌东方龙配色），龙珠彩蛋集齐解锁 */
  loong: {
    eyeColors: ["#f5c542", "#ffd75e", "#eab308", "#38bdf8", "#fbbf24"],
    eyeScreen: "#180f02",
    eyeTilt: 0,
    body: "radial-gradient(circle at 30% 50%, #fffbe8 0%, #ffedb8 55%, #f3d488 100%)",
    bodyShadow: "inset -5px -5px 15px rgba(160,110,20,0.18), inset 5px 5px 15px rgba(255,255,255,0.95), 0 10px 28px rgba(240,180,60,0.35)",
    head: "radial-gradient(circle at 50% 10%, #fffdf4 0%, #ffeec2 60%, #f3d488 100%)",
    headHi: "rgba(255,255,255,0.7)",
    armStops: ["#fff6dc", "#ffe9b0", "#f0cf85"],
  },
};

/** 恶魔犄角：一对深色弯角带红色轮廓光，从头壳顶部两侧探出（仅恶魔皮肤） */
export const DemonHorns = ({ color }: { color: string }) => (
  <div className="pointer-events-none absolute -top-3 left-1/2 z-20 -translate-x-1/2" style={{ width: 72, height: 22 }}>
    {(["left", "right"] as const).map((side) => (
      <svg
        key={side}
        width="20"
        height="24"
        viewBox="0 0 20 24"
        fill="none"
        className="absolute top-0"
        style={{ [side]: 6, transform: side === "right" ? "scaleX(-1)" : undefined } as React.CSSProperties}
      >
        <path d="M15 24C7 21 3 13 4 4C4 4 9 6 13 11C16 15 16 20 15 24Z" fill="#1a0d13" stroke={color} strokeOpacity="0.55" strokeWidth="1" />
        <path d="M12 20C8 17 6 12 6.5 7C9 9 11 12 12 15Z" fill={color} fillOpacity="0.25" />
      </svg>
    ))}
  </div>
);

/** 祥龙鹿角：一对分叉金鹿角（龙有九似「角似鹿」），从头壳顶两侧探出（仅祥龙皮肤） */
export const LoongAntlers = ({ color }: { color: string }) => (
  <div className="pointer-events-none absolute -top-4 left-1/2 z-20 -translate-x-1/2" style={{ width: 78, height: 26 }}>
    {(["left", "right"] as const).map((side) => (
      <svg
        key={side}
        width="26"
        height="28"
        viewBox="0 0 26 28"
        fill="none"
        className="absolute top-0"
        style={{ [side]: 4, transform: side === "right" ? "scaleX(-1)" : undefined } as React.CSSProperties}
      >
        <path d="M20 28 C14 22 10 15 11 6 C11 6 14 8 16 12 C18 16 19 22 20 28 Z" fill="#f7dfa0" stroke="#caa14e" strokeWidth="1" />
        <path d="M12 14 C9 11 7 8 7 4 C10 6 12 9 13 12 Z" fill="#f7dfa0" stroke="#caa14e" strokeWidth="0.8" />
        <path d="M15 20 C13 17 11 15 9 14 C10 17 12 20 14 22 Z" fill="#f7dfa0" stroke="#caa14e" strokeWidth="0.8" opacity="0.9" />
        <path d="M18 24 C15 20 13 14 13.5 8" stroke={color} strokeOpacity="0.5" strokeWidth="0.8" fill="none" />
      </svg>
    ))}
  </div>
);

/** 恶魔獠牙：眼屏下缘两颗小尖牙（仅恶魔皮肤） */
export const DemonFangs = () => (
  <div className="pointer-events-none absolute bottom-[3px] left-1/2 z-30 flex -translate-x-1/2 gap-3">
    {[0, 1].map((i) => (
      <span key={i} className="block h-[6px] w-[4px] bg-white" style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)", filter: "drop-shadow(0 1px 1px rgba(0,0,0,0.3))" }} />
    ))}
  </div>
);


/**
 * 左臂姿态表（角度符号：正 = 顺时针 = 左臂向外张开）。
 * 挥手仅抬到 +76°（臂尖约在胸口高度），满足“招手时手臂也要低”。
 */
export const leftArmVariants: Variants = {
  idle_base: { x: 0, y: 0, rotate: 7 },
  /* 扫描：小抬臂 38° */
  idle_scan: { x: -1, y: -1, rotate: 38, transition: { type: "spring", stiffness: 160, damping: 16 } },
  idle_wave: { x: -2, y: -2, rotate: 76, transition: { type: "spring", stiffness: 170, damping: 15 } },
  idle_dance: { x: -7, y: -4, rotate: [6, 44, 6], transition: { rotate: { repeat: Infinity, duration: 0.4 } } },
  /* 播报：高抬臂 104°，引导视线到头顶全息面板 */
  idle_news: { x: -2, y: -2, rotate: 104, transition: { type: "spring", stiffness: 150, damping: 16 } },
  idle_spin: { x: 0, y: 0, rotate: 7 },
  idle_nod: { x: 0, y: 1, rotate: 12, transition: { type: "spring", stiffness: 200, damping: 18 } },
  /* 伸懒腰：左臂大幅外展 */
  idle_stretch: { x: -6, y: -6, rotate: 118, transition: { type: "spring", stiffness: 140, damping: 14 } },
  idle_tilt: { x: -1, y: 0, rotate: 18, transition: { type: "spring", stiffness: 160, damping: 16 } },
  /* 邀约：左臂外展指内容（屏幕左侧 = 页面主体） */
  idle_invite: { x: -4, y: -3, rotate: 92, transition: { type: "spring", stiffness: 150, damping: 15 } },
  /* 警觉：双臂微张、左臂略抬 */
  idle_alert: { x: -2, y: -2, rotate: 48, transition: { type: "spring", stiffness: 170, damping: 16 } },
  /* 轻鼓：左臂向胸口合拢 */
  idle_clap: {
    x: 4,
    y: 2,
    rotate: [38, 52, 38, 52, 28],
    transition: { duration: 1.55, ease: "easeInOut", times: [0, 0.22, 0.45, 0.68, 1] },
  },
  /* 害羞：左臂贴身收 */
  idle_shy: { x: 2, y: 4, rotate: 22, transition: { type: "spring", stiffness: 180, damping: 18 } },
  flying: { x: -3, y: 6, rotate: 42 },
  falling: { x: -12, y: -13, rotate: 124 },
};

/** 右臂姿态表（镜像）：挥手时轻微外张作配重，重心不歪 */
export const rightArmVariants: Variants = {
  idle_base: { x: 0, y: 0, rotate: -7 },
  idle_scan: { x: 0, y: 0, rotate: -7 },
  idle_wave: { x: 2, y: 1, rotate: -13 },
  idle_dance: { x: 7, y: -4, rotate: [-6, -44, -6], transition: { rotate: { repeat: Infinity, duration: 0.4, delay: 0.2 } } },
  idle_news: { x: 1, y: 0, rotate: -12 },
  idle_spin: { x: 0, y: 0, rotate: -7 },
  idle_nod: { x: 0, y: 1, rotate: -12, transition: { type: "spring", stiffness: 200, damping: 18 } },
  /* 伸懒腰：右臂对称外展 */
  idle_stretch: { x: 6, y: -6, rotate: -118, transition: { type: "spring", stiffness: 140, damping: 14 } },
  idle_tilt: { x: 2, y: 0, rotate: -22, transition: { type: "spring", stiffness: 160, damping: 16 } },
  idle_invite: { x: 2, y: 0, rotate: -16, transition: { type: "spring", stiffness: 150, damping: 15 } },
  idle_alert: { x: 2, y: -1, rotate: -28, transition: { type: "spring", stiffness: 170, damping: 16 } },
  /* 轻鼓：右臂对称合拢 */
  idle_clap: {
    x: -4,
    y: 2,
    rotate: [-38, -52, -38, -52, -28],
    transition: { duration: 1.55, ease: "easeInOut", times: [0, 0.22, 0.45, 0.68, 1] },
  },
  idle_shy: { x: -2, y: 4, rotate: -22, transition: { type: "spring", stiffness: 180, damping: 18 } },
  flying: { x: 3, y: 6, rotate: -42 },
  falling: { x: 12, y: -13, rotate: -124 },
};

/** 呼吸微摆生效的姿态：待机类动作时手臂随身体轻晃 ±1.6°，静态也是“活”的 */
export const SWAY_MODES = new Set<BotMode>(["idle_base", "idle_scan", "idle_news", "idle_spin", "idle_tilt"]);

/** 手臂呼吸微摆（嵌套节点实现，避免与姿态弹簧在同一元素上打架） */
export const armSwayVariants = (side: "left" | "right"): Variants => ({
  sway: {
    rotate: side === "left" ? [1.6, -1.6, 1.6] : [-1.6, 1.6, -1.6],
    transition: { repeat: Infinity, duration: 2.5, ease: "easeInOut" },
  },
  still: { rotate: 0, transition: { duration: 0.35 } },
});

