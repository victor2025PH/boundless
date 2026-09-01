"use client";

import { usePathname } from "next/navigation";
import { LazyAISprite, LazyDragonQuest } from "@/components/fx/LazyFx";
import { overlayPolicy } from "@/lib/overlay-policy";

/**
 * 首页彩蛋层的浮层调度包装（实施78 P0-3）。
 *
 * SiteHome 是服务端组件，拿不到 pathname；把「哪些彩蛋可以出」这一个判断收进这个客户端
 * 薄壳，SiteHome 保持纯展示、不变成客户端组件（否则整棵首页树都要下发到浏览器）。
 *
 * 吉祥物（AISprite）刻意保留：它是在线顾问入口，属服务而非促销。
 * 龙珠集星（DragonQuest）按策略关——国际路由上它是 B2B 可信度的减分项。
 * <768px 整体隐藏的既有约定不变（390px 屏上五层浮动会互相叠压，见 SiteHome 原注）。
 */
export default function HomeEasterEggs() {
  const policy = overlayPolicy(usePathname());
  return (
    <div className="hidden md:contents">
      <LazyAISprite />
      {policy.gamification ? <LazyDragonQuest /> : null}
    </div>
  );
}
