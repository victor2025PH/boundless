/**
 * 可下载桌面客户端注册表（单一事实源）。
 *
 * 驱动四处消费，避免「加一个客户端要改四个地方」：
 *   1. /download 下载中心的客户端卡片矩阵（DownloadHub）
 *   2. 顶栏「下载」下拉（Navbar 桌面端）
 *   3. 移动端菜单「客户端下载」分组（Navbar 移动端）
 *   4. 产品 mega menu 的「提供客户端」徽标（covers 映射）
 *
 * 版本号从各客户端的内容单源引入（releaseNotes / matrixxContent / chatxContent），
 * 本文件不另存版本，防止双源漂移。
 *
 * 合规注意（lib/isolation.ts 是唯一裁判）：gated 客户端（智控）在 public 页面上
 * 只允许出现「产品名 + 中性一句话」，能力细节留在它自己的 noindex 页内（对抗平台
 * 风控类表述全站禁用，见 scripts/check-content-integrity.mjs 黑名单）；
 * 指向 gated 页的链接统一 rel="nofollow"，且不进任何结构化数据（ItemList 等）。
 */
import type { BrandLang, CategoryKey, ProductKey } from "./brand";
import { isGatedSlug } from "./isolation";
import { LATEST_VERSION } from "./releaseNotes";
import { MATRIXX } from "./matrixxContent";
import { CHATX } from "./chatxContent";

export type PlatformStatus = "available" | "coming" | "planned";

export interface ClientApp {
  key: "chatx" | "avatarhub" | "matrixx";
  name: { zh: string; en: string };
  /** 副名（另一语言名 / 引擎名），卡片上以弱化样式展示 */
  subName: string;
  /** 三系归属：决定卡片 accent 色（lib/brand.ts CATEGORIES 同源） */
  family: CategoryKey;
  /** 产品图标 key（ProductIcon 渲染）；无对应产品条目（引擎级客户端）则为 null，用公司 ∞ 标 */
  productIcon: ProductKey | null;
  /** 客户端级专属图标（public 路径）：productIcon 为 null 时优先于公司 ∞ 标（幻境 STUDIO 用） */
  iconSrc?: string;
  /** 卡片一句话。gated 客户端必须用中性口径（见文件头合规注意） */
  tagline: { zh: string; en: string };
  /** 下载页 zh 路径（en 由 localePath 派生） */
  page: string;
  version: string;
  sizeLabel: { zh: string; en: string };
  platforms: { windows: PlatformStatus; macos: PlatformStatus };
  /** 该客户端覆盖哪些产品能力（mega menu「提供客户端」徽标数据源） */
  covers: ProductKey[];
  /** 由 isolation.ts 派生：主站不索引，链接 nofollow，不进结构化数据 */
  gated: boolean;
}

export const CLIENT_APPS: ClientApp[] = [
  {
    key: "chatx",
    name: { zh: "智聊 ChatX", en: "ChatX" },
    subName: "ChatHub",
    family: "growth",
    productIcon: "chatx",
    tagline: {
      zh: "聚合 AI 聊天工作台：全渠道收件箱、AI 自动回复、实时互译（原通译能力已内置）",
      en: "Omni-channel AI chat workspace: unified inbox, AI auto-reply, live translation built in",
    },
    page: "/download/chatx",
    version: CHATX.download.version,
    sizeLabel: CHATX.download.size,
    platforms: { windows: "available", macos: "planned" },
    covers: ["chatx"],
    gated: isGatedSlug("/download/chatx"),
  },
  {
    key: "avatarhub",
    name: { zh: "幻境 STUDIO ", en: "STUDIO" },
    subName: "实时数字人引擎",
    family: "studio",
    productIcon: null,
    iconSrc: "/brand/products/studio.png",
    tagline: {
      zh: "实时数字人引擎：AI 作图、图片 / 视频换脸、直播换脸、变声器、克隆音同传",
      en: "Real-time digital human engine: AI image gen, photo/video face swap, live swap, voice changer, interpreting",
    },
    page: "/download",
    version: LATEST_VERSION,
    sizeLabel: { zh: "45 MB 起", en: "from 45 MB" },
    platforms: { windows: "available", macos: "coming" },
    covers: ["voicex", "livex", "voxx"],
    gated: isGatedSlug("/download"),
  },
  {
    key: "matrixx",
    name: { zh: "智控 MatrixX", en: "MatrixX" },
    subName: "TeleFleet",
    family: "growth",
    productIcon: "matrixx",
    // 中性口径：能力细节与营销话术只在其 noindex 落地页/下载页出现。
    tagline: {
      zh: "Telegram 多账号运营客户端 · 本地部署，数据不出本机",
      en: "Telegram multi-account ops client · runs locally, data stays on-device",
    },
    page: "/matrix/download",
    version: MATRIXX.download.version,
    sizeLabel: MATRIXX.download.size,
    platforms: { windows: "available", macos: "planned" },
    covers: ["matrixx"],
    gated: isGatedSlug("/matrix/download"),
  },
];

/** 提供桌面客户端的产品集合（mega menu ⬇ 徽标查询用） */
export const CLIENT_COVERED_PRODUCTS: ReadonlySet<ProductKey> = new Set(
  CLIENT_APPS.flatMap((c) => c.covers),
);

/** 产品 → 客户端下载页（mega menu 徽标点击 / 未来落地页 CTA 复用） */
export function clientPageForProduct(product: ProductKey): string | null {
  const app = CLIENT_APPS.find((c) => c.covers.includes(product));
  return app ? app.page : null;
}

/** 平台徽章文案 */
export const PLATFORM_LABEL: Record<PlatformStatus, { zh: string; en: string }> = {
  available: { zh: "已上线", en: "Available" },
  coming: { zh: "即将上线", en: "Coming soon" },
  planned: { zh: "规划中", en: "Planned" },
};

/** electron-updater latest.yml 的最小解析（version / path / size）。
 *  格式固定且极简，行级正则即可，不为此引 yaml 依赖；
 *  MatrixX（及未来任何 electron-updater 发布物）的运行时版本校正共用此函数。 */
export function parseLatestYml(text: string): { version: string; filename: string; sizeBytes: number } | null {
  const version = text.match(/^version:\s*(\S+)/m)?.[1];
  if (!version) return null;
  const filename = text.match(/^path:\s*(\S+)/m)?.[1] ?? "";
  const sizeBytes = Number(text.match(/^\s*size:\s*(\d+)/m)?.[1] ?? 0);
  return { version, filename, sizeBytes };
}

/** 字节 → 「507 MB」标签（latest.yml 只有字节数） */
export function formatMb(sizeBytes: number): string {
  return sizeBytes > 0 ? `${Math.round(sizeBytes / 1048576)} MB` : "";
}

export function pf(field: { zh: string; en: string }, lang: BrandLang): string {
  return field[lang];
}
