"use client";

import { useEffect, useState, type CSSProperties } from "react";

const COLORS = ["#67e8f9", "#a78bfa", "#f0abfc", "#34d399", "#fbbf24"];

/** 一次性彩纸爆发:表单提交成功等"成交时刻"的庆祝反馈。
 *  纯 CSS transform/opacity 粒子,播完 1.4s 自卸载;reduced-motion 由样式隐藏。 */
export default function Burst({ count = 18 }: { count?: number }) {
  const [gone, setGone] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setGone(true), 1400);
    return () => window.clearTimeout(id);
  }, []);
  if (gone) return null;
  return (
    <span className="burst" aria-hidden>
      {Array.from({ length: count }, (_, i) => {
        const a = (i / count) * Math.PI * 2 + (i % 2) * 0.35;
        const d = 62 + (i % 5) * 20;
        return (
          <i
            key={i}
            style={
              {
                "--bx": `${Math.round(Math.cos(a) * d)}px`,
                "--by": `${Math.round(Math.sin(a) * d * 0.85)}px`,
                "--bc": COLORS[i % COLORS.length],
                "--bd": `${(i % 4) * 0.06}s`,
              } as CSSProperties
            }
          />
        );
      })}
    </span>
  );
}
