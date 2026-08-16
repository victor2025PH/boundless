"use client";

import { motion } from "framer-motion";
import { ReactNode } from "react";
import { useReducedMotionSafe } from "./useReducedMotionSafe";

interface RevealProps {
  children: ReactNode;
  delay?: number;
  y?: number;
  className?: string;
  once?: boolean;
  /** 首屏元素专用:纯 CSS 入场(不从 opacity:0 起步、不依赖 JS 水合),
   *  SSR HTML 首帧即可见/可绘制,LCP 不再被入场动画推迟。折叠线下的内容勿用。 */
  eager?: boolean;
}

export default function Reveal({ children, delay = 0, y = 28, className, once = true, eager }: RevealProps) {
  // 水合安全版：SSR/客户端首帧一致，挂载后 reduce 用户切静态结构
  const reduced = useReducedMotionSafe();

  if (reduced) {
    return <div className={className}>{children}</div>;
  }

  if (eager) {
    return (
      <div
        className={`reveal-eager ${className ?? ""}`}
        style={delay ? { animationDelay: `${delay}s` } : undefined}
      >
        {children}
      </div>
    );
  }

  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y, filter: "blur(6px)" }}
      whileInView={{ opacity: 1, y: 0, filter: "blur(0px)" }}
      viewport={{ once, margin: "-60px" }}
      transition={{ duration: 0.55, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </motion.div>
  );
}
