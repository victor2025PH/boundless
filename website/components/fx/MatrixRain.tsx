"use client";

import { useEffect, useRef } from "react";

/** 黑客帝国式数字雨（青绿字符流）。轻量 canvas，~18fps 的"步进"观感；
 *  prefers-reduced-motion 下不启动。用 CSS 遮罩淡化，避免压过文字。 */
export default function MatrixRain({ className }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const GLYPHS =
      "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフabcdef0123456789无界科技AI".split("");
    const FONT = 16;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = 0;
    let h = 0;
    let cols = 0;
    let drops: number[] = [];
    let raf = 0;
    let last = 0;

    const resize = () => {
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cols = Math.max(1, Math.floor(w / FONT));
      drops = Array.from({ length: cols }, () => Math.random() * -60);
    };
    resize();
    window.addEventListener("resize", resize);

    const draw = (t: number) => {
      raf = requestAnimationFrame(draw);
      if (t - last < 55) return; // ≈18fps，营造数字雨的步进节奏
      last = t;
      // 半透明黑覆盖形成拖尾
      ctx.fillStyle = "rgba(2,6,14,0.16)";
      ctx.fillRect(0, 0, w, h);
      ctx.font = `${FONT}px monospace`;
      for (let i = 0; i < cols; i++) {
        const x = i * FONT;
        const y = drops[i] * FONT;
        // 头部亮白青
        ctx.fillStyle = "rgba(190,255,244,0.92)";
        ctx.fillText(GLYPHS[(Math.random() * GLYPHS.length) | 0], x, y);
        // 拖尾青绿
        ctx.fillStyle = "rgba(45,212,191,0.5)";
        ctx.fillText(GLYPHS[(Math.random() * GLYPHS.length) | 0], x, y - FONT);
        if (y > h && Math.random() > 0.975) drops[i] = Math.random() * -20;
        drops[i]++;
      }
    };
    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={ref} className={className} aria-hidden />;
}
