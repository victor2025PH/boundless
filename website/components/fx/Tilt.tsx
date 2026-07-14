"use client";

import { useRef, type ReactNode, type MouseEvent } from "react";

/** 3D 悬浮倾斜容器:鼠标位置驱动 rotateX/Y(默认 ≤4°),CSS transition 负责平滑,
 *  无 rAF 循环。触屏设备不会触发 mousemove 悬停流,reduced-motion 下直接静止。 */
export default function Tilt({
  children,
  max = 4,
  className,
}: {
  children: ReactNode;
  max?: number;
  className?: string;
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
    const px = (e.clientX - r.left) / r.width - 0.5;
    const py = (e.clientY - r.top) / r.height - 0.5;
    el.style.transform = `perspective(900px) rotateX(${(-py * max).toFixed(2)}deg) rotateY(${(px * max).toFixed(2)}deg)`;
  };

  const onLeave = () => {
    if (ref.current) ref.current.style.transform = "";
  };

  return (
    <div ref={ref} className={`tilt-3d ${className ?? ""}`} onMouseMove={onMove} onMouseLeave={onLeave}>
      {children}
    </div>
  );
}
