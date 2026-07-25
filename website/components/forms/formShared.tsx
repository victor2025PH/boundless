"use client";

import { useEffect, useRef } from "react";
import {
  AnimatePresence,
  animate,
  motion,
  useAnimationFrame,
  useMotionTemplate,
  useMotionValue,
  useSpring,
  useTransform,
  type MotionValue,
  type Variants,
} from "framer-motion";
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
  /** 手势指令（场景播报/舞台页下发）：非空时覆盖 mode 默认队形 */
  gesture?: HandGesture | null;
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


/** 花瓣形手臂。渐变 id 按侧+皮肤唯一，避免重复 SVG id；皮肤切换时换渐变色 */
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

/* ══════════════ 引力三指（Tri-Pod Levitation Hand）══════════════
 * EVE 式分离手：三枚「指荚」是独立悬浮体，从臂尖脱离展开，与臂端「磁力港」
 * 之间以发光引力丝维系——没有手掌、没有关节，是「场控编队」而非肉体手。
 * 品牌隐喻：聚则为一（无界底座），散则为九（九款产品），维系它们的无形之力是 AI。
 *
 * 造型不变量（决定「优雅」与否的三条硬线，改坐标前先读）：
 * 1) 荚是修长水滴，长宽比 ~2.4:1（7.4 × 17.8）。圆胖卵（旧 1.67:1）在任何
 *    队形下都会读成「白手套/馒头指」，比例是根因，不是间距能补救的；
 * 2) 荚的锚点＝**根部**而非中心（POD_PATH 以 y≈0 为根、向下生长）。旋转绕根
 *    发生 → 张开时根聚梢散，天然成扇；绕中心旋转（旧实现）永远张不开；
 * 3) 展开队形保证「梢间空隙 ≥ 荚宽」：fan/radar 在荚腹高度的净空隙 ≈ 8-12 单位
 *    对 7.4 的荚宽。collapsed 则让根缘刚好相切（间距 7.8 ≈ 荚宽）——聚而不融，
 *    仍读得出三瓣。
 * 旋转符号：SVG rotate 正角 = 顺时针，向下的荚其梢端向 **左** 摆。故左侧荚
 * （x<0）外张取正、右侧荚取负；旧队形表符号写反，是「越挥越挤」的第二根因。
 *
 * 工程不变量：
 * - 指荚常驻臂尖（collapsed 队形＝分瓣臂尖），一切展开都「从臂尖绽放」；
 * - 队形是纯数据（FORMATIONS），新手势＝加一行队形数据，不加逻辑；
 * - 全部运动 MotionValue 直驱（零重渲染）：位置弹簧 + 悬浮微动 rAF +
 *   腕摆滞后弹簧（follow-through 鞭梢感）；
 * - 荚与腕的 transform 走 **SVG attribute** 而非 CSS：SVG 上 CSS transform-origin
 *   的解析依赖 transform-box（浏览器默认 view-box → 原点会漂到 viewBox 中心），
 *   attribute 变换恒以元素局部 (0,0) 为原点，跨浏览器确定；
 * - reduced 直接落位（无环回动画）；lowFx 关悬浮微动/港口呼吸/电弧。
 */

export type HandFormation = "collapsed" | "fan" | "point" | "tripod" | "keyboard" | "radar" | "burst";

/** 手势指令：AISprite 场景播报 / 舞台页经此下发（icon 仅 tripod 托举队形展示） */
export type HandGesture = { formation: HandFormation; icon?: string };

type PodTarget = {
  /** 荚**根部**锚点相对磁力港的坐标（不是荚心；荚自根向下生长 17.8 单位） */
  x: number;
  y: number;
  /** 绕根自转（度）。正 = 梢端向左，故左荚外张取正、右荚取负 */
  r: number;
  s: number;
};
type FormationSpec = {
  /** 三枚指荚的港口相对坐标/自转/缩放（左手系；右手渲染时 x/r 镜像） */
  pods: [PodTarget, PodTarget, PodTarget];
  /** 引力丝显隐系数（collapsed=0：指荚贴附臂尖，力场不可见） */
  filament: number;
  /** 悬浮微动振幅（px）与相位速度（rad/s）——keyboard 的高频微动＝空中打字 */
  floatAmp: number;
  floatSpeed: number;
  /** 旋转抖动幅度（度）：burst 蓄力紊乱专用 */
  jitterRot: number;
  /** 是否腕摆循环（fan 挥手专用） */
  wristSwing: boolean;
};

/**
 * 队形库：collapsed 收纳／fan 挥手／point 聚合指向（三荚叠成一根「超级指」）／
 * tripod 三点托举（呈现物嵌在荚间空当）／keyboard 空中速点／radar 雷达阵列／burst 磁场紊乱。
 *
 * 坐标皆为「荚根」位置。**张开靠自转，不靠平移**——这是本队形表最重要的一条：
 * 真手张开时指根（掌）几乎不动、只有指尖分开；把根也甩出去，三枚荚就从「一只手」
 * 退化成「空中飘着三颗胶囊」（首版实测如此）。故各队形的根一律压在港口附近
 * （|x| ≤ 8.2），梢端的分离度由 30~46° 的外旋提供。
 */
export const FORMATIONS: Record<HandFormation, FormationSpec> = {
  /** 收纳：根缘相切（间距 7.2 ≈ 荚宽 7.4）且上提到港口以内 → 臂尖裂成三瓣花苞，不是白团 */
  collapsed: {
    pods: [{ x: -3.6, y: -1, r: 9, s: 0.95 }, { x: 0, y: 0.4, r: 0, s: 1 }, { x: 3.6, y: -1, r: -9, s: 0.95 }],
    filament: 0, floatAmp: 0.4, floatSpeed: 1.3, jitterRot: 0, wristSwing: false,
  },
  /** 挥手：根仅张 ±7.6（仍是一只手）+ 外旋 40° → 梢端净空隙 ≈ 11 > 荚宽 7.4 */
  fan: {
    pods: [{ x: -7.6, y: 3.2, r: 40, s: 1 }, { x: 0, y: 5.2, r: 0, s: 1.04 }, { x: 7.6, y: 3.2, r: -40, s: 1 }],
    filament: 1, floatAmp: 1.1, floatSpeed: 2, jitterRot: 0, wristSwing: true,
  },
  /** 指向：三荚同轴递缩，串成一根伸缩「超级指」 */
  point: {
    pods: [{ x: 0, y: 1.4, r: 0, s: 1 }, { x: 0, y: 8.8, r: 0, s: 0.9 }, { x: 0, y: 15.6, r: 0, s: 0.8 }],
    filament: 0.85, floatAmp: 0.7, floatSpeed: 2, jitterRot: 0, wristSwing: false,
  },
  /** 托举：两侧荚大张成托盘、中荚下沉让位 → 呈现物嵌在三荚围出的空当 */
  tripod: {
    pods: [{ x: -5.5, y: 2.2, r: 46, s: 0.82 }, { x: 0, y: 16.8, r: 0, s: 0.85 }, { x: 5.5, y: 2.2, r: -46, s: 0.82 }],
    filament: 1, floatAmp: 0.9, floatSpeed: 1.7, jitterRot: 0, wristSwing: false,
  },
  /** 空中速点：微张悬停 + 高频上下微动 */
  keyboard: {
    pods: [{ x: -5.4, y: 3.6, r: 14, s: 1 }, { x: 0, y: 4.8, r: 0, s: 1 }, { x: 5.4, y: 3.6, r: -14, s: 1 }],
    filament: 0.85, floatAmp: 2.2, floatSpeed: 7.5, jitterRot: 0, wristSwing: false,
  },
  /** 雷达：最大张角阵列，梢端彼此远离成探测扇面 */
  radar: {
    pods: [{ x: -8.2, y: 2.8, r: 46, s: 1 }, { x: 0, y: 5.4, r: 0, s: 1.04 }, { x: 8.2, y: 2.8, r: -46, s: 1 }],
    filament: 1, floatAmp: 1.2, floatSpeed: 2.3, jitterRot: 0, wristSwing: false,
  },
  /** 蓄力紊乱：大张角 + 自转抖动 + 高频浮动 */
  burst: {
    pods: [{ x: -7.8, y: 3.2, r: 44, s: 1.02 }, { x: 0, y: 5.6, r: -6, s: 1.06 }, { x: 7.8, y: 3.2, r: -44, s: 1.02 }],
    filament: 0.7, floatAmp: 2.6, floatSpeed: 10, jitterRot: 9, wristSwing: false,
  },
};

/** mode → 默认队形（gesture 指令未下发时）。secondary=右手（配重手，仅大动作跟随展开） */
export function formationForMode(mode: BotMode, opts?: { secondary?: boolean }): HandFormation {
  if (opts?.secondary) {
    if (mode === "falling") return "radar";
    if (mode === "idle_dance") return "fan";
    return "collapsed";
  }
  switch (mode) {
    case "idle_wave":
    case "idle_dance":
      return "fan";
    case "idle_news":
      return "point";
    case "idle_scan":
    case "falling":
      return "radar";
    default:
      return "collapsed";
  }
}

type PodTone = { stops: [string, string, string]; edge: string; shade: string; shadeAlpha: number; hiOpacity: number; claw: boolean; clawGlow?: string };

/** 指荚材质（与臂/身体同族的白瓷水滴；恶魔尖爪化，祥龙金鳞化）。
 *  shade = 右下暗面色，靠一层渐变罩塑体积——纯描边勾轮廓的荚在小尺寸下会读成扁片。 */
const POD_TONE: Record<Skin, PodTone> = {
  normal: { stops: ["#ffffff", "#effaff", "#cfe9fb"], edge: "rgba(88,128,175,0.4)", shade: "#5b8bb5", shadeAlpha: 0.42, hiOpacity: 0.92, claw: false },
  demon: { stops: ["#6b3a4e", "#37232f", "#150a10"], edge: "rgba(244,63,94,0.5)", shade: "#0b0308", shadeAlpha: 0.6, hiOpacity: 0.26, claw: true, clawGlow: "#f43f5e" },
  loong: { stops: ["#fffdf2", "#fbe9b6", "#e8bf67"], edge: "rgba(170,120,30,0.45)", shade: "#a9761d", shadeAlpha: 0.42, hiOpacity: 0.85, claw: true, clawGlow: "#f5c542" },
};

/** 荚长（未缩放，根锚点→梢端）：引力丝埋点与指尖电弧端点按此推算 */
const POD_LEN = 19.2;
const RAD = Math.PI / 180;

/**
 * 指荚轮廓 —— **原点 = 根部锚点**（荚向下生长）。
 * 修长水滴：宽 7.0 × 长 19.2（**2.74:1**），肩在上四分之一处最宽，向下长距柔缓收束，
 * 末端是短促的圆弧（圆润但不钝——钝头会读回「胶囊药丸」，实测 2.4:1 时仍偏胖）。
 * 顶端刻意越过锚点 1.5 单位，让引力丝的线头埋进荚体内，任何过冲都不露头。
 */
const POD_PATH =
  "M0 -1.5 C1.95 -1.4 3.4 1.1 3.5 5 C3.6 9.2 3.1 12.9 2.4 15.2 C2 16.7 1.2 17.7 0 17.7 C-1.2 17.7 -2 16.7 -2.4 15.2 C-3.1 12.9 -3.6 9.2 -3.5 5 C-3.4 1.1 -1.95 -1.4 0 -1.5 Z";
/** 爪化变体（demon/loong）：同样修长，但梢端拉长收锐成利爪 */
const POD_PATH_CLAW =
  "M0 -1.5 C1.95 -1.4 3.4 1.1 3.5 5 C3.6 8.8 2.9 12.4 2 15.2 C1.3 17.4 0.65 19.2 0 20.6 C-0.65 19.2 -1.3 17.4 -2 15.2 C-2.9 12.4 -3.6 8.8 -3.5 5 C-3.4 1.1 -1.95 -1.4 0 -1.5 Z";
/** 左缘弧形高光：细月牙读作釉面反光；圆斑高光在小尺寸下会读成「破洞」 */
const POD_RIM = "M-2.3 1 C-3.25 3.7 -3.35 8.2 -2.7 12.2";

/** 订阅 MotionValue<string> 逐帧写 SVG attribute（零重渲染；避开 framer 对 SVG 绑定的兼容性赌注） */
function useAttr<T extends SVGElement>(name: string, v: MotionValue<string>) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    ref.current?.setAttribute(name, v.get());
    return v.on("change", (val) => ref.current?.setAttribute(name, val));
  }, [name, v]);
  return ref;
}
const useAttrD = (d: MotionValue<string>) => useAttr<SVGPathElement>("d", d);

/**
 * 引力丝：宽淡辉光 + 极细亮芯。刻意压到「远看是隐约力场、近看才见线」——
 * 荚是主体，丝只是它悬浮的理由，抢戏即失败。
 */
const Filament = ({ d, color, opacity }: { d: MotionValue<string>; color: string; opacity: number }) => {
  const glowRef = useAttrD(d);
  const coreRef = useAttrD(d);
  return (
    <g>
      <motion.path ref={glowRef} stroke={color} strokeWidth={1.5} strokeLinecap="round" fill="none" className="transition-colors duration-1000" initial={false} animate={{ opacity: 0.09 * opacity }} transition={{ duration: 0.35 }} />
      <motion.path ref={coreRef} stroke={color} strokeWidth={0.42} strokeLinecap="round" fill="none" className="transition-colors duration-1000" initial={false} animate={{ opacity: 0.24 * opacity }} transition={{ duration: 0.35 }} />
    </g>
  );
};

/**
 * 单枚指荚的运动束：位置/自转/缩放（队形弹簧）+ 悬浮微动 + 腕摆滞后差
 * （follow-through：各荚刚度不同，摆动时梢部滞后回甩成鞭梢）。
 * 输出 transform 字符串（attribute 变换，原点恒为荚根）、引力丝 d、梢端坐标。
 */
function usePod(i: number, mir: 1 | -1, wristOsc: MotionValue<number>) {
  const c = FORMATIONS.collapsed.pods[i];
  const x = useMotionValue(c.x * mir);
  const y = useMotionValue(c.y);
  const rotBase = useMotionValue(c.r * mir);
  const scale = useMotionValue(c.s);
  const float = useMotionValue(0);
  const rotJit = useMotionValue(0);
  const wristLag = useSpring(wristOsc, { stiffness: 250 - i * 60, damping: 15 });
  const yF = useTransform([y, float], (v) => (v[0] as number) + (v[1] as number));
  const rot = useTransform([rotBase, wristOsc, wristLag, rotJit], (v) => (v[0] as number) + ((v[2] as number) - (v[1] as number)) * 0.75 + (v[3] as number));
  const transform = useMotionTemplate`translate(${x} ${yF}) rotate(${rot}) scale(${scale})`;
  /* 引力丝终点：沿荚轴埋进荚体 3.4 单位 → 无论怎么转，线头都藏在荚内 */
  const fex = useTransform([x, rot], (v) => (v[0] as number) - Math.sin((v[1] as number) * RAD) * 3.4);
  const fey = useTransform([yF, rot], (v) => (v[0] as number) + Math.cos((v[1] as number) * RAD) * 3.4);
  /* 起点按荚的方位在港口上微分开（±1.5 内）：三条丝若共用一点，六道描边叠在一起
     会在臂尖糊成一坨深色结（首版实测），分开后才是三缕各自出港的力场线 */
  const fsx = useTransform(x, (v) => Math.max(-1.5, Math.min(1.5, v * 0.19)));
  /* 控制点贴近港口一侧 → 丝出港时几乎垂直、末段才外弯，读作磁力线而非吊绳 */
  const fcx = useTransform([fsx, fex, float], (v) => (v[0] as number) + ((v[1] as number) - (v[0] as number)) * 0.26 + (v[2] as number) * 0.7);
  const fcy = useTransform(fey, (v) => v * 0.72);
  const d = useMotionTemplate`M ${fsx} 0.9 Q ${fcx} ${fcy} ${fex} ${fey}`;
  /* 梢端：供指尖电弧取端点 */
  const tipX = useTransform([x, rot, scale], (v) => (v[0] as number) - Math.sin((v[1] as number) * RAD) * POD_LEN * 0.86 * (v[2] as number));
  const tipY = useTransform([yF, rot, scale], (v) => (v[0] as number) + Math.cos((v[1] as number) * RAD) * POD_LEN * 0.86 * (v[2] as number));
  return { x, y, rotBase, scale, float, rotJit, yF, rot, d, transform, tipX, tipY };
}

/** 首帧 transform（attribute 订阅在 effect 后才生效，静态值兜住 SSR/首绘不闪位） */
const initialPodTransform = (i: number, mir: 1 | -1) => {
  const c = FORMATIONS.collapsed.pods[i];
  return `translate(${c.x * mir} ${c.y}) rotate(${c.r * mir}) scale(${c.s})`;
};

/**
 * 单枚指荚：白瓷底色 → 右下暗面罩 → 左缘月牙反光 → 肩部高光点 → 梢端柔光。
 * 四层都很淡，叠起来才是「釉面」；任一层单独看都不该抢眼（爪化皮肤另加爪尖赤/金光）。
 */
const Pod = ({ index, transform, initial, tone, gid, shid }: { index: number; transform: MotionValue<string>; initial: string; tone: PodTone; gid: string; shid: string }) => {
  const ref = useAttr<SVGGElement>("transform", transform);
  const d = tone.claw ? POD_PATH_CLAW : POD_PATH;
  return (
    <g ref={ref} data-pod={index} transform={initial}>
      <path d={d} fill={`url(#${gid})`} stroke={tone.edge} strokeWidth={0.55} strokeLinejoin="round" />
      <path d={d} fill={`url(#${shid})`} />
      <path d={POD_RIM} stroke="#ffffff" strokeWidth={1.1} strokeOpacity={tone.hiOpacity * 0.62} strokeLinecap="round" fill="none" />
      <ellipse cx={-1.35} cy={2.7} rx={0.9} ry={2} fill="#ffffff" opacity={tone.hiOpacity} />
      <ellipse cx={0.1} cy={13.6} rx={1.3} ry={1.9} fill="#ffffff" opacity={tone.hiOpacity * 0.22} />
      {tone.claw && tone.clawGlow && <circle cx={0} cy={19.3} r={1} fill={tone.clawGlow} style={{ filter: `drop-shadow(0 0 2.5px ${tone.clawGlow})` }} />}
    </g>
  );
};

/** 引力三指本体：臂尖磁力港 + 三指荚 + 引力丝 + 偶发电弧 + tripod 托举物 */
export const TriPodHand = ({
  skin = "normal",
  color,
  formation,
  side = "left",
  icon,
  reduced = false,
  lowFx = false,
}: {
  skin?: Skin;
  color: string;
  formation: HandFormation;
  side?: "left" | "right";
  icon?: string;
  reduced?: boolean;
  lowFx?: boolean;
}) => {
  const tone = POD_TONE[skin];
  const spec = FORMATIONS[formation];
  const mir: 1 | -1 = side === "left" ? 1 : -1;
  const anim = !reduced && !lowFx;
  const gid = `pod-grad-${skin}-${side}`;
  const shid = `pod-shade-${skin}-${side}`;

  const wristOsc = useMotionValue(0);
  const pod0 = usePod(0, mir, wristOsc);
  const pod1 = usePod(1, mir, wristOsc);
  const pod2 = usePod(2, mir, wristOsc);
  const pods = [pod0, pod1, pod2];
  const flow = useRef({ clock: 0, amp: 0, speed: FORMATIONS.collapsed.floatSpeed });
  const specRef = useRef(spec);
  specRef.current = spec;
  const podsRef = useRef(pods);
  podsRef.current = pods;

  /* 队形切换：逐荚错峰弹簧（绽放的 stagger），reduced 直接落位 */
  useEffect(() => {
    const s = FORMATIONS[formation];
    const ctrls = podsRef.current.flatMap((p, i) => {
      const t = s.pods[i];
      /* 错峰：两梢荚先绽、中荚后至（0/0.045 → 0.09），读作由外向内的绽放而非整体平移 */
      const cfg = reduced
        ? { duration: 0 }
        : { type: "spring" as const, stiffness: 320, damping: 19, delay: [0, 0.09, 0.045][i] };
      return [animate(p.x, t.x * mir, cfg), animate(p.y, t.y, cfg), animate(p.rotBase, t.r * mir, cfg), animate(p.scale, t.s, cfg)];
    });
    return () => ctrls.forEach((ctl) => ctl.stop());
  }, [formation, mir, reduced]);

  /* 腕摆：仅 fan 队形循环摆动（挥手），其余队形弹簧归零 */
  useEffect(() => {
    if (spec.wristSwing && !reduced) {
      /* 幅度较旧版收窄（19°→15°）：荚变修长后力臂更长，同角度的梢端位移已经足够大，
         再大就从「打招呼」变成「甩手」 */
      const ctrl = animate(wristOsc, [0, 12 * mir, -15 * mir, 9 * mir, -12 * mir, 4 * mir, 0], { delay: 0.3, duration: 1.7, repeat: Infinity, repeatDelay: 0.4, ease: "easeInOut" });
      return () => ctrl.stop();
    }
    const ctrl = animate(wristOsc, 0, { type: "spring", stiffness: 200, damping: 20 });
    return () => ctrl.stop();
  }, [spec.wristSwing, mir, reduced, wristOsc]);

  /* 悬浮微动 + burst 抖动：单 rAF 驱动三荚（振幅/速度平滑过渡，隐藏页自然暂停） */
  useEffect(() => {
    if (!anim) {
      podsRef.current.forEach((p) => {
        p.float.set(0);
        p.rotJit.set(0);
      });
    }
  }, [anim]);
  useAnimationFrame((_, delta) => {
    if (!anim) return;
    const f = flow.current;
    const m = specRef.current;
    const dt = Math.min(0.05, delta / 1000);
    f.amp += (m.floatAmp - f.amp) * Math.min(1, dt * 4);
    f.speed += (m.floatSpeed - f.speed) * Math.min(1, dt * 4);
    f.clock += dt * f.speed;
    podsRef.current.forEach((p, i) => {
      p.float.set(Math.sin(f.clock + i * 2.094) * f.amp);
      if (m.jitterRot > 0) p.rotJit.set(Math.sin(f.clock * 1.9 + i * 2.4) * m.jitterRot);
      else if (p.rotJit.get() !== 0) p.rotJit.set(0);
    });
  });

  /* 偶发电弧：两梢荚**指尖之间**约 7.5s 一闪（引力场的「静电」），左右手错开节拍 */
  const arcCx = useTransform([pod0.tipX, pod2.tipX], (v) => ((v[0] as number) + (v[1] as number)) / 2);
  const arcCy = useTransform([pod0.tipY, pod2.tipY, pod1.tipY], (v) => Math.max(((v[0] as number) + (v[1] as number)) / 2, v[2] as number) + 3.6);
  const arcD = useMotionTemplate`M ${pod0.tipX} ${pod0.tipY} Q ${arcCx} ${arcCy} ${pod2.tipX} ${pod2.tipY}`;
  const arcRef = useAttrD(arcD);
  /* 腕摆同样走 attribute：绕 (0,0)＝磁力港旋转，引力丝起点永不漂移 */
  const wristTransform = useMotionTemplate`rotate(${wristOsc})`;
  const wristRef = useAttr<SVGGElement>("transform", wristTransform);

  return (
    <svg width="64" height="60" viewBox="0 0 64 60" fill="none" className="overflow-visible" aria-hidden>
      <defs>
        {/* 斜向光轴（左上→右下）：白瓷的亮面在肩、暗面沉到右下梢，单荚就有体积 */}
        <linearGradient id={gid} x1="0.08" y1="0" x2="0.92" y2="1">
          <stop offset="0%" stopColor={tone.stops[0]} />
          <stop offset="48%" stopColor={tone.stops[1]} />
          <stop offset="100%" stopColor={tone.stops[2]} />
        </linearGradient>
        {/* 暗面罩：只在右下半程渐显，塑出釉面的转折而不弄脏亮面 */}
        <linearGradient id={shid} x1="0.2" y1="0.06" x2="1" y2="0.9">
          <stop offset="40%" stopColor={tone.shade} stopOpacity={0} />
          <stop offset="100%" stopColor={tone.shade} stopOpacity={tone.shadeAlpha} />
        </linearGradient>
      </defs>
      {/* 港口空间：臂尖＝原点。腕摆绕原点旋转 → 磁力港钉在臂尖，引力丝起点永不漂移 */}
      <g transform="translate(32 7)">
        <g ref={wristRef}>
          {pods.map((p, i) => (
            <Filament key={`f${i}`} d={p.d} color={color} opacity={spec.filament * (lowFx ? 0.6 : 1)} />
          ))}
          {/* 磁力港：臂尖断口的发光环（呼吸）。刻意做小做淡——它是荚的出处，不是主角 */}
          <motion.ellipse
            cx={0}
            cy={0.6}
            rx={2.9}
            ry={1.5}
            fill="none"
            stroke={color}
            strokeWidth={0.55}
            className="transition-colors duration-1000"
            initial={false}
            animate={anim && spec.filament > 0 ? { opacity: [0.2, 0.55, 0.2] } : { opacity: spec.filament > 0 ? 0.38 : 0 }}
            transition={anim && spec.filament > 0 ? { repeat: Infinity, duration: 2.2, ease: "easeInOut" } : { duration: 0.4 }}
          />
          <motion.circle cx={0} cy={0.6} r={0.7} fill={color} className="transition-colors duration-1000" initial={false} animate={{ opacity: spec.filament > 0 ? 0.5 : 0 }} transition={{ duration: 0.4 }} />
          {anim && spec.filament > 0 && (
            <motion.path
              ref={arcRef}
              stroke={color}
              strokeWidth={0.55}
              fill="none"
              strokeLinecap="round"
              initial={{ opacity: 0 }}
              animate={{ opacity: [0, 0, 0.75, 0] }}
              transition={{ repeat: Infinity, duration: 7.5, times: [0, 0.9, 0.95, 1], ease: "linear", delay: side === "left" ? 2 : 5 }}
            />
          )}
          {/* 三枚指荚（修长水滴独立悬浮体）；data-pod 供行为回归脚本量测队形展开。
              绘序 0→2→1：中荚压在两梢荚之上，收纳态的重叠边缘朝内，三瓣结构才读得清 */}
          {[0, 2, 1].map((i) => (
            <Pod key={i} index={i} transform={pods[i].transform} initial={initialPodTransform(i, mir)} tone={tone} gid={gid} shid={shid} />
          ))}
          {/* tripod 托举：两侧荚外张成托盘，呈现物（产品 emoji 等）嵌在空当里轻浮动 */}
          {icon && formation === "tripod" && (
            <motion.g initial={{ opacity: 0, scale: 0.4 }} animate={{ opacity: 1, scale: 1 }} transition={{ type: "spring", stiffness: 260, damping: 14, delay: 0.16 }}>
              <motion.g animate={anim ? { y: [0, -1.8, 0] } : { y: 0 }} transition={anim ? { repeat: Infinity, duration: 2.2, ease: "easeInOut" } : undefined}>
                {/* 全息底盘：浅色垫底保证深色 emoji（🎬💰等）在深背景可读，光环强化投影感 */}
                <circle cx={0} cy={10.6} r={7.6} fill="#eaf6ff" opacity={0.24} />
                <circle cx={0} cy={10.6} r={7.6} fill={color} opacity={0.15} className="transition-colors duration-1000" />
                <circle cx={0} cy={10.6} r={7.6} fill="none" stroke={color} strokeWidth={0.55} opacity={0.6} className="transition-colors duration-1000" />
                {/* emoji 经 foreignObject 走 HTML 文本链渲染：Windows Chromium 对 SVG <text>
                    彩色 emoji 不可靠（真机 Chrome/Edge 均复现空白），HTML 层各平台稳定 */}
                <foreignObject x={-7.6} y={3} width={15.2} height={15.2} style={{ overflow: "visible" }}>
                  <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "11px", lineHeight: 1 }}>{icon}</div>
                </foreignObject>
              </motion.g>
            </motion.g>
          )}
        </g>
      </g>
    </svg>
  );
};


/** 数码眼：横纹发光屏，支持多种表情形变。tilt=内低外高的“怒”倾角（恶魔皮肤用）、screen=屏底色 */
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
      style={{ backgroundColor: screen, boxShadow: `0 0 5px ${color}80`, border: `1px solid ${color}40`, rotate: tilt }}
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
    eyeColors: ["#22d3ee", "#8b5cf6", "#06b6d4", "#a855f7", "#38bdf8"],
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
 * 挥手仅抬到 +76°（指尖约在胸口高度），满足“招手时手臂也要低”。
 */
export const leftArmVariants: Variants = {
  idle_base: { x: 0, y: 0, rotate: 7 },
  /* 扫描：小抬臂 38°，配合 radar 队形把「探测阵列」端到身前 */
  idle_scan: { x: -1, y: -1, rotate: 38, transition: { type: "spring", stiffness: 160, damping: 16 } },
  idle_wave: { x: -2, y: -2, rotate: 76, transition: { type: "spring", stiffness: 170, damping: 15 } },
  idle_dance: { x: -7, y: -4, rotate: [6, 44, 6], transition: { rotate: { repeat: Infinity, duration: 0.4 } } },
  /* 播报：高抬臂 104°，point 队形正对头顶全息面板——「引导视线」的导购手势 */
  idle_news: { x: -2, y: -2, rotate: 104, transition: { type: "spring", stiffness: 150, damping: 16 } },
  idle_spin: { x: 0, y: 0, rotate: 7 },
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
  flying: { x: 3, y: 6, rotate: -42 },
  falling: { x: 12, y: -13, rotate: -124 },
};

/** 呼吸微摆生效的姿态：待机类动作时手臂随身体轻晃 ±1.6°，静态也是“活”的 */
export const SWAY_MODES = new Set<BotMode>(["idle_base", "idle_scan", "idle_news", "idle_spin"]);

/** 手臂呼吸微摆（嵌套节点实现，避免与姿态弹簧在同一元素上打架） */
export const armSwayVariants = (side: "left" | "right"): Variants => ({
  sway: {
    rotate: side === "left" ? [1.6, -1.6, 1.6] : [-1.6, 1.6, -1.6],
    transition: { repeat: Infinity, duration: 2.5, ease: "easeInOut" },
  },
  still: { rotate: 0, transition: { duration: 0.35 } },
});

