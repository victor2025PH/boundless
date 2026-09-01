/**
 * 首屏浮层调度策略（实施78 P0-3，2026-08-28）。
 *
 * 事故现场（2026-08-28 线上 1440×900 截图）：`/en` 首屏同时压着 5 个浮层——cookie 同意条、
 * 新人 6U 促销卡、客服气泡、吉祥物、「Today's pearl」游戏化气泡，其中 3 个挤在底部三分之一。
 * 一个搜「AI customer service tool」进来的国际买家，在读懂产品是什么之前先被要求做三个决定。
 *
 * 本模块是**唯一**判定处：各浮层组件一律来这里问，别再各自写 pathname 正则——散写的结果
 * 就是「加一个语言/加一个落地页」时漏掉某个组件，浮层又冒出来。
 *
 * 判定口径（刻意保守，只在两类页面上收紧）：
 *   ① **国际路由**（/en、/ko、/ja 及将来的 /vi /th /id）：促销与游戏化是中国消费级增长玩法，
 *      对西方 B2B 买家是可信度减分项 → 关。
 *   ② **GEO 落地页**（/compare*）：从 AI 答案直达的陌生读者，注意力必须留给「我们解决什么」，
 *      任何促销浮层都是抢戏 → 关。
 * 其余中文页面行为完全不变（这些机制在中文消费语境里是有效的，不该一刀切砍掉）。
 *
 * 刻意**不管**的两个浮层：客服气泡（AIChat，是服务入口不是打扰）与 cookie 条（合规必需）。
 * 前者保留、后者的轻量化属视觉层改造，不在策略层处理。
 */

export interface OverlayPolicy {
  /** 促销类浮层（新人礼包海报等） */
  promo: boolean;
  /** 游戏化彩蛋（龙珠集星 / 每日一珠） */
  gamification: boolean;
}

/** 非中文路由前缀。新增语言在此登记即可，各浮层组件零改动。 */
const INTERNATIONAL_ROUTE = /^\/(en|ko|ja|vi|th|id)(\/|$)/;
/** GEO 落地页（zh 与 /en 两侧成对） */
const GEO_LANDER = /^\/(en\/)?compare(\/|$)/;

export function overlayPolicy(pathname: string | null | undefined): OverlayPolicy {
  const p = pathname || "/";
  const quiet = INTERNATIONAL_ROUTE.test(p) || GEO_LANDER.test(p);
  return { promo: !quiet, gamification: !quiet };
}

/** 带 campaign 参数即视为投放/深链来的冷流量。 */
const CAMPAIGN_PARAMS = ["utm_source", "utm_medium", "utm_campaign", "ref"];

/**
 * 冷外部入口判定（供开场动画跳过用）。
 *
 * 「带着问题来的人不该先看品牌片」：从搜索引擎 / AI 答案 / 广告 / 社媒点进来的访客，
 * 目标明确、耐心最低，5-10 秒的开场动画对他们是纯损耗（且是最贵的那批流量）。
 * 直接输入网址或站内跳转的访客不受影响——品牌片对他们仍然有价值。
 *
 * SSR 安全：无 window 时返回 false（保持既有行为，绝不因判定失败而改变默认体验）。
 */
export function isColdExternalEntry(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const q = new URLSearchParams(window.location.search);
    if (CAMPAIGN_PARAMS.some((k) => q.get(k))) return true;
    const ref = document.referrer;
    if (!ref) return false; // 直接访问 / 书签：留住品牌片
    const host = new URL(ref).hostname;
    return host !== window.location.hostname;
  } catch {
    return false;
  }
}
