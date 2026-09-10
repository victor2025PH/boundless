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
  async rewrites() {
    // 安装包下载收口（2026-09-10 下载台账）：public/downloads/**.exe 是静态文件，拿到直链就绕开 /dl
    // 分流器——不记台账、不拦爬虫、还吃主站 15Mbps 管道。beforeFiles 在 public/ 文件之前生效，
    // 把安装包改写（URL 不变）到 /dl/downloads/...，由那里统一记 IP 台账 / 403 爬虫 / 限流 / 302 R2。
    // 为什么不用 middleware：Next 14 在 nginx 反代（X-Forwarded-Proto https）下，middleware 的
    // NextResponse.rewrite 会把目标当「外部 URL」去 https 代理 localhost:3000 → EPROTO 500（09-10 首发实锤）。
    // 只动安装包后缀：latest.yml / .blockmap / manifest.json 仍走静态（electron-updater 每次开机都拉）。
    // HEAD 也会进 /dl，那边对本地有的文件直接答 200 + Content-Length，发版脚本/看门狗校验不受影响。
    return {
      beforeFiles: [
        {
          source: "/downloads/:path(.*\\.(?:exe|msi|dmg|pkg|zip|7z|appimage|deb|rpm))",
          destination: "/dl/downloads/:path",
        },
      ],
      afterFiles: [],
      fallback: [],
    };
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
        // 安装包目录不入索引（robots 已 disallow；静态直链 yml/blockmap 也不该被搜索引擎收录）
        source: "/downloads/:path*",
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
