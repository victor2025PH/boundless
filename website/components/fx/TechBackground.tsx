"use client";

import { useEffect, useRef, useState } from "react";
import { motion, useScroll, useTransform, useReducedMotion } from "framer-motion";

/** 全站固定科技背景:星层视差 + 光晕漂移 + 透视网格 + 流星/扫描/上升光点。
 *  全部动效仅用 transform/opacity(GPU 合成);移动端与 reduced-motion 自动降级。
 *  分层结构:
 *  - motion.div(star-wrap/aurora-wrap)  → 滚动进度视差(framer)
 *  -   内层 ref div                      → 鼠标视差 + 滚动速度联动(单 rAF,直接写 transform)
 *  - .fx-transient(流星+天幕光带)        → fx-calm/fx-video 时整组隐去
 *  - html[data-fx="low"]                → 低配设备裁剪(见 globals.css) */
export default function TechBackground() {
  /* useReducedMotion 在 SSR 恒为 false、客户端 reduce 模式下首帧即 true，
   * 直接用它切 style 会造成两端首帧标记不一致（hydration 报错）。
   * 挂载后才采纳其值：SSR 与客户端首帧都按「非 reduce」渲染，水合一致；
   * 挂载后 reduce 用户再摘掉视差 style（视觉上首帧 y=0，无跳变）。 */
  const prefersReduced = useReducedMotion();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const reduced = mounted && !!prefersReduced;
  const { scrollYProgress } = useScroll();
  // 滚动视差:星层慢、光晕快,拉开纵深(reduced-motion 时不启用)
  const starY = useTransform(scrollYProgress, [0, 1], [0, -60]);
  const auroraY = useTransform(scrollYProgress, [0, 1], [0, -150]);

  const starInner = useRef<HTMLDivElement>(null);
  const auroraInner = useRef<HTMLDivElement>(null);
  const gridWrap = useRef<HTMLDivElement>(null);

  /* 鼠标视差(星 1x/晕 2x/网格 3x 反向) + 滚动速度联动(快滚时星空短暂加速下潜)。
   * 单 rAF 循环,数值无变化时跳过样式写入;触屏/低配/reduced 不启用。 */
  useEffect(() => {
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      window.matchMedia("(pointer: coarse)").matches ||
      window.matchMedia("(max-width: 768px)").matches ||
      document.documentElement.getAttribute("data-fx") === "low"
    )
      return;

    let raf = 0;
    let tx = 0, ty = 0; // 目标(鼠标,-0.5..0.5)
    let cx = 0, cy = 0; // 当前(lerp 后)
    let vel = 0; // 平滑后的滚动速度 px/frame
    let lastScroll = window.scrollY;
    let idle = true;

    const onMouse = (e: MouseEvent) => {
      tx = e.clientX / window.innerWidth - 0.5;
      ty = e.clientY / window.innerHeight - 0.5;
    };

    // 空间跃迁(WarpNav 派发):注入一次速度脉冲,后续由平滑滚动的真实速度接管
    const onWarp = (e: Event) => {
      const dir = (e as CustomEvent).detail?.dir === -1 ? -1 : 1;
      vel += dir * 90;
    };

    const tick = () => {
      cx += (tx - cx) * 0.055;
      cy += (ty - cy) * 0.055;
      const y = window.scrollY;
      vel += (y - lastScroll - vel) * 0.14;
      lastScroll = y;
      const v = Math.max(-42, Math.min(42, vel));

      const moving = Math.abs(tx - cx) > 0.0004 || Math.abs(ty - cy) > 0.0004 || Math.abs(v) > 0.15;
      if (moving || !idle) {
        if (starInner.current)
          starInner.current.style.transform = `translate3d(${(-cx * 14).toFixed(2)}px, ${(-cy * 10 - v * 0.5).toFixed(2)}px, 0)`;
        if (auroraInner.current)
          auroraInner.current.style.transform = `translate3d(${(-cx * 28).toFixed(2)}px, ${(-cy * 18 - v * 0.9).toFixed(2)}px, 0)`;
        if (gridWrap.current)
          gridWrap.current.style.transform = `translate3d(${(-cx * 36).toFixed(2)}px, ${(-v * 1.1).toFixed(2)}px, 0)`;
        idle = !moving;
      }
      raf = requestAnimationFrame(tick);
    };

    window.addEventListener("mousemove", onMouse, { passive: true });
    window.addEventListener("bl-warp", onWarp);
    raf = requestAnimationFrame(tick);
    return () => {
      window.removeEventListener("mousemove", onMouse);
      window.removeEventListener("bl-warp", onWarp);
      cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {/* base gradient */}
      <div className="absolute inset-0 bg-ink-950" />

      {/* deep-space stars: two tiled layers, drift + twinkle, scroll+mouse parallax */}
      <motion.div className="star-wrap absolute inset-0" style={reduced ? undefined : { y: starY }}>
        <div ref={starInner} className="absolute inset-0 will-change-transform">
          <div className="star-layer star-layer-1" />
          <div className="star-layer star-layer-2" />
        </div>
      </motion.div>

      {/* drifting aurora blobs, hue slowly cycling, faster parallax */}
      <motion.div className="aurora-wrap" style={reduced ? undefined : { y: auroraY }}>
        <div ref={auroraInner} className="absolute inset-0 will-change-transform">
          <div className="aurora-blob aurora-1" />
          <div className="aurora-blob aurora-2" />
          <div className="aurora-blob aurora-3" />
          <div className="aurora-blob aurora-4" />
        </div>
      </motion.div>

      {/* transient flare layer: shooting stars + occasional sky sweep */}
      <div className="fx-transient absolute inset-0">
        <div className="sky-sweep" />
        <span className="comet comet-1" />
        <span className="comet comet-2" />
        <span className="comet comet-3" />
      </div>

      {/* perspective grid floor + scan band sweeping from horizon */}
      <div ref={gridWrap} className="absolute inset-0 will-change-transform">
        <div className="tech-grid">
          <div className="tech-grid-scan" />
        </div>
      </div>

      {/* rising glow dust */}
      <div className="embers">
        {Array.from({ length: 12 }, (_, i) => (
          <span key={i} className="ember" />
        ))}
      </div>

      {/* vignette + noise(bg-vignette 有日间变体,见 globals.css) */}
      <div className="bg-vignette absolute inset-0" />
      <div className="noise-overlay" />
    </div>
  );
}
