// 三系视觉令牌（与 brand-assets 头像光环色同源）。
// Tailwind 类名集中在此，避免 Navbar / BrandShowcase / brand 页各写一份。
import type { CategoryKey } from "./brand";

export const CATEGORY_UI: Record<
  CategoryKey,
  {
    label: string;
    /** 产品英文主名（矩阵卡 / 全家福副标）；比 label 略降不透明度，避免抢中文主标 */
    enName: string;
    chip: string;
    ring: string;
    glow: string;
    border: string;
    softBg: string;
  }
> = {
  growth: {
    label: "text-neon-cyan",
    enName: "text-neon-cyan/80",
    chip: "border-neon-cyan/25 bg-neon-cyan/10 text-neon-cyan",
    ring: "group-hover:ring-neon-cyan/40",
    glow: "group-hover:bg-neon-cyan/15",
    border: "hover:border-neon-cyan/40",
    softBg: "bg-neon-cyan/10 text-neon-cyan",
  },
  studio: {
    // 文字用 violet-300：neon-violet(#8b5cf6) 亮度低于另两系强调色，深底上小字发暗看不清；
    // 边框/辉光等非文字装饰仍用 neon-violet 保持系色。
    label: "text-violet-300",
    enName: "text-violet-300/90",
    chip: "border-neon-violet/40 bg-neon-violet/15 text-violet-200",
    ring: "group-hover:ring-neon-violet/40",
    glow: "group-hover:bg-neon-violet/15",
    border: "hover:border-neon-violet/40",
    softBg: "bg-neon-violet/15 text-violet-300",
  },
  lingo: {
    label: "text-amber-300",
    enName: "text-amber-300/80",
    chip: "border-amber-400/25 bg-amber-400/10 text-amber-300",
    ring: "group-hover:ring-amber-400/40",
    glow: "group-hover:bg-amber-400/15",
    border: "hover:border-amber-400/40",
    softBg: "bg-amber-400/10 text-amber-300",
  },
};
