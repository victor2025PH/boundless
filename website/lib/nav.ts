// 顶层导航单一数据源（2026-08-07 菜单一致性收口）。
//
// 背景：桌面顶栏 / 移动抽屉 / 页脚 / 底部粘性条各自手写菜单，文案与顺序已经漂移
//（桌面「购买/看价格」A/B、移动菜单顺序不同、粘性条叫「价格」），跨端不一致
// 直接造成「手机上找不到看价格」的实际工单。此后加减/改名/调序菜单只改本文件：
//   1. Navbar 桌面横排（NAV_SLOT_ORDER 定序）
//   2. Navbar 移动全屏抽屉（同一 NAV_SLOT_ORDER，保证与桌面同序）
//   3. Footer 主导航的「看价格」项
//   4. StickyCTA 移动端底部粘性条（文案与落点与顶栏同源）
//
// 纪律（2026-08-07 拍板）：导航层菜单永不进 A/B 实验——导航是用户的「地图」，
// 地图不能对不同人画得不一样（NAV_BUY 实验已按此收敛，见 lib/ab.ts）。
import type { BrandLang } from "./brand";

export interface NavLinkItem {
  id: string;
  label: { zh: string; en: string };
  /** zh 路径；en 由 localePath 派生（/en 前缀） */
  path?: string;
  /** 首页锚点（子页面由 Navbar 补首页前缀） */
  anchor?: string;
}

/** 价格入口：全端统一「看价格」→ /pricing 报价决策页（2026-08-19 Token 定价改版：
 *  /pricing 负责「看懂与选择」——档位对比 / Token 费率 / 计算器 / FAQ；/order 退居
 *  纯结算页，/pricing 各 CTA 深链直达。旧收藏 /order 不受影响（仍可直接下单）。
 *  文案沿用 2026-08-07 A/B 定稿「看价格」。 */
export const NAV_PRICING: NavLinkItem = {
  id: "pricing",
  label: { zh: "看价格", en: "Pricing" },
  path: "/pricing",
};

export const NAV_CONTACT: NavLinkItem = {
  id: "contact",
  label: { zh: "联系下单", en: "Contact" },
  anchor: "#contact",
};

export const NAV_BRAND: NavLinkItem = {
  id: "brand",
  label: { zh: "品牌", en: "Brand" },
  path: "/brand",
};

/** 顶栏五个槽位的唯一顺序：桌面横排与移动抽屉都按此渲染，天然同序。 */
export type NavSlot = "products" | "pricing" | "download" | "contact" | "brand";
export const NAV_SLOT_ORDER: NavSlot[] = ["products", "pricing", "download", "contact", "brand"];

export function navLabel(item: NavLinkItem, lang: BrandLang): string {
  return item.label[lang];
}
