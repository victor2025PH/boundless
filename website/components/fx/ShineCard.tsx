"use client";

import { useRef, type CSSProperties, type MouseEvent, type ReactNode } from "react";

/** 3D 倾斜 + 鼠标跟随光泽容器（自 /pricing NewbiePoster 提取的通用版）。
 *  - 倾斜与 Tilt 同策略：mousemove 驱动 rotateX/Y，CSS transition 平滑，无 rAF 循环；
 *  - 光泽层 = radial-gradient 跟随 --shine-x/--shine-y（默认停在右上，静止也有质感）；
 *  - 触屏（pointer:coarse）与 prefers-reduced-motion 下不倾斜、光泽静止，SSR 零闪烁。 */
export default function ShineCard({
  children,
  className,
  style,
  maxTiltX = 5,
  maxTiltY = 7,
  /** 光泽颜色（"r,g,b" 字符串），默认琥珀金 */
  shineRgb = "252,211,77",
  shineOpacity = 0.14,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  maxTiltX?: number;
  maxTiltY?: number;
  shineRgb?: string;
  shineOpacity?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const okRef = useRef<boolean | null>(null);

  const usable = () => {
    if (okRef.current === null) {
      okRef.current =
        window.matchMedia("(pointer: fine)").matches &&
        !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }
    return okRef.current;
  };

  const onMove = (e: MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el || !usable()) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width;
    const py = (e.clientY - r.top) / r.height;
    el.style.transform = `perspective(1100px) rotateX(${((0.5 - py) * maxTiltX).toFixed(2)}deg) rotateY(${((px - 0.5) * maxTiltY).toFixed(2)}deg)`;
    el.style.setProperty("--shine-x", `${(px * 100).toFixed(1)}%`);
    el.style.setProperty("--shine-y", `${(py * 100).toFixed(1)}%`);
  };

  const onLeave = () => {
    const el = ref.current;
    if (el) el.style.transform = "perspective(1100px) rotateX(0deg) rotateY(0deg)";
  };

  return (
    <div
      ref={ref}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
      className={`transition-transform duration-200 will-change-transform ${className ?? ""}`}
      style={{ transformStyle: "preserve-3d", ...style }}
    >
      {/* 光泽层（跟随鼠标；无鼠标/reduced-motion 下停在右上作静态高光） */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-[inherit] opacity-60"
        style={{
          background: `radial-gradient(600px circle at var(--shine-x, 70%) var(--shine-y, 30%), rgba(${shineRgb},${shineOpacity}), transparent 45%)`,
        }}
      />
      {children}
    </div>
  );
}
