// 三系视觉令牌（与 brand-assets 头像光环色同源）。
// Tailwind 类名集中在此，避免 Navbar / BrandShowcase / brand 页各写一份。
import type { CategoryKey } from "./brand";

export const CATEGORY_UI: Record<
  CategoryKey,
  {
    label: string;
    chip: string;
    ring: string;
    glow: string;
    border: string;
    softBg: string;
  }
> = {
  growth: {
    label: "text-neon-cyan",
    chip: "border-neon-cyan/25 bg-neon-cyan/10 text-neon-cyan",
    ring: "group-hover:ring-neon-cyan/40",
    glow: "group-hover:bg-neon-cyan/15",
    border: "hover:border-neon-cyan/40",
    softBg: "bg-neon-cyan/10 text-neon-cyan",
  },
  studio: {
    label: "text-neon-violet",
    chip: "border-neon-violet/25 bg-neon-violet/10 text-neon-violet",
    ring: "group-hover:ring-neon-violet/40",
    glow: "group-hover:bg-neon-violet/15",
    border: "hover:border-neon-violet/40",
    softBg: "bg-neon-violet/10 text-neon-violet",
  },
  lingo: {
    label: "text-amber-300",
    chip: "border-amber-400/25 bg-amber-400/10 text-amber-300",
    ring: "group-hover:ring-amber-400/40",
    glow: "group-hover:bg-amber-400/15",
    border: "hover:border-amber-400/40",
    softBg: "bg-amber-400/10 text-amber-300",
  },
};
