import type { Metadata } from "next";
import LoongCodex from "@/components/LoongCodex";

/** 图鉴页：个人收集状态（形态/北斗/月鳞/成就）。内容因人而异，不进索引与站点地图；
 *  但保留 OG 图 —— 助力/分享链接在 TG/微信里要有像样的卡片预览。 */
export const metadata: Metadata = {
  title: "图鉴 · 七星聚，龙行无界",
  description: "每日到访点亮北斗，七星聚齐召唤界龙。这里是你的收集档案。",
  robots: { index: false, follow: false },
  alternates: { canonical: "/loong" },
  openGraph: {
    title: "七星聚 · 龙行无界",
    description: "每日点亮一颗星珠，七天召唤界龙许愿——无界科技限定玩法。",
    images: [{ url: "/brand/campaign/teaser-loong-1280x720.png", width: 1280, height: 720 }],
  },
  twitter: {
    card: "summary_large_image",
    images: ["/brand/campaign/teaser-loong-1280x720.png"],
  },
};

export default function LoongPage() {
  return <LoongCodex />;
}
