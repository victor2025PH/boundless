"use client";

import React, { useEffect, useState } from "react";
import { motion, useTransform, type MotionValue } from "framer-motion";
import {
  ANATOMY,
  DemonEmbers,
  DemonFangs,
  DemonHorns,
  DigitalEye,
  EveArm,
  EveHand,
  LoongAntlers,
  NeuralNeck,
  NewsHologram,
  SKIN,
  SWAY_MODES,
  armSwayVariants,
  buildBodyVariants,
  handWrapperVariants,
  leftArmVariants,
  rightArmVariants,
  type BotMode,
  type EyeExpr,
  type Skin,
} from "./formShared";

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
  /** 低配挡位（html[data-fx="low"]，与全站背景特效同一判定）：裁剪常驻装饰动画 */
  lowFx?: boolean;
  /** 皮肤：normal | demon（隐藏彩蛋，纯外观） */
  skin?: Skin;
};

/** 机器人本体：头 / 颈 / 蛋形身体 / 双臂（左臂带五指手掌）/ 推进器光焰 / 悬浮光池。
 *  已导出：/robot-stage 素材舞台页复用同一实现，保证站内外 IP 形象一致。 */
export const EveBot: React.FC<EveBotProps> = ({ mode, isHovered, newsText, newsCta, scrollTilt, flightRotate, gazeX, gazeY, squashY, shadowOpacity, onNewsCta, reduced, lowFx = false, skin = "normal" }) => {
  const theme = SKIN[skin];
  const isDemon = skin === "demon";
  const isLoong = skin === "loong";
  const [eyeExpression, setEyeExpression] = useState<EyeExpr>("normal");
  const [eyeColor, setEyeColor] = useState(theme.eyeColors[0]);
  const [spinRotation, setSpinRotation] = useState(0);
  const [wink, setWink] = useState(false);
  const waving = mode === "idle_wave";
  /* 着陆回弹的挤压-拉伸：Y 压缩时 X 反向微胖，卡通物理更可信 */
  const squashX = useTransform(squashY, (v) => 1 + (1 - v) * 0.55);
  const swayOn = !reduced && !lowFx && SWAY_MODES.has(mode);

  /* 眼睛霓虹色轮换（随皮肤切换取色池；页面隐藏时暂停；低配挡放慢一倍减少重绘） */
  useEffect(() => {
    const colors = theme.eyeColors;
    let i = 0;
    setEyeColor(colors[0]);
    const t = setInterval(() => {
      if (document.hidden) return;
      i = (i + 1) % colors.length;
      setEyeColor(colors[i]);
    }, lowFx ? 8000 : 4000);
    return () => clearInterval(t);
  }, [lowFx, theme]);

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
          {/* 余烬粒子：恶魔=红火星，祥龙=金瑞粉（同一组件换色） */}
          {(isDemon || isLoong) && !lowFx && !reduced && <DemonEmbers color={eyeColor} />}
          <motion.div
            className={`relative z-30 w-[4.4rem] h-[3.2rem] rounded-[50%_50%_45%_45%] shadow-[inset_0_-2px_6px_rgba(0,0,0,0.15),0_5px_15px_rgba(0,0,0,0.1)] ${isDemon || isLoong ? "overflow-visible" : "overflow-hidden"} flex items-center justify-center`}
            style={{ background: theme.head }}
            animate={{ y: mode === "idle_dance" ? -2 : -8 }}
          >
            {isDemon && <DemonHorns color={eyeColor} />}
            {isLoong && <LoongAntlers color={eyeColor} />}
            <div className="absolute top-1 left-1/4 w-1/2 h-1/2 rounded-full blur-[2px]" style={{ backgroundColor: theme.headHi }} />
            <div className="w-[88%] h-[75%] bg-black rounded-[45%_45%_50%_50%] flex items-center justify-center gap-3 relative shadow-[inset_0_0_10px_rgba(255,255,255,0.15)] overflow-hidden border border-zinc-800/50 mt-1">
              {/* 眼神跟随：整对眼睛朝鼠标方向微移（MotionValue 直驱，零重渲染） */}
              <motion.div className="flex gap-3" style={{ x: gazeX, y: gazeY }}>
                <DigitalEye expression={wink ? "wink" : eyeExpression} color={eyeColor} tilt={theme.eyeTilt} screen={theme.eyeScreen} />
                <DigitalEye expression={eyeExpression} color={eyeColor} tilt={-theme.eyeTilt} screen={theme.eyeScreen} />
              </motion.div>
              {isDemon && <DemonFangs />}
            </div>
          </motion.div>
          <NeuralNeck color={eyeColor} />
          <div className="relative z-20 w-[4rem] h-[5.5rem] mt-[-10px]">
            <div className="w-full h-full relative overflow-hidden" style={{ background: theme.body, borderRadius: "30% 30% 50% 50% / 20% 20% 80% 80%", boxShadow: theme.bodyShadow }}>
              <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[90%] h-5 to-transparent rounded-b-full opacity-40 blur-[1px]" style={{ backgroundImage: `linear-gradient(to bottom, ${isDemon ? "#7f1d2e" : isLoong ? "#f5c542" : "#a5f3fc"}, transparent)` }} />
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
              <EveArm side="left" stops={theme.armStops} />
              <motion.div
                className="eve-hand absolute"
                style={{ left: 4, top: 40, transformOrigin: "12px 22px" }}
                variants={handWrapperVariants(reduced)}
                initial="hidden"
                animate={waving ? "shown" : "hidden"}
              >
                <EveHand skin={skin} />
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
              <EveArm side="right" stops={theme.armStops} />
            </motion.div>
          </motion.div>
          {/* 推进器：用 framer 的 x 居中而非 translate 类（会被 transform 动画覆盖，存量bug） */}
          <motion.div className="absolute top-[88%] left-[49%] -z-10" style={{ x: "-50%" }} animate={{ opacity: mode === "flying" || mode === "idle_dance" ? 0.8 : 0.4, scaleY: mode === "flying" ? 1.5 : 0.8 }}>
            <div className="w-6 h-12 rounded-full blur-[6px] transition-colors duration-1000" style={{ background: `linear-gradient(to top, transparent, ${eyeColor}, white)` }} />
          </motion.div>
        </motion.div>
      </motion.div>
      {/* 悬浮光池：机身辉光在“地面”的反射（深色页面上用光而非阴影表达高度），
          呼吸节奏与身体 2.5s 浮动同步，飞行/拖拽越远越暗；低配/reduced 静态化 */}
      <motion.div className="pointer-events-none absolute left-1/2 top-[97%] -z-20 -translate-x-1/2" style={{ opacity: shadowOpacity }}>
        <motion.div
          className="h-3 w-16 rounded-[50%] blur-[6px] transition-colors duration-1000"
          style={{ background: `radial-gradient(ellipse at center, ${eyeColor}55 0%, ${eyeColor}18 55%, transparent 75%)` }}
          animate={reduced || lowFx ? { scaleX: 1, opacity: 0.8 } : { scaleX: [1, 0.82, 1], opacity: [0.9, 0.55, 0.9] }}
          transition={reduced || lowFx ? undefined : { repeat: Infinity, duration: 2.5, ease: "easeInOut" }}
        />
      </motion.div>
    </motion.div>
  );
};

