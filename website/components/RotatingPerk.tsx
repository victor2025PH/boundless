"use client";

// 轮换卖点 pill（首页 Hero rotating 同节奏，/order 与 /pricing 首屏共用）。
// state 隔离在本组件内——2.2s 一次的轮换绝不重渲染宿主页面；滚出视口自动停摆省 CPU。
import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useInView } from "@/lib/useInView";
import { useReducedMotionSafe } from "./fx/useReducedMotionSafe";

export default function RotatingPerk({ items, zh }: { items: { zh: string; en: string }[]; zh: boolean }) {
  const [idx, setIdx] = useState(0);
  const reduced = useReducedMotionSafe();
  const { ref, inView } = useInView<HTMLDivElement>();
  useEffect(() => {
    if (reduced || !inView) return;
    const id = setInterval(() => setIdx((i) => (i + 1) % items.length), 2200);
    return () => clearInterval(id);
  }, [reduced, inView, items.length]);
  const item = items[idx % items.length];
  return (
    <div ref={ref} className="mt-5 flex h-9 items-center justify-center">
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={idx}
          initial={reduced ? false : { opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reduced ? undefined : { opacity: 0, y: -10 }}
          transition={{ duration: 0.3 }}
          className="inline-flex items-center rounded-full border border-neon-cyan/25 bg-white/[0.04] px-5 py-1.5 shadow-[0_0_18px_rgba(34,211,238,0.12)] backdrop-blur-sm"
        >
          <span className="hero-subrotate text-base font-semibold tracking-wide md:text-lg">
            {zh ? item.zh : item.en}
          </span>
        </motion.span>
      </AnimatePresence>
    </div>
  );
}
