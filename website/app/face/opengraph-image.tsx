import { landingOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "AI 实时换脸 · 无界科技";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return landingOgImage("face", "zh");
}
