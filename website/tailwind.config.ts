import type { Config } from "tailwindcss";
// 无界品牌设计令牌 preset（单一真相 platform/brand/tokens.json 的 Tailwind 派生产物）。
// 仅新增 bl-*/growth/studio/lingo 等品牌类，本文件下方 theme.extend 的 fontFamily.sans
// 会覆盖 preset 字体，故不改动官网现有字体栈（Montserrat 待 webfont 加载后再切）。
// 详见 platform/brand/BRAND_TOKENS.md。
// eslint-disable-next-line @typescript-eslint/no-require-imports
const boundlessPreset = require("../platform/brand/tailwind-preset.cjs");

const config: Config = {
  presets: [boundlessPreset],
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#05060f",
          900: "#0a0c1b",
          800: "#11132a",
          700: "#1a1d3a",
        },
        neon: {
          cyan: "#22d3ee",
          blue: "#3b82f6",
          violet: "#8b5cf6",
          pink: "#ec4899",
        },
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Microsoft YaHei",
          "PingFang SC",
          "sans-serif",
        ],
      },
      backgroundImage: {
        "grid-glow":
          "radial-gradient(circle at 20% 10%, rgba(139,92,246,0.18), transparent 40%), radial-gradient(circle at 80% 0%, rgba(34,211,238,0.16), transparent 45%)",
      },
      keyframes: {
        float: {
          "0%, 100%": { transform: "translateY(0px)" },
          "50%": { transform: "translateY(-12px)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "0% 50%" },
          "100%": { backgroundPosition: "200% 50%" },
        },
      },
      animation: {
        float: "float 6s ease-in-out infinite",
        shimmer: "shimmer 6s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
