"use client";

import React, { useEffect, useState } from "react";
import { motion, useTransform, type MotionValue } from "framer-motion";
import {
  ANATOMY,
  DemonEmbers,
  DigitalEye,
  EveArm,
  EveHand,
  NewsHologram,
  SKIN,
  SWAY_MODES,
  armSwayVariants,
  buildBodyVariants,
  handWrapperVariants,
  leftArmVariants,
  rightArmVariants,
  type DemonProps,
  type EyeExpr,
} from "./formShared";

/** 蝠翼展开状态机：folded=收拢（蝠群飞行/未揭示）→ snap=落地弹簧展开（过冲带颤）→ loop=常态扇动 */
type WingPhase = "folded" | "snap" | "loop";

/**
 * 蝠翼：膜翼带三段翼骨，翼根在肩部。
 * 对称关键：静态镜像 scaleX(-1) 放在外层包裹节点（framer 只动内层），
 * 否则 framer 写 transform 时会清掉镜像（存量 bug 同类：右翼从未真正镜像过）。
 * 内层左右用同一份扇动参数，经外层镜像后天然左右对称。
 * phase 驱动“展翼仪式”：变身/蝠群落地时从收拢态弹簧展开一次，再接扇动循环。
 */
const DemonWing = ({ side, glow, flap, phase = "loop" }: { side: "left" | "right"; glow: string; flap: boolean; phase?: WingPhase }) => (
  <div
    className="absolute top-[44px]"
    style={{
      [side === "left" ? "right" : "left"]: "50%",
      zIndex: 0,
      transform: side === "right" ? "scaleX(-1)" : undefined,
      transformOrigin: "50% 50%",
    } as React.CSSProperties}
  >
    <motion.div
      style={{ transformOrigin: "97% 8%" }}
      animate={
        phase === "folded"
          ? { scaleX: 0.12, rotate: 32, opacity: 0.5 }
          : phase === "snap"
            ? { scaleX: 1, rotate: 0, opacity: 1 }
            : flap
              ? { scaleX: [1, 0.8, 1], rotate: [0, 7, 0], opacity: 1 }
              : { scaleX: 1, rotate: 0, opacity: 1 }
      }
      transition={
        phase === "folded"
          ? { duration: 0.16, ease: "easeIn" }
          : phase === "snap"
            ? { type: "spring", stiffness: 300, damping: 12, mass: 0.9 }
            : flap
              ? { repeat: Infinity, duration: 1.7, ease: "easeInOut" }
              : { duration: 0.4 }
      }
    >
      <svg width="72" height="90" viewBox="0 0 72 90" fill="none">
        <defs>
          <linearGradient id={`wing-${side}`} x1="1" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#341826" />
            <stop offset="65%" stopColor="#1a0d14" />
            <stop offset="100%" stopColor="#0d0709" />
          </linearGradient>
        </defs>
        {/* 膜翼轮廓：翼根在右上(70,6)，膜向左下张开，三个扇形凹口成蝠翼指骨 */}
        <path d="M70 6 C42 2 14 10 4 42 C2 52 3 64 9 78 C16 66 22 70 26 80 C31 68 37 72 42 82 C47 70 54 74 60 84 C66 62 69 30 70 6 Z"
          fill={`url(#wing-${side})`} stroke={glow} strokeOpacity="0.45" strokeWidth="1.1" />
        <path d="M68 10 C48 26 30 48 20 76" stroke={glow} strokeOpacity="0.38" strokeWidth="1" fill="none" />
        <path d="M68 10 C52 22 42 44 41 80" stroke={glow} strokeOpacity="0.3" strokeWidth="1" fill="none" />
        <path d="M68 10 C58 20 54 44 60 82" stroke={glow} strokeOpacity="0.24" strokeWidth="1" fill="none" />
      </svg>
    </motion.div>
  </div>
);

/**
 * 尾巴：更长更灵动的两段式——尾根摆动（root）+ 尾梢跟随延迟摆动（tip），形成鞭尾/S 形甩动。
 * 尾根在斗篷底部中偏右探出，尾梢带发光心形箭镞。
 * 交互升级：bias（MotionValue，度）让尾根随鼠标方向偏转（复用视线跟随信号，零重渲染）；
 * flick=true 边沿触发尾梢快速双甩——从循环动画升级成“有反应的活物”。
 */
const DemonTail = ({ glow, sway, bias, flick = false }: { glow: string; sway: boolean; bias?: MotionValue<number>; flick?: boolean }) => {
  const [flicking, setFlicking] = useState(false);
  useEffect(() => {
    if (!flick) return;
    setFlicking(true);
    const t = setTimeout(() => setFlicking(false), 720);
    return () => clearTimeout(t);
  }, [flick]);

  return (
    /* 外层：鼠标方向偏置（MotionValue 直驱）；内层：常态摆动循环。分两层避免 animate 覆盖 style 旋转 */
    <motion.div className="absolute left-[58%] top-[116px] z-0" style={{ rotate: bias, transformOrigin: "6px 6px" }}>
      <motion.div
        style={{ transformOrigin: "6px 6px" }}
        animate={sway ? { rotate: [12, 34, 12] } : { rotate: 20 }}
        transition={sway ? { repeat: Infinity, duration: 2.6, ease: "easeInOut" } : undefined}
      >
        {/* 尾根段：从斗篷底伸出的粗根，渐细，红边线拉开与暗底/光池的对比 */}
        <svg width="44" height="72" viewBox="0 0 44 72" fill="none" className="overflow-visible">
          <path d="M4 2 C18 6 28 16 32 32" stroke={glow} strokeOpacity="0.45" strokeWidth="8" strokeLinecap="round" fill="none" />
          <path d="M4 2 C18 6 28 16 32 32" stroke="#311a26" strokeWidth="6" strokeLinecap="round" fill="none" />
        {/* 尾梢段：以尾根末端(32,32)为轴延迟跟随摆动（鞭尾感）；hover 双甩优先于循环 */}
          <motion.g
            style={{ transformOrigin: "32px 32px" }}
            animate={flicking ? { rotate: [0, 26, -10, 20, 0] } : sway ? { rotate: [-16, 22, -16] } : { rotate: 0 }}
            transition={
              flicking
                ? { duration: 0.68, ease: "easeOut" }
                : sway
                  ? { repeat: Infinity, duration: 2.6, ease: "easeInOut", delay: 0.45 }
                  : undefined
            }
          >
            <path d="M32 32 C38 40 38 47 32 55" stroke={glow} strokeOpacity="0.42" strokeWidth="6.5" strokeLinecap="round" fill="none" />
            <path d="M32 32 C38 40 38 47 32 55" stroke="#2a1620" strokeWidth="4.5" strokeLinecap="round" fill="none" />
            {/* 心形/箭镞尾尖，带辉光 */}
            <path d="M32 53 C27 57 23 62 32 69 C41 62 37 57 32 53 Z" fill={glow} stroke={glow} strokeWidth="0.6" style={{ filter: `drop-shadow(0 0 3px ${glow})` }} />
          </motion.g>
        </svg>
      </motion.div>
    </motion.div>
  );
};


/**
 * 全新恶魔形象（不复用机器人剪影）：悬浮兜帽小恶魔——蝠翼 + 犄角 + 兜帽 + 红眼 +
 * 胸口符文 + 尾巴 + 三节指骨爪。复用眼睛/爪/余烬/姿态变体，保证与站内 IP 同源。
 */
export const DemonForm: React.FC<DemonProps> = ({ mode, isHovered, newsText, newsCta, scrollTilt, flightRotate, gazeX, gazeY, squashY, shadowOpacity, onNewsCta, reduced, lowFx = false, revealed = true }) => {
  const [eyeColor, setEyeColor] = useState(SKIN.demon.eyeColors[0]);
  const [eyeExpression, setEyeExpression] = useState<EyeExpr>("focused");
  const waving = mode === "idle_wave";
  const squashX = useTransform(squashY, (v) => 1 + (1 - v) * 0.55);
  const anim = !reduced && !lowFx;
  const swayOn = anim && SWAY_MODES.has(mode);
  /* 展翼仪式：初次挂载与每次蝠群落地（revealed 上升沿）都从收拢态弹簧展开，仪式感由 spring 过冲提供 */
  const [wingPhase, setWingPhase] = useState<WingPhase>(anim ? "folded" : "loop");
  /* 尾随鼠标：视线信号（±2.4px）放大为尾根 ±14° 偏转，MotionValue 直驱不触发重渲染 */
  const tailBias = useTransform(gazeX, (v) => v * 6);

  useEffect(() => {
    if (!anim) {
      setWingPhase("loop");
      return;
    }
    if (!revealed) {
      setWingPhase("folded");
      return;
    }
    const snapT = setTimeout(() => setWingPhase("snap"), 100);
    const loopT = setTimeout(() => setWingPhase("loop"), 760);
    return () => {
      clearTimeout(snapT);
      clearTimeout(loopT);
    };
  }, [revealed, anim]);

  useEffect(() => {
    const colors = SKIN.demon.eyeColors;
    let i = 0;
    const t = setInterval(() => {
      if (document.hidden) return;
      i = (i + 1) % colors.length;
      setEyeColor(colors[i]);
    }, lowFx ? 8000 : 3500);
    return () => clearInterval(t);
  }, [lowFx]);

  useEffect(() => {
    if (isHovered) return setEyeExpression("happy");
    if (mode === "falling") return setEyeExpression("scared");
    if (mode === "idle_scan") return setEyeExpression("scanning");
    setEyeExpression("focused");
  }, [mode, isHovered]);

  return (
    <motion.div
      className="relative w-32 h-44 flex items-center justify-center [transform-style:preserve-3d]"
      style={{ rotateX: scrollTilt, rotate: flightRotate }}
    >
      <motion.div className="relative w-full h-full" style={{ scaleY: squashY, scaleX: squashX, transformOrigin: "50% 86%" }}>
        <motion.div
          className="relative w-full h-full flex items-center justify-center"
          variants={buildBodyVariants(reduced)}
          animate={mode === "idle_spin" ? "idle_base" : mode}
          transition={{ type: "spring", stiffness: 100, damping: 20 }}
        >
          <NewsHologram active={mode === "idle_news"} text={newsText} cta={newsCta} color={eyeColor} onCta={onNewsCta} />
          {anim && <DemonEmbers color={eyeColor} />}
          {/* 双翼严格镜像、同步扇动（挥手不再收拢单翼，靠手臂 z 层级压过翼）；phase 驱动展翼仪式 */}
          <DemonWing side="left" glow={eyeColor} flap={anim} phase={wingPhase} />
          <DemonWing side="right" glow={eyeColor} flap={anim} phase={wingPhase} />
          <DemonTail glow={eyeColor} sway={anim} bias={tailBias} flick={isHovered && anim} />

          {/* 身体：兜帽斗篷（锯齿下摆）+ 胸口符文 */}
          <div className="absolute left-1/2 top-[30px] z-10 -translate-x-1/2">
            <svg width="112" height="140" viewBox="0 0 112 140" fill="none">
              <defs>
                <linearGradient id="demon-cloak" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3a2130" />
                  <stop offset="55%" stopColor="#23131c" />
                  <stop offset="100%" stopColor="#120a0f" />
                </linearGradient>
                <radialGradient id="demon-hood" cx="50%" cy="38%" r="60%">
                  <stop offset="0%" stopColor="#4a2a39" />
                  <stop offset="70%" stopColor="#241521" />
                  <stop offset="100%" stopColor="#160c13" />
                </radialGradient>
              </defs>
              {/* 斗篷躯干：肩宽收到锯齿下摆 */}
              <path d="M28 54 C24 40 34 28 56 28 C78 28 88 40 84 54 L92 108 C94 118 90 126 92 138 L84 130 L78 138 L70 129 L62 138 L56 130 L50 138 L42 129 L34 138 L28 128 L20 138 C22 126 18 118 20 108 Z"
                fill="url(#demon-cloak)" stroke={eyeColor} strokeOpacity="0.18" strokeWidth="1" />
              {/* 兜帽 */}
              <path d="M56 8 C82 8 96 30 90 54 C80 44 68 40 56 40 C44 40 32 44 22 54 C16 30 30 8 56 8 Z" fill="url(#demon-hood)" stroke={eyeColor} strokeOpacity="0.3" strokeWidth="1" />
              {/* 兜帽内阴影脸洞 */}
              <ellipse cx="56" cy="46" rx="26" ry="20" fill="#0a0509" />
            </svg>

            {/* 犄角：从兜帽顶两侧探出 */}
            <div className="absolute -top-1 left-1/2 -translate-x-1/2" style={{ width: 84 }}>
              {(["left", "right"] as const).map((s) => (
                <svg key={s} width="26" height="30" viewBox="0 0 26 30" fill="none" className="absolute top-0" style={{ [s]: -2, transform: s === "right" ? "scaleX(-1)" : undefined } as React.CSSProperties}>
                  <path d="M20 30 C9 26 3 15 5 3 C5 3 14 6 19 14 C23 20 22 26 20 30 Z" fill="#160b11" stroke={eyeColor} strokeOpacity="0.5" strokeWidth="1" />
                  <path d="M17 25 C10 21 7 14 8 7 C12 10 15 15 16 20 Z" fill={eyeColor} fillOpacity="0.22" />
                </svg>
              ))}
            </div>

            {/* 红眼（脸洞内），带眼神跟随。外层静态居中，内层做 gaze——
                framer 的 x 会覆盖 -translate-x-1/2 类，故居中用 marginLeft 而非 translate 类 */}
            <div className="absolute left-1/2 top-[42px]" style={{ marginLeft: -27 }}>
              <motion.div className="flex gap-[7px]" style={{ x: gazeX, y: gazeY }}>
                <DigitalEye expression={eyeExpression} color={eyeColor} tilt={13} screen={SKIN.demon.eyeScreen} />
                <DigitalEye expression={eyeExpression} color={eyeColor} tilt={-13} screen={SKIN.demon.eyeScreen} />
              </motion.div>
            </div>

            {/* 胸口符文：无界 LOGO 之印——用品牌 mark 的透明 PNG 做 CSS mask，
                填充随眼色脉动的红光（∞ 破框剪影 = 恶魔化的品牌符文，替代原六边形） */}
            <motion.div
              className="absolute left-1/2 top-[88px]"
              style={{ marginLeft: -20 }}
              animate={anim ? { opacity: [0.6, 1, 0.6], scale: [0.94, 1.06, 0.94] } : { opacity: 0.92 }}
              transition={anim ? { repeat: Infinity, duration: 2.4, ease: "easeInOut" } : undefined}
            >
              <div
                aria-hidden
                style={{
                  width: 40,
                  height: 30,
                  backgroundColor: eyeColor,
                  WebkitMaskImage: "url(/brand/logos/boundless-mark-256.png)",
                  maskImage: "url(/brand/logos/boundless-mark-256.png)",
                  WebkitMaskSize: "contain",
                  maskSize: "contain",
                  WebkitMaskRepeat: "no-repeat",
                  maskRepeat: "no-repeat",
                  WebkitMaskPosition: "center",
                  maskPosition: "center",
                  filter: `drop-shadow(0 0 4px ${eyeColor})`,
                  transition: "background-color 1s",
                }}
              />
            </motion.div>
          </div>

          {/* 左臂：完整袖臂（复用 EveArm 骨架，暗黑渐变+红缘线），挥手时整臂提到 z-30
              压过兜帽/蝠翼——修复原细杆臂“只见爪不见臂”的问题；末端接三节指骨爪。
              肩点挂在斗篷肩线（24,92），抬臂经过下巴以下，不遮红眼 */}
          <motion.div
            className="absolute"
            style={{ left: 24, top: 92, zIndex: waving ? 30 : 12, transformOrigin: ANATOMY.shoulderLeft }}
            variants={leftArmVariants}
            animate={mode}
          >
            <motion.div style={{ transformOrigin: ANATOMY.shoulderLeft }} variants={armSwayVariants("left")} animate={swayOn ? "sway" : "still"}>
              <EveArm side="left" stops={SKIN.demon.armStops} edge={`${eyeColor}66`} />
              <motion.div className="eve-hand absolute" style={{ left: 4, top: 40, transformOrigin: "12px 22px" }} variants={handWrapperVariants(reduced)} initial="hidden" animate={waving ? "shown" : "hidden"}>
                <EveHand skin="demon" />
              </motion.div>
            </motion.div>
          </motion.div>
          {/* 右臂：完整袖臂静垂配重（同款镜像） */}
          <motion.div
            className="absolute z-[12]"
            style={{ right: 24, top: 92, transformOrigin: ANATOMY.shoulderRight }}
            variants={rightArmVariants}
            animate={mode}
          >
            <motion.div style={{ transformOrigin: ANATOMY.shoulderRight }} variants={armSwayVariants("right")} animate={swayOn ? "sway" : "still"}>
              <EveArm side="right" stops={SKIN.demon.armStops} edge={`${eyeColor}66`} />
            </motion.div>
          </motion.div>
        </motion.div>
      </motion.div>

      {/* 悬浮暗红光池 */}
      <motion.div className="pointer-events-none absolute left-1/2 top-[97%] -z-20 -translate-x-1/2" style={{ opacity: shadowOpacity }}>
        <motion.div
          className="h-3 w-16 rounded-[50%] blur-[6px]"
          style={{ background: `radial-gradient(ellipse at center, ${eyeColor}55 0%, ${eyeColor}18 55%, transparent 75%)` }}
          animate={anim ? { scaleX: [1, 0.82, 1], opacity: [0.9, 0.55, 0.9] } : { scaleX: 1, opacity: 0.8 }}
          transition={anim ? { repeat: Infinity, duration: 2.5, ease: "easeInOut" } : undefined}
        />
      </motion.div>
    </motion.div>
  );
};

