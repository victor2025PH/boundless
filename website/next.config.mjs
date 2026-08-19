/** @type {import('next').NextConfig} */
// 注意：不设 X-Frame-Options / frame-ancestors 限制，Telegram Mini App 需要被 web.telegram.org iframe 承载。
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-DNS-Prefetch-Control", value: "on" },
  { key: "Permissions-Policy", value: "browsing-topics=(), interest-cohort=()" },
];

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  distDir: process.env.NEXT_DIST_DIR || ".next",
  experimental: {
    // better-sqlite3 是原生模块（.node 二进制），不能被 webpack 打包，保持外部 require
    serverComponentsExternalPackages: ["better-sqlite3"],
  },
  async redirects() {
    // 2026-08-19 Token 定价改版：/pricing 已是独立报价决策页（app/pricing），
    // 撤销旧「/pricing → /order 同页」重定向（redirect 优先级高于文件路由，
    // 留着会把新页整个挡住——上线当天实测 title 渲染成购买页才揪出来）。
    // 注意旧 301/308 已被浏览器/搜索引擎缓存一段时间，存量客户端会继续跳 /order
    // （/order 本身可用，无伤）；新访客与爬虫重抓后即到新页。
    return [];
  },
  async headers() {
    return [
      {
        source: "/products/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
      {
        source: "/showcase/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=2592000" }],
      },
      {
        // 品牌图标/开场资源：变更频率低但文件名无内容 hash，1 天新鲜 + 7 天 SWR（不敢用 immutable）
        source: "/brand/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=86400, stale-while-revalidate=604800" }],
      },
      {
        source: "/intro/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=86400, stale-while-revalidate=604800" }],
      },
      {
        // 后台不入索引（robots.ts 已 disallow，这里再加响应头双保险）
        source: "/admin/:path*",
        headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }],
      },
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
