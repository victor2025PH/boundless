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

// hero_cta 实验已结案（2026-08-07，最后一个在跑的实验）：30 天窗口 expose a=345/b=273，
// hero_primary 点击 a=0 / b=2——量级太小不足以统计显著，但 b 是唯一有点击的一组，且其
// 「看演示」文案与按钮落点 #autochat 演示区语义一致（a 组承诺「咨询」落点却是演示）。
// 定稿 b 组文案并回归 content.ts::hero.ctaPrimary 单源；另一个深层信号已进 backlog：
// 全站最显眼的按钮月点击 ≈0，Hero 首屏动线本身需要重新设计，不是换文案能救的。
// abVariant/abExpose 基础设施保留给未来实验（导航层菜单永不进实验，见下方纪律）。

// ⚠️ 导航纪律（2026-08-07 拍板）：导航层菜单永不进 A/B 实验。
// 旧 NAV_BUY 实验（A=购买 / B=看价格，localStorage 按浏览器 50/50 分桶）导致同一用户
// 的电脑与手机看到不同菜单文案（分桶单位是设备不是人），被当成「手机版菜单缺失」上报。
// 两组落点收敛到 /order 后实验只剩纯文案差异，收益低于跨端不一致的体验成本，已结案：
// 统一文案「看价格」，单一事实源在 lib/nav.ts::NAV_PRICING（Navbar/抽屉/页脚/粘性条同源）。
// 实验基础设施（abVariant/abExpose）保留给 Hero 文案等**非导航**位继续使用。
