import type { Config } from "tailwindcss";
// 无界品牌设计令牌 preset。
// 🔧 单一真相仍是 monorepo 的 platform/brand/{tailwind-preset.cjs,tokens.json}，
// 但官网部署到只含 website/ 的服务器时无法引用 website 之外的路径（曾导致 build 失败：
// Cannot find module '../platform/brand/tailwind-preset.cjs'）。故改引 website 自带的
// vendored 副本 vendor/brand/（由 `npm run sync:brand` 从 platform/brand 机械同步，勿手改），
// 让官网自包含、部署零外部依赖。详见 vendor/brand/README.md 与 platform/brand/BRAND_TOKENS.md。
// eslint-disable-next-line @typescript-eslint/no-require-imports
const boundlessPreset = require("./vendor/brand/tailwind-preset.cjs");

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
