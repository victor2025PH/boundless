"use client";

import dynamic from "next/dynamic";

/** 纯客户端装饰彩蛋的懒加载入口：ssr:false 把它们切出服务端渲染与首屏关键 JS，
 *  loading 恒返回 null（装饰组件无占位需求，不影响布局）。
 *  ⚠ 仅限无 SEO 价值的纯装饰组件走这里；正文 section 一律保持 SSR。 */
export const LazyDragonQuest = dynamic(() => import("@/components/DragonQuest"), {
  ssr: false,
  loading: () => null,
});

export const LazyAISprite = dynamic(() => import("@/components/AISprite"), {
  ssr: false,
  loading: () => null,
});
