"use client";

import { useEffect } from "react";

/** 静音阅读区:当价格/FAQ/关于/联系等"专注阅读"区块占据视口中带时,
 *  给 <html> 挂 fx-calm 类,背景动效整体淡化(样式见 globals.css)。
 *  区块 id 不存在的页面自动不生效;监听交叉而非滚动,零每帧成本。 */

const CALM_IDS = ["pricing", "faq", "about", "contact", "order"];

export default function CalmZones() {
  useEffect(() => {
    const targets = CALM_IDS.map((id) => document.getElementById(id)).filter(
      (el): el is HTMLElement => !!el
    );
    if (!targets.length) return;

    const visible = new Set<Element>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) visible.add(e.target);
          else visible.delete(e.target);
        }
        document.documentElement.classList.toggle("fx-calm", visible.size > 0);
      },
      // 区块进入视口中部 40% 区带才算"正在阅读",避免边缘掠过就闪切
      { rootMargin: "-30% 0px -30% 0px", threshold: 0 }
    );
    targets.forEach((el) => io.observe(el));
    return () => {
      io.disconnect();
      document.documentElement.classList.remove("fx-calm");
    };
  }, []);

  return null;
}
