"use client";

import { motion } from "framer-motion";

/**
 * 界龙剪影：与 LoongForm 同构的金鳞剪影（非完整角色）。
 * 用于第 3 珠预告、召唤收束「同龙现身」——比挂完整 LoongForm 更轻，且剪影可读性更强。
 */
export default function LoongGhost({
  className = "",
  pulse = true,
  gold = "#f5c542",
}: {
  className?: string;
  pulse?: boolean;
  gold?: string;
}) {
  return (
    <motion.svg
      viewBox="0 0 128 176"
      className={`loong-ghost overflow-visible ${className}`}
      fill="none"
      aria-hidden
      initial={{ opacity: 0, scale: 0.92 }}
      animate={
        pulse
          ? { opacity: [0.28, 0.48, 0.28], scale: [0.98, 1.03, 0.98] }
          : { opacity: 0.4, scale: 1 }
      }
      exit={{ opacity: 0, scale: 0.9 }}
      transition={pulse ? { repeat: Infinity, duration: 2.4, ease: "easeInOut" } : { duration: 0.45 }}
    >
      {/* 尾 */}
      <path d="M86 132 C97 134 104 140 106 148 C114 138 122 136 127 140 C114 152 106 154 105 151" fill={gold} opacity="0.55" />
      {/* 躯干 S */}
      <path
        d="M64 62 C92 70 96 92 70 102 C46 111 40 122 56 133 C66 140 80 139 86 132"
        stroke={gold}
        strokeWidth="22"
        strokeLinecap="round"
        fill="none"
        opacity="0.7"
      />
      {/* 头 */}
      <ellipse cx="64" cy="42" rx="28" ry="24" fill={gold} opacity="0.85" />
      {/* 角 */}
      <path d="M50 26 C43 18 39 8 42 -2 C48 -4 52 8 54 18 Z" fill={gold} opacity="0.75" />
      <path d="M78 26 C85 18 89 8 86 -2 C80 -4 76 8 74 18 Z" fill={gold} opacity="0.75" />
      {/* 鬃剪影 */}
      <path
        d="M64 10 C48 10 36 22 34 36 C26 40 22 50 28 58 C36 62 44 56 46 48 C42 36 50 24 64 24 C78 24 86 36 82 48 C84 56 92 62 100 58 C106 50 102 40 94 36 C92 22 80 10 64 10 Z"
        fill={gold}
        opacity="0.45"
      />
      {/* 珠 */}
      <circle cx="64" cy="92" r="9" fill={gold} opacity="0.95" />
      <circle cx="64" cy="92" r="14" stroke={gold} strokeWidth="1.2" opacity="0.35" />
    </motion.svg>
  );
}
