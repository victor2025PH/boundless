import { landingOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "AI 음성 클로닝 · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

// 韩语页 OG 图复用英文模板（分享卡片以品牌视觉为主，正文语言由页面本身承载）。
export default function OgImage() {
  return landingOgImage("voice", "en");
}
