// 轻量 A/B 实验：本地随机分桶（50/50），localStorage 持久化保证同一访客口径一致。
// 曝光与点击都带 variant 打点（/api/track 原始事件），CTR = cta_click / ab_expose 按桶对比。
import { getLocal, setLocal } from "./safe-storage";
import { track } from "./track";

export type AbVariant = "a" | "b";

const KEY_PREFIX = "ab_";
const exposed = new Set<string>();

/** 取该实验的分桶（首次访问随机 50/50 并落盘）。SSR 阶段返回 "a"（对照组）。 */
export function abVariant(experiment: string): AbVariant {
  if (typeof window === "undefined") return "a";
  const key = KEY_PREFIX + experiment;
  const saved = getLocal(key);
  if (saved === "a" || saved === "b") return saved;
  const v: AbVariant = Math.random() < 0.5 ? "a" : "b";
  setLocal(key, v);
  return v;
}

/** 记一次曝光（每次会话每实验只记一次，避免刷屏）。 */
export function abExpose(experiment: string, variant: AbVariant) {
  if (exposed.has(experiment)) return;
  exposed.add(experiment);
  track("ab_expose", { experiment, variant });
}

/** Hero 主 CTA 文案实验：A=现行方案导向，B=演示钩子导向。 */
export const HERO_CTA_COPY: Record<AbVariant, { zh: string; en: string }> = {
  a: { zh: "咨询 AI 成交方案", en: "Get an AI closing plan" },
  b: { zh: "看 AI 当场成交演示", en: "Watch AI close a deal live" },
};

// ⚠️ 导航纪律（2026-08-07 拍板）：导航层菜单永不进 A/B 实验。
// 旧 NAV_BUY 实验（A=购买 / B=看价格，localStorage 按浏览器 50/50 分桶）导致同一用户
// 的电脑与手机看到不同菜单文案（分桶单位是设备不是人），被当成「手机版菜单缺失」上报。
// 两组落点收敛到 /order 后实验只剩纯文案差异，收益低于跨端不一致的体验成本，已结案：
// 统一文案「看价格」，单一事实源在 lib/nav.ts::NAV_PRICING（Navbar/抽屉/页脚/粘性条同源）。
// 实验基础设施（abVariant/abExpose）保留给 Hero 文案等**非导航**位继续使用。
