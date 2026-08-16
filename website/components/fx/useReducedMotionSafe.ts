"use client";

import { useEffect, useState } from "react";
import { useReducedMotion } from "framer-motion";

/** hydration 安全版 useReducedMotion：SSR 与客户端首帧恒为 false，挂载后才反映真实偏好。
 *
 *  为什么需要它：framer 的 useReducedMotion 在服务端恒为 false，而在开启了
 *  「减少动态效果」的客户端上首帧即为 true——任何用它决定**渲染什么**（条件挂载
 *  元素、切换 style/initial）的组件都会两端首帧标记不一致，触发 React 水合报错，
 *  整棵 Suspense 边界被迫回退到客户端渲染（白屏闪烁 + 性能损耗）。
 *
 *  代价是 reduce 用户会先看到一帧「未降级」的静态标记（动画本身尚未启动），
 *  挂载后立即摘除——视觉无感，换来两端标记严格一致。 */
export function useReducedMotionSafe(): boolean {
  const prefers = useReducedMotion();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted && !!prefers;
}
