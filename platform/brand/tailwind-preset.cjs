/**
 * 无界科技 BOUNDLESS · Tailwind preset
 * ============================================================
 * 单一真相在 ./tokens.json —— 本文件在加载时 require 它并机械派生，
 * 不手写任何数值，因此与 tokens.json / brand.css 天然一致。
 * 本层零反向依赖：不 import 任何产品/引擎代码。
 *
 * 消费方式（Next.js 官网 tailwind.config.ts）：
 *   import type { Config } from "tailwindcss";
 *   const config: Config = {
 *     presets: [require("../platform/brand/tailwind-preset.cjs")],
 *     content: [...],
 *   };
 *
 * 生成的工具类示例：
 *   bg-growth-500 / text-studio / border-lingo-300   三系光环色
 *   bg-ink-950 / text-ink                            深空底 / 墨色文字
 *   bg-brand-gradient / bg-ring-growth               ∞ 渐变 / 系光环渐变
 *   shadow-primary / rounded-xl / z-modal / ease-spring
 */

const tokens = require("./tokens.json");

/** 递归剔除 tokens.json 中以 _ 开头的注释键（_note/_sources/...）。 */
function stripMeta(node) {
  if (Array.isArray(node)) return node.map(stripMeta);
  if (node && typeof node === "object") {
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if (k.startsWith("_")) continue;
      out[k] = stripMeta(v);
    }
    return out;
  }
  return node;
}

/** 含空格的字体名加引号（与 brand.css 的写法一致）。 */
const quoteFont = (name) => (name.includes(" ") ? `"${name}"` : name);

/** 三系色阶：scale + DEFAULT(=accent)，供 text-growth / bg-growth-500 两种写法。 */
function categoryColor(key) {
  const cat = tokens.categoryAccents[key];
  return { ...stripMeta(cat.scale), DEFAULT: cat.accent };
}

const ink = tokens.brand.ink;

module.exports = {
  theme: {
    extend: {
      colors: {
        // 母品牌 ∞ 渐变 7 色标（bg-brand-cyan 等）
        brand: stripMeta(tokens.brand.palette),
        // 墨色文字 + 深空中性阶（深色界面唯一合法底色）
        ink: {
          DEFAULT: ink.text,
          700: ink["700"],
          800: ink["800"],
          900: ink["900"],
          950: ink["950"],
        },
        // 三系光环色：growth 智连蓝 / studio 幻境紫 / lingo 通达橙
        growth: categoryColor("growth"),
        studio: categoryColor("studio"),
        lingo: categoryColor("lingo"),
        // primary 别名 → 智连蓝（供智控王等 growth 系产品沿用 primary-* 写法迁移）
        primary: categoryColor("growth"),
        // 语义色
        success: stripMeta(tokens.semantic.success),
        warning: stripMeta(tokens.semantic.warning),
        error: stripMeta(tokens.semantic.error),
        info: stripMeta(tokens.semantic.info),
        // 灰阶（slate 仅作冷灰备用，暗底一律走 ink）
        gray: stripMeta(tokens.gray),
        slate: stripMeta(tokens.slate),
      },

      backgroundImage: {
        "brand-gradient": tokens.brand.gradient.css,
        "dark-space": tokens.brand.darkSpace.css,
        "ring-growth": tokens.categoryAccents.growth.ring.css,
        "ring-studio": tokens.categoryAccents.studio.ring.css,
        "ring-lingo": tokens.categoryAccents.lingo.ring.css,
      },

      fontFamily: {
        sans: tokens.typography.fontFamily.sans.map(quoteFont),
        mono: tokens.typography.fontFamily.mono.map(quoteFont),
      },
      fontSize: stripMeta(tokens.typography.fontSize),
      fontWeight: stripMeta(tokens.typography.fontWeight),
      lineHeight: stripMeta(tokens.typography.lineHeight),
      letterSpacing: stripMeta(tokens.typography.letterSpacing),

      spacing: stripMeta(tokens.spacing),
      borderRadius: stripMeta(tokens.radius),
      boxShadow: stripMeta(tokens.shadow),

      transitionDuration: stripMeta(tokens.motion.duration),
      transitionTimingFunction: stripMeta(tokens.motion.easing),

      zIndex: Object.fromEntries(
        Object.entries(stripMeta(tokens.zIndex)).map(([k, v]) => [k, String(v)])
      ),

      screens: stripMeta(tokens.breakpoints),
    },
  },
};
