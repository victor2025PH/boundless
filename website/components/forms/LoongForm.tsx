"use client";

import React, { useEffect, useRef, useState } from "react";
import { motion, useAnimationFrame, useMotionValue, useTransform, type Variants } from "framer-motion";
import { track } from "@/lib/track";
import { buildBodyVariants, DemonEmbers, NewsHologram, type BotMode, type DemonProps, type EyeExpr } from "./formShared";

/* ══════════════════════ 祥龙形态（龙珠彩蛋解锁的全龙形 IP，对齐概念稿 A） ══════════════════════ */

/** 祥龙挥手臂姿态（SVG 组旋转，原点=肩关节）：idle 双爪捧珠，wave 抬爪招手 */
const loongArmVariants: Variants = {
  idle_base: { rotate: 0 },
  idle_scan: { rotate: 0 },
  idle_wave: { rotate: 96, transition: { type: "spring", stiffness: 170, damping: 15 } },
  idle_dance: { rotate: [0, 50, 0], transition: { rotate: { repeat: Infinity, duration: 0.4 } } },
  idle_news: { rotate: 6 },
  idle_spin: { rotate: 0 },
  flying: { rotate: 30 },
  falling: { rotate: 120 },
};

/** 蓝鬃单缕：茎向 outward 的水滴形，根部圆钝尖端收细 */
const ManeLock = ({ d, fill, o = 1 }: { d: string; fill: string; o?: number }) => (
  <path d={d} fill={fill} fillOpacity={o} stroke="#1d4ed8" strokeOpacity="0.35" strokeWidth="0.8" />
);

/**
 * 祥龙形态：Q 版小神龙（概念稿 A 落地）——大头萌脸 + 大琥珀动漫眼 + 蓝色蓬鬃 +
 * 金鹿角 + 龙须 + 衔珠双爪 + 鳞纹 S 形蛇躯 + 蓝背鳍 + 蓝尾绒。
 * 整体单张 SVG 场景精绘（层叠可控），动画组：鬃毛/龙须/尾绒摆动、瞳孔视线跟随、
 * 眨眼/表情、挥手臂（SVG 组旋转）、宝珠脉动。复用姿态/挤压/滚动倾斜包装层。
 */
/** 蛇躯中线 10 控制点（三段三次贝塞尔：0-3 / 3-6 / 6-9）与腹甲线、各段波幅 */
const LOONG_BODY_PTS: ReadonlyArray<readonly [number, number]> = [
  [64, 62], [92, 70], [96, 92], [70, 102], [46, 111], [40, 122], [56, 133], [66, 140], [80, 139], [86, 132],
];
const LOONG_BELLY_PTS: ReadonlyArray<readonly [number, number]> = [
  [63, 66], [86, 73], [89, 90], [67, 99], [44, 108], [39, 120], [54, 130], [63, 136], [76, 136], [82, 130],
];
/** 波幅沿身体递增：颈根 0（头固定不脱节）→ 尾端最大；中段略压，减少「甩头傻」感 */
const LOONG_AMP = [0, 0.25, 1.2, 2.1, 3.0, 4.0, 5.2, 6.4, 7.2, 7.6];

/** 锥形躯干：每段线宽递减（颈粗→尾细），圆帽叠接遮住宽度跳变 */
const LOONG_W = {
  outline: [26, 21, 16],
  main: [23, 18, 13.5],
  shadow: [13, 10, 7.5],
  rim: [3.2, 2.6, 2.2],
  dots: [20, 15.5, 11.5],
  belly: [10, 8, 6.3],
} as const;

/** 设计 token：金三档 / 蓝三档 / 暗线 / 奶白两档（P0 色板收敛） */
const LOONG_C = {
  goldHi: "#ffe9a8",
  gold: "#e5a51f",
  goldDeep: "#c98a1b",
  line: "#b8860b",
  shadow: "rgba(140,90,10,0.34)",
  rim: "rgba(255,240,190,0.8)",
  blueHi: "#60a5fa",
  blue: "#3b82f6",
  blueDeep: "#2563eb",
  cream: "#fff6df",
  creamDeep: "#d9a94e",
} as const;

/** 单段三次贝塞尔 d（seg 0..2 → 点 [3s..3s+3]） */
const loongSegD = (pts: ReadonlyArray<ReadonlyArray<number>>, seg: number) => {
  const o = seg * 3;
  return `M${pts[o][0]} ${pts[o][1]} C${pts[o + 1][0]} ${pts[o + 1][1]} ${pts[o + 2][0]} ${pts[o + 2][1]} ${pts[o + 3][0]} ${pts[o + 3][1]}`;
};

export const LoongForm: React.FC<DemonProps> = ({ mode, isHovered, newsText, newsCta, scrollTilt, flightRotate, gazeX, gazeY, squashY, shadowOpacity, onNewsCta, reduced, lowFx = false }) => {
  const [eyeExpression, setEyeExpression] = useState<EyeExpr>("normal");
  const waving = mode === "idle_wave";
  const squashX = useTransform(squashY, (v) => 1 + (1 - v) * 0.55);
  const anim = !reduced && !lowFx;
  const glow = "#f5c542";

  /* ── 游动骨架：飞行大幅快摆=真龙游空，待机微幅慢摆=活物呼吸 ──
     每帧重算躯干贝塞尔控制点（横向正弦 + 尾部纵向倍频 = 8 字微动，头端锚定），
     直接写 DOM 属性零重渲染；鳍/后爪/尾/鬃/须按段位跟随或滞后摆动，不脱节。 */
  const segPathRefs = useRef<Record<string, SVGPathElement | null>>({});
  const finRefs = useRef<Array<SVGPathElement | null>>([]);
  const feetRef = useRef<SVGGElement | null>(null);
  const tailFollowRef = useRef<SVGGElement | null>(null);
  const maneLagRef = useRef<SVGGElement | null>(null);
  const whiskerLagRef = useRef<SVGGElement | null>(null);
  const swimRef = useRef({ phase: 0, k: 0.3, boostUntil: 0 });
  const modeRef2 = useRef(mode);
  modeRef2.current = mode;

  /* 摸头彩蛋：悬停 1.6s 触发 0.9s 游动加速（“被挠痒”的欢腾），不占用点击（点击=开客服） */
  useEffect(() => {
    if (!isHovered || !anim) return;
    const t = setTimeout(() => {
      swimRef.current.boostUntil = performance.now() + 900;
      track("sprite_loong_pet");
    }, 1600);
    return () => clearTimeout(t);
  }, [isHovered, anim]);

  useAnimationFrame((now, delta) => {
    if (!anim) return;
    const dt = Math.min(0.05, delta / 1000);
    const s = swimRef.current;
    const flyingNow = modeRef2.current === "flying" || modeRef2.current === "falling";
    const boost = now < s.boostUntil;
    /* 目标摆幅与波速：飞行 1.0/6.8，摸头 0.75/5.2，待机 0.3/1.7，平滑过渡 */
    const kTarget = flyingNow ? 1 : boost ? 0.75 : 0.3;
    s.k += (kTarget - s.k) * Math.min(1, dt * 5);
    s.phase += dt * (flyingNow ? 6.8 : boost ? 5.2 : 1.7);

    const off = (i: number): [number, number] => {
      const a = LOONG_AMP[i] * s.k;
      const th = s.phase - i * 0.72;
      /* 纵向 = 倍频小分量 → 尾端走 8 字，更接近真龙的立体游动 */
      return [Math.sin(th) * a, Math.sin(th * 2 + 0.6) * a * 0.28];
    };
    const applied = LOONG_BODY_PTS.map((p, i) => {
      const [dx, dy] = off(i);
      return [p[0] + dx, p[1] + dy];
    });
    const appliedBelly = LOONG_BELLY_PTS.map((p, i) => {
      const [dx, dy] = off(i);
      return [p[0] + dx, p[1] + dy];
    });

    for (let seg = 0; seg < 3; seg++) {
      const dS = loongSegD(applied, seg);
      const dB = loongSegD(appliedBelly, seg);
      for (const layer of ["outline", "main", "shadow", "rim", "dots"]) {
        segPathRefs.current[`s-${layer}-${seg}`]?.setAttribute("d", dS);
      }
      segPathRefs.current[`b-main-${seg}`]?.setAttribute("d", dB);
      segPathRefs.current[`b-bands-${seg}`]?.setAttribute("d", dB);
    }

    /* 鳍（段位 2/3/5）、后爪（段位 7）、尾根（段位 9）跟随平移 */
    const finSeg = [2, 3, 5];
    finRefs.current.forEach((el, idx) => {
      if (!el) return;
      const [dx, dy] = off(finSeg[idx] ?? 2);
      el.setAttribute("transform", `translate(${dx} ${dy})`);
    });
    if (feetRef.current) {
      const [dx, dy] = off(7);
      feetRef.current.setAttribute("transform", `translate(${dx} ${dy})`);
    }
    if (tailFollowRef.current) {
      const [dx, dy] = off(9);
      tailFollowRef.current.setAttribute("transform", `translate(${dx} ${dy})`);
    }
    /* 鬃毛/龙须跟随滞后（secondary motion）：相位滞后 1.1，飞行时摆动更明显 */
    const lag = Math.sin(s.phase - 1.1) * 2.6 * s.k;
    maneLagRef.current?.setAttribute("transform", `rotate(${lag.toFixed(2)} 64 40)`);
    whiskerLagRef.current?.setAttribute("transform", `rotate(${(-lag * 1.4).toFixed(2)} 64 52)`);
  });

  /* 表情状态机 + 待机随机眨眼（与 EveBot 同节奏） */
  useEffect(() => {
    if (isHovered) return setEyeExpression("happy");
    if (mode === "falling") return setEyeExpression("scared");
    if (mode === "idle_wave" || mode === "idle_dance") return setEyeExpression("happy");
    let alive = true;
    const blinkLoop = () => {
      if (!alive) return;
      if (mode === "idle_base" || mode === "idle_scan" || mode === "idle_news") {
        setEyeExpression("blink");
        setTimeout(() => setEyeExpression("normal"), 140);
      }
      setTimeout(blinkLoop, 2200 + Math.random() * 2800);
    };
    const timer = setTimeout(blinkLoop, 1800);
    setEyeExpression("normal");
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [mode, isHovered]);

  const happy = eyeExpression === "happy";
  const blink = eyeExpression === "blink";
  const scared = eyeExpression === "scared";

  return (
    <motion.div
      className="relative w-32 h-44 flex items-center justify-center [transform-style:preserve-3d]"
      style={{ rotateX: scrollTilt, rotate: flightRotate }}
    >
      <motion.div className="relative w-full h-full" style={{ scaleY: squashY, scaleX: squashX, transformOrigin: "50% 86%" }}>
        <motion.div
          className="relative w-full h-full"
          variants={buildBodyVariants(reduced)}
          animate={mode === "idle_spin" ? "idle_base" : mode}
          transition={{ type: "spring", stiffness: 100, damping: 20 }}
        >
          <NewsHologram active={mode === "idle_news"} text={newsText} cta={newsCta} color={glow} onCta={onNewsCta} />
          {anim && <DemonEmbers color={glow} />}

          <svg
            width="128"
            height="176"
            viewBox="0 0 128 176"
            fill="none"
            className={`absolute inset-0 overflow-visible ${lowFx && !reduced ? "loong-lowfx-sway" : ""}`}
          >
            <defs>
              <radialGradient id="lg-head" cx="0.5" cy="0.3" r="0.8">
                <stop offset="0%" stopColor="#ffe9a8" />
                <stop offset="62%" stopColor="#f2b93d" />
                <stop offset="100%" stopColor="#d99012" />
              </radialGradient>
              <linearGradient id="lg-body" x1="0.5" y1="0" x2="0.5" y2="1">
                <stop offset="0%" stopColor="#f6c94f" />
                <stop offset="60%" stopColor="#e5a51f" />
                <stop offset="100%" stopColor="#c98a1b" />
              </linearGradient>
              <linearGradient id="lg-belly" x1="0.5" y1="0" x2="0.5" y2="1">
                <stop offset="0%" stopColor="#fff6df" />
                <stop offset="100%" stopColor="#f3d791" />
              </linearGradient>
              <linearGradient id="lg-horn" x1="0.5" y1="1" x2="0.5" y2="0">
                <stop offset="0%" stopColor="#d99012" />
                <stop offset="45%" stopColor="#f2cf7e" />
                <stop offset="100%" stopColor="#fff3d1" />
              </linearGradient>
              <radialGradient id="lg-pearl" cx="0.35" cy="0.3" r="0.9">
                <stop offset="0%" stopColor="#fffdf2" />
                <stop offset="45%" stopColor="#ffd75e" />
                <stop offset="100%" stopColor="#e8960c" />
              </radialGradient>
              <radialGradient id="lg-iris" cx="0.5" cy="0.42" r="0.65">
                <stop offset="0%" stopColor="#ffb84d" />
                <stop offset="70%" stopColor="#c05f10" />
                <stop offset="100%" stopColor="#7c3505" />
              </radialGradient>
            </defs>

            {/* ── 尾部（躯干末端延伸 + 蓝尾绒，最底层）：外层随躯干末端游动平移 ── */}
            <g ref={tailFollowRef}>
            <motion.g
              style={{ transformOrigin: "86px 132px" }}
              animate={anim ? { rotate: [-5, 7, -5] } : { rotate: 0 }}
              transition={anim ? { repeat: Infinity, duration: 3, ease: "easeInOut" } : undefined}
            >
              <path d="M86 132 C97 134 104 140 106 148" stroke="#c98a1b" strokeWidth="12" strokeLinecap="round" fill="none" />
              <path d="M86 132 C97 134 104 140 106 148" stroke="url(#lg-body)" strokeWidth="9" strokeLinecap="round" fill="none" />
              {/* 蓝尾绒：五瓣火苗状 */}
              <motion.g
                style={{ transformOrigin: "106px 148px" }}
                animate={anim ? { rotate: [-7, 7, -7] } : { rotate: 0 }}
                transition={anim ? { repeat: Infinity, duration: 3, ease: "easeInOut", delay: 0.35 } : undefined}
              >
                <ManeLock d="M106 148 C114 138 122 136 127 140 C121 146 114 150 109 152 Z" fill="#3b82f6" />
                <ManeLock d="M106 148 C116 144 124 146 127 152 C120 155 112 155 108 153 Z" fill="#60a5fa" />
                <ManeLock d="M106 148 C114 152 118 158 116 165 C110 160 106 154 105 151 Z" fill="#2563eb" />
                <ManeLock d="M106 148 C108 156 106 163 100 167 C99 160 101 153 103 150 Z" fill="#60a5fa" o={0.9} />
              </motion.g>
            </motion.g>
            </g>

            {/* ── 蛇躯 S 形：三段锥形（颈粗→尾细）× 六层（描边/主体/底影/背光/腹甲/甲纹+鳞点）
                path d 由游动骨架逐帧分段驱动；尾段先画，颈段圆帽叠上遮接缝 ── */}
            {/* 胸口补丁：填住头下-颈弯-双臂之间的空隙，胸前不透底 */}
            <ellipse cx="63" cy="78" rx="16" ry="15" fill="url(#lg-body)" stroke={LOONG_C.line} strokeWidth="1" />
            {[2, 1, 0].map((seg) => (
              <path key={`o${seg}`} ref={(el) => { segPathRefs.current[`s-outline-${seg}`] = el; }} d={loongSegD(LOONG_BODY_PTS, seg)} stroke={LOONG_C.line} strokeWidth={LOONG_W.outline[seg]} strokeLinecap="round" fill="none" />
            ))}
            {[2, 1, 0].map((seg) => (
              <path key={`m${seg}`} ref={(el) => { segPathRefs.current[`s-main-${seg}`] = el; }} d={loongSegD(LOONG_BODY_PTS, seg)} stroke="url(#lg-body)" strokeWidth={LOONG_W.main[seg]} strokeLinecap="round" fill="none" />
            ))}
            {/* 底侧阴影 pass：窄带贴腹缘、向右下错位，塑体积 */}
            <g transform="translate(2 3.4)" opacity="0.9">
              {[2, 1, 0].map((seg) => (
                <path key={`sh${seg}`} ref={(el) => { segPathRefs.current[`s-shadow-${seg}`] = el; }} d={loongSegD(LOONG_BODY_PTS, seg)} stroke={LOONG_C.shadow} strokeWidth={LOONG_W.shadow[seg]} strokeLinecap="round" fill="none" />
              ))}
            </g>
            {/* 背脊 rim light pass：向左上错位的细亮线 */}
            <g transform="translate(-1.2 -2.6)">
              {[2, 1, 0].map((seg) => (
                <path key={`r${seg}`} ref={(el) => { segPathRefs.current[`s-rim-${seg}`] = el; }} d={loongSegD(LOONG_BODY_PTS, seg)} stroke={LOONG_C.rim} strokeWidth={LOONG_W.rim[seg]} strokeLinecap="round" fill="none" />
              ))}
            </g>
            {/* 腹甲：沿内侧曲线的奶白条 + 横向甲片纹 */}
            {[2, 1, 0].map((seg) => (
              <path key={`b${seg}`} ref={(el) => { segPathRefs.current[`b-main-${seg}`] = el; }} d={loongSegD(LOONG_BELLY_PTS, seg)} stroke="url(#lg-belly)" strokeWidth={LOONG_W.belly[seg]} strokeLinecap="round" fill="none" />
            ))}
            <g className="loong-fine">
              {[2, 1, 0].map((seg) => (
                <path key={`bb${seg}`} ref={(el) => { segPathRefs.current[`b-bands-${seg}`] = el; }} d={loongSegD(LOONG_BELLY_PTS, seg)} stroke={LOONG_C.creamDeep} strokeWidth={LOONG_W.belly[seg]} strokeLinecap="round" fill="none" strokeDasharray="1.2 4.2" strokeOpacity="0.55" />
              ))}
              {/* 鳞点：躯干外缘细碎弧点 */}
              {[2, 1, 0].map((seg) => (
                <path key={`d${seg}`} ref={(el) => { segPathRefs.current[`s-dots-${seg}`] = el; }} d={loongSegD(LOONG_BODY_PTS, seg)} stroke="rgba(140,95,15,0.28)" strokeWidth={LOONG_W.dots[seg]} strokeLinecap="round" fill="none" strokeDasharray="2 6.5" />
              ))}
            </g>

            {/* ── 背鳍：沿脊背外缘的三枚小三角蓝鳍（根部埋进描边，随所在段位游动）── */}
            <path ref={(el) => { finRefs.current[0] = el; }} d="M84 66 L92 71 L95 58 Z" fill="#3b82f6" stroke="#1d4ed8" strokeWidth="0.8" />
            <path ref={(el) => { finRefs.current[1] = el; }} d="M92 79 L93 90 L104 85 Z" fill="#3b82f6" stroke="#1d4ed8" strokeWidth="0.8" />
            <path ref={(el) => { finRefs.current[2] = el; }} d="M48 106 L45 115 L36 107 Z" fill="#3b82f6" stroke="#1d4ed8" strokeWidth="0.8" />

            {/* ── 后爪：底弯处一对小金脚（三趾奶白爪尖），随段位游动 ── */}
            <g ref={feetRef}>
              <path d="M56 134 C54 140 54 145 57 148 C60 147 62 143 62 139 Z" fill="url(#lg-body)" stroke="#b8860b" strokeWidth="0.9" />
              {[0, 1, 2].map((i) => (
                <path key={i} d={`M${54 + i * 3.2} 146 C${53.4 + i * 3.2} 149.5 ${54.4 + i * 3.2} 151.5 ${56 + i * 3.2} 152 C${57 + i * 3.2} 150 ${57 + i * 3.2} 147.5 ${56.4 + i * 3.2} 145.6 Z`} fill="#fff3d1" stroke="#c98a1b" strokeWidth="0.7" />
              ))}
              <path d="M74 137 C73 142 74 146 77 149 C80 147 81 143 80 139 Z" fill="url(#lg-body)" stroke="#b8860b" strokeWidth="0.9" />
              {[0, 1, 2].map((i) => (
                <path key={i} d={`M${72.4 + i * 3.2} 147 C${71.8 + i * 3.2} 150.5 ${72.8 + i * 3.2} 152.5 ${74.4 + i * 3.2} 153 C${75.4 + i * 3.2} 151 ${75.4 + i * 3.2} 148.5 ${74.8 + i * 3.2} 146.6 Z`} fill="#fff3d1" stroke="#c98a1b" strokeWidth="0.7" />
              ))}
            </g>

            {/* ── 宝珠（胸前）+ 火焰纹；飞行时拖极短星尘 ── */}
            <motion.g
              style={{ transformOrigin: "64px 92px" }}
              animate={anim ? { scale: [1, 1.08, 1] } : { scale: 1 }}
              transition={anim ? { repeat: Infinity, duration: 2.6, ease: "easeInOut" } : undefined}
            >
              <motion.circle
                cx="64"
                cy="92"
                r="13"
                fill={glow}
                animate={anim ? { opacity: [0.14, 0.3, 0.14] } : { opacity: 0.2 }}
                transition={anim ? { repeat: Infinity, duration: 2.6, ease: "easeInOut" } : undefined}
              />
              <circle cx="64" cy="92" r="9.5" fill="url(#lg-pearl)" stroke="#c98a1b" strokeWidth="0.8" />
              <circle cx="61" cy="88.5" r="2.6" fill="#fffdf2" opacity="0.9" />
              {anim && (mode === "flying" || mode === "falling" || isHovered) && (
                <g className="loong-pearl-dust" opacity="0.7">
                  {[0, 1, 2, 3].map((i) => (
                    <motion.circle
                      key={i}
                      cx={70 + i * 3.2}
                      cy={88 + (i % 2) * 4}
                      r={1.1 - i * 0.12}
                      fill="#ffe9a8"
                      animate={{ opacity: [0.15, 0.75, 0.15], x: [0, 4 + i] }}
                      transition={{ repeat: Infinity, duration: 1.1 + i * 0.15, ease: "easeInOut", delay: i * 0.08 }}
                    />
                  ))}
                </g>
              )}
            </motion.g>

            {/* ── 右臂（静态捧珠）── */}
            <g>
              <path d="M80 82 C82 88 80 93 75 96" stroke="#c98a1b" strokeWidth="7.5" strokeLinecap="round" fill="none" />
              <path d="M80 82 C82 88 80 93 75 96" stroke="url(#lg-body)" strokeWidth="5.5" strokeLinecap="round" fill="none" />
              <ellipse cx="74" cy="97" rx="4.6" ry="4" fill="url(#lg-head)" stroke="#b8860b" strokeWidth="0.8" />
              {[-1, 0, 1].map((i) => (
                <path key={i} d={`M${72 + i * 3} 99 C${71.4 + i * 3} 101.5 ${72.2 + i * 3} 103 ${73.4 + i * 3} 103.4 C${74.2 + i * 3} 101.8 ${74.2 + i * 3} 100 ${73.8 + i * 3} 98.6 Z`} fill="#fff3d1" stroke="#c98a1b" strokeWidth="0.6" />
              ))}
            </g>

            {/* ── 左臂（挥手臂：肩点 48,82 旋转）── */}
            <motion.g style={{ transformOrigin: "48px 82px" }} variants={loongArmVariants} animate={mode}>
              <path d="M48 82 C45 88 47 94 53 97" stroke="#c98a1b" strokeWidth="7.5" strokeLinecap="round" fill="none" />
              <path d="M48 82 C45 88 47 94 53 97" stroke="url(#lg-body)" strokeWidth="5.5" strokeLinecap="round" fill="none" />
              <ellipse cx="54" cy="98" rx="4.6" ry="4" fill="url(#lg-head)" stroke="#b8860b" strokeWidth="0.8" />
              {[-1, 0, 1].map((i) => (
                <path key={i} d={`M${52 + i * 3} 100 C${51.4 + i * 3} 102.5 ${52.2 + i * 3} 104 ${53.4 + i * 3} 104.4 C${54.2 + i * 3} 102.8 ${54.2 + i * 3} 101 ${53.8 + i * 3} 99.6 Z`} fill="#fff3d1" stroke="#c98a1b" strokeWidth="0.6" />
              ))}
            </motion.g>

            {/* ── 蓝鬃（贴头蓬松鬃毛：呼吸微摆 × 游动跟随滞后双层驱动）── */}
            <g ref={maneLagRef}>
            <motion.g
              style={{ transformOrigin: "64px 40px" }}
              animate={anim ? { rotate: [-1.2, 1.2, -1.2], scale: [1, 1.015, 1] } : { rotate: 0 }}
              transition={anim ? { repeat: Infinity, duration: 3.4, ease: "easeInOut" } : undefined}
            >
              {/* 底层大轮廓：环抱头壳的一整片鬃（深蓝），边缘波浪 */}
              <path
                d="M64 8 C46 8 34 18 32 32 C24 34 20 42 24 50 C18 56 20 66 28 70 C34 73 40 70 44 64 C38 56 36 46 40 38 C46 26 54 22 64 22 C74 22 82 26 88 38 C92 46 90 56 84 64 C88 70 94 73 100 70 C108 66 110 56 104 50 C108 42 104 34 96 32 C94 18 82 8 64 8 Z"
                fill="#2563eb"
                stroke="#1d4ed8"
                strokeOpacity="0.5"
                strokeWidth="1"
              />
              {/* 中层亮蓝瓣：叠瓦感 */}
              <ManeLock d="M40 22 C33 16 25 15 19 19 C25 26 33 30 40 31 Z" fill="#3b82f6" />
              <ManeLock d="M33 36 C25 34 17 36 13 42 C21 46 30 45 36 42 Z" fill="#3b82f6" />
              <ManeLock d="M32 50 C25 52 20 58 20 65 C28 63 34 58 37 53 Z" fill="#3b82f6" />
              <ManeLock d="M88 22 C95 16 103 15 109 19 C103 26 95 30 88 31 Z" fill="#3b82f6" />
              <ManeLock d="M95 36 C103 34 111 36 115 42 C107 46 98 45 92 42 Z" fill="#3b82f6" />
              <ManeLock d="M96 50 C103 52 108 58 108 65 C100 63 94 58 91 53 Z" fill="#3b82f6" />
              {/* 内层浅蓝提亮 */}
              <ManeLock d="M48 14 C44 9 38 6 32 7 C36 13 42 18 48 20 Z" fill="#60a5fa" />
              <ManeLock d="M80 14 C84 9 90 6 96 7 C92 13 86 18 80 20 Z" fill="#60a5fa" />
              {/* 颈后披落两缕（贴体外缘，不压宝珠区） */}
              <ManeLock d="M36 62 C30 70 28 79 32 87 C38 81 41 72 41 65 Z" fill="#2563eb" />
              <ManeLock d="M92 62 C98 70 100 79 96 87 C90 81 87 72 87 65 Z" fill="#2563eb" />
            </motion.g>
            </g>

            {/* ── 鹿角（高挑分叉大角：主枝外掠上扬 + 两根内叉，鬃毛之上头壳之后）── */}
            <g>
              <path d="M50 26 C43 18 39 8 42 -4 C44 -8 48 -8 49 -4 C50 4 51 12 54 19 C55 22 54 25 52 27 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="1.1" />
              <path d="M44 10 C39 6 35 0 35 -6 C40 -4 44 2 46 7 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="0.9" />
              <path d="M47 18 C43 16 39 13 37 9 C41 10 45 13 47 15 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="0.8" />
              <path d="M78 26 C85 18 89 8 86 -4 C84 -8 80 -8 79 -4 C78 4 77 12 74 19 C73 22 74 25 76 27 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="1.1" />
              <path d="M84 10 C89 6 93 0 93 -6 C88 -4 84 2 82 7 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="0.9" />
              <path d="M81 18 C85 16 89 13 91 9 C87 10 83 13 81 15 Z" fill="url(#lg-horn)" stroke="#b8860b" strokeWidth="0.8" />
            </g>

            {/* ── 头部（大头萌脸）── */}
            <g>
              {/* 耳朵 */}
              <path d="M36 36 C30 32 26 34 25 39 C28 43 33 44 37 42 Z" fill="url(#lg-head)" stroke="#c98a1b" strokeWidth="0.9" />
              <path d="M92 36 C98 32 102 34 103 39 C100 43 95 44 91 42 Z" fill="url(#lg-head)" stroke="#c98a1b" strokeWidth="0.9" />
              {/* 头壳 */}
              <ellipse cx="64" cy="42" rx="29" ry="25" fill="url(#lg-head)" stroke="#c98a1b" strokeWidth="1.1" />
              {/* 额前蓝色小刘海 */}
              <ManeLock d="M56 19 C56 12 60 7 66 6 C64 12 62 17 61 21 Z" fill="#60a5fa" />
              <ManeLock d="M64 18 C66 11 71 7 77 7 C73 12 69 17 67 21 Z" fill="#3b82f6" />
              {/* 奶白口鼻区 */}
              <ellipse cx="64" cy="53" rx="17" ry="11.5" fill="url(#lg-belly)" stroke="#e0b25e" strokeWidth="0.8" />
              {/* 鼻孔 */}
              <ellipse cx="58.5" cy="49.5" rx="1.3" ry="1.7" fill="#a16207" opacity="0.8" />
              <ellipse cx="69.5" cy="49.5" rx="1.3" ry="1.7" fill="#a16207" opacity="0.8" />
              {/* 嘴：开口笑 + 双小尖牙 */}
              <path d="M55 55.5 C58 61 70 61 73 55.5 C69 64 59 64 55 55.5 Z" fill="#8a3d10" stroke="#7c2d12" strokeWidth="0.7" />
              <path d="M57 56.5 L59.4 56.9 L58.4 59.2 Z" fill="#fffdf2" />
              <path d="M71 56.5 L68.6 56.9 L69.6 59.2 Z" fill="#fffdf2" />
              {/* 腮红（happy 时更深；小屏保留，不入 loong-fine） */}
              <ellipse cx="42" cy="50" rx="4.5" ry="2.8" fill="#f59e0b" opacity={happy ? 0.55 : 0.35} />
              <ellipse cx="86" cy="50" rx="4.5" ry="2.8" fill="#f59e0b" opacity={happy ? 0.55 : 0.35} />

              {/* 眼睛：大琥珀动漫眼（happy=弯月，blink=闭合线，scared=缩瞳） */}
              {happy ? (
                <g>
                  <path d="M42 40 C45 35 53 35 56 40" stroke="#7c3505" strokeWidth="2.6" strokeLinecap="round" fill="none" />
                  <path d="M72 40 C75 35 83 35 86 40" stroke="#7c3505" strokeWidth="2.6" strokeLinecap="round" fill="none" />
                </g>
              ) : blink ? (
                <g>
                  <path d="M42 41 C45 43 53 43 56 41" stroke="#7c3505" strokeWidth="2.4" strokeLinecap="round" fill="none" />
                  <path d="M72 41 C75 43 83 43 86 41" stroke="#7c3505" strokeWidth="2.4" strokeLinecap="round" fill="none" />
                </g>
              ) : (
                <g>
                  {/* 眼白底 */}
                  <ellipse cx="49" cy="40" rx="8.2" ry="9.6" fill="#fffaf0" stroke="#c98a1b" strokeWidth="0.8" />
                  <ellipse cx="79" cy="40" rx="8.2" ry="9.6" fill="#fffaf0" stroke="#c98a1b" strokeWidth="0.8" />
                  {/* 虹膜+瞳孔+高光：整组随视线微移 */}
                  <motion.g style={{ x: gazeX, y: gazeY }}>
                    <circle cx="49" cy="40.5" r={scared ? 4.6 : 6.4} fill="url(#lg-iris)" />
                    <circle cx="49" cy="40.5" r={scared ? 2 : 3.1} fill="#1c0a02" />
                    {/* 数字弧光：虹膜下缘一道细青弧 = AI 血统标记（IP 家族 DNA） */}
                    <path d="M45.4 43.6 A6 6 0 0 0 52.6 43.6" stroke="#7dd3fc" strokeWidth="0.9" strokeLinecap="round" fill="none" opacity="0.6" />
                    <circle cx="46.8" cy="37.8" r="2" fill="#ffffff" opacity="0.95" />
                    <circle cx="51.4" cy="43" r="1" fill="#ffffff" opacity="0.75" />
                    <circle cx="79" cy="40.5" r={scared ? 4.6 : 6.4} fill="url(#lg-iris)" />
                    <circle cx="79" cy="40.5" r={scared ? 2 : 3.1} fill="#1c0a02" />
                    <path d="M75.4 43.6 A6 6 0 0 0 82.6 43.6" stroke="#7dd3fc" strokeWidth="0.9" strokeLinecap="round" fill="none" opacity="0.6" />
                    <circle cx="76.8" cy="37.8" r="2" fill="#ffffff" opacity="0.95" />
                    <circle cx="81.4" cy="43" r="1" fill="#ffffff" opacity="0.75" />
                  </motion.g>
                  {/* 上眼睑线 */}
                  <path d="M41.5 36.5 C44.5 33 53.5 33 56.5 36.5" stroke="#b86a10" strokeWidth="1.4" strokeLinecap="round" fill="none" />
                  <path d="M71.5 36.5 C74.5 33 83.5 33 86.5 36.5" stroke="#b86a10" strokeWidth="1.4" strokeLinecap="round" fill="none" />
                </g>
              )}

              {/* 眉弓小凸 */}
              <path className="loong-fine" d="M44 29 C46 27.5 50 27.5 52 29" stroke="#e0b25e" strokeWidth="1.6" strokeLinecap="round" fill="none" opacity="0.8" />
              <path className="loong-fine" d="M76 29 C78 27.5 82 27.5 84 29" stroke="#e0b25e" strokeWidth="1.6" strokeLinecap="round" fill="none" opacity="0.8" />
            </g>

            {/* ── 龙须（脸颊两侧长须：呼吸轻摆 × 游动滞后反相）── */}
            <g ref={whiskerLagRef}>
            <motion.g
              style={{ transformOrigin: "64px 52px" }}
              animate={anim ? { rotate: [-1.2, 1.2, -1.2] } : { rotate: 0 }}
              transition={anim ? { repeat: Infinity, duration: 3.6, ease: "easeInOut" } : undefined}
            >
              <path className="loong-whisker" d="M46 51 C36 52 26 57 21 65 C18 70 21 74 25 73 C28 72 27 68 24 68" stroke="#ffd75e" strokeWidth="1.7" fill="none" strokeLinecap="round" />
              <path className="loong-whisker" d="M82 51 C92 52 102 57 107 65 C110 70 107 74 103 73 C100 72 101 68 104 68" stroke="#ffd75e" strokeWidth="1.7" fill="none" strokeLinecap="round" />
            </motion.g>
            </g>
          </svg>
        </motion.div>
      </motion.div>

      {/* 悬浮金辉光池 */}
      <motion.div className="pointer-events-none absolute left-1/2 top-[97%] -z-20 -translate-x-1/2" style={{ opacity: shadowOpacity }}>
        <motion.div
          className="h-3 w-16 rounded-[50%] blur-[6px]"
          style={{ background: `radial-gradient(ellipse at center, ${glow}55 0%, ${glow}18 55%, transparent 75%)` }}
          animate={anim ? { scaleX: [1, 0.82, 1], opacity: [0.9, 0.55, 0.9] } : { scaleX: 1, opacity: 0.8 }}
          transition={anim ? { repeat: Infinity, duration: 2.5, ease: "easeInOut" } : undefined}
        />
      </motion.div>
    </motion.div>
  );
};


/** 仪式/图鉴等独立场景用祥龙：内置惰性 MotionValue，默认全幅游动 */
export function LoongHero({ mode = "flying", className = "" }: { mode?: BotMode; className?: string }) {
  const zero = useMotionValue(0);
  const one = useMotionValue(1);
  const shadow = useMotionValue(0.55);
  return (
    <div className={className} data-loong-hero>
      <LoongForm
        mode={mode}
        isHovered={false}
        newsText=""
        newsCta=""
        scrollTilt={zero}
        flightRotate={zero}
        gazeX={zero}
        gazeY={zero}
        squashY={one}
        shadowOpacity={shadow}
        onNewsCta={() => {}}
        reduced={false}
        lowFx={false}
      />
    </div>
  );
}
