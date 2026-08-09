/**
 * 下载镜像单一真相（下载提速 P0 · 2026-08-08）。
 *
 * 背景：主 VPS 出口 15Mbps，60MB–476MB 的安装包直接从本站发流会吃满整管；
 * Cloudflare R2 桶 `avatarhub` 已镜像全部发布物（出站流量 $0，全球边缘），
 * 实测比主源快 3–5 倍（国内亦然）。
 *
 * 用法：页面上的安装包链接一律写 `dlHref("releases/AvatarHub-Setup-x.y.z.exe")`
 * → 指向本站 `/dl/<path>` 分流入口（app/dl/[...path]/route.ts）：服务端
 * HEAD 探测镜像健康（结果缓存 5 分钟），健康 302 到 R2、不健康回落本站文件。
 * 好处：① 未来发新版忘同步镜像也不会 404（按文件自愈回落）；② 换镜像域名
 * 只改这一处；③ 点击埋点仍在页面组件里，不受影响。
 *
 * 注意：R2 的 r2.dev 公开域会 403 掉部分非浏览器 UA（Python-urllib 实锤）——
 * 分流入口探测时必须带自定义 UA（见 route.ts）。
 *
 * 2026-08-09 换正式边缘域 dl.bd2026.cc（R2 自定义域，走 Cloudflare 正式边缘网络，
 * 免受 r2.dev 公开域限速/国内波动；实测国内单流 10MB/s vs r2.dev 4 流 6.8MB/s）。
 * r2.dev 旧域仍在桶上可用，作为应急回退。
 */
export const R2_PUBLIC_ROOT = "https://dl.bd2026.cc";

/** 允许分流的路径前缀（与主站/R2 桶同布局） */
export const DL_PREFIXES = ["releases/", "downloads/"] as const;

/** 生成分流入口链接：dlHref("downloads/ChatX-Setup-1.0.14.exe") → "/dl/downloads/…" */
export function dlHref(path: string): string {
  return `/dl/${path.replace(/^\/+/, "")}`;
}
