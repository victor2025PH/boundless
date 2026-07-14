"use client";

import { motion, useReducedMotion } from "framer-motion";

/** 区块光弧分隔线:进入视口时从中心"画出"一条发光线,中央菱形节点随后点亮。
 *  一次性触发,纯 transform/opacity;reduced-motion 时静态显示。 */
export default function SectionDivider() {
  const reduced = useReducedMotion();

  if (reduced) {
    return (
      <div aria-hidden className="relative mx-auto max-w-5xl px-5">
        <div className="divider-beam" />
        <span className="divider-node" />
      </div>
    );
  }

  return (
    <div aria-hidden className="relative mx-auto max-w-5xl px-5">
      <motion.div
        className="divider-beam"
        initial={{ scaleX: 0, opacity: 0 }}
        whileInView={{ scaleX: 1, opacity: 1 }}
        viewport={{ once: true, margin: "-100px" }}
        transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
      />
      <motion.span
        className="divider-node"
        initial={{ scale: 0, opacity: 0 }}
        whileInView={{ scale: 1, opacity: 1 }}
        viewport={{ once: true, margin: "-100px" }}
        transition={{ duration: 0.5, delay: 0.55, ease: [0.34, 1.56, 0.64, 1] }}
      />
    </div>
  );
}
