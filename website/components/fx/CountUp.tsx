"use client";

import { useEffect, useRef, useState } from "react";
import { useInView, useReducedMotion } from "framer-motion";

interface CountUpProps {
  value: string;
  suffix?: string;
  duration?: number;
  className?: string;
}

/** 数字滚动计数,归位瞬间"锁定":弹跳一下 + 一道掠光扫过,强化"数据落定"的仪式感。
 *  tabular-nums 保证滚动过程数字宽度稳定不抖;reduced-motion 直接显示终值、无闪光。 */
export default function CountUp({ value, suffix = "", duration = 1.6, className }: CountUpProps) {
  const target = parseFloat(value.replace(/[^0-9.]/g, "")) || 0;
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const reduced = useReducedMotion();
  const [display, setDisplay] = useState(reduced ? target : 0);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!inView || reduced) {
      setDisplay(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min((now - start) / (duration * 1000), 1);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(target * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
      else setDone(true);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, reduced, target, duration]);

  const rounded = target % 1 === 0 ? Math.round(display) : display.toFixed(1);

  return (
    <span ref={ref} className={`count-wrap ${done ? "count-done " : ""}${className ?? ""}`}>
      {rounded}
      {suffix}
      {done && <span className="count-sweep" aria-hidden />}
    </span>
  );
}
